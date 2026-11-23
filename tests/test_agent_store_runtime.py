"""Heartbeat storage stays small and shares ownership locks with business writes."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from ctn_data.agent_control import AgentError, AgentStore, KEYS, PREFIX, RUNTIME_KEYS
from ctn_data.db import connect


class AgentStoreRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'agents.sqlite3'
        self.store = AgentStore(self.path)
        self.seed('agents', {'maya': {'id': 'maya', 'generation': {'id': 'job', 'status': 'running'}}})
        self.seed('leases', {'agent:maya': {'token': 'token', 'until': 10}})

    def seed(self, key, value, *, raw=False):
        conn = connect(self.path)
        try:
            with conn:
                conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                             (PREFIX + key, value if raw else json.dumps(value)))
        finally:
            conn.close()

    def raw(self):
        conn = connect(self.path)
        try:
            return {row['key']: row['value'] for row in conn.execute('SELECT key,value FROM meta')}
        finally:
            conn.close()

    def event(self, number):
        return {'id': 'event-' + str(number), 'agent_id': 'maya',
                'at': '2026-10-21T00:00:00Z', 'kind': 'generation_phase',
                'message': 'stage changed', 'details': {'stage': 'thinking'}}

    def test_runtime_skips_fifteen_megabytes_of_business_state_and_upgrades(self):
        self.seed('drafts', {'large': {'body': 'x' * 15_000_000}})
        self.seed('memory', {'maya': {'read_history': [1, 2, 3]}})
        before = self.raw()
        actual_loads = json.loads
        loaded_bytes = []

        def small_loads(value, *args, **kwargs):
            loaded_bytes.append(len(value))
            self.assertLess(len(value), 10_000, 'runtime parsed business source material')
            return actual_loads(value, *args, **kwargs)

        with patch('ctn_data.agent_control.json.loads', side_effect=small_loads), \
                patch('ctn_data.agent_intents.migrate', side_effect=AssertionError('migration in heartbeat')), \
                patch('ctn_data.agent_review.release_disabled_holds', side_effect=AssertionError('review in heartbeat')):
            self.store.change_runtime(lambda data, conn: data['agents']['maya']['generation'].update(heartbeat_until=99))
            runtime = self.store.read_runtime()
        self.assertEqual(runtime['agents']['maya']['generation']['heartbeat_until'], 99)
        self.assertNotIn('language', runtime['agents']['maya'])
        self.assertEqual(set(runtime), set(KEYS))
        self.assertTrue(loaded_bytes)
        after = self.raw()
        for key in KEYS:
            if key not in RUNTIME_KEYS:
                self.assertEqual(after.get(PREFIX + key), before.get(PREFIX + key))

    def test_read_runtime_does_not_modify_database_or_require_valid_business_json(self):
        self.seed('drafts', '{broken business state', raw=True)
        before = self.raw()
        result = self.store.read_runtime()
        self.assertEqual(result['drafts'], {})
        self.assertEqual(result['agents']['maya']['generation']['id'], 'job')
        self.assertEqual(self.raw(), before)

    def test_runtime_event_audit_is_append_only_and_working_events_are_bounded(self):
        events = [self.event(number) for number in range(205)]

        def update(data, conn):
            data['events'].extend(events)
            data['leases']['agent:maya']['until'] = 100
            return 'saved'

        self.assertEqual(self.store.change_runtime(update), 'saved')
        self.assertEqual(len(self.store.read_runtime()['events']), 200)
        self.assertEqual(self.store.read_runtime()['events'][0]['id'], 'event-5')
        self.store.change_runtime(lambda data, conn: data['events'].append(events[0]))
        conn = connect(self.path)
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM agent_audit_events').fetchone()[0], 205)
        finally:
            conn.close()

    def test_audit_failure_rolls_back_runtime_state(self):
        before = self.raw()

        def change(data, conn):
            data['leases']['agent:maya']['until'] = 100
            data['events'].append(self.event(1))

        with patch('ctn_data.agent_audit.record', side_effect=sqlite3.OperationalError('busy')):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.change_runtime(change)
        self.assertEqual(self.raw(), before)

    def test_runtime_rejects_wrong_types_and_malformed_selected_json(self):
        for key, value in [('agents', '[]'), ('leases', 'null'), ('events', '{}'), ('agents', '{bad')]:
            with self.subTest(key=key, value=value):
                self.seed(key, value, raw=True)
                with self.assertRaises(AgentError):
                    self.store.read_runtime()
                with self.assertRaises(AgentError):
                    self.store.change_runtime(lambda data, conn: None)
                self.seed(key, [] if key == 'events' else {})

    def test_runtime_rejects_accidental_business_state_changes_atomically(self):
        before = self.raw()

        def bad_update(data, conn):
            data['leases']['agent:maya']['until'] = 100
            data['memory']['maya'] = {'lost': 'would be discarded'}

        with self.assertRaises(AgentError):
            self.store.change_runtime(bad_update)
        self.assertEqual(self.raw(), before)

    def test_short_lock_timeout_leaves_business_transaction_intact(self):
        entered, release = threading.Event(), threading.Event()
        failures = []

        def business_change(data, conn):
            data['agents']['maya']['name'] = 'Maya Collins'
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test business writer was not released')

        def writer():
            try:
                self.store.change(business_change)
            except Exception as exc:
                failures.append(exc)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(sqlite3.OperationalError):
                self.store.change_runtime(lambda data, conn: data['leases']['agent:maya'].update(until=999),
                                          lock_timeout_seconds=.03)
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(failures, failures)
        runtime = self.store.read_runtime()
        self.assertEqual(runtime['agents']['maya']['name'], 'Maya Collins')
        self.assertEqual(runtime['leases']['agent:maya']['until'], 10)

    def test_runtime_and_business_writers_preserve_each_others_updates(self):
        entered, release, runtime_attempted, runtime_completed = (threading.Event() for _ in range(4))
        failures = []

        def business_change(data, conn):
            data['agents']['maya']['name'] = 'Maya Collins'
            data['memory']['maya'] = {'thought': 'keep this'}
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test writer was not released')

        def run_business():
            try:
                self.store.change(business_change)
            except Exception as exc:
                failures.append(exc)

        def run_runtime():
            runtime_attempted.set()
            try:
                self.store.change_runtime(lambda data, conn: (
                    data['agents']['maya']['generation'].update(heartbeat_until=100),
                    data['leases']['agent:maya'].update(until=200)))
                runtime_completed.set()
            except Exception as exc:
                failures.append(exc)

        business_thread = threading.Thread(target=run_business)
        runtime_thread = threading.Thread(target=run_runtime)
        business_thread.start()
        try:
            self.assertTrue(entered.wait(2))
            runtime_thread.start()
            self.assertTrue(runtime_attempted.wait(1))
            self.assertFalse(runtime_completed.wait(.04), 'runtime bypassed the business writer lock')
        finally:
            release.set()
            business_thread.join(3)
            if runtime_thread.ident is not None:
                runtime_thread.join(3)
        self.assertFalse(failures, failures)
        self.assertFalse(business_thread.is_alive())
        self.assertFalse(runtime_thread.is_alive())
        state = self.store.read()
        self.assertEqual(state['agents']['maya']['name'], 'Maya Collins')
        self.assertEqual(state['agents']['maya']['generation']['heartbeat_until'], 100)
        self.assertEqual(state['leases']['agent:maya']['until'], 200)
        self.assertEqual(state['memory']['maya']['thought'], 'keep this')

    def test_runtime_connection_does_not_leave_short_timeout_on_business_connection(self):
        self.store.change_runtime(lambda data, conn: None, lock_timeout_seconds=.01)
        conn = connect(self.path)
        try:
            self.assertEqual(conn.execute('PRAGMA busy_timeout').fetchone()[0], 15000)
        finally:
            conn.close()

    def test_invalid_timeout_does_not_open_a_transaction(self):
        for timeout in (False, 0, -1, float('nan'), float('inf'), '2'):
            with self.subTest(timeout=timeout):
                with self.assertRaises(AgentError):
                    self.store.change_runtime(lambda data, conn: None, lock_timeout_seconds=timeout)

    def test_budget_does_not_load_runtime_or_fifteen_megabytes_of_business_state(self):
        self.seed('drafts', {'large': {'body': 'x' * 15_000_000}})
        self.seed('rate', {'remaining': 3})
        before = self.raw()
        actual_loads = json.loads
        parsed = []

        def only_budget_loads(value, *args, **kwargs):
            parsed.append(value)
            self.assertEqual(value, before[PREFIX + 'rate'])
            return actual_loads(value, *args, **kwargs)

        def reserve(data, conn):
            data['rate']['remaining'] -= 1
            return data['rate']['remaining']

        with patch('ctn_data.agent_control.json.loads', side_effect=only_budget_loads), \
                patch('ctn_data.agent_intents.migrate', side_effect=AssertionError('migration in budget')):
            self.assertEqual(self.store.change_budget(reserve), 2)
        self.assertEqual(len(parsed), 1)
        after = self.raw()
        for key in KEYS:
            if key != 'rate':
                self.assertEqual(after.get(PREFIX + key), before.get(PREFIX + key))
        self.assertEqual(json.loads(after[PREFIX + 'rate'])['remaining'], 2)

    def test_budget_and_runtime_concurrent_writers_do_not_lose_updates(self):
        self.seed('rate', {'attempts': 0})
        start = threading.Barrier(2)
        failures = []

        def budget_writer():
            try:
                start.wait(2)
                for _ in range(20):
                    self.store.change_budget(lambda data, conn: data['rate'].update(
                        attempts=data['rate']['attempts'] + 1))
            except Exception as exc:
                failures.append(exc)

        def runtime_writer():
            try:
                start.wait(2)
                for number in range(20):
                    self.store.change_runtime(lambda data, conn: data['agents']['maya']['generation'].update(
                        heartbeat_until=number))
            except Exception as exc:
                failures.append(exc)

        threads = [threading.Thread(target=budget_writer), threading.Thread(target=runtime_writer)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertFalse(failures, failures)
        self.assertEqual(json.loads(self.raw()[PREFIX + 'rate'])['attempts'], 20)
        self.assertEqual(self.store.read_runtime()['agents']['maya']['generation']['heartbeat_until'], 19)

    def test_budget_rejects_runtime_and_event_changes_and_rolls_back_quota(self):
        self.seed('rate', {'remaining': 3})
        for key in ('agents', 'events'):
            with self.subTest(key=key):
                before = self.raw()

                def invalid(data, conn):
                    data['rate']['remaining'] -= 1
                    if key == 'events':
                        data['events'].append(self.event(1))
                    else:
                        data['agents']['maya'] = {'id': 'maya'}

                with self.assertRaises(AgentError):
                    self.store.change_budget(invalid)
                self.assertEqual(self.raw(), before)


if __name__ == '__main__':
    unittest.main()
