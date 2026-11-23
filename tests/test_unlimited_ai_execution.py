import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from ctn_data.ai_jobs import AnalysisJobs
from ctn_data.db import connect, initialize
from tests.test_content_research_schedule import ResearchScheduleTests
from ctn_data import content_research as research
from tests.test_content_research import EVIDENCE


class PersistentJobTests(unittest.TestCase):
    def test_reopen_status_and_cancel_from_another_reader_without_restarting_model(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'jobs.sqlite3'
            initialize(path)
            slots = threading.BoundedSemaphore(2)
            first, second = AnalysisJobs(slots, path), AnalysisJobs(slots, path)
            entered = threading.Event()
            calls = []
            def execute(cancelled, progress):
                calls.append(1)
                progress({'stage': 'thinking', 'received_bytes': 2048})
                entered.set()
                cancelled.wait(5)
                return {'text': 'must not accept cancelled result'}
            item = first.start('one', execute, kind='research', scope='test', request_key='same-request')
            self.assertTrue(entered.wait(1))
            recovered = second.get('one', item['id'])
            self.assertEqual(recovered['stage'], 'thinking')
            self.assertEqual(recovered['received_bytes'], 2048)
            self.assertEqual(second.list('one', kind='research', scope='test')['items'][0]['id'], item['id'])
            duplicate = first.start('one', execute, request_key='same-request')
            self.assertEqual(duplicate['id'], item['id'])
            self.assertEqual(calls, [1])
            with self.assertRaises(Exception):
                second.get('another-member', item['id'])
            second.cancel('one', item['id'])
            first.jobs[item['id']]['thread'].join(3)
            self.assertEqual(second.get('one', item['id'])['state'], 'cancelled')
            self.assertTrue(slots.acquire(blocking=False))
            self.assertTrue(slots.acquire(blocking=False))

    def test_a_dead_process_is_reported_as_interrupted_and_never_restarted(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'jobs.sqlite3'
            initialize(path)
            manager = AnalysisJobs(threading.BoundedSemaphore(2), path)
            item = manager.start('owner', lambda cancel, progress: {'text': 'complete'})
            manager.jobs[item['id']]['thread'].join(2)
            conn = connect(path)
            try:
                with conn:
                    row = conn.execute('SELECT value FROM checkpoints WHERE key=?', ('interactive_ai_job_v2:' + item['id'],)).fetchone()
                    job = json.loads(row[0])
                    job.update(state='running', process_id=99999999, process_birth='not-a-real-process', result=None)
                    conn.execute('UPDATE checkpoints SET value=? WHERE key=?', (json.dumps(job), 'interactive_ai_job_v2:' + item['id']))
                other = AnalysisJobs(threading.BoundedSemaphore(2), path)
                self.assertEqual(other.get('owner', item['id'])['state'], 'failed')
                self.assertEqual(other.jobs, {})
            finally:
                conn.close()


class ResearchOwnershipTests(ResearchScheduleTests):
    def test_cancelled_or_replaced_owner_cannot_save_a_late_report(self):
        self.enable()
        _, run_id, _, _ = self.scheduler._claim()
        self.scheduler.cancel_current(run_id)
        for identifier, event in ((run_id, None), ('old-owner', None), (None, threading.Event())):
            if event is not None:
                event.set()
            with self.assertRaises(research.ResearchError):
                research.save_report(self.db, report='late report [post:7]', evidence=EVIDENCE,
                    provider_id='fake', model='fake', scope={}, now=self.now,
                    cancellation_event=event, scheduled_run_id=identifier)
        self.assertEqual(research.snapshot(self.db, self.now)['revision'], 0)

    def test_renewal_failure_cancels_but_exclusivity_outlives_expired_lease(self):
        entered, release = threading.Event(), threading.Event()
        cancelled = []
        def slow(settings, evidence, provider):
            entered.set()
            release.wait(5)
            cancelled.append(self.scheduler.cancellation_event.is_set())
            return self.save(settings, evidence, provider)
        self.scheduler.run = slow
        self.enable()
        worker = threading.Thread(target=self.scheduler.tick)
        worker.start()
        self.assertTrue(entered.wait(2))
        identifier = self.scheduler.run_id
        with patch('ctn_data.content_research_schedule.connect', side_effect=sqlite3.OperationalError('synthetic outage')):
            self.assertFalse(self.scheduler._heartbeat(identifier))
        self.assertTrue(self.scheduler.cancellation_event.is_set())
        self.now += 1000
        another = self.make_scheduler()
        another.tick()
        self.runner.assert_not_called()
        release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(cancelled, [True])
        self.assertEqual(self.scheduler.snapshot()['status']['last_result']['outcome'], 'cancelled')

    def test_progress_is_durable_and_single_round_cancel_preserves_settings(self):
        entered, release = threading.Event(), threading.Event()
        def slow(settings, evidence, provider):
            self.scheduler.record_progress({'stage': 'thinking', 'received_bytes': 1024})
            entered.set()
            release.wait(5)
            return self.save(settings, evidence, provider)
        self.scheduler.run = slow
        self.enable()
        worker = threading.Thread(target=self.scheduler.tick)
        worker.start()
        self.assertTrue(entered.wait(2))
        reader = self.make_scheduler()
        status = reader.snapshot()
        self.assertEqual(status['status']['progress']['stage'], 'thinking')
        self.assertEqual(status['status']['progress']['received_bytes'], 1024)
        before = status['settings']
        reader.cancel_current(status['status']['run_id'])
        self.scheduler._heartbeat(status['status']['run_id'])
        self.assertTrue(self.scheduler.cancellation_event.is_set())
        self.assertEqual(reader.snapshot()['settings'], before)
        release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())


if __name__ == '__main__':
    unittest.main()
