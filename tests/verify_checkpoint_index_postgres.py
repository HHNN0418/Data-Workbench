"""Partial checkpoint time index checks in a random disposable PostgreSQL database only."""
from contextlib import closing
import inspect
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

import psycopg
from psycopg import sql
from unittest.mock import patch

from ctn_data import agent_competition, retention
from ctn_data.db import connect, initialize
from ctn_data.postgres import translate_sql
from ctn_data.retention import prune_history

NOW = agent_competition._epoch('2026-10-20T01:00:00Z')
INDEX = 'idx_checkpoints_retention_utc'
# Kept identical to agent_competition.refresh(); asserted below.
RECEIPTS_SQL = ("SELECT value FROM checkpoints WHERE key LIKE 'agent_tracking:action:%' "
                'AND ctn_timestamp(updated_at)>=ctn_timestamp(?)')
STAMP = """to_char(timestamp '{}' + g * interval '{}', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')"""


class Recording:
    """Delegates to a real connection and keeps every checkpoint query."""
    def __init__(self, conn, seen):
        self._conn, self._seen = conn, seen

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def execute(self, statement, params=()):
        if 'FROM checkpoints' in statement:
            self._seen.append((statement, params))
        return self._conn.execute(statement, params)


def plan(conn, statement, params):
    return json.dumps(conn.execute('EXPLAIN (FORMAT JSON) ' + statement, params).fetchone()[0])


def generic_plan(conn, statement):
    """The plan a server-side prepared statement may switch to after five runs."""
    numbered = iter(range(1, 100))
    text = re.sub(r'%s', lambda _: f'${next(numbered)}', translate_sql(statement)).replace('%%', '%')
    return json.dumps(conn.raw.execute('EXPLAIN (GENERIC_PLAN, FORMAT JSON) ' + text).fetchone()[0])


def rows(conn, statement, params, *, indexes):
    with conn:
        conn.execute('BEGIN')
        if not indexes:
            for name in ('enable_indexscan', 'enable_bitmapscan', 'enable_indexonlyscan'):
                conn.execute(f"SELECT set_config('{name}','off',true)")
        return [tuple(row) for row in conn.execute(statement, params)]


def timed(conn, statement, params, repeat=5):
    best = None
    for _ in range(repeat):
        started = time.perf_counter()
        conn.execute(statement, params).fetchall()
        elapsed = (time.perf_counter() - started) * 1000
        best = elapsed if best is None else min(best, elapsed)
    return round(best, 2)


def run(target):
    checks, measures = {}, {}
    assert RECEIPTS_SQL.split(' AND ')[0] in inspect.getsource(agent_competition.refresh)
    initialize(target)

    # Simulate the live upgrade: old full index, stale fingerprint, then startup DDL.
    with closing(connect(target)) as conn, conn:
        conn.execute(f'DROP INDEX {INDEX}')
        conn.execute('CREATE INDEX idx_checkpoints_updated_utc ON checkpoints(ctn_timestamp(updated_at),key)')
        conn.execute("UPDATE meta SET value='stale' WHERE key='schema_fingerprint'")
    initialize(target, force=True)
    with closing(connect(target)) as conn:
        names = {row[0]: row[1] for row in conn.execute(
            "SELECT indexname,indexdef FROM pg_indexes WHERE tablename='checkpoints'")}
    assert 'idx_checkpoints_updated_utc' not in names, names
    assert 'WHERE' in names[INDEX] and 'counter_attempt' not in names[INDEX], names[INDEX]
    checks['upgrade_drops_full_index_and_creates_partial'] = True

    with closing(connect(target)) as conn, conn:
        conn.execute('BEGIN')
        conn.execute("INSERT INTO checkpoints(key,value,updated_at) SELECT 'counter_attempt:'||g,"
                     "'{\"status\":\"saved\",\"http_status\":200}'," + STAMP.format('2026-10-18 00:00', '1 second') +
                     ' FROM generate_series(1,60000) g')
        conn.execute("INSERT INTO checkpoints(key,value,updated_at) SELECT 'worker_result:'||g,'{}',"
                     + STAMP.format('2026-10-18 12:00', '6 seconds') + ' FROM generate_series(1,20000) g')
        conn.execute("INSERT INTO checkpoints(key,value,updated_at) SELECT 'coordination:post:'||g,"
                     "CASE WHEN mod(g,2)=0 THEN '{\"until\":1}' ELSE '{\"until\":" + str(NOW + 1000) + "}' END,"
                     + STAMP.format('2026-10-18 12:00', '6 seconds') + ' FROM generate_series(1,20000) g')
        conn.execute("INSERT INTO checkpoints(key,value,updated_at) SELECT 'agent_tracking:action:'||g,"
                     "'{\"action\":\"post\"}'," + STAMP.format('2026-10-15 00:00', '7 minutes') +
                     ' FROM generate_series(1,1000) g')
        conn.execute("INSERT INTO checkpoints(key,value,updated_at) SELECT 'browser_receipt:'||g,'{}',"
                     + STAMP.format('2026-10-01 00:00', '1 minute') + ' FROM generate_series(1,2000) g')
    with closing(connect(target)) as conn:
        conn.execute('ANALYZE checkpoints')
        total = conn.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0]
        partial_bytes = conn.execute('SELECT pg_relation_size(?::regclass)', (INDEX,)).fetchone()[0]
        with conn:
            conn.execute('CREATE INDEX tmp_full_checkpoints ON checkpoints(ctn_timestamp(updated_at),key)')
            full_bytes = conn.execute("SELECT pg_relation_size('tmp_full_checkpoints'::regclass)").fetchone()[0]
            conn.execute('DROP INDEX tmp_full_checkpoints')
    measures.update(rows=total, partial_index_kb=partial_bytes // 1024, full_index_kb=full_bytes // 1024)
    assert partial_bytes < full_bytes * 0.6, measures

    # The retention pass, exactly as the worker issues it.
    seen = []
    real_connect = retention.connect
    with patch.object(retention, 'connect', lambda path: Recording(real_connect(path), seen)):
        preview = prune_history(target, now=NOW)
    assert preview['counts']['checkpoints'] == 1000, preview
    assert len(seen) == 1, seen
    retention_sql, retention_params = seen[0]
    with closing(connect(target)) as conn:
        retention_plan = plan(conn, retention_sql, retention_params)
        receipts_params = (agent_competition._iso(NOW - 72 * 3600),)
        receipts_plan = plan(conn, RECEIPTS_SQL, receipts_params)
        assert INDEX in retention_plan, retention_plan
        assert INDEX in receipts_plan, receipts_plan
        checks['retention_and_competition_queries_use_partial_index'] = True
        assert rows(conn, retention_sql, retention_params, indexes=True) == \
            rows(conn, retention_sql, retention_params, indexes=False)
        with_index = sorted(rows(conn, RECEIPTS_SQL, receipts_params, indexes=True))
        assert with_index == sorted(rows(conn, RECEIPTS_SQL, receipts_params, indexes=False))
        assert 0 < len(with_index) < 1000, len(with_index)
        checks['index_results_equal_sequential_scan_results'] = True
        measures['retention_query_ms'] = timed(conn, retention_sql, retention_params)
        measures['receipts_query_ms'] = timed(conn, RECEIPTS_SQL, receipts_params)
        # Pooled connections prepare repeated queries server-side; even the
        # generic plan must prove the predicate from the literal LIKE patterns.
        assert INDEX in generic_plan(conn, retention_sql)
        assert INDEX in generic_plan(conn, RECEIPTS_SQL)
        checks['generic_prepared_plans_use_partial_index'] = True

    # Collector progress writes must not touch the partial index.
    with closing(connect(target)) as conn:
        before = conn.execute('SELECT pg_relation_size(?::regclass)', (INDEX,)).fetchone()[0]
        with conn:
            conn.execute('BEGIN')
            conn.execute("UPDATE checkpoints SET value='{\"status\":\"failed\",\"http_status\":503}',"
                         "updated_at='2026-10-20T00:59:00.000000Z' WHERE key LIKE 'counter_attempt:%'")
        after = conn.execute('SELECT pg_relation_size(?::regclass)', (INDEX,)).fetchone()[0]
    measures['partial_index_growth_after_60k_counter_updates_kb'] = (after - before) // 1024
    assert after == before, (before, after)
    checks['counter_attempt_updates_skip_partial_index'] = True

    result = prune_history(target, now=NOW, dry_run=False)
    assert result['counts']['checkpoints'] == 1000, result
    checks['retention_delete_pass_works'] = True
    return {'checks': checks, 'measures': measures}


def main():
    from ctn_data.secret_files import read_secret
    source = read_secret(Path('data/secrets/postgres_url')).strip()
    parts = urlsplit(source)
    name = 'ctn_ckidx_test_' + uuid.uuid4().hex
    assert re.fullmatch(r'ctn_ckidx_test_[0-9a-f]{32}', name)
    target = urlunsplit(parts._replace(path='/' + name))
    assert target != source
    admin = psycopg.connect(source, autocommit=True, connect_timeout=10)
    created = False
    try:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        created = True
        result = run(target)
    finally:
        try:
            if created:
                admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        finally:
            admin.close()
    result['temporary_database_removed'] = True
    destination = Path('reports/generated/checkpoint-index-postgres.json')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
