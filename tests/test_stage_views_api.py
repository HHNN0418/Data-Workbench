"""The seven read-only stage-page endpoints and their shared background cache."""
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from ctn_data import view_cache
from ctn_data.db import connect, initialize
from ctn_data.server import create_app
from tests.auth_helpers import login

ENDPOINTS = ('/api/results/exposure', '/api/results/attribution', '/api/pipeline/steps', '/api/processing/annotations',
             '/api/strategy/rules', '/api/settings/summary', '/api/maintenance/history')
TABLES = ('meta', 'posts', 'post_annotations', 'post_attribution', 'agent_audit_events', 'llm_calls',
          'official_activity', 'account_annotations', 'checkpoints')


def snapshot(db):
    conn = connect(db)
    try:
        digest = hashlib.sha256()
        for table in TABLES:
            for row in conn.execute(f'SELECT * FROM {table} ORDER BY 1'):
                digest.update(json.dumps(list(row), default=str).encode())
        return digest.hexdigest()
    finally:
        conn.close()


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db = root / 'test.sqlite3'
        initialize(self.db)
        conn = connect(self.db)
        with conn:
            conn.execute("INSERT INTO official_activity(occurred_at,event_type,message,fingerprint,track,points_decimal,"
                         "parse_status) VALUES('2026-10-22T09:19:11Z','SCORE_NARRATIVE',"
                         "'+99 points: NPC engagement: 24 views and 3 likes','f1','red','99','parsed')")
            conn.execute("INSERT INTO post_attribution(post_id,agent_id,published_at,action,fact_ids_json,captured_at) "
                         "VALUES(1,'r1','2026-10-22T09:00:00Z','post','[]','2026-10-22T09:05:00Z')")
        conn.close()
        web = root / 'dist'
        web.mkdir()
        (web / 'index.html').write_text('<html></html>')
        reports = root / 'reports'
        reports.mkdir()
        (reports / 'unified-20261022a-deployment.json').write_text(
            json.dumps({'started_at': '2026-10-22T13:00:00+11:00', 'result': 'ready', 'purpose': 'test'}),
            encoding='utf-8-sig')
        self.client = TestClient(create_app(self.db, backup_dir=root / 'backups', web_root=web),
                                 base_url='http://127.0.0.1:8765')
        self.client.app.state.reports_dir = reports
        login(self.client)

    def test_every_endpoint_reads_without_writing(self):
        before = snapshot(self.db)
        for path in ENDPOINTS:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, response.text)
                data = response.json()
                self.assertTrue(data['read_only'])
                self.assertFalse(data['stale'])
                self.assertIn(self.client.post(path).status_code, (403, 405))
        self.assertEqual(snapshot(self.db), before)
        history = self.client.get('/api/maintenance/history').json()['history']
        self.assertEqual([entry['purpose'] for entry in history], ['test'])
        for page in ('/results/exposure', '/results/attribution', '/agents/pipeline', '/processing', '/strategy',
                     '/settings', '/system/maintenance'):
            self.assertEqual(self.client.get(page).status_code, 200)


class CachedViewTests(unittest.TestCase):
    def test_serves_the_cached_result_and_refreshes_once_in_the_background(self):
        calls, release = [], threading.Event()

        def build():
            calls.append(1)
            if len(calls) > 1:
                release.wait(5)
            return {'n': len(calls)}
        view = view_cache.CachedView(build, seconds=60, name='test')
        self.assertEqual(view.get(), {'n': 1, 'stale': False})
        with mock.patch.object(view_cache.time, 'monotonic', return_value=view._cached[0] + 61):
            self.assertEqual(view.get(), {'n': 1, 'stale': True})
            self.assertEqual(view.get(), {'n': 1, 'stale': True})
        release.set()
        for thread in threading.enumerate():
            if thread.name == 'ctn-view-test':
                thread.join(5)
        self.assertEqual(len(calls), 2)
        self.assertEqual(view.get(), {'n': 2, 'stale': False})

    def test_a_failed_refresh_keeps_the_previous_result(self):
        calls = []

        def build():
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError('database busy')
            return {'n': 1}
        view = view_cache.CachedView(build, seconds=0, name='failing')
        self.assertEqual(view.get()['n'], 1)
        with self.assertLogs('ctn_data.view_cache', level='ERROR'):
            self.assertEqual(view.get(), {'n': 1, 'stale': True})
            for thread in threading.enumerate():
                if thread.name == 'ctn-view-failing':
                    thread.join(5)
        self.assertEqual(len(calls), 2)
        self.assertEqual(view._cached[1], {'n': 1})
        self.assertFalse(view._refreshing)


if __name__ == '__main__':
    unittest.main()
