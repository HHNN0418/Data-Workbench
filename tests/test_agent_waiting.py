"""Waiting regressions using in-memory state, disposable SQLite and fake services."""
import copy
import json
import socket
import unittest
import uuid
from unittest.mock import patch

from ctn_data.agent_budget import Deferred
from ctn_data.agent_control import AgentError
from ctn_data.agent_generation import GenerationManager, _Task
from ctn_data import agent_waiting
from tests import test_agent_actions as actions_fixture
from tests import test_agent_generation_jobs as jobs_fixture


class AgentWaitingTests(unittest.TestCase):
    def setUp(self):
        # A fixture regression must fail instead of reaching any real service.
        self.network = patch.object(
            socket.socket, 'connect', side_effect=AssertionError('Network forbidden in waiting tests'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.fixture = actions_fixture.AutonomousActions()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.controller = self.fixture.controller
        self.agent_id = self.fixture.agent_id
        self.original_call = self.fixture.call.side_effect

        def enable(data, conn):
            data['settings']['enabled'] = True
            data['agents'][self.agent_id].update(enabled=True, mode='auto')

        self.controller.store.change(enable)
        self.model_result()

    @property
    def now(self):
        return self.fixture.now

    def agent(self):
        return self.controller.store.read()['agents'][self.agent_id]

    def public_agent(self):
        return next(row for row in self.controller.snapshot()['agents']
                    if row['id'] == self.agent_id)

    def draft(self):
        return next(row for row in self.controller.store.read()['drafts'].values()
                    if row['agent_id'] == self.agent_id)

    def model_result(self, action='post'):
        output = {
            'action': action,
            'body': '' if action == 'like' else 'A considered view.',
            'target_post_id': 7 if action == 'like' else None,
            'source_ids': ['post:7'],
            'notification_ids': [],
            'memory': 'I considered a recent discussion.',
            'reason': 'Matches my interests.',
        }
        self.fixture.model.return_value = {
            'text': json.dumps(output), 'model': 'fake', 'usage': {}}

    def run_job(self):
        task = _Task(self.agent_id, str(uuid.uuid4()), True)

        def queue(data, conn):
            data['agents'][self.agent_id]['generation'] = {
                'id': task.job_id,
                'owner': self.controller.owner,
                'status': 'queued',
                'stage': 'queued',
                'heartbeat_until': self.now + 45,
                'error': '',
            }

        self.controller.store.change(queue)
        self.controller.generations._worker(task)
        return task

    def wait_for_post(self, seconds=20):
        def defer(controller, bucket, method, *args, **kwargs):
            if bucket == 'post':
                raise Deferred('post', seconds, '全队自定义额度等待中', quota_policy=True)
            return self.original_call(controller, bucket, method, *args, **kwargs)

        self.fixture.call.side_effect = defer
        try:
            return self.run_job()
        finally:
            self.fixture.call.side_effect = self.original_call

    def tick(self):
        # Polling and its own quotas are covered elsewhere. This exercises the
        # real scheduler and execution path without waiting on fake inbox I/O.
        with patch('ctn_data.agent_observation.refresh'), \
                patch('ctn_data.agent_observation.drain_resolutions'):
            self.controller.tick()

    def test_quota_wait_has_retry_time_without_failure_or_failure_counter(self):
        self.controller.store.change(lambda data, conn:
            data['agents'][self.agent_id].update(generation_failures=2))
        task = self.wait_for_post(20)
        agent = self.public_agent()
        job = agent['generation']
        self.assertEqual(job['id'], task.job_id)
        self.assertEqual(job['status'], 'waiting')
        self.assertEqual(job['retry_at'], self.now + 20)
        self.assertEqual(job['error'], '')
        self.assertEqual(self.agent()['generation_failures'], 2)
        self.assertFalse(agent['busy'])
        self.assertEqual(self.draft()['state'], 'pending')
        self.assertEqual(self.draft()['next_attempt_at'], self.now + 20)
        self.assertEqual(self.fixture.social.writes, [])
        self.assertFalse(any(
            event.get('kind') == 'generation_failed'
            and event.get('details', {}).get('job_id') == task.job_id
            for event in self.controller.store.read()['events']))

    def test_pending_publish_waits_until_due_then_completes_same_job_and_clears_errors(self):
        task = self.wait_for_post(20)
        self.fixture.now += 19
        with patch.object(self.controller.generations, 'submit') as submit:
            self.tick()
        submit.assert_not_called()
        self.assertEqual(self.fixture.social.writes, [])
        self.fixture.now += 2
        self.tick()
        agent = self.public_agent()
        self.assertEqual(len(self.fixture.social.writes), 1)
        self.assertEqual(self.draft()['state'], 'published')
        self.assertEqual(self.draft()['generation_job_id'], task.job_id)
        self.assertEqual(agent['generation']['id'], task.job_id)
        self.assertEqual(agent['generation']['status'], 'completed')
        self.assertEqual(agent['generation']['error'], '')
        self.assertEqual(agent['error'], '')
        self.assertEqual(self.agent().get('last_error', ''), '')
        self.assertFalse(agent['busy'])

    def test_new_input_cannot_start_a_new_decision_before_waiting_publish_is_due(self):
        task = self.wait_for_post(20)
        self.fixture.now += 1
        with patch('ctn_data.agent_observation.pending_revision',
                   return_value='new-input-arrived-during-publish-wait'), \
                patch.object(self.controller.generations, 'submit') as submit:
            self.tick()
            self.fixture.now += 18
            self.tick()
        submit.assert_not_called()
        self.assertEqual(self.agent()['generation']['id'], task.job_id)
        self.assertEqual(self.agent()['generation']['status'], 'waiting')
        self.assertEqual(self.draft()['state'], 'pending')
        self.assertEqual(self.fixture.social.writes, [])

    def test_repeated_publish_deferrals_refresh_wait_without_reviving_failure(self):
        task = self.wait_for_post(20)
        self.fixture.now += 20
        for delay in (7, 3):
            with self.subTest(delay=delay):
                def defer(controller, bucket, method, *args, **kwargs):
                    if bucket == 'post':
                        raise Deferred('post', delay, '全队自定义额度等待中', quota_policy=True)
                    return self.original_call(controller, bucket, method, *args, **kwargs)

                self.fixture.call.side_effect = defer
                try:
                    self.tick()
                finally:
                    self.fixture.call.side_effect = self.original_call
                agent = self.public_agent()
                self.assertEqual(agent['generation']['id'], task.job_id)
                self.assertEqual(agent['generation']['status'], 'waiting')
                self.assertEqual(agent['generation']['retry_at'], self.now + delay)
                self.assertEqual(agent['generation']['error'], '')
                self.assertEqual(agent['error'], '')
                self.assertEqual(self.agent().get('generation_failures', 0), 0)
                self.assertEqual(self.draft()['state'], 'pending')
                self.assertEqual(self.draft()['next_attempt_at'], self.now + delay)
                self.assertEqual(self.fixture.social.writes, [])
                self.fixture.now += delay
        self.tick()
        self.assertEqual(len(self.fixture.social.writes), 1)
        self.assertEqual(self.draft()['state'], 'published')
        self.assertEqual(self.agent()['generation']['id'], task.job_id)
        self.assertEqual(self.agent()['generation']['status'], 'completed')
        self.assertEqual(self.agent()['generation']['error'], '')
        self.assertEqual(self.agent().get('last_error', ''), '')

    def test_publisher_busy_is_five_second_wait_and_retry_completes_same_job(self):
        self.model_result('like')
        self.controller.store.change(lambda data, conn:
            data['leases'].update(publisher={
                'owner': 'other-worker', 'token': 'other-publisher', 'until': self.now + 5}))
        task = self.run_job()
        agent = self.agent()
        self.assertEqual(agent['generation']['status'], 'waiting')
        self.assertEqual(agent['generation']['retry_at'], self.now + 5)
        self.assertEqual(agent.get('generation_failures', 0), 0)
        self.assertEqual(self.draft()['state'], 'pending')
        self.assertEqual(self.fixture.social.like_calls, [])
        self.fixture.now += 5
        self.tick()
        self.assertEqual(self.fixture.social.like_calls, [(10, 7)])
        self.assertEqual(self.draft()['state'], 'published')
        self.assertEqual(self.agent()['generation']['id'], task.job_id)
        self.assertEqual(self.agent()['generation']['status'], 'completed')
        self.assertEqual(self.agent().get('last_error', ''), '')

    def test_new_ordinary_claim_is_not_mislabelled_as_old_generation_recovery(self):
        def failed_job(data, conn):
            data['agents'][self.agent_id]['generation'] = {
                'id': 'old-job', 'owner': self.controller.owner, 'status': 'failed',
                'lease_token': 'old-role-token', 'heartbeat_until': 0, 'error': 'Old error'}

        self.controller.store.change(failed_job)
        with self.controller._claim(self.agent_id):
            agent = self.public_agent()
            self.assertTrue(agent['busy'])
            self.assertEqual(agent['busy_reason'], 'operation')
            self.assertEqual(agent['generation']['error'], 'Old error')
        self.assertFalse(self.public_agent()['busy'])

    def test_old_job_success_cannot_clear_a_new_jobs_failure(self):
        old = self.wait_for_post(20)
        new_error = 'New decision failed for an unrelated reason.'

        def replace_job(data, conn):
            data['agents'][self.agent_id].update(
                last_error=new_error, generation_failures=3,
                generation={'id': 'new-job', 'owner': self.controller.owner,
                            'status': 'failed', 'stage': 'failed', 'error': new_error,
                            'heartbeat_until': 0})

        self.controller.store.change(replace_job)
        self.fixture.now += 21
        self.tick()
        agent = self.agent()
        self.assertEqual(self.draft()['generation_job_id'], old.job_id)
        self.assertEqual(self.draft()['state'], 'published')
        self.assertEqual(len(self.fixture.social.writes), 1)
        self.assertEqual(agent['generation']['id'], 'new-job')
        self.assertEqual(agent['generation']['status'], 'failed')
        self.assertEqual(agent['generation']['error'], new_error)
        self.assertEqual(agent['last_error'], new_error)
        self.assertEqual(agent['generation_failures'], 3)

    def test_retry_backoff_cannot_clear_replacement_jobs_error_after_role_lease_release(self):
        old = self.wait_for_post(20)
        self.fixture.now += 20
        new_error = 'A replacement decision failed independently.'
        new_job = {
            'id': 'replacement-job', 'owner': self.controller.owner,
            'status': 'failed', 'stage': 'failed', 'error': new_error,
            'heartbeat_until': 0,
        }
        original_change = self.controller.store.change
        injected = []

        def change_after_replacement(callback):
            if callback.__name__ == 'backoff' and not injected:
                # Reproduce the actual race window: delivery has exited its
                # role claim, but the scheduler has not persisted its retry.
                self.assertNotIn('agent:' + self.agent_id,
                                 self.controller.store.read()['leases'])

                def replacement(data, conn):
                    data['agents'][self.agent_id].update(
                        generation=copy.deepcopy(new_job), last_error=new_error,
                        generation_failures=3, decision_retry_at=self.now + 120)

                original_change(replacement)
                injected.append(True)
            return original_change(callback)

        def defer(controller, bucket, method, *args, **kwargs):
            if bucket == 'post':
                raise Deferred('post', 7, '全队自定义额度等待中', quota_policy=True)
            return self.original_call(controller, bucket, method, *args, **kwargs)

        self.fixture.call.side_effect = defer
        try:
            with patch.object(self.controller.store, 'change', side_effect=change_after_replacement):
                self.tick()
        finally:
            self.fixture.call.side_effect = self.original_call
        self.assertEqual(injected, [True])
        draft, agent = self.draft(), self.agent()
        self.assertEqual(draft['generation_job_id'], old.job_id)
        self.assertEqual(draft['state'], 'pending')
        self.assertEqual(draft['next_attempt_at'], self.now + 7)
        self.assertEqual(agent['generation'], new_job)
        self.assertEqual(agent['last_error'], new_error)
        self.assertEqual(agent['generation_failures'], 3)
        self.assertEqual(agent['decision_retry_at'], self.now + 120)
        self.assertEqual(self.fixture.social.writes, [])

    def test_cancel_waiting_job_holds_its_pending_action_for_review(self):
        self.wait_for_post(20)
        self.controller.generations.cancel(self.agent_id)
        self.assertEqual(self.agent()['generation']['status'], 'cancelled')
        self.assertTrue(self.draft().get('requires_review'))
        self.fixture.now += 21
        with patch.object(self.controller.generations, 'submit'):
            self.tick()
        self.assertEqual(self.fixture.social.writes, [])
        self.assertEqual(self.draft()['state'], 'pending')
        self.assertTrue(self.draft().get('requires_review'))

    def test_uncertain_delivery_stays_failed_and_is_never_automatically_resent(self):
        self.fixture.social.timeout = 'post'
        self.run_job()
        self.assertEqual(self.draft()['state'], 'uncertain')
        self.assertEqual(self.agent()['generation']['status'], 'failed')
        self.assertEqual(len(self.fixture.social.writes), 1)
        self.fixture.now += 3600
        with patch.object(self.controller.generations, 'submit') as submit:
            self.tick()
        submit.assert_not_called()
        self.assertEqual(len(self.fixture.social.writes), 1)
        self.assertEqual(self.draft()['state'], 'uncertain')

    def test_safety_conflict_is_not_reclassified_as_a_normal_wait(self):
        with patch.object(self.controller, '_generate',
                          side_effect=AgentError('安全检查未通过', 409)):
            self.run_job()
        self.assertEqual(self.agent()['generation']['status'], 'failed')
        self.assertEqual(self.agent()['generation']['error'], '安全检查未通过')
        self.assertEqual(self.fixture.social.writes, [])

    def test_waiting_without_a_live_heartbeat_is_not_recovered_as_interrupted(self):
        task = self.wait_for_post(20)
        before = self.agent()
        self.assertEqual(before['generation']['status'], 'waiting')
        self.assertEqual(before['generation'].get('heartbeat_until', 0), 0)
        self.fixture.now += 120
        recovered = self.controller.store.change(
            lambda data, conn: self.controller.generations.recover(data))
        self.assertEqual(recovered, 0)
        self.assertEqual(self.agent()['generation']['id'], task.job_id)
        self.assertEqual(self.agent()['generation']['status'], 'waiting')
        self.assertEqual(self.agent().get('generation_failures', 0),
                         before.get('generation_failures', 0))
        self.assertEqual(self.fixture.social.writes, [])

    def test_repeated_short_model_waits_do_not_acquire_exponential_failure_backoff(self):
        for attempt in range(3):
            with self.subTest(attempt=attempt + 1):
                waiting = self.controller._remote_failure(Deferred(
                    'llm:ctn-official', 5, '所选模型已有角色任务运行'))
                with patch.object(self.controller, '_generate', side_effect=waiting):
                    self.run_job()
                agent = self.agent()
                self.assertEqual(agent['generation']['status'], 'waiting')
                self.assertEqual(agent['generation']['retry_at'], self.now + 5)
                self.assertEqual(agent['decision_retry_at'], self.now + 5)
                self.assertEqual(agent.get('generation_failures', 0), 0)
                self.assertEqual(self.fixture.social.writes, [])
                self.fixture.now += 5


class AgentWaitingStateTests(unittest.TestCase):
    """Ownership and completion boundaries, without a database or services."""

    now = 1792450000.0

    def state(self, *, status='failed', stage='failed',
              error='队伍请求额度等待中，已保存等待时间', draft_state='published'):
        return {
            'agents': {
                'maya': {
                    'id': 'maya', 'last_error': error, 'generation_failures': 2,
                    'decision_retry_at': self.now + 60,
                    'generation': {
                        'id': 'job-1', 'owner': 'owner-1', 'status': status,
                        'stage': stage, 'error': error, 'heartbeat_until': 0,
                    },
                },
            },
            'drafts': {
                'draft-1': {
                    'id': 'draft-1', 'agent_id': 'maya',
                    'generation_job_id': 'job-1', 'state': draft_state,
                },
            },
        }

    def test_action_published_before_defer_is_saved_as_completed(self):
        controller = jobs_fixture.FakeController()
        controller.now = self.now
        manager = GenerationManager(controller)
        controller.manager = manager
        task = _Task('maya', 'job-1', True)

        def published_after_run(data, conn):
            data['agents']['maya'].update(
                last_error='队伍请求额度等待中，已保存等待时间',
                generation_failures=2,
                generation={
                    'id': task.job_id, 'owner': controller.owner,
                    'status': 'running', 'stage': 'publishing',
                    'error': '', 'heartbeat_until': self.now + 45,
                })
            data['drafts']['draft-1'] = {
                'id': 'draft-1', 'agent_id': 'maya',
                'generation_job_id': task.job_id, 'state': 'published',
            }

        controller.store.change(published_after_run)
        result = manager._defer(task, {
            'wait_kind': 'quota', 'wait_message': 'A local quota wait', 'retry_after': 20})
        state = controller.store.read()
        agent = state['agents']['maya']
        self.assertTrue(result)
        self.assertEqual(agent['generation']['status'], 'completed')
        self.assertEqual(agent['generation']['error'], '')
        self.assertNotIn('retry_at', agent['generation'])
        self.assertNotIn('waiting_for', agent['generation'])
        self.assertEqual(agent['last_error'], '')
        self.assertEqual(agent['generation_failures'], 0)
        self.assertEqual(state['events'][-1]['kind'], 'generation_completed')
        self.assertEqual(controller.calls, 0)

    def test_known_legacy_wait_failures_are_completed_by_same_jobs_published_receipt(self):
        for message in ('队伍请求额度等待中，已保存等待时间', '统一执行器已有动作运行'):
            with self.subTest(error=message):
                state = self.state(error=message)
                self.assertEqual(agent_waiting.sync_completed(state, self.now), 1)
                agent = state['agents']['maya']
                self.assertEqual(agent['generation']['status'], 'completed')
                self.assertEqual(agent['generation']['error'], '')
                self.assertEqual(agent['last_error'], '')
                self.assertEqual(agent['generation_failures'], 0)
                self.assertEqual(agent['decision_retry_at'], 0)
                self.assertEqual(agent_waiting.sync_completed(state, self.now), 0)

    def test_legacy_receipt_cannot_clear_interruption_cancellation_or_safety_failure(self):
        cases = (
            {'status': 'failed', 'stage': 'interrupted'},
            {'status': 'cancelled', 'stage': 'cancelled'},
            {'status': 'failed', 'stage': 'failed', 'error': '安全检查未通过'},
            {'status': 'failed', 'stage': 'failed', 'error': '任务租约已过期，结果未执行，请刷新核对'},
        )
        for changes in cases:
            with self.subTest(**changes):
                state = self.state(**changes)
                before = copy.deepcopy(state)
                self.assertEqual(agent_waiting.sync_completed(state, self.now), 0)
                self.assertEqual(state, before)

    def test_a_different_jobs_published_receipt_cannot_clear_the_current_error(self):
        state = self.state()
        state['drafts']['draft-1']['generation_job_id'] = 'older-job'
        before = copy.deepcopy(state)
        self.assertEqual(agent_waiting.sync_completed(state, self.now), 0)
        self.assertEqual(state, before)

    def test_other_uncertain_or_sending_action_prevents_same_job_completion(self):
        for status in ('waiting', 'failed'):
            for unresolved in ('uncertain', 'sending'):
                with self.subTest(job_status=status, unresolved_action=unresolved):
                    state = self.state(status=status)
                    state['drafts']['draft-2'] = {
                        'id': 'draft-2', 'agent_id': 'maya',
                        'generation_job_id': 'job-1', 'state': unresolved,
                    }
                    before = copy.deepcopy(state)
                    self.assertEqual(agent_waiting.sync_completed(state, self.now), 0)
                    self.assertEqual(state, before)

    def test_role_recovery_requires_exact_token_job_and_owner_association(self):
        job = {'id': 'job-1', 'owner': 'owner-1', 'lease_token': 'role-token'}
        lease = {
            'owner': 'owner-1', 'token': 'role-token',
            'generation_job_id': 'job-1', 'until': self.now + 300,
        }
        self.assertTrue(agent_waiting.role_lease_matches(job, lease))
        changes = (
            {'token': 'new-operation-token'},
            {'generation_job_id': 'new-job'},
            {'generation_job_id': None},
            {'owner': 'new-owner'},
        )
        for change in changes:
            with self.subTest(lease_change=change):
                self.assertFalse(agent_waiting.role_lease_matches(job, {**lease, **change}))
        self.assertFalse(agent_waiting.role_lease_matches(
            {key: value for key, value in job.items() if key != 'lease_token'}, lease))


if __name__ == '__main__':
    unittest.main()
