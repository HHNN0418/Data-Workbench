"""Exact date filters and page-bounded account work, using isolated databases."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ctn_data import analytics, queries
from ctn_data.db import connect, initialize


class AnalyticsPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.path = Path(self.temporary.name) / 'performance.sqlite3'
        initialize(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def seed_posts(self):
        stamps = [
            '2026-10-20T00:00:00.000000Z',
            '2026-10-20T00:00:00.000001Z',
            '2026-10-20T11:00:00.000002+11:00',
            '2026-10-20T00:00:00.000003Z',
            'not-a-timestamp',
        ]
        with closing(connect(self.path)) as conn, conn:
            conn.executemany('''INSERT INTO posts(post_id,created_at,first_seen,last_seen)
                VALUES(?,?,?,?)''', [(index, stamp, 'seen', 'seen')
                                     for index, stamp in enumerate(stamps, 1)])
            conn.executemany('''INSERT INTO counter_snapshots(post_id,observed_at,like_count)
                VALUES(?,'2026-10-20T01:00:00Z',1)''', [(index,) for index in range(1, 6)])

    def test_date_range_respects_microseconds_and_equivalent_offsets(self):
        self.seed_posts()
        result = analytics.analyze(self.path, {
            'since': '2026-10-20T00:00:00.000001Z',
            'until': '2026-10-20T00:00:00.000002Z',
            'sections': 'core', 'include_exportable': False,
        })
        self.assertEqual(result['summary']['posts'], 2)
        self.assertEqual(result['summary']['counter_snapshots'], 2)
        self.assertEqual(result['summary']['publication_start'], '2026-10-20T00:00:00.000001Z')
        self.assertEqual(result['summary']['publication_end'], '2026-10-20T00:00:00.000002Z')

    def test_date_filtered_post_lookup_uses_existing_normalized_index(self):
        self.seed_posts()
        queries_seen = []
        original_connect = analytics.connect

        def traced(path):
            conn = original_connect(path)
            conn.set_trace_callback(queries_seen.append)
            return conn

        bounds = {'since': '2026-10-20T00:00:00.000001Z',
                  'until': '2026-10-20T00:00:00.000002Z'}
        for names in (('since',), ('until',), ('since', 'until')):
            with self.subTest(bounds=names):
                queries_seen.clear()
                with patch.object(analytics, 'connect', side_effect=traced):
                    analytics.analyze(self.path, {
                        **{name: bounds[name] for name in names},
                        'sections': 'core', 'include_exportable': False,
                    })
                query = next(sql for sql in queries_seen if sql.startswith('SELECT p.*'))
                with closing(connect(self.path)) as conn:
                    plan = ' '.join(str(tuple(row)) for row in conn.execute('EXPLAIN QUERY PLAN ' + query))
                    snapshot_query = next(sql for sql in queries_seen if sql.startswith('SELECT c.*'))
                    snapshot_plan = ' '.join(str(tuple(row)) for row in conn.execute('EXPLAIN QUERY PLAN ' + snapshot_query))
                self.assertIn('SEARCH p USING INDEX idx_posts_created_utc', plan)
                # Snapshots are read in batches of already selected post ids,
                # so the snapshot query no longer re-scans posts at all.
                self.assertIn('SEARCH c USING INDEX', snapshot_plan)
                self.assertNotIn('SCAN c', snapshot_plan)
                self.assertNotIn('SCAN p', snapshot_plan)

    def test_accounts_count_only_selected_page_posts(self):
        with closing(connect(self.path)) as conn, conn:
            conn.executemany('''INSERT INTO accounts(account_id,username,first_seen,last_seen)
                VALUES(?,?,?,?)''', [(index, f'user-{index}', 'seen', f'{index:06d}')
                                     for index in range(1, 1001)])
            conn.executemany('''INSERT INTO posts(post_id,author_id,first_seen,last_seen)
                VALUES(?,?,?,?)''', [(index, (index - 1) % 1000 + 1, 'seen', 'seen')
                                     for index in range(1, 10001)])
        steps = [0]
        original_connect = queries.connect

        def traced(path):
            conn = original_connect(path)
            def progress():
                steps[0] += 100
                return 0
            conn.set_progress_handler(progress, 100)
            return conn

        with patch.object(queries, 'connect', side_effect=traced):
            result = queries.accounts(self.path, {'limit': 5, 'offset': 10})
        self.assertEqual(result['total'], 1000)
        self.assertEqual([row['account_id'] for row in result['items']], list(range(990, 985, -1)))
        self.assertEqual([row['post_count'] for row in result['items']], [10] * 5)
        # Sorting the account page is necessary. Counting every unreturned
        # account's ten posts takes >70k VM instructions on this fixture.
        self.assertLess(steps[0], 30000)

    def test_account_search_and_membership_survive_page_first_lookup(self):
        with closing(connect(self.path)) as conn, conn:
            conn.executemany('''INSERT INTO accounts(account_id,username,display_name,first_seen,last_seen)
                VALUES(?,?,?,'seen','same')''', [(1, 'needle-one', 'One'), (2, 'other', 'needle-two'),
                                              (3, 'needle-three', 'Three')])
            conn.execute("INSERT INTO team_accounts VALUES(3,'Our team',1,'supplied list','now')")
            conn.execute("INSERT INTO posts(post_id,author_id,first_seen,last_seen) VALUES(1,3,'seen','seen')")
        result = queries.accounts(self.path, {'q': 'needle', 'limit': 1})
        self.assertEqual(result['total'], 3)
        self.assertEqual(result['items'][0]['account_id'], 3)
        self.assertEqual(result['items'][0]['team_label'], 'Our team')
        self.assertEqual(result['items'][0]['post_count'], 1)
        second = queries.accounts(self.path, {'q': 'needle', 'limit': 1, 'offset': 1})
        self.assertEqual(second['items'][0]['account_id'], 2)


if __name__ == '__main__':
    unittest.main()
