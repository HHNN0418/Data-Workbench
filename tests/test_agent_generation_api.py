"""Persistent background generation and complete memory reset over the local API."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from ctn_data import ai_providers
from ctn_data.server import create_app
from tests.auth_helpers import login


class GenerationApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = TestClient(create_app(Path(self.temp.name) / 'local.sqlite3'), base_url='http://127.0.0.1:8765')
        self.addCleanup(self.client.close)
        self.headers = {'X-CTN-Token': login(self.client)}
        self.controller = self.client.app.state.agent_control
        self.addCleanup(self.controller.stop)
        self.controller.providers = SimpleNamespace(get=lambda _: {
            'id': 'fake', 'protocol': 'openai_chat', 'base_url': 'https://example.invalid/v1',
            'model': 'fake', 'max_output_tokens': 1800})
        self.controller.save({'name': 'Fictional reader', 'username': 'reader',
                              'background': 'A fictional competition resident.', 'provider_id': 'fake'})
        self.agent_id = self.controller.snapshot()['agents'][0]['id']
        self.source = {'id': 'post:7', 'post_id': 7, 'kind': 'post', 'version': 'v1', 'title': 'Discussion',
                       'text': 'A fictional local discussion.', 'notification_ids': []}
        self.read = patch.object(self.controller, '_read_sources', return_value=[self.source]).start()
        self.addCleanup(patch.stopall)

    def response(self):
        return {'text': json.dumps({'action': 'post', 'body': 'That is a useful question.',
                                   'memory': 'I am considering the discussion.', 'reason': 'A relevant reply.'}),
                'model': 'fake', 'usage': {}}

    def await_terminal(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.controller.store.read()['agents'][self.agent_id].get('generation', {})
            if state.get('status') in ('completed', 'failed', 'cancelled'):
                return state
            time.sleep(.02)
        self.fail('local fake generation did not finish')

    def test_run_returns_while_model_waits_and_reload_reads_same_job(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def model(*args, **kwargs):
            entered.set()
            release.wait(5)
            return self.response()
        with patch.object(ai_providers, 'complete', side_effect=model) as call:
            result = self.client.post('/api/agents/run', json={'agent_id': self.agent_id}, headers=self.headers)
            self.assertEqual(result.status_code, 202, result.text)
            self.assertTrue(entered.wait(2))
            self.assertEqual(result.json(), {'ok': True})
            job = self.controller.store.read_runtime()['agents'][self.agent_id]['generation']
            snapshot = self.client.get('/api/agents').json()
            self.assertEqual(snapshot['agents'][0]['generation']['id'], job['id'])
            self.assertEqual(snapshot['agents'][0]['generation']['status'], 'running')
            self.assertEqual(snapshot['drafts'], [])
            duplicate = self.client.post('/api/agents/run', json={'agent_id': self.agent_id}, headers=self.headers)
            self.assertEqual(duplicate.status_code, 409)
            release.set()
            self.assertEqual(self.await_terminal()['status'], 'completed')
            self.assertEqual(len(self.controller.snapshot()['drafts']), 1)
            call.assert_called_once()

    def test_cancel_stops_wait_without_draft_and_reset_preserves_persona_and_audit(self):
        entered = threading.Event()
        def model(*args, **kwargs):
            entered.set()
            self.assertTrue(kwargs['cancellation_event'].wait(5))
            raise ai_providers.ProviderError('AI request cancelled', 499)
        original = self.controller.store.read()['agents'][self.agent_id].copy()
        def prepare(data, conn):
            data['memory'][self.agent_id] = {'summary': 'Old thoughts.', 'read_ids': ['post:7'],
                                            'last_sources': [self.source], 'read_versions': {'post:7': 'v1'}}
        self.controller.store.change(prepare)
        with patch.object(ai_providers, 'complete', side_effect=model):
            self.client.post('/api/agents/run', json={'agent_id': self.agent_id}, headers=self.headers)
            self.assertTrue(entered.wait(2))
            blocked = self.client.post('/api/agents/reset-memory', json={'agent_id': self.agent_id}, headers=self.headers)
            self.assertEqual(blocked.status_code, 409)
            cancelled = self.client.post('/api/agents/cancel-generation', json={'agent_id': self.agent_id}, headers=self.headers)
            self.assertEqual(cancelled.status_code, 200)
            self.assertEqual(self.await_terminal()['status'], 'cancelled')
        before = self.controller.store.read()
        result = self.client.post('/api/agents/reset-memory', json={'agent_id': self.agent_id}, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), {'ok': True})
        role = self.controller.snapshot()['agents'][0]
        self.assertEqual(role['memory'], '')
        self.assertEqual(role['read_count'], 0)
        self.assertEqual(role['last_sources'], [])
        for field in ('background', 'provider_id', 'username', 'version', 'account_id'):
            self.assertEqual(role.get(field), original.get(field))
        self.assertEqual(self.controller.store.read()['drafts'], before['drafts'])
        self.assertEqual(len(list((self.controller.secret_dir.parent / 'agent-memory-backups').glob('*.json'))), 1)

    def test_new_endpoints_still_require_member_and_csrf(self):
        for path in ('reset-memory', 'cancel-generation', 'run'):
            with self.subTest(path=path):
                response = self.client.post('/api/agents/' + path, json={'agent_id': self.agent_id})
                self.assertEqual(response.status_code, 403)
        self.client.cookies.clear()
        response = self.client.post('/api/agents/reset-memory', json={'agent_id': self.agent_id}, headers=self.headers)
        self.assertEqual(response.status_code, 401)


if __name__ == '__main__':
    unittest.main()
