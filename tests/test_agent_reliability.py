"""Regression coverage for ambiguous deliveries, store writes and lost leases.

All SDKs/providers are fake and every database lives in a temporary directory.
"""
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ctn_data import agent_budget, agent_competition
from ctn_data.agent_control import AgentController, AgentError, AgentStore, PREFIX, _log_background_failure
from ctn_data.agent_generation import GenerationManager, _Task
from ctn_data.db import connect
from tests import test_agent_actions as actions_fixture
from tests.test_agent_generation_jobs import FakeController
from tests import test_agent_manual_execution as manual_fixture


class UncertainResolutionTests(unittest.TestCase):
    setUp = manual_fixture.ManualExecutionTests.setUp
    draft = manual_fixture.ManualExecutionTests.draft
    row = manual_fixture.ManualExecutionTests.row
    publish = manual_fixture.ManualExecutionTests.publish

    def uncertain(self):
        draft_id = self.draft('like')
        with patch.object(self.account, 'like', side_effect=TimeoutError('not delivered')):
            with self.assertRaises(AgentError):
                self.publish(draft_id)
        self.assertEqual(self.row(draft_id)['state'], 'uncertain')
        return draft_id

    def resolve(self, draft_id, **overrides):
        values = {'confirmed_not_executed': True, 'reason': '已检查账号点赞记录，目标未被点赞',
                  'expected_version': self.row(draft_id)['version'], 'confirmed_by': 'member@example.test'}
        values.update(overrides)
        return self.controller.discard_unexecuted(draft_id, **values)

    def test_explicit_confirmation_releases_role_without_replaying_or_refunding_attempt(self):
        draft_id = self.uncertain()
        fresh = self.draft('post', body='A new manual operation.')
        with self.assertRaisesRegex(AgentError, '操作结果待核对'):
            self.publish(fresh)
        before = self.controller.store.read()
        attempts = before['rate']['manual_accounts'][self.agent_id]['attempts']
        reservation_key = self.row(draft_id)['reservation']['key']
        with patch.object(self.controller, '_sdk', side_effect=AssertionError('must not call remote')):
            self.resolve(draft_id)
        state = self.controller.store.read()
        self.assertEqual(self.row(draft_id)['state'], 'discarded')
        self.assertEqual(self.row(draft_id)['manual_resolution']['confirmed_by'], 'member@example.test')
        self.assertNotIn(reservation_key, state['rate']['hashes'])
        self.assertEqual(state['rate']['manual_accounts'][self.agent_id]['attempts'], attempts)
        events = self.controller.logs(self.agent_id, kind='discard_unexecuted')['items']
        self.assertEqual(len(events), 1)
        self.assertIn('目标未被点赞', events[0]['message'])
        self.assertIn('member@example.test', events[0]['message'])
        self.assertFalse(events[0]['details']['reconciled'])
        self.assertEqual(self.publish(fresh)['state'], 'published')

    def test_confirmation_reason_and_current_version_are_required(self):
        draft_id = self.uncertain()
        for changes in ({'confirmed_not_executed': False}, {'confirmed_not_executed': 'true'},
                        {'reason': '  '}, {'reason': '短'}, {'expected_version': 999},
                        {'confirmed_by': ''}):
            with self.subTest(changes=changes), self.assertRaises(AgentError):
                self.resolve(draft_id, **changes)
            self.assertEqual(self.row(draft_id)['state'], 'uncertain')
        with self.assertRaises(AgentError):
            self.controller.discard(draft_id)

    def test_repeated_delivery_of_confirmation_is_idempotent_and_sending_is_rejected(self):
        draft_id = self.uncertain()
        version = self.row(draft_id)['version']
        self.resolve(draft_id, expected_version=version)
        self.resolve(draft_id, expected_version=version)
        events = self.controller.logs(self.agent_id, kind='discard_unexecuted')['items']
        self.assertEqual(len(events), 1)
        pending = self.draft('post', body='Another operation.')
        self.controller.store.change(lambda data, conn: data['drafts'][pending].update(
            state='sending', lease_until=self.now + 300))
        with self.assertRaises(AgentError):
            self.resolve(pending)

    def test_newer_state_or_active_role_cannot_be_overwritten(self):
        draft_id = self.uncertain()
        with self.controller._claim(self.agent_id):
            with self.assertRaises(AgentError):
                self.resolve(draft_id)
        version = self.row(draft_id)['version']
        self.controller.store.change(lambda data, conn: data['drafts'][draft_id].update(state='published'))
        with self.assertRaises(AgentError):
            self.resolve(draft_id, expected_version=version)
        self.assertEqual(self.row(draft_id)['state'], 'published')


class AgentStoreWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'store.sqlite3'
        self.store = AgentStore(self.path)
        self.sql = []
        def traced(path):
            conn = connect(path)
            conn.set_trace_callback(self.sql.append)
            return conn
        self.trace = patch('ctn_data.agent_control.connect', side_effect=traced)
        self.trace.start()
        self.addCleanup(self.trace.stop)

    def writes(self):
        return [line for line in self.sql if line.startswith('INSERT INTO meta(')]

    def test_noop_store_change_does_not_rewrite_partitions(self):
        self.store.change(lambda data, conn: None)
        self.assertEqual(self.writes(), [])

    def test_nested_change_writes_only_its_partition_and_persists_migrations(self):
        self.store.change(lambda data, conn: data['memory'].update({'role': {'summary': 'observed'}}))
        # Memory persists per role field; every write stays in that partition.
        self.assertTrue(self.writes())
        self.assertTrue(all("'" + PREFIX + 'memory' in line for line in self.writes()), self.writes())
        conn = connect(self.path)
        try:
            with conn:
                conn.execute('DELETE FROM meta WHERE key=?', (PREFIX + 'operations',))
        finally:
            conn.close()
        self.sql.clear()
        self.store.change(lambda data, conn: None)
        self.assertEqual(len(self.writes()), 1)
        self.assertIn(PREFIX + 'operations', self.writes()[0])
        self.assertEqual(self.store.read()['memory']['role']['summary'], 'observed')

    def test_failure_rolls_back_and_leaves_other_controller_updates_intact(self):
        other = AgentStore(self.path)
        other.change(lambda data, conn: data['rate'].update(observed=42))
        def fail(data, conn):
            data['memory']['bad'] = True
            raise RuntimeError('synthetic')
        with self.assertRaises(RuntimeError):
            self.store.change(fail)
        self.assertNotIn('bad', self.store.read()['memory'])
        self.store.change(lambda data, conn: data['memory'].update(good=True))
        self.assertEqual(other.read()['rate']['observed'], 42)

    def test_sdk_attempt_is_committed_before_request_and_only_rate_is_written(self):
        controller = SimpleNamespace(store=self.store, db_path=self.path, clock=lambda: 1792450000.0)
        def get_post():
            totals = self.store.read()['rate']['quota_usage_totals']['']['read:get_post']
            self.assertEqual(totals['attempts'], 1)
            self.assertNotIn('confirmed_success', totals)
            return 'remote result'
        self.assertEqual(agent_budget.call(controller, 'read', get_post), 'remote result')
        self.assertEqual(len(self.writes()), 3)
        self.assertTrue(all(PREFIX + 'rate' in sql for sql in self.writes()))


class CompetitionRefreshLockTests(unittest.TestCase):
    def test_parallel_state_change_during_replay_is_preserved_and_refresh_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1792450000.0]
            path = Path(directory) / 'competition.sqlite3'
            controller = AgentController(path, Path(directory) / 'keys', clock=lambda: now[0])
            other = AgentStore(path)
            controller.competition({'expected_revision': 0, 'mode': 'observe'})
            prepare = agent_competition.prepare_refresh
            def concurrent_change(conn, data, at):
                prepared = prepare(conn, data, at)
                # This separate writer must be able to commit while historical
                # reads still have their snapshot open. Applying that stale
                # result must not clobber the just-committed settings.
                other.change(lambda state, write_conn: state['settings'].update(team_side='New setting'))
                return prepared
            with patch.object(agent_competition, 'prepare_refresh', side_effect=concurrent_change):
                controller._competition_tick()
            state = other.read()
            self.assertEqual(state['settings']['team_side'], 'New setting')
            self.assertIsNone(state['rate']['competition'].get('last_refresh_at'))
            self.assertEqual(controller._competition_next_refresh, now[0] + 2)
            now[0] += 2
            controller._competition_tick()
            self.assertEqual(other.read()['rate']['competition']['last_refresh_at'], now[0])


class GenerationBackoffTests(unittest.TestCase):
    def setUp(self):
        self.controller = FakeController()
        self.manager = GenerationManager(self.controller)
        self.controller.manager = self.manager
        self.task = _Task('maya', str(uuid.uuid4()), True)
        self.controller.store.data['agents']['maya'].update(next_run_at=0, generation={
            'id': self.task.job_id, 'owner': self.controller.owner, 'status': 'running',
            'heartbeat_until': self.controller.now + 45})

    def test_failure_sets_both_gates_with_bounded_increasing_backoff(self):
        self.manager._finish(self.task, 'failed', 'lease lost')
        first = self.controller.store.read()['agents']['maya']
        self.assertEqual(first['next_run_at'], self.controller.now + 60)
        self.assertEqual(first['decision_retry_at'], self.controller.now + 60)
        self.controller.store.data['agents']['maya']['generation']['status'] = 'running'
        self.manager._finish(self.task, 'failed', 'lease lost again')
        second = self.controller.store.read()['agents']['maya']
        self.assertEqual(second['next_run_at'], self.controller.now + 120)

    def test_stale_worker_never_changes_replacement_job_or_schedule(self):
        agent = self.controller.store.data['agents']['maya']
        agent.update(next_run_at=321, decision_retry_at=654)
        agent['generation']['id'] = 'replacement'
        before = self.controller.store.read()
        self.manager._finish(self.task, 'failed', 'lease lost')
        self.assertEqual(self.controller.store.read(), before)

    def test_recovered_job_gets_backoff_only_once(self):
        self.controller.now += 46
        self.controller.store.change(lambda data, conn: self.manager.recover(data))
        first = self.controller.store.read()['agents']['maya']
        self.assertEqual(first['generation']['status'], 'failed')
        self.assertEqual(first['decision_retry_at'], self.controller.now + 60)
        self.controller.store.change(lambda data, conn: self.manager.recover(data))
        self.assertEqual(self.controller.store.read()['agents']['maya'], first)

    def test_real_controller_lease_loss_uses_fallback_backoff(self):
        fixture = actions_fixture.AutonomousActions()
        fixture.setUp()
        try:
            task = _Task(fixture.agent_id, str(uuid.uuid4()), True)
            def prepare(data, conn):
                data['agents'][fixture.agent_id].update(next_run_at=0, generation={
                    'id': task.job_id, 'owner': fixture.controller.owner, 'status': 'queued',
                    'heartbeat_until': fixture.now + 45})
            fixture.controller.store.change(prepare)
            def expire(agent, token, automatic):
                fixture.controller.store.change(lambda data, conn:
                    data['leases']['agent:' + fixture.agent_id].update(until=fixture.now - 1))
                raise AgentError('synthetic lease loss', 409)
            with patch.object(fixture.controller, '_generate', side_effect=expire):
                fixture.controller.generations._worker(task)
            agent = fixture.controller.store.read()['agents'][fixture.agent_id]
            self.assertEqual(agent['generation']['status'], 'failed')
            self.assertGreater(agent['next_run_at'], fixture.now)
            self.assertGreater(agent['decision_retry_at'], fixture.now)
        finally:
            fixture.doCleanups()


class BackgroundLoggingTests(unittest.TestCase):
    def test_background_log_retains_stack_without_exception_payload(self):
        try:
            raise RuntimeError('secret-provider-response')
        except RuntimeError as exc:
            with self.assertLogs('ctn_data.agent_control', level='ERROR') as logged:
                _log_background_failure('Agent test failed', exc)
        text = '\n'.join(logged.output)
        self.assertIn('Agent test failed (RuntimeError)', text)
        # Traceback source may include a literal in synthetic code, but runtime
        # exception values must never be interpolated into the log.
        self.assertNotIn('RuntimeError: secret-provider-response', text)


if __name__ == '__main__':
    unittest.main()
