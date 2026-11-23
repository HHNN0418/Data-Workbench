"""Likers of team posts and followers of team accounts, read without network access."""
import json
from unittest.mock import patch

from ctn_data import engagement
from ctn_data.transport import BudgetExceeded, TransportError
from tests.test_collection import BaseDatabase
from tests.test_social_topics import PublicTransport

NOW = 1792700000.0  # 2026-10-22T21:33:20Z
OURS, OTHER = 6464, 99


def liker(account_id, at):
    return {'account_id': account_id, 'username': f'u{account_id}', 'liked_at': at}


class EngagementTests(BaseDatabase):
    def setUp(self):
        super().setUp()
        self.transport = PublicTransport()
        self.result = {'errors': []}
        with self.conn:
            self.conn.execute("INSERT INTO team_accounts(account_id,team_label,is_own,evidence,updated_at) "
                              "VALUES(?,'Agent Zero',1,'test','2026-10-22T00:00:00Z')", (OURS,))
        self.post(1, OURS, '2026-10-22T20:00:00Z', likes=2)
        self.post(2, OTHER, '2026-10-22T20:00:00Z', likes=9)
        self.post(3, OURS, '2026-10-10T20:00:00Z', likes=4)  # older than the window

    def post(self, post_id, author, created, likes):
        with self.conn:
            self.conn.execute("INSERT INTO posts(post_id,author_id,kind,created_at,first_seen,last_seen) "
                              "VALUES(?,?,'post',?,?,?)", (post_id, author, created, created, created))
            self.counter(post_id, likes, '2026-10-22T20:30:00Z')

    def counter(self, post_id, likes, at):
        with self.conn:
            self.conn.execute('INSERT INTO counter_snapshots(post_id,observed_at,like_count) VALUES(?,?,?)',
                              (post_id, at, likes))

    def likes(self, post_id=1):
        return [tuple(row) for row in self.conn.execute(
            'SELECT account_id,liked_at FROM post_likes WHERE post_id=? ORDER BY account_id', (post_id,))]

    def run_once(self, now=NOW):
        return engagement.collect(self.conn, self.transport, {}, self.result, now=now)

    def test_reads_likers_of_team_posts_in_the_window_only(self):
        self.transport.page('/api/posts/1/likes', [liker(11, '2026-10-22T20:05:00Z'), liker(12, '2026-10-22T20:01:00Z')])
        stats = self.run_once()
        self.assertEqual(self.likes(), [(11, '2026-10-22T20:05:00Z'), (12, '2026-10-22T20:01:00Z')])
        self.assertEqual(stats['likes_saved'], 2)
        self.assertFalse(any('/api/posts/2/' in call or '/api/posts/3/' in call for call in self.transport.calls))

    def test_likers_are_read_again_only_after_the_counter_rises(self):
        self.transport.page('/api/posts/1/likes', [liker(11, '2026-10-22T20:05:00Z'), liker(12, '2026-10-22T20:01:00Z')])
        self.run_once()
        self.run_once(NOW + 60)
        self.assertEqual(sum('/api/posts/1/likes' in call for call in self.transport.calls), 1)
        self.counter(1, 3, '2026-10-22T21:00:00Z')
        self.transport.page('/api/posts/1/likes', [liker(13, '2026-10-22T20:59:00Z'), liker(11, '2026-10-22T20:05:00Z')])
        self.run_once(NOW + 120)
        self.assertEqual([row[0] for row in self.likes()], [11, 12, 13])

    def test_first_read_follows_every_page_later_reads_stop_at_known_likers(self):
        self.counter(1, 3, '2026-10-22T21:00:00Z')
        self.transport.page('/api/posts/1/likes', [liker(13, 'a')], next_cursor='A')
        self.transport.page('/api/posts/1/likes', [liker(12, 'b')], cursor='A', next_cursor='B')
        self.transport.page('/api/posts/1/likes', [liker(11, 'c')], cursor='B')
        self.run_once()
        self.assertEqual([row[0] for row in self.likes()], [11, 12, 13])
        self.counter(1, 4, '2026-10-22T21:10:00Z')
        self.transport.page('/api/posts/1/likes', [liker(14, 'd'), liker(13, 'a')], next_cursor='A')
        self.transport.page('/api/posts/1/likes', [liker(12, 'b')], cursor='A', next_cursor='B')
        self.transport.calls.clear()
        self.run_once(NOW + 60)
        self.assertEqual(sum('/likes' in call for call in self.transport.calls), 2)  # second page adds nobody

    def test_unavailable_post_waits_an_hour(self):
        self.transport.routes[('/api/posts/1/likes', None, None)] = TransportError('gone', status=404)
        self.run_once()
        state = json.loads(self.conn.execute("SELECT value FROM checkpoints WHERE key='likers:1'").fetchone()[0])
        self.assertEqual((state['status'], state['http_status']), ('unavailable', 404))
        self.assertEqual(self.result['errors'][0]['stage'], 'likers')
        self.transport.calls.clear()
        self.run_once(NOW + 600)
        self.assertFalse(any('/api/posts/1/likes' in call for call in self.transport.calls))

    def test_budget_exhaustion_propagates_after_saving_pages(self):
        self.transport.request_budget = 1
        self.transport.page('/api/posts/1/likes', [liker(11, 'a')], next_cursor='A')
        with self.assertRaises(BudgetExceeded):
            self.run_once()
        self.assertEqual([row[0] for row in self.likes()], [11])
        self.assertIsNone(self.conn.execute("SELECT value FROM checkpoints WHERE key='likers:1'").fetchone())

    def test_followers_refresh_on_their_own_interval(self):
        self.transport.page('/api/accounts/6464/followers',
                            [{'account_id': 21, 'followed_at': '2026-10-22T19:53:39Z'}])
        # Team accounts also include blue_reports.OWN_ACCOUNT_IDS; read them all in one pass here.
        patcher = patch.object(engagement, 'FOLLOWER_ACCOUNTS_PER_RUN', 10)
        patcher.start()
        self.addCleanup(patcher.stop)
        stats = self.run_once()
        self.assertEqual(stats['follows_saved'], 1)
        self.assertEqual([tuple(row) for row in self.conn.execute(
            'SELECT account_id,follower_id,followed_at FROM account_followers')], [(6464, 21, '2026-10-22T19:53:39Z')])
        self.transport.calls.clear()
        self.run_once(NOW + 60)
        self.assertFalse(any('/followers' in call for call in self.transport.calls))
        self.run_once(NOW + engagement.FOLLOWER_REFRESH_SECONDS)
        self.assertTrue(any('/followers' in call for call in self.transport.calls))

    def test_withdrawn_like_keeps_its_row(self):
        self.transport.page('/api/posts/1/likes', [liker(11, 'a'), liker(12, 'b')])
        self.run_once()
        self.counter(1, 3, '2026-10-22T21:00:00Z')
        self.transport.page('/api/posts/1/likes', [liker(13, 'c'), liker(11, 'a')])
        self.run_once(NOW + 60)
        self.assertEqual([row[0] for row in self.likes()], [11, 12, 13])


class StageTests(BaseDatabase):
    def test_collector_runs_the_stage_in_the_auxiliary_lane(self):
        from unittest.mock import patch
        from ctn_data.collector import collect_stage
        from ctn_data.scheduler import CollectionScheduler
        with patch('ctn_data.engagement.collect') as collect, \
                patch('ctn_data.egress.choose_public_egress', return_value={}):
            result = collect_stage(self.path, {}, 'engagement')
        collect.assert_called_once()
        self.assertEqual(result['status'], 'completed')
        scheduler = CollectionScheduler(self.path, {'counter_limit': 0})
        from ctn_data import task_queue
        task_queue.ensure_schema(self.conn)
        scheduler._seed(self.conn)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM collection_tasks WHERE task_key='periodic:engagement'").fetchone())
        self.assertEqual(scheduler._period('engagement'), 60)


if __name__ == '__main__':
    import unittest
    unittest.main()
