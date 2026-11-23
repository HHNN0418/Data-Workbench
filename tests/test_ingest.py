import json
from pathlib import Path
import tempfile
import unittest

from ctn_data.db import connect, initialize
from ctn_data.ingest import save_accounts, save_counters, save_news, save_posts

AT = '2026-10-19T07:00:00Z'
LATER = '2026-10-19T07:01:00Z'


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.path = Path(self.directory.name) / 'test.sqlite3'
        initialize(self.path)
        self.conn = connect(self.path)

    def tearDown(self):
        self.conn.close()
        self.directory.cleanup()

    def count(self, table):
        return self.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    def test_versions_history_and_partial_fields(self):
        post = {'post_id': 1, 'author_id': 9, 'kind': 'post', 'body': 'old',
                'visibility': 'public', 'created_at': AT}
        save_posts(self.conn, [post], 'latest', AT)
        save_posts(self.conn, [post], 'trending', AT)
        self.assertEqual(self.count('posts'), 1)
        self.assertEqual(self.count('post_versions'), 1)
        self.assertEqual(self.count('feed_observations'), 2)
        save_posts(self.conn, [{'post_id': 1, 'body': 'new', 'edited_at': LATER}], 'latest', LATER)
        save_posts(self.conn, [{'post_id': 1, 'rank_position': 2}], 'trending', LATER)
        row = dict(self.conn.execute('SELECT * FROM posts').fetchone())
        self.assertEqual(row['body'], 'new')
        self.assertEqual(row['author_id'], 9)
        self.assertEqual(row['first_seen'], AT)
        self.assertEqual(row['last_seen'], LATER)
        self.assertEqual(self.count('post_versions'), 2)
        self.assertEqual(self.count('team_accounts'), 0)

    def test_invalid_batch_is_atomic(self):
        with self.assertRaises(ValueError):
            save_posts(self.conn, [{'post_id': 1}, {'post_id': 'invalid'}], 'latest', AT)
        self.assertEqual(self.count('posts'), 0)
        self.assertEqual(self.count('raw_records'), 0)
        self.assertEqual(self.count('feed_observations'), 0)

    def test_outer_transaction_can_rollback_page_and_checkpoint(self):
        self.conn.execute('BEGIN')
        save_posts(self.conn, [{'post_id': 1}], 'latest', AT, cursor='opaque')
        self.conn.rollback()
        self.assertEqual(self.count('posts'), 0)
        self.assertEqual(self.count('checkpoints'), 0)

    def test_counts_missing_values_and_separate_observations(self):
        save_posts(self.conn, [{'post_id': 1}], 'latest', AT)
        self.assertEqual(save_counters(self.conn, 1, {'post_id': 1, 'like_count': 0}, AT), 1)
        self.assertEqual(save_counters(self.conn, 1, {'post_id': 1, 'like_count': 0}, AT), 0)
        save_counters(self.conn, 1, {'like_count': 2}, LATER)
        rows = self.conn.execute('SELECT * FROM counter_snapshots ORDER BY observed_at').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['like_count'], 0)
        self.assertIsNone(rows[0]['view_count'])
        self.assertEqual(rows[1]['like_count'], 2)
        with self.assertRaises(ValueError):
            save_counters(self.conn, 1, {'post_id': 2}, LATER)
        for value in (-1, True, 1.5, '2'):
            with self.assertRaises(ValueError):
                save_counters(self.conn, 1, {'view_count': value}, LATER)

    def test_private_account_preserves_known_creation_without_inference(self):
        save_accounts(self.conn, [{'account_id': 1, 'username': 'x', 'verified': True,
                                  'private': False, 'created_at': AT}], AT)
        save_accounts(self.conn, [{'account_id': 1, 'private': True}], LATER)
        account = self.conn.execute('SELECT * FROM accounts').fetchone()
        self.assertEqual(account['created_at'], AT)
        self.assertEqual(account['private'], 1)
        self.assertEqual(account['verified'], 1)
        self.assertEqual(self.count('team_accounts'), 0)

    def test_news_summary_does_not_erase_body_and_sites_are_distinct(self):
        article = {'site': {'sub_url': 'daily-kingstonia'}, 'slug': 'story', 'title': 'One',
                   'body': 'Full body', 'author': 'author', 'published_at': AT}
        save_news(self.conn, [article], AT)
        summary = dict(article)
        summary.pop('body')
        summary['title'] = 'Updated'
        save_news(self.conn, [summary], LATER)
        other = {**article, 'site': 'kingstonia-herald'}
        save_news(self.conn, [other], LATER)
        row = self.conn.execute('SELECT * FROM news_articles WHERE site=?', ('daily-kingstonia',)).fetchone()
        self.assertEqual(row['body'], 'Full body')
        self.assertEqual(row['title'], 'Updated')
        self.assertEqual(row['first_seen'], AT)
        self.assertEqual(self.count('news_articles'), 2)


if __name__ == '__main__':
    unittest.main()
