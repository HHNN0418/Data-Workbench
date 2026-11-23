"""Reading-selection records over the API; the reading-model setting was removed (D67)."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from ctn_data.agent_control import AgentController
from ctn_data.server import create_app
from tests.auth_helpers import login


class ReadingSelectionApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / 'reading-api.sqlite3'
        self.app = create_app(self.db, backup_dir=self.root / 'backups')
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')
        self.addCleanup(self.client.close)
        self.controller = self.app.state.agent_control
        self.addCleanup(self.controller.stop)
        self.providers = self.app.state.ai_providers
        self.complete = patch('ctn_data.ai_providers.complete', side_effect=AssertionError('Settings never call a model')).start()
        self.addCleanup(patch.stopall)

    def authorize(self):
        self.headers = {'X-CTN-Token': login(self.client)}

    def provider(self, name, **changes):
        result = self.providers.save({'name': name, 'model': name.lower() + '-model',
            'base_url': 'https://example.invalid/v1', 'api_key': 'secret-only-for-' + name,
            **changes})
        return next(row for row in result['providers'] if row['name'] == name)

    def get(self):
        response = self.client.get('/api/agents/reading-selection')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def configure(self, provider_id, revision):
        return self.client.post('/api/agents/reading-selection',
            json={'provider_id': provider_id, 'expected_revision': revision}, headers=self.headers)

    def test_get_requires_member_and_the_reading_model_setting_is_gone(self):
        self.assertEqual(self.client.get('/api/agents/reading-selection').status_code, 401)
        self.authorize()
        self.assertEqual(self.client.get('/api/agents/reading-selection').status_code, 200)
        # Model-based reading was removed (D67): the setting can no longer be changed.
        self.assertEqual(self.configure('', 1).status_code, 405)
        self.complete.assert_not_called()

    def test_roles_can_only_be_saved_with_rules_reading(self):
        self.authorize()
        response = self.client.post('/api/agents/save', json={'name': 'Reader', 'reading_selection_mode': 'model'},
                                    headers=self.headers)
        self.assertEqual(response.status_code, 400)
        response = self.client.post('/api/agents/save', json={'name': 'Reader'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        agent = next(iter(self.controller.store.read()['agents'].values()))
        self.assertEqual(agent['reading_selection_mode'], 'rules')

    def test_unconfigured_get_has_safe_diagnostics_and_limits_without_model_calls(self):
        self.authorize()
        result = self.get()
        self.assertEqual(result['settings']['provider_id'], '')
        self.assertEqual(result['settings']['revision'], 1)
        self.assertIsNone(result['provider'])
        self.assertTrue(result['provider_error'])
        self.assertEqual(result['roles'], [])
        self.assertEqual(result['recent_calls'], [])
        self.assertTrue(all(result['limits'][key] > 0 for key in ('max_candidates', 'max_selected', 'cache_seconds')))
        self.complete.assert_not_called()

    def test_snapshot_reports_safe_role_selection_and_recent_calls_without_triggering_new_work(self):
        from ctn_data import reading_selection
        self.authorize()
        reading = self.provider('Reading')
        self.controller.save({'name': 'Maya', 'provider_id': reading['id'], 'enabled': False})
        agent = next(iter(self.controller.store.read()['agents'].values()))
        source = {'id': 'post:7', 'kind': 'post', 'post_id': 7, 'author_id': 99,
                  'text': 'A real local housing question.', 'version': 'v1', 'notification_ids': []}
        selected = {'text': json.dumps({'source_ids': ['post:7'], 'reason': '值得进一步阅读'})}
        with patch('ctn_data.agent_budget.model_call', return_value=selected) as model:
            with self.controller._claim(agent['id']) as token:
                reading_selection.select(self.controller, agent, token, [source])
            self.assertEqual(model.call_count, 1)
            result = self.get()
            self.assertEqual(model.call_count, 1)
        role = result['roles'][0]
        self.assertEqual(role['id'], agent['id'])
        self.assertTrue(role['last_selection_at'])
        self.assertEqual(role['selection']['selected_ids'], ['post:7'])
        self.assertEqual(role['selection']['provider_id'], reading['id'])
        self.assertEqual(result['recent_calls'][0]['agent_id'], agent['id'])
        self.assertEqual(result['recent_calls'][0]['provider_id'], reading['id'])
        self.assertNotIn('api_key', json.dumps(result))
        self.assertNotIn('secret-only-for-', json.dumps(result))
        self.assertNotIn('"_binding"', json.dumps(result))
        self.assertNotIn('"_selection"', json.dumps(result))
        self.complete.assert_not_called()

    def test_independent_page_refresh_and_unknown_api_keep_their_routes(self):
        self.authorize()
        for _ in range(2):
            response = self.client.get('/reading-selection')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn('text/html', response.headers['content-type'])
        self.assertEqual(self.client.get('/api/agents/reading-selection/not-an-api').status_code, 404)
        self.assertEqual(self.client.get('/api/missing-reading-api').status_code, 404)
        self.complete.assert_not_called()


if __name__ == '__main__':
    unittest.main()
