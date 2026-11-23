"""Analysis reuse survives navigation/restarts without retaining stale data forever."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ctn_data.analysis_cache import AnalysisCache
from ctn_data.db import connect, initialize, utc_now


class AnalysisProbe:
    """A counted analysis with a controllable expensive operation and failure."""

    def __init__(self, *, payload_size=0):
        self.calls = 0
        self.fail = False
        self.payload_size = payload_size
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.lock = threading.Lock()

    def __call__(self, path, params):
        with self.lock:
            self.calls += 1
            serial = self.calls
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise TimeoutError('Test analysis was not released')
        if self.fail:
            raise RuntimeError('Analysis temporarily unavailable')
        with closing(connect(path)) as conn:
            count = conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0]
        return {'generated_at': f'2026-10-20T00:00:{serial:02d}Z',
                'summary': {'posts': count}, 'serial': serial,
                'payload': 'x' * self.payload_size}


class AnalysisCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'analysis.sqlite3'
        initialize(self.path)
        self.probe = AnalysisProbe()

    def tearDown(self):
        self.temporary.cleanup()

    def cache(self, **kwargs):
        return AnalysisCache(self.path, analyze_fn=self.probe, **kwargs)

    def add_post(self, path=None):
        with closing(connect(path or self.path)) as conn, conn:
            conn.execute("INSERT INTO posts(post_id,first_seen,last_seen) VALUES(1,?,?)",
                         ('2026-10-20T00:00:00Z', '2026-10-20T00:00:00Z'))

    def test_result_survives_new_cache_instance_with_original_analysis_time(self):
        first = self.cache().get({'sections': 'core', 'include_exportable': False})
        reused = self.cache().get({'sections': 'core', 'include_exportable': False})
        self.assertEqual(self.probe.calls, 1)
        self.assertFalse(first['cache']['hit'])
        self.assertTrue(reused['cache']['hit'])
        self.assertEqual(reused['generated_at'], first['generated_at'])
        self.assertEqual(reused['cache']['stored_at'], first['cache']['stored_at'])
        self.assertTrue(reused['cache']['policy'])

    def test_recent_result_is_reused_but_manual_refresh_bypasses_freshness_window(self):
        cache = self.cache()
        first = cache.get()
        self.add_post()
        reused = cache.get()
        refreshed = cache.get(refresh=True)
        after_refresh = self.cache().get()
        self.assertEqual(first['summary']['posts'], 0)
        self.assertEqual(reused['summary']['posts'], 0)
        self.assertTrue(reused['cache']['hit'])
        self.assertEqual(refreshed['summary']['posts'], 1)
        self.assertEqual(self.probe.calls, 2)
        self.assertFalse(refreshed['cache']['hit'])
        self.assertEqual(after_refresh['serial'], refreshed['serial'])

    def test_new_data_is_included_when_persisted_result_reaches_expiry(self):
        with patch('ctn_data.analysis_cache.utc_now', return_value='2026-10-20T00:00:00Z'):
            first = self.cache().get()
        self.add_post()
        with patch('ctn_data.analysis_cache.utc_now', return_value='2026-10-20T00:00:59Z'):
            reused = self.cache().get()
        self.assertTrue(reused['cache']['hit'])
        self.assertEqual(reused['summary']['posts'], 0)
        # Reopening after a full minute must update without refresh=true.
        with patch('ctn_data.analysis_cache.utc_now', return_value='2026-10-20T00:01:00Z'):
            updated = self.cache().get()
            reopened = self.cache().get()
        self.assertFalse(updated['cache']['hit'])
        self.assertEqual(updated['summary']['posts'], 1)
        self.assertNotEqual(updated['generated_at'], first['generated_at'])
        self.assertEqual(updated['cache']['policy'], 'ttl')
        self.assertEqual(updated['cache']['max_age_seconds'], 60)
        self.assertTrue(reopened['cache']['hit'])
        self.assertEqual(self.probe.calls, 2)

    def test_future_cache_timestamp_does_not_prevent_updates_after_clock_correction(self):
        with patch('ctn_data.analysis_cache.utc_now', return_value='2026-10-20T00:02:00Z'):
            self.cache().get()
        self.add_post()
        with patch('ctn_data.analysis_cache.utc_now', return_value='2026-10-20T00:01:00Z'):
            updated = self.cache().get()
        self.assertFalse(updated['cache']['hit'])
        self.assertEqual(updated['summary']['posts'], 1)

    def test_failed_automatic_update_preserves_cache_and_retries_next_read(self):
        with patch('ctn_data.analysis_cache.utc_now', return_value='2026-10-20T00:00:00Z'):
            original = self.cache().get()
        self.add_post()
        self.probe.fail = True
        with patch('ctn_data.analysis_cache.utc_now', return_value='2026-10-20T00:01:00Z'):
            with self.assertRaisesRegex(RuntimeError, 'temporarily unavailable'):
                self.cache().get()
            with closing(connect(self.path)) as conn:
                stored_at = conn.execute('SELECT created_at FROM analysis_cache').fetchone()[0]
            self.assertEqual(stored_at, original['cache']['stored_at'])
            self.probe.fail = False
            recovered = self.cache().get()
        self.assertFalse(recovered['cache']['hit'])
        self.assertEqual(recovered['summary']['posts'], 1)

    def test_equivalent_query_spellings_share_a_persisted_result(self):
        first = self.cache().get({'window_minutes': 60, 'tolerance_minutes': 12,
                                  'sections': 'sampling,core', 'include_exportable': False,
                                  'since': '2026-10-20T11:00:00+11:00'})
        reused = self.cache().get({'window_minutes': '60.0', 'tolerance_minutes': '12',
                                   'sections': 'core,sampling', 'include_exportable': 'false',
                                   'since': '2026-10-20T00:00:00Z'})
        self.assertEqual(self.probe.calls, 1)
        self.assertTrue(reused['cache']['hit'])
        self.assertEqual(first['serial'], reused['serial'])

    def test_refresh_invalidates_sibling_views_but_preserves_other_scopes(self):
        cache = self.cache()
        variants = [
            {'own': 'all', 'sections': 'core', 'include_exportable': False},
            {'own': 'all', 'sections': 'growth', 'include_exportable': False},
            {'own': 'all', 'sections': 'sampling', 'focus_post_id': 7},
            {'own': 'all', 'sections': 'sampling', 'sample_stage': 'total'},
            {'own': 'all', 'sections': 'all', 'include_exportable': True},
            {'own': 'all', 'sections': 'core', 'detail_limit': 50},
        ]
        for params in variants:
            cache.get(params)
        unrelated = [{'own': 'own', 'sections': 'core'},
                     {'own': 'all', 'author_id': 9, 'sections': 'core'}]
        saved = [cache.get(params)['serial'] for params in unrelated]
        refreshed = cache.get(variants[0], refresh=True)
        self.assertFalse(refreshed['cache']['hit'])
        for params in variants[1:]:
            result = cache.get(params)
            self.assertFalse(result['cache']['hit'], params)
        for params, serial in zip(unrelated, saved):
            result = self.cache().get(params)
            self.assertTrue(result['cache']['hit'], params)
            self.assertEqual(result['serial'], serial)

    def test_failed_refresh_preserves_previous_result_and_sibling_views(self):
        cache = self.cache()
        core = {'sections': 'core'}
        growth = {'sections': 'growth'}
        originals = [cache.get(params) for params in (core, growth)]
        self.probe.fail = True
        with self.assertRaisesRegex(RuntimeError, 'temporarily unavailable'):
            cache.get(core, refresh=True)
        self.probe.fail = False
        for params, original in zip((core, growth), originals):
            reused = self.cache().get(params)
            self.assertTrue(reused['cache']['hit'])
            self.assertEqual(reused['serial'], original['serial'])
        self.assertEqual(self.probe.calls, 3)

    def test_unserializable_refresh_does_not_replace_a_usable_result(self):
        original = self.cache().get()
        invalid = AnalysisCache(self.path, analyze_fn=lambda *_: {'metric': float('nan')})
        with self.assertRaises(ValueError):
            invalid.get(refresh=True)
        reused = self.cache().get()
        self.assertTrue(reused['cache']['hit'])
        self.assertEqual(reused['serial'], original['serial'])

    def test_simultaneous_cold_reads_share_one_analysis(self):
        cache = self.cache()
        self.probe.release.clear()
        workers = 6
        ready = threading.Barrier(workers + 1)

        def read():
            ready.wait(timeout=10)
            return cache.get({'sections': 'core'})

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(read) for _ in range(workers)]
            ready.wait(timeout=10)
            try:
                self.assertTrue(self.probe.entered.wait(timeout=5))
            finally:
                self.probe.release.set()
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(self.probe.calls, 1)
        self.assertEqual({result['serial'] for result in results}, {1})

    def test_simultaneous_refresh_requests_share_one_replacement(self):
        cache = self.cache()
        cache.get({'sections': 'core'})
        self.probe.entered.clear()
        self.probe.release.clear()
        workers = 6
        arrived = threading.Barrier(workers + 1)
        threads = set()
        clock_lock = threading.Lock()

        def arrival_clock():
            # Every request records its arrival before any replacement finishes.
            instant = utc_now()
            identifier = threading.get_ident()
            with clock_lock:
                first_call = identifier not in threads
                threads.add(identifier)
            if first_call:
                arrived.wait(timeout=10)
            return instant

        with patch('ctn_data.analysis_cache.utc_now', side_effect=arrival_clock):
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(cache.get, {'sections': 'core'}, refresh=True)
                           for _ in range(workers)]
                arrived.wait(timeout=10)
                try:
                    self.assertTrue(self.probe.entered.wait(timeout=5))
                finally:
                    self.probe.release.set()
                results = [future.result(timeout=10) for future in futures]
        self.assertEqual(self.probe.calls, 2)
        self.assertEqual({result['serial'] for result in results}, {2})

    def test_simultaneous_reads_of_expired_result_share_one_replacement(self):
        cache = self.cache()
        with patch('ctn_data.analysis_cache.utc_now', return_value='2026-10-19T00:00:00Z'):
            cache.get()
        self.add_post()
        self.probe.entered.clear()
        self.probe.release.clear()
        workers = 6
        ready = threading.Barrier(workers + 1)

        def read():
            ready.wait(timeout=10)
            return cache.get()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(read) for _ in range(workers)]
            ready.wait(timeout=10)
            try:
                self.assertTrue(self.probe.entered.wait(timeout=5))
            finally:
                self.probe.release.set()
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(self.probe.calls, 2)
        self.assertEqual({result['serial'] for result in results}, {2})
        self.assertEqual({result['summary']['posts'] for result in results}, {1})

    def test_entry_limit_bounds_persisted_results(self):
        cache = self.cache(max_entries=2)
        params = [{'author_id': author, 'sections': 'core'} for author in (1, 2, 3)]
        for query in params:
            cache.get(query)
        self.assertTrue(self.cache(max_entries=2).get(params[-1])['cache']['hit'])
        self.assertFalse(self.cache(max_entries=2).get(params[0])['cache']['hit'])
        self.assertEqual(self.probe.calls, 4)

    def test_result_larger_than_byte_budget_is_returned_without_persistence(self):
        self.probe.payload_size = 4096
        first = self.cache(max_bytes=512).get()
        second = self.cache(max_bytes=512).get()
        self.assertEqual(len(first['payload']), 4096)
        self.assertEqual(len(second['payload']), 4096)
        self.assertFalse(first['cache']['hit'])
        self.assertFalse(second['cache']['hit'])
        self.assertEqual(self.probe.calls, 2)

    def test_byte_limit_bounds_the_combined_size_of_small_results(self):
        self.probe.payload_size = 512
        first = {'author_id': 1, 'sections': 'core'}
        second = {'author_id': 2, 'sections': 'core'}
        cache = self.cache(max_bytes=1000)
        cache.get(first)
        cache.get(second)
        self.assertTrue(self.cache(max_bytes=1000).get(second)['cache']['hit'])
        self.assertFalse(self.cache(max_bytes=1000).get(first)['cache']['hit'])
        self.assertEqual(self.probe.calls, 3)

    def test_database_targets_cannot_share_analysis_results(self):
        other = Path(self.temporary.name) / 'other.sqlite3'
        initialize(other)
        self.add_post(other)
        first = self.cache().get()
        second = AnalysisCache(other, analyze_fn=self.probe).get()
        self.assertEqual(first['summary']['posts'], 0)
        self.assertEqual(second['summary']['posts'], 1)
        self.assertFalse(second['cache']['hit'])
        self.assertEqual(self.probe.calls, 2)


if __name__ == '__main__':
    unittest.main()
