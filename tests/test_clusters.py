from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from ctn_data import annotations, clusters
from ctn_data.db import connect, initialize
from ctn_data.server import create_app
from tests.auth_helpers import login

SHARED = 'Keeping thirty ICU beds free is crucial for everyone on the island this winter.'


def iso(dt):
    return dt.isoformat(timespec='microseconds').replace('+00:00', 'Z')


class ClusterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.sqlite3'
        initialize(self.path)
        self.seed()

    def tearDown(self):
        self.temp.cleanup()

    def seed(self):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        at = lambda minutes: iso(self.now - timedelta(minutes=minutes))
        rows, post_id = [], 0

        def post(author, body, minutes, kind='post', reply=None, repost=None):
            nonlocal post_id
            post_id += 1
            rows.append((post_id, author, body, at(minutes), at(minutes), at(minutes), kind, reply, repost))
            return post_id
        # Cluster: accounts 1-3 post the same text; 4 boosts them with five reposts.
        shared_ids = [post(author, SHARED, 60 + author) for author in (1, 2, 3)]
        post(1, 'Vote yes on 2 November', 50)
        for minutes in range(5):
            post(4, '', 40 + minutes, kind='repost', repost=shared_ids[minutes % 3])
        # Two accounts sharing a different text stay below the three-account minimum.
        for author in (8, 9):
            post(author, 'A pair of accounts that share a sentence is not yet a cluster at all.', 30)
        # Citizen 20 replies to the cluster and to us; 5554 is ours.
        ours = post(5554, 'Our own original post about the referendum', 20)
        post(20, 'Not sure about this', 10, kind='reply', reply=shared_ids[0])
        post(20, 'What about cost?', 9, kind='reply', reply=ours)
        post(1, 'Replying to the citizen here', 5, kind='reply', reply=post_id - 1)
        with closing(connect(self.path)) as conn, conn:
            conn.executemany('''INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen,kind,
                reply_to_post_id,repost_of_post_id) VALUES(?,?,?,?,?,?,?,?,?)''', rows)
            conn.executemany('INSERT INTO accounts(account_id,username,first_seen,last_seen) VALUES(?,?,?,?)',
                             [(a, f'user{a}', at(0), at(0)) for a in (1, 2, 3, 4, 8, 9, 20, 5554)])
            conn.execute("INSERT INTO team_accounts(account_id,team_label,is_own,evidence,updated_at) VALUES(5554,'Agent Zero',1,'t',?)",
                         (at(0),))
            conn.execute("INSERT INTO feed_observations(post_id,source,observed_at,rank_position) VALUES(?,'trending',?,1)",
                         (shared_ids[0], at(1)))
            conn.execute('''INSERT INTO account_annotations(account_id,is_bot,bot_reasons_json,flagged_at,updated_at)
                VALUES(2,1,'["behavior"]',?,?)''', (at(0), at(0)))
        annotations.refresh_posts(self.path)

    def test_clusters_link_shared_text_and_boosters(self):
        result = clusters.build(self.path, now=iso(self.now))
        self.assertEqual(result['clusters_total'], 1)
        cluster = result['clusters'][0]
        self.assertEqual(sorted(member['account_id'] for member in cluster['members']), [1, 2, 3, 4])
        self.assertEqual(cluster['kinds'], {'post': 4, 'reply': 1, 'repost': 5})
        self.assertEqual((cluster['post_reply_ratio'], result['own']['post_reply_ratio']), (4.0, 1.0))
        self.assertEqual(cluster['trending_share'], 0.25)
        self.assertEqual((cluster['replies_to_citizens_share'], cluster['citizen_replies']), (1.0, 1))
        self.assertEqual(cluster['citizen_points_per_hour'], round(15 / 48, 1))
        self.assertEqual((cluster['bot_flagged'], cluster['bot_reasons']), (1, {'behavior': 1}))
        self.assertEqual(sum(cluster['hourly_writes']), cluster['writes'])
        self.assertEqual(cluster['examples'][0]['why'], 'shared_text')
        self.assertLessEqual(max(len(example['excerpt']) for example in cluster['examples']), 120)
        own = result['own']
        self.assertEqual((own['accounts'], own['citizen_replies'], own['label']), (1, 1, '我们自己（Agent Zero）'))
        self.assertIn('不等于已确认的队伍', result['note'])
        self.assertEqual((cluster['is_own'], own['is_own'], cluster['members_total'], cluster['bot_share']), (False, True, 4, 0.25))
        self.assertEqual(cluster['members'][0]['account_id'], 4)  # most writes first (five reposts)
        self.assertTrue(next(m for m in cluster['members'] if m['account_id'] == 2)['is_bot'])
        self.assertIsNone(cluster['blue_candidates'])
        blue = {'candidates': [{'author_id': 2, 'cluster_id': 'c1'}, {'author_id': 3, 'cluster_id': 'c1'}, {'author_id': 99, 'cluster_id': 'c9'}]}
        linked = clusters.build(self.path, now=iso(self.now), blue=blue)['clusters'][0]
        self.assertEqual((linked['blue_candidates'], linked['blue_cluster_ids']), (2, ['c1']))


class CacheTests(unittest.TestCase):
    def wait_for_refresh(self, cache):
        deadline = time.monotonic() + 5
        while cache._refreshing and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(cache._refreshing)

    def test_stale_result_is_served_while_recomputing_in_background(self):
        results = iter([{'generated_at': 'first'}, {'generated_at': 'second'}, RuntimeError('db down')])

        def fake_build(db_path, *, now=None, blue=None):
            result = next(results)
            if isinstance(result, Exception):
                raise result
            return {**result, 'blue': blue}
        blue = SimpleNamespace(peek=lambda: {'candidates': []})
        age = lambda cache: setattr(cache, '_cached', (cache._cached[0] - clusters.CACHE_SECONDS - 1, cache._cached[1]))
        with patch('ctn_data.clusters.build', side_effect=fake_build):
            cache = clusters.Clusters('unused', blue=blue)
            first = cache.get()
            self.assertEqual((first['generated_at'], first['stale'], first['blue']), ('first', False, {'candidates': []}))
            self.assertEqual(cache.get()['generated_at'], 'first')  # inside ten minutes: no rebuild
            age(cache)
            stale = cache.get()
            self.assertEqual((stale['generated_at'], stale['stale']), ('first', True))
            self.wait_for_refresh(cache)
            self.assertEqual((cache.get()['generated_at'], cache.get()['stale']), ('second', False))
            # A failed recompute keeps the last result, still marked stale.
            age(cache)
            with self.assertLogs('ctn_data.clusters', 'ERROR'):
                stale = cache.get()
                self.wait_for_refresh(cache)
            self.assertEqual((stale['generated_at'], stale['stale']), ('second', True))
            self.assertEqual(cache._cached[1]['generated_at'], 'second')


class EndpointTests(unittest.TestCase):
    def test_endpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'test.sqlite3'
            initialize(db)
            web = Path(temp) / 'dist'
            web.mkdir()
            (web / 'index.html').write_text('<html></html>')
            client = TestClient(create_app(db, backup_dir=Path(temp) / 'backups', web_root=web), base_url='http://127.0.0.1:8765')
            login(client)
            response = client.get('/api/clusters')
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual((data['clusters'], data['stale'], data['own']['accounts']), ([], False, 0))


if __name__ == '__main__':
    unittest.main()
