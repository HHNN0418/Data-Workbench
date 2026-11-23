"""Separate local progress work from network setup; all transport is mocked."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ctn_data import ai_providers as ai
from ctn_data.agent_control import AgentStore
from ctn_data.agent_generation import GenerationManager, _Task
from ctn_data.db import begin_write, connect


@contextmanager
def fake_network(clock, *, resolve=None, fail_send=False):
    """Operation costs are independent of progress callbacks and model work."""
    class FakeSocket:
        def settimeout(self, value):
            pass
        def do_handshake(self):
            clock[0] += .003
        def shutdown(self, how):
            pass
        def close(self):
            pass

    class FakeResponse:
        status = 200
        pending = True
        def getheader(self, name):
            return 'application/json' if name == 'Content-Type' else None
        def read1(self, maximum):
            if not self.pending:
                return b''
            self.pending = False
            return b'{"choices":[{"message":{"content":"Ready"}}]}'

    class FakeConnection:
        def __init__(self, *args, **options):
            pass
        def request(self, *args, **options):
            clock[0] += 21 if fail_send else .004
            if fail_send:
                raise TimeoutError
        def getresponse(self):
            return FakeResponse()
        def close(self):
            pass

    def resolved(*args, **options):
        clock[0] += .001
        return '93.184.216.34'

    def connected(*args, **options):
        clock[0] += .002
        return sock

    sock = FakeSocket()
    with patch.object(ai, '_resolve_address', side_effect=resolve or resolved), \
            patch.object(ai.socket, 'create_connection', side_effect=connected), \
            patch.object(ai.ssl, 'create_default_context') as tls, \
            patch.object(ai.http.client, 'HTTPConnection', FakeConnection):
        tls.return_value.wrap_socket.return_value = sock
        yield


def isolated_lock_reproduction():
    """Real SQLite writer contention, with no production DB or network access."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'isolated.sqlite3'
        store = AgentStore(path)
        locked, release = threading.Event(), threading.Event()

        def hold_writer():
            conn = connect(path)
            try:
                begin_write(conn, 'agent_control')
                locked.set()
                release.wait(5)
                conn.rollback()
            finally:
                conn.close()

        holder = threading.Thread(target=hold_writer)
        holder.start()
        if not locked.wait(3):
            release.set()
            holder.join(3)
            raise AssertionError('isolated writer did not start')
        try:
            manager = GenerationManager(SimpleNamespace(owner='isolated-owner', store=store))
            task = _Task('isolated-agent', 'isolated-job', False)
            began = time.perf_counter()
            manager._on_progress(task, {'stage': 'dns'})
            memory_seconds = time.perf_counter() - began
            timer = threading.Timer(1.0, release.set)
            timer.start()
            try:
                began = time.perf_counter()
                # The previous callback synchronously called _touch/store.change.
                store.change(lambda data, conn: None)
                synchronous_seconds = time.perf_counter() - began
            finally:
                timer.cancel()
            return {'isolated_writer_hold_seconds': 1.0,
                    'old_synchronous_store_callback_seconds': synchronous_seconds,
                    'new_memory_callback_seconds': memory_seconds,
                    'production_database_accessed': False, 'model_requests_sent': 0}
        finally:
            release.set()
            holder.join(3)


class TransportTimingTests(unittest.TestCase):
    def test_actual_writer_lock_blocks_old_callback_but_not_memory_progress(self):
        result = isolated_lock_reproduction()
        self.assertGreaterEqual(result['old_synchronous_store_callback_seconds'], .75)
        self.assertLess(result['new_memory_callback_seconds'], .1)

    def test_twenty_second_budget_is_not_consumed_before_real_dns_starts(self):
        clock = [1000.0]
        old_deadline = clock[0] + 20
        clock[0] += 21  # Original synchronous progress/database callback.
        with patch.object(ai.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(TimeoutError):
                ai._remaining(old_deadline)
            clock[0] = 1000.0
            def slow_initial_progress(progress):
                if progress['stage'] == 'dns':
                    clock[0] += 21
            with fake_network(clock):
                result = ai._request_json('https://example.com/v1/chat/completions', b'{}', {},
                    wait_for_completion=True, connection_timeout_seconds=20,
                    on_progress=slow_initial_progress)
        diagnostics = result['_diagnostics']
        self.assertEqual(diagnostics['local_progress_seconds'], 21)
        self.assertEqual(diagnostics['dns_seconds'], .001)
        self.assertAlmostEqual(sum(diagnostics[key] for key in
            ('dns_seconds', 'connect_seconds', 'tls_seconds', 'send_seconds')), .01)

    def test_setup_timings_exclude_each_main_thread_progress_callback(self):
        clock = [1000.0]
        def report(progress):
            clock[0] += .5
        with patch.object(ai.time, 'monotonic', side_effect=lambda: clock[0]), fake_network(clock):
            result = ai._request_json('https://example.com/v1/chat/completions', b'{}', {},
                wait_for_completion=True, connection_timeout_seconds=20, on_progress=report)
        diagnostics = result['_diagnostics']
        self.assertEqual({key: diagnostics[key] for key in
            ('dns_seconds', 'connect_seconds', 'tls_seconds', 'send_seconds')},
            {'dns_seconds': .001, 'connect_seconds': .002, 'tls_seconds': .003, 'send_seconds': .004})
        self.assertEqual(diagnostics['local_progress_seconds'], 3.5)

    def test_watcher_callback_is_not_counted_or_subtracted_from_active_dns(self):
        clock = [1000.0]
        main_thread = threading.get_ident()
        watcher_started, watcher_reported = threading.Event(), threading.Event()
        def monotonic():
            value = clock[0]
            if threading.get_ident() != main_thread:
                watcher_started.set()
            return value
        def resolve(*args, **options):
            self.assertTrue(watcher_started.wait(2))
            clock[0] += 6
            self.assertTrue(watcher_reported.wait(2))
            return '93.184.216.34'
        def report(progress):
            if threading.get_ident() != main_thread:
                clock[0] += 3
                watcher_reported.set()
        with patch.object(ai.time, 'monotonic', side_effect=monotonic), fake_network(clock, resolve=resolve):
            result = ai._request_json('https://example.com/v1/chat/completions', b'{}', {},
                wait_for_completion=True, connection_timeout_seconds=20, on_progress=report)
        self.assertEqual(result['_diagnostics']['local_progress_seconds'], 0)
        self.assertEqual(result['_diagnostics']['dns_seconds'], 9)

    def test_timeout_retains_operation_timings_through_complete(self):
        clock = [1000.0]
        provider = {'base_url': 'https://example.com/v1', 'model': 'test-model',
                    'protocol': 'openai_chat', 'api_key': 'test-key', 'connection_timeout_seconds': 20}
        with patch.object(ai.time, 'monotonic', side_effect=lambda: clock[0]), \
                fake_network(clock, fail_send=True), self.assertRaises(ai.ProviderError) as caught:
            ai.complete(provider, [{'role': 'user', 'content': 'Test'}])
        self.assertEqual(caught.exception.diagnostics['stage'], 'send')
        self.assertEqual(caught.exception.diagnostics['timeout_seconds'], 20)
        self.assertEqual(caught.exception.diagnostics['send_seconds'], 21)
        self.assertEqual(caught.exception.diagnostics['dns_seconds'], .001)
        self.assertEqual(caught.exception.diagnostics['local_progress_seconds'], 0)

    def test_timing_allowlist_rejects_negative_nonfinite_and_secret_values(self):
        self.assertEqual(ai._safe_diagnostics({'dns_seconds': .125, 'connect_seconds': -1,
            'tls_seconds': float('inf'), 'send_seconds': True,
            'local_progress_seconds': 'private-content', 'unexpected': 123}), {'dns_seconds': .125})


if __name__ == '__main__':
    unittest.main()
