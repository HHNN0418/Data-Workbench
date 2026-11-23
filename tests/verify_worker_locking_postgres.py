"""Opt-in queue concurrency regression in a disposable PostgreSQL database.

Run from the repository root with ``python -m tests.verify_worker_locking_postgres``.
Only the generated database is initialized, written to, and dropped. Credentials
are read from the existing secret file and never included in the report.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

import psycopg
from psycopg import sql

from ctn_data import coordination, feed_tasks, task_queue
from ctn_data.collector import _config, _refresh, record_counter_turn
from ctn_data.db import connect, initialize, utc_now
from ctn_data.ingest import save_posts


def run(target):
    initialize(target)
    connections = []
    background = []
    checks = {}
    timings = {}

    def connection():
        conn = connect(target)
        # A regression must fail promptly while the deliberately held lock is
        # still held, rather than pass after it is released by another thread.
        conn.execute("SET statement_timeout='2500ms'")
        conn.execute("SET lock_timeout='2000ms'")
        connections.append(conn)
        return conn

    def timed(name, action):
        started = time.monotonic()
        value = action()
        timings[name] = round((time.monotonic() - started) * 1000, 3)
        return value

    def seed(conn, key, kind, due=100):
        with conn:
            task_queue.enqueue(conn, key, kind, {'fixture': True}, due_at=due)

    def start_transaction(name, action):
        ready, done = threading.Event(), threading.Event()
        state = {'name': name, 'ready': ready, 'done': done}

        def write():
            try:
                with closing(connect(target)) as conn:
                    conn.execute("SET statement_timeout='3500ms'")
                    conn.execute("SET lock_timeout='3000ms'")
                    state['pid'] = conn.execute('SELECT pg_backend_pid()').fetchone()[0]
                    ready.set()
                    with conn:
                        state['result'] = action(conn)
            except BaseException as exc:
                state['error'] = type(exc).__name__ + ': ' + str(exc)[:200]
                ready.set()
            finally:
                done.set()

        thread = threading.Thread(target=write, name=name, daemon=True)
        state['thread'] = thread
        background.append(thread)
        thread.start()
        assert ready.wait(5), name + ' did not connect'
        assert 'error' not in state, state.get('error')
        return state

    def wait_blocked(observer, state):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            waiting = observer.execute(
                'SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=? AND NOT granted)',
                (state['pid'],)).fetchone()[0]
            if waiting:
                return
            assert not state['done'].is_set(), state.get('error', state['name'] + ' unexpectedly finished')
            state['done'].wait(.01)
        raise AssertionError(state['name'] + ' did not reach its expected lock wait')

    def finish_transactions(*states):
        for state in states:
            state['thread'].join(timeout=6)
            assert not state['thread'].is_alive(), state['name'] + ' did not finish'
            assert 'error' not in state, state.get('error')

    try:
        owner, other, observer = connection(), connection(), connection()
        seed(owner, 'slow:a', 'slow')
        seed(owner, 'slow:b', 'slow', due=101)
        held = task_queue.claim(owner, 'slow-owner', ['slow'], now=100, lease_seconds=5)
        assert held['task_key'] == 'slow:a'
        task_queue.begin_completion(owner, held['task_key'])
        assert owner.execute('SHOW transaction_isolation').fetchone()[0] == 'read committed'
        owner.execute("INSERT INTO meta(key,value) VALUES('uncommitted-observation','pending')")
        task_queue.begin_completion(owner, held['task_key'])
        assert observer.execute("SELECT value FROM meta WHERE key='uncommitted-observation'").fetchone() is None

        # The locked row is already eligible for lease recovery at now=106.
        # SKIP LOCKED must select the next task instead of waiting or stealing it.
        claimed = timed('claim_while_other_result_locked', lambda: task_queue.claim(
            other, 'other-worker', ['slow'], now=106))
        assert claimed and claimed['task_key'] == 'slow:b'
        assert task_queue.claim(other, 'cannot-steal', ['slow'], now=106) is None
        assert observer.execute("SELECT lease_token FROM collection_tasks WHERE task_key='slow:a'").fetchone()[0] == held['lease_token']
        checks['locked_expired_task_skipped_without_reclaim'] = True

        permit = timed('permit_while_result_locked', lambda: coordination.permit(
            other, '/api/posts/11001/counters', 'lock-test', now=100))
        assert permit['allowed'], permit
        checks['limiter_progresses_while_result_locked'] = True
        assert timed('complete_other_task_while_result_locked', lambda: task_queue.complete(
            other, claimed['task_key'], claimed['lease_token'], {'status': 'saved'}, now=107))
        checks['other_task_claim_and_completion_progress'] = True

        owner.rollback()
        assert observer.execute("SELECT value FROM meta WHERE key='uncommitted-observation'").fetchone() is None
        replacement = task_queue.claim(other, 'replacement', ['slow'], now=106)
        assert replacement and replacement['task_key'] == held['task_key']
        assert replacement['lease_token'] != held['lease_token']
        stale_writes = []
        assert not task_queue.complete(owner, held['task_key'], held['lease_token'],
                                       ingest_callback=lambda conn: stale_writes.append(True), now=107)
        assert not stale_writes
        assert task_queue.owned(observer, replacement['task_key'], replacement['lease_token'], now=107)
        assert task_queue.complete(other, replacement['task_key'], replacement['lease_token'], now=107)
        checks['expired_observation_cannot_overwrite_replacement'] = True
        checks['completion_helper_does_not_commit_caller_data'] = True

        # Existing endpoint-coordination clients still use the global advisory
        # lock. Their transaction must not stall unrelated queue work.
        seed(owner, 'global:complete', 'global-complete')
        completion = task_queue.claim(owner, 'complete-worker', ['global-complete'], now=100)
        seed(owner, 'global:claim', 'global-claim')
        locker = connection()
        locker.execute('BEGIN IMMEDIATE')
        try:
            independent = timed('claim_while_global_coordinator_locked', lambda: task_queue.claim(
                other, 'claim-worker', ['global-claim'], now=101))
            assert independent and independent['task_key'] == 'global:claim'
            assert timed('complete_while_global_coordinator_locked', lambda: task_queue.complete(
                other, completion['task_key'], completion['lease_token'], now=101))
        finally:
            locker.rollback()
        assert task_queue.complete(other, independent['task_key'], independent['lease_token'], now=102)
        checks['queue_claim_and_completion_independent_of_global_lock'] = True

        seed(owner, 'rollback:one', 'rollback')
        rollback_task = task_queue.claim(owner, 'rollback-worker', ['rollback'], now=100)

        def broken_ingest(conn):
            save_posts(conn, [{'post_id': 12001, 'body': 'must roll back'}], 'locking-fixture')
            raise ValueError('intentional regression-test rollback')

        try:
            task_queue.complete(owner, rollback_task['task_key'], rollback_task['lease_token'],
                                ingest_callback=broken_ingest, now=101)
        except ValueError as exc:
            assert str(exc) == 'intentional regression-test rollback'
        else:
            raise AssertionError('ingestion error was suppressed')
        assert observer.execute('SELECT 1 FROM posts WHERE post_id=12001').fetchone() is None
        assert observer.execute('SELECT COUNT(*) FROM raw_records').fetchone()[0] == 0
        assert task_queue.owned(observer, rollback_task['task_key'], rollback_task['lease_token'], now=101)
        assert task_queue.complete(owner, rollback_task['task_key'], rollback_task['lease_token'],
            ingest_callback=lambda conn: save_posts(conn, [{'post_id': 12001, 'body': 'committed'}], 'locking-fixture'), now=101)
        assert observer.execute('SELECT body FROM posts WHERE post_id=12001').fetchone()[0] == 'committed'
        assert observer.execute("SELECT state FROM collection_tasks WHERE task_key='rollback:one'").fetchone()[0] == 'done'
        checks['ingestion_and_task_completion_rollback_and_commit_together'] = True

        for index in range(4):
            seed(owner, 'concurrent:' + str(index), 'concurrent')
        barrier = threading.Barrier(4)
        claims, failures = [], []
        guard = threading.Lock()

        def concurrent_claim(index):
            try:
                with closing(connect(target)) as conn:
                    conn.execute("SET statement_timeout='2500ms'")
                    barrier.wait(timeout=5)
                    value = task_queue.claim(conn, 'concurrent-' + str(index), ['concurrent'], now=100)
                    with guard:
                        claims.append(value)
            except BaseException as exc:
                with guard:
                    failures.append(type(exc).__name__)

        workers = [threading.Thread(target=concurrent_claim, args=(index,)) for index in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
        assert not any(worker.is_alive() for worker in workers), 'concurrent claim did not finish'
        assert not failures, failures
        assert len(claims) == 4 and all(claims), claims
        assert len({task['task_key'] for task in claims}) == 4
        assert len({task['lease_token'] for task in claims}) == 4
        checks['concurrent_claims_have_distinct_tasks_and_tokens'] = True

        # Both callers prepared the same old cursor. The second must read the
        # committed checkpoint after waiting, rather than apply its stale page.
        plan = feed_tasks.prepare(observer, 'latest', {'page_limit': 2})
        observed_at = utc_now()
        first_payload = {'items': [{'post_id': 13001, 'body': 'first cursor page',
                                    'created_at': observed_at}], 'next_cursor': 'cursor-one'}
        second_payload = {'items': [{'post_id': 13002, 'body': 'stale cursor page',
                                     'created_at': observed_at}], 'next_cursor': 'cursor-two'}
        feed_tasks.apply(owner, plan, first_payload, observed_at, raw_source='fixture:same:first')

        def stale_page(conn):
            try:
                feed_tasks.apply(conn, plan, second_payload, observed_at, raw_source='fixture:same:second')
            except feed_tasks.StaleFeedPlan:
                return 'stale'
            return 'incorrectly applied'

        stale = start_transaction('same-checkpoint-page', stale_page)
        try:
            wait_blocked(observer, stale)
        finally:
            owner.commit()
        finish_transactions(stale)
        assert stale['result'] == 'stale', stale['result']
        cursor_state = json.loads(observer.execute('SELECT value FROM checkpoints WHERE key=?',
                                                   (plan['checkpoint_key'],)).fetchone()[0])
        assert cursor_state['pages_completed'] == 1 and cursor_state['items_seen'] == 1
        assert cursor_state['cursor'] == 'cursor-one'
        assert observer.execute('SELECT 1 FROM posts WHERE post_id=13002').fetchone() is None
        assert observer.execute("SELECT COUNT(*) FROM raw_records WHERE source='fixture:same:second'").fetchone()[0] == 0
        checks['same_checkpoint_stale_page_cannot_advance_or_ingest'] = True

        # Hold the first post's row so two feeds reach a real overlapping lock
        # wait. With per-page display-order locking, the reverse page would own
        # post 14002 and form a cycle when the first page resumes. A consistent
        # acquisition order lets both pages commit after this row is released.
        with owner:
            save_posts(owner, [{'post_id': 14001, 'body': 'existing first'},
                               {'post_id': 14002, 'body': 'existing second'}], 'locking-order')
        overlap_at = utc_now()
        row_locker = connection()
        row_locker.execute('BEGIN ISOLATION LEVEL READ COMMITTED')
        row_locker.execute('SELECT post_id FROM posts WHERE post_id=14001 FOR UPDATE').fetchone()
        history = feed_tasks.prepare(observer, 'history', {'page_limit': 2})
        trending = feed_tasks.prepare(observer, 'trending', {'page_limit': 2})
        assert history['checkpoint_key'] != trending['checkpoint_key']
        shared_posts = [{'post_id': 14001, 'body': 'overlap first', 'created_at': overlap_at},
                        {'post_id': 14002, 'body': 'overlap second', 'created_at': overlap_at}]
        forward = start_transaction('forward-feed', lambda conn: feed_tasks.apply(conn, history,
            {'items': shared_posts, 'next_cursor': 'history-next'}, overlap_at))
        try:
            wait_blocked(observer, forward)
            reverse = start_transaction('reverse-feed', lambda conn: feed_tasks.apply(conn, trending,
                {'items': list(reversed(shared_posts)), 'next_cursor': 'trending-next'}, overlap_at))
            wait_blocked(observer, reverse)
        finally:
            row_locker.rollback()
        finish_transactions(forward, reverse)
        assert forward['result']['posts'] == reverse['result']['posts'] == 2
        for feed_plan in (history, trending):
            state = json.loads(observer.execute('SELECT value FROM checkpoints WHERE key=?',
                                                (feed_plan['checkpoint_key'],)).fetchone()[0])
            assert state['pages_completed'] == 1 and state['items_seen'] == 2
        assert observer.execute("SELECT COUNT(*) FROM feed_observations WHERE post_id IN (14001,14002) AND source IN ('latest','trending')").fetchone()[0] == 4
        checks['different_feed_checkpoints_with_reverse_post_order_do_not_deadlock'] = True

        # The row lock forces overlapping read/modify/write calls. The second
        # counter turn must retain the field committed by the first turn.
        with owner:
            owner.execute('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)',
                          ('counter_scheduler', json.dumps({'single_next': 'tracked', 'window_next': 'windows'}), observed_at))
        fairness_locker = connection()
        fairness_locker.execute('BEGIN ISOLATION LEVEL READ COMMITTED')
        fairness_locker.execute("SELECT key FROM checkpoints WHERE key='counter_scheduler' FOR UPDATE").fetchone()
        single = start_transaction('single-counter-turn', lambda conn: record_counter_turn(conn,
            {'sampling_queue': 'tracked', 'single_slot': True}, observed_at))
        try:
            wait_blocked(observer, single)
            window = start_transaction('window-counter-turn', lambda conn: record_counter_turn(conn,
                {'sampling_queue': 'tracked', 'window_minutes': 30}, observed_at))
            wait_blocked(observer, window)
        finally:
            fairness_locker.rollback()
        finish_transactions(single, window)
        fairness = json.loads(observer.execute("SELECT value FROM checkpoints WHERE key='counter_scheduler'").fetchone()[0])
        assert fairness == {'single_next': 'new', 'window_next': 'new'}, fairness
        checks['concurrent_counter_fairness_updates_keep_both_fields'] = True

        # The fake transport observes the real database transaction at the next
        # network boundary; it does not perform any external request.
        class RefreshTransport:
            attempts = 0

            def get_json(self, service, path, *, entity_id=None):
                assert not owner.in_transaction, 'body refresh kept a transaction open before the next request'
                if self.attempts:
                    assert observer.execute('SELECT body FROM posts WHERE post_id=15001').fetchone()[0] == 'refreshed 15001'
                self.attempts += 1
                return {'post_id': entity_id, 'body': 'refreshed ' + str(entity_id)}

        refresh_transport = RefreshTransport()
        refresh_result = {'errors': [], 'accounts': 0}
        _refresh(owner, refresh_transport, _config({'counter_limit': 0, 'relation_limit': 0,
            'reply_parent_limit': 0, 'body_refresh_limit': 2, 'account_limit': 0}),
            refresh_result, set(), body_refresh=[15001, 15002])
        assert refresh_transport.attempts == 2 and not refresh_result['errors'], refresh_result
        assert not owner.in_transaction
        assert observer.execute('SELECT body FROM posts WHERE post_id=15002').fetchone()[0] == 'refreshed 15002'
        checks['body_refresh_commits_before_next_network_boundary'] = True
        return {'checked_at': utc_now(), 'checks': checks, 'timings_ms': timings,
                'isolation': 'random temporary database; no production table writes'}
    finally:
        for conn in connections:
            conn.close()
        for thread in background:
            thread.join(timeout=6)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-file', type=Path, default=Path('data/secrets/postgres_url'))
    parser.add_argument('--report', type=Path, default=Path('reports/generated/worker-locking-postgres.json'))
    args = parser.parse_args()
    from ctn_data.secret_files import read_secret
    source = read_secret(args.source_file).strip()
    parts = urlsplit(source)
    if parts.scheme not in ('postgres', 'postgresql') or not parts.hostname:
        raise ValueError('PostgreSQL source must be a connection URI')
    name = 'ctn_worker_lock_test_' + uuid.uuid4().hex
    target = urlunsplit(parts._replace(path='/' + name))
    if target == source or not name.startswith('ctn_worker_lock_test_'):
        raise AssertionError('temporary database isolation check failed')
    admin = psycopg.connect(source, autocommit=True)
    created = False
    try:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        created = True
        result = run(target)
        result['temporary_database'] = name
    finally:
        try:
            if created:
                admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        finally:
            admin.close()
    result['temporary_database_removed'] = True
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
