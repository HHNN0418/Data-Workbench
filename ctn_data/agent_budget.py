"""Persistent team budgets for autonomous competition actors.

All network work runs outside transactions. Home reads share the collector's
feed gate; SDK response budgets and Retry-After extend the same durable gates.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager, nullcontext
import math
import logging
import threading
import time
from urllib.parse import urlsplit
import uuid

from . import ai_providers, coordination, llm_calls
from .db import connect
from .agent_storage_retry import is_transient_storage_error, storage_error_code

# Published route allowances are shared by the team, with continuous refill.
# Server response headers may replace these defaults. There are no additional
# role/day caps or configurable minimum gaps between social actions.
LIMITS = {'post': 3.0, 'like': 30.0, 'unlike': 30.0, 'follow': 30.0, 'unfollow': 30.0,
          'delete_post': 30.0, 'feed': 10.0, 'read': 300.0,
          'auth': 5.0, 'notification': 300.0, 'resolve': 60.0, 'report': 3.0}
_CONTEXT = threading.local()
_PRIORITY = threading.local()
# Order for the shared post allowance (D24/D26). A deferred ranked post keeps
# the next slot until it returns, so automatic drafts cannot keep jumping a
# confirmed manual post or a reply to someone who answered us.
PUBLISH_PRIORITY = {'manual': 0, 'notification_reply': 1, 'reply': 2, 'post': 3}
PRIORITY_BUCKETS = frozenset(('post',))
PRIORITY_HOLD_GRACE_SECONDS = 30
MODEL_LEASE_RENEW_SECONDS = 20
MODEL_LEASE_TTL_SECONDS = 90
MODEL_LEASE_RETRY_ATTEMPTS = 3
MODEL_LEASE_RETRY_SECONDS = .25
_LOGGER = logging.getLogger(__name__)


def _runtime_change(controller, callback):
    """Lease bookkeeping never needs draft, memory or quota migrations."""
    change = getattr(controller.store, 'change_runtime', None) or controller.store.change
    return change(callback)


def _budget_change(controller, callback):
    """Quota updates share their lock with business writes, not their payload."""
    change = getattr(controller.store, 'change_budget', None) or controller.store.change
    return change(callback)


def _runtime_change_retry(controller, callback, cancellation, deadline):
    """Retry only an idempotent storage operation, never the model request."""
    for attempt in range(MODEL_LEASE_RETRY_ATTEMPTS):
        if cancellation is not None and cancellation.is_set():
            raise ai_providers.ProviderError('AI 任务已取消', 499)
        if time.monotonic() >= deadline:
            raise ai_providers.ProviderError('模型请求租约已过期，未启动本次请求', 409)
        try:
            return _runtime_change(controller, callback)
        except Exception as exc:
            remaining = deadline - time.monotonic()
            if (not is_transient_storage_error(exc)
                    or attempt + 1 >= MODEL_LEASE_RETRY_ATTEMPTS
                    or remaining <= MODEL_LEASE_RETRY_SECONDS):
                raise
            _LOGGER.warning('Model lease storage retry (%s, code=%s)',
                            type(exc).__name__, storage_error_code(exc))
            delay = min(MODEL_LEASE_RETRY_SECONDS * (attempt + 1), remaining)
            if cancellation is not None:
                cancellation.wait(delay)
            else:
                time.sleep(delay)


class Deferred(Exception):
    status = status_code = 429

    def __init__(self, bucket, delay, reason='队伍请求额度等待中', *, quota_policy=False):
        self.bucket = bucket
        self.quota_policy = quota_policy
        self.retry_after = max(.05, float(delay))
        super().__init__(f'{reason}，约 {math.ceil(self.retry_after)} 秒后继续')


def _number(value, default=None):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else default
    except (TypeError, ValueError, OverflowError):
        return default


def _value(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')


def _base(bucket):
    return bucket.removeprefix('failed:').split(':', 1)[0]


def _request_bucket(bucket, function):
    # Most reads and the two auth routes each have their own official budget.
    # Do not turn all those independent routes into one local bottleneck.
    name = getattr(function, '__name__', '')
    if bucket in ('read', 'auth') and isinstance(name, str) and name.isidentifier():
        return bucket + ':' + ('me' if name == 'account_from_token' else name)
    return bucket


def _allowance(bucket, state):
    default = LIMITS.get(_base(bucket))
    limit = _number(state.get('limit'), default)
    if limit is None or limit <= 0:
        return None
    return limit


def _tokens(state, now, limit):
    available = _number(state.get('tokens'), limit)
    updated = _number(state.get('token_at'), now)
    period = _number(state.get('refill_period'), 60) or 60
    return min(limit, available + max(0, now - updated) * limit / period)


def _policy_cap(data, bucket):
    if bucket.startswith(('failed:', 'llm:')):
        return None
    value = data['rate'].get('quota_policy', {}).get('limits', {}).get(_base(bucket))
    return value if type(value) is int and value > 0 else None


def _policy_tokens(data, bucket, state, now, cap):
    ledger = data['rate'].get('quota_buckets', {}).get(bucket)
    if ledger:
        old_cap = _number(ledger.get('rate'), cap)
        return min(cap, _tokens(ledger, now, old_cap))
    official = _allowance(bucket, state)
    return min(cap, _tokens(state, now, official)) if official else cap


@contextmanager
def actor(controller, agent_id):
    """Attribute this thread's SDK attempts without giving roles fixed shares."""
    previous = getattr(_CONTEXT, 'actor', None)
    _CONTEXT.actor = (controller, agent_id)
    try:
        yield
    finally:
        _CONTEXT.actor = previous


def _record_usage(controller, bucket, outcome):
    """Best-effort SDK attempt telemetry; never affect the remote outcome."""
    current = getattr(_CONTEXT, 'actor', None)
    agent_id = current[1] if current and current[0] is controller else ''
    def record(data, conn):
        now = controller.clock()
        second = int(now)
        history = data['rate'].setdefault('quota_usage', {})
        for key in list(history):
            if int(key) < second - 59:
                history.pop(key)
        row = history.setdefault(str(second), {}).setdefault(bucket, {}).setdefault(agent_id, {})
        row[outcome] = row.get(outcome, 0) + 1
        totals = data['rate'].setdefault('quota_usage_totals', {}).setdefault(agent_id, {})
        total = totals.setdefault(bucket, {})
        total[outcome] = total.get(outcome, 0) + 1
        if outcome == 'attempts':
            total['last_attempt_at'] = _stamp(now)
    try:
        _budget_change(controller, record)
    except Exception:
        pass


@contextmanager
def publish_priority(level, key=None):
    """Rank this thread's post reservations (PUBLISH_PRIORITY); ``key`` names its draft."""
    previous = getattr(_PRIORITY, 'current', None)
    _PRIORITY.current = (level, None if key is None else str(key)[:120])
    try:
        yield
    finally:
        _PRIORITY.current = previous


def priority_for(draft):
    """Manual work, then replies to people who answered us, other replies, and posts."""
    if not isinstance(draft, dict):
        return PUBLISH_PRIORITY['post']
    if draft.get('origin') == 'manual' or draft.get('manual_only') is True or draft.get('manual_prepared') is True:
        return PUBLISH_PRIORITY['manual']
    if draft.get('action') == 'reply':
        return PUBLISH_PRIORITY['notification_reply' if draft.get('notification_ids') else 'reply']
    return PUBLISH_PRIORITY['post']


def _holds_ahead(data, bucket, now, level, key):
    """Unexpired holds of strictly higher priority than this caller (unranked callers rank last)."""
    holds = data['rate'].get('priority_holds', {}).get(bucket)
    if not holds:
        return 0
    for name in [name for name, hold in holds.items() if _number(hold.get('until'), 0) <= now]:
        holds.pop(name)
    rank = len(PUBLISH_PRIORITY) if level is None else level
    return sum(1 for name, hold in holds.items() if name != key and _number(hold.get('priority'), rank) < rank)


def _hold(controller, bucket, key, level, delay):
    """A deferred ranked caller keeps a claim on the next slot until it returns or expires."""
    def save(data, conn):
        data['rate'].setdefault('priority_holds', {}).setdefault(bucket, {})[key] = {
            'priority': level, 'until': controller.clock() + delay + PRIORITY_HOLD_GRACE_SECONDS}
    try:
        _budget_change(controller, save)
    except Exception:
        pass  # Bookkeeping must never replace the original wait.


def release_priority(controller, bucket, key):
    """Give up a claim on the next slot, for example when a queued manual post is abandoned."""
    def drop(data, conn):
        data['rate'].get('priority_holds', {}).get(bucket, {}).pop(str(key)[:120], None)
    _budget_change(controller, drop)


def reserve(controller, bucket, *, minimum_interval=None):
    if _base(bucket) not in LIMITS and not bucket.startswith('llm:'):
        raise ValueError('未知的 Agent 额度类别')
    ranked = getattr(_PRIORITY, 'current', None) if bucket in PRIORITY_BUCKETS else None
    level, key = ranked or (None, None)

    def claim(data, conn):
        # Read time after taking the database lock. A queued contender must
        # never move token_at backwards and mint already-spent quota again.
        now = controller.clock()
        state = data['rate'].setdefault('buckets', {}).setdefault(bucket, {})
        local = data['rate'].get('quota_buckets', {}).get(bucket, {})
        now = max(now, _number(state.get('token_at'), 0), _number(local.get('token_at'), 0))
        # Legacy next_at represented a local fixed gap. Only explicit model
        # pacing and server cooldowns survive the move to official allowances.
        until = _number(state.get('not_before'), 0)
        if minimum_interval:
            until = max(until, _number(state.get('next_at'), 0))
        if until > now:
            raise Deferred(bucket, until - now)
        # Each pending higher-priority post keeps one slot for itself (D24/D26).
        need = 1 + (_holds_ahead(data, bucket, now, level, key) if bucket in PRIORITY_BUCKETS else 0)
        limit = _allowance(bucket, state)
        tokens = None
        if limit is not None:
            tokens = _tokens(state, now, limit)
            if tokens < need - 1e-9:
                raise Deferred(bucket, (need - tokens) * (_number(state.get('refill_period'), 60) or 60) / limit)
        cap = _policy_cap(data, bucket)
        if cap is not None:
            policy_tokens = _policy_tokens(data, bucket, state, now, cap)
            if policy_tokens < need - 1e-9:
                raise Deferred(bucket, (need - policy_tokens) * 60 / cap,
                               '全队自定义额度等待中', quota_policy=True)
        if key is not None:
            data['rate'].get('priority_holds', {}).get(bucket, {}).pop(key, None)
        if tokens is not None:
            state.update(tokens=max(0, tokens - 1), token_at=now)
        if cap is not None:
            data['rate'].setdefault('quota_buckets', {})[bucket] = {
                'tokens': max(0, policy_tokens - 1), 'token_at': now,
                'rate': cap, 'refill_period': 60}
        interval = max(0, float(minimum_interval or 0))
        state.update(next_at=now + interval, last_attempt_at=_stamp(now),
                     granted=int(state.get('granted', 0)) + 1)
        return now

    try:
        now = _budget_change(controller, claim)
    except Deferred as exc:
        if key is not None and level is not None:
            _hold(controller, bucket, key, level, exc.retry_after)
        raise
    if bucket == 'feed':
        conn = connect(controller.db_path)
        try:
            permit = coordination.permit(conn, '/api/posts', 'agent-home', now=now)
        finally:
            conn.close()
        if not permit['allowed']:
            _refund(controller, bucket, include_policy=True)
            raise Deferred(bucket, permit['retry_after_ms'] / 1000, '共享信息流额度等待中')


def _refund(controller, bucket, *, include_policy=False):
    """Definitively failed writes consume the server's separate failure quota."""
    def refund(data, conn):
        now = controller.clock()
        state = data['rate'].setdefault('buckets', {}).setdefault(bucket, {})
        now = max(now, _number(state.get('token_at'), 0))
        limit = _allowance(bucket, state)
        if limit is not None:
            state.update(tokens=min(limit, _tokens(state, now, limit) + 1), token_at=now)
        if include_policy:
            ledger = data['rate'].get('quota_buckets', {}).get(bucket)
            if ledger:
                now = max(now, _number(ledger.get('token_at'), 0))
                cap = _number(ledger.get('rate'))
                if cap:
                    ledger.update(tokens=min(cap, _tokens(ledger, now, cap) + 1), token_at=now)
    _budget_change(controller, refund)


def cooldown(controller, bucket, delay):
    delay = min(604800.0, max(1.0, _number(delay, 60)))
    def save(data, conn):
        until = controller.clock() + delay
        state = data['rate'].setdefault('buckets', {}).setdefault(bucket, {})
        state['not_before'] = max(_number(state.get('not_before'), 0), until)
        state['last_limited_at'] = _stamp(controller.clock())
        return state['not_before']

    until = _budget_change(controller, save)
    if bucket == 'feed':
        conn = connect(controller.db_path)
        try:
            coordination.extend_cooldown(conn, 'feeds', until)
        finally:
            conn.close()


def observe_rate(controller, rate):
    """SDK callback: ordinary success must never be converted into an error."""
    current = getattr(_CONTEXT, 'current', None)
    if current is None or current[0] is not controller:
        return
    # The callback runs before the SDK returns/raises. Delay applying it until
    # we know whether the headers describe the success or failed-request quota.
    current[2].append(rate)


def _observe(controller, bucket, rate):
    limit = _number(_value(rate, 'limit'))
    remaining = _number(_value(rate, 'remaining'))
    reset = _number(_value(rate, 'reset'))
    def save(data, conn):
        now = controller.clock()
        state = data['rate'].setdefault('buckets', {}).setdefault(bucket, {})
        now = max(now, _number(state.get('token_at'), 0))
        previous_limit = _allowance(bucket, state)
        available = _tokens(state, now, previous_limit) if previous_limit else None
        state.update(limit=limit, remaining=remaining, reset_seconds=reset,
                     observed_at=_stamp(now))
        if limit is not None and limit > 0 and remaining is not None:
            # A header may also include requests made by another team client.
            # Never invent more remaining slots than the server reported.
            available = remaining if previous_limit != limit or available is None else min(available, remaining)
            state.update(tokens=min(limit, available), token_at=now)
        if remaining == 0 and reset is not None:
            # Official buckets refill steadily; reset is time until full.
            wait = max(1.0, reset / max(1.0, limit or 1))
            if reset > 0:
                state['refill_period'] = reset
            state['not_before'] = max(_number(state.get('not_before'), 0), now + wait)

    _budget_change(controller, save)
    if bucket == 'feed' and remaining == 0 and reset is not None:
        cooldown(controller, bucket, max(1.0, reset / max(1.0, limit or 1)))
    if bucket.startswith('failed:') and remaining == 0 and reset is not None:
        cooldown(controller, bucket.removeprefix('failed:'), max(1.0, reset / max(1.0, limit or 1)))


def call(controller, bucket, function, *args, **kwargs):
    """Wait at most one second for a local slot, then invoke exactly once.

    Short sequential reads need to progress through account and target checks.
    Longer waits return to the scheduler; no transaction is held while waiting.
    """
    bucket = _request_bucket(bucket, function)
    deadline = time.monotonic() + 1.0
    while True:
        try:
            reserve(controller, bucket)
            break
        except Deferred as exc:
            if exc.retry_after > deadline - time.monotonic():
                raise
            time.sleep(exc.retry_after)
    previous = getattr(_CONTEXT, 'current', None)
    observations = []
    _CONTEXT.current = (controller, bucket, observations)
    response_bucket = bucket
    try:
        metadata = getattr(function, '__dict__', {})
        gate = metadata.get('_agent_dispatch_context')
        dispatch = metadata.get('_agent_dispatch_function', function)
        with gate() if gate else nullcontext():
            _record_usage(controller, bucket, 'attempts')
            result = dispatch(*args, **kwargs)
        _record_usage(controller, bucket, 'confirmed_success')
        return result
    except Exception as exc:
        from .agent_interaction_policy import PolicyBlocked
        from .agent_actions import DeliveryNotStarted
        if isinstance(exc, (PolicyBlocked, DeliveryNotStarted)):
            # A final dispatch refusal never reached the server. Return both
            # official and operator quota tokens and do not invent an attempt.
            try:
                _refund(controller, bucket, include_policy=True)
            except Exception:
                pass  # Preserve the proven-unsent outcome if bookkeeping fails.
            raise
        status = getattr(exc, 'status', None) or getattr(exc, 'status_code', None)
        _record_usage(controller, bucket, 'definite_failure' if status in (
            400, 401, 403, 404, 409, 422, 429) else 'uncertain')
        if status in (400, 401, 403, 404, 409, 422, 429) and _base(bucket) in (
                'post', 'like', 'unlike', 'follow', 'unfollow', 'delete_post'):
            try:
                _refund(controller, bucket)
            except Exception:
                pass  # Keep the original, definitive remote response.
            response_bucket = 'failed:' + bucket
        if status == 429 and not isinstance(exc, Deferred):
            response = getattr(exc, 'response', None)
            headers = getattr(response, 'headers', {}) or {}
            delay = max(_number(getattr(exc, 'retry_after', None), 0),
                        _number(headers.get('Retry-After'), 0),
                        max(((_number(_value(rate, 'reset'), 60) /
                               max(1, _number(_value(rate, 'limit'), 1)))
                             for rate in observations if _number(_value(rate, 'remaining')) == 0), default=0)) or 60
            try:
                exc.retry_after = delay
            except Exception:
                pass
            try:
                cooldown(controller, bucket, delay)
            except Exception:
                pass  # The action outbox also retains the remote retry delay.
        raise
    finally:
        try:
            for observed in observations:
                try:
                    _observe(controller, response_bucket, observed)
                except Exception:
                    # A quota diagnostic must never turn an accepted write
                    # into an uncertain result or cause that write to repeat.
                    pass
        finally:
            _CONTEXT.current = previous


def model_call(controller, provider, messages, *, max_output_tokens=None, response_schema=None,
               cancellation_event=None, on_progress=None, purpose=None, trace=None):
    """Track each request; only the competition model mandates one at a time.

    ``purpose`` names the step for its llm_calls ledger row and request policy
    (output cap, 402/429 wait, cache session); without it the step is inferred
    from the response schema. ``trace`` carries the role and generation job.
    """
    # Accept legacy callers' token option during rollout, but never impose it
    # on a model request. Account quotas and ownership still apply.
    if cancellation_event is not None and cancellation_event.is_set():
        raise ai_providers.ProviderError('AI 任务已取消', 499)
    host = urlsplit(provider.get('base_url', '')).hostname or ''
    official = host == 'llm.legitreal.com'
    # Agent decisions wait for a complete response, with explicit cancellation.
    # Renew the cross-process model lease while that one request is running.
    # Only opt agent requests into confirmed schema-capable services. Unknown
    # compatibility endpoints (including the competition model) keep their
    # prompt-based JSON contract and local validation. OpenRouter additionally
    # requires a supporting upstream in ai_providers.complete.
    structured_output = host == 'openrouter.ai' or (
        (host == 'api.x.ai' or host.endswith('.api.x.ai'))
        and str(provider.get('model', '')).startswith('grok-4'))
    # A team may save the official connection twice under different names.
    # Its server quota and one-request concurrency still belong to this team.
    identity = 'ctn-official' if official else str(provider.get('id') or 'default')[:80]
    token = str(uuid.uuid4())
    # Third-party connections have no competition concurrency limit. The
    # generation manager already prevents duplicate tasks for the same role.
    key = 'agent-model:' + identity + ('' if official else ':' + token)

    def claim(data, conn):
        # A caller may wait behind another transaction before this runs.
        now = controller.clock()
        lease = data['leases'].get(key, {})
        if _number(lease.get('until'), 0) > now:
            if lease.get('token') == token and lease.get('owner') == controller.owner:
                # A commit acknowledgement may be lost. Retrying this exact
                # not-yet-sent request must recognize its own durable claim.
                return time.monotonic() + lease['until'] - now
            raise Deferred('llm:' + identity, min(5, lease['until'] - now), '所选模型已有角色任务运行')
        data['leases'][key] = {'token': token, 'owner': controller.owner,
                               'until': now + MODEL_LEASE_TTL_SECONDS}
        return time.monotonic() + MODEL_LEASE_TTL_SECONDS

    claim_deadline = time.monotonic() + MODEL_LEASE_TTL_SECONDS
    confirmed_deadline = min(claim_deadline,
        _runtime_change_retry(controller, claim, cancellation_event, claim_deadline))
    heartbeat_stop = threading.Event()
    cancellation = cancellation_event if cancellation_event is not None else threading.Event()

    def owned_lease(data):
        lease = data['leases'].get(key, {})
        if (lease.get('token') != token or lease.get('owner') != controller.owner
                or _number(lease.get('until'), 0) <= controller.clock()):
            raise ai_providers.ProviderError('模型请求租约已失去，未保存本次结果', 409)
        return lease

    def renew_lease():
        nonlocal confirmed_deadline
        while not heartbeat_stop.wait(MODEL_LEASE_RENEW_SECONDS):
            def renew(data, conn):
                if time.monotonic() >= confirmed_deadline:
                    raise RuntimeError('model request lease renewal expired')
                lease = owned_lease(data)
                lease['until'] = controller.clock() + MODEL_LEASE_TTL_SECONDS
                return time.monotonic() + MODEL_LEASE_TTL_SECONDS

            for attempt in range(MODEL_LEASE_RETRY_ATTEMPTS):
                if heartbeat_stop.is_set() or cancellation.is_set():
                    return
                try:
                    # Wall time can jump backwards. Never revive a lease once
                    # its last confirmed duration has expired in real time.
                    if time.monotonic() >= confirmed_deadline:
                        raise RuntimeError('model request lease renewal expired')
                    renewed_deadline = _runtime_change(controller, renew)
                    confirmed_deadline = renewed_deadline
                    break
                except Exception as exc:
                    remaining = confirmed_deadline - time.monotonic()
                    retry = (is_transient_storage_error(exc)
                             and attempt + 1 < MODEL_LEASE_RETRY_ATTEMPTS
                             and remaining > MODEL_LEASE_RETRY_SECONDS)
                    if retry:
                        _LOGGER.warning('Model lease storage retry (%s, code=%s)',
                                        type(exc).__name__, storage_error_code(exc))
                        if heartbeat_stop.wait(min(MODEL_LEASE_RETRY_SECONDS * (attempt + 1), remaining)):
                            return
                        continue
                    # Ownership mismatches, expired leases and persistent
                    # storage failures stop this request; no model replay.
                    if not heartbeat_stop.is_set():
                        cancellation.set()
                    return

    heartbeat = threading.Thread(target=renew_lease, daemon=True)
    heartbeat.start()
    try:
        reserve(controller, 'llm:' + identity, minimum_interval=20 if official else 0)
        options = {'wait_for_completion': True, 'cancellation_event': cancellation}
        if on_progress is not None:
            options['on_progress'] = on_progress
        if structured_output and response_schema is not None:
            options['response_schema'] = response_schema
        trace = trace if isinstance(trace, dict) else {}
        options.update(llm_calls.request_policy(
            provider, purpose or llm_calls.infer_purpose(response_schema),
            db_path=getattr(controller, 'db_path', None), agent_id=trace.get('agent_id'),
            job_id=trace.get('job_id')))
        # Budget reservation can wait independently of this request's claim.
        # Check exact ownership again immediately before starting the model.
        def validate(data, conn):
            owned_lease(data)
        _runtime_change_retry(controller, validate, cancellation, confirmed_deadline)
        if cancellation.is_set() or time.monotonic() >= confirmed_deadline:
            raise ai_providers.ProviderError('AI 任务已取消或模型请求租约已失去', 499)
        result = ai_providers.complete(provider, messages, **options)
        if cancellation.is_set() or time.monotonic() >= confirmed_deadline:
            raise ai_providers.ProviderError('AI 任务已取消或模型请求租约已失去', 499)
        return result
    except Exception as exc:
        status = getattr(exc, 'status', None) or getattr(exc, 'status_code', None)
        if status == 429 and not isinstance(exc, Deferred):
            try:
                cooldown(controller, 'llm:' + identity, getattr(exc, 'retry_after', None) or (20 if official else 60))
            except Exception:
                pass
        raise
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=1)
        def release(data, conn):
            lease = data['leases'].get(key, {})
            if lease.get('token') == token and lease.get('owner') == controller.owner:
                data['leases'].pop(key, None)
        try:
            _runtime_change(controller, release)
        except Exception:
            pass  # The request lease expires; preserve the real model result.


def _remaining_wait(bucket, state, now):
    until = _number(state.get('not_before'), 0)
    if bucket == 'llm:ctn-official':
        until = max(until, _number(state.get('next_at'), 0))
    limit = _allowance(bucket, state)
    token_wait = (max(0, 1 - _tokens(state, now, limit)) *
                  (_number(state.get('refill_period'), 60) or 60) / limit) if limit else 0
    return max(0, until - now, token_wait)


def _route_status(data, bucket, now):
    state = data['rate'].get('buckets', {}).get(bucket, {})
    official = _allowance(bucket, state)
    period = _number(state.get('refill_period'), 60) or 60
    cap = _policy_cap(data, bucket)
    tokens = _tokens(state, now, official) if official else None
    wait = max(_remaining_wait(bucket, state, now),
               _number(data['rate'].get('not_before'), 0) - now)
    if cap is not None:
        local_tokens = _policy_tokens(data, bucket, state, now, cap)
        tokens = min(tokens, local_tokens) if tokens is not None else local_tokens
        wait = max(wait, max(0, 1 - local_tokens) * 60 / cap)
    per_minute = official * 60 / period if official else None
    effective = min(per_minute, cap) if per_minute is not None and cap is not None else (
        per_minute if per_minute is not None else cap)
    remaining = max(0, math.floor(tokens + 1e-9)) if tokens is not None else None
    return {'bucket': bucket, 'default_limit': LIMITS.get(_base(bucket)),
            'server_limit': state.get('limit'), 'server_remaining': state.get('remaining'),
            'server_period_seconds': period, 'configured_cap': cap, 'effective_limit': effective,
            'remaining': remaining, 'available_slots': remaining if wait <= 1e-9 else 0,
            'available': wait <= 1e-9, 'retry_after': max(0, wait),
            'observed_at': state.get('observed_at'), 'last_attempt_at': state.get('last_attempt_at'),
            'granted': int(state.get('granted', 0))}


def configure_quota(controller, bucket, cap):
    """Change one optional team cap without rewriting any official balance."""
    from .agent_control import AgentError
    if not isinstance(bucket, str) or bucket not in LIMITS:
        raise AgentError('额度类别无效；模型服务额度不在此管理')
    if cap is not None and (type(cap) is not int or cap <= 0 or _number(cap) is None):
        raise AgentError('自定义额度须为正整数；恢复官方额度请使用 null')

    def change(data, conn):
        now = controller.clock()
        old = data['rate'].get('quota_policy', {})
        limits = dict(old.get('limits', {}))
        if limits.get(bucket) == cap:
            return
        ledgers = data['rate'].setdefault('quota_buckets', {})
        routes = set(LIMITS) | set(data['rate'].get('buckets', {})) | set(ledgers)
        for route in routes:
            if route.startswith(('failed:', 'llm:')) or _base(route) != bucket:
                continue
            state = data['rate'].get('buckets', {}).get(route, {})
            official = _allowance(route, state)
            ledger = ledgers.get(route)
            settled_at = max(now, _number(state.get('token_at'), 0),
                             _number((ledger or {}).get('token_at'), 0))
            available = _tokens(state, settled_at, official) if official else cap
            if ledger:
                old_rate = _number(ledger.get('rate'), limits.get(bucket) or cap)
                local = _tokens(ledger, settled_at, old_rate)
                available = min(available, local) if available is not None else local
            new_rate = cap or (ledger or {}).get('rate') or limits.get(bucket)
            if new_rate is not None:
                ledgers[route] = {'tokens': min(available, new_rate), 'token_at': settled_at,
                                  'rate': new_rate, 'refill_period': 60}
        if cap is None:
            limits.pop(bucket, None)
        else:
            limits[bucket] = cap
        data['rate']['quota_policy'] = {'limits': limits, 'updated_at': _stamp(now),
                                        'version': int(old.get('version', 0)) + 1}
        # Re-evaluate only waits explicitly caused by the changed local policy.
        # Server cooldowns and unrelated retry/error times remain authoritative.
        for draft in data['drafts'].values():
            route = draft.get('quota_wait_bucket')
            if draft.get('state') == 'pending' and route and _base(route) == bucket:
                draft['next_attempt_at'] = now + _route_status(data, route, now)['retry_after']
        controller._event(data, 'quota_settings', '已更新全队共享动作额度',
                          details={'bucket': bucket, 'cap': cap})
    controller.store.change(change)
    return quota_status(controller)


def _usage(data, now):
    by_bucket, by_agent = {}, {}
    for second, routes in data['rate'].get('quota_usage', {}).items():
        if int(second) < int(now) - 59 or int(second) > int(now):
            continue
        for bucket, actors in routes.items():
            for agent_id, counts in actors.items():
                for event, count in counts.items():
                    team = by_bucket.setdefault(bucket, {})
                    team[event] = team.get(event, 0) + count
                    if agent_id:
                        own = by_agent.setdefault(agent_id, {}).setdefault(bucket, {})
                        own[event] = own.get(event, 0) + count
    return by_bucket, by_agent


def quota_status(controller):
    """Operational overview of this program's shared budgets and demand."""
    data, now = controller.store.read(), controller.clock()
    usage, agent_usage = _usage(data, now)
    routes = set(LIMITS) | {bucket for bucket in data['rate'].get('buckets', {})
                          if not bucket.startswith(('llm:', 'failed:')) and _base(bucket) in LIMITS}
    buckets = {bucket: {**_route_status(data, bucket, now),
                        'actions': ['post', 'reply', 'repost'] if bucket == 'post' else [_base(bucket)],
                        'usage_last_minute': usage.get(bucket, {}).get('attempts', 0),
                        'usage_outcomes': usage.get(bucket, {}),
                        'pending_actions': 0, 'auto_pending_actions': 0}
               for bucket in sorted(routes, key=lambda value: (list(LIMITS).index(_base(value)), value))}
    uncertain = {draft['agent_id'] for draft in data['drafts'].values()
                 if draft.get('state') in ('sending', 'uncertain')}
    agents = {}
    for agent in data['agents'].values():
        totals = data['rate'].get('quota_usage_totals', {}).get(agent['id'], {})
        last_at = max((row.get('last_attempt_at') or '' for row in totals.values()), default='') or None
        counts = agent_usage.get(agent['id'], {})
        agents[agent['id']] = {'agent_id': agent['id'], 'name': agent['name'],
            'enabled': agent['enabled'], 'mode': agent['mode'], 'account_id': agent.get('account_id'),
            'last_attempt_at': last_at, 'usage_last_minute': sum(row.get('attempts', 0) for row in counts.values()),
            'usage_by_bucket': {bucket: row.get('attempts', 0) for bucket, row in counts.items()},
            'usage_outcomes': {event: sum(row.get(event, 0) for row in counts.values())
                               for event in ('attempts', 'confirmed_success', 'definite_failure', 'uncertain')},
            'request_counts': {bucket: row.get('attempts', 0) for bucket, row in totals.items()},
            'pending_actions': 0, 'auto_pending_actions': 0, 'review_pending_actions': 0,
            'manual_pending_actions': 0, 'blocked_pending_actions': 0, 'pending_by_bucket': {}}
    for draft in data['drafts'].values():
        agent = data['agents'].get(draft.get('agent_id'))
        if not agent or draft.get('state') != 'pending':
            continue
        action = draft.get('action')
        bucket = 'post' if action in ('post', 'reply', 'repost') else action
        if bucket not in buckets:
            continue
        own = agents[agent['id']]
        own['pending_actions'] += 1
        own['pending_by_bucket'][bucket] = own['pending_by_bucket'].get(bucket, 0) + 1
        buckets[bucket]['pending_actions'] += 1
        if draft.get('origin') == 'manual' or draft.get('manual_only'):
            own['manual_pending_actions'] += 1
        elif agent['mode'] == 'review':
            own['review_pending_actions'] += 1
        elif (data['settings']['enabled'] and agent['enabled'] and agent.get('account_state') == 'ready'
              and agent.get('account_id') and agent['id'] not in uncertain
              and draft.get('persona_version') == agent.get('version')
              and draft.get('account_id') in (None, agent.get('account_id'))
              and action in agent.get('allowed_actions', [])):
            own['auto_pending_actions'] += 1
            buckets[bucket]['auto_pending_actions'] += 1
        else:
            own['blocked_pending_actions'] += 1
    return {'scope': 'whole_team', 'allocation': 'shared_on_demand', 'usage_window_seconds': 60,
            'usage_semantics': 'SDK request attempts by this program; not all team traffic or confirmed platform actions',
            'observed_at': _stamp(now), 'policy': data['rate'].get('quota_policy', {'limits': {}, 'updated_at': None}),
            'buckets': list(buckets.values()), 'agents': list(agents.values())}


def action_status(controller, allowed_actions):
    """Give the decision maker current timing for each independent action.

    This does not claim quota or create an action. Remaining slots are a local
    estimate anchored to official responses; the executor rechecks on delivery.
    """
    data, now = controller.store.read(), controller.clock()
    buckets = data['rate'].get('buckets', {})
    result = {}
    for action in allowed_actions:
        if action == 'skip':
            result[action] = {'available': True, 'bucket': None, 'retry_after': 0}
            continue
        bucket = 'post' if action in ('post', 'reply', 'repost') else action
        if bucket not in LIMITS:
            continue
        state = buckets.get(bucket, {})
        status = _route_status(data, bucket, now)
        result[action] = {'bucket': bucket, 'scope': 'shared_by_whole_team',
                          'available': status['available'], 'retry_after': status['retry_after'],
                          'limit': status['effective_limit'], 'remaining': status['remaining'],
                          'configured_cap': status['configured_cap'], 'server_limit': status['server_limit'],
                          'last_server_remaining': state.get('remaining')}
    return result


def public_status(controller, *, data=None):
    # Reuse the caller's snapshot when available: quota presentation must not
    # reload every draft and working memory or mix two different state reads.
    if data is None:
        data = controller.store.read()
    now = controller.clock()
    result = {}
    for bucket, state in data['rate'].get('buckets', {}).items():
        status = _route_status(data, bucket, now)
        result[bucket] = {**{key: state.get(key) for key in
                            ('limit', 'remaining', 'reset_seconds', 'observed_at', 'last_attempt_at')},
                          'retry_after': status['retry_after'], 'effective_limit': status['effective_limit'],
                          'configured_cap': status['configured_cap'], 'estimated_remaining': status['remaining']}
    return result
