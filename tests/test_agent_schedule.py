"""Shift schedule (D37): coverage, rest, overlap, gating and publishing."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from ctn_data import agent_lifecycle, agent_schedule
from ctn_data.agent_control import AgentController, AgentError

SYDNEY = ZoneInfo('Australia/Sydney')
A, B = (10783, 6464), (11563, 6494)


def at(hour, minute=0, day=23):
    return datetime(2026, 10, day, hour, minute, tzinfo=SYDNEY).timestamp()


def on(account, when):
    return agent_schedule.current_shift(agent_schedule.DEFAULT_SCHEDULE, account, when) is not None


class DefaultScheduleTest(unittest.TestCase):
    def test_team_is_always_online_and_two_accounts_cover_citizen_peak(self):
        for minute in range(0, 24 * 60, 15):
            when = at(0) + minute * 60
            online = [account for account in (*A, *B) if on(account, when)]
            with self.subTest(minute=minute):
                self.assertTrue(online)
                if 7 * 60 <= minute < 22 * 60:
                    self.assertGreaterEqual(len(online), 2)

    def test_group_c_covers_the_late_morning_peaks_and_the_evening(self):
        for account in (10436, 10440):
            self.assertTrue(on(account, at(9)) and on(account, at(13, 59)) and on(account, at(20)))
            self.assertFalse(on(account, at(7, 59)) or on(account, at(15)) or on(account, at(23)))

    def test_group_d_covers_the_early_morning_and_the_afternoon_peak(self):
        for account in (11252, 11260):
            self.assertTrue(on(account, at(5)) and on(account, at(7)) and on(account, at(15)) and on(account, at(20, 30)))
            self.assertFalse(on(account, at(11)) or on(account, at(21, 30)) or on(account, at(2)))
        # 14:00-16:00 no longer rests on group B alone.
        self.assertTrue(on(B[0], at(15)) and on(11255, at(15)))

    def test_every_account_rests_at_least_six_hours_in_a_row(self):
        for account in (*A, *B, 10436, 11252):
            states = [on(account, at(0) + minute * 60) for minute in range(0, 2 * 24 * 60, 5)]
            longest = run = 0
            for working in states:
                run = 0 if working else run + 5
                longest = max(longest, run)
            with self.subTest(account=account):
                self.assertGreaterEqual(longest, 6 * 60)

    def test_neighbouring_shifts_overlap_for_one_hour(self):
        for hour in (12, 16, 21):
            with self.subTest(hour=hour):
                self.assertTrue(on(A[0], at(hour, 30)) and on(B[0], at(hour, 30)))
                self.assertFalse(on(A[0], at(hour + 1, 30)) and on(B[0], at(hour + 1, 30)))

    def test_switches_and_rates(self):
        schedule = agent_schedule.DEFAULT_SCHEDULE
        self.assertTrue(on(A[0], at(12, 59)) and not on(A[0], at(13, 0)))
        self.assertTrue(on(B[0], at(11, 59)) is False and on(B[0], at(12, 0)))
        self.assertEqual(agent_schedule.current_shift(schedule, B[0], at(23))['max_per_hour'], 4)
        self.assertEqual(agent_schedule.current_shift(schedule, A[0], at(8))['max_per_hour'], 8)
        # Switching from A's first shift to its second: off 13:00-16:00, back at 16:00.
        self.assertFalse(on(A[0], at(15, 59)))
        self.assertTrue(on(A[0], at(16, 0)))
        self.assertEqual(agent_schedule.next_start(schedule, A[0], at(13, 30)), at(16))
        # B's day shift ends at 17:00 and its night shift starts at 21:00.
        self.assertTrue(on(B[0], at(16, 59)) and not on(B[0], at(17, 0)))
        self.assertEqual(agent_schedule.next_start(schedule, B[0], at(17, 30)), at(21))
        self.assertEqual(agent_schedule.next_start(schedule, A[0], at(23)), at(6, day=24))
        self.assertEqual(agent_schedule.next_start(schedule, B[0], at(7)), at(12))
        with self.assertRaises(LookupError):
            agent_schedule.current_shift(schedule, 99999, at(8))


class GateTest(unittest.TestCase):
    def data(self, created=(), schedule=None):
        drafts = {str(i): {'id': str(i), 'agent_id': 'maya', 'action': 'post', 'state': 'published',
                           'created_at': datetime.fromtimestamp(t, SYDNEY).isoformat()} for i, t in enumerate(created)}
        settings = {agent_schedule.SETTINGS_KEY: schedule} if schedule else {}
        return {'settings': settings, 'drafts': drafts}

    def test_off_shift_never_decides_or_publishes(self):
        maya = {'id': 'maya', 'account_id': A[0]}
        for hour in (22, 23, 0, 3, 5):
            with self.subTest(hour=hour):
                reason, retry = agent_schedule.gate(self.data(), maya, at(hour, 30))
                self.assertEqual(reason, 'off_shift')
                self.assertGreater(retry, at(hour, 30))
        self.assertFalse(agent_schedule.may_publish(self.data(), maya, at(23, 0)))
        # A reply generated just before 22:00 may still go out within the grace period.
        self.assertTrue(agent_schedule.may_publish(self.data(), maya, at(22, 10)))

    def test_hourly_limit_and_unscheduled_accounts(self):
        maya = {'id': 'maya', 'account_id': A[0]}
        now = at(17, 30)  # day shift, outside the peaks: the off-peak rate of 6
        six = [now - 3000 + i * 60 for i in range(6)]
        self.assertEqual(agent_schedule.gate(self.data(six), maya, now), ('hourly_limit', six[0] + 3600))
        self.assertIsNone(agent_schedule.gate(self.data(six[:5]), maya, now))
        # 09:00-11:00 is a citizen peak: the full cap of 8, never more.
        peak = at(9, 30)
        seven = [peak - 3000 + i * 60 for i in range(7)]
        self.assertIsNone(agent_schedule.gate(self.data(seven), maya, peak))
        eight = [peak - 3000 + i * 60 for i in range(8)]
        self.assertEqual(agent_schedule.gate(self.data(eight), maya, peak)[0], 'hourly_limit')
        # Drafts that never went out (discarded, failed) do not count.
        drafts = self.data(eight)
        drafts['drafts']['0']['state'] = 'discarded'
        self.assertIsNone(agent_schedule.gate(drafts, maya, peak))
        self.assertIsNone(agent_schedule.gate(self.data(), {'id': 's', 'account_id': 99999}, at(3)))
        self.assertTrue(agent_schedule.may_publish(self.data(), {'id': 's', 'account_id': 99999}, at(3)))

    def test_caps_hold_and_output_leans_to_citizen_peaks(self):
        schedule = agent_schedule.DEFAULT_SCHEDULE
        peaks = {6, 7, 9, 10, 12, 13}
        for account in (*A, *B):
            by_hour = {}
            for minute in range(0, 24 * 60, 10):
                when = at(0) + minute * 60
                shift = agent_schedule.current_shift(schedule, account, when)
                if shift is None:
                    continue
                limit = agent_schedule.hourly_limit(schedule, shift, when)
                night = shift['start'] == '21:00'
                with self.subTest(account=account, minute=minute):
                    self.assertLessEqual(limit, 4 if night else 8)
                if not night:
                    by_hour.setdefault(minute // 60, set()).add(limit)
            with self.subTest(account=account):
                on_peak = [max(v) for h, v in by_hour.items() if h in peaks]
                off_peak = [max(v) for h, v in by_hour.items() if h not in peaks]
                self.assertTrue(on_peak and off_peak)
                self.assertGreater(min(on_peak), max(off_peak))
        # The night shift has no off-peak rate: 4 an hour throughout.
        night = agent_schedule.current_shift(schedule, B[0], at(23))
        self.assertEqual(agent_schedule.hourly_limit(schedule, night, at(23)), 4)
        with self.assertRaises(ValueError):
            agent_schedule.validate({**schedule, 'shifts': [{**schedule['shifts'][0], 'off_peak_per_hour': 9}]})

    def test_three_groups_work(self):
        three = {'timezone': 'Australia/Sydney', 'groups': {'A': [1], 'B': [2], 'C': [3]},
                 'shifts': [{'group': g, 'start': s, 'end': e, 'max_per_hour': 5}
                            for g, s, e in (('A', '00:00', '09:00'), ('B', '08:00', '17:00'), ('C', '16:00', '01:00'))]}
        schedule = agent_schedule.validate(three)
        self.assertIsNotNone(agent_schedule.current_shift(schedule, 3, at(23)))
        self.assertIsNone(agent_schedule.current_shift(schedule, 3, at(12)))
        self.assertIsNotNone(agent_schedule.current_shift(schedule, 2, at(8, 30)))

    def test_settings_override_and_invalid_fallback(self):
        custom = {'timezone': 'Australia/Sydney', 'groups': {'X': [A[0]]},
                  'shifts': [{'group': 'X', 'start': '00:00', 'end': '24:00', 'max_per_hour': 2}]}
        maya = {'id': 'maya', 'account_id': A[0]}
        self.assertIsNone(agent_schedule.gate(self.data(schedule=custom), maya, at(3)))
        broken = {**custom, 'shifts': [{'group': 'nope', 'start': '00:00', 'end': '24:00', 'max_per_hour': 2}]}
        self.assertEqual(agent_schedule.configuration({'settings': {agent_schedule.SETTINGS_KEY: broken}}),
                         agent_schedule.DEFAULT_SCHEDULE)
        with self.assertRaises(ValueError):
            agent_schedule.validate({**custom, 'groups': {'X': [A[0]], 'Y': [A[0]]}})


class ControllerShiftTest(unittest.TestCase):
    """Off-shift roles call no model, their drafts wait, and they register no fair-share demand."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.now = [at(23)]  # Maya (group A) rests; Ethan (group B) is on the night shift
        self.controller = AgentController(
            Path(temporary.name) / 'test.db', Path(temporary.name) / 'secrets',
            provider_store=SimpleNamespace(get=lambda _: {'id': 'fake', 'protocol': 'openai_chat', 'model': 'fake'}),
            clock=lambda: self.now[0])
        self.addCleanup(self.controller.stop)
        self.ids = {}
        for name, account in (('Maya', A[0]), ('Ethan', B[0])):
            self.controller.save({'name': name, 'username': name.lower() + '_shift', 'provider_id': 'fake',
                                  'stance': 'Supports Yes.', 'voice': 'Plain.'})
            agent_id = next(a['id'] for a in self.controller.snapshot()['agents'] if a['name'] == name)
            self.ids[name] = agent_id
            self.controller.store.change(lambda data, conn, agent_id=agent_id, account=account: data['agents'][
                agent_id].update(account_id=account, account_state='ready', mode='auto', enabled=True))
        self.controller.store.change(lambda data, conn: data['settings'].update(enabled=True))

    def test_off_shift_run_calls_no_model_and_waits_for_the_next_shift(self):
        maya = self.ids['Maya']
        with patch('ctn_data.agent_budget.model_call') as model, \
                patch.object(self.controller, '_read_sources') as read:
            self.controller._run(maya, automatic=True)
        model.assert_not_called()
        read.assert_not_called()
        data = self.controller.store.read()
        self.assertEqual(data['agents'][maya]['next_run_at'], at(6, day=24))
        event = [e for e in data['events'] if e.get('agent_id') == maya][-1]
        self.assertEqual((event['kind'], event['details']['reason']), ('skip', 'off_shift'))

    def test_tick_holds_off_shift_drafts_and_demand(self):
        maya, ethan = self.ids['Maya'], self.ids['Ethan']
        def add_draft(data, conn):
            agent = data['agents'][maya]
            data['drafts']['waiting'] = {
                'id': 'waiting', 'agent_id': maya, 'action': 'post', 'state': 'pending', 'body': 'x',
                'created_at': '2026-10-23T10:50:00Z', 'persona_revision': agent_lifecycle.persona_revision(agent)}
        self.controller.store.change(add_draft)
        self.controller.store.change_budget(lambda state, conn: state['rate'].setdefault('competition', {})
                                            .setdefault('settings', {}).update(mode='resources'))
        demand = []
        def order(conn, data, ids, kind, now):
            demand.append((kind, list(ids)))
            return list(ids)
        with patch.object(self.controller, '_competition_tick'), \
                patch('ctn_data.agent_observation.refresh'), patch('ctn_data.agent_observation.drain_resolutions'), \
                patch('ctn_data.agent_competition.order', side_effect=order), \
                patch.object(self.controller, '_publish') as publish, \
                patch.object(self.controller.generations, 'submit') as submit:
            self.controller.tick()
            publish.assert_not_called()
            submit.assert_called_once_with(ethan, automatic=True)
            self.assertNotIn(maya, [aid for _, ids in demand for aid in ids])
            self.assertIn(('generate', [ethan]), demand)
            # At 06:00 Maya's shift starts and the waiting draft goes out.
            self.now[0] = at(6, 1, day=24)
            demand.clear()
            self.controller.tick()
            publish.assert_called_once()
            self.assertEqual(publish.call_args.args[0], 'waiting')
            self.assertIn(('publish', [maya]), demand)

    def test_schedule_is_a_validated_agent_setting(self):
        custom = {'timezone': 'Australia/Sydney', 'groups': {'X': [A[0]]},
                  'shifts': [{'group': 'X', 'start': '00:00', 'end': '24:00', 'max_per_hour': 2}]}
        self.controller.settings({agent_schedule.SETTINGS_KEY: custom})
        stored = self.controller.store.read()['settings'][agent_schedule.SETTINGS_KEY]
        self.assertEqual(stored['shifts'][0]['max_per_hour'], 2)
        self.assertIsNone(agent_schedule.gate(self.controller.store.read(), {'id': 'm', 'account_id': A[0]}, at(23)))
        with self.assertRaises(AgentError):
            self.controller.settings({agent_schedule.SETTINGS_KEY: {**custom, 'shifts': [{'group': 'Y'}]}})
        self.controller.settings({agent_schedule.SETTINGS_KEY: None})
        self.assertNotIn(agent_schedule.SETTINGS_KEY, self.controller.store.read()['settings'])


if __name__ == '__main__':
    unittest.main()
