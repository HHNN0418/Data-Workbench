"""PG backup -> empty PG restore in one try, in random disposable PostgreSQL databases only."""
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit, urlunsplit
import uuid

import psycopg
from psycopg import sql

from ctn_data import leaderboard
from ctn_data.db import connect, initialize
from ctn_data.migrate_postgres import _digest, backup_postgres, migrate, schema_tables
from ctn_data.postgres import IDENTITY_TABLES

BOARD = ('<table><thead><tr><th>#</th><th>Team</th><th>Score</th></tr></thead><tbody>'
         '<tr><td>1</td><td>Alpha</td><td>900</td></tr><tr><td>2</td><td>Agent Zero</td><td>800</td></tr></tbody></table>')
STAMP = """to_char(timestamp '{}' + g * interval '{}', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')"""
SAMPLING = ('post_id', 'first_seen', 'sampled_at', 'has_sample', 'attempted_at', 'attempt_failed', 'attempt_http',
            'browser_sampled_at', 'last_attempt', 'reply_count', 'replies_synced')


def seed(source):
    initialize(source)
    with closing(connect(source)) as conn, conn:
        conn.execute('BEGIN')
        conn.execute("INSERT INTO raw_records(sha256,source,endpoint,observed_at,payload_json) SELECT md5(g::text),"
                     "CASE WHEN mod(g,2)=0 THEN 'browser:counters' ELSE 'api' END,'/x'," + STAMP.format('2026-10-20', '1 minute') +
                     ",'{}' FROM generate_series(1,200) g")
        conn.execute("INSERT INTO accounts(account_id,username,first_seen,last_seen) SELECT g,'user'||g,"
                     "'2026-10-20T00:00:00.000000Z','2026-10-24T00:00:00.000000Z' FROM generate_series(1,300) g")
        conn.execute("INSERT INTO posts(post_id,author_id,kind,body,created_at,first_seen,last_seen,raw_id) "
                     "SELECT g, 1+mod(g,300), 'post', 'body '||g, " + STAMP.format('2026-10-20', '1 minute') + ", "
                     + STAMP.format('2026-10-20', '1 minute') + ", '2026-10-24T00:00:00.000000Z', 1+mod(g,200) "
                     'FROM generate_series(1,3000) g')
        conn.execute("INSERT INTO counter_snapshots(post_id,observed_at,like_count,reply_count,repost_count,view_count,raw_id) "
                     "SELECT 1+mod(g,3000), " + STAMP.format('2026-10-21', '7 seconds') + ", mod(g,9), mod(g,4), 0, mod(g,90), "
                     '1+mod(g,200) FROM generate_series(1,6000) g')
        conn.execute("INSERT INTO checkpoints(key,value,updated_at) SELECT 'counter_attempt:'||g,"
                     "CASE WHEN mod(g,5)=0 THEN '{\"status\":\"failed\",\"http_status\":503}' "
                     "ELSE '{\"status\":\"saved\",\"http_status\":200}' END," + STAMP.format('2026-10-22', '3 seconds') +
                     ' FROM generate_series(1,2500) g')
        conn.execute("INSERT INTO checkpoints(key,value,updated_at) SELECT 'browser_receipt:'||g,'{\"kind\":\"portal\"}',"
                     + STAMP.format('2026-10-22', '1 minute') + ' FROM generate_series(1,50) g')
        conn.execute("INSERT INTO api_call_logs(occurred_at,method,path) SELECT " + STAMP.format('2026-10-23', '1 second') +
                     ",'GET','/p' FROM generate_series(1,500) g")
        conn.execute("INSERT INTO meta(key,value) VALUES('restore_probe','kept')")
        for minute in (0, 15):
            leaderboard.capture(conn, {'competition_id': 2, 'table_html': BOARD}, f'2026-10-24T01:{minute:02d}:00Z')


def rows_of(conn, table, columns, order):
    return [tuple(row) for row in conn.execute(f'SELECT {",".join(columns)} FROM {table} ORDER BY {order}')]


def run(source, target, workdir):
    seed(source)
    path = backup_postgres(source, workdir)
    lite = sqlite3.connect(path)
    try:
        carried = {row[0] for row in lite.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {table: [row[1] for row in lite.execute(f'PRAGMA table_info({table})')] for table in schema_tables()}
    finally:
        lite.close()
    # The case that used to be refused: the backup carries SQLite's derived table.
    assert 'counter_sampling' in carried
    # target is a brand-new database that nobody has initialized.
    report = migrate(path, target)
    assert report['verified'], report
    checks = {'restore_verified_in_one_try': True}
    with closing(connect(source)) as before, closing(connect(target)) as after:
        mismatched = [table for table in schema_tables()
                      if _digest(before, table, columns[table]) != _digest(after, table, columns[table])]
        assert not mismatched, mismatched
        checks['tables_identical'] = len(schema_tables())
        restored_rows = {table: after.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                         for table in ('posts', 'counter_snapshots', 'checkpoints', 'leaderboard_snapshots', 'meta')}
        # counter_sampling is rebuilt by the triggers while posts, snapshots and attempts are copied.
        expected = rows_of(before, 'counter_sampling', SAMPLING, 'post_id')
        assert expected and rows_of(after, 'counter_sampling', SAMPLING, 'post_id') == expected
        checks['derived_counter_sampling_rebuilt_equal'] = len(expected)
        assert after.execute("SELECT value FROM meta WHERE key='restore_probe'").fetchone()[0] == 'kept'
        sequences = {}
        for table in sorted(IDENTITY_TABLES):
            maximum = after.execute(f'SELECT max(id) FROM {table}').fetchone()[0]
            if maximum is None:
                continue
            following = after.raw.execute("SELECT nextval(pg_get_serial_sequence(%s,'id'))", (table,)).fetchone()[0]
            assert following == maximum + 1, (table, maximum, following)
            sequences[table] = following
        checks['identity_sequences_continue'] = sequences
    # A new backend process opening the restored database starts normally.
    done = subprocess.run([sys.executable, '-c', 'import os; from ctn_data.db import initialize; '
                           'initialize(os.environ["CTN_VERIFY_DSN"])'],
                          env={**os.environ, 'CTN_VERIFY_DSN': target}, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-1500:]
    checks['fresh_process_initialize_ok'] = True
    return {'checks': checks, 'restored_rows': restored_rows}


def main():
    from ctn_data.secret_files import read_secret
    source_url = read_secret(Path('data/secrets/postgres_url')).strip()
    names = ['ctn_restore_test_' + uuid.uuid4().hex for _ in range(2)]
    assert all(re.fullmatch(r'ctn_restore_test_[0-9a-f]{32}', name) for name in names)
    source, target = (urlunsplit(urlsplit(source_url)._replace(path='/' + name)) for name in names)
    assert source_url not in (source, target)
    admin = psycopg.connect(source_url, autocommit=True, connect_timeout=10)
    created = []
    try:
        for name in names:
            admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
            created.append(name)
        with tempfile.TemporaryDirectory() as workdir:
            result = run(source, target, workdir)
    finally:
        try:
            for name in created:
                admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        finally:
            admin.close()
    result['temporary_databases_removed'] = True
    destination = Path('reports/generated/backup-restore-postgres.json')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
