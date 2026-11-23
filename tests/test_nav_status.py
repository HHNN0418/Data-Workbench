from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest

from fastapi.testclient import TestClient

from ctn_data import nav_status
from ctn_data.agent_control import PREFIX
from ctn_data.db import connect, initialize
from ctn_data.server import create_app
from tests.auth_helpers import login


class RuleTests(unittest.TestCase):
    def test_unfinished_draft_matches_agents_page(self):
        review, auto = {'mode': 'review'}, {'mode': 'auto'}
        skip = {'action': 'skip', 'state': 'skipped', 'notification_ids': ['n1']}
        cases = [({'state': 'pending'}, auto, True), ({'state': 'rejected'}, auto, True),
                 ({'state': 'uncertain'}, auto, True), ({'state': 'sending'}, auto, True),
                 ({'state': 'published'}, auto, False), ({'state': 'discarded'}, auto, False),
                 (skip, review, True), (skip, auto, False), ({**skip, 'requires_review': True}, auto, True),
                 ({**skip, 'notifications_resolved': True}, review, False),
                 ({**skip, 'notification_confirmation_authorized_at': 'x'}, review, False),
                 ({**skip, 'policy_blocked': True}, review, False), ({**skip, 'notification_ids': []}, review, False)]
        for draft, agent, expected in cases:
            with self.subTest(draft=draft, agent=agent):
                self.assertEqual(nav_status.unfinished_draft(draft, agent), expected)

    def test_provider_ready_matches_frontend(self):
        self.assertTrue(nav_status.provider_ready({'enabled': True, 'has_key': True, 'base_url': 'https://x'}))
        self.assertTrue(nav_status.provider_ready({'enabled': True, 'has_key': False, 'base_url': 'http://127.0.0.1:1/v1'}))
        self.assertFalse(nav_status.provider_ready({'enabled': True, 'has_key': False, 'base_url': 'https://x'}))
        self.assertFalse(nav_status.provider_ready({'enabled': False, 'has_key': True}))
        self.assertFalse(nav_status.provider_ready(None))


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'test.sqlite3'
        initialize(self.db)
        web = Path(self.temp.name) / 'dist'
        web.mkdir()
        (web / 'index.html').write_text('<html></html>')
        self.app = create_app(self.db, backup_dir=Path(self.temp.name) / 'backups', web_root=web)
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')
        login(self.client)

    def tearDown(self):
        self.temp.cleanup()

    def seed(self):
        rows = {'settings': {'enabled': True, 'max_accounts': 10, 'block_agent_interactions': True},
                'agents': {'a1': {'id': 'a1', 'name': 'A', 'enabled': True, 'mode': 'review', 'account_id': 5,
                                  'generation': {'status': 'running'}, 'last_error': 'boom'},
                           'a2': {'id': 'a2', 'name': 'B', 'enabled': False, 'mode': 'auto'}},
                'drafts': ['d1', 'd2', 'd3'],
                'drafts/d1': {'id': 'd1', 'agent_id': 'a1', 'state': 'pending', 'created_at': 'x'},
                'drafts/d2': {'id': 'd2', 'agent_id': 'a1', 'state': 'uncertain', 'created_at': 'x'},
                'drafts/d3': {'id': 'd3', 'agent_id': 'a2', 'state': 'published', 'created_at': 'x'},
                'memory': ['a1'], 'memory/a1': ['inbox'],
                'memory/a1/inbox': [{'id': 'n1', 'status': 'unresolved'}, {'id': 'n2', 'status': 'ack_pending'},
                                    {'id': 'n3', 'status': 'resolved'}, {'id': 'n4', 'status': 'unresolved', 'policy_ignored': True}]}
        with closing(connect(self.db)) as conn, conn:
            for key, value in rows.items():
                conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                             (PREFIX + key, json.dumps(value)))
            conn.execute("INSERT INTO agent_audit_events(id,agent_id,at,kind,message,details_json) VALUES('e1','a1','2026-10-22T01:00:00Z','error','x','{}')")

    def test_status_counts_and_sections(self):
        self.seed()
        response = self.client.get('/api/nav/status')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        agents = data['agents']
        self.assertEqual((agents['team_enabled'], agents['roles_total'], agents['roles_enabled'], agents['roles_running']),
                         (True, 2, 1, 1))
        self.assertEqual((agents['pending_actions'], agents['uncertain_actions'], agents['inbox_unresolved']), (2, 1, 2))
        self.assertEqual((agents['linked_accounts'], agents['roles_with_error'], agents['last_error_at']),
                         (1, 1, '2026-10-22T01:00:00Z'))
        for name in ('review', 'facts', 'research', 'collection', 'score_sync', 'ai'):
            self.assertNotIn('error', data[name], name)
        self.assertEqual(data['facts']['pending'], 0)
        self.assertTrue(data['review']['skip_all'])
        self.assertFalse(data['review']['outbound_paused'])

    def test_failing_section_is_isolated(self):
        broken = SimpleNamespace(snapshot=lambda: (_ for _ in ()).throw(RuntimeError('down')))
        result = nav_status.build(self.db, controller=self.app.state.agent_control, providers=self.app.state.ai_providers,
                                  research=broken, scheduler=SimpleNamespace(status=lambda: {'enabled': True, 'lane_errors': {'feeds': 'x'}}))
        self.assertEqual(result['research'], {'error': 'RuntimeError'})
        self.assertEqual(result['collection']['lane_errors'], ['feeds: x'])
        self.assertFalse(result['collection']['healthy'])
        self.assertIn('pending_actions', result['agents'])


if __name__ == '__main__':
    unittest.main()
