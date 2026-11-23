"""Collection reconnect and browser-receipt index checks in a random disposable PostgreSQL database only.

Every session of the disposable database is terminated mid-run, which is what
a PostgreSQL restart does to the backend's connections.
"""
from contextlib import closing
import inspect
import json
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit, urlunsplit
import uuid

import psycopg
from psycopg import sql
from unittest.mock import patch

from ctn_data import browser_status, task_queue
from ctn_data.db import connect, initialize
from ctn_data.scheduler import CollectionScheduler

INDEX = 'idx_checkpoints_browser_receipts'
# Kept identical to server.extension_status(); asserted below.
OBSERVATIONS_SQL = ("SELECT json_extract(value,'$.kind') AS kind,count(*) AS count,max(updated_at) AS last_at "
                    "FROM checkpoints WHERE key LIKE 'browser_receipt:%' GROUP BY kind")
PORTAL_SQL = inspect.getsource(browser_status.latest_portal).split('"""')[1]


def terminate_sessions(admin, name):
    return admin.execute('SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity '
                         'WHERE datname=%s AND pid<>pg_backend_pid()', (name,)).fetchone()[0]


def lane_survives_restart(target, admin, name):
    scheduler = CollectionScheduler(target, {}, True)
    with closing(connect(target)) as conn:
        task_queue.ensure_schema(conn)
        scheduler._seed(conn)
    kinds, killed, resumed = [], [], threading.Event()
    def stage(db_path, config, kind):
        kinds.append(kind)
        if len(kinds) == 1:
            killed.append(terminate_sessions(admin, name))
        else:
            resumed.set()
            scheduler.stopping = True
        return {'status': 'completed', 'errors': [], 'requests': 0}
    errors = []
    with patch('ctn_data.scheduler.collect_stage', stage):
        thread = threading.Thread(target=scheduler._worker_loop, args=('auxiliary', ('auxiliary', 'social_context')))
        thread.start()
        try:
            while not resumed.wait(.1) and thread.is_alive() and len(errors) < 300:
                errors.append(dict(scheduler.lane_errors))
        finally:
            scheduler.stopping = True
            thread.join(10)
    assert resumed.is_set(), (kinds, scheduler.lane_errors)
    assert killed and killed[0] >= 1, killed
    return {'sessions_terminated': killed[0], 'kinds': kinds,
            'errors_seen': sorted({value for seen in errors for value in seen.values()})}


def planner_survives_restart(target, admin, name):
    scheduler = CollectionScheduler(target, {}, True)
    scheduler.PLAN_SECONDS = 0
    plans, replanned = [], threading.Event()
    def plan(conn, config):
        plans.append(1)
        if len(plans) == 1:
            terminate_sessions(admin, name)
        else:
            conn.execute('SELECT 1').fetchone()  # Fails forever on the dead session.
            replanned.set()
        return 0
    with patch('ctn_data.scheduler.plan_counter_tasks', plan), \
            patch.object(CollectionScheduler, '_worker_loop', lambda self, lane, kinds: None):
        scheduler.start()
        try:
            assert replanned.wait(15), (len(plans), scheduler.lane_errors)
        finally:
            scheduler.stop()
    return {'plans': len(plans)}


def receipts_use_index(target):
    assert OBSERVATIONS_SQL in Path('ctn_data/server.py').read_text(encoding='utf-8')
    with closing(connect(target)) as conn:
        with conn:
            conn.execute('BEGIN')
            conn.execute("INSERT INTO checkpoints(key,value,updated_at) SELECT 'counter_attempt:'||g,"
                         "'{\"status\":\"saved\",\"http_status\":200}','2026-10-20T00:00:00.000000Z' "
                         'FROM generate_series(1,60000) g')
            conn.execute("INSERT INTO checkpoints(key,value,updated_at) SELECT 'browser_receipt:'||g,"
                         "json_build_object('kind',CASE WHEN mod(g,3)=0 THEN 'portal' ELSE 'counters' END,"
                         "'observed_at',to_char(timestamp '2026-10-19 00:00' + g * interval '1 minute',"
                         "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'))::text,"
                         "to_char(timestamp '2026-10-19 00:00' + g * interval '1 minute','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') "
                         'FROM generate_series(1,3000) g')
        conn.execute('ANALYZE checkpoints')
        result = {}
        for label, statement in (('observations', OBSERVATIONS_SQL), ('latest_portal', PORTAL_SQL)):
            plan = json.dumps(conn.execute('EXPLAIN (FORMAT JSON) ' + statement).fetchone()[0])
            assert INDEX in plan, (label, plan)
            with conn:
                conn.execute('BEGIN')
                fast = [tuple(row) for row in conn.execute(statement)]
            with conn:
                conn.execute('BEGIN')
                for name in ('enable_indexscan', 'enable_bitmapscan', 'enable_indexonlyscan'):
                    conn.execute(f"SELECT set_config('{name}','off',true)")
                slow = [tuple(row) for row in conn.execute(statement)]
            assert sorted(fast) == sorted(slow), label
            result[label] = len(fast)
        portal, _ = browser_status.latest_portal(conn)
        # The newest portal receipt is g=3000: 3000 minutes after 2026-10-19 00:00.
        assert portal['kind'] == 'portal' and portal['observed_at'].startswith('2026-10-21T02:00'), portal
    return result


def run(target, admin, name):
    initialize(target)
    checks = {}
    checks['lane'] = lane_survives_restart(target, admin, name)
    checks['planner'] = planner_survives_restart(target, admin, name)
    checks['browser_receipts_use_partial_index'] = receipts_use_index(target)
    return checks


def main():
    from ctn_data.secret_files import read_secret
    source = read_secret(Path('data/secrets/postgres_url')).strip()
    parts = urlsplit(source)
    name = 'ctn_reconnect_test_' + uuid.uuid4().hex
    assert re.fullmatch(r'ctn_reconnect_test_[0-9a-f]{32}', name)
    target = urlunsplit(parts._replace(path='/' + name))
    assert target != source
    admin = psycopg.connect(source, autocommit=True, connect_timeout=10)
    created = False
    try:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        created = True
        result = run(target, admin, name)
    finally:
        try:
            if created:
                admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        finally:
            admin.close()
    result['temporary_database_removed'] = True
    destination = Path('reports/generated/collection-reconnect-postgres.json')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
