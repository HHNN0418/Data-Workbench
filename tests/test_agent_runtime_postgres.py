"""Runtime storage/heartbeat contracts against an explicitly isolated PG DB.

Normal discovery skips this module. Run the companion verification module to
create, verify and remove a random disposable database; no live CTN writes or
social/model requests are performed.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
import re
import sqlite3
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
import uuid

from ctn_data.agent_control import AgentController, AgentStore, PREFIX
from ctn_data.agent_generation import GenerationManager, _Task
from ctn_data.db import begin_write, connect, utc_now


DATABASE_PREFIX = 'ctn_agent_runtime_test_'
DSN_VARIABLE = 'CTN_TEST_AGENT_RUNTIME_POSTGRES_DSN'


class _ObservedConnection:
    def __init__(self, connection, calls):
        self.connection = connection
        self.calls = calls

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, parameters=None):
        self.calls.append((sql, parameters))
        return self.connection.execute(sql, parameters)

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *arguments):
        return self.connection.__exit__(*arguments)


@unittest.skipUnless(os.getenv(DSN_VARIABLE), 'requires verified disposable runtime PostgreSQL DSN')
class AgentRuntimePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.target = os.environ[DSN_VARIABLE]
        cls.database = unquote(urlsplit(cls.target).path.lstrip('/'))
        # Fail before any DDL/write if a caller accidentally supplies live CTN.
        if not re.fullmatch(DATABASE_PREFIX + r'[0-9a-f]{32}', cls.database):
            raise AssertionError('runtime tests require a random isolated database')
        with closing(connect(cls.target)) as connection:
            if connection.execute('SELECT current_database()').fetchone()[0] != cls.database:
                raise AssertionError('runtime test database identity mismatch')
        cls.store = AgentStore(cls.target)

    def setUp(self):
        self.owner = str(uuid.uuid4())
        self.aid = 'fixture-actor'
        self.job_id = str(uuid.uuid4())
        self.token = str(uuid.uuid4())
        self.now = time.time()
        self.generation = {
            'id': self.job_id, 'owner': self.owner, 'lease_token': self.token,
            'status': 'running', 'stage': 'reading', 'heartbeat_until': self.now + 45,
            'started_at': utc_now(), 'updated_at': utc_now(), 'elapsed_seconds': 0,
        }
        seed = {
            'settings': {'enabled': False},
            'agents': {self.aid: {'id': self.aid, 'name': 'Runtime fixture',
                'account_id': 11, 'persona_revision': 1, 'version': 1,
                'generation': self.generation}},
            'leases': {'agent:' + self.aid: {'token': self.token, 'owner': self.owner,
                'generation_job_id': self.job_id, 'until': self.now + 300}},
            'events': [], 'drafts': {}, 'memory': {}, 'rate': {}, 'operations': {},
        }
        with closing(connect(self.target)) as connection:
            with connection:
                connection.execute('DELETE FROM meta WHERE key LIKE ?', (PREFIX + '%',))
                connection.execute('DELETE FROM agent_audit_events')
                for key, value in seed.items():
                    connection.execute('INSERT INTO meta(key,value) VALUES(?,?)',
                                       (PREFIX + key, json.dumps(value)))
        self.controller = SimpleNamespace(store=self.store, owner=self.owner, clock=time.time)
        self.controller._event = lambda *a, **kw: AgentController._event(self.controller, *a, **kw)
        self.manager = GenerationManager(self.controller, heartbeat_interval=.03, progress_interval=.02)
        self.task = _Task(self.aid, self.job_id, False, lease_token=self.token)

    def _raw(self, *keys):
        with closing(connect(self.target)) as connection:
            return {key: connection.execute('SELECT value FROM meta WHERE key=?',
                        (PREFIX + key,)).fetchone()[0] for key in keys}

    def _wait(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail('isolated runtime operation did not finish')

    def test_large_business_state_is_never_selected_or_reencoded_by_heartbeat(self):
        # Mimic the production 15 MB state while keeping payload out of output.
        drafts = {'historical': {'id': 'historical', 'agent_id': self.aid,
            'state': 'published', 'origin': 'manual', 'body': 'x' * 12_000_000}}
        memory = {self.aid: {'summary': 'x' * 3_000_000}}
        with closing(connect(self.target)) as connection:
            with connection:
                for key, value in (('drafts', drafts), ('memory', memory)):
                    connection.execute('UPDATE meta SET value=? WHERE key=?',
                                       (json.dumps(value), PREFIX + key))
        previous = self._raw('drafts', 'memory', 'rate', 'operations', 'settings')
        calls = []

        def observed(target):
            return _ObservedConnection(connect(target), calls)

        with patch('ctn_data.agent_control.connect', side_effect=observed), \
                patch.object(self.store, '_load', side_effect=AssertionError('full state loaded')):
            self.manager._touch(self.task, {'stage': 'thinking', 'received_bytes': 25})
            state = self.store.read_runtime()
        self.assertEqual(state['agents'][self.aid]['generation']['stage'], 'thinking')
        self.assertEqual(state['agents'][self.aid]['generation']['received_bytes'], 25)
        self.assertFalse(self.task.cancellation.is_set())
        self.assertEqual(self._raw(*previous), previous)
        selects = [(query, params) for query, params in calls
                   if query.lstrip().upper().startswith('SELECT') and 'FROM meta' in query]
        self.assertGreaterEqual(len(selects), 2)
        for query, parameters in selects:
            self.assertIn(' IN ', query)
            self.assertEqual(set(parameters), {PREFIX + k for k in ('agents', 'leases', 'events')})
        with closing(connect(self.target)) as connection:
            audit = connection.execute("SELECT kind,details_json FROM agent_audit_events").fetchall()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]['kind'], 'generation_phase')
        self.assertEqual(json.loads(audit[0]['details_json'])['stage'], 'thinking')
        self.assertGreater(len(previous['drafts']), 12_000_000)

    def _check_interleaving(self, first_is_runtime):
        entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()

        def ordinary(data, connection):
            data['memory'].setdefault(self.aid, {})['business_edit'] = 'retained'
            data['agents'][self.aid]['name'] = 'Updated persona name'
            data['drafts']['saved'] = {'id': 'saved', 'agent_id': self.aid,
                                     'state': 'pending', 'origin': 'manual', 'body': 'Retained draft'}

        def runtime(data, connection):
            data['agents'][self.aid]['generation']['received_bytes'] = 992
            data['leases']['agent:' + self.aid]['until'] = self.now + 400

        first = runtime if first_is_runtime else ordinary
        second = ordinary if first_is_runtime else runtime
        first_write = self.store.change_runtime if first_is_runtime else self.store.change
        second_write = self.store.change if first_is_runtime else self.store.change_runtime

        def held(data, connection):
            first(data, connection)
            entered.set()
            if not release.wait(3):
                raise AssertionError('test writer was not released')

        def following(data, connection):
            second_entered.set()
            return second(data, connection)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(first_write, held)
            self.assertTrue(entered.wait(2))
            second_future = pool.submit(second_write, following)
            try:
                self.assertFalse(second_entered.wait(.08), 'business and runtime locks did not serialize')
            finally:
                release.set()
            first_future.result(timeout=4)
            second_future.result(timeout=4)
        state = self.store.read()
        self.assertEqual(state['memory'][self.aid]['business_edit'], 'retained')
        self.assertEqual(state['drafts']['saved']['body'], 'Retained draft')
        self.assertEqual(state['agents'][self.aid]['name'], 'Updated persona name')
        self.assertEqual(state['agents'][self.aid]['generation']['received_bytes'], 992)
        self.assertEqual(state['leases']['agent:' + self.aid]['until'], self.now + 400)

    def test_business_write_then_runtime_preserves_both_changes(self):
        self._check_interleaving(first_is_runtime=False)

    def test_runtime_then_business_write_preserves_both_changes(self):
        self._check_interleaving(first_is_runtime=True)

    def test_real_lock_timeout_is_retried_without_cancelling_live_generation(self):
        first_timeout = threading.Event()
        original_runtime = self.store.change_runtime
        failures = []

        def short_wait(callback, **options):
            try:
                return original_runtime(callback, lock_timeout_seconds=.05)
            except sqlite3.OperationalError as error:
                failures.append(type(error).__name__)
                first_timeout.set()
                raise

        blocker = connect(self.target)
        worker = None
        try:
            begin_write(blocker, 'agent_control')
            with patch.object(self.store, 'change_runtime', side_effect=short_wait):
                worker = threading.Thread(target=self.manager._heartbeat, args=(self.task,), daemon=True)
                worker.start()
                self.assertTrue(first_timeout.wait(2), 'PostgreSQL did not produce the expected lock timeout')
                self.assertFalse(self.task.cancellation.is_set(), 'one transient lock timeout cancelled the request')
                blocker.commit()
                self._wait(lambda: self.store.read_runtime()['agents'][self.aid]['generation']['heartbeat_until']
                           > self.generation['heartbeat_until'])
                self.assertTrue(worker.is_alive())
                self.assertFalse(self.task.cancellation.is_set())
                self.assertEqual(self.store.read_runtime()['agents'][self.aid]['generation']['status'], 'running')
                self.task.heartbeat_stop.set()
                worker.join(timeout=2)
        finally:
            blocker.rollback()
            blocker.close()
            self.task.heartbeat_stop.set()
            if worker is not None:
                worker.join(timeout=2)
        self.assertGreaterEqual(len(failures), 1)

    def test_replacement_lease_is_never_extended_or_removed_by_old_heartbeat(self):
        replacement = {'owner': 'replacement-worker', 'token': 'replacement-token',
                       'generation_job_id': 'replacement-job', 'until': self.now + 800}
        self.store.change_runtime(lambda data, connection:
            data['leases'].update({'agent:' + self.aid: replacement.copy()}))
        self.manager._touch(self.task, {'stage': 'thinking'})
        state = self.store.read_runtime()
        self.assertEqual(state['leases']['agent:' + self.aid], replacement)
        self.assertTrue(self.task.cancellation.is_set())
        self.assertIn('租约', self.task.failure_reason)

    def test_replacement_job_remains_unchanged_by_old_heartbeat(self):
        replacement = {'id': 'replacement-job', 'owner': 'replacement-worker',
                       'status': 'running', 'stage': 'headers', 'heartbeat_until': self.now + 120}
        self.store.change_runtime(lambda data, connection:
            data['agents'][self.aid].update(generation=replacement.copy()))
        self.manager._touch(self.task, {'stage': 'thinking'})
        state = self.store.read_runtime()
        self.assertEqual(state['agents'][self.aid]['generation'], replacement)
        self.assertTrue(self.task.cancellation.is_set())
        self.assertIn('所有权', self.task.failure_reason)

    def test_completed_result_survives_transient_terminal_save_lock_timeout(self):
        original_runtime = self.store.change_runtime
        timed_out = threading.Event()

        def short_wait(callback, **options):
            try:
                return original_runtime(callback, lock_timeout_seconds=.05)
            except sqlite3.OperationalError:
                timed_out.set()
                raise

        blocker = connect(self.target)
        try:
            begin_write(blocker, 'agent_control')
            with patch.object(self.store, 'change_runtime', side_effect=short_wait), \
                    ThreadPoolExecutor(max_workers=1) as pool:
                finishing = pool.submit(self.manager._finish, self.task, 'completed')
                try:
                    self.assertTrue(timed_out.wait(2), 'terminal save did not encounter the expected lock timeout')
                    self.assertFalse(finishing.done(), 'terminal save abandoned a completed result after one lock timeout')
                finally:
                    blocker.commit()
                finishing.result(timeout=3)
        finally:
            blocker.rollback()
            blocker.close()
        state = self.store.read_runtime()
        self.assertEqual(state['agents'][self.aid]['generation']['status'], 'completed')
        self.assertEqual(state['agents'][self.aid]['generation']['heartbeat_until'], 0)
        self.assertNotIn('agent:' + self.aid, state['leases'])
        with closing(connect(self.target)) as connection:
            events = connection.execute("SELECT kind FROM agent_audit_events WHERE kind='generation_completed'").fetchall()
        self.assertEqual(len(events), 1, 'terminal-save retry duplicated the completion audit')


if __name__ == '__main__':
    unittest.main()
