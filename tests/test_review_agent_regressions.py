"""Regression coverage for account rollback and cancellable model requests."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ctn_data import agent_budget, agent_control, ai_providers
from ctn_data.agent_control import AgentController, AgentError


PERSONA = {'name': 'Synthetic Resident', 'username': 'synthetic_resident',
           'background': ('A fictional resident with a detailed synthetic history. ' * 5).strip(),
           'stance': 'Synthetic position', 'voice': 'Brief and specific',
           'interests': ['Synthetic local history'], 'language': 'English'}


class _CommitFailure:
    """Fail after a real callback and its SQL writes, before durable commit."""
    def __init__(self, connection, attempts):
        self.connection, self.attempts = connection, attempts

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, kind, value, traceback):
        if kind is None:
            self.attempts.append(True)
            self.connection.rollback()
            raise sqlite3.OperationalError('synthetic commit failure')
        return self.connection.__exit__(kind, value, traceback)


class AgentReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = 1792450000.0
        self.provider = {'id': 'synthetic-provider', 'protocol': 'openai_chat',
                         'model': 'synthetic-model', 'base_url': 'https://example.invalid/v1',
                         'api_key': 'SYNTHETIC-KEY', 'max_output_tokens': 1800}
        self.controller = AgentController(self.root / 'state.sqlite3', self.root / 'keys',
            provider_store=SimpleNamespace(get=lambda _: dict(self.provider)),
            sdk_factory=Mock(side_effect=AssertionError('No external platform calls allowed')),
            clock=lambda: self.now)
        self.addCleanup(self.controller.stop)
        # Binary, opaque-on-disk test protection; never persist a real secret.
        self.protect = patch.object(ai_providers, '_protect',
            side_effect=lambda text: b'\xffopaque\x00' + text.encode()).start()
        self.addCleanup(patch.stopall)
        self.controller.save({'name': 'Synthetic actor'})
        self.agent_id = next(iter(self.controller.store.read()['agents']))
        self.path = self.controller.secret_dir / (self.agent_id + '.key')

    def account(self, account_id, **profile):
        return SimpleNamespace(account_id=account_id, username='actor_' + str(account_id),
                               token='synthetic-token-' + str(account_id), expires_at='', profile=profile)

    def bind(self, account_id, **profile):
        with self.controller._claim(self.agent_id) as token:
            self.controller._save_account(self.agent_id, token, self.account(account_id, **profile),
                                          {'password': 'synthetic-password'})

    def answer(self):
        return {'text': json.dumps(PERSONA), 'model': 'synthetic-model'}

    def test_invalid_profile_restores_original_encrypted_bytes_and_binding(self):
        self.bind(1)
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.bind(2, created_at='invalid-date')
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.controller.store.read()['agents'][self.agent_id]['account_id'], 1)
        self.assertEqual(self.protect.call_count, 2, 'rollback must not decrypt/re-encrypt old credentials')

    def test_commit_failure_restores_previous_file_or_removes_first_file(self):
        real_connect = agent_control.connect
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    self.bind(1)
                original = self.path.read_bytes() if self.path.exists() else None
                before = self.controller.store.read()['agents'][self.agent_id]['account_id']
                attempts = []
                with self.controller._claim(self.agent_id) as token:
                    with patch.object(agent_control, 'connect',
                            side_effect=lambda path: _CommitFailure(real_connect(path), attempts)):
                        with self.assertRaisesRegex(sqlite3.OperationalError, 'synthetic commit failure'):
                            self.controller._save_account(self.agent_id, token, self.account(2),
                                                          {'password': 'synthetic-password'})
                self.assertEqual(attempts, [True], 'the full callback must succeed before simulated commit failure')
                self.assertEqual(self.path.read_bytes() if self.path.exists() else None, original)
                self.assertEqual(self.controller.store.read()['agents'][self.agent_id]['account_id'], before)

    def test_failed_stale_binding_does_not_replace_a_newer_credential_file(self):
        self.bind(1)
        newer = b'\x00newer encrypted binding\xff'
        def competing_write(*args, **kwargs):
            self.path.write_bytes(newer)
            raise ValueError('synthetic failed stale transaction')
        with patch('ctn_data.agent_tracking.record_account', side_effect=competing_write):
            with self.assertRaises(ValueError):
                self.bind(2)
        self.assertEqual(self.path.read_bytes(), newer)

    def test_persona_lost_model_lease_cancels_transport_and_releases_own_slot(self):
        observed = {}
        def blocked(*args, **kwargs):
            observed.update(kwargs)
            leases = self.controller.store.read_runtime()['leases']
            owned = [key for key in leases if key.startswith('agent-model:synthetic-provider:')]
            self.assertEqual(len(owned), 1, 'persona did not acquire its own model lease')
            observed['lease_key'] = owned[0]
            self.assertEqual(leases[owned[0]]['owner'], self.controller.owner)
            # Expire confirmed ownership, without imposing a persona-generation
            # deadline. The real lease heartbeat must cancel the transport.
            self.now += agent_budget.MODEL_LEASE_TTL_SECONDS + 1
            self.assertTrue(kwargs['cancellation_event'].wait(2), 'lost model ownership must cancel the transport')
            raise ai_providers.ProviderError('synthetic cancellation', 499)
        with patch.object(agent_budget, 'MODEL_LEASE_RENEW_SECONDS', .02), \
                patch.object(ai_providers, 'complete', side_effect=blocked) as complete:
            with self.assertRaises(AgentError) as caught:
                self.controller.generate_persona({})
        complete.assert_called_once()
        self.assertEqual(caught.exception.status_code, 499)
        self.assertTrue(observed['wait_for_completion'])
        self.assertNotIn('timeout_seconds', observed)
        self.assertFalse(self.controller._persona_requests)
        self.assertNotIn(observed['lease_key'], self.controller.store.read_runtime()['leases'])
        self.assertFalse(any(key.startswith('agent-model:synthetic-provider:')
                             for key in self.controller.store.read_runtime()['leases']))
        with patch.object(ai_providers, 'complete', return_value=self.answer()) as complete:
            self.assertEqual(self.controller.generate_persona({})['persona'], PERSONA)
        complete.assert_called_once()

    def test_stop_cancels_persona_and_start_allows_a_new_managed_request(self):
        entered, errors = threading.Event(), []
        def blocked(*args, **kwargs):
            entered.set()
            if not kwargs['cancellation_event'].wait(2):
                raise AssertionError('shutdown did not cancel persona transport')
            raise ai_providers.ProviderError('synthetic cancellation', 499)
        def generate():
            try:
                self.controller.generate_persona({})
            except Exception as exc:
                errors.append(exc)
        with patch.object(ai_providers, 'complete', side_effect=blocked):
            worker = threading.Thread(target=generate)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.controller.stop()
            finally:
                self.controller.stop()
                worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], AgentError)
        self.assertEqual(errors[0].status_code, 499)
        self.assertFalse(self.controller._persona_requests)
        self.assertNotIn('agent-model:synthetic-provider', self.controller.store.read()['leases'])
        with patch.object(ai_providers, 'complete') as complete:
            with self.assertRaises(AgentError) as stopped:
                self.controller.generate_persona({})
            self.assertEqual(stopped.exception.status_code, 503)
            complete.assert_not_called()
        self.now += 2
        with patch.object(self.controller, 'tick'), patch.object(ai_providers, 'complete', return_value=self.answer()):
            self.controller.start()
            try:
                self.assertEqual(self.controller.generate_persona({})['persona'], PERSONA)
            finally:
                self.controller.stop()

    def test_regular_generation_waits_past_lease_duration_while_ownership_is_renewed(self):
        observed = {}
        def regular(*args, **kwargs):
            observed.update(kwargs)
            self.assertTrue(kwargs['wait_for_completion'])
            # Await more than two complete lease durations. A fixed application
            # generation cutoff or a non-renewed model lease would cancel this
            # request before the synthetic complete answer is available.
            self.assertFalse(kwargs['cancellation_event'].wait(.5))
            self.assertFalse(kwargs['cancellation_event'].is_set())
            return self.answer()
        with patch.object(agent_budget, 'MODEL_LEASE_TTL_SECONDS', .2), \
                patch.object(agent_budget, 'MODEL_LEASE_RENEW_SECONDS', .02), \
                patch.object(self.controller.store, 'change_runtime',
                             wraps=self.controller.store.change_runtime) as runtime, \
                patch.object(ai_providers, 'complete', side_effect=regular) as complete:
            result = agent_budget.model_call(self.controller, self.provider,
                                            [{'role': 'user', 'content': 'synthetic'}])
        self.assertEqual(result, self.answer())
        complete.assert_called_once()
        self.assertNotIn('timeout_seconds', observed)
        self.assertGreaterEqual(sum(call.args[0].__name__ == 'renew'
                                    for call in runtime.call_args_list), 2)
        self.assertFalse(any(key.startswith('agent-model:synthetic-provider:')
                             for key in self.controller.store.read_runtime()['leases']))


if __name__ == '__main__':
    unittest.main()
