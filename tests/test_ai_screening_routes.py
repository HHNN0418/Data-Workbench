import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from ctn_data.server import create_app
from tests.auth_helpers import login, completed_job


class ScreeningRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.temp.name) / 'screen.sqlite3')
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')
        self.token = login(self.client)

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def post(self, path, payload):
        return completed_job(self.client, self.client.post('/api/ai/' + path, json=payload, headers={'X-CTN-Token': self.token}))

    def test_local_scan_does_not_invoke_cloud(self):
        with patch('ctn_data.account_screening.build_screening', return_value={'items': [], 'summary': {}}) as scan, patch('ctn_data.ai_providers.complete') as cloud:
            result = self.post('account-screening', {'params': {'own': 'unknown', 'window_minutes': 60}, 'limit': 50, 'min_score': 40})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(scan.call_args.args[1], {'own': 'unknown'})
        self.assertEqual(scan.call_args.kwargs, {'limit': 50, 'min_score': 40})
        cloud.assert_not_called()

    def test_review_retains_peer_scope_and_selects_requested_account(self):
        screening = {'items': [{'account_id': 3, 'score': 60}, {'account_id': 7, 'score': 30}], 'summary': {}, 'methodology': {}}
        with patch('ctn_data.account_screening.build_screening', return_value=screening) as scan, patch.object(self.app.state.ai_providers, 'get', return_value={'protocol': 'openai_chat'}), patch('ctn_data.account_judgment.judge_account', return_value={'answers': {}, 'model': 'test'}) as judge:
            result = self.post('account-review', {'account_id': 7, 'provider_id': 'provider', 'params': {'own': 'all'}})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertNotIn('author_id', scan.call_args.args[1])
        self.assertEqual(judge.call_args.args[1]['account']['account_id'], 7)
        self.assertEqual(judge.call_count, 1)

    def test_missing_or_own_account_does_not_invoke_model(self):
        with patch('ctn_data.account_screening.build_screening', return_value={'items': []}), patch('ctn_data.account_judgment.judge_account') as judge:
            result = self.post('account-review', {'account_id': 7, 'provider_id': 'provider', 'params': {'own': 'own'}})
        self.assertEqual(result.status_code, 400)
        judge.assert_not_called()

    def test_scan_requires_session_csrf_and_strict_bounds(self):
        for value in (True, 201, '100'):
            self.assertEqual(self.post('account-screening', {'limit': value}).status_code, 400)
        self.assertEqual(self.client.post('/api/ai/account-screening', json={}).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.post('account-screening', {}).status_code, 401)
