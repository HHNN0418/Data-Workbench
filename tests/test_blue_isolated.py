"""Blue candidates in a child process (keeps ~17 s CPU off the backend) and their stage timing."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ctn_data import blue_reports, blue_view, style_groups
from ctn_data.db import connect, initialize
from tests.test_blue_reports import NOW, memory_db, scenario

STAGES = ('read_own_unavailable', 'behaviour', 'read_posts_accounts', 'texts', 'calibration', 'style_citizens',
          'evidence', 'select_actions', 'clusters_sides', 'result')
STYLE = {'style_group': 'S801', 'group_size': 6, 'group_hidden_accounts': 3, 'possible_teammate': False,
         'linked_to_ours': 0}


class TimingTests(unittest.TestCase):
    def test_every_stage_reports_wall_and_cpu_seconds(self):
        conn = memory_db()
        self.addCleanup(conn.close)
        scenario(conn)
        timing = {}
        blue_reports.candidates(conn, now=NOW, limit=50, timing=timing)
        self.assertEqual([key for key in timing if key in STAGES], list(STAGES))
        self.assertTrue(all(set(timing[key]) == {'wall', 'cpu'} and timing[key]['wall'] >= 0 for key in STAGES))
        self.assertGreater(timing['posts'], 0)

    def test_given_style_evidence_replaces_the_in_memory_cache(self):
        with patch('ctn_data.style_groups.account_evidence', side_effect=AssertionError('memory read')), \
                patch('ctn_data.risk_assessment.account_risk', return_value={}), \
                patch.object(blue_reports.agent_targeting, 'citizen_accounts', return_value=frozenset()):
            found = blue_reports._style(None, 'test.db', {801, 802}, {801: STYLE})
        self.assertEqual(set(found), {801})
        self.assertEqual((found[801]['id'], found[801]['others_hidden'], found[801]['counts']), ('S801', 3, True))

    def test_the_snapshot_holds_only_what_candidates_reads(self):
        full = {801: {**STYLE, 'peers': [1, 2, 3], 'examples': [{'excerpt': 'x' * 160}], 'username': 'u'}}
        with patch.object(style_groups, 'listed_accounts', return_value=[801]), \
                patch.object(style_groups, 'account_evidence', return_value=full) as evidence:
            snapshot = blue_reports.style_snapshot()
        evidence.assert_called_once_with([801])
        self.assertEqual(snapshot, {'801': STYLE})

    def test_listed_accounts_reads_the_cache_and_expires_with_it(self):
        index = {'generated_at': '2026-10-24T05:00:00Z', 'accounts': {9: {}, 3: {}}, 'groups': {}, 'posts': {},
                 'usernames': {}}
        with patch.dict(style_groups._STATE, {'index': index}, clear=True):
            self.assertEqual(style_groups.listed_accounts(now='2026-10-24T06:00:00Z'), [3, 9])
            self.assertEqual(style_groups.listed_accounts(now='2026-10-24T09:00:00Z'), [])
        with patch.dict(style_groups._STATE, {}, clear=True):
            self.assertEqual(style_groups.listed_accounts(), [])


class ChildProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'ctn.sqlite3'
        initialize(self.db)
        conn = connect(self.db)
        try:
            self.ids = scenario(conn)
        finally:
            conn.close()

    def test_the_child_returns_what_candidates_returns_in_process_with_style_evidence(self):
        with patch.object(blue_reports, 'style_snapshot', return_value={'801': STYLE}):
            items, timing = blue_reports.candidates_isolated(self.db, limit=50, now=NOW)
        with patch.object(blue_reports.agent_targeting, 'citizen_accounts', return_value=frozenset()):
            expected = blue_reports.candidates(str(self.db), NOW, limit=50, style_evidence={801: STYLE})
        self.assertEqual([(item['post_id'], item['p'], item['style_group']) for item in items],
                         [(item['post_id'], item['p'], item['style_group']) for item in expected])
        self.assertEqual(items[0]['post_id'], self.ids['a801'])
        self.assertEqual(items[0]['style_group'], 'S801')
        self.assertTrue(timing['isolated'])
        self.assertTrue(set(STAGES) <= set(timing))
        self.assertGreater(timing['child_wall'], 0)
        self.assertGreater(timing['request_bytes'], 0)
        self.assertIn('child_cpu', timing)

    def test_a_failing_child_raises_with_its_last_error_line(self):
        with patch.object(blue_reports, 'style_snapshot', return_value={}):
            with self.assertRaisesRegex(RuntimeError, 'Blue 候选子进程失败'):
                blue_reports.candidates_isolated(Path(self.temp.name), limit=50)

    def test_a_child_over_its_time_is_killed(self):
        with patch.object(blue_reports, 'style_snapshot', return_value={}):
            with self.assertRaisesRegex(RuntimeError, '超过 0.01 秒'):
                blue_reports.candidates_isolated(self.db, limit=50, timeout=0.01)


class BuildTests(unittest.TestCase):
    def test_an_isolated_build_records_each_part_of_the_rebuild(self):
        stages = {'texts': {'wall': 1.0, 'cpu': 1.0}, 'isolated': True}
        checks = {}
        with patch.object(blue_view.blue_reports, 'candidates_isolated', return_value=([], stages)) as child, \
                patch.object(blue_view.blue_reports, 'candidates', side_effect=AssertionError('in process')), \
                patch.object(blue_view.blue_availability, 'check', return_value=checks), \
                patch('ctn_data.blue_submit.submitted', return_value=[]), \
                patch('ctn_data.blue_submit.submitted_today', return_value=[]):
            view = blue_view.build('test.db', limit=20, isolated=True)
        child.assert_called_once_with('test.db', limit=20)
        timing = view['timing']
        self.assertEqual((timing['candidates'], timing['isolated']), (stages, True))
        self.assertTrue({'candidates_seconds', 'availability_seconds', 'accounts_seconds', 'total_seconds',
                         'finished_at'} <= set(timing))

    def test_a_failed_refresh_keeps_the_previous_result_and_says_why(self):
        view = blue_view.BlueCandidates('test.db', isolated=True)
        old = {'candidates': [], 'availability_issues': [], 'timing': {'total_seconds': 3}}
        view._cached[50] = (float('-inf'), old)
        with patch.object(blue_view, 'build', side_effect=RuntimeError('Blue 候选子进程失败（1）：boom')) as build:
            view._refresh(50, 0)
        build.assert_called_once_with('test.db', limit=50, isolated=True)
        self.assertIs(view.peek(), old)
        with patch.object(view, '_refreshing', {50}):
            result = view.get()
        self.assertEqual(result['timing'], {'total_seconds': 3})
        self.assertIn('boom', result['refresh_error']['error'])

    def test_rebuilding_the_cache_after_a_report_keeps_the_timing(self):
        view = blue_view.BlueCandidates('test.db')
        timing = {'total_seconds': 4}
        stamp = '2026-10-24T00:00:00.000000Z'
        item = {'post_id': 1, 'author_id': 55, 'p': 0.9, 'ev': 10, 'cluster_id': 'c1', 'verified': False, 'evidence': [],
                'availability': {'status': 'available', 'checked_at': stamp}, 'availability_checked_at': stamp}
        view._cached[50] = (0.0, blue_view._view([item, {**item, 'post_id': 2}], [], timing))
        view._remove_reported({1}, [])
        self.assertEqual(view.peek()['timing'], timing)


if __name__ == '__main__':
    unittest.main()
