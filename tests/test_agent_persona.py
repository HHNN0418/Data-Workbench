"""Persona previews use synthetic models and never contact the social platform."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from ctn_data import ai_providers
from ctn_data.agent_control import AgentController, AgentError
from ctn_data.db import connect
from ctn_data.server import create_app
from tests.auth_helpers import login, completed_job


PERSONA = {'name': 'Maya Collins', 'username': 'maya_collins',
           'background': (
               'Kingstonia 虚构人物设定。34岁的 Maya 在 Kingstonia City 图书馆工作，租住在父母附近。'
               '父亲曾在旅游业谋生，疫情时期的收入起伏让她从小额支出开始记录家庭账本。'
               '大学后她先做社区活动助理，后来为了稳定收入进入图书馆；一次活动取消让她学会先核对安排再许诺。'
               '她每周帮父母采购，常在下班后修补旧书，也会担心父亲看病时的等候时间。'
               '她支持有免费获取渠道和医学豁免的疫苗凭证，但在乎轮班工人的实际便利。'
               '她容易把自己的谨慎误当成所有人的需要，不擅长解释医学数据；遇到可信的新资料会修正细节。'),
           'stance': '支持 Vote Yes，关心医疗承受力与执行便利；需要清楚的豁免安排和证据。',
           'voice': '用简短具体的中文表达疑问，先理解对方处境再提出证据；不重复口号。',
           'interests': ['社区图书馆', '旧书修补', '公共医疗', '公交通勤'], 'language': 'English'}


class PersonaGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = 1792450000.0
        self.provider = {'id': 'synthetic', 'protocol': 'openai_chat', 'model': 'synthetic-model',
                         'base_url': 'https://example.invalid/v1', 'api_key': 'PRIVATE-TEST-KEY',
                         'max_output_tokens': 1800}
        self.get_provider = Mock(side_effect=lambda _: dict(self.provider))
        self.sdk = Mock(side_effect=AssertionError('Persona generation must not open a platform session'))
        self.controller = AgentController(self.root / 'state.db', self.root / 'keys',
            provider_store=SimpleNamespace(get=self.get_provider), sdk_factory=self.sdk, clock=lambda: self.now)
        self.complete = patch('ctn_data.ai_providers.complete', return_value=self.answer()).start()
        self.addCleanup(patch.stopall)

    @staticmethod
    def answer(persona=None):
        return {'text': json.dumps(persona or PERSONA, ensure_ascii=False),
                'model': 'synthetic-model', 'usage': {'total_tokens': 1000, 'invalid': 100}}

    def generate(self, **payload):
        return self.controller.generate_persona(payload)

    def saved_agent(self):
        self.controller.save({**PERSONA, 'provider_id': 'existing-provider'})
        return next(iter(self.controller.store.read()['agents']))

    def assert_no_business_changes(self, before):
        after = self.controller.store.read()
        for field in ('agents', 'settings', 'memory', 'drafts', 'events'):
            self.assertEqual(before[field], after[field], field)
        self.sdk.assert_not_called()

    def test_empty_brief_can_preview_complete_saveable_persona_without_persisting_it(self):
        before = self.controller.store.read()
        result = self.generate()
        self.assertEqual(result, {'persona': PERSONA, 'provider_id': 'synthetic',
                                 'model': 'synthetic-model', 'usage': {'total_tokens': 1000}})
        self.get_provider.assert_called_once_with('')
        self.complete.assert_called_once()
        self.assert_no_business_changes(before)
        self.assertNotIn('agent-model:synthetic', self.controller.store.read()['leases'])
        # The returned fields can be handed to the existing save contract.
        self.controller.save(result['persona'])
        self.assertEqual(len(self.controller.store.read()['agents']), 1)

    def test_existing_agent_uses_unsaved_editable_persona_without_exposing_memory(self):
        agent_id = self.saved_agent()
        self.controller.store.change(lambda data, _: data['memory'].update({agent_id: {'summary': 'PRIVATE-MEMORY'}}))
        before = copy.deepcopy(self.controller.store.read())
        self.generate(existing_agent_id=agent_id, brief='保留年龄职业，丰富家庭与经历',
                      persona={'background': '34岁图书馆员工，在父母附近居住。', 'interests': '旧书，公共医疗'})
        self.get_provider.assert_called_once_with('existing-provider')
        messages = self.complete.call_args.args[1]
        context = json.loads(messages[1]['content'])
        self.assertEqual(context['current_persona']['background'], '34岁图书馆员工，在父母附近居住。')
        self.assertEqual(context['current_persona']['interests'], ['旧书', '公共医疗'])
        self.assertEqual(context['current_persona']['name'], PERSONA['name'])
        self.assertEqual(context['current_persona']['language'], 'English')
        self.assertNotIn('PRIVATE-', json.dumps(messages))
        self.assert_no_business_changes(before)

    def test_explicit_provider_overrides_saved_provider(self):
        self.generate(existing_agent_id=self.saved_agent(), provider_id='chosen')
        self.get_provider.assert_called_once_with('chosen')

    def test_registered_or_uncertain_identity_keeps_username(self):
        agent_id = self.saved_agent()
        changed = {**PERSONA, 'username': 'different_user'}
        self.complete.return_value = self.answer(changed)
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                def identity(data, _):
                    row = data['agents'][agent_id]
                    if legacy:
                        row['account_id'] = 42
                        row.pop('account_state', None)
                    else:
                        row['account_state'] = 'uncertain'
                self.controller.store.change(identity)
                self.now += 2
                result = self.generate(existing_agent_id=agent_id, persona={'username': 'new_from_form'})
                self.assertEqual(result['persona']['username'], PERSONA['username'])
                context = json.loads(self.complete.call_args.args[1][1]['content'])
                self.assertEqual(context['locked_username'], PERSONA['username'])
                self.assertEqual(context['current_persona']['username'], PERSONA['username'])

    def test_world_setting_and_biography_memory_boundaries_are_explicit(self):
        self.generate()
        messages = self.complete.call_args.args[1]
        system = messages[0]['content']
        for constraint in ('Kingstonia', 'H2D2', '免费', '医学豁免', '尚无持续本土社区传播',
                           '实际记忆', '平台账号互动', '年龄', '职业', '发言语言', '默认用中文',
                           '完整的人设 JSON', '知识边界'):
            self.assertIn(constraint, system)
        self.assertIn('/2026/setting/', messages[1]['content'])

    def test_official_provider_uses_compact_prompt_cap_and_shared_pacing(self):
        self.provider.update(base_url='https://llm.legitreal.com/v1', max_output_tokens=8192)
        self.generate()
        self.assertNotIn('max_output_tokens', self.complete.call_args.kwargs)
        self.assertNotIn('输出额度', self.complete.call_args.args[1][0]['content'])
        self.provider['id'] = 'another-official-name'
        with self.assertRaises(AgentError) as caught:
            self.generate()
        self.assertEqual(caught.exception.status_code, 429)
        self.assertGreater(caught.exception.retry_after, 0)
        self.complete.assert_called_once()

    def test_third_party_persona_generation_can_overlap_without_a_competition_lease(self):
        def completion(*args, **kwargs):
            with patch('ctn_data.ai_providers.complete', return_value=self.answer()) as parallel:
                self.assertEqual(self.generate()['persona'], PERSONA)
                parallel.assert_called_once()
            # The synthetic network step can obtain an independent DB writer.
            conn = connect(self.controller.db_path)
            try:
                conn.execute('BEGIN IMMEDIATE')
                conn.rollback()
            finally:
                conn.close()
            return self.answer()
        self.complete.side_effect = completion
        self.generate()
        self.complete.assert_called_once()
        self.assertEqual(self.controller.store.read()['leases'], {})

    def test_grok_waits_for_completion_and_keeps_structured_schema(self):
        self.provider.update(base_url='https://api.x.ai/v1', model='grok-4.7')
        self.generate()
        options = self.complete.call_args.kwargs
        self.assertTrue(options['wait_for_completion'])
        self.assertNotIn('timeout_seconds', options)
        self.assertNotIn('reasoning_effort', options)
        self.assertFalse(options['response_schema']['additionalProperties'])
        self.assertEqual(set(options['response_schema']['required']), set(PERSONA))

    def test_provider_configuration_failure_stops_before_model_request(self):
        self.get_provider.side_effect = ai_providers.ProviderError('该 AI 连接尚未配置 API Key', 400)
        with self.assertRaisesRegex(AgentError, '尚未配置 API Key') as caught:
            self.generate()
        self.assertEqual(caught.exception.status_code, 400)
        self.complete.assert_not_called()

    def test_non_chat_provider_is_rejected(self):
        self.provider['protocol'] = 'openai_embeddings'
        with self.assertRaisesRegex(AgentError, '文字模型连接'):
            self.generate()
        self.complete.assert_not_called()

    def test_old_output_budget_does_not_limit_or_prevent_generation(self):
        self.provider['max_output_tokens'] = 256
        before = copy.deepcopy(self.controller.store.read())
        self.assertEqual(self.generate(persona={'name': 'Maya Collins'})['persona'], PERSONA)
        self.assertNotIn('max_output_tokens', self.complete.call_args.kwargs)
        self.assert_no_business_changes(before)

    def test_timeout_provider_error_keeps_status_and_does_not_persist_profile(self):
        before = self.controller.store.read()
        self.complete.side_effect = ai_providers.ProviderError('AI 服务响应超时，请稍后重试', 504)
        with self.assertRaisesRegex(AgentError, '响应超时') as caught:
            self.generate()
        self.assertEqual(caught.exception.status_code, 504)
        self.assert_no_business_changes(before)
        self.assertEqual(self.controller.store.read()['leases'], {})

    def test_provider_rate_limit_cools_down_future_generation(self):
        self.complete.side_effect = ai_providers.ProviderError('AI 服务限流或额度不足，请稍后重试', 429)
        with self.assertRaises(AgentError) as first:
            self.generate()
        self.assertEqual(first.exception.status_code, 429)
        self.now += 2
        with self.assertRaises(AgentError) as second:
            self.generate()
        self.assertEqual(second.exception.status_code, 429)
        self.complete.assert_called_once()

    def test_unexpected_model_failure_hides_raw_details_and_releases_lease(self):
        self.complete.side_effect = RuntimeError('PRIVATE-TEST-KEY PRIVATE-RESPONSE')
        with self.assertRaisesRegex(AgentError, '人设生成请求未完成') as caught:
            self.generate()
        self.assertEqual(caught.exception.status_code, 502)
        self.assertNotIn('PRIVATE-', str(caught.exception))
        self.assertEqual(self.controller.store.read()['leases'], {})

    def test_fenced_json_is_accepted(self):
        self.complete.return_value['text'] = '```json\n' + json.dumps(PERSONA) + '\n```'
        self.assertEqual(self.generate()['persona'], PERSONA)

    def test_malformed_json_and_bad_response_shape_are_502(self):
        for result in ({'text': 'PRIVATE-RESPONSE not JSON'}, {'text': None}, None):
            with self.subTest(result=result):
                self.now += 2
                self.complete.return_value = result
                with self.assertRaises(AgentError) as caught:
                    self.generate()
                self.assertEqual(caught.exception.status_code, 502)
                self.assertNotIn('PRIVATE-', str(caught.exception))

    def test_strict_output_validation_rejects_incomplete_or_unsaveable_personas(self):
        missing = dict(PERSONA)
        missing.pop('voice')
        cases = [missing, [], {**PERSONA, 'memory': 'invented memory'},
                 {**PERSONA, 'name': ''}, {**PERSONA, 'name': 1}, {**PERSONA, 'name': 'x' * 81},
                 {**PERSONA, 'username': 'not a valid name'}, {**PERSONA, 'background': 'too simple'},
                 {**PERSONA, 'voice': 'control\x00character'}, {**PERSONA, 'interests': 'comma,separated'},
                 {**PERSONA, 'interests': []}, {**PERSONA, 'interests': ['x'] * 21},
                 {**PERSONA, 'interests': ['x' * 101]}, {**PERSONA, 'interests': [False]}]
        for output in cases:
            with self.subTest(output=output):
                self.now += 2
                self.complete.return_value = {'text': json.dumps(output)}
                with self.assertRaises(AgentError) as caught:
                    self.generate()
                self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(self.controller.store.read()['agents'], {})

    def test_bad_input_stops_before_model(self):
        for payload in ({'brief': 'x' * 2001}, {'brief': 4}, {'extra': True},
                        {'persona': {'enabled': True}}, {'persona': {'interests': [3]}},
                        {'existing_agent_id': 'invalid'}, {'persona': {'background': 'x' * 3001}}):
            with self.subTest(payload=payload), self.assertRaises(AgentError):
                self.controller.generate_persona(payload)
        self.complete.assert_not_called()


class PersonaRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.app = create_app(root / 'routes.db', backup_dir=root / 'backups')
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')
        self.addCleanup(self.client.close)
        self.generate = patch.object(self.app.state.agent_control, 'generate_persona',
                                     return_value={'persona': PERSONA, 'provider_id': 'synthetic'}).start()
        self.addCleanup(patch.stopall)

    def post(self, payload, token=None):
        return completed_job(self.client, self.client.post('/api/agents/generate-persona', json=payload,
                                headers={'X-CTN-Token': token} if token else {}))

    def test_requires_workbench_login_and_csrf(self):
        token = self.client.get('/api/bootstrap').json()['csrf_token']
        self.assertEqual(self.post({}, token).status_code, 401)
        token = login(self.client)
        self.assertEqual(self.post({}).status_code, 403)
        response = self.post({'brief': '图书馆员工', 'persona': {'name': 'Maya'}}, token)
        self.assertEqual(response.status_code, 200, response.text)
        self.generate.assert_called_once()
        self.assertEqual(self.generate.call_args.args[0], {'brief': '图书馆员工', 'persona': {'name': 'Maya'}})
        self.assertIn('cancellation_event', self.generate.call_args.kwargs)

    def test_request_rejects_unknown_fields_bad_types_and_oversized_values(self):
        token = login(self.client)
        for payload in ({'brief': 'x' * 2001}, {'brief': 7}, {'provider_id': False}, {'enabled': True},
                        {'persona': {'id': 'hidden'}}, {'persona': {'background': 'x' * 3001}},
                        {'persona': {'interests': [3]}}, {'persona': {'interests': ['x'] * 21}}):
            with self.subTest(payload=payload):
                self.assertEqual(self.post(payload, token).status_code, 400)
        self.generate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
