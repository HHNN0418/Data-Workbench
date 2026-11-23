"""Hourly voting intention of known NPCs and a Poll 2 forecast calibrated on Poll 1 (D74).

Known NPCs are the accounts created before any team registered (id <=
PRE_COMPETITION_MAX_ID). Every hour each of them who posted in the last 24
hours is labelled by Jev (yes / no / unsure, with probabilities) from up to six
recent posts. The window before Poll 1 is labelled once: dividing the official
Poll 1 shares by its probability-weighted shares gives one factor per class,
applied to the latest window. The range resamples the people in both windows
and the poll's own 1,000 respondents.

Measured on 23 Oct: three models (Jev, DeepSeek, Grok) agree on 79-86% of
people, but a sample of a few hundred can show the opposite trend, so every
active known NPC is labelled. Writes only npc_vote_labels and one meta key;
reads the platform through Jev only.
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import jev
from .db import connect, utc_now

LOGGER = logging.getLogger(__name__)

PRE_COMPETITION_MAX_ID = 2343
CHOICES = jev.VOTE_CHOICES
# Herald, 21 Oct: a representative poll of 1,000 citizens, scored at 20 Oct 20:11 UTC.
POLL1 = {'yes': 33.8, 'no': 36.2, 'unsure': 29.5}
POLL1_RESPONDENTS = 1000
BASELINE_WINDOW = ('2026-10-19T00:00:00Z', '2026-10-20T20:11:00Z')
WINDOW_HOURS = 24
POSTS_PER_NPC = 6
WORKERS = 8
SIMULATIONS = 2000
# 52-48 or closer is a close call (+3000 each side); a clear lead pays +6000.
CLOSE_CALL = (48.0, 52.0)
SNAPSHOT_KEY = 'vote_tracker_v1'
HISTORY_POINTS = 72


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _people(conn, since, until):
    """{known NPC: up to POSTS_PER_NPC newest post texts} for posts in [since, until)."""
    found = {}
    for author, body in conn.execute(
            'SELECT author_id,body FROM posts WHERE author_id<=? AND body IS NOT NULL AND repost_of_post_id IS NULL '
            'AND created_at>=? AND created_at<? ORDER BY created_at DESC', (PRE_COMPETITION_MAX_ID, since, until)):
        posts = found.setdefault(author, [])
        if len(posts) < POSTS_PER_NPC:
            posts.append(re.sub(r'\s+', ' ', body)[:280])
    return found


def _labelled(conn, window_end):
    return {row[0]: {'choice': row[1], 'p': {'yes': row[2], 'no': row[3], 'unsure': row[4]}} for row in conn.execute(
        'SELECT account_id,choice,p_yes,p_no,p_unsure FROM npc_vote_labels WHERE window_end=?', (window_end,))}


def label_window(db_path, provider, since, until, *, label=jev.vote_intention):
    """Label everyone active in [since, until) not yet labelled for this window; returns (labelled, failed)."""
    conn = connect(db_path)
    try:
        people = _people(conn, since, until)
        done = set(_labelled(conn, until))
    finally:
        conn.close()
    todo = [(account, posts) for account, posts in sorted(people.items()) if account not in done]

    def one(item):
        try:
            return item[0], label(provider, item[1])
        except Exception as exc:
            return item[0], exc
    results = []
    with ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(one, todo))
    rows = [(account, until, value['choice'], value['probabilities']['yes'], value['probabilities']['no'],
             value['probabilities']['unsure'], value.get('model'), utc_now())
            for account, value in results if isinstance(value, dict)]
    conn = connect(db_path)
    try:
        with conn:
            conn.executemany('INSERT INTO npc_vote_labels(account_id,window_end,choice,p_yes,p_no,p_unsure,model,'
                             'labelled_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(window_end,account_id) DO NOTHING', rows)
    finally:
        conn.close()
    return len(rows), len(results) - len(rows)


def _shares(rows, mode='probability'):
    if not rows:
        return {choice: 0.0 for choice in CHOICES}
    if mode == 'argmax':
        counts = Counter(row['choice'] for row in rows)
        return {choice: counts[choice] / len(rows) for choice in CHOICES}
    return {choice: sum(row['p'][choice] for row in rows) / len(rows) for choice in CHOICES}


def _calibrated(base, latest, official):
    adjusted = {choice: latest[choice] * official[choice] / base[choice] if base[choice] else 0.0 for choice in CHOICES}
    total = sum(adjusted.values()) or 1.0
    return {choice: adjusted[choice] / total for choice in CHOICES}


def _yes_of_decided(shares):
    decided = shares['yes'] + shares['no']
    return shares['yes'] * 100 / decided if decided else None


def forecast(base_rows, latest_rows, *, seed=0, simulations=SIMULATIONS):
    """Calibrated Poll 2 shares, a 90% range of Yes among decided voters and the three outcomes' chances."""
    official = {choice: value / sum(POLL1.values()) for choice, value in POLL1.items()}
    base, latest = list(base_rows), list(latest_rows)
    point = _calibrated(_shares(base), _shares(latest), official)
    rng = random.Random(seed)
    weights = [official[choice] for choice in CHOICES]
    sims = []
    for _ in range(simulations):
        poll = Counter(rng.choices(CHOICES, weights=weights, k=POLL1_RESPONDENTS))
        drawn = {choice: poll[choice] / POLL1_RESPONDENTS for choice in CHOICES}
        value = _yes_of_decided(_calibrated(_shares([rng.choice(base) for _ in base]),
                                            _shares([rng.choice(latest) for _ in latest]), drawn))
        if value is not None:
            sims.append(value)
    sims.sort()
    low, high = CLOSE_CALL
    at = lambda q: round(sims[min(len(sims) - 1, int(q * len(sims)))], 1) if sims else None
    return {'shares': {choice: round(point[choice] * 100, 1) for choice in CHOICES},
            'yes_of_decided': round(_yes_of_decided(point), 1) if _yes_of_decided(point) is not None else None,
            'range_90': [at(0.05), at(0.95)],
            'outcomes': {'yes_lead': round(sum(s > high for s in sims) / len(sims), 3) if sims else None,
                         'close_call': round(sum(low <= s <= high for s in sims) / len(sims), 3) if sims else None,
                         'no_lead': round(sum(s < low for s in sims) / len(sims), 3) if sims else None}}


def changes(base, latest):
    """How the people active in both windows moved: transition counts and net Yes / No."""
    both = sorted(set(base) & set(latest))
    moves = Counter((base[a]['choice'], latest[a]['choice']) for a in both)
    before = Counter(base[a]['choice'] for a in both)
    after = Counter(latest[a]['choice'] for a in both)
    return {'people': len(both), 'net': {choice: after[choice] - before[choice] for choice in CHOICES},
            'transitions': [{'from': a, 'to': b, 'count': n} for (a, b), n in moves.most_common()]}


def persuasion(conn, base, latest, since, until):
    """Same-person change of the known NPCs our team replied to in [since, until) against everyone else."""
    from .npc_calibration import team_accounts
    team = sorted(team_accounts(conn))
    replied = set()
    if team:
        marks = ','.join('?' for _ in team)
        replied = {row[0] for row in conn.execute(
            'SELECT DISTINCT p.author_id FROM posts r JOIN posts p ON p.post_id=r.reply_to_post_id '
            f'WHERE r.author_id IN ({marks}) AND p.author_id<=? AND r.created_at>=? AND r.created_at<?',
            (*team, PRE_COMPETITION_MAX_ID, since, until))}
    both = set(base) & set(latest)

    def group(members):
        net = {choice: sum((latest[a]['choice'] == choice) - (base[a]['choice'] == choice) for a in members)
               for choice in CHOICES}
        shift = {choice: round(net[choice] * 100 / len(members), 1) if members else None for choice in CHOICES}
        return {'people': len(members), 'net': net, 'shift_points': shift}
    return {'replied': group(both & replied), 'not_replied': group(both - replied)}


def _jev_provider(store):
    providers = [item for item in store.list_public().get('providers', [])
                 if item.get('protocol') == 'typesafe' and item.get('enabled')]
    return store.get(providers[0]['id']) if providers else None


def run_once(db_path, store, now=None, *, label=jev.vote_intention):
    """Label the baseline once and the latest completed hour, then store the snapshot."""
    now = now or datetime.now(timezone.utc)
    provider = _jev_provider(store)
    if provider is None:
        return _save(db_path, {'status': 'no_jev_connection', 'generated_at': utc_now()})
    end = now.replace(minute=0, second=0, microsecond=0)
    window = (_iso(end - timedelta(hours=WINDOW_HOURS)), _iso(end))
    work = {}
    for since, until in (BASELINE_WINDOW, window):
        work[until] = label_window(db_path, provider, since, until, label=label)
    conn = connect(db_path)
    try:
        base, latest = _labelled(conn, BASELINE_WINDOW[1]), _labelled(conn, window[1])
        previous = _load(conn)
        effect = persuasion(conn, base, latest, BASELINE_WINDOW[1], window[1])
    finally:
        conn.close()
    result = forecast(base.values(), latest.values()) if base and latest else None
    history = [point for point in (previous or {}).get('history', []) if point.get('window_end') != window[1]]
    if result:
        history.append({'window_end': window[1], 'yes_of_decided': result['yes_of_decided'],
                        'range_90': result['range_90'], 'people': len(latest)})
    snapshot = {'status': 'ok' if result else 'waiting', 'generated_at': utc_now(), 'model': provider.get('model'),
                'poll1': {'shares': POLL1, 'window': list(BASELINE_WINDOW), 'respondents': POLL1_RESPONDENTS},
                'baseline': {'people': len(base), 'probability': _pct(_shares(list(base.values()))),
                             'argmax': _pct(_shares(list(base.values()), 'argmax'))},
                'latest': {'window': list(window), 'people': len(latest), 'probability': _pct(_shares(list(latest.values()))),
                           'argmax': _pct(_shares(list(latest.values()), 'argmax'))},
                'forecast': result, 'changes': changes(base, latest), 'persuasion': effect,
                'labelled_now': {key: {'labelled': value[0], 'failed': value[1]} for key, value in work.items()},
                'history': history[-HISTORY_POINTS:],
                'method': ['已知 NPC = 比赛开始前创建的账号（id ≤ 2343）；每小时把最近 24 小时发过帖的每个人交给 Jev，'
                           '按最多 6 条帖子判断投票意向（Yes / No / 没决定），带概率。',
                           'Poll 1 之前（19 日 0 点到 20 日 20:11 UTC）只判一次，用官方 Poll 1 的比例给每一类一个校准系数，'
                           '乘到最新一小时的概率加权比例上。',
                           '区间同时重抽两段时间的人和 Poll 1 的 1,000 名受访者；只校准过一次民调，Poll 2 公布后要重新校准。',
                           '我们看的是在发帖的 NPC，官方问的是全体市民。']}
    return _save(db_path, snapshot)


def _pct(shares):
    return {choice: round(value * 100, 1) for choice, value in shares.items()}


def _load(conn):
    row = conn.execute('SELECT value FROM meta WHERE key=?', (SNAPSHOT_KEY,)).fetchone()
    try:
        return json.loads(row[0]) if row else None
    except (TypeError, ValueError):
        return None


def _save(db_path, snapshot):
    conn = connect(db_path)
    try:
        with conn:
            conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (SNAPSHOT_KEY, json.dumps(snapshot, ensure_ascii=False)))
    finally:
        conn.close()
    return snapshot


def snapshot(db_path):
    """The latest stored snapshot (GET /api/polls/forecast); read-only."""
    conn = connect(db_path)
    try:
        return _load(conn) or {'status': 'not_run', 'generated_at': None}
    finally:
        conn.close()


class VoteTracker:
    """Lifecycle companion: one pass shortly after start, then every hour."""

    def __init__(self, db_path, store, *, first_delay_seconds=120, interval_seconds=3600):
        self.db_path, self.store = db_path, store
        self.first_delay_seconds, self.interval_seconds = first_delay_seconds, interval_seconds
        self._stop = threading.Event()
        self._thread = None

    def _run(self):
        delay = self.first_delay_seconds
        while not self._stop.wait(delay):
            started = time.monotonic()
            try:
                run_once(self.db_path, self.store)
            except Exception as exc:
                LOGGER.error('Vote tracking failed (%s); retrying next hour', type(exc).__name__)
            delay = max(60, self.interval_seconds - (time.monotonic() - started))

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='ctn-vote-tracker', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


_labels_cache = {}
_labels_lock = threading.Lock()
LABELS_CACHE_SECONDS = 300


def latest_labels(db_path):
    """{account: {'choice', 'p'}} from the newest labelled window (cached 5 min); empty before the first run."""
    key, clock = str(db_path), time.monotonic()
    with _labels_lock:
        cached = _labels_cache.get(key)
        if cached and cached[0] > clock:
            return cached[1]
    conn = connect(db_path)
    try:
        row = conn.execute('SELECT MAX(window_end) FROM npc_vote_labels WHERE window_end<>?', (BASELINE_WINDOW[1],)).fetchone()
        found = _labelled(conn, row[0]) if row and row[0] else {}
    except Exception:
        found = {}
    finally:
        conn.close()
    with _labels_lock:
        _labels_cache[key] = (clock + LABELS_CACHE_SECONDS, found)
    return found
