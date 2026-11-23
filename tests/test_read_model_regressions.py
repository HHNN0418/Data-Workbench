"""Legacy timestamp correctness and bounded dashboard reads."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ctn_data import queries
from ctn_data.db import SCHEMA, backup, connect, initialize, iter_rows, timestamp_key
from ctn_data.ingest import save_counters, save_posts


class ReadModelRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.db = Path(self.temp.name) / 'review.sqlite3'
        initialize(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_latest_and_history_respect_subsecond_time_in_legacy_records(self):
        with closing(connect(self.db)) as conn, conn:
            save_posts(conn, [{'post_id': 1, 'body': 'a'}], 'latest', '2026-10-19T01:00:00Z')
            save_counters(conn, 1, {'like_count': 1}, '2026-10-19T01:00:00Z')
            save_counters(conn, 1, {'like_count': 2}, '2026-10-19T01:00:00.000001Z')
            save_counters(conn, 1, {'like_count': 3}, '2026-10-19T12:00:00.900000+11:00')
        self.assertEqual(queries.posts(self.db, {})['items'][0]['latest_counts']['like_count'], 3)
        history = queries.post_detail(self.db, 1)['counters']
        self.assertEqual([row['like_count'] for row in history], [1, 2, 3])
        self.assertTrue(all(row['elapsed_minutes'] >= 0 for row in history[1:]))
        self.assertEqual(history[1]['deltas']['like_count'], 1)

    def test_older_submillisecond_post_cannot_replace_newer_body(self):
        with closing(connect(self.db)) as conn, conn:
            save_posts(conn, [{'post_id': 1, 'body': 'new'}], 'latest', '2026-10-19T01:00:00.000002Z')
            save_posts(conn, [{'post_id': 1, 'body': 'old'}], 'latest', '2026-10-19T01:00:00.000001Z')
        self.assertEqual(queries.post_detail(self.db, 1)['post']['body'], 'new')

    def test_trending_head_selects_latest_instant_not_largest_string(self):
        with closing(connect(self.db)) as conn, conn:
            save_posts(conn, [{'post_id': 1, 'rank_position': 1}], 'trending', '2026-10-19T01:00:00Z')
            save_posts(conn, [{'post_id': 2, 'rank_position': 1}], 'trending', '2026-10-19T01:00:00.900Z')
        self.assertEqual([row['post_id'] for row in queries.posts(self.db, {'feed': 'trending'})['items']], [2])

    def test_page_references_and_attempts_are_batched(self):
        with closing(connect(self.db)) as conn, conn:
            save_posts(conn, [{'post_id': 1, 'body': 'root'}], 'latest')
            save_posts(conn, [{'post_id': i, 'reply_to_post_id': 1, 'root_post_id': 1,
                              'repost_of_post_id': 999, 'body': 'child'} for i in range(2, 202)], 'latest')
            conn.executemany('INSERT INTO checkpoints VALUES(?,?,?)', [
                ('relation:999', '{"status":"unavailable"}', '2026-10-19T01:00:00Z'),
                ('counter_attempt:201', '{"status":"failed"}', '2026-10-19T01:00:00Z')])
        statements = []
        original = queries.connect
        def traced(path):
            conn = original(path)
            conn.set_trace_callback(statements.append)
            return conn
        with patch.object(queries, 'connect', side_effect=traced):
            page = queries.posts(self.db, {'limit': 200})
        self.assertLessEqual(sum(sql.startswith('SELECT') for sql in statements), 8)
        self.assertEqual(len(page['items']), 200)
        first = page['items'][0]
        self.assertEqual(first['counter_state'], 'failed')
        self.assertEqual(first['references']['root_post_id']['post']['body'], 'root')
        self.assertEqual(first['references']['repost_of_post_id']['status'], 'unavailable')

    def test_operations_does_not_return_durable_receipts(self):
        with closing(connect(self.db)) as conn, conn:
            conn.executemany('INSERT INTO checkpoints VALUES(?,?,?)',
                [(f'worker_result:{i}', '{"accepted":true}', '2026-10-19T01:00:00Z') for i in range(5000)] +
                [('browser_receipt:portal', '{"total":123}', '2026-10-19T01:00:00Z'),
                 ('feed:latest', '{"cursor":"next-page"}', '2026-10-19T01:00:00Z')])
        result = queries.operations(self.db, {})
        self.assertEqual([row['key'] for row in result['checkpoints']], ['feed:latest'])
        self.assertLess(len(json.dumps(result)), 10000)
        with closing(connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0], 5002)

    def test_schema_upgrade_preserves_legacy_values_and_installs_sort_index(self):
        old = Path(self.temp.name) / 'legacy.sqlite3'
        with closing(sqlite3.connect(old)) as conn, conn:
            conn.executescript('\n'.join(line for line in SCHEMA.splitlines() if 'ctn_timestamp' not in line))
            conn.execute('PRAGMA user_version=3')
            conn.execute("INSERT INTO posts(post_id,first_seen,last_seen) VALUES(1,'old','old')")
            conn.executemany('INSERT INTO counter_snapshots(post_id,observed_at,like_count) VALUES(1,?,?)',
                             [('2026-10-19T01:00:00Z', 1), ('2026-10-19T01:00:00.900Z', 2)])
        initialize(old)
        with closing(connect(old)) as conn:
            self.assertEqual(conn.execute('SELECT observed_at FROM counter_snapshots ORDER BY id LIMIT 1').fetchone()[0], '2026-10-19T01:00:00Z')
            plan = ' '.join(str(tuple(row)) for row in conn.execute('EXPLAIN QUERY PLAN SELECT id FROM counter_snapshots WHERE post_id=1 ORDER BY ctn_timestamp(observed_at) DESC,id DESC LIMIT 1'))
            self.assertIn('idx_counters_post_utc', plan)
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        portable = backup(old, Path(self.temp.name) / 'backups')
        with closing(sqlite3.connect(portable)) as conn, conn:
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            conn.execute("UPDATE counter_snapshots SET like_count=3 WHERE id=1")

    def test_exact_sort_keys_and_cursor_cleanup(self):
        self.assertEqual(timestamp_key('2026-10-19T12:00:00.000001+11:00'), '2026-10-19T01:00:00.000001Z')
        self.assertEqual(timestamp_key('Mon, 19 Oct 2026 01:00:00 GMT'), '2026-10-19T01:00:00.000000Z')
        self.assertIsNone(timestamp_key('not-a-time'))
        with closing(connect(self.db)) as conn:
            with iter_rows(conn, 'SELECT 1') as cursor:
                self.assertEqual(cursor.fetchone()[0], 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                cursor.fetchone()


if __name__ == '__main__':
    unittest.main()
