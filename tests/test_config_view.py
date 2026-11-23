"""Strategy rules, settings summary and maintenance history: read from code and stored state, never written."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ctn_data import agent_decision, agent_schedule, agent_targeting, config_view
from ctn_data.agent_control import REPLY_DELAY_SECONDS
from ctn_data.db import connect, initialize

# 22:00 Sydney (AEDT): group B's night shift and group C's evening shift are on, A is off.
NOW = datetime(2026, 10, 22, 11, 0, tzinfo=timezone.utc)
AGENTS = {
    'r1': {'id': 'r1', 'name': 'Bev Thornton', 'username': 'bev_thornton_kc', 'account_id': 10783, 'enabled': True,
           'mode': 'auto', 'reading_selection_mode': 'rules', 'provider_id': 'p1', 'allowed_actions': ['post', 'reply'],
           'daily_limit': 6, 'interval_seconds': 1800, 'last_error': None},
    'r2': {'id': 'r2', 'name': 'Simon Mercer', 'username': 'simon_mercer', 'account_id': 6503, 'enabled': True,
           'mode': 'auto', 'reading_selection_mode': 'rules', 'provider_id': 'gone', 'allowed_actions': ['post', 'reply'],
           'last_error': '请先保存并选择一个 AI 连接'},
    'r3': {'id': 'r3', 'name': 'Ruth Hansen', 'account_id': 10436, 'enabled': False, 'allowed_actions': ['post']},
    'r4': {'id': 'r4', 'name': 'Loose Role', 'account_id': 777, 'enabled': True, 'allowed_actions': ['like']},
}
SETTINGS = {'enabled': True, 'team_side': 'Vote Yes', 'max_accounts': 15,
            'ai_review': {'enabled': True, 'provider_id': 'p1', 'english_only': True, '_provider_signature': {'x': 1}},
            'fact_bank': {'review_mode': 'auto', 'extract_enabled': True, 'pipeline': 'staged', 'provider_id': 'p1'}}


class Providers:
    def list_public(self):
        return {'providers': [{'id': 'p1', 'name': 'openroute', 'model': 'deepseek', 'enabled': True}],
                'default_provider_id': 'p1'}


class Nav:
    def get(self):
        return {'agents': {'team_enabled': True, 'pending_actions': 2}, 'ai': {'providers': 1, 'default_ok': True},
                'collection': {'scheduler_enabled': True, 'healthy': True}}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'test.sqlite3'
        initialize(self.db)
        conn = connect(self.db)
        with conn:
            for key, value in (('settings', SETTINGS), ('agents', AGENTS),
                               ('rate', {'buckets': {'x': list(range(50))},
                                         'maintenance': {'id': 'm1', 'active': True, 'phase': 'draining',
                                                         'reason': '服务更新，正在等待已有任务结束',
                                                         'started_at': '2026-10-22T10:36:38Z'}})):
                conn.execute('INSERT INTO meta(key,value) VALUES(?,?)', ('agent_control_v1:' + key, json.dumps(value)))
            for index, action in enumerate(['post', 'reply', 'post', 'post']):
                conn.execute("INSERT INTO post_attribution(post_id,agent_id,published_at,action,fact_ids_json,captured_at) "
                             "VALUES(?,'r1',?,?,'[]',?)", (index + 1, f'2026-10-22T10:0{index}:00Z', action,
                                                           f'2026-10-22T10:0{index}:00Z'))
            for at, kind in (('2026-10-22T09:57:29Z', 'maintenance_started'),
                             ('2026-10-22T09:58:31Z', 'maintenance_completed'),
                             ('2026-10-22T10:36:38Z', 'maintenance_started')):
                conn.execute("INSERT INTO agent_audit_events(id,agent_id,at,kind,message,details_json) "
                             "VALUES(?,NULL,?,?,'服务更新','{}')", (at + kind, at, kind))
        conn.close()


class StrategyTests(Fixture):
    def test_rules_are_the_code_constants(self):
        result = config_view.strategy(self.db, now=NOW)
        mix = {rule['key']: rule['value'] for rule in result['mix']}
        self.assertEqual(mix['originals_per_reply'], agent_decision.ORIGINALS_PER_REPLY)
        self.assertEqual(mix['precise_target_seconds'], agent_decision.PRECISE_TARGET_SECONDS)
        self.assertEqual(tuple(mix['reply_delay_seconds']), REPLY_DELAY_SECONDS)
        targeting = {rule['key']: rule for rule in result['targeting']}
        self.assertEqual(targeting['max_reply_depth']['value'], agent_targeting.MAX_REPLY_DEPTH)
        self.assertEqual(targeting['max_team_replies_per_thread']['value'], agent_targeting.MAX_TEAM_REPLIES_PER_THREAD)
        self.assertEqual(targeting['crowded_competitor_replies']['value'], agent_targeting.CROWDED_COMPETITOR_REPLIES)
        self.assertIn(str(agent_targeting.MAX_REPLY_DEPTH), targeting['max_reply_depth']['text'])
        self.assertTrue(all(rule['text'] and rule['title'] for rule in result['mix'] + result['targeting']))
        self.assertTrue(result['read_only'])

    def test_public_figures_and_their_concerns(self):
        figures = {row['username']: row for row in config_view.strategy(self.db, now=NOW)['public_figures']['figures']}
        self.assertEqual(set(figures), set(agent_targeting.PUBLIC_FIGURES))
        self.assertEqual(figures['DrAmaraSingh']['first_for'], ['health'])
        self.assertIn('hospital', figures['DrAmaraSingh']['concerns'])
        self.assertIn('tourism', figures['MarinaCastilloCEO']['concerns'])

    def test_schedule_groups_with_role_names_and_current_shift(self):
        schedule = config_view.strategy(self.db, now=NOW)['schedule']
        groups = {group['group']: group for group in schedule['groups']}
        self.assertEqual(set(groups), set(agent_schedule.DEFAULT_SCHEDULE['groups']))
        self.assertEqual(groups['A']['members'][0]['name'], 'Bev Thornton')
        self.assertEqual((groups['A']['on_shift'], groups['B']['on_shift'], groups['C']['on_shift']), (False, True, True))
        self.assertEqual((groups['B']['current_limit'], groups['C']['current_limit']), (4, 6))
        self.assertEqual(len(groups['A']['hour_limits']), 24)
        self.assertEqual((groups['A']['hour_limits'][7], groups['A']['hour_limits'][8], groups['A']['hour_limits'][23]),
                         (8, 6, None))
        self.assertFalse(schedule['custom'])
        self.assertEqual(schedule['unscheduled'], ['Loose Role'])

    def test_recent_mix_and_allowed_actions(self):
        result = config_view.strategy(self.db, now=NOW)
        self.assertEqual(result['recent_mix'], {'actions': 4, 'posts': 3, 'replies': 1, 'post_share': 0.75,
                                                'originals_per_reply': 3.0})
        self.assertEqual(result['allowed_actions'], {'roles_enabled': 3, 'counts': {'post': 2, 'reply': 2, 'like': 1}})


class SettingsTests(Fixture):
    def test_roles_with_group_reading_mode_and_provider(self):
        result = config_view.settings(self.db, nav=Nav(), providers=Providers())
        roles = {role['id']: role for role in result['roles']}
        self.assertEqual((roles['r1']['group'], roles['r1']['reading_selection_mode'], roles['r1']['provider_name']),
                         ('A', 'rules', 'openroute'))
        self.assertTrue(roles['r2']['provider_missing'])
        self.assertTrue(roles['r2']['has_error'])
        self.assertFalse(roles['r1']['provider_missing'])
        self.assertEqual(roles['r3']['provider_name'], 'openroute')
        self.assertIsNone(roles['r4']['group'])
        self.assertEqual(result['summary']['enabled'], 3)
        self.assertEqual(result['summary']['groups'], {'A': 1, 'B': 1, 'C': 1, 'none': 1})

    def test_team_settings_are_whitelisted(self):
        result = config_view.settings(self.db, nav=Nav(), providers=Providers())
        self.assertEqual(result['team']['global_interval_seconds'], 25)
        self.assertEqual(result['review']['provider_name'], 'openroute')
        self.assertNotIn('_provider_signature', json.dumps(result))
        self.assertEqual(result['fact_bank']['pipeline'], 'staged')
        self.assertEqual(result['status']['collection'], {'scheduler_enabled': True, 'healthy': True})
        self.assertTrue(result['read_only'])


class MaintenanceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.reports = self.root / 'reports'
        self.reports.mkdir()
        for name, data in (('unified-20261022a', {'started_at': '2026-10-22T13:00:00+11:00', 'purpose': 'reload one',
                                                  'result': 'ready', 'drained_at': '2026-10-22T13:02:00+11:00',
                                                  'finished_at': '2026-10-22T13:03:00+11:00', 'code_sha256': {'a': 'b'}}),
                           ('unified-20261022b', {'started_at': '2026-10-22T19:28:06+11:00', 'purpose': 'reload two',
                                                  'result': 'aborted_existing_backend_kept',
                                                  'failed_stage': 'drain_existing_work',
                                                  'finished_at': '2026-10-22T19:33:21+11:00'})):
            # PowerShell writes these files with a byte-order mark.
            (self.reports / f'{name}-deployment.json').write_text(json.dumps(data), encoding='utf-8-sig')
        (self.reports / 'unified-20261022c-deployment.json').write_text('{not json', encoding='utf-8')
        (self.reports / 'agent-pipeline-deployment.json').write_text('{}', encoding='utf-8')
        self.backups = self.root / 'backups'
        self.backups.mkdir()
        (self.backups / 'ctn-pg-20261022T100000000000Z.sqlite3').write_bytes(b'x' * 10)
        (self.backups / 'notes.txt').write_text('ignore')

    def test_history_state_events_and_backups(self):
        class Retention:
            retention_days, interval_seconds, last_result = 30, 300, {'counts': {'api_call_logs': 0}}
        result = config_view.maintenance(self.db, reports_dir=self.reports, backup_dir=self.backups,
                                         retention=Retention())
        self.assertEqual([entry['name'] for entry in result['history']],
                         ['unified-20261022b', 'unified-20261022a', 'unified-20261022c'])
        latest, ready, broken = result['history']
        self.assertEqual((latest['ok'], latest['failed_stage'], latest['total_seconds']),
                         (False, 'drain_existing_work', 315.0))
        self.assertEqual((ready['ok'], ready['drain_seconds'], ready['total_seconds']), (True, 120.0, 180.0))
        self.assertNotIn('code_sha256', ready)
        self.assertTrue(broken['unreadable'])
        self.assertEqual(result['history_summary']['results'], {'ready': 1, 'aborted_existing_backend_kept': 1})
        self.assertEqual(result['history_summary']['last_ready_at'], '2026-10-22T02:03:00.000000Z')
        self.assertEqual(ready['started_at'], '2026-10-22T02:00:00.000000Z')
        self.assertEqual(result['state'], {'active': True, 'phase': 'draining', 'reason': '服务更新，正在等待已有任务结束',
                                           'started_at': '2026-10-22T10:36:38Z', 'finished_at': None})
        self.assertEqual([event['kind'] for event in result['events']],
                         ['maintenance_started', 'maintenance_completed', 'maintenance_started'])
        self.assertEqual([row['name'] for row in result['backups']], ['ctn-pg-20261022T100000000000Z.sqlite3'])
        self.assertEqual(result['backups'][0]['bytes'], 10)
        self.assertEqual(result['retention']['retention_days'], 30)
        self.assertTrue(result['read_only'])

    def test_missing_report_directory_is_empty_history(self):
        result = config_view.maintenance(self.db, reports_dir=self.root / 'missing')
        self.assertEqual((result['history'], result['backups']), ([], []))


if __name__ == '__main__':
    unittest.main()
