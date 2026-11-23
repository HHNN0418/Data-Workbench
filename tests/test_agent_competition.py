"""Competition invariants, with real isolated SQLite and fixed observation time."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ctn_data import agent_competition as competition
from ctn_data.agent_control import AgentStore
from ctn_data.db import connect, initialize


NOW = competition._epoch('2026-10-20T01:00:00Z')


def actor(aid, account):
    return {'id': aid, 'name': aid, 'account_id': account, 'account_state': 'ready',
            'enabled': True, 'mode': 'auto', 'allowed_actions': ['post', 'reply'],
            'provider_id': 'standard'}


class CompetitionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / 'competition.db'
        initialize(self.db)
        self.conn = connect(self.db)
        self.addCleanup(self.conn.close)
        self.data = {'settings': {'enabled': True},
                     'agents': {'a': actor('a', 11), 'b': actor('b', 22)},
                     'rate': {}, 'drafts': {}}
        competition.sync(self.conn, self.data, NOW - 7200, ready_ids=['a', 'b'])
        competition.configure(self.conn, self.data, {'expected_revision': 0, 'mode': 'resources'}, NOW)

    def post(self, pid, account=11, at=NOW, reply_to=None, visibility='public'):
        stamp = competition._iso(at)
        self.conn.execute(
            'INSERT INTO posts(post_id,author_id,kind,body,visibility,created_at,first_seen,last_seen,reply_to_post_id)'
            'VALUES(?,?,?,?,?,?,?,?,?)',
            (pid, account, 'reply' if reply_to else 'post', 'Example', visibility, stamp, stamp, stamp, reply_to))

    def counter(self, pid, at, *, views=0, likes=0, replies=0, reposts=0):
        self.conn.execute('INSERT INTO counter_snapshots(post_id,observed_at,view_count,like_count,reply_count,repost_count) '
                          'VALUES(?,?,?,?,?,?)', (pid, competition._iso(at), views, likes, replies, reposts))

    def publish(self, pid, aid='a', at=NOW, automatic=True, origin='automatic'):
        draft = {'id': 'draft-' + str(pid), 'post_id': pid, 'agent_id': aid,
                 'account_id': self.data['agents'][aid]['account_id'], 'state': 'published',
                 'action': 'post', 'published_at': competition._iso(at),
                 'competition_automatic': automatic, 'origin': origin}
        result = competition.record_published(self.conn, self.data, draft, at)
        return draft, result

    def mature(self, pid, aid='a', at=NOW, likes=1):
        self.post(pid, self.data['agents'][aid]['account_id'], at)
        self.publish(pid, aid, at)
        self.counter(pid, at + 30)
        self.counter(pid, at + 3630, likes=likes)

    def refresh(self, at=NOW + 2 * 3600 + 600):
        return competition.refresh(self.conn, self.data, at, force=True)

    def test_default_read_is_pure_and_cas_is_strict(self):
        data = {'settings': {'enabled': True}, 'agents': {'a': actor('a', 11)}, 'rate': {}}
        before = deepcopy(data)
        view = competition.snapshot(self.conn, data, NOW)
        self.assertEqual(data, before)
        self.assertEqual(view['settings']['mode'], 'off')
        self.assertEqual(view['generated_at'], NOW)
        self.assertIsNone(view['agents'][0]['first_ready_at'])
        with self.assertRaises(competition.CompetitionError) as raised:
            competition.configure(self.conn, self.data, {'expected_revision': 0, 'mode': 'models'}, NOW)
        self.assertEqual(raised.exception.status, 409)
        for invalid in (-1, 1001, True, '2'):
            with self.assertRaises(competition.CompetitionError):
                competition.configure(self.conn, self.data, {'expected_revision': 1, 'enhanced_hourly_calls': invalid}, NOW)

    def test_first_ready_survives_pause_recreation_and_binding_change(self):
        first = competition.snapshot(self.conn, self.data, NOW)['agents'][0]['first_ready_at']
        self.data['settings']['enabled'] = False
        competition.sync(self.conn, self.data, NOW)
        del self.data['agents']['a']
        self.data['agents']['c'] = actor('c', 11)
        self.data['settings']['enabled'] = True
        competition.sync(self.conn, self.data, NOW + 100)
        c = next(a for a in competition.snapshot(self.conn, self.data, NOW + 100)['agents'] if a['agent_id'] == 'c')
        self.assertEqual(c['first_ready_at'], first)
        self.data['agents']['c']['account_id'] = 33
        competition.sync(self.conn, self.data, NOW + 200)
        c = next(a for a in competition.snapshot(self.conn, self.data, NOW + 200)['agents'] if a['agent_id'] == 'c')
        self.assertEqual(c['protected_until'], NOW + 3800)

    def test_provider_readiness_and_duplicate_account(self):
        self.data['agents']['b']['account_id'] = 11
        competition.sync(self.conn, self.data, NOW, ready_ids=['a', 'b'])
        self.assertEqual(self.data['rate']['competition']['ready_ids'], ['a'])
        self.assertEqual(next(a for a in competition.snapshot(self.conn, self.data, NOW)['agents']
                              if a['agent_id'] == 'b')['status'], 'duplicate_account')
        competition.sync(self.conn, self.data, NOW, ready_ids=[])
        self.assertEqual(self.data['rate']['competition']['ready_ids'], [])

    def test_publication_is_exact_automatic_idempotent_and_durable(self):
        self.post(1)
        draft, accepted = self.publish(1)
        self.assertTrue(accepted)
        self.assertFalse(competition.record_published(self.conn, self.data, draft, NOW))
        self.post(2)
        self.assertFalse(self.publish(2, automatic=False)[1])
        self.post(3)
        self.assertFalse(self.publish(3, origin='manual')[1])
        self.conn.commit()
        self.conn.close()
        self.conn = connect(self.db)
        self.addCleanup(self.conn.close)
        self.assertEqual([p['post_id'] for p in competition._publications(self.conn)], [1])

    def test_high_water_decrease_recovery_counts_only_new_high(self):
        self.post(1)
        self.publish(1)
        for seconds, likes in ((0, 10), (600, 13), (1200, 9), (1800, 13), (3600, 15)):
            self.counter(1, NOW + seconds, likes=likes)
        result = self.refresh()['posts'][0]
        self.assertTrue(result['eligible'])
        self.assertEqual(result['score'], 25)
        self.assertEqual(result['observed_minutes'], 60)

    def test_unknown_is_not_zero_and_lower_endpoint_is_not_rankable(self):
        self.post(1)
        self.publish(1)
        self.counter(1, NOW, views=None)
        self.counter(1, NOW + 3600, views=4, likes=2)
        self.post(2)
        self.publish(2)
        self.counter(2, NOW, likes=10)
        self.counter(2, NOW + 3600, likes=9)
        by_id = {p['post_id']: p for p in self.refresh()['posts']}
        self.assertIsNone(by_id[1]['score'])
        self.assertEqual(by_id[1]['known_component_subtotal'], 10)
        self.assertIsNone(by_id[2]['score'])
        self.assertFalse(by_id[2]['eligible'])

    def test_baseline_and_endpoint_are_strict_and_pending_is_bounded(self):
        self.post(1)
        self.publish(1)
        pending = self.refresh(NOW + 10)['pending_samples'][0]
        self.assertEqual(pending, {'post_id': 1, 'target_at': NOW, 'due_at': NOW,
                                   'deadline': NOW + 120, 'stage': 'baseline'})
        self.counter(1, NOW + 121)
        self.counter(1, NOW + 3721, likes=3)
        result = self.refresh()['posts'][0]
        self.assertIsNone(result['baseline_at'])
        self.assertEqual(result['target_at'], NOW + 3720)
        self.post(2)
        self.publish(2)
        self.counter(2, NOW + 30)
        self.counter(2, NOW + 3751, likes=3)
        result = next(p for p in self.refresh()['posts'] if p['post_id'] == 2)
        self.assertIsNone(result['endpoint_at'])

    def test_first_endpoint_and_round_target_not_publish_hour(self):
        self.post(1)
        self.publish(1)
        self.counter(1, NOW + 60)
        self.counter(1, NOW + 3690, likes=2)
        self.counter(1, NOW + 3720, likes=10)
        result = self.refresh()['posts'][0]
        self.assertEqual(result['score'], 10)
        self.assertEqual(result['round_id'], str(int(NOW + 3600)))
        self.assertEqual(result['observed_minutes'], 60.5)

    def test_team_cap_includes_manual_posts_and_largest_remainder_batch(self):
        for pid, account in ((1, 11), (2, 22), (3, 11)):
            self.post(pid, account)
            self.counter(pid, NOW - 100)
        self.counter(3, NOW - 50, views=490)  # no competition attribution
        self.publish(1)
        self.publish(2, 'b')
        self.counter(1, NOW)
        self.counter(2, NOW)
        self.counter(1, NOW + 3600, views=12)
        self.counter(2, NOW + 3600, views=8)
        view = self.refresh()
        results = {p['post_id']: p for p in view['posts']}
        self.assertEqual((results[1]['score'], results[2]['score']), (6, 4))
        self.assertEqual(view['daily_views'][-1]['credited_views'], 500)
        self.assertEqual(view['daily_views'][-1]['observed_views'], 510)

    def test_cross_midnight_reset_keeps_high_water_and_applies_later_multiplier(self):
        midnight = competition._epoch('2026-10-25T13:00:00Z')  # Sydney Oct 26: x2 begins
        self.post(1, at=midnight - 3600)
        self.publish(1, at=midnight - 3600)
        self.counter(1, midnight - 3600, views=100, likes=10)
        self.counter(1, midnight, views=110, likes=11)
        result = self.refresh(midnight + 7200)['posts'][0]
        self.assertEqual(result['score'], 30)
        self.assertTrue(result['crosses_day_boundary'])
        self.assertEqual(self.data['rate']['competition']['daily_views'][-1]['observed_views'], 10)

    def test_old_canonical_peak_revision_repairs_future_day_not_only_last_48h(self):
        self.post(1, at=NOW - 4 * 86400)
        self.counter(1, NOW - 4 * 86400)
        self.counter(1, NOW - 3 * 86400, views=100)
        self.counter(1, NOW, views=20)
        before = self.refresh(NOW)['daily_views'][-1]
        self.assertEqual(before['observed_views'], 0)
        self.conn.execute('UPDATE counter_snapshots SET view_count=10 WHERE post_id=1 AND observed_at=?',
                          (competition._iso(NOW - 3 * 86400),))
        self.assertEqual(self.refresh(NOW)['daily_views'][-1]['observed_views'], 10)

    def test_three_mature_posts_and_coverage_are_required_for_ranking(self):
        for pid in range(1, 4):
            self.mature(pid, likes=pid)
        view = self.refresh()
        a = next(a for a in view['agents'] if a['agent_id'] == 'a')
        self.assertTrue(a['qualified'])
        self.assertEqual(a['score'], 30)
        self.assertEqual(a['rank'], 1)
        self.post(4)
        self.publish(4)
        view = self.refresh()
        a = next(a for a in view['agents'] if a['agent_id'] == 'a')
        self.assertEqual(a['coverage'], .75)
        self.assertFalse(a['qualified'])
        self.assertIsNone(a['score'])
        self.assertEqual(a['known_component_subtotal'], 30)

    def test_late_observation_revises_original_round_without_double_reward(self):
        for pid in range(1, 4):
            self.mature(pid)
        first = self.refresh()
        rid, revision = first['rounds'][0]['round_id'], first['rounds'][0]['revision']
        epoch = first['allocation_epoch']
        shares = [a['share'] for a in first['agents']]
        self.conn.execute('UPDATE counter_snapshots SET like_count=4 WHERE post_id=1 AND like_count=1')
        changed = self.refresh()
        self.assertEqual(changed['rounds'][0]['round_id'], rid)
        self.assertGreater(changed['rounds'][0]['revision'], revision)
        self.assertEqual(changed['allocation_epoch'], epoch)
        self.assertEqual([a['share'] for a in changed['agents']], shares)

    def test_known_team_interaction_and_attribution_conflict_are_excluded(self):
        self.mature(1)
        self.data['drafts']['like'] = {'state': 'published', 'action': 'like',
                                     'target_post_id': 1, 'published_at': competition._iso(NOW + 60)}
        self.assertFalse(self.refresh()['posts'][0]['eligible'])
        self.data['drafts'].clear()
        draft, _ = self.publish(1)
        draft['id'] = 'other-draft'
        competition.record_published(self.conn, self.data, draft, NOW)
        self.assertIn('同一帖子存在不同发布归属，待核对', self.refresh()['posts'][0]['reasons'])

    def test_durable_like_receipt_survives_draft_retention(self):
        self.mature(1)
        at = competition._iso(NOW + 60)
        self.conn.execute('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)',
                          ('agent_tracking:action:old-like',
                           json.dumps({'action': 'like', 'target_post_id': 1,
                                       'tracking': {'tracked_at': at}}), at))
        self.assertFalse(self.refresh()['posts'][0]['eligible'])
        self.assertEqual(self.data['drafts'], {})

    def test_paused_and_deleted_actor_keeps_revisable_historical_round(self):
        for pid in range(1, 4):
            self.mature(pid)
        first = self.refresh()
        rid = first['rounds'][0]['round_id']
        self.data['agents']['a']['enabled'] = False
        competition.sync(self.conn, self.data, NOW + 9000)
        self.conn.execute('UPDATE counter_snapshots SET like_count=4 WHERE post_id=1 AND like_count=1')
        changed = self.refresh()
        historical = next(r for r in changed['rounds'] if r['round_id'] == rid)
        self.assertEqual(next(r for r in historical['results'] if r['agent_id'] == 'a')['score'], 30)
        del self.data['agents']['a']
        competition.sync(self.conn, self.data, NOW + 9001)
        historical = next(r for r in self.refresh()['rounds'] if r['round_id'] == rid)
        self.assertEqual(next(r for r in historical['results'] if r['agent_id'] == 'a')['score'], 30)

    def test_sync_retries_publication_receipt_without_invalidating_replay_forever(self):
        self.post(1)
        draft = {'id': 'late-receipt', 'post_id': 1, 'agent_id': 'a', 'account_id': 11,
                 'state': 'published', 'action': 'post', 'published_at': competition._iso(NOW),
                 'competition_automatic': True}
        self.data['drafts']['late-receipt'] = draft
        competition.sync(self.conn, self.data, NOW + 20)
        self.assertEqual(len(competition._publications(self.conn)), 1)
        self.refresh(NOW + 20)
        competition.sync(self.conn, self.data, NOW + 30)
        self.assertEqual(self.data['rate']['competition']['last_refresh_at'], NOW + 20)

    class Counting:
        """The connection, recording each statement sync sends (agent-control lock, 24 Oct)."""
        def __init__(self, conn):
            self.conn, self.sql = conn, []

        def execute(self, sql, *args):
            self.sql.append(sql)
            return self.conn.execute(sql, *args)

        def lookups(self):
            return sum(sql.startswith('SELECT value FROM checkpoints') for sql in self.sql)

    def test_sync_checks_only_drafts_published_since_its_last_pass(self):
        old, _ = self.publish(1, at=NOW - 3 * 3600)
        new, _ = self.publish(2, at=NOW)
        self.data['drafts'] = {d['id']: d for d in (old, new)}
        first = self.Counting(self.conn)
        competition.sync(first, self.data, NOW + 10)
        self.assertEqual(first.lookups(), 2)
        self.assertEqual(self.data['rate']['competition']['receipts_synced_through'], NOW)
        # A delivery confirmed late (published 30 minutes before the last pass) is still checked.
        late, _ = self.publish(3, at=NOW - 1800)
        self.data['drafts'][late['id']] = late
        second = self.Counting(self.conn)
        competition.sync(second, self.data, NOW + 40)
        self.assertEqual(second.lookups(), 2)
        self.assertEqual(self.data['rate']['competition']['receipts_synced_through'], NOW)

    def test_prefetched_receipts_save_only_lookups_that_would_change_nothing(self):
        recorded, _ = self.publish(4)
        self.data['drafts'] = {recorded['id']: recorded}
        known = competition.prefetch_receipts(self.conn, self.data, NOW)
        self.assertEqual(set(known), {competition.PREFIX + '4'})
        counting = self.Counting(self.conn)
        competition.sync(counting, self.data, NOW + 10, known=known)
        self.assertEqual(counting.lookups(), 0)
        # A different draft claiming the same post still takes the original path and is flagged.
        other = {**recorded, 'id': 'draft-other'}
        self.data['drafts'] = {'draft-other': other}
        counting = self.Counting(self.conn)
        self.assertFalse(competition.record_published(counting, self.data, other, NOW + 20, known=known))
        self.assertEqual(counting.lookups(), 1)
        receipt = json.loads(self.conn.execute('SELECT value FROM checkpoints WHERE key=?',
                                               (competition.PREFIX + '4',)).fetchone()['value'])
        self.assertTrue(receipt['attribution_conflict'])
        # A receipt missing from the prefetch is looked up and inserted as before.
        fresh = {**recorded, 'id': 'draft-5', 'post_id': 5}
        counting = self.Counting(self.conn)
        self.assertTrue(competition.record_published(counting, self.data, fresh, NOW + 30, known=known))
        self.assertEqual(counting.lookups(), 1)

    def cold(self):
        row = self.conn.execute('SELECT value FROM meta WHERE key=?', (competition.COLD_KEY,)).fetchone()
        return json.loads(row['value']) if row else {}

    def test_off_moves_estimates_to_the_cold_row_once_and_the_page_still_shows_them(self):
        state = self.data['rate']['competition']
        state['settings']['mode'] = 'off'
        posts = [{'post_id': i, 'score': i} for i in range(150)]
        rounds = [{'round_id': '1', 'results': []}]
        state.update(posts=deepcopy(posts), daily_views=[{'date': '2026-10-24', 'observed_views': 9}],
                     pending_samples=[{'post_id': 1}], rounds=deepcopy(rounds))
        self.assertTrue(competition.needs_offload(state, NOW))
        self.assertTrue(competition.offload(self.conn, self.data, NOW))
        self.assertEqual((state['posts'], state['daily_views'], state['pending_samples']), ([], [], []))
        self.assertEqual(state['rounds'], rounds)  # decisions stay where they are
        cold = self.cold()
        self.assertEqual(cold['posts'], posts)
        self.assertEqual(cold['daily_views'], [{'date': '2026-10-24', 'observed_views': 9}])
        # Repeating the move changes nothing.
        self.assertFalse(competition.needs_offload(state, NOW))
        self.assertFalse(competition.offload(self.conn, self.data, NOW + 30))
        self.assertEqual(self.cold(), cold)
        view = competition.snapshot(self.conn, self.data, NOW)
        self.assertEqual(view['posts'], posts[-100:])
        self.assertEqual(view['daily_views'], cold['daily_views'])

    def test_model_selections_older_than_an_hour_move_and_retries_keep_their_model(self):
        first = competition.select_model(self.conn, self.data, 'a', 'standard', 'job-old', NOW - 2 * 3600)
        recent = competition.select_model(self.conn, self.data, 'a', 'standard', 'job-recent', NOW - 600)
        state = self.data['rate']['competition']
        self.assertTrue(competition.offload(self.conn, self.data, NOW))
        self.assertEqual(set(state['model_requests']), {'a:job-recent'})
        self.assertEqual(self.cold()['model_requests']['a:job-old']['selection'], first)
        # A retry within the hour gets the same model, without a new choice.
        calls = state['models']['a']['calls']
        self.assertEqual(competition.select_model(self.conn, self.data, 'a', 'standard', 'job-recent', NOW), recent)
        self.assertEqual(state['models']['a']['calls'], calls)
        # The cold row keeps the newest COLD_REQUEST_LIMIT selections.
        state['model_requests'] = {f'a:{i}': {'at': NOW - 7200 + i, 'selection': {}}
                                   for i in range(competition.COLD_REQUEST_LIMIT + 5)}
        competition.offload(self.conn, self.data, NOW)
        kept = self.cold()['model_requests']
        self.assertEqual(len(kept), competition.COLD_REQUEST_LIMIT)
        self.assertNotIn('a:job-old', kept)
        self.assertIn(f'a:{competition.COLD_REQUEST_LIMIT + 4}', kept)

    def test_first_enable_without_publications_has_no_settlement(self):
        first = self.refresh()
        self.assertEqual(first['rounds'], [])
        self.assertIsNone(first['last_settled_at'])
        self.data['settings']['enabled'] = False
        competition.sync(self.conn, self.data, NOW + 9000)
        paused = self.refresh(NOW + 9000)
        self.assertEqual(paused['rounds'], [])
        self.assertIsNone(paused['last_settled_at'])

    def test_old_synthetic_empty_round_is_removed_without_publications(self):
        state = self.data['rate']['competition']
        start = int(NOW + 3600)
        state['rounds'] = [{'round_id': str(start), 'start_at': start,
                            'end_at': start + 3600, 'sealed_at': start + 4200,
                            'revision': 1, 'results': []}]
        state['last_settled_at'] = start + 4200
        state['allocation_epoch'] = str(start)
        view = self.refresh()
        self.assertEqual(view['rounds'], [])
        self.assertIsNone(view['last_settled_at'])
        self.assertIsNone(view['allocation_epoch'])

    def test_actual_settlement_time_is_preserved_on_revision_and_no_output_has_history(self):
        for pid in range(1, 4):
            self.mature(pid)
        actual = NOW + 2 * 3600 + 683
        first = self.refresh(actual)
        self.assertEqual(first['last_settled_at'], actual)
        self.assertEqual(first['rounds'][0]['sealed_at'], actual)
        self.assertEqual(first['rounds'][0]['scheduled_settle_at'], NOW + 2 * 3600 + 600)
        revised = self.refresh(actual + 60)
        self.assertEqual(revised['rounds'][0]['sealed_at'], actual)
        next_actual = actual + 3600
        following = self.refresh(next_actual)
        self.assertEqual(following['rounds'][0]['sealed_at'], next_actual)
        self.assertTrue(all(r['due_posts'] == 0 for r in following['rounds'][0]['results']))
        self.assertEqual(following['rounds'][1]['sealed_at'], actual)

    def test_resource_projection_bounds_and_unqualified_freeze(self):
        state = self.data['rate']['competition']
        state['allocations']['a'] = {'share': .7, 'ever_qualified': True}
        state['allocations']['b'] = {'share': .3}
        results = [{'agent_id': 'a', 'qualified': False, 'status': 'insufficient_data', 'score': None},
                   {'agent_id': 'b', 'qualified': True, 'status': 'eligible', 'score': 100}]
        competition._allocate(state, results, NOW)
        self.assertAlmostEqual(state['allocations']['a']['share'], .7)
        self.assertAlmostEqual(state['allocations']['b']['share'], .3)
        for result in results:
            result.update(qualified=True, status='eligible')
        results[0]['score'] = 200
        competition._allocate(state, results, NOW)
        self.assertAlmostEqual(sum(x['share'] for x in state['allocations'].values()), 1)
        self.assertLessEqual(state['allocations']['a']['share'], .84)
        self.assertGreaterEqual(state['allocations']['b']['share'], .24)

    def test_fair_gate_weighted_service_idempotent_and_idle_borrow(self):
        state = self.data['rate']['competition']
        state['allocations']['a']['share'] = .75
        state['allocations']['b']['share'] = .25
        totals = {'a': 0, 'b': 0}
        for i in range(400):
            ordered = competition.order(self.conn, self.data, ['a', 'b'], 'generate', NOW + i / 10)
            chosen = ordered[0]
            permit = competition.admit(self.conn, self.data, chosen, 'generate', str(i), NOW + i / 10)
            self.assertTrue(permit['allowed'])
            self.assertTrue(competition.admit(self.conn, self.data, chosen, 'generate', str(i), NOW + i / 10)['replayed'])
            totals[chosen] += 1
        self.assertLessEqual(abs(totals['a'] - 300), 1)
        self.assertLessEqual(abs(totals['b'] - 100), 1)
        competition.withdraw(self.conn, self.data, 'b', 'generate')
        for i in range(20):
            self.assertTrue(competition.admit(self.conn, self.data, 'a', 'generate', 'solo' + str(i), NOW + 100 + i)['allowed'])
        ordered = competition.order(self.conn, self.data, ['a', 'b'], 'generate', NOW + 150)
        fair = state['fair']['generate']
        self.assertGreaterEqual(fair['served']['b'], fair['served']['a'] - 1 / .75 - 1e-9)
        self.assertEqual(set(ordered), {'a', 'b'})

    def test_model_tiers_ratio_budget_and_request_freeze(self):
        state = self.data['rate']['competition']
        competition.configure(self.conn, self.data, {
            'expected_revision': 1, 'mode': 'models', 'enhanced_provider_id': 'enhanced',
            'premium_provider_id': 'premium', 'enhanced_hourly_calls': 10,
            'enhanced_max_output_tokens': 4096}, NOW)
        state['models']['a'] = {'tier': 2, 'calls': 1000, 'rewarded_calls': 0}
        selections = [competition.select_model(self.conn, self.data, 'a', 'standard', str(i), NOW) for i in range(10)]
        self.assertEqual(sum(s['enhanced'] for s in selections), 8)
        self.assertTrue(all(s['provider_id'] == 'premium' for s in selections if s['enhanced']))
        frozen = competition.select_model(self.conn, self.data, 'a', 'changed-standard', '0', NOW)
        self.assertEqual(frozen, selections[0])
        more = [competition.select_model(self.conn, self.data, 'a', 'standard', str(i), NOW) for i in range(10, 30)]
        self.assertEqual(sum(s['enhanced'] for s in selections + more), 10)
        self.assertEqual(self.data['agents']['a']['provider_id'], 'standard')

    def test_two_consecutive_rounds_promote_once_and_missing_breaks_streak(self):
        state = self.data['rate']['competition']
        def round_(hour, a_score=100, qualified=True):
            return {'round_id': str(hour * 3600), 'results': [
                {'agent_id': 'a', 'qualified': qualified, 'score': a_score},
                {'agent_id': 'b', 'qualified': True, 'score': 1}]}
        competition._model_decision(state, round_(1), None)
        competition._model_decision(state, round_(2), round_(1))
        self.assertEqual(state['models']['a']['tier'], 1)
        competition._model_decision(state, round_(2), round_(1))
        self.assertEqual(state['models']['a']['tier'], 1)
        competition._model_decision(state, round_(3), round_(2))
        self.assertEqual(state['models']['a']['tier'], 1)
        competition._model_decision(state, round_(4, None, False), round_(3))
        competition._model_decision(state, round_(5), round_(4, None, False))
        self.assertEqual(state['models']['a']['tier'], 1)
        competition._model_decision(state, round_(6), round_(5))
        self.assertEqual(state['models']['a']['tier'], 2)

    def test_protection_disables_reward_even_if_old_tier_is_present(self):
        state = self.data['rate']['competition']
        state['settings'].update(mode='models', enhanced_provider_id='reward')
        state['profiles']['11']['protected_until'] = NOW + 3600
        state['models']['a'] = {'tier': 2, 'calls': 0, 'rewarded_calls': 0, 'reward_credit': 1}
        selected = competition.select_model(self.conn, self.data, 'a', 'standard', 'protected', NOW)
        self.assertEqual(selected['tier'], 0)
        self.assertFalse(selected['enhanced'])
        self.assertEqual(selected['provider_id'], 'standard')

    def test_agent_store_transaction_persists_configuration_and_protection(self):
        self.conn.commit()
        store = AgentStore(self.db)
        def setup(data, conn):
            data['settings']['enabled'] = True
            data['agents']['a'] = actor('a', 11)
            competition.sync(conn, data, NOW)
            return competition.configure(conn, data, {'expected_revision': 0, 'mode': 'observe'}, NOW)
        setup_result = store.change(setup)
        reopened = AgentStore(self.db).read()
        fresh = competition.snapshot(self.conn, reopened, NOW + 300)
        self.assertEqual(fresh['revision'], setup_result['revision'])
        self.assertEqual(fresh['settings']['mode'], 'observe')
        self.assertEqual(fresh['agents'][0]['protected_until'], NOW + 3600)

    def test_allocation_keeps_actual_missing_sample_reason_when_scores_are_revised(self):
        state = self.data['rate']['competition']
        state['allocations']['a'].update(share=.7, ever_qualified=True)
        state['allocations']['b']['share'] = .3
        results = [{'agent_id': aid, 'account_id': int(state['bindings'][aid]['account_id']),
                    'binding_epoch': state['bindings'][aid]['epoch'], 'rank': None,
                    'qualified': aid == 'b', 'status': 'eligible' if aid == 'b' else 'insufficient_data',
                    'score': 100 if aid == 'b' else None, 'coverage': .5, 'reasons': []} for aid in ('a', 'b')]
        rid = str(int(NOW - 3600))
        competition._allocate(state, results, NOW, rid)
        basis = deepcopy(state['allocations']['a']['basis'])
        self.assertTrue(basis['preserved_missing_data'])
        self.assertEqual(basis['previous_share'], .7)
        self.assertEqual(basis['share'], .7)
        self.assertAlmostEqual(sum(basis[key] for key in ('base_component', 'performance_component', 'exploration_component')),
                               basis['target_share'])
        self.assertFalse(basis['performance_comparison_valid'])
        results[0].update(qualified=True, status='eligible', score=200, coverage=1, rank=1)
        state['rounds'] = [{'round_id': rid, 'sealed_at': NOW, 'results': results}]
        context = competition.context_for(self.data, self.data['agents']['a'], NOW)
        self.assertEqual(context['latest_round']['rank'], 1)
        self.assertTrue(context['resources']['basis']['preserved_missing_data'])
        self.assertIn('保留原份额', ' '.join(context['resources']['reasons']))
        self.assertEqual(state['allocations']['a']['basis'], basis)

    def test_allocation_snapshot_records_smoothing_and_membership_rebase_replaces_it(self):
        state = self.data['rate']['competition']
        state['allocations']['a']['share'] = .3
        state['allocations']['b']['share'] = .7
        results = [{'agent_id': aid, 'qualified': True, 'status': 'eligible', 'score': score}
                   for aid, score in (('a', 100), ('b', 1))]
        competition._allocate(state, results, NOW, '3600')
        basis = state['allocations']['a']['basis']
        self.assertTrue(basis['performance_comparison_valid'])
        self.assertEqual(basis['adjustment'], 'bounded')
        self.assertLess(basis['share'], basis['target_share'])
        self.assertIn('未直接跳到目标', ' '.join(basis['reasons']))
        self.assertEqual(basis['round_id'], '3600')
        competition._rebase(state, ['a'], NOW + 5)
        rebased = state['allocations']['a']['basis']
        self.assertEqual(rebased['kind'], 'membership_rebase')
        self.assertIsNone(rebased['round_id'])
        self.assertEqual(rebased['share'], 1)
        self.assertIn('不是成绩奖惩', ' '.join(rebased['reasons']))

    def test_model_decision_records_real_promotion_demotion_and_retains_original_reason(self):
        state = self.data['rate']['competition']
        def round_(hour, a_score=100, qualified=True, status='eligible'):
            return {'round_id': str(hour * 3600), 'results': [
                {'agent_id': aid, 'account_id': int(state['bindings'][aid]['account_id']),
                 'binding_epoch': state['bindings'][aid]['epoch'], 'qualified': qualified if aid == 'a' else True,
                 'score': a_score if aid == 'a' else 1, 'status': status if aid == 'a' else 'eligible'}
                for aid in ('a', 'b')]}
        first, second = round_(1), round_(2)
        competition._model_decision(state, first, None)
        self.assertIn('尚无相邻', state['models']['a']['decision']['reason'])
        competition._model_decision(state, second, first)
        promotion = deepcopy(state['models']['a']['decision'])
        self.assertEqual((promotion['previous_tier'], promotion['tier'], promotion['direction']), (0, 1, 'up'))
        self.assertIn('连续两轮', promotion['reason'])
        state['rounds'] = [first, second]
        context = competition.context_for(self.data, self.data['agents']['a'], NOW)
        self.assertEqual(context['model_reward']['last_decision']['reason'], promotion['reason'])
        corrected = round_(2, None, False, 'insufficient_data')
        competition._model_decision(state, corrected, first)
        self.assertEqual(state['models']['a']['decision'], promotion)
        third, fourth = round_(3, 0), round_(4, 0)
        competition._model_decision(state, third, second)
        self.assertEqual(state['models']['a']['decision']['direction'], 'unchanged')
        competition._model_decision(state, fourth, third)
        demotion = state['models']['a']['decision']
        self.assertEqual((demotion['previous_tier'], demotion['tier'], demotion['direction']), (1, 0, 'down'))
        self.assertIn('落后', demotion['reason'])

    def test_model_decision_explains_protection_missing_ties_and_boundary(self):
        state = self.data['rate']['competition']
        def round_(hour, **changes):
            return {'round_id': str(hour * 3600), 'results': [
                {'agent_id': 'a', 'qualified': True, 'score': 100, 'status': 'eligible', **changes},
                {'agent_id': 'b', 'qualified': True, 'score': 1, 'status': 'eligible'}]}
        protected = round_(1, qualified=False, score=None, status='protected')
        competition._model_decision(state, protected, None)
        self.assertIn('保护期', state['models']['a']['decision']['reason'])
        missing = round_(2, qualified=False, score=None, status='insufficient_data')
        competition._model_decision(state, missing, protected)
        self.assertIn('样本或覆盖不足', state['models']['a']['decision']['reason'])
        tied = round_(3, score=1)
        competition._model_decision(state, tied, missing)
        self.assertIn('同分', state['models']['a']['decision']['reason'])
        state['models']['a']['tier'] = 2
        fourth, fifth = round_(4), round_(5)
        competition._model_decision(state, fourth, tied)
        competition._model_decision(state, fifth, fourth)
        decision = state['models']['a']['decision']
        self.assertEqual(decision['direction'], 'unchanged')
        self.assertIn('最高档', decision['reason'])


class CompetitionContextTests(unittest.TestCase):
    def setUp(self):
        self.agent = actor('a', 11)
        self.state = {
            'settings': {'mode': 'resources'}, 'ready_ids': ['a', 'b'],
            'bindings': {'a': {'account_id': '11', 'epoch': NOW - 7200},
                         'b': {'account_id': '22', 'epoch': NOW - 7200}},
            'profiles': {'11': {'first_ready_at': NOW - 7200, 'protected_until': NOW - 3600}},
            'allocations': {'a': {'share': .6, 'target_share': .7, 'ever_qualified': True}},
            'models': {'a': {'tier': 1, 'rewarded_calls': 3}}, 'allocation_epoch': str(int(NOW - 3600)),
            'rounds': [{'round_id': str(int(NOW - 3600)), 'sealed_at': NOW - 100,
                        'results': [self.result('a', 11, rank=1, score=100),
                                    self.result('b', 22, rank=2, score=50)]}],
        }
        self.data = {'settings': {'enabled': True}, 'agents': {'a': self.agent, 'b': actor('b', 22)},
                     'rate': {'competition': self.state}}

    def result(self, aid, account, **changes):
        return {'agent_id': aid, 'account_id': account, 'binding_epoch': NOW - 7200,
                'qualified': True, 'coverage': 1, 'eligible_posts': 4, 'due_posts': 4,
                'reasons': [], 'status': 'eligible', **changes}

    def context(self, selection=None):
        return competition.context_for(self.data, self.agent, NOW, selection)

    def test_feedback_links_only_own_account_and_explains_actual_resources(self):
        self.state['rounds'][0]['results'][1].update(name='private-other-persona', background='private-background')
        self.state['settings']['enhanced_provider_id'] = 'private-provider-id'
        before = deepcopy(self.data)
        with patch.object(competition, 'refresh', side_effect=AssertionError('No refresh')), \
                patch.object(competition, 'sync', side_effect=AssertionError('No sync')), \
                patch.object(competition, 'select_model', side_effect=AssertionError('No model selection')):
            context = self.context({'tier': 1, 'enhanced': True, 'provider_id': 'private-provider-id'})
        self.assertEqual(self.data, before)
        self.assertEqual(context['latest_round']['rank'], 1)
        self.assertEqual(context['latest_round']['participant_count'], 2)
        self.assertEqual(context['latest_round']['ranked_count'], 2)
        self.assertEqual(context['latest_round']['score_estimate'], 100)
        self.assertEqual(context['resources']['share'], .6)
        self.assertEqual(context['resources']['target_share'], .7)
        self.assertIn('高于', ' '.join(context['resources']['reasons']))
        self.assertFalse(context['model_reward']['used_this_request'])
        self.assertIsNone(context['model_reward']['target_ratio'])
        serialized = json.dumps(context, ensure_ascii=False)
        for hidden in ('private-other-persona', 'private-background', 'private-provider-id'):
            self.assertNotIn(hidden, serialized)
        self.assertIn('不是官方', context['interpretation'])
        self.assertIn('不是平台配额', context['interpretation'])
        self.assertIn('不得', context['interpretation'])

    def test_objective_explicitly_competes_for_score_and_resources_and_describes_current_mode(self):
        for mode in ('resources', 'models', 'observe', 'off'):
            with self.subTest(mode=mode):
                self.state['settings']['mode'] = mode
                context = self.context()
                objective = context['objective']
                for phrase in ('得分', '资源', '竞赛排名', '表现反馈', '优化选题', '表达', '互动策略'):
                    self.assertIn(phrase, objective)
                self.assertNotIn('不要为追奖励', context['interpretation'])
                if mode == 'off':
                    self.assertIn('当前竞赛暂停', objective)
                    self.assertIn('恢复后', objective)
                elif mode == 'observe':
                    self.assertIn('当前只观察计分', objective)
                elif mode == 'models':
                    self.assertIn('争取升级模型', objective)
                else:
                    self.assertIn('不发放模型升级奖励', objective)

    def test_objective_combines_research_reading_memory_and_feedback_to_adapt_actions_and_writing(self):
        objective = self.context()['objective']
        for phrase in ('strategy_context内容分析参考', '筛选后的sources', '历史阅读与互动', '个人记忆',
                       'performance_feedback', '本竞赛反馈', '自主', 'allowed_actions',
                       '只在allowed_actions开放的动作（', '回应对象与时机',
                       '回复文字', '开头', '结构', '论据', '语气', '措辞', '反馈迭代', '避免固定轮换', '因果证明'):
            self.assertIn(phrase, objective)

    def test_unqualified_and_absent_results_are_unknown_not_zero(self):
        own = self.state['rounds'][0]['results'][0]
        own.update(qualified=False, rank=None, score=None, coverage=.5, status='insufficient_data',
                   eligible_posts=1, due_posts=2, reasons=['完整样本覆盖不足 80%，未知值不按零分处理'])
        context = self.context()
        self.assertIsNone(context['latest_round']['rank'])
        self.assertIsNone(context['latest_round']['score_estimate'])
        self.assertEqual(context['latest_round']['coverage'], .5)
        self.assertEqual(context['latest_round']['unranked_reasons'], own['reasons'])
        self.assertIn('历史分配原因未保存', ' '.join(context['resources']['reasons']))
        self.state['rounds'] = []
        empty = self.context()['latest_round']
        for key in ('rank', 'score_estimate', 'coverage', 'eligible_posts', 'due_posts', 'participant_count'):
            self.assertIsNone(empty[key])
        self.assertFalse(empty['account_result_available'])

    def test_changed_account_and_binding_epoch_never_inherit_results_or_rewards(self):
        self.agent['account_id'] = 33
        stale = self.context({'tier': 2, 'enhanced': True})
        self.assertEqual(stale['status'], 'awaiting_binding')
        self.assertFalse(stale['latest_round']['account_result_available'])
        self.assertIsNone(stale['resources']['share'])
        self.assertIsNone(stale['model_reward']['tier'])
        self.agent['account_id'] = 11
        self.state['bindings']['a']['epoch'] = NOW - 100
        same_account_new_binding = self.context()
        self.assertFalse(same_account_new_binding['latest_round']['account_result_available'])
        self.state['rounds'][0]['results'][0].pop('binding_epoch')
        self.assertFalse(self.context()['latest_round']['account_result_available'])

    def test_modes_readiness_and_protection_do_not_claim_active_rewards(self):
        for mode in ('off', 'observe'):
            with self.subTest(mode=mode):
                self.state['settings']['mode'] = mode
                context = self.context()
                self.assertFalse(context['resources']['active'])
                self.assertIsNone(context['resources']['share'])
                self.assertFalse(context['model_reward']['used_this_request'])
        self.state['settings']['mode'] = 'models'
        self.agent['enabled'] = False
        self.assertEqual(self.context()['status'], 'inactive')
        self.assertIsNone(self.context({'tier': 1, 'enhanced': True})['model_reward']['used_this_request'])
        self.agent['enabled'] = True
        self.state['duplicate_ids'] = ['a']
        self.state['ready_ids'] = ['b']
        self.assertEqual(self.context()['status'], 'duplicate_account')
        self.state['duplicate_ids'] = []
        self.assertEqual(self.context()['status'], 'awaiting_model')
        self.state['ready_ids'] = ['a', 'b']
        self.state['profiles']['11']['protected_until'] = NOW + 25
        context = self.context({'tier': 0, 'enhanced': False})
        self.assertEqual(context['status'], 'protected')
        self.assertEqual(context['protection'], {'active': True, 'remaining_seconds': 25})
        self.assertEqual(context['model_reward']['effective_tier'], 0)
        self.assertFalse(context['model_reward']['used_this_request'])

    def test_model_reward_reports_selected_request_without_inferring_a_win(self):
        self.state['settings']['mode'] = 'models'
        reward = self.context({'tier': 2, 'enhanced': True})['model_reward']
        self.assertTrue(reward['used_this_request'])
        self.assertEqual(reward['effective_tier'], 2)
        self.assertEqual(reward['target_ratio'], .8)
        self.assertEqual(reward['tier'], 1)  # Stored tier and frozen selection are distinct.
        fallback = self.context({'tier': 1, 'enhanced': False,
                                 'fallback_reason': '奖励连接不可用，本次沿用角色模型'})['model_reward']
        self.assertFalse(fallback['used_this_request'])
        self.assertIn('不可用', fallback['reason'])
        ordinary = self.context({'tier': 1, 'enhanced': False})['model_reward']
        self.assertFalse(ordinary['used_this_request'])
        self.assertIn('共同限制', ordinary['reason'])
        self.assertIsNone(self.context()['model_reward']['used_this_request'])

    def test_context_is_bounded_detached_and_handles_no_competition_state(self):
        own = self.state['rounds'][0]['results'][0]
        own.update(qualified=False, reasons=['x' * 10000] * 100)
        self.state['settings']['mode'] = 'models'
        self.state['allocations']['a']['basis'] = {
            **self.state['bindings']['a'], 'binding_epoch': self.state['bindings']['a']['epoch'],
            'share': .6, 'target_share': .7, 'reasons': ['z' * 10000] * 100}
        self.state['models']['a']['decision'] = {
            'account_id': '11', 'binding_epoch': NOW - 7200, 'reason': 'd' * 10000}
        context = self.context({'tier': 1, 'enhanced': False, 'fallback_reason': 'y' * 10000})
        self.assertLess(len(json.dumps(context, ensure_ascii=False)), 4000)
        context['latest_round']['unranked_reasons'].append('new local reason')
        self.assertNotIn('new local reason', own['reasons'])
        empty_data = {'settings': {'enabled': True}, 'agents': {'a': self.agent}, 'rate': {}}
        before = deepcopy(empty_data)
        empty = competition.context_for(empty_data, self.agent, NOW)
        self.assertEqual(empty_data, before)
        self.assertEqual(empty['mode'], 'off')
        self.assertIsNone(empty['model_reward']['tier'])
        self.assertIsNone(empty['protection']['active'])

    def test_stale_cause_snapshots_are_not_reused_for_new_binding_or_changed_shares(self):
        self.state['allocations']['a']['basis'] = {
            'account_id': '11', 'binding_epoch': NOW - 7200, 'round_id': '3600',
            'share': .6, 'target_share': .7, 'reasons': ['Saved actual cause']}
        self.state['models']['a']['decision'] = {
            'account_id': '11', 'binding_epoch': NOW - 7200, 'round_id': '3600',
            'previous_tier': 0, 'tier': 1, 'direction': 'up', 'reason': 'Saved promotion cause'}
        self.assertEqual(self.context()['resources']['reasons'], ['Saved actual cause'])
        self.assertEqual(self.context()['model_reward']['last_decision']['direction'], 'up')
        self.state['allocations']['a']['share'] = .5
        self.assertIsNone(self.context()['resources']['basis'])
        self.state['bindings']['a']['epoch'] = NOW - 100
        context = self.context()
        self.assertIsNone(context['resources']['basis'])
        self.assertIsNone(context['model_reward']['last_decision'])
        self.assertIn('不能反推', context['model_reward']['decision_history'])


if __name__ == '__main__':
    unittest.main()
