"""Manual previews use authenticated fake reads, never models or writes."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from ctn_data import agent_audit
from ctn_data.agent_control import AgentController, AgentError, AgentStore
from ctn_data.agent_manual import prepare


class ManualSDK:
    def __init__(self, test):
        self.test = test
        self.calls = []
        self.closed = 0
        self.post_result = {'post_id': 7, 'author_id': 9, 'body': 'An actual returned discussion',
            'root_post_id': 3, 'reply_to_post_id': 3, 'kind': 'reply', 'created_at': '2026-10-20T01:00:00Z',
            'token': 'MUST-NOT-BE-STORED'}
        self.account_result = {'account_id': 53, 'username': 'actual_person', 'display_name': 'Actual name',
                               'verified': True, 'private': False, 'password': 'MUST-NOT-BE-STORED'}
        self.actor = SimpleNamespace(account_id=42, username='actual_actor', token='synthetic-session')
        self.actor.get_post = self.get_post
        self.actor.get_account = self.get_account
        self.after_read = None
        self.error = None

    def read(self, name):
        self.test.assertFalse(self.test.in_transaction, 'SDK work must stay outside state transactions')
        self.calls.append(name)
        if self.error:
            raise self.error

    def factory(self, **kwargs):
        self.read('factory')
        self.test.assertEqual(kwargs['max_retries'], 0)
        return self

    def account_from_token(self, token):
        self.read('account_from_token')
        self.test.assertEqual(token, 'synthetic-session')
        return self.actor

    def get_post(self, post_id):
        self.read(('get_post', post_id))
        if self.after_read:
            self.after_read()
        return copy.deepcopy(self.post_result)

    def get_account(self, account_id):
        self.read(('get_account', account_id))
        if self.after_read:
            self.after_read()
        return copy.deepcopy(self.account_result)

    def close(self):
        self.closed += 1


class AgentManualPrepareTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.in_transaction = False
        self.sdk = ManualSDK(self)
        self.controller = AgentController(self.root / 'manual.sqlite3', self.root / 'secrets',
                                          sdk_factory=self.sdk.factory)
        self.controller.save({'name': 'Maya', 'username': 'maya_local', 'allowed_actions': ['skip'],
                              'enabled': False, 'mode': 'review'})
        self.agent_id = next(iter(self.controller.store.read()['agents']))
        self.controller.store.change(lambda state, conn: state['agents'][self.agent_id].update(
            account_state='ready', account_id=42))
        self.controller._save_secret(self.agent_id, {'account_id': 42, 'username': 'actual_actor',
            'password': 'synthetic-password', 'token': 'synthetic-session'})
        original_change = self.controller.store.change

        def tracked_change(callback):
            def in_transaction(data, conn):
                self.in_transaction = True
                try:
                    return callback(data, conn)
                finally:
                    self.in_transaction = False
            return original_change(in_transaction)
        self.controller.store.change = tracked_change
        self.model = patch('ctn_data.ai_providers.complete', side_effect=AssertionError('No model request allowed')).start()

        def read_call(controller, bucket, function, *args, **kwargs):
            self.assertEqual(bucket, 'read')
            self.assertFalse(self.in_transaction)
            return function(*args, **kwargs)
        patch('ctn_data.agent_budget.call', side_effect=read_call).start()
        self.addCleanup(patch.stopall)

    def payload(self, action='post', **values):
        payload = {'agent_id': self.agent_id, 'action': action, 'idempotency_key': str(uuid.uuid4())}
        if action in ('post', 'reply'):
            payload['body'] = 'Human writing, with an ordinary thought.'
        if action in ('reply', 'repost', 'like', 'unlike', 'delete_post'):
            payload['target_post_id'] = 7
        if action in ('follow', 'unfollow'):
            payload['target_account_id'] = 53
        return {**payload, **values}

    def state(self):
        return self.controller.store.read()

    def test_post_creates_manual_pending_with_actor_preview_without_model_or_memory(self):
        before = self.state()['agents'][self.agent_id]
        response = prepare(self.controller, self.payload(body='Human thought — my own punctuation.'))
        draft = response['draft']
        self.assertFalse(response['idempotent'])
        self.assertEqual(draft['state'], 'pending')
        self.assertEqual(draft['origin'], 'manual')
        self.assertTrue(draft['manual_only'])
        self.assertTrue(draft['manual_prepared'])
        self.assertEqual(draft['user_instruction'], 'manual_operator_request')
        self.assertEqual(draft['reason'], '用户手动安排')
        self.assertEqual(draft['body'], 'Human thought — my own punctuation.')
        self.assertEqual(draft['style_warnings'], [])
        self.assertEqual(draft['sources'], [])
        self.assertEqual(response['preview']['actor'], {'account_id': 42, 'username': 'actual_actor', 'name': 'Maya'})
        self.assertEqual(self.state()['memory'], {})
        self.assertEqual(self.state()['agents'][self.agent_id], before)
        self.assertEqual(self.sdk.calls, ['factory', 'account_from_token'])
        self.assertEqual(self.sdk.closed, 1)
        logs = self.controller.logs(self.agent_id, kind='manual_prepared')
        self.assertEqual(logs['total'], 1)
        self.assertTrue(logs['items'][0]['details']['manual'])
        self.assertEqual(logs['items'][0]['details']['origin'], 'manual')
        self.assertEqual(self.controller.logs(self.agent_id, kind='manual_prepared', q='"manual":true')['total'], 1)
        self.model.assert_not_called()

    def test_reply_repost_like_and_unlike_use_exact_observed_post_without_fake_author(self):
        for action in ('reply', 'repost', 'like', 'unlike'):
            with self.subTest(action=action):
                result = prepare(self.controller, self.payload(action))
                draft = result['draft']
                self.assertEqual(draft['source_ids'], ['post:7'])
                source = draft['sources'][0]
                self.assertEqual(source['post_id'], 7)
                self.assertEqual(source['author_id'], 9)
                self.assertEqual(source['root_post_id'], 3)
                self.assertEqual(source['text'], self.sdk.post_result['body'])
                self.assertEqual(result['preview']['target_post']['author_id'], 9)
                self.assertEqual(result['preview']['target_post']['body'], self.sdk.post_result['body'])
                self.assertNotIn('MUST-NOT-BE-STORED', json.dumps(draft))
                self.assertEqual(draft['reply_to'], 7 if action == 'reply' else None)

    def test_follow_unfollow_use_real_profile_identity_and_no_post_target(self):
        for action in ('follow', 'unfollow'):
            with self.subTest(action=action):
                result = prepare(self.controller, self.payload(action))
                draft, preview = result['draft'], result['preview']['target_account']
                self.assertEqual(draft['target_account_id'], 53)
                self.assertIsNone(draft['target_post_id'])
                self.assertEqual(draft['sources'][0]['author_id'], 53)
                self.assertEqual(draft['sources'][0]['kind'], 'account')
                self.assertEqual(draft['source_ids'], ['account:53'])
                self.assertEqual(preview, {'account_id': 53, 'username': 'actual_person', 'display_name': 'Actual name'})
                self.assertNotIn('MUST-NOT-BE-STORED', json.dumps(draft))
        self.assertNotIn(('get_post', 7), self.sdk.calls)

    def test_delete_requires_exact_owner_and_preserves_target_body_preview(self):
        with self.assertRaisesRegex(AgentError, '只能删除'):
            prepare(self.controller, self.payload('delete_post'))
        self.assertEqual(self.state()['drafts'], {})
        self.sdk.post_result['author_id'] = 42
        result = prepare(self.controller, self.payload('delete_post'))
        draft = result['draft']
        self.assertEqual(draft['action'], 'delete_post')
        self.assertEqual(draft['body'], '')
        self.assertEqual(result['preview']['target_post']['author_id'], 42)
        self.assertEqual(result['preview']['target_post']['body'], self.sdk.post_result['body'])
        self.assertNotIn('delete_confirmed', draft)
        self.assertNotIn('delete_confirmation_token', draft)

    def test_missing_or_boolean_owner_cannot_authorize_deletion(self):
        for author in (None, True, '42'):
            self.sdk.post_result['author_id'] = author
            with self.subTest(author=author), self.assertRaises(AgentError):
                prepare(self.controller, self.payload('delete_post'))
        self.assertEqual(self.state()['drafts'], {})

    def test_identical_request_reuses_same_draft_preview_and_reads_only_once(self):
        payload = self.payload('follow')
        first = prepare(self.controller, payload)
        calls = list(self.sdk.calls)
        second = prepare(self.controller, dict(payload))
        self.assertTrue(second['idempotent'])
        self.assertEqual(second['draft_id'], first['draft_id'])
        self.assertEqual(second['preview'], first['preview'])
        self.assertEqual(self.sdk.calls, calls)
        self.assertEqual(self.controller.logs(self.agent_id, kind='manual_prepared')['total'], 1)

    def test_same_key_changed_payload_is_conflict_without_new_read(self):
        payload = self.payload()
        prepare(self.controller, payload)
        calls = list(self.sdk.calls)
        with self.assertRaises(AgentError) as error:
            prepare(self.controller, {**payload, 'body': 'A different instruction'})
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.sdk.calls, calls)
        self.assertEqual(len(self.state()['drafts']), 1)

    def test_idempotent_published_result_stays_single_and_trimmed_history_never_recreates(self):
        payload = self.payload()
        result = prepare(self.controller, payload)
        self.controller.store.change(lambda state, conn: state['drafts'][result['draft_id']].update(state='published'))
        published = prepare(self.controller, payload)
        self.assertTrue(published['idempotent'])
        self.assertEqual(published['draft']['state'], 'published')
        self.controller.store.change(lambda state, conn: state['drafts'].pop(result['draft_id']))
        with self.assertRaisesRegex(AgentError, '未创建新动作') as error:
            prepare(self.controller, payload)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.state()['drafts'], {})
        self.assertIn(payload['idempotency_key'], self.state()['rate']['manual_requests'])

    def test_idempotency_index_survives_store_reopen_and_account_change_rejects(self):
        payload = self.payload()
        first = prepare(self.controller, payload)
        self.controller.store = AgentStore(self.controller.db_path)
        self.assertEqual(prepare(self.controller, payload)['draft_id'], first['draft_id'])
        self.controller.store.change(lambda state, conn: state['agents'][self.agent_id].update(account_id=99))
        with self.assertRaises(AgentError) as error:
            prepare(self.controller, payload)
        self.assertEqual(error.exception.status_code, 409)

    def test_published_delete_replay_hides_internal_tokens_without_mutating_receipt_or_audit(self):
        self.sdk.post_result['author_id'] = 42
        payload = self.payload('delete_post')
        first = prepare(self.controller, payload)
        def delivered(state, conn):
            state['drafts'][first['draft_id']].update(state='published', delete_confirmed=True,
                send_token='INTERNAL-SEND-TOKEN', delete_confirmation_token='INTERNAL-DELETE-TOKEN')
            self.controller._event(state, 'manual_delete_confirmed', 'User confirmed the exact own post',
                self.agent_id, first['draft_id'], details={'action': 'delete_post', 'target_post_id': 7})
        self.controller.store.change(delivered)
        replay = prepare(self.controller, payload)
        self.assertTrue(replay['idempotent'])
        self.assertNotIn('send_token', replay['draft'])
        self.assertNotIn('delete_confirmation_token', replay['draft'])
        self.assertNotIn('INTERNAL-', json.dumps(replay))
        stored = self.state()['drafts'][first['draft_id']]
        self.assertEqual(stored['send_token'], 'INTERNAL-SEND-TOKEN')
        self.assertEqual(stored['delete_confirmation_token'], 'INTERNAL-DELETE-TOKEN')
        self.assertTrue(stored['delete_confirmed'])
        self.assertEqual(self.controller.logs(self.agent_id, kind='manual_delete_confirmed')['total'], 1)

    def test_cross_role_key_reuse_never_discloses_other_role_draft(self):
        payload = self.payload()
        original = prepare(self.controller, payload)
        self.controller.save({'name': 'Ethan', 'username': 'ethan_test'})
        other_id = next(row['id'] for row in self.state()['agents'].values() if row['name'] == 'Ethan')
        with self.assertRaises(AgentError) as error:
            prepare(self.controller, {**payload, 'agent_id': other_id})
        self.assertEqual(error.exception.status_code, 409)
        self.assertNotIn(original['draft_id'], str(error.exception))

    def test_autonomous_pending_drafts_do_not_consume_manual_pending_limit(self):
        def automated(state, conn):
            for number in range(15):
                draft_id = f'auto-{number}'
                state['drafts'][draft_id] = {'id': draft_id, 'agent_id': self.agent_id, 'state': 'pending',
                    'origin': 'ai', 'created_at': '2026-10-20T01:00:00Z'}
        self.controller.store.change(automated)
        self.assertEqual(prepare(self.controller, self.payload())['draft']['origin'], 'manual')

    def test_manual_pending_limit_checked_before_sdk_and_duplicate_still_returns(self):
        first_payload = self.payload()
        first = prepare(self.controller, first_payload)
        for _ in range(9):
            prepare(self.controller, self.payload())
        calls = list(self.sdk.calls)
        with self.assertRaisesRegex(AgentError, '10 条'):
            prepare(self.controller, self.payload())
        self.assertEqual(self.sdk.calls, calls)
        self.assertEqual(prepare(self.controller, first_payload)['draft_id'], first['draft_id'])

    def test_invalid_payloads_fail_before_sdk_or_persistence(self):
        cases = [self.payload(idempotency_key='not-a-uuid'), self.payload(action='skip'),
            self.payload(body=''), self.payload(body='x' * 501), self.payload(body='bad\x00text'),
            self.payload('reply', target_post_id=True), self.payload('reply', target_post_id='7'),
            self.payload('like', body='No body'), self.payload('delete_post', body='No body'),
            self.payload('follow', target_account_id=0), self.payload('follow', target_post_id=7),
            self.payload(target_post_id=7), self.payload(unexpected=True)]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(AgentError):
                prepare(self.controller, payload)
        self.assertEqual(self.sdk.calls, [])
        self.assertEqual(self.state()['drafts'], {})
        self.assertNotIn('manual_requests', self.state()['rate'])

    def test_unbound_active_generation_and_self_follow_fail_before_sdk(self):
        self.controller.store.change(lambda state, conn: state['agents'][self.agent_id].update(account_state='unbound'))
        with self.assertRaises(AgentError):
            prepare(self.controller, self.payload())
        self.controller.store.change(lambda state, conn: state['agents'][self.agent_id].update(
            account_state='ready', generation={'status': 'running', 'heartbeat_until': self.controller.clock() + 60}))
        with self.assertRaises(AgentError):
            prepare(self.controller, self.payload())
        self.controller.store.change(lambda state, conn: state['agents'][self.agent_id].pop('generation'))
        with self.assertRaises(AgentError):
            prepare(self.controller, self.payload('follow', target_account_id=42))
        self.assertEqual(self.sdk.calls, [])
        self.assertEqual(self.state()['drafts'], {})

    def test_wrong_session_and_target_id_fail_without_manual_intention(self):
        self.sdk.actor.account_id = 77
        with self.assertRaises(AgentError):
            prepare(self.controller, self.payload())
        self.sdk.actor.account_id = 42
        for action, field, invalid in (('reply', 'post_id', 99), ('follow', 'account_id', True)):
            remote = self.sdk.post_result if action == 'reply' else self.sdk.account_result
            remote[field] = invalid
            with self.subTest(action=action), self.assertRaises(AgentError):
                prepare(self.controller, self.payload(action))
        self.assertEqual(self.state()['drafts'], {})
        self.assertEqual(self.state()['memory'], {})
        self.assertEqual(self.controller.logs(self.agent_id, kind='manual_prepared')['total'], 0)

    def test_remote_error_is_visible_and_client_is_closed(self):
        self.sdk.after_read = lambda: (_ for _ in ()).throw(RuntimeError('connection reset by peer'))
        with self.assertRaises(AgentError) as error:
            prepare(self.controller, self.payload('reply'))
        self.assertIn('RuntimeError', str(error.exception))
        self.assertIn('connection reset by peer', str(error.exception))
        self.assertEqual(self.state()['drafts'], {})
        self.assertEqual(self.state()['memory'], {})
        self.assertEqual(self.sdk.closed, 1)

    def test_target_preview_uses_authenticated_account_reads_including_own_private_post(self):
        self.sdk.post_result.update(author_id=42, visibility='private')
        # The actor retains its original authenticated read method. Any use
        # of the separate unauthenticated social client must fail this test.
        self.sdk.get_post = lambda post_id: (_ for _ in ()).throw(AssertionError('public read forbidden'))
        result = prepare(self.controller, self.payload('delete_post'))
        self.assertEqual(result['preview']['target_post']['author_id'], 42)
        self.assertEqual(result['draft']['sources'][0]['visibility'], 'private')

    def test_account_change_during_preview_rejects_but_persona_change_is_allowed(self):
        self.sdk.after_read = lambda: self.controller.store.change(lambda state, conn:
            state['agents'][self.agent_id].update(account_id=77))
        with self.assertRaises(AgentError):
            prepare(self.controller, self.payload('reply'))
        self.assertEqual(self.state()['drafts'], {})
        self.controller.store.change(lambda state, conn: state['agents'][self.agent_id].update(account_id=42))
        self.sdk.after_read = lambda: self.controller.store.change(lambda state, conn:
            state['agents'][self.agent_id].update(version=20, voice='Changed voice'))
        draft = prepare(self.controller, self.payload('reply'))['draft']
        self.assertEqual(draft['persona_version'], 20)
        self.assertEqual(draft['body'], 'Human writing, with an ordinary thought.')

    def test_audit_failure_rolls_back_both_draft_and_request_index(self):
        original = agent_audit.record
        def fail_after_record(conn, events):
            rows = list(events)
            count = original(conn, rows)
            if any(row['kind'] == 'manual_prepared' for row in rows):
                raise RuntimeError('synthetic audit write failure')
            return count
        with patch('ctn_data.agent_audit.record', side_effect=fail_after_record):
            with self.assertRaises(RuntimeError):
                prepare(self.controller, self.payload())
        self.assertEqual(self.state()['drafts'], {})
        self.assertNotIn('manual_requests', self.state()['rate'])
        self.assertEqual(self.controller.logs(self.agent_id, kind='manual_prepared')['total'], 0)
        self.assertEqual(self.state()['memory'], {})


if __name__ == '__main__':
    unittest.main()
