"""Bounded public collection; no login, remote writes, or continuous scheduler."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from datetime import datetime, timedelta, timezone
import json
import hashlib
import os
from pathlib import Path
import re
import threading
import time
import urllib.parse

from .db import connect, initialize, raw_record, utc_now
from . import feed_tasks
from .ingest import save_accounts, save_counters, save_news, save_posts
from .transport import BudgetExceeded, ReadOnlyTransport, RequestDeferred, TransportError, validate_path

DEFAULT_CONFIG = {'page_limit': 50, 'max_pages': 5, 'counter_limit': 30,
                  'counter_new_share': 0.5, 'counter_recent_share': 0, 'counter_window_share': 0.4,
                  'latest_incremental_pages': 0, 'trending_max_pages': 0,
                  'relation_limit': 0, 'reply_parent_limit': 0, 'body_refresh_limit': 40,
                  'account_limit': 50, 'news_limit': 10, 'site_limit': 10,
                  'collect_trending': True, 'collect_news': True,
                  'timeout': 15, 'max_retries': 3, 'request_budget': 160,
                  'deadline_seconds': 120, 'max_wait_seconds': 30}
_THREAD_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
COUNTER_QUEUE_LIMIT = 200
COUNTER_PLAN_LIMIT = 100


class CollectionBusy(RuntimeError):
    pass


def _config(config):
    result = dict(DEFAULT_CONFIG)
    if config:
        metadata = {'team_label', 'timezone', 'interval_seconds', 'read_only_remote'}
        unknown = set(config) - set(result) - metadata
        if unknown:
            raise ValueError('unsupported collector settings: ' + ', '.join(sorted(unknown)))
        result.update({key: value for key, value in config.items() if key in result})
    for key, low, high in (('page_limit', 1, 200), ('max_pages', 1, 20),
                           ('counter_limit', 0, 300), ('latest_incremental_pages', 0, 5), ('trending_max_pages', 0, 20),
                           ('relation_limit', 0, 100), ('reply_parent_limit', 0, 20), ('body_refresh_limit', 0, 100), ('account_limit', 0, 300),
                           ('news_limit', 0, 30), ('site_limit', 1, 10),
                           ('max_retries', 0, 5), ('request_budget', 1, 500)):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f'{key} must be an integer between {low} and {high}')
    for key, low, high in (('timeout', .1, 60), ('deadline_seconds', 1, 300),
                           ('max_wait_seconds', 0, 180), ('counter_new_share', 0, 1),
                           ('counter_recent_share', 0, 0.5), ('counter_window_share', 0, 1)):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not low <= value <= high:
            raise ValueError(f'{key} must be between {low} and {high}')
    for key in ('collect_trending', 'collect_news'):
        if not isinstance(result[key], bool):
            raise ValueError(key + ' must be boolean')
    return result


@contextmanager
def _lane_lock(db_path, lane):
    """Local threads + OS advisory file lock; process exit releases the OS lock."""
    path = _lock_path(db_path, lane)
    with _LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(path, threading.Lock())
    if not thread_lock.acquire(blocking=False):
        raise CollectionBusy('a collector is already running for this database')
    handle = None
    acquired = False
    try:
        target = Path(path + '.collect.lock')
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = os.fdopen(os.open(target, os.O_RDWR | os.O_CREAT, 0o600), 'r+b')
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            raise CollectionBusy('a collector is already running for this database') from None
        # Only the first byte is locked. Status readers never acquire any lane.
        handle.seek(1)
        handle.write(str(os.getpid()).encode('ascii'))
        handle.truncate()
        handle.flush()
        yield
    finally:
        if handle is not None:
            if acquired:
                handle.seek(1)
                handle.truncate()
                handle.flush()
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        thread_lock.release()


def _lock_path(db_path, lane):
    if str(db_path).startswith(('postgres://', 'postgresql://')):
        # Credentials never become file names or diagnostics.
        digest = hashlib.sha256(str(db_path).encode()).hexdigest()[:24]
        path = str(Path(__file__).resolve().parent.parent / 'data' / ('pg-' + digest))
    else:
        path = str(Path(db_path).resolve())
    return path + '.' + lane


@contextmanager
def collection_lock(db_path, lane=None):
    """Full CLI runs exclude all lanes; scheduled lanes can run independently."""
    with ExitStack() as stack:
        for name in ([lane] if lane else ['feeds', 'counters', 'auxiliary']):
            stack.enter_context(_lane_lock(db_path, name))
        yield


def is_collecting(db_path):
    """Read-only advisory status. Only collection_lock decides who may collect."""
    for lane in ('feeds', 'counters', 'auxiliary'):
        path = _lock_path(db_path, lane)
        with _LOCKS_GUARD:
            lock = _THREAD_LOCKS.get(path)
            if lock is not None and lock.locked():
                return True
        try:
            with Path(path + '.collect.lock').open('rb') as handle:
                handle.seek(1)
                pid = int(handle.read(20))
            if pid <= 0:
                continue
            if os.name == 'nt':
                import ctypes
                from ctypes import wintypes
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
                kernel.OpenProcess.restype = wintypes.HANDLE
                kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
                kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
                process = kernel.OpenProcess(0x1000, False, pid)
                if process:
                    try:
                        code = wintypes.DWORD()
                        if kernel.GetExitCodeProcess(process, ctypes.byref(code)) and code.value == 259:
                            return True
                    finally:
                        kernel.CloseHandle(process)
            else:
                os.kill(pid, 0)
                return True
        except (OSError, ValueError):
            continue
    return False


def _checkpoint(conn, key):
    row = conn.execute('SELECT value FROM checkpoints WHERE key=?', (key,)).fetchone()
    if row is None:
        return {}
    try:
        result = json.loads(row['value'])
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def _set_checkpoint(conn, key, value, at):
    conn.execute('''INSERT INTO checkpoints(key,value,updated_at) VALUES (?,?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at''',
        (key, json.dumps(value, ensure_ascii=False, sort_keys=True), at))


def _position(post):
    try:
        at = datetime.fromisoformat(post['created_at'].replace('Z', '+00:00'))
        if at.tzinfo is None:
            return None
        return (at.astimezone(timezone.utc).isoformat(timespec='microseconds'), int(post['post_id']))
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def _error(result, stage, exc, entity_id=None):
    # Never include arbitrary remote response bodies, URLs, credentials or config values.
    if isinstance(exc, TransportError):
        message = str(exc)
    elif isinstance(exc, (ValueError, TypeError)):
        message = 'invalid public response: ' + str(exc)[:180]
    else:
        message = type(exc).__name__
    result['errors'].append({'stage': stage, 'entity_id': entity_id,
                             'status': getattr(exc, 'status', None), 'message': message})


def _feed(conn, transport, source, cfg, result, seen, checkpoint_key=None, refresh_head=True):
    key = checkpoint_key or 'feed:' + source
    state = _checkpoint(conn, key)
    cursor = state.get('cursor')
    boundary = tuple(state['watermark']) if state.get('watermark') else None
    cycle_head = tuple(state['cycle_head']) if state.get('cycle_head') else None
    has_feed_page = any(feed.get('pages', 0) for feed in result['feeds'].values())
    coverage = {'pages': 0, 'cycle_complete': False, 'resumed': bool(cursor), 'head_refreshed': False}
    result['feeds'][source] = coverage
    visited = set()
    for page_index in range(cfg['max_pages']):
        # A backfill must not consume every request or the entire deadline
        # before counters run. Always allow the first live page of the run.
        reserve = cfg.get('_counter_request_reserve', 0)
        time_reserve = cfg.get('_counter_time_reserve', 0)
        request_edge = reserve and transport.attempts >= transport.request_budget - reserve
        time_edge = (time_reserve and hasattr(transport, 'deadline') and hasattr(transport, 'clock')
                     and transport.clock() >= transport.deadline - time_reserve)
        if (has_feed_page or page_index) and (request_edge or time_edge):
            coverage['paused_for_counters'] = True
            return
        # Keep the live head visible while an older cursor is being backfilled.
        # This extra head consumes one of max_pages and MUST NOT advance the
        # backfill's cycle_head: doing so could skip the gap below that page.
        head_only = bool(refresh_head and cursor and page_index == 0 and cfg['max_pages'] > 1)
        query = {'sort': 'recent' if source == 'latest' else 'trending', 'limit': cfg['page_limit']}
        if cursor and not head_only:
            query['cursor'] = cursor
        endpoint = '/api/posts?' + urllib.parse.urlencode(query)
        try:
            payload = transport.get_json('social', endpoint)
        except TransportError as exc:
            # Expired ranking cursors cannot be replayed indefinitely.
            if cursor and not head_only and exc.status in (400, 404, 410):
                with conn:
                    _set_checkpoint(conn, key, {**state, 'cursor': None, 'watermark': state.get('watermark'),
                                               'cycle_head': None}, utc_now())
            raise
        if not isinstance(payload, dict) or not isinstance(payload.get('items'), list):
            raise ValueError('feed must contain an items list')
        posts = payload['items']
        if any(not isinstance(post, dict) for post in posts):
            raise ValueError('feed items must be objects')
        next_cursor = payload.get('next_cursor')
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise ValueError('feed cursor must be text or null')
        if next_cursor:
            validate_path('social', '/api/posts?sort=recent&cursor=' + urllib.parse.quote(next_cursor, safe=''))
        if next_cursor and not head_only and (next_cursor == cursor or next_cursor in visited):
            raise ValueError('server repeated a pagination cursor')
        if cursor and not head_only:
            visited.add(cursor)
        positions = [p for post in posts if (p := _position(post)) is not None]
        if cycle_head is None and positions and not head_only:
            cycle_head = max(positions)
        reached = source == 'latest' and boundary is not None and any(p <= boundary for p in positions)
        complete = not posts or not next_cursor or reached
        if complete:
            head = max([x for x in (boundary, cycle_head) if x is not None], default=None)
            new_state = {'cursor': None, 'cycle_head': None, 'watermark': head}
        else:
            new_state = {'cursor': next_cursor, 'cycle_head': cycle_head, 'watermark': boundary}
        at = utc_now()
        new_state.update(pages_completed=state.get('pages_completed', 0) + (0 if head_only else 1),
                         items_seen=state.get('items_seen', 0) + (0 if head_only else len(posts)),
                         oldest_created_at=min([p[0] for p in positions] + [state['oldest_created_at']] if state.get('oldest_created_at') else [p[0] for p in positions], default=None),
                         reached_end=state.get('reached_end', False) or (not head_only and (not posts or not next_cursor)))
        with conn:
            raw_record(conn, 'social:' + source, '/api/posts', payload, at)
            save_posts(conn, posts, source, at)
            if not head_only:
                _set_checkpoint(conn, key, new_state, at)
        seen.update(post['post_id'] for post in posts)
        coverage['pages'] += 1
        if head_only:
            coverage['head_refreshed'] = True
            continue
        if complete:
            coverage['cycle_complete'] = True
            return
        cursor = next_cursor
        state = new_state


def _counter_cutoff(at, minutes):
    """Match ctn_timestamp's exact UTC key for indexed time comparisons."""
    cutoff = datetime.fromisoformat(at.replace('Z', '+00:00')).astimezone(timezone.utc) - timedelta(minutes=minutes)
    return cutoff.isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _counter_windows(conn, limit=300, now=None, *, exclude=()):
    """Find unfilled endpoints, bounded to 10,000 posts created in the last day.

    The first real counter sample within ten minutes of publication is the
    baseline, matching analytics._growth. An existing sample anywhere inside
    the analytics tolerance satisfies an endpoint; priority requests start at
    the target itself, so they do not spend the early side of that tolerance.
    """
    at = now or utc_now()
    snapshot_cutoff = _counter_cutoff(at, 24 * 60 + 11)
    # Calculate the database's time value once, preserving its rounding rules.
    # PostgreSQL's compatibility julianday() is a non-inlined function; calling
    # it for the same timestamp on every candidate adds substantial CPU work.
    now_jd = conn.execute('SELECT julianday(?)', (at,)).fetchone()[0]
    # The publication window comes from the created_at key index; julianday is
    # computed only for the bounded candidates, not for every recent post.
    rows = conn.execute('''WITH candidates AS MATERIALIZED (
        SELECT post_id,created_at,julianday(created_at) AS created_jd FROM (
          SELECT post_id,created_at FROM posts
          WHERE ctn_timestamp(created_at)>=? AND ctn_timestamp(created_at)<=?
          AND (created_at GLOB '*Z' OR SUBSTR(created_at,-6,1) IN ('+','-'))
          ORDER BY created_at DESC,post_id DESC LIMIT 10001) recent
      ), first_samples AS MATERIALIZED (
        SELECT p.post_id,p.created_jd,
          (SELECT julianday(c.observed_at) FROM counter_snapshots c
          WHERE c.post_id=p.post_id AND ctn_timestamp(c.observed_at)>=ctn_timestamp(p.created_at)
          ORDER BY ctn_timestamp(c.observed_at),c.id LIMIT 1) AS first_sample
        FROM candidates p ORDER BY p.created_at DESC,p.post_id DESC LIMIT 10000
      ), baselines AS MATERIALIZED (
        SELECT post_id,CASE WHEN first_sample<=created_jd+10.0/1440 THEN first_sample END AS baseline
        FROM first_samples
      ), windows(minutes,tolerance) AS (VALUES (30,6),(60,12),(180,36))
      SELECT b.post_id,w.minutes AS window_minutes,
        b.baseline+w.minutes/1440.0 AS target,
        b.baseline+(w.minutes+w.tolerance)/1440.0 AS deadline,
        ? AS now_jd,q.value AS attempt_value,q.updated_at AS last_attempt,
        julianday(q.updated_at) AS attempt_jd,
        EXISTS(SELECT 1 FROM counter_snapshots c WHERE c.post_id=b.post_id AND ctn_timestamp(c.observed_at)>=?
          AND julianday(c.observed_at)>b.baseline
          AND julianday(c.observed_at) BETWEEN
            b.baseline+(w.minutes-w.tolerance)/1440.0-0.00000001 AND
            b.baseline+(w.minutes+w.tolerance)/1440.0+0.00000001) AS satisfied,
        (SELECT COUNT(*) FROM candidates)>10000 AS truncated
      FROM baselines b CROSS JOIN windows w
      LEFT JOIN checkpoints q ON q.key='counter_attempt:' || b.post_id
      WHERE b.baseline IS NOT NULL
      UNION ALL SELECT NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,
        (SELECT COUNT(*) FROM candidates)>10000''',
        (_counter_cutoff(at, 24 * 60 + 10), _counter_cutoff(at, 0), now_jd, snapshot_cutoff)).fetchall()
    diagnostics = {'as_of': at, 'lookback_hours': 24, 'candidate_limit': 10000, 'counts_truncated': False,
                   'by_window': {str(minutes): {'queued': 0, 'backoff': 0, 'missed': 0,
                                               'satisfied': 0, 'upcoming': 0}
                                 for minutes in (30, 60, 180)}}
    due = []
    for item in rows:
        row = dict(item)
        diagnostics['counts_truncated'] |= bool(row['truncated'])
        if row['post_id'] is None:
            continue
        counts = diagnostics['by_window'][str(row['window_minutes'])]
        if row['satisfied']:
            counts['satisfied'] += 1
        elif row['now_jd'] > row['deadline'] + 0.00000001:
            counts['missed'] += 1
        elif row['now_jd'] + 0.00000001 < row['target']:
            counts['upcoming'] += 1
        else:
            try:
                attempt = json.loads(row['attempt_value'] or '{}')
                failed = attempt.get('status') == 'failed'
                retry_minutes = 15 if attempt.get('http_status') in (403, 404, 410) else 5
            except (ValueError, AttributeError):
                failed = False
                retry_minutes = 5
            if failed and row['attempt_jd'] is not None and row['attempt_jd'] > row['now_jd'] - retry_minutes/1440.0:
                counts['backoff'] += 1
            else:
                counts['queued'] += 1
                due.append({'post_id': row['post_id'], 'sampling_queue': 'tracked',
                            'window_minutes': row['window_minutes'], 'deadline': row['deadline'],
                            'last_attempt': row['last_attempt'] or ''})
    due.sort(key=lambda row: (row['deadline'], row['last_attempt'], -row['post_id']))
    diagnostics['queued'] = sum(row['queued'] for row in diagnostics['by_window'].values())
    diagnostics['backoff'] = sum(row['backoff'] for row in diagnostics['by_window'].values())
    diagnostics['missed'] = sum(row['missed'] for row in diagnostics['by_window'].values())
    excluded = set(exclude)
    return [row for row in due if row['post_id'] not in excluded][:limit], diagnostics


def _counter_queues(conn, limit, excluded, at):
    """Oldest-attempted new and tracked candidates, each bounded to ``limit``.

    counter_sampling holds each post's first-seen, latest sample, latest
    attempt and latest browser sample keys, maintained by triggers on every
    writer. Each queue walks its (has_sample, last_attempt) index and stops at
    ``limit``, instead of probing the full history of every post per round.
    """
    exclusions = ' AND s.post_id NOT IN (' + ','.join('?' for _ in excluded) + ')' if excluded else ''
    queues = {}
    for name, has_sample in (('new', 0), ('tracked', 1)):
        # A failed attempt backs off for 5 minutes and 403/404/410 for 15. A
        # post the browser sampled within the last minute needs no request.
        queues[name] = [dict(row) for row in conn.execute('''SELECT s.post_id,s.last_attempt,? AS sampling_queue
            FROM counter_sampling s WHERE s.has_sample=?
            AND NOT COALESCE(s.attempt_failed=1 AND s.attempted_at>?,FALSE)
            AND NOT COALESCE(s.attempt_http IN (403,404,410) AND s.attempted_at>?,FALSE)
            AND NOT COALESCE(s.browser_sampled_at>?,FALSE)''' + exclusions + '''
            ORDER BY s.last_attempt,s.post_id DESC LIMIT ?''',
            (name, has_sample, _counter_cutoff(at, 5), _counter_cutoff(at, 15), _counter_cutoff(at, 1),
             *excluded, limit))]
    return queues


def _standard_counter_schedule(conn, cfg, *, exclude=(), now=None, queues=None):
    """The original new/history quotas, with persistent failure backoff.

    ``queues`` may reuse _counter_queues() read for the same exclusions with at
    least this limit: both queues are strictly ordered, so a prefix is identical.
    """
    limit = cfg['counter_limit']
    if not limit:
        return []
    at = now or utc_now()
    excluded = tuple(exclude)
    exclusions = ' AND p.post_id NOT IN (' + ','.join('?' for _ in excluded) + ')' if excluded else ''
    if queues is None:
        queues = _counter_queues(conn, limit, excluded, at)
    queues = {name: rows[:limit] for name, rows in queues.items()}
    share = cfg['counter_new_share']
    if limit == 1 and 0 < share < 1 and queues['new'] and queues['tracked']:
        phase = _checkpoint(conn, 'counter_scheduler').get('single_next', 'tracked')
        new_slots = 1 if phase == 'new' else 0
    else:
        new_slots = int(limit * share + .5)
        if limit >= 2 and 0 < share < 1:
            new_slots = max(1, min(limit - 1, new_slots))
    selected = {'new': min(new_slots, len(queues['new'])),
                'tracked': min(limit - new_slots, len(queues['tracked']))}
    spare = limit - sum(selected.values())
    # Borrow unused quota; historical work starts first if both queues exist.
    for name in ('tracked', 'new'):
        extra = min(spare, len(queues[name]) - selected[name])
        selected[name] += extra
        spare -= extra
    recent_slots = int(selected['new'] * cfg['counter_recent_share'])
    if recent_slots:
        # Publication time uses the created_at key index; sampling state comes
        # from counter_sampling. Any attempt in 5 minutes, or 403/404/410 in
        # 15, still waits.
        recent = conn.execute('''SELECT p.post_id FROM posts p
            JOIN counter_sampling s ON s.post_id=p.post_id
            WHERE ctn_timestamp(p.created_at)>=? AND ctn_timestamp(p.created_at)<=?
            AND s.has_sample=0
            AND NOT COALESCE(s.attempted_at>?,FALSE)
            AND NOT COALESCE(s.attempt_http IN (403,404,410) AND s.attempted_at>?,FALSE)'''
            + exclusions + ''' ORDER BY p.created_at DESC,p.post_id DESC LIMIT ?''',
            (_counter_cutoff(at, 10), _counter_cutoff(at, 0), _counter_cutoff(at, 5),
             _counter_cutoff(at, 15), *excluded, recent_slots)).fetchall()
        seen_recent = {r['post_id'] for r in recent}
        queues['new'] = [dict(r,sampling_queue='new') for r in recent] + [r for r in queues['new'] if r['post_id'] not in seen_recent]
    scheduled = []
    for index in range(max(selected.values(), default=0)):
        for name in ('tracked', 'new'):
            if index < selected[name]:
                scheduled.append(queues[name][index])
    return scheduled


def _counter_schedule(conn, cfg, *, exclude=(), now=None, window_state=None):
    """Reserve due windows and fresh baselines; lend empty slots to history."""
    limit = cfg['counter_limit']
    if not limit:
        return []
    if not cfg['counter_window_share']:
        return _standard_counter_schedule(conn, cfg, exclude=exclude, now=now)
    due, _ = window_state or _counter_windows(conn, limit, now, exclude=exclude)
    excluded = set(exclude)
    due = [row for row in due if row['post_id'] not in excluded]
    if not due or not limit or not cfg['counter_window_share']:
        return _standard_counter_schedule(conn, cfg, exclude=exclude, now=now)
    if limit == 1:
        # One request left still gives windows first use. After that, alternate
        # with a fresh baseline while both queues have work across runs.
        phase = _checkpoint(conn, 'counter_scheduler').get('window_next', 'windows')
        if phase == 'new' and cfg['counter_new_share']:
            fresh = _standard_counter_schedule(conn, {**cfg, 'counter_new_share': 1}, exclude=exclude, now=now)
            if fresh and fresh[0]['sampling_queue'] == 'new':
                return fresh
        return due[:1]
    window_slots = max(1, int(limit * cfg['counter_window_share'] + .5))
    if cfg['counter_new_share']:
        window_slots = min(limit - 1, window_slots)
    window_slots = min(len(due), window_slots)
    priority = due[:window_slots]
    remaining = limit - len(priority)
    new_slots = min(remaining, int(limit * cfg['counter_new_share'] + .5))
    regular_exclude = (*excluded, *(row['post_id'] for row in due))
    at = now or utc_now()
    # One candidate read serves both the history probe and the regular quota.
    queues = _counter_queues(conn, remaining, regular_exclude, at) if remaining else None
    # A one-slot history request would pick tracked work exactly when any exists.
    if limit >= 3 and remaining >= 2 and new_slots == remaining and queues['tracked']:
        new_slots -= 1
    regular_cfg = {**cfg, 'counter_limit': remaining,
                   'counter_new_share': new_slots / remaining if remaining else 0}
    regular = _standard_counter_schedule(conn, regular_cfg, exclude=regular_exclude, now=at, queues=queues)
    # If new/history queues are empty, the window queue borrows their quota.
    spare = limit - len(priority) - len(regular)
    return priority + regular + due[window_slots:window_slots + spare]


def record_counter_turn(conn, row, at=None):
    """Advance fairness only inside the transaction accepting an actual attempt."""
    if row.get('sampling_queue') not in ('new', 'tracked'):
        return
    if getattr(conn, 'dialect', None) == 'postgresql':
        if not conn.in_transaction:
            conn.execute('BEGIN ISOLATION LEVEL READ COMMITTED')
        lock_id = int.from_bytes(hashlib.sha256(b'ctn:counter_scheduler').digest()[:8], signed=True)
        conn.execute('SELECT pg_advisory_xact_lock(?)', (lock_id,))
    scheduler = _checkpoint(conn, 'counter_scheduler')
    if row.get('single_slot'):
        scheduler['single_next'] = 'new' if row['sampling_queue'] == 'tracked' else 'tracked'
    if row.get('window_minutes') or row['sampling_queue'] == 'new':
        scheduler['window_next'] = 'new' if row.get('window_minutes') else 'windows'
    _set_checkpoint(conn, 'counter_scheduler', scheduler, at or utc_now())


def _sample_counters(conn, transport, cfg, result, seen_posts, counters, diagnostics=None):
    sampling = result.setdefault('counter_sampling', {
        name: {'selected': 0, 'attempted': 0, 'saved': 0} for name in ('new', 'tracked')})
    if 'windows' not in sampling:
        if diagnostics is None:
            _, diagnostics = _counter_windows(conn)
        sampling['windows'] = {'selected': 0, 'attempted': 0, 'saved': 0, **diagnostics}
        for stats in sampling['windows']['by_window'].values():
            stats.update(selected=0, attempted=0, saved=0)
    for row in counters:
        sampling[row['sampling_queue']]['selected'] += 1
        if row.get('window_minutes'):
            sampling['windows']['selected'] += 1
            sampling['windows']['by_window'][str(row['window_minutes'])]['selected'] += 1
    body_refresh = []
    for row in counters:
        if row.get('window_minutes'):
            dispatch_at = datetime.fromisoformat(utc_now().replace('Z', '+00:00'))
            if dispatch_at.timestamp()/86400.0 + 2440587.5 > row['deadline'] + 0.00000001:
                result['counter_window_expired_skipped'] = result.get('counter_window_expired_skipped', 0) + 1
                continue
        # Recheck at dispatch: browser data can arrive after the queue was built.
        fresh = conn.execute('''SELECT 1 FROM counter_snapshots s JOIN raw_records r ON r.id=s.raw_id
            WHERE s.post_id=? AND r.source='browser:counters'
            AND julianday(s.observed_at)>julianday(?)-1.0/1440 LIMIT 1''', (row['post_id'], utc_now())).fetchone()
        if fresh:
            result['counter_recent_skipped'] = result.get('counter_recent_skipped', 0) + 1
            continue
        before = transport.attempts
        counter_status, http_status = 'failed', None
        try:
            # Counter samples are the analysis contract. Optional body refresh
            # follows the entire counter queue so a tight budget still creates
            # repeated measurements rather than spending it on metadata first.
            payload = transport.get_json('social', f'/api/posts/{row["post_id"]}/counters', entity_id=row['post_id'])
            saved = save_counters(conn, row['post_id'], payload)
            counter_status = 'saved'
            result['counters'] += saved
            sampling[row['sampling_queue']]['saved'] += saved
            if row.get('window_minutes'):
                sampling['windows']['saved'] += saved
                sampling['windows']['by_window'][str(row['window_minutes'])]['saved'] += saved
            if row['post_id'] not in seen_posts:
                body_refresh.append(row['post_id'])
        except BudgetExceeded:
            raise
        except Exception as exc:
            http_status = getattr(exc, 'status', None)
            _error(result, 'counters', exc, row['post_id'])
        finally:
            if transport.attempts > before:
                sampling[row['sampling_queue']]['attempted'] += 1
                if row.get('window_minutes'):
                    sampling['windows']['attempted'] += 1
                    sampling['windows']['by_window'][str(row['window_minutes'])]['attempted'] += 1
                with conn:
                    _set_checkpoint(conn, 'counter_attempt:' + str(row['post_id']),
                                    {'status': counter_status, 'http_status': http_status}, utc_now())
                    record_counter_turn(conn, {**row, 'single_slot': cfg['counter_limit'] == 1})
    return body_refresh


def _account_candidates(conn, limit):
    # NULLS FIRST preserves never-attempted priority on both databases. PostgreSQL
    # cannot resolve a SELECT alias inside ORDER BY's `alias IS NOT NULL`.
    return conn.execute('''SELECT DISTINCT p.author_id,COALESCE(q.updated_at,a.last_seen) AS last_attempt
        FROM posts p LEFT JOIN accounts a ON a.account_id=p.author_id
        LEFT JOIN checkpoints q ON q.key='account_attempt:' || p.author_id WHERE p.author_id IS NOT NULL
        ORDER BY last_attempt ASC NULLS FIRST,p.author_id LIMIT ?''', (limit,)).fetchall()


def _refresh(conn, transport, cfg, result, seen_posts, *, exclude=(), body_refresh=None):
    body_refresh = list(body_refresh or [])
    if cfg['counter_limit']:
        window_state = _counter_windows(conn, cfg['counter_limit'], exclude=exclude)
        counters = _counter_schedule(conn, cfg, exclude=exclude, window_state=window_state)
        body_refresh += _sample_counters(conn, transport, cfg, result, seen_posts, counters, window_state[1])
    if cfg['relation_limit'] or cfg['reply_parent_limit']:
        from .relations import enrich_relations
        enrich_relations(conn, transport, cfg, result)
    for post_id in body_refresh[:cfg['body_refresh_limit']]:
        try:
            post = transport.get_json('social', f'/api/posts/{post_id}', entity_id=post_id)
            if not isinstance(post, dict) or post.get('post_id') != post_id:
                raise ValueError('post_id does not match requested post')
            with conn:
                save_posts(conn, [post], 'refresh')
        except BudgetExceeded:
            raise
        except Exception as exc:
            _error(result, 'post_refresh', exc, post_id)
    accounts = _account_candidates(conn, cfg['account_limit'])
    for row in accounts:
        before = transport.attempts
        try:
            payload = transport.get_json('social', f'/api/accounts/{row["author_id"]}', entity_id=row['author_id'])
            if not isinstance(payload, dict) or payload.get('account_id') != row['author_id']:
                raise ValueError('account_id does not match requested account')
            result['accounts'] += save_accounts(conn, [payload])
        except BudgetExceeded:
            raise
        except Exception as exc:
            _error(result, 'accounts', exc, row['author_id'])
        finally:
            if transport.attempts > before:
                with conn:
                    _set_checkpoint(conn, 'account_attempt:' + str(row['author_id']), {}, utc_now())


def _news(conn, transport, cfg, result):
    payload = transport.get_json('news', '/index.json')
    if not isinstance(payload, dict) or not isinstance(payload.get('sites'), list):
        raise ValueError('news index must contain sites')
    with conn:
        raw_record(conn, 'news:index', '/index.json', payload, utc_now())
    candidates = {}
    sites = payload['sites']
    if not sites:
        return
    state = _checkpoint(conn, 'news:sites')
    start = state.get('next_index', 0)
    start = start % len(sites) if type(start) is int else 0
    remaining = max(0, transport.request_budget - transport.attempts)
    # Leave room for at least one front page before reserving body requests.
    # Scheduled news has eight requests total, including the index above.
    reserved = min(cfg['news_limit'], max(0, remaining - 1))
    for offset in range(min(cfg['site_limit'], len(sites))):
        if transport.request_budget - transport.attempts <= reserved:
            break
        index = (start + offset) % len(sites)
        site = sites[index]
        try:
            sub = site.get('sub_url') if isinstance(site, dict) else None
            if not isinstance(sub, str):
                raise ValueError('news site requires sub_url')
            front_path = '/' + sub + '.json'
            validate_path('news', front_path)
            front = transport.get_json('news', front_path)
            if not isinstance(front, dict) or not isinstance(front.get('articles'), list):
                raise ValueError('news front page must contain articles')
            articles = []
            for item in front['articles']:
                if not isinstance(item, dict) or not isinstance(item.get('slug'), str):
                    raise ValueError('article requires slug')
                validate_path('news', f'/{sub}/articles/{item["slug"]}.json')
                article = {**item, 'site': site}
                articles.append(article)
                candidates[(sub, item['slug'])] = article
            with conn:
                raw_record(conn, 'news:front', front_path, front, utc_now())
                save_news(conn, articles)
            result['news'] += len(articles)
        except BudgetExceeded:
            raise
        except Exception as exc:
            _error(result, 'news_front', exc)
        finally:
            # Advance after attempted work, including a failed/slow site, so
            # a bounded run never starts with the same exhausted prefix forever.
            with conn:
                _set_checkpoint(conn, 'news:sites', {'next_index': (index + 1) % len(sites)}, utc_now())
    # Never let one prolific publication fill every body slot on every run.
    enriched = []
    for key, item in candidates.items():
        old = conn.execute('SELECT body,last_seen FROM news_articles WHERE article_key=?', ('/'.join(key),)).fetchone()
        enriched.append((old is not None and old['body'] is not None, item.get('published_at') or '', key, item))
    enriched.sort(key=lambda row: (not row[0], row[1]), reverse=True)
    for _, _, (sub, slug), item in enriched[:cfg['news_limit']]:
        try:
            path = f'/{sub}/articles/{slug}.json'
            full = transport.get_json('news', path)
            if not isinstance(full, dict) or not isinstance(full.get('article'), dict):
                raise ValueError('news response must contain article')
            article = {**full['article'], 'site': item['site']}
            if article.get('slug') != slug:
                raise ValueError('article slug does not match requested article')
            with conn:
                raw_record(conn, 'news:article', path, full, utc_now())
                save_news(conn, [article])
        except BudgetExceeded:
            raise
        except Exception as exc:
            _error(result, 'news_article', exc)


def collect_stage(db_path, config, stage, *, task=None, interval=60):
    """One bounded independent lane; long feed cooldowns never sleep here."""
    if stage not in ('latest', 'latest_head', 'history', 'trending', 'trending_head', 'auxiliary', 'news', 'social_context',
                     'engagement'):
        raise ValueError('unknown collection stage')
    cfg = _config(config)
    lane = 'feeds' if stage in ('latest', 'latest_head', 'history', 'trending', 'trending_head') else 'auxiliary'
    cfg.update(max_pages=1, counter_limit=0, max_retries=0, max_wait_seconds=.4,
               request_budget=8, deadline_seconds=12, timeout=min(cfg['timeout'], 8))
    cfg.update(account_limit=min(3, cfg['account_limit']), relation_limit=min(4, cfg['relation_limit']),
               reply_parent_limit=min(1, cfg['reply_parent_limit']), body_refresh_limit=0,
               news_limit=min(2, cfg['news_limit']))
    result = dict(status='running', stage=stage, posts=0, counters=0, accounts=0,
                  news=0, errors=[], feeds={}, requests=0, waited_seconds=0)
    with collection_lock(db_path, lane):
        conn = connect(db_path)
        try:
            with conn:
                result['run_id'] = conn.execute('INSERT INTO collection_runs(started_at,status,metrics_json) VALUES(?,?,?)',
                    (utc_now(), 'running', json.dumps({'stage': stage}))).lastrowid
            egress = {}
            if stage in ('auxiliary','news','social_context','engagement'):
                from .egress import choose_public_egress
                egress = choose_public_egress(conn)
            transport = ReadOnlyTransport(conn, **egress, **{key: cfg[key] for key in
                ('timeout', 'max_retries', 'request_budget', 'deadline_seconds', 'max_wait_seconds')})
            seen = set()
            plan = payload = None
            failed_status = None
            try:
                if stage in feed_tasks.KINDS:
                    plan = feed_tasks.prepare(conn, stage, cfg)
                    if plan is not None:
                        try:
                            payload = transport.get_json('social', plan['path'])
                        except TransportError as exc:
                            failed_status = exc.status
                            raise
                elif stage == 'auxiliary':
                    _refresh(conn, transport, cfg, result, seen)
                elif stage == 'news':
                    _news(conn, transport, cfg, result)
                elif stage == 'social_context':
                    from .social_topics import collect_social_context
                    collect_social_context(conn, transport, cfg, result)
                elif stage == 'engagement':
                    from .engagement import collect
                    collect(conn, transport, cfg, result)
                result['status'] = 'partial' if result['errors'] else 'completed'
            except (RequestDeferred, BudgetExceeded, feed_tasks.StaleFeedPlan) as exc:
                result['status'] = 'deferred'
                _error(result, stage, exc)
            except Exception as exc:
                result['status'] = 'partial'
                _error(result, stage, exc)
            seen.update(result.pop('post_ids', set()))
            result.update(posts=len(seen), requests=transport.attempts, waited_seconds=transport.waited)
            with conn:
                if stage in feed_tasks.KINDS:
                    from . import task_queue
                    task_queue.begin_completion(conn, task['task_key'] if task else None)
                    lease_now = time.time()
                    valid = task is None or task_queue.owned(conn,task['task_key'],task['lease_token'],now=lease_now)
                    if not valid:
                        result['status']='stale_lease'
                    else:
                        if plan is not None and payload is not None:
                            try:
                                saved=feed_tasks.apply(conn,plan,payload,utc_now())
                                seen.update(saved.pop('post_ids'))
                                result.update(saved)
                            except feed_tasks.StaleFeedPlan as exc:
                                result['status']='deferred'
                                _error(result,stage,exc)
                        elif plan is not None and failed_status is not None:
                            feed_tasks.reset_expired(conn,plan,failed_status)
                        result['posts']=len(seen)
                        if task is not None:
                            result['task_completed']=True
                            task_queue.finish(conn,task['task_key'],task['lease_token'],result,
                                next_due=feed_tasks.next_due(conn,stage,result,interval),now=lease_now)
                conn.execute('''UPDATE collection_runs SET finished_at=?,status=?,metrics_json=?,error=? WHERE id=?''',
                    (utc_now(), result['status'], json.dumps(result), json.dumps(result['errors']) if result['errors'] else None, result['run_id']))
            return result
        finally:
            conn.close()


def plan_counter_tasks(conn, config):
    """Select fair new/window/history work once, shared by direct and proxy workers."""
    from .task_queue import enqueue
    cfg = _config(config)
    cfg['counter_limit'] = min(COUNTER_PLAN_LIMIT, cfg['counter_limit'])
    if not cfg['counter_limit']:
        return 0
    active = conn.execute("SELECT payload_json FROM collection_tasks WHERE kind='counter' AND state IN ('pending','leased')").fetchall()
    excluded = [json.loads(row['payload_json'])['post_id'] for row in active]
    # Keep one batch buffered while the next selection runs. HTTP pacing and
    # each planning batch remain unchanged; only bounded queued work grows.
    queue_limit = COUNTER_QUEUE_LIMIT
    if len(excluded) >= queue_limit:
        return 0
    cfg['counter_limit'] = min(cfg['counter_limit'], queue_limit-len(excluded))
    rows = _counter_schedule(conn, cfg, exclude=excluded)
    added = 0
    with conn:
        for row in rows:
            fresh = conn.execute('''SELECT 1 FROM counter_snapshots WHERE post_id=?
                AND ctn_timestamp(observed_at)>? LIMIT 1''', (row['post_id'], _counter_cutoff(utc_now(), 1))).fetchone()
            if fresh:
                continue
            payload = {**row, 'single_slot': cfg['counter_limit'] == 1,
                       'service': 'social', 'path': f'/api/posts/{row["post_id"]}/counters'}
            # Preserve the selector's 40/40/20-like interleaving rather than
            # starving historical samples with an always-full new queue.
            enqueue(conn, 'counter:' + str(row['post_id']), 'counter', payload, priority=10)
            added += 1
    return added


def execute_counter_task(db_path, config, task):
    from . import task_queue
    try:
        with collection_lock(db_path, 'counters'):
            return _execute_counter_task(db_path, config, task)
    except CollectionBusy:
        conn = connect(db_path)
        try:
            with conn:
                task_queue.defer(conn, task['task_key'], task['lease_token'], time.time()+1, 'collector_busy')
        finally:
            conn.close()
        return {'status':'deferred','stage':'counter','requests':0,'counters':0,'errors':[]}


def _execute_counter_task(db_path, config, task):
    """Fetch outside DB transactions, then atomically fence and save the result."""
    from . import task_queue
    from .coordination import cooldowns
    cfg = _config(config)
    row = task['payload']
    conn = connect(db_path)
    result = dict(status='completed', stage='counter', posts=0, counters=0, news=0, errors=[], requests=0)
    try:
        if not task_queue.owned(conn, task['task_key'], task['lease_token']):
            return {**result, 'status': 'lease_expired'}
        recent_cutoff = _counter_cutoff(utc_now(), 1)
        recent_operator = '>'
        if row.get('requested_by') == 'agent_competition' and row.get('target_at') is not None:
            # A snapshot just before the maturity target is not its endpoint.
            # Keep the ordinary freshness guard for all other queued work.
            recent_cutoff = datetime.fromtimestamp(row['target_at'], timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
            recent_operator = '>='
        recent = conn.execute('SELECT 1 FROM counter_snapshots WHERE post_id=? '
            'AND ctn_timestamp(observed_at)' + recent_operator + '? LIMIT 1',
            (row['post_id'], recent_cutoff)).fetchone()
        expired_window = row.get('deadline') and time.time()/86400 + 2440587.5 > row['deadline']
        if recent or expired_window:
            result['status'] = 'already_sampled' if recent else 'window_expired'
            task_queue.complete(conn, task['task_key'], task['lease_token'], result)
            return result
        transport = ReadOnlyTransport(conn, timeout=min(cfg['timeout'], 8), max_retries=0,
            request_budget=1, deadline_seconds=10, max_wait_seconds=.4)
        try:
            payload = transport.get_json('social', row['path'], entity_id=row['post_id'])
        except (RequestDeferred, BudgetExceeded) as exc:
            result['status'] = 'deferred'
            result['requests'] = transport.attempts
            post_lease = _checkpoint(conn, 'coordination:post:' + str(row['post_id']))
            object_until = post_lease.get('until',0) if post_lease.get('actor') != 'backend' else 0
            with conn:
                task_queue.defer(conn, task['task_key'], task['lease_token'], max(time.time()+1, object_until, cooldowns(conn)['counters']/1000))
            return result
        except Exception as exc:
            _error(result, 'counters', exc, row['post_id'])
            result.update(status='partial', requests=transport.attempts)
            if getattr(exc, 'status', None) == 429:
                task_queue.complete(conn, task['task_key'], task['lease_token'], result,
                    ingest_callback=lambda c: record_counter_turn(c, row),
                    next_due=max(time.time()+1, cooldowns(conn)['counters']/1000))
            else:
                def failed(c):
                    _set_checkpoint(c, 'counter_attempt:' + str(row['post_id']),
                        {'status': 'failed', 'http_status': getattr(exc, 'status', None)}, utc_now())
                    record_counter_turn(c, row)
                task_queue.complete(conn, task['task_key'], task['lease_token'], result, ingest_callback=failed)
            return result
        result['requests'] = transport.attempts
        at = utc_now()
        def ingest(c):
            result['counters'] = save_counters(c, row['post_id'], payload, at)
            _set_checkpoint(c, 'counter_attempt:' + str(row['post_id']), {'status': 'saved', 'http_status': 200}, at)
            record_counter_turn(c, row, at)
        if not task_queue.complete(conn, task['task_key'], task['lease_token'], result, ingest_callback=ingest):
            result['status'] = 'lease_expired'
        return result
    finally:
        conn.close()


def collect_once(db_path, config=None):
    """Run a bounded collection. Returns explicit partial failures and run metrics."""
    cfg = _config(config)
    result = {'status': 'running', 'run_id': None, 'posts': 0, 'counters': 0,
              'accounts': 0, 'news': 0, 'errors': [], 'feeds': {},
              'scope': {key: cfg[key] for key in ('page_limit', 'max_pages', 'counter_limit',
                        'counter_new_share', 'counter_recent_share', 'counter_window_share', 'account_limit', 'news_limit', 'request_budget', 'deadline_seconds',
                        'latest_incremental_pages', 'trending_max_pages', 'relation_limit', 'reply_parent_limit', 'body_refresh_limit')}}
    try:
        with collection_lock(db_path):
            initialize(db_path)
            conn = connect(db_path)
            try:
                with conn:
                    conn.execute('''UPDATE collection_runs SET status='interrupted',finished_at=?,
                        error='previous collector exited before finalising' WHERE status='running' ''', (utc_now(),))
                    cursor = conn.execute('INSERT INTO collection_runs(started_at,status,metrics_json) VALUES (?,?,?)',
                                          (utc_now(), 'running', '{}'))
                    result['run_id'] = cursor.lastrowid
                transport = ReadOnlyTransport(conn, **{key: cfg[key] for key in
                    ('timeout', 'max_retries', 'request_budget', 'deadline_seconds', 'max_wait_seconds')})
                seen = set()
                budget_exhausted = False
                early_rows, early_bodies = [], []
                refresh_cfg = cfg
                if cfg['counter_limit'] and cfg['counter_window_share']:
                    window_state = _counter_windows(conn, cfg['counter_limit'])
                    if window_state[0]:
                        # Endpoints have deadlines. Take their reserved share
                        # before slow feed backfills, keeping one request for
                        # a live feed whenever the global budget allows it.
                        available = min(cfg['counter_limit'], max(1, cfg['request_budget'] - 1))
                        early_cfg = {**cfg, 'counter_limit': available}
                        planned = _counter_schedule(conn, early_cfg, window_state=window_state)
                        reserved = max(1, int(available * cfg['counter_window_share'] + .5))
                        if available > 1 and cfg['counter_new_share']:
                            reserved = min(available - 1, reserved)
                        early_rows = [row for row in planned if row.get('window_minutes')][:reserved]
                        if available == 1:
                            early_rows = planned  # Persisted new/window turn-taking.
                        try:
                            early_bodies = _sample_counters(conn, transport, early_cfg, result,
                                                           seen, early_rows, window_state[1])
                        except BudgetExceeded as exc:
                            _error(result, 'counter_windows', exc)
                            budget_exhausted = True
                        remaining = cfg['counter_limit'] - len(early_rows)
                        fresh_saved = sum(row['sampling_queue'] == 'new' for row in early_rows)
                        target_fresh = max(0, int(cfg['counter_limit'] * cfg['counter_new_share'] + .5) - fresh_saved)
                        if cfg['counter_limit'] >= 3 and remaining >= 2 and target_fresh >= remaining:
                            history = _standard_counter_schedule(conn,
                                {**cfg, 'counter_limit': 1, 'counter_new_share': 0},
                                exclude=[row['post_id'] for row in window_state[0]])
                            if history and history[0]['sampling_queue'] == 'tracked':
                                target_fresh = remaining - 1
                        refresh_cfg = {**cfg, 'counter_limit': remaining, 'counter_window_share': 0,
                                       'counter_new_share': min(1, target_fresh / remaining) if remaining else 0}
                request_reserve = min(refresh_cfg['counter_limit'], max(0, cfg['request_budget'] - transport.attempts - 1))
                feed_cfg = {**cfg, '_counter_request_reserve': request_reserve,
                            '_counter_time_reserve': min(cfg['timeout'], cfg['deadline_seconds'] * .25)
                            if refresh_cfg['counter_limit'] else 0}
                sources = [] if budget_exhausted else (['latest', 'trending'] if cfg['collect_trending'] else ['latest'])
                for source in sources:
                    try:
                        if source == 'latest' and cfg['latest_incremental_pages']:
                            # Follow the new-post gap independently of the long historical backlog.
                            live_key = 'feed:latest:live'
                            if not _checkpoint(conn, live_key):
                                history = _checkpoint(conn, 'feed:latest')
                                boundary = history.get('cycle_head') or history.get('watermark')
                                with conn:
                                    _set_checkpoint(conn, live_key, {'watermark': boundary}, utc_now())
                            live_cfg = {**feed_cfg, 'max_pages': cfg['latest_incremental_pages']}
                            _feed(conn, transport, source, live_cfg, result, seen, live_key, refresh_head=True)
                            live_coverage = dict(result['feeds'][source])
                            history = _checkpoint(conn, 'feed:latest')
                            if history.get('reached_end') and not history.get('cursor'):
                                result['feeds'][source] = {'pages': 0, 'cycle_complete': True, 'history_complete': True}
                            else:
                                _feed(conn, transport, source, feed_cfg, result, seen, refresh_head=False)
                            result['feeds'][source]['incremental'] = live_coverage
                        else:
                            source_cfg = {**feed_cfg, 'max_pages': cfg['trending_max_pages'] or cfg['max_pages']} if source == 'trending' else feed_cfg
                            _feed(conn, transport, source, source_cfg, result, seen)
                    except BudgetExceeded as exc:
                        _error(result, source, exc)
                        # A feed bucket's Retry-After may exceed its wait
                        # allowance while counters still have request/time
                        # budget in their own bucket. Let that stage try.
                        budget_exhausted = (transport.attempts >= transport.request_budget or
                            (hasattr(transport, 'deadline') and hasattr(transport, 'clock')
                             and transport.clock() >= transport.deadline))
                        break
                    except Exception as exc:
                        _error(result, source, exc)
                result['posts'] = len(seen)
                if not budget_exhausted:
                    try:
                        _refresh(conn, transport, refresh_cfg, result, seen,
                                 exclude=[row['post_id'] for row in early_rows], body_refresh=early_bodies)
                    except BudgetExceeded as exc:
                        _error(result, 'refresh', exc)
                        budget_exhausted = True
                if cfg['collect_news'] and not budget_exhausted:
                    try:
                        _news(conn, transport, cfg, result)
                    except Exception as exc:
                        _error(result, 'news', exc)
                if not budget_exhausted:
                    try:
                        from .social_topics import collect_social_context
                        collect_social_context(conn, transport, cfg, result)
                    except Exception as exc:
                        _error(result, 'social_context', exc)
                    seen.update(result.pop('post_ids', set()))
                    result['posts'] = len(seen)
                result['status'] = 'partial' if result['errors'] else 'completed'
                result['requests'] = transport.attempts
                result['waited_seconds'] = transport.waited
            except BaseException as exc:
                conn.rollback()
                result['status'] = 'interrupted' if isinstance(exc, (KeyboardInterrupt, SystemExit)) else 'failed'
                _error(result, 'collection', exc)
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
            finally:
                if result['run_id'] is not None:
                    with conn:
                        conn.execute('''UPDATE collection_runs SET finished_at=?,status=?,metrics_json=?,error=? WHERE id=?''',
                            (utc_now(), result['status'], json.dumps(result, ensure_ascii=False),
                             json.dumps(result['errors'], ensure_ascii=False) if result['errors'] else None,
                             result['run_id']))
                conn.close()
    except CollectionBusy as exc:
        result['status'] = 'busy'
        _error(result, 'lock', exc)
    return result
