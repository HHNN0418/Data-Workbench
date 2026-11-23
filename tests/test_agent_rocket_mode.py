import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ctn_data.agent_control import AgentController
from ctn_data import agent_schedule


class RocketModeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        provider = {'id': 'fake', 'protocol': 'openai_chat', 'model': 'fake', 'max_output_tokens': 1800}
        self.controller = AgentController(
            Path(self.temp.name) / 'test.db', Path(self.temp.name) / 'keys',
            provider_store=SimpleNamespace(get=lambda _: provider), clock=lambda: 1792450000.0)
        self.controller.settings({'team_code': 'synthetic-team'})
        self.controller.save({'name': 'Role A', 'username': 'role_a', 'provider_id': 'fake',
                              'background': 'Fictional resident', 'stance': 'Vote Yes', 'enabled': True})
        self.controller.save({'name': 'Role B', 'username': 'role_b', 'provider_id': 'fake',
                              'background': 'Fictional resident', 'stance': 'Vote Yes', 'enabled': False})
        self.controller.settings({'enabled': False})

    def _by_user(self, data):
        return {a.get('username'): a for a in data['agents'].values()}

    def test_on_enables_all_and_removes_cap(self):
        self.controller.rocket_mode(True)
        data = self.controller.store.read()
        self.assertTrue(data['settings']['rocket_mode'])
        self.assertTrue(data['settings']['enabled'])
        self.assertTrue(all(a['enabled'] for a in data['agents'].values()))
        self.assertIn('rocket_snapshot', data['settings'])
        # Every real account is now outside any group, so the gate imposes no cap.
        for agent in data['agents'].values():
            self.assertIsNone(agent_schedule.gate(data, agent, 1792450000.0))

    def test_off_restores_the_snapshot(self):
        self.controller.rocket_mode(True)
        self.controller.rocket_mode(False)
        data = self.controller.store.read()
        self.assertFalse(data['settings']['rocket_mode'])
        # Global run flag and each role's enable return to their pre-rocket values.
        self.assertFalse(data['settings']['enabled'])
        by_user = self._by_user(data)
        self.assertTrue(by_user['role_a']['enabled'])
        self.assertFalse(by_user['role_b']['enabled'])
        # No override left behind: the default schedule applies again.
        self.assertNotIn(agent_schedule.SETTINGS_KEY, data['settings'])
        self.assertNotIn('rocket_snapshot', data['settings'])

    def test_on_is_idempotent_and_keeps_first_snapshot(self):
        self.controller.rocket_mode(True)
        first = self.controller.store.read()['settings']['rocket_snapshot']
        self.controller.rocket_mode(True)
        again = self.controller.store.read()['settings']['rocket_snapshot']
        # A second ON must not overwrite the snapshot with the now-all-on state.
        self.assertEqual(first, again)
        self.assertFalse(again['agents'][self._role_b_id()])

    def _role_b_id(self):
        for a in self.controller.store.read()['agents'].values():
            if a.get('username') == 'role_b':
                return a['id']
        raise AssertionError('role_b missing')


if __name__ == '__main__':
    unittest.main()
