"""Interrupted analysis retries are authenticated, explicit and scoped to a round."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from ctn_data.server import create_app
from tests.auth_helpers import login


class ResearchRetryEndpointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app = create_app(Path(temporary.name) / 'research.db')
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')
        self.addCleanup(self.client.close)
        self.token = login(self.client)

    def test_explicit_retry_passes_exact_round_without_requesting_a_model_in_route(self):
        expected = {'settings': {}, 'status': {'running': False, 'last_result': {'outcome': 'retry_pending'}}}
        with patch.object(self.app.state.research_automation, 'retry_interrupted', return_value=expected) as retry:
            response = self.client.post('/api/ai/research/automation/retry', json={'run_id': 'interrupted-round'},
                                        headers={'X-CTN-Token': self.token})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), expected)
        retry.assert_called_once_with('interrupted-round')

    def test_retry_requires_csrf_login_and_valid_round_identifier(self):
        with patch.object(self.app.state.research_automation, 'retry_interrupted') as retry:
            response = self.client.post('/api/ai/research/automation/retry', json={'run_id': 'old-round'})
            self.assertEqual(response.status_code, 403)
            for body in ({}, {'run_id': ''}, {'run_id': 'old-round', 'enabled': True}):
                response = self.client.post('/api/ai/research/automation/retry', json=body,
                                            headers={'X-CTN-Token': self.token})
                self.assertEqual(response.status_code, 400, response.text)
            self.client.cookies.clear()
            response = self.client.post('/api/ai/research/automation/retry', json={'run_id': 'old-round'},
                                        headers={'X-CTN-Token': self.token})
            self.assertEqual(response.status_code, 401)
            retry.assert_not_called()


if __name__ == '__main__':
    unittest.main()
