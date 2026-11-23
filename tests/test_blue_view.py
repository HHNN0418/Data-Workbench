from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, call, patch

from fastapi.testclient import TestClient

from ctn_data import blue_availability, blue_view
from ctn_data.db import initialize
from ctn_data.server import create_app
from ctn_data.transport import BudgetExceeded, TransportError
from tests.auth_helpers import login


class ReportNoteTests(unittest.TestCase):
    def test_note_uses_plain_reasons_strongest_first(self):
        evidence = [
            {'rule': 'fast_answers', 'log_odds': 1.2, 'median_latency_seconds': 32.3},
            {'rule': 'behaviour_bot_rule', 'log_odds': 2.0, 'answer_rate': 0.882, 'received': 93, 'deep_posts': 51},
            {'rule': 'deep_post', 'log_odds': 0.3, 'depth': 12},
            {'rule': 'answer_latency', 'log_odds': 0.0, 'latency_seconds': 20},
        ]
        note = blue_view.report_note(evidence)
        self.assertEqual(note, 'I think that this content is inauthentic (bot) activity because the account answered 82 of '
                               'the 93 replies it received within 72 h; its median reply time is 32 seconds.')
        self.assertNotIn('agent_targeting', note)
        many = [{'rule': 'self_repeat', 'log_odds': 0.8, 'repeats': n} for n in range(3, 40)]
        self.assertLessEqual(len(blue_view.report_note(many)), blue_view.MAX_NOTE_CHARS)
        self.assertIsNone(blue_view.report_note([{'rule': 'deep_post', 'log_odds': 0.3}]))


class AvailabilityTests(unittest.TestCase):
    def test_checks_each_public_post_once_and_keeps_uncertainty_separate(self):
        transport = MagicMock()
        transport.get_json.side_effect = [
            {'post_id': 1}, {'post_id': 999}, None,
            TransportError('HTTP 403', status=403), TransportError('HTTP 404', status=404),
            TransportError('HTTP 410', status=410), TransportError('HTTP 429', status=429),
            TransportError('HTTP 503', status=503), TimeoutError('timed out'),
        ]
        with patch.object(blue_availability, 'connect') as connect, \
                patch.object(blue_availability, 'ReadOnlyTransport', return_value=transport) as factory:
            result = blue_availability.check('test.db', [*range(1, 10), 1], deadline_seconds=10)
        self.assertEqual({post_id: row['status'] for post_id, row in result.items()},
                         {1: 'available', 2: 'unverified', 3: 'unverified', 4: 'unverified',
                          5: 'unavailable', 6: 'unavailable', 7: 'unverified', 8: 'unverified', 9: 'unverified'})
        self.assertEqual([result[post_id]['http_status'] for post_id in range(1, 10)],
                         [200, 200, 200, 403, 404, 410, 429, 503, None])
        self.assertTrue(all(row['checked_at'] for row in result.values()))
        self.assertEqual(result[1]['error'], '')
        self.assertIn('timed out', result[9]['error'])
        self.assertEqual(transport.get_json.call_args_list,
                         [call('social', f'/api/posts/{post_id}', entity_id=post_id) for post_id in range(1, 10)])
        factory.assert_called_once_with(connect.return_value, timeout=5, max_retries=0,
                                        request_budget=9, deadline_seconds=10, max_wait_seconds=0, actor='blue-check')
        connect.return_value.close.assert_called_once()
        connect.return_value.execute.assert_not_called()

    def test_empty_input_does_not_open_a_connection_or_send_requests(self):
        with patch.object(blue_availability, 'connect') as connect, \
                patch.object(blue_availability, 'ReadOnlyTransport') as factory:
            self.assertEqual(blue_availability.check('test.db', []), {})
        connect.assert_not_called()
        factory.assert_not_called()

    def test_exhausted_budget_leaves_posts_unverified(self):
        transport = MagicMock()
        transport.get_json.side_effect = BudgetExceeded('collection request or time budget exhausted')
        with patch.object(blue_availability, 'connect'), \
                patch.object(blue_availability, 'ReadOnlyTransport', return_value=transport):
            result = blue_availability.check('test.db', [1, 2])
        self.assertTrue(all(row['status'] == 'unverified' and row['http_status'] is None
                            for row in result.values()))


class BuildTests(unittest.TestCase):
    def test_only_confirmed_available_and_never_submitted_posts_enter_candidates_and_plan(self):
        stamp = '2026-10-24T00:00:00.000000Z'
        items = [{'post_id': post_id, 'evidence': [{'rule': 'self_repeat', 'repeats': 4}], 'p': 0.9}
                 for post_id in range(1, 7)]
        checks = {post_id: {'status': status, 'http_status': code, 'error': '', 'checked_at': stamp}
                  for post_id, status, code in [(1, 'available', 200), (2, 'unavailable', 404),
                                                (3, 'unavailable', 410), (4, 'unverified', 403),
                                                (5, 'unverified', 429)]}
        with patch.object(blue_view.blue_reports, 'candidates', return_value=items), \
                patch.object(blue_view.blue_availability, 'check', return_value=checks) as check, \
                patch('ctn_data.blue_submit.submitted', return_value=[{'post_id': 6, 'day': '2026-10-23'}]), \
                patch('ctn_data.blue_submit.submitted_today', return_value=[]) as today, \
                patch.object(blue_view.blue_reports, 'daily_plan', side_effect=lambda rows, _: {'items': rows}) as plan:
            result = blue_view.build('test.db')
        check.assert_called_once_with('test.db', [1, 2, 3, 4, 5])
        self.assertEqual([row['post_id'] for row in result['candidates']], [1])
        self.assertEqual([row['post_id'] for row in result['plan']['items']], [1])
        self.assertEqual([row['post_id'] for row in result['availability_issues']], [2, 3, 4, 5])
        issue = result['availability_issues'][0]
        self.assertEqual(issue['availability'], checks[2])
        self.assertEqual(issue['platform_url'], 'https://social.legitreal.com/twoot/2')
        self.assertEqual(issue['local_url'], '/posts?post=2')
        self.assertIn('4 times', issue['suggested_note'])
        self.assertEqual(issue['availability_checked_at'], stamp)
        self.assertEqual(result['candidates'][0]['availability_checked_at'], stamp)
        self.assertEqual(result['availability_checked_at'], stamp)
        self.assertEqual(result['availability_summary'], {'available': 1, 'unavailable': 2, 'unverified': 2})
        self.assertIn('保留在下方待核对建议', result['caveats'][-1])
        self.assertEqual(plan.call_args.args[0], result['candidates'])
        today.assert_called_once_with('test.db')


class RecheckTests(unittest.TestCase):
    def setUp(self):
        self.view = blue_view.BlueCandidates('test.db')
        submitted = patch('ctn_data.blue_submit.submitted', return_value=[])
        self.submitted = submitted.start()
        self.addCleanup(submitted.stop)
        today = patch('ctn_data.blue_submit.submitted_today', return_value=[])
        today.start()
        self.addCleanup(today.stop)
        self.stamp = '2026-10-24T00:00:00.000000Z'
        self.next_stamp = '2026-10-24T00:01:00.000000Z'

    def item(self, post_id, status='available', *, error=''):
        availability = {'status': status, 'http_status': 200 if status == 'available' else 404,
                        'error': error, 'checked_at': self.stamp}
        return {'post_id': post_id, 'author_id': 100 + post_id, 'p': 0.95, 'ev': 20,
                'cluster_id': f'a{post_id}', 'verified': False, 'username': f'account_{post_id}',
                'evidence': [], 'suggested_note': 'Report evidence.',
                'platform_url': f'https://social.legitreal.com/twoot/{post_id}',
                'local_url': f'/posts?post={post_id}', 'availability': availability,
                'availability_checked_at': self.stamp}

    def seed(self, limit, items):
        self.view._cached[limit] = (blue_view.time.monotonic(), blue_view._view(items, []))

    def test_one_post_check_recovers_issues_in_every_related_cache(self):
        self.seed(50, [self.item(1, 'unavailable'), self.item(2)])
        self.seed(10, [self.item(1, 'unverified'), self.item(3)])
        self.seed(20, [self.item(4)])
        unrelated = self.view.peek(20)
        verification = {'status': 'available', 'http_status': 200, 'error': '', 'checked_at': self.next_stamp}
        with patch.object(blue_view.blue_availability, 'check', return_value={1: verification}) as check, \
                patch.object(blue_view.blue_reports, 'candidates') as candidates:
            result = self.view.recheck(1)
        check.assert_called_once_with('test.db', [1], deadline_seconds=10)
        candidates.assert_not_called()
        self.assertEqual(result['availability'], verification)
        self.assertEqual(result['post_id'], 1)
        self.assertFalse(result['view']['stale'])
        self.assertEqual({row['post_id'] for row in result['view']['candidates']}, {1, 2})
        self.assertEqual({row['post_id'] for row in result['view']['plan']['items']}, {1, 2})
        self.assertEqual(result['view']['availability_issues'], [])
        self.assertEqual(self.view.peek(10)['availability_issues'], [])
        self.assertEqual({row['post_id'] for row in self.view.peek(10)['candidates']}, {1, 3})
        self.assertIs(self.view.peek(20), unrelated)
        unchanged = next(row for row in result['view']['candidates'] if row['post_id'] == 2)
        self.assertEqual(unchanged['availability_checked_at'], self.stamp)

    def test_failed_recheck_moves_a_normal_candidate_to_issues_and_updates_its_reason(self):
        self.seed(50, [self.item(1), self.item(2)])
        first = {'status': 'unverified', 'http_status': 503, 'error': 'HTTP 503', 'checked_at': self.next_stamp}
        second = {**first, 'status': 'unavailable', 'http_status': 404, 'error': 'HTTP 404',
                  'checked_at': '2026-10-24T00:02:00.000000Z'}
        with patch.object(blue_view.blue_availability, 'check', side_effect=[{1: first}, {1: second}]) as check:
            result = self.view.recheck(1)
            self.assertEqual([row['post_id'] for row in result['view']['candidates']], [2])
            self.assertEqual([row['post_id'] for row in result['view']['plan']['items']], [2])
            self.assertEqual(result['view']['availability_issues'][0]['availability'], first)
            result = self.view.recheck(1)
        self.assertEqual(check.call_args_list, [call('test.db', [1], deadline_seconds=10)] * 2)
        self.assertEqual(len(result['view']['availability_issues']), 1)
        self.assertEqual(result['view']['availability_issues'][0]['availability'], second)
        self.assertEqual(result['view']['availability_issues'][0]['availability_checked_at'], second['checked_at'])

    def test_unknown_posts_and_invalid_ids_or_limits_never_send_a_get(self):
        self.seed(50, [self.item(1)])
        with patch.object(blue_view.blue_availability, 'check') as check:
            for post_id, limit in ((0, 50), (True, 50), ('1', 50), (1, 0), (1, True), (1, 201),
                                   (999, 50), (1, 10)):
                with self.subTest(post_id=post_id, limit=limit), self.assertRaises(ValueError):
                    self.view.recheck(post_id, limit)
        check.assert_not_called()

    def test_a_successful_historical_report_is_removed_without_another_get(self):
        self.seed(50, [self.item(1, 'unavailable'), self.item(2)])
        self.submitted.return_value = [{'post_id': 1, 'day': '2026-10-23'}]
        with patch.object(blue_view.blue_availability, 'check') as check, \
                self.assertRaisesRegex(ValueError, '已经举报过'):
            self.view.recheck(1)
        check.assert_not_called()
        self.assertEqual([row['post_id'] for row in self.view.peek()['candidates']], [2])
        self.assertEqual(self.view.peek()['availability_issues'], [])

    def test_a_report_submitted_during_the_get_is_not_restored_to_suggestions(self):
        self.seed(50, [self.item(1, 'unavailable'), self.item(2)])
        self.submitted.side_effect = [[], [{'post_id': 1, 'day': '2026-10-24'}]]
        verification = {'status': 'available', 'http_status': 200, 'error': '', 'checked_at': self.next_stamp}
        with patch.object(blue_view.blue_availability, 'check', return_value={1: verification}), \
                self.assertRaisesRegex(ValueError, '已经举报过'):
            self.view.recheck(1)
        self.assertEqual([row['post_id'] for row in self.view.peek()['candidates']], [2])
        self.assertEqual(self.view.peek()['availability_issues'], [])

    def test_an_older_background_refresh_cannot_overwrite_a_manual_recheck(self):
        self.seed(50, [self.item(1, 'unavailable')])
        _, result = self.view._cached[50]
        self.view._cached[50] = (float('-inf'), result)
        started, release = threading.Event(), threading.Event()

        def slow_build(*args, **kwargs):
            started.set()
            release.wait(5)
            return result

        verification = {'status': 'available', 'http_status': 200, 'error': '', 'checked_at': self.next_stamp}
        with patch.object(blue_view, 'build', side_effect=slow_build), \
                patch.object(blue_view.blue_availability, 'check', return_value={1: verification}):
            self.assertTrue(self.view.get()['stale'])
            self.assertTrue(started.wait(5))
            try:
                self.view.recheck(1)
            finally:
                release.set()
            for _ in range(500):
                if not self.view._refreshing:
                    break
                threading.Event().wait(0.01)
        self.assertFalse(self.view._refreshing)
        self.assertEqual(self.view.peek()['availability_issues'], [])
        self.assertEqual(self.view.peek()['candidates'][0]['availability'], verification)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'test.sqlite3'
        initialize(self.db)
        web = Path(self.temp.name) / 'dist'
        web.mkdir()
        (web / 'index.html').write_text('<html></html>')
        self.client = TestClient(create_app(self.db, backup_dir=Path(self.temp.name) / 'backups', web_root=web),
                                 base_url='http://127.0.0.1:8765')
        login(self.client)

    def tearDown(self):
        self.temp.cleanup()

    def test_empty_database_is_read_only_and_empty(self):
        response = self.client.get('/api/blue/candidates')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual((data['candidates'], data['submits_reports'], data['stale']), ([], False, False))
        self.assertEqual(data['availability_issues'], [])
        self.assertTrue(data['day'] and data['caveats'])
        self.assertEqual(self.client.get('/api/blue/candidates?limit=0').status_code, 400)
        self.assertEqual(data['plan']['items'], [])
        self.assertIn(self.client.post('/api/blue/candidates').status_code, (403, 405))

    def test_stale_result_is_served_while_refreshing_in_background(self):
        view = blue_view.BlueCandidates(self.db)
        item = {'post_id': 7, 'evidence': [{'rule': 'self_repeat', 'log_odds': 0.8, 'repeats': 4}], 'p': 0.9}
        started, release = threading.Event(), threading.Event()
        calls = []

        def fake_candidates(db_path, limit, timing=None):
            calls.append(limit)
            if len(calls) > 1:
                started.set()
                release.wait(5)
            return [dict(item)]
        with patch('ctn_data.blue_reports.candidates', side_effect=fake_candidates), \
                patch.object(blue_view.blue_availability, 'check',
                             return_value={7: {'status': 'available', 'http_status': 200, 'error': '',
                                               'checked_at': '2026-10-24T00:00:00.000000Z'}}), \
                patch('ctn_data.blue_reports.daily_plan', return_value={'items': []}):
            first = view.get()
            item0 = first['candidates'][0]
            self.assertEqual(item0['platform_url'], 'https://social.legitreal.com/twoot/7')
            self.assertEqual(item0['local_url'], '/posts?post=7')
            self.assertEqual(item0['suggested_reason'], 'Inauthentic (bot) / Spam')
            self.assertIn('4 times', item0['suggested_note'])
            with patch.object(blue_view, 'CACHE_SECONDS', 0):
                stale = view.get()
                self.assertTrue(stale['stale'])
                self.assertTrue(started.wait(5))
                view.get()  # a second stale read does not start another refresh
                release.set()
                for _ in range(500):
                    if not view._refreshing:
                        break
                    threading.Event().wait(0.01)
            self.assertFalse(view._refreshing)
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
