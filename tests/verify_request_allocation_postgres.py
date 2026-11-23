"""Opt-in dynamic allocation checks in a disposable PostgreSQL database.

Run ``python -m tests.verify_request_allocation_postgres`` from the repository
root. Only the randomly named test database receives application writes.
Credentials are loaded from the existing secret file and never reported.
HTTP 429 is simulated locally; no external collection requests are made.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import io
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit, urlunsplit
import urllib.error
import uuid

import psycopg
from psycopg import sql

from ctn_data import coordination
from ctn_data.db import connect, initialize, utc_now
from ctn_data.transport import ReadOnlyTransport, RequestDeferred


PREFIX = 'ctn_request_allocation_test_'


def run(target, database_name, report):
    # The target has already been checked through a separate raw connection.
    initialize(target)
    checks = report['checks']
    timings = report['timings_ms']

    def connection():
        conn = connect(target)
        try:
            assert conn.execute('SELECT current_database()').fetchone()[0] == database_name, 'unexpected database'
            conn.execute("SET statement_timeout='5000ms'")
            conn.execute("SET lock_timeout='4000ms'")
            return conn
        except BaseException:
            conn.close()
            raise

    def state(conn):
        row = conn.execute("SELECT value FROM checkpoints WHERE key='coordination:counters'").fetchone()
        return json.loads(row[0]) if row else {}

    def leases(conn):
        return {row[0]: json.loads(row[1]) for row in conn.execute(
            "SELECT key,value FROM checkpoints WHERE key LIKE 'coordination:post:%'")}

    def reset(conn):
        # Only this verified, disposable database is reachable here.
        with conn:
            conn.execute("DELETE FROM checkpoints WHERE key LIKE 'coordination:%' OR key LIKE 'throttle:%'")

    def concurrent_permits(requests, now):
        barrier = threading.Barrier(len(requests))

        def request(pair):
            actor, path = pair
            with closing(connection()) as conn:
                barrier.wait(timeout=5)
                return actor, path, coordination.permit(conn, path, actor, now)

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=len(requests)) as pool:
            result = list(pool.map(request, requests))
        timings['concurrent_' + str(int(now))] = round((time.monotonic()-started)*1000, 3)
        return result

    with closing(connection()) as owner, closing(connection()) as observer:
        # Different posts avoid confusing object de-duplication with the shared
        # endpoint budget. Simultaneous connections must still grant once.
        requests = [('simultaneous-' + str(i), '/api/posts/' + str(41001+i) + '/counters') for i in range(4)]
        results = concurrent_permits(requests, 1000)
        granted = [(actor, path) for actor, path, result in results if result['allowed']]
        denied = [(actor, path) for actor, path, result in results if not result['allowed']]
        assert len(granted) == 1 and len(denied) == 3, 'shared slot granted more than once'
        snapshot = state(observer)
        assert snapshot['next_at'] == 1000 + coordination.INTERVALS['counters'], 'incorrect shared pacing'
        assert sum(value for key, value in snapshot.items() if key.endswith('_grants')) == 1
        assert {row['actor'] for row in snapshot['allocation']['waiting']} == {actor for actor, _ in denied}, 'concurrent demand lost'
        assert len(leases(observer)) == 1, 'denied requests created object leases'
        checks['different_posts_concurrent_permits_grant_one_slot'] = True
        checks['concurrent_denials_keep_all_waiters_and_create_no_leases'] = True

        reset(owner)
        assert coordination.permit(owner, '/api/posts/42000/counters', 'seed', 2000)['allowed']
        requests = [('queued-' + str(i), '/api/posts/' + str(42001+i) + '/counters') for i in range(4)]
        before = state(observer)
        lease_before = leases(observer)
        results = concurrent_permits(requests, 2000.01)
        assert all(not result['allowed'] for _, _, result in results)
        snapshot = state(observer)
        waiting = snapshot['allocation']['waiting']
        assert len(waiting) == len(requests) and {row['actor'] for row in waiting} == {actor for actor, _ in requests}, 'queue update lost'
        assert snapshot['next_at'] == before['next_at'], 'denials advanced next_at'
        assert leases(observer) == lease_before, 'denials changed object leases'
        checks['concurrent_read_modify_write_preserves_all_queue_entries'] = True
        checks['unsuccessful_polls_do_not_advance_budget_or_object_leases'] = True

        # All submitting connections are closed above. A faster route may use
        # two idle slots, then must hand over to the persisted waiting route.
        fifo = [(row['actor'], row['path']) for row in waiting]
        with closing(connection()) as reconnect:
            for index in range(2):
                borrowed = coordination.permit(reconnect, '/api/posts/' + str(42991+index) + '/counters', 'fast-local', 2000.301+index*.301)
                assert borrowed['allowed'], 'ready route could not borrow idle capacity'
            before = state(observer)
            lease_before = leases(observer)
            rejected = coordination.permit(reconnect, '/api/posts/42999/counters', 'fast-local', 2000.903)
            assert not rejected['allowed'] and rejected['reason'] == 'allocation', 'fast route bypassed waiting workers without a bound'
            assert state(observer)['next_at'] == before['next_at']
            assert leases(observer) == lease_before
            assert [(row['actor'], row['path']) for row in state(observer)['allocation']['waiting'][:len(fifo)]] == fifo
            coordination.cancel_wait(reconnect, '/api/posts/42999/counters', 'fast-local')
            for index, (actor, path) in enumerate(fifo):
                now = 2000.904 + index*.301
                assert coordination.permit(reconnect, path, actor, now)['allowed'], 'reconnected waiting worker was not admitted'
                if index+1 < len(fifo):
                    next_actor, next_path = fifo[index+1]
                    prior = state(observer)['next_at']
                    prior_leases = leases(observer)
                    blocked = coordination.permit(reconnect, next_path, next_actor, now)
                    assert not blocked['allowed'] and blocked['reason'] == 'pacing'
                    assert state(observer)['next_at'] == prior
                    assert leases(observer) == prior_leases
            assert not state(observer)['allocation']['waiting'], 'admitted waiters remained queued'
        checks['reconnect_preserves_waiters_after_bounded_borrowing'] = True
        checks['ready_route_can_borrow_two_idle_slots_then_must_handover'] = True

        reset(owner)
        assert coordination.permit(owner, '/api/posts/43000/counters', 'seed', 3000)['allowed']
        for index, actor in enumerate(('cancel-first', 'keep-second', 'keep-third')):
            assert not coordination.permit(owner, '/api/posts/' + str(43001+index) + '/counters', actor, 3000.01)['allowed']
        original = state(observer)
        coordination.cancel_wait(owner, '/api/posts/43999/counters', 'cancel-first')
        assert state(observer) == original, 'cancellation removed a different request'
        coordination.cancel_wait(owner, '/api/posts/43001/counters', 'cancel-first')
        coordination.cancel_wait(owner, '/api/posts/43001/counters', 'cancel-first')
        snapshot = state(observer)
        assert [row['actor'] for row in snapshot['allocation']['waiting']] == ['keep-second', 'keep-third']
        assert snapshot['next_at'] == original['next_at']
        assert coordination.permit(owner, '/api/posts/43002/counters', 'keep-second', 3000.301)['allowed']
        checks['cancel_is_idempotent_and_preserves_other_requests'] = True
        checks['cancelled_head_does_not_delay_next_ready_worker'] = True

        reset(owner)
        assert coordination.permit(owner, '/api/posts/43500/counters', 'seed', 3500)['allowed']
        first = coordination.permit(owner, '/api/posts/43501/counters', 'late-route', 3500.01)
        assert not first['allowed'] and first['reason'] == 'pacing'
        assert coordination.permit(owner, '/api/posts/43501/counters', 'late-route', 3500.401)['allowed']
        before = state(observer)
        lease_before = leases(observer)
        compensated = coordination.permit(owner, '/api/posts/43502/counters', 'late-route', 3500.411)
        assert not compensated['allowed'] and compensated['reason'] == 'pacing'
        assert compensated['retry_after_ms'] < int((before['next_at']-3500.411)*1000), 'late polling was not compensated'
        assert state(observer)['next_at'] == before['next_at']
        assert leases(observer) == lease_before
        checks['measured_poll_lag_shortens_wakeup_without_early_admission'] = True

        reset(owner)

        class Clock:
            now = 0.0

            def __call__(self):
                return self.now

            def sleep(self, seconds):
                self.now += seconds

        class RateLimitedOpener:
            calls = 0

            def open(self, request, timeout):
                self.calls += 1
                raise urllib.error.HTTPError(request.full_url, 429, 'fixture rate limit',
                    {'Retry-After': '60'}, io.BytesIO())

        clock, opener = Clock(), RateLimitedOpener()
        transport = ReadOnlyTransport(owner, opener=opener, clock=clock, sleeper=clock.sleep, actor='rate-limited')
        transport.epoch_base = 4000
        try:
            transport.get_json('social', '/api/posts/44001/counters')
        except RequestDeferred as exc:
            assert exc.status == 429
        else:
            raise AssertionError('HTTP 429 was not deferred')
        assert opener.calls == 1, 'HTTP 429 triggered another network request'
        before = state(observer)
        lease_before = leases(observer)
        with closing(connection()) as reconnect:
            blocked = coordination.permit(reconnect, '/api/posts/44002/counters', 'other-route', 4001)
            assert not blocked['allowed'] and blocked['reason'] == 'cooldown'
            assert blocked['retry_after_ms'] >= 59000
            assert not state(observer)['allocation']['waiting'], 'cooldown retained stale demand'
            assert state(observer)['next_at'] == before['next_at']
            assert leases(observer) == lease_before
            coordination.extend_cooldown(reconnect, 'counters', 4010)
            assert coordination.cooldowns(observer)['counters'] == 4060000, 'shorter cooldown overwrote HTTP 429'
            assert coordination.permit(reconnect, '/api/posts/44003/counters', 'recovered', 4060.001)['allowed']
            assert not coordination.permit(reconnect, '/api/posts/44004/counters', 'another-route', 4060.001)['allowed'], 'cooldown recovery caused a burst'
        checks['simulated_429_makes_one_request_and_persists_shared_cooldown'] = True
        checks['cooldown_survives_reconnect_never_shortens_and_recovers_without_burst'] = True
        assert observer.execute('SELECT COUNT(*) FROM posts').fetchone()[0] == 0
        report['fixture_http_calls'] = opener.calls
        report['external_http_calls'] = 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-file', type=Path, default=Path('data/secrets/postgres_url'))
    parser.add_argument('--report', type=Path, default=Path('reports/generated/request-allocation-postgres-verification.json'))
    args = parser.parse_args()
    report = {'checked_at': utc_now(), 'status': 'failed', 'checks': {}, 'timings_ms': {},
              'isolation': 'random temporary database; no production table writes',
              'temporary_database_removed': False}
    admin, created, failure = None, False, None
    name = PREFIX + uuid.uuid4().hex
    try:
        from ctn_data.secret_files import read_secret
        source = read_secret(args.source_file).strip()
        parts = urlsplit(source)
        if parts.scheme not in ('postgres', 'postgresql') or not parts.hostname:
            raise ValueError('PostgreSQL source must be a connection URI')
        target = urlunsplit(parts._replace(path='/' + name))
        if target == source or not re.fullmatch(PREFIX + r'[0-9a-f]{32}', name):
            raise AssertionError('temporary database isolation check failed')
        admin = psycopg.connect(source, autocommit=True, connect_timeout=10)
        source_database = admin.execute('SELECT current_database()').fetchone()[0]
        assert source_database != name, 'temporary database matches source'
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        created = True
        with psycopg.connect(target, autocommit=True, connect_timeout=10) as isolated:
            actual = isolated.execute('SELECT current_database()').fetchone()[0]
            assert actual == name and actual != source_database, 'target did not resolve to the temporary database'
        report['checks']['target_database_verified_distinct_from_source'] = True
        report['temporary_database'] = name
        run(target, name, report)
        report['status'] = 'passed'
    except BaseException as exc:
        failure = exc
        # Connection errors can contain source details. Report only their type;
        # assertion messages here are fixture text and contain no credentials.
        report['error_type'] = type(exc).__name__
        if isinstance(exc, AssertionError):
            report['error'] = str(exc)
    finally:
        try:
            if created:
                assert re.fullmatch(PREFIX + r'[0-9a-f]{32}', name), 'refusing unexpected cleanup target'
                admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
                report['temporary_database_removed'] = True
        except BaseException as exc:
            report['status'] = 'failed'
            report['cleanup_error_type'] = type(exc).__name__
            failure = failure or exc
        finally:
            if admin is not None:
                admin.close()
        report['finished_at'] = utc_now()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))
    if failure is not None:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
