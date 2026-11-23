"""Offline contracts for the global policy across decisions, settings and previews."""
import unittest

from ctn_data.agent_control import AgentController, AgentError, AgentStore
from ctn_data.agent_manual import prepare
from ctn_data.agent_routes import SettingsRequest
from ctn_data import agent_relationships
from ctn_data.db import utc_now
from pydantic import ValidationError
from tests import test_agent_relationship_decisions as decision_fixtures
from tests import test_agent_manual_prepare as manual_fixtures
from tests import test_agent_management_api as api_fixtures


class InteractionDecisionWorkflows(unittest.TestCase):
    setUp = decision_fixtures.RelationshipDecisionTests.setUp
    decide = decision_fixtures.RelationshipDecisionTests.decide

    def peer(self, account_id=99, username='peer_writer'):
        self.controller.save({'name': 'Peer', 'username': username, 'enabled': False})
        peer = next(row for row in self.controller.store.read()['agents'].values()
                    if row['name'] == 'Peer')
        self.controller.store.change(lambda data, conn: data['agents'][peer['id']].update(
            account_id=account_id, account_state='ready'))
        return peer['id']

    def test_default_and_partial_settings_survive_controller_restart(self):
        self.assertTrue(self.controller.snapshot()['settings']['block_agent_interactions'])
        self.controller.settings({'block_agent_interactions': False})
        self.controller.settings({'enabled': True})
        self.assertFalse(self.controller.snapshot()['settings']['block_agent_interactions'])
        fresh = AgentController(self.db, self.controller.secret_dir.parent)
        self.addCleanup(fresh.stop)
        self.assertFalse(fresh.snapshot()['settings']['block_agent_interactions'])
        self.assertTrue(fresh.snapshot()['settings']['enabled'])

    def test_all_internal_model_actions_are_terminal_local_blocks(self):
        self.peer()
        cases = [('reply', {'body': 'A reply', 'target_post_id': 7}),
                 ('like', {'target_post_id': 7}), ('repost', {'target_post_id': 7}),
                 ('unlike', {'target_post_id': 7}), ('follow', {'target_account_id': 99}),
                 ('unfollow', {'target_account_id': 99}),
                 ('post', {'body': 'Hello @PEER_WRITER, a question.'})]
        for action, fields in cases:
            with self.subTest(action=action):
                state, call = self.decide(action, **fields)
                draft = next(row for row in state['drafts'] if row['action'] == action)
                self.assertEqual(draft['state'], 'skipped')
                self.assertTrue(draft['policy_blocked'])
                self.assertTrue(draft['policy_reason'])
                self.assertNotIn('uncertain', {row['state'] for row in state['drafts']})
                schema = call.call_args.kwargs['response_schema']['properties']
                self.assertNotIn(7, schema['target_post_id']['enum'])
                self.assertNotIn(99, schema['target_account_id']['enum'])
        self.assertEqual(self.controller.snapshot()['interaction_policy']['blocked_today'], len(cases))

    def test_reading_peer_evidence_can_still_produce_original_post(self):
        self.peer()
        state, call = self.decide('post', body='My independent thought about the library.')
        self.assertEqual(state['drafts'][0]['state'], 'pending')
        import json
        context = json.loads(call.call_args.args[2][1]['content'])
        self.assertEqual(context['sources'][0]['text'], self.source['text'])
        self.assertTrue(context['sources'][0]['interaction_blocked'])

    def test_liked_internal_source_cannot_reenter_targets_when_post_cache_is_missing(self):
        self.peer()
        self.controller.store.change(lambda data, conn: agent_relationships.observe(
            data, 10, 'like', 7, True, utc_now(), 'authenticated fixture'))
        _, call = self.decide('post', body='An independent thought about this conversation.')
        schema = call.call_args.kwargs['response_schema']['properties']
        self.assertNotIn(7, schema['target_post_id']['enum'])

    def test_enabling_policy_retires_old_draft_without_replaying_when_disabled(self):
        self.peer()
        self.controller.settings({'block_agent_interactions': False})
        state, _ = self.decide('reply', body='Old internal reply', target_post_id=7)
        draft_id = state['drafts'][0]['id']
        self.controller.settings({'block_agent_interactions': True})
        row = self.controller.store.read()['drafts'][draft_id]
        self.assertEqual(row['state'], 'skipped')
        self.assertTrue(row['policy_blocked'])
        self.controller.settings({'block_agent_interactions': False})
        self.assertEqual(self.controller.store.read()['drafts'][draft_id]['state'], 'skipped')
        with self.assertRaises(AgentError):
            self.controller.publish(draft_id)

    def test_rebinding_retains_old_account_and_username_after_store_restart(self):
        peer_id = self.peer()
        self.controller.store.change(lambda data, conn: data['agents'][peer_id].update(
            account_id=100, username='new_peer_writer'))
        registry = AgentStore(self.db).read()['rate']['managed_agent_accounts']
        self.assertIn('peer_writer', registry['99']['usernames'])
        self.assertIn('new_peer_writer', registry['100']['usernames'])
        state, _ = self.decide('post', body='Calling @peer_writer again.')
        self.assertTrue(state['drafts'][0]['policy_blocked'])


class InteractionManualPreviews(unittest.TestCase):
    setUp = manual_fixtures.AgentManualPrepareTests.setUp
    payload = manual_fixtures.AgentManualPrepareTests.payload
    state = manual_fixtures.AgentManualPrepareTests.state

    def peer(self, account_id, username):
        self.controller.save({'name': 'Peer', 'username': username})
        peer_id = next(row['id'] for row in self.state()['agents'].values() if row['name'] == 'Peer')
        self.controller.store.change(lambda data, conn: data['agents'][peer_id].update(
            account_id=account_id, account_state='ready'))

    def test_manual_internal_reply_and_mention_cannot_create_pending_drafts(self):
        self.peer(9, 'actual_peer')
        for payload in (self.payload('reply'), self.payload(body='Hello @actual_peer')):
            with self.subTest(action=payload['action']):
                with self.assertRaises(AgentError) as error:
                    prepare(self.controller, payload)
                self.assertEqual(error.exception.status_code, 409)
                self.assertEqual(self.state()['drafts'], {})

    def test_manual_follow_blocked_but_manual_cleanup_is_available(self):
        self.peer(53, 'actual_person')
        with self.assertRaises(AgentError):
            prepare(self.controller, self.payload('follow'))
        result = prepare(self.controller, self.payload('unfollow'))
        self.assertEqual(self.state()['drafts'][result['draft']['id']]['state'], 'pending')


class InteractionSettingsAPI(unittest.TestCase):
    setUp = api_fixtures.AgentManagementAPITests.setUp

    def test_strict_boolean_api_payload_and_partial_update(self):
        # Call the registered route directly: this Windows environment's
        # TestClient socketpair can block before any application code runs.
        route = next(route for route in self.app.routes if route.path == '/api/agents/settings')
        self.assertTrue(route.dependant.dependencies)
        for invalid in ('false', 0, 1, []):
            with self.assertRaises(ValidationError):
                SettingsRequest(block_agent_interactions=invalid)
        self.assertEqual(route.endpoint(SettingsRequest(block_agent_interactions=False)), {'ok': True})
        self.assertFalse(self.controller.snapshot()['settings']['block_agent_interactions'])
        route.endpoint(SettingsRequest(enabled=True))
        self.assertFalse(self.controller.snapshot()['settings']['block_agent_interactions'])


if __name__ == '__main__':
    unittest.main()
