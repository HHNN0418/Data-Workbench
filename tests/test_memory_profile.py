"""GET /api/diagnostics/memory: object counts, our caches and on-demand tracemalloc, behind the login (D124)."""
from pathlib import Path
import tempfile
import tracemalloc
import unittest

from fastapi.testclient import TestClient

from ctn_data import account_status, memory_profile  # noqa: F401 (account_status: its cache is reported)
from ctn_data.server import create_app
from tests.auth_helpers import login


BASE_URL = 'http://127.0.0.1:8765'


class SnapshotTests(unittest.TestCase):
    def tearDown(self):
        if tracemalloc.is_tracing():
            tracemalloc.stop()

    def test_counts_object_types_and_our_caches_with_tracemalloc_off(self):
        view = memory_profile.snapshot()
        self.assertLessEqual(len(view['types']), memory_profile.TOP_TYPES)
        self.assertIn('dict', [row['type'] for row in view['types']])
        self.assertGreater(view['gc']['tracked_objects'], 0)
        self.assertIn('account_status._cache', view['caches'])
        self.assertEqual(view['tracemalloc'], {'tracing': False})
        self.assertFalse(tracemalloc.is_tracing())

    def test_tracemalloc_runs_only_when_asked_and_lists_the_sites_holding_memory(self):
        self.assertTrue(memory_profile.snapshot('start')['tracemalloc']['tracing'])
        kept = [bytearray(4096) for _ in range(500)]  # 2 MB held while the top sites are read
        top = memory_profile.snapshot('top')['tracemalloc']['top']
        self.assertLessEqual(len(top), memory_profile.TOP_ALLOCATIONS)
        self.assertTrue(any(row['where'].startswith('test_memory_profile.py:') for row in top), top)
        del kept
        self.assertEqual(memory_profile.snapshot('stop')['tracemalloc'], {'tracing': False})

    def test_an_unknown_trace_action_is_refused(self):
        with self.assertRaises(ValueError):
            memory_profile.snapshot('always')

    def test_reading_changes_no_cache(self):
        before = memory_profile._caches()
        memory_profile.snapshot()
        self.assertEqual(memory_profile._caches(), before)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.client = TestClient(create_app(root / 'memory.db', backup_dir=root / 'backups'), base_url=BASE_URL)
        self.addCleanup(self.client.close)

    def test_it_needs_a_session_and_answers_once_signed_in(self):
        self.assertEqual(self.client.get('/api/diagnostics/memory').status_code, 401)
        login(self.client)
        response = self.client.get('/api/diagnostics/memory')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertLessEqual({'process', 'gc', 'types', 'caches', 'tracemalloc'}, set(response.json()))
        self.assertEqual(self.client.get('/api/diagnostics/memory?trace=always').status_code, 400)


if __name__ == '__main__':
    unittest.main()
