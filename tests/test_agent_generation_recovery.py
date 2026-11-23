"""Recovery revokes only a generation's provable role lease, never delivery."""
import copy
import unittest
from unittest.mock import patch

from ctn_data.agent_control import AgentController, AgentError
from ctn_data.agent_generation import GenerationManager, _Task
from tests.test_agent_generation_jobs import FakeController


class GenerationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.controller = FakeController()
        self.manager = GenerationManager(self.controller)
        self.controller.manager = self.manager
        self.addCleanup(self.manager.stop)

    def seed(self, *, owner='previous-process', status='running', job_id='expired-job',
             token='expired-role-token', lease_changes=None, job_changes=None):
        job = {'id': job_id, 'owner': owner, 'status': status, 'stage': 'headers',
               'heartbeat_until': self.controller.now - 1, 'lease_token': token,
               'elapsed_seconds': 30, 'error': '', **(job_changes or {})}
        lease = {'owner': owner, 'token': token, 'until': self.controller.now + 255,
                 'generation_job_id': job_id, **(lease_changes or {})}
        def update(data, conn):
            data['agents']['maya']['generation'] = job
            data['leases']['agent:maya'] = lease
        self.controller.store.change(update)
        return job, lease

    def state(self):
        return self.controller.store.read()

    def recover(self):
        return self.controller.store.change(lambda data, conn: self.manager.recover(data))

    def test_restart_recovers_expired_generation_and_revokes_its_unexpired_role_lease(self):
        self.seed()
        self.assertEqual(self.recover(), 1)
        state = self.state()
        self.assertNotIn('agent:maya', state['leases'])
        job = state['agents']['maya']['generation']
        self.assertEqual((job['status'], job['stage']), ('failed', 'interrupted'))
        self.assertTrue(job['role_lease_released'])
        self.assertFalse(job['recovery_waiting_for_lease'])
        self.assertTrue(state['events'][-1]['details']['role_lease_released'])
        self.assertGreater(state['agents']['maya']['decision_retry_at'], self.controller.now)
        self.assertEqual(self.recover(), 0)
        self.assertEqual(self.controller.calls, 0)

    def test_wrong_owner_token_job_or_missing_bidirectional_evidence_preserves_role_lease(self):
        cases = [({'owner': 'replacement-owner'}, {}), ({'token': 'replacement-token'}, {}),
                 ({'generation_job_id': 'replacement-job'}, {}), ({'generation_job_id': None}, {}),
                 ({}, {'lease_token': None}), ({}, {'lease_token': ''}), ({}, {'owner': None})]
        for lease_changes, job_changes in cases:
            with self.subTest(lease=lease_changes, job=job_changes):
                _, lease = self.seed(lease_changes=lease_changes, job_changes=job_changes)
                self.assertEqual(self.recover(), 1)
                state = self.state()
                self.assertEqual(state['leases']['agent:maya'], lease)
                job = state['agents']['maya']['generation']
                self.assertEqual(job['status'], 'failed')
                self.assertTrue(job['recovery_waiting_for_lease'])
                self.assertEqual(job['recovery_lease_until'], lease['until'])
                self.assertFalse(job.get('role_lease_released', False))

    def test_same_process_manual_operation_is_not_released_by_recovery(self):
        _, manual = self.seed(owner=self.controller.owner,
            lease_changes={'generation_job_id': None, 'token': 'manual-role-token'})
        self.recover()
        self.assertEqual(self.state()['leases']['agent:maya'], manual)

    def test_live_heartbeat_is_not_recovered_even_when_role_lease_is_present(self):
        job, lease = self.seed(job_changes={'heartbeat_until': self.controller.now + 45})
        self.assertEqual(self.recover(), 0)
        self.assertEqual(self.state()['agents']['maya']['generation'], job)
        self.assertEqual(self.state()['leases']['agent:maya'], lease)

    def test_sending_uncertain_receipts_and_publisher_lock_survive_role_recovery(self):
        for action_state in ('sending', 'uncertain'):
            with self.subTest(state=action_state):
                self.seed()
                def sending(data, conn):
                    data['drafts'] = {'delivery': {'id': 'delivery', 'agent_id': 'maya',
                        'generation_job_id': 'expired-job', 'state': action_state,
                        'send_token': 'publisher-token', 'lease_until': self.controller.now + 240,
                        'body': 'Never automatically resend this action.', 'post_id': None}}
                    data['leases']['publisher'] = {'owner': 'previous-process',
                        'token': 'publisher-token', 'until': self.controller.now + 240}
                self.controller.store.change(sending)
                before = self.state()
                self.recover()
                after = self.state()
                self.assertNotIn('agent:maya', after['leases'])
                self.assertEqual(after['drafts'], before['drafts'])
                self.assertEqual(after['leases']['publisher'], before['leases']['publisher'])
                self.assertEqual(self.controller.calls, 0)

    def test_attach_persists_exact_job_and_token_association_before_renewing(self):
        task = _Task('maya', 'current-job', False)
        self.manager._local.task = task
        self.addCleanup(lambda: setattr(self.manager._local, 'task', None))
        self.seed(owner=self.controller.owner, job_id=task.job_id,
                  job_changes={'heartbeat_until': self.controller.now + 45, 'lease_token': None},
                  lease_changes={'generation_job_id': None})
        self.manager.attach_lease('maya', 'expired-role-token')
        state = self.state()
        self.assertEqual(state['agents']['maya']['generation']['lease_token'], 'expired-role-token')
        self.assertEqual(state['leases']['agent:maya']['generation_job_id'], task.job_id)
        self.assertEqual(state['leases']['agent:maya']['until'], self.controller.now + 300)

    def test_touch_cannot_reassociate_a_claim_already_bound_to_another_job_or_token(self):
        for lease_changes, job_changes in (({'generation_job_id': 'replacement-job'}, {}),
                                           ({}, {'lease_token': 'other-role-token'})):
            with self.subTest(lease=lease_changes, job=job_changes):
                task = _Task('maya', 'current-job', False, lease_token='expired-role-token')
                job, lease = self.seed(owner=self.controller.owner, job_id=task.job_id,
                    job_changes={'heartbeat_until': self.controller.now + 45, **job_changes},
                    lease_changes=lease_changes)
                self.manager._touch(task)
                state = self.state()
                self.assertTrue(task.cancellation.is_set())
                self.assertEqual(state['leases']['agent:maya'], lease)
                self.assertEqual(state['agents']['maya']['generation']['lease_token'], job['lease_token'])

    def test_finish_failure_and_cancellation_release_only_their_own_role_claim(self):
        for final_state in ('failed', 'cancelled'):
            with self.subTest(state=final_state):
                self.seed(owner=self.controller.owner)
                task = _Task('maya', 'expired-job', False, lease_token='expired-role-token')
                self.manager._finish(task, final_state, 'The worker ended.')
                state = self.state()
                self.assertNotIn('agent:maya', state['leases'])
                self.assertEqual(state['agents']['maya']['generation']['status'], final_state)
                self.assertTrue(state['agents']['maya']['generation']['role_lease_released'])

    def test_finish_of_already_recovered_or_cancelled_job_can_release_matching_residue(self):
        for terminal in ('failed', 'cancelled'):
            with self.subTest(state=terminal):
                self.seed(owner=self.controller.owner, status=terminal, job_changes={'error': 'Original terminal reason'})
                task = _Task('maya', 'expired-job', False, lease_token='expired-role-token')
                self.manager._finish(task, 'failed', 'Do not overwrite the previous result.')
                state = self.state()
                self.assertNotIn('agent:maya', state['leases'])
                self.assertEqual(state['agents']['maya']['generation']['status'], terminal)
                self.assertEqual(state['agents']['maya']['generation']['error'], 'Original terminal reason')

    def test_stale_finish_cannot_release_a_replacement_job_or_lease(self):
        task = _Task('maya', 'expired-job', False, lease_token='expired-role-token')
        for job_id, owner, lease_changes in (('replacement-job', self.controller.owner, {}),
                                            ('expired-job', 'replacement-owner', {}),
                                            ('expired-job', self.controller.owner, {'token': 'replacement-token'}),
                                            ('expired-job', self.controller.owner, {'generation_job_id': 'new-job'})):
            with self.subTest(job=job_id, owner=owner, lease=lease_changes):
                job, lease = self.seed(owner=owner, job_id=job_id, lease_changes=lease_changes)
                self.manager._finish(task, 'failed', 'Old worker failed.')
                state = self.state()
                self.assertEqual(state['leases']['agent:maya'], lease)
                if job_id != task.job_id or owner != self.controller.owner:
                    self.assertEqual(state['agents']['maya']['generation'], job)

    def test_recovered_local_worker_is_cancelled_and_cannot_save_or_renew(self):
        task = _Task('maya', 'expired-job', False, lease_token='expired-role-token')
        self.seed(owner=self.controller.owner)
        self.manager._jobs['maya'] = task
        self.manager._local.task = task
        self.addCleanup(lambda: setattr(self.manager._local, 'task', None))
        self.recover()
        self.assertTrue(task.cancellation.is_set())
        with self.assertRaises(AgentError):
            AgentController._check_lease(self.controller, self.state(), 'maya', 'expired-role-token')
        def save_late(data, conn):
            self.manager.check_cancelled('maya', data)
            data['drafts']['late'] = {'state': 'pending', 'body': 'A stale model result'}
        with self.assertRaises(AgentError):
            self.controller.store.change(save_late)
        self.manager._touch(task)
        self.assertNotIn('agent:maya', self.state()['leases'])
        self.assertNotIn('late', self.state()['drafts'])
        self.assertEqual(self.state()['agents']['maya']['generation']['status'], 'failed')

    def test_maintenance_rejects_queue_atomically_without_a_new_thread_or_job(self):
        self.controller.store.change(lambda data, conn: data.update(rate={'maintenance': {'active': True}}))
        before = copy.deepcopy(self.state())
        with patch('ctn_data.agent_generation.threading.Thread') as thread:
            with self.assertRaisesRegex(AgentError, '服务正在维护，暂不接收新任务') as caught:
                self.manager.submit('maya')
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.manager._jobs, {})
        thread.assert_not_called()


if __name__ == '__main__':
    unittest.main()
