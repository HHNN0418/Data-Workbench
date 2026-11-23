from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ctn_data import annotations, insights, queries
from ctn_data.db import connect, initialize


def iso(dt):
    return dt.isoformat(timespec='microseconds').replace('+00:00', 'Z')


class InsightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.sqlite3'
        initialize(self.path)
        self.seed()

    def seed(self):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        with closing(connect(self.path)) as conn, conn:
            conn.executemany('INSERT INTO accounts(account_id,username,first_seen,last_seen) VALUES(?,?,?,?)',
                             [(1, 'citizen_a', iso(self.now), iso(self.now)), (2, 'citizen_b', iso(self.now), iso(self.now - timedelta(hours=1))),
                              (5554, 'maya_collins', iso(self.now), iso(self.now)), (9, 'botty', iso(self.now), iso(self.now)),
                              (7, 'DailyKingstonia', iso(self.now), iso(self.now))])
            conn.execute("INSERT INTO team_accounts(account_id,team_label,is_own,evidence,updated_at) VALUES(5554,'Agent Zero',1,'test',?)",
                         (iso(self.now),))
        old, young = self.now - timedelta(hours=3), self.now - timedelta(minutes=30)
        self.post(1, 1, 'I am voting no on this mandate, what about my rights', old, likes=2, replies=1)  # 25 points
        self.post(2, 1, 'Not sure yet, worried about side effects', old, likes=1)                          # 5
        self.post(3, 2, 'Vote yes to protect Kingstonia', old)                                             # 0
        self.post(4, 5554, 'Vote yes: hospitals need breathing room ' + 'x' * 300, old, likes=4, reposts=1)  # 30
        self.post(5, 9, 'Vote yes vote yes', old, likes=10)                                                  # 50 (bot)
        self.post(6, 7, 'News: referendum date set', old, replies=2)                                         # 30 (news)
        self.post(7, 1, 'Still deciding about the vaccine', young, likes=9)                                  # too young
        annotations.refresh_posts(self.path)
        with patch('ctn_data.agent_targeting.behavior_bots', return_value=frozenset({9})), \
                patch('ctn_data.agent_targeting.duplicate_authors', return_value=frozenset({2})):
            annotations.refresh_accounts(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def post(self, post_id, author, body, created, likes=0, replies=0, reposts=0):
        with closing(connect(self.path)) as conn, conn:
            conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen,kind) VALUES(?,?,?,?,?,?,?)',
                         (post_id, author, body, iso(created), iso(created), iso(created), 'post'))
            conn.execute('INSERT INTO counter_snapshots(post_id,observed_at,like_count,reply_count,repost_count,view_count) '
                         'VALUES(?,?,?,?,?,0)', (post_id, iso(self.now - timedelta(minutes=5)), likes, replies, reposts))

    def test_groups_opinion_timing_and_content(self):
        result = insights.build(self.path, hours=24, now=iso(self.now))
        groups = result['own_performance']['groups']
        self.assertEqual((groups['citizens']['posts'], groups['citizens']['median']), (3, 5))
        self.assertEqual((groups['own']['posts'], groups['own']['median']), (1, 30))
        self.assertEqual((groups['bots']['posts'], groups['news']['posts']), (1, 1))
        self.assertEqual(result['own_performance']['accounts'][0]['username'], 'maya_collins')
        self.assertEqual(result['own_performance']['top_posts'][0]['post_id'], 4)
        totals = {group: sum(sum(entry[group].values()) for entry in result['opinion']['timeseries'])
                  for group in insights.GROUPS}
        self.assertEqual(totals, {'citizens': 4, 'own': 1, 'bots': 1, 'news': 1})
        self.assertEqual(result['opinion']['last_6h']['citizens']['posts'], 4)
        hour = (self.now - timedelta(hours=3)).astimezone(insights.SYDNEY).hour
        self.assertEqual(result['timing'][hour]['citizens']['posts'], 3)
        length = {(row['group'], row['value']): row['posts'] for row in result['content']['length']}
        self.assertEqual(length[('own', '>280')], 1)
        stance = {(row['group'], row['value']): row['posts'] for row in result['content']['stance']}
        self.assertEqual(stance[('citizens', 'no')], 1)
        self.assertEqual(result['author_points'][1], 25 + 5 + 45)
        self.assertNotIn('author_points', insights.public(result))

    def test_accounts_rank_filter_and_lean(self):
        points = insights.build(self.path, hours=24, now=iso(self.now))['author_points']
        ranked = queries.accounts(self.path, {'sort': 'engagement'}, points)
        self.assertEqual([item['account_id'] for item in ranked['items']][:2], [1, 9])
        self.assertTrue(ranked['engagement_ready'])
        by_id = {item['account_id']: item for item in ranked['items']}
        self.assertEqual((by_id[1]['posts_24h'], by_id[1]['engagement_72h']), (3, 75))
        self.assertEqual(by_id[9]['bot_reasons'], ['behavior'])
        self.assertTrue(by_id[9]['is_bot'])
        self.assertFalse(by_id[2]['is_bot'])
        self.assertEqual(by_id[1]['stance_counts'].get('no'), 1)
        self.assertEqual(queries.accounts(self.path, {'bots': 'only'})['total'], 1)
        self.assertEqual(queries.accounts(self.path, {'bots': 'exclude'})['total'], 4)
        plain = queries.accounts(self.path, {})
        self.assertFalse(plain['engagement_ready'])
        self.assertIsNone(plain['items'][0]['engagement_72h'])
        self.assertEqual(queries.accounts(self.path, {'sort': 'posts'})['items'][0]['account_id'], 1)
        with self.assertRaises(ValueError):
            queries.accounts(self.path, {'sort': 'bogus'})

    def test_lean_rules(self):
        self.assertIsNone(queries._stance_lean({}))
        self.assertEqual(queries._stance_lean({'yes': 2, 'undecided': 5}), 'undecided')
        self.assertEqual(queries._stance_lean({'yes': 3}), 'yes')
        self.assertEqual(queries._stance_lean({'yes': 1, 'no': 4}), 'no')
        self.assertEqual(queries._stance_lean({'yes': 2, 'no': 2}), 'mixed')


if __name__ == '__main__':
    unittest.main()
