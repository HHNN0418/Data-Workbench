"""Exposure per official hour and attribution summaries (results pages); read-only."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ctn_data import post_attribution, results_view
from ctn_data.db import connect, initialize

NOW = datetime(2026, 10, 22, 11, 0, tzinfo=timezone.utc)
AGENTS = {'r1': {'id': 'r1', 'name': 'Maya Collins', 'account_id': 5554},
          'r2': {'id': 'r2', 'name': 'Ethan Brooks', 'account_id': 5832}}


def iso(value):
    return value.isoformat(timespec='seconds').replace('+00:00', 'Z')


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'test.sqlite3'
        initialize(self.db)
        self.conn = connect(self.db)
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO meta(key,value) VALUES('agent_control_v1:agents',?)", (json.dumps(AGENTS),))
        self.conn.commit()


class ExposureTests(Fixture):
    def report(self, at, message, points, track='red'):
        self.conn.execute(
            'INSERT INTO official_activity(occurred_at,event_type,message,fingerprint,track,points_decimal,parse_status) '
            "VALUES(?,'SCORE_NARRATIVE',?,?,?,?,'parsed')", (at, message, at + message, track, points))

    def published(self, at, action, post_id=None):
        details = {'action': action, 'post_id': post_id, 'manual': False}
        self.conn.execute("INSERT INTO agent_audit_events(id,agent_id,at,kind,message,draft_id,details_json) "
                          "VALUES(?,?,?,'published','赛事操作执行成功',NULL,?)",
                          (f'e{at}{action}', 'r1', at, json.dumps(details)))

    def setUp(self):
        super().setUp()
        self.report('2026-10-22T03:47:47.000000Z', '+174 points: NPC engagement: 38 views, 6 likes, 7 replies and '
                    '1 contribution to a change of mind towards your side', '174')
        self.report('2026-10-22T06:47:47.000000Z', '+49 points: NPC engagement: 13 views, 1 like, 2 replies, '
                    '1 primary contribution to a change of mind on vaccination towards your side and '
                    '2 contributions to changes of mind on vaccination towards neither side', '49')
        self.report('2026-10-22T07:16:15.000000Z', '+38 points: NPC engagement: 3 views, 1 like and 2 replies', '38')
        self.report('2026-10-22T09:19:11.000000Z', '+99 points: NPC engagement: 24 views, 3 likes, 1 repost, '
                    '2 replies and 1 follow', '99')
        self.report('2026-10-22T09:20:00.000000Z', '-8 points: NPC engagement: 1 contribution to a change of mind '
                    'away from your side', '-8')
        self.report('2026-10-22T08:19:11.000000Z', '+15 points: Moderation: 3 inauthentic reports', '15', 'blue')
        self.report('2026-10-19T09:53:49.000000Z', '+5 points: NPC engagement: 5 views', '5')
        self.conn.execute("INSERT INTO posts(post_id,author_id,kind,body,created_at,first_seen,last_seen) "
                          "VALUES(5001,5554,'post','Clinics open late',?,?,?)", ('2026-10-22T08:00:00Z',) * 3)
        self.conn.execute("INSERT INTO feed_observations(post_id,source,observed_at,rank_position) "
                          "VALUES(5001,'trending','2026-10-22T08:30:00Z',3)")
        self.published('2026-10-22T08:00:00.000000Z', 'post', 5001)
        self.published('2026-10-22T08:30:00.000000Z', 'reply', 5002)
        self.published('2026-10-22T08:31:00.000000Z', 'reply', 5003)
        self.published('2026-10-22T08:10:00.000000Z', 'like', 4000)
        self.published('2026-10-22T07:47:00.000000Z', 'post', 5004)
        self.published('2026-10-22T10:50:00.000000Z', 'post', 5005)
        self.conn.commit()

    def test_parse_engagement_reads_every_item(self):
        counts, unparsed = results_view.parse_engagement(
            '+174 points: NPC engagement: 38 views, 6 likes, 1 repost, 7 replies, 1 follow and 1 contribution to a '
            'change of mind towards your side')
        self.assertEqual(counts, {'views': 38, 'likes': 6, 'reposts': 1, 'replies': 7, 'follows': 1, 'toward': 1,
                                  'away': 0, 'neither': 0, 'primary_toward': 0})
        self.assertEqual(unparsed, [])
        counts, _ = results_view.parse_engagement('-8 points: NPC engagement: 1 contribution to a change of mind '
                                                  'away from your side')
        self.assertEqual(counts['away'], 1)
        self.assertEqual(results_view.parse_engagement('+15 points: Moderation: 3 inauthentic reports')[1],
                         ['+15 points: Moderation: 3 inauthentic reports'])

    def test_official_hours_end_at_47_minutes(self):
        at = datetime(2026, 10, 22, 9, 19, 11, tzinfo=timezone.utc)
        self.assertEqual(results_view.hour_end(at), datetime(2026, 10, 22, 8, 47, tzinfo=timezone.utc))
        self.assertEqual(results_view.hour_end(at.replace(minute=47, second=0)), at.replace(minute=47, second=0))

    def test_reports_and_our_actions_per_official_hour(self):
        result = results_view.exposure(self.db, now=NOW)
        hours = {row['end']: row for row in result['hours']}
        self.assertEqual(len(result['hours']), 49)
        self.assertEqual(result['hours'][0]['end'], '2026-10-22T11:47:00Z')
        self.assertEqual(result['hours'][0]['status'], 'in_progress')
        self.assertEqual(result['hours'][0]['our_posts'], 1)
        # Both :19 reports arrived after the 08:47 hour had ended.
        late = hours['2026-10-22T08:47:00Z']
        self.assertEqual((late['status'], late['reports'], late['points'], late['views'], late['away']),
                         ('reported', 2, 91, 24, 1))
        self.assertEqual((late['our_posts'], late['our_replies'], late['our_other'], late['our_trending']), (1, 2, 1, 1))
        self.assertEqual(late['post_share'], round(1 / 3, 3))
        self.assertEqual(hours['2026-10-22T07:47:00Z']['our_posts'], 1)
        frozen = hours['2026-10-22T06:47:00Z']
        self.assertEqual((frozen['reports'], frozen['views'], frozen['toward'], frozen['primary_toward'],
                          frozen['neither'], frozen['outage']), (2, 16, 1, 1, 2, True))
        self.assertEqual(hours['2026-10-22T05:47:00Z']['status'], 'no_report')
        self.assertTrue(hours['2026-10-22T05:47:00Z']['outage'])
        self.assertFalse(hours['2026-10-22T04:47:00Z']['outage'])
        self.assertEqual(hours['2026-10-22T03:47:00Z']['toward'], 1)
        self.assertEqual(hours['2026-10-22T09:47:00Z']['status'], 'not_imported')
        self.assertEqual(hours['2026-10-22T10:47:00Z']['status'], 'not_imported')
        self.assertNotIn('2026-10-19T09:47:00Z', hours)
        self.assertEqual(result['imported_until'], '2026-10-22T09:20:00Z')
        self.assertTrue(result['read_only'])

    def test_summaries_leave_the_outage_out_of_views_per_original(self):
        result = results_view.exposure(self.db, now=NOW)
        recent, window = result['summary']['recent'], result['summary']['window']
        self.assertEqual(recent['hours'], 6)
        self.assertEqual((window['hours'], window['points'], window['views']), (48, 352, 78))
        self.assertEqual((window['our_posts'], window['our_replies'], window['outage_hours']), (2, 2, 3))
        # Only reported hours without the outage: 08:47 (24 views, 1 post) and 03:47 (38 views, no post).
        self.assertEqual(window['views_per_original'], 62.0)
        self.assertIn('16:00–18:36', result['outage']['note'])


class AttributionTests(Fixture):
    def row(self, post_id, published, action='reply', concern='hospital', agent='r1', reward=None, settled=True,
            npc=0, yes=0, likes=0, depth=2, hour=15, stance='undecided', pipeline='staged'):
        self.conn.execute(
            '''INSERT INTO post_attribution(post_id,agent_id,draft_id,published_at,action,concern,used_radar,fact_ids_json,
                   target_stance,target_bot,thread_depth,body_chars,hour_local,pipeline,captured_at,outcome_at,npc_replies,
                   reply_yes,reply_undecided,reply_no,likes,reposts,reward) VALUES(?,?,?,?,?,?,1,?,?,0,?,200,?,?,?,?,?,?,0,0,?,0,?)''',
            (post_id, agent, f'd{post_id}', iso(published), action, concern, json.dumps(['f1']), stance, depth, hour,
             pipeline, iso(published), iso(published + timedelta(hours=6)) if settled else None,
             npc if settled else None, yes if settled else None, likes if settled else None,
             reward if settled else None))

    def setUp(self):
        super().setUp()
        for index in range(12):
            self.row(100 + index, NOW - timedelta(hours=10, minutes=index), reward=20 if index < 3 else 0,
                     npc=1 if index < 3 else 0, likes=1 if index < 3 else 0)
        self.row(200, NOW - timedelta(hours=8), action='post', concern='tourism', agent='r2', reward=35, npc=1, yes=1,
                 likes=4, depth=None, hour=9, stance=None)
        self.row(201, NOW - timedelta(hours=8, minutes=5), action='post', concern=None, agent='r2', reward=5, likes=1,
                 depth=None, hour=9, stance=None)
        self.row(300, NOW - timedelta(hours=2), settled=False)
        self.row(301, NOW - timedelta(hours=1), settled=False)
        self.row(900, NOW - timedelta(hours=60), reward=100)
        self.conn.commit()

    def test_groups_average_settled_rewards(self):
        result = results_view.attribution(self.db, now=NOW)
        totals = result['totals']
        self.assertEqual((totals['posts'], totals['settled'], totals['pending']), (16, 14, 2))
        self.assertEqual(totals['next_settle_at'], iso(NOW - timedelta(hours=2) + timedelta(hours=6)))
        actions = {group['key']: group for group in result['groups']['action']}
        self.assertEqual((actions['reply']['posts'], actions['reply']['settled']), (14, 12))
        self.assertEqual(actions['reply']['mean_reward'], 5.0)
        self.assertEqual(actions['reply']['success_rate'], 0.25)
        self.assertEqual(actions['post']['mean_reward'], 20.0)
        self.assertEqual(actions['post']['reply_yes'], 1)
        agents = {group['key']: group for group in result['groups']['agent']}
        self.assertEqual(agents['r2']['label'], 'Ethan Brooks')
        self.assertEqual([group['key'] for group in result['groups']['hour']], [9, 15])
        depth = {group['key']: group['posts'] for group in result['groups']['depth']}
        self.assertEqual(depth, {'2': 14, 'none': 2})
        self.assertIn('none', {group['key'] for group in result['groups']['concern']})

    def test_arms_come_from_post_attribution_and_mark_ranked_ones(self):
        result = results_view.attribution(self.db, now=NOW)
        arms = {(arm['concern'], arm['action']): arm for arm in result['arms']}
        expected = post_attribution.arm_stats(self.db, now=NOW)
        self.assertEqual(set(arms), set(expected))
        self.assertTrue(arms[('hospital', 'reply')]['ranked'])
        self.assertFalse(arms[('tourism', 'post')]['ranked'])
        self.assertEqual(arms[('hospital', 'reply')]['successes'], 3)

    def test_latest_lists_settled_rows_newest_first(self):
        result = results_view.attribution(self.db, now=NOW, latest=3)
        self.assertEqual([row['post_id'] for row in result['latest']], [200, 201, 100])
        self.assertEqual(result['latest'][0]['agent_name'], 'Ethan Brooks')
        self.assertEqual(result['latest'][0]['fact_ids'], ['f1'])
        self.assertNotIn(900, [row['post_id'] for row in result['latest']])
        self.assertTrue(result['read_only'])
        self.assertEqual(result['weights'], post_attribution.WEIGHTS)


if __name__ == '__main__':
    unittest.main()
