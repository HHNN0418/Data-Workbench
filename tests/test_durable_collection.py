"""Durable collector regressions with temporary databases and no remote I/O."""
from datetime import datetime
import hashlib
import json
from unittest.mock import patch

from starlette.requests import Request

from ctn_data import task_queue
from ctn_data.collector import (_config, _counter_schedule, _counter_windows, _refresh,
                                execute_counter_task, plan_counter_tasks)
from ctn_data.db import utc_now
from ctn_data.ingest import save_counters, save_posts
from ctn_data.scheduler import CollectionScheduler
from ctn_data.server import create_app
from tests.test_collection import BaseDatabase, ScriptedTransport
from tests.test_counter_schedule import NOW, minutes_before


class DurableCounterTests(BaseDatabase):
    def setUp(self):
        super().setUp()
        ScriptedTransport.routes = {}
        ScriptedTransport.calls = []
        self.config = {'counter_limit': 1, 'counter_new_share': .5, 'counter_window_share': 0}

    def seed(self, pid, *, baseline_age=None):
        created = minutes_before(baseline_age + 2 if baseline_age is not None else 2)
        with self.conn:
            save_posts(self.conn, [{'post_id': pid, 'created_at': created}], 'test', created)
            if baseline_age is not None:
                save_counters(self.conn, pid, {'like_count': 0}, minutes_before(baseline_age))
        ScriptedTransport.routes[('social', f'/api/posts/{pid}/counters', None, None)] = {'like_count': 1}

    def state(self):
        row = self.conn.execute("SELECT value FROM checkpoints WHERE key='counter_scheduler'").fetchone()
        return json.loads(row['value']) if row else {}

    def run_one(self, config=None):
        plan_counter_tasks(self.conn, config or self.config)
        task = task_queue.claim(self.conn, 'test-direct', ('counter',))
        self.assertIsNotNone(task)
        result = execute_counter_task(self.path, config or self.config, task)
        self.assertEqual(result['counters'], 1)
        return task['payload']

    def test_durable_single_slot_alternates_history_and_new(self):
        for pid in (1, 2, 3):
            self.seed(pid, baseline_age=500)
        self.seed(100)
        with patch('ctn_data.collector.utc_now', return_value=NOW), \
             patch('ctn_data.collector.ReadOnlyTransport', ScriptedTransport):
            first, second = self.run_one(), self.run_one()
        self.assertEqual([first['sampling_queue'], second['sampling_queue']], ['tracked', 'new'])
        self.assertEqual(second['post_id'], 100)
        self.assertEqual(self.state()['single_next'], 'tracked')

    def test_default_queue_with_one_remaining_slot_alternates(self):
        self.seed(1, baseline_age=500)
        self.seed(2)
        with self.conn:
            for pid in range(1000, 1199):
                task_queue.enqueue(self.conn, f'counter:{pid}', 'counter', {'post_id': pid}, due_at=4e9)
        config = {'counter_limit': 250, 'counter_new_share': .4, 'counter_window_share': 0}
        with patch('ctn_data.collector.utc_now', return_value=NOW), \
             patch('ctn_data.collector.ReadOnlyTransport', ScriptedTransport):
            first, second = self.run_one(config), self.run_one(config)
        self.assertTrue(first['single_slot'])
        self.assertEqual([first['sampling_queue'], second['sampling_queue']], ['tracked', 'new'])

    def test_queue_keeps_one_batch_buffered_but_stops_before_a_third(self):
        # None of these posts has a snapshot: the cap cannot pass merely
        # because the planner's final freshness check discards candidates.
        with self.conn:
            save_posts(self.conn, [{'post_id': pid, 'created_at': minutes_before(2)}
                                   for pid in range(1, 301)], 'test', minutes_before(2))
        config = {'counter_limit': 300, 'counter_new_share': 1, 'counter_window_share': 0}
        with patch('ctn_data.collector.utc_now', return_value=NOW):
            first_added = plan_counter_tasks(self.conn, config)
            self.assertGreater(first_added, 0)
            self.assertLessEqual(first_added, 100)
            held = task_queue.claim(self.conn, 'slow-worker', ('counter',))
            self.assertIsNotNone(held)
            second_added = plan_counter_tasks(self.conn, config)
            self.assertGreater(second_added, 0)
            self.assertLessEqual(second_added, 100)
            active = self.conn.execute("SELECT COUNT(*) FROM collection_tasks WHERE kind='counter' AND state IN ('pending','leased')").fetchone()[0]
            self.assertEqual(active, 200)
            self.assertEqual(plan_counter_tasks(self.conn, config), 0)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM collection_tasks WHERE kind='counter' AND state IN ('pending','leased')").fetchone()[0], 200)
            self.assertTrue(task_queue.owned(self.conn, held['task_key'], held['lease_token']))

    def test_durable_windows_alternate_with_new_baselines(self):
        self.seed(1, baseline_age=60)
        self.seed(2, baseline_age=60)
        self.seed(100)
        config = {**self.config, 'counter_window_share': .4}
        epoch = datetime.fromisoformat(NOW.replace('Z', '+00:00')).timestamp()
        with patch('ctn_data.collector.utc_now', return_value=NOW), \
             patch('ctn_data.collector.time.time', return_value=epoch), \
             patch('ctn_data.collector.ReadOnlyTransport', ScriptedTransport):
            first, second = self.run_one(config), self.run_one(config)
        self.assertEqual(first['window_minutes'], 60)
        self.assertEqual(second['sampling_queue'], 'new')

    def test_failed_completion_rolls_back_sample_and_rotation(self):
        self.seed(1, baseline_age=500)
        with patch('ctn_data.collector.utc_now', return_value=NOW), \
             patch('ctn_data.collector.ReadOnlyTransport', ScriptedTransport):
            plan_counter_tasks(self.conn, self.config)
            task = task_queue.claim(self.conn, 'test', ('counter',))
            with patch('ctn_data.task_queue.finish', side_effect=RuntimeError('simulated failure')):
                with self.assertRaises(RuntimeError):
                    execute_counter_task(self.path, self.config, task)
        self.assertEqual(self.state(), {})
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM counter_snapshots').fetchone()[0], 1)
        self.assertTrue(task_queue.owned(self.conn, task['task_key'], task['lease_token']))

    def test_latest_sample_orders_fractional_seconds_and_old_offsets(self):
        for pid in (1, 2):
            self.seed(pid)
        with self.conn:
            for pid, at in ((1, '2026-10-19T11:00:00Z'), (1, '2026-10-19T22:00:00.500000+11:00'),
                            (2, '2026-10-19T11:00:00.250000Z')):
                self.conn.execute('INSERT INTO counter_snapshots(post_id,observed_at) VALUES(?,?)', (pid, at))
            self.conn.execute("UPDATE posts SET first_seen='2026-10-18T00:00:00Z'")
        selected = _counter_schedule(self.conn, _config({**self.config, 'counter_new_share': 0}), now=NOW)
        self.assertEqual(selected[0]['post_id'], 2)

    def test_recent_browser_sample_in_old_offset_format_is_reused(self):
        self.seed(1, baseline_age=500)
        with self.conn:
            save_counters(self.conn, 1, {'like_count': 5}, minutes_before(.5), source='browser:counters')
            self.conn.execute("UPDATE counter_snapshots SET observed_at='2026-10-19T22:59:30+11:00' WHERE like_count=5")
        selected = _counter_schedule(self.conn, _config(self.config), now=NOW)
        self.assertEqual(selected, [])

    def test_excluded_window_does_not_hide_next_due_window(self):
        self.seed(1, baseline_age=31)
        self.seed(2, baseline_age=32)
        selected, _ = _counter_windows(self.conn, limit=1, now=NOW, exclude=(2,))
        self.assertEqual([row['post_id'] for row in selected], [1])

    def test_zero_counter_limit_skips_queries_but_still_refreshes_accounts(self):
        result = {'accounts': 0, 'errors': []}
        config = _config({'counter_limit': 0, 'account_limit': 0})
        with patch('ctn_data.collector._counter_windows', side_effect=AssertionError('counter work disabled')), \
             patch('ctn_data.collector._account_candidates', return_value=[]) as accounts:
            _refresh(self.conn, object(), config, result, set())
        accounts.assert_called_once()


class WorkerRuntimeSwitchTests(BaseDatabase):
    def app(self, enabled):
        schedulers = []
        def factory(*args, **kwargs):
            scheduler = CollectionScheduler(*args, **kwargs)
            schedulers.append(scheduler)
            return scheduler
        with patch('ctn_data.scheduler.CollectionScheduler', side_effect=factory):
            app = create_app(self.path, {'counter_limit': 0}, auto_collect=enabled)
        self.scheduler = schedulers[0]
        self.scheduler._seed(self.conn)
        self.routes = {route.path: route.endpoint for route in app.routes}

    def call(self, action, body=None):
        path = '/api/worker/' + action
        request = Request({'type': 'http', 'method': 'POST', 'path': path, 'headers': [],
                           'scheme': 'http', 'server': ('localhost', 8765), 'query_string': b''})
        # Only authentication is replaced. Runtime gating, leases, transactions
        # and result validation use the real app endpoints without a socket.
        with patch('ctn_data.worker_api.worker_authorized', return_value=True):
            return self.routes[path](request, {'worker': 'test-runtime', **(body or {})})

    def test_no_auto_blocks_proxy_without_replacing_saved_preference(self):
        with self.conn:
            self.conn.execute("INSERT INTO meta(key,value) VALUES('auto_collection',?)",
                              (json.dumps({'enabled': True, 'interval_seconds': 60}),))
        self.app(False)
        self.assertFalse(self.scheduler.enabled)
        self.assertTrue(self.call('claim', {'capabilities': ['feed']})['paused'])
        saved = json.loads(self.conn.execute("SELECT value FROM meta WHERE key='auto_collection'").fetchone()[0])
        self.assertTrue(saved['enabled'])
        self.scheduler.configure(True, 60)
        task = self.call('claim', {'capabilities': ['feed']})['task']
        self.assertIsNotNone(task)
        self.assertTrue(self.call('permit', task)['allowed'])
        self.scheduler.configure(False, 60)
        self.assertEqual(self.call('permit', task)['reason'], 'paused')

    def test_fresh_auto_start_allows_proxy_and_saved_pause_is_respected(self):
        self.app(True)
        self.assertIsNotNone(self.call('claim', {'capabilities': ['feed']})['task'])
        self.scheduler.configure(False, 60)
        self.app(True)
        self.assertFalse(self.scheduler.enabled)
        self.assertTrue(self.call('claim', {'capabilities': ['feed']})['paused'])

    def test_feed_failure_is_retained_separately_from_counter_success(self):
        self.app(True)
        feed = self.call('claim', {'capabilities': ['feed']})['task']
        def result(task, status, payload=None):
            return {**task, 'observation_id': hashlib.sha256((task['task_key'] + ':' + task['lease_token']).encode()).hexdigest(),
                    'observed_at': utc_now(), 'status': status, 'payload': payload}
        self.assertTrue(self.call('result', result(feed, 403))['accepted'])
        with self.conn:
            save_posts(self.conn, [{'post_id': 1}], 'test')
            task_queue.enqueue(self.conn, 'counter:1', 'counter', {'post_id': 1})
        counter = self.call('claim')['task']
        self.assertTrue(self.call('result', result(counter, 200, {'like_count': 1}))['accepted'])
        status = json.loads(self.conn.execute("SELECT value FROM meta WHERE key='worker_status:test-runtime'").fetchone()[0])
        self.assertEqual(status['last_http_status'], 200)
        self.assertEqual(status['last_feed_http_status'], 403)

    def test_proxy_result_commits_rotation_once_and_rolls_back_with_receipt(self):
        self.app(True)
        with self.conn:
            save_posts(self.conn, [{'post_id': 1}], 'test')
            task_queue.enqueue(self.conn, 'counter:1', 'counter',
                               {'post_id': 1, 'sampling_queue': 'tracked', 'single_slot': True})
        task = self.call('claim')['task']
        result = {**task, 'observation_id': hashlib.sha256((task['task_key'] + ':' + task['lease_token']).encode()).hexdigest(),
                  'observed_at': utc_now(), 'status': 200, 'payload': {'like_count': 1}}
        with patch('ctn_data.task_queue.finish', side_effect=RuntimeError('simulated failure')):
            with self.assertRaises(RuntimeError):
                self.call('result', result)
        self.assertIsNone(self.conn.execute("SELECT value FROM checkpoints WHERE key='counter_scheduler'").fetchone())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM counter_snapshots').fetchone()[0], 0)
        self.assertTrue(self.call('result', result)['accepted'])
        state = json.loads(self.conn.execute("SELECT value FROM checkpoints WHERE key='counter_scheduler'").fetchone()[0])
        self.assertEqual(state['single_next'], 'new')
        self.assertTrue(self.call('result', result)['duplicate'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM counter_snapshots').fetchone()[0], 1)
