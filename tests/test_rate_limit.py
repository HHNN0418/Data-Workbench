import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from ctn_data import coordination
from ctn_data.transport import ReadOnlyTransport, RequestDeferred, rate_limit_delay
from tests.test_collection import BaseDatabase, FakeClock, Opener, Response


class RateLimitDelayTests(unittest.TestCase):
    def test_relative_reset_is_time_to_refill_the_whole_bucket(self):
        self.assertEqual(rate_limit_delay(300, 0, 60), .2)
        self.assertEqual(rate_limit_delay(200, 0, 60), .3)
        self.assertEqual(rate_limit_delay(10, 0, 60), 6)

    def test_missing_budget_or_remaining_tokens_needs_no_cooldown(self):
        for limit, remaining, reset in ((None, 0, 60), (0, 0, 60),
                                        (300, None, 60), (300, 1, 60), (300, 0, None)):
            with self.subTest(limit=limit, remaining=remaining, reset=reset):
                self.assertIsNone(rate_limit_delay(limit, remaining, reset))

    def test_absolute_reset_still_waits_until_the_timestamp(self):
        now = 1_800_000_000
        self.assertEqual(rate_limit_delay(300, 0, now+60, now=now), 60)
        self.assertEqual(rate_limit_delay(300, 0, now-1, now=now), 1)
        with patch('ctn_data.transport.time.time', return_value=now):
            self.assertEqual(rate_limit_delay(300, 0, now+60), 60)


class TransportRefillTests(BaseDatabase):
    def exercise_counter_refill(self, *, coordinated=True, proxy_url=None):
        clock = FakeClock()
        opener = Opener([
            Response({'like_count': 1}, 'https://social.legitreal.com/api/posts/1/counters',
                     {'X-RateLimit-Limit': '300', 'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '60'}),
            Response({'like_count': 2}, 'https://social.legitreal.com/api/posts/2/counters')])
        transport = ReadOnlyTransport(self.conn, opener=opener, clock=clock, sleeper=clock.sleep,
                                      coordinated=coordinated, proxy_url=proxy_url, max_wait_seconds=2)
        self.assertEqual(transport.get_json('social', '/api/posts/1/counters')['like_count'], 1)
        saved = json.loads(self.conn.execute(
            "SELECT value FROM checkpoints WHERE key='throttle:social:/api/posts/:id/counters'"
        ).fetchone()[0])['not_before_epoch']
        self.assertAlmostEqual(saved-transport.epoch_base, .2, delta=.001)
        self.assertEqual(transport.get_json('social', '/api/posts/2/counters')['like_count'], 2)
        self.assertEqual(len(opener.calls), 2)
        self.assertGreaterEqual(clock.now, .19)
        self.assertLess(clock.now, 1)

    def test_coordinated_direct_waits_for_one_token_and_keeps_local_pacing(self):
        self.exercise_counter_refill()

    def test_coordinated_proxy_uses_the_same_refill_rule(self):
        self.exercise_counter_refill(proxy_url='http://127.0.0.1:18881')

    def test_uncoordinated_transport_uses_the_same_refill_rule(self):
        self.exercise_counter_refill(coordinated=False)

    def test_429_retry_after_remains_the_shared_minimum(self):
        clock = FakeClock()
        headers = {'Retry-After': '7', 'X-RateLimit-Limit': '300',
                   'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '60'}
        error = HTTPError('https://social.legitreal.com/api/posts/1/counters',
                          429, 'limit', headers, io.BytesIO())
        opener = Opener([error])
        transport = ReadOnlyTransport(self.conn, opener=opener, clock=clock, sleeper=clock.sleep)
        with self.assertRaises(RequestDeferred):
            transport.get_json('social', '/api/posts/1/counters')
        blocked = coordination.permit(self.conn, '/api/posts/2/counters', 'browser',
                                      now=transport.epoch_base+1)
        self.assertEqual(blocked['reason'], 'cooldown')
        self.assertGreaterEqual(blocked['retry_after_ms'], 5999)
        self.assertLessEqual(blocked['retry_after_ms'], 6001)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(clock.waits, [])

    def test_refill_estimate_never_shortens_an_existing_server_cooldown(self):
        clock = FakeClock()
        opener = Opener([Response({}, 'https://social.legitreal.com/api/posts/1/counters',
                                  {'X-RateLimit-Limit': '300', 'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '60'})])
        transport = ReadOnlyTransport(self.conn, opener=opener, clock=clock, sleeper=clock.sleep)
        def in_flight_response(request, timeout):
            coordination.extend_cooldown(self.conn, 'counters', transport.epoch_base+120)
            return opener.open(request, timeout)
        transport.openers['social'] = type('InFlightOpener', (), {'open': staticmethod(in_flight_response)})()
        transport.get_json('social', '/api/posts/1/counters')
        self.assertGreaterEqual(coordination.cooldowns(self.conn)['counters']/1000,
                                transport.epoch_base+119.999)
