"""Durable, cancellable background generation for competition roles.

The manager starts no network requests itself. It owns one controller task and
its heartbeat, leaving model transport, social delivery and transaction safety
to the controller. Reloading a browser has no effect on the worker.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
import logging
import threading
import time
import uuid

from . import agent_waiting
from .agent_storage_retry import is_transient_storage_error, storage_error_code


_LOG = logging.getLogger(__name__)


ACTIVE = frozenset(('queued', 'running', 'cancelling'))
_TIMINGS = ('dns_seconds', 'connect_seconds', 'tls_seconds', 'send_seconds', 'local_progress_seconds')
_STAGES = frozenset(('queued', 'starting', 'preparing', 'reading', 'dns', 'connect',
                     'tls', 'send', 'headers', 'body', 'thinking', 'receiving',
                     'complete', 'saving', 'publishing'))
# Audit rows record phase changes only. Each model request walks these transport
# stages and a staged job makes three to six requests, so stage-level rows came to
# about 10 per job, 47% of all audit rows on 2026-10-22. The job record still shows
# the live stage, llm_calls keeps per-request timing, and a failed job's audit row
# names the stage it stopped in.
_REQUEST_STAGES = frozenset(('dns', 'connect', 'tls', 'send', 'headers', 'body', 'thinking',
                             'receiving', 'complete'))


def phase(stage):
    """The audited phase of a job stage: every model-request stage is 'model'."""
    return 'model' if stage in _REQUEST_STAGES else stage


def _stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')


def _number(value, default=0.0):
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else default
    except (TypeError, ValueError, OverflowError):
        return default


def _error(message, status=409):
    from .agent_control import AgentError
    return AgentError(message, status)


def _rejected_details(rejected):
    """The action, text and issues of a round that failed its checks, cleaned and cut by agent_audit."""
    if not isinstance(rejected, dict):
        return {}
    from .agent_audit import rejected_fields
    action = rejected.get('action')
    return ({'action': action} if action in ('post', 'reply') else {}) | rejected_fields(
        rejected.get('body'), rejected.get('issues'))


@dataclass
class _Task:
    agent_id: str
    job_id: str
    automatic: bool
    started: float = field(default_factory=time.monotonic)
    cancellation: threading.Event = field(default_factory=threading.Event)
    heartbeat_stop: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    progress_lock: threading.Lock = field(default_factory=threading.Lock)
    lease_token: str | None = None
    thread: threading.Thread | None = None
    last_progress_write: float = 0.0
    last_heartbeat_saved: float = field(default_factory=time.monotonic)
    progress_revision: int = 0
    saved_progress_revision: int = 0
    recovery_grace_used: bool = False
    progress: dict = field(default_factory=dict)
    failure_reason: str = ''


class GenerationManager:
    def __init__(self, controller, *, heartbeat_interval=15.0, progress_interval=3.0):
        self.controller = controller
        self.owner = controller.owner
        self.heartbeat_interval = max(.01, float(heartbeat_interval))
        self.progress_interval = max(.01, float(progress_interval))
        self.heartbeat_ttl = max(45.0, self.heartbeat_interval * 3)
        self._lock = threading.Lock()
        self._local = threading.local()
        self._jobs = {}
        self._stopping = False

    def _agent(self, data, agent_id):
        agent = data.get('agents', {}).get(agent_id)
        if not isinstance(agent, dict):
            raise _error('角色不存在', 404)
        return agent

    def _runtime_change(self, callback):
        store = self.controller.store
        return getattr(store, 'change_runtime', store.change)(callback)

    def _runtime_read(self):
        store = self.controller.store
        return getattr(store, 'read_runtime', store.read)()

    def _save_runtime(self, task, callback, *, renewal=False):
        """Retry only an idempotent ownership/bookkeeping transaction.

        Every renewal callback still checks the durable owner and unexpired
        role token after acquiring the database lock. This never retries work
        performed by a provider or by the platform.
        """
        deadline = time.monotonic() + self.heartbeat_ttl
        def guarded(data, conn):
            if renewal and time.monotonic() >= task.last_heartbeat_saved + self.heartbeat_ttl:
                raise _error('生成任务心跳保存已超出有效期，结果未保存', 409)
            return callback(data, conn)
        while True:
            if renewal:
                # A recovery grace can reset the last confirmed save, but an
                # expired retry must not revive itself after waiting for a lock.
                deadline = task.last_heartbeat_saved + self.heartbeat_ttl
                if time.monotonic() >= deadline:
                    raise _error('生成任务心跳保存已超出有效期，结果未保存', 409)
            try:
                return self._runtime_change(guarded if renewal else callback)
            except Exception as exc:
                remaining = deadline - time.monotonic()
                if not is_transient_storage_error(exc) or remaining <= 0:
                    raise
                _LOG.warning('Generation storage retry: agent=%s job=%s error=%s sqlstate=%s',
                    task.agent_id, task.job_id, type(exc).__name__, storage_error_code(exc) or 'none')
                if renewal:
                    if task.heartbeat_stop.wait(min(.25, remaining)):
                        return
                else:
                    time.sleep(min(.25, remaining))

    def _backoff(self, agent, now):
        # Persist a scheduler gate as well as next_run_at: new input must not
        # immediately bypass a worker failure. The caller has verified the job
        # identity or is recovering the current expired job under the store lock.
        failures = min(6, int(_number(agent.get('generation_failures'))) + 1)
        delay = min(900, 60 * 2 ** (failures - 1))
        agent.update(generation_failures=failures,
                     next_run_at=max(_number(agent.get('next_run_at')), now + delay),
                     decision_retry_at=max(_number(agent.get('decision_retry_at')), now + delay))

    def _release_role_lease(self, data, agent_id, job):
        """Release only a role claim proven to belong to this exact generation.

        A shared owner alone is insufficient: manual operations and replacement
        workers can hold another claim in the same process. Legacy jobs without
        the bidirectional token/job association retain their existing expiry.
        Publisher leases and action receipts are deliberately outside this scope.
        """
        key = 'agent:' + agent_id
        lease = data.get('leases', {}).get(key, {})
        matched = agent_waiting.role_lease_matches(job, lease)
        now = self.controller.clock()
        if matched:
            data['leases'].pop(key, None)
            job.update(role_lease_released=True, role_lease_released_at=_stamp(now))
        remaining = data.get('leases', {}).get(key, {})
        waiting = _number(remaining.get('until')) > now
        job.update(recovery_waiting_for_lease=waiting,
                   recovery_lease_until=_number(remaining.get('until')) if waiting else 0)
        return bool(matched)

    def recover(self, data):
        """Mark abandoned jobs interrupted inside the caller's transaction.

        Recovery revokes the exact generation's role claim. It never restarts a
        request or removes an action's audit, sending state or publisher lease.
        """
        now, recovered = self.controller.clock(), 0
        for agent in data.get('agents', {}).values():
            job = agent.get('generation') if isinstance(agent, dict) else None
            if (isinstance(job, dict) and job.get('status') in ACTIVE
                    and _number(job.get('heartbeat_until')) <= now):
                task = self._jobs.get(agent['id'])
                lease = data.get('leases', {}).get('agent:' + agent['id'], {})
                # After suspension a live owner can be scheduled just after a
                # reader notices expiry. Give that exact, still leased worker
                # one renewal window; a dead or fenced worker gets no grace.
                if (task is not None and task.job_id == job.get('id')
                        and job.get('owner') == self.owner
                        and task.thread is not None and task.thread.is_alive()
                        and not task.cancellation.is_set() and not task.failure_reason
                        and not task.recovery_grace_used
                        and task.lease_token == job.get('lease_token')
                        and agent_waiting.role_lease_matches(job, lease)
                        and _number(lease.get('until')) > now):
                    job.update(heartbeat_until=now + self.heartbeat_ttl,
                               updated_at=_stamp(now))
                    task.last_heartbeat_saved = time.monotonic()
                    task.recovery_grace_used = True
                    self.controller._event(data, 'generation_heartbeat_resumed',
                        '原任务仍在运行，正在恢复心跳', agent['id'],
                        details={'job_id': job['id'], 'stage': job.get('stage', '')})
                    continue
                job.update(status='failed', stage='interrupted', updated_at=_stamp(now),
                           error='生成进程心跳已中断；未自动重发，请检查结果后重新运行',
                           heartbeat_until=0)
                released = self._release_role_lease(data, agent['id'], job)
                # Do not acquire the manager lock while holding the store lock:
                # submit takes those locks in the opposite order. The identity
                # check prevents cancelling a newer local task for this actor.
                task = self._jobs.get(agent['id'])
                if task is not None and task.job_id == job.get('id') and job.get('owner') == self.owner:
                    task.failure_reason = job['error']
                    task.cancellation.set()
                self._backoff(agent, now)
                self.controller._event(data, 'generation_interrupted', job['error'], agent['id'],
                    details={'job_id': job.get('id'), 'stage': 'interrupted',
                             'elapsed_seconds': job.get('elapsed_seconds', 0),
                             'role_lease_released': released,
                             'waiting_for_role_lease': job['recovery_waiting_for_lease']})
                recovered += 1
        return recovered

    def submit(self, agent_id, automatic=False):
        """Reserve a durable job and return without waiting for generation."""
        task = _Task(agent_id, str(uuid.uuid4()), bool(automatic))
        with self._lock:
            if self._stopping:
                raise _error('服务正在关闭，请稍后重新运行', 503)

            def queue(data, conn):
                if data.get('rate', {}).get('maintenance', {}).get('active'):
                    raise _error('服务正在维护，暂不接收新任务', 409)
                self.recover(data)
                agent = self._agent(data, agent_id)
                job = agent.get('generation', {})
                if isinstance(job, dict) and job.get('status') in ACTIVE:
                    raise _error('该角色已有生成任务运行，请等待完成或取消', 409)
                lease = data.get('leases', {}).get('agent:' + agent_id, {})
                if _number(lease.get('until')) > self.controller.clock():
                    raise _error('该角色已有任务运行，请稍后再试', 409)
                now = self.controller.clock()
                agent['generation'] = {'id': task.job_id, 'status': 'queued', 'stage': 'queued',
                    'started_at': _stamp(now), 'updated_at': _stamp(now), 'elapsed_seconds': 0,
                    'error': '', 'heartbeat_until': now + self.heartbeat_ttl, 'owner': self.owner}
                self.controller._event(data, 'generation_queued', '阅读与决策任务已排队', agent_id,
                    details={'job_id': task.job_id, 'stage': 'queued', 'automatic': task.automatic})

            self.controller.store.change(queue)
            task.last_heartbeat_saved = time.monotonic()
            self._jobs[agent_id] = task
            task.thread = threading.Thread(target=self._worker, args=(task,),
                name='ctn-agent-generation-' + agent_id[:12], daemon=True)
            try:
                task.thread.start()
            except Exception:
                self._jobs.pop(agent_id, None)
                self._finish(task, 'failed', '无法启动后台生成任务，请稍后重试')
                raise _error('无法启动后台生成任务，请稍后重试', 503) from None
        return self.controller.snapshot()

    def _current(self, agent_id, token=None):
        task = getattr(self._local, 'task', None)
        if task is None or task.agent_id != agent_id:
            return None
        if token is not None and task.lease_token != token:
            return None
        return task

    def attach_lease(self, agent_id, token):
        """Attach only the role claim held by this manager's current worker."""
        task = self._current(agent_id)
        if task is None:
            return
        self.check_cancelled(agent_id)
        with task.lock:
            task.lease_token = token
        self._touch(task, {'stage': 'reading'})
        self.check_cancelled(agent_id)

    def detach_lease(self, agent_id, token):
        """Stop renewing this role claim before the controller releases it.

        Call from _run's finally block inside _claim, outside a database
        transaction. Synchronizing with an in-flight heartbeat ensures it can
        never mistake an intentional release for lost exclusive ownership.
        """
        task = self._current(agent_id)
        if task is not None:
            with task.lock:
                if task.lease_token == token:
                    task.lease_token = None

    def request_options(self, agent_id, token):
        """Transport hooks; ordinary synchronous calls intentionally get none."""
        task = self._current(agent_id, token)
        if task is None:
            return {}
        self.check_cancelled(agent_id)
        return {'cancellation_event': task.cancellation,
                'on_progress': lambda progress: self._on_progress(task, progress),
                'trace': {'agent_id': agent_id, 'job_id': task.job_id}}

    def draft_metadata(self, agent_id):
        """Server-owned association; model output never supplies this identity."""
        task = self._current(agent_id)
        return {'generation_job_id': task.job_id} if task is not None else {}

    @staticmethod
    def _retain_cancelled_drafts(data, agent_id, job_id):
        for draft in data.get('drafts', {}).values():
            if (draft.get('agent_id') == agent_id and draft.get('generation_job_id') == job_id
                    and draft.get('origin') != 'manual' and not draft.get('manual_only')
                    and not draft.get('manual_prepared')
                    and draft.get('state') in ('pending', 'rejected', 'skipped')):
                draft.update(requires_review=True, review_reason='生成任务已取消，已保存的结果需要成员确认后执行')

    @staticmethod
    def _operation_started(data, agent_id, job):
        return bool(job.get('operation_started') or any(
            draft.get('agent_id') == agent_id and draft.get('generation_job_id') == job.get('id')
            and draft.get('state') in ('sending', 'uncertain', 'published')
            for draft in data.get('drafts', {}).values()))

    def check_publish(self, agent_id, data, draft, *, automatic=False):
        """Guard worker, scheduler, and explicit execution entry points alike."""
        self.check_cancelled(agent_id, data)
        if not automatic:
            return
        job = self._agent(data, agent_id).get('generation', {})
        cancelled_job = (draft.get('generation_job_id') and isinstance(job, dict)
                         and job.get('id') == draft['generation_job_id']
                         and job.get('status') in ('cancelling', 'cancelled'))
        if draft.get('requires_review') or (cancelled_job and draft.get('review_approved') is not True):
            raise _error('生成任务已取消，已保存的动作须由成员确认后执行', 409)

    def check_cancelled(self, agent_id, data=None):
        """Read-only cancellation guard, safe inside a result-save transaction."""
        task = self._current(agent_id)
        if task is None:
            return
        state = data if data is not None else self._runtime_read()
        job = self._agent(state, agent_id).get('generation', {})
        if (not isinstance(job, dict) or job.get('id') != task.job_id
                or job.get('owner') != self.owner or job.get('status') not in ACTIVE):
            task.failure_reason = '生成任务的所有权已改变，结果未保存'
            task.cancellation.set()
        elif job.get('status') == 'cancelling':
            task.cancellation.set()
        if task.cancellation.is_set():
            if task.failure_reason:
                raise _error(task.failure_reason, 409)
            raise _error('生成任务已取消，未保存未完成的模型结果', 499)

    def before_publish(self, agent_id, data, draft=None, *, automatic=False):
        """End cancellation atomically before an action or notification write."""
        if draft is not None:
            self.check_publish(agent_id, data, draft, automatic=automatic)
        task = self._current(agent_id)
        job = self._agent(data, agent_id).get('generation', {})
        associated = (draft is not None and draft.get('generation_job_id')
                      and isinstance(job, dict) and job.get('id') == draft['generation_job_id']
                      and job.get('status') in ACTIVE)
        if task is None and not associated:
            return
        self.check_cancelled(agent_id, data)
        if job.get('operation_started'):
            return
        job.update(operation_started=True, stage='publishing', updated_at=_stamp(self.controller.clock()))
        self.controller._event(data, 'generation_phase', '模型结果已保存，开始执行操作', agent_id,
            details={'job_id': job['id'], 'stage': 'publishing', 'operation_started': True})

    def cancel(self, agent_id):
        """Persist cancellation, including jobs owned by another process."""
        cancelled_id = None

        def cancel(data, conn):
            nonlocal cancelled_id
            self.recover(data)
            job = self._agent(data, agent_id).get('generation', {})
            if not isinstance(job, dict) or job.get('status') not in ACTIVE | {'waiting'}:
                raise _error('该角色当前没有正在生成的任务', 409)
            waiting = job.get('status') == 'waiting'
            # A deferred attempt may have entered sending and then safely
            # rolled back to pending before any request. Its old phase flag is
            # not evidence of an in-flight write; actual action states are.
            operation_started = (any(
                draft.get('agent_id') == agent_id
                and draft.get('generation_job_id') == job.get('id')
                and draft.get('state') in ('sending', 'uncertain', 'published')
                for draft in data.get('drafts', {}).values())
                if waiting else self._operation_started(data, agent_id, job))
            if operation_started:
                raise _error('生成已完成，操作已进入发送流程；请在操作记录中核对结果', 409)
            cancelled_id = job['id']
            if job.get('status') != 'cancelling':
                self.controller._event(data, 'generation_cancel_requested', '已请求取消生成任务', agent_id,
                    details={'job_id': job['id'], 'stage': 'cancelling'})
            job.update(status='cancelled' if waiting else 'cancelling',
                       stage='cancelled' if waiting else 'cancelling',
                       updated_at=_stamp(self.controller.clock()),
                       error='生成任务已取消，已保存的动作需要成员确认' if waiting else '')
            if waiting:
                job['heartbeat_until'] = 0
                for key in ('waiting_for', 'wait_kind', 'wait_message', 'retry_at'):
                    job.pop(key, None)
            self._retain_cancelled_drafts(data, agent_id, cancelled_id)

        self.controller.store.change(cancel)
        with self._lock:
            task = self._jobs.get(agent_id)
            if task is not None and task.job_id == cancelled_id:
                task.cancellation.set()
        return self.controller.snapshot()

    def _touch(self, task, progress=None):
        """Renew this exact job and, once attached, this exact role lease."""
        def touch(data, conn):
            job = self._agent(data, task.agent_id).get('generation', {})
            if (not isinstance(job, dict) or job.get('id') != task.job_id
                    or job.get('owner') != self.owner or job.get('status') not in ACTIVE):
                task.failure_reason = '生成任务的所有权已改变，结果未保存'
                task.cancellation.set()
                return
            if job.get('status') == 'cancelling':
                task.cancellation.set()
            now = self.controller.clock()
            previous_stage = job.get('stage')
            if task.lease_token is not None:
                lease = data.get('leases', {}).get('agent:' + task.agent_id, {})
                if (lease.get('token') != task.lease_token or lease.get('owner') != self.owner
                        or _number(lease.get('until')) <= now
                        or lease.get('generation_job_id') not in (None, task.job_id)
                        or job.get('lease_token') not in (None, task.lease_token)):
                    task.failure_reason = '角色任务租约已失去，生成结果未保存'
                    task.cancellation.set()
                else:
                    from .agent_control import LEASE_SECONDS
                    # The first attach verifies the current worker's in-memory
                    # claim before adding durable recovery evidence. Subsequent
                    # heartbeats must preserve, rather than replace, that pair.
                    lease['generation_job_id'] = task.job_id
                    job['lease_token'] = task.lease_token
                    lease['until'] = now + LEASE_SECONDS
            job.update(updated_at=_stamp(now), heartbeat_until=now + self.heartbeat_ttl,
                       elapsed_seconds=round(max(0, time.monotonic() - task.started), 3))
            if job.get('operation_started'):
                job['stage'] = 'publishing'
            elif job.get('status') == 'cancelling':
                job['stage'] = 'cancelling'
            elif isinstance(progress, dict):
                stage = progress.get('stage')
                if stage in _STAGES:
                    job['stage'] = stage
                for key in ('stage_elapsed_seconds', 'received_bytes', *_TIMINGS):
                    value = progress.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                        job[key] = value
            if phase(job.get('stage')) != phase(previous_stage):
                self.controller._event(data, 'generation_phase', '生成阶段：' + str(job['stage']), task.agent_id,
                    details={key: job.get(key) for key in ('stage', 'elapsed_seconds', 'received_bytes', *_TIMINGS)}
                        | {'job_id': task.job_id, 'phase': phase(job['stage'])})

        # detach_lease waits for any renewal already in progress. After detach
        # returns, no heartbeat can retain an old token past role-claim release.
        with task.lock:
            self._save_runtime(task, touch, renewal=True)
            task.last_heartbeat_saved = time.monotonic()
            task.recovery_grace_used = False

    def _on_progress(self, task, progress):
        if not isinstance(progress, dict):
            return
        # Transport callbacks must never wait on the database or lease lock.
        # Only the heartbeat worker flushes the latest allowlisted measurements.
        with task.progress_lock:
            task.progress = {key: progress[key] for key in ('stage', 'stage_elapsed_seconds', 'received_bytes', *_TIMINGS)
                             if key in progress}
            task.progress_revision += 1

    def _heartbeat(self, task):
        interval = min(self.heartbeat_interval, self.progress_interval)
        while not task.heartbeat_stop.wait(interval):
            now = time.monotonic()
            with task.progress_lock:
                progress = dict(task.progress)
                revision = task.progress_revision
            heartbeat_due = now - task.last_heartbeat_saved >= self.heartbeat_interval
            progress_due = (revision != task.saved_progress_revision
                            and now - task.last_progress_write >= self.progress_interval)
            if not heartbeat_due and not progress_due:
                continue
            try:
                self._touch(task, progress)
                task.saved_progress_revision = revision
                task.last_progress_write = time.monotonic()
                if task.cancellation.is_set():
                    return
            except Exception as exc:
                _LOG.error('Generation heartbeat storage failed: agent=%s job=%s error=%s sqlstate=%s',
                    task.agent_id, task.job_id, type(exc).__name__, storage_error_code(exc) or 'none')
                task.failure_reason = '无法保存生成任务心跳，结果未保存'
                task.cancellation.set()
                return

    def _finish(self, task, status, error='', rejected=None):
        """``rejected``: a failed check round's {action, body, issues} (AgentError.rejected)."""
        with task.progress_lock:
            final_progress = dict(task.progress)
        def finish(data, conn):
            agent = self._agent(data, task.agent_id)
            job = agent.get('generation', {})
            # A stale worker cannot overwrite a recovered or replacement job.
            if (not isinstance(job, dict) or job.get('id') != task.job_id
                    or job.get('owner') != self.owner):
                return
            if job.get('status') not in ACTIVE:
                if job.get('status') in ('failed', 'cancelled'):
                    self._release_role_lease(data, task.agent_id, job)
                return
            last_stage = job.get('stage')
            job.update(status=status, stage='complete' if status == 'completed' else status,
                updated_at=_stamp(self.controller.clock()),
                elapsed_seconds=round(max(0, time.monotonic() - task.started), 3),
                heartbeat_until=0, error=error[:2000])
            for key in _TIMINGS:
                value = final_progress.get(key)
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    job[key] = value
            released = self._release_role_lease(data, task.agent_id, job)
            if status == 'failed':
                self._backoff(agent, self.controller.clock())
            elif status == 'completed':
                agent['generation_failures'] = 0
            message = {'completed': '阅读与决策任务已完成', 'cancelled': '生成任务已取消',
                       'failed': '生成任务失败'}.get(status, status)
            self.controller._event(data, 'generation_' + status, message + ('：' + error[:2000] if error else ''),
                task.agent_id, details={key: job.get(key) for key in _TIMINGS} | {'job_id': task.job_id, 'stage': job['stage'],
                                       'elapsed_seconds': job['elapsed_seconds'], 'automatic': task.automatic,
                                       'role_lease_released': released}
                                       # Where an unfinished job stopped (phase rows no longer list every stage).
                                       | ({'phase': last_stage} if status != 'completed' and isinstance(last_stage, str) else {})
                                       | (_rejected_details(rejected) if status == 'failed' else {}))
        self._save_runtime(task, finish)

    def _defer(self, task, description):
        """Persist a scheduler wait after _run has released its role claim."""
        def defer(data, conn):
            agent = self._agent(data, task.agent_id)
            job = agent.get('generation', {})
            if (not isinstance(job, dict) or job.get('id') != task.job_id
                    or job.get('owner') != self.owner or job.get('status') not in ACTIVE):
                return False
            if job.get('status') == 'cancelling':
                task.cancellation.set()
                return False
            drafts = [draft for draft in data.get('drafts', {}).values()
                      if isinstance(draft, dict) and draft.get('agent_id') == task.agent_id
                      and draft.get('generation_job_id') == task.job_id]
            if any(draft.get('state') in ('sending', 'uncertain') for draft in drafts):
                return False
            pending = next((draft for draft in drafts
                            if draft.get('state') == 'pending'
                            and draft.get('origin') != 'manual'
                            and not draft.get('manual_only') and not draft.get('manual_prepared')
                            and not draft.get('requires_review')), None)
            published = next((draft for draft in drafts if draft.get('state') == 'published'), None)
            # Do not disguise discarded, cancelled or review-only actions as a
            # pending automatic delivery. No associated action means the model
            # or reading request itself is waiting.
            if drafts and pending is None and published is None:
                return False
            now = self.controller.clock()
            retry_at = now + description['retry_after']
            retry_at = max(retry_at, _number(
                pending.get('next_attempt_at') if pending else agent.get('decision_retry_at')))
            job.update(status='waiting', stage='waiting', updated_at=_stamp(now),
                       elapsed_seconds=round(max(0, time.monotonic() - task.started), 3),
                       heartbeat_until=0, error='', operation_started=False,
                       waiting_for='publish' if pending or published else 'decision',
                       wait_kind=description['wait_kind'],
                       wait_message=description['wait_message'], retry_at=retry_at)
            if pending and pending.get('id'):
                job['draft_id'] = pending['id']
            else:
                job.pop('draft_id', None)
            if pending:
                pending['next_attempt_at'] = retry_at
            elif not published:
                agent['decision_retry_at'] = retry_at
            agent['last_error'] = ''
            released = self._release_role_lease(data, task.agent_id, job)
            # A tick can publish the saved action after _run releases its claim
            # but before this worker persists the deferred state.
            completed = published is not None and agent_waiting.complete_action(data, published, now)
            self.controller._event(data, 'generation_completed' if completed else 'generation_waiting',
                '阅读与决策任务已完成' if completed else description['wait_message'],
                task.agent_id, details={'job_id': task.job_id, 'stage': job['stage'],
                    'waiting_for': job.get('waiting_for'), 'wait_kind': job.get('wait_kind'),
                    'retry_at': job.get('retry_at'), 'draft_id': job.get('draft_id'),
                    'automatic': task.automatic, 'role_lease_released': released})
            return True
        # Serialize with an already-started heartbeat so it cannot renew the
        # waiting job or a role lease after this transaction completes.
        with task.lock:
            return self.controller.store.change(defer)

    def _worker(self, task):
        self._local.task = task
        heartbeat = threading.Thread(target=self._heartbeat, args=(task,), daemon=True,
                                     name='ctn-agent-generation-heartbeat')
        try:
            def start(data, conn):
                job = self._agent(data, task.agent_id).get('generation', {})
                if job.get('id') != task.job_id or job.get('owner') != self.owner:
                    raise _error('生成任务的所有权已改变', 409)
                if job.get('status') == 'queued':
                    now = self.controller.clock()
                    job.update(status='running', stage='starting', updated_at=_stamp(now),
                               heartbeat_until=now + self.heartbeat_ttl)
                    self.controller._event(data, 'generation_started', '开始阅读与决策', task.agent_id,
                        details={'job_id': task.job_id, 'stage': 'starting', 'automatic': task.automatic})
            self._save_runtime(task, start)
            task.last_heartbeat_saved = time.monotonic()
            heartbeat.start()
            self.check_cancelled(task.agent_id)
            self.controller._run(task.agent_id, automatic=task.automatic)
            self.check_cancelled(task.agent_id)
            self._finish(task, 'completed')
        except Exception as exc:
            from .agent_control import AgentError
            from .ai_providers import ProviderError
            status_code = getattr(exc, 'status_code', None) or getattr(exc, 'status', None)
            cancelled = (status_code == 499 or task.cancellation.is_set()) and not task.failure_reason
            safe = str(exc) if isinstance(exc, (AgentError, ProviderError)) else '生成任务未完成；敏感错误已隐藏'
            if is_transient_storage_error(exc):
                safe = '数据库暂时繁忙，本轮任务未完成；未重发模型请求，请稍后重试'
            if not isinstance(exc, (AgentError, ProviderError)):
                _LOG.error('Generation worker failed: agent=%s job=%s error=%s sqlstate=%s',
                    task.agent_id, task.job_id, type(exc).__name__, storage_error_code(exc) or 'none')
            if task.failure_reason:
                safe = task.failure_reason
            if cancelled:
                safe = '生成任务已取消，未保存未完成的模型结果'
            try:
                description = agent_waiting.classify(exc) if not cancelled and not task.failure_reason else None
                if description:
                    task.heartbeat_stop.set()
                    if self._defer(task, description):
                        return
                    cancelled = task.cancellation.is_set() and not task.failure_reason
                    if cancelled:
                        safe = '生成任务已取消，未保存未完成的模型结果'
                self._finish(task, 'cancelled' if cancelled else 'failed', safe,
                             None if cancelled or task.failure_reason else getattr(exc, 'rejected', None))
            except Exception:
                # Persisted heartbeat expiry is the recovery path if storage is
                # unavailable. Never retry the model or publish from this path.
                pass
        finally:
            task.heartbeat_stop.set()
            if heartbeat.is_alive():
                heartbeat.join(timeout=1)
            self._local.task = None
            with self._lock:
                if self._jobs.get(task.agent_id) is task:
                    self._jobs.pop(task.agent_id, None)

    def stop(self):
        """Cancel local workers on shutdown without changing automation settings."""
        with self._lock:
            self._stopping = True
            tasks = list(self._jobs.values())
        # Local transport cancellation must not depend on a database write.
        # Cancel every worker first, so one unavailable/locked transaction
        # cannot leave the remaining model requests running during shutdown.
        # Sending receipts remain protected by the persistent checks below.
        for task in tasks:
            task.cancellation.set()
        for task in tasks:
            try:
                def cancelling(data, conn):
                    job = self._agent(data, task.agent_id).get('generation', {})
                    if (isinstance(job, dict) and job.get('id') == task.job_id
                            and job.get('owner') == self.owner and job.get('status') in ACTIVE):
                        if self._operation_started(data, task.agent_id, job):
                            return False
                        job.update(status='cancelling', stage='cancelling',
                                   updated_at=_stamp(self.controller.clock()))
                        self._retain_cancelled_drafts(data, task.agent_id, task.job_id)
                        return True
                    return False
                self.controller.store.change(cancelling)
            except Exception as exc:
                # The exact job remains eligible for normal expiry recovery;
                # never expose storage text or replay an interrupted request.
                _LOG.warning('Generation shutdown state save failed: agent=%s job=%s error=%s sqlstate=%s',
                    task.agent_id, task.job_id, type(exc).__name__, storage_error_code(exc) or 'none')
        deadline = time.monotonic() + 1
        for task in tasks:
            if task.thread is not None and task.thread is not threading.current_thread():
                task.thread.join(timeout=max(0, deadline - time.monotonic()))
