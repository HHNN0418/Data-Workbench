"""Bounded exports, static caching and nonblocking session checks."""
import asyncio
import csv
import io
from pathlib import Path
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from ctn_data import queries
from ctn_data.db import connect
from ctn_data.server import LocalSecurityMiddleware, create_app, spreadsheet_cell


def scope(path):
    return {'type': 'http', 'method': 'GET', 'path': path, 'scheme': 'http',
            'query_string': b'', 'headers': [(b'host', b'127.0.0.1:8765')], 'state': {}}


class SessionConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_storage_and_cookie_revalidation_run_off_event_loop(self):
        loop_thread = threading.get_ident()
        calls = []

        class Auth:
            def user(self, request):
                calls.append(('user', threading.get_ident()))
                request.state.ctn_session_cookie = ('fixture', 1)
                return {'name': 'Fixture'}

            def cookie_headers(self, request):
                calls.append(('cookie', threading.get_ident()))
                return [(b'set-cookie', b'fixture=valid')]

        async def app(request_scope, receive, send):
            self.assertEqual(request_scope['state']['ctn_session_cookie'][0], 'fixture')
            await JSONResponse({'ok': True})(request_scope, receive, send)

        async def receive():
            return {'type': 'http.request', 'body': b''}

        messages = []

        async def send(message):
            messages.append(message)

        await LocalSecurityMiddleware(app, 'csrf', auth=Auth())(scope('/api/posts'), receive, send)
        self.assertEqual([name for name, _ in calls], ['user', 'cookie'])
        self.assertTrue(all(thread != loop_thread for _, thread in calls))
        self.assertIn((b'set-cookie', b'fixture=valid'), messages[0]['headers'])

    async def test_slow_session_lookup_does_not_delay_health_request(self):
        started, release = threading.Event(), threading.Event()
        completed = []

        class Auth:
            def user(self, request):
                started.set()
                release.wait(1)
                return {'name': 'Fixture'}

        async def app(request_scope, receive, send):
            completed.append(request_scope['path'])
            await JSONResponse({'ok': True})(request_scope, receive, send)

        async def receive():
            return {'type': 'http.request', 'body': b''}

        async def send(message):
            pass

        middleware = LocalSecurityMiddleware(app, 'csrf', auth=Auth())
        protected = asyncio.create_task(middleware(scope('/api/posts'), receive, send))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            await middleware(scope('/api/health'), receive, send)
            self.assertEqual(completed, ['/api/health'])
            self.assertFalse(protected.done())
        finally:
            release.set()
            await protected


class StaticCacheTests(unittest.TestCase):
    def test_only_existing_fingerprinted_assets_are_cacheable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'assets').mkdir()
            (root / 'index.html').write_text('<html>fixture</html>', encoding='utf8')
            for filename in ('app-Abcd1234.js', 'app.js', 'common-A_b-12CD.css'):
                (root / 'assets' / filename).write_text('fixture', encoding='utf8')
            app = create_app(root / 'test.db', web_root=root)
            with TestClient(app, base_url='http://127.0.0.1:8765') as client:
                for path in ('/assets/app-Abcd1234.js', '/assets/common-A_b-12CD.css'):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers['cache-control'], 'public, max-age=31536000, immutable')
                    self.assertEqual(len(response.headers.get_list('cache-control')), 1)
                    self.assertNotIn('set-cookie', response.headers)
                for path in ('/', '/posts', '/api/health', '/api/posts',
                             '/assets/app.js', '/assets/missing-Abcd1234.js'):
                    self.assertEqual(client.get(path).headers['cache-control'], 'no-store', path)


class ExportBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_batches_without_changing_csv_or_formula_protection(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'export.db'
            app = create_app(db)
            connection = connect(db)
            try:
                with connection:
                    connection.executemany('''INSERT INTO posts
                        (post_id,kind,body,created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?)''',
                        ((index, 'post', '=formula,"引号"\n第二行 😀 ' + 'x' * 100,
                          '2026-10-21T00:00:00Z', '2026-10-21T00:00:00Z', '2026-10-21T00:00:00Z')
                         for index in range(1, 4001)))
                expected = io.StringIO(newline='')
                writer = csv.writer(expected)
                cursor = connection.execute(queries.EXPORTS['posts'])
                writer.writerow([column[0] for column in cursor.description])
                writer.writerows([spreadsheet_cell(value) for value in row] for row in cursor)
            finally:
                connection.close()
            endpoint = next(route.endpoint for route in app.routes if getattr(route, 'path', '') == '/api/export')
            response = endpoint('posts')
            chunks = [chunk async for chunk in response.body_iterator]
            self.assertEqual(b''.join(chunks), b'\xef\xbb\xbf' + expected.getvalue().encode('utf8'))
            self.assertLess(len(chunks), 30, 'Avoid an ASGI thread handoff for every row')
            self.assertLess(max(map(len, chunks)), 128 * 1024, 'Keep export buffering bounded')
            rows = list(csv.DictReader(io.StringIO(b''.join(chunks).decode('utf-8-sig'))))
            self.assertEqual(len(rows), 4000)
            self.assertTrue(rows[0]['body'].startswith("'=formula"))
