"""Storage contract tests; set CTN_TEST_POSTGRES_DSN for isolated live PG tests."""
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ctn_data.db import connect, initialize, backup, is_postgres, resolve_database_target, raw_record
from ctn_data.postgres import Row, translate_sql, postgres_schema
from ctn_data.migrate_postgres import migrate


class SQLTranslationTests(unittest.TestCase):
    def test_identification_and_explicit_target(self):
        self.assertTrue(is_postgres('postgresql://localhost/ctn'))
        self.assertFalse(is_postgres('data/ctn.sqlite3'))
        with connect(':memory:') as conn:
            self.assertIsInstance(conn, sqlite3.Connection)

    def test_placeholder_literals_and_percent(self):
        self.assertEqual(translate_sql("SELECT '?' AS x, ? AS y WHERE ? LIKE '%100%'"),
                         "SELECT '?' AS x, %s AS y WHERE %s LIKE '%%100%%'")

    def test_insert_ignore_and_number_json(self):
        self.assertEqual(translate_sql('INSERT OR IGNORE INTO meta VALUES (?,?)'),
                         'INSERT INTO meta VALUES (%s,%s) ON CONFLICT DO NOTHING')
        self.assertIn('ctn_json_number', translate_sql("SELECT json_extract(q.value,'$.http_status')"))

    def test_row_mapping_and_sequence(self):
        row = Row(['id','name'], [7,'x'])
        self.assertEqual(dict(row), {'id':7,'name':'x'})
        self.assertEqual(tuple(row), (7,'x'))
        self.assertEqual(row['id'], row[0])

    def test_initialization_cache_and_force(self):
        from ctn_data.postgres import initialize_postgres
        with patch('ctn_data.postgres._initialize_postgres') as setup:
            initialize_postgres('postgresql://test/cache', 'fixture_schema_1', 3)
            initialize_postgres('postgresql://test/cache', 'fixture_schema_1', 3)
            self.assertEqual(setup.call_count, 1)
            initialize_postgres('postgresql://test/cache', 'fixture_schema_1', 3, force=True)
            self.assertEqual(setup.call_count, 2)
            initialize_postgres('postgresql://test/cache', 'fixture_schema_2', 3)
            self.assertEqual(setup.call_count, 3)

    def test_failed_initialization_not_cached(self):
        from ctn_data.postgres import initialize_postgres
        with patch('ctn_data.postgres._initialize_postgres', side_effect=[RuntimeError('fixture'), None]) as setup:
            with self.assertRaises(RuntimeError):
                initialize_postgres('postgresql://test/cache_failure', 'fixture_schema', 3)
            initialize_postgres('postgresql://test/cache_failure', 'fixture_schema', 3)
            self.assertEqual(setup.call_count, 2)

    def test_subsecond_observation_updates_whole_second(self):
        from ctn_data.db import SCHEMA
        from ctn_data.ingest import save_posts
        conn = connect(':memory:')
        try:
            conn.executescript(SCHEMA)
            save_posts(conn, [{'post_id':1,'body':'old'}], 'test', '2026-10-19T00:00:00Z')
            save_posts(conn, [{'post_id':1,'body':'new'}], 'test', '2026-10-19T00:00:00.100Z')
            self.assertEqual(conn.execute('SELECT body FROM posts WHERE post_id=1').fetchone()[0], 'new')
        finally:
            conn.close()


@unittest.skipUnless(os.getenv('CTN_TEST_POSTGRES_DSN'), 'requires isolated PostgreSQL DSN')
class PostgreSQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = os.environ['CTN_TEST_POSTGRES_DSN']
        initialize(cls.dsn)

    def setUp(self):
        self.conn = connect(self.dsn)

    def tearDown(self):
        self.conn.close()

    def test_transaction_rows_identity_and_rollback(self):
        with self.conn:
            self.conn.execute('DELETE FROM analysis_runs')
            cursor = self.conn.execute('INSERT INTO analysis_runs(created_at,kind,parameters_json,result_json) VALUES(?,?,?,?)', ('now','test','{}','{}'))
            self.assertGreater(cursor.lastrowid, 0)
        count = self.conn.execute('SELECT COUNT(*) FROM analysis_runs').fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            with self.conn:
                self.conn.execute('INSERT INTO analysis_runs(created_at,kind,parameters_json,result_json) VALUES(?,?,?,?)', ('now',None,'{}','{}'))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM analysis_runs').fetchone()[0], count)

    def test_functions_and_repeatable_read(self):
        row = self.conn.execute("SELECT julianday('1970-01-01T00:00:00Z') jd,json_valid('{}') valid,json_extract('{\"a\":\"x\"}','$.a') value,ctn_substr('2026+00:00',-6,1) sign").fetchone()
        self.assertEqual(row['jd'], 2440587.5)
        self.assertTrue(row['valid'])
        self.assertEqual(row['value'], 'x')
        self.assertEqual(row['sign'], '+')
        self.conn.execute('BEGIN')
        self.assertEqual(self.conn.execute('SHOW transaction_isolation').fetchone()[0], 'repeatable read')
        self.conn.rollback()

    def test_unicode_digest_order_matches_sqlite(self):
        from ctn_data.migrate_postgres import _select, digest_rows
        memory = connect(':memory:')
        values = [('中','一'), ('é','two'), ('_a','three'), ('A','four'), ('a','five'), ('10','six'), ('2','seven')]
        try:
            memory.execute('CREATE TABLE hash_order_fixture(key TEXT PRIMARY KEY,value TEXT)')
            memory.executemany('INSERT INTO hash_order_fixture VALUES(?,?)', values)
            with self.conn:
                self.conn.execute('CREATE TEMP TABLE hash_order_fixture(key TEXT PRIMARY KEY,value TEXT)')
                self.conn.executemany('INSERT INTO hash_order_fixture VALUES(?,?)', values)
            columns = ['key','value']
            self.assertEqual(digest_rows(memory.execute(_select('hash_order_fixture',columns,memory))),
                             digest_rows(self.conn.execute(_select('hash_order_fixture',columns,self.conn))))
        finally:
            memory.close()

    def test_raw_record_dedup(self):
        with self.conn:
            first = raw_record(self.conn, 'test', '/fixture', {'v':1})
            second = raw_record(self.conn, 'test', '/fixture', {'v':1})
        self.assertEqual(first, second)

    def test_concurrent_permit_is_single_grant(self):
        from ctn_data.coordination import permit
        with self.conn:
            self.conn.execute("DELETE FROM checkpoints WHERE key IN ('coordination:feeds','throttle:social:feeds')")
        def attempt(index):
            conn = connect(self.dsn)
            try:
                return permit(conn, '/api/posts', 'test', now=1234567)['allowed']
            finally:
                conn.close()
        with ThreadPoolExecutor(max_workers=8) as pool:
            allowed = list(pool.map(attempt, range(8)))
        self.assertEqual(sum(allowed), 1)

    def test_conflicting_counts_and_score_dedup(self):
        from ctn_data.ingest import save_posts, save_counters
        from ctn_data.imports import add_score_snapshot
        at = '2026-10-19T01:00:00Z'
        with self.conn:
            save_posts(self.conn, [{'post_id':999991,'body':'fixture','kind':'post'}], 'test', observed_at=at)
            save_counters(self.conn, 999991, {'like_count':1}, at)
            save_counters(self.conn, 999991, {'like_count':2}, at)
            save_counters(self.conn, 999991, {'like_count':2}, at)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM counter_snapshots WHERE post_id=999991').fetchone()[0],1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM counter_conflicts WHERE post_id=999991').fetchone()[0],1)
        first = add_score_snapshot(self.dsn, '1.25', at, 'test')
        second = add_score_snapshot(self.dsn, '1.25', at, 'test')
        self.assertEqual(first['id'], second['id'])

    def test_latest_unknown_date_sorts_last(self):
        from ctn_data.ingest import save_posts
        from ctn_data.queries import posts
        with self.conn:
            save_posts(self.conn, [
                {'post_id':999992,'body':'null_order_fixture','kind':'post','created_at':None},
                {'post_id':999993,'body':'null_order_fixture','kind':'post','created_at':'2026-10-19T00:00:00Z'},
            ], 'latest', observed_at='2026-10-19T01:00:00Z')
        result = posts(self.dsn, {'feed':'latest','q':'null_order_fixture'})
        self.assertEqual([post['post_id'] for post in result['items']], [999993,999992])

    def test_portal_receipt_rollback_then_idempotent_retry(self):
        from ctn_data.browser_capture import capture
        from ctn_data.imports import add_score_snapshot
        with self.conn:
            self.conn.execute("INSERT INTO meta(key,value) VALUES('browser_team_id','46') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        record = {'id':'integration-portal-receipt','kind':'portal','observed_at':'2026-10-19T01:02:03Z',
                  'data':{'team_id':46,'total':'42.5','activity_csv':'When (UTC),Type,Message\r\n2026-10-19T00:00:00Z,OTHER,fixture\r\n'}}
        before = {table:self.conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
                  for table in ('activity_imports','official_activity','score_snapshots','raw_records')}
        def fail_after_score(*args, **kwargs):
            add_score_snapshot(*args, **kwargs)
            raise ValueError('injected failure after score write')
        with patch('ctn_data.browser_capture.add_score_snapshot', side_effect=fail_after_score):
            result = capture(self.dsn, [record])
        self.assertEqual(result['accepted'], [])
        self.assertEqual(len(result['errors']), 1)
        for table, count in before.items():
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0], count)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM checkpoints WHERE key='browser_receipt:integration-portal-receipt'").fetchone())
        self.assertEqual(capture(self.dsn, [record])['accepted'], [record['id']])
        after = {table:self.conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in before}
        self.assertEqual(capture(self.dsn, [record])['accepted'], [record['id']])
        self.assertEqual(after, {table:self.conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in before})

    def test_concurrent_partial_post_waits_for_full_record(self):
        from ctn_data import ingest
        from ctn_data.postgres import PostgresConnection
        original = PostgresConnection.execute
        full_read, partial_read, partial_started, release_full = (threading.Event() for _ in range(4))
        local = threading.local()
        with self.conn:
            ingest.save_posts(self.conn, [{'post_id':999994,'body':'old'}], 'test', '2026-10-19T00:00:00Z')
        def inspect(conn, sql, parameters=None):
            value = original(conn, sql, parameters)
            if 'AS ingest_last_seen_key FROM posts' in sql and 999994 in parameters[0]:
                if getattr(local, 'kind', '') == 'full':
                    full_read.set()
                    if not release_full.wait(5):
                        raise RuntimeError('test fixture timeout')
                elif getattr(local, 'kind', '') == 'partial':
                    partial_read.set()
            return value
        def write(kind):
            local.kind = kind
            conn = connect(self.dsn)
            try:
                if kind == 'partial':
                    partial_started.set()
                with conn:
                    ingest.save_posts(conn, [{'post_id':999994, **({'body':'fresh'} if kind=='full' else {'visibility':'public'})}],
                                      'test', '2026-10-19T00:00:0' + ('1Z' if kind=='full' else '2Z'))
            finally:
                conn.close()
        with patch.object(PostgresConnection, 'execute', inspect):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(write, 'full')
                try:
                    self.assertTrue(full_read.wait(5))
                    second = pool.submit(write, 'partial')
                    self.assertTrue(partial_started.wait(5))
                    self.assertFalse(partial_read.wait(.2), 'partial read must wait for the full update commit')
                finally:
                    release_full.set()
                first.result(timeout=5)
                second.result(timeout=5)
        row = self.conn.execute('SELECT body,visibility FROM posts WHERE post_id=999994').fetchone()
        self.assertEqual(tuple(row), ('fresh','public'))


if __name__ == '__main__':
    unittest.main()
