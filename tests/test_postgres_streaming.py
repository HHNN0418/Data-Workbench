"""Real PostgreSQL streaming and index contracts; isolated DSN required."""
import os
from contextlib import closing
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch
from datetime import datetime, timezone

from ctn_data.db import connect, initialize, iter_rows, timestamp_key


@unittest.skipUnless(os.getenv('CTN_TEST_POSTGRES_DSN'), 'requires isolated PostgreSQL DSN')
class PostgresStreamingTests(unittest.TestCase):
    def setUp(self):
        self.dsn = os.environ['CTN_TEST_POSTGRES_DSN']
        initialize(self.dsn)
        self.conn = connect(self.dsn)

    def tearDown(self):
        self.conn.close()

    def test_named_cursor_fetches_bounded_batches_and_releases_own_transaction(self):
        with iter_rows(self.conn, 'SELECT generate_series(1,12000) AS n', batch_size=127) as cursor:
            self.assertTrue(cursor.name.startswith('ctn_stream_'))
            self.assertTrue(self.conn.in_transaction)
            total = 0
            for row in cursor:
                total += row['n']
                self.assertLessEqual(cursor.pgresult.ntuples, 127)
            self.assertEqual(total, 12000 * 12001 // 2)
        self.assertFalse(self.conn.in_transaction)
        self.assertTrue(cursor.closed)

    def test_early_exit_and_exception_close_cursor_but_keep_caller_transaction(self):
        self.conn.execute('BEGIN')
        with self.assertRaisesRegex(RuntimeError, 'stop'):
            with iter_rows(self.conn, 'SELECT generate_series(1,10000) AS n', batch_size=10) as cursor:
                self.assertEqual(next(iter(cursor))['n'], 1)
                self.assertEqual(self.conn.execute('SELECT 42').fetchone()[0], 42)
                raise RuntimeError('stop')
        self.assertTrue(cursor.closed)
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()

    def test_query_error_rolls_back_owned_transaction(self):
        with self.assertRaises(sqlite3.OperationalError):
            with iter_rows(self.conn, 'SELECT nonexistent_column FROM posts'):
                self.fail('invalid query must fail before yielding')
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute('SELECT 1').fetchone()[0], 1)

    def test_timestamp_keys_match_sqlite_and_do_not_depend_on_session_timezone(self):
        samples = ['2026-10-19T01:00:00Z', '2026-10-19T12:00:00.000001+11:00',
                   'Mon, 19 Oct 2026 01:00:00 GMT', '19/10/2026 01:00:00',
                   '19 Oct 2026 01:00:00', 'Oct 19, 2026, 01:00 AM',
                   '2026-10-19 01:00 UTC', '2026-10-19', 'bad', None]
        self.conn.execute("SET TIME ZONE 'America/New_York'")
        for value in samples:
            with self.subTest(value=value):
                self.assertEqual(self.conn.execute('SELECT ctn_timestamp(?)', (value,)).fetchone()[0], timestamp_key(value))
        self.assertIsNone(self.conn.execute("SELECT ctn_timestamp('now')").fetchone()[0])


@unittest.skipUnless(os.getenv('CTN_TEST_POSTGRES_DSN'), 'requires isolated PostgreSQL DSN')
class PostgresReadModelTests(unittest.TestCase):
    """Committed fixtures exercise read models opening their own PG connections."""

    def setUp(self):
        self.dsn = os.environ['CTN_TEST_POSTGRES_DSN']
        initialize(self.dsn)
        self.conn = connect(self.dsn)
        self.base = 10000000000 + uuid.uuid4().int % 10000000000
        self.post_ids = [self.base, self.base + 1]
        self.author = self.base + 100
        self.task_keys_before = {row[0] for row in self.conn.execute('SELECT task_key FROM collection_tasks')}
        with self.conn:
            self.conn.execute('''INSERT INTO accounts(account_id,username,first_seen,last_seen)
                VALUES(?,?,?,?)''', (self.author, 'streaming-fixture', '2026-10-19T07:00:00Z', '2026-10-19T08:00:00Z'))
            for post_id in self.post_ids:
                self.conn.execute('''INSERT INTO posts(post_id,author_id,body,kind,visibility,created_at,first_seen,last_seen)
                    VALUES(?,?,?,'post','public',?,?,?)''',
                    (post_id, self.author, 'isolated streaming fixture', '2026-10-19T07:00:00Z',
                     '2026-10-19T07:00:00Z', '2026-10-19T08:00:00Z'))

    def tearDown(self):
        try:
            self.conn.rollback()
            with self.conn:
                task_keys = {row[0] for row in self.conn.execute('SELECT task_key FROM collection_tasks')}
                for key in task_keys - self.task_keys_before:
                    self.conn.execute('DELETE FROM collection_tasks WHERE task_key=?', (key,))
                for post_id in self.post_ids:
                    self.conn.execute('DELETE FROM counter_snapshots WHERE post_id=?', (post_id,))
                    self.conn.execute('DELETE FROM feed_observations WHERE post_id=?', (post_id,))
                    self.conn.execute('DELETE FROM posts WHERE post_id=?', (post_id,))
                    self.conn.execute('DELETE FROM checkpoints WHERE key=?', ('counter_attempt:' + str(post_id),))
                self.conn.execute('DELETE FROM checkpoints WHERE key=?', ('relation:' + str(self.base + 2),))
                self.conn.execute('DELETE FROM accounts WHERE account_id=?', (self.author,))
        finally:
            self.conn.close()

    def counter(self, observed_at, likes):
        self.conn.execute('''INSERT INTO counter_snapshots
            (post_id,observed_at,like_count,reply_count,repost_count,view_count) VALUES(?,?,?,0,0,10)''',
            (self.base + 1, observed_at, likes))

    def feed(self, observed_at, rank, source='trending'):
        self.conn.execute('''INSERT INTO feed_observations(post_id,source,observed_at,rank_position)
            VALUES(?,?,?,?)''', (self.base + 1, source, observed_at, rank))

    def test_latest_references_coverage_and_planner_execute_on_postgres(self):
        from ctn_data.queries import posts
        from ctn_data.coverage import coverage
        from ctn_data.collector import _config, _counter_schedule, plan_counter_tasks
        before = coverage(self.conn, {}, [], now=datetime(2026, 10, 19, 8, tzinfo=timezone.utc))['counters']
        with self.conn:
            self.conn.execute('UPDATE posts SET reply_to_post_id=?,root_post_id=?,repost_of_post_id=? WHERE post_id=?',
                              (self.base, self.base, self.base + 2, self.base + 1))
            self.conn.execute('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)',
                              ('relation:' + str(self.base + 2), '{"status":"unavailable"}', '2026-10-19T07:00:00Z'))
            self.conn.execute('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)',
                              ('counter_attempt:' + str(self.base), '{"status":"failed"}', '2026-10-19T06:00:00Z'))
            self.counter('2026-10-19T07:00:00Z', 1)
            self.counter('2026-10-19T07:00:00.900000Z', 2)
            self.feed('2026-10-19T07:00:00Z', 1)
            self.feed('2026-10-19T07:00:00.900000Z', 1)
        result = posts(self.dsn, {'author_id': str(self.author)})
        by_id = {row['post_id']: row for row in result['items']}
        child = by_id[self.base + 1]
        self.assertEqual(child['latest_counts']['like_count'], 2)
        self.assertEqual(child['references']['reply_to_post_id']['post']['post_id'], self.base)
        self.assertEqual(child['references']['root_post_id']['status'], 'available')
        self.assertEqual(child['references']['repost_of_post_id']['status'], 'unavailable')
        self.assertEqual(by_id[self.base]['counter_state'], 'failed')
        self.assertEqual(posts(self.dsn, {'feed': 'trending', 'snapshot': '2026-10-19T07:00:00.900000Z',
                                         'author_id': str(self.author)})['items'][0]['post_id'], self.base + 1)
        after = coverage(self.conn, {}, [], now=datetime(2026, 10, 19, 8, tzinfo=timezone.utc))['counters']
        self.assertEqual(after['measured'] - before['measured'], 1)
        self.assertEqual(after['repeated'] - before['repeated'], 1)
        cfg = _config({'counter_limit': 100, 'counter_new_share': .4, 'counter_window_share': .4})
        rows = _counter_schedule(self.conn, cfg, now='2026-10-19T08:00:00Z')
        self.assertIn(self.base + 1, {row['post_id'] for row in rows})
        with patch('ctn_data.collector.utc_now', return_value='2026-10-19T08:00:00Z'):
            self.assertGreater(plan_counter_tasks(self.conn, cfg), 0)
        self.assertIsNotNone(self.conn.execute('SELECT 1 FROM collection_tasks WHERE task_key=?',
                                             ('counter:' + str(self.base + 1),)).fetchone())

    def test_analytics_streams_and_keeps_normalized_trending_contract(self):
        from ctn_data.analytics import analyze
        with self.conn:
            self.counter('2026-10-19T07:00:00Z', 1)
            self.counter('2026-10-19T08:00:00Z', 4)
            self.feed('2026-10-19T07:00:00Z', 8)
            self.feed('2026-10-19T18:00:00+11:00', 7)
            self.feed('2026-10-19T07:00:00.000001Z', 2)
            self.feed('Mon, 19 Oct 2026 08:00:00 GMT', 3)
        result = analyze(self.dsn, {'author_id': self.author, 'detail_limit': 1,
                                   'focus_post_id': self.base + 1, 'as_of': '2026-10-19T09:00:00Z'})
        self.assertEqual(result['summary']['posts'], 2)
        self.assertEqual(result['summary']['counter_snapshots'], 2)
        self.assertEqual(result['growth'][0]['interaction_delta'], 3)
        self.assertEqual(result['trending']['summary']['observations'], 3)
        trajectory = result['trending']['trajectories'][0]
        self.assertEqual((trajectory['first_rank'], trajectory['last_rank'], trajectory['best_rank']), (7, 3, 2))
        self.assertEqual(trajectory['points'][0]['rank'], 3)
        self.assertTrue(trajectory['points_truncated'])

    def test_postgres_backup_is_plain_sqlite_and_rebuilds_indexes_on_initialize(self):
        from ctn_data.migrate_postgres import backup_postgres
        index_names = {'idx_counters_post_utc', 'idx_counters_utc', 'idx_feed_post_utc', 'idx_feed_source_utc'}
        with self.conn:
            self.counter('2026-10-19T07:00:00Z', 1)
            self.feed('2026-10-19T07:00:00Z', 3)
        with tempfile.TemporaryDirectory() as temporary:
            destination = backup_postgres(self.dsn, temporary)
            with closing(sqlite3.connect(destination)) as plain, plain:
                self.assertEqual(plain.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                present = {row[0] for row in plain.execute("SELECT name FROM sqlite_master WHERE type='index'")}
                self.assertFalse(index_names & present)
                self.assertEqual(plain.execute('SELECT like_count FROM counter_snapshots WHERE post_id=?',
                                              (self.base + 1,)).fetchone()[0], 1)
                plain.execute('UPDATE counter_snapshots SET like_count=2 WHERE post_id=?', (self.base + 1,))
                plain.execute('UPDATE feed_observations SET rank_position=2 WHERE post_id=?', (self.base + 1,))
                plain.commit()
                self.assertEqual(plain.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            initialize(destination)
            restored = connect(destination)
            try:
                present = {row[0] for row in restored.execute("SELECT name FROM sqlite_master WHERE type='index'")}
                self.assertTrue(index_names <= present)
                self.assertEqual(restored.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(restored.execute('SELECT like_count FROM counter_snapshots WHERE post_id=?',
                                                 (self.base + 1,)).fetchone()[0], 2)
            finally:
                restored.close()


if __name__ == '__main__':
    unittest.main()
