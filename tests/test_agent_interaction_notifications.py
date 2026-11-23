"""Internal notifications remain visible without waking decisions or ack retries."""
import unittest
import uuid

from ctn_data import agent_actions, agent_notifications, agent_observation as observation
from tests import test_agent_observation as fixtures


class InternalNotificationTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.AgentObservationTests(methodName='runTest')
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.controller = self.case.controller
        self.agent_id = self.case.agent_id
        self.case.bind()
        self.controller.save({'name': 'Other Agent', 'username': 'internal_peer'})
        peer = next(row for row in self.controller.snapshot()['agents'] if row['id'] != self.agent_id)
        self.peer_id = peer['id']
        self.controller.store.change(lambda data, conn: data['agents'][self.peer_id].update(
            account_id=72, account_state='ready', enabled=False))

    def state(self):
        return self.controller.store.read()

    def inbox(self):
        return {row['id']: row for row in self.case.memory().get('inbox', [])}

    def refresh(self):
        with self.controller._claim(self.agent_id) as token:
            observation.refresh(self.controller, self.case.agent(), token)

    def seed(self, notices, **memory):
        def update(data, conn):
            data['memory'].setdefault(self.agent_id, {}).update(inbox=notices, **memory)
            agent_notifications.apply_interaction_policy(data, self.agent_id, conn)
        self.controller.store.change(update)

    def setting(self, enabled):
        def update(data, conn):
            data['settings']['block_agent_interactions'] = enabled
            agent_notifications.apply_interaction_policy(data, self.agent_id, conn)
        self.controller.store.change(update)

    def test_sdk_actor_is_preserved_and_target_author_is_not_a_like_actor(self):
        notices, _ = observation._notifications([
            {'notification_id': 1, 'kind': 'reply', 'actor': {'account_id': 72, 'username': 'internal_peer'},
             'post': {'post_id': 10, 'author_id': 72}},
            {'notification_id': 2, 'kind': 'like', 'actor': {'account_id': 90, 'username': 'visitor'},
             'post': {'post_id': 10, 'author_id': 72}},
            {'notification_id': 3, 'kind': 'like', 'post': {'post_id': 10, 'author_id': 72}},
            {'notification_id': 4, 'kind': 'follow', 'post': {'post_id': 10, 'author_id': 72}},
            {'notification_id': 5, 'kind': 'mention', 'post': {'post_id': 11, 'author_id': 72}},
            {'notification_id': 6, 'kind': 'mention', 'actor': {'username': 'INTERNAL_PEER'},
             'post': {'post_id': 12}},
            {'notification_id': 7, 'kind': 'reply', 'actor': {'account_id': 90},
             'post': {'post_id': 13, 'author_id': 90, 'root_post_id': 10, 'reply_to_post_id': 10}},
        ], observation._at(self.case.now))
        self.assertEqual([row.get('actor_account_id') for row in notices], [72, 90, None, None, 72, None, 90])
        self.assertEqual(notices[0]['actor_username'], 'internal_peer')
        self.seed(notices)
        self.assertEqual([self.inbox()[value]['policy_ignored'] for value in range(1, 8)],
                         [True, False, False, False, True, True, False])

    def test_internal_reply_is_visible_but_does_not_wake_reading_or_decisions(self):
        post = self.case.post(10, 'Internal reply.', age=86400, author_id=72, kind='reply')
        self.case.social.notices = [{'notification_id': 1, 'kind': 'reply',
                                    'actor': {'account_id': 72}, 'post': post}]
        self.refresh()
        memory = self.case.memory()
        self.assertEqual(memory['observation']['unresolved_count'], 0)
        self.assertEqual(observation.pending_revision(self.controller, self.case.agent(), memory), '')
        self.assertEqual(self.case.read(for_decision=True), [])
        self.assertEqual(self.case.read(), [])
        self.assertEqual(self.case.social.resolved, [])
        visible = agent_notifications.enrich_inbox(self.state(), self.agent_id)[0]
        self.assertEqual(visible['local_progress'], 'policy_ignored')
        self.assertEqual(visible['status'], 'unresolved')
        self.assertFalse(visible['can_confirm_skip'])
        self.assertFalse(visible['can_reconsider'])

    def test_internal_like_and_follow_with_actor_ids_are_ignored(self):
        post = self.case.post(10, 'My old post.', age=86400, author_id=71)
        self.case.social.notices = [
            {'notification_id': 1, 'kind': 'like', 'actor_id': 72, 'post': post},
            {'notification_id': 2, 'kind': 'follow', 'actor_account_id': 72},
            {'notification_id': 3, 'kind': 'like', 'actor_id': 90, 'post': post},
        ]
        self.refresh()
        self.assertEqual(self.case.memory()['observation']['unresolved_count'], 1)
        self.assertEqual([row['notification_ids'] for row in self.case.read(for_decision=True)], [[3]])
        self.assertEqual(self.case.social.resolved, [])

    def test_unknown_like_actor_does_not_inherit_internal_target_author(self):
        post = self.case.post(10, 'An internal post.', age=86400, author_id=72)
        self.case.social.notices = [{'notification_id': 1, 'kind': 'like', 'post': post}]
        self.refresh()
        self.assertFalse(self.inbox()[1]['policy_ignored'])
        self.assertTrue(observation.pending_revision(self.controller, self.case.agent(), self.case.memory()))
        self.assertEqual(self.case.read(for_decision=True)[0]['notification_ids'], [1])

    def test_historical_reply_uses_cached_post_author_and_actor_username_is_supported(self):
        self.case.post(10, 'Historical internal reply.', age=86400, author_id=72)
        self.seed([
            {'id': 1, 'kind': 'reply', 'post_id': 10, 'status': 'unresolved'},
            {'id': 2, 'kind': 'mention', 'actor_username': '@INTERNAL_PEER', 'status': 'unresolved'},
            {'id': 3, 'kind': 'like', 'post_id': 10, 'status': 'unresolved'},
        ])
        notices = self.inbox()
        self.assertEqual(notices[1]['actor_account_id'], 72)
        self.assertTrue(notices[1]['policy_ignored'])
        self.assertTrue(notices[2]['policy_ignored'])
        self.assertFalse(notices[3]['policy_ignored'])
        self.assertEqual(self.case.memory()['observation']['unresolved_count'], 1)

    def test_refresh_keeps_known_actor_and_local_state_when_new_payload_omits_actor(self):
        post = self.case.post(10, 'An old post.', age=86400, author_id=71)
        self.case.social.notices = [{'notification_id': 1, 'kind': 'like', 'actor_id': 72, 'post': post}]
        self.refresh()
        initial_at = self.inbox()[1]['policy_ignored_at']
        self.case.now += 61
        self.case.social.notices = [{'notification_id': 1, 'kind': 'like', 'post': post}]
        self.refresh()
        notice = self.inbox()[1]
        self.assertEqual(notice['actor_account_id'], 72)
        self.assertEqual(notice['policy_ignored_at'], initial_at)
        self.assertTrue(notice['policy_ignored'])

    def test_fetching_missing_reply_identifies_actor_before_offering_source(self):
        self.seed([{'id': 1, 'kind': 'reply', 'post_id': 10, 'status': 'unresolved'}])
        self.case.social.posts[10] = {'post_id': 10, 'kind': 'reply', 'author_id': 72, 'body': 'Internal reply.'}
        self.assertEqual(self.case.read(for_decision=True), [])
        self.assertIn(('get_post', 10), self.case.social.calls)
        self.assertTrue(self.inbox()[1]['policy_ignored'])
        self.assertEqual(self.case.memory()['observation']['unresolved_count'], 0)

    def test_pending_ack_is_cancelled_locally_and_setting_off_does_not_resume_it(self):
        prior_draft_id = str(uuid.uuid4())
        self.controller.store.change(lambda data, conn: data['drafts'].update({
            prior_draft_id: {'id': prior_draft_id, 'agent_id': self.agent_id, 'state': 'published',
                              'action': 'like', 'notification_ids': [1], 'notifications_resolved': False,
                              'created_at': observation._at(self.case.now), 'version': 1}}))
        self.seed([{'id': 1, 'kind': 'like', 'actor_account_id': 72, 'status': 'ack_pending'}],
                  notification_outbox={'1': {'notification_id': 1, 'state': 'pending',
                                            'next_retry_at': 0, 'attempts': 0}})
        self.assertEqual(self.case.memory()['notification_outbox']['1']['state'], 'policy_ignored')
        with self.controller._claim(self.agent_id) as token:
            observation.drain_resolutions(self.controller, self.case.agent(), token)
            self.assertFalse(observation.resolve_selected(self.controller, self.case.agent(), token, [1]))
        self.assertEqual(self.case.social.resolved, [])
        self.setting(False)
        with self.controller._claim(self.agent_id) as token:
            observation.drain_resolutions(self.controller, self.case.agent(), token)
            # This is the same entry point used by maintenance for an already
            # published action whose acknowledgement had failed earlier.
            agent_actions.resolve_notifications(self.controller, self.case.agent(), token, prior_draft_id)
        self.assertEqual(self.case.social.resolved, [])
        self.assertTrue(self.state()['drafts'][prior_draft_id]['notifications_resolved'])
        self.assertFalse(self.inbox()[1]['policy_ignored'])
        self.assertEqual(self.inbox()[1]['status'], 'unresolved')
        # Only a newly authorized decision can enqueue confirmation again.
        with self.controller._claim(self.agent_id) as token:
            self.assertTrue(observation.resolve_selected(self.controller, self.case.agent(), token, [1],
                                                        draft_id='future-decision'))
        self.assertEqual(self.case.social.resolved, [1])

    def test_disabled_policy_reopens_ignored_input_without_consuming_decision_ids(self):
        post = self.case.post(10, 'Internal question.', age=86400, author_id=72, kind='reply')
        self.case.social.notices = [{'notification_id': 1, 'kind': 'reply', 'post': post}]
        self.refresh()
        self.assertEqual(self.case.read(for_decision=True), [])
        self.assertNotIn(1, self.case.memory().get('decision_notification_ids', []))
        self.setting(False)
        self.assertTrue(observation.pending_revision(self.controller, self.case.agent(), self.case.memory()))
        self.assertEqual(self.case.read(for_decision=True)[0]['notification_ids'], [1])
        self.assertEqual(self.case.social.resolved, [])

    def test_failed_inflight_ack_cannot_restore_cancelled_retry_after_toggle(self):
        self.setting(False)
        self.seed([{'id': 1, 'kind': 'like', 'actor_account_id': 72, 'status': 'unresolved'}])

        def fail_after_toggle(notification_id):
            self.setting(True)
            self.setting(False)
            raise TimeoutError('The acknowledgement did not complete.')

        self.case.social.resolve_notification = fail_after_toggle
        with self.controller._claim(self.agent_id) as token:
            self.assertFalse(observation.resolve_selected(self.controller, self.case.agent(), token, [1],
                                                         draft_id='earlier-decision'))
        self.assertEqual(self.case.memory()['notification_outbox']['1']['state'], 'policy_ignored')
        self.assertEqual(self.inbox()[1]['status'], 'unresolved')
        self.assertEqual(self.case.social.resolved, [])


if __name__ == '__main__':
    unittest.main()
