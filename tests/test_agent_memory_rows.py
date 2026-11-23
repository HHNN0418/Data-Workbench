"""Role memory loads field by field and writes back only what a call touched (D89)."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ctn_data.agent_control import AgentStore, PREFIX
from ctn_data.agent_memory_rows import LazyMemory, LazyRole
from ctn_data.db import connect

BIG = 'x' * 50_000


class LazyMemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'agents.sqlite3'
        self.store = AgentStore(self.path)
        # The archive adds an archive_version field to each role it captures.
        self.store._archive_enabled = False

        def seed(data, conn):
            data['memory']['role-a'] = {'observation': {'n': 1}, 'episodes': {'readings': [BIG]}, 'inbox': [{'id': 1}]}
            data['memory']['role-b'] = {'inbox': [{'id': 2}], 'subjective': {'notes': [BIG]}}
        self.store.change(seed)
        self.expected = {'role-a': {'observation': {'n': 1}, 'episodes': {'readings': [BIG]}, 'inbox': [{'id': 1}]},
                         'role-b': {'inbox': [{'id': 2}], 'subjective': {'notes': [BIG]}}}

    def raw(self):
        conn = connect(self.path)
        try:
            return {row['key'][len(PREFIX):]: row['value'] for row in conn.execute(
                'SELECT key,value FROM meta WHERE key LIKE ?', (PREFIX + 'memory%',))}
        finally:
            conn.close()

    def no_big_loads(self):
        actual = json.loads

        def loads(value, *args, **kwargs):
            self.assertLess(len(value), 10_000, 'decoded a memory field nobody used')
            return actual(value, *args, **kwargs)
        return patch('ctn_data.agent_control.json.loads', side_effect=loads), \
            patch('ctn_data.agent_memory_rows.json.loads', side_effect=loads)

    def test_read_decodes_only_the_fields_used(self):
        first, second = self.no_big_loads()
        with first, second:
            data = self.store.read()
            self.assertIsInstance(data['memory'], LazyMemory)
            self.assertEqual(list(data['memory']), ['role-a', 'role-b'])
            self.assertEqual(list(data['memory']['role-a']), ['observation', 'episodes', 'inbox'])
            self.assertEqual(data['memory']['role-a']['observation'], {'n': 1})
            self.assertEqual(data['memory'].get('role-b', {}).get('inbox', []), [{'id': 2}])
            self.assertIn('episodes', data['memory']['role-a'])
            self.assertIsNone(data['memory'].get('role-z'))
        # Whole-value views still see everything.
        self.assertEqual(data['memory'], self.expected)
        self.assertEqual(json.loads(json.dumps(data['memory'])), self.expected)
        self.assertEqual(copy.deepcopy(data['memory']), self.expected)
        self.assertEqual(dict(data['memory']['role-b']), self.expected['role-b'])
        self.assertEqual({**data['memory']['role-a']}, self.expected['role-a'])

    def test_change_rewrites_only_touched_fields(self):
        before = self.raw()
        first, second = self.no_big_loads()

        def touch(data, conn):
            data['memory']['role-a']['observation']['n'] = 2
            data['memory']['role-b'].setdefault('read_ids', []).append('p:1')
        with first, second:
            self.store.change(touch)
        after = self.raw()
        changed = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
        self.assertEqual(changed, {'memory/role-a/observation', 'memory/role-b', 'memory/role-b/read_ids'})
        self.assertEqual(self.store.read()['memory']['role-b']['read_ids'], ['p:1'])

    def test_replacing_deleting_and_adding_roles(self):
        def edit(data, conn):
            data['memory']['role-a'] = {'summary': 'fresh'}
            data['memory'].pop('role-b')
            data['memory'].setdefault('role-c', {})['inbox'] = []
        self.store.change(edit)
        rows = self.raw()
        self.assertEqual(json.loads(rows['memory']), ['role-a', 'role-c'])
        self.assertEqual(sorted(name for name in rows if name.startswith('memory/')),
                         ['memory/role-a', 'memory/role-a/summary', 'memory/role-c', 'memory/role-c/inbox'])
        self.assertEqual(self.store.read()['memory'], {'role-a': {'summary': 'fresh'}, 'role-c': {'inbox': []}})

    def test_deleting_a_field_and_replacing_all_memory(self):
        self.store.change(lambda data, conn: data['memory']['role-a'].pop('episodes'))
        self.assertNotIn('memory/role-a/episodes', self.raw())
        self.assertEqual(list(self.store.read()['memory']['role-a']), ['observation', 'inbox'])

        def replace(data, conn):
            data['memory'] = {'role-d': {'inbox': [{'id': 9}]}}
        self.store.change(replace)
        self.assertEqual(self.store.read()['memory'], {'role-d': {'inbox': [{'id': 9}]}})
        self.assertEqual(sorted(self.raw()), ['memory', 'memory/role-d', 'memory/role-d/inbox'])

    def test_archive_captures_only_the_roles_a_write_touched(self):
        self.store._archive_enabled = True
        fetched = []
        actual = LazyMemory.fetch

        def fetch(memory, role, fields):
            fetched.append(role)
            return actual(memory, role, fields)
        with patch.object(LazyMemory, 'fetch', fetch), patch('ctn_data.agent_memory_archive._store') as stored:
            self.store.change(lambda data, conn: data['memory']['role-b']['inbox'].append({'id': 4}))
        self.assertEqual(set(fetched), {'role-b'})
        self.assertTrue({call.args[1] for call in stored.call_args_list} <= {'role-b'})
        rows = self.raw()
        self.assertIn('memory/role-b/archive_version', rows)
        self.assertNotIn('memory/role-a/archive_version', rows)

    def test_a_role_kept_after_the_write_still_reads(self):
        kept = self.store.change(lambda data, conn: data['memory']['role-b'])
        self.assertIsInstance(kept, LazyRole)
        self.assertEqual(kept['subjective'], {'notes': [BIG]})

    def test_detached_copy_is_independent(self):
        role = self.store.read()['memory']['role-a']
        role['observation']['n'] = 5  # loaded and changed locally
        twin = role.detached()
        twin['observation']['n'] = 7
        twin['inbox'].append({'id': 3})
        self.assertEqual(role['observation'], {'n': 5})
        self.assertEqual(role['inbox'], [{'id': 1}])
        self.assertEqual(twin['episodes'], {'readings': [BIG]})


if __name__ == '__main__':
    unittest.main()
