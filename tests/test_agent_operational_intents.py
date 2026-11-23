"""Offline lifecycle integration; all models and social interfaces are fakes."""
import copy
import json
import unittest
from unittest.mock import patch

from ctn_data import agent_intents, agent_observation, agent_memory
from ctn_data.agent_control import AgentError
from ctn_data.db import connect
from ctn_data.ingest import save_posts
from tests import test_agent_actions

REAL_OBSERVE = agent_observation.observe


class OperationalIntentTests(unittest.TestCase):
    def setUp(self):
        self.case = test_agent_actions.AutonomousActions(methodName='runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.controller = self.case.controller
        self.agent_id = self.case.agent_id
        for source in self.case.sources:
            source['version'] = 'v1'

    def test_reset_keeps_pending_claim_and_rejects_reworded_duplicate(self):
        draft = self.case.decide('reply', notification_ids=[101])
        self.controller.reset_memory(self.agent_id)
        state = self.controller.store.read()
        view = agent_memory.context_for(state, state['agents'][self.agent_id], [], self.case.now)
        self.assertEqual(len(view['pending_intentions']), 1)
        self.assertTrue(view['pending_intentions'][0]['operational_only'])
        _, choices = agent_observation._new_sources(
            self.controller, state['agents'][self.agent_id], state['memory'][self.agent_id],
            [], [self.case.sources[0]], True)
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0]['notification_ids'], [])
        with self.assertRaisesRegex(AgentError, '已有待办'):
            self.case.decide('reply', body='Different words for the same answer.', notification_ids=[101])
        self.assertEqual(list(self.controller.store.read()['drafts']), [draft['id']])
        self.assertEqual(self.case.social.writes, [])

    def test_real_sql_observation_allows_other_action_but_cannot_duplicate_pending_after_reset(self):
        conn = connect(self.controller.db_path)
        try:
            with conn:
                save_posts(conn, [{'post_id': 7, 'author_id': 99, 'kind': 'post',
                    'body': 'A concrete local referendum topic.', 'visibility': 'public',
                    'created_at': agent_observation._at(self.case.now)}],
                    'trending', observed_at=agent_observation._at(self.case.now))
        finally:
            conn.close()
        def read(agent, token, for_decision=False):
            return REAL_OBSERVE(self.controller, agent, token, refresh=False, for_decision=for_decision)
        with patch.object(self.controller, '_read_sources', side_effect=read), \
                patch.object(agent_observation, '_enrich_notifications', return_value=[]):
            draft = self.case.decide('reply')
            self.controller.reset_memory(self.agent_id)
            self.case.model.reset_mock()
            with self.assertRaisesRegex(AgentError, '已有待办'):
                self.controller.run(self.agent_id)
            self.assertEqual(list(self.controller.store.read()['drafts']), [draft['id']])
            different = self.case.decide('like')
        self.assertEqual(different['action'], 'like')
        self.assertEqual(len(self.controller.store.read()['drafts']), 2)

    def test_discard_is_not_replayed_and_other_action_can_be_considered(self):
        from ctn_data.agent_notifications import reconsider
        draft = self.case.decide('reply', notification_ids=[101])
        self.controller.discard(draft['id'])
        reconsider(self.controller, self.agent_id, 101)
        state = self.controller.store.read()
        _, choices = agent_observation._new_sources(
            self.controller, state['agents'][self.agent_id], state['memory'][self.agent_id],
            [], [self.case.sources[0]], True)
        self.assertEqual(len(choices), 1)
        with self.assertRaisesRegex(AgentError, '已放弃'):
            self.case.decide('reply', body='Reworded rejected reply.', notification_ids=[101])
        different = self.case.decide('like', notification_ids=[101])
        self.assertEqual(different['action'], 'like')
        self.assertEqual(self.case.social.writes, [])
        self.assertEqual(self.case.account.resolved, [])

    def test_discard_guard_survives_memory_reset_and_archived_draft_removal(self):
        draft = self.case.decide('reply', notification_ids=[101])
        self.controller.discard(draft['id'])
        self.controller.reset_memory(self.agent_id)
        self.controller.store.change(lambda data, conn: data['drafts'].pop(draft['id']))
        with self.assertRaisesRegex(AgentError, '已放弃'):
            self.case.decide('reply', body='A new wording does not undo rejection.', notification_ids=[101])
        self.assertEqual(self.case.social.writes, [])

    def test_revised_target_evidence_is_a_new_intent(self):
        draft = self.case.decide('reply')
        self.controller.discard(draft['id'])
        self.case.sources[0].update(version='v2', text='The author has substantially revised the proposal.')
        second = self.case.decide('reply', body='A response to the revised proposal.')
        self.assertNotEqual(draft['id'], second['id'])

    def test_operational_profile_saves_do_not_invalidate_draft(self):
        draft = self.case.decide('reply')
        revision = draft['persona_revision']
        for change in ({'enabled': True}, {'enabled': False}, {'mode': 'auto'},
                       {'interval_seconds': 1200}, {'provider_id': 'another-provider'}):
            self.controller.save({'id': self.agent_id, **change})
            current = self.controller.store.read()['agents'][self.agent_id]
            self.assertEqual(current['persona_revision'], revision)
        self.controller.publish(draft['id'])
        self.assertEqual(len(self.case.social.writes), 1)

    def test_persona_edit_still_invalidates_existing_draft(self):
        draft = self.case.decide('reply')
        self.controller.save({'id': self.agent_id, 'voice': 'A substantially different voice.'})
        with self.assertRaises(AgentError):
            self.controller.publish(draft['id'])
        self.assertEqual(self.case.social.writes, [])

    def test_pending_draft_created_before_binding_still_protects_same_intent(self):
        draft = self.case.decide('reply')
        self.controller.store.change(lambda data, conn: data['drafts'][draft['id']].update(account_id=None))
        with self.assertRaisesRegex(AgentError, '已有待办'):
            self.case.decide('reply', body='Reworded after account binding.')

    def test_reconsider_releases_all_old_discard_holds_but_not_intent_guard(self):
        from ctn_data.agent_notifications import reconsider
        import uuid
        draft = self.case.decide('reply', notification_ids=[101])
        self.controller.discard(draft['id'])
        older = {**draft, 'id': str(uuid.uuid4()), 'state': 'discarded', 'created_at': '2025-01-01T00:00:00Z'}
        self.controller.store.change(lambda data, conn: data['drafts'].update({older['id']: older}))
        reconsider(self.controller, self.agent_id, 101)
        state = self.controller.store.read()
        self.assertEqual(agent_intents.claims(state, self.agent_id, abandoned_only=True), (set(), set()))
        with self.assertRaisesRegex(AgentError, '已放弃'):
            self.case.decide('reply', body='Still not an approved replay.', notification_ids=[101])

    def test_manual_note_survives_automatic_decision_and_raw_model_claim_is_audit_only(self):
        self.controller.memory(self.agent_id, 'Keep my questions about library access in mind.')
        draft = self.case.decide('reply', memory='I already posted my reply.')
        state = self.controller.store.read()
        view = agent_memory.context_for(state, state['agents'][self.agent_id], [], self.case.now)
        self.assertEqual(view['completed_actions'], [])
        self.assertNotIn('I already posted', json.dumps(view))
        self.assertEqual(state['memory'][self.agent_id]['summary'],
                         'Keep my questions about library access in mind.')
        self.assertEqual(state['memory'][self.agent_id]['episodes']['decisions'][0]['reflection'],
                         'I already posted my reply.')
        self.assertEqual(state['drafts'][draft['id']]['state'], 'pending')

    def test_loading_migration_is_idempotent_and_does_not_approve_stale_persona(self):
        draft = self.case.decide('reply')
        def legacy(data, conn):
            agent = data['agents'][self.agent_id]
            agent.pop('persona_revision', None)
            agent['version'] += 5
            data['drafts'][draft['id']].pop('persona_revision', None)
        self.controller.store.change(legacy)
        before = self.controller.store.read()
        after = copy.deepcopy(before)
        agent_intents.migrate(after)
        agent_intents.migrate(after)
        self.assertEqual(before, after)
        self.assertNotIn('persona_revision', after['drafts'][draft['id']])
        with self.assertRaises(AgentError):
            self.controller.publish(draft['id'])


if __name__ == '__main__':
    unittest.main()
