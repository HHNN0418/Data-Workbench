"""Managed identity isolation, durable refusals and final dispatch races."""
from datetime import datetime
import sqlite3
import threading
import unittest
import uuid
from unittest.mock import patch
from zoneinfo import ZoneInfo

from ctn_data import agent_budget, agent_interaction_policy as policy, agent_relationships
from ctn_data.agent_control import AgentError
from ctn_data.db import utc_now
from tests import test_agent_relationship_actions as fixtures


REAL_BUDGET_CALL = agent_budget.call


class PolicyIdentityTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.execute('CREATE TABLE accounts(account_id INTEGER PRIMARY KEY, username TEXT)')
        self.conn.execute('CREATE TABLE posts(post_id INTEGER PRIMARY KEY, author_id INTEGER)')
        self.agent = {'id': 'self', 'account_id': 10, 'username': 'current_self'}
        self.peer = {'id': 'peer', 'account_id': 20, 'username': 'paused_peer', 'enabled': False}
        self.data = {'settings': {}, 'rate': {}, 'agents': {'self': self.agent, 'peer': self.peer}}

    def test_all_bindings_and_historical_accounts_are_protected_without_including_human_teammates(self):
        policy.remember_account(self.data, self.peer, {'id': 'peer', 'account_id': 19, 'username': 'previous_peer'})
        self.conn.executemany('INSERT INTO accounts VALUES(?,?)', [(20, 'renamed_peer'), (99, 'human_teammate')])
        self.assertEqual(policy.blocked_account_ids(None, self.data, self.agent), {19, 20})
        self.assertEqual(policy.blocked_usernames(self.conn, self.data, self.agent),
                         {'paused_peer', 'previous_peer', 'renamed_peer'})
        del self.data['agents']['peer']
        self.assertEqual(policy.blocked_account_ids(None, self.data, self.agent), {19, 20})

    def test_history_keeps_aliases_on_rename_and_excludes_self_identity(self):
        policy.remember_account(self.data, {**self.peer, 'username': 'renamed_peer'}, self.peer)
        policy.remember_account(self.data, self.agent)
        self.assertEqual(policy.blocked_account_ids(None, self.data, self.agent), {20})
        self.assertEqual(policy.blocked_usernames(None, self.data, self.agent), {'paused_peer', 'renamed_peer'})

    def test_mentions_match_whole_case_insensitive_handles_and_historical_aliases(self):
        policy.remember_account(self.data, self.peer, {**self.peer, 'username': 'old_peer'})
        self.conn.execute('INSERT INTO accounts VALUES(20,?)', ('new_peer',))
        for body in ('Hello @PAUSED_PEER!', 'Hello ＠old_peer', 'Hello @new_peer.'):
            with self.subTest(body=body), self.assertRaises(policy.PolicyBlocked):
                policy.check_body(self.conn, self.data, self.agent, body)
        for body in ('Contact somebody@paused_peer.com', '@paused_peer_extra', '@current_self', '@human_teammate'):
            with self.subTest(body=body):
                policy.check_body(self.conn, self.data, self.agent, body)

    def test_positive_interactions_and_automatic_reversals_are_blocked_but_manual_cleanup_is_allowed(self):
        for action in ('reply', 'repost', 'like', 'unlike', 'follow', 'unfollow'):
            draft = {'action': action, 'target_post_id': 7, 'target_account_id': 20,
                     'sources': [{'post_id': 7, 'author_id': 20}]}
            with self.subTest(action=action), self.assertRaises(policy.PolicyBlocked):
                policy.check(self.conn, self.data, self.agent, draft)
            if action in ('unlike', 'unfollow'):
                policy.check(self.conn, self.data, self.agent, draft, manual=True)
            else:
                with self.subTest(manual=action), self.assertRaises(policy.PolicyBlocked):
                    policy.check(self.conn, self.data, self.agent, draft, manual=True)

    def test_remote_author_is_required_and_overrides_stale_local_evidence(self):
        draft = {'action': 'like', 'target_post_id': 7, 'sources': [{'post_id': 7, 'author_id': 99}]}
        with self.assertRaises(policy.PolicyBlocked) as missing:
            policy.check(self.conn, self.data, self.agent, draft, target_result={'post_id': 7}, require_author=True)
        self.assertEqual(missing.exception.policy_reason, 'unknown_target_author')
        with self.assertRaises(policy.PolicyBlocked):
            policy.check(self.conn, self.data, self.agent, draft, require_author=True)
        with self.assertRaises(policy.PolicyBlocked):
            policy.check(self.conn, self.data, self.agent, draft,
                         target_result={'post_id': 7, 'author_id': 20}, require_author=True)
        policy.check(self.conn, self.data, self.agent, draft,
                     target_result={'post_id': 7, 'author_id': 99}, require_author=True)

    def test_local_author_fallback_and_switch_disable(self):
        self.conn.execute('INSERT INTO posts VALUES(7,20)')
        draft = {'action': 'reply', 'target_post_id': 7, 'body': '@paused_peer hello'}
        with self.assertRaises(policy.PolicyBlocked):
            policy.check(self.conn, self.data, self.agent, draft)
        self.data['settings']['block_agent_interactions'] = False
        self.assertEqual(policy.blocked_account_ids(None, self.data, self.agent), set())
        policy.check(self.conn, self.data, self.agent, draft, target_result={}, require_author=True)

    def test_self_reply_remains_allowed(self):
        policy.check(self.conn, self.data, self.agent,
                     {'action': 'reply', 'body': '@current_self more context', 'target_post_id': 7},
                     target_result={'post_id': 7, 'author_id': 10}, require_author=True)


class PolicyExecutionTests(unittest.TestCase):
    def setUp(self):
        fixtures.RelationshipActions.setUp(self)
        self.account.get_post = self.social.get_post
        self.account.get_account = self.social.get_account

    def peer(self, account_id=99):
        self.controller.save({'name': 'Paused peer', 'username': 'paused_peer', 'enabled': False})
        peer_id = next(row['id'] for row in self.controller.snapshot()['agents'] if row['username'] == 'paused_peer')
        with self.controller._interaction_dispatch_lock:
            self.controller.store.change(lambda data, conn: data['agents'][peer_id].update(
                account_id=account_id, account_state='ready'))
        return peer_id

    def draft(self, action='like', *, manual=False, body='', source_author=99):
        draft_id = str(uuid.uuid4())
        def save(data, conn):
            agent = data['agents'][self.agent_id]
            data['drafts'][draft_id] = {
                'id': draft_id, 'agent_id': self.agent_id, 'account_id': agent['account_id'],
                'persona_version': agent['version'], 'action': action, 'body': body,
                'target_post_id': 7 if action in ('reply', 'repost', 'like', 'unlike') else None,
                'target_account_id': 99 if action in ('follow', 'unfollow') else None,
                'sources': [{'id': 'post:7', 'kind': 'post', 'post_id': 7, 'author_id': source_author}],
                'source_ids': ['post:7'], 'notification_ids': [], 'relationship_context': {},
                'reason': 'Existing proposed action', 'created_at': utc_now(), 'version': 1,
                'state': 'pending', 'post_id': None, 'error': '',
                **({'origin': 'manual', 'manual_only': True, 'manual_prepared': True,
                    'user_instruction': 'manual_operator_request'} if manual else {})}
        self.controller.store.change(save)
        return draft_id

    def row(self, draft_id):
        return self.controller.store.read()['drafts'][draft_id]

    def test_managed_interaction_refusal_is_durable_and_never_becomes_uncertain(self):
        self.peer()
        draft_id = self.draft()
        with self.assertRaises(policy.PolicyBlocked):
            self.controller.publish(draft_id)
        row = self.row(draft_id)
        self.assertEqual(row['state'], 'skipped')
        self.assertTrue(row['policy_blocked'])
        self.assertNotIn('reservation', row)
        self.assertEqual(self.social.like_calls, [])
        with self.assertRaises(AgentError):
            self.controller.publish(draft_id)
        day = datetime.fromtimestamp(self.now, ZoneInfo('Australia/Sydney')).date().isoformat()
        self.assertEqual(self.controller.store.read()['rate']['interaction_policy_stats'][day]['blocked'], 1)

    def test_manual_cleanup_is_allowed_while_new_manual_interactions_are_blocked(self):
        self.peer()
        self.account.likes.add(7)
        self.followed.add(99)
        for action in ('unlike', 'unfollow'):
            self.controller.publish(self.draft(action, manual=True))
        self.assertEqual(self.relationship_writes, [('unlike', 7), ('unfollow', 99)])
        for action in ('like', 'follow'):
            with self.subTest(action=action), self.assertRaises(policy.PolicyBlocked):
                self.controller.publish(self.draft(action, manual=True))
        self.assertEqual(self.social.like_calls, [])

    def test_editing_a_draft_body_cannot_introduce_an_internal_mention(self):
        self.peer()
        draft_id = self.draft('post', body='An independent post.')
        with self.assertRaises(policy.PolicyBlocked):
            self.controller.publish(draft_id, body='Hello @paused_peer')
        self.assertTrue(self.row(draft_id)['policy_blocked'])
        self.assertEqual(self.social.writes, [])

    def test_remote_author_check_rejects_a_target_missing_author_evidence(self):
        draft_id = self.draft(source_author=98)
        self.social.posts[7].pop('author_id')
        with self.assertRaises(policy.PolicyBlocked):
            self.controller.publish(draft_id)
        self.assertEqual(self.row(draft_id)['policy_code'], 'unknown_target_author')
        self.assertEqual(self.social.like_calls, [])

    def test_switch_enabled_during_budget_wait_refunds_tokens_and_delivery_reservation(self):
        self.controller.settings({'block_agent_interactions': False})
        self.peer()
        draft_id = self.draft()
        self.call.side_effect = REAL_BUDGET_CALL
        real_reserve = agent_budget.reserve
        delayed = []
        def reserve(controller, bucket, **kwargs):
            if bucket == 'like' and not delayed:
                delayed.append(True)
                raise agent_budget.Deferred(bucket, .05)
            return real_reserve(controller, bucket, **kwargs)
        def awaken(delay):
            self.now += delay
            self.controller.settings({'block_agent_interactions': True})
        self.controller.store.change(lambda data, conn: data['rate'].update(quota_policy={'limits': {'like': 3}}))
        with patch('ctn_data.agent_budget.reserve', side_effect=reserve), patch('ctn_data.agent_budget.time.sleep', side_effect=awaken):
            with self.assertRaises(policy.PolicyBlocked):
                self.controller.publish(draft_id)
        data = self.controller.store.read()
        row = data['drafts'][draft_id]
        self.assertEqual(row['state'], 'skipped')
        self.assertNotIn('reservation', row)
        self.assertNotIn('publisher', data['leases'])
        self.assertEqual(data['rate'].get('hashes', {}), {})
        self.assertNotIn(self.agent_id, data['rate'].get('accounts', {}))
        self.assertEqual(data['rate']['buckets']['like']['tokens'], agent_budget.LIMITS['like'])
        self.assertEqual(data['rate']['quota_buckets']['like']['tokens'], 3)
        for usage in data['rate'].get('quota_usage_totals', {}).values():
            self.assertNotIn('like', usage)
        self.assertFalse(agent_relationships.snapshot_for(data, data['agents'][self.agent_id])['relations'][0]['present'])
        self.assertEqual(self.social.like_calls, [])

    def test_new_peer_binding_during_budget_wait_is_checked_at_dispatch(self):
        draft_id = self.draft()
        def call(controller, bucket, method, *args, **kwargs):
            if bucket == 'like':
                self.peer()
            return method(*args, **kwargs)
        self.call.side_effect = call
        with self.assertRaises(policy.PolicyBlocked):
            self.controller.publish(draft_id)
        self.assertTrue(self.row(draft_id)['policy_blocked'])
        self.assertEqual(self.social.like_calls, [])

    def test_policy_activation_waits_for_a_request_that_has_already_started(self):
        self.controller.settings({'block_agent_interactions': False})
        self.peer()
        draft_id = self.draft()
        entered, release, changed = threading.Event(), threading.Event(), threading.Event()
        failures = []
        original_like = self.account.like
        def like(post_id):
            entered.set()
            if not release.wait(5):
                raise AssertionError('test request did not get released')
            return original_like(post_id)
        self.account.like = like
        def publish():
            try:
                self.controller.publish(draft_id)
            except Exception as exc:
                failures.append(exc)
        def activate():
            try:
                self.controller.settings({'block_agent_interactions': True})
                changed.set()
            except Exception as exc:
                failures.append(exc)
        publisher = threading.Thread(target=publish)
        updater = threading.Thread(target=activate)
        publisher.start()
        try:
            self.assertTrue(entered.wait(3))
            updater.start()
            self.assertFalse(changed.wait(.05))
        finally:
            release.set()
            publisher.join(5)
            if updater.ident is not None:
                updater.join(5)
        self.assertFalse(failures)
        self.assertTrue(changed.is_set())
        self.assertEqual(self.social.like_calls, [(10, 7)])

    def test_uncertain_internal_action_can_be_reconciled_after_enabling_policy(self):
        self.controller.settings({'block_agent_interactions': False})
        self.peer()
        draft_id = self.draft()
        self.social.timeout = 'like'
        with self.assertRaises(AgentError):
            self.controller.publish(draft_id)
        self.assertEqual(self.row(draft_id)['state'], 'uncertain')
        self.controller.settings({'block_agent_interactions': True})
        self.controller.reconcile(draft_id)
        self.assertEqual(self.row(draft_id)['state'], 'published')
        self.assertEqual(self.social.like_calls, [(10, 7)])
