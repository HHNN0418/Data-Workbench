"""Process-wide model admission: priority, background reserve, fairness, cancellation and 402 backoff."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ctn_data import ai_providers as ai
from ctn_data import llm_gateway
from ctn_data.db import connect, initialize

OPENROUTER = {'id': 'or-1', 'base_url': 'https://openrouter.ai/api/v1', 'model': 'deepseek/deepseek-v4.1-flash',
              'protocol': 'openai_chat', 'api_key': 'fake-key', 'token_parameter': 'max_tokens'}
ANSWER = {'choices': [{'message': {'content': 'Ready'}, 'finish_reason': 'stop'}],
          'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}


class GateTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.gate = llm_gateway.Gate(limit=1, reserve=0, clock=lambda: self.now, poll_seconds=.01)

    def hold(self, gate, purpose='decide', agent=None):
        """Occupy one slot from another thread until released."""
        entered, release = threading.Event(), threading.Event()

        def run():
            with gate.slot(purpose, agent):
                entered.set()
                release.wait(5)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(entered.wait(5))
        return release, thread

    def queue(self, gate, purpose, agent, order):
        def run():
            with gate.slot(purpose, agent):
                order.append(purpose + ':' + str(agent))
                time.sleep(.02)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def wait_for_waiters(self, gate, count):
        deadline = time.monotonic() + 5
        while sum(gate.snapshot()['waiting'].values()) < count:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.005)

    def test_started_actions_then_decisions_then_reading_then_background(self):
        release, holder = self.hold(self.gate)
        order = []
        threads = [self.queue(self.gate, purpose, None, order) for purpose in ('radar', 'reading', 'decide', 'write')]
        self.wait_for_waiters(self.gate, 4)
        release.set()
        for thread in [holder, *threads]:
            thread.join(5)
        self.assertEqual(order, ['write:None', 'decide:None', 'reading:None', 'radar:None'])

    def test_background_only_uses_spare_slots(self):
        gate = llm_gateway.Gate(limit=3, reserve=2, clock=lambda: self.now, poll_seconds=.01)
        release, holder = self.hold(gate, 'decide', 'agent-1')
        admitted = threading.Event()

        def background():
            with gate.slot('extract'):
                admitted.set()
        thread = threading.Thread(target=background, daemon=True)
        thread.start()
        self.assertFalse(admitted.wait(.2))
        with gate.slot('reading', 'agent-2'):
            pass  # foreground work still has room while background waits
        release.set()
        holder.join(5)
        self.assertTrue(admitted.wait(5))
        thread.join(5)

    def test_role_with_fewer_requests_in_flight_goes_first(self):
        gate = llm_gateway.Gate(limit=2, reserve=0, clock=lambda: self.now, poll_seconds=.01)
        busy_release, busy = self.hold(gate, 'decide', 'agent-a')
        other_release, other = self.hold(gate, 'decide', 'agent-z')
        order = []
        first = self.queue(gate, 'decide', 'agent-a', order)
        self.wait_for_waiters(gate, 1)
        second = self.queue(gate, 'decide', 'agent-b', order)
        self.wait_for_waiters(gate, 2)
        other_release.set()
        other.join(5)
        for thread in (first, second):
            thread.join(.5)
        busy_release.set()
        for thread in (busy, first, second):
            thread.join(5)
        self.assertEqual(order[0], 'decide:agent-b')

    def test_cancellation_while_waiting_leaves_no_ticket(self):
        release, holder = self.hold(self.gate)
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(llm_gateway.Cancelled):
            with self.gate.slot('write', 'agent-1', cancelled):
                self.fail('must not be admitted')
        self.assertEqual(self.gate.snapshot()['waiting'], {})
        release.set()
        holder.join(5)
        self.assertEqual(self.gate.snapshot()['active'], 0)

    def test_in_flight_backoff_admits_no_more_than_already_running_until_it_expires(self):
        gate = llm_gateway.Gate(limit=4, reserve=0, clock=lambda: self.now, poll_seconds=.01)
        holds = [self.hold(gate, 'decide', 'agent-' + str(i)) for i in range(3)]
        gate.backoff(30)
        self.assertEqual(gate.snapshot()['capacity'], 2)
        admitted = threading.Event()

        def late():
            with gate.slot('write', 'agent-9'):
                admitted.set()
        thread = threading.Thread(target=late, daemon=True)
        thread.start()
        self.assertFalse(admitted.wait(.2))
        self.now += 31
        self.assertTrue(admitted.wait(5))
        for release, holder in holds:
            release.set()
            holder.join(5)
        thread.join(5)
        self.assertEqual(gate.snapshot()['capacity'], 4)


class CompleteIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'data.sqlite3'
        initialize(self.db)
        self.gate = llm_gateway.Gate(limit=1, reserve=0, poll_seconds=.01)
        patch.object(llm_gateway, 'GATE', self.gate).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.temp.cleanup)

    def ledger(self, purpose='write'):
        return {'db_path': str(self.db), 'purpose': purpose, 'agent_id': 'agent-1', 'job_id': 'job-1'}

    def test_recorded_requests_wait_for_a_slot_and_record_the_wait(self):
        entered, release = threading.Event(), threading.Event()

        def holder():
            with self.gate.slot('decide', 'agent-0'):
                entered.set()
                release.wait(5)
        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(entered.wait(5))
        threading.Timer(.3, release.set).start()
        with patch.object(ai, '_request_json', return_value=dict(ANSWER)):
            ai.complete(dict(OPENROUTER), [{'role': 'user', 'content': 'Test'}], ledger=self.ledger())
        thread.join(5)
        conn = connect(self.db)
        try:
            wait_ms = conn.execute('SELECT wait_ms FROM llm_calls').fetchone()[0]
        finally:
            conn.close()
        self.assertGreaterEqual(wait_ms, 200)

    def test_unrecorded_requests_bypass_the_gate(self):
        with self.gate.slot('decide', 'agent-0'):
            with patch.object(ai, '_request_json', return_value=dict(ANSWER)):
                result = ai.complete(dict(OPENROUTER), [{'role': 'user', 'content': 'Test'}])
        self.assertEqual(result['text'], 'Ready')

    def test_cancelled_while_queued_is_a_499_without_a_request(self):
        cancelled = threading.Event()
        cancelled.set()
        with self.gate.slot('decide', 'agent-0'), patch.object(ai, '_request_json') as request, \
                self.assertRaises(ai.ProviderError) as caught:
            ai.complete(dict(OPENROUTER), [{'role': 'user', 'content': 'Test'}], cancellation_event=cancelled,
                        ledger=self.ledger())
        self.assertEqual(caught.exception.status_code, 499)
        request.assert_not_called()

    def test_in_flight_rejection_shrinks_the_gate(self):
        error = ai.ProviderError('AI 服务在途额度暂满，请稍后重试', 429, diagnostics={'error_code': 'in_flight_budget'})
        error.retry_after = 20
        with patch.object(ai, '_request_json', side_effect=error), patch.object(self.gate, 'backoff') as backoff, \
                self.assertRaises(ai.ProviderError):
            ai.complete(dict(OPENROUTER), [{'role': 'user', 'content': 'Test'}], ledger=self.ledger())
        backoff.assert_called_once_with(20)


if __name__ == '__main__':
    unittest.main()
