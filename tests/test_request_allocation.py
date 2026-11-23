"""Counter allocation regressions with isolated SQLite and virtual clocks."""
import heapq
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from ctn_data import request_allocation
from ctn_data.coordination import cancel_wait, extend_cooldown, permit
from ctn_data.db import connect
from ctn_data.ingest import save_counters, save_posts
from ctn_data.transport import BudgetExceeded, ReadOnlyTransport, RequestDeferred
from tests.test_collection import BaseDatabase, FakeClock, Opener


class RequestAllocationTests(BaseDatabase):
    @staticmethod
    def counter(post_id):
        return '/api/posts/' + str(post_id) + '/counters'

    def state(self):
        row = self.conn.execute("SELECT value FROM checkpoints WHERE key='coordination:counters'").fetchone()
        return json.loads(row['value']) if row else {}

    def save_state(self, state):
        with self.conn:
            self.conn.execute('''INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at''',
                ('coordination:counters', json.dumps(state), '1970-01-01T00:16:40Z'))

    def waiting(self):
        return self.state().get('allocation', {}).get('waiting', [])

    def test_single_actor_keeps_existing_endpoint_budget(self):
        granted = []
        for index in range(20):
            now = 1000 + index * .301
            self.assertTrue(permit(self.conn, self.counter(1), 'backend', now)['allowed'])
            granted.append(now)
            before = self.state()
            rejected = permit(self.conn, self.counter(1), 'backend', now + .1)
            self.assertFalse(rejected['allowed'])
            after = self.state()
            self.assertEqual(after['next_at'], before['next_at'])
            self.assertEqual(after['backend_grants'], before['backend_grants'])
        self.assertEqual(self.state()['backend_grants'], 20)
        self.assertTrue(all(b - a >= .3 for a, b in zip(granted, granted[1:])))

    def test_fast_polls_cannot_indefinitely_overtake_waiting_proxy(self):
        self.assertTrue(permit(self.conn, self.counter(1), 'backend', 1000)['allowed'])
        self.assertFalse(permit(self.conn, self.counter(2), 'proxy:one', 1000.01)['allowed'])
        overtakes = 0
        now = 1000.05
        while now < 1000.91:
            response = permit(self.conn, self.counter(1), 'backend', now)
            overtakes += response['allowed']
            now += .01
        self.assertLessEqual(overtakes, 2)
        self.assertEqual([row['actor'] for row in self.waiting()].count('proxy:one'), 1)
        # The proxy is physically ready before the bounded handoff expires.
        ready_at = self.state()['next_at'] + .001
        self.assertTrue(permit(self.conn, self.counter(2), 'proxy:one', ready_at)['allowed'])
        self.assertEqual(self.state()['proxy:one_grants'], 1)

    def test_offline_demand_expires_and_recovered_actor_can_return(self):
        permit(self.conn, self.counter(1), 'backend', 1000)
        permit(self.conn, self.counter(2), 'proxy:offline', 1000.01)
        recovered_at = 1000 + request_allocation.DEMAND_TTL + .1
        self.assertTrue(permit(self.conn, self.counter(1), 'backend', recovered_at)['allowed'])
        self.assertNotIn('proxy:offline', [row['actor'] for row in self.waiting()])
        self.assertTrue(permit(self.conn, self.counter(2), 'proxy:offline', recovered_at + .301)['allowed'])
        self.assertEqual(self.state()['backend_grants'], 2)

    def test_cancel_matches_current_path_and_repeated_polls_do_not_duplicate_actor(self):
        permit(self.conn, self.counter(1), 'backend', 1000)
        for index in range(5):
            permit(self.conn, self.counter(2), 'proxy:one', 1000.01 + index * .01)
        permit(self.conn, self.counter(3), 'proxy:one', 1000.06)
        self.assertEqual(len(self.waiting()), 1)
        self.assertEqual(self.waiting()[0]['path'], self.counter(3))
        retry_at = self.state()['allocation']['actors']['proxy:one']['retry_at']
        cancel_wait(self.conn, self.counter(2), 'proxy:one')
        self.assertEqual(len(self.waiting()), 1)
        self.assertEqual(self.state()['allocation']['actors']['proxy:one']['retry_at'], retry_at)
        cancel_wait(self.conn, self.counter(3), 'proxy:one')
        self.assertEqual(self.waiting(), [])
        self.assertNotIn('retry_at', self.state()['allocation']['actors']['proxy:one'])
        self.assertEqual(self.state()['backend_grants'], 1)
        self.assertAlmostEqual(self.state()['next_at'], 1000.3)
        for post_id in (2, 3):
            self.assertIsNone(self.conn.execute('SELECT value FROM checkpoints WHERE key=?',
                ('coordination:post:' + str(post_id),)).fetchone())
        self.assertTrue(permit(self.conn, self.counter(1), 'backend', 1000.301)['allowed'])

    def test_cancelling_later_waiter_preserves_existing_head_deadline(self):
        permit(self.conn, self.counter(1), 'backend', 1000)
        permit(self.conn, self.counter(2), 'proxy:a', 1000.01)
        permit(self.conn, self.counter(3), 'proxy:b', 1000.02)
        turn = self.state()['allocation']['turn']
        cancel_wait(self.conn, self.counter(3), 'proxy:b')
        self.assertEqual(self.state()['allocation']['turn'], turn)
        self.assertEqual([row['actor'] for row in self.waiting()], ['proxy:a'])

    def test_concurrent_distinct_posts_share_one_slot_and_keep_all_demand(self):
        def attempt(index):
            conn = connect(self.path)
            try:
                return permit(conn, self.counter(100 + index), 'worker_' + str(index), 1000)
            finally:
                conn.close()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(8)))
        self.assertEqual(sum(result['allowed'] for result in results), 1)
        self.assertEqual(len(self.waiting()), 7)
        self.assertEqual(len({row['actor'] for row in self.waiting()}), 7)
        self.assertEqual(sum(value for key, value in self.state().items() if key.endswith('_grants')), 1)

    def test_feed_endpoint_retains_pacing_without_counter_allocation(self):
        self.assertTrue(permit(self.conn, '/api/posts?sort=recent', 'backend', 1000)['allowed'])
        denied = permit(self.conn, '/api/posts?sort=trending', 'proxy:a', 1000.01)
        self.assertFalse(denied['allowed'])
        self.assertEqual(denied['reason'], 'pacing')
        self.assertTrue(permit(self.conn, '/api/posts', 'backend', 1008)['allowed'])
        state = json.loads(self.conn.execute("SELECT value FROM checkpoints WHERE key='coordination:feeds'").fetchone()['value'])
        self.assertNotIn('allocation', state)
        self.assertEqual(state['backend_grants'], 2)

    def test_recent_observation_removes_ineligible_demand_without_consuming_slot(self):
        permit(self.conn, self.counter(1), 'backend', 1000)
        permit(self.conn, self.counter(2), 'proxy:one', 1000.01)
        with self.conn:
            save_posts(self.conn, [{'post_id': 2, 'body': 'fixture'}], 'allocation-test')
            save_counters(self.conn, 2, {'like_count': 3}, '1970-01-01T00:16:40Z')
        denied = permit(self.conn, self.counter(2), 'proxy:one', 1000.02)
        self.assertEqual(denied['reason'], 'recent_sample')
        self.assertEqual(self.waiting(), [])
        self.assertEqual(self.state()['backend_grants'], 1)
        self.assertNotIn('proxy:one_grants', self.state())
        self.assertTrue(permit(self.conn, self.counter(1), 'backend', 1000.301)['allowed'])

    def test_object_lease_removes_ineligible_waiter_without_changing_owner(self):
        permit(self.conn, self.counter(1), 'backend', 1000)
        permit(self.conn, self.counter(2), 'proxy:one', 1000.01)
        with self.conn:
            self.conn.execute('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)',
                ('coordination:post:2', json.dumps({'actor': 'proxy:other', 'until': 1030}),
                 '1970-01-01T00:16:40Z'))
        denied = permit(self.conn, self.counter(2), 'proxy:one', 1000.02)
        self.assertEqual(denied['reason'], 'post_in_flight')
        self.assertEqual(self.waiting(), [])
        lease = json.loads(self.conn.execute("SELECT value FROM checkpoints WHERE key='coordination:post:2'").fetchone()['value'])
        self.assertEqual(lease, {'actor': 'proxy:other', 'until': 1030})
        self.assertTrue(permit(self.conn, self.counter(3), 'backend', 1000.301)['allowed'])

    def test_shared_cooldown_clears_demand_without_shortening_or_catchup(self):
        permit(self.conn, self.counter(1), 'backend', 1000)
        permit(self.conn, self.counter(2), 'proxy:one', 1000.01)
        extend_cooldown(self.conn, 'counters', 1005)
        extend_cooldown(self.conn, 'counters', 1003)
        denied = permit(self.conn, self.counter(2), 'proxy:one', 1000.02)
        self.assertEqual(denied['reason'], 'cooldown')
        self.assertGreaterEqual(denied['retry_after_ms'], 4980)
        self.assertEqual(self.waiting(), [])
        self.assertEqual(self.state()['backend_grants'], 1)
        self.assertTrue(permit(self.conn, '/api/posts', 'backend', 1000.02)['allowed'])
        self.assertFalse(permit(self.conn, self.counter(1), 'backend', 1004.99)['allowed'])
        self.assertTrue(permit(self.conn, self.counter(2), 'proxy:one', 1005.01)['allowed'])
        self.assertFalse(permit(self.conn, self.counter(1), 'backend', 1005.01)['allowed'])
        self.assertEqual(self.state()['backend_grants'], 1)

    def test_legacy_checkpoint_keeps_grants_and_budget(self):
        self.save_state({'next_at': 1000.3, 'last_actor': 'backend', 'last_at': 1000,
                         'backend_grants': 17, 'legacy_extension': 'keep'})
        denied = permit(self.conn, self.counter(2), 'proxy:one', 1000.01)
        self.assertFalse(denied['allowed'])
        self.assertEqual(self.state()['backend_grants'], 17)
        self.assertTrue(permit(self.conn, self.counter(2), 'proxy:one', 1000.301)['allowed'])
        state = self.state()
        self.assertEqual(state['backend_grants'], 17)
        self.assertEqual(state['legacy_extension'], 'keep')
        self.assertEqual(state['allocation']['version'], 1)
        self.assertEqual(state['proxy:one_grants'], 1)

    def test_short_backend_wait_budget_cancels_readiness_before_deferring(self):
        clock, opener = FakeClock(), Opener([])
        transport = ReadOnlyTransport(self.conn, opener=opener, clock=clock,
            sleeper=clock.sleep, max_wait_seconds=.4, deadline_seconds=10,
            request_budget=1, max_retries=0)
        now = transport.epoch_base
        permit(self.conn, self.counter(1), 'backend', now)
        state = self.state()
        state['allocation'] = {'version': 1, 'mode': 'ready_queue',
            'actors': {'proxy:one': {'seen_at': now, 'grace': .6}},
            'waiting': [{'actor': 'proxy:one', 'path': self.counter(2),
                         'seen_at': now, 'bypassed': 2}],
            'turn': {'actor': 'proxy:one', 'until': now + .9}}
        self.save_state(state)
        with self.assertRaises(RequestDeferred):
            transport.get_json('social', self.counter(1))
        self.assertEqual(opener.calls, [])
        self.assertEqual(transport.attempts, 0)
        self.assertLessEqual(transport.waited, .4)
        self.assertNotIn('backend', [row['actor'] for row in self.waiting()])
        self.assertNotIn('retry_at', self.state()['allocation']['actors']['backend'])
        self.assertTrue(permit(self.conn, self.counter(2), 'proxy:one', now + .401)['allowed'])
        self.assertTrue(permit(self.conn, self.counter(2), 'proxy:one', now + .702)['allowed'])

    def test_exhausted_request_budget_withdraws_existing_readiness(self):
        clock, opener = FakeClock(), Opener([])
        transport = ReadOnlyTransport(self.conn, opener=opener, clock=clock,
            sleeper=clock.sleep, request_budget=1)
        now = transport.epoch_base
        permit(self.conn, self.counter(2), 'proxy:one', now)
        permit(self.conn, self.counter(1), 'backend', now + .01)
        transport.attempts = 1
        with self.assertRaises(BudgetExceeded):
            transport.get_json('social', self.counter(1))
        self.assertEqual(opener.calls, [])
        self.assertNotIn('backend', [row['actor'] for row in self.waiting()])
        self.assertNotIn('retry_at', self.state()['allocation']['actors']['backend'])

    def test_deadline_overrun_while_sleeping_withdraws_readiness(self):
        clock, opener = FakeClock(), Opener([])
        transport = ReadOnlyTransport(self.conn, opener=opener, clock=clock,
            sleeper=lambda delay: clock.sleep(delay + 1.1), deadline_seconds=1)
        permit(self.conn, self.counter(2), 'proxy:one', transport.epoch_base)
        with self.assertRaises(BudgetExceeded):
            transport.get_json('social', self.counter(1))
        self.assertEqual(opener.calls, [])
        self.assertNotIn('backend', [row['actor'] for row in self.waiting()])
        self.assertNotIn('retry_at', self.state()['allocation']['actors']['backend'])

    def simulate(self, *, seconds=60, backend_wait_budget=None, proxy_completion=.1):
        """A remote control poll adds 250ms; no real HTTP or wall-clock sleeps."""
        events = [(0., 0, 'backend', 0.), (.01, 1, 'proxy:a', 0.),
                  (.02, 2, 'proxy:b', 0.)]
        heapq.heapify(events)
        ids = {'backend': 10, 'proxy:a': 11, 'proxy:b': 12}
        granted, counts, cancelled, serial = [], Counter(), 0, 3
        by_actor = {actor: [] for actor in ids}
        while events:
            now, _, actor, waited = heapq.heappop(events)
            if now > seconds:
                break
            path = self.counter(ids[actor])
            result = permit(self.conn, path, actor, 1000 + now)
            if result['allowed']:
                granted.append(now)
                by_actor[actor].append(now)
                counts[actor] += 1
                delay = proxy_completion if actor.startswith('proxy:') else .1
                waited = 0.
            else:
                delay = result['retry_after_ms'] / 1000
                if (actor == 'backend' and backend_wait_budget is not None
                        and waited + delay > backend_wait_budget):
                    cancel_wait(self.conn, path, actor)
                    self.assertNotIn(actor, [row['actor'] for row in self.waiting()])
                    delay, waited = 1., 0.
                    cancelled += 1
                else:
                    waited += delay
            rpc = .25 if actor.startswith('proxy:') else 0
            heapq.heappush(events, (now + delay + rpc, serial, actor, waited))
            serial += 1
        self.assertTrue(all(b - a >= .3 - 1e-9 for a, b in zip(granted, granted[1:])))
        return counts, by_actor, cancelled

    def test_slow_permission_rpc_does_not_phase_lock_or_starve_ready_proxy(self):
        counts, granted, cancelled = self.simulate()
        self.assertEqual(cancelled, 0)
        self.assertGreaterEqual(sum(counts.values()), 120)
        for actor in ('backend', 'proxy:a', 'proxy:b'):
            self.assertGreaterEqual(counts[actor], 20)
            gaps = [b - a for a, b in zip(granted[actor], granted[actor][1:])]
            self.assertLess(max(gaps), 3.)

    def test_backend_400ms_cancel_preserves_progress_and_effective_throughput(self):
        counts, granted, cancelled = self.simulate(backend_wait_budget=.4)
        self.assertGreater(cancelled, 0)
        self.assertGreaterEqual(sum(counts.values()), 120)
        self.assertGreaterEqual(counts['backend'], 25)
        for actor in ('proxy:a', 'proxy:b'):
            self.assertGreaterEqual(counts[actor], 20)
            gaps = [b - a for a, b in zip(granted[actor], granted[actor][1:])]
            self.assertLess(max(gaps), 3.)

    def test_slow_remote_completion_loans_capacity_to_ready_local_route(self):
        counts, granted, _ = self.simulate(backend_wait_budget=.4, proxy_completion=1.)
        self.assertGreaterEqual(sum(counts.values()), 120)
        self.assertGreater(counts['backend'], counts['proxy:a'])
        self.assertGreater(counts['backend'], counts['proxy:b'])
        for actor in ('proxy:a', 'proxy:b'):
            self.assertGreaterEqual(counts[actor], 15)
            gaps = [b - a for a, b in zip(granted[actor], granted[actor][1:])]
            self.assertLess(max(gaps), 4.)
