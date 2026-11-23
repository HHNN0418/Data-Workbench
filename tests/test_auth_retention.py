"""Durable, renewable sessions using isolated temporary application data."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.requests import Request

from ctn_data.auth import COOKIE_NAME, MEMBERS, AuthStore, session_database_path
from ctn_data.server import create_app
from tests.auth_helpers import login


BASE_URL = 'http://127.0.0.1:8765'
EMAIL = 'dashboard-fixture-one@example.com'


class SessionPathTests(unittest.TestCase):
    def test_postgres_sessions_are_isolated_by_target_without_exposing_passwords(self):
        root = Path('temporary-session-data')
        base = session_database_path('postgresql://member:first-secret@localhost:55432/ctn', root)
        rotated = session_database_path('postgres://member:changed-secret@localhost:55432/ctn', root)
        self.assertEqual(base, rotated)
        self.assertNotIn('secret', str(base))
        for target in ('postgresql://member:first-secret@localhost:55432/other',
                       'postgresql://another:first-secret@localhost:55432/ctn',
                       'postgresql://member:first-secret@localhost:55433/ctn',
                       'postgresql://member:first-secret@remote:55432/ctn',
                       'postgresql://member:first-secret@localhost:55432/ctn?dbname=other'):
            self.assertNotEqual(base, session_database_path(target, root))


class SessionRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'dashboard.db'
        self.app = create_app(self.db)
        self.store = self.app.state.auth
        self.client = self.client_for(self.app)

    def client_for(self, app, cookie=None):
        client = TestClient(app, base_url=BASE_URL)
        self.addCleanup(client.close)
        if cookie:
            client.cookies.set(COOKIE_NAME, cookie)
        return client

    def expiry(self, raw):
        with closing(sqlite3.connect(self.store.session_path)) as connection:
            row = connection.execute('SELECT expires_at FROM dashboard_sessions WHERE token_hash=?',
                                     (AuthStore.session_key(raw),)).fetchone()
            return row[0] if row else None

    def test_cookie_survives_restart_and_logout_revokes_it_in_every_instance(self):
        old_csrf = login(self.client)
        cookie = self.client.cookies[COOKIE_NAME]
        restarted = create_app(self.db)
        restored = self.client_for(restarted, cookie)
        response = restored.get('/api/auth/session')
        self.assertEqual(response.json()['user']['email'], EMAIL)
        fresh_csrf = response.json()['csrf_token']
        self.assertNotEqual(old_csrf, fresh_csrf)
        self.assertEqual(restored.get('/api/posts').status_code, 200)
        response = restored.post('/api/auth/logout', json={}, headers={'X-CTN-Token': fresh_csrf})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.headers.get_list('set-cookie')), 1)
        self.assertIn('Max-Age=0', response.headers['set-cookie'])
        self.assertIsNone(self.expiry(cookie))
        self.assertEqual(self.client.get('/api/posts').status_code, 401)
        self.assertIsNone(self.client.get('/api/auth/session').json()['user'])

    def test_active_requests_renew_server_expiry_and_browser_cookie_after_twelve_hours(self):
        with patch.object(self.store, 'clock', return_value=1000):
            login(self.client)
        cookie = self.client.cookies[COOKIE_NAME]
        later = 1000 + 13 * 60 * 60
        with patch.object(self.store, 'clock', return_value=later):
            response = self.client.get('/api/posts')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.expiry(cookie), later + self.store.session_seconds)
        for flag in ('Max-Age=2592000', 'HttpOnly', 'SameSite=strict', 'Path=/'):
            self.assertIn(flag, response.headers['set-cookie'])
        self.assertEqual(self.client.cookies[COOKIE_NAME], cookie)

    def test_renewal_bounds_disk_writes_and_cookie_matches_durable_expiry(self):
        with patch.object(self.store, 'clock', return_value=1000):
            login(self.client)
        cookie = self.client.cookies[COOKIE_NAME]
        original_expiry = self.expiry(cookie)
        before_renewal = 1000 + self.store.renewal_interval - 1
        with patch.object(self.store, 'clock', return_value=before_renewal):
            response = self.client.get('/api/auth/session')
        self.assertEqual(self.expiry(cookie), original_expiry)
        self.assertIn(f'Max-Age={self.store.session_seconds - self.store.renewal_interval + 1}',
                      response.headers['set-cookie'])
        renewal_time = before_renewal + 1
        with patch.object(self.store, 'clock', return_value=renewal_time):
            response = self.client.get('/api/auth/session')
        self.assertEqual(response.json()['user']['email'], EMAIL)
        self.assertEqual(self.expiry(cookie), renewal_time + self.store.session_seconds)
        self.assertIn('Max-Age=2592000', response.headers['set-cookie'])

    def test_thirty_days_without_activity_expires_even_after_restart(self):
        with patch.object(self.store, 'clock', return_value=1000):
            login(self.client)
        cookie = self.client.cookies[COOKIE_NAME]
        restarted = create_app(self.db)
        restored = self.client_for(restarted, cookie)
        with patch.object(restarted.state.auth, 'clock', return_value=1000 + self.store.session_seconds):
            response = restored.get('/api/auth/session')
            self.assertIsNone(response.json()['user'])
            self.assertNotIn('set-cookie', response.headers)
            self.assertEqual(restored.get('/api/posts').status_code, 401)

    def test_storage_has_only_token_hash_and_another_database_cannot_use_cookie(self):
        login(self.client)
        cookie = self.client.cookies[COOKIE_NAME]
        with closing(sqlite3.connect(self.store.session_path)) as connection:
            rows = connection.execute('SELECT * FROM dashboard_sessions').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], AuthStore.session_key(cookie))
        self.assertNotIn(cookie, str(rows))
        other = self.client_for(create_app(Path(self.temp.name) / 'other.db'), cookie)
        self.assertIsNone(other.get('/api/auth/session').json()['user'])
        self.assertEqual(other.get('/api/posts').status_code, 401)

    def test_changing_account_credentials_revokes_existing_session(self):
        login(self.client)
        member = MEMBERS[EMAIL]
        with patch.dict(MEMBERS, {EMAIL: (member[0], member[1], '0' * len(member[2]))}):
            self.assertEqual(self.client.get('/api/posts').status_code, 401)
        self.assertIsNone(self.client.get('/api/auth/session').json()['user'])

    def test_failed_and_anonymous_requests_never_issue_a_session_cookie(self):
        self.assertNotIn('set-cookie', self.client.get('/api/auth/session').headers)
        self.client.cookies.set(COOKIE_NAME, 'unknown-cookie')
        self.assertNotIn('set-cookie', self.client.get('/api/posts').headers)
        login(self.client)
        self.assertEqual(self.client.get('/api/posts?limit=invalid').status_code, 400)
        self.assertNotIn('set-cookie', self.client.get('/api/posts?limit=invalid').headers)

    def test_relogin_revokes_the_previous_session_after_restart(self):
        login(self.client)
        previous = self.client.cookies[COOKIE_NAME]
        login(self.client)
        restarted = create_app(self.db)
        stale = self.client_for(restarted, previous)
        self.assertEqual(stale.get('/api/posts').status_code, 401)
        restored = self.client_for(restarted, self.client.cookies[COOKIE_NAME])
        self.assertEqual(restored.get('/api/posts').status_code, 200)

    def test_late_response_cannot_reissue_a_cookie_revoked_by_relogin(self):
        login(self.client)
        previous = self.client.cookies[COOKIE_NAME]
        old_request = Request({'type': 'http', 'scheme': 'http', 'path': '/api/posts',
                               'headers': [(b'host', b'127.0.0.1:8765'),
                                           (b'cookie', f'{COOKIE_NAME}={previous}'.encode())]})
        self.assertIsNotNone(self.store.user(old_request))
        login(self.client)
        self.assertNotEqual(self.client.cookies[COOKIE_NAME], previous)
        self.assertEqual(self.store.cookie_headers(old_request), [])

    def test_late_response_uses_latest_expiry_instead_of_shortening_cookie(self):
        with patch.object(self.store, 'clock', return_value=1000):
            login(self.client)
            cookie = self.client.cookies[COOKIE_NAME]
            old_request = Request({'type': 'http', 'scheme': 'http', 'path': '/api/posts',
                                   'headers': [(b'host', b'127.0.0.1:8765'),
                                               (b'cookie', f'{COOKIE_NAME}={cookie}'.encode())]})
            self.assertIsNotNone(self.store.user(old_request))
        with patch.object(self.store, 'clock', return_value=1000 + self.store.renewal_interval):
            self.assertEqual(self.client.get('/api/posts').status_code, 200)
            headers = self.store.cookie_headers(old_request)
        self.assertEqual(len(headers), 1)
        self.assertIn(b'Max-Age=2592000', headers[0][1])


if __name__ == '__main__':
    unittest.main()
