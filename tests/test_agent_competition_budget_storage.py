"""Fair-queue bookkeeping must not load unrelated draft/source JSON."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ctn_data import agent_competition
from ctn_data.agent_control import AgentController, AgentStore, PREFIX, RATE_PARTS
from ctn_data.db import connect


class CompetitionBudgetStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'competition.sqlite3'
        self.store = AgentStore(self.path)
        self.now = 1792450000.0
        self.maya, self.leah = '10000000-0000-4000-8000-000000000001', '10000000-0000-4000-8000-000000000002'
        self.rate = {'competition': {'settings': {**agent_competition.DEFAULTS, 'mode': 'resources'},
            'ready_ids': [self.maya, self.leah], 'fair': {'generate': {
                'served': {self.maya: 2, self.leah: 0},
                'pending': {self.maya: {'at': self.now}, self.leah: {'at': self.now}},
                'grants': {'previous': {'at': self.now, 'permit': {'allowed': True}}}}}}}
        self.seed('rate', self.rate)
        self.seed('drafts', '{unrelated draft JSON must not be parsed', raw=True)
        self.seed('memory', '{unrelated memory JSON must not be parsed', raw=True)

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
            return {row['key'][len(PREFIX):]: row['value'] for row in conn.execute(
                'SELECT key,value FROM meta WHERE key LIKE ?', (PREFIX + '%',))}
        finally:
            conn.close()

    @staticmethod
    def logical_rate(raw):
        """The logical rate: its large parts persist in their own rows."""
        value = json.loads(raw['rate'])
        value.update({part: json.loads(raw['rate.' + part]) for part in RATE_PARTS if 'rate.' + part in raw})
        return value

    @staticmethod
    def business(raw):
        return {key: value for key, value in raw.items() if key != 'rate' and not key.startswith('rate.')}

    def test_withdraw_uses_only_rate_and_preserves_fair_grants(self):
        expected = {'rate': deepcopy(self.rate)}
        agent_competition.withdraw(None, expected, self.maya, 'generate')
        before = self.raw()
        controller = SimpleNamespace(store=self.store)
        with patch.object(self.store, '_load', side_effect=AssertionError('full state parsed')), \
                patch.object(self.store, 'change', side_effect=AssertionError('full business transaction')), \
                patch.object(self.store, 'change_budget', wraps=self.store.change_budget) as budget:
            AgentController._competition_withdraw(controller, self.maya, 'generate')
        budget.assert_called_once()
        after = self.raw()
        self.assertEqual(self.logical_rate(after), expected['rate'])
        self.assertEqual(self.business(after), self.business(before))

    def test_withdraw_storage_failure_is_not_automatically_retried(self):
        before = self.raw()
        with patch.object(agent_competition, 'withdraw', side_effect=sqlite3.OperationalError('locked')) as withdraw:
            with self.assertRaises(sqlite3.OperationalError):
                AgentController._competition_withdraw(SimpleNamespace(store=self.store), self.maya, 'generate')
        withdraw.assert_called_once()
        self.assertEqual(self.raw(), before)

    def test_scheduler_queue_preserves_order_without_loading_business_json(self):
        agents = {aid: {'id': aid, 'created_at': str(index), 'enabled': True, 'mode': 'auto',
            'account_state': 'ready', 'account_id': index + 1, 'generation': {},
            'next_run_at': self.now - 1, 'decision_retry_at': 0}
            for index, aid in enumerate((self.maya, self.leah))}
        memory = {aid: {'observation': {'next_refresh_at': self.now + 100}} for aid in agents}
        snapshot = {'settings': {'enabled': True}, 'agents': agents, 'drafts': {}, 'memory': memory,
                    'rate': deepcopy(self.rate), 'leases': {}}
        self.seed('agents', agents)
        expected = {'rate': deepcopy(self.rate)}
        agent_competition.order(None, expected, [], 'publish', self.now)
        order = agent_competition.order(None, expected, list(agents), 'generate', self.now)
        controller = AgentController.__new__(AgentController)
        controller.store = self.store
        controller.clock = lambda: self.now
        controller._stop = threading.Event()
        controller.generations = SimpleNamespace(submit=Mock())
        before = self.raw()
        with patch.object(controller, '_competition_tick'), \
                patch.object(self.store, 'read', side_effect=lambda: deepcopy(snapshot)), \
                patch.object(self.store, '_load', side_effect=AssertionError('full state parsed')), \
                patch.object(self.store, 'change', side_effect=AssertionError('full business transaction')), \
                patch.object(self.store, 'change_budget', wraps=self.store.change_budget) as budget, \
                patch('ctn_data.agent_observation.pending_revision', return_value='observed'):
            controller.tick()
        controller.generations.submit.assert_called_once_with(order[0], automatic=True)
        self.assertEqual(budget.call_count, 1)  # Queue demands; the cursor has its own row.
        after = self.raw()
        self.assertEqual(self.logical_rate(after)['competition'], expected['rate']['competition'])
        self.assertEqual(self.store.read_cursors()['scheduler_cursor'], order[0])
        self.assertEqual(after['drafts'], before['drafts'])
        self.assertEqual(after['memory'], before['memory'])


if __name__ == '__main__':
    unittest.main()
