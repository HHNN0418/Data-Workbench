"""Persistent, independently paced feed, counter and auxiliary collection lanes."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
import time
import uuid

from .collector import collect_once, collect_stage, plan_counter_tasks, execute_counter_task, CollectionBusy, COUNTER_QUEUE_LIMIT
from .db import connect, reconnect_if_lost, utc_now
from . import task_queue, feed_tasks
from .coordination import cooldowns

PROXY_ENABLED_PATH = Path(__file__).resolve().parent.parent/'data/proxy-enabled'
# Direct lanes record about two collection_runs a minute.
STALL_MINUTES = 10


def collection_health(status, last_run_at, *, now=None):
    """What /api/health reports about direct collection.

    Proxy feed pages create no collection_runs, so posts can keep arriving
    while counters, news and auxiliary collection are stopped. Report only:
    health.ok stays a liveness signal because the watchdog restarts on it.
    """
    now = time.time() if now is None else now
    # A freshly started scheduler is not stalled just because the last run is old.
    quiet_since = max(filter(None, (last_run_at, status.get('started_at'))), default=None)
    minutes = None
    if quiet_since:
        try:
            minutes = round((now - datetime.fromisoformat(quiet_since.replace('Z', '+00:00')).timestamp()) / 60, 1)
        except ValueError:
            pass
    enabled = status['enabled'] is True
    stalled = enabled and (not status['active'] or (minutes is not None and minutes >= STALL_MINUTES))
    return {'enabled': enabled, 'active': status['active'], 'stalled': stalled,
            'last_run_at': last_run_at, 'quiet_minutes': minutes,
            'lane_errors': status['lane_errors']}


def proxy_feed_available(conn, *, now=None, marker_path=None):
    """Use a live proxy without making a network probe or changing its budget."""
    now=time.time() if now is None else now
    if not Path(marker_path or PROXY_ENABLED_PATH).exists():
        return False
    rows=conn.execute("SELECT value FROM meta WHERE key LIKE 'worker_status:%'").fetchall()
    for row in rows:
        try:
            worker=json.loads(row['value'])
            seen=float(worker.get('last_seen_epoch',0))
            http_status=worker.get('last_http_status')
            feed_status=worker.get('last_feed_http_status',http_status)
            # A live control connection does not prove the VPN data path works.
            # 429 remains healthy: all routes must honor its shared cooldown.
            if http_status == 0 or (isinstance(http_status,int) and http_status>=500):
                continue
            if feed_status is not None and feed_status not in (200, 429):
                continue
            if (worker.get('supports_feed') is True and 0<=now-seen<=60
                    and worker.get('status') in ('online','sampling','waiting','idle')):
                return True
        except (TypeError,ValueError):
            continue
    return False


class CollectionScheduler:
    PLAN_SECONDS = 10

    def __init__(self, db_path, config, enabled=False, runner=None):
        self.db_path, self.config = db_path, dict(config)
        self.runner = runner or collect_once
        self.custom_runner = runner is not None
        self.worker_id = 'direct-' + uuid.uuid4().hex[:12]
        self.workers = []
        self.active_tasks = {}
        self.lane_errors = {}
        self.lane_results = {}
        self.queue_status = []
        self.feed_route = 'direct'
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stopping = False
        self.thread = None
        self.started_at = None
        self.enabled = enabled
        self.interval = max(30, min(86400, int(config.get('interval_seconds', 300))))
        self.running = False
        self.next_run = None
        self.last_finished = None
        self.last_result = None
        self.last_error = None
        if enabled:
            conn = connect(db_path)
            try:
                row = conn.execute("SELECT value FROM meta WHERE key='auto_collection'").fetchone()
                if row:
                    saved = json.loads(row['value'])
                    self.enabled = saved['enabled'] is True
                    self.interval = max(30, min(86400, int(saved['interval_seconds'])))
            finally:
                conn.close()

    def status(self):
        with self.lock:
            return dict(enabled=self.enabled, interval_seconds=self.interval,
                        running=self.running, next_run_at=self.next_run,
                        last_finished_at=self.last_finished, last_result=self.last_result,
                        last_error=self.last_error, active=bool(self.thread and self.thread.is_alive()),
                        started_at=self.started_at,
                        mode='legacy' if self.custom_runner else 'durable_lanes',
                        lanes=dict(self.active_tasks), queue=self.queue_status,
                        lane_errors=dict(self.lane_errors),lane_results=dict(self.lane_results),
                        feed_route=self.feed_route,
                        policy={'latest_seconds':self._period('latest'), 'trending_seconds':180,
                                'counter_queue_limit':COUNTER_QUEUE_LIMIT, 'counter_freshness_seconds':60,
                                'shared_endpoint_budget':True, 'social_context_seconds':180})

    def configure(self, enabled, interval):
        with self.lock:
            conn = connect(self.db_path)
            try:
                with conn:
                    conn.execute("INSERT INTO meta(key,value) VALUES('auto_collection',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                 (json.dumps(dict(enabled=enabled, interval_seconds=interval)),))
            finally:
                conn.close()
            self.enabled, self.interval = enabled, interval
            self.next_run = None
            self.wake.set()
        return self.status()

    def collect_now(self, overrides=None):
        """Advance existing pending work; never launch a second collection path."""
        if overrides:
            raise ValueError('统一调度不支持单次参数覆盖，请使用当前采集配置')
        with self.lock:
            if not self.enabled:
                raise ValueError('请先启用自动采集，再立即调度')
            conn = connect(self.db_path)
            try:
                task_queue.ensure_schema(conn)
                self._seed(conn)
                blocked = cooldowns(conn)['feeds']/1000
                kinds = ['latest','latest_head','history','auxiliary','social_context','engagement']
                if self.config.get('collect_trending',True): kinds += ['trending','trending_head']
                if self.config.get('collect_news',True): kinds.append('news')
                with conn:
                    for kind in kinds:
                        due = max(time.time(),blocked) if kind not in ('auxiliary','news','social_context','engagement') else time.time()
                        conn.execute("UPDATE collection_tasks SET due_at=?,updated_at=? WHERE task_key=? AND state='pending'",
                                     (due,utc_now(),'periodic:'+kind))
                added = plan_counter_tasks(conn,self.config)
                self.queue_status = task_queue.snapshot(conn)
                self.wake.set()
                return {'status':'queued','mode':'durable_lanes','counter_tasks_added':added,
                        'queue':self.queue_status}
            finally:
                conn.close()

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stopping = False
            self.started_at = utc_now()
            self.thread = threading.Thread(target=self._legacy_loop if self.custom_runner else self._loop,
                                           name='ctn-auto-collector', daemon=True)
            self.thread.start()

    def stop(self):
        with self.lock:
            self.stopping = True
            self.next_run = None
            self.wake.set()
        if self.thread:
            self.thread.join(timeout=self.config.get('deadline_seconds', 120) + self.config.get('timeout', 15) + 5)

    def _legacy_loop(self):
        while True:
            with self.lock:
                if self.stopping:
                    return
                self.wake.clear()
                if not self.enabled:
                    delay = None
                elif self.next_run:
                    delay = max(0, (datetime.fromisoformat(self.next_run) - datetime.now(timezone.utc)).total_seconds())
                else:
                    delay = 0
            if delay is None or delay > 0:
                self.wake.wait(delay)
                continue
            with self.lock:
                if not self.enabled or self.stopping:
                    continue
                self.running = True
                self.next_run = None
            try:
                result = self.runner(self.db_path, self.config)
                with self.lock:
                    self.last_result = {key: result.get(key) for key in ('run_id', 'status', 'requests', 'posts', 'counters', 'news', 'errors')}
                    self.last_error = None
            except Exception as exc:
                with self.lock:
                    self.last_error = type(exc).__name__
            finally:
                with self.lock:
                    self.running = False
                    self.last_finished = utc_now()
                    self.next_run = (datetime.now(timezone.utc) + timedelta(seconds=self.interval)).isoformat() if self.enabled and not self.stopping else None

    def _period(self, kind):
        return {'latest': max(30,min(60,self.interval)), 'latest_head':45, 'trending':180, 'trending_head':180,
                'history':30, 'auxiliary':120, 'news':300, 'social_context':180, 'engagement':60}.get(kind, 1)

    def _seed(self, conn):
        now = time.time()
        with conn:
            for index, kind in enumerate(('latest','latest_head','trending','trending_head','history','auxiliary','news','social_context',
                                          'engagement')):
                if kind in ('trending','trending_head') and not self.config.get('collect_trending', True):
                    continue
                if kind == 'news' and not self.config.get('collect_news', True):
                    continue
                task_queue.enqueue(conn, 'periodic:'+kind, kind, {}, priority=100-index,
                                   due_at=now+index*.01)

    def _loop(self):
        conn = connect(self.db_path)
        try:
            task_queue.ensure_schema(conn)
            self._seed(conn)
            feeds = ('latest','latest_head','history') + (('trending','trending_head') if self.config.get('collect_trending',True) else ())
            auxiliary = ('auxiliary','social_context','engagement') + (('news',) if self.config.get('collect_news',True) else ())
            groups = [('feeds', feeds), ('counters', ('counter',)), ('auxiliary', auxiliary)]
            self.workers = [threading.Thread(target=self._worker_loop, args=(lane,kinds),
                name='ctn-'+lane, daemon=True) for lane,kinds in groups]
            for worker in self.workers:
                worker.start()
            last_plan = 0
            while not self.stopping:
                if self.enabled and time.monotonic()-last_plan >= self.PLAN_SECONDS:
                    try:
                        plan_counter_tasks(conn, self.config)
                        self.lane_errors.pop('planner', None)
                        self.queue_status = task_queue.snapshot(conn)
                        self.next_run = (datetime.now(timezone.utc)+timedelta(seconds=self.PLAN_SECONDS)).isoformat()
                    except Exception as exc:
                        with self.lock:
                            self.last_error = type(exc).__name__
                            self.lane_errors['planner'] = type(exc).__name__
                        # This connection lives as long as the thread; after a
                        # database restart only a new session recovers.
                        conn = reconnect_if_lost(conn, self.db_path)
                    last_plan = time.monotonic()
                self.wake.wait(.5)
                self.wake.clear()
        except Exception as exc:
            with self.lock:
                self.last_error = type(exc).__name__
        finally:
            # Every request has a short timeout; leases recover even after a
            # hard process exit. No browser tabs or workers are force-killed.
            self.stopping = True
            for worker in self.workers:
                worker.join(timeout=20)
            conn.close()
            with self.lock:
                self.running = False
                self.next_run = None

    def _worker_loop(self, lane, kinds):
        conn = connect(self.db_path)
        try:
            while not self.stopping:
                if not self.enabled:
                    time.sleep(.2)
                    continue
                try:
                    if lane == 'feeds':
                        proxy_available=proxy_feed_available(conn)
                        with self.lock:
                            self.feed_route='proxy' if proxy_available else 'direct'
                        if proxy_available:
                            time.sleep(.5)
                            continue
                    task = task_queue.claim(conn, self.worker_id+':'+lane, kinds, lease_seconds=45)
                    if task is None:
                        time.sleep(.2)
                        continue
                    with self.lock:
                        self.active_tasks[lane] = task['task_key']
                        self.running = True
                    if task['kind'] == 'counter':
                        result = execute_counter_task(self.db_path, self.config, task)
                    else:
                        try:
                            result = collect_stage(self.db_path, self.config, task['kind'],
                                **({'task':task,'interval':self.interval} if lane=='feeds' else {}))
                        except CollectionBusy:
                            result = {'status':'deferred','errors':[], 'requests':0}
                        due = feed_tasks.next_due(conn, task['kind'], result, self.interval) if lane=='feeds' else time.time()+self._period(task['kind'])
                        if lane!='feeds' or result.get('task_completed') is not True:
                            task_queue.complete(conn, task['task_key'], task['lease_token'], result, next_due=due)
                    with self.lock:
                        self.last_result = result
                        self.lane_results[lane] = result
                        self.lane_errors.pop(lane, None)
                        self.last_error = None
                        self.last_finished = utc_now()
                except Exception as exc:
                    # Failed execution retains its durable lease. Another
                    # worker retries after expiry rather than losing work.
                    with self.lock:
                        self.last_error = type(exc).__name__
                        self.lane_errors[lane] = type(exc).__name__
                    conn = reconnect_if_lost(conn, self.db_path)
                    time.sleep(.5)
                finally:
                    with self.lock:
                        self.active_tasks.pop(lane, None)
                        self.running = bool(self.active_tasks)
        finally:
            conn.close()
