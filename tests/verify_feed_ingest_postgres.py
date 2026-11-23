"""Opt-in batch-ingestion contract check against its own random PostgreSQL DB.

Run with ``python -m tests.verify_feed_ingest_postgres``. The configured DSN is
used only to create/drop a random test database; application data is untouched.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

import psycopg
from psycopg import sql

from ctn_data.db import SCHEMA, connect, initialize
from ctn_data.ingest import save_posts


class CountedConnection:
    def __init__(self, conn):
        self.conn = conn
        self.execute_calls = 0
        self.executemany_calls = 0
        self.executemany_rows = 0
        self.post_lock_parameters = []

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def execute(self, statement, parameters=None):
        self.execute_calls += 1
        return self.conn.execute(statement, parameters)

    def executemany(self, statement, rows):
        rows = list(rows)
        self.executemany_calls += 1
        self.executemany_rows += len(rows)
        if statement == 'SELECT pg_advisory_xact_lock(?)':
            self.post_lock_parameters.extend(rows)
        return self.conn.executemany(statement, rows)

    def __enter__(self):
        self.conn.__enter__()
        return self

    def __exit__(self, *args):
        return self.conn.__exit__(*args)


def snapshot(conn):
    """Compare stable content and raw provenance, independent of generated IDs."""
    queries = {
        'raw': '''SELECT sha256,source,endpoint,observed_at,payload_json
                  FROM raw_records ORDER BY sha256''',
        'posts': '''SELECT p.post_id,p.author_id,p.kind,p.body,p.reply_to_post_id,
                    p.root_post_id,p.repost_of_post_id,p.visibility,p.embed_json,
                    p.created_at,p.edited_at,p.first_seen,p.last_seen,r.sha256
                    FROM posts p JOIN raw_records r ON r.id=p.raw_id ORDER BY p.post_id''',
        'versions': '''SELECT v.post_id,v.body,v.edited_at,v.observed_at,v.content_hash,r.sha256
                       FROM post_versions v JOIN raw_records r ON r.id=v.raw_id
                       ORDER BY v.post_id,v.content_hash''',
        'feed': '''SELECT post_id,source,observed_at,rank_position,global_hot_score
                   FROM feed_observations ORDER BY post_id,source,observed_at''',
        'cursor': '''SELECT key,value,updated_at FROM checkpoints
                     WHERE key LIKE 'feed:%:cursor' ORDER BY key''',
    }
    return {name:[tuple(row) for row in conn.execute(statement).fetchall()]
            for name,statement in queries.items()}


def compare(reference, conn):
    expected, actual = snapshot(reference), snapshot(conn)
    for table in expected:
        assert actual[table] == expected[table], 'SQLite contract differs: '+table


def timed_batch(conn, posts, at, cursor):
    measured = CountedConnection(conn)
    started = time.monotonic()
    with measured:
        assert save_posts(measured, posts, 'batch-test', at, cursor) == len(posts)
    elapsed = round((time.monotonic()-started)*1000, 3)
    calls = measured.execute_calls+measured.executemany_calls
    assert calls <= 12, f'Expected a bounded number of batch client calls, got {calls}'
    expected_locks = [(int.from_bytes(hashlib.sha256(f'ctn:post:{post_id}'.encode()).digest()[:8],
                                    signed=True),) for post_id in sorted({p['post_id'] for p in posts})]
    assert measured.post_lock_parameters == expected_locks, 'Post lock order changed'
    return {'posts':len(posts),'elapsed_ms':elapsed,'execute_calls':measured.execute_calls,
            'executemany_calls':measured.executemany_calls,'executemany_parameter_sets':measured.executemany_rows,
            'client_calls':calls}


def rollback_case(reference, conn, posts, label):
    before = snapshot(conn)
    for database in (reference, conn):
        with database:
            database.execute('''INSERT INTO meta(key,value) VALUES(?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value''', ('outer-guard:'+label,'retained'))
            try:
                save_posts(database, posts, 'batch-test', '2026-10-20T01:06:00Z', 'must-rollback')
            except (ValueError, TypeError, sqlite3.Error):
                pass
            else:
                raise AssertionError('Invalid batch unexpectedly succeeded: '+label)
        assert database.execute('SELECT value FROM meta WHERE key=?',('outer-guard:'+label,)).fetchone()[0] == 'retained'
    assert snapshot(conn) == before, 'Savepoint leaked partial writes: '+label
    compare(reference, conn)


def main():
    from ctn_data.secret_files import read_secret
    source = read_secret(Path('data/secrets/postgres_url')).strip()
    name = 'ctn_feed_ingest_test_' + uuid.uuid4().hex[:12]
    target = urlunsplit(urlsplit(source)._replace(path='/'+name))
    admin = psycopg.connect(source, autocommit=True)
    conn = reference = None
    created = False
    result = {}
    try:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        created = True
        initialize(target)
        conn = connect(target)
        reference = connect(':memory:')
        reference.executescript(SCHEMA)
        posts = [{'post_id':number,'author_id':number+1000,'kind':'post','body':'批次正文 '+str(number),
                  'visibility':'public','created_at':'2026-10-20T01:00:00Z',
                  'embed':{'title':'链接 '+str(number)},'rank_position':number,
                  'global_hot_score':number/10} for number in range(100,0,-1)]
        result['new_100_posts'] = timed_batch(conn, posts, '2026-10-20T01:01:00Z', 'first')
        with reference:
            save_posts(reference, posts, 'batch-test', '2026-10-20T01:01:00Z', 'first')
        compare(reference, conn)
        result['existing_100_posts'] = timed_batch(conn, posts, '2026-10-20T01:02:00Z', 'repeat')
        with reference:
            save_posts(reference, posts, 'batch-test', '2026-10-20T01:02:00Z', 'repeat')
        compare(reference, conn)
        assert conn.execute('SELECT COUNT(*) FROM raw_records').fetchone()[0] == 100
        assert conn.execute('SELECT COUNT(*) FROM post_versions').fetchone()[0] == 100
        assert conn.execute('SELECT COUNT(*) FROM feed_observations').fetchone()[0] == 200
        result['raw_dedup_first_observed_and_version_uniqueness'] = True

        batches = [
            # Repeated IDs merge in input order, but feed rank keeps the first input.
            ([{'post_id':101,'body':'first','visibility':'public','embed':{'value':1},'rank_position':1},
              {'post_id':101,'visibility':'private','rank_position':99},
              {'post_id':101,'body':'final','embed':None,'rank_position':98},
              {'post_id':1,'body':'updated'}, {'post_id':1,'author_id':999}], '2026-10-20T01:04:00Z'),
            # Stale repeated inputs each merge with the current accepted row.
            ([{'post_id':101,'body':'stale-one','embed':{'old':True}},
              {'post_id':101,'visibility':'public'}, {'post_id':1,'body':'stale-two'}],
             '2026-10-20T01:03:00Z'),
            ([{'post_id':101,'edited_at':'2026-10-20T01:05:00Z'},
              {'post_id':101,'embed':{'new':True}}, {'post_id':101,'body':'final-two'}],
             '2026-10-20T01:05:00Z'),
        ]
        for number,(batch,at) in enumerate(batches):
            for database in (reference, conn):
                with database:
                    assert save_posts(database, batch, 'batch-test', at, 'semantic-'+str(number)) == len(batch)
            compare(reference, conn)
        row = conn.execute('SELECT body,visibility,embed_json FROM posts WHERE post_id=101').fetchone()
        assert tuple(row) == ('final-two','private','{"new":true}')
        assert conn.execute('''SELECT rank_position FROM feed_observations
            WHERE post_id=101 AND observed_at=?''',('2026-10-20T01:04:00Z',)).fetchone()[0] == 1
        result['partial_fields_duplicate_order_rank_and_stale_history'] = True

        rollback_case(reference, conn, [{'post_id':201,'body':'must rollback'}, {'post_id':0}], 'validation')
        # The failure happens in feed_observations after raw/posts/versions were written.
        rollback_case(reference, conn, [{'post_id':202,'body':'must rollback','rank_position':{'invalid':True}}], 'late-write')
        result['validation_and_late_write_savepoint_rollback'] = True

        for database in (reference, conn):
            with database:
                assert save_posts(database, [], 'batch-test', '2026-10-20T01:07:00Z', 'empty-batch') == 0
            assert database.execute('SELECT value FROM checkpoints WHERE key=?',('feed:batch-test:cursor',)).fetchone()[0] == 'empty-batch'
        compare(reference, conn)
        result['empty_batch_cursor'] = True
        print(json.dumps(result, ensure_ascii=False))
    finally:
        if conn is not None:
            conn.close()
        if reference is not None:
            reference.close()
        if created:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        admin.close()


if __name__ == '__main__':
    main()
