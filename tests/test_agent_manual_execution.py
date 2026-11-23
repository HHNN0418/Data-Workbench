"""Manual account control uses the outbox without autonomous policy limits."""
from datetime import datetime
import json
import unittest
import uuid
from unittest.mock import patch
from zoneinfo import ZoneInfo

from capturethenarrative._http import APIError

from ctn_data import agent_actions
from ctn_data.agent_budget import Deferred, LIMITS
from ctn_data.agent_control import AgentError
from ctn_data.db import connect, utc_now
from ctn_data.ingest import save_counters, save_posts
from tests import test_agent_relationship_actions as fixtures


class ManualExecutionTests(unittest.TestCase):
    def setUp(self):
        fixtures.RelationshipActions.setUp(self)
        self.deleted = []
        self.me_reads = 0
        def me():
            self.me_reads += 1
            return {'account': {'account_id': self.account.account_id}}
        self.account.me = me
        def read(post_id):
            if post_id not in self.social.posts:
                raise APIError('GET', f'https://social.legitreal.com/api/posts/{post_id}', 404,
                               '{"error":"post not found"}')
            return self.social.posts[post_id]
        self.account.get_post = read
        self.account.get_account = self.social.get_account
        def delete(post_id):
            self.deleted.append(post_id)
            self.social.posts.pop(post_id, None)
            if self.social.timeout == 'delete_post':
                raise TimeoutError('delete accepted but acknowledgement lost')
            return None
        self.account.delete_post = delete
        self.social.posts[71] = {'post_id': 71, 'author_id': self.account.account_id,
                                'body': 'An actual earlier post.', 'kind': 'post'}
        self.controller.store.change(lambda data, conn: data['agents'][self.agent_id].update(
            allowed_actions=['skip'], interval_seconds=1800, daily_limit=1))

    def draft(self, action, *, body='', target=7, account_target=99, markers=None):
        draft_id = str(uuid.uuid4())
        def save(data, conn):
            agent = data['agents'][self.agent_id]
            post_target = target if action in ('reply', 'repost', 'like', 'unlike', 'delete_post') else None
            sources = [dict(self.social.posts[target], id='post:' + str(target), kind='post')] if post_target else [
                {'id': 'account:' + str(account_target), 'kind': 'account', 'author_id': account_target}]
            data['drafts'][draft_id] = {'id': draft_id, 'agent_id': self.agent_id,
                'account_id': agent['account_id'], 'persona_version': agent['version'],
                'body': body, 'action': action, 'target_post_id': post_target,
                'target_account_id': account_target if action in ('follow', 'unfollow') else None,
                'sources': sources, 'source_ids': [], 'notification_ids': [],
                'reason': 'Member explicitly requested this operation.', 'state': 'pending',
                'created_at': utc_now(), 'version': 1, 'origin': 'manual', 'manual_only': True,
                'manual_prepared': True, 'user_instruction': 'manual_operator_request',
                'manual_request_id': str(uuid.uuid4()), **(markers or {})}
        self.controller.store.change(save)
        return draft_id

    def row(self, draft_id):
        return self.controller.store.read()['drafts'][draft_id]

    def publish(self, draft_id, *, confirm=None):
        self.controller.publish(draft_id, confirmation_post_id=confirm)
        return self.row(draft_id)

    def test_manual_post_ignores_autonomous_permissions_and_style_without_changing_them(self):
        text = 'First—second. **An operator note.**'
        draft_id = self.draft('post', body=text)
        self.controller.save({'id': self.agent_id, 'name': 'Changed persona'})
        row = self.publish(draft_id)
        self.assertEqual(row['state'], 'published')
        self.assertEqual(self.social.writes[0]['body'], text)
        self.assertEqual(self.controller.store.read()['agents'][self.agent_id]['allowed_actions'], ['skip'])
        self.model.assert_not_called()

    def test_manual_reply_repost_and_like_use_real_targets_and_common_executor(self):
        for action, body in (('reply', 'A manual reply—kept exactly.'), ('repost', 'My exact comment.'), ('like', '')):
            with self.subTest(action=action):
                self.now += 1
                self.assertEqual(self.publish(self.draft(action, body=body))['state'], 'published')
        self.assertEqual(self.social.writes[0]['reply_to_post_id'], 7)
        self.assertEqual(self.social.writes[0]['root_post_id'], 3)
        self.assertEqual(self.social.writes[1]['repost_of_post_id'], 7)
        self.assertEqual(self.social.like_calls, [(10, 7)])

    def test_manual_own_post_interaction_is_decided_by_platform_without_expanding_autonomy(self):
        self.publish(self.draft('like', target=71))
        self.now += 1
        self.publish(self.draft('unlike', target=71))
        self.assertEqual(self.social.like_calls, [(10, 71)])
        self.assertEqual(self.relationship_writes, [('unlike', 71)])
        self.assertEqual(self.controller.store.read()['agents'][self.agent_id]['allowed_actions'], ['skip'])

    def test_manual_relationship_reversal_bypasses_strategy_cooldown_and_uses_separate_counts(self):
        day = datetime.fromtimestamp(self.now, ZoneInfo('Australia/Sydney')).date().isoformat()
        self.controller.store.change(lambda data, conn: data['rate'].setdefault('accounts', {}).update(
            {self.agent_id: {'day': day, 'attempts': 1, 'last_at': self.now}}))
        self.publish(self.draft('follow'))
        self.now += 3
        self.publish(self.draft('unfollow'))
        self.now += 3
        self.account.likes.add(7)
        self.publish(self.draft('unlike'))
        data = self.controller.store.read()
        self.assertEqual(self.relationship_writes, [('follow', 99), ('unfollow', 99), ('unlike', 7)])
        self.assertEqual(data['rate']['accounts'][self.agent_id]['attempts'], 1)
        self.assertEqual(data['rate']['manual_accounts'][self.agent_id]['attempts'], 3)

    def test_automatic_execution_rejects_manual_even_when_autonomous_role_is_enabled(self):
        draft_id = self.draft('post', body='Manual only.')
        self.controller.store.change(lambda data, conn: data['agents'][self.agent_id].update(
            enabled=True, allowed_actions=['post', 'skip']))
        with self.controller._claim(self.agent_id) as token:
            with self.assertRaisesRegex(AgentError, '人工指令只能'):
                agent_actions.execute(self.controller, draft_id, None, token, automatic=True)
        self.assertEqual(self.social.writes, [])

    def test_partial_manual_marker_cannot_bypass_autonomous_permissions(self):
        for markers in ({'manual_prepared': False}, {'manual_only': False}, {'user_instruction': ''}):
            with self.subTest(markers=markers), self.assertRaisesRegex(AgentError, '人工指令来源'):
                self.publish(self.draft('post', body='Not a prepared manual request.', markers=markers))
        self.assertEqual(self.social.writes, [])

    def test_domain_body_validation_still_applies_to_manual_text(self):
        for action, body in (('post', ''), ('reply', ''), ('post', 'a' * 501), ('like', 'not empty'), ('delete_post', 'text')):
            with self.subTest(action=action, body_length=len(body)), self.assertRaises(AgentError):
                self.publish(self.draft(action, body=body, target=71 if action == 'delete_post' else 7), confirm=71)
        self.assertEqual(self.social.writes, [])
        self.assertEqual(self.deleted, [])

    def test_manual_still_respects_global_platform_wait_and_publisher_lease(self):
        for change in (lambda data: data['rate'].update(not_before=self.now + 30),
                       lambda data: data['leases'].update(publisher={'until': self.now + 30, 'token': 'someone'})):
            draft_id = self.draft('post', body=str(uuid.uuid4()))
            self.controller.store.change(lambda data, conn: change(data))
            with self.assertRaises(AgentError):
                self.publish(draft_id)
            self.controller.store.change(lambda data, conn: (data['rate'].pop('not_before', None), data['leases'].pop('publisher', None)))
        self.assertEqual(self.social.writes, [])

    def test_manual_budget_deferral_refunds_only_manual_counter(self):
        draft_id = self.draft('follow')
        previous = self.call.side_effect
        self.call.side_effect = lambda controller, bucket, method, *args, **kwargs: (
            (_ for _ in ()).throw(Deferred(bucket, 3)) if bucket == 'follow' else previous(controller, bucket, method, *args, **kwargs))
        with self.assertRaises(AgentError):
            self.publish(draft_id)
        rate = self.controller.store.read()['rate']
        self.assertEqual(rate['manual_accounts'][self.agent_id].get('attempts', 0), 0)
        self.assertNotIn(self.agent_id, rate.get('accounts', {}))

    def test_delete_sdk_none_creates_tombstone_and_preserves_historical_data(self):
        original_id = str(uuid.uuid4())
        def historical(data, conn):
            save_posts(conn, [self.social.posts[71]], 'earlier-real-observation', utc_now())
            save_counters(conn, 71, {'like_count': 4}, utc_now())
            data['drafts'][original_id] = {'id': original_id, 'agent_id': self.agent_id, 'account_id': 10,
                'action': 'post', 'post_id': 71, 'body': 'An actual earlier post.', 'state': 'published',
                'created_at': utc_now(), 'published_at': utc_now()}
        self.controller.store.change(historical)
        delete_id = self.draft('delete_post', target=71)
        row = self.publish(delete_id, confirm=71)
        self.assertEqual(row['state'], 'published')
        self.assertIsNone(row['post_id'])
        self.assertEqual(self.deleted, [71])
        self.assertIn('delete_post', self.buckets)
        self.assertGreaterEqual(self.me_reads, 1)
        data = self.controller.store.read()
        self.assertEqual(data['drafts'][original_id]['state'], 'published')
        self.assertEqual(data['drafts'][original_id]['body'], 'An actual earlier post.')
        self.assertEqual(data['drafts'][original_id]['remote_delete_draft_id'], delete_id)
        self.assertTrue(data['rate']['remote_deleted_posts']['10:71']['remote_deleted'])
        conn = connect(self.controller.db_path)
        try:
            self.assertEqual(conn.execute('SELECT body FROM posts WHERE post_id=71').fetchone()[0], 'An actual earlier post.')
            self.assertEqual(conn.execute('SELECT like_count FROM counter_snapshots WHERE post_id=71').fetchone()[0], 4)
            tombstone = json.loads(conn.execute("SELECT value FROM checkpoints WHERE key='agent_tracking:deleted:10:71'").fetchone()[0])
            self.assertEqual(tombstone['delete_draft_id'], delete_id)
        finally:
            conn.close()

    def test_delete_requires_target_confirmation_and_current_claim_token(self):
        draft_id = self.draft('delete_post', target=71)
        for confirm in (None, 7):
            with self.subTest(confirm=confirm), self.assertRaises(AgentError):
                self.publish(draft_id, confirm=confirm)
        self.controller.store.change(lambda data, conn: data['drafts'][draft_id].update(
            delete_confirmed=True, delete_confirmation_token='old-claim'))
        with self.controller._claim(self.agent_id) as token:
            with self.assertRaisesRegex(AgentError, '明确确认'):
                agent_actions.execute(self.controller, draft_id, None, token)
        self.assertEqual(self.deleted, [])

    def test_delete_rejects_other_author_or_changed_authenticated_identity(self):
        with self.assertRaisesRegex(AgentError, '本人发布'):
            self.publish(self.draft('delete_post', target=7), confirm=7)
        draft_id = self.draft('delete_post', target=71)
        self.account.me = lambda: {'account': {'account_id': 999}}
        with self.assertRaisesRegex(AgentError, '认证账号'):
            self.publish(draft_id, confirm=71)
        self.assertEqual(self.deleted, [])

    def test_delete_timeout_is_never_replayed_and_owner_404_reconciles(self):
        draft_id = self.draft('delete_post', target=71)
        self.social.timeout = 'delete_post'
        with self.assertRaises(AgentError):
            self.publish(draft_id, confirm=71)
        self.assertEqual(self.row(draft_id)['state'], 'uncertain')
        with self.assertRaises(AgentError):
            self.publish(draft_id, confirm=71)
        self.controller.reconcile(draft_id)
        row = self.row(draft_id)
        self.assertEqual(row['state'], 'published')
        self.assertEqual(row['tracking']['deletion']['source'], 'authenticated_owner_get_404')
        self.assertEqual(self.deleted, [71])

    def test_delete_reconcile_does_not_infer_success_from_generic_errors_or_existing_post(self):
        draft_id = self.draft('delete_post', target=71)
        self.social.timeout = 'delete_post'
        with self.assertRaises(AgentError):
            self.publish(draft_id, confirm=71)
        for outcome in (KeyError('missing'), TimeoutError(), APIError('GET', 'https://social.legitreal.com/api/posts/71', 403, '{}'),
                        APIError('GET', 'https://social.legitreal.com/api/posts/72', 404, '{}'),
                        APIError('DELETE', 'https://social.legitreal.com/api/posts/71', 404, '{}')):
            self.account.get_post = lambda post_id, error=outcome: (_ for _ in ()).throw(error)
            with self.subTest(outcome=type(outcome).__name__), self.assertRaises(AgentError):
                self.controller.reconcile(draft_id)
            self.assertEqual(self.row(draft_id)['state'], 'uncertain')
        self.account.get_post = lambda post_id: {'post_id': post_id, 'author_id': 10}
        with self.assertRaisesRegex(AgentError, '仍然存在'):
            self.controller.reconcile(draft_id)
        self.assertEqual(self.deleted, [71])

    def test_local_tracking_failure_after_remote_delete_stays_uncertain_until_readonly_confirmation(self):
        draft_id = self.draft('delete_post', target=71)
        with patch('ctn_data.agent_tracking.record_success', side_effect=RuntimeError('local persistence unavailable')):
            with self.assertRaises(AgentError):
                self.publish(draft_id, confirm=71)
        data = self.controller.store.read()
        self.assertEqual(data['drafts'][draft_id]['state'], 'uncertain')
        self.assertEqual(data['rate'].get('remote_deleted_posts', {}), {})
        self.controller.reconcile(draft_id)
        self.assertEqual(self.row(draft_id)['state'], 'published')
        self.assertEqual(self.deleted, [71])

    def test_delete_reconcile_rechecks_authenticated_identity_and_original_owner_evidence(self):
        draft_id = self.draft('delete_post', target=71)
        self.social.timeout = 'delete_post'
        with self.assertRaises(AgentError):
            self.publish(draft_id, confirm=71)
        self.account.me = lambda: {'account': {'account_id': 999}}
        with self.assertRaisesRegex(AgentError, '认证账号'):
            self.controller.reconcile(draft_id)
        self.account.me = lambda: {'account': {'account_id': 10}}
        self.controller.store.change(lambda data, conn: data['drafts'][draft_id]['execution_target'].update(author_id=999))
        with self.assertRaisesRegex(AgentError, '本人作者证据'):
            self.controller.reconcile(draft_id)
        self.assertEqual(self.row(draft_id)['state'], 'uncertain')
        self.assertEqual(self.deleted, [71])

    def test_delete_cannot_be_autonomous_even_if_allowed_actions_are_corrupted(self):
        draft_id = self.draft('delete_post', target=71, markers={'origin': 'model', 'manual_only': False, 'manual_prepared': False})
        self.controller.store.change(lambda data, conn: data['agents'][self.agent_id].update(allowed_actions=['delete_post']))
        with self.assertRaisesRegex(AgentError, '明确确认'):
            self.publish(draft_id, confirm=71)
        self.assertEqual(self.deleted, [])
        self.assertEqual(LIMITS['delete_post'], 30)


if __name__ == '__main__':
    unittest.main()
