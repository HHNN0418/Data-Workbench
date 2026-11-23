"""Exercise exploration state through the actual local HTTP contract."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from ctn_data.db import connect, initialize
from ctn_data.server import create_app
from tests.auth_helpers import login


class AnalysisApiTests(unittest.TestCase):
    def test_saved_analysis_survives_reopening_within_ttl_and_explicit_refresh_recomputes(self):
        from ctn_data.analytics import analyze
        with (tempfile.TemporaryDirectory() as temp,
              patch('ctn_data.analysis_cache.analyze', wraps=analyze) as compute,
              patch('ctn_data.analysis_cache.utc_now', return_value='2026-10-20T00:00:00.000000Z') as clock):
            path = Path(temp) / 'analysis.sqlite3'
            app = create_app(path)
            with TestClient(app, base_url='http://127.0.0.1:8765') as client:
                token = login(client)
                first = client.get('/api/analysis').json()
                self.assertFalse(first['cache']['hit'])
                self.assertEqual(first['cache']['policy'], 'ttl')
                self.assertEqual(first['cache']['max_age_seconds'], 60)
                again = client.get('/api/analysis').json()
                self.assertTrue(again['cache']['hit'])
                self.assertEqual(first['generated_at'], again['generated_at'])
                saved = client.post('/api/analysis/run', json={}, headers={'X-CTN-Token': token})
                self.assertEqual(saved.status_code, 200, saved.text)
                self.assertEqual(saved.json()['generated_at'], first['generated_at'])
                self.assertEqual(compute.call_count, 1)
                self.assertEqual(client.get('/api/analysis?refresh=invalid').status_code, 400)
                self.assertEqual(client.get('/api/analysis?window_minutes=true').status_code, 400)
            # A browser reload or backend restart reuses an unexpired durable result.
            clock.return_value = '2026-10-20T00:00:59.000000Z'
            with TestClient(create_app(path), base_url='http://127.0.0.1:8765') as client:
                login(client)
                self.assertTrue(client.get('/api/analysis').json()['cache']['hit'])
                self.assertEqual(compute.call_count, 1)
                refreshed = client.get('/api/analysis?refresh=true').json()
                self.assertFalse(refreshed['cache']['hit'])
                self.assertNotEqual(refreshed['generated_at'], first['generated_at'])
                self.assertEqual(compute.call_count, 2)

    def test_expired_analysis_get_includes_new_data_after_reopening_without_explicit_refresh(self):
        from ctn_data.analytics import analyze
        with (tempfile.TemporaryDirectory() as temp,
              patch('ctn_data.analysis_cache.analyze', wraps=analyze) as compute,
              patch('ctn_data.analysis_cache.utc_now', return_value='2026-10-20T00:00:00.000000Z') as clock):
            path = Path(temp) / 'analysis.sqlite3'
            with TestClient(create_app(path), base_url='http://127.0.0.1:8765') as client:
                login(client)
                response = client.get('/api/analysis')
                self.assertEqual(response.status_code, 200, response.text)
                first = response.json()
                self.assertEqual(first['summary']['posts'], 0)
                self.assertEqual(first['summary']['counter_snapshots'], 0)
                self.assertFalse(first['cache']['hit'])

            # Collection can continue while the analysis page is closed.
            observed_at = '2026-10-20T00:00:30.000000Z'
            with closing(connect(path)) as conn, conn:
                conn.execute('''INSERT INTO posts(post_id,kind,body,created_at,first_seen,last_seen)
                    VALUES(1,'post','Newly collected post',?,?,?)''', (observed_at,) * 3)
                conn.execute('''INSERT INTO counter_snapshots
                    (post_id,observed_at,like_count,reply_count,repost_count,view_count)
                    VALUES(1,?,1,0,0,10)''', (observed_at,))

            clock.return_value = '2026-10-20T00:00:59.999999Z'
            with TestClient(create_app(path), base_url='http://127.0.0.1:8765') as client:
                login(client)
                within_ttl = client.get('/api/analysis').json()
                self.assertTrue(within_ttl['cache']['hit'])
                self.assertEqual(within_ttl['cache']['stored_at'], first['cache']['stored_at'])
                self.assertEqual(within_ttl['summary']['posts'], 0)
                self.assertEqual(compute.call_count, 1)

                # Expiry alone must make an ordinary GET include the latest rows.
                clock.return_value = '2026-10-20T00:01:00.000000Z'
                response = client.get('/api/analysis')
                self.assertEqual(response.status_code, 200, response.text)
                updated = response.json()
                self.assertFalse(updated['cache']['hit'])
                self.assertEqual(updated['cache']['stored_at'], clock.return_value)
                self.assertEqual(updated['summary']['posts'], 1)
                self.assertEqual(updated['summary']['counter_snapshots'], 1)
                self.assertEqual(updated['quality']['last_counter_observed_at'], observed_at)
                self.assertEqual(sum(row['posts_published'] for row in updated['timeseries']), 1)
                self.assertEqual(sum(row['snapshots'] for row in updated['timeseries']), 1)
                self.assertEqual(compute.call_count, 2)
                self.assertTrue(client.get('/api/analysis').json()['cache']['hit'])
                self.assertEqual(compute.call_count, 2)

    def test_linked_selection_preserves_scope_and_rejects_ambiguous_filters(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'analysis.sqlite3'
            initialize(path)
            with closing(connect(path)) as conn, conn:
                conn.executemany('''INSERT INTO posts
                    (post_id,author_id,kind,body,created_at,first_seen,last_seen,reply_to_post_id)
                    VALUES(?,?,?,?,?,?,?,?)''', [
                    (1, 10, 'post', 'Root', '2026-10-19T07:00:00Z', '2026-10-19T07:00:00Z', '2026-10-19T07:00:00Z', None),
                    (2, 20, 'reply', 'Reply', '2026-10-19T07:01:00Z', '2026-10-19T07:01:00Z', '2026-10-19T07:01:00Z', 1),
                ])
            with TestClient(create_app(path), base_url='http://127.0.0.1:8765') as client:
                login(client)
                response = client.get('/api/analysis', params={'focus_post_id': 2, 'sample_stage': 'total'})
                self.assertEqual(response.status_code, 200, response.text)
                data = response.json()
                self.assertEqual(data['summary']['posts'], 2)
                self.assertEqual(data['replies']['selected_root_id'], 1)
                self.assertEqual({node['post_id'] for node in data['replies']['nodes']}, {1, 2})
                self.assertEqual(data['sampling']['posts_total'], 2)
                self.assertEqual(data['sampling']['selected_stage'], 'total')
                response = client.get('/api/analysis', params={'sample_stage': 'total', 'sample_status': 'eligible'})
                self.assertEqual(response.status_code, 400)
                self.assertIn('不可同时指定', response.json()['error'])
                self.assertEqual(client.get('/api/analysis?focus_post_id=-1').status_code, 400)
                filtered = client.get('/api/analysis?author_id=20&focus_post_id=2').json()
                self.assertEqual(filtered['summary']['posts'], 1)
                self.assertFalse(next(node for node in filtered['replies']['nodes'] if node['post_id'] == 1)['in_scope'])


if __name__ == '__main__':
    unittest.main()
