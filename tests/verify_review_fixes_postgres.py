"""Verify the 2026-10-21 review fixes on a random temporary PostgreSQL database.

Creates ctn_verify_<random>, runs the checks and drops it; never touches live CTN.
"""
import json
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from ctn_data import postgres
from ctn_data.db import connect, initialize, utc_now


class ReviewFixes(unittest.TestCase):
    dsn = None

    def setUp(self):
        initialize(self.dsn)
        self.conn = connect(self.dsn)
        self.addCleanup(self.conn.close)

    def test_initialize_skips_ddl_when_schema_is_current(self):
        postgres._INITIALIZED.clear()
        with patch.object(postgres, '_initialize_postgres', side_effect=AssertionError('DDL repeated')):
            initialize(self.dsn)
        # A changed fingerprint still applies the schema.
        with self.conn:
            self.conn.execute("UPDATE meta SET value='stale' WHERE key='schema_fingerprint'")
        postgres._INITIALIZED.clear()
        initialize(self.dsn)
        self.assertNotEqual(self.conn.execute("SELECT value FROM meta WHERE key='schema_fingerprint'").fetchone()[0], 'stale')

    def test_fast_timestamp_matches_parser_and_keeps_index_matching(self):
        values = ['2026-10-21T07:00:00.123456Z', '2026-02-29T00:00:00.000000Z', '2024-02-29T00:00:00.000000Z',
                  '2026-04-31T00:00:00.000000Z', '2026-10-21T23:59:60.000000Z', '2026-10-21T24:00:00.000000Z',
                  '0000-01-01T00:00:00.000000Z', ' 2026-10-21T07:00:00.000000Z', '2026-10-21T07:00:00Z',
                  '2026-10-21T07:00:00+11:00', '21/10/2026 07:00:00', 'garbage', None,
                  '2026-10-21T07:00:00.5Z', '2026-10-21T07:00:00.12345Z', '2026-10-21T07:00:00.1234567Z',
                  '2026-10-21T07:00:00.Z', '2026-13-01T00:00:00.5Z', '2026-10-21T07:60:00.5Z', '2026-1a-21T07:00:00.5Z']
        for value in values:
            with self.subTest(value=value):
                fast, parsed = self.conn.execute('SELECT ctn_timestamp(?),ctn_timestamp_parse(?)', (value, value)).fetchone()
                self.assertEqual(fast, parsed)
        plan = '\n'.join(row[0] for row in self.conn.execute(
            "EXPLAIN SELECT post_id FROM posts WHERE ctn_timestamp(created_at)>=? "
            "ORDER BY ctn_timestamp(created_at) DESC NULLS LAST,post_id DESC LIMIT 5", ('2026-01-01T00:00:00.000000Z',)))
        self.assertIn('idx_posts_created_utc', plan)
        with self.conn:
            self.conn.executemany("INSERT INTO posts(post_id,created_at,first_seen,last_seen,visibility) VALUES(?,?,?,?,'public')",
                                  [(20000 + i, f'2026-10-{1 + i % 28:02d}T00:00:00.000000Z', utc_now(), utc_now()) for i in range(2000)])
            self.conn.execute('ANALYZE posts')
        freshness = '\n'.join(row[0] for row in self.conn.execute(
            "EXPLAIN SELECT p.post_id FROM posts p WHERE COALESCE(ctn_timestamp(p.edited_at),ctn_timestamp(p.created_at),"
            "ctn_timestamp(p.first_seen))>=? ORDER BY COALESCE(ctn_timestamp(p.edited_at),ctn_timestamp(p.created_at),"
            "ctn_timestamp(p.first_seen)) DESC,p.post_id DESC LIMIT 20", ('2026-10-20T00:00:00.000000Z',)))
        self.assertIn('idx_posts_freshness_utc', freshness)

    def test_sampling_read_model_tracks_every_writer(self):
        from ctn_data.ingest import save_counters, save_posts
        from ctn_data.collector import _set_checkpoint
        now = utc_now()
        with self.conn:
            save_posts(self.conn, [{'post_id': 9001, 'author_id': 1, 'created_at': now, 'visibility': 'public'},
                                   {'post_id': 9002, 'author_id': 1, 'created_at': now, 'visibility': 'public'}], 'latest', now)
            save_counters(self.conn, 9001, {'post_id': 9001, 'like_count': 1}, now)
            save_counters(self.conn, 9002, {'post_id': 9002, 'like_count': 2}, now, source='browser:counters')
            _set_checkpoint(self.conn, 'counter_attempt:9001', {'status': 'failed', 'http_status': 404}, now)
            _set_checkpoint(self.conn, 'counter_attempt:9001', {'status': 'failed', 'http_status': 403}, now)
            self.conn.execute("INSERT INTO checkpoints(key,value,updated_at) VALUES('counter_attempt:abc','{}',?)", (now,))
        live = {row['post_id']: dict(row) for row in self.conn.execute('SELECT * FROM counter_sampling WHERE post_id IN (9001,9002)')}
        self.assertEqual(live[9001]['attempt_http'], 403)
        self.assertEqual(live[9001]['attempt_failed'], 1)
        self.assertIsNotNone(live[9002]['browser_sampled_at'])
        # The trigger-maintained rows equal a fresh backfill of the same data.
        with self.conn:
            self.conn.execute('DELETE FROM counter_sampling WHERE post_id IN (9001,9002)')
            backfill = 'INSERT INTO counter_sampling(post_id,first_seen,sampled_at' + postgres.POSTGRES_DERIVED.split(
                'INSERT INTO counter_sampling(post_id,first_seen,sampled_at', 1)[1]
            self.conn.raw.execute(backfill)
        rebuilt = {row['post_id']: dict(row) for row in self.conn.execute('SELECT * FROM counter_sampling WHERE post_id IN (9001,9002)')}
        self.assertEqual(live, rebuilt)

    def test_browser_capture_lock_failure_does_not_poison_batch(self):
        from ctn_data import browser_capture
        from ctn_data.db import begin_write as real
        calls = []
        def flaky(conn, scope):
            calls.append(scope)
            if len(calls) == 1:
                conn.execute('SELECT 1/0')  # aborts the transaction like a lock timeout
            return real(conn, scope)
        now = utc_now()
        records = [{'id': 'verify-record-' + str(i), 'kind': 'post', 'observed_at': now,
                    'data': {'post_id': 9100 + i, 'kind': 'post', 'visibility': 'public', 'created_at': now}}
                   for i in range(2)]
        with patch.object(browser_capture, 'begin_write', flaky):
            result = browser_capture.capture(self.dsn, records)
        self.assertEqual(result['accepted'], ['verify-record-1'])
        self.assertEqual([row['id'] for row in result['errors']], ['verify-record-0'])

    def test_pool_reuses_clean_sessions_only(self):
        first = connect(self.dsn)
        pid = first.execute('SELECT pg_backend_pid()').fetchone()[0]
        first.execute('BEGIN')
        first.execute('SELECT pg_advisory_xact_lock(424242)')
        first.execute("INSERT INTO meta(key,value) VALUES('pool_probe','uncommitted')")
        first.close()  # returned mid-transaction: rolled back, lock released
        with self.assertRaises(Exception):
            first.execute('SELECT 1')
        second = connect(self.dsn)
        try:
            self.assertEqual(second.execute('SELECT pg_backend_pid()').fetchone()[0], pid)
            self.assertIsNone(second.execute("SELECT value FROM meta WHERE key='pool_probe'").fetchone())
            self.assertTrue(second.execute('SELECT pg_try_advisory_xact_lock(424242)').fetchone()[0])
            # A session-level lock (content research owner) must end with close().
            second.execute('SELECT pg_try_advisory_lock(434343)').fetchone()
        finally:
            second.close()
        other = postgres.PostgresConnection(self.dsn)
        try:
            self.assertTrue(other.execute('SELECT pg_try_advisory_lock(434343)').fetchone()[0])
        finally:
            other.close()
        from ctn_data import content_research_schedule
        for _ in range(2):  # the owner lock can be taken again after release
            with content_research_schedule.owner_lock(self.dsn):
                pass
        # A session the server dropped is discarded on checkout, not handed out.
        killer = postgres.PostgresConnection(self.dsn)
        try:
            killer.execute('SELECT pg_terminate_backend(?)', (pid,)).fetchone()
        finally:
            killer.close()
        with patch.object(postgres, 'POOL_CHECK_AFTER_SECONDS', -1):
            third = connect(self.dsn)
        try:
            self.assertNotEqual(third.execute('SELECT pg_backend_pid()').fetchone()[0], pid)
        finally:
            third.close()

    def test_backup_with_triggers_is_portable(self):
        from ctn_data.migrate_postgres import backup_postgres, schema_tables
        import sqlite3
        self.assertNotIn('counter_sampling', schema_tables())
        with tempfile.TemporaryDirectory() as directory:
            target = backup_postgres(self.dsn, directory)
            plain = sqlite3.connect(target)  # ordinary tool: no ctn_timestamp registered
            try:
                self.assertEqual(plain.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0], 0)
                plain.execute("INSERT INTO posts(post_id,first_seen,last_seen) VALUES(424242,'x','x')")
            finally:
                plain.close()


def run():
    from ctn_data.secret_files import read_secret
    config = conninfo_to_dict(read_secret(ROOT / 'data/secrets/postgres_url').strip())
    database = 'ctn_verify_' + uuid.uuid4().hex[:12]
    admin = psycopg.connect(make_conninfo(**config), autocommit=True)
    admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
    try:
        ReviewFixes.dsn = (f"postgresql://{quote(config['user'], safe='')}:{quote(config['password'], safe='')}"
                           f"@{config['host']}:{config['port']}/{database}")
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ReviewFixes))
        report = {'verified_at': utc_now(), 'passed': result.testsRun - len(result.failures) - len(result.errors),
                  'failed': len(result.failures), 'errors': len(result.errors),
                  'isolation': 'random temporary database, dropped afterwards; no writes to production CTN'}
        (ROOT / 'reports/generated').mkdir(parents=True, exist_ok=True)
        (ROOT / 'reports/generated/review-fixes-postgres-verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        return result.wasSuccessful()
    finally:
        postgres._INITIALIZED.clear()
        time.sleep(0.5)
        admin.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(database)))
        admin.close()


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
