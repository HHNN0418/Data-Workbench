"""P1-2 effect attribution: features at publish time, settled reactions, Thompson-ordered priorities."""
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ctn_data import agent_targeting, post_attribution
from ctn_data.db import connect, initialize

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def at(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec='seconds').replace('+00:00', 'Z')


class AttributionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'ctn.sqlite3')
        initialize(self.db)
        agent_targeting._cache.clear()
        conn = connect(self.db)
        with conn:
            conn.execute('INSERT INTO team_accounts VALUES(?,?,?,?,?)', (900, 'Agent Zero', 1, 'manual', at(30)))
            # The citizen's post we replied to, our reply (post 50) and our original post (post 60).
            self.post(conn, 10, 101, "I'm unsure, will the national ID card track my health?", 9)
            self.post(conn, 50, 900, 'The certificate is separate from the ID card. Voting Yes.', 8, parent=10)
            self.post(conn, 60, 900, 'A full hospital is the real risk. Voting Yes.', 8)
            # Reactions to our reply: two citizens (one leaning yes), one of ours, and one too late.
            self.post(conn, 51, 101, "Okay, that helps. I'm voting yes now.", 7, parent=50)
            self.post(conn, 52, 102, 'Still not sure about this.', 6, parent=50)
            self.post(conn, 53, 900, 'Glad to help.', 6, parent=50)
            self.post(conn, 54, 103, 'Late reply, vote yes.', 1, parent=50)
            conn.execute('INSERT INTO counter_snapshots(post_id,observed_at,like_count,reply_count,repost_count) '
                         'VALUES(?,?,?,?,?)', (50, at(4), 3, 2, 1))
        conn.close()
        self.state = {'drafts': {
            'r': {'id': 'r', 'agent_id': 'maya', 'state': 'published', 'action': 'reply', 'post_id': 50,
                  'target_post_id': 10, 'concern': 'id_privacy', 'fact_ids': ['certificate_separate'],
                  'body': 'x' * 120, 'published_at': at(8), 'pipeline': 'staged', 'research_brief_id': 'b'},
            'p': {'id': 'p', 'agent_id': 'leah', 'state': 'published', 'action': 'post', 'post_id': 60,
                  'concern': 'hospital', 'body': 'y' * 80, 'published_at': at(8), 'pipeline': 'staged'},
            'like': {'id': 'l', 'agent_id': 'leah', 'state': 'published', 'action': 'like', 'post_id': None},
            'pending': {'id': 'q', 'agent_id': 'leah', 'state': 'pending', 'action': 'post', 'post_id': 61}}}

    def tearDown(self):
        agent_targeting._cache.clear()
        self.tmp.cleanup()

    def post(self, conn, post_id, author, body, hours, parent=None):
        conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen,reply_to_post_id) '
                     'VALUES(?,?,?,?,?,?,?)', (post_id, author, body, at(hours), at(hours), at(hours), parent))

    def rows(self):
        conn = connect(self.db)
        try:
            return {row['post_id']: dict(row) for row in conn.execute('SELECT * FROM post_attribution')}
        finally:
            conn.close()

    def test_capture_records_features_once(self):
        self.assertEqual(post_attribution.capture(self.db, self.state, now=NOW), 2)
        self.assertEqual(post_attribution.capture(self.db, self.state, now=NOW), 0)
        reply = self.rows()[50]
        self.assertEqual((reply['action'], reply['concern'], reply['used_radar']), ('reply', 'id_privacy', 1))
        self.assertEqual((reply['target_author_id'], reply['target_stance'], reply['target_bot']), (101, 'undecided', 0))
        self.assertEqual(reply['thread_depth'], 0)
        self.assertEqual(reply['hour_local'], (NOW - timedelta(hours=8)).astimezone(post_attribution.LOCAL).hour)
        self.assertIsNone(self.rows()[60]['target_post_id'])

    def test_settle_counts_citizen_reactions_within_six_hours(self):
        post_attribution.capture(self.db, self.state, now=NOW)
        self.assertEqual(post_attribution.settle(self.db, now=NOW), 2)
        reply = self.rows()[50]
        # Two citizens within 6 h (ours and the late one excluded); one leaned yes; 3 likes and 1 repost.
        self.assertEqual((reply['npc_replies'], reply['reply_yes'], reply['likes'], reply['reposts']), (2, 1, 3, 1))
        self.assertEqual(reply['reward'], 15 * 2 + 10 * 1 + 5 * 3 + 20 * 1)
        self.assertEqual(self.rows()[60]['reward'], 0)
        self.assertEqual(post_attribution.settle(self.db, now=NOW), 0)

    def test_settle_ignores_heavy_volume_repliers(self):
        # Account 102 writes like a competitor (over CITIZEN_MAX_WRITES in the window, by real time).
        recent = datetime.now(timezone.utc)
        conn = connect(self.db)
        with conn:
            for index in range(agent_targeting.CITIZEN_MAX_WRITES + 1):
                stamp = (recent - timedelta(minutes=index)).isoformat(timespec='seconds').replace('+00:00', 'Z')
                conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen) '
                             'VALUES(?,?,?,?,?,?)', (1000 + index, 102, 'spam', stamp, stamp, stamp))
        conn.close()
        post_attribution.capture(self.db, self.state, now=NOW)
        post_attribution.settle(self.db, now=NOW)
        reply = self.rows()[50]
        self.assertEqual((reply['npc_replies'], reply['reply_yes']), (1, 1))

    def test_capture_and_settle_use_the_shared_citizen_rule(self):
        from unittest import mock
        # Only 102 counts as a citizen: 101's reply and the target flag follow citizen_accounts.
        with mock.patch.object(agent_targeting, 'citizen_accounts',
                               side_effect=lambda db, authors, own=(), **_: frozenset(set(authors) & {102})) as rule:
            post_attribution.capture(self.db, self.state, now=NOW)
            post_attribution.settle(self.db, now=NOW)
        self.assertTrue(rule.called)
        reply = self.rows()[50]
        self.assertEqual((reply['target_bot'], reply['npc_replies'], reply['reply_yes']), (1, 1, 0))

    def test_enrich_adds_measured_results_and_reorders_with_enough_data(self):
        conn = connect(self.db)
        with conn:
            for index in range(24):
                concern, success = ('hospital', index < 10) if index < 12 else ('id_privacy', index < 13)
                conn.execute('''INSERT INTO post_attribution(post_id,agent_id,published_at,action,concern,fact_ids_json,
                    captured_at,outcome_at,reward) VALUES(?,?,?,?,?,?,?,?,?)''',
                    (1000 + index, 'a', at(10), 'reply', concern, '[]', at(10), at(4), 30 if success else 0))
        conn.close()
        context = {'kind': 'concern_radar', 'priorities': [{'concern': 'id_privacy'}, {'concern': 'hospital'},
                                                           {'concern': 'cost'}]}
        result = post_attribution.enrich(self.db, context, rng=random.Random(1), now=NOW)
        self.assertEqual([p['concern'] for p in result['priorities']], ['hospital', 'id_privacy', 'cost'])
        self.assertEqual(result['priorities'][0]['measured']['reply']['posts'], 12)
        self.assertEqual(result['priorities_order'], 'thompson')
        self.assertNotIn('measured', result['priorities'][2])
        # Too little data: the radar order stays.
        few = {'kind': 'concern_radar', 'priorities': [{'concern': 'cost'}, {'concern': 'freedom'}]}
        self.assertNotIn('priorities_order', post_attribution.enrich(self.db, few, now=NOW))
        self.assertIsNone(post_attribution.enrich(self.db, None))

    def test_arm_stats_are_json_serialisable_when_postgres_returns_decimals(self):
        # PostgreSQL returns SUM(bigint) as numeric; the radar evidence must still serialise.
        import json
        from decimal import Decimal
        from unittest import mock

        row = {'concern': 'hospital', 'action': 'reply', 'posts': 3, 'successes': Decimal(2),
               'mean_reward': Decimal('17.5'), 'npc_replies': Decimal(14), 'reply_yes': Decimal(4)}
        fake = mock.MagicMock()
        fake.execute.return_value.fetchall.return_value = [row]
        with mock.patch.object(post_attribution, 'connect', return_value=fake):
            stats = post_attribution.arm_stats(self.db, now=NOW)
        json.dumps(list(stats.values()))
        self.assertEqual(stats[('hospital', 'reply')]['npc_replies'], 14)


if __name__ == '__main__':
    unittest.main()
