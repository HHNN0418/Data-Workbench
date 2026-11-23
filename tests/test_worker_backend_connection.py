import errno
import http.client
import socket
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import HTTPHandler, ProxyHandler

from ctn_data.proxy_worker import (
    BackendHTTPConnection,
    BackendHTTPHandler,
    NoRedirect,
    ProxyWorker,
    _connect_backend,
    retry_diagnostics,
)


V4 = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('192.0.2.1', 8765))
V6 = (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('2001:db8::1', 8765, 0, 0))


class FakeSocket:
    def __init__(self, connect_error=None, clock=None, send_error=None, read_error=None):
        self.connect_error = connect_error
        self.clock = clock
        self.send_error = send_error
        self.read_error = read_error
        self.timeouts = []
        self.endpoints = []
        self.sent = []
        self.closed = False
        self.bound = None

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def bind(self, source):
        self.bound = source

    def connect(self, endpoint):
        self.endpoints.append(endpoint)
        if self.connect_error:
            if self.clock is not None:
                self.clock[0] += self.timeouts[-1]
            raise self.connect_error

    def close(self):
        self.closed = True

    def setsockopt(self, *args):
        pass

    def sendall(self, data):
        self.sent.append(data)
        if self.send_error:
            raise self.send_error

    def makefile(self, *args, **kwargs):
        if self.read_error:
            raise self.read_error
        raise AssertionError('Unexpected response read')


class BackendConnectionTests(unittest.TestCase):
    def test_transient_connect_failure_uses_a_fresh_socket_and_restores_read_timeout(self):
        first = FakeSocket(TimeoutError('private connection detail'))
        second = FakeSocket()
        with patch('ctn_data.proxy_worker.socket.getaddrinfo', return_value=[V4]), patch(
            'ctn_data.proxy_worker.socket.socket', side_effect=[first, second]
        ) as factory:
            result = _connect_backend(('backend.invalid', 8765), 15)
        self.assertIs(result, second)
        self.assertEqual(factory.call_count, 2)
        self.assertTrue(first.closed)
        self.assertEqual(first.timeouts, [3.0])
        self.assertEqual(second.timeouts, [3.0, 15])

    def test_three_slow_connects_have_a_nine_second_budget(self):
        clock = [0.0]
        sockets = [FakeSocket(TimeoutError('private'), clock) for _ in range(3)]
        with patch('ctn_data.proxy_worker.socket.getaddrinfo', return_value=[V4]), patch(
            'ctn_data.proxy_worker.socket.socket', side_effect=sockets
        ) as factory, patch('ctn_data.proxy_worker.time.monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(TimeoutError):
                _connect_backend(('backend.invalid', 8765), 15)
        self.assertEqual(factory.call_count, 3)
        self.assertEqual(clock[0], 9)
        self.assertTrue(all(item.closed for item in sockets))
        self.assertTrue(all(item.timeouts == [3.0] for item in sockets))

    def test_remaining_total_budget_caps_a_later_attempt(self):
        first, second, third = [FakeSocket(TimeoutError('private')) for _ in range(3)]
        with patch('ctn_data.proxy_worker.socket.getaddrinfo', return_value=[V4]), patch(
            'ctn_data.proxy_worker.socket.socket', side_effect=[first, second, third]
        ), patch('ctn_data.proxy_worker.time.monotonic', side_effect=[0, 0, 4, 8]):
            with self.assertRaises(TimeoutError):
                _connect_backend(('backend.invalid', 8765), 15)
        self.assertEqual(third.timeouts, [1.0])

    def test_dns_order_is_kept_and_ipv6_does_not_mask_ipv4_timeout(self):
        first_error = TimeoutError('DO_NOT_LOG_PRIVATE_ADDRESS')
        sockets = [FakeSocket(first_error), FakeSocket(OSError(errno.ENETUNREACH, 'private')), FakeSocket(TimeoutError())]
        with patch('ctn_data.proxy_worker.socket.getaddrinfo', return_value=[V4, V6]), patch(
            'ctn_data.proxy_worker.socket.socket', side_effect=sockets
        ) as factory:
            with self.assertRaises(TimeoutError) as raised:
                _connect_backend(('backend.invalid', 8765), 15)
        self.assertIs(raised.exception, first_error)
        self.assertEqual([item.endpoints[0] for item in sockets], [V4[4], V6[4], V4[4]])
        self.assertEqual([call.args[0] for call in factory.call_args_list], [socket.AF_INET, socket.AF_INET6, socket.AF_INET])
        self.assertNotIn('DO_NOT_LOG', retry_diagnostics(URLError(raised.exception)))

    def test_initial_unroutable_address_does_not_hide_later_effective_error(self):
        timeout = TimeoutError('private')
        sockets = [FakeSocket(OSError(errno.ENETUNREACH, 'private')), FakeSocket(timeout), FakeSocket(OSError(errno.ENETUNREACH, 'private'))]
        with patch('ctn_data.proxy_worker.socket.getaddrinfo', return_value=[V6, V4]), patch(
            'ctn_data.proxy_worker.socket.socket', side_effect=sockets
        ):
            with self.assertRaises(TimeoutError) as raised:
                _connect_backend(('backend.invalid', 8765), 15)
        self.assertIs(raised.exception, timeout)

    def test_permission_error_fails_without_retry(self):
        first = FakeSocket(PermissionError(errno.EACCES, 'private'))
        with patch('ctn_data.proxy_worker.socket.getaddrinfo', return_value=[V4]), patch(
            'ctn_data.proxy_worker.socket.socket', return_value=first
        ) as factory:
            with self.assertRaises(PermissionError):
                _connect_backend(('backend.invalid', 8765), 15)
        self.assertEqual(factory.call_count, 1)
        self.assertTrue(first.closed)

    def test_shorter_caller_timeout_and_source_address_are_preserved(self):
        connected = FakeSocket()
        with patch('ctn_data.proxy_worker.socket.getaddrinfo', return_value=[V4]), patch(
            'ctn_data.proxy_worker.socket.socket', return_value=connected
        ):
            _connect_backend(('backend.invalid', 8765), 0.4, ('127.0.0.1', 0))
        self.assertEqual(connected.timeouts, [0.4, 0.4])
        self.assertEqual(connected.bound, ('127.0.0.1', 0))

    def test_request_send_timeout_never_reconnects_or_replays_post(self):
        connected = FakeSocket(send_error=TimeoutError('private request data'))
        with patch('ctn_data.proxy_worker.socket.getaddrinfo', return_value=[V4]), patch(
            'ctn_data.proxy_worker.socket.socket', return_value=connected
        ) as factory:
            connection = BackendHTTPConnection('backend.invalid', 8765, timeout=15)
            with self.assertRaises(TimeoutError):
                connection.request('POST', '/api/worker/result', body=b'private payload')
            connection.close()
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(len(connected.endpoints), 1)
        self.assertEqual(sum(chunk.startswith(b'POST ') for chunk in connected.sent), 1)

    def test_response_read_timeout_never_reconnects_or_replays_post(self):
        connected = FakeSocket(read_error=TimeoutError('private response data'))
        with patch('ctn_data.proxy_worker.socket.getaddrinfo', return_value=[V4]), patch(
            'ctn_data.proxy_worker.socket.socket', return_value=connected
        ) as factory:
            connection = BackendHTTPConnection('backend.invalid', 8765, timeout=15)
            connection.request('POST', '/api/worker/result', body=b'private payload')
            with self.assertRaises(TimeoutError):
                connection.getresponse()
            connection.close()
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(len(connected.endpoints), 1)
        self.assertEqual(sum(chunk.startswith(b'POST ') for chunk in connected.sent), 1)
        self.assertEqual(connected.timeouts[-1], 15)

    def test_only_backend_opener_changes_and_remote_proxy_remains_explicit(self):
        with patch.object(Path, 'mkdir'):
            worker = ProxyWorker('http://backend.invalid:8765', 'http://vpn.invalid:8888', 'test-worker', Path('mock-state/token'), 'mock-state')
        self.assertTrue(any(type(handler) is BackendHTTPHandler for handler in worker.local.handlers))
        self.assertFalse(any(isinstance(handler, BackendHTTPHandler) for handler in worker.remote.handlers))
        self.assertTrue(any(type(handler) is HTTPHandler for handler in worker.remote.handlers))
        self.assertTrue(any(isinstance(handler, NoRedirect) for handler in worker.local.handlers))
        self.assertTrue(any(isinstance(handler, NoRedirect) for handler in worker.remote.handlers))
        remote_proxy = next(handler for handler in worker.remote.handlers if isinstance(handler, ProxyHandler))
        self.assertEqual(remote_proxy.proxies, {'http': 'http://vpn.invalid:8888', 'https': 'http://vpn.invalid:8888'})


if __name__ == '__main__':
    unittest.main()
