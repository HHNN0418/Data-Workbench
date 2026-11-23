"""Exercise actor workflows against a stateful fake platform, never real endpoints."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ctn_data.agent_control import AgentController, AgentError


class SDKError(Exception):
    def __init__(self, status, retry_after=0):
        self.status, self.retry_after = status, retry_after


class FakeAccount:
    def __init__(self, social, username, account_id):
        self.social, self.username, self.account_id = social, username, account_id
        self.token, self.expires_at = 'synthetic-session', ''
        self.profile = {'account_id': account_id, 'username': username}

    def post(self, body):
        return self.social.write(self, body)

    def reply(self, post_id, body, **kwargs):
        return self.social.write(self, body, post_id, kwargs.get('root_post_id'))

    def reply_to(self, post, body):
        return self.reply(post['post_id'], body, root_post_id=post.get('root_post_id') or post['post_id'])


class FakeSocial:
    def __init__(self):
        self.accounts, self.posts, self.writes = {}, {}, []
        self.callback = None
        self.timeout_write = False
        self.expired = False
        self.login_count = 0
        self.register_status = None
        self.write_status = None

    def factory(self, **kwargs):
        self.callback = kwargs['on_rate_limit']
        assert kwargs['max_retries'] == 0
        return self

    def rate(self):
        self.callback(SimpleNamespace(limit=5, remaining=4, reset=60))

    def register(self, username, password, team_key, display_name=None):
        if self.register_status:
            raise SDKError(self.register_status, 60)
        account = FakeAccount(self, username, 70 + len(self.accounts))
        self.accounts[username] = account
        self.rate()
        return account

    def login(self, username, password):
        self.login_count += 1
        self.rate()
        return self.accounts[username]

    def account_from_token(self, token):
        if self.expired:
            raise SDKError(401)
        self.rate()
        return next(iter(self.accounts.values()))

    def write(self, account, body, parent=None, root=None):
        if self.write_status:
            raise SDKError(self.write_status, 60)
        post = {'post_id': 900 + len(self.writes), 'author_id': account.account_id,
                'body': body, 'reply_to': parent, 'root_post_id': root}
        self.writes.append(post)
        self.posts[post['post_id']] = post
        self.rate()
        if self.timeout_write:
            raise TimeoutError('synthetic timeout after remote accepted write')
        return post

    def get_post(self, post_id):
        return self.posts[post_id]


class ActorWorkflowIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.social = FakeSocial()
        self.now = 1792450000.0
        provider = SimpleNamespace(get=lambda _: {'protocol': 'openai_chat', 'model': 'fake', 'max_output_tokens': 1800})
        self.controller = AgentController(Path(self.temp.name) / 'test.sqlite3', Path(self.temp.name) / 'secrets',
                                          provider_store=provider, sdk_factory=self.social.factory, clock=lambda: self.now)
        self.controller.settings({'team_code': 'synthetic-team-code'})
        self.controller.save({'name': 'Synthetic Resident', 'username': 'resident_yes', 'background': 'A fictional Kingstonia resident.',
                              'stance': 'Vote Yes', 'provider_id': 'fake', 'language': 'English'})
        self.agent_id = self.controller.snapshot()['agents'][0]['id']
        # Observation and persistent quota admission have their own integration
        # suites. Keep these workflows deterministic on a frozen fake clock.
        self.evidence = patch('ctn_data.agent_observation.observe', return_value=[
            {'id': 'post:7', 'kind': 'post', 'post_id': 7, 'text': 'A fictional local discussion.',
             'title': 'Discussion', 'version': 'synthetic-v1', 'notification_ids': []}]).start()
        patch('ctn_data.agent_budget.reserve', return_value=None).start()
        self.complete = patch('ctn_data.ai_providers.complete', return_value={'text': json.dumps({
            'action': 'post', 'body': 'A thoughtful fictional Yes discussion.', 'memory': 'I read a local discussion.', 'reason': 'Relevant topic.'}),
            'model': 'fake', 'usage': {}}).start()
        self.addCleanup(patch.stopall)

    def bind(self):
        self.controller.register(self.agent_id)

    def test_archived_old_impression_is_in_actual_next_decision_prompt(self):
        from ctn_data import agent_memory
        self.bind()
        def remember(data, conn):
            for number in range(1, 211):
                source = {'id': f'post:{10000 + number}', 'post_id': 10000 + number,
                          'author_id': 18 if number == 1 else 100 + number, 'version': 'v1',
                          'title': 'Harbour access' if number == 1 else 'Other subjects',
                          'text': 'The ancient harbour accessibility meeting.' if number == 1 else f'Other discussion {number}'}
                agent_memory.record_read(data, self.agent_id, [source], self.now - 1000 + number)
                draft = {'id': f'old:{number}', 'agent_id': self.agent_id, 'sources': [source],
                         'source_ids': [source['id']]}
                agent_memory.record_subjective_updates(data, draft, {'viewpoints': [{
                    'text': 'I remember the ancient harbour accessibility meeting.' if number == 1 else f'Later unrelated impression {number}',
                    'source_ids': [source['id']]}]}, self.now - 1000 + number)
        self.controller.store.change(remember)
        self.evidence.return_value = [{'id': 'post:7', 'post_id': 7, 'author_id': 18,
            'version': 'v1', 'kind': 'post', 'title': 'Ancient harbour access',
            'text': 'Can we revisit the ancient harbour accessibility meeting?', 'notification_ids': []}]
        self.controller.run(self.agent_id)
        context = json.loads(self.complete.call_args.args[1][1]['content'])['autobiographical_memory']
        self.assertTrue(any('ancient harbour' in row['text'] for row in context['subjective_notes']['viewpoints']))
        self.assertTrue(any(row.get('memory_origin') == 'archive' for row in context['observed_reading']))
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), agent_memory.MAX_CONTEXT_CHARACTERS)

    def draft(self):
        self.controller.run(self.agent_id)
        return self.controller.snapshot()['drafts'][0]['id']

    def test_successful_sdk_callbacks_do_not_cancel_registration_or_publish(self):
        from ctn_data.db import connect
        from ctn_data.ingest import save_accounts
        conn = connect(self.controller.db_path)
        try:
            with conn:
                save_accounts(conn, [{'account_id': 70, 'username': 'resident_yes'}])
                conn.execute('INSERT INTO team_accounts(account_id,team_label,is_own,evidence,updated_at) VALUES(?,?,1,?,?)',
                             (70, 'My team', 'Manually verified teammate', '2026-10-20T00:00:00Z'))
        finally:
            conn.close()
        self.bind()
        draft_id = self.draft()
        state = self.controller.publish(draft_id)
        self.assertEqual(state['drafts'][0]['state'], 'published')
        self.assertEqual(len(self.social.writes), 1)
        self.assertNotIn('synthetic-session', json.dumps(state))
        self.assertNotIn('synthetic-team-code', json.dumps(state))
        conn = connect(self.controller.db_path)
        try:
            row = conn.execute('SELECT team_label,evidence FROM team_accounts WHERE account_id=70').fetchone()
            self.assertEqual(row['team_label'], 'My team')
            self.assertEqual(row['evidence'], 'Manually verified teammate')
        finally:
            conn.close()

    def test_publish_enters_shared_analysis_and_sample_feedback_reaches_next_decision(self):
        from datetime import datetime, timezone
        from ctn_data.db import connect
        from ctn_data.ingest import save_counters
        self.bind()
        state = self.controller.publish(self.draft())
        draft = state['drafts'][0]
        feedback = state['agents'][0]['feedback']
        self.assertEqual(feedback['known_post_count'], 1)
        self.assertEqual(feedback['recent_posts'][0]['post_id'], draft['post_id'])
        self.assertIsNone(feedback['metrics']['like_count'])
        conn = connect(self.controller.db_path)
        try:
            self.assertEqual(conn.execute('SELECT username FROM accounts WHERE account_id=?', (draft['account_id'],)).fetchone()[0], 'resident_yes')
            self.assertEqual(conn.execute('SELECT state FROM collection_tasks WHERE task_key=?', ('counter:' + str(draft['post_id']),)).fetchone()[0], 'pending')
            with conn:
                save_counters(conn, draft['post_id'], {'like_count': 3, 'reply_count': 1, 'repost_count': 0, 'view_count': 19},
                              observed_at=datetime.fromtimestamp(self.now, timezone.utc).isoformat())
        finally:
            conn.close()
        self.now += 31  # New sample becomes visible after the bounded feedback TTL.
        self.controller.run(self.agent_id)
        context = json.loads(self.complete.call_args.args[1][1]['content'])
        performance = context['performance_feedback']
        self.assertEqual(performance['recent_authored_posts'][0]['latest_counts']['like_count'], 3)
        self.assertIsNone(performance['official_points'])

    def test_accepted_then_timeout_is_never_replayed_and_can_be_reconciled(self):
        self.bind()
        draft_id = self.draft()
        self.social.timeout_write = True
        with self.assertRaises(AgentError):
            self.controller.publish(draft_id)
        self.assertEqual(self.controller.snapshot()['drafts'][0]['state'], 'uncertain')
        with self.assertRaises(AgentError):
            self.controller.publish(draft_id)
        self.assertEqual(len(self.social.writes), 1)
        state = self.controller.reconcile(draft_id, self.social.writes[0]['post_id'])
        self.assertEqual(state['drafts'][0]['state'], 'published')
        self.assertEqual(len(self.social.writes), 1)

    def test_expired_token_refreshes_existing_identity(self):
        self.bind()
        self.social.expired = True
        self.controller.publish(self.draft())
        self.assertEqual(self.social.login_count, 1)
        self.assertEqual(len(self.social.writes), 1)

    def test_definitive_registration_rejection_does_not_lock_username(self):
        self.social.register_status = 409
        with self.assertRaises(AgentError):
            self.bind()
        self.assertEqual(self.controller.snapshot()['agents'][0]['account_state'], 'unbound')
        self.controller.save({'id': self.agent_id, 'name': 'Synthetic Resident', 'username': 'another_yes'})
        self.social.register_status = None
        self.bind()
        self.assertEqual(self.controller.snapshot()['agents'][0]['account_state'], 'ready')

    def test_definitive_rate_limit_keeps_retryable_draft(self):
        self.bind()
        draft_id = self.draft()
        self.social.write_status = 429
        with self.assertRaises(AgentError):
            self.controller.publish(draft_id)
        self.assertEqual(self.controller.snapshot()['drafts'][0]['state'], 'pending')
        self.assertEqual(len(self.social.writes), 0)
        self.now += 2000
        self.social.write_status = None
        self.controller.publish(draft_id)
        self.assertEqual(len(self.social.writes), 1)

    def test_pause_does_not_generate_or_publish(self):
        self.controller.settings({'enabled': False})
        self.controller.save({'id': self.agent_id, 'name': 'Synthetic Resident', 'enabled': True, 'mode': 'auto'})
        self.controller.tick()
        self.complete.assert_not_called()
        self.assertEqual(self.social.writes, [])

    def test_role_memory_is_separate(self):
        self.controller.memory(self.agent_id, 'First role private memory.')
        self.controller.save({'name': 'Other Resident', 'username': 'other_yes', 'provider_id': 'fake'})
        other = next(a for a in self.controller.snapshot()['agents'] if a['id'] != self.agent_id)
        self.controller.memory(other['id'], 'Other role private memory.')
        roles = {a['id']: a['memory'] for a in self.controller.snapshot()['agents']}
        self.assertEqual(roles[self.agent_id], 'First role private memory.')
        self.assertEqual(roles[other['id']], 'Other role private memory.')


    def test_remote_success_then_local_lease_loss_stays_uncertain(self):
        self.bind()
        draft_id = self.draft()
        original = self.social.write
        def delayed_commit(*args, **kwargs):
            result = original(*args, **kwargs)
            self.now += 400
            return result
        self.social.write = delayed_commit
        with self.assertRaises(AgentError):
            self.controller.publish(draft_id)
        self.assertEqual(len(self.social.writes), 1)
        self.assertEqual(self.controller.snapshot()['drafts'][0]['state'], 'uncertain')
        with self.assertRaises(AgentError):
            self.controller.publish(draft_id)
        self.assertEqual(len(self.social.writes), 1)

    def test_wrong_token_identity_does_not_post(self):
        self.bind()
        draft_id = self.draft()
        self.social.accounts['other'] = FakeAccount(self.social, 'other', 999)
        self.social.account_from_token = lambda _: self.social.accounts['other']
        with self.assertRaises(AgentError):
            self.controller.publish(draft_id)
        self.assertEqual(self.social.writes, [])

    def test_account_cannot_bind_to_two_independent_roles(self):
        self.bind()
        self.controller.save({'name': 'Other Role', 'username': 'other_role'})
        second = next(a for a in self.controller.snapshot()['agents'] if a['id'] != self.agent_id)
        with self.assertRaises(AgentError):
            self.controller.bind(second['id'], 'resident_yes', 'synthetic-password')
        state = {a['id']: a for a in self.controller.snapshot()['agents']}
        self.assertIsNone(state[second['id']]['account_id'])

    def test_stop_during_generation_prevents_automatic_post(self):
        self.bind()
        self.controller.save({'id': self.agent_id, 'name': 'Synthetic Resident', 'mode': 'auto', 'enabled': True})
        self.controller.settings({'enabled': True})
        answer = self.complete.return_value
        def stop_before_return(*args, **kwargs):
            self.controller.stop()
            return answer
        self.complete.side_effect = stop_before_return
        self.controller.run(self.agent_id)
        self.assertEqual(self.social.writes, [])
        self.assertEqual(self.controller.snapshot()['drafts'][0]['state'], 'pending')

    def test_legacy_daily_limit_does_not_disable_an_automatic_role(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        self.bind()
        self.controller.save({'id': self.agent_id, 'name': 'Synthetic Resident', 'mode': 'auto', 'enabled': True, 'daily_limit': 1})
        self.draft()
        self.controller.save({'name': 'Second Resident', 'username': 'second_resident', 'mode': 'auto', 'enabled': True, 'provider_id': 'fake'})
        second = next(a for a in self.controller.snapshot()['agents'] if a['id'] != self.agent_id)
        self.controller.register(second['id'])
        self.controller.run(second['id'])
        self.controller.settings({'enabled': True})
        day = datetime.fromtimestamp(self.now, ZoneInfo('Australia/Sydney')).date().isoformat()
        self.controller.store.change(lambda d, c: d['rate'].setdefault('accounts', {}).update({self.agent_id: {'day': day, 'attempts': 1, 'last_at': 0}}))
        with patch.object(self.controller, '_publish') as publish:
            self.controller.tick()
        self.assertEqual(publish.call_count, 1)
        sent_id = publish.call_args.args[0]
        self.assertEqual(self.controller.store.read()['drafts'][sent_id]['agent_id'], self.agent_id)


    def test_agent_page_supports_direct_navigation(self):
        from fastapi.testclient import TestClient
        from ctn_data.server import create_app
        web_root = Path(self.temp.name) / 'web'
        web_root.mkdir()
        (web_root / 'index.html').write_text('<!doctype html><div id="root"></div>', encoding='utf-8')
        with TestClient(create_app(self.controller.db_path, web_root=web_root), base_url='http://127.0.0.1:8765') as client:
            result = client.get('/agents')
            self.assertEqual(result.status_code, 200)
            self.assertIn('text/html', result.headers['content-type'])

    def test_tracking_failure_after_remote_acceptance_is_uncertain_until_reconciled(self):
        from ctn_data.db import connect
        self.bind()
        draft_id = self.draft()
        with patch('ctn_data.agent_tracking.record_success', side_effect=RuntimeError('synthetic local index failure')):
            with self.assertRaises(AgentError):
                self.controller.publish(draft_id)
        state = self.controller.snapshot()
        self.assertEqual(state['drafts'][0]['state'], 'uncertain')
        self.assertEqual(len(self.social.writes), 1)
        self.assertEqual(state['agents'][0]['feedback']['known_post_count'], 0)
        with self.assertRaises(AgentError):
            self.controller.publish(draft_id)
        state = self.controller.reconcile(draft_id, self.social.writes[0]['post_id'])
        self.assertEqual(state['drafts'][0]['state'], 'published')
        self.assertEqual(state['agents'][0]['feedback']['known_post_count'], 1)
        self.assertEqual(len(self.social.writes), 1)


    def test_agent_api_requires_member_and_csrf(self):
        from fastapi.testclient import TestClient
        from ctn_data.server import create_app
        from tests.auth_helpers import login
        client = TestClient(create_app(self.controller.db_path), base_url='http://127.0.0.1:8765')
        self.addCleanup(client.close)
        token = client.get('/api/bootstrap').json()['csrf_token']
        self.assertEqual(client.get('/api/agents').status_code, 401)
        self.assertEqual(client.post('/api/agents/settings', json={'enabled': False}, headers={'X-CTN-Token': token}).status_code, 401)
        token = login(client)
        self.assertEqual(client.get('/api/agents').status_code, 200)
        self.assertEqual(client.post('/api/agents/settings', json={'enabled': False}).status_code, 403)
        self.assertEqual(client.post('/api/agents/settings', json={'enabled': False}, headers={'X-CTN-Token': token}).status_code, 200)
        self.assertEqual(client.post('/api/agents/run', json={'agent_id': 'invalid'}, headers={'X-CTN-Token': token}).status_code, 400)

    def test_agent_publish_api_accepts_empty_like_and_repost_bodies(self):
        from fastapi.testclient import TestClient
        from ctn_data.server import create_app
        from tests.auth_helpers import login
        client = TestClient(create_app(self.controller.db_path), base_url='http://127.0.0.1:8765')
        self.addCleanup(client.close)
        csrf = login(client)
        controller = client.app.state.agent_control
        for action in ('like', 'repost'):
            with self.subTest(action=action), patch.object(controller, 'publish', return_value={'ok': True}) as publish:
                response = client.post('/api/agents/publish', json={'draft_id': self.agent_id, 'body': ''},
                                       headers={'X-CTN-Token': csrf})
                self.assertEqual(response.status_code, 200, response.text)
                publish.assert_called_once_with(self.agent_id, '')


if __name__ == '__main__':
    unittest.main()
