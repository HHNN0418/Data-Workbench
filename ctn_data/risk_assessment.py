"""Risk assessment: which posts the platform has hidden, for every account.

The platform's recent feed (GET /api/posts?sort=recent) lists every public
post, reply and repost newest first, up to 200 a page, and leaves hidden posts
out. Its cursor is base64 JSON {"t": created_at, "i": post_id}, so any stretch
of time can be read directly. Each scan reads one stretch, compares it with the
posts we collected for that stretch, and asks the platform only about the
posts the feed no longer lists. Checked on 24 Oct: 764 requests read 48 h
(152,794 posts), and every post missing from the feed answered 404, including
all 20 of our roles' known hidden posts.

A first 404 is not yet a hidden post: five teams' reports hide a post until the
moderator rules, about 11 minutes after the first report (3 h halved per extra
team). So the first 404 makes the post a suspect, and only a 404 again at least
CONFIRM_MINUTES later confirms it (the user, 24 Oct: 「第一次看到 404 后，20分钟后
还是就被确认」; 10 minutes since the shadow checks, 「改吧」). Confirmed posts go into hidden_posts, where
hidden_posts.risk_accounts counts our roles' risk. Public data cannot tell a
moderator's hide from the author deleting the post; posts our own roles
deleted are skipped.

Schedule, in one background thread: due shadow checks and re-checks first,
then the newest SEGMENT_HOURS every FAST_INTERVAL_MINUTES, then the older
segments back to FULL_HOURS once every FULL_INTERVAL_MINUTES. During a long
scan the due checks are looked at every DUE_CHECK_SECONDS between its requests.
Shadow checks (SHADOW_CHECKS) ask a new suspect again 5 minutes after its first
404, and a confirmed post again at 20 minutes, and only record the answer
(post_visibility_shadow, shadow_summary). Anonymous public GETs only; nothing
is posted or reported.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from . import copy_patterns
from .db import begin_write, connect, utc_now

LOGGER = logging.getLogger(__name__)

# Phase 2 (the user, 24 Oct: 「改吧」): 10 minutes, down from 20, after 56 suspects shadow-checked at 5 and
# 10 minutes all stayed 404 and every one confirmed at 20 stayed hidden.
CONFIRM_MINUTES = 10
# Shadow re-checks (the user, 24 Oct: 「先做影子复核（5 分钟和 10 分钟各多查一次，只记录）」): a suspect is
# asked once more about each of these many minutes after its first 404 and the answer is only recorded in
# post_visibility_shadow, never acted on. Since phase 2 the 10-minute check is the confirmation itself, so
# the second shadow check moved to 20 minutes, after confirmation: a post served again then is one the
# 10-minute rule confirmed wrongly. A shadow check more than SHADOW_LATE_MINUTES late is skipped.
SHADOW_CHECKS = (('5m', 5), ('20m', 20))
SHADOW_LATE_MINUTES = 10
# Due re-checks and shadow checks are looked at this often, also between the requests of a long scan.
DUE_CHECK_SECONDS = 60
ANSWERED = (200, 404, 410)
SEGMENT_HOURS = 6
FAST_INTERVAL_MINUTES = 20
FULL_HOURS = 48
FULL_INTERVAL_MINUTES = 180
PAGE_LIMIT = 200
# Collection can trail the feed by a minute or two.
FEED_LAG_MINUTES = 5
# A post the feed leaves out but the platform still serves is asked again after this.
VISIBLE_RECHECK_HOURS = 6
# A suspect whose check got no answer (timeout, 429, 5xx) is asked again after this.
RETRY_MINUTES = 5
MAX_PAGES = 400
PAUSE_SECONDS = 0.6
# Slow down when the platform's per-minute allowance runs low.
LOW_RATE_REMAINING = 50
LOW_RATE_PAUSE_SECONDS = 5
GONE = (404, 410)
WRITE_SCOPE = 'post_visibility'
STATE_KEY = 'risk_assessment_state'
# Account report rule: one moderator-hidden post and 100 actions on the platform.
ACCOUNT_REPORT_MIN_HIDDEN = 1
ACCOUNT_REPORT_MIN_ACTIONS = 100
# An account that posted this recently is not suspended.
ACTIVE_HOURS = 6
EXCERPT_CHARS = 120


def _key(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _time(text):
    return datetime.fromisoformat(str(text).replace('Z', '+00:00'))


def _now(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    return _time(value) if isinstance(value, str) else value


def cursor(at, post_id=2**31 - 1):
    """The recent feed's cursor for posts created before ``at`` (a datetime or timestamp key)."""
    stamp = at if isinstance(at, str) else _key(at)
    raw = json.dumps({'t': stamp, 'i': post_id}, separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def _status(error):
    return getattr(error, 'status', None) or getattr(error, 'status_code', None)


def _pause(client, pause):
    limit = getattr(client, 'rate_limit', None)
    remaining = getattr(limit, 'remaining', None)
    time.sleep(LOW_RATE_PAUSE_SECONDS if remaining is not None and remaining < LOW_RATE_REMAINING else pause)


def feed_ids(client, start, end, *, pause=PAUSE_SECONDS, between=None):
    """Post ids the recent feed lists with ``start <= created_at < end``, and the pages read.

    ``between`` is called after each page (RiskMonitor runs due checks there).
    """
    listed, pages, next_cursor = set(), 0, cursor(end)
    while next_cursor and pages < MAX_PAGES:
        page = client.feed_latest(limit=PAGE_LIMIT, cursor=next_cursor)
        pages += 1
        stamps = [str(item.get('created_at') or '') for item in page if isinstance(item, dict)]
        listed.update(item['post_id'] for item in page
                      if isinstance(item, dict) and start <= str(item.get('created_at') or '') < end)
        next_cursor = page.next_cursor
        if not page or min(stamps, default='') < start:
            break
        _pause(client, pause)
        if between is not None:
            between()
    return listed, pages


def check(client, post_id):
    """The platform's HTTP answer for one post: 200, an error status, or None when there was none."""
    try:
        client.get_post(post_id)
    except Exception as exc:
        return _status(exc)
    return 200


def own_deleted(conn):
    """Posts our roles deleted themselves; their 404 says nothing about moderation."""
    found = set()
    for row in conn.execute("SELECT details_json FROM agent_audit_events WHERE kind='published' "
                            "AND details_json LIKE '%delete_post%'"):
        try:
            details = json.loads(row[0] or '{}')
        except (TypeError, ValueError):
            continue
        if details.get('action') != 'delete_post' or details.get('already_applied'):
            continue
        target = details.get('target_post_id', details.get('post_id'))
        if type(target) is int:
            found.add(target)
    return found


def observe(conn, post_id, account_id, created_at, http_status, at=None, *, skip=None):
    """Record one platform answer for a post; the caller owns the transaction.

    Returns the post's state afterwards: 'suspect' after a first 404, 'hidden'
    once a 404 comes again CONFIRM_MINUTES or more after the first (the post
    then goes into hidden_posts), 'visible' when the platform serves it (a
    restored post leaves hidden_posts), 'skipped' for a post our own roles
    deleted, and otherwise the state it already had ('unknown' if none).
    ``skip`` is the set of our own deleted posts, when the caller has it.
    """
    at = at or utc_now()
    if post_id in (own_deleted(conn) if skip is None else skip):
        return 'skipped'
    begin_write(conn, WRITE_SCOPE)
    row = conn.execute('SELECT status,first_404_at FROM post_visibility WHERE post_id=?', (post_id,)).fetchone()
    state = row[0] if row else None
    if http_status in GONE:
        if state == 'suspect' and _time(at) - _time(row[1]) >= timedelta(minutes=CONFIRM_MINUTES):
            conn.execute('UPDATE post_visibility SET status=?,confirmed_at=?,last_checked_at=?,last_http=?,'
                         'checks=checks+1 WHERE post_id=?', ('hidden', at, at, http_status, post_id))
            conn.execute('INSERT INTO hidden_posts(post_id,account_id,created_at,detected_at) VALUES(?,?,?,?) '
                         'ON CONFLICT(post_id) DO NOTHING', (post_id, account_id, created_at, at))
            return 'hidden'
        if state in ('suspect', 'hidden'):
            conn.execute('UPDATE post_visibility SET last_checked_at=?,last_http=?,checks=checks+1 WHERE post_id=?',
                         (at, http_status, post_id))
            return state
        _put(conn, post_id, account_id, created_at, 'suspect', at, http_status, first_404_at=at)
        return 'suspect'
    if http_status == 200:
        if state == 'hidden':
            conn.execute('DELETE FROM hidden_posts WHERE post_id=?', (post_id,))
        _put(conn, post_id, account_id, created_at, 'visible', at, http_status)
        return 'visible'
    if row:
        conn.execute('UPDATE post_visibility SET last_checked_at=?,last_http=?,checks=checks+1 WHERE post_id=?',
                     (at, http_status, post_id))
    return state or 'unknown'


def _put(conn, post_id, account_id, created_at, state, at, http_status, *, first_404_at=None, confirmed_at=None):
    conn.execute('''INSERT INTO post_visibility(post_id,account_id,created_at,status,first_404_at,confirmed_at,
        last_checked_at,last_http,checks) VALUES(?,?,?,?,?,?,?,?,1)
        ON CONFLICT(post_id) DO UPDATE SET status=excluded.status,first_404_at=excluded.first_404_at,
        confirmed_at=excluded.confirmed_at,last_checked_at=excluded.last_checked_at,
        last_http=excluded.last_http,checks=post_visibility.checks+1''',
                 (post_id, account_id, created_at, state, first_404_at, confirmed_at, at, http_status))


def due(conn, at=None, *, limit=500):
    """Suspects whose first 404 is at least CONFIRM_MINUTES old: (post_id, account_id, created_at).

    A suspect whose last check got no answer waits RETRY_MINUTES before the next.
    """
    now = _now(at)
    cutoff, retry = _key(now - timedelta(minutes=CONFIRM_MINUTES)), _key(now - timedelta(minutes=RETRY_MINUTES))
    return [tuple(row) for row in conn.execute(
        "SELECT post_id,account_id,created_at FROM post_visibility WHERE status='suspect' AND first_404_at<=? "
        'AND last_checked_at<=? ORDER BY first_404_at,post_id LIMIT ?', (cutoff, retry, limit))]


def _missing(conn, start, end, listed, at):
    """Collected public posts in [start, end) the feed did not list and that are not settled yet."""
    recheck_before = _key(at - timedelta(hours=VISIBLE_RECHECK_HOURS))
    rows = conn.execute('''SELECT p.post_id,p.author_id,p.created_at,p.visibility,v.status,v.last_checked_at
        FROM posts p LEFT JOIN post_visibility v ON v.post_id=p.post_id
        WHERE p.created_at>=? AND p.created_at<?''', (start, end)).fetchall()
    found, collected = [], 0
    for post_id, author_id, created_at, visibility, state, checked in rows:
        if str(visibility or 'public').lower() not in ('', 'public') or author_id is None:
            continue
        collected += 1
        if post_id in listed or state in ('suspect', 'hidden') or (state == 'visible' and (checked or '') > recheck_before):
            continue
        found.append((post_id, author_id, created_at))
    return found, collected


def _ask(db_path, client, posts, *, skip, pause, at=None, between=None):
    """GET each post and record the answer at ``at`` (else the time of each answer); counts by resulting state.

    ``between`` is called after each answer.
    """
    counts = {'requests': 0}
    stamp = None if at is None else _key(_now(at))
    conn = connect(db_path)
    try:
        for post_id, account_id, created_at in posts:
            if post_id in skip:
                counts['skipped'] = counts.get('skipped', 0) + 1
                continue
            answer = check(client, post_id)
            counts['requests'] += 1
            with conn:
                state = observe(conn, post_id, account_id, created_at, answer, stamp, skip=skip)
            counts[state] = counts.get(state, 0) + 1
            _pause(client, pause)
            if between is not None:
                between()
    finally:
        conn.close()
    return counts


def scan(db_path, client, start, end, *, at=None, pause=PAUSE_SECONDS, between=None):
    """Compare one stretch of the recent feed with collected posts and check what it left out.

    ``at`` fixes the scan's clock (tests); by default each answer is recorded when it arrives.
    ``between`` is called after each page and each answer.
    """
    when = _now(at)
    listed, pages = feed_ids(client, start, end, pause=pause, between=between)
    conn = connect(db_path)
    try:
        missing, collected = _missing(conn, start, end, listed, when)
        skip = own_deleted(conn)
    finally:
        conn.close()
    counts = _ask(db_path, client, missing, skip=skip, pause=pause, at=at, between=between)
    return {'start': start, 'end': end, 'at': _key(when), 'pages': pages, 'listed': len(listed),
            'collected': collected, 'missing': len(missing), **counts, 'requests': pages + counts['requests']}


def recheck(db_path, client, *, at=None, pause=PAUSE_SECONDS):
    """Ask again about every suspect whose first 404 is old enough to confirm."""
    conn = connect(db_path)
    try:
        posts, skip = due(conn, at), own_deleted(conn)
    finally:
        conn.close()
    return {'at': _key(_now(at)), 'due': len(posts), **_ask(db_path, client, posts, skip=skip, pause=pause, at=at)}


def shadow_due(conn, at=None, *, limit=200):
    """Suspects, and posts confirmed since, due a shadow check: (post_id, account_id, check_label, first_404_at).

    Due once SHADOW_CHECKS minutes have passed since the first 404 and no later than
    SHADOW_LATE_MINUTES after that; one answered check per label, an unanswered one
    is retried after RETRY_MINUTES.
    """
    now, found = _now(at), []
    retry = _key(now - timedelta(minutes=RETRY_MINUTES))
    for label, minutes in SHADOW_CHECKS:
        found += [tuple(row) for row in conn.execute(
            "SELECT v.post_id,v.account_id,?,v.first_404_at FROM post_visibility v "
            'LEFT JOIN post_visibility_shadow s ON s.post_id=v.post_id AND s.check_label=? '
            "WHERE v.status IN ('suspect','hidden') AND v.first_404_at<=? AND v.first_404_at>=? "
            'AND (s.post_id IS NULL OR (s.http_status NOT IN (200,404,410) OR s.http_status IS NULL) AND s.checked_at<=?) '
            'ORDER BY v.first_404_at,v.post_id LIMIT ?',
            (label, label, _key(now - timedelta(minutes=minutes)),
             _key(now - timedelta(minutes=minutes + SHADOW_LATE_MINUTES)), retry, limit))]
    order = {label: index for index, (label, _) in enumerate(SHADOW_CHECKS)}
    return sorted(found, key=lambda row: (row[3], row[0], order[row[2]]))[:limit]


def shadow(db_path, client, *, at=None, pause=PAUSE_SECONDS):
    """Ask once more about every suspect due a shadow check and record the answer only.

    post_visibility, hidden_posts and the confirmation rule are left as they are.
    """
    conn = connect(db_path)
    try:
        rows = shadow_due(conn, at)
    finally:
        conn.close()
    counts = {'requests': 0}
    conn = connect(db_path)
    try:
        for post_id, _account_id, label, first_404_at in rows:
            answer = check(client, post_id)
            counts['requests'] += 1
            stamp = utc_now() if at is None else _key(_now(at))
            minutes = round((_time(stamp) - _time(first_404_at)).total_seconds() / 60, 2)
            with conn:
                conn.execute('''INSERT INTO post_visibility_shadow(post_id,check_label,checked_at,http_status,
                    minutes_after_first_404,first_404_at) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(post_id,check_label) DO UPDATE SET checked_at=excluded.checked_at,
                    http_status=excluded.http_status,minutes_after_first_404=excluded.minutes_after_first_404''',
                             (post_id, label, stamp, answer, minutes, first_404_at))
            outcome = 'gone' if answer in GONE else 'back' if answer == 200 else 'unanswered'
            counts[f'{label}_{outcome}'] = counts.get(f'{label}_{outcome}', 0) + 1
            _pause(client, pause)
    finally:
        conn.close()
    return {'at': _key(_now(at)), 'due': len(rows), **counts}


def shadow_summary(conn):
    """What the shadow checks saw, per label, and what became of those posts. Read-only.

    For each label: answered checks, still 404 ('gone'), served again ('back'),
    unanswered, the median minutes after the first 404; and for the gone and back
    posts their state now (post_visibility: hidden once confirmed at
    CONFIRM_MINUTES, visible, or still suspect).
    """
    rows = conn.execute('SELECT s.post_id,s.check_label,s.http_status,s.minutes_after_first_404,v.status '
                        'FROM post_visibility_shadow s LEFT JOIN post_visibility v ON v.post_id=s.post_id').fetchall()
    labels = {}
    for label, _ in SHADOW_CHECKS:
        mine = [row for row in rows if row[1] == label]
        answered = [row for row in mine if row[2] in ANSWERED]
        minutes = sorted(float(row[3]) for row in answered if row[3] is not None)

        def outcome(group):
            found = {'hidden': 0, 'visible': 0, 'suspect': 0}
            for row in group:
                found[row[4] if row[4] in found else 'suspect'] += 1
            return found
        gone, back = [row for row in answered if row[2] in GONE], [row for row in answered if row[2] == 200]
        labels[label] = {'checked': len(answered), 'gone': len(gone), 'back': len(back),
                         'unanswered': len(mine) - len(answered),
                         'median_minutes': minutes[len(minutes) // 2] if minutes else None,
                         'gone_now': outcome(gone), 'back_now': outcome(back)}
    return {'posts': len({row[0] for row in rows}), 'confirm_minutes': CONFIRM_MINUTES,
            'checks': [{'label': label, 'minutes': minutes} for label, minutes in SHADOW_CHECKS], 'labels': labels}


def unadopted(conn, at=None, *, limit=500):
    """hidden_posts rows written on a first 404 alone, before this module, whose 404 is CONFIRM_MINUTES old."""
    cutoff = _key(_now(at) - timedelta(minutes=CONFIRM_MINUTES))
    return [tuple(row) for row in conn.execute(
        'SELECT h.post_id,h.account_id,h.created_at,h.detected_at FROM hidden_posts h '
        'LEFT JOIN post_visibility v ON v.post_id=h.post_id WHERE v.post_id IS NULL AND h.detected_at<=? '
        'ORDER BY h.post_id LIMIT ?', (cutoff, limit))]


def adopt(db_path, client, *, at=None, pause=PAUSE_SECONDS):
    """Apply the confirmation rule to unadopted hidden_posts rows, their record time being the first 404.

    A post that still answers 404 is confirmed; one the platform serves again
    leaves hidden_posts; any other answer leaves the row for the next round.
    """
    conn = connect(db_path)
    try:
        rows = unadopted(conn, at)
    finally:
        conn.close()
    counts = {'requests': 0}
    conn = connect(db_path)
    try:
        for post_id, account_id, created_at, detected_at in rows:
            answer = check(client, post_id)
            counts['requests'] += 1
            now = utc_now() if at is None else _key(_now(at))
            with conn:
                begin_write(conn, WRITE_SCOPE)
                if answer in GONE:
                    _put(conn, post_id, account_id, created_at, 'hidden', now, answer,
                         first_404_at=detected_at, confirmed_at=now)
                    state = 'hidden'
                elif answer == 200:
                    conn.execute('DELETE FROM hidden_posts WHERE post_id=?', (post_id,))
                    _put(conn, post_id, account_id, created_at, 'visible', now, answer)
                    state = 'visible'
                else:
                    # No answer: the row waits as a suspect and is re-checked like any other.
                    conn.execute('DELETE FROM hidden_posts WHERE post_id=?', (post_id,))
                    _put(conn, post_id, account_id, created_at, 'suspect', now, answer, first_404_at=detected_at)
                    state = 'suspect'
            counts[state] = counts.get(state, 0) + 1
            _pause(client, pause)
    finally:
        conn.close()
    return {'at': _key(_now(at)), 'rows': len(rows), **counts}


def _save_state(db_path, state):
    conn = connect(db_path)
    try:
        with conn:
            conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (STATE_KEY, json.dumps(state, ensure_ascii=False)))
    finally:
        conn.close()


def _load_state(conn):
    row = conn.execute('SELECT value FROM meta WHERE key=?', (STATE_KEY,)).fetchone()
    try:
        value = json.loads(row[0]) if row else {}
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


class RiskMonitor:
    """Lifecycle companion: one thread doing the next due unit of work at a time."""

    def __init__(self, db_path, *, client=None, first_run_seconds=120, idle_seconds=30, pause=PAUSE_SECONDS):
        self.db_path, self.first_run_seconds, self.idle_seconds, self.pause = db_path, first_run_seconds, idle_seconds, pause
        self._client = client
        self._stop = threading.Event()
        self._thread = None
        self._state = None
        self._last_due = float('-inf')
        # 文案识别: its own thread runs a child process every 30 minutes; the overview reads the cached result.
        self.copy_patterns = copy_patterns.CopyPatternMonitor(db_path)

    def client(self):
        if self._client is None:
            import capturethenarrative
            self._client = capturethenarrative.LegitSocial(timeout=20, max_retries=0)
        return self._client

    def state(self):
        if self._state is None:
            conn = connect(self.db_path)
            try:
                self._state = _load_state(conn)
            finally:
                conn.close()
        return self._state

    def step(self, now=None):
        """Do the next due unit of work; returns its name, or None when nothing is due.

        ``now`` fixes the clock (tests); by default answers are recorded when they arrive.
        """
        fixed, now, state = now, _now(now), self.state()
        conn = connect(self.db_path)
        try:
            legacy, shadows = bool(unadopted(conn, now, limit=1)), bool(shadow_due(conn, now, limit=1))
            pending = bool(due(conn, now, limit=1))
        finally:
            conn.close()
        between = (lambda: self._interleave(fixed))
        if legacy:
            unit, result = 'legacy', adopt(self.db_path, self.client(), at=fixed, pause=self.pause)
        elif shadows:
            unit, result = 'shadow', shadow(self.db_path, self.client(), at=fixed, pause=self.pause)
        elif pending:
            unit, result = 'recheck', recheck(self.db_path, self.client(), at=fixed, pause=self.pause)
        elif self._due(state.get('fast', {}).get('at'), now, FAST_INTERVAL_MINUTES):
            unit = 'fast'
            result = scan(self.db_path, self.client(), _key(now - timedelta(hours=SEGMENT_HOURS)),
                          _key(now - timedelta(minutes=FEED_LAG_MINUTES)), at=fixed, pause=self.pause, between=between)
        else:
            cycle = state.get('full') or {}
            if cycle.get('next_segment') is None and not self._due(cycle.get('started_at'), now, FULL_INTERVAL_MINUTES):
                return None
            if cycle.get('next_segment') is None:
                cycle = {'started_at': _key(now), 'next_segment': 1, 'segments': [], 'finished_at': None}
            index, anchor = cycle['next_segment'], _time(cycle['started_at'])
            unit = 'full'
            result = scan(self.db_path, self.client(), _key(anchor - timedelta(hours=SEGMENT_HOURS * (index + 1))),
                          _key(anchor - timedelta(hours=SEGMENT_HOURS * index)), at=fixed, pause=self.pause,
                          between=between)
            cycle['segments'] = [*cycle['segments'], result]
            last = index + 1 >= FULL_HOURS // SEGMENT_HOURS
            cycle.update(next_segment=None if last else index + 1, finished_at=_key(now) if last else None)
            state['full'] = cycle
        if unit != 'full':
            state[unit] = result
        state['last_unit'], state['last_at'], state['last_error'] = unit, _key(now), None
        _save_state(self.db_path, state)
        return unit

    def _interleave(self, fixed=None):
        """Inside a long scan, at most every DUE_CHECK_SECONDS: due shadow checks, then due re-checks.

        Keeps shadow checks near their minute and confirmations near CONFIRM_MINUTES
        while a scan of many pages or answers runs. The results go into the state
        the scan's step saves.
        """
        clock = time.monotonic()
        if clock - self._last_due < DUE_CHECK_SECONDS:
            return
        self._last_due = clock
        now, state = _now(fixed), self.state()
        conn = connect(self.db_path)
        try:
            shadows, pending = bool(shadow_due(conn, now, limit=1)), bool(due(conn, now, limit=1))
        finally:
            conn.close()
        if shadows:
            state['shadow'] = shadow(self.db_path, self.client(), at=fixed, pause=self.pause)
        if pending:
            state['recheck'] = recheck(self.db_path, self.client(), at=fixed, pause=self.pause)

    @staticmethod
    def _due(last, now, minutes):
        return not last or now - _time(last) >= timedelta(minutes=minutes)

    def _run(self):
        wait = self.first_run_seconds
        while not self._stop.wait(wait):
            try:
                wait = self.idle_seconds if self.step() is None else 1
            except Exception as exc:
                LOGGER.error('Risk assessment step failed (%s); retrying in a minute', type(exc).__name__)
                state = self.state()
                state['last_error'] = {'at': utc_now(), 'error': f'{type(exc).__name__}: {exc}'[:300]}
                try:
                    _save_state(self.db_path, state)
                except Exception:
                    pass
                wait = 60

    def start(self):
        self.copy_patterns.start()
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='ctn-risk-assessment', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.copy_patterns.stop()
        if self._thread is not None:
            self._thread.join(timeout=30)


def account_risk(conn, account_ids=None):
    """{account_id: {'hidden', 'suspect', 'last_confirmed_at'}} for accounts with a hidden or suspect post."""
    marks = ''
    params = ()
    if account_ids is not None:
        account_ids = [account_id for account_id in account_ids if type(account_id) is int]
        if not account_ids:
            return {}
        marks, params = ' AND account_id IN (' + ','.join('?' for _ in account_ids) + ')', tuple(account_ids)
    return {row[0]: {'hidden': int(row[1] or 0), 'suspect': int(row[2] or 0), 'last_confirmed_at': row[3]}
            for row in conn.execute(
                "SELECT account_id,SUM(CASE WHEN status='hidden' THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN status='suspect' THEN 1 ELSE 0 END),MAX(confirmed_at) FROM post_visibility "
                "WHERE status IN ('hidden','suspect')" + marks + ' GROUP BY account_id', params)}


def all_roles(conn):
    """{account_id: {'name', 'state'}} for every role bound to an account, suspended and disabled ones too.

    hidden_posts.roles keeps the roles in use, which is right for the execution
    stage's alerts; this page shows them all, so a suspended role (still listed
    in team_accounts) is never taken for a teammate's account. ``state`` is
    'suspended' (official log or the role row), 'disabled' (enabled false) or 'active'.
    """
    from .account_status import suspended
    from .hidden_posts import AGENTS_KEY
    row = conn.execute('SELECT value FROM meta WHERE key=?', (AGENTS_KEY,)).fetchone()
    try:
        agents = json.loads(row[0]) if row else {}
    except (TypeError, ValueError):
        agents = {}
    banned, found = suspended(conn), {}
    for agent in (agents.values() if isinstance(agents, dict) else ()):
        if not isinstance(agent, dict) or type(agent.get('account_id')) is not int:
            continue
        account_id = agent['account_id']
        state = ('suspended' if account_id in banned or agent.get('account_suspended')
                 else 'disabled' if agent.get('enabled') is False else 'active')
        found[account_id] = {'name': agent.get('name') or str(account_id), 'state': state}
    return found


def _chunks(values, size=500):
    values = list(values)
    for index in range(0, len(values), size):
        yield values[index:index + size]


def overview(db_path, now=None, *, limit=200):
    """GET /api/risk/overview: scan status, hidden posts and each account's standing. Read-only."""
    now = _now(now)
    from .hidden_posts import risk_accounts
    conn = connect(db_path)
    try:
        state = _load_state(conn)
        shadows = shadow_summary(conn)
        totals = {row[0]: row[1] for row in conn.execute('SELECT status,COUNT(*) FROM post_visibility GROUP BY status')}
        day_ago = _key(now - timedelta(hours=24))
        hidden_24h = conn.execute("SELECT COUNT(*) FROM post_visibility WHERE status='hidden' AND confirmed_at>=?",
                                  (day_ago,)).fetchone()[0]
        risk = account_risk(conn)
        ours = all_roles(conn)
        # Teammates are the team's accounts that no role of ours is bound to.
        team = {row[0] for row in conn.execute('SELECT account_id FROM team_accounts')} - set(ours)
        own_risks, _ = risk_accounts(conn)
        names, activity, bots = {}, {}, set()
        for batch in _chunks(risk):
            marks = ','.join('?' for _ in batch)
            names.update((row[0], row[1]) for row in conn.execute(
                f'SELECT account_id,username FROM accounts WHERE account_id IN ({marks})', batch))
            activity.update((row[0], (row[1], row[2])) for row in conn.execute(
                f'SELECT author_id,COUNT(*),MAX(created_at) FROM posts WHERE author_id IN ({marks}) GROUP BY author_id',
                batch))
            bots.update(row[0] for row in conn.execute(
                f'SELECT account_id FROM account_annotations WHERE is_bot=1 AND account_id IN ({marks})', batch))
        recent = [dict(zip(('post_id', 'account_id', 'created_at', 'status', 'first_404_at', 'confirmed_at', 'body'), row))
                  for row in conn.execute(
                      "SELECT v.post_id,v.account_id,v.created_at,v.status,v.first_404_at,v.confirmed_at,p.body "
                      "FROM post_visibility v LEFT JOIN posts p ON p.post_id=v.post_id "
                      "WHERE v.status IN ('hidden','suspect') "
                      'ORDER BY COALESCE(v.confirmed_at,v.first_404_at) DESC,v.post_id DESC LIMIT 50')]
    finally:
        conn.close()
    active_since = _key(now - timedelta(hours=ACTIVE_HOURS))
    groups = {'ours': [], 'teammates': [], 'others': []}
    for account_id, counts in risk.items():
        actions, last_post = activity.get(account_id, (0, None))
        row = {'account_id': account_id, 'username': names.get(account_id), **counts,
               'collected_actions': actions, 'last_post_at': last_post, 'is_bot': account_id in bots,
               'active': bool(last_post and last_post >= active_since)}
        if account_id in ours:
            groups['ours'].append({**row, 'name': ours[account_id]['name'], 'role_state': ours[account_id]['state']})
        elif account_id in team:
            groups['teammates'].append(row)
        else:
            # The account-report rule as far as public data shows it: collected posts, replies and
            # reposts are a lower bound on platform actions (likes are not collected).
            missing = []
            if counts['hidden'] < ACCOUNT_REPORT_MIN_HIDDEN:
                missing.append('hidden')
            if actions < ACCOUNT_REPORT_MIN_ACTIONS:
                missing.append('actions')
            groups['others'].append({**row, 'account_report': {'ready': not missing, 'missing': missing}})
    for rows in groups.values():
        rows.sort(key=lambda row: (-row['hidden'], -row['suspect'], row['account_id']))
    for item in recent:
        body = ' '.join(str(item.pop('body') or '').split())
        item['excerpt'] = body[:EXCERPT_CHARS - 1] + '…' if len(body) > EXCERPT_CHARS else body
        item['username'] = names.get(item['account_id'])
        item['group'] = 'ours' if item['account_id'] in ours else 'teammates' if item['account_id'] in team else 'others'
    others = groups['others']
    return {
        'generated_at': _key(now),
        'rules': {'confirm_minutes': CONFIRM_MINUTES, 'segment_hours': SEGMENT_HOURS,
                  'fast_interval_minutes': FAST_INTERVAL_MINUTES, 'full_hours': FULL_HOURS,
                  'full_interval_minutes': FULL_INTERVAL_MINUTES, 'page_limit': PAGE_LIMIT,
                  'account_report_min_hidden': ACCOUNT_REPORT_MIN_HIDDEN,
                  'account_report_min_actions': ACCOUNT_REPORT_MIN_ACTIONS, 'active_hours': ACTIVE_HOURS,
                  'shadow_checks': [minutes for _, minutes in SHADOW_CHECKS], 'due_check_seconds': DUE_CHECK_SECONDS},
        'monitor': {key: state.get(key) for key in ('fast', 'full', 'recheck', 'shadow', 'legacy', 'last_unit', 'last_at',
                                                    'last_error')},
        # Shadow re-checks at 5 and 10 minutes, recorded only (shadow_summary).
        'shadow': shadows,
        'totals': {'suspect': totals.get('suspect', 0), 'hidden': totals.get('hidden', 0),
                   'visible': totals.get('visible', 0), 'hidden_24h': hidden_24h,
                   'accounts_with_hidden': sum(1 for counts in risk.values() if counts['hidden']),
                   'others_report_ready': sum(1 for row in others if row['account_report']['ready'])},
        'own_risks': own_risks,
        'ours': groups['ours'], 'teammates': groups['teammates'],
        'others': others[:limit], 'others_total': len(others),
        'recent': recent,
        # Shared wording of every account's posts (copy_patterns.py), from its in-memory cache.
        'copy_patterns': copy_patterns.cached(db_path),
        'caveats': ['公开数据分不清版主隐藏和作者自己删帖；我方角色自己删的帖子已排除',
                    f'第一次 404 记为待复核，{CONFIRM_MINUTES} 分钟后再次 404 才确认为隐藏；五队举报的临时隐藏约 11 分钟内就会裁定',
                    '动作数是采集到的发帖、回复和转发，是平台动作数的下限（点赞没有采集）；平台账号接口不提供动作数',
                    f'{ACTIVE_HOURS} 小时内还在发帖的账号没有被封号；平台不公开封号状态'],
    }
