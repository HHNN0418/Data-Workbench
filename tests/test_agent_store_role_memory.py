"""Channel bookkeeping updates one role's memory rows, not the whole agent store."""
import copy
import json
from pathlib import Path
import tempfile
import types
import unittest
import uuid
from unittest.mock import patch

from ctn_data import agent_observation
from ctn_data.agent_control import AgentError, AgentStore, PREFIX
from ctn_data.db import connect

MAYA, NORA = str(uuid.uuid4()), str(uuid.uuid4())


class RoleMemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'agents.sqlite3'
        self.store = AgentStore(self.path)

        def seed(data, conn):
            data['agents'].update({MAYA: {'id': MAYA}, NORA: {'id': NORA}})
            data['memory'][MAYA] = {'observation': {'home_next_at': 1}, 'episodes': {'readings': ['x' * 20_000]}}
            data['memory'][NORA] = {'observation': {'home_next_at': 2}, 'inbox': [{'id': 7}]}
            data['drafts']['big'] = {'id': 'big', 'agent_id': NORA, 'state': 'published', 'body': 'y' * 20_000}
        self.store.change(seed)

    def raw(self):
        conn = connect(self.path)
        try:
            return {row['key']: row['value'] for row in conn.execute('SELECT key,value FROM meta')}
        finally:
            conn.close()

    def test_loads_and_rewrites_only_the_listed_rows(self):
        before = self.raw()
        actual_loads = json.loads

        def small_loads(value, *args, **kwargs):
            self.assertLess(len(value), 10_000, 'loaded business state outside the scope')
            return actual_loads(value, *args, **kwargs)

        def take(data, conn):
            self.assertEqual(data['memory'], {MAYA: {'observation': {'home_next_at': 1}}})
            data['leases']['observation:' + MAYA + ':home'] = {'token': 'claim', 'until': 50}
            data['memory'][MAYA]['observation']['home_next_at'] = 60
            return 'claim'

        with patch('ctn_data.agent_control.json.loads', side_effect=small_loads):
            self.assertEqual(self.store.change_role_memory(MAYA, ('observation',), take), 'claim')
        after = self.raw()
        changed = {key for key in after if after[key] != before.get(key)}
        self.assertEqual(changed, {PREFIX + 'leases', PREFIX + 'memory/' + MAYA + '/observation'})
        state = self.store.read()
        self.assertEqual(state['memory'][MAYA]['observation'], {'home_next_at': 60})
        self.assertEqual(state['memory'][MAYA]['episodes'], {'readings': ['x' * 20_000]})
        self.assertEqual({key: state['memory'][NORA][key] for key in ('observation', 'inbox')},
                         {'observation': {'home_next_at': 2}, 'inbox': [{'id': 7}]})

    def test_a_new_field_row_is_part_of_the_role_from_then_on(self):
        self.store.change_role_memory(NORA, ('observation', 'home_sources'),
                                      lambda data, conn: data['memory'].setdefault(NORA, {}).update(home_sources=[{'id': 'post:1'}]))
        self.assertEqual(self.store.read()['memory'][NORA]['home_sources'], [{'id': 'post:1'}])
        # A later full write keeps it and lists it in the role's own row.
        self.store.change(lambda data, conn: data['memory'][NORA]['observation'].update(home_next_at=3))
        self.assertEqual(json.loads(self.raw()[PREFIX + 'memory/' + NORA])[-1], 'home_sources')
        self.assertEqual(self.store.read()['memory'][NORA]['home_sources'], [{'id': 'post:1'}])

    def test_rejects_unloaded_roles_fields_and_keys_without_writing(self):
        before = self.raw()
        for name, callback in (
                ('other role', lambda data, conn: data['memory'].update({NORA: {'observation': {}}})),
                ('other field', lambda data, conn: data['memory'].setdefault(MAYA, {}).update(inbox=[])),
                ('other key', lambda data, conn: data['drafts'].update(new={'id': 'new'}))):
            with self.subTest(name), self.assertRaises(AgentError):
                self.store.change_role_memory(MAYA, ('observation',), lambda data, conn: (
                    data['leases'].update(x={'until': 1}), callback(data, conn)))
        self.assertEqual(self.raw(), before)
        for fields, keys in ((('observation',), ('leases', 'memory')), ((), ('leases',)), (('a/b',), ('leases',))):
            with self.subTest(fields=fields, keys=keys), self.assertRaises(AgentError):
                self.store.change_role_memory(MAYA, fields, lambda data, conn: None, keys=keys)


class ObservationChannelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'agents.sqlite3'
        self.store = AgentStore(self.path)
        self.store.change(lambda data, conn: data['agents'].update({MAYA: {'id': MAYA}}))
        self.checked = []
        self.controller = types.SimpleNamespace(
            store=self.store, db_path=self.path, clock=lambda: 1000.0,
            _check_lease=lambda data, agent_id, token: self.checked.append((agent_id, token)))
        self.agent = {'id': MAYA, 'notification_interval_seconds': 60}

    def test_claims_and_home_results_do_not_rewrite_the_whole_store(self):
        with patch.object(self.store, 'change', side_effect=AssertionError('full store write')):
            claim = agent_observation._take_channel(self.controller, self.agent, 'token', 'home')
            self.assertTrue(claim)
            # Already claimed: the next call in the same minute takes nothing.
            self.assertIsNone(agent_observation._take_channel(self.controller, self.agent, 'token', 'home'))
            agent_observation._finish_channel(self.controller, self.agent, 'token', 'home', claim,
                                              status='ok', items=[{'id': 'post:9'}])
            agent_observation._finish_channel(self.controller, self.agent, 'token', 'notifications', 'other',
                                              status='error', error='x', retry_after=30)
        state = self.store.read()
        self.assertEqual(state['memory'][MAYA]['home_sources'], [{'id': 'post:9'}])
        self.assertEqual(state['memory'][MAYA]['observation']['home_status'], 'ok')
        self.assertNotIn('observation:' + MAYA + ':home', state['leases'])
        self.assertEqual(self.checked, [(MAYA, 'token')] * 4)

    def test_notification_results_still_merge_the_inbox_with_the_full_store(self):
        claim = agent_observation._take_channel(self.controller, self.agent, 'token', 'notifications')
        with patch.object(self.store, 'change', wraps=self.store.change) as change:
            agent_observation._finish_channel(self.controller, self.agent, 'token', 'notifications', claim, status='ok',
                                              items=[{'id': 5, 'notification_id': 5, 'post_id': 3, 'status': 'unresolved'}])
        change.assert_called_once()
        self.assertEqual([row['id'] for row in self.store.read()['memory'][MAYA]['inbox']], [5])

    def test_lease_checks_read_only_the_runtime_rows(self):
        with patch.object(self.store, 'read', side_effect=AssertionError('full store read')):
            agent_observation._check(self.controller, self.agent, 'token')
        self.assertEqual(self.checked, [(MAYA, 'token')])

    def test_policy_on_a_held_snapshot_copies_only_this_roles_memory(self):
        snapshot = self.store.read()
        snapshot['memory'][MAYA] = {'inbox': [{'id': 4, 'status': 'ack_pending'}]}
        original = copy.deepcopy(snapshot)
        with patch.object(self.store, 'read', side_effect=AssertionError('full store read')):
            data, memory = agent_observation._policy_memory(self.controller, self.agent, None, snapshot)
        self.assertEqual(memory['observation']['unresolved_count'], 1)
        self.assertIs(data['drafts'], snapshot['drafts'])
        self.assertEqual(snapshot, original)


if __name__ == '__main__':
    unittest.main()
