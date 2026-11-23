import tempfile
import unittest
from pathlib import Path
from ctn_data.db import initialize, connect
from ctn_data.ingest import save_posts
from ctn_data.queries import posts


class FeedViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'feeds.db'
        initialize(self.db)
        self.conn = connect(self.db)
        with self.conn:
            save_posts(self.conn, [self.post(1, 2), self.post(2, 1)], 'latest', '2026-10-19T01:00:00Z')
            save_posts(self.conn, [self.post(3, 3, 1, 9), self.post(1, 2, 2, 7)], 'trending', '2026-10-19T01:01:00Z')
            save_posts(self.conn, [self.post(2, 1, 1, 11)], 'trending', '2026-10-19T01:02:00Z')
            save_posts(self.conn, [self.post(3, 3, 51, 5)], 'trending', '2026-10-19T01:03:00Z')

    def tearDown(self):
        self.conn.close(); self.temp.cleanup()

    @staticmethod
    def post(pid, minute, rank=None, hot=None):
        return dict(post_id=pid, body='body', kind='post', created_at=f'2026-10-19T00:{minute:02d}:00Z',rank_position=rank,global_hot_score=hot)

    def test_latest_filters_source_and_orders_publication(self):
        result = posts(self.db, {'feed':'latest'})
        self.assertEqual([p['post_id'] for p in result['items']], [1,2])

    def test_trending_uses_latest_head_not_latest_tail_or_stale_ranks(self):
        result = posts(self.db, {'feed':'trending'})
        self.assertEqual([p['post_id'] for p in result['items']], [2])
        self.assertEqual(result['items'][0]['rank_position'], 1)
        self.assertEqual(result['items'][0]['global_hot_score'], 11)
        self.assertEqual(result['feed_observed_at'], '2026-10-19T01:02:00Z')

    def test_observed_page_keeps_ranks_and_filters_without_reindexing(self):
        result = posts(self.db, {'feed':'trending','snapshot':'2026-10-19T01:01:00Z','q':'1'})
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['rank_position'], 2)
        self.assertEqual(len(posts(self.db, {'feed':'all'})['items']),3)
        with self.assertRaises(ValueError):
            posts(self.db, {'feed':'trending','snapshot':'bad'})
