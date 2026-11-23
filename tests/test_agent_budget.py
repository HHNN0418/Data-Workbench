"""Shared team pacing tested without contacting a platform or model."""
import tempfile
import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ctn_data import agent_budget as budget, coordination, llm_calls
from ctn_data.agent_control import AgentController
from ctn_data.db import connect


class BudgetIntegration(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.now = 1792450000.0
        self.path = Path(folder.name) / 'budget.sqlite3'
        self.first = AgentController(self.path, Path(folder.name) / 'secrets', clock=lambda: self.now)
        self.second = AgentController(self.path, Path(folder.name) / 'secrets', clock=lambda: self.now)

    def advance(self, seconds):
        self.now += seconds

    def test_official_allowance_is_shared_with_bursts_and_independent_like_quota(self):
        write = Mock(return_value={'post_id': 1})
        for _ in range(3):
            budget.call(self.first, 'post', write)
        with self.assertRaises(budget.Deferred):
            budget.call(self.second, 'post', write)
        budget.call(self.second, 'like', write)
        self.assertEqual(write.call_count, 4)
        self.now += 20
        budget.call(self.second, 'post', write)
        self.assertEqual(write.call_count, 5)

    def test_sequential_reads_use_available_official_quota_without_local_wait(self):
        read = Mock(return_value='read')
        with patch.object(budget.time, 'sleep', side_effect=self.advance) as sleep:
            self.assertEqual(budget.call(self.first, 'read', read), 'read')
            self.assertEqual(budget.call(self.second, 'read', read), 'read')
        self.assertEqual(read.call_count, 2)
        sleep.assert_not_called()

    def test_old_global_gap_does_not_override_official_allowance(self):
        self.first.store.change(lambda data, conn: data['settings'].update(global_interval_seconds=86400))
        for controller in (self.first, self.second, self.first):
            budget.reserve(controller, 'post')
        with self.assertRaises(budget.Deferred) as denied:
            budget.reserve(self.second, 'post')
        self.assertEqual(denied.exception.retry_after, 20)

    def test_delayed_transaction_cannot_rewind_refill_time(self):
        original = self.first.store.change_budget
        def delayed(change):
            self.advance(20)
            budget.reserve(self.second, 'post')
            return original(change)
        with patch.object(self.first.store, 'change_budget', side_effect=delayed):
            budget.reserve(self.first, 'post')
        budget.reserve(self.second, 'post')
        with self.assertRaises(budget.Deferred):
            budget.reserve(self.second, 'post')
        state = self.first.store.read()['rate']['buckets']['post']
        self.assertEqual(state['token_at'], self.now)

    def test_actions_share_post_quota_but_keep_like_and_follow_available(self):
        for _ in range(3):
            budget.reserve(self.first, 'post')
        actions = budget.action_status(self.second, ['post', 'reply', 'repost', 'like', 'follow', 'skip'])
        for action in ('post', 'reply', 'repost'):
            self.assertFalse(actions[action]['available'])
            self.assertEqual(actions[action]['retry_after'], 20)
        for action in ('like', 'follow', 'skip'):
            self.assertTrue(actions[action]['available'])
        self.assertEqual(self.first.store.read()['rate']['buckets']['post']['granted'], 3)

    def test_relationship_routes_have_thirty_slots_and_two_second_refill(self):
        for action in ('like', 'unlike', 'follow', 'unfollow', 'delete_post'):
            for _ in range(30):
                budget.reserve(self.first, action)
            with self.assertRaises(budget.Deferred):
                budget.reserve(self.second, action)
        self.advance(2)
        for action in ('like', 'unlike', 'follow', 'unfollow', 'delete_post'):
            budget.reserve(self.second, action)

    def test_read_routes_have_independent_team_quotas(self):
        def get_post():
            return 1
        def following():
            return []
        for _ in range(300):
            budget.reserve(self.first, 'read:get_post')
        with self.assertRaises(budget.Deferred):
            budget.reserve(self.second, 'read:get_post')
        self.assertEqual(budget.call(self.second, 'read', following), [])
        self.assertEqual(budget._request_bucket('read', get_post), 'read:get_post')

    def test_server_quota_overrides_default_and_failed_writes_use_separate_quota(self):
        def accepted():
            budget.observe_rate(self.first, SimpleNamespace(limit=6, remaining=5, reset=60))
        budget.call(self.first, 'post', accepted)
        for _ in range(5):
            budget.reserve(self.second, 'post')
        with self.assertRaises(budget.Deferred) as denied:
            budget.reserve(self.first, 'post')
        self.assertEqual(denied.exception.retry_after, 10)
        error = RuntimeError('definitively rejected')
        error.status = 409
        def rejected():
            budget.observe_rate(self.first, SimpleNamespace(limit=10, remaining=9, reset=60))
            raise error
        with self.assertRaises(RuntimeError):
            budget.call(self.first, 'like', rejected)
        state = budget.public_status(self.first)
        self.assertEqual(state['failed:like']['limit'], 10)
        self.assertIsNone(state['like']['limit'])
        for _ in range(30):
            budget.reserve(self.second, 'like')

    def test_success_with_empty_server_bucket_is_still_successful(self):
        def accepted():
            budget.observe_rate(self.first, SimpleNamespace(limit=3, remaining=0, reset=60))
            return {'post_id': 42}
        result = budget.call(self.first, 'post', accepted)
        self.assertEqual(result['post_id'], 42)
        self.assertEqual(budget.public_status(self.first)['post']['remaining'], 0)
        with self.assertRaises(budget.Deferred):
            budget.call(self.second, 'post', Mock())

    def test_server_refill_time_replaces_local_default(self):
        def accepted():
            budget.observe_rate(self.first, SimpleNamespace(limit=3, remaining=0, reset=9))
        budget.call(self.first, 'post', accepted)
        self.advance(3)
        budget.reserve(self.second, 'post')

    def test_rate_observation_failure_preserves_remote_success_and_restores_context(self):
        def accepted():
            budget.observe_rate(self.first, SimpleNamespace(limit=3, remaining=0, reset=60))
            return {'post_id': 42}
        with patch.object(budget, '_observe', side_effect=RuntimeError('synthetic diagnostic failure')):
            self.assertEqual(budget.call(self.first, 'post', accepted), {'post_id': 42})
        self.assertIsNone(getattr(budget._CONTEXT, 'current', None))

    def test_quota_persistence_failure_preserves_definitive_remote_error(self):
        error = RuntimeError('original remote refusal')
        error.status, error.retry_after = 429, 75
        with patch.object(budget, '_refund', side_effect=RuntimeError('refund failure')), \
                patch.object(budget, 'cooldown', side_effect=RuntimeError('cooldown failure')):
            with self.assertRaises(RuntimeError) as caught:
                budget.call(self.first, 'like', Mock(side_effect=error))
        self.assertIs(caught.exception, error)
        self.assertIsNone(getattr(budget._CONTEXT, 'current', None))

    def test_429_sets_only_affected_budget_and_never_retries_request(self):
        error = RuntimeError('synthetic throttling')
        error.status, error.retry_after = 429, 75
        endpoint = Mock(side_effect=error)
        with self.assertRaises(RuntimeError):
            budget.call(self.first, 'like', endpoint)
        self.assertEqual(endpoint.call_count, 1)
        with self.assertRaises(budget.Deferred):
            budget.call(self.second, 'like', endpoint)
        self.assertEqual(endpoint.call_count, 1)
        self.assertEqual(budget.call(self.second, 'post', lambda: 8), 8)
        self.assertGreaterEqual(budget.public_status(self.first)['like']['retry_after'], 75)

    def test_429_wait_honors_exhausted_header_even_when_sdk_retry_is_shorter(self):
        error = RuntimeError('synthetic throttling')
        error.status, error.retry_after = 429, 2
        def rejected():
            budget.observe_rate(self.first, SimpleNamespace(limit=30, remaining=0, reset=300))
            raise error
        with self.assertRaises(RuntimeError):
            budget.call(self.first, 'like', rejected)
        self.assertEqual(error.retry_after, 10)
        self.advance(2)
        with self.assertRaises(budget.Deferred):
            budget.reserve(self.second, 'like')
        self.advance(8)
        budget.reserve(self.second, 'like')

    def test_agent_home_reads_obey_public_collector_gate(self):
        conn = connect(self.path)
        try:
            self.assertTrue(coordination.permit(conn, '/api/posts', 'collector', now=self.now)['allowed'])
        finally:
            conn.close()
        endpoint = Mock(return_value=[])
        with self.assertRaises(budget.Deferred):
            budget.call(self.first, 'feed', endpoint)
        endpoint.assert_not_called()
        self.now += 9
        budget.call(self.first, 'feed', endpoint)
        endpoint.assert_called_once()

    def test_home_response_cooldown_propagates_back_to_collector(self):
        def home():
            budget.observe_rate(self.first, SimpleNamespace(limit=10, remaining=0, reset=200))
            return []
        budget.call(self.first, 'feed', home)
        self.now += 9
        conn = connect(self.path)
        try:
            denied = coordination.permit(conn, '/api/posts', 'collector', now=self.now)
        finally:
            conn.close()
        self.assertFalse(denied['allowed'])
        self.assertEqual(denied['reason'], 'cooldown')

    def test_third_party_models_allow_parallel_roles_and_release_request_leases(self):
        provider = {'id': 'shared', 'base_url': 'https://example.invalid/v1', 'max_output_tokens': 1500}
        def complete(*args, **kwargs):
            with patch.object(budget.ai_providers, 'complete', return_value={'text': 'parallel'}) as parallel:
                self.assertEqual(budget.model_call(self.second, provider, [])['text'], 'parallel')
                parallel.assert_called_once()
            raise RuntimeError('synthetic model failure')
        with patch.object(budget.ai_providers, 'complete', side_effect=complete) as model:
            with self.assertRaises(RuntimeError):
                budget.model_call(self.first, provider, [])
            model.assert_called_once()
        self.assertFalse(any(key.startswith('agent-model:shared:') for key in self.first.store.read()['leases']))
        with patch.object(budget.ai_providers, 'complete', return_value={'text': 'ok'}) as model:
            budget.model_call(self.second, provider, [])
            model.assert_called_once()

    def test_duplicate_official_connections_share_team_cap_without_output_limit(self):
        provider = {'id': 'first-name', 'base_url': 'https://llm.legitreal.com/v1', 'max_output_tokens': 1800}
        with patch.object(budget.ai_providers, 'complete', return_value={'text': 'ok'}) as model:
            budget.model_call(self.first, provider, [], max_output_tokens=1800)
            self.assertNotIn('max_output_tokens', model.call_args.kwargs)
            with self.assertRaises(budget.Deferred):
                budget.model_call(self.second, {**provider, 'id': 'second-name'}, [])
            model.assert_called_once()
        self.now += 21
        with patch.object(budget.ai_providers, 'complete', return_value={'text': 'ok'}) as model:
            budget.model_call(self.second, provider, [])
            model.assert_called_once()

    def test_agent_waits_for_complete_result_without_changing_output_budget(self):
        provider = {'id': 'grok', 'base_url': 'https://api.x.ai/v1', 'model': 'grok-4.7',
                    'max_output_tokens': 1800}
        def complete(*args, **kwargs):
            self.assertTrue(kwargs['wait_for_completion'])
            self.assertNotIn('timeout_seconds', kwargs)
            self.assertNotIn('reasoning_effort', kwargs)
            self.assertNotIn('max_output_tokens', kwargs)
            self.assertEqual(kwargs['response_schema'], {'type': 'object'})
            self.assertIsInstance(kwargs['cancellation_event'], threading.Event)
            leases = [lease for key, lease in self.first.store.read()['leases'].items() if key.startswith('agent-model:grok:')]
            self.assertEqual(len(leases), 1)
            self.assertEqual(leases[0]['until'], self.now + 90)
            self.now += 60
            with patch.object(budget.ai_providers, 'complete', return_value={'text': 'parallel'}) as second_request:
                budget.model_call(self.second, provider, [])
                second_request.assert_called_once()
            return {'text': 'ok'}
        with patch.object(budget.ai_providers, 'complete', side_effect=complete):
            result = budget.model_call(self.first, provider, [], response_schema={'type': 'object'})
        self.assertEqual(result['text'], 'ok')
        self.assertFalse(any(key.startswith('agent-model:grok:') for key in self.first.store.read()['leases']))

    def test_agent_schema_forwarding_keeps_unknown_and_official_endpoints_compatible(self):
        schema = {'type': 'object'}
        cases = (
            ('https://openrouter.ai/api/v1', 'deepseek/deepseek-v4.1-flash', True),
            ('https://api.x.ai/v1', 'grok-4.7', True),
            ('https://us-east-1.api.x.ai/v1', 'grok-4.7', True),
            ('https://api.x.ai/v1', 'grok-3', False),
            ('https://llm.legitreal.com/v1', 'competition-model', False),
            ('https://api.deepseek.com/v1', 'deepseek-chat', False),
            ('https://example.com/v1', 'grok-4.7', False),
            ('https://openrouter.ai.example.com/v1', 'deepseek/deepseek-chat', False),
        )
        for index, (base_url, name, supported) in enumerate(cases):
            provider = {'id': 'schema-' + str(index), 'base_url': base_url, 'model': name}
            with self.subTest(base_url=base_url, model=name), \
                    patch.object(budget.ai_providers, 'complete', return_value={'text': '{}'}) as model:
                budget.model_call(self.first, provider, [], response_schema=schema)
                options = model.call_args.kwargs
                self.assertEqual('response_schema' in options, supported)
                if supported:
                    self.assertEqual(options['response_schema'], schema)
                self.assertNotIn('max_output_tokens', options)
                self.assertNotIn('timeout_seconds', options)

    def test_openrouter_agent_schema_reaches_wire_with_required_upstream_support(self):
        provider = {'id': 'openrouter', 'base_url': 'https://openrouter.ai/api/v1',
                    'model': 'deepseek/deepseek-v4.1-flash', 'protocol': 'openai_chat',
                    'api_key': 'fake-key', 'thinking_mode': 'enabled'}
        schema = {'type': 'object', 'properties': {'source_ids': {
            'type': 'array', 'items': {'type': 'string', 'enum': ['post:7']}}},
            'required': ['source_ids'], 'additionalProperties': False}
        response = {'choices': [{'message': {'content': '{"source_ids":["post:7"]}'}}]}
        with patch.object(budget.ai_providers, '_request_json', return_value=response) as request:
            result = budget.model_call(self.first, provider,
                [{'role': 'user', 'content': 'Return JSON source IDs.'}], response_schema=schema)
        self.assertEqual(result['text'], '{"source_ids":["post:7"]}')
        request.assert_called_once()
        self.assertEqual(request.call_args.args[0], 'https://openrouter.ai/api/v1/chat/completions')
        wire = json.loads(request.call_args.args[1])
        self.assertEqual(wire['response_format']['json_schema']['schema'], schema)
        self.assertTrue(wire['response_format']['json_schema']['strict'])
        self.assertEqual(wire['provider'], {'require_parameters': True})
        self.assertEqual(wire['reasoning'], {'enabled': True})
        # D4/D13: an OpenRouter step sends its per-purpose cap (bounds the
        # in-flight credit hold) and a sticky cache session.
        self.assertEqual(wire['max_tokens'], llm_calls.MAX_TOKENS['agent'])
        self.assertNotIn('max_completion_tokens', wire)
        self.assertEqual(wire['session_id'], 'ctn-agent')
        self.assertTrue(request.call_args.kwargs['wait_for_completion'])

    def test_wait_and_explicit_cancellation_progress_apply_to_other_agent_models(self):
        provider = {'id': 'deepseek', 'base_url': 'https://api.deepseek.com/v1',
                    'model': 'deepseek-flash', 'max_output_tokens': 1800,
                    'thinking_mode': 'enabled'}
        cancelled, progress = threading.Event(), Mock()
        with patch.object(budget.ai_providers, 'complete', return_value={'text': 'ok'}) as model:
            budget.model_call(self.first, provider, [], cancellation_event=cancelled, on_progress=progress)
        options = model.call_args.kwargs
        self.assertTrue(options['wait_for_completion'])
        self.assertIs(options['cancellation_event'], cancelled)
        self.assertIs(options['on_progress'], progress)
        self.assertNotIn('reasoning_effort', options)
        self.assertNotIn('timeout_seconds', options)
        self.assertEqual(provider['thinking_mode'], 'enabled')

    def test_model_lease_cleanup_failure_does_not_discard_completed_result(self):
        original = self.first.store.change_runtime
        release_attempted = threading.Event()
        def store_change(change):
            if change.__name__ == 'release':
                release_attempted.set()
                raise RuntimeError('synthetic cleanup failure')
            return original(change)
        provider = {'id': 'cleanup', 'base_url': 'https://example.invalid/v1'}
        with patch.object(self.first.store, 'change_runtime', side_effect=store_change), \
                patch.object(budget.ai_providers, 'complete', return_value={'text': 'complete'}):
            self.assertEqual(budget.model_call(self.first, provider, [])['text'], 'complete')
        self.assertTrue(release_attempted.is_set())

    def test_model_lease_is_renewed_while_waiting_and_cleanup_stops_heartbeat(self):
        provider = {'id': 'renewed', 'base_url': 'https://example.invalid/v1'}
        cancelled = threading.Event()
        waiting = threading.Event()

        def complete(*args, **kwargs):
            key = next(key for key in self.first.store.read()['leases'] if key.startswith('agent-model:renewed:'))
            self.now += 60
            for _ in range(100):
                until = self.first.store.read()['leases'][key]['until']
                if until == self.now + 90:
                    break
                waiting.wait(.005)
            self.assertEqual(until, self.now + 90)
            with patch.object(budget.ai_providers, 'complete', return_value={'text': 'parallel'}) as second_request:
                budget.model_call(self.second, provider, [], cancellation_event=cancelled)
                second_request.assert_called_once()
            return {'text': 'ok'}

        with patch.object(budget, 'MODEL_LEASE_RENEW_SECONDS', .01), \
                patch.object(budget.ai_providers, 'complete', side_effect=complete):
            budget.model_call(self.first, provider, [], cancellation_event=cancelled)
        self.assertFalse(any(key.startswith('agent-model:renewed:') for key in self.first.store.read()['leases']))


if __name__ == '__main__':
    unittest.main()
