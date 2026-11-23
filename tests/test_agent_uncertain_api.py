"""Authenticated manual resolution API, with fake platform and temp storage."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ctn_data.agent_control import AgentError
from ctn_data.agent_routes import register_agent_routes
from tests import test_agent_manual_execution as fixtures


class UncertainApiTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManualExecutionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.controller = self.fixture.controller
        self.draft_id = self.fixture.draft('like')
        with patch.object(self.fixture.account, 'like', side_effect=TimeoutError('not delivered')):
            with self.assertRaises(AgentError):
                self.fixture.publish(self.draft_id)
        self.app = FastAPI()
        self.app.state.auth = SimpleNamespace(user=lambda request: {'email': 'logged-in@example.test'})
        with patch('ctn_data.agent_routes.AgentController', return_value=self.controller):
            register_agent_routes(self.app, self.controller.db_path, self.controller.secret_dir)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.payload = {'draft_id': self.draft_id, 'confirmed_not_executed': True,
                        'expected_version': self.fixture.row(self.draft_id)['version'],
                        'reason': '检查了账号点赞列表，目标未被点赞'}

    def test_endpoint_records_authenticated_member_and_is_idempotent(self):
        for _ in range(2):
            result = self.client.post('/api/agents/discard-unexecuted', json=self.payload)
            self.assertEqual(result.status_code, 200, result.text)
        row = self.fixture.row(self.draft_id)
        self.assertEqual(row['state'], 'discarded')
        self.assertEqual(row['manual_resolution']['confirmed_by'], 'logged-in@example.test')
        self.assertEqual(len(self.controller.logs(self.fixture.agent_id, kind='discard_unexecuted')['items']), 1)

    def test_strict_validation_rejects_spoofing_missing_confirmation_and_stale_version(self):
        for update, status in (({'confirmed_not_executed': 'true'}, 422),
                               ({'confirmed_not_executed': False}, 400),
                               ({'confirmed_by': 'other@example.test'}, 422),
                               ({'reason': ' '}, 422), ({'expected_version': 999}, 409)):
            result = self.client.post('/api/agents/discard-unexecuted', json={**self.payload, **update})
            self.assertEqual(result.status_code, status, result.text)
            self.assertEqual(self.fixture.row(self.draft_id)['state'], 'uncertain')

    def test_anonymous_request_cannot_resolve_uncertain_action(self):
        self.app.state.auth.user = lambda request: None
        result = self.client.post('/api/agents/discard-unexecuted', json=self.payload)
        self.assertEqual(result.status_code, 401)
        self.assertEqual(self.fixture.row(self.draft_id)['state'], 'uncertain')
