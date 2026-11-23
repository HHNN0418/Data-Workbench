"""Read-only social context API preserves account scope and local paging."""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from ctn_data.db import connect
from ctn_data.ingest import save_accounts, save_posts
from ctn_data.server import create_app
from tests.auth_helpers import login


class SocialContextAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'test.sqlite3'
        self.client = TestClient(create_app(self.db, backup_dir=Path(self.temp.name) / 'backups'),
                                 base_url='http://127.0.0.1:8765')
        self.addCleanup(self.client.close)
        login(self.client)
        conn = connect(self.db)
        try:
            with conn:
                save_accounts(conn, [
                    {'account_id': 1, 'username': 'DailyKingstonia', 'display_name': 'Daily Kingstonia'},
                    {'account_id': 2, 'username': 'KingstoniaHerald', 'display_name': 'Kingstonia Herald'},
                    {'account_id': 3, 'username': 'DailyKingstoniaFan'},
                ])
                save_posts(conn, [
                    {'post_id': 101, 'author_id': 1, 'body': 'Vaccine report #health', 'created_at': '2026-10-20T01:00:00Z'},
                    {'post_id': 102, 'author_id': 2, 'body': 'Harbour report', 'created_at': '2026-10-20T02:00:00Z'},
                    {'post_id': 103, 'author_id': 3, 'body': 'Vaccine fan post', 'created_at': '2026-10-20T03:00:00Z'},
                ], 'latest')
        finally:
            conn.close()

    def test_news_accounts_only_exact_usernames_with_local_paging(self):
        response = self.client.get('/api/social-context?view=news_accounts&limit=1')
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data['total'], 2)
        self.assertEqual([row['post_id'] for row in data['items']], [102])
        second = self.client.get('/api/social-context?view=news_accounts&limit=1&offset=1').json()
        self.assertEqual([row['post_id'] for row in second['items']], [101])

    def test_search_is_parameterized_and_cannot_expand_account_scope(self):
        data = self.client.get('/api/social-context?view=news_accounts&q=Vaccine').json()
        self.assertEqual([row['post_id'] for row in data['items']], [101])
        data = self.client.get('/api/social-context', params={'view': 'news_accounts', 'q': "' OR 1=1--"}).json()
        self.assertEqual(data['total'], 0)

    def test_empty_tags_are_not_fabricated_and_input_is_validated(self):
        response = self.client.get('/api/social-context?popular=true')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['items'], [])
        self.assertEqual(response.json()['total'], 0)
        for query in ('view=unknown', 'popular=maybe', 'limit=oops'):
            self.assertEqual(self.client.get('/api/social-context?' + query).status_code, 400)

    def test_context_requires_member_session(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/social-context').status_code, 401)


if __name__ == '__main__':
    unittest.main()
