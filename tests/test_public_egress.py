import io
import json
import urllib.error
import urllib.request
from unittest.mock import patch

from ctn_data.db import connect
from ctn_data.egress import choose_public_egress
from ctn_data.transport import BudgetExceeded, ReadOnlyTransport, RequestDeferred, TransportError
from tests.test_collection import BaseDatabase, FakeClock, Opener, Response


class PublicEgressTests(BaseDatabase):
    def heartbeat(self, worker, *, at=999, status=200):
        with self.conn:
            self.conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                              ('worker_status:' + worker, json.dumps({'last_seen_epoch': at, 'last_http_status': status})))

    def test_disabled_is_direct_even_with_healthy_workers(self):
        self.heartbeat('docker-au-a')
        with patch('ctn_data.egress.PROXY_ENABLED_PATH') as enabled:
            enabled.exists.return_value = False
            self.assertEqual(choose_public_egress(self.conn), {'proxy_url': None, 'actor': 'backend'})

    def test_persistent_round_robin_and_unhealthy_exclusion(self):
        self.heartbeat('docker-au-a')
        self.heartbeat('docker-au-b')
        with patch('ctn_data.egress.PROXY_ENABLED_PATH') as enabled, patch('ctn_data.egress.time.time', return_value=1000):
            enabled.exists.return_value = True
            self.assertEqual(choose_public_egress(self.conn)['actor'], 'backend')
            other = connect(self.path)
            try:
                self.assertEqual(choose_public_egress(other), {'proxy_url': 'http://127.0.0.1:18881', 'actor': 'proxy:docker-au-a'})
            finally:
                other.close()
            self.assertEqual(choose_public_egress(self.conn)['actor'], 'proxy:docker-au-b')
            self.heartbeat('docker-au-a', status=429)
            self.heartbeat('docker-au-b', at=939)
            self.assertEqual(choose_public_egress(self.conn)['actor'], 'backend')
            self.assertEqual(choose_public_egress(self.conn)['actor'], 'backend')
            self.heartbeat('docker-au-b', at=940)
            self.assertEqual(choose_public_egress(self.conn)['actor'], 'proxy:docker-au-b')

    def test_proxy_tunnel_cannot_be_bypassed_or_retry_direct(self):
        calls = []
        def fail_tunnel(handler, request):
            calls.append((request.host, request._tunnel_host))
            raise urllib.error.URLError('proxy unavailable')
        clock = FakeClock()
        with patch('urllib.request.proxy_bypass', return_value=True), patch.object(urllib.request.HTTPSHandler, 'https_open', new=fail_tunnel):
            transport = ReadOnlyTransport(self.conn, proxy_url='http://127.0.0.1:18881', actor='proxy:docker-au-a',
                                          max_retries=1, clock=clock, sleeper=clock.sleep)
            with self.assertRaises(TransportError):
                transport.get_json('social', '/api/accounts/42')
        self.assertEqual(calls, [('127.0.0.1:18881', 'social.legitreal.com')] * 2)
        logs = self.conn.execute('SELECT service,error FROM api_call_logs ORDER BY id').fetchall()
        self.assertEqual([row['service'] for row in logs], ['proxy:docker-au-a'] * 2)
        self.assertEqual([row['error'] for row in logs], ['URLError'] * 2)

    def test_proxy_news_logs_exit_without_splitting_service_cooldown(self):
        error = urllib.error.HTTPError('https://sites.legitreal.com/index.json', 429, 'limit', {'Retry-After': '60'}, io.BytesIO())
        transport = ReadOnlyTransport(self.conn, proxy_url='http://127.0.0.1:18882', actor='proxy:docker-au-b', opener=Opener([error]))
        with self.assertRaises(RequestDeferred):
            transport.get_json('news', '/index.json')
        self.assertEqual(self.conn.execute('SELECT service FROM api_call_logs').fetchone()[0], 'proxy:docker-au-b:news')
        self.assertIsNotNone(self.conn.execute("SELECT value FROM checkpoints WHERE key='throttle:news:/index.json'").fetchone())
        direct_opener = Opener([])
        direct = ReadOnlyTransport(self.conn, opener=direct_opener, max_wait_seconds=0)
        with self.assertRaises(BudgetExceeded):
            direct.get_json('news', '/index.json')
        self.assertEqual(direct_opener.calls, [])

    def test_proxy_feed_and_direct_share_permit_budget_and_actor_tracking(self):
        first = Opener([Response({'items': []})])
        proxy = ReadOnlyTransport(self.conn, proxy_url='http://127.0.0.1:18881', actor='proxy:docker-au-a', opener=first)
        proxy.get_json('social', '/api/posts')
        state = json.loads(self.conn.execute("SELECT value FROM checkpoints WHERE key='coordination:feeds'").fetchone()[0])
        self.assertEqual(state['last_actor'], 'proxy:docker-au-a')
        direct = ReadOnlyTransport(self.conn, opener=Opener([]), max_wait_seconds=0)
        with self.assertRaises(RequestDeferred):
            direct.get_json('social', '/api/posts')

    def test_proxy_configuration_rejects_credentials_and_non_http_proxy(self):
        for url in ('socks5://127.0.0.1:18881', 'http://user:secret@127.0.0.1:18881', 'http://127.0.0.1', 'http://127.0.0.1:18881/path'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ReadOnlyTransport(self.conn, proxy_url=url)
