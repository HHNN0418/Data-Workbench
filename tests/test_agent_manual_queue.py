"""D26: a confirmed manual post waits for the shared allowance and reaches the platform at most once."""
import unittest
from unittest.mock import patch

from capturethenarrative._http import APIError

from ctn_data import agent_manual
from ctn_data.agent_budget import Deferred
from ctn_data.agent_control import AgentError
from tests import test_agent_manual_api as api
from tests import test_agent_manual_execution as execution


class ManualQueueTests(unittest.TestCase):
    def setUp(self):
        execution.ManualExecutionTests.setUp(self)
        self.waits = []  # retry_after of the next refused post reservations
        previous = self.call.side_effect
        def call(controller, bucket, method, *args, **kwargs):
            if bucket == 'post' and self.waits:
                raise Deferred(bucket, self.waits.pop(0), '全队自定义额度等待中', quota_policy=True)
            return previous(controller, bucket, method, *args, **kwargs)
        self.call.side_effect = call

    def draft(self, action='post', **kwargs):
        kwargs.setdefault('body', 'A confirmed operator post.')
        return execution.ManualExecutionTests.draft(self, action, **kwargs)

    def row(self, draft_id):
        return self.controller.store.read()['drafts'][draft_id]

    def queue(self, draft_id):
        return self.row(draft_id).get('manual_queue', {})

    def enqueue(self, draft_id, **kwargs):
        return agent_manual.queue_publish(self.controller, draft_id, requested_by='member@example.test', **kwargs)

    def hold(self, draft_id):
        self.controller.store.change(lambda data, conn: data['rate'].setdefault('priority_holds', {}).setdefault(
            'post', {}).update({draft_id: {'priority': 0, 'until': self.now + 3600}}))

    def holds(self):
        return self.controller.store.read()['rate'].get('priority_holds', {}).get('post', {})

    def test_available_allowance_sends_immediately_and_only_once(self):
        draft_id = self.draft()
        result = self.enqueue(draft_id)
        self.assertEqual(result['manual_queue']['state'], 'sent')
        self.assertEqual(result['manual_queue']['attempts'], 1)
        row = self.row(draft_id)
        self.assertEqual(row['state'], 'published')
        self.assertEqual(result['manual_queue']['post_id'], row['post_id'])
        self.now += 120
        self.assertFalse(agent_manual.drain_queue(self.controller))
        self.assertEqual(len(self.social.writes), 1)
        kinds = [item['kind'] for item in self.controller.logs(self.agent_id)['items']]
        self.assertIn('manual_queued', kinds)
        self.assertIn('manual_queue_sent', kinds)

    def test_exhausted_allowance_waits_then_sends_exactly_once(self):
        draft_id = self.draft('reply', body='A queued reply.')
        self.waits = [12]
        result = self.enqueue(draft_id)
        queue = result['manual_queue']
        self.assertEqual(queue['state'], 'waiting')
        self.assertEqual(queue['next_attempt_at'], self.now + 12)
        self.assertEqual(queue['requested_by'], 'member@example.test')
        self.assertEqual(self.row(draft_id)['state'], 'pending')
        self.assertEqual(self.social.writes, [])
        self.now += 5
        self.assertFalse(agent_manual.drain_queue(self.controller))
        self.assertEqual(self.social.writes, [])
        self.now += 8
        self.assertTrue(agent_manual.drain_queue(self.controller))
        self.assertEqual(self.queue(draft_id)['state'], 'sent')
        self.assertEqual(self.queue(draft_id)['attempts'], 2)
        self.assertEqual(len(self.social.writes), 1)
        self.assertEqual(self.social.writes[0]['reply_to_post_id'], 7)
        self.now += 300
        agent_manual.drain_queue(self.controller)
        self.assertEqual(len(self.social.writes), 1)

    def test_retries_follow_retry_after_and_expiry_releases_the_hold(self):
        draft_id = self.draft()
        self.waits = [30] * 100
        self.enqueue(draft_id)
        self.hold(draft_id)
        attempts = 1
        while self.queue(draft_id)['state'] == 'waiting' and attempts < 60:
            self.now = self.queue(draft_id)['next_attempt_at']
            agent_manual.drain_queue(self.controller)
            attempts += 1
        queue = self.queue(draft_id)
        self.assertEqual(queue['state'], 'expired')
        self.assertLessEqual(queue['attempts'], agent_manual.QUEUE_SECONDS // 30 + 1)
        self.assertEqual(self.row(draft_id)['state'], 'pending')
        self.assertEqual(self.social.writes, [])
        self.assertNotIn(draft_id, self.holds())

    def test_uncertain_delivery_stops_and_is_never_resent(self):
        draft_id = self.draft()
        self.social.timeout = 'post'
        with self.assertRaises(AgentError):
            self.enqueue(draft_id)
        self.assertEqual(self.row(draft_id)['state'], 'uncertain')
        self.assertEqual(self.queue(draft_id)['state'], 'stopped')
        self.social.timeout = ''
        for _ in range(3):
            self.now += 60
            agent_manual.drain_queue(self.controller)
        self.assertEqual(len(self.social.writes), 1)

    def test_definite_platform_rejection_stops_without_retry(self):
        draft_id = self.draft()
        def reject(body):
            raise APIError('POST', 'https://social.legitreal.com/api/posts', 422, '{"error":"invalid"}')
        self.account.post = reject
        with self.assertRaises(AgentError):
            self.enqueue(draft_id)
        self.assertEqual(self.row(draft_id)['state'], 'rejected')
        self.assertEqual(self.queue(draft_id)['state'], 'stopped')
        self.now += 60
        self.assertFalse(agent_manual.drain_queue(self.controller))
        self.assertEqual(self.social.writes, [])

    def test_publisher_lease_and_platform_wait_are_local_refusals_that_retry(self):
        for name, change in (('publisher', lambda data: data['leases'].update(
                                 publisher={'until': self.now + 30, 'token': 'someone'})),
                             ('not_before', lambda data: data['rate'].update(not_before=self.now + 20))):
            with self.subTest(wait=name):
                draft_id = self.draft(body='Waiting for ' + name)
                self.controller.store.change(lambda data, conn: change(data))
                self.assertEqual(self.enqueue(draft_id)['manual_queue']['state'], 'waiting')
                self.assertEqual(self.row(draft_id)['state'], 'pending')
                self.controller.store.change(lambda data, conn: (
                    data['leases'].pop('publisher', None), data['rate'].pop('not_before', None)))
                self.now = self.queue(draft_id)['next_attempt_at']
                self.assertTrue(agent_manual.drain_queue(self.controller))
                self.assertEqual(self.queue(draft_id)['state'], 'sent')
        self.assertEqual(len(self.social.writes), 2)

    def test_cancel_and_discard_release_the_hold_and_never_send(self):
        cancelled = self.draft(body='Cancelled while waiting.')
        self.waits = [20]
        self.enqueue(cancelled)
        self.hold(cancelled)
        result = agent_manual.cancel_queue(self.controller, cancelled)
        self.assertEqual(result['manual_queue']['state'], 'cancelled')
        self.assertNotIn(cancelled, self.holds())
        with self.assertRaisesRegex(AgentError, '没有在排队'):
            agent_manual.cancel_queue(self.controller, cancelled)
        discarded = self.draft(body='Discarded while waiting.')
        self.waits = [20]
        self.enqueue(discarded)
        self.hold(discarded)
        self.controller.discard(discarded)
        self.now += 30
        agent_manual.drain_queue(self.controller)
        self.assertEqual(self.queue(discarded)['state'], 'cancelled')
        self.assertNotIn(discarded, self.holds())
        self.assertEqual(self.row(cancelled)['state'], 'pending')
        self.assertEqual(self.social.writes, [])

    def test_only_pending_prepared_posts_with_their_previewed_body_can_queue(self):
        cases = (('automatic draft', self.draft(markers={'origin': 'automatic', 'manual_only': False,
                                                          'manual_prepared': False}), {}, '手动操作'),
                 ('relationship action', self.draft('like', body=''), {}, '发帖、回复和转发'),
                 ('edited body', self.draft(), {'body': 'Different words.'}, '已预览的正文'))
        for name, draft_id, kwargs, message in cases:
            with self.subTest(name), self.assertRaisesRegex(AgentError, message):
                self.enqueue(draft_id, **kwargs)
        published = self.draft(body='Already sent.')
        self.controller.publish(published)
        with self.assertRaisesRegex(AgentError, '待确认'):
            self.enqueue(published)
        self.assertEqual(len(self.social.writes), 1)

    def test_repeated_confirmation_keeps_one_waiting_request(self):
        draft_id = self.draft()
        self.waits = [40]
        first = self.enqueue(draft_id)['manual_queue']
        second = self.enqueue(draft_id)['manual_queue']
        self.assertEqual(second['requested_at'], first['requested_at'])
        self.assertEqual(second['attempts'], 1)
        self.assertEqual(self.social.writes, [])

    def test_send_in_flight_elsewhere_leaves_the_queue_to_settle_later(self):
        draft_id = self.draft()
        self.waits = [10]
        self.enqueue(draft_id)
        self.controller.store.change(lambda data, conn: data['drafts'][draft_id].update(
            state='sending', lease_until=self.now + 600))
        self.now += 11
        self.assertFalse(agent_manual.drain_queue(self.controller))
        self.assertEqual(self.queue(draft_id)['state'], 'waiting')
        self.assertEqual(self.queue(draft_id)['attempts'], 1)
        self.controller.store.change(lambda data, conn: data['drafts'][draft_id].update(
            state='published', post_id=4242))
        agent_manual.drain_queue(self.controller)
        self.assertEqual(self.queue(draft_id)['state'], 'sent')
        self.assertEqual(self.queue(draft_id)['post_id'], 4242)
        self.assertEqual(self.social.writes, [])

    def test_teammate_reply_while_waiting_stops_the_queue_before_sending(self):
        draft_id = self.draft('reply', body='A queued reply.')
        self.waits = [10]
        self.enqueue(draft_id)
        self.hold(draft_id)
        self.controller.store.change(lambda data, conn: data['drafts'].update({'teammate': {
            'id': 'teammate', 'agent_id': 'other-role', 'action': 'reply', 'target_post_id': 7,
            'state': 'published', 'post_id': 5555}}))
        self.now += 11
        agent_manual.drain_queue(self.controller)
        queue = self.queue(draft_id)
        self.assertEqual(queue['state'], 'stopped')
        self.assertIn('我方已有账号回复过', queue['reason'])
        self.assertEqual(self.row(draft_id)['state'], 'pending')
        self.assertNotIn(draft_id, self.holds())
        self.assertEqual(self.social.writes, [])

    def test_condition_acknowledged_at_preparation_does_not_stop_the_queue(self):
        draft_id = self.draft('reply', body='A queued reply.',
                              markers={'manual_warnings_acknowledged': ['team_already_replied']})
        self.waits = [10]
        self.enqueue(draft_id)
        self.controller.store.change(lambda data, conn: data['drafts'].update({'teammate': {
            'id': 'teammate', 'agent_id': 'other-role', 'action': 'reply', 'target_post_id': 7,
            'state': 'published', 'post_id': 5555}}))
        self.now += 11
        self.assertTrue(agent_manual.drain_queue(self.controller))
        self.assertEqual(self.queue(draft_id)['state'], 'sent')
        self.assertEqual(len(self.social.writes), 1)

    def test_scheduler_tick_drains_even_while_autonomy_is_paused(self):
        draft_id = self.draft()
        self.controller.store.change(lambda data, conn: data['settings'].update(enabled=False))
        self.waits = [10]
        self.enqueue(draft_id)
        self.now += 11
        with patch.object(self.controller.generations, 'submit') as submit:
            self.controller.tick()
        submit.assert_not_called()
        self.assertEqual(self.queue(draft_id)['state'], 'sent')
        self.assertEqual(len(self.social.writes), 1)


class ManualQueueApiTests(unittest.TestCase):
    def setUp(self):
        api.ManualApiTests.setUp(self)

    def prepare(self, *args, **kwargs):
        return api.ManualApiTests.prepare(self, *args, **kwargs)

    def test_queue_routes_require_a_session_and_publish_once(self):
        draft_id = self.prepare().json()['draft_id']
        self.assertEqual(self.client.post('/api/agents/manual/queue', json={'draft_id': draft_id}).status_code, 403)
        self.assertEqual(self.social.writes, [])
        response = self.client.post('/api/agents/manual/queue', json={'draft_id': draft_id}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['manual_queue']['state'], 'sent')
        self.assertEqual(response.json()['manual_queue']['requested_by'] and True, True)
        again = self.client.post('/api/agents/manual/queue', json={'draft_id': draft_id}, headers=self.headers)
        self.assertEqual(again.status_code, 409, again.text)
        cancel = self.client.post('/api/agents/manual/queue/cancel', json={'draft_id': draft_id}, headers=self.headers)
        self.assertEqual(cancel.status_code, 409, cancel.text)
        self.assertEqual(len(self.social.writes), 1)
        self.model.assert_not_called()


if __name__ == '__main__':
    unittest.main()
