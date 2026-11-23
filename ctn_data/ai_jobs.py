"""Persistent member-owned AI jobs, cancelled explicitly rather than by elapsed time."""
from __future__ import annotations
import copy
import json
import logging
import os
import threading
import time
import uuid
import weakref
from contextlib import closing
from datetime import datetime, timezone
from .db import begin_write, connect, utc_now
from fastapi import HTTPException

PREFIX = 'interactive_ai_job_v2:'
ACTIVE = ('running', 'cancelling')
logger = logging.getLogger(__name__)
_managers = weakref.WeakValueDictionary()


def _process_birth(pid):
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4))
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            try:
                times = [wintypes.FILETIME() for _ in range(4)]
                if kernel.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
                    return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
                return None
            finally:
                kernel.CloseHandle(handle)
        from pathlib import Path
        return Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return None


class AnalysisJobs:
    def __init__(self, slots, db_path=None):
        self.slots, self.db_path = slots, db_path
        self.lock, self.jobs, self.closed = threading.RLock(), {}, False
        self.instance, self.birth = uuid.uuid4().hex, _process_birth(os.getpid())
        _managers[self.instance] = self

    @staticmethod
    def _stored(job):
        return {k: v for k, v in job.items() if k not in
                {'cancel', 'thread', 'monotonic_started', 'last_persisted'}}

    @staticmethod
    def _public(job, include_result=True):
        hidden = {'owner', 'cancel', 'thread', 'process_id', 'process_birth', 'instance',
                  'monotonic_started', 'last_persisted', 'cancel_requested'}
        result = copy.deepcopy({k: v for k, v in job.items() if k not in hidden})
        if job['state'] in ACTIVE:
            result['elapsed_seconds'] = max(0, (datetime.now(timezone.utc) -
                datetime.fromisoformat(job['created_at'].replace('Z', '+00:00'))).total_seconds())
        if not include_result:
            result.pop('result', None)
        return result

    def _read(self, identifier):
        if not self.db_path:
            return None
        with closing(connect(self.db_path)) as conn:
            row = conn.execute('SELECT value FROM checkpoints WHERE key=?', (PREFIX + identifier,)).fetchone()
        return json.loads(row['value']) if row else None

    def _persist(self, job):
        if not self.db_path:
            return
        with closing(connect(self.db_path)) as conn, conn:
            begin_write(conn, 'interactive_ai_jobs')
            row = conn.execute('SELECT value FROM checkpoints WHERE key=?', (PREFIX + job['id'],)).fetchone()
            previous = json.loads(row['value']) if row else {}
            if previous.get('cancel_requested'):
                job['cancel_requested'] = True
                job['cancel'].set()
                if job['state'] == 'completed':
                    job.update(state='cancelled', result=None, stage='已取消', error='AI 任务已取消')
            conn.execute('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                (PREFIX + job['id'], json.dumps(self._stored(job), ensure_ascii=False, allow_nan=False), utc_now()))
        job['last_persisted'] = time.monotonic()

    def _safe_persist(self, job):
        try:
            self._persist(job)
        except Exception as exc:
            logger.error('AI job status storage failed: exception_type=%s', type(exc).__name__)

    def start(self, owner, execute, *, kind='analysis', label='AI 分析', scope='', request_key=None):
        with self.lock:
            if self.closed:
                raise HTTPException(503, '服务正在停止，请稍后重试')
            if request_key:
                previous = next((j for j in self.list(owner)['items'] if j.get('request_key') == request_key), None)
                if previous:
                    return self.get(owner, previous['id'])
            if not self.slots.acquire(blocking=False):
                raise HTTPException(429, '已有两个 AI 任务正在运行，请稍后再试')
            finished = sorted((j for j in self.jobs.values() if j['state'] not in ACTIVE), key=lambda j: j['created_at'])
            while len(self.jobs) >= 64 and finished:
                self.jobs.pop(finished.pop(0)['id'], None)
            job = {'id': uuid.uuid4().hex, 'owner': owner, 'kind': kind, 'label': label,
                'scope': scope, 'request_key': request_key, 'state': 'running', 'stage': '准备资料',
                'workflow_step': '', 'created_at': utc_now(), 'finished_at': None,
                'elapsed_seconds': 0, 'received_bytes': 0, 'result': None, 'error': None,
                'cancel_requested': False, 'cancel': threading.Event(), 'process_id': os.getpid(),
                'process_birth': self.birth, 'instance': self.instance,
                'monotonic_started': time.monotonic(), 'last_persisted': 0}
            self.jobs[job['id']] = job
            try:
                self._persist(job)
            except Exception:
                self.jobs.pop(job['id'], None)
                self.slots.release()
                raise HTTPException(503, '任务状态暂时无法保存，尚未调用模型') from None

            def progress(value):
                with self.lock:
                    if job['cancel'].is_set():
                        return
                    before = job['stage']
                    if isinstance(value, str):
                        job.update(workflow_step=value, stage=value)
                    elif isinstance(value, dict):
                        from .ai_providers import _safe_diagnostics
                        job.update({k: v for k, v in _safe_diagnostics(value).items() if k != 'elapsed_seconds'})
                    job['elapsed_seconds'] = time.monotonic() - job['monotonic_started']
                    if job['stage'] != before or time.monotonic() - job['last_persisted'] >= 2:
                        self._safe_persist(job)

            def work():
                finished = threading.Event()
                def watch():
                    while not finished.wait(1):
                        if self.closed:
                            job['cancel'].set()
                        if self.db_path:
                            try:
                                saved = self._read(job['id'])
                                if saved and saved.get('cancel_requested'):
                                    job['cancel'].set()
                            except Exception as exc:
                                logger.warning('AI cancellation status read failed: exception_type=%s', type(exc).__name__)
                watcher = threading.Thread(target=watch, name='ctn-ai-job-control', daemon=True)
                try:
                    watcher.start()
                    result = execute(job['cancel'], progress)
                    with self.lock:
                        if job['cancel'].is_set():
                            job.update(state='cancelled', stage='已取消', error='AI 任务已取消')
                        else:
                            job.update(state='completed', result=result, stage='已完成')
                except Exception as exc:
                    with self.lock:
                        if job['cancel'].is_set():
                            job.update(state='cancelled', stage='已取消', error='AI 任务已取消')
                        else:
                            from .ai_providers import ProviderError
                            from .agent_control import AgentError
                            from .content_research import ResearchError
                            domain_errors = (ValueError, ProviderError, AgentError, ResearchError)
                            safe = exc.detail if isinstance(exc, HTTPException) else (
                                str(exc) if isinstance(exc, domain_errors) else 'AI 任务未完成，请稍后重试')
                            job.update(state='failed', stage='执行失败', error=safe,
                                error_status=exc.status_code if isinstance(exc, (HTTPException, ProviderError, AgentError, ResearchError)) else 400 if isinstance(exc, ValueError) else 500)
                            logger.warning('AI job failed: exception_type=%s', type(exc).__name__)
                finally:
                    finished.set()
                    if watcher.ident is not None:
                        watcher.join()
                    with self.lock:
                        job.update(finished_at=utc_now(), elapsed_seconds=time.monotonic() - job['monotonic_started'])
                        self._safe_persist(job)
                    self.slots.release()
            job['thread'] = threading.Thread(target=work, name='ctn-ai-analysis', daemon=True)
            try:
                job['thread'].start()
            except Exception:
                job.update(state='failed', error='无法启动后台任务', finished_at=utc_now())
                self._safe_persist(job)
                self.slots.release()
                raise
            return self._public(job)

    def get(self, owner, identifier):
        with self.lock:
            job = self.jobs.get(identifier) or self._read(identifier)
            if job is None or job['owner'] != owner:
                raise HTTPException(404, '任务不存在或已过期')
            if job['state'] in ACTIVE and identifier not in self.jobs:
                alive = (_process_birth(job.get('process_id', 0)) == job.get('process_birth')
                         and job.get('process_birth') is not None)
                if job.get('process_id') == os.getpid() and job.get('instance') not in _managers:
                    alive = False
                if not alive:
                    job.update(state='failed', stage='服务已重启', finished_at=utc_now(),
                        error='服务重启中断了此任务；未自动重发，请检查后重新执行')
            return self._public(job)

    def list(self, owner, *, kind=None, scope=None):
        with self.lock:
            entries = {}
            if self.db_path:
                with closing(connect(self.db_path)) as conn:
                    rows = conn.execute('SELECT value FROM checkpoints WHERE key LIKE ? '
                        'ORDER BY updated_at DESC LIMIT 128', (PREFIX + '%',)).fetchall()
                for row in rows:
                    saved = json.loads(row['value'])
                    if saved.get('owner') == owner:
                        entries[saved['id']] = saved
            entries.update({k: j for k, j in self.jobs.items() if j['owner'] == owner})
            selected = [self.get(owner, j['id']) for j in entries.values()
                        if (kind is None or j.get('kind') == kind) and (scope is None or j.get('scope', '') == scope)]
            for job in selected:
                job.pop('result', None)
            return {'items': sorted(selected, key=lambda j: j['created_at'], reverse=True)}

    def cancel(self, owner, identifier):
        with self.lock:
            current = self.get(owner, identifier)
            if current['state'] not in ACTIVE:
                return current
            job = self.jobs.get(identifier)
            if job is not None:
                job['cancel'].set()
                job.update(cancel_requested=True, state='cancelling', stage='正在取消')
                self._persist(job)
            elif self.db_path:
                with closing(connect(self.db_path)) as conn, conn:
                    begin_write(conn, 'interactive_ai_jobs')
                    row = conn.execute('SELECT value FROM checkpoints WHERE key=?', (PREFIX + identifier,)).fetchone()
                    saved = json.loads(row['value'])
                    if saved['state'] in ACTIVE:
                        saved.update(cancel_requested=True, state='cancelling', stage='正在取消')
                        conn.execute('UPDATE checkpoints SET value=?,updated_at=? WHERE key=?',
                            (json.dumps(saved, ensure_ascii=False), utc_now(), PREFIX + identifier))
            return self.get(owner, identifier)

    def stop(self):
        with self.lock:
            self.closed = True
            for job in self.jobs.values():
                if job['state'] in ACTIVE:
                    job['cancel'].set()
                    job.update(cancel_requested=True, state='cancelling', stage='服务停止，正在取消')
                    self._safe_persist(job)
