"""Shared reading service tests; fake model and no external network calls."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ctn_data import agent_budget, reading_selection
from ctn_data.agent_control import AgentController, AgentError
from ctn_data.ai_providers import ProviderError
from tests import test_agent_reading as reading_fixture
from tests import test_agent_reading_integration as integration_fixture


class SharedReadingSelectionTests(unittest.TestCase):
    def setUp(self):
        self.case = reading_fixture.ReadingTests(methodName='runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.controller = self.case.controller
        self.store = self.case.store
        self.store.state['settings'] = {'enabled': False, 'team_side': 'Vote Yes'}
        self.default = 'workbench'
        self.providers = {name: {**copy.deepcopy(self.case.provider), 'id': name, 'name': name,
                                 'model': name + '-model', 'enabled': True}
                          for name in ('workbench', 'shared', 'role')}
        def get(provider_id):
            effective = provider_id or self.default
            if effective not in self.providers:
                raise ProviderError('AI 连接不存在', 400)
            return self.providers[effective]
        self.controller.providers.get.side_effect = get
        self.case.agent['provider_id'] = 'role'
        self.store.state['agents']['maya']['provider_id'] = 'role'

    def select(self, candidates=None):
        return reading_selection.select(self.controller, self.case.agent, 'lease',
                                        self.case.sources if candidates is None else candidates)

    def test_initial_snapshot_is_read_only_and_has_stable_empty_role_contract(self):
        before = copy.deepcopy(self.store.state)
        status = reading_selection.snapshot(self.controller)
        self.assertEqual(status['settings'], {'provider_id': '', 'revision': 1})
        self.assertEqual(status['provider']['id'], 'workbench')
        self.assertEqual(status['roles'][0]['selection'], None)
        self.assertIsNone(status['roles'][0]['source_count'])
        self.assertEqual(status['roles'][0]['context'], {})
        self.assertEqual(status['recent_calls'], [])
        self.assertEqual(self.store.state, before)
        self.case.model.assert_not_called()

    def test_old_manual_observation_is_not_presented_as_shared_selection_reading(self):
        self.store.state['memory']['maya']['observation'] = {
            'source_count': 13, 'thread_context': {'complete': True}}
        role = reading_selection.snapshot(self.controller)['roles'][0]
        self.assertIsNone(role['selection'])
        self.assertIsNone(role['source_count'])
        self.assertEqual(role['context'], {})

    def test_configure_shared_provider_is_optimistic_and_preserves_role_and_runtime(self):
        actor = copy.deepcopy(self.store.state['agents']['maya'])
        status = reading_selection.configure(self.controller, {'provider_id': 'shared', 'expected_revision': 1})
        self.assertEqual(status['settings']['revision'], 2)
        self.assertEqual(status['provider']['id'], 'shared')
        self.assertEqual(self.store.state['agents']['maya'], actor)
        self.assertFalse(self.store.state['settings']['enabled'])
        self.case.model.assert_not_called()
        with self.assertRaises(AgentError) as caught:
            reading_selection.configure(self.controller, {'provider_id': '', 'expected_revision': 1})
        self.assertEqual(caught.exception.status_code, 409)
        unchanged = reading_selection.configure(self.controller, {'provider_id': 'shared', 'expected_revision': 2})
        self.assertEqual(unchanged['settings'], status['settings'])

    def test_invalid_missing_disabled_and_non_text_provider_are_rejected(self):
        for payload in ({'provider_id': 'shared'}, {'provider_id': 'shared', 'expected_revision': True},
                        {'provider_id': 'bad', 'expected_revision': 1}):
            with self.subTest(payload=payload), self.assertRaises(AgentError):
                reading_selection.configure(self.controller, payload)
        for change in ({'enabled': False}, {'protocol': 'other'}):
            self.providers['shared'] = {**self.providers['shared'], **change}
            with self.assertRaises(AgentError) as caught:
                reading_selection.configure(self.controller, {'provider_id': 'shared', 'expected_revision': 1})
            self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(reading_selection.snapshot(self.controller)['settings']['revision'], 1)

    def test_selector_uses_default_then_shared_without_using_role_provider(self):
        self.select()
        self.assertEqual(self.case.model.call_args.args[1]['id'], 'workbench')
        reading_selection.configure(self.controller, {'provider_id': 'shared', 'expected_revision': 1})
        _, metadata = self.select()
        self.assertFalse(metadata['cache_hit'])
        self.assertEqual(metadata['settings_revision'], 2)
        self.assertEqual(self.case.model.call_args.args[1]['id'], 'shared')
        self.assertEqual(self.store.state['agents']['maya']['provider_id'], 'role')

    def test_default_and_model_edits_invalidate_cache_but_role_provider_change_does_not(self):
        self.select()
        self.case.agent['provider_id'] = 'shared'
        self.store.state['agents']['maya']['provider_id'] = 'shared'
        _, metadata = self.select()
        self.assertTrue(metadata['cache_hit'])
        self.default = 'shared'
        _, metadata = self.select()
        self.assertFalse(metadata['cache_hit'])
        self.providers['shared']['model'] = 'edited-model'
        _, metadata = self.select()
        self.assertFalse(metadata['cache_hit'])
        self.assertEqual(self.case.model.call_count, 3)

    def test_inflight_global_configuration_or_effective_model_changes_cannot_save_result(self):
        def configure_during_call(*args, **kwargs):
            reading_selection.configure(self.controller, {'provider_id': 'shared', 'expected_revision': 1})
            return {'text': '{"source_ids":["post:1"],"reason":"library"}'}
        self.case.model.side_effect = configure_during_call
        with self.assertRaises(AgentError) as caught:
            self.select()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertNotIn('reading_selector_cache', self.store.state['memory']['maya'])
        self.case.model.side_effect = lambda *a, **kw: self._edit_default_and_return()
        self.store.state['settings'].pop('reading_selection')
        with self.assertRaises(AgentError) as caught:
            self.select()
        self.assertEqual(caught.exception.status_code, 409)

    def _edit_default_and_return(self):
        self.default = 'shared'
        return {'text': '{"source_ids":["post:1"],"reason":"library"}'}

    def test_observation_commit_guard_rejects_configuration_changed_after_selection(self):
        _, metadata = self.select()
        reading_selection.configure(self.controller, {'provider_id': 'shared', 'expected_revision': 1})
        with self.assertRaises(AgentError) as caught:
            self.store.change(lambda data, conn: reading_selection.check_result(self.controller, metadata, data, conn))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertNotIn('read_versions', self.store.state['memory']['maya'])

    def test_real_observation_cannot_record_old_persona_selection_after_profile_edit(self):
        race = integration_fixture.ReadingIntegrationTests(methodName='runTest')
        race.setUp()
        self.addCleanup(race.doCleanups)
        race.post(3, 'Public libraries and housing services.')
        self.case.model.return_value = race.selection(['post:3'])
        from ctn_data import agent_thread_context
        original_pack = agent_thread_context.pack
        def edit_after_selection(*args, **kwargs):
            result = original_pack(*args, **kwargs)
            race.controller.save({'id': race.agent_id, 'name': 'Changed persona'})
            return result
        with patch.object(agent_thread_context, 'pack', side_effect=edit_after_selection):
            with self.assertRaises(AgentError) as caught:
                race.observe()
        self.assertEqual(caught.exception.status_code, 409)
        own = race.memory()
        self.assertFalse(own.get('read_versions'))
        self.assertFalse(own.get('read_ids'))
        self.assertFalse(own.get('last_sources'))
        self.assertFalse(own.get('observation'))
        self.assertFalse(own.get('episodes', {}).get('readings'))

    def test_recent_calls_cover_cache_empty_fallback_and_deferred_without_secret_fields(self):
        self.select()
        self.select()
        self.controller.providers.get.reset_mock()
        self.select([])
        self.controller.providers.get.assert_not_called()
        self.store.state['memory']['maya'].pop('reading_selector_cache', None)
        self.case.model.return_value = {'text': 'invalid-json'}
        self.select()
        self.store.state['memory']['maya'].pop('reading_selector_cache', None)
        deferred = agent_budget.Deferred('llm:shared', 20)
        self.case.model.side_effect = deferred
        with self.assertRaises(agent_budget.Deferred) as caught:
            self.select()
        self.assertIs(caught.exception, deferred)
        status = reading_selection.snapshot(self.controller)
        calls = status['recent_calls']
        self.assertEqual([row['status'] for row in calls], ['deferred', 'fallback', 'empty', 'selected', 'selected'])
        self.assertTrue(calls[-2]['cache_hit'])
        serialized = json.dumps(status)
        for forbidden in ('api_key', 'NEVER-IN-PROMPT', 'fingerprint', '"_binding"', '"_selection"', 'context_key'):
            self.assertNotIn(forbidden, serialized)
        self.assertIsInstance(calls[0]['error'], str)
        self.assertTrue(all('settings_revision' in row and row['provider_id'] == 'workbench' for row in calls[:-3]))

    def test_recent_calls_are_bounded(self):
        binding = reading_selection.capture(self.controller, self.store.read())
        metadata = {'status': 'empty', 'candidate_count': 3, 'selected_count': 0, 'cache_hit': False, 'reason': 'No interest'}
        for _ in range(205):
            reading_selection._record(self.controller, self.case.agent, 'lease', metadata, binding)
        self.assertEqual(len(self.store.state['rate']['reading_selection_calls']), 200)
        self.assertEqual(len(reading_selection.snapshot(self.controller)['recent_calls']), 200)

    def test_real_provider_missing_credentials_keeps_fallback_instead_of_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = AgentController(root / 'local.db', root / 'keys', clock=lambda: self.case.now)
            try:
                controller.providers.save({'name': 'No saved key', 'base_url': 'https://example.invalid/v1',
                                           'model': 'offline-model', 'protocol': 'openai_chat'})
                controller.save({'name': 'Reader', 'username': 'offline_reader'})
                agent = controller.snapshot()['agents'][0]
                with controller._claim(agent['id']) as token:
                    selected, metadata = reading_selection.select(controller, agent, token, self.case.sources)
                self.assertTrue(selected)
                self.assertEqual(metadata['status'], 'fallback')
                self.assertEqual(metadata['error_code'], 'provider_unavailable')
                self.case.model.assert_not_called()
                self.assertIsNone(reading_selection.snapshot(controller)['provider'])
                with self.assertRaises(AgentError) as caught:
                    reading_selection.configure(controller, {'provider_id': '', 'expected_revision': 1})
                self.assertEqual(caught.exception.status_code, 400)
            finally:
                controller.stop()


if __name__ == '__main__':
    unittest.main()
