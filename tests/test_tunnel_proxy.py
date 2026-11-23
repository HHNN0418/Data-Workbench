"""Public tunnel integration without exposing or modifying the local backend."""
import asyncio
import gzip
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect
from starlette.responses import RedirectResponse
from starlette.routing import Route

from ctn_data.tunnel_proxy import UpstreamResponse, create_proxy
from ctn_data.server import create_app


PUBLIC_URL = 'https://ctn-test.example.com'
PUBLIC_HOST = 'ctn-test.example.com'
LOCAL_URL = 'http://127.0.0.1:8765'
EMAIL = 'dashboard-fixture-one@example.com'
LOGIN = {'email': EMAIL, 'password': 'dashboard-fixture-one'}


class TunnelIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        dist = root / 'dist'
        (dist / 'assets').mkdir(parents=True)
        (dist / 'index.html').write_text('<html>CTN test dashboard</html>', encoding='utf-8')
        (dist / 'assets' / 'app.js').write_text('console.log("CTN")', encoding='utf-8')
        self.backend = create_app(root / 'ctn.sqlite3', backup_dir=root / 'backups', web_root=dist)
        proxy = create_proxy(PUBLIC_URL, transport=httpx.ASGITransport(app=self.backend))
        self.client = TestClient(proxy, base_url=PUBLIC_URL)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def token(self):
        response = self.client.get('/api/auth/session')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['csrf_token']

    def login(self):
        token = self.token()
        response = self.client.post('/api/auth/login', json=LOGIN, headers={
            'Origin': PUBLIC_URL, 'X-CTN-Token': token,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return token, response

    def test_frontend_and_bootstrap_work_but_data_requires_login(self):
        for path in ('/', '/posts', '/assets/app.js', '/api/health', '/api/bootstrap'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
        self.assertIn('CTN test dashboard', self.client.get('/').text)
        self.assertIsNone(self.client.get('/api/auth/session').json()['user'])
        token = self.token()
        for path in ('/api/posts', '/api/overview', '/api/export?table=posts', '/openapi.json'):
            with self.subTest(path=path):
                response = self.client.get(path, headers={'X-CTN-Token': token})
                self.assertEqual(response.status_code, 401, response.text)

    def test_login_cookie_and_logout_revocation_work_through_https(self):
        token, response = self.login()
        self.assertEqual(response.json()['user']['email'], EMAIL)
        cookie_header = response.headers['set-cookie'].lower()
        for attribute in ('secure', 'httponly', 'samesite=strict', 'path=/'):
            self.assertIn(attribute, cookie_header)
        cookie = self.client.cookies['ctn_session']
        self.assertEqual(self.client.get('/api/posts').status_code, 200)
        self.assertEqual(self.client.get('/api/auth/session').json()['user']['email'], EMAIL)

        response = self.client.post('/api/auth/logout', json={}, headers={
            'Origin': PUBLIC_URL, 'X-CTN-Token': token,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('secure', response.headers['set-cookie'].lower())
        self.assertNotIn('ctn_session', self.client.cookies)
        self.assertEqual(self.client.get('/api/posts').status_code, 401)
        # A copied cookie must be revoked on the actual backend as well.
        response = self.client.get('/api/posts', headers={'Cookie': 'ctn_session=' + cookie})
        self.assertEqual(response.status_code, 401, response.text)

    def test_second_visitor_does_not_inherit_shared_upstream_cookie_jar(self):
        self.login()
        first_cookie = self.client.cookies['ctn_session']
        self.client.cookies.clear()
        self.assertIsNone(self.client.get('/api/auth/session').json()['user'])
        self.assertEqual(self.client.get('/api/posts').status_code, 401)

        second_email = 'dashboard-fixture-two@example.com'
        response = self.client.post('/api/auth/login', json={
            'email': second_email, 'password': 'dashboard-fixture-two',
        }, headers={'Origin': PUBLIC_URL, 'X-CTN-Token': self.token()})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get('/api/auth/session').json()['user']['email'], second_email)
        self.assertNotEqual(self.client.cookies['ctn_session'], first_cookie)
        response = self.client.get('/api/auth/session', headers={'Cookie': 'ctn_session=' + first_cookie})
        self.assertEqual(response.json()['user']['email'], EMAIL)

    def test_public_writes_require_exact_origin_and_existing_csrf_token(self):
        token = self.token()
        for headers in (
            {'X-CTN-Token': token},
            {'Origin': PUBLIC_URL},
            {'Origin': PUBLIC_URL, 'X-CTN-Token': 'incorrect'},
            {'Origin': LOCAL_URL, 'X-CTN-Token': token},
        ):
            with self.subTest(headers=headers):
                response = self.client.post('/api/auth/login', json=LOGIN, headers=headers)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertNotIn('ctn_session', self.client.cookies)
        token, _ = self.login()
        payload = {'account_ids': [1], 'evidence': 'tunnel integration test'}
        for headers in ({'X-CTN-Token': token}, {'Origin': PUBLIC_URL},
                        {'Origin': 'https://evil.example', 'X-CTN-Token': token}):
            with self.subTest(headers=headers):
                response = self.client.post('/api/team-accounts', json=payload, headers=headers)
                self.assertEqual(response.status_code, 403, response.text)
        response = self.client.post('/api/team-accounts', json=payload, headers={
            'Origin': PUBLIC_URL, 'X-CTN-Token': token,
        })
        self.assertEqual(response.status_code, 200, response.text)

    def test_host_and_origin_suffixes_and_duplicates_are_rejected(self):
        for host in ('evil.example', PUBLIC_HOST + '.evil.example', 'other.' + PUBLIC_HOST,
                     PUBLIC_HOST + '.', PUBLIC_HOST + ':80', '127.0.0.1:8766'):
            with self.subTest(host=host):
                response = self.client.get('/api/health', headers={'Host': host})
                self.assertEqual(response.status_code, 403, response.text)
        for origin in ('null', 'http://' + PUBLIC_HOST, PUBLIC_URL + '.evil.example',
                       PUBLIC_URL + '/', 'https://evil.example'):
            with self.subTest(origin=origin):
                response = self.client.get('/api/health', headers={'Origin': origin})
                self.assertEqual(response.status_code, 403, response.text)
        for headers in (
            [('Host', PUBLIC_HOST), ('Host', 'evil.example')],
            [('Origin', PUBLIC_URL), ('Origin', PUBLIC_URL)],
        ):
            with self.subTest(headers=headers):
                response = self.client.get('/api/health', headers=headers)
                self.assertEqual(response.status_code, 403, response.text)

    def test_local_backend_access_and_its_original_security_still_work(self):
        with TestClient(self.backend, base_url=LOCAL_URL) as local:
            self.assertEqual(local.get('/api/health').status_code, 200)
            token = local.get('/api/auth/session').json()['csrf_token']
            response = local.post('/api/auth/login', json=LOGIN, headers={
                'Origin': LOCAL_URL, 'X-CTN-Token': token,
            })
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(local.get('/api/posts').status_code, 200)
            self.assertNotIn('; secure', response.headers['set-cookie'].lower())
            self.assertEqual(local.get('/api/health', headers={'Host': PUBLIC_HOST}).status_code, 403)
            response = local.post('/api/auth/logout', json={}, headers={
                'Origin': PUBLIC_URL, 'X-CTN-Token': token,
            })
            self.assertEqual(response.status_code, 403, response.text)

    def test_backend_absolute_redirect_uses_public_https(self):
        async def redirect(request):
            return RedirectResponse(LOCAL_URL + '/api/health?source=tunnel', status_code=307)

        # Insert before the production SPA fallback in this temporary app only.
        self.backend.router.routes.insert(0, Route('/api/test-redirect', redirect))
        self.login()
        response = self.client.get('/api/test-redirect', follow_redirects=False)
        self.assertEqual(response.status_code, 307, response.text)
        self.assertEqual(response.headers['location'], PUBLIC_URL + '/api/health?source=tunnel')
        self.assertEqual(self.client.get('/api/test-redirect').status_code, 200)


class ByteChunks(httpx.AsyncByteStream):
    """Model an upstream socket response that has not been read or decoded yet."""
    def __init__(self, *chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class TunnelTransportTests(unittest.TestCase):
    def client(self, handler):
        proxy = create_proxy(PUBLIC_URL, transport=httpx.MockTransport(handler))
        client = TestClient(proxy, base_url=PUBLIC_URL)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    @staticmethod
    def response():
        return httpx.Response(200, headers={'Content-Type': 'application/json'}, stream=ByteChunks(b'{}'))

    def test_forwarded_claims_are_removed_and_exact_path_query_and_cookie_are_kept(self):
        received = []

        def upstream(request):
            received.append(request)
            return self.response()

        client = self.client(upstream)
        response = client.post('/assets/a%2Fb.js?q=a%2Fb&x=1', content=b'{}', headers={
            'Origin': PUBLIC_URL, 'Content-Type': 'application/json', 'X-CTN-Token': 'csrf-token',
            'Cookie': 'ctn_session=visitor-only', 'Forwarded': 'host=evil.example;proto=https',
            'X-Forwarded-Host': 'evil.example', 'X-Forwarded-Proto': 'https',
            'X-Forwarded-For': '203.0.113.1', 'X-Real-IP': '203.0.113.1',
            'CF-Connecting-IP': '203.0.113.1', 'True-Client-IP': '203.0.113.1',
            'Connection': 'keep-alive, x-remove-me', 'X-Remove-Me': 'untrusted',
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(received), 1)
        request = received[0]
        self.assertEqual(str(request.url).split('/assets')[0], LOCAL_URL)
        self.assertEqual(request.url.raw_path, b'/assets/a%2Fb.js?q=a%2Fb&x=1')
        self.assertEqual(request.headers['host'], '127.0.0.1:8765')
        self.assertEqual(request.headers['origin'], LOCAL_URL)
        self.assertEqual(request.headers['x-ctn-token'], 'csrf-token')
        self.assertEqual(request.headers['cookie'], 'ctn_session=visitor-only')
        self.assertEqual(request.content, b'{}')
        for header in ('forwarded', 'x-forwarded-host', 'x-forwarded-proto', 'x-forwarded-for',
                       'x-real-ip', 'cf-connecting-ip', 'true-client-ip', 'connection', 'x-remove-me'):
            self.assertNotIn(header, request.headers)

    def test_invalid_content_lengths_and_transfer_encoding_never_reach_backend(self):
        received = []

        def upstream(request):
            received.append(request)
            return self.response()

        client = self.client(upstream)
        variants = (
            [('Content-Length', '-1')], [('Content-Length', 'invalid')],
            [('Content-Length', '')], [('Content-Length', '1, 1')],
            [('Content-Length', '1')], [('Content-Length', '3')],
            [('Content-Length', '2'), ('Content-Length', '2')],
            [('Content-Length', '2'), ('Transfer-Encoding', 'chunked')],
            [('Transfer-Encoding', 'gzip')],
            [('Transfer-Encoding', 'chunked'), ('Transfer-Encoding', 'chunked')],
        )
        for headers in variants:
            with self.subTest(headers=headers):
                response = client.post('/api/auth/login', content=b'{}', headers=[
                    ('Origin', PUBLIC_URL), ('Content-Type', 'application/json'), *headers,
                ])
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(received, [])

    def test_declared_and_streamed_bodies_are_bounded_before_forwarding(self):
        received = []

        def upstream(request):
            received.append(request)
            return self.response()

        client = self.client(upstream)
        headers = {'Origin': PUBLIC_URL, 'Content-Type': 'application/json'}
        response = client.post('/api/auth/login', content=b'{}', headers={
            **headers, 'Content-Length': str(10 * 1024 * 1024 + 1),
        })
        self.assertEqual(response.status_code, 413, response.text)

        def oversized_chunks():
            yield b'x' * (5 * 1024 * 1024)
            yield b'x' * (5 * 1024 * 1024 + 1)

        response = client.post('/api/auth/login', content=oversized_chunks(), headers=headers)
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(received, [])
        response = client.post('/api/auth/login', content=iter((b'{', b'}')), headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(received[0].content, b'{}')

    def test_stream_preserves_gzip_bytes_duplicate_cookies_and_security_headers(self):
        original = b'CTN export content\n' * 1024
        compressed = gzip.compress(original)
        stream = ByteChunks(compressed[:10], compressed[10:])

        def upstream(request):
            return httpx.Response(200, headers=[
                ('Content-Type', 'application/octet-stream'), ('Content-Encoding', 'gzip'),
                ('Content-Length', str(len(compressed))), ('Cache-Control', 'no-store'),
                ('Content-Security-Policy', "default-src 'self'"),
                ('Set-Cookie', 'first=one; Path=/; HttpOnly; SameSite=Strict'),
                ('Set-Cookie', 'second=two; Path=/; Secure'),
                ('Connection', 'keep-alive, x-remove-me'), ('X-Remove-Me', 'upstream-only'),
            ], stream=stream)

        client = self.client(upstream)
        with client.stream('GET', '/api/export?table=posts') as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['content-encoding'], 'gzip')
            self.assertEqual(response.headers['content-length'], str(len(compressed)))
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(response.headers['content-security-policy'], "default-src 'self'")
            cookies = response.headers.get_list('set-cookie')
            self.assertEqual(len(cookies), 2)
            self.assertTrue(cookies[0].startswith('first=one;'))
            self.assertTrue(cookies[1].startswith('second=two;'))
            self.assertTrue(all('; secure' in cookie.lower() for cookie in cookies))
            self.assertNotIn('connection', response.headers)
            self.assertNotIn('x-remove-me', response.headers)
            wire_body = b''.join(response.iter_raw())
        self.assertEqual(wire_body, compressed)
        self.assertEqual(gzip.decompress(wire_body), original)
        self.assertTrue(stream.closed)

    def test_backend_connection_failure_returns_bounded_gateway_error(self):
        def unavailable(request):
            raise httpx.ConnectError('private connection details', request=request)

        response = self.client(unavailable).get('/api/health')
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('private connection details', response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_disconnected_response_header_send_closes_upstream(self):
        async def disconnected_delivery():
            stream = ByteChunks(b'not yet delivered')
            upstream = httpx.Response(200, stream=stream)
            response = UpstreamResponse(upstream, [])

            async def receive():
                return {'type': 'http.disconnect'}

            async def send(message):
                self.assertEqual(message['type'], 'http.response.start')
                raise OSError('client disconnected before response headers')

            # ASGI 2.4 turns a failed send into ClientDisconnect directly.
            with self.assertRaises(ClientDisconnect):
                await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
            self.assertTrue(stream.closed)
            self.assertTrue(upstream.is_closed)

        asyncio.run(disconnected_delivery())


class TunnelConfigurationTests(unittest.TestCase):
    def test_only_a_specific_https_dns_origin_is_accepted(self):
        for value in (None, 123, '', 'http://ctn-test.example.com',
                      'https://*.example.com', 'https://localhost', 'https://127.0.0.1',
                      'https://[::1]', 'https://user:secret@ctn-test.example.com',
                      PUBLIC_URL + ':80', PUBLIC_URL + ':invalid', PUBLIC_URL + '/path',
                      PUBLIC_URL + '?token=secret', PUBLIC_URL + '#fragment',
                      PUBLIC_URL + '?', PUBLIC_URL + '#', ' ' + PUBLIC_URL,
                      PUBLIC_URL + ' ', PUBLIC_URL + '.', 'https://-bad.example'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                create_proxy(value)
        for value in (PUBLIC_URL, PUBLIC_URL + '/', PUBLIC_URL + ':443'):
            with self.subTest(value=value):
                self.assertEqual(create_proxy(value).state.public_origin, PUBLIC_URL)

    def test_backend_port_must_be_a_real_tcp_port(self):
        for port in (None, True, '8765', 0, -1, 65536):
            with self.subTest(port=port), self.assertRaises(ValueError):
                create_proxy(PUBLIC_URL, backend_port=port)


if __name__ == '__main__':
    unittest.main()
