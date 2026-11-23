"""Offline decision boundary tests for persona-driven relationship operations."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ctn_data import agent_decision, agent_memory, agent_relationships
from ctn_data.agent_control import AgentController, AgentError, AgentStore


class RelationshipDecisionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / 'test.db'
        self.controller = AgentController(self.db, Path(temporary.name) / 'secrets',
            provider_store=SimpleNamespace(get=lambda _: {'id': 'fake', 'protocol': 'openai_chat',
                'model': 'fake', 'max_output_tokens': 1800}), clock=lambda: 1792450000.0)
        self.addCleanup(self.controller.stop)
        self.controller.save({'name': 'Reader', 'username': 'fictional_reader', 'provider_id': 'fake'})
        self.agent_id = self.controller.snapshot()['agents'][0]['id']
        self.source = {'id': 'post:7', 'post_id': 7, 'author_id': 99, 'kind': 'post',
                       'text': 'A fictional conversation about the library.', 'version': 'v1',
                       'notification_ids': [101]}
        self.controller.store.change(lambda data, conn: data['agents'][self.agent_id].update(
            account_id=10, account_state='ready'))
        patcher = patch.object(self.controller, '_read_sources', return_value=[self.source])
        patcher.start()
        self.addCleanup(patcher.stop)

    def decide(self, action, **changes):
        output = {'action': action, 'body': '', 'target_post_id': None, 'target_account_id': None,
                  'memory': 'I want to follow the library discussion.', 'reason': 'Relevant discussion.',
                  'source_ids': [], 'notification_ids': [], **changes}
        with patch('ctn_data.agent_budget.model_call', return_value={
                'text': json.dumps(output), 'model': 'fake', 'usage': {'total_tokens': 15}}) as call:
            self.controller.run(self.agent_id)
        return self.controller.snapshot(), call

    def test_follow_uses_source_author_and_retains_relationship_context(self):
        state, call = self.decide('follow', target_account_id=99)
        draft = state['drafts'][0]
        self.assertEqual(draft['state'], 'pending')
        self.assertEqual(draft['target_account_id'], 99)
        self.assertIsNone(draft['target_post_id'])
        self.assertEqual(draft['source_ids'], ['post:7'])
        self.assertEqual(draft['relationship_context']['account_id'], 10)
        prompt = json.loads(call.call_args.args[2][1]['content'])
        self.assertIn('relationship_context', prompt)
        self.assertIn('autobiographical_memory', prompt)
        memory = state['agents'][0]['memory_view']
        self.assertEqual(memory['pending_intentions'][0]['target_account_id'], 99)
        self.assertEqual(memory['completed_actions'], [])
        self.controller.discard(draft['id'])
        self.assertEqual(self.controller.snapshot()['agents'][0]['memory_view']['pending_intentions'], [])

    def test_model_sees_distinct_official_action_availability(self):
        now = self.controller.clock()
        self.controller.store.change(lambda data, conn: data['rate'].setdefault('buckets', {}).update({
            'post': {'not_before': now + 20}, 'like': {'not_before': now + 5}}))
        _, call = self.decide('follow', target_account_id=99)
        context = json.loads(call.call_args.args[2][1]['content'])
        availability = context['action_availability']
        for action in ('post', 'reply', 'repost'):
            self.assertFalse(availability[action]['available'])
            self.assertEqual(availability[action]['bucket'], 'post')
            self.assertEqual(availability[action]['retry_after'], 20)
            self.assertEqual(availability[action]['limit'], 3)
        self.assertFalse(availability['like']['available'])
        self.assertEqual(availability['like']['retry_after'], 5)
        self.assertTrue(availability['follow']['available'])
        self.assertEqual(availability['follow']['bucket'], 'follow')
        self.assertEqual(availability['follow']['limit'], 30)

    def test_enabled_post_reply_like_and_follow_are_independent_choices(self):
        allowed = ['post', 'reply', 'like', 'follow', 'skip']
        self.controller.save({'id': self.agent_id, 'name': 'Reader', 'allowed_actions': allowed})
        cases = [('post', {'body': 'A separate thought about our library.'}),
                 ('reply', {'body': 'I have a question about that library proposal.', 'target_post_id': 7}),
                 ('like', {'target_post_id': 7}), ('follow', {'target_account_id': 99})]
        for action, fields in cases:
            with self.subTest(action=action):
                state, call = self.decide(action, **fields)
                self.assertTrue(any(draft['action'] == action and draft['state'] == 'pending'
                                    for draft in state['drafts']))
                context = json.loads(call.call_args.args[2][1]['content'])
                self.assertEqual(context['allowed_actions'], allowed)
                self.assertEqual(self.controller.store.read()['agents'][self.agent_id]['allowed_actions'], allowed)

    def test_social_impressions_stay_subjective_without_inventing_relationships(self):
        impression = '我对账号99的信任有所增加，因为他认真解释了图书馆计划；我仍不同意其中一点，想继续了解。'
        self.decide('follow', target_account_id=99, memory=impression,
                    memory_updates={'viewpoints': [], 'questions': [],
                                    'people': [{'account_id': 99, 'text': impression, 'source_ids': ['post:7']}]})
        _, call = self.decide('skip', memory=impression)
        context = json.loads(call.call_args.args[2][1]['content'])
        self.assertNotIn('memory', context)
        notes = context['autobiographical_memory']['subjective_notes']
        self.assertEqual(notes['summary'], '')
        self.assertEqual(notes['people'][0]['text'], impression)
        self.assertEqual(notes['epistemic_status'], 'self_reported_thoughts')
        self.assertEqual(context['relationship_context']['relations'], [])
        self.assertEqual(context['autobiographical_memory']['completed_actions'], [])

    def test_retained_legacy_summary_is_visible_and_reaches_the_next_model_decision(self):
        summary = 'I care about library access and remember the ferry discussion. ' * 30
        self.controller.store.change(lambda data, conn: data['memory'].setdefault(
            self.agent_id, {}).update(summary=summary))
        before = self.controller.store.read()['memory'][self.agent_id]['summary']
        state, call = self.decide('skip')
        self.assertEqual(state['agents'][0]['memory'], summary)
        context = json.loads(call.call_args.args[2][1]['content'])
        notes = context['autobiographical_memory']['subjective_notes']
        self.assertEqual(notes['summary'], summary.strip())
        self.assertEqual(notes['summary_source'], 'legacy_unknown')
        self.assertEqual(context['autobiographical_memory']['completed_actions'], [])
        self.assertEqual(self.controller.store.read()['memory'][self.agent_id]['summary'], before)
        self.assertIn('动作是否完成以 completed_actions', call.call_args.args[2][0]['content'])

    def test_unfollow_known_relationship_not_in_current_sources(self):
        self.controller.store.change(lambda data, conn: agent_relationships.observe(
            data, 10, 'follow', 88, True, '2026-10-19T00:00:00Z', 'test'))
        state, _ = self.decide('unfollow', target_account_id=88)
        self.assertEqual(state['drafts'][0]['target_account_id'], 88)
        self.assertEqual(state['drafts'][0]['source_ids'], ['post:7'])
        self.assertEqual(self.controller.store.read()['memory'][self.agent_id]['decision_versions'], {'post:7': 'v1'})

    def test_unlike_known_liked_post_and_no_authored_post_feedback(self):
        self.controller.store.change(lambda data, conn: agent_relationships.observe(
            data, 10, 'like', 44, True, '2026-10-19T00:00:00Z', 'test'))
        state, _ = self.decide('unlike', target_post_id=44)
        self.assertEqual(state['drafts'][0]['target_post_id'], 44)
        self.assertIsNone(state['drafts'][0]['target_account_id'])
        self.assertEqual(state['agents'][0]['feedback']['known_post_count'], 0)
        self.assertEqual(state['drafts'][0]['source_ids'], ['post:7'])
        self.assertEqual(self.controller.store.read()['memory'][self.agent_id]['decision_versions'], {'post:7': 'v1'})

    def test_unknown_unlike_and_invented_follow_or_self_target_are_rejected(self):
        for action, fields in [('unlike', {'target_post_id': 44}),
                               ('follow', {'target_account_id': 222}),
                               ('follow', {'target_account_id': True}),
                               ('follow', {'target_account_id': 10})]:
            with self.subTest(action=action, fields=fields):
                with self.assertRaises(AgentError):
                    self.decide(action, **fields)
                self.assertEqual(self.controller.store.read()['drafts'], {})

    def test_new_relationship_actions_cannot_resolve_notification_or_write_body(self):
        for fields in ({'notification_ids': [101]}, {'body': 'Some public text'}, {'target_post_id': 7}):
            with self.subTest(fields=fields):
                with self.assertRaises(AgentError):
                    self.decide('follow', target_account_id=99, **fields)
                self.assertEqual(self.controller.store.read()['drafts'], {})

    def test_schema_keeps_account_targets_separate_and_own_account_out(self):
        agent = self.controller.store.read()['agents'][self.agent_id]
        context = {'relations': [{'kind': 'follow', 'target_account_id': 88, 'present': True},
                                 {'kind': 'like', 'target_post_id': 44, 'present': True}]}
        schema = agent_decision.response_schema(agent, [self.source, {**self.source, 'author_id': 10}], context)
        self.assertEqual(schema['properties']['target_account_id']['enum'], [None, 88, 99])
        self.assertEqual(schema['properties']['target_post_id']['enum'], [None, 7, 44])

    def test_permission_subset_is_enforced(self):
        self.controller.save({'id': self.agent_id, 'name': 'Reader', 'allowed_actions': ['post', 'skip']})
        with self.assertRaises(AgentError):
            self.decide('follow', target_account_id=99)

    def test_recorded_outcome_identifies_account_and_reset_preserves_safety_state(self):
        data = self.controller.store.read()
        draft = {'id': 'synthetic', 'agent_id': self.agent_id, 'action': 'follow', 'body': '',
                 'state': 'published', 'target_account_id': 99, 'published_at': '2026-10-20T00:00:00Z'}
        agent_memory.record_outcome(data, draft, 'published', 'Confirmed')
        row = agent_memory.public_view(data, data['agents'][self.agent_id])['completed_actions'][0]
        self.assertEqual(row['target_author_id'], 99)
        agent_relationships.observe(data, 10, 'follow', 99, True, '2026-10-20T00:00:00Z', 'test', changed=True)
        agent_memory.reset(data, self.agent_id, '2026-10-20T00:01:00Z')
        self.assertEqual(agent_memory.public_view(data, data['agents'][self.agent_id])['completed_actions'], [])
        self.assertTrue(agent_relationships.snapshot_for(data, data['agents'][self.agent_id])['relations'][0]['present'])

    def test_upgrade_only_extends_old_full_default_once(self):
        old = ['post', 'reply', 'repost', 'like', 'skip']
        def prepare(data, conn):
            data['rate'].pop('action_capabilities_version', None)
            data['agents'][self.agent_id].update(allowed_actions=old, enabled=False, mode='review')
        self.controller.store.change(prepare)
        store = AgentStore(self.db)
        upgraded = store.read()['agents'][self.agent_id]
        self.assertIn('follow', upgraded['allowed_actions'])
        self.assertEqual((upgraded['enabled'], upgraded['mode']), (False, 'review'))
        store.change(lambda data, conn: data['agents'][self.agent_id].update(allowed_actions=['post', 'skip']))
        self.assertEqual(AgentStore(self.db).read()['agents'][self.agent_id]['allowed_actions'], ['post', 'skip'])

    def test_readonly_confirmation_does_not_increase_interaction_familiarity(self):
        data = self.controller.store.read()
        draft = {'id': 'synthetic', 'agent_id': self.agent_id, 'action': 'follow', 'body': '',
                 'state': 'published', 'target_account_id': 99, 'account_id': 10,
                 'published_at': '2026-10-20T00:00:00Z', 'relationship_already_applied': True}
        agent_memory.record_outcome(data, draft, 'published', 'Existing relationship verified')
        view = agent_memory.public_view(data, data['agents'][self.agent_id])
        self.assertTrue(view['completed_actions'][0]['already_applied'])
        self.assertEqual(view['relationships'], [])
        data['drafts'][draft['id']] = draft
        data['memory'][self.agent_id]['episodes']['outcomes'][0].pop('already_applied')
        old_view = agent_memory.public_view(data, data['agents'][self.agent_id])
        self.assertTrue(old_view['completed_actions'][0]['already_applied'])
        self.assertEqual(old_view['relationships'], [])


if __name__ == '__main__':
    unittest.main()
