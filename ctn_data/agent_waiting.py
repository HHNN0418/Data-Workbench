"""State helpers for expected resource waits, never for uncertain deliveries.

Call state-mutating helpers inside the controller store transaction. A waiting
job owns no worker or lease; the existing scheduler retries its saved action.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math


_WAIT_KINDS = frozenset(('quota', 'quota_policy', 'model', 'competition', 'platform', 'executor'))
_LEGACY_EXACT = frozenset((
    '统一执行器已有动作运行',
    '队伍请求额度等待中，已保存等待时间',
    '正在等待竞赛分配的生成机会',
    '正在等待竞赛分配的发文机会',
    'AI 服务限流或额度不足，请稍后重试',
))
_LEGACY_PREFIXES = (
    '队伍请求额度等待中，约 ',
    '全队自定义额度等待中，约 ',
    '共享信息流额度等待中，约 ',
    '所选模型已有角色任务运行，约 ',
    '平台限流等待中，约 ',
)


def _number(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else default
    except (TypeError, ValueError, OverflowError):
        return default


def _stamp(now):
    return datetime.fromtimestamp(now, timezone.utc).isoformat().replace('+00:00', 'Z')


def classify(exc):
    """Return a safe defer description, or None for a genuine task failure.

    Ordinary 409s protect configuration, cancellation, leases and uncertain
    actions. Only the explicitly labelled publisher contention is retryable.
    """
    from .agent_budget import Deferred
    from .agent_control import AgentError
    from .ai_providers import ProviderError

    if not isinstance(exc, (AgentError, ProviderError, Deferred)):
        return None
    status = getattr(exc, 'status_code', None) or getattr(exc, 'status', None)
    kind = getattr(exc, 'wait_kind', None)
    if status != 429 and not (status == 409 and kind == 'executor'):
        return None
    message = str(exc)[:2000]
    bucket = getattr(exc, 'quota_bucket', None) or getattr(exc, 'bucket', '')
    if kind not in _WAIT_KINDS:
        if getattr(exc, 'quota_policy', False):
            kind = 'quota_policy'
        elif str(bucket).startswith('llm:') or message.startswith(('AI 服务', '所选模型')):
            kind = 'model'
        elif message.startswith('正在等待竞赛分配的'):
            kind = 'competition'
        elif message.startswith('平台限流'):
            kind = 'platform'
        else:
            kind = 'quota'
    delay = _number(getattr(exc, 'retry_after', None), 5.0 if kind == 'executor' else 60.0)
    return {'wait_kind': kind, 'wait_message': message, 'retry_after': max(.05, delay)}


def role_lease_matches(job, lease):
    """Require both sides of the exact role/job/owner association."""
    if not isinstance(job, dict) or not isinstance(lease, dict):
        return False
    token, job_id, owner = job.get('lease_token'), job.get('id'), job.get('owner')
    return bool(isinstance(token, str) and token
                and isinstance(job_id, str) and job_id
                and isinstance(owner, str) and owner
                and lease.get('token') == token and lease.get('owner') == owner
                and lease.get('generation_job_id') == job_id)


def _legacy_wait(job):
    if job.get('status') != 'failed' or job.get('stage') == 'interrupted':
        return False
    message = job.get('error')
    return isinstance(message, str) and (
        message in _LEGACY_EXACT or message.startswith(_LEGACY_PREFIXES))


def complete_action(data, draft, now):
    """Complete only the current waiting job proven by a published receipt.

    Cancellation, recovery failures, replacement jobs and generic historical
    errors are intentionally never rewritten as success.
    """
    if not isinstance(draft, dict) or draft.get('state') != 'published':
        return False
    job_id = draft.get('generation_job_id')
    if not isinstance(job_id, str) or not job_id:
        return False
    agent = data.get('agents', {}).get(draft.get('agent_id'))
    job = agent.get('generation') if isinstance(agent, dict) else None
    if (not isinstance(job, dict) or job.get('id') != job_id
            or not (job.get('status') == 'waiting' or _legacy_wait(job))):
        return False
    # A malformed or historical multi-action job must not hide a write whose
    # result still needs reconciliation just because another action succeeded.
    if any(isinstance(row, dict) and row.get('agent_id') == draft.get('agent_id')
           and row.get('generation_job_id') == job_id
           and row.get('state') in ('sending', 'uncertain')
           for row in data.get('drafts', {}).values()):
        return False
    job.update(status='completed', stage='complete', updated_at=_stamp(now),
               heartbeat_until=0, error='')
    if draft.get('id'):
        job['draft_id'] = draft['id']
    for key in ('waiting_for', 'wait_kind', 'wait_message', 'retry_at'):
        job.pop(key, None)
    agent.update(last_error='', generation_failures=0, decision_retry_at=0)
    return True


def sync_completed(data, now):
    """Repair only exact published receipts for waiting/known old wait jobs."""
    return sum(complete_action(data, draft, now)
               for draft in data.get('drafts', {}).values())
