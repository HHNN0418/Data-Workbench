"""Verify research recovery in a disposable PostgreSQL database with fake AI.

Run from the project root. The configured database is used only to create and
drop a random test database. Connection details and exception text are never
printed or included in the verification report.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import re
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ctn_data import agent_maintenance as maintenance
from ctn_data import content_research as research
from ctn_data import content_research_schedule as schedule
from ctn_data.agent_control import AgentStore
from ctn_data.db import begin_write, connect, initialize
from tests.verify_agent_interaction_policy_postgres import main as isolated_main


PREFIX = 'ctn_research_recovery_test_'
EVIDENCE = {
    'sources': [{'id': 'post:7', 'kind': 'post', 'post_id': 7,
                 'title': 'Isolated fixture', 'text': 'A fictional public discussion.'}],
    'summary': {'scope_count': 1},
}


def run(target, database_name, report):
    assert re.fullmatch(PREFIX + r'[0-9a-f]{32}', database_name), 'unexpected isolated database'
    with closing(connect(target)) as conn:
        assert conn.execute('SELECT current_database()').fetchone()[0] == database_name, 'isolated database mismatch'
    initialize(target)
    mock_model_calls = []

    class PostgreSQLResearchRecoveryTests(unittest.TestCase):
        def setUp(self):
            with closing(connect(target)) as conn, conn:
                self.assertEqual(conn.execute('SELECT current_database()').fetchone()[0], database_name)
                conn.execute('DELETE FROM meta WHERE key LIKE ?', ('agent_control_v1:%',))
                conn.execute('DELETE FROM checkpoints WHERE key IN (?,?)', (schedule.KEY, research.KEY))
                conn.execute('DELETE FROM posts WHERE post_id=?', (7,))
                conn.execute('INSERT INTO posts(post_id,author_id,body,kind,visibility,created_at,first_seen,last_seen) '
                             'VALUES(?,?,?,?,?,?,?,?)',
                             (7, 99, EVIDENCE['sources'][0]['text'], 'post', 'public',
                              '2026-10-21T00:00:00Z', '2026-10-21T00:00:00Z', '2026-10-21T00:00:00Z'))
            self.store = AgentStore(target)
            self.store.change(lambda data, conn: data['settings'].update(enabled=True))
            self.mid = str(uuid.uuid4())
            # A fixed clock uses exact seconds so ISO microsecond rounding
            # cannot place an immediate retry a fraction ahead of that clock.
            self.now = float(int(time.time()))
            self.provider = {'id': 'isolated-fixture', 'protocol': 'openai_chat', 'model': 'fake-model'}
            self.provider_for = Mock(return_value=self.provider)
            self.prepare = Mock(side_effect=lambda settings: deepcopy(EVIDENCE))
            self.runner = Mock(side_effect=self.save)
            self.scheduler = self.make_scheduler()
            self.seed()

        def make_scheduler(self):
            return schedule.ResearchAutomation(
                target, provider_for=self.provider_for, prepare=self.prepare,
                run=self.runner, clock=lambda: self.now)

        def save(self, settings, evidence, provider):
            mock_model_calls.append(self.id().rsplit('.', 1)[-1])
            return research.save_report(
                target, report='Fictional discussion [post:7]', evidence=evidence,
                provider_id=provider['id'], model=provider['model'], scope={}, now=self.now)

        def state(self):
            with closing(connect(target)) as conn:
                return schedule._load(conn)

        def seed(self, *, settings=None, status=None):
            value = {
                'settings': {**deepcopy(schedule.DEFAULTS), 'enabled': True,
                             'provider_id': self.provider['id'], **(settings or {})},
                'status': {**{field: None for field in schedule.STATUS_FIELDS}, **(status or {})},
            }
            with closing(connect(target)) as conn, conn:
                begin_write(conn, 'content_research')
                schedule._save(conn, value)
            return deepcopy(value)

        def test_maintenance_gate_blocks_new_research_claims(self):
            before = self.state()['settings']
            maintenance.begin(self.store, self.mid)
            self.assertEqual(self.state()['status']['maintenance_id'], self.mid)
            self.assertIsNone(self.scheduler._claim())
            self.scheduler.tick()
            self.provider_for.assert_not_called()
            self.prepare.assert_not_called()
            self.runner.assert_not_called()
            self.assertEqual(self.state()['settings'], before)
            maintenance.finish(self.store, self.mid, aborted=True)
            self.assertNotIn('maintenance_id', self.state()['status'])
            self.assertIsNotNone(self.scheduler._claim())

        def test_live_research_blocks_drain_without_cancellation(self):
            self.seed(status={'run_id': 'live-fixture', 'lease_until': self.now + 120,
                              'cancel_requested': False})
            held = maintenance.begin(self.store, self.mid)
            self.assertEqual(held['active_research'], 1)
            self.assertFalse(held['drained'])
            self.assertEqual(maintenance.store_snapshot(self.store)['active_research'], 1)
            self.assertIsNone(maintenance.fence_drained(self.store, self.mid))
            with self.assertRaises(maintenance.MaintenanceError):
                maintenance.wait_drained(self.store, self.mid, timeout=0)
            with self.assertRaises(maintenance.MaintenanceError):
                maintenance.finish(self.store, self.mid)
            current = self.state()
            self.assertEqual(current['status']['run_id'], 'live-fixture')
            self.assertFalse(current['status']['cancel_requested'])
            self.assertTrue(current['settings']['enabled'])
            maintenance.finish(self.store, self.mid, aborted=True)

        def test_postgres_owner_lock_blocks_stale_recovery_and_maintenance(self):
            due = schedule._stamp(self.now + 3600)
            self.seed(status={'run_id': 'stale-owned', 'lease_until': self.now - 1,
                              'next_run_at': due, 'started_input_hash': 'same-fixture'})
            # Hold only the real PG session lock, not the local file/thread lock.
            # This checks exclusion against a different process or host.
            with closing(connect(target)) as owner:
                self.assertTrue(owner.execute(
                    'SELECT pg_try_advisory_lock(?)', (schedule.OWNER_LOCK,)).fetchone()[0])
                owner.commit()
                self.scheduler.tick()
                self.assertEqual(self.state()['status']['run_id'], 'stale-owned')
                with self.assertRaises(research.ResearchError):
                    self.scheduler.retry_interrupted('stale-owned')
                maintenance.begin(self.store, self.mid)
                self.assertIsNone(maintenance.fence_drained(self.store, self.mid))
                with self.assertRaises(maintenance.MaintenanceError):
                    maintenance.wait_drained(self.store, self.mid, timeout=0)
                with self.assertRaises(maintenance.MaintenanceError):
                    maintenance.finish(self.store, self.mid)
            # Session close releases the independent PG owner lock. Recovery
            # precedes the future due time and the durable maintenance gate.
            self.scheduler.tick()
            status = self.state()['status']
            self.assertNotIn('run_id', status)
            self.assertEqual(status['last_result']['outcome'], 'interrupted')
            self.assertEqual(status['last_result']['run_id'], 'stale-owned')
            self.assertEqual(status['next_run_at'], due)
            self.assertEqual(status['maintenance_id'], self.mid)
            self.runner.assert_not_called()
            self.prepare.assert_not_called()
            self.assertTrue(maintenance.wait_drained(self.store, self.mid, timeout=0)['drained'])
            maintenance.finish(self.store, self.mid)

        def test_read_snapshot_does_not_recover_and_tick_recovers_before_due(self):
            before = self.seed(status={
                'run_id': 'abandoned-fixture', 'lease_until': self.now - 1,
                'next_run_at': schedule._stamp(self.now + 3600), 'started_input_hash': 'unchanged'})
            view = self.scheduler.snapshot()['status']
            self.assertFalse(view['running'])
            self.assertTrue(view['stale'])
            self.assertEqual(self.state(), before)
            self.scheduler.tick()
            status = self.state()['status']
            self.assertEqual(status['last_result']['outcome'], 'interrupted')
            self.assertEqual(status['last_result']['reason'], 'owner_lost')
            self.assertEqual(status['blocked_input_hash'], 'unchanged')
            self.assertEqual(status['next_run_at'], before['status']['next_run_at'])
            self.assertNotIn('run_id', status)
            self.provider_for.assert_not_called()
            self.runner.assert_not_called()

        def test_concurrent_retry_and_workers_run_one_mock_request(self):
            fingerprint = schedule._fingerprint(self.state()['settings'], self.provider, EVIDENCE)
            self.seed(status={'last_result': {'outcome': 'interrupted', 'run_id': 'retry-fixture'},
                              'blocked_input_hash': fingerprint,
                              'next_run_at': schedule._stamp(self.now + 3600)})
            schedulers = [self.scheduler, self.make_scheduler()]
            barrier = threading.Barrier(2)

            def retry(worker):
                barrier.wait(timeout=15)
                try:
                    worker.retry_interrupted('retry-fixture')
                    return 'accepted'
                except research.ResearchError:
                    return 'rejected'

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(retry, schedulers))
            self.assertEqual(sorted(results), ['accepted', 'rejected'])
            started, release = threading.Event(), threading.Event()

            def blocked_save(settings, evidence, provider):
                started.set()
                if not release.wait(15):
                    raise AssertionError('mock request release timed out')
                return self.save(settings, evidence, provider)

            self.runner.side_effect = blocked_save
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(schedulers[0].tick)
                try:
                    self.assertTrue(started.wait(15), 'mock request was not started')
                    second = pool.submit(schedulers[1].tick)
                    second.result(timeout=15)
                    self.assertEqual(self.runner.call_count, 1)
                finally:
                    release.set()
                first.result(timeout=15)
            self.assertEqual(self.state()['status']['last_result']['outcome'], 'generated')
            self.assertEqual(self.runner.call_count, 1)
            with self.assertRaises(research.ResearchError):
                self.scheduler.retry_interrupted('retry-fixture')
            for worker in schedulers:
                worker.tick()
            self.assertEqual(self.runner.call_count, 1)

        def test_abort_and_finish_preserve_enabled_preferences(self):
            for aborted in (False, True):
                for agent_enabled in (False, True):
                    for research_enabled in (False, True):
                        with self.subTest(aborted=aborted, agent_enabled=agent_enabled,
                                          research_enabled=research_enabled):
                            mid = str(uuid.uuid4())
                            self.store.change(lambda data, conn: data['settings'].update(enabled=agent_enabled))
                            before = self.seed(settings={'enabled': research_enabled, 'auto_activate': True})
                            maintenance.begin(self.store, mid)
                            self.assertEqual(self.state()['settings'], before['settings'])
                            maintenance.wait_drained(self.store, mid, timeout=0)
                            finished = maintenance.finish(self.store, mid, aborted=aborted)
                            self.assertFalse(finished['active'])
                            self.assertEqual(self.store.read()['settings']['enabled'], agent_enabled)
                            self.assertEqual(self.state()['settings'], before['settings'])
                            self.assertNotIn('maintenance_id', self.state()['status'])

        def test_agent_pause_and_research_gate_rollback_together(self):
            before = self.state()
            with patch.object(schedule, '_save', side_effect=RuntimeError('fixture write failure')):
                with self.assertRaises(RuntimeError):
                    maintenance.begin(self.store, self.mid)
            data = self.store.read()
            self.assertTrue(data['settings']['enabled'])
            self.assertFalse(data['rate'].get('maintenance', {}).get('active'))
            self.assertEqual(self.state(), before)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PostgreSQLResearchRecoveryTests)
    test_names = [test.id().rsplit('.', 1)[-1] for test in suite]
    result = unittest.TestResult()
    with patch('ctn_data.ai_providers.complete', side_effect=AssertionError('real AI forbidden')) as real_model:
        suite.run(result)
        real_model.assert_not_called()
    failed = {test.id().rsplit('.', 1)[-1] for test, _ in (*result.failures, *result.errors)}
    for name in test_names:
        report['checks'][name.removeprefix('test_')] = name not in failed
    report.update(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                  real_model_calls=0, mock_model_calls=len(mock_model_calls), external_http_calls=0)
    if failed:
        report['failed_tests'] = sorted(failed)
        # TestResult retains diagnostic tracebacks only in memory. Do not emit
        # raw database exceptions, which can contain connection information.
        raise AssertionError('isolated research recovery regression failed')


if __name__ == '__main__':
    isolated_main(
        run_checks=run, database_prefix=PREFIX,
        default_report='reports/generated/research-recovery-postgres-verification.json')
