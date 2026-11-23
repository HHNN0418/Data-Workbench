"""Offline selection -> coherent observation -> decision regressions."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ctn_data import agent_observation, agent_memory, ai_providers, agent_reading, agent_audit
from ctn_data.agent_control import AgentController, AgentError
from ctn_data.db import connect
from ctn_data.ingest import save_posts


class ReadingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1792450000.0
        self.provider = {'id': 'fake', 'protocol': 'openai_chat', 'model': 'fake',
                         'base_url': 'https://example.invalid/v1', 'max_output_tokens': 1800}
        root = Path(self.temp.name)
        self.controller = AgentController(root / 'test.db', root / 'keys',
            provider_store=SimpleNamespace(get=lambda _: self.provider), clock=lambda: self.now,
            sdk_factory=lambda **kw: self.fail('No SDK is needed for these local observations'))
        self.addCleanup(self.controller.stop)
        self.controller.save({'name': '本地居民', 'username': 'reader',
                              'interests': ['住房与公共交通'], 'provider_id': 'fake'})
        self.agent_id = self.controller.snapshot()['agents'][0]['id']

    def agent(self):
        return self.controller.store.read()['agents'][self.agent_id]

    def memory(self):
        return self.controller.store.read()['memory'].get(self.agent_id, {})

    def post(self, post_id, body, age=0, **extra):
        raw = {'post_id': post_id, 'author_id': 90, 'kind': 'post', 'body': body,
               'visibility': 'public', 'created_at': agent_observation._at(self.now - age), **extra}
        conn = connect(self.controller.db_path)
        try:
            with conn:
                save_posts(conn, [raw], 'trending', observed_at=agent_observation._at(self.now))
        finally:
            conn.close()

    def observe(self, for_decision=True):
        with self.controller._claim(self.agent_id) as token:
            return agent_observation.observe(self.controller, self.agent(), token,
                                             refresh=False, for_decision=for_decision)

    def independent_providers(self):
        store = ai_providers.ProviderStore(self.controller.db_path, Path(self.temp.name) / 'selection-provider-keys')
        def add(name):
            result = store.save({'name': name, 'model': name.lower() + '-model', 'protocol': 'openai_chat',
                'base_url': 'https://example.invalid/v1', 'api_key': 'private-key-for-' + name})
            return next(row for row in result['providers'] if row['name'] == name)
        decision, reading = add('Decision'), add('Reading')
        self.controller.providers = store
        self.controller.save({'id': self.agent_id, 'name': self.agent()['name'], 'provider_id': decision['id']})
        return store, decision, reading

    def configure_reading(self, provider_id):
        revision = self.controller.reading_selection()['settings']['revision']
        return self.controller.reading_selection({'provider_id': provider_id, 'expected_revision': revision})

    @staticmethod
    def selection(ids):
        return {'text': json.dumps({'source_ids': ids, 'reason': '与住房兴趣有关'})}

    @staticmethod
    def decision():
        return {'text': json.dumps({'action': 'post', 'body': 'Housing and transport deserve attention.',
                                   'source_ids': ['post:3'], 'notification_ids': [], 'reason': 'A relevant topic.'})}

    def test_chinese_persona_reaches_selector_with_english_sources_and_required_context(self):
        self.post(1, 'Original proposal: affordable housing near transit.', age=86400)
        self.post(2, 'Would this rent policy cover new residents?', age=7200,
                  kind='reply', root_post_id=1, reply_to_post_id=1)
        self.post(3, 'Yes, the proposal includes new residents.', kind='reply',
                  root_post_id=1, reply_to_post_id=2)
        for number in range(4, 30):
            self.post(number, 'An unrelated sports result. ' * 90, age=number)
        with patch('ctn_data.agent_budget.model_call', return_value=self.selection(['post:3'])) as model:
            sources = self.observe()
            prompt = json.loads(model.call_args.args[2][1]['content'])
            self.assertEqual(prompt['persona']['interests'], ['住房与公共交通'])
            self.assertIn('post:3', {row['id'] for row in prompt['directory']})
            self.assertEqual({row['id'] for row in sources}, {'post:1', 'post:2', 'post:3'})
            self.assertTrue(next(row for row in sources if row['id'] == 'post:3')['thread_context']['complete'])
            self.assertEqual(sum(bool(row.get('context_only')) for row in sources), 2)
            self.assertLessEqual(len(json.dumps(sources, ensure_ascii=False)), 12000)
            self.assertEqual(set(self.memory()['read_versions']), {'post:1', 'post:2', 'post:3'})
            self.assertFalse(self.memory().get('decision_versions'))
            self.assertEqual(self.memory()['observation']['new_source_count'], 1)
            # Recording this read must not invalidate the selector's own cache.
            self.observe()
            self.assertEqual(model.call_count, 1)
            self.assertTrue(self.memory()['observation']['reading_selection']['cache_hit'])
            self.assertTrue(self.controller.store.read()['events'][-1]['message'].startswith('复用此前'))

    def test_empty_selection_does_not_consume_notices_or_record_preview_as_read(self):
        self.post(3, 'A question I do not need to answer.')
        def prepare(data, conn):
            data['memory'][self.agent_id] = {'inbox': [
                {'id': 10, 'post_id': 3, 'status': 'unresolved'}]}
        self.controller.store.change(prepare)
        with patch('ctn_data.agent_budget.model_call', return_value=self.selection([])) as model:
            self.controller._run(self.agent_id)
            self.controller._run(self.agent_id)
            model.assert_called_once()
        own = self.memory()
        self.assertEqual(own['inbox'][0]['status'], 'unresolved')
        self.assertFalse(own.get('read_ids'))
        self.assertFalse(own.get('read_notification_ids'))
        self.assertFalse(own.get('decision_notification_ids'))
        self.assertFalse(self.controller.snapshot()['drafts'])
        self.assertEqual(self.agent()['last_error'], '')
        self.assertEqual(own['observation']['reading_selection']['status'], 'empty')
        self.assertTrue(any('没有需要深入阅读' in row['message']
                            for row in self.controller.store.read()['events']))

    def test_fallback_reading_survives_decision_failure_and_recovers_next_selection(self):
        self.post(3, 'An English housing proposal for the referendum.')
        with patch('ctn_data.agent_budget.model_call', side_effect=[
                {'text': 'invalid-json'}, ai_providers.ProviderError('Synthetic decision failure', 502)]) as model:
            with self.assertRaisesRegex(AgentError, 'Synthetic decision failure'):
                self.controller._run(self.agent_id)
            self.assertEqual(model.call_count, 2)
        own = self.memory()
        self.assertEqual(own['observation']['reading_selection']['status'], 'fallback')
        self.assertIn('post:3', own['read_versions'])
        self.assertFalse(own.get('decision_versions'))
        self.assertFalse(self.controller.snapshot()['drafts'])
        self.controller.store.change(lambda data, conn: data['agents'][self.agent_id].update(
            decision_retry_at=self.now + 600))
        self.now += agent_reading.FALLBACK_RETRY_SECONDS + 1
        self.post(4, 'A new housing and transit proposal worth reading.')
        recovered = json.loads(self.decision()['text'])
        recovered['source_ids'] = ['post:4']
        with patch('ctn_data.agent_budget.model_call', side_effect=[
                self.selection(['post:4']), {'text': json.dumps(recovered)}]) as model:
            self.controller._run(self.agent_id)
            self.assertEqual(model.call_count, 2)
        own = self.memory()
        self.assertEqual(own['observation']['reading_selection']['status'], 'selected')
        self.assertFalse(own['observation']['reading_selection']['cache_hit'])
        self.assertNotIn('error', own['observation']['reading_selection'])
        self.assertIn('post:4', own['decision_versions'])
        self.assertEqual(self.agent()['decision_retry_at'], 0)
        self.assertEqual(len(self.controller.snapshot()['drafts']), 1)
        calls = self.controller.reading_selection()['recent_calls']
        self.assertEqual([row['status'] for row in calls], ['selected', 'fallback'])
        self.assertEqual(calls[1]['error_stage'], 'validation')
        self.assertEqual(calls[1]['error_detail'], 'invalid_json')
        conn = connect(self.controller.db_path)
        try:
            audit = agent_audit.query(conn, self.agent_id, kind='reading_selection')['items']
            failed = next(row for row in audit if row['details'].get('selection_status') == 'fallback')
            self.assertEqual(failed['details']['error_detail'], 'invalid_json')
            self.assertEqual(failed['details']['http_status'], 502)
        finally:
            conn.close()

    def test_official_interval_reuses_selector_batch_before_decision_even_with_new_inputs(self):
        self.provider['base_url'] = 'https://llm.legitreal.com/v1'
        self.post(3, 'An English housing proposal for the referendum.')
        with patch('ctn_data.ai_providers.complete',
                   side_effect=[self.selection(['post:3']), self.decision()]) as model:
            with self.assertRaises(AgentError) as caught:
                self.controller._run(self.agent_id)
            self.assertEqual(caught.exception.status_code, 429)
            self.assertFalse(self.memory().get('decision_versions'))
            self.assertEqual(model.call_count, 1)
            # Other roles or upstream throttling may keep this role waiting
            # longer than the ordinary short handoff window.
            self.now += 650
            self.post(4, 'New material arriving during the model cooldown.')
            self.controller._run(self.agent_id)
            self.assertEqual(model.call_count, 2)
        self.assertEqual(self.memory()['observation']['reading_selection']['cache_mode'], 'deferred_handoff')
        self.assertEqual(self.controller.snapshot()['drafts'][0]['action'], 'post')
        self.assertEqual(set(self.memory()['decision_versions']), {'post:3'})

    def test_missing_context_is_explicit_in_final_decision_and_default_evidence_excludes_background(self):
        self.post(1, 'Original proposal.', age=86400)
        self.post(3, 'A reply with an unavailable parent.', kind='reply',
                  root_post_id=1, reply_to_post_id=2)
        decision = self.decision()
        output = json.loads(decision['text'])
        output.pop('source_ids')
        decision['text'] = json.dumps(output)
        with patch('ctn_data.agent_budget.model_call',
                   side_effect=[self.selection(['post:3']), decision]) as model:
            self.controller._run(self.agent_id)
        context = json.loads(model.call_args.args[2][1]['content'])
        source = next(row for row in context['sources'] if row['id'] == 'post:3')
        self.assertFalse(source['thread_context']['complete'])
        self.assertTrue(source['thread_context']['missing'])
        self.assertEqual(set(self.memory()['decision_versions']), {'post:3'})
        self.assertIn('post:1', self.memory()['read_versions'])
        episode = next(row for row in self.memory()['episodes']['readings'] if row['source_id'] == 'post:3')
        self.assertFalse(episode['thread_context']['complete'])
        self.assertTrue(episode['thread_context']['missing'])

    def test_read_only_command_has_no_model_and_reset_clears_selector_state(self):
        self.post(1, 'An English housing proposal.')
        with patch('ctn_data.agent_budget.model_call', side_effect=AssertionError('Read-only calls no AI')):
            self.assertEqual(len(self.observe(for_decision=False)), 1)
        with patch('ctn_data.agent_budget.model_call', return_value=self.selection(['post:1'])):
            self.observe()
        self.assertIn('reading_selector_cache', self.memory())
        self.controller.store.change(lambda data, conn: agent_memory.reset(data, self.agent_id, self.now + 1))
        self.assertNotIn('reading_selector_cache', self.memory())
        self.assertNotIn('reading_selection', self.memory()['observation'])
        self.assertNotIn('thread_context', self.memory()['observation'])

    def test_already_consumed_input_is_idle_not_a_collection_error(self):
        self.post(3, 'Previously considered housing proposal.')
        with patch('ctn_data.agent_budget.model_call',
                   side_effect=[self.selection(['post:3']), self.decision()]) as model:
            self.controller._run(self.agent_id)
            self.controller._run(self.agent_id)
            self.assertEqual(model.call_count, 2)
        self.assertEqual(self.agent()['last_error'], '')
        self.assertEqual(len(self.controller.snapshot()['drafts']), 1)
        self.assertTrue(any('没有可供判断的新资料' in row['message']
                            for row in self.controller.store.read()['events']))

    def test_public_selection_provider_and_role_decision_provider_are_separate(self):
        _, decision_provider, reading_provider = self.independent_providers()
        self.configure_reading(reading_provider['id'])
        self.post(3, 'A new housing proposal.')
        with patch('ctn_data.agent_budget.model_call',
                   side_effect=[self.selection(['post:3']), self.decision()]) as model:
            self.controller._run(self.agent_id)
        self.assertEqual([call.args[1]['id'] for call in model.call_args_list],
                         [reading_provider['id'], decision_provider['id']])
        self.assertEqual(self.agent()['provider_id'], decision_provider['id'])
        self.assertEqual(self.controller.reading_selection()['settings']['provider_id'], reading_provider['id'])
        self.assertNotIn('private-key-for-', json.dumps(self.controller.reading_selection()))

    def test_unset_public_provider_uses_workbench_default_instead_of_role_provider(self):
        providers, decision_provider, reading_provider = self.independent_providers()
        providers.save({'default_provider_id': reading_provider['id']})
        self.configure_reading('')
        self.post(3, 'A new transit and housing proposal.')
        with patch('ctn_data.agent_budget.model_call',
                   side_effect=[self.selection(['post:3']), self.decision()]) as model:
            self.controller._run(self.agent_id)
        self.assertEqual([call.args[1]['id'] for call in model.call_args_list],
                         [reading_provider['id'], decision_provider['id']])

    def test_global_provider_change_invalidates_selection_cache_without_changing_role_provider(self):
        _, decision_provider, reading_provider = self.independent_providers()
        self.post(3, 'A relevant housing proposal.')
        self.configure_reading(reading_provider['id'])
        with patch('ctn_data.agent_budget.model_call', return_value=self.selection(['post:3'])) as model:
            self.observe()
            self.observe()
            self.assertEqual(model.call_count, 1)
            self.assertTrue(self.memory()['observation']['reading_selection']['cache_hit'])
            self.configure_reading(decision_provider['id'])
            self.observe()
            self.assertEqual(model.call_count, 2)
            self.assertEqual([call.args[1]['id'] for call in model.call_args_list],
                             [reading_provider['id'], decision_provider['id']])
        self.assertEqual(self.agent()['provider_id'], decision_provider['id'])

    def test_default_provider_change_invalidates_unset_public_provider_cache(self):
        providers, decision_provider, reading_provider = self.independent_providers()
        self.configure_reading('')
        self.post(3, 'A relevant English housing proposal.')
        with patch('ctn_data.agent_budget.model_call', return_value=self.selection(['post:3'])) as model:
            self.observe()
            providers.save({'default_provider_id': reading_provider['id']})
            self.observe()
        self.assertEqual([call.args[1]['id'] for call in model.call_args_list],
                         [decision_provider['id'], reading_provider['id']])

    def test_two_roles_keep_separate_selection_memory_and_cache_on_one_public_provider(self):
        _, decision_provider, reading_provider = self.independent_providers()
        self.configure_reading(reading_provider['id'])
        self.controller.save({'name': '另一位居民', 'username': 'second_reader',
            'interests': ['医疗与渡轮'], 'provider_id': decision_provider['id']})
        other_id = next(row['id'] for row in self.controller.store.read()['agents'].values() if row['id'] != self.agent_id)
        self.controller.memory(self.agent_id, 'ROLE_A_ONLY_INTEREST')
        self.controller.memory(other_id, 'ROLE_B_ONLY_INTEREST')
        self.post(3, 'A housing proposal near the ferry clinic.')
        with patch('ctn_data.agent_budget.model_call', return_value=self.selection(['post:3'])) as model:
            self.observe()
            with self.controller._claim(other_id) as token:
                other = self.controller.store.read()['agents'][other_id]
                agent_observation.observe(self.controller, other, token, refresh=False, for_decision=True)
            self.observe()
        self.assertEqual(model.call_count, 2)
        first, second = [call.args[2][1]['content'] for call in model.call_args_list]
        self.assertIn('ROLE_A_ONLY_INTEREST', first)
        self.assertNotIn('ROLE_B_ONLY_INTEREST', first)
        self.assertIn('ROLE_B_ONLY_INTEREST', second)
        self.assertNotIn('ROLE_A_ONLY_INTEREST', second)
        own = self.controller.store.read()['memory']
        self.assertNotEqual(own[self.agent_id]['reading_selector_cache']['context_key'],
                            own[other_id]['reading_selector_cache']['context_key'])

    def test_unavailable_public_provider_does_not_silently_run_role_model_for_selection(self):
        providers, decision_provider, reading_provider = self.independent_providers()
        self.configure_reading(reading_provider['id'])
        providers.save({'id': reading_provider['id'], 'enabled': False})
        self.post(3, 'A housing proposal that remains real local evidence.')
        with patch('ctn_data.agent_budget.model_call', return_value=self.decision()) as model:
            self.controller._run(self.agent_id)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(model.call_args.args[1]['id'], decision_provider['id'])
        selection = self.memory()['observation']['reading_selection']
        self.assertEqual(selection['status'], 'fallback')
        self.assertEqual(selection['error_code'], 'provider_unavailable')
        self.assertEqual(self.controller.reading_selection()['settings']['provider_id'], reading_provider['id'])

    def test_missing_public_provider_key_keeps_audited_fallback_without_false_binding_conflict(self):
        providers, decision_provider, reading_provider = self.independent_providers()
        self.configure_reading(reading_provider['id'])
        providers._path(reading_provider['id']).unlink()
        self.post(3, 'An already collected housing proposal.')
        with patch('ctn_data.agent_budget.model_call', return_value=self.decision()) as model:
            self.controller._run(self.agent_id)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(model.call_args.args[1]['id'], decision_provider['id'])
        self.assertEqual(self.memory()['observation']['reading_selection']['status'], 'fallback')
        self.assertEqual(self.memory()['observation']['reading_selection']['error_code'], 'provider_unavailable')
        self.assertEqual(len(self.controller.snapshot()['drafts']), 1)
        self.assertFalse(self.controller.reading_selection()['provider'])

    def test_unreadable_public_key_keeps_fallback_and_does_not_affect_decision_credentials(self):
        providers, decision_provider, reading_provider = self.independent_providers()
        self.configure_reading(reading_provider['id'])
        reading_secret = providers._path(reading_provider['id']).read_bytes()
        original_unprotect = ai_providers._unprotect
        def decrypt(value):
            if value == reading_secret:
                raise UnicodeError('Synthetic unreadable reading credential')
            return original_unprotect(value)
        self.post(3, 'An existing housing proposal remains valid evidence.')
        with patch('ctn_data.ai_providers._unprotect', side_effect=decrypt), \
                patch('ctn_data.agent_budget.model_call', return_value=self.decision()) as model:
            self.controller._run(self.agent_id)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(model.call_args.args[1]['id'], decision_provider['id'])
        self.assertEqual(self.memory()['observation']['reading_selection']['status'], 'fallback')
        self.assertEqual(len(self.controller.snapshot()['drafts']), 1)

    def test_provider_change_during_selection_rejects_old_result_before_read_or_decision(self):
        _, decision_provider, reading_provider = self.independent_providers()
        self.configure_reading(reading_provider['id'])
        self.post(3, 'A relevant housing proposal.')
        def selected(*args, **kwargs):
            self.assertEqual(args[1]['id'], reading_provider['id'])
            self.configure_reading(decision_provider['id'])
            return self.selection(['post:3'])
        with patch('ctn_data.agent_budget.model_call', side_effect=selected) as model:
            with self.assertRaises(AgentError) as caught:
                self.controller._run(self.agent_id)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(model.call_count, 1)
        self.assertFalse(self.memory().get('read_ids'))
        self.assertFalse(self.memory().get('decision_versions'))
        self.assertFalse(self.controller.snapshot()['drafts'])


if __name__ == '__main__':
    unittest.main()
