"""Actor feedback uses exact local identities and real counter observations."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from ctn_data import agent_feedback as feedback
from ctn_data.db import connect, initialize


class AgentFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'feedback.sqlite'
        initialize(self.db)
        self.conn = connect(self.db)
        self.addCleanup(self.conn.close)
        self.now = datetime(2026, 10, 20, 2, 0, tzinfo=timezone.utc)
        self.agent = {'id': 'maya', 'account_id': 77, 'username': 'maya_collins'}
        self.data = {'drafts': {}, 'memory': {}}

    def at(self, minutes=0):
        return (self.now + timedelta(minutes=minutes)).isoformat()

    def post(self, post_id=1, author_id=77, **changes):
        row = {'post_id': post_id, 'author_id': author_id, 'kind': 'post',
               'body': f'Library update {post_id}', 'created_at': self.at(-30),
               'first_seen': self.at(-30), 'last_seen': self.at(), **changes}
        self.conn.execute('INSERT INTO posts(' + ','.join(row) + ') VALUES(' + ','.join('?' for _ in row) + ')', tuple(row.values()))

    def sample(self, post_id=1, minutes=0, **changes):
        row = {'post_id': post_id, 'observed_at': self.at(minutes),
               'like_count': 0, 'reply_count': 0, 'repost_count': 0, 'view_count': 0, **changes}
        cursor = self.conn.execute('INSERT INTO counter_snapshots(' + ','.join(row) + ') VALUES(' + ','.join('?' for _ in row) + ')', tuple(row.values()))
        return cursor.lastrowid

    def draft(self, number=1, **changes):
        row = {'id': f'draft:{number}', 'agent_id': 'maya', 'account_id': 77,
               'action': 'post', 'state': 'published', 'post_id': number,
               'created_at': self.at(-30), 'published_at': self.at(-20), **changes}
        self.data['drafts'][row['id']] = row
        return row

    def snapshot(self, **kwargs):
        return feedback.snapshot_for(self.conn, self.data, self.agent, self.now, **kwargs)

    def test_unbound_has_unknown_stats_and_only_general_score_link(self):
        self.agent['account_id'] = None
        result = self.snapshot()
        self.assertEqual(result['status'], 'unbound')
        self.assertIsNone(result['known_post_count'])
        self.assertTrue(all(value is None for value in result['metrics'].values()))
        self.assertIsNone(result['links']['analysis'])
        self.assertEqual(result['links']['score'], '/score')

    def test_existing_relationship_checks_are_not_counted_as_new_interactions(self):
        self.draft(1, action='follow', post_id=None, target_account_id=98,
                   relationship_already_applied=True, result={'already_applied': True})
        self.draft(2, action='unlike', post_id=None, target_post_id=99)
        result = self.snapshot()
        self.assertEqual(result['verified_record_count'], 2)
        self.assertEqual(result['confirmed_action_count'], 1)
        self.assertEqual(result['action_counts']['follow'], 0)
        self.assertEqual(result['action_counts']['unlike'], 1)
        self.assertEqual(result['known_post_count'], 0)
        follow = next(row for row in result['actions'] if row['action'] == 'follow')
        self.assertTrue(follow['already_applied'])
        self.assertEqual(follow['performance_scope'], 'relationship')
        self.assertIsNone(follow['performance'])

    def test_deleted_posts_remain_historical_without_inflating_current_metrics(self):
        self.post(1)
        self.post(2)
        self.sample(1, like_count=100)
        self.sample(2, like_count=2)
        self.draft(5, action='delete_post', post_id=None, target_post_id=1, origin='manual')
        self.data['rate'] = {'remote_deleted_posts': {'77:1': {
            'account_id': 77, 'post_id': 1, 'confirmed_at': self.at(), 'remote_deleted': True}}}
        result = self.snapshot()
        self.assertEqual(result['known_post_count'], 2)
        self.assertEqual(result['window']['post_count'], 1)
        self.assertEqual(result['metrics']['like_count'], 2)
        historical = next(row for row in result['recent_posts'] if row['post_id'] == 1)
        self.assertEqual(historical['remote_deleted_at'], self.at())
        self.assertEqual(historical['latest_counts']['like_count'], 100)
        deletion = result['actions'][0]
        self.assertEqual(deletion['action'], 'delete_post')
        self.assertEqual(deletion['performance_scope'], 'deletion')
        self.assertIsNone(deletion['performance'])
        self.assertIsNone(deletion['post_id'])
        self.assertEqual(deletion['origin'], 'manual')

    def test_deletion_tombstone_invalidates_feedback_cache_without_retained_draft(self):
        self.post(1)
        self.sample(1, like_count=5)
        cache = feedback.FeedbackCache(ttl_seconds=300)
        self.assertEqual(self.snapshot(cache=cache)['metrics']['like_count'], 5)
        self.data['rate'] = {'remote_deleted_posts': {'77:1': {
            'account_id': 77, 'post_id': 1, 'confirmed_at': self.at(), 'remote_deleted': True}}}
        self.assertIsNone(self.snapshot(cache=cache)['metrics']['like_count'])
        self.assertIsNotNone(self.snapshot(cache=cache)['recent_posts'][0]['remote_deleted_at'])

    def test_account_id_defines_scope_not_username_or_team_total(self):
        self.post(1)
        self.post(2, author_id=78)
        self.sample(1, like_count=2)
        self.sample(2, like_count=999)
        result = self.snapshot()
        self.assertEqual(result['known_post_count'], 1)
        self.assertEqual([row['post_id'] for row in result['recent_posts']], [1])
        self.assertEqual(result['metrics']['like_count'], 2)
        self.assertEqual(result['links']['posts'], '/posts?feed=all&author_id=77')
        self.assertIn('author_id=77', result['links']['analysis'])

    def test_missing_samples_remain_unknown_and_real_zero_remains_zero(self):
        self.post()
        result = self.snapshot()
        self.assertEqual(result['window']['sampled_posts'], 0)
        self.assertIsNone(result['recent_posts'][0]['interaction_count'])
        self.assertIsNone(result['latest_sampled_at'])
        self.sample()
        result = self.snapshot()
        self.assertEqual(result['window']['sampled_posts'], 1)
        self.assertEqual(result['metrics']['like_count'], 0)
        self.assertEqual(result['recent_posts'][0]['interaction_count'], 0)

    def test_partial_window_exposes_coverage_without_claiming_total(self):
        self.post(1)
        self.post(2)
        self.sample(1, like_count=0, view_count=None)
        self.sample(2, like_count=None, view_count=30)
        result = self.snapshot()
        self.assertIsNone(result['metrics']['like_count'])
        self.assertEqual(result['observed_metric_subtotals']['like_count'], 0)
        self.assertEqual(result['metric_coverage']['like_count'], 1)
        self.assertIsNone(result['metrics']['view_count'])
        self.assertEqual(result['observed_metric_subtotals']['view_count'], 30)
        self.assertEqual(result['metrics']['reply_count'], 0)

    def test_latest_timestamp_wins_over_ingestion_order_and_keeps_negative_delta(self):
        self.post()
        latest_id = self.sample(minutes=0, like_count=2, reply_count=7, view_count=50)
        previous_id = self.sample(minutes=-10, like_count=5, reply_count=None, view_count=40)
        row = self.snapshot()['recent_posts'][0]
        self.assertEqual(row['provenance']['counter_snapshot_id'], latest_id)
        self.assertEqual(row['provenance']['previous_counter_snapshot_id'], previous_id)
        self.assertEqual(row['counter_delta']['like_count'], -3)
        self.assertIsNone(row['counter_delta']['reply_count'])
        self.assertEqual(row['counter_delta']['view_count'], 10)
        self.assertEqual(row['delta_elapsed_seconds'], 600)

    def test_no_baseline_does_not_invent_growth(self):
        self.post()
        self.sample(like_count=9)
        row = self.snapshot()['recent_posts'][0]
        self.assertTrue(all(value is None for value in row['counter_delta'].values()))
        self.assertIsNone(row['previous_sampled_at'])
        self.assertIsNone(row['delta_elapsed_seconds'])

    def test_equivalent_timestamp_spellings_are_not_two_change_observations(self):
        self.post()
        self.sample(observed_at='2026-10-20T02:00:00Z', like_count=9)
        self.sample(observed_at='2026-10-20T13:00:00+11:00', like_count=8)
        row = self.snapshot()['recent_posts'][0]
        self.assertIsNone(row['previous_sampled_at'])
        self.assertIsNone(row['counter_delta']['like_count'])

    def test_like_target_feedback_never_becomes_authored_performance(self):
        self.post(1)
        self.post(9, author_id=98)
        self.sample(1, like_count=2)
        self.sample(9, like_count=1000)
        self.draft(2, action='like', post_id=9, target_post_id=9)
        result = self.snapshot()
        self.assertEqual(result['metrics']['like_count'], 2)
        action = result['actions'][0]
        self.assertEqual(action['performance_scope'], 'target_context')
        self.assertEqual(action['performance']['latest_counts']['like_count'], 1000)
        self.assertEqual(action['performance']['post_id'], 9)
        self.assertIsNone(action['performance']['draft_id'])

    def test_only_current_bound_account_confirmed_actions_are_counted(self):
        self.draft(1)
        self.draft(2, state='pending')
        self.draft(3, state='uncertain')
        self.draft(4, account_id=78)
        self.draft(5, account_id=None)
        self.draft(6, agent_id='ethan')
        self.draft(7, action='skip')
        result = self.snapshot()
        self.assertEqual(result['confirmed_action_count'], 1)
        self.assertEqual(result['action_counts']['post'], 1)
        self.assertEqual([row['draft_id'] for row in result['actions']], ['draft:1'])
        self.assertEqual(result['action_history_scope'], 'retained_local_outbox')

    def test_reply_repost_and_manually_created_posts_are_precisely_linked(self):
        self.post(1, kind='reply', reply_to_post_id=900)
        self.post(2, kind='repost', repost_of_post_id=901)
        self.post(3)
        self.draft(1, action='reply', target_post_id=900)
        self.draft(2, action='repost', target_post_id=901)
        result = self.snapshot()
        self.assertTrue(all(row['performance_scope'] == 'own_post' for row in result['actions']))
        self.assertEqual({row['performance_post_id'] for row in result['actions']}, {1, 2})
        manual = next(row for row in result['recent_posts'] if row['post_id'] == 3)
        self.assertIsNone(manual['draft_id'])
        self.assertEqual(manual['links']['post'], '/posts?post=3')

    def test_mismatched_author_cannot_be_claimed_as_own_post(self):
        self.post(1, author_id=98)
        self.sample(1, view_count=900)
        self.draft(1)
        result = self.snapshot()
        self.assertEqual(result['known_post_count'], 0)
        self.assertEqual(result['actions'][0]['performance_scope'], 'unknown')
        self.assertIsNone(result['actions'][0]['performance'])
        self.assertIsNone(result['metrics']['view_count'])

    def test_window_is_bounded_but_older_completed_action_is_still_joined(self):
        for number in range(1, 31):
            self.post(number, created_at=self.at(number - 40))
        self.sample(1, like_count=3)
        self.draft(1)
        result = self.snapshot()
        self.assertEqual(result['known_post_count'], 30)
        self.assertEqual(result['window']['post_count'], 20)
        self.assertEqual(len(result['recent_posts']), 20)
        self.assertNotIn(1, [row['post_id'] for row in result['recent_posts']])
        self.assertEqual(result['actions'][0]['performance']['latest_counts']['like_count'], 3)
        self.assertIsNone(result['metrics']['like_count'])

    def test_team_score_is_context_only_with_exact_provenance_and_no_estimate(self):
        self.conn.execute('INSERT INTO score_snapshots(observed_at,total_decimal,source,note) VALUES(?,?,?,?)',
                          (self.at(-20), '123.45', 'browser:portal', 'team'))
        self.conn.execute('INSERT INTO score_snapshots(observed_at,total_decimal,source,note) VALUES(?,?,?,?)',
                          (self.at(-40), '99.9', 'historical:manual', 'older observation imported later'))
        result = self.snapshot()
        official = result['official']
        self.assertEqual(official['status'], 'unattributed')
        self.assertIsNone(official['points_decimal'])
        self.assertEqual(official['events'], [])
        self.assertEqual(official['team_snapshot']['total_decimal'], '123.45')
        self.assertEqual(official['team_snapshot']['observed_at'], self.at(-20))
        self.assertEqual(official['team_snapshot']['source'], 'browser:portal')
        self.assertEqual(official['team_snapshot']['basis'], 'latest_observed_snapshot')
        self.assertEqual(official['team_snapshot']['attribution'], 'team_only')
        self.assertFalse(result['score_estimate']['is_official'])
        self.assertIsNone(result['score_estimate']['points_decimal'])

    def test_cache_avoids_poll_queries_then_expires_and_actions_invalidate_it(self):
        self.post()
        self.sample(like_count=2)
        cache = feedback.FeedbackCache(ttl_seconds=30)
        statements = []
        self.conn.set_trace_callback(statements.append)
        first = self.snapshot(cache=cache)
        self.assertTrue(statements)
        self.sample(minutes=1, like_count=6)
        statements.clear()
        cached = feedback.snapshot_for(self.conn, self.data, self.agent, self.now + timedelta(seconds=5), cache=cache)
        self.assertEqual(statements, [])
        self.assertEqual(cached['metrics']['like_count'], first['metrics']['like_count'])
        self.draft(1)
        changed = self.snapshot(cache=cache)
        self.assertTrue(statements)
        self.assertEqual(changed['confirmed_action_count'], 1)
        self.assertEqual(changed['metrics']['like_count'], 6)
        statements.clear()
        feedback.snapshot_for(self.conn, self.data, self.agent, self.now + timedelta(seconds=31), cache=cache)
        self.assertTrue(statements)

    def test_cache_copies_values_and_refreshes_age_without_changing_sampling_time(self):
        self.post()
        self.sample(minutes=-59)
        cache = feedback.FeedbackCache(ttl_seconds=300)
        initial = self.snapshot(cache=cache)
        initial['recent_posts'][0]['body'] = 'Must not mutate cache'
        later = feedback.snapshot_for(self.conn, self.data, self.agent, self.now + timedelta(minutes=2), cache=cache)
        self.assertNotEqual(later['recent_posts'][0]['body'], 'Must not mutate cache')
        self.assertTrue(later['recent_posts'][0]['stale'])
        self.assertEqual(later['recent_posts'][0]['sample_age_seconds'], 3660)
        self.assertEqual(later['latest_sampled_at'], initial['latest_sampled_at'])

    def test_binding_change_invalidates_cache_and_does_not_inherit_actions(self):
        self.post(1)
        self.post(2, author_id=78)
        self.draft(1)
        cache = feedback.FeedbackCache()
        self.snapshot(cache=cache)
        self.agent['account_id'] = 78
        result = self.snapshot(cache=cache)
        self.assertEqual(result['confirmed_action_count'], 0)
        self.assertEqual([row['post_id'] for row in result['recent_posts']], [2])

    def test_model_context_contains_real_observation_deltas_and_unknown_score(self):
        for number in range(1, 10):
            self.post(number, body='x' * 500)
        self.sample(9, minutes=-10, like_count=5)
        self.sample(9, minutes=0, like_count=2)
        before = json.dumps(self.data, sort_keys=True)
        result = feedback.context_for(self.conn, self.data, self.agent, self.now)
        self.assertEqual(len(result['recent_authored_posts']), 6)
        self.assertTrue(all(len(row['body']) <= 180 for row in result['recent_authored_posts']))
        row = next(row for row in result['recent_authored_posts'] if row['post_id'] == 9)
        self.assertEqual(row['counter_delta']['like_count'], -3)
        self.assertIn('not NPC identities', result['interpretation'])
        self.assertIsNone(result['official_points'])
        self.assertEqual(json.dumps(self.data, sort_keys=True), before)
        self.assertNotIn('team_snapshot', result)

    def test_feedback_uses_no_analysis_or_unbounded_official_activity_queries(self):
        self.post()
        statements = []
        self.conn.set_trace_callback(statements.append)
        self.snapshot()
        selects = [sql for sql in statements if sql.lstrip().upper().startswith('SELECT')]
        self.assertFalse(any('official_activity' in sql or 'analysis_runs' in sql for sql in selects))
        self.assertTrue(any('WHERE p.author_id=77' in sql and 'LIMIT 20' in sql for sql in selects))
        self.assertTrue(any('FROM score_snapshots' in sql and 'ctn_timestamp(observed_at) DESC' in sql
                            and 'LIMIT 1' in sql for sql in selects))


if __name__ == '__main__':
    unittest.main()
