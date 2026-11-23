"""Opt-in research with durable progress, cancellation and exclusive ownership."""
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import logging
import threading
import time
import uuid

from . import content_research
from .collector import CollectionBusy, collection_lock
from .db import begin_write, connect, is_postgres, utc_now


KEY = 'content_research_automation_v1'
LEASE_SECONDS = 180
HEARTBEAT_SECONDS = 2
PROGRESS_SECONDS = 5
OWNER_LOCK = 743828952224359011
# mode: 'radar' = concern radar (default); 'popular' = the earlier popular-post research.
DEFAULTS = {'revision': 0, 'enabled': False, 'provider_id': '', 'interval_seconds': 7200,
            'auto_activate': False, 'params': {}, 'instruction': '', 'sample_size': 100,
            'include_news': True, 'max_output_tokens': None, 'mode': 'radar'}
MODES = ('radar', 'popular')
STATUS_FIELDS = ('last_run_at', 'last_finished_at', 'last_success_at', 'next_run_at',
                 'last_error', 'last_result')


def _stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace('+00:00', 'Z')


def _load(conn):
    row = conn.execute('SELECT value FROM checkpoints WHERE key=?', (KEY,)).fetchone()
    if row is None:
        return {'settings': deepcopy(DEFAULTS), 'status': {key: None for key in STATUS_FIELDS}}
    try:
        state = json.loads(row['value'])
        settings, status = state['settings'], state['status']
        # Compatibility only: old saved budgets never limit a new request.
        if isinstance(settings, dict):
            settings['max_output_tokens'] = None
            # Settings saved before the concern radar existed run the radar.
            settings.setdefault('mode', 'radar')
        if (not isinstance(settings, dict) or not isinstance(status, dict)
                or set(settings) != set(DEFAULTS) or settings['mode'] not in MODES
                or type(settings['revision']) is not int or settings['revision'] < 0
                or type(settings['enabled']) is not bool or type(settings['auto_activate']) is not bool
                or type(settings['interval_seconds']) is not int
                or not 900 <= settings['interval_seconds'] <= 86400
                or not isinstance(settings['provider_id'], str)
                or not isinstance(settings['params'], dict)
                or not isinstance(settings['instruction'], str)
                or type(settings['sample_size']) is not int or not 20 <= settings['sample_size'] <= 200
                or type(settings['include_news']) is not bool):
            raise ValueError
        return state
    except (ValueError, TypeError, KeyError):
        raise content_research.ResearchError('自动内容分析配置无法读取，请检查备份', 503) from None


def _save(conn, state):
    conn.execute('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?) '
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                 (KEY, json.dumps(state, ensure_ascii=False, allow_nan=False), utc_now()))


@contextmanager
def owner_lock(db_path):
    """Exclude research owners and maintenance across processes and hosts."""
    with collection_lock(db_path, 'content-research'):
        if not is_postgres(db_path):
            yield None
            return
        with closing(connect(db_path)) as conn:
            acquired = conn.execute('SELECT pg_try_advisory_lock(?)', (OWNER_LOCK,)).fetchone()[0]
            conn.commit()
            if not acquired:
                raise CollectionBusy('content research is already running')
            # Closing this dedicated session releases the lock even if an
            # outage prevents an explicit unlock.
            yield conn


def _fingerprint(settings, provider, evidence):
    # Collection timestamps change even when the evidence itself does not.
    # Preserve publication dates, article text and measured engagement.
    volatile = {'captured_at', 'generated_at', 'observed_at', 'trending_observed_at', 'last_seen',
                'observation_start', 'observation_end'}
    def stable(value):
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items() if key not in volatile}
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value
    payload = {'settings': {key: settings[key] for key in
                ('provider_id', 'params', 'instruction', 'sample_size', 'include_news',
                 'auto_activate', 'mode')},
               'provider': {key: provider.get(key) for key in
                ('id', 'base_url', 'model', 'thinking_mode')},
               'evidence': stable(evidence)}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode('utf-8')).hexdigest()


class ResearchAutomation:
    def __init__(self, db_path, *, provider_for, prepare, run, clock=time.time):
        self.db_path = db_path
        self.provider_for, self.prepare, self.run, self.clock = provider_for, prepare, run, clock
        self._stop = threading.Event()
        self._progress = None
        self._progress_lock = threading.Lock()
        self._run_id = None
        self._cancel_event = None
        self._ownership_lost = False
        self._last_progress_write = 0
        self._wake = threading.Event()
        self._thread = None
        self._thread_lock = threading.Lock()

    @property
    def cancellation_event(self):
        with self._progress_lock:
            return self._cancel_event if self._cancel_event is not None else self._stop

    @property
    def run_id(self):
        with self._progress_lock:
            return self._run_id

    def record_progress(self, progress):
        """Keep counters only, flushing a bounded number of durable updates."""
        if not isinstance(progress, dict):
            return
        with self._progress_lock:
            if self._progress is None:
                return
            current = dict(self._progress)
            changed = progress.get('stage') and progress['stage'] != current.get('stage')
            if progress.get('stage') in {'preparing', 'request', 'dns', 'connect', 'tls', 'send',
                                         'headers', 'body', 'thinking', 'receiving', 'complete'}:
                current['stage'] = progress['stage']
            for key in ('elapsed_seconds', 'received_bytes', 'completion_tokens'):
                if type(progress.get(key)) in (int, float):
                    current[key] = progress[key]
            current['updated_at'] = _stamp(self.clock())
            # Elapsed time starts at claim, not each transport phase.
            current['elapsed_seconds'] = max(0, self.clock() - content_research._now(
                current['started_at']).timestamp())
            self._progress = current
            run_id = self._run_id
            flush = changed or self.clock() - self._last_progress_write >= PROGRESS_SECONDS
        if flush and run_id:
            self._heartbeat(run_id)

    def snapshot(self):
        with closing(connect(self.db_path)) as conn:
            state = _load(conn)
        status = state['status']
        running = bool(status.get('run_id') and status.get('lease_until', 0) > self.clock())
        stale = bool(status.get('run_id') and not running)
        last_result = status.get('last_result') or {}
        interrupted = not status.get('run_id') and last_result.get('outcome') == 'interrupted'
        progress = deepcopy(status.get('progress')) if status.get('run_id') else (
            deepcopy(last_result.get('progress')) if interrupted else None)
        if running and progress and progress.get('started_at'):
            progress['elapsed_seconds'] = max(
                0, self.clock() - content_research._now(progress['started_at']).timestamp())
        return {'settings': state['settings'], 'status': {
            **{key: status.get(key) for key in STATUS_FIELDS},
            'running': running, 'run_id': status.get('run_id'), 'progress': progress,
            'stale': stale, 'interrupted': bool(interrupted),
            'interrupted_run_id': last_result.get('run_id') if interrupted else None,
            'maintenance_id': status.get('maintenance_id'),
            'cancel_requested': bool(status.get('cancel_requested')),
            'lease_healthy': running}}

    def _recover_interrupted_state(self, state):
        """The caller must hold owner_lock and a content_research transaction."""
        status = state['status']
        run_id = status.get('run_id')
        if not run_id or status.get('lease_until', 0) > self.clock():
            return False
        input_hash = status.get('started_input_hash')
        if input_hash:
            status['blocked_input_hash'] = input_hash
        status['last_result'] = {
            'outcome': 'interrupted', 'run_id': run_id, 'reason': 'owner_lost',
            'progress': deepcopy(status.get('progress'))}
        status['last_finished_at'] = _stamp(self.clock())
        status['last_error'] = '本轮分析已中断；相同资料不会自动重发，可手动重新分析'
        for key in ('run_id', 'run_revision', 'lease_until', 'started_input_hash',
                    'cancel_requested', 'progress', 'retry_of_run_id'):
            status.pop(key, None)
        return True

    def _recover_interrupted(self):
        """Recover only while holding owner_lock, never on a read request."""
        with closing(connect(self.db_path)) as conn, conn:
            begin_write(conn, 'content_research')
            state = _load(conn)
            if not self._recover_interrupted_state(state):
                return False
            _save(conn, state)
            return True

    def retry_interrupted(self, run_id):
        """Authorize one retry of the exact interrupted round, without running AI here."""
        try:
            with self._owner_lock():
                with closing(connect(self.db_path)) as conn, conn:
                    begin_write(conn, 'content_research')
                    state = _load(conn)
                    status = state['status']
                    result = status.get('last_result') or {}
                    active_id = status.get('run_id')
                    if (not run_id or (active_id and (active_id != run_id
                            or status.get('lease_until', 0) > self.clock()))
                            or (not active_id and (result.get('outcome') != 'interrupted'
                            or result.get('run_id') != run_id))):
                        raise content_research.ResearchError('本轮状态已改变，请刷新后重试', 409)
                    if not state['settings']['enabled']:
                        raise content_research.ResearchError('请先启用自动分析，再重新分析本轮', 409)
                    if status.get('maintenance_id'):
                        raise content_research.ResearchError('服务维护中，请完成维护后重新分析', 409)
                    self._recover_interrupted_state(state)
                    status['retry_run_id'] = run_id
                    status['last_result'] = {'outcome': 'retry_pending', 'run_id': run_id}
                    status['last_error'] = None
                    status['next_run_at'] = _stamp(self.clock())
                    _save(conn, state)
        except CollectionBusy:
            raise content_research.ResearchError('上一轮分析尚未退出，请稍后刷新状态', 409) from None
        self._wake.set()
        return self.snapshot()

    def cancel_current(self, run_id):
        """Cancel this exact round; keep automatic settings and its next interval."""
        with closing(connect(self.db_path)) as conn, conn:
            begin_write(conn, 'content_research')
            state = _load(conn)
            status = state['status']
            if not run_id or status.get('run_id') != run_id:
                raise content_research.ResearchError('本轮已结束或已切换，请刷新运行状态', 409)
            status['cancel_requested'] = True
            if status.get('started_input_hash'):
                status['blocked_input_hash'] = status['started_input_hash']
            _save(conn, state)
        with self._progress_lock:
            if self._run_id == run_id and self._cancel_event is not None:
                self._cancel_event.set()
        self._wake.set()
        return self.snapshot()

    def configure(self, payload):
        payload = dict(payload)
        expected = payload.pop('expected_revision')
        payload['max_output_tokens'] = None
        with closing(connect(self.db_path)) as conn:
            settings = _load(conn)['settings']
        if settings['revision'] != expected:
            raise content_research.ResearchError('自动分析设置已更新，请刷新后重试', 409)
        merged = {**settings, **payload}
        if merged['mode'] not in MODES:
            raise content_research.ResearchError('自动分析模式无效')
        if merged['enabled'] and not merged['provider_id']:
            raise content_research.ResearchError('开启自动分析前请选择文字 AI 服务')
        if merged['enabled']:
            provider = self.provider_for(merged['provider_id'])
            if provider.get('protocol', 'openai_chat') != 'openai_chat':
                raise content_research.ResearchError('自动内容分析需要文字 AI 服务')
        with closing(connect(self.db_path)) as conn, conn:
            begin_write(conn, 'content_research')
            state = _load(conn)
            if state['settings']['revision'] != expected:
                raise content_research.ResearchError('自动分析设置已更新，请刷新后重试', 409)
            merged['revision'] = expected + 1
            state['settings'] = merged
            # Keep the running lease until its owner stops. Changing settings
            # must not let another process launch a second concurrent request.
            state['status']['next_run_at'] = _stamp(self.clock()) if merged['enabled'] else None
            state['status']['last_error'] = None
            _save(conn, state)
        self._wake.set()
        return self.snapshot()

    def _claim(self):
        now = self.clock()
        with closing(connect(self.db_path)) as conn, conn:
            begin_write(conn, 'content_research')
            state = _load(conn)
            settings, status = state['settings'], state['status']
            if not settings['enabled'] or self._stop.is_set() or status.get('maintenance_id'):
                return None
            # Expired rounds are recovered separately, under the owner lock,
            # before scheduling is considered.
            if status.get('run_id'):
                return None
            due = status.get('next_run_at')
            if due and content_research._now(due).timestamp() > now:
                return None
            run_id = uuid.uuid4().hex
            research = content_research._load(conn)
            baseline = research['revision']
            previous_report = next((brief for brief in research['briefs']
                if brief['brief_id'] == status.get('last_generated_brief_id')), None)
            usable = (previous_report is not None
                and previous_report['citation_status'] == 'valid'
                and content_research._now(previous_report['expires_at']).timestamp() > now
                and content_research._sources_available(conn, previous_report))
            previous = {status.get('input_hash') if usable else None,
                        status.get('blocked_input_hash')}
            previous.discard(None)
            retry_of_run_id = status.pop('retry_run_id', None)
            if retry_of_run_id:
                # An explicit retry also works when the interrupted owner
                # never reached the point of hashing/sending its request.
                previous.clear()
            status.update(run_id=run_id, run_revision=settings['revision'],
                          lease_until=now + LEASE_SECONDS, last_run_at=_stamp(now),
                          next_run_at=_stamp(now + settings['interval_seconds']), last_error=None,
                          cancel_requested=False, started_input_hash=None,
                          retry_of_run_id=retry_of_run_id,
                          progress={'stage': 'preparing', 'started_at': _stamp(now),
                                    'elapsed_seconds': 0, 'received_bytes': 0})
            _save(conn, state)
            return deepcopy(settings), run_id, baseline, previous

    def _current(self, run_id, revision):
        with closing(connect(self.db_path)) as conn:
            state = _load(conn)
        return (not self._stop.is_set() and state['settings']['enabled']
                and state['settings']['revision'] == revision
                and state['status'].get('run_id') == run_id
                and not state['status'].get('cancel_requested')
                and not self.cancellation_event.is_set())

    def _lose_ownership(self, run_id):
        with self._progress_lock:
            if self._run_id != run_id:
                return
            self._ownership_lost = True
            if self._cancel_event is not None:
                self._cancel_event.set()

    def _heartbeat(self, run_id, owner_connection=None):
        try:
            if owner_connection is not None:
                owner_connection.execute('SELECT 1')
                owner_connection.commit()
            with closing(connect(self.db_path)) as conn, conn:
                begin_write(conn, 'content_research')
                state = _load(conn)
                status = state['status']
                if status.get('run_id') != run_id:
                    self._lose_ownership(run_id)
                    return False
                if status.get('cancel_requested'):
                    self.cancellation_event.set()
                with self._progress_lock:
                    progress = deepcopy(self._progress)
                status['lease_until'] = self.clock() + LEASE_SECONDS
                if progress is not None:
                    status['progress'] = progress
                _save(conn, state)
                self._last_progress_write = self.clock()
            return True
        except Exception as exc:
            # Keep the initiator lock until the old transport has exited.
            # Expiry alone must never permit a second in-flight request.
            self._lose_ownership(run_id)
            logging.getLogger(__name__).error(
                'Content research heartbeat failed: exception_type=%s', type(exc).__name__)
            return False

    def _renew(self, run_id, done, owner_connection=None):
        while not done.wait(HEARTBEAT_SECONDS):
            if not self._heartbeat(run_id, owner_connection):
                return

    @contextmanager
    def _owner_lock(self):
        with owner_lock(self.db_path) as conn:
            yield conn

    def _mark_request(self, run_id, input_hash):
        with closing(connect(self.db_path)) as conn, conn:
            begin_write(conn, 'content_research')
            state = _load(conn)
            status = state['status']
            if (status.get('run_id') != run_id or status.get('cancel_requested')
                    or self.cancellation_event.is_set() or not state['settings']['enabled']
                    or status.get('run_revision') != state['settings']['revision']):
                return False
            status['started_input_hash'] = input_hash
            if status.get('retry_of_run_id'):
                # A manual retry permits this attempt only. Keep its actual
                # input blocked if sending fails or the owner disappears again.
                status['blocked_input_hash'] = input_hash
            _save(conn, state)
        return True

    def _finish(self, settings, run_id, baseline, result, input_hash=None, error=None):
        now = self.clock()
        with closing(connect(self.db_path)) as conn, conn:
            begin_write(conn, 'content_research')
            state = _load(conn)
            status = state['status']
            if status.get('run_id') != run_id:
                return
            current = (state['settings']['revision'] == settings['revision']
                       and state['settings']['enabled'] and not self._stop.is_set())
            cancelled = status.get('cancel_requested') or self.cancellation_event.is_set()
            if cancelled:
                result = {'outcome': 'cancelled'}
                error = ('运行状态续约失败，本轮已停止；相同资料不会自动重发'
                         if self._ownership_lost else None)
            if result['outcome'] == 'cancelled' and status.get('started_input_hash'):
                status['blocked_input_hash'] = status['started_input_hash']
            if result['outcome'] == 'generated':
                result['activated'] = False
                if settings['auto_activate'] and current:
                    try:
                        content_research._activate_locked(conn, result['brief_id'], baseline + 1,
                                                          content_research._now(now))
                        result['activated'] = True
                    except content_research.ResearchError as exc:
                        result['activation_note'] = str(exc)
                elif settings['auto_activate']:
                    result['activation_note'] = '运行期间设置已改变或服务停止，报告已保存但未自动启用'
                status['last_success_at'] = _stamp(now)
                if current:
                    status['input_hash'] = input_hash
                    status['last_generated_brief_id'] = result['brief_id']
                    if status.get('blocked_input_hash') == input_hash:
                        status.pop('blocked_input_hash', None)
            previous_result = status.get('last_result') or {}
            if (result['outcome'] == 'unchanged' and previous_result.get('outcome') == 'interrupted'
                    and input_hash == status.get('blocked_input_hash')):
                # A free scheduled check must not hide the interrupted round
                # or remove the user's ability to explicitly retry it.
                result, error = previous_result, status.get('last_error')
            status.update(last_finished_at=_stamp(now), last_error=error, last_result=result)
            status.pop('run_id', None)
            status.pop('run_revision', None)
            status.pop('lease_until', None)
            status.pop('started_input_hash', None)
            status.pop('cancel_requested', None)
            status.pop('progress', None)
            status.pop('retry_of_run_id', None)
            if current:
                status['next_run_at'] = _stamp(now + settings['interval_seconds'])
            _save(conn, state)

    def tick(self):
        try:
            with self._owner_lock() as owner_connection:
                self._tick_owned(owner_connection)
        except CollectionBusy:
            return

    def _tick_owned(self, owner_connection):
        # Detect abandoned work before enabled/maintenance/due checks. Do not
        # combine recovery with a new paid request in the same tick.
        if self._recover_interrupted():
            return
        claim = self._claim()
        if claim is None:
            return
        settings, run_id, baseline, previous = claim
        with self._progress_lock:
            self._run_id = run_id
            self._cancel_event = threading.Event()
            self._ownership_lost = False
            self._progress = {'stage': 'preparing', 'started_at': _stamp(self.clock()),
                              'elapsed_seconds': 0, 'received_bytes': 0}
        done = threading.Event()
        heartbeat = threading.Thread(target=self._renew, args=(run_id, done, owner_connection), daemon=True)
        heartbeat.start()
        result, input_hash, error = {'outcome': 'cancelled'}, None, None
        try:
            provider = self.provider_for(settings['provider_id'])
            if provider.get('protocol', 'openai_chat') != 'openai_chat':
                raise content_research.ResearchError('自动内容分析需要文字 AI 服务')
            evidence = self.prepare(settings)
            input_hash = _fingerprint(settings, provider, evidence)
            if not self._current(run_id, settings['revision']):
                pass
            elif not evidence.get('sources'):
                result = {'outcome': 'empty'}
            elif input_hash in previous:
                result = {'outcome': 'unchanged'}
            elif self._mark_request(run_id, input_hash):
                self.record_progress({'stage': 'request'})
                if self.cancellation_event.is_set():
                    return
                saved = self.run(settings, evidence, provider)
                result = {'outcome': 'generated', 'brief_id': saved['created_brief_id']}
        except Exception as exc:
            from .ai_providers import ProviderError
            error = str(exc) if isinstance(exc, (ProviderError, content_research.ResearchError)) else (
                str(exc.detail) if getattr(exc, 'status_code', None) in (400, 429, 502, 503, 504)
                and isinstance(getattr(exc, 'detail', None), str) else '自动内容分析未完成，请检查 AI 服务或稍后重试')
            result = {'outcome': 'failed'}
        finally:
            try:
                self._finish(settings, run_id, baseline, result, input_hash, error)
            finally:
                done.set()
                heartbeat.join()
                with self._progress_lock:
                    self._progress = None
                    self._run_id = None
                    self._cancel_event = None

    def start(self):
        with self._thread_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            def loop():
                while not self._stop.is_set():
                    try:
                        self.tick()
                    except Exception as exc:
                        logging.getLogger(__name__).error(
                            'Content research scheduler failed: exception_type=%s', type(exc).__name__)
                    self._wake.wait(10)
                    self._wake.clear()
            self._thread = threading.Thread(target=loop, name='ctn-content-research', daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self.cancellation_event.set()
        self._wake.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)
