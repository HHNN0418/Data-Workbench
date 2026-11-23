"""Author enrichment ordering and optional strictly read-only PostgreSQL check."""
import os
import unittest

from ctn_data.collector import _account_candidates
from ctn_data.db import SCHEMA, connect


class AccountCandidateTests(unittest.TestCase):
    def test_unseen_authors_first_then_oldest_attempt_and_no_duplicates(self):
        conn = connect(':memory:')
        try:
            conn.executescript(SCHEMA)
            for post_id, author_id in enumerate((2, 2, 5, 1, 3, None), 1):
                conn.execute('INSERT INTO posts(post_id,author_id,first_seen,last_seen) VALUES(?,?,?,?)',
                             (post_id, author_id, '2026-10-19T00:00:00Z', '2026-10-19T00:00:00Z'))
            for author_id, seen in ((1, '2026-10-19T08:00:00Z'), (3, '2026-10-19T09:00:00Z')):
                conn.execute('INSERT INTO accounts(account_id,first_seen,last_seen) VALUES(?,?,?)',
                             (author_id, seen, seen))
            conn.execute('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)',
                         ('account_attempt:3', '{}', '2026-10-19T07:00:00Z'))
            self.assertEqual([row['author_id'] for row in _account_candidates(conn, 10)], [2, 5, 3, 1])
            self.assertEqual([row['author_id'] for row in _account_candidates(conn, 2)], [2, 5])
            self.assertEqual(_account_candidates(conn, 0), [])
        finally:
            conn.close()

    @unittest.skipUnless(os.getenv('CTN_READONLY_POSTGRES_DSN'), 'requires read-only PostgreSQL audit DSN')
    def test_actual_postgres_query_in_read_only_transaction(self):
        conn = connect(os.environ['CTN_READONLY_POSTGRES_DSN'])
        try:
            conn.raw.execute('BEGIN READ ONLY')
            rows = _account_candidates(conn, 3)
            self.assertLessEqual(len(rows), 3)
            self.assertEqual(len({row['author_id'] for row in rows}), len(rows))
            self.assertTrue(all(row['author_id'] is not None for row in rows))
        finally:
            conn.rollback()
            conn.close()
