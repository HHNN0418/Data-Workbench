"""Model lease renewal tolerates brief storage contention without model replay."""
from copy import deepcopy
import sqlite3
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ctn_data import agent_budget as budget


class RuntimeStore:
    def __init__(self):
        self.data = {'leases': {}}
        self.lock = threading.Lock()
        self.calls = []
        self.before = None
        self.after = None

    def change(self, callback):
        raise AssertionError('Model lease bookkeeping loaded the full business state')

    def change_runtime(self, callback):
        with self.lock:
            self.calls.append(callback.__name__)
            if self.before:
                self.before(callback.__name__)
            candidate = deepcopy(self.data)
            result = callback(candidate, None)
            self.data = candidate
            if self.after:
                self.after(callback.__name__)
            return result


class BudgetStore(RuntimeStore):
    def __init__(self):
        super().__init__()
        self.data = {'rate': {}}

    change_budget = RuntimeStore.change_runtime


class BudgetRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.store = BudgetStore()
        self.controller = SimpleNamespace(store=self.store, clock=lambda: 1792539000.0,
                                          owner='owner')

    def test_sdk_budget_and_usage_updates_only_load_rate(self):
        calls = []

        def accepted():
            calls.append(True)
            budget.observe_rate(self.controller,
                                SimpleNamespace(limit=30, remaining=29, reset=60))
            return {'success': True}

        with budget.actor(self.controller, 'actor'):
            self.assertEqual(budget.call(self.controller, 'like', accepted), {'success': True})
        self.assertEqual(calls, [True])
        self.assertEqual(self.store.calls, ['claim', 'record', 'record', 'save'])
        rate = self.store.data['rate']
        self.assertEqual(rate['buckets']['like']['tokens'], 29)
        totals = rate['quota_usage_totals']['actor']['like']
        self.assertEqual(totals['attempts'], 1)
        self.assertEqual(totals['confirmed_success'], 1)

    def test_definitive_failure_refunds_budget_without_replaying_sdk(self):
        failure = RuntimeError('request rejected')
        failure.status = 409
        calls = []

        def rejected():
            calls.append(True)
            raise failure

        with budget.actor(self.controller, 'actor'):
            with self.assertRaises(RuntimeError) as caught:
                budget.call(self.controller, 'post', rejected)
        self.assertIs(caught.exception, failure)
        self.assertEqual(calls, [True])
        rate = self.store.data['rate']
        self.assertEqual(rate['buckets']['post']['tokens'], 3)
        totals = rate['quota_usage_totals']['actor']['post']
        self.assertEqual(totals['attempts'], 1)
        self.assertEqual(totals['definite_failure'], 1)
        self.assertIn('refund', self.store.calls)

    def test_cooldown_is_persisted_on_budget_path(self):
        budget.cooldown(self.controller, 'like', 40)
        self.assertEqual(self.store.data['rate']['buckets']['like']['not_before'],
                         self.controller.clock() + 40)
        with self.assertRaises(budget.Deferred):
            budget.reserve(self.controller, 'like')

    def test_failed_non_idempotent_reservation_is_not_retried(self):
        def before(name):
            if name == 'claim':
                raise sqlite3.OperationalError('database is locked')

        self.store.before = before
        with patch.object(budget.ai_providers, 'complete') as model:
            with self.assertRaises(sqlite3.OperationalError):
                budget.reserve(self.controller, 'like')
        self.assertEqual(self.store.calls, ['claim'])
        model.assert_not_called()


class ModelRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.store = RuntimeStore()
        self.now = 1792539000.0
        self.controller = SimpleNamespace(store=self.store, clock=lambda: self.now, owner='owner')
        self.provider = {'id': 'test', 'base_url': 'https://example.invalid/v1'}
        self.cancelled = threading.Event()
        self.reserve = patch.object(budget, 'reserve').start()
        self.addCleanup(patch.stopall)
        patch.object(budget, 'MODEL_LEASE_RENEW_SECONDS', .005).start()
        patch.object(budget, 'MODEL_LEASE_RETRY_SECONDS', .002).start()

    def call(self, complete):
        with patch.object(budget.ai_providers, 'complete', side_effect=complete) as request:
            result = budget.model_call(self.controller, self.provider, [],
                                       cancellation_event=self.cancelled)
        self.assertEqual(request.call_count, 1)
        return result

    def test_claim_validate_and_release_use_only_runtime_state(self):
        self.assertEqual(self.call(lambda *a, **kw: {'text': 'ok'}), {'text': 'ok'})
        self.assertEqual(self.store.calls, ['claim', 'validate', 'release'])
        self.assertEqual(self.store.data['leases'], {})

    def test_two_brief_lock_timeouts_retry_only_the_renewal(self):
        renewed = threading.Event()
        attempts = 0

        def before(name):
            nonlocal attempts
            if name == 'renew':
                attempts += 1
                if attempts < 3:
                    # Deliberately contains private-looking text: diagnostics
                    # may log the exception type/code, never this message.
                    raise sqlite3.OperationalError('database is locked private-provider-value')
                renewed.set()

        self.store.before = before

        def complete(*args, **kwargs):
            self.assertTrue(renewed.wait(1))
            self.assertFalse(self.cancelled.is_set())
            return {'text': 'complete'}

        with self.assertLogs('ctn_data.agent_budget', level='WARNING') as messages:
            self.assertEqual(self.call(complete), {'text': 'complete'})
        self.assertEqual(attempts, 3)
        self.assertNotIn('private-provider-value', '\n'.join(messages.output))
        self.assertEqual(self.store.data['leases'], {})

    def test_repeated_lock_failures_cancel_instead_of_replaying_model(self):
        def before(name):
            if name == 'renew':
                raise sqlite3.OperationalError('database is locked')

        self.store.before = before

        def complete(*args, **kwargs):
            self.assertTrue(self.cancelled.wait(1))
            return {'text': 'late result'}

        with patch.object(budget.ai_providers, 'complete', side_effect=complete) as request:
            with self.assertRaises(budget.ai_providers.ProviderError):
                budget.model_call(self.controller, self.provider, [], cancellation_event=self.cancelled)
            self.assertEqual(request.call_count, 1)
        self.assertEqual(self.store.calls.count('renew'), budget.MODEL_LEASE_RETRY_ATTEMPTS)
        self.assertEqual(self.store.data['leases'], {})

    def test_replaced_owner_is_neither_renewed_nor_released(self):
        replacement = {'owner': 'new-owner', 'token': 'new-token', 'until': self.now + 300}

        def complete(*args, **kwargs):
            with self.store.lock:
                key = next(iter(self.store.data['leases']))
                self.store.data['leases'][key] = replacement.copy()
            self.assertTrue(self.cancelled.wait(1))
            return {'text': 'stale'}

        with self.assertRaises(budget.ai_providers.ProviderError):
            self.call(complete)
        self.assertEqual(self.store.calls.count('renew'), 1)
        self.assertEqual(list(self.store.data['leases'].values()), [replacement])

    def test_lease_expired_during_reservation_prevents_model_request(self):
        self.reserve.side_effect = lambda *a, **kw: setattr(self, 'now', self.now + 100)
        with patch.object(budget.ai_providers, 'complete') as request:
            with self.assertRaises(budget.ai_providers.ProviderError):
                budget.model_call(self.controller, self.provider, [])
        request.assert_not_called()
        self.assertEqual(self.store.data['leases'], {})

    def test_expiry_while_waiting_for_lock_cannot_revive_lease_after_backward_clock_jump(self):
        # The wall clock remains fixed, modelling a backwards clock correction.
        # A renewal queued while valid must still reject expiry before callback.
        patch.object(budget, 'MODEL_LEASE_TTL_SECONDS', .025).start()

        def before(name):
            if name == 'renew':
                time.sleep(.035)

        self.store.before = before

        def complete(*args, **kwargs):
            self.assertTrue(self.cancelled.wait(1))
            return {'text': 'stale'}

        with self.assertRaises(budget.ai_providers.ProviderError):
            self.call(complete)
        self.assertEqual(self.store.calls.count('renew'), 1)

    def test_claim_recovers_unknown_commit_without_a_second_model_request(self):
        committed = []

        def after(name):
            if name == 'claim':
                committed.append(deepcopy(self.store.data['leases']))
                if len(committed) == 1:
                    raise sqlite3.OperationalError('database is locked')

        self.store.after = after
        self.assertEqual(self.call(lambda *a, **kw: {'text': 'ok'}), {'text': 'ok'})
        self.assertEqual(self.store.calls.count('claim'), 2)
        self.assertEqual(committed[0], committed[1])
        self.assertEqual(self.store.data['leases'], {})

    def test_send_validation_retries_storage_only_without_repeating_reservation(self):
        validations = 0

        def before(name):
            nonlocal validations
            if name == 'validate':
                validations += 1
                if validations == 1:
                    raise sqlite3.OperationalError('database is locked')

        self.store.before = before
        self.assertEqual(self.call(lambda *a, **kw: {'text': 'ok'}), {'text': 'ok'})
        self.assertEqual(validations, 2)
        self.reserve.assert_called_once()
        self.assertEqual(self.store.data['leases'], {})

    def test_non_storage_bug_cancels_without_retry(self):
        def before(name):
            if name == 'renew':
                raise ValueError('invalid runtime state')

        self.store.before = before

        def complete(*args, **kwargs):
            self.assertTrue(self.cancelled.wait(1))
            return {'text': 'stale'}

        with self.assertRaises(budget.ai_providers.ProviderError):
            self.call(complete)
        self.assertEqual(self.store.calls.count('renew'), 1)


if __name__ == '__main__':
    unittest.main()
