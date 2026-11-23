"""Read models for the results pages: exposure (GET /api/results/exposure) and
attribution (GET /api/results/attribution).

Exposure puts the official hourly NPC engagement reports next to what we
published in the same official hour, so a drop in views can be read against
our own output. The official hour runs from (hh-1):47 to hh:47 Sydney time;
each report counts for the official hour that had ended when it arrived, and
our actions for the hour they were published in.

Attribution summarises post_attribution: the features each publication was
captured with and the citizen reaction settled SETTLE_HOURS later. The reward
is the proxy defined in post_attribution (official persuasion is only
reported per team and hour), so it ranks arms; it is not official score.

Both are read-only; nothing here writes to the database.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from . import post_attribution
from .db import connect, utc_now
from .imports import normalize_timestamp

EXPOSURE_HOURS = 48
HOUR_MINUTE = 47
SUMMARY_HOURS = 6
# The platform froze counters on 22 Oct from about 16:00 to 18:36 Sydney (AEDT, UTC+11).
OUTAGE = ('2026-10-22T05:00:00Z', '2026-10-22T07:36:00Z')
OUTAGE_NOTE = '平台故障：10 月 22 日悉尼时间约 16:00–18:36 计数冻结，这几个官方小时的浏览和互动偏低，不代表内容效果'
EXPOSURE_NOTES = [
    '官方小时按悉尼时间 (hh-1):47 到 hh:47 计；每份官方报告算进它到达时已经结束的那个官方小时',
    '故障之后官方报告大约在每小时 :19 到达，这样的报告仍算进刚结束的 :47 官方小时，前后最多差半小时',
    '我方原创和回复来自 Agent 发布记录（agent_audit_events 的 published），按发布时间归到官方小时',
    '「进 Trending」只统计我方原创：发出后任何时候在 Trending 采集里出现过',
]
ATTRIBUTION_HOURS = 48
LATEST_ROWS = 50
ATTRIBUTION_NOTES = [
    f'发布 {post_attribution.SETTLE_HOURS} 小时后结算：市民回复 ×{post_attribution.WEIGHTS["npc_replies"]}、'
    f'转发 ×{post_attribution.WEIGHTS["reposts"]}、点赞 ×{post_attribution.WEIGHTS["likes"]}、'
    f'偏 Yes 的回复再 ×{post_attribution.WEIGHTS["reply_yes"]}',
    '回报是代理指标：官方的说服只按队伍和小时公布，不能落到单条帖子',
    '只算市民回复：机器人和高频竞品账号的回复不计入',
    f'回报 ≥ {post_attribution.SUCCESS_REWARD} 记为成功；同一「顾虑 × 动作」满 {post_attribution.MIN_ARM_POSTS} '
    '条已结算后，才参与顾虑雷达的排序',
]
AGENTS_META_KEY = 'agent_control_v1:agents'

_ITEM_SPLIT = re.compile(r',\s*|\s+and\s+(?=\d)')
_ITEM = re.compile(r'(\d+)\s+(.+)')


def _parse(value):
    try:
        return datetime.fromisoformat(normalize_timestamp(value).replace('Z', '+00:00'))
    except (TypeError, ValueError, AttributeError):
        return None


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _number(value):
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        return None
    return int(number) if number == number.to_integral_value() else float(number)


def _details(text):
    try:
        value = json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def agent_names(conn):
    """Role id -> display name from the Agent store row (no AgentStore, which may run DDL)."""
    row = conn.execute('SELECT value FROM meta WHERE key=?', (AGENTS_META_KEY,)).fetchone()
    try:
        agents = json.loads(row[0]) if row else {}
    except (TypeError, ValueError):
        agents = {}
    if not isinstance(agents, dict):
        return {}
    return {key: (agent.get('name') or agent.get('username') or key)
            for key, agent in agents.items() if isinstance(agent, dict)}


def hour_end(at):
    """The latest official hour end (hh:47) at or before ``at``.

    Sydney's offsets are whole hours, so hh:47 there is hh:47 in UTC as well.
    """
    end = at.replace(minute=HOUR_MINUTE, second=0, microsecond=0)
    return end if end <= at else end - timedelta(hours=1)


def _slot_end(at):
    """The end of the official hour (end-1h, end] that contains ``at``."""
    end = at.replace(minute=HOUR_MINUTE, second=0, microsecond=0)
    return end if end >= at else end + timedelta(hours=1)


def parse_engagement(message):
    """Counts from an official 'NPC engagement: ...' message; unknown items are kept as text.

    Since 22 Oct the platform also writes 'NPC engagement and moderation: ...'
    with Blue report items in the same list; those are scored on the Blue side.
    """
    counts = {key: 0 for key in ('views', 'likes', 'reposts', 'replies', 'follows', 'toward', 'away', 'neither',
                                 'primary_toward')}
    unparsed = []
    text = str(message or '')
    prefix = re.search(r'NPC engagement[^:]*:', text)
    if not prefix:
        return counts, [text[:160]] if text else []
    body = text[prefix.end():].strip().rstrip('.')
    for item in filter(None, (part.strip() for part in _ITEM_SPLIT.split(body))):
        match = _ITEM.match(item)
        if not match:
            unparsed.append(item[:160])
            continue
        count, rest = int(match.group(1)), match.group(2).lower()
        if 'inauthentic' in rest:
            continue
        if 'contribution' in rest:
            if 'neither' in rest:
                counts['neither'] += count
            elif 'away from' in rest:
                counts['away'] += count
            elif 'your side' in rest:
                counts['toward'] += count
                if rest.startswith('primary'):
                    counts['primary_toward'] += count
            else:
                unparsed.append(item[:160])
        elif rest.startswith('view'):
            counts['views'] += count
        elif rest.startswith('like'):
            counts['likes'] += count
        elif rest.startswith('repost'):
            counts['reposts'] += count
        elif rest.startswith('repl'):
            counts['replies'] += count
        elif rest.startswith('follow'):
            counts['follows'] += count
        else:
            unparsed.append(item[:160])
    return counts, unparsed


_HOUR_FIELDS = ('points', 'views', 'likes', 'reposts', 'replies', 'follows', 'toward', 'away', 'neither',
                'primary_toward', 'our_posts', 'our_replies', 'our_other', 'our_trending')


def _summary(hours):
    total = {key: 0 for key in _HOUR_FIELDS}
    for hour in hours:
        for key in _HOUR_FIELDS:
            total[key] += hour[key] or 0
    # Views per original only over hours with a report and working counters.
    clean = [hour for hour in hours if hour['status'] == 'reported' and not hour['outage']]
    posts = sum(hour['our_posts'] for hour in clean)
    total['hours'] = len(hours)
    total['outage_hours'] = sum(1 for hour in hours if hour['outage'])
    total['views_per_original'] = round(sum(hour['views'] for hour in clean) / posts, 2) if posts else None
    total['points'] = round(total['points'], 1) if isinstance(total['points'], float) else total['points']
    return total


def exposure(db_path, *, now=None, hours=EXPOSURE_HOURS):
    now = _parse(now) if now else datetime.now(timezone.utc)
    last_end = hour_end(now)
    ends = [last_end - timedelta(hours=k) for k in range(hours)]
    start = ends[-1] - timedelta(hours=1)
    outage = (_parse(OUTAGE[0]), _parse(OUTAGE[1]))
    conn = connect(db_path)
    try:
        reports = conn.execute(
            "SELECT occurred_at,points_decimal,message FROM official_activity WHERE event_type='SCORE_NARRATIVE' "
            "AND track='red' AND occurred_at IS NOT NULL AND ctn_timestamp(occurred_at)>? "
            'ORDER BY ctn_timestamp(occurred_at)', (normalize_timestamp(start),)).fetchall()
        imported = conn.execute('SELECT MAX(ctn_timestamp(occurred_at)) FROM official_activity').fetchone()[0]
        # Reports the CSV export has not reached yet come from the live activity table.
        from .official_feed import pending_rows
        pending = pending_rows(conn)
        live_until = pending[-1]['occurred_at'] if pending else None
        reports = list(reports) + [row for row in pending if row['track'] == 'red' and row['event_type'] == 'SCORE_NARRATIVE'
                                   and row['occurred_at'] > normalize_timestamp(start)]
        published = conn.execute(
            "SELECT at,details_json FROM agent_audit_events WHERE kind='published' AND ctn_timestamp(at)>?",
            (normalize_timestamp(start),)).fetchall()
        actions = []
        for row in published:
            at, details = _parse(row['at']), _details(row['details_json'])
            if at is not None and at <= now:
                actions.append((at, details.get('action'), details.get('post_id')))
        originals = sorted({post for _, action, post in actions if action == 'post' and type(post) is int})
        trending = set()
        for offset in range(0, len(originals), 500):
            batch = originals[offset:offset + 500]
            trending.update(row[0] for row in conn.execute(
                "SELECT DISTINCT post_id FROM feed_observations WHERE source='trending' AND post_id IN ("
                + ','.join('?' for _ in batch) + ')', batch))
    finally:
        conn.close()
    imported_until = _parse(imported) if imported else None
    seen_until = _parse(live_until) if live_until else imported_until
    slots = {}
    if now > last_end:
        slots[last_end + timedelta(hours=1)] = 'in_progress'
    for end in ends:
        slots[end] = None
    rows = {end: {'start': _iso(end - timedelta(hours=1)), 'end': _iso(end), 'reports': 0, 'live_reports': 0, 'unparsed': [],
                  **{key: 0 for key in _HOUR_FIELDS}} for end in slots}
    for report in reports:
        at = _parse(report['occurred_at'])
        row = rows.get(hour_end(at)) if at else None
        if row is None:
            continue
        counts, unparsed = parse_engagement(report['message'])
        row['reports'] += 1
        row['live_reports'] += bool(report.get('preview')) if isinstance(report, dict) else 0
        row['points'] += _number(report['points_decimal']) or 0
        row['unparsed'] += unparsed
        for key, value in counts.items():
            row[key] += value
    for at, action, post in actions:
        row = rows.get(_slot_end(at))
        if row is None:
            continue
        if action == 'post':
            row['our_posts'] += 1
            row['our_trending'] += post in trending
        elif action == 'reply':
            row['our_replies'] += 1
        else:
            row['our_other'] += 1
    result = []
    for end in sorted(rows, reverse=True):
        row = rows[end]
        if slots[end] == 'in_progress':
            status = 'in_progress'
        elif row['reports']:
            status = 'reported'
        elif seen_until is None or end > seen_until:
            status = 'not_imported'
        else:
            status = 'no_report'
        row['status'] = status
        row['outage'] = end - timedelta(hours=1) < outage[1] and end > outage[0]
        mix = row['our_posts'] + row['our_replies']
        row['post_share'] = round(row['our_posts'] / mix, 3) if mix else None
        result.append(row)
    complete = [row for row in result if row['status'] != 'in_progress']
    return {'generated_at': utc_now(), 'window': {'hours': hours, 'from': _iso(start), 'to': _iso(last_end)},
            'hour_minute': HOUR_MINUTE, 'timezone': 'Australia/Sydney',
            'imported_until': _iso(imported_until) if imported_until else None,
            'live_until': live_until,
            'outage': {'from': OUTAGE[0], 'to': OUTAGE[1], 'note': OUTAGE_NOTE},
            'hours': result,
            'summary': {'recent': _summary(complete[:SUMMARY_HOURS]), 'window': _summary(complete)},
            'summary_hours': SUMMARY_HOURS, 'notes': EXPOSURE_NOTES, 'read_only': True}


def _depth_bucket(depth):
    if depth is None:
        return None
    return str(depth) if depth < 4 else '4–6' if depth < 7 else '7+'


def _group(rows, key, labels=None):
    groups = defaultdict(list)
    for row in rows:
        value = key(row)
        groups['none' if value is None else value].append(row)
    out = []
    for value, members in groups.items():
        settled = [row for row in members if row['outcome_at']]
        n = len(settled)

        def mean(field):
            return round(sum(row[field] or 0 for row in settled) / n, 2) if n else None
        out.append({'key': value, 'label': (labels or {}).get(value, value), 'posts': len(members), 'settled': n,
                    'mean_reward': mean('reward'), 'npc_replies_per_post': mean('npc_replies'),
                    'likes_per_post': mean('likes'), 'reposts_per_post': mean('reposts'),
                    'reply_yes': sum(row['reply_yes'] or 0 for row in settled),
                    'success_rate': round(sum((row['reward'] or 0) >= post_attribution.SUCCESS_REWARD
                                              for row in settled) / n, 3) if n else None})
    return out


def attribution(db_path, *, now=None, hours=ATTRIBUTION_HOURS, latest=LATEST_ROWS):
    now = _parse(now) if now else datetime.now(timezone.utc)
    since = _iso(now - timedelta(hours=hours))
    conn = connect(db_path)
    try:
        rows = [dict(row) for row in conn.execute(
            '''SELECT post_id,agent_id,published_at,action,concern,used_radar,fact_ids_json,target_stance,target_bot,
                      thread_depth,body_chars,hour_local,pipeline,npc_replies,reply_yes,reply_undecided,reply_no,
                      likes,reposts,reward,outcome_at
               FROM post_attribution WHERE published_at>=? ORDER BY published_at DESC''', (since,))]
        names = agent_names(conn)
    finally:
        conn.close()
    for row in rows:
        # PostgreSQL may return numeric aggregates and DOUBLE PRECISION; keep the payload plain JSON.
        row['reward'] = float(row['reward']) if row['reward'] is not None else None
        try:
            row['fact_ids'] = list(json.loads(row.pop('fact_ids_json') or '[]'))
        except (TypeError, ValueError):
            row['fact_ids'] = []
        row['agent_name'] = names.get(row['agent_id'], row['agent_id'])
    arms = [{'concern': concern, 'action': action, **stats,
             'ranked': stats['posts'] >= post_attribution.MIN_ARM_POSTS}
            for (concern, action), stats in post_attribution.arm_stats(db_path, now=now, hours=hours).items()]
    arms.sort(key=lambda arm: (-arm['posts'], arm['concern'], arm['action']))
    settled = [row for row in rows if row['outcome_at']]
    pending = [row for row in rows if not row['outcome_at']]
    next_settle = min((row['published_at'] for row in pending), default=None)
    groups = {
        'action': _group(rows, lambda row: row['action']),
        'concern': _group(rows, lambda row: row['concern']),
        'agent': _group(rows, lambda row: row['agent_id'], names),
        'hour': sorted(_group(rows, lambda row: row['hour_local']),
                       key=lambda group: (group['key'] == 'none', group['key'] if group['key'] != 'none' else 0)),
        'target_stance': _group(rows, lambda row: row['target_stance']),
        'pipeline': _group(rows, lambda row: row['pipeline']),
        'depth': _group(rows, lambda row: _depth_bucket(row['thread_depth'])),
    }
    for key, values in groups.items():
        if key != 'hour':
            values.sort(key=lambda group: (-group['settled'], -group['posts'], str(group['key'])))
    total = _group(rows, lambda row: 'all')
    return {'generated_at': utc_now(), 'window': {'hours': hours, 'from': since, 'to': _iso(now)},
            'totals': {**(total[0] if total else {'posts': 0, 'settled': 0, 'mean_reward': None,
                                                     'success_rate': None, 'npc_replies_per_post': None,
                                                     'likes_per_post': None, 'reposts_per_post': None,
                                                     'reply_yes': 0}),
                       'pending': len(pending),
                       'next_settle_at': _iso(_parse(next_settle) + timedelta(hours=post_attribution.SETTLE_HOURS))
                       if next_settle else None},
            'groups': groups, 'arms': arms, 'latest': settled[:latest],
            'settle_hours': post_attribution.SETTLE_HOURS, 'success_reward': post_attribution.SUCCESS_REWARD,
            'min_arm_posts': post_attribution.MIN_ARM_POSTS, 'weights': post_attribution.WEIGHTS,
            'notes': ATTRIBUTION_NOTES, 'read_only': True}
