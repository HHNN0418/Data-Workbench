"""Drafts and memory persist per entry; a change rewrites only that entry."""
import json
from pathlib import Path
import tempfile
import unittest

from ctn_data.agent_control import AgentStore, PREFIX
from ctn_data.db import connect

DRAFTS = {'d-3': {'id': 'd-3', 'state': 'pending', 'body': 'c'},
          'd-1': {'id': 'd-1', 'state': 'pending', 'body': 'a' * 5000},
          'd-2': {'id': 'd-2', 'state': 'published', 'body': 'b'}}
MEMORY = {'role-b': {'inbox': [{'id': 1}], 'summary': 'x' * 3000, 'read_ids': ['p:1', 'p:2']},
          'role-a': {'inbox': [], 'observation': {'next_refresh_at': 5}}}


class AgentStatePartitionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'agents.sqlite3'
        self.store = AgentStore(self.path)
        # The pre-split layout: one blob per key.
        for key, value in (('drafts', DRAFTS), ('memory', MEMORY)):
            self.seed(key, value)

    def seed(self, key, value):
        conn = connect(self.path)
        try:
            with conn:
                conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                             (PREFIX + key, json.dumps(value)))
        finally:
            conn.close()

    def raw(self):
        conn = connect(self.path)
        try:
            return {row['key'][len(PREFIX):]: row['value'] for row in conn.execute(
                'SELECT key,value FROM meta WHERE key LIKE ?', (PREFIX + '%',))}
        finally:
            conn.close()

    def check(self, data, drafts, memory):
        self.assertEqual(list(data['drafts']), list(drafts))  # insertion order kept
        self.assertEqual(data['drafts'], drafts)
        self.assertEqual(list(data['memory']), list(memory))
        self.assertEqual(data['memory'], memory)

    def test_legacy_blobs_load_and_split_on_first_write(self):
        self.check(self.store.read(), DRAFTS, MEMORY)
        self.store.change(lambda data, conn: None)
        self.store.change(lambda data, conn: None)
        rows = self.raw()
        self.assertEqual(json.loads(rows['drafts']), ['d-3', 'd-1', 'd-2'])
        self.assertEqual(json.loads(rows['memory']), ['role-b', 'role-a'])
        self.assertEqual(json.loads(rows['memory/role-b'])[:3], ['inbox', 'summary', 'read_ids'])
        self.assertEqual(json.loads(rows['memory/role-b/summary']), 'x' * 3000)
        self.assertEqual(json.loads(rows['drafts/d-1'])['body'], 'a' * 5000)
        data = self.store.read()
        # The memory archive marks each role on its first write; nothing else changes.
        for memory in data['memory'].values():
            memory.pop('archive_version', None)
        self.check(data, DRAFTS, MEMORY)

    def test_change_rewrites_only_the_changed_entries(self):
        self.store.change(lambda data, conn: None)
        before = self.raw()
        def edit(data, conn):
            data['drafts']['d-3']['state'] = 'published'
            data['memory']['role-a']['observation']['next_refresh_at'] = 9
        self.store.change(edit)
        after = self.raw()
        changed = {name for name in after if after[name] != before.get(name)}
        self.assertTrue(changed <= {'drafts/d-3', 'memory/role-a/observation', 'events', 'rate', 'settings', 'agents'}, changed)
        self.assertIn('drafts/d-3', changed)
        self.assertIn('memory/role-a/observation', changed)
        self.assertEqual(self.store.read()['memory']['role-a']['observation']['next_refresh_at'], 9)

    def test_added_and_removed_entries_keep_order_and_delete_rows(self):
        self.store.change(lambda data, conn: None)
        def edit(data, conn):
            data['drafts'].pop('d-1')
            data['drafts']['d-4'] = {'id': 'd-4', 'state': 'pending'}
            data['memory'].pop('role-b')
            data['memory']['role-c'] = {}
        self.store.change(edit)
        rows = self.raw()
        self.assertNotIn('drafts/d-1', rows)
        self.assertFalse([name for name in rows if name.startswith('memory/role-b')])
        data = self.store.read()
        self.assertEqual(list(data['drafts']), ['d-3', 'd-2', 'd-4'])
        self.assertEqual(list(data['memory']), ['role-a', 'role-c'])

    def test_selected_load_reads_partitioned_rows(self):
        self.store.change(lambda data, conn: None)
        full = self.store.read()
        conn = connect(self.path)
        try:
            data = self.store._load_selected(conn, ('drafts', 'memory'))
        finally:
            conn.close()
        self.check(data, full['drafts'], full['memory'])


if __name__ == '__main__':
    unittest.main()
