"""Fixed-clock counter scheduling; all remote responses are local fixtures."""
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch

from ctn_data.collector import (_config, _counter_schedule, _counter_windows,
                                _set_checkpoint, collect_once)
from ctn_data.ingest import save_counters, save_posts
from ctn_data.transport import BudgetExceeded, TransportError
from tests.test_collection import BaseDatabase, ScriptedTransport


NOW = '2026-10-19T12:00:00Z'


def minutes_before(minutes):
    return (datetime(2026, 10, 19, 12, tzinfo=timezone.utc) - timedelta(minutes=minutes)).isoformat().replace('+00:00', 'Z')


class CounterWindowTests(BaseDatabase):
    def setUp(self):
        super().setUp()
        ScriptedTransport.routes = {('social', '/api/posts', 'recent', None): {'items': [], 'next_cursor': None}}
        ScriptedTransport.calls = []

    def seed(self, pid, *, baseline_age=None, created_age=None):
        age = created_age if created_age is not None else (baseline_age + 2 if baseline_age is not None else 2)
        item = {'post_id': pid, 'author_id': 7, 'kind': 'post', 'body': 'public text',
                'created_at': minutes_before(age)}
        save_posts(self.conn, [item], 'fixture', minutes_before(age))
        if baseline_age is not None:
            self.snapshot(pid, minutes_before(baseline_age))
        ScriptedTransport.routes[('social', f'/api/posts/{pid}/counters', None, None)] = {
            'post_id': pid, 'like_count': 2, 'reply_count': 1, 'repost_count': 0, 'view_count': 10}
        self.conn.commit()
        return item

    def snapshot(self, pid, at):
        save_counters(self.conn, pid, {'post_id': pid, 'like_count': 1, 'reply_count': 0,
                                     'repost_count': 0, 'view_count': 5}, at)

    def cfg(self, **overrides):
        return _config({'counter_limit': 10, 'counter_new_share': .5,
                        'counter_recent_share': .5, 'counter_window_share': .4,
                        'max_pages': 1, 'collect_news': False, 'collect_trending': False,
                        'account_limit': 0, 'body_refresh_limit': 0, **overrides})

    def collect(self, transport=ScriptedTransport, **overrides):
        with patch('ctn_data.collector.utc_now', return_value=NOW), \
             patch('ctn_data.ingest.utc_now', return_value=NOW), \
             patch('ctn_data.collector.ReadOnlyTransport', transport), \
             patch('ctn_data.social_topics.collect_social_context'):
            return collect_once(self.path, self.cfg(**overrides))

    def test_due_windows_use_earliest_deadline_across_all_three_windows(self):
        self.seed(1, baseline_age=31)   # 30-minute deadline is five minutes away.
        self.seed(2, baseline_age=60)   # 60-minute deadline is twelve minutes away.
        self.seed(3, baseline_age=210)  # 180-minute deadline is six minutes away.
        due, metrics = _counter_windows(self.conn, now=NOW)
        self.assertEqual([(row['post_id'], row['window_minutes']) for row in due], [(1, 30), (3, 180), (2, 60)])
        self.assertEqual(metrics['queued'], 3)
        self.assertEqual([metrics['by_window'][str(w)]['queued'] for w in (30, 60, 180)], [1, 1, 1])

    def test_priority_starts_at_target_and_stops_at_tolerance_deadline(self):
        self.seed(1, baseline_age=29)
        self.seed(2, baseline_age=36)
        self.seed(3, baseline_age=37)
        due, metrics = _counter_windows(self.conn, now=NOW)
        self.assertEqual([row['post_id'] for row in due], [2])
        self.assertEqual(metrics['by_window']['30']['upcoming'], 1)
        self.assertEqual(metrics['by_window']['30']['missed'], 1)

    def test_existing_sample_inside_tolerance_is_satisfied_even_before_target(self):
        self.seed(1, baseline_age=60)
        self.snapshot(1, minutes_before(11))  # baseline + 49 minutes, within 60 +/- 12.
        due, metrics = _counter_windows(self.conn, now=NOW)
        self.assertEqual(due, [])
        self.assertEqual(metrics['by_window']['60']['satisfied'], 1)

    def test_no_baseline_late_baseline_and_future_publication_never_enter_windows(self):
        self.seed(1, baseline_age=None, created_age=62)
        self.seed(2, baseline_age=60, created_age=80)
        self.seed(3, baseline_age=60, created_age=-5)
        self.seed(4, baseline_age=60)
        self.conn.execute("UPDATE posts SET created_at='2026-10-19T10:58:00' WHERE post_id=4")
        due, metrics = _counter_windows(self.conn, now=NOW)
        self.assertEqual(due, [])
        self.assertEqual(metrics['queued'], 0)
        self.assertEqual(metrics['missed'], 0)

    def test_permanent_http_failure_retains_existing_fifteen_minute_backoff(self):
        self.seed(1, baseline_age=60)
        with self.conn:
            _set_checkpoint(self.conn, 'counter_attempt:1', {'status': 'failed', 'http_status': 404}, minutes_before(6))
        due, metrics = _counter_windows(self.conn, now=NOW)
        self.assertEqual(due, [])
        self.assertEqual(metrics['backoff'], 1)
        self.assertEqual(_counter_schedule(self.conn, self.cfg(), now=NOW), [])

    def test_recent_browser_counter_skips_remote_refresh_with_fixed_clock(self):
        self.seed(1, baseline_age=500)
        save_counters(self.conn, 1, {'post_id': 1, 'like_count': 4}, minutes_before(.5), source='browser:counters')
        self.conn.commit()
        result = self.collect(counter_limit=1)
        self.assertEqual(result['counter_sampling']['tracked']['selected'], 0)
        self.assertEqual(result['counter_sampling']['tracked']['attempted'], 0)
        self.assertEqual(result['counters'], 0)
        self.assertEqual(result['requests'], 1)

    def test_failed_attempt_persists_five_minute_backoff_across_connections(self):
        self.seed(1, baseline_age=60)
        with self.conn:
            _set_checkpoint(self.conn, 'counter_attempt:1', {'status': 'failed'}, minutes_before(2))
        due, metrics = _counter_windows(self.conn, now=NOW)
        self.assertEqual(due, [])
        self.assertEqual(metrics['by_window']['60']['backoff'], 1)
        self.assertEqual(_counter_schedule(self.conn, self.cfg(), now=NOW), [])
        later = '2026-10-19T12:04:00Z'
        due, _ = _counter_windows(self.conn, now=later)
        self.assertEqual([row['post_id'] for row in due], [1])

    def test_busy_default_queues_keep_new_windows_and_ordinary_history(self):
        for pid in range(1, 11):
            self.seed(pid, baseline_age=60)
        for pid in range(101, 111):
            self.seed(pid)
        for pid in range(201, 211):
            self.seed(pid, baseline_age=500)
        scheduled = _counter_schedule(self.conn, self.cfg(), now=NOW)
        self.assertEqual(len(scheduled), 10)
        self.assertEqual(sum(bool(row.get('window_minutes')) for row in scheduled), 4)
        self.assertEqual(sum(row['sampling_queue'] == 'new' for row in scheduled), 5)
        self.assertEqual(sum(row['sampling_queue'] == 'tracked' and not row.get('window_minutes') for row in scheduled), 1)
        self.assertEqual(len({row['post_id'] for row in scheduled}), 10)

    def test_empty_queues_lend_their_slots_without_duplicate_windows(self):
        for pid in range(1, 7):
            self.seed(pid, baseline_age=60)
        scheduled = _counter_schedule(self.conn, self.cfg(), now=NOW)
        self.assertEqual(len(scheduled), 6)
        self.assertEqual(len({row['post_id'] for row in scheduled}), 6)
        for pid in range(201, 209):
            self.seed(pid, baseline_age=500)
        scheduled = _counter_schedule(self.conn, self.cfg(), now=NOW)
        self.assertEqual(len(scheduled), 10)
        self.assertEqual(sum(row['sampling_queue'] == 'new' for row in scheduled), 0)

    def test_three_slots_keep_one_new_one_window_and_one_ordinary_history(self):
        self.seed(1, baseline_age=60)
        self.seed(2, baseline_age=60)
        self.seed(100)
        self.seed(101)
        self.seed(200, baseline_age=500)
        scheduled = _counter_schedule(self.conn, self.cfg(counter_limit=3), now=NOW)
        self.assertEqual(sum(bool(row.get('window_minutes')) for row in scheduled), 1)
        self.assertEqual(sum(row['sampling_queue'] == 'new' for row in scheduled), 1)
        self.assertIn(200, [row['post_id'] for row in scheduled])
        result = self.collect(counter_limit=3)
        self.assertEqual(result['counter_sampling']['windows']['saved'], 1)
        self.assertEqual(result['counter_sampling']['new']['saved'], 1)
        self.assertEqual(result['counter_sampling']['tracked']['saved'], 2)
        self.assertEqual(result['counters'], 3)

    def test_single_counter_slot_alternates_due_windows_with_new_baselines(self):
        self.seed(1, baseline_age=60)
        self.seed(2, baseline_age=60)
        self.seed(100)
        self.seed(101)
        first = self.collect(counter_limit=1, request_budget=2)
        second = self.collect(counter_limit=1, request_budget=2)
        third = self.collect(counter_limit=1, request_budget=2)
        self.assertEqual([r['counter_sampling']['windows']['saved'] for r in (first, second, third)], [1, 0, 1])
        self.assertEqual(second['counter_sampling']['new']['saved'], 1)
        self.assertTrue(all(r['requests'] == 2 and r['status'] == 'completed' for r in (first, second, third)))

    def test_one_remaining_counter_request_also_rotates_with_large_counter_limit(self):
        self.seed(1, baseline_age=60)
        self.seed(2, baseline_age=60)
        self.seed(100)
        self.seed(101)
        first = self.collect(counter_limit=20, request_budget=2)
        second = self.collect(counter_limit=20, request_budget=2)
        self.assertEqual(first['counter_sampling']['windows']['saved'], 1)
        self.assertEqual(second['counter_sampling']['new']['saved'], 1)
        self.assertTrue(all(r['requests'] == 2 for r in (first, second)))

    def test_failed_windows_rotate_and_backoff_is_visible(self):
        self.seed(1, baseline_age=60)
        self.seed(2, baseline_age=60)
        ScriptedTransport.routes[('social', '/api/posts/2/counters', None, None)] = TransportError('HTTP 404', status=404)
        first = self.collect(counter_limit=1, request_budget=2)
        second = self.collect(counter_limit=1, request_budget=2)
        self.assertEqual(first['counter_sampling']['windows']['attempted'], 1)
        self.assertEqual(first['counter_sampling']['windows']['saved'], 0)
        self.assertEqual(second['counter_sampling']['windows']['backoff'], 1)
        self.assertEqual(second['counter_sampling']['windows']['saved'], 1)
        checkpoint = json.loads(self.conn.execute("SELECT value FROM checkpoints WHERE key='counter_attempt:2'").fetchone()[0])
        self.assertEqual(checkpoint['status'], 'failed')

    def test_request_budget_one_prioritizes_due_window_without_expanding_budget(self):
        self.seed(1, baseline_age=60)
        result = self.collect(counter_limit=10, request_budget=1)
        self.assertEqual(result['counter_sampling']['windows']['saved'], 1)
        self.assertEqual(result['requests'], 1)
        self.assertEqual(ScriptedTransport.calls[0][1], '/api/posts/1/counters')

    def test_feed_backfill_reserves_counter_requests_and_checkpoint_continuity(self):
        self.seed(1, baseline_age=500)
        item = self.seed(100)
        ScriptedTransport.routes[('social', '/api/posts', 'recent', None)] = {'items': [item], 'next_cursor': 'older'}
        ScriptedTransport.routes[('social', '/api/posts', 'recent', 'older')] = {'items': [], 'next_cursor': None}
        result = self.collect(counter_limit=2, request_budget=3, max_pages=5)
        self.assertEqual(result['counter_sampling']['tracked']['saved'], 1)
        self.assertEqual(result['counter_sampling']['new']['saved'], 1)
        self.assertEqual(result['requests'], 3)
        self.assertTrue(result['feeds']['latest']['paused_for_counters'])
        state = json.loads(self.conn.execute("SELECT value FROM checkpoints WHERE key='feed:latest'").fetchone()[0])
        self.assertEqual(state['cursor'], 'older')

    def test_slow_feed_leaves_time_for_counter_stage(self):
        self.seed(100)
        item = {'post_id': 100, 'created_at': minutes_before(2)}
        ScriptedTransport.routes[('social', '/api/posts', 'recent', None)] = {'items': [item], 'next_cursor': 'older'}

        class SlowFeedTransport(ScriptedTransport):
            def __init__(self, conn, **kwargs):
                super().__init__(conn, **kwargs)
                self.now = 0
                self.deadline = kwargs['deadline_seconds']
                self.clock = lambda: self.now

            def get_json(self, service, path, **kwargs):
                result = super().get_json(service, path, **kwargs)
                if path.startswith('/api/posts?'):
                    self.now += 15
                return result

        result = self.collect(transport=SlowFeedTransport, counter_limit=1,
                              request_budget=10, max_pages=5, deadline_seconds=20)
        self.assertEqual(result['counter_sampling']['new']['saved'], 1)
        self.assertTrue(result['feeds']['latest']['paused_for_counters'])
        self.assertEqual(result['requests'], 2)

    def test_feed_bucket_wait_does_not_suppress_counters_with_budget_left(self):
        self.seed(1, baseline_age=500)

        class FeedCooldownTransport(ScriptedTransport):
            def get_json(self, service, path, **kwargs):
                if path.startswith('/api/posts?'):
                    raise BudgetExceeded('server wait exceeds the remaining collection budget')
                return super().get_json(service, path, **kwargs)

        result = self.collect(transport=FeedCooldownTransport, counter_limit=1, request_budget=3)
        self.assertEqual(result['counter_sampling']['tracked']['saved'], 1)
        self.assertEqual(result['requests'], 1)
        self.assertEqual(result['status'], 'partial')

    def test_fresh_browser_sample_overrides_old_backend_attempt(self):
        self.seed(1,baseline_age=500)
        _set_checkpoint(self.conn,'counter_attempt:1',{'status':'saved'},minutes_before(100))
        save_counters(self.conn,1,{'like_count':2},minutes_before(.5),source='browser:counters')
        self.conn.commit()
        selected=_counter_schedule(self.conn,self.cfg(counter_window_share=0),now=NOW)
        self.assertNotIn(1,[row['post_id'] for row in selected])

    def test_unavailable_recent_baseline_backs_off_fifteen_minutes(self):
        self.seed(1)
        _set_checkpoint(self.conn,'counter_attempt:1',{'status':'failed','http_status':404},minutes_before(6))
        self.conn.commit()
        selected=_counter_schedule(self.conn,self.cfg(counter_window_share=0),now=NOW)
        self.assertNotIn(1,[row['post_id'] for row in selected])
