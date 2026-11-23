"""NPC calibration: official hourly NPC reply counts against who replied to us."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from ctn_data import agent_targeting, blue_reports, npc_calibration
from ctn_data.browser_capture import _save_preview
from ctn_data.db import connect, initialize
from ctn_data.server import create_app
from tests.auth_helpers import login

# Before the 22 Oct outage, so no hour is excluded for it.
T0 = datetime(2026, 10, 21, 3, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat(timespec='seconds').replace('+00:00', 'Z')


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'test.sqlite3'
        initialize(self.db)
        for cache in (npc_calibration._cache, agent_targeting._cache):
            cache.clear()
            self.addCleanup(cache.clear)
        self.next_id = 5000
        conn = connect(self.db)
        with conn:
            conn.execute("INSERT INTO team_accounts VALUES(70,'Agent Zero',1,'test',?)", (iso(T0),))
            self.root = self.post(conn, 70, T0 - timedelta(hours=3, minutes=30))
            # Hour 1 (official 0 replies): 801 mid-hour is certainly not an NPC; 802 is too close to the edge.
            self.post(conn, 801, T0 - timedelta(hours=2, minutes=30), parent=self.root)
            self.post(conn, 802, T0 - timedelta(hours=2, minutes=1), parent=self.root)
            # Hour 2 (official 1): 801 is known, so 803 must be the NPC.
            self.post(conn, 801, T0 - timedelta(hours=1, minutes=40), parent=self.root)
            self.post(conn, 803, T0 - timedelta(hours=1, minutes=20), parent=self.root)
            # Hour 3 (official 2): 803 plus one of 804/805, undecidable.
            for author, minutes in ((803, 50), (804, 40), (805, 30)):
                self.post(conn, author, T0 - timedelta(minutes=minutes), parent=self.root)
            for end, message in ((T0 - timedelta(hours=2), '+5 points: NPC engagement: 5 views'),
                                 (T0 - timedelta(hours=1), '+20 points: NPC engagement: 2 views and 1 reply'),
                                 (T0, '+33 points: NPC engagement: 3 views and 2 replies'),
                                 # 10 minutes after the last one: covers part of an hour only.
                                 (T0 + timedelta(minutes=10), '+1 points: NPC engagement: 1 view')):
                self.official(conn, end, message)
        conn.close()

    def post(self, conn, author, at, parent=None, body='honestly not sure about this yet'):
        self.next_id += 1
        conn.execute('INSERT INTO posts(post_id,author_id,reply_to_post_id,body,created_at,first_seen,last_seen) '
                     'VALUES(?,?,?,?,?,?,?)', (self.next_id, author, parent, body, iso(at), iso(at), iso(at)))
        return self.next_id

    def official(self, conn, at, message):
        self.next_id += 1
        conn.execute('INSERT INTO official_activity(occurred_at,event_type,message,fingerprint,occurrence,track,'
                     "parse_status) VALUES(?,?,?,?,1,'red','parsed')", (iso(at), 'SCORE_NARRATIVE', message, str(self.next_id)))


class EngagementTests(unittest.TestCase):
    def test_reads_every_count_and_skips_reports(self):
        counts = npc_calibration.engagement(
            '+156 points: NPC engagement and moderation: 16 views, 2 likes, 4 replies, 1 primary contribution to a '
            'change of mind on vaccination towards neither side and 2 first inauthentic reports')
        self.assertEqual(counts, {'views': 16, 'likes': 2, 'replies': 4, 'mind': 1})
        self.assertEqual(npc_calibration.engagement('+38 points: NPC engagement: 3 views, 1 like and 1 reply')['replies'], 1)
        self.assertEqual(npc_calibration.engagement('+52 points: Moderation (×0.5 points): 4 first inauthentic reports'), {})


class LabelTests(Fixture):
    def test_zero_hours_and_propagation_label_authors(self):
        conn = connect(self.db)
        try:
            hours, team = npc_calibration.bags(conn)
        finally:
            conn.close()
        self.assertEqual([hour['flag'] for hour in hours], ['clean', 'clean', 'clean', 'irregular'])
        self.assertIn(70, team)
        labels, contradictions = npc_calibration.hard_labels(hours)
        self.assertEqual({author: label for author, (label, _) in labels.items()}, {801: 0, 803: 1})
        self.assertEqual(contradictions, [])
        self.assertEqual(npc_calibration.certain_non_npcs(self.db), frozenset({801}))

    def test_certain_non_npcs_feed_the_citizen_rule_and_blue(self):
        self.assertIn('calibrated', agent_targeting.non_citizen_reasons(self.db, [801, 803]).get(801, []))
        self.assertNotIn(803, agent_targeting.non_citizen_reasons(self.db, [801, 803]))
        p, support = blue_reports.precision(['official_non_npc'])
        self.assertEqual(support, 1)
        self.assertAlmostEqual(p, 0.9, places=2)  # exempt from the single-family cap
        self.assertLessEqual(blue_reports.precision(['self_repeat_strong'])[0], blue_reports.SINGLE_FAMILY_CAP)

    def test_snapshot_scores_the_rule_on_clean_hours(self):
        snap = npc_calibration.snapshot(self.db, now=T0 + timedelta(minutes=20))
        self.assertEqual((snap['hours_total'], snap['hours_clean']), (4, 3))
        live = snap['metrics']['all']
        # Every replier passes the rule here, so it over-counts hour 1 and 2.
        self.assertEqual((live['hours'], live['official_total'], live['predicted_total']), (3, 3, 7))
        self.assertEqual(snap['labels']['non_npc'], 1)
        self.assertEqual([row['account_id'] for row in snap['labels']['misjudged_citizens']], [801])
        self.assertEqual(snap['variants'][0]['key'], 'live')
        self.assertTrue(any(item['key'] == 'with_single_issue' and item['candidate'] for item in snap['variants']))
        self.assertIsNone(snap['alert'])  # fewer than 6 clean hours: too few to alert on

    def test_live_table_rows_fill_in_after_the_export(self):
        conn = connect(self.db)
        with conn:
            count = _save_preview(conn, [
                {'when': '2026/10/21 15:19:10', 'event': '[SCORE_NARRATIVE] +30 points: NPC engagement: 15 views and 3 likes'},
                {'when': '2026/10/21 13:00:00', 'event': '[SCORE_NARRATIVE] +33 points: NPC engagement: 3 views and 2 replies'},
                {'when': '2026/10/21 15:00:00', 'event': '[TEAMCODE] code shared'}], iso(T0 + timedelta(hours=1)))
        self.assertEqual(count, 2)
        # Extension 1.3.5 sends each row's data-utc time instead.
        utc = _save_preview(conn, [{'when_utc': '2026-10-21T04:19:10Z', 'event': '[SCORE_NARRATIVE] +30 points: NPC engagement: 15 views and 3 likes'},
                                   {'when_utc': '2026-10-21T02:00:00Z', 'event': '[SCORE_NARRATIVE] +33 points: NPC engagement: 3 views and 2 replies'}],
                            iso(T0 + timedelta(hours=1)))
        self.assertEqual(utc, 2)
        stored = json.loads(conn.execute('SELECT value FROM meta WHERE key=?', (npc_calibration.PREVIEW_KEY,)).fetchone()[0])
        self.assertEqual(stored['rows'][0], {'occurred_at': '2026-10-21T04:19:10Z', 'event_type': 'SCORE_NARRATIVE',
                                             'message': '+30 points: NPC engagement: 15 views and 3 likes'})
        reports = npc_calibration.red_reports(conn)
        conn.close()
        # The 13:00 Sydney row duplicates the exported T0 report and is ignored; 15:19 is new.
        self.assertEqual([iso(end) for end, _ in reports][-2:], ['2026-10-21T03:10:00Z', '2026-10-21T04:19:10Z'])
        conn = connect(self.db)
        try:
            with self.assertRaises(ValueError):
                _save_preview(conn, [{'when': 'yesterday', 'event': '[SCORE_NARRATIVE] x'}], iso(T0))
        finally:
            conn.close()


class SignalTests(Fixture):
    """Likes, reposts and follows join replies (D70)."""

    def setUp(self):
        super().setUp()
        conn = connect(self.db)
        with conn:
            # Hour 2 also scores 1 like, 1 follow; hour 3 also 1 repost.
            conn.execute("UPDATE official_activity SET message=? WHERE occurred_at=?",
                         ('+45 points: NPC engagement: 2 views, 1 like, 1 reply and 1 follow', iso(T0 - timedelta(hours=1))))
            conn.execute("UPDATE official_activity SET message=? WHERE occurred_at=?",
                         ('+43 points: NPC engagement: 3 views, 1 repost and 2 replies', iso(T0)))
            # Hour 1 scores no likes or follows: 901 and 902 are certainly not NPCs.
            self.like(conn, 901, T0 - timedelta(hours=2, minutes=30))
            self.follow(conn, 902, T0 - timedelta(hours=2, minutes=20))
            # Hour 2: one unknown liker for one official like, which likes can never prove.
            self.like(conn, 907, T0 - timedelta(hours=1, minutes=30))
            self.follow(conn, 909, T0 - timedelta(hours=1, minutes=25))
            # Hour 3: the only repost of our content fills the official repost.
            self.next_id += 1
            conn.execute('INSERT INTO posts(post_id,author_id,repost_of_post_id,created_at,first_seen,last_seen) '
                         'VALUES(?,?,?,?,?,?)', (self.next_id, 908, self.root, *(iso(T0 - timedelta(minutes=35)),) * 3))
        conn.close()

    def like(self, conn, account, at):
        conn.execute('INSERT INTO post_likes(post_id,account_id,liked_at,first_seen,last_seen) VALUES(?,?,?,?,?)',
                     (self.root, account, iso(at), iso(at), iso(at)))

    def follow(self, conn, account, at):
        conn.execute('INSERT INTO account_followers(account_id,follower_id,followed_at,first_seen,last_seen) '
                     'VALUES(70,?,?,?,?)', (account, iso(at), iso(at), iso(at)))

    def labels(self):
        conn = connect(self.db)
        try:
            hours, _ = npc_calibration.bags(conn)
        finally:
            conn.close()
        sources = {}
        labels, contradictions = npc_calibration.hard_labels(hours, sources)
        return {author: label for author, (label, _) in labels.items()}, sources, contradictions

    def test_likes_prove_only_non_npcs_and_reposts_prove_npcs(self):
        labels, sources, contradictions = self.labels()
        self.assertEqual((labels[901], sources[901]), (0, 'likes'))
        self.assertEqual((labels[902], sources[902]), (0, 'follows'))
        self.assertNotIn(907, labels)
        self.assertEqual((labels[908], sources[908]), (1, 'reposts'))
        self.assertEqual((labels[801], labels[803]), (0, 1))
        self.assertEqual(contradictions, [])
        self.assertEqual(npc_calibration.certain_npcs(self.db), frozenset({803, 908}))

    def test_follows_prove_npcs_once_every_team_account_was_read_after_the_hour(self):
        self.assertNotIn(909, self.labels()[0])
        conn = connect(self.db)
        with conn:
            for account in npc_calibration.team_accounts(conn):
                conn.execute('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)',
                             ('followers:' + str(account), json.dumps({'read_at_epoch': T0.timestamp()}), iso(T0)))
        conn.close()
        labels, sources, _ = self.labels()
        self.assertEqual((labels[909], sources[909]), (1, 'follows'))

    def test_snapshot_counts_labels_by_kind(self):
        snap = npc_calibration.snapshot(self.db, now=T0 + timedelta(minutes=20))
        self.assertEqual(snap['labels']['by_kind']['likes'], {'non_npc': 1, 'npc': 0})
        self.assertEqual(snap['labels']['by_kind']['reposts'], {'non_npc': 0, 'npc': 1})
        self.assertEqual({row['account_id'] for row in snap['labels']['newest_npc']}, {803, 908})
        hour = next(row for row in snap['hours'] if row['end'] == iso(T0 - timedelta(hours=1)))
        self.assertEqual((hour['official_likes'], hour['likes_to_us'], hour['follows_of_us']), (1, 1, 1))


class RouteTests(Fixture):
    def test_calibration_and_prompt_usage_routes(self):
        client = TestClient(create_app(self.db), base_url='http://127.0.0.1:8765')
        self.addCleanup(client.close)
        login(client)
        self.addCleanup(client.app.state.agent_control.stop)
        response = client.get('/api/accounts/calibration')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['labels']['non_npc'], 1)
        # The unused model reading prompt is no longer listed (D67).
        prompts = client.get('/api/agents/facts/prompts').json()['prompts']
        self.assertNotIn('reading', [prompt['key'] for prompt in prompts])


if __name__ == '__main__':
    unittest.main()
