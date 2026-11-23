"""Collection regressions using temporary databases and mocked remote I/O."""
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

from starlette.requests import Request

from ctn_data import collector, coordination, task_queue
from ctn_data.db import connect, initialize, utc_now
from ctn_data.ingest import save_posts
from ctn_data.transport import BudgetExceeded, ReadOnlyTransport
from ctn_data.worker_api import register_worker_routes


class RouteApp:
    """Call registered sync endpoints directly, without a TestClient socketpair."""
    def __init__(self):
        self.routes = {}

    def post(self, path, **kwargs):
        def register(function):
            self.routes[path] = function
            return function
        return register


class TemporaryDatabase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='ctn-collection-regression-')
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'test.sqlite3'
        initialize(self.path)
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        for target in ('socket.create_connection', 'socket.socket.connect', 'socket.socket.connect_ex'):
            blocker = patch(target, side_effect=AssertionError('Network forbidden in collection regression'))
            blocker.start()
            self.addCleanup(blocker.stop)


class CounterFailureTests(TemporaryDatabase):
    def setUp(self):
        super().setUp()
        self.app = RouteApp()
        register_worker_routes(self.app, self.path)
        with self.conn:
            self.conn.execute("INSERT INTO meta(key,value) VALUES('auto_collection',?)", ('{"enabled":true}',))
        authentication = patch('ctn_data.worker_api.worker_authorized', return_value=True)
        authentication.start()
        self.addCleanup(authentication.stop)

    def call(self, action, body=None):
        path = '/api/worker/' + action
        request = Request({'type': 'http', 'method': 'POST', 'path': path,
            'headers': [], 'scheme': 'http', 'server': ('localhost', 8765), 'query_string': b''})
        return self.app.routes[path](request, {'worker': 'regression', **(body or {})})

    def seed(self, post_ids):
        with self.conn:
            save_posts(self.conn, [{'post_id': pid} for pid in post_ids], 'test')
            for pid in post_ids:
                task_queue.enqueue(self.conn, 'counter:' + str(pid), 'counter', {'post_id': pid}, due_at=1)

    def result(self, task, status):
        observation_id = hashlib.sha256((task['task_key'] + ':' + task['lease_token']).encode()).hexdigest()
        return {**task, 'observation_id': observation_id, 'observed_at': utc_now(),
                'status': status, 'retry_after': 120, 'payload': None}

    def test_non429_counter_failures_finish_and_keep_planner_backoff(self):
        statuses = (0, 400, 401, 403, 404, 408, 410, 500, 502, 503, 504)
        self.seed(range(1, len(statuses) + 1))
        for status in statuses:
            with self.subTest(status=status):
                task = self.call('claim')['task']
                body = self.result(task, status)
                self.assertTrue(self.call('result', body)['accepted'])
                row = self.conn.execute('SELECT state,lease_token FROM collection_tasks WHERE task_key=?', (task['task_key'],)).fetchone()
                self.assertEqual(row['state'], 'done')
                self.assertIsNone(row['lease_token'])
                attempt = self.conn.execute('SELECT value FROM checkpoints WHERE key=?', ('counter_attempt:' + str(task['post_id']),)).fetchone()
                self.assertEqual(json.loads(attempt['value'])['http_status'], status)
                self.assertTrue(self.call('result', body)['duplicate'])
        self.assertEqual(collector.plan_counter_tasks(self.conn, {'counter_window_share': 0}), 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM api_call_logs').fetchone()[0], len(statuses))

    def test_full_failed_queue_leaves_room_for_a_new_post(self):
        capacity = collector.COUNTER_QUEUE_LIMIT
        self.seed(range(1, capacity + 1))
        for _ in range(capacity):
            task = self.call('claim')['task']
            self.assertTrue(self.call('result', self.result(task, 404))['accepted'])
        new_id = capacity + 1
        with self.conn:
            save_posts(self.conn, [{'post_id': new_id, 'created_at': utc_now()}], 'test')
        self.assertEqual(collector.plan_counter_tasks(self.conn, {'counter_window_share': 0}), 1)
        task = self.call('claim')['task']
        self.assertEqual(task['post_id'], new_id)

    def test_counter_429_stays_pending_until_shared_cooldown(self):
        self.seed([1])
        task = self.call('claim')['task']
        before = time.time()
        self.assertTrue(self.call('result', self.result(task, 429))['accepted'])
        row = self.conn.execute('SELECT state,due_at FROM collection_tasks WHERE task_key=?', (task['task_key'],)).fetchone()
        self.assertEqual(row['state'], 'pending')
        self.assertGreaterEqual(row['due_at'], before + 120)
        self.assertGreaterEqual(coordination.cooldowns(self.conn)['counters'] / 1000, before + 119.999)
        self.assertIsNone(self.call('claim')['task'])

    def test_feed_failure_keeps_its_periodic_retry(self):
        with self.conn:
            task_queue.enqueue(self.conn, 'periodic:latest', 'latest', due_at=1)
        task = self.call('claim', {'capabilities': ['counter', 'feed']})['task']
        before = time.time()
        self.assertTrue(self.call('result', self.result(task, 404))['accepted'])
        row = self.conn.execute('SELECT state,due_at FROM collection_tasks WHERE task_key=?', (task['task_key'],)).fetchone()
        self.assertEqual(row['state'], 'pending')
        self.assertGreaterEqual(row['due_at'], before + 900)

    def test_failed_result_rolls_back_attempt_receipt_and_completion_together(self):
        self.seed([1])
        task = self.call('claim')['task']
        body = self.result(task, 404)
        with patch('ctn_data.task_queue.finish', side_effect=RuntimeError('simulated commit failure')):
            with self.assertRaises(RuntimeError):
                self.call('result', body)
        self.assertTrue(task_queue.owned(self.conn, task['task_key'], task['lease_token']))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM api_call_logs').fetchone()[0], 0)
        for key in ('counter_attempt:1', 'worker_result:' + body['observation_id']):
            self.assertIsNone(self.conn.execute('SELECT value FROM checkpoints WHERE key=?', (key,)).fetchone())


class Response:
    status = 200
    headers = {}

    def __init__(self, url, payload):
        self.url, self.payload = url, payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.url

    def read(self, limit):
        return json.dumps(self.payload).encode()[:limit]


class NewsOpener:
    def __init__(self, count, failed_sites=()):
        self.sites = [{'sub_url': 'site' + str(i)} for i in range(1, count + 1)]
        self.failed_sites, self.calls = set(failed_sites), []

    def open(self, request, timeout=None):
        path = urlsplit(request.full_url).path
        self.calls.append(path)
        article = {'slug': 'story', 'title': 'Story', 'published_at': '2026-10-19T00:00:00Z'}
        if path == '/index.json':
            payload = {'sites': self.sites}
        elif '/articles/' in path:
            payload = {'article': {**article, 'body': 'Full body'}}
        else:
            if path in self.failed_sites:
                raise HTTPError(request.full_url, 500, 'simulated failure', {}, None)
            payload = {'articles': [article]}
        return Response(request.full_url, payload)


class NewsBudgetTests(TemporaryDatabase):
    def run_news(self, opener, **config):
        def factory(conn, **kwargs):
            return ReadOnlyTransport(conn, opener=opener, coordinated=False, **kwargs)
        with patch('ctn_data.collector.ReadOnlyTransport', side_effect=factory), \
             patch('ctn_data.egress.choose_public_egress', return_value={}):
            return collector.collect_stage(self.path, config, 'news')

    def test_seven_sites_receive_bodies_without_exceeding_eight_requests(self):
        opener = NewsOpener(7)
        result = self.run_news(opener)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['requests'], 8)
        self.assertEqual(sum('/articles/' in path for path in opener.calls), 2)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_articles WHERE body IS NOT NULL').fetchone()[0], 2)

    def test_eight_sites_and_their_bodies_progress_across_fresh_runs(self):
        opener = NewsOpener(8)
        first_start = None
        for turn in range(5):
            before = len(opener.calls)
            result = self.run_news(opener)
            self.assertEqual(result['status'], 'completed')
            self.assertLessEqual(result['requests'], 8)
            current = opener.calls[before:]
            self.assertLessEqual(sum('/articles/' in path for path in current), 2)
            if turn == 0:
                first_start = current[1]
            elif turn == 1:
                self.assertNotEqual(current[1], first_start)
                self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_articles').fetchone()[0], 8)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_articles WHERE body IS NOT NULL').fetchone()[0], 8)

    def test_site_limit_remains_a_per_run_limit_while_sites_rotate(self):
        opener = NewsOpener(3)
        for _ in range(3):
            result = self.run_news(opener, site_limit=1)
            self.assertEqual(result['news'], 1)
            self.assertEqual(result['requests'], 3)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_articles WHERE body IS NOT NULL').fetchone()[0], 3)

    def test_zero_news_limit_uses_only_metadata_requests_and_rotates(self):
        opener = NewsOpener(10)
        for _ in range(2):
            result = self.run_news(opener, news_limit=0)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['requests'], 8)
        self.assertFalse(any('/articles/' in path for path in opener.calls))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_articles').fetchone()[0], 10)

    def test_empty_site_directory_finishes_after_index(self):
        opener = NewsOpener(0)
        result = self.run_news(opener)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['requests'], 1)
        self.assertEqual(result['news'], 0)

    def test_front_failure_preserves_body_budget_and_rotates(self):
        opener = NewsOpener(8, failed_sites={'/site1.json'})
        first = self.run_news(opener)
        self.assertEqual(first['status'], 'partial')
        self.assertEqual(first['requests'], 8)
        self.assertEqual(sum('/articles/' in path for path in opener.calls), 2)
        before = len(opener.calls)
        self.run_news(opener)
        self.assertEqual(opener.calls[before + 1], '/site6.json')

    def test_budget_interruption_advances_persisted_site_rotation(self):
        class InterruptedTransport:
            request_budget = 8
            attempts = 0

            def get_json(self, service, path):
                if path == '/index.json':
                    self.attempts += 1
                    return {'sites': [{'sub_url': 'site1'}, {'sub_url': 'site2'}]}
                raise BudgetExceeded('simulated time budget exhaustion')

        with self.assertRaises(BudgetExceeded):
            collector._news(self.conn, InterruptedTransport(), collector._config({}), {'news': 0, 'errors': []})
        opener = NewsOpener(2)
        result = self.run_news(opener, site_limit=1)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(opener.calls[1], '/site2.json')


if __name__ == '__main__':
    unittest.main()
