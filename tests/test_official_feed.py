"""Score and exposure readers take the live activity table's newest rows until the CSV export has them."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ctn_data import official_feed, queries, results_view, scoring
from ctn_data.db import connect, initialize
from ctn_data.npc_calibration import PREVIEW_KEY

EXPORTED = ('2026-10-22T11:19:10Z', '+30 points: NPC engagement: 15 views and 3 likes')
LIVE = [  # newest first, as the page lists them
    {'occurred_at': '2026-10-22T13:19:11Z', 'event_type': 'SCORE_NARRATIVE',
     'message': '+72 points: NPC engagement: 12 views, 4 likes, 1 repost and 2 replies'},
    {'occurred_at': '2026-10-22T12:49:10Z', 'event_type': 'MESSAGE_NARRATIVE',
     'message': 'Moderation: your account @maya_collins (id 5554) has been suspended after 3 of its posts were hidden'},
    # Already in the export: never counted twice.
    {'occurred_at': EXPORTED[0], 'event_type': 'SCORE_NARRATIVE', 'message': EXPORTED[1]},
]


class OfficialFeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'test.sqlite3'
        initialize(self.db)
        conn = connect(self.db)
        with conn:
            conn.execute('INSERT INTO official_activity(occurred_at,event_type,message,fingerprint,occurrence,track,'
                         "points_decimal,parse_status) VALUES(?,?,?,?,1,'red','30','parsed')",
                         ('2026-10-22T11:19:10.000000Z', 'SCORE_NARRATIVE', EXPORTED[1], 'exported-1'))
            conn.execute('INSERT INTO meta(key,value) VALUES(?,?)', (PREVIEW_KEY, json.dumps({'rows': LIVE})))
        conn.close()

    def test_only_rows_newer_than_the_export_are_pending(self):
        conn = connect(self.db)
        try:
            pending = official_feed.pending_rows(conn)
        finally:
            conn.close()
        self.assertEqual([(row['occurred_at'], row['track'], row['points_decimal']) for row in pending],
                         [('2026-10-22T12:49:10.000000Z', 'blue', None), ('2026-10-22T13:19:11.000000Z', 'red', '72')])
        self.assertTrue(all(row['preview'] for row in pending))

    def test_score_overview_counts_live_points_once(self):
        result = scoring.score_overview(self.db)
        self.assertEqual(result['parsed_total'], '102')  # 30 exported + 72 live
        self.assertEqual(result['activity']['live'], 2)

    def test_activity_list_leads_with_live_rows(self):
        result = queries.activity(self.db, {})
        self.assertEqual([row.get('preview', False) for row in result['items']], [True, True, False])
        self.assertEqual(result['total'], 3)
        red = queries.activity(self.db, {'track': 'red'})
        self.assertEqual([row['points_decimal'] for row in red['items']], ['72', '30'])
        later = queries.activity(self.db, {'offset': '20'})
        self.assertFalse(any(row.get('preview') for row in later['items']))

    def test_exposure_reports_the_live_hour(self):
        result = results_view.exposure(self.db, now=datetime(2026, 10, 22, 13, 30, tzinfo=timezone.utc))
        hours = {row['end']: row for row in result['hours']}
        live = next(row for row in hours.values() if row['live_reports'])
        self.assertEqual((live['status'], live['views'], live['replies']), ('reported', 12, 2))
        self.assertEqual(result['live_until'], '2026-10-22T13:19:11.000000Z')


class CombinedReportTests(unittest.TestCase):
    def test_engagement_and_moderation_lines_are_counted(self):
        counts, unparsed = results_view.parse_engagement(
            '+156 points: NPC engagement and moderation: 16 views, 2 likes, 4 replies, 1 primary contribution to a '
            'change of mind on vaccination towards neither side and 2 first inauthentic reports')
        self.assertEqual((counts['views'], counts['likes'], counts['replies'], counts['neither']), (16, 2, 4, 1))
        self.assertEqual(unparsed, [])


if __name__ == '__main__':
    unittest.main()
