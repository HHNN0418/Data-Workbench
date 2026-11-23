import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ctn_data import leaderboard
from ctn_data.db import connect, initialize

BOARD = """<table class="board"><thead><tr><th>#</th><th>Team</th><th>Members</th><th>Score</th></tr></thead><tbody>
<tr><td>1</td><td><a href="/user/teams/9">placeholder</a></td><td>4</td><td>44,128</td></tr>
<tr><td>2</td><td>C--</td><td>3</td><td>43,300</td></tr>
<tr><td>39</td><td><a href="/user/teams/46">Agent&nbsp;Zero</a> <span class="badge">5</span></td><td>5</td><td>4,370</td></tr>
</tbody></table>"""
T0 = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def stamp(moment):
    return moment.isoformat().replace('+00:00', 'Z')


class ParseTableTests(unittest.TestCase):
    def test_reads_rank_team_score_and_team_link(self):
        rows = leaderboard.parse_table(BOARD)
        self.assertEqual([(r['rank'], r['team'], r['score'], r['team_id']) for r in rows],
                         [(1, 'placeholder', 44128, 9), (2, 'C--', 43300, None), (39, 'Agent Zero 5', 4370, 46)])

    def test_column_order_change_and_header_row_in_tbody(self):
        html = """<table><tr><th>Points</th><th>Team name</th><th>Pos</th></tr>
        <tr><td>1 234.5</td><td>Beta</td><td>2</td></tr><tr><td>12,345</td><td>Alpha</td><td>1</td></tr></table>"""
        self.assertEqual([(r['rank'], r['team'], r['score']) for r in leaderboard.parse_table(html)],
                         [(2, 'Beta', 1234.5), (1, 'Alpha', 12345)])

    def test_thousands_separators_and_unicode_minus(self):
        html = ('<table><tr><th>Team</th><th>Score</th></tr><tr><td>A</td><td>1,234,567</td></tr>'
                '<tr><td>B</td><td>1\u202f234 pts</td></tr><tr><td>C</td><td>\u221212</td></tr></table>')
        self.assertEqual([r['score'] for r in leaderboard.parse_table(html)], [1234567, 1234, -12])

    def test_tied_ranks(self):
        explicit = """<table><thead><tr><th>Rank</th><th>Team</th><th>Score</th></tr></thead><tbody>
        <tr><td>1</td><td>A</td><td>900</td></tr><tr><td>=2</td><td>B</td><td>800</td></tr>
        <tr><td>T2</td><td>C</td><td>800</td></tr><tr><td></td><td>D</td><td>800</td></tr>
        <tr><td>5</td><td>E</td><td>100</td></tr></tbody></table>"""
        self.assertEqual([r['rank'] for r in leaderboard.parse_table(explicit)], [1, 2, 2, 2, 5])
        # No rank column: equal scores share a competition rank.
        implicit = ('<table><tr><th>Team</th><th>Score</th></tr><tr><td>A</td><td>10</td></tr>'
                    '<tr><td>B</td><td>7</td></tr><tr><td>C</td><td>7</td></tr><tr><td>D</td><td>3</td></tr></table>')
        self.assertEqual([r['rank'] for r in leaderboard.parse_table(implicit)], [1, 2, 2, 4])

    def test_unlabelled_rank_column_colspan_nested_table_and_footer(self):
        html = """<table><thead><tr><th></th><th colspan="2">Team</th><th>Score</th></tr></thead><tbody>
        <tr><td>#1</td><td><img alt=""></td><td>Alpha<table><tr><td>999</td></tr></table></td><td>50</td></tr>
        <tr><td>#2</td><td></td><td>Beta<script>var x = 1;</script></td><td>40</td></tr>
        <tr><td>#3</td><td></td><td>Gamma</td><td>-</td></tr></tbody>
        <tfoot><tr><td></td><td></td><td>Total</td><td>90</td></tr></tfoot></table><table><tr><td>later</td></tr></table>"""
        rows = leaderboard.parse_table(html)
        self.assertEqual([(r['rank'], r['team'], r['score']) for r in rows], [(1, 'Alpha', 50), (2, 'Beta', 40)])

    def test_duplicate_names_are_kept_apart_by_team_link(self):
        html = ('<table><tr><th>Team</th><th>Score</th></tr><tr><td><a href="/user/teams/1">Same</a></td><td>5</td></tr>'
                '<tr><td><a href="/user/teams/2">Same</a></td><td>4</td></tr><tr><td><a href="/user/teams/1">Same</a></td><td>4</td></tr></table>')
        self.assertEqual([r['team'] for r in leaderboard.parse_table(html)], ['Same', 'Same (#2)'])

    def test_unreadable_tables_raise(self):
        for html in ('', '<table><tr><th>Name</th><th>Link</th></tr><tr><td>a</td><td>b</td></tr></table>',
                     '<table><tr><th>Team</th><th>Score</th></tr><tr><td>A</td><td>—</td></tr></table>', None):
            with self.assertRaises(ValueError):
                leaderboard.parse_table(html)


class StoreAndQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'board.db'
        initialize(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def capture(self, data, moment):
        conn = connect(self.db)
        try:
            with conn:
                return leaderboard.capture(conn, data, stamp(moment))
        finally:
            conn.close()

    def put(self, moment, rows):
        conn = connect(self.db)
        try:
            with conn:
                leaderboard.save_snapshot(conn, stamp(moment), [
                    {'rank': rank, 'team': team, 'score': score, 'team_id': team_id} for rank, team, score, team_id in rows])
        finally:
            conn.close()

    def rows(self):
        conn = connect(self.db)
        try:
            return [tuple(row) for row in conn.execute('SELECT observed_at,rank,team,score,team_id FROM leaderboard_snapshots ORDER BY observed_at,rank')]
        finally:
            conn.close()

    def test_capture_stores_a_snapshot_once_per_observation(self):
        data = {'competition_id': 2, 'table_html': BOARD, 'rows': []}
        receipt = self.capture(data, T0)
        self.assertEqual(receipt, {'competition_id': 2, 'parser': 'server', 'teams': 3})
        self.capture(data, T0)
        stored = self.rows()
        self.assertEqual(len(stored), 3)
        self.assertEqual(stored[0], ('2026-10-24T12:00:00.000000Z', 1, 'placeholder', 44128.0, 9))

    def test_unparseable_table_falls_back_to_extension_rows_and_keeps_the_sample(self):
        data = {'competition_id': 2, 'table_html': '<table><tr><th>Who</th><th>How much</th></tr></table>',
                'rows': [{'rank': 1, 'team': 'A', 'score': 10}, {'rank': None, 'team': 'B', 'score': 10}]}
        receipt = self.capture(data, T0)
        self.assertEqual((receipt['parser'], receipt['teams']), ('extension', 2))
        self.assertEqual([(r[1], r[2]) for r in self.rows()], [(1, 'A'), (1, 'B')])
        receipt = self.capture({**data, 'rows': [{'team': 'A', 'score': 'x'}]}, T0 + timedelta(minutes=10))
        self.assertEqual((receipt['parser'], receipt['teams']), (None, 0))
        self.assertIn('Team 或 Score', receipt['parse_error'])
        conn = connect(self.db)
        try:
            failure = json.loads(conn.execute('SELECT value FROM meta WHERE key=?', (leaderboard.FAILURE_KEY,)).fetchone()[0])
        finally:
            conn.close()
        self.assertIn('How much', failure['table_html'])
        self.assertEqual(leaderboard.overview(self.db)['last_failure']['observed_at'], '2026-10-24T12:10:00.000000Z')

    def test_capture_rejects_malformed_records(self):
        for data in ({'competition_id': 3, 'table_html': BOARD, 'rows': []},
                     {'competition_id': 2, 'table_html': 'x' * (300 * 1024 + 1), 'rows': []},
                     {'competition_id': 2, 'table_html': 42, 'rows': []},
                     {'competition_id': 2, 'table_html': BOARD, 'rows': [], 'extra': 1}, 'not a dict'):
            with self.assertRaises(ValueError):
                self.capture(data, T0)
        self.assertEqual(self.rows(), [])

    def test_changes_rates_ranks_and_own_team(self):
        teams = lambda a, b, ours, own_rank: [(1, 'Alpha', a, 1), (2, 'Beta', b, 2), (own_rank, 'Agent Zero', ours, 46)]
        self.put(T0 - timedelta(hours=24, minutes=5), teams(1000, 900, 100, 5))
        self.put(T0 - timedelta(hours=6), teams(1600, 1500, 300, 4))
        self.put(T0 - timedelta(hours=1, minutes=10), teams(1900, 1800, 500, 3))
        self.put(T0 - timedelta(minutes=30), teams(1950, 1850, 520, 3))
        self.put(T0, teams(2000, 1860, 560, 3) + [(4, 'Newcomer', 10, None)])
        board = leaderboard.overview(self.db)
        self.assertEqual(board['status'], 'ok')
        self.assertEqual(board['snapshot']['observed_at'], '2026-10-24T12:00:00.000000Z')
        self.assertEqual((board['snapshot']['teams'], board['snapshot']['snapshots_24h']), (4, 4))
        alpha, beta, ours, new = board['teams']
        self.assertEqual((alpha['change_1h'], alpha['change_6h'], alpha['change_24h']), (100, 400, 1000))
        self.assertEqual(alpha['rate_1h'], round(100 / (70 / 60), 2))
        self.assertEqual(alpha['rate_24h'], round(1000 / (24 + 5 / 60), 2))
        self.assertEqual((alpha['speed_per_hour'], alpha['speed_window']), (alpha['rate_6h'], 6))
        self.assertEqual(alpha['rate_6h'], round(400 / 6, 2))
        self.assertTrue(ours['is_own'])
        self.assertFalse(alpha['is_own'])
        self.assertEqual(ours['rank_change_24h'], 2)
        self.assertEqual((new['change_1h'], new['speed_per_hour'], new['rank_change_24h']), (None, None, None))
        own = board['own']
        self.assertEqual(own['above']['team'], 'Beta')
        self.assertEqual(own['above']['gap'], 1300)
        self.assertIsNone(own['above']['catch_up_hours'])  # Beta is faster
        self.assertEqual(own['first']['team'], 'Alpha')
        self.assertEqual(board['summary']['fastest'][0]['team'], 'Alpha')

    def test_own_team_by_paired_team_id_and_catch_up_time(self):
        self.put(T0 - timedelta(hours=6), [(1, 'Leader', 1000, 7), (2, 'Our Renamed Team', 400, 46)])
        self.put(T0, [(1, 'Leader', 1060, 7), (2, 'Our Renamed Team', 700, 46)])
        conn = connect(self.db)
        try:
            with conn:
                conn.execute("INSERT INTO meta(key,value) VALUES('browser_team_id','46')")
        finally:
            conn.close()
        own = leaderboard.overview(self.db)['own']
        self.assertEqual(own['team'], 'Our Renamed Team')
        self.assertEqual((own['above']['gap'], own['above']['catch_up_hours']), (360, 9))
        self.assertEqual(own['first']['team'], 'Leader')

    def test_windows_without_a_close_baseline_are_unknown(self):
        # Only a snapshot 3 hours back: too old for the 1-hour window, too recent for 6 hours.
        self.put(T0 - timedelta(hours=3), [(1, 'Alpha', 100, None)])
        self.put(T0, [(1, 'Alpha', 160, None)])
        alpha = leaderboard.overview(self.db)['teams'][0]
        self.assertEqual((alpha['change_1h'], alpha['change_6h'], alpha['change_24h'], alpha['speed_per_hour']), (None, None, None, None))

    def test_empty_overview(self):
        board = leaderboard.overview(self.db)
        self.assertEqual((board['status'], board['teams'], board['own']), ('empty', [], None))

    def test_series_for_selected_teams(self):
        for minutes in range(0, 600, 10):
            self.put(T0 - timedelta(hours=50) + timedelta(minutes=minutes), [(1, 'Alpha', minutes, None), (2, 'Beta', 1, None)])
        for minutes in (0, 15, 30):
            self.put(T0 - timedelta(minutes=30 - minutes), [(1, 'Alpha', 1000 + minutes, None), (2, 'Beta', 2, None), (3, 'Gamma', 3, None)])
        result = leaderboard.series(self.db, ['Alpha', 'Gamma', 'Missing', 'Alpha'], hours=48)
        self.assertEqual(list(result), ['Alpha', 'Gamma', 'Missing'])
        self.assertEqual([p['score'] for p in result['Alpha']][-3:], [1000, 1015, 1030])
        self.assertTrue(all(p['at'] >= '2026-10-22T12:00' for p in result['Alpha']))
        self.assertEqual(result['Missing'], [])
        self.assertEqual(len(leaderboard.series(self.db, ['Alpha'], hours=168)['Alpha']), 63)
        with patch.object(leaderboard, 'MAX_SERIES_POINTS', 10):
            points = leaderboard.series(self.db, ['Alpha'], hours=168)['Alpha']
        self.assertLessEqual(len(points), 11)
        self.assertEqual(points[-1]['score'], 1030)

    def test_cached_view_reuses_the_ranking_and_series(self):
        self.put(T0, [(1, 'Alpha', 100, None)])
        view = leaderboard.Leaderboard(self.db, seconds=60)
        with patch.object(leaderboard, 'overview', wraps=leaderboard.overview) as built, \
                patch.object(leaderboard, 'series', wraps=leaderboard.series) as drawn:
            view = leaderboard.Leaderboard(self.db, seconds=60)
            first = view.get(teams='Alpha\n Beta ', hours=24)
            second = view.get(teams='Alpha\n Beta ', hours=24)
            plain = view.get()
        self.assertEqual((built.call_count, drawn.call_count), (1, 1))
        self.assertEqual(first['series']['Alpha'][0]['score'], 100)
        self.assertEqual(list(second['series']), ['Alpha', 'Beta'])
        self.assertNotIn('series', plain)
        self.assertFalse(plain['stale'])


class BrowserCaptureAndApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'capture.db'
        initialize(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_browser_capture_accepts_leaderboard_records_once(self):
        from ctn_data.browser_capture import capture
        # browser_capture refuses observations from the future.
        moment = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
        record = {'id': 'board-record-123', 'kind': 'leaderboard', 'observed_at': stamp(moment),
                  'data': {'competition_id': 2, 'table_html': BOARD, 'rows': []}}
        bad = {**record, 'id': 'board-record-456', 'data': {**record['data'], 'competition_id': 3}}
        result = capture(self.db, [record, bad])
        self.assertEqual(result['accepted'], [record['id']])
        self.assertEqual([(e['id'], e['retryable']) for e in result['errors']], [(bad['id'], False)])
        self.assertEqual(capture(self.db, [record])['accepted'], [record['id']])
        conn = connect(self.db)
        try:
            self.assertEqual(conn.execute('SELECT count(*) FROM leaderboard_snapshots').fetchone()[0], 3)
            receipt = json.loads(conn.execute('SELECT value FROM checkpoints WHERE key=?', ('browser_receipt:' + record['id'],)).fetchone()[0])
        finally:
            conn.close()
        self.assertEqual((receipt['kind'], receipt['teams'], receipt['parser']), ('leaderboard', 3, 'server'))

    def test_leaderboard_endpoint_is_read_only_and_returns_series(self):
        from fastapi.testclient import TestClient
        from ctn_data.server import create_app
        from tests.auth_helpers import login
        conn = connect(self.db)
        try:
            with conn:
                leaderboard.save_snapshot(conn, stamp(T0), [{'rank': 1, 'team': 'Alpha', 'score': 10, 'team_id': None},
                                                            {'rank': 2, 'team': 'Agent Zero', 'score': 5, 'team_id': None}])
        finally:
            conn.close()
        with TestClient(create_app(self.db, backup_dir=Path(self.tmp.name) / 'backups'), base_url='http://127.0.0.1:8765') as client:
            self.assertEqual(client.get('/api/leaderboard').status_code, 401)
            login(client)
            board = client.get('/api/leaderboard').json()
            self.assertEqual([row['team'] for row in board['teams']], ['Alpha', 'Agent Zero'])
            self.assertTrue(board['teams'][1]['is_own'])
            self.assertNotIn('series', board)
            series = client.get('/api/leaderboard', params={'teams': 'Agent Zero\nAlpha', 'hours': 24}).json()
            self.assertEqual(series['series_hours'], 24)
            self.assertEqual(series['series']['Agent Zero'][0]['score'], 5)
            self.assertIn(client.post('/api/leaderboard', json={}).status_code, (403, 405))


if __name__ == '__main__':
    unittest.main()
