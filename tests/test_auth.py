"""Login, session revocation and machine-client authorization regressions."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from ctn_data.server import create_app
from ctn_data.worker_api import WORKER_PATHS, worker_authorized
from tests.auth_helpers import login


BASE_URL = 'http://127.0.0.1:8765'
ACCOUNTS = (
    ('dashboard-fixture-one@example.com', 'Test Member One'),
    ('dashboard-fixture-two@example.com', 'Test Member Two'),
    ('dashboard-fixture-three@example.com', 'Test Member Three'),
    ('dashboard-fixture-four@example.com', 'Test Member Four'),
)


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = create_app(self.root / 'login.db', backup_dir=self.root / 'backups')
        self.client = TestClient(self.app, base_url=BASE_URL)

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def token(self):
        response = self.client.get('/api/auth/session')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['csrf_token']

    def test_all_four_accounts_and_email_normalization(self):
        for email, name in ACCOUNTS:
            with self.subTest(email=email):
                login(self.client, '  ' + email.upper() + '  ')
                self.assertEqual(self.client.get('/api/auth/session').json()['user'],
                                 {'name': name, 'email': email})
                self.assertEqual(self.client.get('/api/posts').status_code, 200)

    def test_unknown_account_and_wrong_password_do_not_create_session(self):
        token = self.token()
        failures = []
        for email, password in (
            ('unknown@ad.unsw.edu.au', 'unknown'),
            (ACCOUNTS[0][0], 'incorrect'),
            (ACCOUNTS[0][0], 'DASHBOARD-FIXTURE-ONE'),
            (ACCOUNTS[0][0], ' dashboard-fixture-one '),
        ):
            response = self.client.post('/api/auth/login', json={'email': email, 'password': password},
                                        headers={'X-CTN-Token': token})
            self.assertEqual(response.status_code, 401, response.text)
            failures.append(response.json())
            self.assertNotIn('ctn_session', self.client.cookies)
            self.assertIsNone(self.client.get('/api/auth/session').json()['user'])
        self.assertEqual(failures[0], failures[1], 'Do not disclose which emails are registered')

    def test_login_rejects_invalid_field_types(self):
        token = self.token()
        for payload in ({}, {'email': 123, 'password': 'dashboard-fixture-one'},
                        {'email': ACCOUNTS[0][0], 'password': True},
                        {'email': ACCOUNTS[0][0], 'password': None},
                        {'email': [ACCOUNTS[0][0]], 'password': 'dashboard-fixture-one'}):
            with self.subTest(payload=payload):
                response = self.client.post('/api/auth/login', json=payload,
                                            headers={'X-CTN-Token': token})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertNotIn('ctn_session', self.client.cookies)

    def test_failed_attempt_limit_expires_and_does_not_block_existing_session(self):
        token = login(self.client)
        payload = {'email': ACCOUNTS[0][0], 'password': 'wrong'}
        with patch.object(self.app.state.auth, 'clock', return_value=1000):
            for _ in range(10):
                response = self.client.post('/api/auth/login', json=payload,
                                            headers={'X-CTN-Token': token})
                self.assertEqual(response.status_code, 401, response.text)
            payload['password'] = 'dashboard-fixture-one'
            response = self.client.post('/api/auth/login', json=payload,
                                        headers={'X-CTN-Token': token})
            self.assertEqual(response.status_code, 429, response.text)
            self.assertEqual(self.client.get('/api/posts').status_code, 200)
        with patch.object(self.app.state.auth, 'clock', return_value=1301):
            login(self.client)

    def test_session_cookie_restores_identity_and_logout_revokes_copied_cookie(self):
        token = self.token()
        response = self.client.post('/api/auth/login', json={
            'email': ACCOUNTS[0][0], 'password': 'dashboard-fixture-one',
        }, headers={'X-CTN-Token': token})
        self.assertEqual(response.status_code, 200, response.text)
        cookie_header = response.headers['set-cookie'].lower()
        for flag in ('httponly', 'samesite=strict', 'path=/', 'max-age=2592000'):
            self.assertIn(flag, cookie_header)
        cookie = self.client.cookies['ctn_session']
        with TestClient(self.app, base_url=BASE_URL) as restored:
            restored.cookies.set('ctn_session', cookie)
            self.assertEqual(restored.get('/api/auth/session').json()['user']['email'], ACCOUNTS[0][0])
            self.assertEqual(restored.get('/api/overview').status_code, 200)
            response = self.client.post('/api/auth/logout', json={}, headers={'X-CTN-Token': token})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn('ctn_session', self.client.cookies)
            self.assertIsNone(restored.get('/api/auth/session').json()['user'])
            self.assertEqual(restored.get('/api/overview').status_code, 401)

    def test_relogin_rotates_session_and_revokes_previous_cookie(self):
        login(self.client)
        previous = self.client.cookies['ctn_session']
        login(self.client, ACCOUNTS[1][0])
        self.assertNotEqual(self.client.cookies['ctn_session'], previous)
        with TestClient(self.app, base_url=BASE_URL) as stale:
            stale.cookies.set('ctn_session', previous)
            self.assertEqual(stale.get('/api/posts').status_code, 401)

    def test_expired_session_cannot_read_data(self):
        with patch.object(self.app.state.auth, 'clock', return_value=1000):
            login(self.client)
        with patch.object(self.app.state.auth, 'clock', return_value=1000 + self.app.state.auth.session_seconds):
            self.assertIsNone(self.client.get('/api/auth/session').json()['user'])
            self.assertEqual(self.client.get('/api/posts').status_code, 401)

    def test_data_exports_backups_and_docs_require_session_even_with_csrf_token(self):
        token = login(self.client)
        backup = self.client.post('/api/backup', json={}, headers={'X-CTN-Token': token})
        self.assertEqual(backup.status_code, 200, backup.text)
        paths = ('/api/overview', '/api/posts', '/api/export?table=posts',
                 backup.json()['url'], '/api/extension-status', '/docs', '/openapi.json')
        self.client.cookies.clear()
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, headers={'X-CTN-Token': token}).status_code, 401)
        self.assertEqual(self.client.post('/api/backup', json={}, headers={'X-CTN-Token': token}).status_code, 401)
        for path in ('/api/health', '/api/bootstrap', '/api/auth/session'):
            self.assertEqual(self.client.get(path).status_code, 200)

    def test_login_and_logout_require_csrf_and_same_origin(self):
        payload = {'email': ACCOUNTS[0][0], 'password': 'dashboard-fixture-one'}
        token = self.token()
        for headers in ({}, {'X-CTN-Token': 'wrong'},
                        {'X-CTN-Token': token, 'Origin': 'https://evil.example'}):
            self.assertEqual(self.client.post('/api/auth/login', json=payload, headers=headers).status_code, 403)
        self.assertEqual(self.client.post('/api/auth/login', json=payload, headers={
            'X-CTN-Token': token, 'Origin': BASE_URL}).status_code, 200)
        for headers in ({}, {'X-CTN-Token': token, 'Origin': 'https://evil.example'}):
            self.assertEqual(self.client.post('/api/auth/logout', json={}, headers=headers).status_code, 403)
        self.assertIsNotNone(self.client.get('/api/auth/session').json()['user'])

    def test_paired_extension_operates_without_user_cookie_only_on_whitelisted_routes(self):
        token = login(self.client)
        response = self.client.post('/api/extension-pair', json={'extension_id': 'a' * 32, 'team_id': 46},
                                    headers={'X-CTN-Token': token})
        self.assertEqual(response.status_code, 200, response.text)
        self.client.cookies.clear()
        headers = {'Origin': 'chrome-extension://' + 'a' * 32, 'X-CTN-Token': token}
        self.assertEqual(self.client.get('/api/bootstrap', headers=headers).status_code, 200)
        self.assertEqual(self.client.get('/api/extension-status', headers=headers).status_code, 200)
        self.assertEqual(self.client.post('/api/browser-capture', json={'records': []}, headers=headers).status_code, 200)
        self.assertEqual(self.client.post('/api/extension-permit', json={'path': '/api/posts'}, headers=headers).status_code, 200)
        heartbeat = {'extension_id': 'a' * 32, 'version': '1.3.0', 'auto_enabled': True, 'pages': []}
        self.assertEqual(self.client.post('/api/extension-status', json=heartbeat, headers=headers).status_code, 200)
        for path in ('/api/posts', '/api/export?table=posts', '/api/auth/session', '/openapi.json'):
            self.assertEqual(self.client.get(path, headers=headers).status_code, 403, path)
        self.assertEqual(self.client.post('/api/browser-capture', json={'records': []}, headers={
            'Origin': headers['Origin']}).status_code, 403)
        self.assertEqual(self.client.get('/api/bootstrap', headers={
            'Origin': 'chrome-extension://' + 'b' * 32}).status_code, 403)

    def test_worker_token_is_independent_of_user_session_and_cannot_read_user_data(self):
        token_path = self.root / 'worker-token'
        token_path.write_text('w' * 48, encoding='utf-8')

        def authorize(request, *unused):
            return worker_authorized(request, token_path)

        with patch('ctn_data.worker_api.worker_authorized', side_effect=authorize):
            headers = {'Authorization': 'Bearer ' + 'w' * 48}
            response = self.client.post('/api/worker/claim', json={'worker': 'auth-test'}, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()['paused'])
            for path in ('/api/posts', '/api/operations', '/api/export?table=posts'):
                self.assertEqual(self.client.get(path, headers=headers).status_code, 401)
            for path in WORKER_PATHS:
                self.assertEqual(self.client.post(path, json={'worker': 'auth-test'}).status_code, 401)


if __name__ == '__main__':
    unittest.main()
