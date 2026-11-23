"""One process-wide admission gate for every recorded model request (S4).

Agent steps, research, the concern radar, fact extraction and interactive
analysis used to be limited by two unrelated mechanisms. All of them now enter
this gate (ai_providers.complete with a ledger), which provides:

- one global concurrency limit;
- priority: a started action finishes first (write/check), then decisions,
  reading, interactive work, and background analysis last;
- background analysis only uses spare slots (``RESERVE`` stay free for Agents);
- fairness: among equal priorities, the role with fewer requests in flight
  goes first, then arrival order;
- an in-flight credit 402 shrinks the limit to what is running until its
  Retry-After passes, instead of every caller failing separately.

docs/AI_CALL_EFFICIENCY.md, section 8 (S4).
"""
from __future__ import annotations

import itertools
import threading
import time
from collections import Counter
from contextlib import contextmanager

PRIORITY = {'write': 0, 'check': 0, 'decide': 1, 'single': 1, 'reading': 2, 'agent': 2,
            'interactive': 3, 'connection_test': 3, 'radar': 4, 'research': 4, 'extract': 4}
BACKGROUND = 4
LIMIT = 6
RESERVE = 2


class Cancelled(Exception):
    """The caller's cancellation event was set while waiting for a slot."""


class _Ticket:
    __slots__ = ('priority', 'agent', 'seq')

    def __init__(self, priority, agent, seq):
        self.priority, self.agent, self.seq = priority, agent, seq


class Gate:
    def __init__(self, limit=LIMIT, reserve=RESERVE, clock=time.monotonic, poll_seconds=.2):
        self.limit, self.reserve, self.clock, self.poll_seconds = limit, reserve, clock, poll_seconds
        self._cond = threading.Condition()
        self._seq = itertools.count()
        self._waiting = []
        self._active = 0
        self._by_agent = Counter()
        self._reduced = None
        self._reduced_until = 0.0

    def _capacity(self):
        if self._reduced is not None and self.clock() < self._reduced_until:
            return self._reduced
        self._reduced = None
        return self.limit

    def _fits(self, ticket, capacity):
        if ticket.priority >= BACKGROUND:
            return self._active < max(1, capacity - self.reserve)
        return self._active < capacity

    def _rank(self, ticket):
        return ticket.priority, self._by_agent[ticket.agent] if ticket.agent else 0, ticket.seq

    def _admissible(self, ticket):
        capacity = self._capacity()
        candidates = [waiting for waiting in self._waiting if self._fits(waiting, capacity)]
        return bool(candidates) and min(candidates, key=self._rank) is ticket

    @contextmanager
    def slot(self, purpose, agent_id=None, cancellation_event=None):
        """Wait for a slot; yields the seconds spent waiting."""
        ticket = _Ticket(PRIORITY.get(purpose, PRIORITY['agent']), agent_id, next(self._seq))
        started = self.clock()
        with self._cond:
            self._waiting.append(ticket)
            try:
                while not self._admissible(ticket):
                    if cancellation_event is not None and cancellation_event.is_set():
                        raise Cancelled
                    self._cond.wait(self.poll_seconds)
            except BaseException:
                self._waiting.remove(ticket)
                self._cond.notify_all()
                raise
            self._waiting.remove(ticket)
            self._active += 1
            if agent_id:
                self._by_agent[agent_id] += 1
        try:
            yield max(0.0, self.clock() - started)
        finally:
            with self._cond:
                self._active -= 1
                if agent_id:
                    self._by_agent[agent_id] -= 1
                    if self._by_agent[agent_id] <= 0:
                        del self._by_agent[agent_id]
                self._cond.notify_all()

    def backoff(self, seconds):
        """An in-flight credit rejection: admit no more than is already running for a while."""
        with self._cond:
            self._reduced = max(1, min(self._capacity(), self._active - 1))
            self._reduced_until = max(self._reduced_until, self.clock() + max(1.0, float(seconds)))

    def snapshot(self):
        with self._cond:
            return {'limit': self.limit, 'capacity': self._capacity(), 'active': self._active,
                    'waiting': dict(Counter(ticket.priority for ticket in self._waiting))}


GATE = Gate()
