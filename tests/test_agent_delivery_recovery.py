"""A role lease revoked after reservation cannot authorize a later SDK write."""
import copy
import unittest
from unittest.mock import patch

from ctn_data import agent_actions, agent_budget, agent_relationships
from ctn_data.agent_control import AgentError
from tests import test_agent_relationship_actions as fixtures


REAL_BUDGET_CALL = agent_budget.call


class DeliveryRecoveryTests(unittest.TestCase):
    setUp = fixtures.RelationshipActions.setUp
    draft = fixtures.RelationshipActions.draft
    row = fixtures.RelationshipActions.row

    def expire_generation(self, data, draft_id):
        lease = data['leases']['agent:' + self.agent_id]
        lease['generation_job_id'] = 'expired-during-budget'
        data['agents'][self.agent_id]['generation'] = {
            'id': 'expired-during-budget', 'owner': lease['owner'],
            'lease_token': lease['token'], 'status': 'running',
            'heartbeat_until': self.now - 1, 'stage': 'headers'}
        data['drafts'][draft_id]['generation_job_id'] = 'expired-during-budget'
        self.controller.generations.recover(data)

    def after_write_quota(self, callback):
        self.call.side_effect = REAL_BUDGET_CALL
        original_reserve = agent_budget.reserve
        used = []
        def reserve(controller, bucket, **kwargs):
            result = original_reserve(controller, bucket, **kwargs)
            if bucket == 'like' and not used:
                used.append(True)
                callback()
            return result
        return patch('ctn_data.agent_budget.reserve', side_effect=reserve)

    def assert_no_write_or_charge(self, draft_id):
        data = self.controller.store.read()
        row = data['drafts'][draft_id]
        self.assertEqual(row['state'], 'rejected')
        self.assertTrue(row['requires_review'])
        self.assertTrue(row['delivery_not_started'])
        self.assertFalse(row.get('policy_blocked', False))
        self.assertNotIn('reservation', row)
        self.assertEqual(data['rate'].get('hashes', {}), {})
        self.assertNotIn(self.agent_id, data['rate'].get('accounts', {}))
        self.assertEqual(data['rate']['buckets']['like']['tokens'], agent_budget.LIMITS['like'])
        for usage in data['rate'].get('quota_usage_totals', {}).values():
            self.assertNotIn('like', usage)
        self.assertFalse(agent_relationships.snapshot_for(data, data['agents'][self.agent_id])['relations'][0]['present'])
        self.assertEqual(self.social.like_calls, [])
        return data

    def test_recovery_after_reserve_refunds_and_requires_review_then_manual_execution_can_succeed(self):
        draft_id = self.draft('like')
        self.controller.store.change(lambda data, conn: data['rate'].update(quota_policy={'limits': {'like': 3}}))
        def expire():
            self.controller.store.change(lambda data, conn: self.expire_generation(data, draft_id))
        with self.after_write_quota(expire):
            with self.assertRaises(agent_actions.DeliveryNotStarted) as caught:
                self.controller.publish(draft_id)
        self.assertEqual(caught.exception.delivery_reason, 'role_lease_lost')
        data = self.assert_no_write_or_charge(draft_id)
        self.assertEqual(data['rate']['quota_buckets']['like']['tokens'], 3)
        self.assertNotIn('publisher', data['leases'])
        self.assertNotIn('agent:' + self.agent_id, data['leases'])
        self.assertEqual(data['agents'][self.agent_id]['generation']['status'], 'failed')
        self.controller.publish(draft_id)
        row = self.row(draft_id)
        self.assertEqual(row['state'], 'published')
        self.assertFalse(row.get('requires_review', False))
        self.assertFalse(row.get('delivery_not_started', False))
        self.assertEqual(self.social.like_calls, [(10, 7)])

    def test_recovery_does_not_remove_another_roles_replacement_lease(self):
        draft_id = self.draft('like')
        replacement = {'owner': 'replacement-owner', 'token': 'replacement-token',
                       'until': self.now + 300, 'generation_job_id': 'replacement-job'}
        def replace():
            def changed(data, conn):
                self.expire_generation(data, draft_id)
                data['leases']['agent:' + self.agent_id] = copy.deepcopy(replacement)
            self.controller.store.change(changed)
        with self.after_write_quota(replace):
            with self.assertRaises(agent_actions.DeliveryNotStarted):
                self.controller.publish(draft_id)
        data = self.assert_no_write_or_charge(draft_id)
        self.assertEqual(data['leases']['agent:' + self.agent_id], replacement)

    def test_final_generation_authorization_is_checked_even_when_role_lease_is_still_valid(self):
        draft_id = self.draft('like')
        self.controller.settings({'enabled': True})
        self.controller.store.change(lambda data, conn: data['agents'][self.agent_id].update(enabled=True, mode='auto'))
        def revoke_authorization():
            def changed(data, conn):
                data['drafts'][draft_id].update(requires_review=True, generation_job_id='cancelled-job')
                data['agents'][self.agent_id]['generation'] = {
                    'id': 'cancelled-job', 'owner': self.controller.owner, 'status': 'cancelled', 'heartbeat_until': 0}
            self.controller.store.change(changed)
        with self.after_write_quota(revoke_authorization):
            with self.controller._claim(self.agent_id) as token:
                with self.assertRaises(agent_actions.DeliveryNotStarted) as caught:
                    agent_actions.execute(self.controller, draft_id, None, token, automatic=True)
        self.assertEqual(caught.exception.delivery_reason, 'generation_authorization_changed')
        self.assert_no_write_or_charge(draft_id)

    def test_different_send_token_and_existing_uncertain_action_are_not_rewritten(self):
        for changed_state in ('sending', 'uncertain'):
            with self.subTest(state=changed_state):
                draft_id = self.draft('like')
                expected = {}
                def replace_delivery():
                    def changed(data, conn):
                        data['drafts'][draft_id].update(state=changed_state, send_token='replacement-send-token')
                        data['leases']['publisher'] = {'owner': 'replacement-owner',
                            'token': 'replacement-send-token', 'until': self.now + 300}
                        expected['draft'] = copy.deepcopy(data['drafts'][draft_id])
                        expected['publisher'] = copy.deepcopy(data['leases']['publisher'])
                    self.controller.store.change(changed)
                with self.after_write_quota(replace_delivery):
                    with self.assertRaises(agent_actions.DeliveryNotStarted):
                        self.controller.publish(draft_id)
                data = self.controller.store.read()
                self.assertEqual(data['drafts'][draft_id], expected['draft'])
                self.assertEqual(data['leases']['publisher'], expected['publisher'])
                self.assertEqual(self.social.like_calls, [])
                # Independent cases must not inherit the deliberately retained
                # unresolved action and replacement publisher from the prior one.
                self.controller.store.change(lambda state, conn: (
                    state['drafts'].pop(draft_id), state['leases'].pop('publisher'),
                    state['rate'].update(hashes={}, relationships={}),
                    state['rate'].get('accounts', {}).pop(self.agent_id, None)))

    def test_a_write_already_accepted_before_recovery_remains_uncertain_and_only_reconciles(self):
        draft_id = self.draft('like')
        original_like = self.account.like
        def accepted_then_recovered(post_id):
            original_like(post_id)
            self.controller.store.change(lambda data, conn: self.expire_generation(data, draft_id))
        self.account.like = accepted_then_recovered
        with self.assertRaises(AgentError):
            self.controller.publish(draft_id)
        row = self.row(draft_id)
        self.assertEqual(row['state'], 'uncertain')
        self.assertFalse(row.get('delivery_not_started', False))
        self.assertEqual(self.social.like_calls, [(10, 7)])
        self.controller.reconcile(draft_id)
        self.assertEqual(self.row(draft_id)['state'], 'published')
        self.assertEqual(self.social.like_calls, [(10, 7)])


if __name__ == '__main__':
    unittest.main()
