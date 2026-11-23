"""Storage failures and progress contention never replay a model request."""
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from ctn_data.agent_generation import GenerationManager
from ctn_data.agent_storage_retry import is_transient_storage_error, storage_error_code
from tests.test_agent_generation_jobs import FakeController


class HeartbeatStorageTests(unittest.TestCase):
    def setUp(self):
        self.controller = FakeController()
        self.manager = GenerationManager(self.controller, heartbeat_interval=.02, progress_interval=.02)
        self.controller.manager = self.manager
        self.addCleanup(self.manager.stop)

    def job(self):
        return self.controller.store.read()['agents']['maya']['generation']

    def wait_for(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.005)
        self.fail('expected background state was not reached')

    def start(self):
        self.manager.submit('maya')
        self.assertTrue(self.controller.started.wait(1))

    def test_progress_callback_remains_nonblocking_while_heartbeat_write_is_blocked(self):
        self.start()
        blocked, release = threading.Event(), threading.Event()
        original = self.manager._runtime_change

        def gated(callback):
            if threading.current_thread().name == 'ctn-agent-generation-heartbeat':
                blocked.set()
                release.wait(2)
            return original(callback)

        self.manager._runtime_change = gated
        try:
            callback = self.controller.options['on_progress']
            callback({'stage': 'headers'})
            self.assertTrue(blocked.wait(1))
            before = time.monotonic()
            for count in range(500):
                callback({'stage': 'thinking', 'received_bytes': count,
                          'dns_seconds': .02, 'connect_seconds': .1,
                          'local_progress_seconds': .00002,
                          'body': 'PRIVATE-MODEL-CONTENT'})
            self.assertLess(time.monotonic() - before, .1)
            self.assertFalse(self.controller.options['cancellation_event'].is_set())
        finally:
            release.set()
        self.wait_for(lambda: self.job().get('received_bytes') == 499)
        self.assertNotIn('PRIVATE-', repr(self.job()))
        self.assertEqual(self.job()['dns_seconds'], .02)
        self.assertEqual(self.controller.calls, 1)
        self.controller.release.set()
        self.wait_for(lambda: self.job()['status'] == 'completed')
        completed = [event for event in self.controller.store.read()['events']
                     if event['kind'] == 'generation_completed']
        self.assertEqual(completed[-1]['details']['local_progress_seconds'], .00002)

    def test_transient_lock_failures_retry_bookkeeping_and_preserve_the_one_request(self):
        self.start()
        original = self.manager._runtime_change
        failed = []

        def flaky(callback):
            if threading.current_thread().name == 'ctn-agent-generation-heartbeat' and len(failed) < 2:
                failed.append(True)
                raise sqlite3.OperationalError('database is locked')
            return original(callback)

        self.manager._runtime_change = flaky
        self.controller.options['on_progress']({'stage': 'thinking', 'received_bytes': 123})
        self.wait_for(lambda: self.job().get('received_bytes') == 123)
        self.assertEqual(len(failed), 2)
        self.assertFalse(self.controller.options['cancellation_event'].is_set())
        self.assertEqual(self.controller.calls, 1)
        self.controller.release.set()
        self.wait_for(lambda: self.job()['status'] == 'completed')

    def test_persistent_lock_failure_exhausts_renewal_budget_without_replaying(self):
        self.start()
        original = self.manager._runtime_change
        self.manager.heartbeat_ttl = .08

        def locked(callback):
            if threading.current_thread().name == 'ctn-agent-generation-heartbeat':
                raise sqlite3.OperationalError('database is locked')
            return original(callback)

        self.manager._runtime_change = locked
        self.wait_for(lambda: self.job()['status'] == 'failed')
        self.assertEqual(self.controller.calls, 1)
        self.assertNotIn('new', self.controller.store.read()['drafts'])

    def test_nontransient_failure_is_not_retried_or_exposed(self):
        self.start()
        original = self.manager._runtime_change
        attempts = []

        def broken(callback):
            if threading.current_thread().name == 'ctn-agent-generation-heartbeat':
                attempts.append(True)
                raise RuntimeError('PRIVATE-DB-SECRET')
            return original(callback)

        self.manager._runtime_change = broken
        self.wait_for(lambda: self.job()['status'] == 'failed')
        self.assertEqual(len(attempts), 1)
        self.assertNotIn('PRIVATE-', self.job()['error'])

    def test_completion_retries_only_its_idempotent_terminal_save(self):
        self.start()
        original = self.manager._runtime_change
        failures = []

        def finishing(callback):
            if callback.__name__ == 'finish' and len(failures) < 2:
                failures.append(True)
                raise sqlite3.OperationalError('database is locked')
            return original(callback)

        self.manager._runtime_change = finishing
        self.controller.release.set()
        self.wait_for(lambda: self.job()['status'] == 'completed')
        self.assertEqual(len(failures), 2)
        self.assertEqual(self.controller.calls, 1)
        self.assertIn('new', self.controller.store.read()['drafts'])

    def test_expiry_while_acquiring_storage_lock_cannot_renew_an_old_heartbeat(self):
        self.manager.heartbeat_interval = 10
        self.manager.progress_interval = 3
        self.start()
        task = self.manager._jobs['maya']
        before = self.controller.store.read()
        original = self.manager._runtime_change

        def delayed(callback):
            task.last_heartbeat_saved -= self.manager.heartbeat_ttl + 1
            return original(callback)

        self.manager._runtime_change = delayed
        with self.assertRaisesRegex(Exception, '超出有效期'):
            self.manager._touch(task)
        self.assertEqual(self.controller.store.read()['leases'], before['leases'])
        self.assertEqual(self.job(), before['agents']['maya']['generation'])
        self.assertEqual(self.controller.calls, 1)

    def test_live_exact_owner_gets_one_recovery_window_after_clock_jump(self):
        self.manager.heartbeat_interval = 10
        self.manager.progress_interval = 3
        self.start()
        self.controller.now += 46
        self.assertEqual(self.controller.store.change(lambda data, conn: self.manager.recover(data)), 0)
        self.assertGreater(self.job()['heartbeat_until'], self.controller.now)
        self.assertFalse(self.controller.options['cancellation_event'].is_set())
        self.controller.now += 46
        self.assertEqual(self.controller.store.change(lambda data, conn: self.manager.recover(data)), 1)
        self.assertEqual(self.job()['status'], 'failed')
        self.assertTrue(self.controller.options['cancellation_event'].is_set())

    def test_stop_cancels_all_local_transports_before_a_failed_persistent_save(self):
        self.manager.heartbeat_interval = self.manager.progress_interval = 10
        self.controller.store.change(lambda data, conn:
            data['agents'].update({'leah': {'id': 'leah', 'account_id': 6464}}))
        self.start()
        self.manager.submit('leah')
        self.wait_for(lambda: self.controller.calls == 2)
        tasks = list(self.manager._jobs.values())
        original = self.controller.store.change
        stopping_thread = threading.current_thread()
        observed = []

        def unavailable(callback):
            if threading.current_thread() is stopping_thread:
                observed.append(all(task.cancellation.is_set() for task in tasks))
                raise sqlite3.OperationalError('database is locked PRIVATE-SHUTDOWN-SQL')
            return original(callback)

        with patch.object(self.controller.store, 'change', side_effect=unavailable), \
                self.assertLogs('ctn_data.agent_generation', level='WARNING') as messages:
            self.manager.stop()
        self.assertEqual(observed, [True, True])
        self.assertTrue(all(task.cancellation.is_set() for task in tasks))
        self.assertTrue(all(not task.thread.is_alive() for task in tasks))
        self.assertEqual(self.controller.calls, 2)
        state = self.controller.store.read()
        self.assertEqual(state['leases'], {})
        self.assertEqual(state['drafts'], {})
        self.assertTrue(all(agent['generation']['status'] == 'cancelled'
                            for agent in state['agents'].values()))
        self.assertNotIn('PRIVATE-', '\n'.join(messages.output))

    def test_stop_cancels_transport_when_all_storage_writes_are_unavailable(self):
        self.manager.heartbeat_interval = self.manager.progress_interval = 10
        self.start()
        task = self.manager._jobs['maya']
        with patch.object(self.controller.store, 'change',
                side_effect=sqlite3.OperationalError('database is locked PRIVATE-SHUTDOWN-SQL')), \
                self.assertLogs('ctn_data.agent_generation', level='WARNING') as messages:
            self.manager.stop()
            self.assertTrue(task.cancellation.is_set())
            self.assertTrue(self.controller.options['cancellation_event'].is_set())
            # Failed writes cannot invent a durable cancelled state. The
            # unfinished task remains recoverable if the process exits now.
            self.assertEqual(self.job()['status'], 'running')
        self.wait_for(lambda: self.job()['status'] == 'cancelled')
        self.assertEqual(self.controller.calls, 1)
        self.assertEqual(self.controller.store.read()['drafts'], {})
        self.assertEqual(self.controller.store.read()['leases'], {})
        self.assertNotIn('PRIVATE-', '\n'.join(messages.output))


class StorageErrorClassificationTests(unittest.TestCase):
    def test_wrapped_sqlstate_and_sqlite_contention_are_retryable(self):
        cause = RuntimeError('private SQL and DSN')
        cause.sqlstate = '55P03'
        outer = sqlite3.OperationalError('private SQL and DSN')
        outer.__cause__ = cause
        self.assertTrue(is_transient_storage_error(outer))
        self.assertEqual(storage_error_code(outer), '55P03')
        self.assertTrue(is_transient_storage_error(sqlite3.OperationalError('database is locked')))

    def test_schema_and_programming_errors_are_not_retried(self):
        self.assertFalse(is_transient_storage_error(sqlite3.OperationalError('no such table: meta')))
        self.assertFalse(is_transient_storage_error(RuntimeError('database is locked')))
        self.assertEqual(storage_error_code(RuntimeError('PRIVATE')), '')
