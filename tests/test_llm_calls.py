"""Per-request LLM ledger, per-purpose request policy, 402/429 waits and session routing."""
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ctn_data import agent_budget as budget
from ctn_data import agent_pipeline, agent_reading, fact_extraction, llm_calls
from ctn_data import ai_providers as ai
from ctn_data.db import connect, initialize

OPENROUTER = {'id': 'or-1', 'base_url': 'https://openrouter.ai/api/v1', 'model': 'deepseek/deepseek-v4.1-flash',
              'protocol': 'openai_chat', 'api_key': 'fake-key', 'token_parameter': 'max_tokens'}
ANSWER = {'choices': [{'message': {'content': 'Ready'}, 'finish_reason': 'stop'}], 'provider': 'DeepInfra',
          'usage': {'prompt_tokens': 1000, 'completion_tokens': 50, 'total_tokens': 1050,
                    'prompt_tokens_details': {'cached_tokens': 800, 'cache_write_tokens': 0},
                    'completion_tokens_details': {'reasoning_tokens': 30}, 'cost': 0.00042}}


class FakeEvent:
    """Cancellation event whose waits return at once and are recorded."""

    def __init__(self, cancel_on_wait=False):
        self.waits, self.cancel_on_wait = [], cancel_on_wait

    def is_set(self):
        return False

    def wait(self, delay):
        self.waits.append(delay)
        return self.cancel_on_wait


def rate_limited(delay=None):
    error = ai.ProviderError('AI 服务在途额度暂满，请稍后重试', 429, diagnostics={'error_code': 'in_flight_budget'})
    if delay is not None:
        error.retry_after = delay
    return error


class PurposeAndPolicyTests(unittest.TestCase):
    def test_purpose_is_inferred_from_each_step_contract(self):
        cases = (
            (agent_reading._schema(['post:1']), 'reading'),
            (agent_pipeline.WRITE_SCHEMA, 'write'),
            (agent_pipeline.CHECK_SCHEMA, 'check'),
            (fact_extraction._schema(), 'extract'),
            ({'required': ['action', 'body', 'reason'], 'properties': {'action': {}, 'body': {}}}, 'single'),
            ({'required': ['action', 'concern', 'fact_ids'], 'properties': {'action': {}}}, 'decide'),
            ({'required': ['name', 'stance'], 'properties': {}}, 'agent'),
            (None, 'agent'),
        )
        for schema, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(llm_calls.infer_purpose(schema), expected)

    def test_openrouter_policy_sets_cap_wait_budget_session_and_ledger(self):
        options = llm_calls.request_policy(OPENROUTER, 'write', db_path='db', agent_id='agent-1', job_id='job-1')
        self.assertEqual(options['max_tokens'], llm_calls.MAX_TOKENS['write'])
        self.assertEqual(options['retry_budget_seconds'], llm_calls.RETRY_BUDGET_SECONDS)
        self.assertEqual(options['session_id'], 'ctn-agent-1')
        self.assertEqual(options['ledger'], {'db_path': 'db', 'purpose': 'write', 'agent_id': 'agent-1',
                                             'job_id': 'job-1'})
        self.assertEqual(options['reasoning_effort'], 'low')
        self.assertEqual(llm_calls.request_policy(OPENROUTER, 'radar')['session_id'], 'ctn-radar')
        for purpose in ('decide', 'single', 'radar', 'research', 'interactive', 'agent'):
            self.assertNotIn('reasoning_effort', llm_calls.request_policy(OPENROUTER, purpose))
        for purpose in ('write', 'check', 'extract'):
            self.assertEqual(llm_calls.request_policy(OPENROUTER, purpose)['reasoning_effort'], 'low')
        self.assertEqual(llm_calls.request_policy(OPENROUTER, 'reading')['reasoning_effort'], 'none')
        self.assertEqual(llm_calls.request_policy(OPENROUTER, 'unknown-step')['max_tokens'],
                         llm_calls.MAX_TOKENS['agent'])

    def test_other_services_keep_transport_unchanged_and_only_record(self):
        for base_url in ('https://llm.legitreal.com/v1', 'https://api.x.ai/v1', 'https://openrouter.ai.example.com/v1'):
            with self.subTest(base_url=base_url):
                options = llm_calls.request_policy({'base_url': base_url}, 'decide', db_path='db')
                self.assertEqual(set(options), {'ledger'})
        self.assertEqual(llm_calls.request_policy({'model': 'mock'}, 'research'), {})


class WireTests(unittest.TestCase):
    def wire(self, **options):
        with patch.object(ai, '_request_json', return_value=dict(ANSWER)) as request:
            result = ai.complete(dict(OPENROUTER), [{'role': 'user', 'content': 'Test'}], **options)
        return result, json.loads(request.call_args.args[1])

    def test_cap_and_session_reach_the_openrouter_wire(self):
        result, wire = self.wire(max_tokens=6000, session_id='ctn-agent-1')
        self.assertEqual(wire['max_tokens'], 6000)
        self.assertEqual(wire['session_id'], 'ctn-agent-1')
        self.assertEqual(result['usage'], {'prompt_tokens': 1000, 'completion_tokens': 50, 'total_tokens': 1050,
                                           'reasoning_tokens': 30, 'cached_tokens': 800, 'cache_write_tokens': 0,
                                           'cost': 0.00042})
        _, wire = self.wire(max_output_tokens=42)
        self.assertNotIn('max_tokens', wire)
        self.assertNotIn('session_id', wire)

    def test_usage_sanitizing_is_idempotent_for_streamed_cache_counts(self):
        once = ai._safe_usage(ANSWER['usage'])
        self.assertEqual(ai._safe_usage(once), once)
        self.assertEqual((once['cached_tokens'], once['cost']), (800, 0.00042))
        stream = ai._SSECompletion()
        stream.feed(b'data: {"choices":[{"index":0,"delta":{"content":"Ready"},"finish_reason":"stop"}],'
                    b'"usage":{"prompt_tokens":820,"completion_tokens":22,"cost":0.00004,'
                    b'"prompt_tokens_details":{"cached_tokens":768}}}\n\ndata: [DONE]\n\n')
        with patch.object(ai, '_request_json', return_value=stream.result()):
            result = ai.complete(dict(OPENROUTER), [{'role': 'user', 'content': 'Test'}])
        self.assertEqual(result['usage']['cached_tokens'], 768)
        self.assertEqual(result['usage']['cost'], 0.00004)

    def test_session_is_openrouter_only_and_token_parameter_is_respected(self):
        provider = {**OPENROUTER, 'base_url': 'https://api.openai.com/v1', 'token_parameter': 'max_completion_tokens'}
        with patch.object(ai, '_request_json', return_value=dict(ANSWER)) as request:
            ai.complete(provider, [{'role': 'user', 'content': 'Test'}], max_tokens=500, session_id='ctn-x')
        wire = json.loads(request.call_args.args[1])
        self.assertEqual(wire['max_completion_tokens'], 500)
        self.assertNotIn('max_tokens', wire)
        self.assertNotIn('session_id', wire)

    def test_step_reasoning_effort_overrides_connection_mode_on_openrouter_only(self):
        for mode in ('auto', 'enabled'):
            with self.subTest(mode=mode), patch.object(ai, '_request_json', return_value=dict(ANSWER)) as request:
                ai.complete({**OPENROUTER, 'thinking_mode': mode}, [{'role': 'user', 'content': 'Test'}],
                            reasoning_effort='low')
            self.assertEqual(json.loads(request.call_args.args[1])['reasoning'], {'effort': 'low'})
        _, wire = self.wire(reasoning_effort='none')
        self.assertEqual(wire['reasoning'], {'effort': 'none'})
        _, wire = self.wire()
        self.assertNotIn('reasoning', wire)
        with patch.object(ai, '_request_json', return_value=dict(ANSWER)) as request:
            ai.complete({**OPENROUTER, 'base_url': 'https://api.deepseek.com/v1'},
                        [{'role': 'user', 'content': 'Test'}], reasoning_effort='low')
        wire = json.loads(request.call_args.args[1])
        self.assertNotIn('reasoning', wire)
        self.assertNotIn('reasoning_effort', wire)

    def test_invalid_policy_values_are_rejected_before_any_request(self):
        for options in ({'max_tokens': 0}, {'max_tokens': 1.5}, {'session_id': ''}, {'session_id': 'x' * 257},
                        {'session_id': 'bad\nid'}, {'retry_budget_seconds': -1}, {'retry_budget_seconds': 601},
                        {'reasoning_effort': 'max'}, {'reasoning_effort': 'LOW'}):
            with self.subTest(options=options), patch.object(ai, '_request_json') as request:
                with self.assertRaises(ValueError):
                    ai.complete(dict(OPENROUTER), [{'role': 'user', 'content': 'Test'}], **options)
                request.assert_not_called()

    def test_capped_truncation_names_the_cap_and_keeps_the_error_code(self):
        truncated = {'choices': [{'message': {'content': ''}, 'finish_reason': 'length'}]}
        with patch.object(ai, '_request_json', return_value=truncated), \
                self.assertRaises(ai.ProviderError) as caught:
            ai.complete(dict(OPENROUTER), [{'role': 'user', 'content': 'Test'}], max_tokens=6000)
        self.assertIn('6000', str(caught.exception))
        self.assertEqual(caught.exception.diagnostics['error_code'], 'output_truncated')


class PaymentRequiredTests(unittest.TestCase):
    def response(self, header):
        wire = ('HTTP/1.1 402 Payment Required\r\n' + header +
                'Content-Length: 30\r\n\r\nprivate upstream body fake-key').encode('ascii')

        class WireSocket:
            def settimeout(self, value):
                pass

            def sendall(self, value):
                pass

            def makefile(self, mode):
                return io.BytesIO(wire)

            def shutdown(self, how):
                pass

            def close(self):
                pass

        provider = {'base_url': 'http://127.0.0.1:1234/v1', 'model': 'test-model',
                    'protocol': 'openai_chat', 'api_key': 'fake-key'}
        with patch.object(ai, '_resolve_address', return_value='127.0.0.1'), \
                patch.object(ai.socket, 'create_connection', return_value=WireSocket()) as connected, \
                self.assertRaises(ai.ProviderError) as caught:
            ai.complete(provider, [{'role': 'user', 'content': 'Test'}])
        connected.assert_called_once()
        self.assertNotIn('private upstream', str(caught.exception))
        return caught.exception

    def test_in_flight_budget_402_becomes_a_timed_wait(self):
        error = self.response('Retry-After: 7\r\n')
        self.assertEqual(error.status_code, 429)
        self.assertEqual(error.retry_after, 7)
        self.assertEqual(error.diagnostics['error_code'], 'in_flight_budget')
        self.assertTrue(str(error).startswith('AI 服务'))

    def test_402_without_retry_after_means_credits_are_exhausted(self):
        error = self.response('')
        self.assertEqual(error.status_code, 402)
        self.assertFalse(hasattr(error, 'retry_after'))
        self.assertEqual(error.diagnostics['error_code'], 'payment_required')


class RetryTests(unittest.TestCase):
    def call(self, side_effect, *, budget_seconds=60, event=None):
        with patch.object(ai, '_request_json', side_effect=side_effect) as request:
            try:
                return ai.complete(dict(OPENROUTER), [{'role': 'user', 'content': 'Test'}],
                                   retry_budget_seconds=budget_seconds,
                                   cancellation_event=event or FakeEvent()), request
            except ai.ProviderError as exc:
                return exc, request

    def test_rejected_request_is_sent_again_after_retry_after(self):
        event = FakeEvent()
        result, request = self.call([rate_limited(7), dict(ANSWER)], event=event)
        self.assertEqual(result['text'], 'Ready')
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].args, request.call_args_list[1].args)
        self.assertEqual(event.waits, [7])

    def test_wait_has_a_floor_and_a_default(self):
        event = FakeEvent()
        result, _ = self.call([rate_limited(0), rate_limited(None), dict(ANSWER)], event=event)
        self.assertEqual(result['text'], 'Ready')
        self.assertEqual(event.waits, [1.0, 5.0])

    def test_no_retry_without_budget_beyond_budget_or_for_other_errors(self):
        cases = (
            ([rate_limited(7)], 0),
            ([rate_limited(61)], 60),
            ([rate_limited(30), rate_limited(31)], 60),
            ([ai.ProviderError('AI 服务账户额度不足（402），请检查服务商余额', 402)], 60),
            ([ai.ProviderError('AI 服务请求失败', 502)], 60),
        )
        for side_effect, seconds in cases:
            with self.subTest(statuses=[e.status_code for e in side_effect], budget=seconds):
                error, request = self.call(side_effect, budget_seconds=seconds)
                self.assertIsInstance(error, ai.ProviderError)
                self.assertEqual(request.call_count, len(side_effect))

    def test_at_most_three_attempts(self):
        error, request = self.call([rate_limited(1)] * 5)
        self.assertEqual(error.status_code, 429)
        self.assertEqual(request.call_count, 3)

    def test_broken_structured_output_is_requested_once_more(self):
        broken = {'choices': [{'message': {'content': '{"body": "cut'}, 'finish_reason': 'stop'}]}
        fixed = {'choices': [{'message': {'content': '```json\n{"body": "ok"}\n```'}, 'finish_reason': 'stop'}]}
        schema = {'type': 'object', 'required': ['body'], 'properties': {'body': {'type': 'string'}}}
        with patch.object(ai, '_request_json', side_effect=[broken, fixed]) as request:
            result = ai.complete(dict(OPENROUTER), [{'role': 'user', 'content': 'Test'}], response_schema=schema,
                                 retry_budget_seconds=60)
        self.assertIn('"ok"', result['text'])
        self.assertEqual(request.call_count, 2)
        with patch.object(ai, '_request_json', side_effect=[broken, broken]) as request, \
                self.assertRaises(ai.ProviderError) as caught:
            ai.complete(dict(OPENROUTER), [{'role': 'user', 'content': 'Test'}], response_schema=schema,
                        retry_budget_seconds=60)
        self.assertEqual(caught.exception.diagnostics['error_code'], 'invalid_json')
        self.assertEqual(request.call_count, 2)
        # Callers without the request policy keep validating the text themselves.
        with patch.object(ai, '_request_json', return_value=broken) as request:
            result = ai.complete(dict(OPENROUTER), [{'role': 'user', 'content': 'Test'}], response_schema=schema)
        self.assertEqual(result['text'], '{"body": "cut')
        request.assert_called_once()

    def test_cancellation_during_wait_stops_without_resending(self):
        error, request = self.call([rate_limited(7), dict(ANSWER)], event=FakeEvent(cancel_on_wait=True))
        self.assertEqual(error.status_code, 499)
        request.assert_called_once()


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'data.sqlite3'
        initialize(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def rows(self):
        conn = connect(self.db)
        try:
            return [dict(zip(row.keys(), tuple(row))) for row in conn.execute('SELECT * FROM llm_calls')]
        finally:
            conn.close()

    def ledger(self, purpose='write'):
        return {'db_path': str(self.db), 'purpose': purpose, 'agent_id': 'agent-1', 'job_id': 'job-1'}

    def test_success_row_has_usage_cache_cost_and_no_content(self):
        replies = [rate_limited(2), dict(ANSWER)]

        def request(*args, **kwargs):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            kwargs['on_progress']({'stage': 'thinking', 'elapsed_seconds': 1.25})
            kwargs['on_progress']({'stage': 'receiving', 'elapsed_seconds': 3.0})
            return reply
        with patch.object(ai, '_request_json', side_effect=request):
            ai.complete(dict(OPENROUTER), [{'role': 'user', 'content': 'private prompt'}], max_tokens=8000,
                        session_id='ctn-agent-1', retry_budget_seconds=60, cancellation_event=FakeEvent(),
                        ledger=self.ledger())
        [row] = self.rows()
        self.assertEqual((row['purpose'], row['agent_id'], row['job_id'], row['status']),
                         ('write', 'agent-1', 'job-1', 'ok'))
        self.assertEqual((row['provider_id'], row['model'], row['upstream']),
                         ('or-1', 'deepseek/deepseek-v4.1-flash', 'DeepInfra'))
        self.assertEqual((row['prompt_tokens'], row['cached_tokens'], row['completion_tokens'],
                          row['reasoning_tokens'], row['cost']), (1000, 800, 50, 30, 0.00042))
        self.assertEqual((row['attempts'], row['wait_ms'], row['ttft_ms'], row['max_tokens']), (2, 2000.0, 1250.0, 8000))
        self.assertEqual((row['http_status'], row['finish_reason'], row['error_code']), (200, 'stop', None))
        self.assertEqual(len(row['session_hash']), 16)
        self.assertNotIn('private prompt', json.dumps(row))
        self.assertNotIn('ctn-agent-1', json.dumps(row))

    def test_failure_rows_keep_the_code_and_token_counts(self):
        truncated = {'choices': [{'message': {'content': ''}, 'finish_reason': 'length'}],
                     'usage': {'prompt_tokens': 900, 'completion_tokens': 6000}}
        with patch.object(ai, '_request_json', return_value=truncated), self.assertRaises(ai.ProviderError):
            ai.complete(dict(OPENROUTER), [{'role': 'user', 'content': 'Test'}], max_tokens=6000,
                        ledger=self.ledger('check'))
        with patch.object(ai, '_request_json', side_effect=ai.ProviderError('AI 服务请求失败', 502)), \
                self.assertRaises(ai.ProviderError):
            ai.complete(dict(OPENROUTER), [{'role': 'user', 'content': 'Test'}], ledger=self.ledger('reading'))
        rows = {row['purpose']: row for row in self.rows()}
        self.assertEqual((rows['check']['status'], rows['check']['error_code'], rows['check']['completion_tokens']),
                         ('error', 'output_truncated', 6000))
        self.assertEqual((rows['reading']['http_status'], rows['reading']['error_code']), (502, 'provider_error'))

    def test_no_row_without_ledger_before_any_request_or_for_missing_database(self):
        with patch.object(ai, '_request_json', return_value=dict(ANSWER)):
            ai.complete(dict(OPENROUTER), [{'role': 'user', 'content': 'Test'}])
        with self.assertRaises(ValueError):
            ai.complete(dict(OPENROUTER), [], ledger=self.ledger())
        missing = Path(self.temp.name) / 'gone' / 'data.sqlite3'
        with patch.object(ai, '_request_json', return_value=dict(ANSWER)):
            ai.complete(dict(OPENROUTER), [{'role': 'user', 'content': 'Test'}],
                        ledger={**self.ledger(), 'db_path': str(missing)})
        self.assertEqual(self.rows(), [])
        self.assertFalse(missing.parent.exists())

    def test_storage_failure_never_changes_the_result(self):
        with patch.object(ai, '_request_json', return_value=dict(ANSWER)), \
                patch.object(llm_calls, 'connect', side_effect=RuntimeError('database down')):
            result = ai.complete(dict(OPENROUTER), [{'role': 'user', 'content': 'Test'}], ledger=self.ledger())
        self.assertEqual(result['text'], 'Ready')


class ModelCallTests(unittest.TestCase):
    def setUp(self):
        self.store = SimpleNamespace(data={'leases': {}}, calls=[])

        def change(callback):
            return callback(self.store.data, None)
        self.store.change = change
        self.controller = SimpleNamespace(store=self.store, clock=lambda: 1000.0, owner='owner', db_path='ledger-db')
        patch.object(budget, 'reserve').start()
        self.addCleanup(patch.stopall)

    def call(self, provider, **options):
        with patch.object(budget.ai_providers, 'complete', return_value={'text': '{}'}) as model:
            budget.model_call(self.controller, provider, [], cancellation_event=threading.Event(), **options)
        return model.call_args.kwargs

    def test_agent_steps_get_purpose_role_session_and_cap(self):
        options = self.call(OPENROUTER, response_schema=agent_pipeline.WRITE_SCHEMA,
                            trace={'agent_id': 'agent-1', 'job_id': 'job-1'})
        self.assertEqual(options['ledger'], {'db_path': 'ledger-db', 'purpose': 'write', 'agent_id': 'agent-1',
                                             'job_id': 'job-1'})
        self.assertEqual((options['max_tokens'], options['session_id'], options['retry_budget_seconds']),
                         (llm_calls.MAX_TOKENS['write'], 'ctn-agent-1', llm_calls.RETRY_BUDGET_SECONDS))
        self.assertEqual(options['reasoning_effort'], 'low')
        self.assertNotIn('trace', options)
        self.assertNotIn('max_output_tokens', options)

    def test_explicit_purpose_wins_and_other_services_are_only_recorded(self):
        options = self.call(OPENROUTER, response_schema=agent_pipeline.WRITE_SCHEMA, purpose='check')
        self.assertEqual(options['ledger']['purpose'], 'check')
        self.assertEqual(options['session_id'], 'ctn-check')
        options = self.call({'id': 'official', 'base_url': 'https://llm.legitreal.com/v1'},
                            response_schema=agent_pipeline.CHECK_SCHEMA)
        self.assertEqual(options['ledger']['purpose'], 'check')
        for key in ('max_tokens', 'session_id', 'retry_budget_seconds', 'response_schema', 'reasoning_effort'):
            self.assertNotIn(key, options)


if __name__ == '__main__':
    unittest.main()
