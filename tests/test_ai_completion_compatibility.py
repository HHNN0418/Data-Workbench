"""Regression cases for unrestricted thinking models and final-answer extraction.

Every response is synthetic. These tests make no external model requests.
"""
import json
import unittest
from unittest.mock import patch

from ctn_data import ai_providers as ai


class CompletionCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.provider = {'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash',
                         'protocol': 'openai_chat', 'api_key': 'test-private-api-key',
                         'token_parameter': 'max_tokens'}
        self.messages = [{'role': 'user', 'content': 'Reply with OK.'}]

    @staticmethod
    def response(content='OK', finish='stop', **message_fields):
        return {'choices': [{'finish_reason': finish,
                             'message': {'content': content, **message_fields}}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 12, 'total_tokens': 22}}

    def invoke(self, response, provider=None, **options):
        with patch.object(ai, '_request_json', return_value=response) as request:
            result = ai.complete(provider or self.provider, self.messages, **options)
        request.assert_called_once()
        return result, json.loads(request.call_args.args[1])

    def assert_safe_failure(self, response, expected):
        with patch.object(ai, '_request_json', return_value=response) as request:
            with self.assertRaises(ai.ProviderError) as caught:
                ai.complete(self.provider, self.messages)
        request.assert_called_once()
        message = str(caught.exception)
        self.assertRegex(message, expected)
        self.assertNotIn('test-private-api-key', message)
        self.assertNotIn('PRIVATE-REASONING-TRACE', message)
        self.assertNotIn('PRIVATE-RAW-RESPONSE', message)
        return caught.exception

    def test_legacy_provider_and_call_output_budgets_are_never_sent(self):
        provider = {**self.provider, 'max_output_tokens': 4096}
        result, body = self.invoke(self.response(), provider)
        self.assertNotIn('max_tokens', body)
        self.assertNotIn('max_completion_tokens', body)
        self.assertEqual(result, {'text': 'OK', 'model': 'deepseek-flash',
                                  'usage': {'prompt_tokens': 10, 'completion_tokens': 12, 'total_tokens': 22}})
        _, body = self.invoke(self.response(), provider, max_output_tokens=512)
        self.assertNotIn('max_tokens', body)
        _, body = self.invoke(self.response())
        self.assertNotIn('max_tokens', body)

    def test_auto_thinking_preserves_model_defaults_including_deepseek(self):
        for base_url in ('https://api.deepseek.com', 'https://api.deepseek.com/v1'):
            with self.subTest(base_url=base_url):
                _, body = self.invoke(self.response(), {**self.provider, 'base_url': base_url, 'thinking_mode': 'auto'})
                self.assertNotIn('thinking', body)
        for base_url in ('https://api.openai.com/v1', 'https://example.com/v1',
                         'https://api.deepseek.com.example.com/v1'):
            with self.subTest(base_url=base_url):
                _, body = self.invoke(self.response(), {**self.provider, 'base_url': base_url, 'thinking_mode': 'auto'})
                self.assertNotIn('thinking', body)
                self.assertNotIn('reasoning_effort', body)

    def test_explicit_enabled_and_legacy_disabled_both_allow_thinking(self):
        for mode in ('enabled', 'disabled'):
            with self.subTest(mode=mode):
                _, body = self.invoke(self.response(), {**self.provider, 'thinking_mode': mode})
                if mode == 'enabled':
                    self.assertEqual(body['thinking'], {'type': 'enabled'})
                else:
                    self.assertNotIn('thinking', body)

    def test_final_text_blocks_are_read_and_reasoning_is_never_used_as_answer(self):
        response = self.response(content=[
            {'type': 'reasoning', 'text': 'PRIVATE-REASONING-TRACE'},
            {'type': 'text', 'text': '第一段'},
            {'type': 'output_text', 'text': '第二段'},
        ], reasoning_content='PRIVATE-REASONING-TRACE')
        result, _ = self.invoke(response)
        self.assertEqual(result['text'].replace('\n', ''), '第一段第二段')
        self.assertNotIn('PRIVATE-REASONING-TRACE', json.dumps(result))
        self.assert_safe_failure(self.response(content=None, reasoning_content='PRIVATE-REASONING-TRACE'),
                                 r'最终|文字|正文|内容')

    def test_length_exhaustion_rejects_both_empty_and_partial_final_answers_without_retry(self):
        for content in (None, '', 'A truncated but visible answer'):
            with self.subTest(content=content):
                error = self.assert_safe_failure(self.response(content=content, finish='length',
                    reasoning_content='PRIVATE-REASONING-TRACE'), r'预算|上限|长度')
                self.assertNotIn('请检查模型是否支持该协议', str(error))

    def test_content_filter_and_refusal_are_specific_and_do_not_echo_upstream_text(self):
        self.assert_safe_failure(self.response(content=None, finish='content_filter'), r'过滤|拒绝')
        self.assert_safe_failure(self.response(content=None, refusal='PRIVATE-RAW-RESPONSE test-private-api-key'),
                                 r'拒绝|拒答|过滤')

    def test_tool_calls_and_interrupted_generations_are_not_successful_answers(self):
        self.assert_safe_failure(self.response(content=None, finish='tool_calls',
            tool_calls=[{'id': 'private-call', 'function': {'name': 'PRIVATE-RAW-RESPONSE'}}]), r'工具')
        for finish in ('insufficient_system_resource', 'aborted'):
            with self.subTest(finish=finish):
                self.assert_safe_failure(self.response(content=None, finish=finish,
                    reasoning_content='PRIVATE-REASONING-TRACE'), r'中断|终止|资源')

    def test_malformed_choices_fail_safely_once_and_final_text_still_redacts_credentials(self):
        for response in ({}, {'choices': []}, {'choices': None}, {'choices': ['PRIVATE-RAW-RESPONSE']},
                         {'choices': [{'message': None}]}, {'choices': [{'message': {'content': 42}}]}):
            with self.subTest(response=response):
                self.assert_safe_failure(response, r'响应|格式|文字|内容')
        result, _ = self.invoke(self.response(content='Answer test-private-api-key',
                                              reasoning_content='PRIVATE-REASONING-TRACE'))
        self.assertEqual(result['text'], 'Answer [REDACTED]')
        self.assertNotIn('PRIVATE-REASONING-TRACE', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
