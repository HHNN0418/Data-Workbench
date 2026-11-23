"""Opt-in agent competition smoke checks in a disposable PostgreSQL database.

Run ``python -m tests.verify_agent_competition_postgres`` from the repository.
Only a verified random test database receives application writes. The existing
connection secret is read without reporting it; no model or HTTP calls occur.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

import psycopg
from psycopg import sql

from ctn_data import agent_competition as competition
from ctn_data.agent_control import AgentStore
from ctn_data.db import connect, initialize, utc_now


PREFIX = 'ctn_agent_competition_test_'
NOW = competition._epoch('2026-10-20T01:00:00Z')


def actor(aid, account):
    return {'id': aid, 'name': aid, 'account_id': account, 'account_state': 'ready',
            'enabled': True, 'mode': 'auto', 'allowed_actions': ['post', 'reply'],
            'provider_id': 'fixture-standard'}


def run(target, database_name, report):
    initialize(target)
    checks = report['checks']
    stores = [AgentStore(target), AgentStore(target)]
    backend_pids = set()

    def checked(conn):
        assert conn.execute('SELECT current_database()').fetchone()[0] == database_name, 'unexpected application database'
        conn.execute("SET LOCAL statement_timeout='5000ms'")
        conn.execute("SET LOCAL lock_timeout='4000ms'")
        backend_pids.add(conn.execute('SELECT pg_backend_pid()').fetchone()[0])

    def change(store, callback):
        def isolated(data, conn):
            checked(conn)
            return callback(data, conn)
        return store.change(isolated)

    def seed(data, conn):
        data['settings']['enabled'] = True
        data['agents'].update({'a': actor('a', 11), 'b': actor('b', 22)})
        competition.sync(conn, data, NOW - 7200, ready_ids=['a', 'b'])
        return competition.snapshot(conn, data, NOW)

    initial = change(stores[0], seed)
    assert initial['settings']['mode'] == 'off' and initial['revision'] == 0, 'initial competition settings changed'
    checks['real_agent_store_defaults_off_and_sync_persists'] = True

    barrier = threading.Barrier(2)

    def configure(pair):
        store, mode = pair
        barrier.wait(timeout=5)
        try:
            def update(data, conn):
                data['rate']['cas_attempt'] = mode
                return competition.configure(conn, data, {'expected_revision': 0, 'mode': mode}, NOW)
            result = change(store, update)
            return {'status': 'saved', 'mode': mode, 'revision': result['revision']}
        except competition.CompetitionError as exc:
            assert exc.status == 409, 'unexpected configuration error'
            return {'status': 'conflict', 'mode': mode}

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(configure, zip(stores, ('observe', 'resources'))))
    winners = [row for row in outcomes if row['status'] == 'saved']
    assert len(winners) == 1 and sum(row['status'] == 'conflict' for row in outcomes) == 1, 'concurrent CAS admitted multiple updates'
    persisted = stores[1].read()
    assert persisted['rate']['competition']['revision'] == 1, 'CAS revision was not persisted'
    assert persisted['rate']['cas_attempt'] == winners[0]['mode'], 'rejected transaction overwrote state'
    checks['concurrent_configuration_cas_commits_one_revision'] = True

    def rejected_transaction(data, conn):
        conn.execute('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)',
                     ('competition-smoke-rollback', '{}', competition._iso(NOW)))
        return competition.configure(conn, data, {'expected_revision': 0, 'mode': 'off'}, NOW)
    try:
        change(stores[0], rejected_transaction)
    except competition.CompetitionError as exc:
        assert exc.status == 409, 'stale configuration returned wrong status'
    else:
        raise AssertionError('stale configuration was accepted')
    with closing(connect(target)) as observer:
        assert observer.execute('SELECT current_database()').fetchone()[0] == database_name
        assert observer.execute('SELECT COUNT(*) FROM checkpoints WHERE key=?',
                                ('competition-smoke-rollback',)).fetchone()[0] == 0, 'CAS failure did not roll back checkpoint write'
    checks['failed_cas_rolls_back_checkpoint_and_agent_state'] = True
    change(stores[0], lambda data, conn: competition.configure(
        conn, data, {'expected_revision': 1, 'mode': 'resources'}, NOW))

    def seed_publications(data, conn):
        def counter(pid, at, views=0, likes=0):
            conn.execute('INSERT INTO counter_snapshots(post_id,observed_at,view_count,like_count,reply_count,repost_count) '
                         'VALUES(?,?,?,?,?,?)', (pid, competition._iso(at), views, likes, 0, 0))

        for pid, account in ((1, 11), (2, 22), (3, 11), (4, 11), (5, 11), (6, 22), (7, 22)):
            stamp = competition._iso(NOW - 200)
            conn.execute('INSERT INTO posts(post_id,author_id,kind,body,visibility,created_at,first_seen,last_seen) '
                         'VALUES(?,?,?,?,?,?,?,?)', (pid, account, 'post', 'Isolated fixture', 'public', stamp, stamp, stamp))
            counter(pid, NOW - 100)
            if pid == 3:
                counter(pid, NOW - 50, views=490)  # Manual own post consumes the same team cap.
                continue
            aid = 'a' if account == 11 else 'b'
            draft = {'id': 'fixture-draft-' + str(pid), 'post_id': pid, 'agent_id': aid,
                     'account_id': account, 'state': 'published', 'action': 'post',
                     'published_at': competition._iso(NOW), 'competition_automatic': True,
                     'origin': 'automatic', 'actual_provider_id': 'fixture-standard'}
            assert competition.record_published(conn, data, draft, NOW), 'publication checkpoint was not inserted'
            assert not competition.record_published(conn, data, draft, NOW), 'publication checkpoint was duplicated'
            counter(pid, NOW)
            counter(pid, NOW + 3600, views={1: 12, 2: 8}.get(pid, 0),
                    likes=2 if pid in (4, 5) else 1 if pid in (6, 7) else 0)
        receipts = competition._publications(conn)
        assert len(receipts) == 6 and all(row['origin'] == 'automatic' for row in receipts), 'publication receipts were not persisted'

    change(stores[0], seed_publications)
    view = change(stores[1], lambda data, conn: competition.refresh(conn, data, NOW + 7800, force=True))
    posts = {row['post_id']: row for row in view['posts']}
    assert (posts[1]['score'], posts[2]['score']) == (6, 4), 'shared daily cap allocation differs on PostgreSQL'
    ledger = view['daily_views'][-1]
    assert ledger['credited_views'] == 500 and ledger['observed_views'] == 510, 'team daily ledger cap was not applied'
    results = {row['agent_id']: row for row in view['rounds'][0]['results']}
    assert results['a']['qualified'] and results['b']['qualified'], 'complete fixture posts did not qualify'
    assert (results['a']['score'], results['b']['score']) == (26, 14), 'official-weight hourly scores differ'
    allocated = {row['agent_id']: row['share'] for row in view['agents']}
    assert abs(allocated['a'] - .6) < 1e-8 and abs(allocated['b'] - .4) < 1e-8, 'hourly allocation was not persisted'
    checks['publication_checkpoints_are_atomic_and_idempotent'] = True
    checks['postgres_refresh_replays_team_500_view_cap_and_official_weights'] = True
    checks['qualified_hourly_scores_produce_bounded_resource_shares'] = True

    barrier = threading.Barrier(2)

    def concurrent_grant(store):
        barrier.wait(timeout=5)
        return change(store, lambda data, conn: competition.admit(
            conn, data, 'a', 'publish', 'same-delivery', NOW + 7801, contenders=['a', 'b']))

    with ThreadPoolExecutor(max_workers=2) as pool:
        grants = list(pool.map(concurrent_grant, stores))
    assert all(row['allowed'] for row in grants), 'replayed grant was rejected'
    assert sum(bool(row.get('replayed')) for row in grants) == 1, 'concurrent retry charged more than one grant'
    fair = stores[1].read()['rate']['competition']['fair']['publish']
    assert len(fair['grants']) == 1 and abs(fair['served']['a'] - 1 / allocated['a']) < 1e-8, 'grant consumption was not atomic'
    checks['cross_connection_same_request_grant_is_charged_once'] = True

    totals = {'a': 0, 'b': 0}
    for index in range(40):
        def grant(data, conn):
            at = NOW + 7810 + index / 10
            ordered = competition.order(conn, data, ['a', 'b'], 'generate', at)
            chosen = ordered[0]
            permit = competition.admit(conn, data, chosen, 'generate', 'work-' + str(index), at)
            assert permit['allowed'], 'weighted winner was denied'
            return chosen
        totals[change(stores[index % 2], grant)] += 1
    assert abs(totals['a'] - 24) <= 1 and abs(totals['b'] - 16) <= 1, 'cross-connection weighted service drifted'
    report['weighted_grants'] = totals
    checks['weighted_fair_grants_persist_across_independent_connections'] = True

    def add_newcomer(data, conn):
        data['agents']['c'] = actor('c', 33)
        competition.sync(conn, data, NOW + 7900, ready_ids=['a', 'b', 'c'])
        return competition.snapshot(conn, data, NOW + 7900)

    before = change(stores[0], add_newcomer)
    before_rows = {row['agent_id']: row for row in before['agents']}
    restarted = AgentStore(target)

    def resumed(data, conn):
        competition.sync(conn, data, NOW + 8200, ready_ids=['a', 'b', 'c'])
        return competition.snapshot(conn, data, NOW + 8200)

    after = change(restarted, resumed)
    after_rows = {row['agent_id']: row for row in after['agents']}
    assert after_rows['c']['first_ready_at'] == NOW + 7900, 'restart reset first ready time'
    assert after_rows['c']['protected_until'] == NOW + 11500, 'restart extended newcomer protection'
    assert after_rows['c']['protection_remaining_seconds'] == 3300, 'protection countdown did not survive restart'
    assert all(abs(before_rows[aid]['share'] - after_rows[aid]['share']) < 1e-8 for aid in before_rows), 'restart changed resource shares'
    replay = change(restarted, lambda data, conn: competition.admit(
        conn, data, 'a', 'publish', 'same-delivery', NOW + 8201))
    assert replay['allowed'] and replay.get('replayed'), 'restart lost grant idempotency'
    checks['restart_preserves_newcomer_protection_and_allocated_shares'] = True
    checks['restart_preserves_grant_idempotency'] = True
    report['application_connection_count'] = len(backend_pids)
    report['external_http_calls'] = 0
    report['model_calls'] = 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-file', type=Path, default=Path('data/secrets/postgres_url'))
    parser.add_argument('--report', type=Path, default=Path('reports/generated/agent-competition-postgres-verification.json'))
    args = parser.parse_args()
    report = {'checked_at': utc_now(), 'status': 'failed', 'checks': {},
              'isolation': 'random temporary database; no production table writes',
              'temporary_database_removed': False}
    admin, created, failure = None, False, None
    name = PREFIX + uuid.uuid4().hex
    started = time.monotonic()
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
        report['elapsed_ms'] = round((time.monotonic() - started) * 1000, 3)
        report['finished_at'] = utc_now()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))
    if failure is not None:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
