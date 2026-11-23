"""Read-only KPI snapshot of score progress and Agent behaviour.

Every database session starts with ``BEGIN READ ONLY`` and is rolled back; the
script makes no LLM/API calls and changes no service state. It writes exactly
one file: reports/generated/kpi-<label>-<YYYYMMDD-HHMM>.json (UTC stamp).

    .venv/Scripts/python.exe -X utf8 scripts/measure_kpis.py --label baseline --hours 8

Each section is computed independently; a failing section records its error
string under ``error`` and the others still run.
"""
import sys

sys.dont_write_bytecode = True  # keep the run free of side files

import argparse
import json
import math
import re
import statistics
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ctn_data import agent_targeting  # noqa: E402
from ctn_data.db import connect  # noqa: E402
from ctn_data.secret_files import read_secret  # noqa: E402

OUT_DIR = ROOT / 'reports' / 'generated'
AGENTS_META_KEY = 'agent_control_v1:agents'
DEEP_REPLY_DEPTH = 9


# ---------------------------------------------------------------- helpers

def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def parse_iso(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def to_number(value):
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        return None
    return int(number) if number == number.to_integral_value() else float(number)


def pct(values, q):
    """Linear-interpolated percentile (q in 0..1); None when empty."""
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    k = (len(values) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    result = values[lo] + (values[hi] - values[lo]) * (k - lo)
    return round(result, 3)


def median(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 3) if values else None


def share(part, whole):
    return round(part / whole, 4) if whole else None


def loads(text):
    try:
        value = json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def norm_digits(text):
    return re.sub(r'\d+', '#', str(text or '')).strip()


class ReadOnly:
    """One database session that always starts with BEGIN READ ONLY and never commits."""

    def __init__(self, url):
        self.url = url

    def __enter__(self):
        self.conn = connect(self.url)
        try:
            self.conn.execute('BEGIN READ ONLY')
        except Exception:
            self.conn.close()
            raise
        return self.conn

    def __exit__(self, *exc):
        try:
            self.conn.rollback()
        finally:
            self.conn.close()


def readonly_connect(path):
    """Drop-in for agent_targeting.connect so its helpers also run read-only."""
    conn = connect(path)
    conn.execute('BEGIN READ ONLY')
    return conn


def audit_rows(conn, kinds, start, end):
    marks = ','.join('?' for _ in kinds)
    return conn.execute(
        'SELECT agent_id,at,kind,message,draft_id,details_json FROM agent_audit_events '
        'WHERE kind IN (' + marks + ') AND ctn_timestamp(at)>=ctn_timestamp(?) '
        'AND ctn_timestamp(at)<ctn_timestamp(?) ORDER BY ctn_timestamp(at)',
        (*kinds, iso(start), iso(end))).fetchall()


# ---------------------------------------------------------------- red parsing

_ITEM_SPLIT = re.compile(r',\s*|\s+and\s+(?=\d)')


def contribution_direction(text):
    text = text.lower()
    if 'neither' in text:
        return 'neither'
    if 'away from' in text:
        return 'away'
    if 'your side' in text:
        return 'toward'
    if 'other view' in text or 'other side' in text:
        return 'other'
    return 'unspecified'


def empty_composition():
    directions = {key: 0 for key in ('toward', 'away', 'neither', 'other', 'unspecified')}
    return {'events': 0, 'points': 0, 'positive_points': 0, 'negative_points': 0,
            'views': 0, 'likes': 0, 'reposts': 0, 'replies': 0, 'follows': 0,
            'contributions': dict(directions), 'primary_contributions': dict(directions),
            'unparsed_items': []}


def add_red_message(comp, message, points):
    comp['events'] += 1
    if points is not None:
        comp['points'] += points
        comp['positive_points' if points >= 0 else 'negative_points'] += points
    text = str(message or '')
    if 'NPC engagement:' not in text:
        comp['unparsed_items'].append(text[:160])
        return
    body = text.split('NPC engagement:', 1)[1].strip().rstrip('.')
    for item in filter(None, (part.strip() for part in _ITEM_SPLIT.split(body))):
        match = re.match(r'(\d+)\s+(.+)', item)
        if not match:
            comp['unparsed_items'].append(item[:160])
            continue
        count, rest = int(match.group(1)), match.group(2).lower()
        if 'contribution' in rest:
            bucket = 'primary_contributions' if rest.startswith('primary') else 'contributions'
            comp[bucket][contribution_direction(rest)] += count
        elif rest.startswith('view'):
            comp['views'] += count
        elif rest.startswith('like'):
            comp['likes'] += count
        elif rest.startswith('repost'):
            comp['reposts'] += count
        elif rest.startswith('repl'):
            comp['replies'] += count
        elif rest.startswith('follow'):
            comp['follows'] += count
        else:
            comp['unparsed_items'].append(item[:160])


# ---------------------------------------------------------------- sections

def section_freshness(url, ctx):
    with ReadOnly(url) as conn:
        def latest(sql):
            row = conn.execute(sql).fetchone()
            return row[0] if row else None
        return {
            'latest_official_activity': latest('SELECT max(ctn_timestamp(occurred_at)) FROM official_activity'),
            'latest_red_activity': latest("SELECT max(ctn_timestamp(occurred_at)) FROM official_activity "
                                          "WHERE event_type='SCORE_NARRATIVE' AND track='red'"),
            'latest_score_snapshot': latest('SELECT max(ctn_timestamp(observed_at)) FROM score_snapshots'),
            'latest_audit_event': latest('SELECT max(ctn_timestamp(at)) FROM agent_audit_events'),
            'latest_post_created': latest('SELECT max(ctn_timestamp(created_at)) FROM posts'),
        }


def section_agents(url, ctx):
    with ReadOnly(url) as conn:
        row = conn.execute('SELECT value FROM meta WHERE key=?', (AGENTS_META_KEY,)).fetchone()
    agents = loads(row[0]) if row else {}
    result = {}
    for agent_id, agent in agents.items():
        if isinstance(agent, dict):
            result[agent_id] = {'name': agent.get('name'), 'account_id': agent.get('account_id'),
                                'enabled': agent.get('enabled')}
    ctx['agents'] = result
    return result


def section_score(url, ctx, target, rate_hours):
    end = ctx['end']
    rate_start = end - timedelta(hours=rate_hours)
    with ReadOnly(url) as conn:
        snap = conn.execute(
            'SELECT observed_at,total_decimal,source FROM score_snapshots '
            'WHERE ctn_timestamp(observed_at)<=ctn_timestamp(?) '
            'ORDER BY ctn_timestamp(observed_at) DESC,id DESC LIMIT 1', (iso(end),)).fetchone()
        snap_before = conn.execute(
            'SELECT observed_at,total_decimal FROM score_snapshots '
            'WHERE ctn_timestamp(observed_at)<=ctn_timestamp(?) '
            'ORDER BY ctn_timestamp(observed_at) DESC,id DESC LIMIT 1', (iso(rate_start),)).fetchone()
        rows = conn.execute(
            "SELECT occurred_at,track,points_decimal FROM official_activity "
            "WHERE event_type='SCORE_NARRATIVE' AND ctn_timestamp(occurred_at)<=ctn_timestamp(?) "
            "ORDER BY ctn_timestamp(occurred_at)", (iso(end),)).fetchall()
    score = to_number(snap['total_decimal']) if snap else None
    needed = max(0, target - score) if score is not None else None
    events = [(parse_iso(r['occurred_at']), r['track'], to_number(r['points_decimal'])) for r in rows]
    red = [(at, pts) for at, track, pts in events if track == 'red' and at is not None and pts is not None]
    red_window = sum(pts for at, pts in red if rate_start <= at <= end)
    rate_now = red_window / rate_hours if rate_hours else None

    # Official reports arrive hourly and are imported in batches, so a window
    # ending "now" undercounts until the next import. Also measure the latest
    # rate_hours hourly report buckets, anchored at the newest imported one.
    buckets = defaultdict(int)
    for at, pts in red:
        buckets[at.replace(minute=0, second=0, microsecond=0)] += pts
    if buckets:
        last_bucket = max(buckets)
        first_bucket = last_bucket - timedelta(hours=rate_hours - 1)
        rate_reports = sum(v for k, v in buckets.items() if k >= first_bucket) / rate_hours
    else:
        last_bucket, rate_reports = None, None

    red_total = sum(pts for _, pts in red)
    if red:
        first_at, last_at = red[0][0], red[-1][0]
        history_hours = (last_at - first_at).total_seconds() / 3600 + 1.0
        history_rate = red_total / history_hours
    else:
        first_at = last_at = None
        history_hours = history_rate = None

    def eta(rate):
        if needed is None or not rate or rate <= 0:
            return None, None
        hours = needed / rate
        return round(hours, 2), iso(end + timedelta(hours=hours))

    eta_hours, eta_at = eta(rate_now)
    eta_reports_hours, eta_reports_at = eta(rate_reports)
    snapshot_delta = None
    if snap and snap_before:
        before = to_number(snap_before['total_decimal'])
        if before is not None and score is not None:
            snapshot_delta = {'from_observed_at': snap_before['observed_at'], 'from_total': before,
                              'delta': score - before, 'per_hour': round((score - before) / rate_hours, 2)}
    return {
        'latest_score': score,
        'latest_score_observed_at': snap['observed_at'] if snap else None,
        'latest_score_source': snap['source'] if snap else None,
        'target': target,
        'points_needed': needed,
        'official_points_total_imported': sum(p for _, _, p in events if p is not None),
        'points_by_track': {track: sum(p for _, t, p in events if t == track and p is not None)
                            for track in sorted({t for _, t, _ in events if t})},
        'red_rate_window_hours': rate_hours,
        'red_points_last_window_to_end': red_window,
        'red_points_per_hour_last_window': round(rate_now, 2) if rate_now is not None else None,
        'hours_to_target_at_window_rate': eta_hours,
        'eta_at_window_rate': eta_at,
        'red_points_per_hour_latest_hourly_reports': round(rate_reports, 2) if rate_reports is not None else None,
        'latest_red_report_hour': iso(last_bucket) if last_bucket else None,
        'hours_to_target_at_latest_reports_rate': eta_reports_hours,
        'eta_at_latest_reports_rate': eta_reports_at,
        'red_points_total_history': red_total,
        'red_history_first_event': iso(first_at) if first_at else None,
        'red_history_last_event': iso(last_at) if last_at else None,
        'red_history_hours': round(history_hours, 2) if history_hours else None,
        'red_points_per_hour_history': round(history_rate, 2) if history_rate is not None else None,
        'snapshot_delta_over_rate_window': snapshot_delta,
        'notes': ['window rate = red points with occurred_at in [end-%dh, end] / %d' % (rate_hours, rate_hours),
                  'history rate = all red points / (last - first red event + 1h; each report covers an hour)',
                  'latest-reports rate = last %d hourly report buckets up to the newest imported report' % rate_hours],
    }


def section_red_composition(url, ctx):
    start, end = ctx['start'], ctx['end']
    with ReadOnly(url) as conn:
        rows = conn.execute(
            "SELECT occurred_at,points_decimal,message FROM official_activity "
            "WHERE event_type='SCORE_NARRATIVE' AND track='red' AND ctn_timestamp(occurred_at)<=ctn_timestamp(?) "
            "ORDER BY ctn_timestamp(occurred_at)", (iso(end),)).fetchall()
    window, history = empty_composition(), empty_composition()
    for row in rows:
        at, pts = parse_iso(row['occurred_at']), to_number(row['points_decimal'])
        add_red_message(history, row['message'], pts)
        if at and start <= at <= end:
            add_red_message(window, row['message'], pts)
    return {'window': window, 'all_history': history}


def _nested_top(counter, limit):
    """{(head, sub): n} -> {head: {sub: n}} keeping the top ``limit`` subs per head."""
    nested = defaultdict(Counter)
    for (head, sub), count in counter.items():
        nested[head][sub] += count
    return {head: dict(subs.most_common(limit)) for head, subs in nested.items()}


def section_generation(url, ctx):
    rows_by_kind = defaultdict(list)
    with ReadOnly(url) as conn:
        for row in audit_rows(conn, ('generation_completed', 'generation_failed', 'generation_cancelled',
                                     'generation_interrupted', 'generation_started', 'error'),
                              ctx['start'], ctx['end']):
            rows_by_kind[row['kind']].append(row)
    completed, failed = rows_by_kind['generation_completed'], rows_by_kind['generation_failed']
    elapsed = lambda rows: [loads(r['details_json']).get('elapsed_seconds') for r in rows]
    completed_elapsed = [v for v in elapsed(completed) if isinstance(v, (int, float))]
    failed_elapsed = [v for v in elapsed(failed) if isinstance(v, (int, float))]

    heads, subs, failed_heads = Counter(), Counter(), Counter()
    for row in rows_by_kind['error']:
        message = str(row['message'] or '')
        parts = re.split(r'[：:]', message, 1)
        head, rest = parts[0], parts[1] if len(parts) > 1 else ''
        head = norm_digits(head)[:120]
        heads[head] += 1
        # Check failures join their reasons with a full-width semicolon; an
        # ASCII ';' belongs inside one reason ("312 characters; keep it under").
        for part in filter(None, (p.strip() for p in rest.split('；'))):
            key = re.split(r'[：:]', part, 1)[0]
            subs[(head, norm_digits(key)[:120])] += 1
    for row in failed:
        message = re.sub(r'^生成任务失败[：:]', '', str(row['message'] or ''))
        failed_heads[norm_digits(re.split(r'[：:]', message, 1)[0])[:120]] += 1

    per_agent = defaultdict(Counter)
    for kind, rows in rows_by_kind.items():
        for row in rows:
            per_agent[row['agent_id'] or 'unknown'][kind] += 1
    return {
        'started': len(rows_by_kind['generation_started']),
        'completed': len(completed),
        'completed_with_elapsed': len(completed_elapsed),
        'completed_without_elapsed_after_wait': len(completed) - len(completed_elapsed),
        'failed': len(failed),
        'cancelled': len(rows_by_kind['generation_cancelled']),
        'interrupted': len(rows_by_kind['generation_interrupted']),
        'failure_rate': share(len(failed), len(failed) + len(completed_elapsed)),
        'completed_elapsed_seconds': {'median': median(completed_elapsed), 'p90': pct(completed_elapsed, 0.9),
                                      'sum': round(sum(completed_elapsed), 1)},
        'failed_elapsed_seconds': {'median': median(failed_elapsed), 'p90': pct(failed_elapsed, 0.9),
                                   'sum': round(sum(failed_elapsed), 1)},
        'error_events': len(rows_by_kind['error']),
        'error_reasons': dict(heads.most_common()),
        'error_sub_reasons_top': _nested_top(subs, 10),
        'generation_failed_reasons': dict(failed_heads.most_common()),
        'per_agent': {agent: dict(counts) for agent, counts in per_agent.items()},
        'notes': ['failure_rate = failed / (failed + completed runs that report elapsed_seconds)',
                  'completed events without elapsed_seconds close a waiting action, not a model run'],
    }


def _cached_tokens(usage):
    details = usage.get('prompt_tokens_details')
    if isinstance(details, dict) and isinstance(details.get('cached_tokens'), (int, float)):
        return details['cached_tokens']
    for key in ('cached_tokens', 'prompt_cache_hit_tokens', 'cache_read_input_tokens'):
        if isinstance(usage.get(key), (int, float)):
            return usage[key]
    return None


def section_tokens_and_actions(url, ctx):
    with ReadOnly(url) as conn:
        drafts = audit_rows(conn, ('draft',), ctx['start'], ctx['end'])
        published = audit_rows(conn, ('published',), ctx['start'], ctx['end'])
    fields = ('prompt_tokens', 'completion_tokens', 'reasoning_tokens', 'total_tokens')
    values = {field: [] for field in fields}
    cached_sum = cached_prompt_sum = cached_drafts = 0
    cost_sum, cost_drafts = 0.0, 0
    by_model = defaultdict(lambda: Counter())
    actions, drafts_per_agent = Counter(), defaultdict(Counter)
    without_usage = 0
    for row in drafts:
        details = loads(row['details_json'])
        action = details.get('action') or 'unknown'
        actions[action] += 1
        drafts_per_agent[row['agent_id'] or 'unknown'][action] += 1
        usage = details.get('usage') if isinstance(details.get('usage'), dict) else None
        if not usage:
            without_usage += 1
            continue
        model = details.get('model') or 'unknown'
        by_model[model]['drafts'] += 1
        for field in fields:
            value = usage.get(field)
            if isinstance(value, (int, float)):
                values[field].append(value)
                by_model[model][field] += value
        cached = _cached_tokens(usage)
        if cached is not None and isinstance(usage.get('prompt_tokens'), (int, float)):
            cached_sum += cached
            cached_prompt_sum += usage['prompt_tokens']
            cached_drafts += 1
        if isinstance(usage.get('cost'), (int, float)):
            cost_sum += usage['cost']
            cost_drafts += 1
    tokens = {
        'drafts': len(drafts),
        'drafts_with_usage': len(drafts) - without_usage,
        'sum': {field: sum(values[field]) for field in fields},
        'median_per_draft': {field: median(values[field]) for field in fields},
        'p90_per_draft': {field: pct(values[field], 0.9) for field in fields},
        'drafts_reporting_reasoning_tokens': len(values['reasoning_tokens']),
        'cached_token_share': share(cached_sum, cached_prompt_sum) if cached_drafts else None,
        'drafts_reporting_cached_tokens': cached_drafts,
        'cost_sum': round(cost_sum, 6) if cost_drafts else None,
        'drafts_reporting_cost': cost_drafts,
        'by_model': {model: dict(counts) for model, counts in by_model.items()},
        'notes': ['usage is logged on draft events only; failed generations also spend tokens but are not counted'],
    }

    published_per_agent, published_actions = defaultdict(Counter), Counter()
    for row in published:
        action = loads(row['details_json']).get('action') or 'unknown'
        published_actions[action] += 1
        published_per_agent[row['agent_id'] or 'unknown'][action] += 1
    names = ctx.get('agents', {})
    per_agent = {}
    for agent in sorted(set(published_per_agent) | set(drafts_per_agent)):
        info = names.get(agent, {})
        per_agent[agent] = {'name': info.get('name'), 'account_id': info.get('account_id'),
                            'published': sum(published_per_agent[agent].values()),
                            'published_by_action': dict(published_per_agent[agent]),
                            'drafts_by_action': dict(drafts_per_agent[agent])}
    actions_section = {
        'draft_action_mix': dict(actions.most_common()),
        'published_total': len(published),
        'published_action_mix': dict(published_actions.most_common()),
        'per_agent': per_agent,
    }
    return tokens, actions_section


def section_reply_quality(url, ctx):
    with ReadOnly(url) as conn:
        published = audit_rows(conn, ('published',), ctx['start'], ctx['end'])
        replies, others = [], []
        for row in published:
            details = loads(row['details_json'])
            item = {'agent_id': row['agent_id'], 'action': details.get('action'),
                    'post_id': details.get('post_id'), 'target_post_id': details.get('target_post_id'),
                    'target_account_id': details.get('target_account_id')}
            (replies if item['action'] == 'reply' else others).append(item)
        ids = {i[k] for i in replies + others for k in ('post_id', 'target_post_id') if type(i[k]) is int}
        authors = {}
        chunk_ids = sorted(ids)
        for begin in range(0, len(chunk_ids), 500):
            chunk = chunk_ids[begin:begin + 500]
            for row in conn.execute('SELECT post_id,author_id FROM posts WHERE post_id IN ('
                                    + ','.join('?' for _ in chunk) + ')', tuple(chunk)):
                authors[row['post_id']] = row['author_id']
        reply_ids = {i[k] for i in replies for k in ('post_id', 'target_post_id') if type(i[k]) is int}
        depths = agent_targeting._depths(conn, agent_targeting._parents(conn, reply_ids)) if reply_ids else {}
        own = {row[0] for row in conn.execute('SELECT account_id FROM team_accounts WHERE is_own=1')}
    own |= {a.get('account_id') for a in ctx.get('agents', {}).values() if a.get('account_id')}

    original = agent_targeting.connect
    agent_targeting.connect = readonly_connect
    try:
        bots = agent_targeting.behavior_bots(url)
    finally:
        agent_targeting.connect = original

    n = len(replies)
    known_author = bot = own_target = known_depth = deep = blocked = target_depth_known = 0
    reply_depths = []
    per_agent = defaultdict(Counter)
    for item in replies:
        agent = item['agent_id'] or 'unknown'
        per_agent[agent]['replies'] += 1
        target = item['target_post_id']
        author = authors.get(target)
        if author is not None:
            known_author += 1
            if author in bots:
                bot += 1
                per_agent[agent]['bot_target'] += 1
            if author in own:
                own_target += 1
                per_agent[agent]['own_target'] += 1
        depth = depths.get(item['post_id'])
        if depth is None and target in depths:
            depth = depths[target] + 1
        if depth is not None:
            known_depth += 1
            reply_depths.append(depth)
            if depth >= DEEP_REPLY_DEPTH:
                deep += 1
                per_agent[agent]['deep_ge_%d' % DEEP_REPLY_DEPTH] += 1
        if target in depths:
            target_depth_known += 1
        if type(target) is int and agent_targeting.blocked_reason(
                {'kind': 'post', 'post_id': target, 'author_id': author}, bots, depths):
            blocked += 1

    other_own = Counter()
    for item in others:
        target_author = authors.get(item['target_post_id']) if item['target_post_id'] else item['target_account_id']
        if target_author is not None and target_author in own:
            other_own[item['action'] or 'unknown'] += 1
    return {
        'published_replies': n,
        'target_author_known': known_author,
        'bot_target_count': bot,
        'bot_target_share': share(bot, known_author),
        'deep_reply_threshold': DEEP_REPLY_DEPTH,
        'reply_depth_known': known_depth,
        'deep_reply_count': deep,
        'deep_reply_share': share(deep, known_depth),
        'reply_depth_median': median(reply_depths),
        'reply_depth_p90': pct(reply_depths, 0.9),
        'own_account_target_count': own_target,
        'own_account_target_share': share(own_target, known_author),
        'blocked_under_current_policy_count': blocked,
        'blocked_under_current_policy_share': share(blocked, n),
        'behavior_bots_now': len(bots),
        'own_account_ids': sorted(a for a in own if a is not None),
        'non_reply_actions_on_own_accounts': dict(other_own),
        'per_agent': {agent: dict(counts) for agent, counts in per_agent.items()},
        'notes': ['shares use the replies whose target author / depth is known as denominator',
                  'reply depth = depth of our published reply (0 = root); falls back to target depth + 1',
                  'behavior_bots is evaluated now over its own %dh window, not as of each reply'
                  % agent_targeting.BEHAVIOR_WINDOW_HOURS,
                  'blocked_under_current_policy = agent_targeting.blocked_reason (bot author or target depth >= %d)'
                  % agent_targeting.MAX_REPLY_DEPTH],
    }


def section_exposure(url, ctx):
    """Per official hour: citizen views, changes toward us, and our post/reply mix.

    Official reports are imported in batches, so the window is the last N hourly
    reports up to the newest one imported (N = window hours), not the clock.
    """
    hours = max(1, int(round((ctx['end'] - ctx['start']).total_seconds() / 3600)))
    with ReadOnly(url) as conn:
        reports = conn.execute(
            "SELECT occurred_at,points_decimal,message FROM official_activity "
            "WHERE event_type='SCORE_NARRATIVE' AND track='red' AND ctn_timestamp(occurred_at)<=ctn_timestamp(?) "
            "ORDER BY ctn_timestamp(occurred_at) DESC LIMIT ?", (iso(ctx['end']), hours)).fetchall()
        exists = conn.execute("SELECT to_regclass('post_attribution') IS NOT NULL").fetchone()[0]
        ours = conn.execute('SELECT published_at,action FROM post_attribution').fetchall() if exists else []
    published = [(parse_iso(row['published_at']), row['action']) for row in ours]
    rows = []
    for report in reversed(reports):
        end = parse_iso(report['occurred_at'])
        start = end - timedelta(hours=1)
        comp = empty_composition()
        add_red_message(comp, report['message'], to_number(report['points_decimal']))
        posts = sum(1 for at, action in published if at and start <= at < end and action == 'post')
        replies = sum(1 for at, action in published if at and start <= at < end and action == 'reply')
        rows.append({'hour_end': iso(end), 'points': comp['points'], 'views': comp['views'],
                     'toward_us': comp['contributions']['toward'] + comp['primary_contributions']['toward'],
                     'primary_toward_us': comp['primary_contributions']['toward'],
                     'neither': comp['contributions']['neither'] + comp['primary_contributions']['neither'],
                     'away': comp['contributions']['away'] + comp['primary_contributions']['away'],
                     'our_posts': posts, 'our_replies': replies, 'post_share': share(posts, posts + replies)})
    n = len(rows) or 1
    total_posts = sum(r['our_posts'] for r in rows)
    total_replies = sum(r['our_replies'] for r in rows)
    return {'hourly': rows, 'hours': len(rows),
            'views_per_hour': round(sum(r['views'] for r in rows) / n, 2),
            'toward_us_total': sum(r['toward_us'] for r in rows),
            'toward_us_per_hour': round(sum(r['toward_us'] for r in rows) / n, 2),
            'neither_total': sum(r['neither'] for r in rows),
            'post_share': share(total_posts, total_posts + total_replies),
            'our_posts': total_posts, 'our_replies': total_replies,
            'attribution_table': bool(exists),
            'notes': ['hours = latest N imported hourly red reports up to --end',
                      'our posts/replies from post_attribution (published_at in each report hour)']}


# ---------------------------------------------------------------- main

def run_section(report, name, func, *args):
    try:
        result = func(*args)
    except Exception as exc:  # one failing section must not stop the others
        report[name] = {'error': '%s: %s' % (type(exc).__name__, exc),
                        'trace': traceback.format_exc(limit=3)[-1200:]}
        return None
    report[name] = result
    return result


def summary_lines(r):
    lines = []
    s = r.get('score', {})
    if 'error' in s:
        lines.append('score: ERROR ' + s['error'])
    elif s:
        lines.append('Score %s (at %s); %s needed for %s.' % (s['latest_score'], s['latest_score_observed_at'],
                                                             s['points_needed'], s['target']))
        lines.append('Red pts/h: last %dh %s | latest %d reports %s | history %s. Hours to target @%dh rate: %s (@reports: %s)'
                     % (s['red_rate_window_hours'], s['red_points_per_hour_last_window'], s['red_rate_window_hours'],
                        s['red_points_per_hour_latest_hourly_reports'], s['red_points_per_hour_history'],
                        s['red_rate_window_hours'], s['hours_to_target_at_window_rate'],
                        s['hours_to_target_at_latest_reports_rate']))
    c = r.get('red_composition', {})
    if 'error' in c:
        lines.append('red composition: ERROR ' + c['error'])
    elif c:
        w = c['window']
        lines.append('Red (window): %d events, %s pts; views %d likes %d reposts %d replies %d follows %d; '
                     'contrib %s; primary %s' % (w['events'], w['points'], w['views'], w['likes'], w['reposts'],
                                                 w['replies'], w['follows'],
                                                 {k: v for k, v in w['contributions'].items() if v},
                                                 {k: v for k, v in w['primary_contributions'].items() if v}))
    e = r.get('exposure', {})
    if 'error' in e:
        lines.append('exposure: ERROR ' + e['error'])
    elif e:
        lines.append('Exposure (%d hourly reports): views/h %s; toward us %d (%s/h); neither %d; post share %s '
                     '(%d posts, %d replies)' % (e['hours'], e['views_per_hour'], e['toward_us_total'],
                                                e['toward_us_per_hour'], e['neither_total'], e['post_share'],
                                                e['our_posts'], e['our_replies']))
    g = r.get('generation', {})
    if 'error' in g:
        lines.append('generation: ERROR ' + g['error'])
    elif g:
        lines.append('Generation: %d completed (%d model runs), %d failed (rate %s); elapsed median/p90 ok %s/%s s, '
                     'failed %s/%s s' % (g['completed'], g['completed_with_elapsed'], g['failed'], g['failure_rate'],
                                         g['completed_elapsed_seconds']['median'], g['completed_elapsed_seconds']['p90'],
                                         g['failed_elapsed_seconds']['median'], g['failed_elapsed_seconds']['p90']))
        for reason, count in list(g['error_reasons'].items())[:5]:
            lines.append('  %4d  %s' % (count, reason))
    t = r.get('tokens', {})
    if 'error' in t:
        lines.append('tokens: ERROR ' + t['error'])
    elif t:
        lines.append('Tokens: %d drafts; sum prompt/completion/reasoning %s/%s/%s; median %s/%s/%s; cached share %s'
                     % (t['drafts'], t['sum']['prompt_tokens'], t['sum']['completion_tokens'],
                        t['sum']['reasoning_tokens'], t['median_per_draft']['prompt_tokens'],
                        t['median_per_draft']['completion_tokens'], t['median_per_draft']['reasoning_tokens'],
                        t['cached_token_share']))
    a = r.get('actions', {})
    if 'error' in a:
        lines.append('actions: ERROR ' + a['error'])
    elif a:
        lines.append('Draft mix %s; published %d' % (a['draft_action_mix'], a['published_total']))
        for agent, info in a['per_agent'].items():
            lines.append('  %-14s published %3d %s' % (info['name'] or agent[:8], info['published'],
                                                       info['published_by_action']))
    q = r.get('reply_quality', {})
    if 'error' in q:
        lines.append('reply quality: ERROR ' + q['error'])
    elif q:
        lines.append('Replies %d: bot target %s (%d/%d), depth>=%d %s (%d/%d), own target %s, blocked now %s; bots now %d'
                     % (q['published_replies'], q['bot_target_share'], q['bot_target_count'], q['target_author_known'],
                        q['deep_reply_threshold'], q['deep_reply_share'], q['deep_reply_count'], q['reply_depth_known'],
                        q['own_account_target_share'], q['blocked_under_current_policy_share'], q['behavior_bots_now']))
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--label', default='run', help='label used in the output file name')
    parser.add_argument('--hours', type=float, default=8.0, help='window length in hours (default 8)')
    parser.add_argument('--end', help='window end as ISO UTC (default: now)')
    parser.add_argument('--target', type=int, default=7000, help='score target (default 7000)')
    parser.add_argument('--rate-hours', type=int, default=12, help='red rate window in hours (default 12)')
    args = parser.parse_args()
    label = re.sub(r'[^A-Za-z0-9_.-]+', '-', args.label).strip('-') or 'run'

    now = datetime.now(timezone.utc)
    end = parse_iso(args.end) if args.end else now
    if end is None:
        parser.error('--end must be an ISO timestamp')
    start = end - timedelta(hours=args.hours)
    ctx = {'start': start, 'end': end}
    report = {'label': label, 'generated_at': iso(now), 'window': {'start': iso(start), 'end': iso(end),
                                                                   'hours': args.hours},
              'read_only': True}
    url = read_secret(ROOT / 'data/secrets/postgres_url').strip()

    run_section(report, 'freshness', section_freshness, url, ctx)
    run_section(report, 'agents', section_agents, url, ctx)
    run_section(report, 'score', section_score, url, ctx, args.target, args.rate_hours)
    run_section(report, 'red_composition', section_red_composition, url, ctx)
    run_section(report, 'exposure', section_exposure, url, ctx)
    run_section(report, 'generation', section_generation, url, ctx)
    try:
        report['tokens'], report['actions'] = section_tokens_and_actions(url, ctx)
    except Exception as exc:
        error = {'error': '%s: %s' % (type(exc).__name__, exc), 'trace': traceback.format_exc(limit=3)[-1200:]}
        report['tokens'], report['actions'] = error, dict(error)
    run_section(report, 'reply_quality', section_reply_quality, url, ctx)

    out = OUT_DIR / ('kpi-%s-%s.json' % (label, now.strftime('%Y%m%d-%H%M')))
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print('KPI %s  window %s .. %s (%sh)' % (label, iso(start), iso(end), args.hours))
    for line in summary_lines(report):
        print(line)
    print('Wrote', out)


if __name__ == '__main__':
    main()
