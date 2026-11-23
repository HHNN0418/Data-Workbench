"""AI reading tests use only dictionaries and a mocked budget/model boundary."""
import copy
import json
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ctn_data import agent_budget, agent_reading
from ctn_data.agent_control import AgentError
from ctn_data.ai_providers import ProviderError


class Store:
    def __init__(self, state):
        self.state = state

    def read(self):
        return copy.deepcopy(self.state)

    def change(self, callback):
        changed = copy.deepcopy(self.state)
        result = callback(changed, None)
        self.state = changed
        return result


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.now = 1792481200.0
        self.agent = {'id': 'maya', 'version': 4, 'persona_revision': 3, 'provider_id': 'p',
                      'account_id': 5554, 'name': 'Maya', 'username': 'maya',
                      'background': '完整中文人设。' * 200, 'stance': '支持但仍会核对具体问题',
                      'voice': '自然具体', 'interests': ['公共图书馆服务', '海边散步'], 'language': 'English'}
        self.provider = {'id': 'p', 'protocol': 'openai_chat', 'base_url': 'https://fake.invalid',
                         'model': 'configured-model', 'thinking_mode': 'enabled', 'max_output_tokens': 5000,
                         'api_key': 'NEVER-IN-PROMPT'}
        self.store = Store({'agents': {'maya': copy.deepcopy(self.agent)}, 'drafts': {}, 'events': [],
                            'rate': {}, 'memory': {'maya': {'inbox': [{'id': 101, 'status': 'unresolved'}]}}})
        def cancellation(agent_id, data=None):
            if self.store.state['agents'][agent_id].get('cancelled'):
                raise AgentError('cancelled', 499)
        self.options = {'cancellation_event': object(), 'on_progress': lambda _: None}
        self.controller = SimpleNamespace(
            store=self.store, clock=lambda: self.now,
            _agent=lambda data, agent_id: data['agents'][agent_id],
            _check_lease=Mock(), _event=Mock(), providers=SimpleNamespace(get=Mock(return_value=self.provider)),
            generations=SimpleNamespace(check_cancelled=Mock(side_effect=cancellation),
                                        request_options=Mock(return_value=self.options)))
        self.model = patch.object(agent_budget, 'model_call', return_value={
            'text': json.dumps({'source_ids': ['post:1'], 'reason': 'Fits my interests.'})}).start()
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('Offline only')).start()
        self.addCleanup(patch.stopall)
        self.sources = [self.source(1), self.source(2, origin='notification', notification_ids=[101]),
                        self.source(3, kind='news', origin='news')]

    @staticmethod
    def source(post_id, **extra):
        return {'id': f'post:{post_id}', 'post_id': post_id, 'kind': 'post', 'post_kind': 'post',
                'version': 'v1', 'origin': 'recent', 'author_id': 99,
                'title': 'Public library discussion', 'text': 'Public libraries and paper information. ' * 30,
                'created_at': '2026-10-20T07:00:00Z', 'notification_ids': [], **extra}

    def select(self, sources=None, agent=None):
        return agent_reading.select(self.controller, agent or self.agent, 'lease',
                                    self.sources if sources is None else sources)

    def payload(self):
        return json.loads(self.model.call_args.args[2][1]['content'])

    def test_real_ids_deduplicated_original_content_and_provider_options_preserved(self):
        self.model.return_value = {'text': json.dumps({'source_ids': ['post:1', 'post:1'], 'reason': 'Library.'})}
        before = copy.deepcopy(self.store.state['memory']['maya'])
        selected, metadata = self.select()
        self.assertEqual(metadata['selected_ids'], ['post:1'])
        self.assertIs(selected[0], self.sources[0])
        self.assertEqual(self.payload()['persona'], agent_reading._persona(self.agent))
        self.assertEqual(self.model.call_args.args[1], self.provider)
        self.assertNotIn('max_output_tokens', self.model.call_args.kwargs)
        self.assertIs(self.model.call_args.kwargs['cancellation_event'], self.options['cancellation_event'])
        self.assertNotIn('NEVER-IN-PROMPT', json.dumps(self.payload()))
        after = self.store.state['memory']['maya']
        self.assertEqual(after['inbox'], before['inbox'])
        for key in ('read_versions', 'decision_versions', 'decision_notification_ids'):
            self.assertNotIn(key, after)

    def test_directory_is_bounded_balanced_and_sorted_within_each_channel(self):
        sources = [self.source(i, origin='notification', notification_ids=[i]) for i in range(1, 61)]
        sources += [self.source(i, kind='news', origin='news') for i in range(61, 91)]
        sources += [self.source(i) for i in range(91, 151)]
        sources[1]['created_at'] = '2026-10-20T08:00:00Z'
        self.select(sources)
        directory = self.payload()['directory']
        self.assertEqual(len(directory), agent_reading.MAX_CATALOG)
        self.assertEqual(directory[0]['id'], 'post:2')
        self.assertTrue(any(row['kind'] == 'news' for row in directory))
        self.assertTrue(any(not row['notification_ids'] and row['kind'] == 'post' for row in directory))
        self.assertTrue(all(len(row['excerpt']) <= agent_reading.SNIPPET_CHARACTERS for row in directory))

    def test_no_candidates_does_not_resolve_provider_or_model(self):
        selected, metadata = self.select([])
        self.assertEqual(selected, [])
        self.assertEqual(metadata['status'], 'empty')
        self.controller.providers.get.assert_not_called()
        self.model.assert_not_called()

    def test_successful_empty_selection_is_cached_but_not_handed_to_changed_candidates(self):
        self.model.return_value = {'text': '{"source_ids":[],"reason":"Nothing new interests me."}'}
        self.select()
        self.now += 30
        _, cached = self.select()
        self.assertTrue(cached['cache_hit'])
        self.assertEqual(cached['status'], 'empty')
        self.select(self.sources + [self.source(4)])
        self.assertEqual(self.model.call_count, 2)

    def test_observation_and_reading_log_changes_do_not_invalidate_cache(self):
        self.select()
        own = self.store.state['memory']['maya']
        own.update(observation={'observed_at': 'now'}, last_read_at='now', last_sources=self.sources)
        own['episodes'] = {'readings': [{'source_id': 'post:2', 'author_id': 99, 'last_read_at': 'now'}]}
        self.sources[0]['observed_at'] = 'a new poll timestamp'
        self.sources[0]['last_seen'] = 'another poll timestamp'
        _, metadata = self.select()
        self.assertTrue(metadata['cache_hit'])
        self.assertEqual(self.model.call_count, 1)

    def test_nonempty_handoff_survives_new_sources_and_notifications_for_120_seconds(self):
        self.select()
        self.now += 60
        changed = copy.deepcopy(self.sources) + [self.source(4)]
        changed[0]['notification_ids'] = [202]
        selected, metadata = self.select(changed)
        self.assertEqual(metadata['cache_mode'], 'handoff')
        self.assertEqual(metadata['candidate_count'], 3)
        self.assertEqual(metadata['available_count'], 4)
        self.assertEqual(selected[0]['notification_ids'], [202])
        self.assertEqual(self.model.call_count, 1)
        self.now += 61
        self.select(changed)
        self.assertEqual(self.model.call_count, 2)

    def test_selected_content_change_or_disappearance_invalidates_handoff(self):
        self.select()
        changed = copy.deepcopy(self.sources)
        changed[0]['version'] = 'v2'
        self.select(changed)
        self.assertEqual(self.model.call_count, 2)
        self.select(changed[1:])
        self.assertEqual(self.model.call_count, 3)

    def test_long_decision_deferral_reuses_batch_despite_new_candidates_until_success(self):
        self.select()
        self.store.state['agents']['maya']['decision_retry_at'] = self.now + 600
        self.now += 650
        changed = copy.deepcopy(self.sources) + [self.source(4)]
        _, metadata = self.select(changed)
        self.assertEqual(metadata['cache_mode'], 'deferred_handoff')
        self.assertEqual(metadata['candidate_count'], 3)
        self.assertEqual(metadata['available_count'], 4)
        self.assertEqual(self.model.call_count, 1)
        # Another long contention/backoff cycle still retains the same batch.
        self.store.state['agents']['maya']['decision_retry_at'] = self.now + 900
        self.now += 1000
        changed.append(self.source(5))
        _, metadata = self.select(changed)
        self.assertEqual(metadata['cache_mode'], 'deferred_handoff')
        self.assertEqual(self.model.call_count, 1)
        # A successful decision clears the durable retry marker. New ordinary
        # input can then cause a new selection after the normal handoff window.
        self.store.state['agents']['maya']['decision_retry_at'] = 0
        _, metadata = self.select(changed)
        self.assertFalse(metadata['cache_hit'])
        self.assertEqual(self.model.call_count, 2)

    def test_long_deferred_batch_still_requires_live_source_versions_and_current_persona(self):
        self.select()
        self.store.state['agents']['maya']['decision_retry_at'] = self.now + 600
        self.now += 650
        changed = copy.deepcopy(self.sources) + [self.source(4)]
        changed[0]['text'] += ' Edited evidence.'
        _, metadata = self.select(changed)
        self.assertFalse(metadata['cache_hit'])
        self.assertEqual(self.model.call_count, 2)
        self.now += 650
        self.store.state['agents']['maya']['decision_retry_at'] = self.now + 600
        updated_agent = copy.deepcopy(self.agent)
        updated_agent['persona_revision'] += 1
        updated_agent['interests'] = ['本地交通']
        self.store.state['agents']['maya'] = {**self.store.state['agents']['maya'], **updated_agent}
        _, metadata = self.select(changed, agent=updated_agent)
        self.assertFalse(metadata['cache_hit'])
        self.assertEqual(self.model.call_count, 3)

    def test_persona_or_model_setting_changes_invalidate_cache(self):
        self.select()
        changed = copy.deepcopy(self.agent)
        changed['interests'] = ['修理旧自行车']
        changed['persona_revision'] += 1
        self.store.state['agents']['maya'] = changed
        self.select(agent=changed)
        self.provider['thinking_mode'] = 'disabled'
        self.select(agent=changed)
        self.assertEqual(self.model.call_count, 3)

    def test_legacy_summary_is_available_as_subjective_memory_without_reviving_reset_actions(self):
        own = self.store.state['memory']['maya']
        own.update(summary='UNVERIFIED MODEL STORY', reset_at='2026-10-20T06:00:00Z')
        self.store.state['drafts']['old'] = {'id': 'old', 'agent_id': 'maya', 'state': 'published',
            'action': 'reply', 'body': 'OLD COMPLETED BODY', 'created_at': '2026-10-19T00:00:00Z'}
        own['subjective'] = {'viewpoints': [{'text': 'Paper access matters.', 'at': '2026-10-20T07:00:00Z'}],
                             'questions': [], 'people': []}
        self.select()
        memory = json.dumps(self.payload()['memory'])
        self.assertEqual(self.payload()['memory']['subjective_notes']['summary'], 'UNVERIFIED MODEL STORY')
        self.assertEqual(self.payload()['memory']['subjective_notes']['epistemic_status'], 'self_reported_thoughts')
        self.assertEqual(self.payload()['memory']['completed_actions'], [])
        self.assertNotIn('OLD COMPLETED BODY', memory)
        self.assertIn('Paper access matters.', memory)
        self.assertNotIn('observed_reading', self.payload()['memory'])

    def test_subjective_change_and_memory_reset_invalidate_cache(self):
        self.select()
        self.store.state['memory']['maya']['subjective'] = {
            'viewpoints': [{'text': 'I now care about bus access.', 'at': '2026-10-20T07:00:00Z'}],
            'questions': [], 'people': []}
        self.select()
        self.store.state['memory']['maya']['reset_at'] = '2026-10-20T07:30:00Z'
        self.select()
        self.assertEqual(self.model.call_count, 3)

    def test_retained_summary_change_refreshes_selection_and_reaches_selector_model(self):
        own = self.store.state['memory']['maya']
        own['summary'] = 'Remember the ferry discussion and paper access.'
        self.select()
        self.assertEqual(self.payload()['memory']['subjective_notes']['summary'], own['summary'])
        self.select()
        self.assertEqual(self.model.call_count, 1)
        own = self.store.state['memory']['maya']
        own['summary'] = 'Remember the library discussion and bus access.'
        self.select()
        self.assertEqual(self.model.call_count, 2)
        self.assertEqual(self.payload()['memory']['subjective_notes']['summary'], own['summary'])

    def test_deferred_and_cancelled_are_propagated_without_fallback_or_cache(self):
        for error in (agent_budget.Deferred('llm:p', 20), AgentError('cancelled', 499)):
            with self.subTest(error=type(error).__name__):
                self.model.side_effect = error
                with self.assertRaises(type(error)) as caught:
                    self.select()
                self.assertIs(caught.exception, error)
                self.assertNotIn('reading_selector_cache', self.store.state['memory']['maya'])

    def test_invalid_or_invented_selection_uses_only_true_ids_in_safe_fallback(self):
        for output in ('not-json', '{"source_ids":["post:invented"],"reason":"x"}'):
            with self.subTest(output=output):
                self.store.state['memory']['maya'].pop('reading_selector_cache', None)
                self.model.return_value = {'text': output}
                selected, metadata = self.select()
                self.assertEqual(metadata['status'], 'fallback')
                self.assertIsInstance(metadata['error'], str)
                self.assertEqual(metadata['error_code'], 'invalid_selection')
                self.assertTrue(all(source['id'] in {row['id'] for row in self.sources} for source in selected))

    def test_service_and_unsupported_provider_failures_are_auditable_without_secrets(self):
        self.model.side_effect = RuntimeError('SECRET UPSTREAM RESPONSE')
        _, metadata = self.select()
        self.assertEqual(metadata['status'], 'fallback')
        self.assertNotIn('SECRET', json.dumps(metadata))
        self.controller._event.assert_called()
        self.store.state['memory']['maya'].pop('reading_selector_cache', None)
        self.provider['protocol'] = 'unsupported'
        self.model.reset_mock()
        _, metadata = self.select()
        self.assertEqual(metadata['error_code'], 'unsupported_provider')
        self.model.assert_not_called()

    def test_validation_failures_keep_specific_safe_codes(self):
        cases = [('not-json SECRET RESPONSE', 'invalid_json'),
                 (json.dumps({'source_ids': ['post:invented'], 'reason': 'x'}), 'unknown_source_id'),
                 (json.dumps({'source_ids': ['post:1'] * 13, 'reason': 'x'}), 'too_many_sources')]
        for output, code in cases:
            with self.subTest(code=code):
                self.store.state['memory']['maya'].pop('reading_selector_cache', None)
                self.model.return_value = {'text': output}
                selected, metadata = self.select()
                self.assertTrue(selected)
                self.assertEqual(metadata['error_code'], 'invalid_selection')
                self.assertEqual(metadata['error_stage'], 'validation')
                self.assertEqual(metadata['error_detail'], code)
                self.assertNotIn('SECRET', json.dumps(metadata))

    def test_over_long_reason_is_truncated_without_discarding_the_selection(self):
        self.store.state['memory']['maya'].pop('reading_selector_cache', None)
        self.model.return_value = {'text': json.dumps({'source_ids': ['post:1'], 'reason': 'x' * 900})}
        selected, metadata = self.select()
        self.assertEqual(metadata['status'], 'selected')
        self.assertEqual([source['id'] for source in selected], ['post:1'])

    def test_request_parameter_error_is_not_misreported_as_invalid_model_output(self):
        self.model.side_effect = ValueError('SECRET INVALID PARAMETER')
        selected, metadata = self.select()
        self.assertTrue(selected)
        self.assertEqual(metadata['error_code'], 'selector_request_error')
        self.assertEqual(metadata['error_stage'], 'request')
        self.assertNotIn('SECRET', json.dumps(metadata))

    def test_safe_provider_reason_and_diagnostics_survive_fallback(self):
        error = ProviderError('AI 输出预算已耗尽 NEVER-IN-PROMPT Bearer secret-token', 502,
                              diagnostics={'stage': 'body', 'elapsed_seconds': 12,
                                           'request_id': 'request-123', 'api_key': 'secret',
                                           'prompt': 'SECRET PROMPT'})
        self.model.side_effect = error
        _, metadata = self.select()
        self.assertEqual(metadata['error_code'], 'selector_service_error')
        self.assertEqual(metadata['error_status'], 502)
        self.assertIn('AI 输出预算已耗尽', metadata['error'])
        self.assertEqual(metadata['error_diagnostics'],
                         {'stage': 'body', 'elapsed_seconds': 12, 'request_id': 'request-123'})
        for secret in ('NEVER-IN-PROMPT', 'secret-token', 'SECRET PROMPT'):
            self.assertNotIn(secret, json.dumps(metadata))
        event = self.controller._event.call_args
        self.assertIn('AI 输出预算已耗尽', event.args[2])
        self.assertNotIn('NEVER-IN-PROMPT', event.args[2])

    def test_truncated_selection_falls_back_and_records_its_cause(self):
        # The transport diagnostics carry their own error_code; recording the
        # fallback once raised TypeError and failed the whole role task.
        self.model.side_effect = ProviderError('AI 服务的输出预算已耗尽，最终回答未完成', 502,
                                               diagnostics={'error_code': 'output_truncated', 'stage': 'body',
                                                            'completion_tokens': 8000})
        selected, metadata = self.select()
        self.assertTrue(selected)
        self.assertEqual(metadata['status'], 'fallback')
        details = self.controller._event.call_args.kwargs['details']
        self.assertEqual(details['error_code'], 'selector_service_error')
        self.assertEqual(details['error_cause'], 'output_truncated')
        self.assertEqual(details['completion_tokens'], 8000)

    def test_failed_selection_recovers_despite_long_decision_retry_and_new_candidates(self):
        self.model.return_value = {'text': 'invalid-json'}
        first, metadata = self.select()
        self.assertEqual(metadata['status'], 'fallback')
        self.assertTrue(first)
        self.store.state['agents']['maya']['decision_retry_at'] = self.now + 600
        changed = copy.deepcopy(self.sources) + [self.source(4)]
        self.model.return_value = {'text': json.dumps({'source_ids': ['post:4'], 'reason': 'Recovered'})}
        self.now += agent_reading.FALLBACK_RETRY_SECONDS - 1
        _, metadata = self.select(changed)
        self.assertEqual(metadata['cache_mode'], 'fallback_retry')
        self.assertEqual(self.model.call_count, 1)
        self.now += 3600
        selected, metadata = self.select(changed)
        self.assertEqual([row['id'] for row in selected], ['post:4'])
        self.assertEqual(metadata['status'], 'selected')
        self.assertFalse(metadata['cache_hit'])
        self.assertNotIn('error', metadata)
        self.assertEqual(self.model.call_count, 2)
        self.select(changed)
        self.assertEqual(self.model.call_count, 2)

    def test_repeated_failure_retries_after_delay_and_later_recovers(self):
        self.model.side_effect = ProviderError('AI 服务暂不可用', 503)
        self.select()
        self.select()
        self.assertEqual(self.model.call_count, 1)
        self.now += agent_reading.FALLBACK_RETRY_SECONDS
        _, metadata = self.select()
        self.assertEqual(metadata['status'], 'fallback')
        self.assertEqual(self.model.call_count, 2)
        self.now += agent_reading.FALLBACK_RETRY_SECONDS
        self.model.side_effect = None
        _, metadata = self.select()
        self.assertEqual(metadata['status'], 'selected')
        self.assertIsNone(self.store.state['memory']['maya']['reading_selector_cache']['error'])
        self.assertEqual(self.model.call_count, 3)

    def test_upstream_rate_limit_keeps_reading_but_respects_retry_after(self):
        error = ProviderError('AI 服务暂时限流', 429)
        error.retry_after = 180
        self.model.side_effect = error
        selected, metadata = self.select()
        self.assertTrue(selected)
        self.assertEqual(metadata['error_code'], 'selector_rate_limited')
        self.assertEqual(metadata['error_status'], 429)
        self.model.side_effect = None
        self.now += 179
        _, metadata = self.select()
        self.assertEqual(metadata['status'], 'fallback')
        self.assertEqual(self.model.call_count, 1)
        self.now += 1
        _, metadata = self.select()
        self.assertEqual(metadata['status'], 'selected')
        self.assertEqual(self.model.call_count, 2)

    def test_cancellation_or_persona_change_during_model_call_cannot_save_cache(self):
        def cancel(*args, **kwargs):
            self.store.state['agents']['maya']['cancelled'] = True
            return {'text': '{"source_ids":["post:1"],"reason":"library"}'}
        self.model.side_effect = cancel
        with self.assertRaises(AgentError) as caught:
            self.select()
        self.assertEqual(caught.exception.status_code, 499)
        self.assertNotIn('reading_selector_cache', self.store.state['memory']['maya'])
        self.store.state['agents']['maya'].pop('cancelled')
        def revise(*args, **kwargs):
            self.store.state['agents']['maya']['persona_revision'] += 1
            return {'text': '{"source_ids":["post:1"],"reason":"library"}'}
        self.model.side_effect = revise
        with self.assertRaises(AgentError) as caught:
            self.select()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertNotIn('reading_selector_cache', self.store.state['memory']['maya'])

    def test_persisted_cache_survives_a_new_controller_instance(self):
        self.select()
        other = copy.copy(self.controller)
        other.generations = SimpleNamespace(check_cancelled=Mock(), request_options=Mock(return_value={}))
        _, metadata = agent_reading.select(other, self.agent, 'another-lease', self.sources)
        self.assertTrue(metadata['cache_hit'])
        self.assertEqual(self.model.call_count, 1)


if __name__ == '__main__':
    unittest.main()
