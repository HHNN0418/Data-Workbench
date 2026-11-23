"""Notification decisions and platform acknowledgements have separate lifecycles."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from ctn_data import agent_notifications, agent_observation
from ctn_data.agent_control import AgentError
from ctn_data.agent_routes import DraftID, NotificationReconsiderRequest, register_agent_routes
from tests import test_agent_actions as action_fixtures


class NotificationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.case = action_fixtures.AutonomousActions(methodName='runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.controller = self.case.controller
        self.agent_id = self.case.agent_id
        for source in self.case.sources:
            source.update(version='v1', origin='notification')

    def data(self):
        return self.controller.store.read()

    def inbox(self):
        return {row['id']: row for row in agent_notifications.enrich_inbox(self.data(), self.agent_id)}

    def test_received_reply_can_be_handled_by_a_like_and_progress_is_read_only(self):
        draft = self.case.decide('like', notification_ids=[101])
        before = self.data()
        notice = self.inbox()[101]
        self.assertEqual(notice['local_progress'], 'awaiting_review')
        self.assertEqual(notice['related_action'], 'like')
        self.assertEqual(notice['status'], 'unresolved')
        self.assertEqual(self.data(), before)
        self.controller.publish(draft['id'])
        completed = self.inbox()[101]
        self.assertEqual(completed['local_progress'], 'completed')
        self.assertEqual(completed['status'], 'resolved')
        self.assertEqual(completed['related_draft_id'], draft['id'])

    def test_review_skip_requires_explicit_confirmation_of_only_selected_ids(self):
        draft = self.case.decide('skip', notification_ids=[101])
        self.assertEqual(self.inbox()[101]['local_progress'], 'skipped_awaiting_confirmation')
        self.assertTrue(self.inbox()[101]['can_confirm_skip'])
        self.assertEqual(self.case.account.resolved, [])
        agent_notifications.confirm_skip(self.controller, draft['id'])
        self.assertEqual(self.case.account.resolved, [101])
        self.assertEqual(self.inbox()[102]['status'], 'unresolved')
        self.assertEqual(self.inbox()[101]['local_progress'], 'skipped')
        row = self.data()['drafts'][draft['id']]
        self.assertTrue(row['notification_confirmation_authorized_at'])
        self.assertEqual(row['state'], 'skipped')
        self.assertTrue(row['review_approved'])
        agent_notifications.confirm_skip(self.controller, draft['id'])
        self.assertEqual(self.case.account.resolved, [101])

    def test_confirmation_failure_keeps_skip_and_retries_only_acknowledgement(self):
        draft = self.case.decide('skip', notification_ids=[101])
        original = self.case.account.resolve_notification
        self.case.account.resolve_notification = lambda notification_id: (_ for _ in ()).throw(TimeoutError())
        agent_notifications.confirm_skip(self.controller, draft['id'])
        state = self.data()
        self.assertEqual(state['drafts'][draft['id']]['state'], 'skipped')
        self.assertFalse(state['drafts'][draft['id']]['notifications_resolved'])
        self.assertEqual(self.inbox()[101]['status'], 'ack_pending')
        self.assertEqual(self.case.social.writes, [])
        self.assertEqual(self.case.social.like_calls, [])
        self.case.account.resolve_notification = original
        self.case.now += 10
        with self.controller._claim(self.agent_id) as token:
            agent_observation.drain_resolutions(self.controller, self.data()['agents'][self.agent_id], token)
        self.assertEqual(self.case.account.resolved, [101])
        self.assertEqual(self.inbox()[101]['status'], 'resolved')
        self.assertEqual(self.case.social.writes, [])

    def test_explicit_skip_confirmation_authorizes_a_cancelled_skip_without_manual_origin(self):
        draft = self.case.decide('skip', notification_ids=[101])
        self.controller.store.change(lambda data, conn: data['drafts'][draft['id']].update(
            requires_review=True, review_hold_reason='生成已取消，待审阅'))
        agent_notifications.confirm_skip(self.controller, draft['id'])
        row = self.data()['drafts'][draft['id']]
        self.assertFalse(row['requires_review'])
        self.assertTrue(row['review_approved'])
        self.assertNotEqual(row.get('origin'), 'manual')
        self.assertEqual(self.case.account.resolved, [101])

    def test_discard_does_not_reopen_automatically_and_reconsider_changes_only_selected_input_cache(self):
        draft = self.case.decide('reply', notification_ids=[101])
        self.controller.discard(draft['id'])
        self.assertEqual(self.inbox()[101]['local_progress'], 'discarded')
        self.assertTrue(self.inbox()[101]['can_reconsider'])
        before = self.data()
        own = before['memory'][self.agent_id]
        self.assertIn(101, own['decision_notification_ids'])
        self.case.model.reset_mock()
        agent_notifications.reconsider(self.controller, self.agent_id, 101)
        after = self.data()
        self.assertNotIn(101, after['memory'][self.agent_id]['decision_notification_ids'])
        self.assertNotIn('post:7', after['memory'][self.agent_id]['decision_versions'])
        self.assertEqual(after['drafts'][draft['id']], before['drafts'][draft['id']])
        self.assertEqual(after['agents'][self.agent_id]['next_run_at'], before['agents'][self.agent_id]['next_run_at'])
        self.assertEqual(self.inbox()[101]['status'], 'unresolved')
        self.assertEqual(self.inbox()[101]['local_progress'], 'reconsidered')
        self.assertFalse(self.inbox()[101]['can_reconsider'])
        self.assertNotIn('reconsidered_at', self.inbox()[102])
        self.case.model.assert_not_called()
        self.assertEqual(self.case.social.writes, [])
        self.assertEqual(self.case.account.resolved, [])
        agent_notifications.reconsider(self.controller, self.agent_id, 101)
        self.assertEqual(self.inbox()[101]['local_progress'], 'reconsidered')

    def test_reconsider_rejects_active_claims_confirmed_notices_and_unrelated_drafts(self):
        draft = self.case.decide('reply', notification_ids=[101])
        with self.assertRaises(AgentError):
            agent_notifications.reconsider(self.controller, self.agent_id, 101)
        with self.assertRaises(AgentError):
            agent_notifications.reconsider(self.controller, self.agent_id, 102)
        with self.assertRaises(AgentError):
            agent_notifications.confirm_skip(self.controller, draft['id'])
        self.controller.publish(draft['id'])
        with self.assertRaises(AgentError):
            agent_notifications.reconsider(self.controller, self.agent_id, 101)

    def test_reconsider_allows_another_action_but_not_a_reworded_abandoned_reply(self):
        draft = self.case.decide('reply', body='A first intended reply.', notification_ids=[101])
        self.controller.discard(draft['id'])
        agent_notifications.reconsider(self.controller, self.agent_id, 101)
        with self.assertRaises(AgentError):
            self.case.decide('reply', body='The same reply with different wording.', notification_ids=[101])
        alternate = self.case.decide('like', notification_ids=[101])
        self.assertEqual(alternate['action'], 'like')
        self.assertEqual(alternate['state'], 'pending')
        self.assertEqual(self.case.social.writes, [])
        self.assertEqual(self.case.social.like_calls, [])


class NotificationRouteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.app = FastAPI()
        self.app.state.auth = SimpleNamespace(user=lambda request: bool(request.headers.get('X-Test-Member')))
        controller = register_agent_routes(self.app, directory / 'routes.sqlite3', directory / 'secrets')
        self.addCleanup(controller.stop)

    def route(self, suffix):
        return next(route for route in self.app.routes
                    if getattr(route, 'path', '') == '/api/agents/notifications/' + suffix)

    def test_routes_require_membership_and_strict_notice_ids(self):
        for route, payload in (('confirm-skip', {'draft_id': 'draft'}),
                               ('reconsider', {'agent_id': 'role', 'notification_id': 101})):
            registered = self.route(route)
            self.assertEqual(registered.methods, {'POST'})
            authorization = registered.dependant.dependencies[0].call
            with self.assertRaises(HTTPException) as raised:
                authorization(Request({'type': 'http', 'headers': []}))
            self.assertEqual(raised.exception.status_code, 401)
            authorization(Request({'type': 'http', 'headers': [(b'x-test-member', b'yes')]}))
        for value in (True, '101', 0, -1):
            with self.assertRaises(ValidationError):
                NotificationReconsiderRequest.model_validate({'agent_id': 'role', 'notification_id': value})
        with self.assertRaises(ValidationError):
            DraftID.model_validate({'draft_id': 'selected', 'notification_ids': [999]})

    def test_routes_dispatch_only_the_explicit_selected_draft_or_notification(self):
        with patch('ctn_data.agent_notifications.confirm_skip', return_value={'ok': True}) as confirm:
            result = self.route('confirm-skip').endpoint(DraftID(draft_id='skip-selected'))
            self.assertEqual(result, {'ok': True})
            self.assertEqual(confirm.call_args.args[1:], ('skip-selected',))
        with patch('ctn_data.agent_notifications.reconsider', return_value={'ok': True}) as reconsider:
            result = self.route('reconsider').endpoint(NotificationReconsiderRequest(agent_id='role', notification_id=101))
            self.assertEqual(result, {'ok': True})
            self.assertEqual(reconsider.call_args.args[1:], ('role', 101))


if __name__ == '__main__':
    unittest.main()
