from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from ctn_data.server import create_app
from tests.auth_helpers import login


class ReviewAPITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.app = create_app(self.directory / 'test.db', backup_dir=self.directory / 'backups')
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')
        self.addCleanup(self.client.close)
        self.cloud = patch('ctn_data.ai_providers.complete', side_effect=AssertionError('No live AI')).start()
        self.addCleanup(patch.stopall)

    def payload(self, revision=1, **changes):
        return {'enabled': False, 'provider_id': '', 'platform_policy': 'hold',
                'expected_revision': revision, **changes}

    def test_auth_and_csrf_are_required(self):
        self.assertEqual(self.client.get('/api/agents/review').status_code, 401)
        login(self.client)
        self.assertEqual(self.client.post('/api/agents/review', json=self.payload()).status_code, 403)

    def test_save_uses_revision_and_keeps_local_english_policy_fixed(self):
        csrf = login(self.client)
        headers = {'X-CTN-Token': csrf}
        current = self.client.get('/api/agents/review')
        self.assertEqual(current.status_code, 200)
        self.assertTrue(current.json()['settings']['english_only'])
        revision = current.json()['settings']['revision']
        saved = self.client.post('/api/agents/review', json=self.payload(revision), headers=headers)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertGreater(saved.json()['settings']['revision'], revision)
        stale = self.client.post('/api/agents/review', json=self.payload(revision), headers=headers)
        self.assertEqual(stale.status_code, 409)
        invalid = self.client.post('/api/agents/review',
            json=self.payload(saved.json()['settings']['revision'], english_only=False), headers=headers)
        self.assertEqual(invalid.status_code, 400)
        self.cloud.assert_not_called()

    def test_enabling_requires_an_explicit_valid_text_connection(self):
        csrf = login(self.client)
        revision = self.client.get('/api/agents/review').json()['settings']['revision']
        response = self.client.post('/api/agents/review',
            json=self.payload(revision, enabled=True), headers={'X-CTN-Token': csrf})
        self.assertEqual(response.status_code, 400, response.text)
        self.cloud.assert_not_called()

    def test_disabling_does_not_require_previously_selected_connection(self):
        csrf = login(self.client)
        revision = self.client.get('/api/agents/review').json()['settings']['revision']
        response = self.client.post('/api/agents/review',
            json=self.payload(revision, provider_id='removed-review-model'),
            headers={'X-CTN-Token': csrf})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertFalse(result['settings']['enabled'])
        self.assertEqual(result['status'], 'disabled')
        self.assertEqual(result['records'], [])
        self.cloud.assert_not_called()

    def test_review_route_and_health_are_available(self):
        self.assertEqual(self.client.get('/ai/review').status_code, 200)
        self.assertEqual(self.client.get('/api/health').json()['agent_review_policy'], 2)
