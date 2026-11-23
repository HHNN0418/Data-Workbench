"""Tunnel login attempts are rate limited per Cloudflare client, not per loopback."""
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from starlette.requests import Request

from ctn_data.auth import AuthStore, login_client
from ctn_data.tunnel_proxy import create_proxy
from tests.test_tunnel_proxy import ByteChunks

PUBLIC_URL = 'https://ctn-test.example.com'
SECRET = 'b' * 43


def request(headers, host='127.0.0.1'):
    return Request({'type': 'http', 'method': 'POST', 'path': '/api/auth/login',
                    'headers': [(key.lower().encode(), value.encode()) for key, value in headers.items()],
                    'client': (host, 50000)})


class LoginClientIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.token = self.root / 'tunnel_bridge_token'
        self.token.write_text(SECRET, encoding='utf-8')

    def test_local_request_keeps_socket_address(self):
        self.assertEqual(login_client(request({}), self.token), '127.0.0.1')

    def test_verified_bridge_names_the_public_client(self):
        headers = {'X-CTN-Bridge': SECRET, 'X-CTN-Client-IP': '203.0.113.7'}
        self.assertEqual(login_client(request(headers), self.token), 'public:203.0.113.7')
        headers['X-CTN-Client-IP'] = 'unknown'
        self.assertEqual(login_client(request(headers), self.token), 'public:unknown')

    def test_unverified_claims_never_pose_as_loopback_or_choose_a_bucket(self):
        for headers in ({'X-CTN-Client-IP': '198.51.100.1'},
                        {'X-CTN-Bridge': 'wrong', 'X-CTN-Client-IP': '198.51.100.1'},
                        {'X-CTN-Bridge': 'é' * 40, 'X-CTN-Client-IP': '198.51.100.1'}):
            with self.subTest(headers=headers):
                self.assertEqual(login_client(request(headers), self.token), 'public:unverified')
        self.assertEqual(login_client(request({'X-CTN-Bridge': SECRET}), self.root / 'missing'),
                         'public:unverified')

    def test_one_public_client_cannot_lock_out_a_member_for_others(self):
        store = AuthStore(self.root / 'sessions.sqlite3')
        store.clock = lambda: 1000
        email = next(iter(__import__('ctn_data.auth', fromlist=['MEMBERS']).MEMBERS))
        for _ in range(store.max_failures):
            with self.assertRaises(Exception) as caught:
                store.login(email, 'wrong', 'public:203.0.113.7')
            self.assertEqual(caught.exception.status_code, 401)
        with self.assertRaises(Exception) as caught:
            store.login(email, 'wrong', 'public:203.0.113.7')
        self.assertEqual(caught.exception.status_code, 429)
        # Another client still reaches the credential check.
        with self.assertRaises(Exception) as caught:
            store.login(email, 'wrong', 'public:198.51.100.9')
        self.assertEqual(caught.exception.status_code, 401)


class BridgeHeaderTests(unittest.TestCase):
    def forward(self, headers, secret=SECRET):
        received = []
        def upstream(outgoing):
            received.append(outgoing)
            return httpx.Response(200, headers={'Content-Type': 'application/json'}, stream=ByteChunks(b'{}'))
        proxy = create_proxy(PUBLIC_URL, transport=httpx.MockTransport(upstream), bridge_secret=secret)
        with TestClient(proxy, base_url=PUBLIC_URL) as client:
            self.assertEqual(client.get('/api/health', headers=headers).status_code, 200)
        return received[0].headers

    def test_bridge_replaces_client_supplied_identity_headers(self):
        forwarded = self.forward({'CF-Connecting-IP': '203.0.113.7', 'X-CTN-Bridge': 'forged',
                                  'X-CTN-Client-IP': '198.51.100.1'})
        self.assertEqual(forwarded.get_list('x-ctn-bridge'), [SECRET])
        self.assertEqual(forwarded.get_list('x-ctn-client-ip'), ['203.0.113.7'])
        self.assertNotIn('cf-connecting-ip', forwarded)

    def test_missing_or_invalid_cloudflare_address_is_unknown(self):
        for headers in ({}, {'CF-Connecting-IP': 'not-an-ip'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.forward(headers).get('x-ctn-client-ip'), 'unknown')

    def test_bridge_without_secret_adds_nothing(self):
        forwarded = self.forward({'CF-Connecting-IP': '203.0.113.7', 'X-CTN-Bridge': 'forged'}, secret=None)
        self.assertNotIn('x-ctn-bridge', forwarded)
        self.assertNotIn('x-ctn-client-ip', forwarded)


if __name__ == '__main__':
    unittest.main()
