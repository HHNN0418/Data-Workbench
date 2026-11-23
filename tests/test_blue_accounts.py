"""Blue by account: candidates grouped per author, and hand-filed account reports once hidden posts are confirmed."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from ctn_data import blue_submit, blue_view
from ctn_data.agent_control import AgentController, AgentError
from ctn_data.db import connect, initialize
from ctn_data.server import create_app
from tests.auth_helpers import login

STAMP = '2026-10-24T00:00:00.000000Z'
BEHAVIOUR = {'rule': 'behaviour_bot_rule', 'log_odds': 2.0, 'answer_rate': 0.9, 'received': 40, 'detail': 'bot rule'}
LATENCY = {'rule': 'fast_answers', 'log_odds': 1.2, 'median_latency_seconds': 30, 'detail': 'fast'}
SHARED = {'rule': 'shared_text_post', 'log_odds': 1.8, 'accounts': 5, 'detail': 'shared text'}


def item(post_id, author, *, p=0.95, cluster='c1', side='no', evidence=(BEHAVIOUR,), hidden=1, actions=150,
         status='available', last_report=None, verified=False):
    return {'post_id': post_id, 'author_id': author, 'username': f'user{author}', 'p': p, 'ev': 20.0,
            'cluster_id': cluster, 'cluster_size': 8, 'side': side, 'side_counts': {'no': 5, 'yes': 0},
            'verified': verified, 'observed_actions': actions, 'created_at': STAMP, 'evidence': list(evidence),
            'suggested_note': 'Post note.', 'availability': {'status': status, 'checked_at': STAMP},
            'availability_checked_at': STAMP, 'author_risk': {'hidden': hidden, 'suspect': 0, 'last_confirmed_at': STAMP},
            'author_account_report': last_report}


def accounts(items, reports_today=()):
    return {row['account_id']: row for row in blue_view._view(list(items), list(reports_today))['accounts']}


class AccountViewTests(unittest.TestCase):
    def test_posts_are_grouped_per_author_with_account_evidence_only(self):
        view = blue_view._view([item(1, 55, evidence=(BEHAVIOUR, SHARED)), item(2, 55, p=0.97, evidence=(LATENCY,)),
                                item(3, 66, side='mixed')], [])
        rows = {row['account_id']: row for row in view['accounts']}
        self.assertEqual(sorted(rows), [55, 66])
        account = rows[55]
        self.assertEqual([post['post_id'] for post in account['posts']], [2, 1])  # plan order
        self.assertEqual(account['p'], 0.97)
        self.assertEqual([evidence['rule'] for evidence in account['evidence']], ['behaviour_bot_rule', 'fast_answers'])
        self.assertTrue(all('author_risk' not in post for post in account['posts']))
        self.assertEqual(account['planned'], 2)
        self.assertIsNone(account['copy_evidence'])
        self.assertEqual(view['account_summary'], {'accounts': 2, 'with_plan': 2, 'account_report_ready': 2,
                                                   'excluded_teammates': 0})

    def test_an_account_report_needs_a_confirmed_hidden_post_and_100_actions(self):
        rows = accounts([item(1, 55), item(2, 66, hidden=0), item(3, 77, actions=99), item(4, 88, p=0.5),
                         item(5, 99, verified=None)])
        ready = rows[55]['account_report']
        self.assertEqual((ready['eligible'], ready['hidden'], ready['actions']), (True, 1, 150))
        self.assertTrue(ready['suggested_note'].startswith(blue_view.ACCOUNT_NOTE_OPENING))
        self.assertIn('answered 36 of the 40 replies', ready['suggested_note'])
        self.assertEqual(rows[66]['account_report']['missing'], ['hidden'])
        self.assertEqual(rows[77]['account_report']['missing'], ['actions'])
        self.assertEqual(rows[88]['account_report']['missing'], ['below_threshold'])
        self.assertEqual(rows[99]['account_report']['missing'], ['verified_or_unknown'])

    def test_an_account_at_the_three_hide_suspension_line_gets_no_account_report(self):
        rows = accounts([item(1, 55, hidden=3), item(2, 66, hidden=2, cluster='c2'), item(3, 77, hidden=4, actions=50, cluster='c3')])
        self.assertEqual(rows[55]['account_report']['missing'], ['suspension_line'])
        self.assertTrue(rows[66]['account_report']['eligible'])
        self.assertEqual(rows[77]['account_report']['missing'], ['suspension_line', 'actions'])

    def test_post_only_evidence_cannot_carry_an_account_report(self):
        rows = accounts([item(1, 55, evidence=(SHARED,))])
        self.assertEqual(rows[55]['account_report']['missing'], ['no_note'])

    def test_a_reported_account_waits_six_hours(self):
        recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace('+00:00', 'Z')
        old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat().replace('+00:00', 'Z')
        rows = accounts([item(1, 55, last_report={'at': recent, 'status': 'submitted'}),
                         item(2, 66, cluster='c2', last_report={'at': old, 'status': 'submitted'}),
                         item(3, 77, cluster='c3', last_report={'at': recent, 'status': 'failed'})])
        self.assertEqual(rows[55]['account_report']['missing'], ['recently_reported'])
        self.assertTrue(rows[66]['account_report']['eligible'])
        self.assertTrue(rows[77]['account_report']['eligible'])

    def test_account_reports_share_the_cluster_s_full_rate_places_with_planned_posts(self):
        today = [{'cluster_id': 'c1', 'post_id': 100 + n, 'author_id': 500 + n} for n in range(4)]
        # Four reports today and one planned post fill c1's five full-rate places.
        rows = accounts([item(1, 55)], today)
        self.assertEqual(rows[55]['account_report']['missing'], ['cluster_daily_cap'])

    def test_yes_side_accounts_come_last_then_most_hidden_and_actions(self):
        view = blue_view._view([item(1, 55, side='yes', hidden=3), item(2, 66, hidden=1, actions=120, cluster='c2'),
                                item(3, 77, hidden=2, cluster='c3'), item(4, 88, hidden=1, actions=400, cluster='c4')], [])
        self.assertEqual([row['account_id'] for row in view['accounts']], [77, 88, 66, 55])

    def test_posts_awaiting_access_confirmation_stay_with_their_account(self):
        rows = accounts([item(1, 55), item(2, 55, status='unverified')])
        posts = {post['post_id']: post['availability_issue'] for post in rows[55]['posts']}
        self.assertEqual(posts, {1: False, 2: True})

    def test_build_attaches_confirmed_hidden_posts_and_the_last_account_report(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'test.sqlite3'
            initialize(db)
            conn = connect(db)
            with conn:
                conn.executemany("INSERT INTO post_visibility(post_id,account_id,created_at,status,first_404_at,confirmed_at,"
                                 "last_checked_at,last_http) VALUES(?,?,?,?,?,?,?,?)",
                                 [(10, 55, STAMP, 'hidden', STAMP, STAMP, STAMP, 404),
                                  (11, 55, STAMP, 'suspect', STAMP, None, STAMP, 404)])
            conn.close()
            blue_submit._append(db, {'target': 'account', 'post_id': None, 'author_id': 55, 'status': 'submitted',
                                     'at': '2026-10-20T00:00:00Z', 'day': '2026-10-20'})
            raw = [{key: value for key, value in item(1, 55).items() if not key.startswith('author_') or key == 'author_id'}]
            checks = {1: {'status': 'available', 'http_status': 200, 'error': '', 'checked_at': STAMP}}
            with patch.object(blue_view.blue_reports, 'candidates', return_value=raw), \
                    patch.object(blue_view.blue_availability, 'check', return_value=checks):
                view = blue_view.build(db)
        account = view['accounts'][0]
        self.assertEqual((account['account_report']['hidden'], account['account_report']['suspect']), (1, 1))
        self.assertEqual(account['last_account_report'], {'at': '2026-10-20T00:00:00Z', 'day': '2026-10-20',
                                                          'status': 'submitted'})
        self.assertTrue(account['account_report']['eligible'])


class FakeController:
    def __init__(self, error=None):
        self.store = SimpleNamespace(read=lambda: {'agents': {
            'simon': {'id': 'simon', 'account_id': blue_submit.REPORTER_ACCOUNT_ID, 'account_state': 'ready'}}})
        self.calls, self.error = [], error

    def report_account(self, agent_id, account_id, reason, detail):
        self.calls.append((agent_id, account_id, reason, detail))
        if self.error:
            raise self.error
        return {'id': 88, 'status': 'pending', 'extra': 'dropped'}


class SubmitAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'test.sqlite3'
        initialize(self.db)
        self.hide(10, 55)
        self.view = blue_view._view([item(1, 55), item(2, 66, hidden=0, cluster='c2')], [])

    def hide(self, post_id, account_id, status='hidden'):
        conn = connect(self.db)
        with conn:
            conn.execute("INSERT INTO post_visibility(post_id,account_id,created_at,status,first_404_at,confirmed_at,"
                         "last_checked_at,last_http) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET "
                         "status=excluded.status", (post_id, account_id, STAMP, status, STAMP, STAMP, STAMP, 404))
        conn.close()

    def test_files_a_ready_account_once_from_the_reporter_account(self):
        controller = FakeController()
        entry = blue_submit.submit_account(self.db, controller, self.view, 55)
        note = self.view['accounts'][0]['account_report']['suggested_note']
        self.assertEqual(controller.calls, [('simon', 55, 'inauthentic', note)])
        self.assertEqual((entry['target'], entry['post_id'], entry['author_id'], entry['hidden'], entry['status']),
                         ('account', None, 55, 1, 'submitted'))
        self.assertEqual(entry['response'], {'id': 88, 'status': 'pending'})
        self.assertEqual([e['author_id'] for e in blue_submit.submitted_today(self.db)], [55])
        with self.assertRaisesRegex(ValueError, '6 小时内已经举报过'):
            blue_submit.submit_account(self.db, controller, self.view, 55)
        self.assertEqual(len(controller.calls), 1)

    def test_refuses_accounts_that_are_not_ready_or_not_candidates(self):
        controller = FakeController()
        with self.assertRaisesRegex(ValueError, '还不满足账号举报条件：hidden'):
            blue_submit.submit_account(self.db, controller, self.view, 66)
        with self.assertRaisesRegex(ValueError, '不在当前的 Blue 候选里'):
            blue_submit.submit_account(self.db, controller, self.view, 77)
        with self.assertRaisesRegex(ValueError, '账号 ID 无效'):
            blue_submit.submit_account(self.db, controller, self.view, 0)
        self.assertEqual(controller.calls, [])

    def test_hidden_posts_are_read_again_before_filing(self):
        self.hide(10, 55, status='visible')
        controller = FakeController()
        with self.assertRaisesRegex(ValueError, '已经没有确认隐藏的帖子'):
            blue_submit.submit_account(self.db, controller, self.view, 55)
        self.assertEqual(controller.calls, [])

    def test_a_platform_failure_is_logged_and_can_be_retried(self):
        with self.assertRaises(AgentError):
            blue_submit.submit_account(self.db, FakeController(AgentError('HTTP 409: already reported', 409)), self.view, 55)
        failed = blue_submit.today(self.db)[0]
        self.assertEqual((failed['target'], failed['status'], failed['error']), ('account', 'failed', 'HTTP 409: already reported'))
        self.assertEqual(blue_submit.submit_account(self.db, FakeController(), self.view, 55)['status'], 'submitted')

    def test_an_account_report_does_not_block_reporting_its_posts(self):
        blue_submit.submit_account(self.db, FakeController(), self.view, 55)
        plan = {'plan': {'items': [self.view['plan']['items'][0]]}}
        controller = MagicMock()
        controller.store.read.return_value = FakeController().store.read()
        controller.report_post.return_value = {'id': 1}
        with patch('ctn_data.blue_availability.check',
                   return_value={1: {'status': 'available', 'http_status': 200, 'error': '', 'checked_at': STAMP}}):
            self.assertEqual(blue_submit.submit(self.db, controller, plan, 1)['target'], 'post')


class ControllerAccountReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.controller = AgentController(Path(temporary.name) / 'test.db', Path(temporary.name) / 'secrets',
                                          provider_store=SimpleNamespace(get=lambda _: {}), clock=lambda: 1792450000.0)
        self.addCleanup(self.controller.stop)
        self.controller.save({'name': 'Simon', 'username': 'simon_mercer', 'stance': 'Supports Yes.'})
        self.agent_id = self.controller.snapshot()['agents'][0]['id']
        self.controller.store.change(lambda data, conn: data['agents'][self.agent_id].update(
            account_id=6503, account_state='ready'))

    def test_report_goes_through_the_account_and_is_recorded(self):
        account = MagicMock()
        account.report_account.return_value = {'id': 6}
        with patch.object(self.controller, '_sdk'), patch.object(self.controller, '_account', return_value=account):
            self.assertEqual(self.controller.report_account(self.agent_id, 55, 'inauthentic', 'Account note.'), {'id': 6})
        account.report_account.assert_called_once_with(55, 'inauthentic', 'Account note.')
        events = [e for e in self.controller.store.read()['events'] if e.get('kind') == 'report']
        self.assertEqual(events[0]['details'], {'target_account_id': 55, 'reason': 'inauthentic'})

    def test_bad_requests_never_reach_the_platform(self):
        account = MagicMock()
        with patch.object(self.controller, '_sdk'), patch.object(self.controller, '_account', return_value=account):
            for args in ((55, 'hate_speech', 'x'), (0, 'inauthentic', 'x'), (55, 'inauthentic', '   ')):
                with self.assertRaises(AgentError):
                    self.controller.report_account(self.agent_id, *args)
        account.report_account.assert_not_called()


class EndpointTests(unittest.TestCase):
    def test_an_account_report_needs_the_dialog_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'test.sqlite3'
            initialize(db)
            client = TestClient(create_app(db, backup_dir=Path(temp) / 'backups'), base_url='http://127.0.0.1:8765')
            headers = {'X-CTN-Token': login(client)}
            with patch('ctn_data.blue_submit.submit_account') as submit, \
                    patch('ctn_data.blue_view.BlueCandidates.invalidate') as invalidate:
                for body in ({'account_id': 55}, {'account_id': 55, 'confirmed': False}, {'account_id': 0, 'confirmed': True}):
                    self.assertEqual(client.post('/api/blue/report-account', json=body, headers=headers).status_code, 400)
                submit.assert_not_called()
                submit.return_value = {'target': 'account', 'author_id': 55, 'status': 'submitted'}
                with patch('ctn_data.blue_view.BlueCandidates.get', return_value={'accounts': []}):
                    response = client.post('/api/blue/report-account', json={'account_id': 55, 'confirmed': True},
                                           headers=headers)
                self.assertEqual((response.status_code, response.json()['status']), (200, 'submitted'))
                submit.assert_called_once()
                invalidate.assert_called_once()


if __name__ == '__main__':
    unittest.main()
