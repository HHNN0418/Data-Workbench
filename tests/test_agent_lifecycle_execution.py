"""Offline execution boundaries for cancelled and revised model intentions."""
import copy
import socket
import unittest
from unittest.mock import patch

from ctn_data import agent_actions
from ctn_data.agent_control import AgentError
from ctn_data.agent_generation import _Task
from ctn_data.agent_lifecycle import draft_matches_persona, persona_revision
from tests import test_agent_actions as action_fixture


class PersonaRevisionCompatibilityTests(unittest.TestCase):
    def test_explicit_revision_survives_operational_version_changes(self):
        agent = {'version': 12, 'persona_revision': 7}
        self.assertEqual(persona_revision(agent), 7)
        self.assertTrue(draft_matches_persona({'persona_version': 8, 'persona_revision': 7}, agent))
        self.assertFalse(draft_matches_persona({'persona_revision': 6}, agent))

    def test_legacy_draft_does_not_inherit_a_matching_revision_by_guessing(self):
        agent = {'version': 12, 'persona_revision': 7}
        self.assertFalse(draft_matches_persona({'persona_version': 7}, agent))
        self.assertTrue(draft_matches_persona({'persona_version': 12}, agent))
        self.assertEqual(persona_revision({'version': 12}), 12)

    def test_missing_and_boolean_revisions_are_not_execution_authority(self):
        self.assertFalse(draft_matches_persona({}, {}))
        self.assertFalse(draft_matches_persona({'persona_revision': True}, {'persona_revision': 1}))


class WaitingForPublicationTests(unittest.TestCase):
    def test_a_draft_from_an_older_persona_does_not_hold_the_generation(self):
        from ctn_data.agent_lifecycle import waiting_for_publication
        agent = {'id': 'm', 'persona_revision': 13,
                 'generation': {'id': 'job', 'status': 'waiting', 'waiting_for': 'publish'}}
        draft = {'agent_id': 'm', 'generation_job_id': 'job', 'state': 'pending', 'persona_revision': 13}
        self.assertTrue(waiting_for_publication({'drafts': {'d': draft}}, agent))
        # The profile was saved after the draft was written: it is never sent, so it never holds the job.
        self.assertFalse(waiting_for_publication({'drafts': {'d': {**draft, 'persona_revision': 12}}}, agent))
        self.assertFalse(waiting_for_publication({'drafts': {'d': {**draft, 'state': 'discarded'}}}, agent))
        self.assertFalse(waiting_for_publication({'drafts': {'d': {**draft, 'generation_job_id': 'old'}}}, agent))
        self.assertFalse(waiting_for_publication(
            {'drafts': {'d': draft}}, {**agent, 'generation': {'id': 'job', 'status': 'running'}}))


class LifecycleExecutionTests(unittest.TestCase):
    def setUp(self):
        # Existing fixture supplies only fake providers/SDK and a TemporaryDirectory.
        self.case = action_fixture.AutonomousActions(methodName='runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('Offline test'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.controller = self.case.controller
        self.agent_id = self.case.agent_id
        self.manager = self.controller.generations

    def draft(self, action='like', **fields):
        return self.case.decide(action, **fields)

    def attach_job(self, draft):
        job_id = 'offline-' + draft['id']
        def attach(data, conn):
            agent = data['agents'][self.agent_id]
            agent.update(enabled=True, mode='auto', generation={
                'id': job_id, 'owner': self.controller.owner, 'status': 'running',
                'stage': 'saving', 'heartbeat_until': self.controller.clock() + 300})
            data['settings']['enabled'] = True
            data['drafts'][draft['id']]['generation_job_id'] = job_id
        self.controller.store.change(attach)
        return job_id

    def state(self, draft):
        return self.controller.store.read()['drafts'][draft['id']]

    def test_metadata_is_bound_to_the_current_worker_only(self):
        self.assertEqual(self.manager.draft_metadata(self.agent_id), {})
        task = _Task(self.agent_id, 'offline-job', True)
        self.manager._local.task = task
        try:
            self.assertEqual(self.manager.draft_metadata(self.agent_id), {'generation_job_id': 'offline-job'})
            self.assertEqual(self.manager.draft_metadata('another-role'), {})
        finally:
            self.manager._local.task = None

    def test_cancelled_saved_result_cannot_automatically_enter_sdk(self):
        draft = self.draft()
        self.attach_job(draft)
        self.manager.cancel(self.agent_id)
        self.assertTrue(self.state(draft)['requires_review'])
        with patch.object(self.controller, '_sdk') as sdk:
            with self.controller._claim(self.agent_id) as token:
                with self.assertRaises(AgentError):
                    agent_actions.execute(self.controller, draft['id'], None, token, automatic=True)
            sdk.assert_not_called()
        self.assertEqual(self.case.social.like_calls, [])

    def test_explicit_confirmation_can_execute_a_cancelled_saved_result(self):
        draft = self.draft()
        self.attach_job(draft)
        self.manager.cancel(self.agent_id)
        self.controller.publish(draft['id'])
        self.assertEqual(self.state(draft)['state'], 'published')
        self.assertNotIn('requires_review', self.state(draft))
        self.assertTrue(self.state(draft)['review_approved'])
        self.assertEqual(len(self.case.social.like_calls), 1)

    def test_cancellation_during_preflight_is_rechecked_by_atomic_reservation(self):
        draft = self.draft()
        self.attach_job(draft)
        account_method = self.controller._account
        def cancel_after_auth(*args):
            account = account_method(*args)
            self.manager.cancel(self.agent_id)
            return account
        with patch.object(self.controller, '_account', side_effect=cancel_after_auth):
            with self.controller._claim(self.agent_id) as token:
                with self.assertRaises(AgentError):
                    agent_actions.execute(self.controller, draft['id'], None, token, automatic=True)
        self.assertEqual(self.state(draft)['state'], 'pending')
        self.assertTrue(self.state(draft)['requires_review'])
        self.assertNotIn('publisher', self.controller.store.read()['leases'])
        self.assertEqual(self.case.social.like_calls, [])

    def test_atomic_sending_transition_closes_cancellation(self):
        draft = self.draft()
        self.attach_job(draft)
        with self.controller._claim(self.agent_id) as token:
            from ctn_data import agent_review
            agent_review.ensure(self.controller, draft['id'], draft['body'],
                                target_result=self.case.social.posts[7], automatic=True, token=token)
            agent_actions.reserve(self.controller, draft['id'], None, token, True,
                                  target_result=self.case.social.posts[7], relationship_present=False)
            with self.assertRaises(AgentError) as caught:
                self.manager.cancel(self.agent_id)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.state(draft)['state'], 'sending')
        self.assertTrue(self.controller.store.read()['agents'][self.agent_id]['generation']['operation_started'])
        self.assertEqual(self.case.social.like_calls, [])

    def test_switch_to_review_during_preflight_blocks_automatic_reservation(self):
        draft = self.draft()
        self.attach_job(draft)
        account_method = self.controller._account
        def review_after_auth(*args):
            account = account_method(*args)
            self.controller.store.change(lambda data, conn: data['agents'][self.agent_id].update(mode='review'))
            return account
        with patch.object(self.controller, '_account', side_effect=review_after_auth):
            with self.controller._claim(self.agent_id) as token:
                with self.assertRaises(AgentError):
                    agent_actions.execute(self.controller, draft['id'], None, token, automatic=True)
        self.assertEqual(self.state(draft)['state'], 'pending')
        self.assertEqual(self.case.social.like_calls, [])

    def test_shutdown_does_not_report_an_entered_write_as_cancelled(self):
        draft = self.draft()
        job_id = self.attach_job(draft)
        task = _Task(self.agent_id, job_id, True)
        self.manager._jobs[self.agent_id] = task
        with self.controller._claim(self.agent_id) as token:
            from ctn_data import agent_review
            agent_review.ensure(self.controller, draft['id'], draft['body'],
                                target_result=self.case.social.posts[7], automatic=True, token=token)
            agent_actions.reserve(self.controller, draft['id'], None, token, True,
                                  target_result=self.case.social.posts[7], relationship_present=False)
        self.manager.stop()
        # stop() deliberately signals every local worker first: that event only
        # interrupts model calls and storage retries, never a platform write.
        # The durable contract is that an entered write is not reported cancelled.
        self.assertEqual(self.controller.store.read()['agents'][self.agent_id]['generation']['status'], 'running')
        self.assertEqual(self.state(draft)['state'], 'sending')
        self.assertEqual(self.case.social.like_calls, [])

    def test_reservation_deferral_does_not_prematurely_close_cancellation(self):
        draft = self.draft()
        self.attach_job(draft)
        self.controller.store.change(lambda data, conn: data['rate'].update(not_before=self.case.now + 60))
        with self.controller._claim(self.agent_id) as token:
            from ctn_data import agent_review
            agent_review.ensure(self.controller, draft['id'], draft['body'],
                                target_result=self.case.social.posts[7], automatic=True, token=token)
            with self.assertRaises(AgentError) as caught:
                agent_actions.reserve(self.controller, draft['id'], None, token, True,
                                      target_result=self.case.social.posts[7], relationship_present=False)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertFalse(self.controller.store.read()['agents'][self.agent_id]['generation'].get('operation_started'))
        self.manager.cancel(self.agent_id)
        self.assertTrue(self.state(draft)['requires_review'])

    def test_cancel_does_not_mark_an_unrelated_or_manual_intention(self):
        draft = self.draft()
        job_id = self.attach_job(draft)
        def extras(data, conn):
            other = copy.deepcopy(data['drafts'][draft['id']])
            other.update(id='unrelated', generation_job_id='another-job')
            data['drafts']['unrelated'] = other
            manual = copy.deepcopy(other)
            manual.update(id='manual', generation_job_id=job_id, origin='manual', manual_only=True,
                          manual_prepared=True, user_instruction='Synthetic manual instruction')
            data['drafts']['manual'] = manual
        self.controller.store.change(extras)
        self.manager.cancel(self.agent_id)
        rows = self.controller.store.read()['drafts']
        self.assertTrue(rows[draft['id']]['requires_review'])
        self.assertNotIn('requires_review', rows['unrelated'])
        self.assertNotIn('requires_review', rows['manual'])

    def test_cancelled_skip_does_not_acknowledge_notifications(self):
        draft = self.draft('skip', notification_ids=[101])
        self.attach_job(draft)
        self.manager.cancel(self.agent_id)
        with self.controller._claim(self.agent_id) as token:
            agent = self.controller.store.read()['agents'][self.agent_id]
            agent_actions.resolve_notifications(self.controller, agent, token, draft['id'])
        self.assertEqual(self.case.account.resolved, [])
        self.assertTrue(self.state(draft)['requires_review'])

    def test_skip_acknowledgement_closes_cancellation_before_remote_mutation(self):
        draft = self.draft('skip', notification_ids=[101])
        self.attach_job(draft)
        original_resolve = self.case.account.resolve_notification
        def resolve(notification_id):
            with self.assertRaises(AgentError):
                self.manager.cancel(self.agent_id)
            original_resolve(notification_id)
        self.case.account.resolve_notification = resolve
        with self.controller._claim(self.agent_id) as token:
            agent = self.controller.store.read()['agents'][self.agent_id]
            agent_actions.resolve_notifications(self.controller, agent, token, draft['id'])
        self.assertEqual(self.case.account.resolved, [101])
        self.assertTrue(self.state(draft)['notifications_resolved'])

    def test_explicitly_approved_skip_can_acknowledge_after_cancellation(self):
        draft = self.draft('skip', notification_ids=[101])
        self.attach_job(draft)
        self.manager.cancel(self.agent_id)
        def approve(data, conn):
            data['drafts'][draft['id']].update(requires_review=False, review_approved=True)
        self.controller.store.change(approve)
        with self.controller._claim(self.agent_id) as token:
            agent = self.controller.store.read()['agents'][self.agent_id]
            agent_actions.resolve_notifications(self.controller, agent, token, draft['id'], automatic=False)
        self.assertEqual(self.case.account.resolved, [101])

    def test_execution_uses_persona_revision_instead_of_operational_version(self):
        draft = self.draft()
        def revise_settings(data, conn):
            agent = data['agents'][self.agent_id]
            data['drafts'][draft['id']]['persona_revision'] = persona_revision(agent)
            agent['persona_revision'] = persona_revision(agent)
            agent['version'] += 10
        self.controller.store.change(revise_settings)
        self.controller.publish(draft['id'])
        self.assertEqual(self.state(draft)['state'], 'published')

    def test_actual_persona_revision_change_blocks_before_sdk(self):
        draft = self.draft()
        def revise_persona(data, conn):
            agent = data['agents'][self.agent_id]
            revision = persona_revision(agent)
            data['drafts'][draft['id']]['persona_revision'] = revision
            agent['persona_revision'] = revision + 1
        self.controller.store.change(revise_persona)
        with patch.object(self.controller, '_sdk') as sdk:
            with self.assertRaises(AgentError):
                self.controller.publish(draft['id'])
            sdk.assert_not_called()


if __name__ == '__main__':
    unittest.main()
