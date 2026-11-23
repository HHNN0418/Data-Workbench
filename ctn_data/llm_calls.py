"""Per-request LLM call ledger and per-purpose request policy.

One ``llm_calls`` row per model request, failures included. Rows hold only
counts, timings, fixed codes and bounded identifiers: never prompts, answers,
reasoning, upstream error bodies or credentials. Recording is best effort and
can never change a model result.

The policy only applies to OpenRouter connections, the only service with an
in-flight credit hold, sticky prompt-cache routing and an explicit 402 wait:
https://openrouter.ai/docs/api-reference/limits
https://openrouter.ai/docs/features/prompt-caching
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .db import connect, is_postgres

_LOGGER = logging.getLogger(__name__)

# max_tokens counts reasoning and visible output together. Each cap is several
# times the observed p99 of its step (docs/AI_CALL_EFFICIENCY.md, E3): it bounds
# the in-flight hold and runaway outputs, and should almost never truncate.
MAX_TOKENS = {
    'reading': 8000, 'decide': 12000, 'single': 20000, 'write': 8000, 'check': 6000,
    'extract': 12000, 'agent': 16000, 'radar': 24000, 'research': 24000,
    'interactive': 24000, 'connection_test': 4000,
    # D77: stance/concern labels for citizen posts (stance_labels), 15 posts per request.
    'label': 6000,
}
PURPOSES = frozenset(MAX_TOKENS)
# Light steps think less (D13/D19). Measured on one write request: auto 624
# reasoning tokens / 12.0 s, low 266 / 6.9 s, none 0 / 3.4 s but a body cut at
# the 500-character limit. Live, low did not reduce reading (median 3,907
# reasoning tokens; Baidu ran 12 of 118 to the 8,000 cap), so reading only
# chooses source IDs without reasoning. Decide and single-call decisions keep auto.
REASONING = {'reading': 'none', 'write': 'low', 'check': 'low', 'extract': 'low', 'label': 'low'}
# A 402 in-flight or 429 rejection happens before generation starts, so the
# identical request may be sent again after Retry-After within this budget.
RETRY_BUDGET_SECONDS = 60
_LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9 ._:/+()\-]{0,199}')
_COLUMNS = ('id', 'started_at', 'finished_at', 'purpose', 'agent_id', 'job_id', 'provider_id', 'model',
            'upstream', 'status', 'http_status', 'error_code', 'finish_reason', 'attempts', 'wait_ms',
            'ttft_ms', 'duration_ms', 'max_tokens', 'session_hash', 'prompt_tokens', 'cached_tokens',
            'cache_write_tokens', 'completion_tokens', 'reasoning_tokens', 'cost')


def infer_purpose(response_schema):
    """Name an agent request by its structured-output contract when the caller did not."""
    if not isinstance(response_schema, dict):
        return 'agent'
    required = set(response_schema.get('required') or ())
    properties = response_schema.get('properties') or {}
    if {'source_ids', 'reason'} <= required and 'action' not in required:
        return 'reading'
    if {'direction', 'concedes_no'} <= required:
        return 'check'
    if required == {'body'}:
        return 'write'
    if required == {'facts'}:
        return 'extract'
    if 'action' in required:
        return 'single' if 'body' in properties else 'decide'
    return 'agent'


def _openrouter(provider):
    try:
        return urlsplit(str((provider or {}).get('base_url') or '')).hostname == 'openrouter.ai'
    except ValueError:
        return False


def request_policy(provider, purpose, *, db_path=None, agent_id=None, job_id=None):
    """Keyword options for ai_providers.complete; empty transport changes off OpenRouter."""
    purpose = purpose if purpose in PURPOSES else 'agent'
    options = {}
    if db_path:
        options['ledger'] = {'db_path': db_path, 'purpose': purpose, 'agent_id': agent_id, 'job_id': job_id}
    if _openrouter(provider):
        options.update(max_tokens=MAX_TOKENS[purpose], retry_budget_seconds=RETRY_BUDGET_SECONDS)
        if purpose in REASONING:
            options['reasoning_effort'] = REASONING[purpose]
        # One session per role keeps its decide/write/check steps on one warm
        # upstream cache; background jobs get their own stable session.
        options['session_id'] = 'ctn-' + (str(agent_id)[:80] if agent_id else purpose)
    return options


def _stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _count(value):
    return value if type(value) is int and 0 <= value <= 10 ** 12 else None


def _seconds_ms(value):
    return round(value * 1000, 1) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _label(value, maximum=200):
    return value[:maximum] if isinstance(value, str) and _LABEL.fullmatch(value[:maximum]) else None


def row(ledger, *, provider, started, finished, usage=None, status='ok', http_status=None, error_code=None,
        finish_reason=None, upstream=None, attempts=1, wait_seconds=0.0, ttft_seconds=None,
        max_tokens=None, session_id=None):
    """Bounded, credential-free ledger row from one completed or failed request."""
    usage = usage if isinstance(usage, dict) else {}
    cost = usage.get('cost')
    return {
        'id': str(uuid.uuid4()), 'started_at': _stamp(started), 'finished_at': _stamp(finished),
        'purpose': ledger.get('purpose') if ledger.get('purpose') in PURPOSES else 'agent',
        'agent_id': _label(ledger.get('agent_id'), 80), 'job_id': _label(ledger.get('job_id'), 80),
        'provider_id': _label((provider or {}).get('id'), 80), 'model': _label((provider or {}).get('model')),
        'upstream': _label(upstream), 'status': status if status in ('ok', 'error', 'cancelled') else 'error',
        'http_status': http_status if type(http_status) is int and 0 <= http_status < 1000 else None,
        'error_code': _label(error_code, 60), 'finish_reason': _label(finish_reason, 40),
        'attempts': attempts if type(attempts) is int and attempts > 0 else 1,
        'wait_ms': _seconds_ms(wait_seconds), 'ttft_ms': _seconds_ms(ttft_seconds),
        'duration_ms': _seconds_ms(finished - started), 'max_tokens': _count(max_tokens),
        'session_hash': hashlib.sha256(session_id.encode()).hexdigest()[:16] if isinstance(session_id, str) else None,
        **{key: _count(usage.get(key)) for key in ('prompt_tokens', 'cached_tokens', 'cache_write_tokens',
                                                   'completion_tokens', 'reasoning_tokens')},
        'cost': cost if type(cost) in (int, float) and math.isfinite(cost) and 0 <= cost < 1000 else None,
    }


def record(db_path, values):
    """Insert one ledger row; a storage failure is logged and otherwise ignored."""
    if not db_path:
        return
    try:
        # connect() would create a missing SQLite directory and file; a ledger
        # must never resurrect a database that no longer exists.
        if not is_postgres(db_path) and str(db_path) != ':memory:' and not Path(str(db_path)).is_file():
            return
        conn = connect(db_path)
        try:
            conn.execute('INSERT INTO llm_calls(' + ','.join(_COLUMNS) + ') VALUES(' +
                         ','.join('?' for _ in _COLUMNS) + ')', tuple(values.get(key) for key in _COLUMNS))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        _LOGGER.warning('LLM call ledger write failed: exception_type=%s', type(exc).__name__)
