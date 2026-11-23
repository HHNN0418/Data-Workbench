"""Optional team caps and quota management, with isolated DBs and fake SDK calls."""
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from ctn_data import agent_budget as budget, coordination
from ctn_data.agent_control import AgentController, AgentError
from ctn_data.db import connect
from ctn_data.server import create_app
from tests.auth_helpers import login


class TeamQuotaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.now = 1792450000.0
        self.controller = AgentController(self.folder / 'quotas.db', self.folder / 'secrets',
                                          clock=lambda: self.now)
        self.other = AgentController(self.controller.db_path, self.folder / 'secrets', clock=lambda: self.now)

    def row(self, bucket):
        return next(row for row in self.controller.quotas()['buckets'] if row['bucket'] == bucket)

    def configure(self, bucket, cap):
        return self.controller.quotas({'bucket': bucket, 'cap': cap})

    def test_default_view_lists_unused_routes_without_changing_state(self):
        before = self.controller.store.read()
        status = self.controller.quotas()
        self.assertEqual({row['bucket'] for row in status['buckets']}, set(budget.LIMITS))
        self.assertEqual(status['policy']['limits'], {})
        self.assertEqual(status['allocation'], 'shared_on_demand')
        self.assertTrue(all(row['configured_cap'] is None for row in status['buckets']))
        self.assertEqual(self.controller.store.read(), before)
        self.assertEqual(self.row('post')['actions'], ['post', 'reply', 'repost'])

    def test_custom_cap_is_shared_by_all_controllers_but_other_routes_stay_available(self):
        self.configure('post', 1)
        budget.reserve(self.controller, 'post')
        with self.assertRaises(budget.Deferred) as denied:
            budget.reserve(self.other, 'post')
        self.assertTrue(denied.exception.quota_policy)
        self.assertEqual(denied.exception.retry_after, 60)
        budget.reserve(self.other, 'like')
        budget.reserve(self.other, 'follow')

    def test_cap_above_official_limit_never_grants_more_than_official(self):
        self.configure('post', 100)
        self.assertEqual(self.row('post')['configured_cap'], 100)
        self.assertEqual(self.row('post')['effective_limit'], 3)
        for _ in range(3):
            budget.reserve(self.controller, 'post')
        with self.assertRaises(budget.Deferred):
            budget.reserve(self.other, 'post')

    def test_latest_server_rate_is_a_stricter_bound_than_custom_cap(self):
        self.configure('like', 10)
        self.controller.store.change(lambda data, conn: data['rate'].setdefault('buckets', {}).update({
            'like': {'limit': 2, 'remaining': 2, 'tokens': 2, 'token_at': self.now,
                     'refill_period': 60, 'observed_at': '2026-10-20T00:00:00Z'}}))
        self.assertEqual(self.row('like')['server_limit'], 2)
        self.assertEqual(self.row('like')['effective_limit'], 2)
        for _ in range(2):
            budget.reserve(self.controller, 'like')
        with self.assertRaises(budget.Deferred):
            budget.reserve(self.other, 'like')

    def test_changing_or_toggling_caps_cannot_mint_tokens(self):
        self.configure('like', 2)
        for _ in range(2):
            budget.reserve(self.controller, 'like')
        self.configure('like', 5)
        with self.assertRaises(budget.Deferred) as denied:
            budget.reserve(self.other, 'like')
        self.assertEqual(denied.exception.retry_after, 12)
        self.configure('like', None)
        self.configure('like', 5)
        with self.assertRaises(budget.Deferred):
            budget.reserve(self.controller, 'like')
        self.now += 12
        budget.reserve(self.controller, 'like')

    def test_policy_change_settles_elapsed_time_at_old_rate_before_clamping(self):
        self.configure('like', 5)
        for _ in range(5):
            budget.reserve(self.controller, 'like')
        self.now += 6
        self.configure('like', 1)
        self.assertAlmostEqual(self.row('like')['retry_after'], 30)
        self.now += 30
        budget.reserve(self.other, 'like')

    def test_config_changes_preserve_settings_server_balances_leases_and_other_caps(self):
        self.configure('follow', 2)
        def seed(data, conn):
            data['settings'].update(enabled=True, team_side='existing-side')
            data['leases']['synthetic'] = {'token': 'unchanged', 'until': self.now + 90}
            data['rate'].setdefault('buckets', {})['post'] = {
                'tokens': .25, 'token_at': self.now, 'limit': 3, 'remaining': 0,
                'not_before': self.now + 90, 'refill_period': 60}
        self.controller.store.change(seed)
        before = self.controller.store.read()
        self.configure('post', 1)
        after = self.controller.store.read()
        for key in ('settings', 'leases', 'drafts', 'agents'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after['rate']['buckets'], before['rate']['buckets'])
        self.assertEqual(after['rate']['quota_policy']['limits'], {'follow': 2, 'post': 1})
        self.configure('post', None)
        self.assertEqual(self.controller.store.read()['rate']['buckets'], before['rate']['buckets'])
        self.assertEqual(self.row('post')['retry_after'], 90)

    def test_policy_waits_public_status_and_model_context_use_the_same_balance(self):
        self.configure('post', 1)
        budget.reserve(self.controller, 'post')
        row = self.row('post')
        public = budget.public_status(self.controller)['post']
        actions = budget.action_status(self.controller, ['post', 'reply', 'repost', 'like', 'follow'])
        self.assertEqual(public['retry_after'], row['retry_after'])
        self.assertEqual(public['estimated_remaining'], row['remaining'])
        for action in ('post', 'reply', 'repost'):
            self.assertEqual(actions[action]['retry_after'], row['retry_after'])
            self.assertEqual(actions[action]['limit'], row['effective_limit'])
            self.assertFalse(actions[action]['available'])
        self.assertTrue(actions['like']['available'])
        self.assertTrue(actions['follow']['available'])

    def test_read_policy_is_inherited_by_each_independent_official_route(self):
        self.configure('read', 1)
        budget.reserve(self.controller, 'read:get_post')
        with self.assertRaises(budget.Deferred):
            budget.reserve(self.other, 'read:get_post')
        budget.reserve(self.other, 'read:following')
        self.assertEqual(self.row('read:get_post')['configured_cap'], 1)
        self.assertEqual(self.row('read:following')['configured_cap'], 1)

    def test_untransmitted_feed_attempt_refunds_both_ledgers_and_records_no_usage(self):
        self.configure('feed', 1)
        conn = connect(self.controller.db_path)
        try:
            self.assertTrue(coordination.permit(conn, '/api/posts', 'collector', now=self.now)['allowed'])
        finally:
            conn.close()
        with self.assertRaises(budget.Deferred):
            budget.reserve(self.controller, 'feed')
        self.assertEqual(self.row('feed')['remaining'], 1)
        self.assertEqual(self.row('feed')['usage_last_minute'], 0)
        self.now += 9
        budget.reserve(self.other, 'feed')

    def test_sdk_attempt_telemetry_distinguishes_outcomes_and_does_not_count_reservations(self):
        self.controller.save({'name': 'Reader'})
        agent_id = next(iter(self.controller.store.read()['agents']))
        error = RuntimeError('synthetic refusal')
        error.status = 403
        with self.controller._claim(agent_id):
            budget.call(self.controller, 'like', lambda: None)
            with self.assertRaises(RuntimeError):
                budget.call(self.controller, 'like', Mock(side_effect=error))
            with self.assertRaises(TimeoutError):
                budget.call(self.controller, 'like', Mock(side_effect=TimeoutError('ambiguous')))
        budget.reserve(self.controller, 'like')
        status = self.controller.quotas()
        row = next(row for row in status['buckets'] if row['bucket'] == 'like')
        self.assertEqual(row['usage_last_minute'], 3)
        self.assertEqual(row['usage_outcomes'], {'attempts': 3, 'confirmed_success': 1,
                                                'definite_failure': 1, 'uncertain': 1})
        own = status['agents'][0]
        self.assertEqual(own['usage_last_minute'], 3)
        self.assertEqual(own['request_counts']['like'], 3)
        self.assertIsNone(getattr(budget._CONTEXT, 'actor', None))
        self.now += 60
        self.assertEqual(self.row('like')['usage_last_minute'], 0)
        self.assertEqual(self.controller.quotas()['agents'][0]['request_counts']['like'], 3)

    def test_local_policy_counts_actual_failed_attempts_without_consuming_success_quota(self):
        self.configure('like', 1)
        error = RuntimeError('synthetic refusal')
        error.status = 409
        with self.assertRaises(RuntimeError):
            budget.call(self.controller, 'like', Mock(side_effect=error))
        with self.assertRaises(budget.Deferred) as denied:
            budget.reserve(self.other, 'like')
        self.assertTrue(denied.exception.quota_policy)
        self.assertEqual(self.controller.store.read()['rate']['buckets']['like']['tokens'], 30)

    def test_pending_demand_distinguishes_automatic_review_manual_and_blocked(self):
        ids = {}
        for label in ('ready', 'review', 'manual', 'paused', 'unbound', 'uncertain'):
            self.controller.save({'name': label, 'mode': 'auto', 'enabled': True})
            ids[label] = next(row['id'] for row in self.controller.store.read()['agents'].values()
                              if row['name'] == label)
        def seed(data, conn):
            data['settings']['enabled'] = True
            for number, (label, agent_id) in enumerate(ids.items(), 1):
                agent = data['agents'][agent_id]
                agent.update(account_state='ready', account_id=number)
                if label == 'review':
                    agent['mode'] = 'review'
                if label == 'paused':
                    agent['enabled'] = False
                if label == 'unbound':
                    agent.update(account_state='unbound', account_id=None)
                data['drafts'][label] = {'id': label, 'agent_id': agent_id, 'state': 'pending',
                    'action': 'like', 'persona_version': agent['version'], 'account_id': agent['account_id']}
                if label == 'manual':
                    data['drafts'][label].update(origin='manual', manual_only=True)
                if label == 'uncertain':
                    data['drafts']['uncertain-write'] = {'id': 'uncertain-write', 'agent_id': agent_id,
                        'state': 'uncertain', 'action': 'post'}
        self.controller.store.change(seed)
        status = self.controller.quotas()
        row = next(row for row in status['buckets'] if row['bucket'] == 'like')
        self.assertEqual(row['pending_actions'], 6)
        self.assertEqual(row['auto_pending_actions'], 1)
        people = {row['name']: row for row in status['agents']}
        self.assertEqual(people['review']['review_pending_actions'], 1)
        self.assertEqual(people['manual']['manual_pending_actions'], 1)
        for name in ('paused', 'unbound', 'uncertain'):
            self.assertEqual(people[name]['blocked_pending_actions'], 1)

    def test_reconfiguring_a_policy_wait_preserves_any_stricter_server_wait(self):
        self.configure('like', 1)
        budget.reserve(self.controller, 'like')
        def pending(data, conn):
            data['drafts']['local-wait'] = {'id': 'local-wait', 'agent_id': 'synthetic', 'action': 'like',
                'state': 'pending', 'next_attempt_at': self.now + 60, 'quota_wait_bucket': 'like'}
            data['drafts']['other-wait'] = {'id': 'other-wait', 'agent_id': 'synthetic', 'action': 'like',
                'state': 'pending', 'next_attempt_at': self.now + 90}
        self.controller.store.change(pending)
        self.configure('like', 10)
        data = self.controller.store.read()
        self.assertEqual(data['drafts']['local-wait']['next_attempt_at'], self.now + 6)
        self.assertEqual(data['drafts']['other-wait']['next_attempt_at'], self.now + 90)
        budget.cooldown(self.controller, 'like', 45)
        self.configure('like', None)
        self.assertEqual(self.controller.store.read()['drafts']['local-wait']['next_attempt_at'], self.now + 45)

    def test_invalid_policy_inputs_are_atomic_and_models_cannot_be_configured(self):
        before = self.controller.store.read()
        for payload in ({'bucket': 'llm:default', 'cap': 1}, {'bucket': 'unknown', 'cap': 1},
                        {'bucket': 'like', 'cap': True}, {'bucket': 'like', 'cap': 0},
                        {'bucket': 'like', 'cap': -1}, {'bucket': 'like', 'cap': 1.5},
                        {'bucket': 'like', 'cap': '3'}, {'bucket': 'like'},
                        {'bucket': 'like', 'cap': 3, 'enabled': True}):
            with self.subTest(payload=payload), self.assertRaises(AgentError):
                self.controller.quotas(payload)
        self.assertEqual(self.controller.store.read(), before)


class QuotaAPITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.app = create_app(directory / 'quota-api.db', backup_dir=directory / 'backups')
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')
        self.addCleanup(self.client.close)
        self.controller = self.app.state.agent_control
        self.addCleanup(self.controller.stop)

    def test_quota_get_requires_member_and_updates_require_csrf(self):
        self.assertEqual(self.client.get('/api/agents/quotas').status_code, 401)
        token = login(self.client)
        self.assertEqual(self.client.get('/api/agents/quotas').status_code, 200)
        self.assertEqual(self.client.post('/api/agents/quotas', json={'bucket': 'like', 'cap': 5}).status_code, 403)
        result = self.client.post('/api/agents/quotas', json={'bucket': 'like', 'cap': 5},
                                  headers={'X-CTN-Token': token})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['policy']['limits'], {'like': 5})

    def test_quota_api_persists_one_cap_and_can_restore_official(self):
        token = login(self.client)
        headers = {'X-CTN-Token': token}
        settings = copy.deepcopy(self.controller.store.read()['settings'])
        for bucket, cap in (('like', 5), ('follow', 2), ('like', None)):
            response = self.client.post('/api/agents/quotas', json={'bucket': bucket, 'cap': cap}, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get('/api/agents/quotas').json()['policy']['limits'], {'follow': 2})
        self.assertEqual(self.controller.store.read()['settings'], settings)
        self.assertEqual(AgentController(self.controller.db_path, self.controller.secret_dir.parent).quotas()[
            'policy']['limits'], {'follow': 2})

    def test_api_rejects_ambiguous_zero_boolean_unknown_and_unrelated_settings(self):
        token = login(self.client)
        before = self.controller.store.read()
        for payload in ({'bucket': 'post', 'cap': 0}, {'bucket': 'post', 'cap': True},
                        {'bucket': 'llm:default', 'cap': 3}, {'bucket': 'like'},
                        {'bucket': 'like', 'cap': 5, 'enabled': True}):
            with self.subTest(payload=payload):
                response = self.client.post('/api/agents/quotas', json=payload,
                                            headers={'X-CTN-Token': token})
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.controller.store.read(), before)


if __name__ == '__main__':
    unittest.main()
