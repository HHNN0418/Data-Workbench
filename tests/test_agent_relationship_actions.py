"""Offline relationship execution: exact state, reversals and ambiguous writes."""
from datetime import datetime, timezone
import json
import unittest
import uuid
from unittest.mock import patch

from capturethenarrative._http import Page

from ctn_data import agent_relationships
from ctn_data.agent_budget import Deferred, LIMITS
from ctn_data.agent_control import AgentController, AgentError
from ctn_data.db import connect, utc_now
from tests import test_agent_actions as fixtures


class RelationshipActions(unittest.TestCase):
    def setUp(self):
        fixtures.AutonomousActions.setUp(self)
        self.now = datetime.now(timezone.utc).timestamp() + 1
        self.followed, self.relationship_writes, self.follow_pages = set(), [], []
        self.social.get_account = lambda account_id: {'account_id': account_id, 'username': 'observed_account'}
        def write(action, target):
            self.relationship_writes.append((action, target))
            if action == 'follow':
                self.followed.add(target)
            elif action == 'unfollow':
                self.followed.discard(target)
            else:
                self.account.likes.discard(target)
            if self.social.timeout == action:
                raise TimeoutError('accepted but acknowledgement lost')
        self.account.follow = lambda target: write('follow', target)
        self.account.unfollow = lambda target: write('unfollow', target)
        self.account.unlike = lambda target: write('unlike', target)
        self.account.following = lambda account_id, limit=50, cursor=None: Page(
            [{'account_id': target} for target in sorted(self.followed)])
        self.controller.store.change(lambda data, conn: data['agents'][self.agent_id].update(
            allowed_actions=['post', 'reply', 'repost', 'like', 'unlike', 'follow', 'unfollow', 'skip'],
            daily_limit=50, interval_seconds=1))

    def draft(self, action, *, target=99, sources=None, context=None):
        draft_id = str(uuid.uuid4())
        def save(data, conn):
            agent = data['agents'][self.agent_id]
            data['drafts'][draft_id] = {'id': draft_id, 'agent_id': self.agent_id,
                'account_id': agent['account_id'], 'persona_version': agent['version'],
                'body': '', 'action': action, 'target_account_id': target if action in ('follow', 'unfollow') else None,
                'target_post_id': 7 if action in ('like', 'unlike') else None,
                'reply_to': None, 'source_ids': [], 'notification_ids': [],
                'reason': 'Based on observed interaction.', 'sources': self.sources if sources is None else sources,
                'relationship_context': context or {}, 'state': 'pending', 'version': 1,
                'created_at': utc_now(), 'error': ''}
        self.controller.store.change(save)
        return draft_id

    def row(self, draft_id):
        return self.controller.store.read()['drafts'][draft_id]

    def publish(self, draft_id):
        self.controller.publish(draft_id)
        return self.row(draft_id)

    def test_follow_sdk_none_confirms_actual_relationship_without_post(self):
        row = self.publish(self.draft('follow'))
        self.assertEqual(row['state'], 'published')
        self.assertIsNone(row['post_id'])
        self.assertEqual(self.relationship_writes, [('follow', 99)])
        state = self.controller.store.read()
        relation = agent_relationships.snapshot_for(state, state['agents'][self.agent_id])['relations'][0]
        self.assertTrue(relation['present'])
        self.assertEqual(relation['target_account_id'], 99)
        self.assertIn('follow', self.buckets)
        conn = connect(self.controller.db_path)
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM counter_snapshots').fetchone()[0], 0)
        finally:
            conn.close()

    def test_unfollow_and_unlike_sdk_none_are_successful(self):
        self.followed.add(99)
        self.assertEqual(self.publish(self.draft('unfollow'))['state'], 'published')
        self.now += 10
        self.account.likes.add(7)
        row = self.publish(self.draft('unlike'))
        self.assertEqual(row['state'], 'published')
        self.assertIsNone(row['post_id'])
        self.assertEqual(self.relationship_writes, [('unfollow', 99), ('unlike', 7)])
        self.assertIn('unlike', self.buckets)
        self.assertIn('unfollow', self.buckets)

    def test_accepted_follow_timeout_is_uncertain_and_reconciled_without_resend(self):
        draft_id = self.draft('follow')
        self.social.timeout = 'follow'
        with self.assertRaises(AgentError):
            self.controller.publish(draft_id)
        self.assertEqual(self.row(draft_id)['state'], 'uncertain')
        data = self.controller.store.read()
        self.assertIsNone(agent_relationships.snapshot_for(data, data['agents'][self.agent_id])['relations'][0]['present'])
        with self.assertRaises(AgentError):
            self.controller.publish(draft_id)
        self.controller.reconcile(draft_id)
        self.assertEqual(self.row(draft_id)['state'], 'published')
        self.assertEqual(self.relationship_writes, [('follow', 99)])

    def test_accepted_unfollow_and_unlike_reconcile_absence_without_resend(self):
        for action in ('unfollow', 'unlike'):
            with self.subTest(action=action):
                self.now += 10
                self.followed.add(99)
                self.account.likes.add(7)
                self.social.timeout = action
                draft_id = self.draft(action)
                with self.assertRaises(AgentError):
                    self.controller.publish(draft_id)
                self.controller.reconcile(draft_id)
                self.assertEqual(self.row(draft_id)['state'], 'published')
                self.assertEqual(sum(row[0] == action for row in self.relationship_writes), 1)

    def test_incomplete_following_pagination_never_claims_absence(self):
        calls = []
        def pages(account_id, limit=50, cursor=None):
            calls.append(cursor)
            if cursor is None:
                return Page([{'account_id': 88}], 'more')
            raise Deferred('read', 5)
        self.account.following = pages
        draft_id = self.draft('unfollow')
        with self.assertRaises(AgentError):
            self.controller.publish(draft_id)
        self.assertEqual(calls, [None, 'more'])
        self.assertEqual(self.row(draft_id)['state'], 'pending')
        self.assertEqual(self.relationship_writes, [])
        data = self.controller.store.read()
        self.assertIsNone(agent_relationships.snapshot_for(data, data['agents'][self.agent_id])['relations'][0]['present'])

    def test_positive_following_match_does_not_require_unrelated_remaining_pages(self):
        calls = []
        def pages(account_id, limit=50, cursor=None):
            calls.append((account_id, limit, cursor))
            return Page([{'account_id': 99}], 'more') if cursor is None else Page([])
        self.account.following = pages
        row = self.publish(self.draft('follow'))
        self.assertTrue(row['result']['already_applied'])
        self.assertEqual(calls, [(10, 50, None)])
        self.assertEqual(self.relationship_writes, [])

    def test_pagination_cycle_or_malformed_page_prevents_write(self):
        for output in (Page([], 'same'), Page([{'username': 'missing_id'}])):
            with self.subTest(output=output):
                self.account.following = lambda *args, **kwargs: output
                with self.assertRaises(AgentError):
                    self.controller.publish(self.draft('follow'))
        self.assertEqual(self.relationship_writes, [])

    def test_follow_unfollow_follow_has_no_local_reversal_delay(self):
        self.publish(self.draft('follow'))
        reverse = self.draft('unfollow')
        self.now += 5
        self.publish(reverse)
        self.now += 1
        self.publish(self.draft('follow'))
        self.assertEqual(self.relationship_writes, [('follow', 99), ('unfollow', 99), ('follow', 99)])

    def test_like_unlike_like_has_no_local_reversal_delay(self):
        self.publish(self.draft('like'))
        reverse = self.draft('unlike')
        self.now += 2
        self.publish(reverse)
        self.now += 1
        self.publish(self.draft('like'))
        self.assertEqual(len(self.social.like_calls), 2)
        self.assertEqual(self.relationship_writes, [('unlike', 7)])

    def test_memory_reset_and_restart_preserve_facts_without_a_reversal_delay(self):
        self.publish(self.draft('follow'))
        self.controller.store.change(lambda data, conn: data['memory'].pop(self.agent_id, None))
        self.controller = AgentController(self.controller.db_path, self.controller.secret_dir.parent,
            provider_store=self.controller.providers, sdk_factory=self.social.factory, clock=lambda: self.now)
        self.now += 5
        self.publish(self.draft('unfollow'))
        data = self.controller.store.read()
        self.assertFalse(agent_relationships.snapshot_for(data, data['agents'][self.agent_id])['relations'][0]['present'])
        self.assertEqual(self.relationship_writes, [('follow', 99), ('unfollow', 99)])

    def test_cancel_before_publish_prevents_write_after_preflight(self):
        draft_id = self.draft('follow')
        with patch.object(self.controller.generations, 'before_publish', side_effect=AgentError('任务已取消', 409)):
            with self.assertRaisesRegex(AgentError, '取消'):
                self.controller.publish(draft_id)
        self.assertEqual(self.row(draft_id)['state'], 'pending')
        self.assertEqual(self.relationship_writes, [])

    def test_budget_deferral_refunds_daily_slot_and_keeps_pending(self):
        draft_id = self.draft('follow')
        previous = self.call.side_effect
        def call(controller, bucket, method, *args, **kwargs):
            if bucket == 'follow':
                raise Deferred(bucket, 3)
            return previous(controller, bucket, method, *args, **kwargs)
        self.call.side_effect = call
        with self.assertRaises(AgentError):
            self.controller.publish(draft_id)
        data = self.controller.store.read()
        self.assertEqual(self.row(draft_id)['state'], 'pending')
        self.assertEqual(data['rate']['accounts'][self.agent_id].get('attempts', 0), 0)
        self.assertEqual(self.relationship_writes, [])

    def test_allowlist_self_target_and_original_identity_are_enforced(self):
        for target in (10, 555):
            with self.subTest(target=target), self.assertRaises(AgentError):
                self.controller.publish(self.draft('follow', target=target))
        draft_id = self.draft('follow')
        self.social.timeout = 'follow'
        with self.assertRaises(AgentError):
            self.controller.publish(draft_id)
        self.controller.store.change(lambda data, conn: data['agents'][self.agent_id].update(account_id=999))
        with self.assertRaisesRegex(AgentError, '原动作账号'):
            self.controller.reconcile(draft_id)
        self.assertEqual(self.relationship_writes, [('follow', 99)])

    def test_known_relationship_target_can_be_unfollowed_or_unliked_without_old_source(self):
        context = {'relations': [{'kind': 'follow', 'target_account_id': 99, 'present': True},
                                 {'kind': 'like', 'target_post_id': 7, 'present': True}]}
        self.followed.add(99)
        self.publish(self.draft('unfollow', sources=[], context=context))
        self.now += 10
        self.account.likes.add(7)
        self.publish(self.draft('unlike', sources=[], context=context))
        self.assertEqual(self.relationship_writes, [('unfollow', 99), ('unlike', 7)])

    def test_legacy_like_can_be_canceled_after_memory_clear_without_original_source(self):
        self.publish(self.draft('like'))
        def old_storage(data, conn):
            data['rate'].pop('relationships', None)
            data['memory'].pop(self.agent_id, None)
        self.controller.store.change(old_storage)
        self.now += agent_relationships.COOLDOWN_SECONDS + 10
        data = self.controller.store.read()
        context = agent_relationships.context_for(data, data['agents'][self.agent_id], [])
        row = self.publish(self.draft('unlike', sources=[], context=context))
        self.assertEqual(row['state'], 'published')
        self.assertEqual(self.relationship_writes, [('unlike', 7)])

    def test_relationship_routes_have_separate_official_allowances(self):
        for action in ('like', 'follow', 'unfollow', 'unlike'):
            self.assertEqual(LIMITS[action], 30)

    def test_tracking_current_like_checkpoint_changes_and_old_ack_cannot_revert_it(self):
        from ctn_data.agent_tracking import record_success
        first = self.draft('like')
        self.publish(first)
        self.now += agent_relationships.COOLDOWN_SECONDS + 10
        self.publish(self.draft('unlike'))
        def old_ack(data, conn):
            record_success(data, conn, data['drafts'][first], data['agents'][self.agent_id], None, utc_now())
        self.controller.store.change(old_ack)
        data = self.controller.store.read()
        relation = agent_relationships.snapshot_for(data, data['agents'][self.agent_id])['relations'][0]
        self.assertFalse(relation['present'])
        conn = connect(self.controller.db_path)
        try:
            row = conn.execute("SELECT value FROM checkpoints WHERE key='agent_tracking:like:10:7'").fetchone()
            self.assertFalse(json.loads(row['value'])['present'])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM counter_snapshots').fetchone()[0], 0)
        finally:
            conn.close()

    def test_malformed_my_likes_response_is_unknown_and_cannot_trigger_unlike(self):
        self.account.my_likes = lambda ids: {'unexpected': 'format'}
        draft_id = self.draft('unlike')
        with self.assertRaisesRegex(AgentError, '点赞关系返回无效'):
            self.controller.publish(draft_id)
        self.assertEqual(self.row(draft_id)['state'], 'pending')
        self.assertEqual(self.relationship_writes, [])

    def test_tracking_failure_after_follow_ack_stays_uncertain_without_replay(self):
        draft_id = self.draft('follow')
        with patch('ctn_data.agent_tracking.record_success', side_effect=RuntimeError('local store error')):
            with self.assertRaises(AgentError):
                self.controller.publish(draft_id)
        self.assertEqual(self.row(draft_id)['state'], 'uncertain')
        with self.assertRaises(AgentError):
            self.controller.publish(draft_id)
        self.controller.reconcile(draft_id)
        self.assertEqual(self.row(draft_id)['state'], 'published')
        self.assertEqual(self.relationship_writes, [('follow', 99)])


if __name__ == '__main__':
    unittest.main()
