"""NPC calibration loop: official hourly reports against who actually engaged with us.

Each official red report counts the NPC replies, reposts, likes and follows our
team's content received in the hour before it. Our collector knows who replied
to and reposted our accounts in that hour, and (since D70) who liked our posts
and followed our accounts, so each count is a bag with a known number of NPCs
(learning from label proportions; reports/generated/npc-identification-research.md §1, §3).
Likes are listed only when the collector re-reads a post after its like
counter rises, and the counters of posts older than a few hours are rarely
sampled again, so an hour can miss likes: likes only ever prove that someone is
not an NPC. Follows prove either way once every team account's followers were
read after the hour.

  * an hour the official log scores 0 of a kind makes everyone who did it to us
    then (away from the hour's edges) certainly not an NPC;
  * an hour whose count equals the number of unlabelled actions of a complete
    kind makes them all NPCs, one whose known NPCs already fill the count makes
    the rest not NPCs, and labels propagate between hours and kinds until
    nothing changes;
  * the deployed citizen rule is scored on every clean hour (correlation, mean
    absolute error, exact hours, bias), next to each of its reasons left out
    and to candidate reasons not deployed yet, so a change is promoted only
    when the official hours say it is better.

Certain non-NPCs feed back into agent_targeting.non_citizen_reasons
('calibrated') and the Blue candidate queue ('official_non_npc'); new rulings
and new hours then add labels. Read-only: nothing here writes to the database.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import connect, timestamp_key

# Replies this close to a window edge may belong to the neighbouring report.
MARGIN_SECONDS = 180
# Lines of one report are written within a second or two of each other.
SAME_REPORT_SECONDS = 61
# A report sooner than this after the previous one covers part of an hour.
MIN_REPORT_GAP_MINUTES = 50
# Platform outages (UTC); hours overlapping them are not clean. The official
# platform was down 22 Oct 16:00-18:36 Sydney.
OUTAGES = ((datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc), datetime(2026, 10, 22, 7, 36, tzinfo=timezone.utc)),)
# An hour in which we collected less than this share of the median platform
# writes probably misses replies.
LOW_COLLECTION_RATIO = 0.5
RECENT_HOURS = 24
# The recommended rule reached MAE 0.11 on 44 hours; above these the rule is
# probably being evaded by a new bot family.
ALERT_MAE = 0.5
ALERT_BIAS = 4
CACHE_SECONDS = 600
PREVIEW_KEY = 'official_activity_preview_v1'
# Official counts and their collected counterparts; likes cannot prove an NPC (above).
SIGNALS = ('replies', 'reposts', 'likes', 'follows')
SIGNAL_LABELS = {'replies': '回复', 'reposts': '转发', 'likes': '点赞', 'follows': '关注'}
# Candidate reasons, scored next to the deployed rule but not deployed
# (research §8.2 and §5 P2).
SINGLE_ISSUE_MIN_POSTS = 5
SINGLE_ISSUE_MAX_OFF_TOPIC = 0.10
RATE_CAP_PER_DAY = 12
RATE_CAP_MAX_DAY = 16
PRE_COMPETITION_MAX_ID = 2343
PRE_COMPETITION_WINDOW_HOURS = 48
_ITEM = re.compile(r',\s*|\s+and\s+(?=\d)')
_cache_lock = threading.Lock()
_cache = {}


def _dt(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    key = timestamp_key(str(value))
    if key is None:
        raise ValueError(f'invalid timestamp {value!r}')
    return datetime.fromisoformat(key.replace('Z', '+00:00'))


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def engagement(message):
    """Counter of views, likes, reposts, replies, follows and mind from one red report line."""
    found = Counter()
    lowered = (message or '').lower()
    if 'npc engagement' not in lowered:
        return found
    body = re.split(r'npc engagement[^:]*:', message, maxsplit=1, flags=re.I)[-1].strip().rstrip('.')
    for item in filter(None, (part.strip() for part in _ITEM.split(body))):
        match = re.match(r'(\d+)\s+(.+)', item)
        if not match:
            continue
        count, rest = int(match.group(1)), match.group(2).lower()
        if 'inauthentic' in rest or 'report' in rest:
            continue
        if 'contribution' in rest or 'change' in rest:
            found['mind'] += count
        elif rest.startswith('view'):
            found['views'] += count
        elif rest.startswith('like'):
            found['likes'] += count
        elif rest.startswith('repost'):
            found['reposts'] += count
        elif rest.startswith('repl'):
            found['replies'] += count
        elif rest.startswith('follow'):
            found['follows'] += count
    return found


def preview_rows(conn):
    """Rows the extension read from the live activity table (ahead of the CSV export)."""
    row = conn.execute('SELECT value FROM meta WHERE key=?', (PREVIEW_KEY,)).fetchone()
    if not row:
        return []
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError):
        return []
    return [item for item in value.get('rows', []) if isinstance(item, dict)]


def red_reports(conn):
    """[(end, Counter)] per official red report, oldest first; live-table rows fill in after the export."""
    from .imports import parse_activity
    lines = []
    latest = None
    for occurred_at, message in conn.execute(
            "SELECT occurred_at,message FROM official_activity WHERE track='red' "
            "AND event_type='SCORE_NARRATIVE' AND occurred_at IS NOT NULL"):
        stamp = _dt(occurred_at)
        lines.append((stamp, message))
        latest = stamp if latest is None or stamp > latest else latest
    for item in preview_rows(conn):
        try:
            stamp = _dt(item['occurred_at'])
        except (KeyError, TypeError, ValueError):
            continue
        if (latest is None or stamp > latest + timedelta(seconds=SAME_REPORT_SECONDS)) and \
                item.get('event_type') == 'SCORE_NARRATIVE' and \
                parse_activity(item['event_type'], item.get('message', ''))['track'] == 'red':
            lines.append((stamp, item.get('message', '')))
    lines.sort(key=lambda line: line[0])
    reports = []
    for stamp, message in lines:
        if reports and (stamp - reports[-1][0]).total_seconds() <= SAME_REPORT_SECONDS:
            reports[-1][1].update(engagement(message))
        else:
            reports.append((stamp, engagement(message)))
    return reports


def team_accounts(conn):
    from .blue_reports import _own_accounts
    return frozenset(_own_accounts(conn))


def _hours(conn, reports):
    hours = []
    previous = None
    for end, counts in reports:
        start = end - timedelta(hours=1)
        flag = 'clean'
        if previous is not None and (end - previous).total_seconds() < MIN_REPORT_GAP_MINUTES * 60:
            flag = 'irregular'
        for down, up in OUTAGES:
            if start < up and end > down:
                flag = 'outage'
        writes = conn.execute('SELECT COUNT(*) FROM posts WHERE ctn_timestamp(created_at)>? AND ctn_timestamp(created_at)<=?',
                              (_iso_key(start), _iso_key(end))).fetchone()[0]
        hours.append({'start': start, 'end': end, 'official': counts, 'flag': flag, 'platform_writes': int(writes or 0)})
        previous = end
    typical = sorted(hour['platform_writes'] for hour in hours if hour['flag'] == 'clean')
    if typical:
        median = typical[len(typical) // 2]
        for hour in hours:
            if hour['flag'] == 'clean' and hour['platform_writes'] < LOW_COLLECTION_RATIO * median:
                hour['flag'] = 'low_collection'
    return hours


def _iso_key(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _replies_to(conn, team, since, until):
    """Replies to our team's content by other authors, (post_id, author_id, created_at)."""
    if not team:
        return []
    marks = ','.join('?' for _ in team)
    rows = conn.execute(
        'SELECT p.post_id,p.author_id,p.created_at FROM posts p JOIN posts parent ON parent.post_id=p.reply_to_post_id '
        f'WHERE parent.author_id IN ({marks}) AND p.author_id IS NOT NULL AND p.author_id NOT IN ({marks}) '
        'AND p.repost_of_post_id IS NULL AND ctn_timestamp(p.created_at)>? AND ctn_timestamp(p.created_at)<=?',
        (*team, *team, _iso_key(since), _iso_key(until))).fetchall()
    return [(row[0], row[1], _dt(row[2])) for row in rows if row[2]]


def _reposts_of(conn, team, since, until):
    """Reposts of our team's content by other authors, (post_id, author_id, created_at)."""
    if not team:
        return []
    marks = ','.join('?' for _ in team)
    rows = conn.execute(
        'SELECT p.post_id,p.author_id,p.created_at FROM posts p JOIN posts original ON original.post_id=p.repost_of_post_id '
        f'WHERE original.author_id IN ({marks}) AND p.author_id IS NOT NULL AND p.author_id NOT IN ({marks}) '
        'AND ctn_timestamp(p.created_at)>? AND ctn_timestamp(p.created_at)<=?',
        (*team, *team, _iso_key(since), _iso_key(until))).fetchall()
    return [(row[0], row[1], _dt(row[2])) for row in rows if row[2]]


def _likes_of(conn, team, since, until):
    """Likes of our team's posts by other accounts, (post_id, account_id, liked_at)."""
    if not team:
        return []
    marks = ','.join('?' for _ in team)
    rows = conn.execute(
        'SELECT l.post_id,l.account_id,l.liked_at FROM post_likes l JOIN posts p ON p.post_id=l.post_id '
        f'WHERE p.author_id IN ({marks}) AND l.account_id NOT IN ({marks}) AND l.liked_at IS NOT NULL '
        'AND ctn_timestamp(l.liked_at)>? AND ctn_timestamp(l.liked_at)<=?',
        (*team, *team, _iso_key(since), _iso_key(until))).fetchall()
    return [(row[0], row[1], _dt(row[2])) for row in rows]


def _follows_of(conn, team, since, until):
    """Follows of our team's accounts by other accounts, (account_id, follower_id, followed_at)."""
    if not team:
        return []
    marks = ','.join('?' for _ in team)
    rows = conn.execute(
        f'SELECT account_id,follower_id,followed_at FROM account_followers WHERE account_id IN ({marks}) '
        f'AND follower_id NOT IN ({marks}) AND followed_at IS NOT NULL '
        'AND ctn_timestamp(followed_at)>? AND ctn_timestamp(followed_at)<=?',
        (*team, *team, _iso_key(since), _iso_key(until))).fetchall()
    return [(row[0], row[1], _dt(row[2])) for row in rows]


def _follows_read_until(conn, team):
    """Followers are complete up to the oldest latest read of any team account; None before all were read.

    An account whose followers the platform refuses (suspended or private) is skipped.
    """
    oldest = None
    for account in team:
        row = conn.execute('SELECT value FROM checkpoints WHERE key=?', ('followers:' + str(account),)).fetchone()
        try:
            state = json.loads(row[0]) if row else {}
        except (TypeError, ValueError):
            state = {}
        read = state.get('read_at_epoch') if isinstance(state, dict) else None
        if not read and isinstance(state, dict) and state.get('status') == 'unavailable':
            continue
        if not read:
            return None
        oldest = read if oldest is None else min(oldest, read)
    return datetime.fromtimestamp(oldest, timezone.utc) if oldest is not None else None


def bags(conn):
    """Official hours with what we collected of each kind; the base of every metric."""
    reports = red_reports(conn)
    if not reports:
        return [], frozenset()
    team = team_accounts(conn)
    hours = _hours(conn, reports)
    since, until = hours[0]['start'], hours[-1]['end']
    collected = {'replies': _replies_to(conn, team, since, until), 'reposts': _reposts_of(conn, team, since, until),
                 'likes': _likes_of(conn, team, since, until), 'follows': _follows_of(conn, team, since, until)}
    follows_until = _follows_read_until(conn, team)
    for hour in hours:
        for kind, actions in collected.items():
            hour[kind] = [(target, actor, at) for target, actor, at in actions if hour['start'] < at <= hour['end']]
        hour['complete'] = {'replies': True, 'reposts': True, 'likes': False,
                            'follows': follows_until is not None and hour['end'] <= follows_until}
    return hours, team


def hard_labels(hours, sources=None):
    """{author: (label, hour end)}: 0 certainly not an NPC, 1 certainly an NPC; plus contradictions.

    ``sources`` (a dict), when given, receives the kind of action each label came from.
    """
    clean = [hour for hour in hours if hour['flag'] == 'clean']
    labels = {}
    sources = {} if sources is None else sources
    margin = timedelta(seconds=MARGIN_SECONDS)

    def inside(hour, at):
        return hour['start'] + margin < at <= hour['end'] - margin

    def label(author, value, hour, kind):
        labels[author] = (value, hour['end'])
        sources[author] = kind

    for hour in clean:
        for kind in SIGNALS:
            if hour['official'][kind] == 0:
                for _, author, at in hour.get(kind, ()):
                    if inside(hour, at) and author not in labels:
                        label(author, 0, hour, kind)
    # Propagate only through hours with no action of that kind near an edge.
    exact = [(hour, kind) for hour in clean for kind in SIGNALS
             if kind in hour and all(inside(hour, at) for _, _, at in hour[kind])]
    changed = True
    while changed:
        changed = False
        for hour, kind in exact:
            authors = [author for _, author, _ in hour[kind]]
            known = sum(1 for author in authors if labels.get(author, (None,))[0] == 1)
            unknown = [author for author in authors if author not in labels]
            if not unknown:
                continue
            if known == hour['official'][kind]:
                for author in set(unknown):
                    label(author, 0, hour, kind)
                changed = True
            elif hour['complete'][kind] and known + len(unknown) == hour['official'][kind]:
                for author in set(unknown):
                    label(author, 1, hour, kind)
                changed = True
    contradictions = []
    for hour, kind in exact:
        authors = [author for _, author, _ in hour[kind]]
        positive = sum(1 for author in authors if labels.get(author, (None,))[0] == 1)
        unknown = sum(1 for author in authors if author not in labels)
        if positive > hour['official'][kind] or (hour['complete'][kind] and positive + unknown < hour['official'][kind]):
            contradictions.append({'end': _iso(hour['end']), 'kind': kind, 'official': hour['official'][kind],
                                   'labelled_npc': positive, 'unlabelled': unknown})
    return labels, contradictions


def _source_key(source):
    return str(source) if isinstance(source, (str, Path)) else None


def _certain(source):
    """(not NPCs, NPCs) the official hours prove (cached 10 min for a path); empty when it cannot judge."""
    key = _source_key(source)
    clock = time.monotonic()
    if key is not None:
        with _cache_lock:
            cached = _cache.get(('labels', key))
            if cached and cached[0] > clock:
                return cached[1]
    conn = connect(source) if key is not None else source
    try:
        hours, team = bags(conn)
        labels, _ = hard_labels(hours)
        found = tuple(frozenset(author for author, (label, _) in labels.items() if label == value and author not in team)
                      for value in (0, 1))
    except Exception:
        found = (frozenset(), frozenset())
    finally:
        if key is not None:
            conn.close()
    if key is not None:
        with _cache_lock:
            _cache[('labels', key)] = (clock + CACHE_SECONDS, found)
    return found


def certain_non_npcs(source):
    """Authors the official hours prove are not NPCs."""
    return _certain(source)[0]


def certain_npcs(source):
    """Accounts the official hours prove are NPCs (D70)."""
    return _certain(source)[1]


def metrics(predicted, official):
    """Correlation, mean absolute error, exact hours and bias of hourly counts."""
    n = len(predicted)
    if not n:
        return {'hours': 0}
    mean_p, mean_o = sum(predicted) / n, sum(official) / n
    spread_p = math.sqrt(sum((p - mean_p) ** 2 for p in predicted))
    spread_o = math.sqrt(sum((o - mean_o) ** 2 for o in official))
    corr = (sum((p - mean_p) * (o - mean_o) for p, o in zip(predicted, official)) / (spread_p * spread_o)
            if spread_p and spread_o else None)
    return {'hours': n, 'corr': None if corr is None else round(corr, 3),
            'mae': round(sum(abs(p - o) for p, o in zip(predicted, official)) / n, 3),
            'exact_hours': sum(1 for p, o in zip(predicted, official) if p == o),
            'predicted_total': sum(predicted), 'official_total': sum(official), 'bias': sum(predicted) - sum(official),
            'over_hours': sum(1 for p, o in zip(predicted, official) if p > o),
            'under_hours': sum(1 for p, o in zip(predicted, official) if p < o)}


def _single_issue(conn, authors):
    found = set()
    for chunk in _chunks(authors):
        marks = ','.join('?' for _ in chunk)
        for author, total, off in conn.execute(
                "SELECT author_id,COUNT(*),SUM(CASE WHEN stance='unrelated' THEN 1 ELSE 0 END) FROM post_annotations "
                f'WHERE author_id IN ({marks}) GROUP BY author_id', tuple(chunk)):
            total, off = int(total), int(off or 0)
            if total >= SINGLE_ISSUE_MIN_POSTS and off / total < SINGLE_ISSUE_MAX_OFF_TOPIC:
                found.add(author)
    return found


def _rate_capped(conn, authors, now):
    found = set()
    for chunk in _chunks(authors):
        marks = ','.join('?' for _ in chunk)
        days = defaultdict(Counter)
        first = {}
        for author, created in conn.execute(f'SELECT author_id,created_at FROM posts WHERE author_id IN ({marks})',
                                            tuple(chunk)):
            if not created:
                continue
            at = _dt(created)
            days[author][(at + timedelta(hours=11)).date()] += 1
            first[author] = min(first.get(author, at), at)
        for author, per_day in days.items():
            span = max((now - first[author]).total_seconds() / 86400, 0.25)
            if sum(per_day.values()) / span > RATE_CAP_PER_DAY or max(per_day.values()) > RATE_CAP_MAX_DAY:
                found.add(author)
    return found


def _chunks(values, size=500):
    values = sorted(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


LIVE_REASONS = ('heavy', 'repost_ring', 'verified', 'profile', 'style', 'batch_name_or_artefact')
CANDIDATE_REASONS = {'single_issue': '单议题账号（≥5 条帖子，无关话题 < 10%）',
                     'rate_cap': '长期速率（平均每天 > 12 条或单日 > 16 条）'}
REASON_LABELS = {'heavy': '高频', 'repost_ring': '转发圈', 'verified': '已认证', 'profile': '资料不像 NPC',
                 'style': '文风不像 NPC', 'batch_name_or_artefact': '批量用户名或 AI 套话'}


def snapshot(db_path, now=None):
    """Calibration of the citizen rule against every official hour; read-only."""
    from . import agent_targeting
    now = _dt(now) if now is not None else datetime.now(timezone.utc)
    conn = connect(db_path)
    try:
        hours, team = bags(conn)
        sources = {}
        labels, contradictions = hard_labels(hours, sources)
        authors = {author for hour in hours for _, author, _ in hour['replies']}
        pre_since = _iso_key(now - timedelta(hours=PRE_COMPETITION_WINDOW_HOURS))
        precomp = {row[0] for row in conn.execute(
            'SELECT DISTINCT author_id FROM posts WHERE author_id IS NOT NULL AND author_id<=? AND ctn_timestamp(created_at)>=?',
            (PRE_COMPETITION_MAX_ID, pre_since))} - set(team)
        extra = {'single_issue': _single_issue(conn, authors | precomp),
                 'rate_cap': _rate_capped(conn, authors | precomp, now)}
        usernames = {}
        for chunk in _chunks(authors | set(labels)):
            marks = ','.join('?' for _ in chunk)
            usernames.update((row[0], row[1]) for row in conn.execute(
                f'SELECT account_id,username FROM accounts WHERE account_id IN ({marks})', tuple(chunk)))
    finally:
        conn.close()
    reasons = agent_targeting.non_citizen_reasons(db_path, authors | precomp, now=now.timestamp())
    bots = agent_targeting.bot_accounts(db_path)
    live = set(LIVE_REASONS)
    excluded = {author: set(found) & live for author, found in reasons.items()}
    for name, members in extra.items():
        for author in members:
            excluded.setdefault(author, set()).add(name)

    def predicate(active):
        return lambda author: author not in bots and not (excluded.get(author, set()) & active)

    clean = [hour for hour in hours if hour['flag'] == 'clean']
    recent = clean[-RECENT_HOURS:]

    def score(rule, window):
        predicted = [sum(1 for _, author, _ in hour['replies'] if rule(author)) for hour in window]
        return metrics(predicted, [hour['official']['replies'] for hour in window])

    variants = [('live', '线上规则', live)]
    variants += [(f'without_{reason}', f'去掉「{REASON_LABELS[reason]}」', live - {reason}) for reason in LIVE_REASONS]
    variants += [(f'with_{name}', f'加上「{label}」', live | {name}) for name, label in CANDIDATE_REASONS.items()]
    scored = []
    for key, label, active in variants:
        rule = predicate(active)
        scored.append({'key': key, 'label': label, 'all': score(rule, clean), 'recent': score(rule, recent),
                       'precompetition_pass': round(sum(1 for a in precomp if rule(a)) / len(precomp), 4) if precomp else None,
                       'candidate': key.startswith('with_')})
    live_rule = predicate(live)
    live_scores = scored[0]
    better = [item for item in scored if item['candidate'] and _better(item, live_scores)]
    alert = None
    recent_live = live_scores['recent']
    if recent_live.get('hours', 0) >= 6 and (recent_live['mae'] > ALERT_MAE or abs(recent_live['bias']) > ALERT_BIAS):
        head = f"最近 {recent_live['hours']} 个干净小时：每小时误差 {recent_live['mae']}，偏差 {recent_live['bias']:+d}。"
        if recent_live['bias'] > 0:
            alert = head + '规则把别人的 bot 算成了市民，可能有新的 bot 家族绕过了规则；请看下方被误算为市民的回复者。'
        else:
            alert = head + ('规则少算了 NPC：可能有本队账号没登记（官方算了回复它们的 NPC，我们没有采集），'
                            '或者规则排除了真 NPC；请对比下方去掉单个理由后的结果。')
    hour_rows = []
    for hour in hours[-48:]:
        hour_rows.append({
            'start': _iso(hour['start']), 'end': _iso(hour['end']), 'flag': hour['flag'],
            'official_replies': hour['official']['replies'], 'official_views': hour['official']['views'],
            'official_likes': hour['official']['likes'], 'replies_to_us': len(hour['replies']),
            'official_reposts': hour['official']['reposts'], 'reposts_to_us': len(hour['reposts']),
            'likes_to_us': len(hour['likes']), 'official_follows': hour['official']['follows'],
            'follows_of_us': len(hour['follows']),
            'predicted_npc_replies': sum(1 for _, author, _ in hour['replies'] if live_rule(author)),
            'platform_writes': hour['platform_writes']})
    negatives = sorted(((author, end) for author, (label, end) in labels.items() if label == 0 and author not in team),
                       key=lambda item: item[1], reverse=True)
    positives = sorted(((author, end) for author, (label, end) in labels.items() if label == 1 and author not in team),
                       key=lambda item: item[1], reverse=True)
    by_kind = {kind: {'non_npc': sum(1 for author, _ in negatives if sources.get(author) == kind),
                      'npc': sum(1 for author, _ in positives if sources.get(author) == kind)} for kind in SIGNALS}
    return {
        'generated_at': _iso(now),
        'latest_report': _iso(hours[-1]['end']) if hours else None,
        'hours_total': len(hours), 'hours_clean': len(clean),
        'flags': dict(Counter(hour['flag'] for hour in hours)),
        'metrics': {'all': live_scores['all'], 'recent': live_scores['recent']},
        'alert': alert,
        'variants': scored,
        'promotable': [item['key'] for item in better],
        'labels': {
            'non_npc': len(negatives), 'npc': len(positives), 'contradictions': contradictions,
            'by_kind': by_kind,
            'newest_npc': [{'account_id': author, 'username': usernames.get(author), 'hour': _iso(end),
                            'kind': sources.get(author)} for author, end in positives[:30]],
            'misjudged_citizens': [{'account_id': author, 'username': usernames.get(author), 'hour': _iso(end)}
                                   for author, end in negatives if live_rule(author)][:30],
            'newest_non_npc': [{'account_id': author, 'username': usernames.get(author), 'hour': _iso(end),
                                'kind': sources.get(author)} for author, end in negatives[:30]],
        },
        'hours': hour_rows,
        'precompetition_authors': len(precomp),
        'method': [
            '官方红队小时报告给出每小时 NPC 回复、转发、点赞和关注我们的次数；采集到的回复者、转发者、'
            '点赞者和关注者是这一小时的候选人。',
            '某一类官方为 0 的小时里，离窗口边界 3 分钟以外做这件事的账号一定不是 NPC；'
            '再在各小时、各类之间传播：已知 NPC 数等于官方数时其余都不是 NPC，未知人数刚好补足官方数时都是 NPC。',
            '点赞名单可能漏掉老帖子上后来的赞，所以点赞只用来证明"不是 NPC"；关注要等所有账号的关注者都在这个小时之后读过一遍，'
            '才用来证明"是 NPC"。',
            '只用干净小时：去掉停机、间隔不足 50 分钟的报告和采集量不到中位数一半的小时。',
            '线上规则的分数不使用这些校准标签，避免自己考自己；候选规则只有在两个窗口都更好、'
            '赛前 NPC 通过率不降超过 0.5 个百分点时才标为可上线，上线仍需批准。',
        ],
    }


def _better(candidate, live):
    """A candidate beats the live rule on both windows without costing pre-competition NPCs."""
    for window in ('all', 'recent'):
        a, b = candidate[window], live[window]
        if not a.get('hours') or a['mae'] > b['mae'] or (a['mae'] == b['mae'] and a['exact_hours'] <= b['exact_hours']):
            return False
    pass_now, pass_live = candidate['precompetition_pass'], live['precompetition_pass']
    return pass_now is not None and pass_live is not None and pass_live - pass_now <= 0.005
