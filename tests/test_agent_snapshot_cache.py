"""GET /api/agents: concurrent polls share one snapshot and any write drops it."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from ctn_data.server import create_app
from tests.auth_helpers import login


class SnapshotCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = TestClient(create_app(Path(self.temp.name) / 'local.sqlite3'), base_url='http://127.0.0.1:8765')
        self.addCleanup(self.client.close)
        self.headers = {'X-CTN-Token': login(self.client)}
        self.controller = self.client.app.state.agent_control
        self.addCleanup(self.controller.stop)

    def test_polls_share_one_snapshot_until_a_write(self):
        real = self.controller.snapshot
        with patch.object(self.controller, 'snapshot', side_effect=real) as snapshot:
            self.assertEqual(self.client.get('/api/agents').status_code, 200)
            self.assertEqual(self.client.get('/api/agents').status_code, 200)
            self.assertEqual(snapshot.call_count, 1)
            self.assertEqual(snapshot.call_args.kwargs, {'compact': True})
            # A write drops the cached snapshot and answers with a receipt only.
            response = self.client.post('/api/agents/settings', json={'enabled': False}, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {'ok': True})
            calls = snapshot.call_count
            self.assertFalse(self.client.get('/api/agents').json()['settings']['enabled'])
            self.assertEqual(snapshot.call_count, calls + 1)

    def test_concurrent_polls_compute_once(self):
        started, release = threading.Event(), threading.Event()
        real = self.controller.snapshot

        def slow(**kwargs):
            started.set()
            release.wait(5)
            return real(**kwargs)
        with patch.object(self.controller, 'snapshot', side_effect=slow) as snapshot:
            results = []
            threads = [threading.Thread(target=lambda: results.append(self.client.get('/api/agents').status_code))
                       for _ in range(3)]
            for thread in threads:
                thread.start()
            self.assertTrue(started.wait(5))
            release.set()
            for thread in threads:
                thread.join(10)
            self.assertEqual(results, [200, 200, 200])
            self.assertEqual(snapshot.call_count, 1)

    def test_write_during_a_poll_is_not_hidden_by_the_cache(self):
        started, release = threading.Event(), threading.Event()
        real = self.controller.snapshot

        def stale(**kwargs):
            value = real(**kwargs)  # read before the write below commits
            started.set()
            release.wait(5)
            return value
        with patch.object(self.controller, 'snapshot', side_effect=stale):
            poll = threading.Thread(target=lambda: self.client.get('/api/agents'))
            poll.start()
            self.assertTrue(started.wait(5))
            saved = self.client.post('/api/agents/save', json={'name': 'Maya', 'username': 'cache_maya'},
                                     headers=self.headers)
            self.assertEqual(saved.status_code, 200, saved.text)
            release.set()
            poll.join(10)
        self.assertEqual([row['username'] for row in self.client.get('/api/agents').json()['agents']], ['cache_maya'])

    def test_snapshot_is_sent_compressed_when_accepted(self):
        response = self.client.get('/api/agents', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['content-encoding'], 'gzip')
        self.assertIn('settings', response.json())
        plain = self.client.get('/api/agents', headers={'Accept-Encoding': 'identity'})
        self.assertNotIn('content-encoding', plain.headers)
        self.assertEqual(json.loads(plain.content), response.json())


if __name__ == '__main__':
    unittest.main()
