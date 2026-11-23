import io
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
import os
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request

from ctn_data.collector import collect_once, collection_lock
from ctn_data.db import connect, initialize
from ctn_data.transport import (BudgetExceeded, ReadOnlyTransport, TransportError,
                                _OfficialRedirect, validate_path)


class Response:
    def __init__(self, payload, url='https://social.legitreal.com/api/posts', headers=None, body=None):
        self.status = 200
        self.headers = headers or {}
        self.url = url
        self.body = json.dumps(payload).encode() if body is None else body

    def geturl(self):
        return self.url

    def read(self, length):
        return self.body[:length]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Opener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, delay):
        self.waits.append(delay)
        self.now += delay


class BaseDatabase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.path = Path(self.directory.name) / 'test.sqlite3'
        initialize(self.path)
        self.conn = connect(self.path)

    def tearDown(self):
        self.conn.close()
        self.directory.cleanup()


class TransportTests(BaseDatabase):
    def transport(self, outcomes, **kwargs):
        self.opener = Opener(outcomes)
        self.clock = FakeClock()
        return ReadOnlyTransport(self.conn, opener=self.opener, sleeper=self.clock.sleep,
                                 clock=self.clock, coordinated=False, **kwargs)

    def test_get_allowlist_rejects_urls_auth_queries_and_writes(self):
        transport = self.transport([])
        for service, path in [('social', 'https://evil.test/api/posts'), ('social', '//evil.test/posts'),
                              ('social', '/api/auth/login'), ('social', '/api/posts/1/view'),
                              ('social', '/api/posts?token=secret'), ('news', '/../private.json'),
                              ('social', '/api/posts?sort=home'), ('news', '/index.json?token=secret')]:
            with self.subTest(path=path), self.assertRaises(TransportError):
                transport.get_json(service, path)
        self.assertEqual(len(self.opener.calls), 0)

    def test_every_retry_logged_without_queries_or_secrets(self):
        rate = urllib.error.HTTPError('https://ignored/?token=SECRET', 429, 'secret',
                                      {'Retry-After': '2', 'X-RateLimit-Limit': '10',
                                       'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '10'}, io.BytesIO(b'secret'))
        server = urllib.error.HTTPError('https://ignored/', 503, 'secret', {}, io.BytesIO(b'secret'))
        transport = self.transport([rate, server, Response({'items': []})])
        self.assertEqual(transport.get_json('social', '/api/posts?sort=recent&limit=1&cursor=opaque'), {'items': []})
        logs = self.conn.execute('SELECT * FROM api_call_logs ORDER BY id').fetchall()
        self.assertEqual([row['status'] for row in logs], [429, 503, 200])
        self.assertEqual([row['retry_count'] for row in logs], [0, 1, 2])
        self.assertTrue(all(row['path'] == '/api/posts' for row in logs))
        self.assertNotIn('SECRET', str([dict(row) for row in logs]))
        self.assertEqual(self.clock.waits, [2, 2])
        self.assertTrue(all(req.get_method() == 'GET' for req, _ in self.opener.calls))
        self.assertTrue(all(not req.has_header('Authorization') for req, _ in self.opener.calls))

    def test_network_exception_is_redacted_and_retried(self):
        transport = self.transport([urllib.error.URLError('token=TOPSECRET'), Response({'ok': True})])
        self.assertTrue(transport.get('social', '/api/posts')['ok'])
        errors = [row[0] for row in self.conn.execute('SELECT error FROM api_call_logs')]
        self.assertEqual(errors, ['URLError', None])

    def test_long_server_wait_and_request_budget_are_finite(self):
        error = urllib.error.HTTPError('x', 429, 'wait', {'Retry-After': '120'}, io.BytesIO())
        transport = self.transport([error], max_wait_seconds=3)
        with self.assertRaises(BudgetExceeded):
            transport.get('social', '/api/posts')
        self.assertEqual(len(self.opener.calls), 1)
        self.assertEqual(self.clock.waits, [])
        transport = self.transport([Response({})], request_budget=1)
        transport.get('social', '/api/accounts')
        with self.assertRaises(BudgetExceeded):
            transport.get('social', '/api/accounts')
        self.assertEqual(len(self.opener.calls), 1)

    def test_retry_after_survives_a_new_run(self):
        error = urllib.error.HTTPError('x', 429, 'wait', {'Retry-After': '120'}, io.BytesIO())
        first = self.transport([error], max_wait_seconds=0)
        with self.assertRaises(BudgetExceeded):
            first.get('social', '/api/posts')
        second = self.transport([], max_wait_seconds=0)
        with self.assertRaises(BudgetExceeded):
            second.get('social', '/api/posts')
        self.assertEqual(self.opener.calls, [])

    def test_bad_json_is_logged_without_retry(self):
        transport = self.transport([Response({}, body=b'<html>SECRET</html>')])
        with self.assertRaisesRegex(TransportError, 'invalid JSON'):
            transport.get('social', '/api/posts')
        row = self.conn.execute('SELECT * FROM api_call_logs').fetchone()
        self.assertEqual(row['status'], 200)
        self.assertEqual(row['error'], 'invalid JSON response')
        self.assertEqual(len(self.opener.calls), 1)

    def test_redirects_cannot_escape_or_create_hidden_requests(self):
        handler = _OfficialRedirect('social')
        req = urllib.request.Request('https://social.legitreal.com/api/posts', method='GET')
        for url in ('https://evil.test/api/posts', 'http://social.legitreal.com/api/posts',
                    'https://social.legitreal.com/api/auth/me', 'https://social.legitreal.com/api/posts?sort=recent'):
            with self.subTest(url=url), self.assertRaises(TransportError):
                handler.redirect_request(req, None, 302, 'Found', {}, url)

    def test_counter_budget_is_shared_between_ids(self):
        transport = self.transport([
            Response({}, 'https://social.legitreal.com/api/posts/1/counters',
                     {'X-RateLimit-Limit': '300', 'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '300'}),
            Response({}, 'https://social.legitreal.com/api/posts/2/counters')])
        transport.get('social', '/api/posts/1/counters')
        transport.get('social', '/api/posts/2/counters')
        self.assertEqual(self.clock.waits, [1])


class ScriptedTransport:
    routes = {}
    calls = []

    def __init__(self, conn, **kwargs):
        self.attempts = 0
        self.waited = 0
        self.request_budget = kwargs.get('request_budget', 160)

    def get_json(self, service, path, **kwargs):
        if self.attempts >= self.request_budget:
            raise BudgetExceeded('collection request or time budget exhausted')
        self.attempts += 1
        self.calls.append((service, path))
        parsed = urllib.parse.urlsplit(path)
        query = urllib.parse.parse_qs(parsed.query)
        key = (service, parsed.path, query.get('sort', [None])[0], query.get('cursor', [None])[0])
        if key not in self.routes:
            raise AssertionError('unexpected fixture request ' + str(key))
        value = self.routes[key]
        if isinstance(value, BaseException):
            raise value
        return value


def post(pid, author=7):
    return {'post_id': pid, 'author_id': author, 'body': 'public text', 'kind': 'post',
            'created_at': f'2026-10-19T07:{pid:02d}:00Z'}


class CollectorTests(BaseDatabase):
    def setUp(self):
        super().setUp()
        ScriptedTransport.routes = {}
        ScriptedTransport.calls = []

    def collect(self, **overrides):
        config = {'collect_news': False, 'collect_trending': False, 'max_pages': 1,
                  'counter_limit': 0, 'account_limit': 0, **overrides}
        with patch('ctn_data.collector.ReadOnlyTransport', ScriptedTransport), \
             patch('ctn_data.social_topics.collect_social_context'):
            return collect_once(self.path, config)

    def test_resume_completed_page_and_incremental_watermark(self):
        routes = ScriptedTransport.routes
        routes[('social', '/api/posts', 'recent', None)] = {'items': [post(3)], 'next_cursor': 'older'}
        routes[('social', '/api/posts', 'recent', 'older')] = {'items': [post(2), post(1)], 'next_cursor': None}
        first = self.collect()
        second = self.collect()
        self.assertEqual(first['status'], 'completed')
        self.assertEqual(second['posts'], 2)
        self.assertIn('cursor=older', ScriptedTransport.calls[1][1])
        routes[('social', '/api/posts', 'recent', None)] = {'items': [post(4), post(3)], 'next_cursor': 'old-again'}
        third = self.collect(max_pages=3)
        self.assertEqual(third['status'], 'completed')
        self.assertEqual(len(ScriptedTransport.calls), 3)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 4)
        checkpoint = json.loads(self.conn.execute("SELECT value FROM checkpoints WHERE key='feed:latest'").fetchone()[0])
        self.assertIsNone(checkpoint['cursor'])

    def test_independent_incremental_cursor_catches_gap_during_history_backfill(self):
        routes = ScriptedTransport.routes
        def page(cursor, ids, next_cursor):
            routes[('social', '/api/posts', 'recent', cursor)] = {'items': [post(i) for i in ids], 'next_cursor': next_cursor}
        page(None, [5], 'old')
        self.collect()
        page(None, [10], 'gap')
        page('old', [4], 'older')
        self.collect(latest_incremental_pages=1)
        state = json.loads(self.conn.execute("SELECT value FROM checkpoints WHERE key='feed:latest:live'").fetchone()[0])
        self.assertEqual(state['cursor'], 'gap')
        page('gap', [9,8], 'gap2')
        page('older', [3], 'oldest')
        self.collect(latest_incremental_pages=1)
        page('gap2', [7,6,5], 'not-needed')
        page('oldest', [2,1], None)
        self.collect(latest_incremental_pages=1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 10)
        live = json.loads(self.conn.execute("SELECT value FROM checkpoints WHERE key='feed:latest:live'").fetchone()[0])
        history = json.loads(self.conn.execute("SELECT value FROM checkpoints WHERE key='feed:latest'").fetchone()[0])
        self.assertIsNone(live['cursor'])
        self.assertEqual(live['watermark'][1],10)
        self.assertEqual(history['watermark'][1],5)

    def test_completed_history_does_not_duplicate_live_head_requests(self):
        routes=ScriptedTransport.routes
        routes[('social','/api/posts','recent',None)]={'items':[post(1)],'next_cursor':None}
        self.collect()
        ScriptedTransport.calls=[]
        routes[('social','/api/posts','recent',None)]={'items':[post(2),post(1)],'next_cursor':None}
        result=self.collect(latest_incremental_pages=1)
        self.assertEqual(len(ScriptedTransport.calls),1)
        self.assertTrue(result['feeds']['latest']['history_complete'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0],2)

    def test_latest_trending_dedup_accounts_and_counter_history(self):
        routes = ScriptedTransport.routes
        for sort in ('recent', 'trending'):
            routes[('social', '/api/posts', sort, None)] = {'items': [post(2), post(1)], 'next_cursor': None}
        for pid in (1, 2):
            routes[('social', f'/api/posts/{pid}/counters', None, None)] = {'post_id': pid, 'like_count': 1}
        routes[('social', '/api/accounts/7', None, None)] = {'account_id': 7, 'username': 'public', 'verified': False}
        result = self.collect(collect_trending=True, counter_limit=2, account_limit=10)
        self.assertEqual((result['posts'], result['counters'], result['accounts']), (2, 2, 1))
        account_calls = [path for service, path in ScriptedTransport.calls if '/accounts/' in path]
        self.assertEqual(account_calls, ['/api/accounts/7'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM feed_observations').fetchone()[0], 4)
        self.collect(collect_trending=True, counter_limit=2, account_limit=10)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM counter_snapshots').fetchone()[0], 4)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM team_accounts').fetchone()[0], 0)

    def test_invalid_page_does_not_advance_checkpoint(self):
        routes = ScriptedTransport.routes
        key = ('social', '/api/posts', 'recent', None)
        routes[key] = {'items': [post(3)], 'next_cursor': 'older'}
        self.collect()
        routes[('social', '/api/posts', 'recent', 'older')] = {'items': [post(2), {'post_id': 'bad'}], 'next_cursor': 'even-older'}
        result = self.collect()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 1)
        state = json.loads(self.conn.execute("SELECT value FROM checkpoints WHERE key='feed:latest'").fetchone()[0])
        self.assertEqual(state['cursor'], 'older')

    def test_os_lock_stale_run_recovery_and_finished_status(self):
        with collection_lock(self.path):
            result = self.collect()
            self.assertEqual(result['status'], 'busy')
            self.assertIsNone(result['run_id'])
        self.conn.execute("INSERT INTO collection_runs(started_at,status) VALUES ('old','running')")
        self.conn.commit()
        ScriptedTransport.routes[('social', '/api/posts', 'recent', None)] = {'items': [], 'next_cursor': None}
        result = self.collect()
        self.assertEqual(result['status'], 'completed')
        rows = self.conn.execute('SELECT * FROM collection_runs ORDER BY id').fetchall()
        self.assertEqual([row['status'] for row in rows], ['interrupted', 'completed'])
        self.assertTrue(all(row['finished_at'] for row in rows))

    def test_os_lock_releases_after_process_termination(self):
        script = ('from ctn_data.collector import collection_lock\nimport sys,time\n'
                  'with collection_lock(sys.argv[1]):\n print("locked",flush=True)\n time.sleep(30)\n')
        child = subprocess.Popen([sys.executable, '-c', script, str(self.path)],
            cwd=Path(__file__).resolve().parent.parent, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'locked')
            self.assertEqual(self.collect()['status'], 'busy')
        finally:
            child.terminate()
            child.communicate(timeout=5)
        ScriptedTransport.routes[('social', '/api/posts', 'recent', None)] = {'items': [], 'next_cursor': None}
        self.assertEqual(self.collect()['status'], 'completed')

    def test_failed_counter_rotates_instead_of_starving_other_posts(self):
        routes = ScriptedTransport.routes
        routes[('social', '/api/posts', 'recent', None)] = {'items': [post(2), post(1)], 'next_cursor': None}
        routes[('social', '/api/posts/2/counters', None, None)] = TransportError('HTTP 404', status=404)
        routes[('social', '/api/posts/1/counters', None, None)] = {'post_id': 1, 'like_count': 3}
        self.assertEqual(self.collect(counter_limit=1)['status'], 'partial')
        second = self.collect(counter_limit=1)
        self.assertEqual(second['counters'], 1)
        self.assertEqual(second['status'], 'completed')

    def counter_fixtures(self, ids):
        for pid in ids:
            ScriptedTransport.routes[('social', f'/api/posts/{pid}/counters', None, None)] = {
                'post_id': pid, 'like_count': pid, 'view_count': pid * 2}
            ScriptedTransport.routes[('social', f'/api/posts/{pid}', None, None)] = post(pid)

    def test_continuous_new_posts_still_produce_historical_repeat_samples(self):
        self.counter_fixtures(range(1, 21))
        head = ('social', '/api/posts', 'recent', None)
        ScriptedTransport.routes[head] = {'items': [post(2), post(1)], 'next_cursor': None}
        first = self.collect(counter_limit=2)
        self.assertEqual(first['counter_sampling']['new']['saved'], 2)
        for begin in (3, 9, 15):
            # Six newly observed posts per run exceeds the total sampling limit.
            ScriptedTransport.routes[head] = {
                'items': [post(pid) for pid in range(begin + 5, begin - 1, -1)], 'next_cursor': None}
            result = self.collect(counter_limit=2)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['counter_sampling']['new']['saved'], 1)
            self.assertEqual(result['counter_sampling']['tracked']['saved'], 1)
        repeats = self.conn.execute('''SELECT post_id,COUNT(*) AS observations
            FROM counter_snapshots WHERE post_id IN (1,2) GROUP BY post_id''').fetchall()
        self.assertTrue(all(row['observations'] >= 2 for row in repeats))

    def test_single_counter_slot_alternates_across_runs(self):
        self.counter_fixtures(range(1, 15))
        head = ('social', '/api/posts', 'recent', None)
        ScriptedTransport.routes[head] = {'items': [post(2), post(1)], 'next_cursor': None}
        self.collect(counter_limit=1)
        ScriptedTransport.routes[head] = {'items': [post(pid) for pid in range(8, 2, -1)], 'next_cursor': None}
        second = self.collect(counter_limit=1)
        self.assertEqual(second['counter_sampling']['tracked']['saved'], 1)
        ScriptedTransport.routes[head] = {'items': [post(pid) for pid in range(14, 8, -1)], 'next_cursor': None}
        third = self.collect(counter_limit=1)
        self.assertEqual(third['counter_sampling']['new']['saved'], 1)

    def test_tight_request_budget_saves_historical_counter_before_new_baseline(self):
        self.counter_fixtures(range(1, 9))
        head = ('social', '/api/posts', 'recent', None)
        ScriptedTransport.routes[head] = {'items': [post(2), post(1)], 'next_cursor': None}
        self.collect(counter_limit=2)
        ScriptedTransport.routes[head] = {'items': [post(pid) for pid in range(8, 2, -1)], 'next_cursor': None}
        result = self.collect(counter_limit=4, request_budget=2)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['counter_sampling']['tracked']['saved'], 1)
        self.assertEqual(result['counter_sampling']['new']['saved'], 0)
        self.assertEqual(result['requests'], 2)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM counter_snapshots').fetchone()[0], 3)

    def test_empty_new_queue_lends_quota_to_historical_checks(self):
        self.counter_fixtures(range(1, 5))
        head = ('social', '/api/posts', 'recent', None)
        ScriptedTransport.routes[head] = {'items': [post(pid) for pid in range(4, 0, -1)], 'next_cursor': None}
        self.collect(counter_limit=4)
        second = self.collect(counter_limit=4)
        self.assertEqual(second['counter_sampling']['tracked']['saved'], 4)
        self.assertEqual(second['counter_sampling']['new']['selected'], 0)

    def test_head_refresh_during_backfill_does_not_skip_the_gap(self):
        routes = ScriptedTransport.routes
        head_key = ('social', '/api/posts', 'recent', None)
        routes[head_key] = {'items': [post(5)], 'next_cursor': 'older'}
        self.collect()
        routes[head_key] = {'items': [post(10)], 'next_cursor': 'gap'}
        routes[('social', '/api/posts', 'recent', 'older')] = {'items': [post(4)], 'next_cursor': None}
        second = self.collect(max_pages=2)
        self.assertTrue(second['feeds']['latest']['head_refreshed'])
        state = json.loads(self.conn.execute("SELECT value FROM checkpoints WHERE key='feed:latest'").fetchone()[0])
        self.assertEqual(state['watermark'][1], 5)
        routes[('social', '/api/posts', 'recent', 'gap')] = {
            'items': [post(pid) for pid in range(9, 4, -1)], 'next_cursor': 'ignore-after-boundary'}
        third = self.collect(max_pages=2)
        self.assertEqual(third['status'], 'completed')
        ids = {row[0] for row in self.conn.execute('SELECT post_id FROM posts')}
        self.assertTrue(set(range(4, 11)).issubset(ids))

    def test_news_uses_sdk_json_paths_and_preserves_front_and_full(self):
        routes = ScriptedTransport.routes
        routes[('social', '/api/posts', 'recent', None)] = {'items': [], 'next_cursor': None}
        site = {'sub_url': 'kingstonia-herald', 'name': 'Herald'}
        article = {'slug': 'story', 'title': 'Story', 'published_at': '2026-10-20T00:00:00Z'}
        routes[('news', '/index.json', None, None)] = {'sites': [site]}
        routes[('news', '/kingstonia-herald.json', None, None)] = {'articles': [article]}
        routes[('news', '/kingstonia-herald/articles/story.json', None, None)] = {'article': {**article, 'body': 'Full story'}}
        result = self.collect(collect_news=True)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['news'], 1)
        row = self.conn.execute('SELECT * FROM news_articles').fetchone()
        self.assertEqual(row['body'], 'Full story')
        self.assertEqual(row['site'], 'kingstonia-herald')

    def test_partial_error_records_status_and_does_not_erase_posts(self):
        routes = ScriptedTransport.routes
        routes[('social', '/api/posts', 'recent', None)] = {'items': [post(1)], 'next_cursor': None}
        routes[('social', '/api/posts/1/counters', None, None)] = TransportError('HTTP 404', status=404)
        result = self.collect(counter_limit=1)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['errors'][0]['status'], 404)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM counter_snapshots').fetchone()[0], 0)

    def test_untrusted_config_and_zero_budget_rejected_before_network(self):
        with self.assertRaises(ValueError):
            self.collect(base_url='https://evil.test')
        with self.assertRaises(ValueError):
            self.collect(request_budget=0)
        for share in (-.1, 1.1, True):
            with self.assertRaises(ValueError):
                self.collect(counter_new_share=share)
        self.assertEqual(ScriptedTransport.calls, [])


if __name__ == '__main__':
    unittest.main()
