"""Manual preparation and explicit operator delivery across the authenticated API."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from capturethenarrative import APIError
from fastapi.testclient import TestClient

from ctn_data.server import create_app
from tests.auth_helpers import login
from tests.test_agent_actions import Account, Social


class ManualAccount(Account):
    def me(self):
        return {'account': {'account_id': self.account_id, 'username': self.username}}

    def get_post(self, post_id):
        return self.social.get_post(post_id)

    def get_account(self, account_id):
        return self.social.get_account(account_id)

    def delete_post(self, post_id):
        self.social.deleted.append((self.account_id, post_id))
        self.social.posts.pop(post_id)


class ManualSocial(Social):
    def __init__(self):
        super().__init__()
        self.deleted = []

    def register(self, username, password, team_code, display_name=None):
        account = ManualAccount(self, username, len(self.accounts) + 10)
        self.accounts[username] = account
        return account

    def get_account(self, account_id):
        return {'account_id': account_id, 'username': 'fixture_' + str(account_id)}

    def get_post(self, post_id):
        if post_id not in self.posts:
            raise APIError('GET', 'https://social.legitreal.com/api/posts/' + str(post_id), 404, '{}')
        return self.posts[post_id]


class ManualApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.client = TestClient(create_app(Path(temporary.name) / 'test.db'), base_url='http://127.0.0.1:8765')
        self.addCleanup(self.client.close)
        self.controller = self.client.app.state.agent_control
        self.addCleanup(self.controller.stop)
        self.social = ManualSocial()
        self.controller.sdk_factory = self.social.factory
        self.headers = {'X-CTN-Token': login(self.client)}
        budget = patch('ctn_data.agent_budget.call', side_effect=lambda controller, bucket, method, *a, **kw: method(*a, **kw))
        budget.start()
        self.addCleanup(budget.stop)
        model = patch('ctn_data.agent_budget.model_call', side_effect=AssertionError('Manual operations must not call AI'))
        self.model = model.start()
        self.addCleanup(model.stop)
        self.controller.settings({'team_code': 'fictional-test-team'})
        self.controller.save({'name': 'Operator account', 'username': 'manual_fixture', 'allowed_actions': ['skip']})
        self.agent_id = self.controller.snapshot()['agents'][0]['id']
        self.controller.register(self.agent_id)
        self.actor = self.social.accounts['manual_fixture']
        self.social.posts[19] = {'post_id': 19, 'author_id': self.actor.account_id, 'body': 'Own existing post'}

    def prepare(self, action='post', **values):
        payload = {'agent_id': self.agent_id, 'action': action, 'body': 'My own words — a human instruction.',
                   'idempotency_key': str(uuid.uuid4()), **values}
        return self.client.post('/api/agents/manual/prepare', json=payload, headers=self.headers)

    def publish(self, draft_id, **values):
        return self.client.post('/api/agents/publish', json={'draft_id': draft_id, **values}, headers=self.headers)

    def test_human_post_needs_separate_execution_and_is_not_ai_memory(self):
        prepared = self.prepare()
        self.assertEqual(prepared.status_code, 200, prepared.text)
        data = prepared.json()
        self.assertEqual(data['preview']['actor']['account_id'], self.actor.account_id)
        self.assertEqual(data['draft']['origin'], 'manual')
        self.assertEqual(data['draft']['body'], 'My own words — a human instruction.')
        self.assertEqual(self.social.writes, [])
        self.assertEqual(self.controller.snapshot()['agents'][0]['memory_view']['completed_actions'], [])
        published = self.publish(data['draft_id'])
        self.assertEqual(published.status_code, 200, published.text)
        self.assertEqual(len(self.social.writes), 1)
        self.assertEqual(published.json()['draft']['state'], 'published')
        outcome = self.client.get(f'/api/agents/{self.agent_id}/memory').json()['memory_view']['completed_actions'][0]
        self.assertEqual(outcome['origin'], 'manual')
        self.assertEqual(self.controller.store.read()['agents'][self.agent_id]['allowed_actions'], ['skip'])
        self.model.assert_not_called()
        self.assertNotIn('send_token', published.json()['draft'])

    def test_idempotent_prepare_and_publish_do_not_duplicate_real_write(self):
        key = str(uuid.uuid4())
        first = self.prepare(idempotency_key=key).json()
        again = self.prepare(idempotency_key=key)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()['draft_id'], first['draft_id'])
        self.assertEqual(self.publish(first['draft_id']).status_code, 200)
        self.assertEqual(self.publish(first['draft_id']).status_code, 200)
        self.assertEqual(len(self.social.writes), 1)
        conflict = self.prepare(idempotency_key=key, body='Another instruction')
        self.assertEqual(conflict.status_code, 409)

    def test_delete_requires_matching_post_confirmation_and_preserves_audit(self):
        prepared = self.prepare('delete_post', body='', target_post_id=19)
        self.assertEqual(prepared.status_code, 200, prepared.text)
        draft = prepared.json()
        self.assertEqual(draft['preview']['target_post']['author_id'], self.actor.account_id)
        for value in (None, 20, True):
            with self.subTest(value=value):
                response = self.publish(draft['draft_id'], **({} if value is None else {'confirmation_post_id': value}))
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self.social.deleted, [])
        response = self.publish(draft['draft_id'], confirmation_post_id=19)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.social.deleted, [(self.actor.account_id, 19)])
        deleted = response.json()['draft']
        self.assertEqual(deleted['id'], draft['draft_id'])
        self.assertEqual(deleted['state'], 'published')
        self.assertIsNone(deleted['post_id'])
        self.assertNotIn('delete_confirmation_token', deleted)
        events = self.controller.logs(self.agent_id)['items']
        self.assertTrue(any(row['kind'] == 'manual_delete_confirmed' for row in events))
        self.model.assert_not_called()

    def test_cannot_prepare_deletion_of_someone_else_post(self):
        response = self.prepare('delete_post', body='', target_post_id=7)
        self.assertIn(response.status_code, (400, 403, 409), response.text)
        self.assertEqual(self.social.deleted, [])
        self.assertEqual(self.controller.snapshot()['drafts'], [])

    def test_manual_queue_is_not_sent_by_autonomous_scheduler(self):
        prepared = self.prepare().json()
        def configure(data, conn):
            data['settings']['enabled'] = True
            agent = data['agents'][self.agent_id]
            agent.update(enabled=True, mode='auto', next_run_at=self.controller.clock() + 3600)
            data['memory'].setdefault(self.agent_id, {})['observation'] = {'next_refresh_at': self.controller.clock() + 3600}
        self.controller.store.change(configure)
        with patch.object(self.controller, '_publish') as publish, patch.object(self.controller.generations, 'submit') as submit:
            self.controller.tick()
        publish.assert_not_called()
        submit.assert_not_called()
        self.assertEqual(self.controller.store.read()['drafts'][prepared['draft_id']]['state'], 'pending')
        self.assertEqual(self.social.writes, [])

    def test_manual_route_requires_session_csrf_and_rejects_server_markers(self):
        payload = {'agent_id': self.agent_id, 'action': 'post', 'body': 'text', 'idempotency_key': str(uuid.uuid4())}
        self.assertEqual(self.client.post('/api/agents/manual/prepare', json=payload).status_code, 403)
        self.assertEqual(self.client.post('/api/agents/manual/prepare', json={**payload, 'manual_prepared': True}, headers=self.headers).status_code, 400)
        self.assertEqual(self.prepare('delete_post', body='', target_post_id=True).status_code, 400)
        self.client.cookies.clear()
        self.assertEqual(self.client.post('/api/agents/manual/prepare', json=payload, headers=self.headers).status_code, 401)


if __name__ == '__main__':
    unittest.main()
