"""Accounts the platform suspended are skipped at runtime: no polling, model or platform calls (24 Oct, Colin 11746)."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ctn_data import agent_control
from ctn_data.agent_control import AgentController, AgentError

NOTICE = {'at': '2026-10-24T01:16:10Z', 'username': 'colin_ferryman',
          'message': 'Moderation: your account @colin_ferryman (id 11746) has been suspended after 3 of its posts '
                     'were hidden as inauthentic.'}


class SuspendedSkipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.now = [1792450000.0]
        self.controller = AgentController(
            Path(temporary.name) / 'test.db', Path(temporary.name) / 'secrets',
            provider_store=SimpleNamespace(get=lambda _: {'id': 'fake', 'protocol': 'openai_chat', 'model': 'fake'}),
            clock=lambda: self.now[0])
        self.addCleanup(self.controller.stop)
        self.ids = {}
        for name, account in (('Colin', 11746), ('Maya', 5555)):
            self.controller.save({'name': name, 'username': name.lower() + '_test', 'provider_id': 'fake',
                                  'stance': 'Supports Yes.', 'voice': 'Plain.'})
            agent_id = next(a['id'] for a in self.controller.snapshot()['agents'] if a['name'] == name)
            self.ids[name] = agent_id
            self.controller.store.change(lambda data, conn, agent_id=agent_id, account=account: data['agents'][
                agent_id].update(account_id=account, account_state='ready', mode='auto', enabled=True))
        self.controller.store.change(lambda data, conn: data['settings'].update(enabled=True))
        for target in ('ctn_data.agent_schedule.gate', 'ctn_data.agent_observation.refresh',
                       'ctn_data.agent_observation.drain_resolutions'):
            patcher = patch(target, return_value=False if target.endswith('gate') else None)
            setattr(self, target.rsplit('.', 1)[1], patcher.start())
            self.addCleanup(patcher.stop)
        patcher = patch.object(self.controller, '_competition_tick')
        patcher.start()
        self.addCleanup(patcher.stop)

    def tick(self, suspended):
        """One tick with this suspension list; returns the role ids it submitted for generation."""
        options = {'side_effect': suspended} if isinstance(suspended, Exception) else {'return_value': suspended}
        with patch('ctn_data.account_status.suspended', **options), \
                patch.object(self.controller.generations, 'submit') as submit:
            self.controller.tick()
        return [call.args[0] for call in submit.call_args_list]

    def notes(self):
        return [e for e in self.controller.store.read()['events'] if e['kind'] == 'account_suspended']

    def test_a_suspended_role_is_never_scheduled_and_the_next_role_gets_the_tick(self):
        submitted = self.tick({11746: NOTICE})
        self.assertEqual(submitted, [self.ids['Maya']])
        # Notification polling skips the account too and serves the next role.
        self.assertEqual([call.args[1]['id'] for call in self.refresh.call_args_list], [self.ids['Maya']])
        notes = self.notes()
        self.assertEqual([(n['agent_id'], n['details']) for n in notes], [(self.ids['Colin'], {'account_id': 11746})])
        self.assertTrue(notes[0]['message'].startswith(agent_control.SUSPENDED_SKIP + '：Moderation: your account'))
        # The role is skipped at runtime only; enabling it stays the user's decision.
        self.assertTrue(self.controller.store.read()['agents'][self.ids['Colin']]['enabled'])

    def test_the_skip_note_is_written_at_most_once_an_hour_per_role(self):
        self.tick({11746: NOTICE})
        self.now[0] += 600
        self.tick({11746: NOTICE})
        self.assertEqual(len(self.notes()), 1)
        self.now[0] += agent_control.SUSPENDED_NOTE_SECONDS
        self.tick({11746: NOTICE})
        self.assertEqual(len(self.notes()), 2)

    def test_the_flag_on_the_role_row_also_counts(self):
        self.controller.store.change(lambda data, conn: data['agents'][self.ids['Colin']].update(
            account_suspended=NOTICE))
        self.assertEqual(self.tick({}), [self.ids['Maya']])

    def test_an_unreadable_list_never_stops_the_team(self):
        submitted = self.tick(RuntimeError('official_activity unavailable'))
        self.assertEqual(len(submitted), 1)
        self.assertFalse(self.notes())

    def test_roles_that_are_not_suspended_are_unaffected(self):
        self.assertEqual(len(self.tick({})), 1)
        self.assertFalse(self.notes())

    # The platform's role limit no longer counts suspended accounts (user, 24 Oct).
    @staticmethod
    def listing(found):
        """account_status.suspended as it answers: ``found``, or None/{} when the log is unreadable."""
        return lambda source, strict=False: found if found is not None else (None if strict else {})

    def test_suspended_roles_do_not_count_toward_the_role_limit(self):
        self.controller.settings({'max_accounts': 2})
        # The log cannot be read: every role counts, as before, so a third role is refused.
        with patch('ctn_data.account_status.suspended', side_effect=self.listing(None)):
            with self.assertRaisesRegex(AgentError, '已达到角色数量上限'):
                self.controller.save({'name': 'Marta', 'username': 'marta_test'})
        with patch('ctn_data.account_status.suspended', side_effect=self.listing({11746: NOTICE})):
            self.controller.save({'name': 'Marta', 'username': 'marta_test'})
        self.assertEqual(len(self.controller.snapshot()['agents']), 3)
        # The flag on a role row counts as suspended too.
        self.controller.store.change(lambda data, conn: data['agents'][self.ids['Maya']].update(
            account_suspended=NOTICE))
        with patch('ctn_data.account_status.suspended', side_effect=self.listing({11746: NOTICE})):
            self.controller.save({'name': 'Lena', 'username': 'lena_test'})
        self.assertEqual(len(self.controller.snapshot()['agents']), 4)

    def test_lowering_the_limit_counts_the_same_way(self):
        self.controller.settings({'max_accounts': 2})
        with patch('ctn_data.account_status.suspended', side_effect=self.listing(None)):
            with self.assertRaisesRegex(AgentError, '角色上限不能小于现有角色数量'):
                self.controller.settings({'max_accounts': 1})
        with patch('ctn_data.account_status.suspended', side_effect=self.listing({11746: NOTICE})):
            self.controller.settings({'max_accounts': 1})
        self.assertEqual(self.controller.store.read()['settings']['max_accounts'], 1)

    def test_manual_start_and_run_call_no_model_or_platform(self):
        with patch('ctn_data.account_status.suspended', return_value={11746: NOTICE}), \
                patch('ctn_data.agent_budget.model_call') as model, \
                patch.object(self.controller, '_read_sources') as read, \
                patch.object(self.controller.generations, 'submit') as submit:
            with self.assertRaises(AgentError) as started:
                self.controller.start_generation(self.ids['Colin'])
            with self.assertRaises(AgentError) as ran:
                self.controller.run(self.ids['Colin'])
        self.assertEqual((started.exception.status_code, ran.exception.status_code), (403, 403))
        self.assertTrue(str(ran.exception).startswith(agent_control.SUSPENDED_SKIP))
        submit.assert_not_called()
        model.assert_not_called()
        read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
