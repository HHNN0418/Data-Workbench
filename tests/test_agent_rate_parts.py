"""Large rate history persists in its own rows; quota writes stay small."""
import json
from pathlib import Path
import tempfile
import unittest

from ctn_data.agent_control import AgentStore, PREFIX, RATE_PARTS
from ctn_data.db import connect

COMPETITION = {'settings': {'mode': 'resources'}, 'history': ['x' * 50] * 200}
CALLS = [{'id': 'call-' + str(number), 'detail': 'y' * 40} for number in range(100)]


class AgentRatePartsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'agents.sqlite3'
        self.store = AgentStore(self.path)
        # The pre-split layout: everything inside the single 'rate' row.
        self.seed('rate', {'buckets': {'post': {'tokens': 3}}, 'competition': COMPETITION,
                           'reading_selection_calls': CALLS, 'remote_deleted_posts': {}})

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
            return {row['key']: row['value'] for row in conn.execute('SELECT key,value FROM meta')}
        finally:
            conn.close()

    def test_legacy_rows_read_unchanged_and_split_on_first_write(self):
        for data in (self.store.read(), self.store.read_budget()):
            self.assertEqual(data['rate']['competition'], COMPETITION)
            self.assertEqual(data['rate']['reading_selection_calls'], CALLS)
        self.store.change_budget(lambda data, conn: data['rate']['buckets']['post'].update(tokens=2))
        rows = self.raw()
        main = json.loads(rows[PREFIX + 'rate'])
        self.assertNotIn('competition', main)
        self.assertNotIn('reading_selection_calls', main)
        self.assertIn('remote_deleted_posts', main)  # content research reads it from this row
        self.assertEqual(json.loads(rows[PREFIX + 'rate.competition']), COMPETITION)
        self.assertEqual(json.loads(rows[PREFIX + 'rate.reading_selection_calls']), CALLS)
        for data in (self.store.read(), self.store.read_budget()):
            self.assertEqual(data['rate']['buckets']['post']['tokens'], 2)
            self.assertEqual(data['rate']['competition'], COMPETITION)

    def test_quota_write_leaves_unchanged_parts_alone(self):
        self.store.change(lambda data, conn: None)
        before = self.raw()
        self.store.change_budget(lambda data, conn: data['rate']['buckets']['post'].update(tokens=1))
        after = self.raw()
        for part in RATE_PARTS:
            self.assertEqual(after[PREFIX + 'rate.' + part], before[PREFIX + 'rate.' + part])
        self.assertLess(len(after[PREFIX + 'rate']), 200)
        self.store.change(lambda data, conn: data['rate']['competition']['settings'].update(mode='models'))
        self.assertEqual(self.store.read()['rate']['competition']['settings']['mode'], 'models')

    def test_removed_part_is_deleted(self):
        self.store.change(lambda data, conn: None)
        self.store.change(lambda data, conn: data['rate'].pop('reading_selection_calls'))
        self.assertNotIn(PREFIX + 'rate.reading_selection_calls', self.raw())
        self.assertNotIn('reading_selection_calls', self.store.read()['rate'])


if __name__ == '__main__':
    unittest.main()
