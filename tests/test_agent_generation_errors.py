"""Generation errors remain actionable without consuming observed sources.

All model responses and transport failures are synthetic; no external requests.
"""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ctn_data import ai_providers
from ctn_data.agent_control import AgentController, AgentError


class AgentGenerationErrors(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.provider = {'id': 'synthetic', 'protocol': 'openai_chat',
                         'base_url': 'https://example.invalid/v1', 'model': 'synthetic-model',
                         'api_key': 'PRIVATE-TEST-CREDENTIAL', 'max_output_tokens': 1800}
        self.controller = AgentController(Path(self.temp.name) / 'state.db', Path(self.temp.name) / 'keys',
            provider_store=SimpleNamespace(get=lambda _: self.provider), clock=lambda: 1792450000.0)
        self.controller.save({'name': 'Synthetic role', 'username': 'synthetic_role',
                              'provider_id': 'synthetic', 'language': 'English'})
        self.agent_id = self.controller.snapshot()['agents'][0]['id']
        self.sources = [{'id': 'post:7', 'post_id': 7, 'text': 'A recent fictional discussion.',
                         'version': 'fresh-version', 'notification_ids': [101]}]
        def initial(data, conn):
            data['memory'][self.agent_id] = {
                'summary': 'Existing personal memory.', 'last_sources': self.sources,
                'decision_versions': {'post:old': 'old-version'}, 'decision_notification_ids': [90],
                'inbox': [{'id': 101, 'post_id': 7, 'status': 'unresolved'}]}
        self.controller.store.change(initial)
        self.original_memory = copy.deepcopy(self.controller.store.read()['memory'][self.agent_id])
        self.addCleanup(patch.stopall)
        patch('ctn_data.agent_observation.observe', return_value=self.sources).start()
        patch('ctn_data.agent_budget.reserve').start()
        self.request = patch('ctn_data.ai_providers._request_json').start()

    @staticmethod
    def response(content, finish='stop', **message):
        return {'choices': [{'finish_reason': finish, 'message': {'content': content, **message}}]}

    def assert_failure(self, message, status):
        with self.assertRaisesRegex(AgentError, message) as caught:
            self.controller.run(self.agent_id)
        self.assertEqual(caught.exception.status_code, status)
        state = self.controller.store.read()
        self.assertEqual(state['memory'][self.agent_id], self.original_memory)
        self.assertEqual(state['drafts'], {})
        if status == 429:
            self.assertEqual(state['agents'][self.agent_id]['last_error'], '')
            self.assertEqual(state['events'][-1]['kind'], 'waiting')
            self.assertGreater(state['agents'][self.agent_id]['decision_retry_at'], self.controller.clock())
        else:
            self.assertEqual(state['agents'][self.agent_id]['last_error'], str(caught.exception))
            self.assertEqual(state['events'][-1]['kind'], 'error')
        self.assertNotIn('PRIVATE-', json.dumps(state, ensure_ascii=False))
        return caught.exception

    def test_transport_timeout_keeps_specific_message_and_504(self):
        self.request.side_effect = TimeoutError('PRIVATE-TEST-CREDENTIAL')
        self.assert_failure('AI 服务响应超时', 504)
        self.request.assert_called_once()

    def test_provider_rate_limit_keeps_specific_message_and_429(self):
        self.request.side_effect = ai_providers.ProviderError('AI 服务限流或额度不足，请稍后重试', 429)
        self.assert_failure('AI 服务限流或额度不足', 429)
        self.request.assert_called_once()

    def test_truncated_output_is_budget_error_instead_of_json_error(self):
        self.request.return_value = self.response('{"action":"post","body":"unfinished', finish='length',
            reasoning_content='PRIVATE-REASONING-TRACE')
        error = self.assert_failure('AI 服务的输出预算已耗尽', 502)
        self.assertNotIn('JSON', str(error))
        self.assertEqual(self.request.call_count, 2)
        self.assertEqual(error.diagnostics['error_code'], 'output_truncated')

    def test_reasoning_only_response_explains_missing_final_answer(self):
        self.request.return_value = self.response(None, reasoning_content='PRIVATE-REASONING-TRACE')
        error = self.assert_failure('只返回了推理过程', 502)
        self.assertNotIn('JSON', str(error))
        self.request.assert_called_once()
        self.assertEqual(error.diagnostics['error_code'], 'reasoning_only')

    def test_non_json_final_text_has_distinct_json_error(self):
        self.request.return_value = self.response('PRIVATE-RAW-RESPONSE is not valid JSON')
        self.assert_failure('已返回文字，但不是完整有效的 JSON', 502)
        self.request.assert_called_once()

    def test_invalid_body_explains_length_type_or_empty_text(self):
        for value, reason in ((None, '正文字段不是文字'), ('x' * 501, '501 字符'), ('  ', '正文为空')):
            with self.subTest(reason=reason):
                self.request.return_value = self.response(json.dumps({'action': 'post', 'body': value}))
                self.assert_failure(reason, 502)

    def test_unexpected_error_does_not_leak_exception_or_claim_json_failure(self):
        self.request.side_effect = RuntimeError('PRIVATE-RAW-RESPONSE PRIVATE-TEST-CREDENTIAL')
        error = self.assert_failure('AI 写作请求未完成', 502)
        self.assertNotIn('JSON', str(error))
        self.request.assert_called_once()

    def test_provider_configuration_error_keeps_safe_message_and_400(self):
        self.controller.providers = SimpleNamespace(get=lambda _: self.missing_provider())
        self.assert_failure('该 AI 连接尚未配置 API Key', 400)
        self.request.assert_not_called()

    @staticmethod
    def missing_provider():
        raise ai_providers.ProviderError('该 AI 连接尚未配置 API Key', 400)

    def test_valid_fenced_json_still_saves_one_review_action(self):
        output = {'action': 'reply', 'body': 'A considered fictional reply.', 'target_post_id': 7,
                  'source_ids': ['post:7'], 'notification_ids': [101],
                  'memory': 'I prepared a reply.', 'reason': 'Relevant discussion.'}
        self.request.return_value = self.response('```json\n' + json.dumps(output) + '\n```')
        result = self.controller.run(self.agent_id)
        self.assertEqual(len(result['drafts']), 1)
        self.assertEqual(result['drafts'][0]['state'], 'pending')
        memory = self.controller.store.read()['memory'][self.agent_id]
        self.assertEqual(memory['decision_versions']['post:7'], 'fresh-version')
        self.assertEqual(memory['inbox'][0]['status'], 'unresolved')
        self.assertEqual(memory['summary'], 'Existing personal memory.')
        self.assertEqual(memory['episodes']['decisions'][0]['reflection'], 'I prepared a reply.')
        self.request.assert_called_once()


if __name__ == '__main__':
    unittest.main()
