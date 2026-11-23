"""The Agents' staged pipeline step by step (GET /api/pipeline/steps).

reading -> decide -> write -> check -> publish, over the last WINDOW_HOURS:
how often each step ran, how long it took (median and p90) and why rounds
failed. Model steps come from llm_calls (one row per model request, by
purpose); rule-based reading and publishing come from agent_audit_events.

Publishing time is measured from the 'draft' event (the action was created)
to 'published', so it includes any wait for the scheduler or platform budget
(replies have had no deliberate pause since D72).

/api/pipeline/status has one headline per stage for the home page; this is
the detail behind its execute stage. Read-only.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import connect, utc_now
from .imports import normalize_timestamp
from .results_view import _details, _parse, agent_names

WINDOW_HOURS = 6
LOCAL = ZoneInfo('Australia/Sydney')
STEPS = (('reading', '选读'), ('decide', '决策'), ('write', '写作'), ('check', '检查'), ('publish', '发布'))
MODEL_STEPS = ('reading', 'decide', 'write', 'check')
# A draft created up to this long before the window may still be published inside it.
DRAFT_LOOKBACK_HOURS = 2
FAILURE_PREFIX = '生成任务失败：'
TOP_REASONS = 8
EXAMPLES = 3
EVENT_KINDS = ('reading_selection', 'generation_completed', 'generation_failed', 'published')
NOTES = ['模型步骤（选读、决策、写作、检查）的次数和耗时来自每一次模型请求；规则选读不调用模型，只计次数',
         '发布耗时从「生成动作」算到「发布成功」；回复会先故意等 3–10 分钟再发，所以比原创慢',
         '一轮失败可能在任何一步：下面的失败原因按整轮记录，「失败发生在」是失败时所处的阶段']


def percentile(values, q):
    """Linear-interpolated percentile (q in 0..1); None when empty."""
    values = sorted(value for value in values if value is not None)
    if not values:
        return None
    k = (len(values) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return round(values[lo] + (values[hi] - values[lo]) * (k - lo), 1)


def _durations(values):
    return {'median_ms': percentile(values, 0.5), 'p90_ms': percentile(values, 0.9)}


def failure_reason(message):
    """(category, detail) of a failed round's message, without the common prefix."""
    text = ' '.join(str(message or '').split())
    if text.startswith(FAILURE_PREFIX):
        text = text[len(FAILURE_PREFIX):]
    category, _, detail = text.partition('：')
    return category.strip() or '未说明原因', detail.strip()


def build(db_path, *, nav=None, now=None, hours=WINDOW_HOURS):
    now = _parse(now) if now else datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    stamp = normalize_timestamp
    conn = connect(db_path)
    try:
        calls = [dict(row) for row in conn.execute(
            'SELECT purpose,agent_id,status,error_code,http_status,duration_ms FROM llm_calls '
            'WHERE ctn_timestamp(started_at)>=? AND ctn_timestamp(started_at)<=?', (stamp(since), stamp(now)))]
        events = [dict(row) for row in conn.execute(
            'SELECT agent_id,at,kind,message,draft_id,details_json FROM agent_audit_events WHERE kind IN ('
            + ','.join('?' for _ in EVENT_KINDS) + ') AND ctn_timestamp(at)>=? AND ctn_timestamp(at)<=?',
            (*EVENT_KINDS, stamp(since), stamp(now)))]
        created = {}
        for row in conn.execute(
                "SELECT draft_id,at FROM agent_audit_events WHERE kind='draft' AND draft_id IS NOT NULL "
                'AND ctn_timestamp(at)>=? AND ctn_timestamp(at)<=?',
                (stamp(since - timedelta(hours=DRAFT_LOOKBACK_HOURS)), stamp(now))):
            at = _parse(row['at'])
            if at is not None and (row['draft_id'] not in created or at < created[row['draft_id']]):
                created[row['draft_id']] = at
        names = agent_names(conn)
    finally:
        conn.close()

    by_purpose = defaultdict(list)
    for call in calls:
        by_purpose[call['purpose']].append(call)
    for event in events:
        event['details'] = _details(event.pop('details_json'))
        event['time'] = _parse(event['at'])
    kinds = defaultdict(list)
    for event in events:
        kinds[event['kind']].append(event)

    steps = []
    for key, label in STEPS:
        step = {'key': key, 'label': label}
        if key in MODEL_STEPS:
            rows = by_purpose.get(key, [])
            errors = [row for row in rows if row['status'] != 'ok']
            step.update({'model_calls': len(rows), 'ok': len(rows) - len(errors), 'errors': len(errors),
                         'error_rate': round(len(errors) / len(rows), 3) if rows else None,
                         **_durations([row['duration_ms'] for row in rows if row['status'] == 'ok']),
                         'top_errors': [{'reason': reason, 'count': count} for reason, count in Counter(
                             row['error_code'] or (f"HTTP {row['http_status']}" if row['http_status'] else row['status'])
                             for row in errors).most_common(3)]})
        if key == 'reading':
            modes = Counter('rules' if str(event['details'].get('selection_status') or '').startswith('rules')
                            else 'model' for event in kinds['reading_selection'])
            step.update({'runs': len(kinds['reading_selection']), 'rules_runs': modes['rules'],
                         'model_runs': modes['model']})
        elif key in ('decide', 'write', 'check'):
            step['runs'] = step['model_calls']
        else:
            latency = defaultdict(list)
            actions = Counter()
            for event in kinds['published']:
                action = event['details'].get('action') or 'other'
                actions[action] += 1
                start = created.get(event['draft_id'])
                if start is not None and event['time'] is not None and event['time'] >= start:
                    latency[action if action in ('post', 'reply') else 'other'].append(
                        (event['time'] - start).total_seconds() * 1000)
            every = [value for values in latency.values() for value in values]
            step.update({'runs': len(kinds['published']), 'actions': dict(actions), **_durations(every),
                         'by_action': {action: {'count': len(values), **_durations(values)}
                                       for action, values in latency.items()}})
        steps.append(step)
    other = {purpose: len(rows) for purpose, rows in by_purpose.items() if purpose not in MODEL_STEPS}

    failed = kinds['generation_failed']
    completed = kinds['generation_completed']
    reasons = defaultdict(Counter)
    for event in failed:
        category, detail = failure_reason(event['message'])
        reasons[category][detail[:160]] += 1
    top = sorted(reasons.items(), key=lambda item: -sum(item[1].values()))[:TOP_REASONS]
    rounds = completed + failed
    totals = {'completed': len(completed), 'failed': len(failed),
              'failure_rate': round(len(failed) / len(rounds), 3) if rounds else None,
              'published': len(kinds['published']),
              'round_seconds': {'median': percentile([e['details'].get('elapsed_seconds') for e in completed], 0.5),
                                'p90': percentile([e['details'].get('elapsed_seconds') for e in completed], 0.9)}}

    hourly = {}
    first = since.astimezone(LOCAL).replace(minute=0, second=0, microsecond=0)
    hour = first
    while hour <= now.astimezone(LOCAL):
        hourly[hour] = Counter()
        hour += timedelta(hours=1)
    for event in events:
        if event['time'] is None:
            continue
        bucket = hourly.get(event['time'].astimezone(LOCAL).replace(minute=0, second=0, microsecond=0))
        if bucket is None:
            continue
        if event['kind'] == 'published':
            action = event['details'].get('action')
            bucket['post' if action == 'post' else 'reply' if action == 'reply' else 'other'] += 1
        elif event['kind'] in ('generation_completed', 'generation_failed'):
            bucket[event['kind'].split('_')[1]] += 1

    roles = defaultdict(Counter)
    for event in events:
        if not event['agent_id']:
            continue
        if event['kind'] == 'published':
            action = event['details'].get('action')
            roles[event['agent_id']]['post' if action == 'post' else 'reply' if action == 'reply' else 'other'] += 1
        elif event['kind'] in ('generation_completed', 'generation_failed'):
            roles[event['agent_id']][event['kind'].split('_')[1]] += 1
    for call in calls:
        if call['agent_id']:
            roles[call['agent_id']]['model_calls'] += 1
            roles[call['agent_id']]['model_errors'] += call['status'] != 'ok'
    agents_status = {}
    try:
        agents_status = (nav.get() if nav is not None else {}).get('agents') or {}
    except Exception:
        agents_status = {}
    queue = {key: agents_status.get(key) for key in ('pending_actions', 'uncertain_actions', 'inbox_unresolved',
                                                     'roles_running', 'roles_enabled', 'team_enabled')}
    queue['maintenance'] = agents_status.get('maintenance')
    return {'generated_at': utc_now(), 'window': {'hours': hours, 'from': stamp(since), 'to': stamp(now)},
            'totals': totals, 'steps': steps, 'other_model_calls': other, 'queue': queue,
            'failures': [{'reason': category, 'count': sum(details.values()),
                          'examples': [{'detail': detail, 'count': count}
                                       for detail, count in details.most_common(EXAMPLES) if detail]}
                         for category, details in top],
            'failure_phases': dict(Counter(event['details'].get('phase') or 'unknown' for event in failed)),
            'hourly': [{'hour': stamp(hour), **{key: counts[key] for key in
                                                 ('completed', 'failed', 'post', 'reply', 'other')}}
                       for hour, counts in sorted(hourly.items(), reverse=True)],
            'roles': sorted(({'agent_id': agent_id, 'name': names.get(agent_id, agent_id),
                              **{key: counts[key] for key in ('completed', 'failed', 'post', 'reply', 'other',
                                                               'model_calls', 'model_errors')}}
                             for agent_id, counts in roles.items()),
                            key=lambda row: (-(row['post'] + row['reply']), str(row['name']))),
            'notes': NOTES, 'read_only': True}
