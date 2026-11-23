"""Background generation jobs use fake controllers, never providers or SDKs."""
import copy
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from ctn_data.agent_control import AgentController, AgentError
from ctn_data.agent_generation import GenerationManager


class MemoryStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.writes = 0
        self.data = {'agents': {'maya': {'id': 'maya', 'account_id': 5554,
                                      'background': 'A local librarian.'}},
                     'drafts': {}, 'leases': {}, 'events': [], 'settings': {'enabled': False}}

    def read(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def change(self, callback):
        with self.lock:
            candidate = copy.deepcopy(self.data)
            result = callback(candidate, None)
            self.data = candidate
            self.writes += 1
            return result


class FakeController:
    _event = AgentController._event

    def __init__(self, store=None):
        self.store = store or MemoryStore()
        self.owner = str(uuid.uuid4())
        self.now = time.time()
        self.manager = None
        self.started = threading.Event()
        self.release = threading.Event()
        self.options = None
        self.calls = 0
        self.behaviour = None
        self.after_lease_release = None

    def clock(self):
        return self.now

    def snapshot(self):
        return self.store.read()

    def _run(self, agent_id, automatic=False):
        token = str(uuid.uuid4())

        def claim(data, conn):
            lease = data['leases'].get('agent:' + agent_id, {})
            if lease.get('until', 0) > self.clock():
                raise AgentError('Role already has a lease.', 409)
            data['leases']['agent:' + agent_id] = {
                'token': token, 'owner': self.owner, 'until': self.clock() + 300}

        self.store.change(claim)
        try:
            self.manager.attach_lease(agent_id, token)
            self.options = self.manager.request_options(agent_id, token)
            self.calls += 1
            self.started.set()
            if self.behaviour is not None:
                return self.behaviour(agent_id, token, self.options)
            while not self.release.wait(.005):
                if self.options['cancellation_event'].is_set():
                    raise AgentError('Request cancelled.', 499)

            def save(data, conn):
                self.manager.check_cancelled(agent_id, data)
                data['drafts']['new'] = {'agent_id': agent_id, 'state': 'pending', 'body': 'Completed text.'}
            self.store.change(save)
        finally:
            self.manager.detach_lease(agent_id, token)
            def release(data, conn):
                if data['leases'].get('agent:' + agent_id, {}).get('token') == token:
                    data['leases'].pop('agent:' + agent_id, None)
            self.store.change(release)
            if self.after_lease_release is not None:
                self.after_lease_release()


class GenerationJobTests(unittest.TestCase):
    def setUp(self):
        self.controller = FakeController()
        self.manager = GenerationManager(self.controller, heartbeat_interval=.02, progress_interval=.05)
        self.controller.manager = self.manager
        self.addCleanup(self.manager.stop)

    def job(self):
        return self.controller.store.read()['agents']['maya'].get('generation', {})

    def wait_for(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.005)
        self.fail('Background job did not reach the expected state: ' + repr(self.job()))

    def start(self):
        before = time.monotonic()
        result = self.manager.submit('maya')
        self.assertLess(time.monotonic() - before, .5)
        self.assertIn(result['agents']['maya']['generation']['status'], ('queued', 'running'))
        self.assertTrue(self.controller.started.wait(1))

    def test_submit_returns_immediately_and_reload_observes_same_completed_job(self):
        self.start()
        job_id = self.job()['id']
        self.assertEqual(self.controller.snapshot()['agents']['maya']['generation']['id'], job_id)
        self.assertEqual(self.controller.calls, 1)
        self.controller.release.set()
        self.wait_for(lambda: self.job().get('status') == 'completed')
        state = self.controller.store.read()
        self.assertEqual(state['drafts']['new']['body'], 'Completed text.')
        self.assertEqual(state['agents']['maya']['generation']['id'], job_id)
        self.assertEqual(state['agents']['maya']['generation']['heartbeat_until'], 0)
        self.assertEqual(state['leases'], {})
        self.assertEqual(state['agents']['maya']['account_id'], 5554)

    def test_duplicate_submit_is_rejected_across_managers_without_second_run(self):
        self.start()
        for manager in (self.manager, GenerationManager(self.controller)):
            with self.assertRaises(AgentError) as caught:
                manager.submit('maya')
            self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.controller.calls, 1)

    def test_cancel_stops_worker_without_saving_partial_result_and_sync_calls_are_unaffected(self):
        self.start()
        response = self.manager.cancel('maya')
        self.assertIn(response['agents']['maya']['generation']['status'], ('cancelling', 'cancelled'))
        self.wait_for(lambda: self.job().get('status') == 'cancelled')
        self.assertEqual(self.controller.store.read()['drafts'], {})
        self.assertEqual(self.controller.store.read()['leases'], {})
        self.assertEqual(self.manager.request_options('maya', 'ordinary-sync-token'), {})
        self.manager.check_cancelled('maya')
        self.assertIn('已取消', self.job()['error'])

    def test_foreign_process_cancel_reaches_local_transport_event(self):
        self.start()
        foreign = FakeController(self.controller.store)
        foreign.now = self.controller.now
        GenerationManager(foreign).cancel('maya')
        self.wait_for(lambda: self.controller.options['cancellation_event'].is_set())
        self.wait_for(lambda: self.job().get('status') == 'cancelled')
        self.assertEqual(self.controller.calls, 1)
        self.assertEqual(self.controller.store.read()['drafts'], {})

    def test_heartbeat_renews_exact_role_lease_without_a_generation_time_limit(self):
        self.start()
        previous = self.controller.store.read()['leases']['agent:maya']['until']
        for _ in range(4):
            self.controller.now += 100
            self.wait_for(lambda: self.controller.store.read()['leases']['agent:maya']['until'] > previous)
            previous = self.controller.store.read()['leases']['agent:maya']['until']
            self.assertEqual(self.job()['status'], 'running')
            self.assertGreater(self.job()['heartbeat_until'], self.controller.clock())
        self.assertFalse(self.controller.options['cancellation_event'].is_set())

    def test_heartbeat_never_renews_or_releases_a_replacement_role_lease(self):
        self.start()
        replacement = {'owner': 'new-owner', 'token': 'new-token', 'until': self.controller.now + 1000}
        self.controller.store.change(lambda data, conn: data['leases'].update({'agent:maya': replacement.copy()}))
        self.wait_for(lambda: self.job().get('status') == 'failed')
        self.assertEqual(self.controller.store.read()['leases']['agent:maya'], replacement)
        self.assertIn('租约', self.job()['error'])
        self.assertEqual(self.controller.store.read()['drafts'], {})

    def test_completed_result_is_not_failed_while_run_returns_after_releasing_role_lease(self):
        released, finish_return = threading.Event(), threading.Event()

        def pause_after_release():
            released.set()
            finish_return.wait(2)

        self.controller.after_lease_release = pause_after_release
        self.start()
        try:
            self.controller.release.set()
            self.assertTrue(released.wait(1))
            before = self.controller.store.writes
            self.wait_for(lambda: self.controller.store.writes >= before + 3)
            self.assertEqual(self.controller.store.read()['leases'], {})
            self.assertFalse(self.controller.options['cancellation_event'].is_set())
            self.assertEqual(self.job()['status'], 'running')
        finally:
            finish_return.set()
        self.wait_for(lambda: self.job().get('status') == 'completed')
        self.assertEqual(self.controller.store.read()['drafts']['new']['body'], 'Completed text.')

    def test_detach_waits_for_inflight_renewal_before_the_role_lease_is_released(self):
        self.start()
        task = self.manager._jobs['maya']
        touch_started, finish_touch = threading.Event(), threading.Event()
        original_change = self.controller.store.change

        def gated_change(callback):
            if threading.current_thread().name == 'touch-probe':
                touch_started.set()
                finish_touch.wait(2)
            return original_change(callback)

        self.controller.store.change = gated_change
        probe = threading.Thread(target=self.manager._touch, args=(task,), name='touch-probe')
        probe.start()
        try:
            self.assertTrue(touch_started.wait(1))
            self.controller.release.set()
            self.wait_for(lambda: 'new' in self.controller.store.read()['drafts'])
            # The completed draft has been saved. The worker's finally block
            # must still retain its role claim while a renewal holds the lock.
            self.assertIn('agent:maya', self.controller.store.read()['leases'])
        finally:
            finish_touch.set()
            probe.join(timeout=1)
            self.controller.store.change = original_change
        self.wait_for(lambda: self.job().get('status') == 'completed')
        self.assertEqual(self.controller.store.read()['leases'], {})
        self.assertFalse(self.controller.options['cancellation_event'].is_set())

    def test_recover_marks_expired_job_interrupted_and_preserves_action_audit(self):
        def stale(data, conn):
            data['agents']['maya']['generation'] = {'id': 'old', 'owner': 'old-process',
                'status': 'running', 'heartbeat_until': self.controller.now - 1, 'stage': 'headers'}
            data['drafts']['uncertain'] = {'agent_id': 'maya', 'state': 'uncertain', 'post_id': None}
        self.controller.store.change(stale)
        recovered = self.controller.store.change(lambda data, conn: self.manager.recover(data))
        self.assertEqual(recovered, 1)
        self.assertEqual(self.job()['status'], 'failed')
        self.assertEqual(self.job()['stage'], 'interrupted')
        self.assertEqual(self.controller.store.read()['drafts']['uncertain']['state'], 'uncertain')
        self.assertEqual(self.controller.calls, 0)

    def test_old_worker_cannot_overwrite_replacement_generation(self):
        self.start()
        replacement = {'id': 'new-job', 'owner': 'new-owner', 'status': 'running',
                       'heartbeat_until': self.controller.now + 1000, 'stage': 'headers'}
        self.controller.store.change(lambda data, conn: data['agents']['maya'].update(generation=replacement.copy()))
        self.wait_for(lambda: self.controller.options['cancellation_event'].is_set())
        self.wait_for(lambda: not self.manager._jobs)
        self.assertEqual(self.job(), replacement)

    def test_progress_is_throttled_and_does_not_persist_provider_text_or_secrets(self):
        manager = GenerationManager(self.controller, heartbeat_interval=10, progress_interval=.05)
        self.manager.stop()
        self.manager = manager
        self.controller.manager = manager
        self.addCleanup(manager.stop)
        self.start()
        callback = self.controller.options['on_progress']
        before = self.controller.store.writes
        for _ in range(200):
            callback({'stage': 'headers', 'received_bytes': 0, 'body': 'PRIVATE-CONTENT',
                      'request_id': 'PRIVATE-SECRET', 'elapsed_seconds': 999999})
        self.assertLessEqual(self.controller.store.writes - before, 1)
        self.wait_for(lambda: self.job()['stage'] == 'headers')
        self.assertNotIn('PRIVATE-', repr(self.job()))
        callback({'stage': 'body', 'received_bytes': 27})
        self.wait_for(lambda: self.job()['stage'] == 'body')
        self.assertEqual(self.job()['received_bytes'], 27)
        self.assertLess(self.job()['elapsed_seconds'], 1)

    def test_progress_exposes_streaming_thinking_and_receiving_stages(self):
        self.start()
        callback = self.controller.options['on_progress']
        callback({'stage': 'thinking', 'received_bytes': 12})
        self.wait_for(lambda: self.job().get('stage') == 'thinking')
        time.sleep(.06)
        callback({'stage': 'receiving', 'received_bytes': 28})
        self.wait_for(lambda: self.job().get('stage') == 'receiving')
        self.assertEqual(self.job()['received_bytes'], 28)

    def test_stop_cancels_local_job_and_rejects_new_submissions(self):
        self.start()
        self.manager.stop()
        self.wait_for(lambda: self.job().get('status') == 'cancelled')
        self.assertEqual(self.controller.store.read()['drafts'], {})
        with self.assertRaises(AgentError) as caught:
            self.manager.submit('maya')
        self.assertEqual(caught.exception.status_code, 503)

    def test_cancel_before_publication_does_not_start_a_social_operation(self):
        gate = threading.Event()
        def publish(agent_id, token, options):
            gate.wait(2)
            self.controller.store.change(lambda data, conn: self.manager.before_publish(agent_id, data))
            self.fail('cancelled generation must not enter publication')
        self.controller.behaviour = publish
        self.start()
        self.manager.cancel('maya')
        gate.set()
        self.wait_for(lambda: self.job().get('status') == 'cancelled')
        self.assertFalse(self.job().get('operation_started', False))
        self.assertEqual(self.controller.store.read()['drafts'], {})

    def test_publication_transition_cannot_claim_an_inflight_write_was_cancelled(self):
        publishing = threading.Event()
        def publish(agent_id, token, options):
            self.controller.store.change(lambda data, conn: self.manager.before_publish(agent_id, data))
            publishing.set()
            self.controller.release.wait(2)
        self.controller.behaviour = publish
        self.start()
        self.assertTrue(publishing.wait(1))
        with self.assertRaises(AgentError) as caught:
            self.manager.cancel('maya')
        self.assertEqual(caught.exception.status_code, 409)
        self.assertFalse(self.controller.options['cancellation_event'].is_set())
        self.controller.release.set()
        self.wait_for(lambda: self.job().get('status') == 'completed')

    def test_a_failed_check_round_keeps_its_text_and_issues_on_the_failure_event(self):
        def fail(*args):
            error = AgentError('AI 正文两次未通过方向与事实检查，本轮未创建动作：It concedes a No argument.', 502)
            error.rejected = {'action': 'post', 'body': 'The jab is free. ' + 'y' * 400,
                              'issues': ['It concedes a No argument.', 'api_key: PRIVATE-TEST-TOKEN']}
            raise error
        self.controller.behaviour = fail
        self.manager.submit('maya')
        self.wait_for(lambda: self.job().get('status') == 'failed')
        failed = next(e for e in self.controller.store.read()['events'] if e['kind'] == 'generation_failed')
        self.assertEqual(failed['details']['action'], 'post')
        self.assertEqual(len(failed['details']['rejected_body']), 300)
        self.assertEqual(failed['details']['rejected_issues'][0], 'It concedes a No argument.')
        self.assertNotIn('PRIVATE-', failed['details']['rejected_issues'][1])

    def test_unexpected_failure_is_sanitized_and_not_retried(self):
        def fail(*args):
            raise RuntimeError('PRIVATE-SECRET-KEY')
        self.controller.behaviour = fail
        self.manager.submit('maya')
        self.wait_for(lambda: self.job().get('status') == 'failed')
        self.assertNotIn('PRIVATE-', self.job()['error'])
        self.assertEqual(self.controller.calls, 1)
        self.assertEqual(self.controller.store.read()['drafts'], {})


if __name__ == '__main__':
    unittest.main()
