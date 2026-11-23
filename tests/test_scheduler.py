from contextlib import closing
from datetime import datetime, timezone
import sqlite3
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from ctn_data import task_queue
from ctn_data.db import connect, connection_lost, initialize, reconnect_if_lost
from ctn_data.scheduler import CollectionScheduler, collection_health


class DyingConnection:
    """A real SQLite connection that fails like a PostgreSQL session after a server restart."""
    def __init__(self, path):
        self._path, self._opened = path, None
        self.raw = types.SimpleNamespace(closed=False, broken=False)
        self.closed_by_caller = False

    @property
    def _conn(self):
        # Opened by the thread that uses it, like the scheduler's own connect().
        if self._opened is None:
            self._opened = connect(self._path)
        return self._opened

    def kill(self):
        self.raw.closed = self.raw.broken = True

    def execute(self, *args):
        if self.raw.closed:
            raise sqlite3.OperationalError('server closed the connection unexpectedly')
        return self._conn.execute(*args)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def close(self):
        self.closed_by_caller = True
        self._conn.close()


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'scheduler.db'
        initialize(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_pause_persists_and_does_not_overlap_running_cycle(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def run(*args):
            calls.append(1); entered.set(); release.wait(3)
            return dict(status='completed', run_id=1)
        scheduler = CollectionScheduler(self.db, {}, True, run)
        scheduler.start()
        try:
            self.assertTrue(entered.wait(2))
            scheduler.configure(False, 60)
            self.assertTrue(scheduler.status()['running'])
            self.assertFalse(scheduler.status()['enabled'])
            scheduler.start()
            self.assertEqual(len(calls), 1)
        finally:
            release.set(); scheduler.stop()
        self.assertIsNone(scheduler.status()['next_run_at'])
        restored = CollectionScheduler(self.db, {}, True, run)
        self.assertFalse(restored.enabled)
        self.assertEqual(restored.interval, 60)

    def test_failure_is_recorded_and_next_cycle_is_scheduled(self):
        finished = threading.Event()
        def run(*args):
            finished.set()
            raise RuntimeError('simulated failure')
        scheduler = CollectionScheduler(self.db, {}, True, run)
        scheduler.start()
        self.assertTrue(finished.wait(2))
        # Stop joins the worker so assertions never race its finally block.
        scheduler.stop()
        self.assertEqual(scheduler.status()['last_error'], 'RuntimeError')
        self.assertIsNotNone(scheduler.status()['last_finished_at'])

    def test_disabled_start_does_not_collect(self):
        entered = threading.Event()
        scheduler = CollectionScheduler(self.db, {}, False, lambda *args: entered.set())
        scheduler.start()
        self.assertFalse(entered.wait(.05))
        scheduler.stop()

    def test_worker_lane_replaces_a_dead_connection_and_claims_again(self):
        dying = DyingConnection(self.db)
        scheduler = CollectionScheduler(self.db, {}, True)
        with closing(connect(self.db)) as conn:
            task_queue.ensure_schema(conn)
            scheduler._seed(conn)
        kinds, resumed = [], threading.Event()
        def stage(db_path, config, kind):
            kinds.append(kind)
            if len(kinds) == 1:
                dying.kill()  # The database restarts while the first task runs.
            else:
                resumed.set()
                scheduler.stopping = True
            return {'status': 'completed', 'errors': [], 'requests': 0}
        with patch('ctn_data.scheduler.connect', return_value=dying), patch('ctn_data.scheduler.collect_stage', stage):
            thread = threading.Thread(target=scheduler._worker_loop, args=('auxiliary', ('auxiliary', 'social_context')))
            thread.start()
            try:
                self.assertTrue(resumed.wait(5), scheduler.lane_errors)
            finally:
                scheduler.stopping = True
                thread.join(5)
        # The first task's lease is retained and retried after expiry; the
        # lane itself claims the next task on a new connection.
        self.assertEqual(kinds, ['auxiliary', 'social_context'])
        self.assertTrue(dying.closed_by_caller)
        self.assertNotIn('auxiliary', scheduler.lane_errors)

    def test_planner_replaces_a_dead_connection(self):
        dying = DyingConnection(self.db)
        scheduler = CollectionScheduler(self.db, {}, True)
        scheduler.PLAN_SECONDS = 0
        planned, replanned = [], threading.Event()
        def plan(conn, config):
            planned.append(conn)
            if conn is dying:
                dying.kill()  # The snapshot right after this plan fails.
            else:
                replanned.set()
            return 0
        with patch('ctn_data.scheduler.connect', side_effect=[dying]), \
                patch('ctn_data.scheduler.plan_counter_tasks', plan), \
                patch.object(CollectionScheduler, '_worker_loop', lambda self, lane, kinds: None):
            scheduler.start()
            try:
                self.assertTrue(replanned.wait(5), scheduler.lane_errors)
            finally:
                scheduler.stop()
        self.assertIs(planned[0], dying)
        self.assertIsNot(planned[-1], dying)
        self.assertTrue(dying.closed_by_caller)
        self.assertNotIn('planner', scheduler.lane_errors)


class ReconnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'reconnect.db'
        initialize(self.db)

    def test_live_connections_are_kept(self):
        with closing(connect(self.db)) as conn:
            self.assertFalse(connection_lost(conn))
            self.assertIs(reconnect_if_lost(conn, self.db), conn)
        alive = DyingConnection(self.db)
        self.addCleanup(alive.close)
        self.assertIs(reconnect_if_lost(alive, self.db), alive)

    def test_dead_connection_is_closed_and_replaced(self):
        dying = DyingConnection(self.db)
        dying.kill()
        replacement = reconnect_if_lost(dying, self.db)
        self.addCleanup(replacement.close)
        self.assertTrue(dying.closed_by_caller)
        self.assertEqual(replacement.execute('SELECT 1').fetchone()[0], 1)

    def test_dead_pooled_session_handed_out_after_a_restart_is_skipped(self):
        dying, stale = DyingConnection(self.db), DyingConnection(self.db)
        dying.kill()
        stale.kill()  # Idle in the pool when the server restarted; not yet noticed.
        live = connect(self.db)
        self.addCleanup(live.close)
        with patch('ctn_data.db.connect', side_effect=[stale, live]):
            self.assertIs(reconnect_if_lost(dying, self.db), live)
        self.assertTrue(stale.closed_by_caller)

    def test_database_still_down_returns_the_dead_connection_without_raising(self):
        dying = DyingConnection(self.db)
        dying.kill()
        with patch('ctn_data.db.connect', side_effect=sqlite3.OperationalError('connection refused')):
            self.assertIs(reconnect_if_lost(dying, self.db), dying)
        self.assertTrue(connection_lost(dying))


class CollectionHealthTests(unittest.TestCase):
    NOW = datetime(2026, 10, 24, 1, 0, tzinfo=timezone.utc).timestamp()

    def status(self, **overrides):
        return {'enabled': True, 'active': True, 'started_at': '2026-10-24T00:00:00.000000Z',
                'lane_errors': {}, **overrides}

    def test_quiet_direct_lanes_are_reported_stalled(self):
        health = collection_health(self.status(lane_errors={'counters': 'OperationalError'}),
                                   '2026-10-24T00:45:00.000000Z', now=self.NOW)
        self.assertTrue(health['stalled'])
        self.assertEqual(health['quiet_minutes'], 15.0)
        self.assertEqual(health['lane_errors'], {'counters': 'OperationalError'})

    def test_recent_run_or_fresh_start_is_not_stalled(self):
        recent = collection_health(self.status(), '2026-10-24T00:55:00.000000Z', now=self.NOW)
        self.assertFalse(recent['stalled'])
        self.assertEqual(recent['quiet_minutes'], 5.0)
        fresh = collection_health(self.status(started_at='2026-10-24T00:58:00.000000Z'),
                                  '2026-10-23T22:00:00.000000Z', now=self.NOW)
        self.assertFalse(fresh['stalled'])
        self.assertEqual(fresh['quiet_minutes'], 2.0)

    def test_paused_is_not_stalled_but_a_dead_scheduler_thread_is(self):
        old = '2026-10-23T22:00:00.000000Z'
        self.assertFalse(collection_health(self.status(enabled=False), old, now=self.NOW)['stalled'])
        self.assertTrue(collection_health(self.status(active=False), '2026-10-24T00:59:00.000000Z',
                                          now=self.NOW)['stalled'])
