"""Hand-filed Blue reports: only planned posts, each once, every attempt logged (D58)."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from capturethenarrative import APIError, RateLimited
from fastapi.testclient import TestClient

from ctn_data import blue_submit
from ctn_data.agent_control import AgentController, AgentError
from ctn_data.agent_budget import Deferred
from ctn_data.db import connect, initialize
from ctn_data.server import create_app
from tests.auth_helpers import login

NOTE = 'I think that this content is inauthentic (bot) activity because the account answered 31 of the 32 replies.'
PLAN = {'plan': {'items': [{'post_id': 901, 'author_id': 55, 'username': 'spammy', 'cluster_id': 'c1', 'p': 0.99,
                            'side': 'yes', 'suggested_note': NOTE}]}}
AVAILABLE = {'status': 'available', 'http_status': 200, 'error': '', 'checked_at': '2026-10-24T00:00:00Z'}


class FakeController:
    def __init__(self, agents=None, error=None):
        self.store = SimpleNamespace(read=lambda: {'agents': agents if agents is not None else {
            'simon': {'id': 'simon', 'account_id': blue_submit.REPORTER_ACCOUNT_ID, 'account_state': 'ready'}}})
        self.calls, self.error = [], error

    def report_post(self, agent_id, post_id, reason, detail):
        self.calls.append((agent_id, post_id, reason, detail))
        if self.error:
            raise self.error
        return {'id': 77, 'status': 'pending', 'extra': 'dropped'}


class SubmitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'test.sqlite3'
        initialize(self.db)
        checker = patch('ctn_data.blue_availability.check', return_value={901: dict(AVAILABLE)})
        self.check = checker.start()
        self.addCleanup(checker.stop)

    def test_files_a_planned_post_once_from_the_reporter_account(self):
        controller = FakeController()
        entry = blue_submit.submit(self.db, controller, PLAN, 901)
        self.assertEqual(controller.calls, [('simon', 901, 'inauthentic', NOTE)])
        self.assertEqual((entry['status'], entry['response'], entry['account_id']), ('submitted', {'id': 77, 'status': 'pending'}, 6503))
        self.assertEqual([e['post_id'] for e in blue_submit.submitted_today(self.db)], [901])
        with self.assertRaisesRegex(ValueError, '已经举报过'):
            blue_submit.submit(self.db, controller, PLAN, 901)
        self.assertEqual(len(controller.calls), 1)

    def test_refuses_posts_outside_the_plan_or_without_a_reporter(self):
        with self.assertRaisesRegex(ValueError, '不在今天的 Blue 举报计划里'):
            blue_submit.submit(self.db, FakeController(), PLAN, 902)
        with self.assertRaisesRegex(ValueError, '没有可用的角色'):
            blue_submit.submit(self.db, FakeController(agents={}), PLAN, 901)
        weak = {'plan': {'items': [{**PLAN['plan']['items'][0], 'suggested_note': None}]}}
        with self.assertRaisesRegex(ValueError, '证据'):
            blue_submit.submit(self.db, FakeController(), weak, 901)
        self.assertEqual(blue_submit.log(self.db), [])

    def test_a_platform_failure_is_logged_and_can_be_retried(self):
        with self.assertRaises(AgentError):
            blue_submit.submit(self.db, FakeController(error=AgentError('赛事平台拒绝了请求', 400)), PLAN, 901)
        (failed,) = blue_submit.today(self.db)
        self.assertEqual((failed['status'], failed['error']), ('failed', '赛事平台拒绝了请求'))
        self.assertEqual(blue_submit.submitted_today(self.db), [])
        self.assertEqual(blue_submit.submit(self.db, FakeController(), PLAN, 901)['status'], 'submitted')
        self.assertEqual([e['status'] for e in blue_submit.today(self.db)], ['submitted', 'failed'])

    def test_preflight_stops_unavailable_or_unverified_posts_and_logs_the_reason(self):
        for status, code, detail in (('unavailable', 404, 'HTTP 404'), ('unavailable', 410, 'HTTP 410'),
                                     ('unverified', 403, 'HTTP 403'), ('unverified', 429, 'HTTP 429'),
                                     ('unverified', None, 'TimeoutError')):
            with self.subTest(status=status, http_status=code):
                controller = FakeController()
                self.check.return_value = {901: {**AVAILABLE, 'status': status, 'http_status': code, 'error': detail}}
                with self.assertRaisesRegex(AgentError, '本次未提交举报') as error:
                    blue_submit.submit(self.db, controller, PLAN, 901)
                self.assertIn(detail, str(error.exception))
                self.assertEqual(controller.calls, [])
                self.assertEqual(blue_submit.today(self.db)[0]['availability']['status'], status)
        self.assertEqual(blue_submit.submitted_today(self.db), [])

    def test_each_submit_gets_a_fresh_check_even_with_a_checked_plan(self):
        controller = FakeController()
        checked_plan = {'plan': {'items': [{**PLAN['plan']['items'][0], 'availability_checked_at': AVAILABLE['checked_at']}]}}
        blue_submit.submit(self.db, controller, checked_plan, 901)
        self.check.assert_called_once_with(self.db, [901], deadline_seconds=10)
        self.assertEqual(blue_submit.today(self.db)[0]['availability'], AVAILABLE)

    def test_reports_from_an_earlier_day_cannot_be_submitted_again(self):
        blue_submit._append(self.db, {'post_id': 901, 'status': 'submitted', 'day': '2026-10-01',
                                     'at': '2026-10-01T01:00:00Z'})
        controller = FakeController()
        with self.assertRaisesRegex(ValueError, '已经举报过'):
            blue_submit.submit(self.db, controller, PLAN, 901)
        self.check.assert_not_called()
        self.assertEqual(controller.calls, [])

    def test_history_includes_earlier_dates_failures_and_server_pagination(self):
        for day, status in (('2026-10-01', 'submitted'), ('2026-10-02', 'failed'), ('2026-10-03', 'submitted')):
            blue_submit._append(self.db, {'post_id': 901, 'day': day, 'at': day + 'T01:00:00Z',
                                         'status': status, 'error': 'HTTP 404' if status == 'failed' else ''})
        page = blue_submit.history(self.db, limit=1, offset=1)
        self.assertEqual((page['total'], page['limit'], page['offset']), (3, 1, 1))
        self.assertEqual((page['reports'][0]['day'], page['reports'][0]['error']), ('2026-10-02', 'HTTP 404'))
        self.assertEqual([e['day'] for e in blue_submit.history(self.db)['reports']],
                         ['2026-10-03', '2026-10-02', '2026-10-01'])
        self.assertEqual(blue_submit.history(self.db, offset=5)['reports'], [])

    def test_history_keeps_records_beyond_the_old_thousand_attempt_limit(self):
        entries = [{'post_id': n, 'day': '2026-10-01', 'at': '2026-10-01T01:00:00Z',
                    'status': 'submitted'} for n in range(1, 1001)]
        conn = connect(self.db)
        try:
            with conn:
                conn.execute('INSERT INTO meta(key,value) VALUES(?,?)',
                             (blue_submit.SUBMITTED_KEY, json.dumps(entries)))
        finally:
            conn.close()
        blue_submit._append(self.db, {'post_id': 1001, 'day': '2026-10-02', 'at': '2026-10-02T01:00:00Z',
                                     'status': 'failed', 'error': 'HTTP 404'})
        self.assertEqual(blue_submit.history(self.db)['total'], 1001)
        self.assertEqual(blue_submit.log(self.db)[0]['post_id'], 1)

    def test_history_rejects_invalid_pagination(self):
        for values in ({'limit': 0}, {'limit': 101}, {'limit': True}, {'offset': -1}, {'offset': True}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                blue_submit.history(self.db, **values)


class ControllerReportTests(unittest.TestCase):
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
        account.report_post.return_value = {'id': 5}
        with patch.object(self.controller, '_sdk'), patch.object(self.controller, '_account', return_value=account):
            self.assertEqual(self.controller.report_post(self.agent_id, 901, 'inauthentic', NOTE), {'id': 5})
        account.report_post.assert_called_once_with(901, 'inauthentic', NOTE)
        events = [e for e in self.controller.store.read()['events'] if e.get('kind') == 'report']
        self.assertEqual(len(events), 1)

    def test_platform_error_reaches_the_report_log_without_truncation(self):
        message = 'Report already filed. ' + 'Original platform explanation. ' * 15
        account = MagicMock()
        account.report_post.side_effect = APIError('POST', 'https://social.legitreal.com/api/posts/901/report',
                                                  409, json.dumps({'error': message}))
        with patch.object(self.controller, '_sdk'), patch.object(self.controller, '_account', return_value=account), \
                patch('ctn_data.blue_availability.check', return_value={901: dict(AVAILABLE)}):
            with self.assertRaises(AgentError) as error:
                blue_submit.submit(self.controller.db_path, self.controller, PLAN, 901)
        self.assertEqual(error.exception.status_code, 502)
        self.assertEqual(str(error.exception), f'赛事平台操作失败（HTTP 409）：{message}')
        self.assertEqual(blue_submit.today(self.controller.db_path)[0]['error'], str(error.exception))
        account.report_post.assert_called_once_with(901, 'inauthentic', NOTE)

    def test_bad_requests_never_reach_the_platform(self):
        account = MagicMock()
        with patch.object(self.controller, '_sdk'), patch.object(self.controller, '_account', return_value=account):
            for args in ((901, 'spam', NOTE), (0, 'inauthentic', NOTE), (901, 'inauthentic', '   ')):
                with self.assertRaises(AgentError):
                    self.controller.report_post(self.agent_id, *args)
        account.report_post.assert_not_called()


class RemoteFailureTests(unittest.TestCase):
    def setUp(self):
        self.controller = AgentController.__new__(AgentController)

    def test_http_failure_keeps_the_status_and_platform_explanation(self):
        for status in (400, 401, 403, 404, 409, 422, 500):
            with self.subTest(status=status):
                exc = APIError('POST', 'https://social.legitreal.com/api/posts/901/report', status,
                               '{"error":"Original platform explanation"}')
                error = self.controller._remote_failure(exc)
                self.assertEqual(error.status_code, 502)
                self.assertEqual(str(error), f'赛事平台操作失败（HTTP {status}）：Original platform explanation')

    def test_non_http_failure_keeps_the_exception_type_and_message(self):
        for exc in (TimeoutError('timed out'), TimeoutError(), URLError('DNS lookup failed'),
                    RuntimeError('connection reset by peer')):
            with self.subTest(exception=type(exc).__name__, message=str(exc)):
                error = self.controller._remote_failure(exc)
                self.assertEqual(error.status_code, 502)
                self.assertIn(type(exc).__name__, str(error))
                if str(exc):
                    self.assertIn(str(exc), str(error))

    def test_existing_agent_errors_are_unchanged(self):
        original = AgentError('角色正在运行', 409)
        self.assertIs(self.controller._remote_failure(original), original)

    def test_quota_errors_keep_wait_metadata_and_the_platform_message(self):
        exc = RateLimited('POST', 'https://social.legitreal.com/api/posts/901/report', 429,
                          '{"error":"Report rate limit exceeded"}', 17)
        error = self.controller._remote_failure(exc)
        self.assertEqual((error.status_code, error.retry_after, error.wait_kind), (429, 17, 'quota'))
        self.assertIn('HTTP 429', str(error))
        self.assertIn('Report rate limit exceeded', str(error))
        local = Deferred('report', 9, '请等待额度恢复')
        error = self.controller._remote_failure(local)
        self.assertEqual(str(error), str(local))
        self.assertEqual((error.status_code, error.retry_after, error.quota_bucket), (429, 9, 'report'))


class EndpointTests(unittest.TestCase):
    def test_recheck_requires_login_and_only_checks_the_requested_post(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'test.sqlite3'
            app = create_app(db, backup_dir=Path(temp) / 'backups')
            client = TestClient(app, base_url='http://127.0.0.1:8765')
            token = client.get('/api/bootstrap').json()['csrf_token']
            headers = {'X-CTN-Token': token}
            self.assertEqual(client.post('/api/blue/recheck', json={'post_id': 901}, headers=headers).status_code, 401)
            headers = {'X-CTN-Token': login(client)}
            result = {'post_id': 901, 'availability': AVAILABLE, 'view': PLAN}
            with patch('ctn_data.blue_view.BlueCandidates.recheck', return_value=result) as recheck, \
                    patch('ctn_data.blue_submit.submit') as submit:
                response = client.post('/api/blue/recheck', json={'post_id': 901}, headers=headers)
                self.assertEqual((response.status_code, response.json()), (200, result))
                recheck.assert_called_once_with(901, limit=50)
                submit.assert_not_called()
                for body in ({'post_id': 0}, {'post_id': 901, 'limit': 0}, {'post_id': 901, 'confirmed': True}):
                    self.assertEqual(client.post('/api/blue/recheck', json=body, headers=headers).status_code, 400)
                recheck.assert_called_once()

    def test_a_report_needs_the_dialog_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'test.sqlite3'
            initialize(db)
            web = Path(temp) / 'dist'
            web.mkdir()
            (web / 'index.html').write_text('<html></html>')
            client = TestClient(create_app(db, backup_dir=Path(temp) / 'backups', web_root=web),
                                base_url='http://127.0.0.1:8765')
            token = login(client)
            headers = {'X-CTN-Token': token}
            self.assertEqual(client.get('/api/blue/reports').json()['reports'], [])
            with patch('ctn_data.blue_submit.submit') as submit:
                self.assertEqual(client.post('/api/blue/report', json={'post_id': 901}, headers=headers).status_code, 400)
                self.assertEqual(client.post('/api/blue/report', json={'post_id': 901, 'confirmed': False},
                                             headers=headers).status_code, 400)
                submit.assert_not_called()
                submit.return_value = {'post_id': 901, 'status': 'submitted'}
                with patch('ctn_data.blue_view.BlueCandidates.get', return_value=PLAN):
                    response = client.post('/api/blue/report', json={'post_id': 901, 'confirmed': True}, headers=headers)
                self.assertEqual((response.status_code, response.json()['status']), (200, 'submitted'))
                submit.assert_called_once()

    def test_history_endpoint_keeps_earlier_dates_and_requires_a_member(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'test.sqlite3'
            initialize(db)
            app = create_app(db, backup_dir=Path(temp) / 'backups')
            client = TestClient(app, base_url='http://127.0.0.1:8765')
            self.assertEqual(client.get('/api/blue/reports/history').status_code, 401)
            login(client)
            blue_submit._append(db, {'post_id': 901, 'day': '2026-10-01', 'at': '2026-10-01T01:00:00Z',
                                     'status': 'failed', 'error': 'HTTP 404: post not found'})
            response = client.get('/api/blue/reports/history?limit=1&offset=0')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['total'], 1)
            self.assertEqual(response.json()['reports'][0]['error'], 'HTTP 404: post not found')
            self.assertEqual(client.get('/api/blue/reports/history?offset=-1').status_code, 400)

    def test_failed_report_invalidates_the_cached_candidates(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'test.sqlite3'
            app = create_app(db, backup_dir=Path(temp) / 'backups')
            client = TestClient(app, base_url='http://127.0.0.1:8765')
            headers = {'X-CTN-Token': login(client)}
            with patch('ctn_data.blue_view.BlueCandidates.get', return_value=PLAN), \
                    patch('ctn_data.blue_view.BlueCandidates.invalidate') as invalidate, \
                    patch('ctn_data.blue_submit.submit', side_effect=AgentError('HTTP 404: post not found', 409)):
                response = client.post('/api/blue/report', json={'post_id': 901, 'confirmed': True}, headers=headers)
            self.assertEqual((response.status_code, response.json()['error']), (409, 'HTTP 404: post not found'))
            invalidate.assert_called_once()


if __name__ == '__main__':
    unittest.main()
