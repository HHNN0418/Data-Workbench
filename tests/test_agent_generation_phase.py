"""generation_phase audit rows mark phase changes, not every transport stage (D48)."""
import unittest

from ctn_data.agent_generation import GenerationManager, _Task, phase
from tests.test_agent_generation_jobs import FakeController


class PhaseAuditTests(unittest.TestCase):
    def setUp(self):
        self.controller = FakeController()
        self.manager = GenerationManager(self.controller, heartbeat_interval=60, progress_interval=60)
        self.addCleanup(self.manager.stop)
        self.task = _Task(agent_id='maya', job_id='job-1', automatic=True)
        self.controller.store.change(lambda data, conn: data['agents']['maya'].update(generation={
            'id': 'job-1', 'owner': self.manager.owner, 'status': 'running', 'stage': 'starting',
            'heartbeat_until': self.controller.now + 60}))

    def phases(self):
        return [e for e in self.controller.store.read()['events'] if e['kind'] == 'generation_phase']

    def test_three_model_requests_write_one_model_row(self):
        stages = ['reading',
                  'dns', 'connect', 'send', 'headers', 'thinking', 'receiving', 'complete',   # decide
                  'send', 'headers', 'body', 'complete',                                     # write
                  'send', 'headers', 'thinking', 'receiving', 'complete']                    # check
        for stage in stages:
            self.manager._touch(self.task, {'stage': stage, 'received_bytes': 10})
        self.controller.store.change(lambda data, conn: data['agents']['maya']['generation'].update(
            operation_started=True))
        self.manager._touch(self.task, {'stage': 'complete'})
        rows = self.phases()
        self.assertEqual([row['details']['stage'] for row in rows], ['reading', 'dns', 'publishing'])
        self.assertEqual([row['details']['phase'] for row in rows], ['reading', 'model', 'publishing'])
        # The live job still shows the latest fine-grained stage for the UI.
        job = self.controller.store.read()['agents']['maya']['generation']
        self.assertEqual(job['stage'], 'publishing')

    def test_live_stage_keeps_updating_without_new_rows(self):
        self.manager._touch(self.task, {'stage': 'headers'})
        self.manager._touch(self.task, {'stage': 'thinking'})
        self.assertEqual(self.controller.store.read()['agents']['maya']['generation']['stage'], 'thinking')
        self.assertEqual(len(self.phases()), 1)

    def test_a_failed_job_names_the_stage_it_stopped_in(self):
        self.manager._touch(self.task, {'stage': 'headers'})
        self.manager._finish(self.task, 'failed', 'timeout')
        failed = next(e for e in self.controller.store.read()['events'] if e['kind'] == 'generation_failed')
        self.assertEqual(failed['details']['phase'], 'headers')
        # Only a failed round carries rejected text.
        self.assertNotIn('rejected_body', failed['details'])

    def test_a_completed_job_never_carries_rejected_text(self):
        self.manager._finish(self.task, 'completed', rejected={'action': 'post', 'body': 'x', 'issues': ['y']})
        done = next(e for e in self.controller.store.read()['events'] if e['kind'] == 'generation_completed')
        self.assertFalse({'action', 'rejected_body', 'rejected_issues'} & set(done['details']))

    def test_phase_mapping(self):
        self.assertEqual({phase(s) for s in ('dns', 'tls', 'headers', 'body', 'thinking', 'receiving', 'complete')},
                         {'model'})
        self.assertEqual([phase(s) for s in ('reading', 'publishing', 'cancelling')],
                         ['reading', 'publishing', 'cancelling'])


if __name__ == '__main__':
    unittest.main()
