"""Authenticated individual-role management and durable audit API contracts."""
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from fastapi.testclient import TestClient

from ctn_data.agent_control import AgentController
from ctn_data.server import create_app
from tests.auth_helpers import login


AT = '2026-10-20T01:00:00Z'


class AgentManagementAPITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.app = create_app(self.directory / 'management.sqlite3', backup_dir=self.directory / 'backups')
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')
        self.addCleanup(self.client.close)
        self.controller = self.app.state.agent_control
        self.controller.save({'name': 'Maya', 'username': 'maya_test'})
        self.controller.save({'name': 'Ethan', 'username': 'ethan_test'})
        agents = self.controller.store.read()['agents'].values()
        self.maya = next(row['id'] for row in agents if row['name'] == 'Maya')
        self.ethan = next(row['id'] for row in agents if row['name'] == 'Ethan')
        self.complete = patch('ctn_data.ai_providers.complete', side_effect=AssertionError('No real model calls')).start()
        self.addCleanup(patch.stopall)

    def authorize(self):
        self.token = login(self.client)

    def url(self, suffix='', agent_id=None):
        return '/api/agents/' + (agent_id or self.maya) + suffix

    def add_events(self, count=250):
        def add(data, conn):
            for number in range(count):
                self.controller._event(data, 'reading', f'Maya read {number}', self.maya,
                                       details={'job_id': f'job-{number}', 'source_count': 1})
            for number in range(5):
                self.controller._event(data, 'reading', f'ETHAN_PRIVATE_LOG_{number}', self.ethan)
            self.controller._event(data, 'settings', 'SYSTEM_ONLY_EVENT')
        self.controller.store.change(add)

    def test_detail_logs_and_export_require_authenticated_workbench(self):
        for suffix in ('', '/logs', '/logs/export?format=csv'):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.client.get(self.url(suffix)).status_code, 401)

    def test_management_page_can_be_opened_directly_and_refreshed(self):
        self.authorize()
        for _ in range(2):
            response = self.client.get('/agents/' + self.maya)
            self.assertEqual(response.status_code, 200)
            self.assertIn('text/html', response.headers['content-type'])
        self.assertEqual(self.client.get('/agents/invalid').status_code, 404)
        self.assertEqual(self.client.get('/api/missing-agent-route').status_code, 404)

    def test_detail_contains_only_selected_role_memory_inbox_and_drafts(self):
        self.authorize()
        maya_draft, ethan_draft = str(uuid.uuid4()), str(uuid.uuid4())

        def seed(data, conn):
            data['memory'][self.maya] = {'summary': 'MAYA_MEMORY', 'summary_source': 'operator', 'inbox': [{'id': 'maya-notice'}]}
            data['memory'][self.ethan] = {'summary': 'ETHAN_PRIVATE_MEMORY', 'summary_source': 'operator', 'inbox': [{'id': 'ethan-notice'}]}
            for agent_id, draft_id in ((self.maya, maya_draft), (self.ethan, ethan_draft)):
                data['drafts'][draft_id] = {'id': draft_id, 'agent_id': agent_id, 'created_at': AT,
                    'state': 'pending', 'action': 'post', 'body': 'Draft for ' + agent_id}
        self.controller.store.change(seed)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['agent']['id'], self.maya)
        self.assertEqual(result['agent']['memory'], 'MAYA_MEMORY')
        self.assertEqual([row['id'] for row in result['inbox']], ['maya-notice'])
        self.assertEqual(result['inbox'][0]['local_progress'], 'awaiting_decision')
        self.assertEqual([row['id'] for row in result['drafts']], [maya_draft])
        self.assertNotIn(self.ethan, response.text)
        self.assertNotIn('ETHAN_PRIVATE_MEMORY', response.text)
        self.assertNotIn('agents', result)
        self.complete.assert_not_called()

    def test_logs_beyond_recent_200_are_retained_and_do_not_mix_roles(self):
        self.authorize()
        self.add_events()
        self.assertEqual(len(self.controller.store.read()['events']), 200)
        response = self.client.get(self.url('/logs'), params={'kind': 'reading', 'limit': 200})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['total'], 250)
        self.assertEqual(len(result['items']), 200)
        self.assertTrue(result['has_more'])
        self.assertEqual(result['retention'], 'since_audit_enabled')
        self.assertNotIn('ETHAN_PRIVATE_LOG', response.text)
        self.assertNotIn('SYSTEM_ONLY_EVENT', response.text)
        second = self.client.get(self.url('/logs'), params={'kind': 'reading', 'limit': 200,
                                                          'cursor': result['next_cursor']})
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(len(second.json()['items']), 50)
        ids = [row['id'] for row in result['items'] + second.json()['items']]
        self.assertEqual(len(set(ids)), 250)
        self.assertFalse(second.json()['has_more'])

    def test_search_and_cursor_are_bound_to_exact_role_and_filter(self):
        self.authorize()
        self.add_events(20)
        first = self.client.get(self.url('/logs'), params={'kind': 'reading', 'limit': 2}).json()
        for agent_id, params in ((self.ethan, {'kind': 'reading'}), (self.maya, {'kind': 'draft'}),
                                 (self.maya, {'kind': 'reading', 'q': 'Maya read 1'})):
            with self.subTest(agent=agent_id, params=params):
                response = self.client.get(self.url('/logs', agent_id),
                    params={**params, 'cursor': first['next_cursor']})
                self.assertEqual(response.status_code, 400, response.text)
        match = self.client.get(self.url('/logs'), params={'kind': 'reading', 'q': 'Maya read 19'})
        self.assertEqual(match.status_code, 200, match.text)
        self.assertEqual(match.json()['total'], 1)

    def test_csv_download_is_complete_filtered_and_neutralizes_formulas(self):
        self.authorize()
        self.add_events(260)
        self.controller.store.change(lambda data, conn:
            self.controller._event(data, 'reading', '=UNSAFE_FORMULA()', self.maya))
        response = self.client.get(self.url('/logs/export'), params={'format': 'csv', 'kind': 'reading'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('text/csv', response.headers['content-type'])
        self.assertIn('attachment;', response.headers['content-disposition'])
        self.assertIn('no-store', [value.strip() for value in response.headers.get('cache-control', '').split(',')])
        self.assertTrue(response.text.startswith('\ufeff'))
        rows = list(csv.DictReader(io.StringIO(response.text.lstrip('\ufeff'))))
        self.assertEqual(len(rows), 261)
        self.assertTrue(all(row['agent_id'] == self.maya for row in rows))
        self.assertIn("'=UNSAFE_FORMULA()", [row['message'] for row in rows])
        self.assertNotIn('ETHAN_PRIVATE_LOG', response.text)
        selected = self.client.get(self.url('/logs/export'), params={'q': 'Maya read 259'})
        self.assertEqual(selected.status_code, 200, selected.text)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(selected.text.lstrip('\ufeff'))))), 1)

    def test_malformed_or_unknown_role_returns_400_or_404_before_csv_headers(self):
        self.authorize()
        for suffix in ('', '/logs', '/logs/export'):
            for agent_id, expected in (('invalid', 400), (str(uuid.uuid4()), 404)):
                with self.subTest(suffix=suffix, agent_id=agent_id):
                    response = self.client.get(self.url(suffix, agent_id))
                    self.assertEqual(response.status_code, expected, response.text)
                    self.assertNotIn('text/csv', response.headers.get('content-type', ''))

    def test_invalid_filter_and_cursor_fail_as_400_instead_of_streaming_errors(self):
        self.authorize()
        for suffix, params in (('/logs', {'limit': 0}), ('/logs', {'limit': 201}),
                               ('/logs', {'limit': 'bad'}), ('/logs', {'cursor': 'invalid'}),
                               ('/logs', {'q': 'x' * 201}), ('/logs/export', {'q': 'x' * 201}),
                               ('/logs/export', {'format': 'json'})):
            with self.subTest(suffix=suffix, params=params):
                response = self.client.get(self.url(suffix), params=params)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertNotIn('text/csv', response.headers.get('content-type', ''))

    def test_memory_reset_clears_experiences_and_preserves_operational_audit(self):
        from ctn_data import agent_memory
        self.authorize()
        self.add_events(215)

        def remember(data, conn):
            data['memory'][self.maya] = {'summary': 'Thoughts before reset'}
            agent_memory.record_read(data, self.maya, [{'id': 'post:99', 'post_id': 99,
                'author_id': 8, 'text': 'A previously read discussion', 'version': 'v1',
                'title': 'Discussion', 'kind': 'post'}], AT)
        self.controller.store.change(remember)
        before = self.client.get(self.url('/logs'), params={'kind': 'reading'}).json()['total']
        response = self.client.post('/api/agents/reset-memory', json={'agent_id': self.maya},
                                    headers={'X-CTN-Token': self.token})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get(self.url('/logs'), params={'kind': 'reading'}).json()['total'], before)
        reset_logs = self.client.get(self.url('/logs'), params={'kind': 'memory_reset'}).json()
        self.assertEqual(reset_logs['total'], 1)
        memory = self.controller.store.read()['memory'][self.maya]
        self.assertEqual(memory['summary'], '')
        self.assertEqual(memory['read_ids'], [])
        self.assertEqual(memory['episodes']['readings'], [])
        self.assertTrue(memory['reset_at'])

    def test_history_survives_controller_reinitialization_without_duplicates(self):
        self.authorize()
        self.add_events(230)
        prior = self.controller.logs(self.maya, kind='reading')
        fresh = AgentController(self.controller.db_path, self.directory / 'secrets')
        current = fresh.logs(self.maya, kind='reading')
        self.assertEqual(current['total'], 230)
        self.assertEqual(current, prior)

    def test_capability_upgrade_preserves_custom_permissions_and_run_settings(self):
        previous = ['post', 'reply', 'repost', 'like', 'skip']
        pending_id = str(uuid.uuid4())
        def legacy(data, conn):
            data['rate'].pop('action_capabilities_version', None)
            data['agents'][self.maya].update(allowed_actions=previous, enabled=False, mode='review', version=7)
            data['agents'][self.ethan].update(allowed_actions=['reply', 'skip'], enabled=False, mode='review', version=9)
            data['drafts'][pending_id] = {'id': pending_id, 'agent_id': self.maya, 'account_id': None,
                'persona_version': 7, 'created_at': AT, 'state': 'pending', 'action': 'post',
                'body': 'A simple local discussion.', 'sources': [], 'notification_ids': [],
                'target_post_id': None, 'version': 1, 'error': ''}
        self.controller.store.change(legacy)
        fresh = AgentController(self.controller.db_path, self.directory / 'secrets')
        agents = fresh.store.read()['agents']
        self.assertEqual(set(agents[self.maya]['allowed_actions']),
                         {'post', 'reply', 'repost', 'like', 'unlike', 'follow', 'unfollow', 'skip'})
        self.assertEqual(agents[self.maya]['version'], 7)
        self.assertEqual(agents[self.ethan]['allowed_actions'], ['reply', 'skip'])
        self.assertEqual(agents[self.ethan]['version'], 9)
        self.assertFalse(agents[self.maya]['enabled'])
        self.assertEqual(agents[self.maya]['mode'], 'review')
        self.assertEqual(fresh.logs(self.maya, kind='capabilities')['total'], 1)
        self.assertEqual(fresh.logs(self.ethan, kind='capabilities')['total'], 0)
        retained = fresh.store.read()['drafts'][pending_id]
        self.assertEqual(retained['persona_version'], agents[self.maya]['version'])
        self.assertEqual(retained['state'], 'pending')
        self.assertEqual(retained['body'], 'A simple local discussion.')
        # A real executor path against the existing stateful fake platform
        # proves the old reviewed draft remains sendable after the migration.
        from tests.test_agent_integration import FakeSocial
        social = FakeSocial()
        fresh.sdk_factory = social.factory
        fresh.settings({'team_code': 'synthetic-team-key'})
        with patch('ctn_data.agent_budget.reserve', return_value=None):
            fresh.register(self.maya)
            state = fresh.publish(pending_id)
        self.assertEqual(next(row for row in state['drafts'] if row['id'] == pending_id)['state'], 'published')
        self.assertEqual(len(social.writes), 1)

    def test_save_api_accepts_new_relation_actions_without_enabling_autonomy(self):
        self.authorize()
        payload = {'id': self.maya, 'name': 'Maya',
                   'allowed_actions': ['follow', 'unfollow', 'unlike', 'skip']}
        response = self.client.post('/api/agents/save', json=payload,
                                    headers={'X-CTN-Token': self.token})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {'ok': True})
        agent = next(row for row in self.client.get('/api/agents').json()['agents'] if row['id'] == self.maya)
        self.assertEqual(agent['allowed_actions'], payload['allowed_actions'])
        self.assertFalse(agent['enabled'])
        self.assertEqual(agent['mode'], 'review')


if __name__ == '__main__':
    unittest.main()
