"""End-to-end pipeline status for the home page (GET /api/pipeline/status).

Six stages, each {status: ok|warn|bad|unknown, headline, metrics, updated_at},
computed only from existing tables and caches. Thresholds live here so the
page only displays. A failing stage becomes status 'unknown' with an error and
the others are still returned. Shared for 60 seconds.
"""
from __future__ import annotations

import json
import logging
import re
import statistics
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from .db import connect, utc_now
from .imports import normalize_timestamp

LOGGER = logging.getLogger(__name__)

CACHE_SECONDS = 60
STAGES = ('collect', 'process', 'analyze', 'strategy', 'execute', 'outcome')
LABELS = {'collect': '采集', 'process': '处理', 'analyze': '分析', 'strategy': '策略', 'execute': '执行', 'outcome': '效果'}

# Thresholds: (warn, bad). Values beyond warn are warn, beyond bad are bad.
FEED_AGE_MINUTES = (5, 15)            # newest Latest-feed observation
COUNTER_AGE_MINUTES = (10, 30)        # newest counter snapshot
TASK_BACKLOG = (1000, 5000)           # due collection tasks not yet done
ANNOTATION_BACKLOG = (2000, 10000)    # posts without a current annotation
EXTRACTION_AGE_HOURS = (6, 24)        # only when fact extraction is enabled
RADAR_AGE_HOURS = (3, 6)              # radar runs every 2 hours
POST_SHARE = (0.4, 0.1)               # share of posts among the last published actions; lower is worse
BLOCKED_SHARE = (0.9, 0.97)           # share of recent posts that may not be targeted
FAILURE_RATE = (0.3, 0.6)             # failed / (completed + failed) decisions, 6 h
IDLE_HOURS = 2                        # no publication for this long while the team runs is bad
# Alerts (23 Oct): 12:00-14:19 half the decisions came back as invalid JSON (a
# connection without schema enforcement) and originals fell to 0-1 an hour,
# while the 6 h failure rate only read warn.
INVALID_OUTPUT_SHARE = (0.1, 0.2)     # decisions whose model answer was not valid JSON, last 30 min
INVALID_OUTPUT_MARK = '不是完整有效的 JSON'
MIN_DECISIONS_30M = 6                 # fewer decisions than this are not judged
LOW_POSTS_PER_HOUR = 5                # originals published in the last hour while the team is busy
BUSY_DECISIONS_PER_HOUR = 20
RECENT_ACTIONS = 50
MAX_REPLY_DEPTH = 7                   # agent_targeting.MAX_REPLY_DEPTH
RANK = {'ok': 0, 'warn': 1, 'bad': 2}

VIEW_CAP = 500                        # paid views per team per Sydney day
_CHANGE = re.compile(r'(\d+) (?:primary )?contributions? to (?:a )?changes? of mind(?: on vaccination)? '
                     r'(towards your side|away from your side|towards neither side)')
_VIEWS = re.compile(r'(\d+) views?\b')


def _parse(value):
    try:
        return datetime.fromisoformat(normalize_timestamp(value).replace('Z', '+00:00'))
    except (TypeError, ValueError, AttributeError):
        return None


def _minutes_since(value, now):
    at = _parse(value)
    # Small clock differences between writers must not produce negative ages.
    return round(max(0.0, (now - at).total_seconds()) / 60, 1) if at else None


def _level(value, limits, *, lower_is_worse=False):
    """'ok'/'warn'/'bad' for a value against (warn, bad) limits; None stays ok."""
    if value is None:
        return 'ok'
    warn, bad = limits
    if lower_is_worse:
        return 'bad' if value < bad else 'warn' if value < warn else 'ok'
    return 'bad' if value > bad else 'warn' if value > warn else 'ok'


def _worst(*levels):
    return max(levels, key=RANK.__getitem__)


def post_reply_ratio(posts, replies):
    """Originals per reply (target 2.0); with no replies it is the post count, None when both are 0."""
    if replies:
        return round(posts / replies, 2)
    return float(posts) if posts else None


def _shown(value):
    """A metric for a headline; missing values read as a dash, never 'None'."""
    return '—' if value is None else value


def _stage(status, headline, metrics, updated_at):
    return {'status': status, 'headline': headline, 'metrics': metrics, 'updated_at': updated_at}


def _collect(conn, now, stamp):
    feed = conn.execute("SELECT MAX(ctn_timestamp(observed_at)) FROM feed_observations "
                        "WHERE source IN ('latest','browser_latest') AND ctn_timestamp(observed_at)>=?",
                        (stamp(now - timedelta(days=1)),)).fetchone()[0]
    counter = conn.execute('SELECT MAX(ctn_timestamp(observed_at)) FROM counter_snapshots WHERE ctn_timestamp(observed_at)>=?',
                           (stamp(now - timedelta(days=1)),)).fetchone()[0]
    metrics = {
        'posts_total': conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0],
        'posts_last_hour': conn.execute('SELECT COUNT(*) FROM posts WHERE ctn_timestamp(created_at)>=?',
                                        (stamp(now - timedelta(hours=1)),)).fetchone()[0],
        'feed_age_minutes': _minutes_since(feed, now),
        'counter_age_minutes': _minutes_since(counter, now),
        # due_at is epoch seconds, unlike the ISO text timestamps elsewhere.
        'task_backlog': conn.execute("SELECT COUNT(*) FROM collection_tasks WHERE state='pending' AND due_at<=?",
                                     (now.timestamp(),)).fetchone()[0],
        'news_articles': conn.execute('SELECT COUNT(*) FROM news_articles').fetchone()[0]}
    status = _worst(_level(metrics['feed_age_minutes'], FEED_AGE_MINUTES),
                    _level(metrics['counter_age_minutes'], COUNTER_AGE_MINUTES),
                    _level(metrics['task_backlog'], TASK_BACKLOG), 'bad' if feed is None else 'ok')
    headline = (f"近 1 小时新增 {metrics['posts_last_hour']} 帖；最新采集 {metrics['feed_age_minutes']} 分钟前"
                if feed else '24 小时内没有 Latest 采集记录')
    return _stage(status, headline, metrics, feed)


def _process(conn, now, stamp, nav):
    from .annotations import classifier_version
    from .stance_labels import CLASSIFIER as MODEL_LABEL
    # Rows carrying a model label (D84) are current, not outdated keyword rows.
    backlog = conn.execute('''SELECT COUNT(*) FROM posts p LEFT JOIN post_annotations a ON a.post_id=p.post_id
        WHERE a.post_id IS NULL OR a.classifier NOT IN (?,?)''', (classifier_version(), MODEL_LABEL)).fetchone()[0]
    latest = conn.execute('SELECT MAX(updated_at) FROM post_annotations').fetchone()[0]
    extraction = conn.execute('SELECT MAX(updated_at) FROM fact_extractions').fetchone()[0]
    facts = (nav or {}).get('facts') or {}
    extraction_enabled = facts.get('extraction_enabled')
    age = _minutes_since(extraction, now)
    metrics = {
        'annotated_posts': conn.execute('SELECT COUNT(*) FROM post_annotations').fetchone()[0],
        'annotation_backlog': backlog,
        'bot_accounts': conn.execute('SELECT COUNT(*) FROM account_annotations WHERE is_bot=1').fetchone()[0],
        'pending_facts': conn.execute("SELECT COUNT(*) FROM facts WHERE status='pending'").fetchone()[0],
        'fact_extraction_enabled': extraction_enabled,
        'last_fact_extraction_at': extraction,
        'fact_extraction_age_hours': round(age / 60, 1) if age is not None else None}
    status = _worst(_level(backlog, ANNOTATION_BACKLOG),
                    _level(metrics['fact_extraction_age_hours'], EXTRACTION_AGE_HOURS) if extraction_enabled else 'ok')
    headline = f"标注积压 {backlog} 帖；机器人账号 {metrics['bot_accounts']} 个；待审事实 {metrics['pending_facts']} 条"
    return _stage(status, headline, metrics, latest)


def _analyze(conn, now, stamp, blue):
    counts = Counter({row['stance']: row['n'] for row in conn.execute(
        '''SELECT pa.stance,COUNT(*) AS n FROM post_annotations pa
           LEFT JOIN team_accounts t ON t.account_id=pa.author_id
           LEFT JOIN account_annotations aa ON aa.account_id=pa.author_id
           WHERE ctn_timestamp(pa.created_at)>=? AND COALESCE(t.is_own,0)=0 AND COALESCE(aa.is_bot,0)=0
           GROUP BY pa.stance''', (stamp(now - timedelta(hours=6)),))})
    row = conn.execute("SELECT value FROM checkpoints WHERE key='agent_content_research_v1'").fetchone()
    briefs = (json.loads(row['value']).get('briefs') or []) if row else []
    radar = max((brief.get('generated_at') for brief in briefs
                 if brief.get('kind') == 'concern_radar' and brief.get('generated_at')), default=None)
    radar_age = _minutes_since(radar, now)
    sided = counts['yes'] + counts['no']
    plan = (blue or {}).get('plan') or {}
    metrics = {
        'undecided_posts_6h': counts['undecided'],
        'yes_share_6h': round(counts['yes'] / sided, 3) if sided else None,
        'citizen_posts_6h': sum(counts.values()),
        'radar_generated_at': radar,
        'radar_age_hours': round(radar_age / 60, 1) if radar_age is not None else None,
        # Only when the Blue page computed candidates recently; never computed here.
        'blue_plan_today': len(plan.get('items') or []) if blue else None}
    status = _level(metrics['radar_age_hours'], RADAR_AGE_HOURS) if radar else 'warn'
    yes = f"{metrics['yes_share_6h']:.0%}" if metrics['yes_share_6h'] is not None else '—'
    radar_text = f"雷达 {metrics['radar_age_hours']} 小时前" if radar_age is not None else '没有雷达报告'
    headline = f"6 小时犹豫帖 {counts['undecided']} 条；公民 Yes 占比 {yes}；{radar_text}"
    return _stage(status, headline, metrics, radar)


def _strategy(conn, now, stamp):
    actions = Counter(row['action'] for row in conn.execute(
        'SELECT action FROM post_attribution ORDER BY published_at DESC LIMIT ?', (RECENT_ACTIONS,)))
    latest = conn.execute('SELECT MAX(published_at) FROM post_attribution').fetchone()[0]
    pool = conn.execute(
        '''SELECT COUNT(*) AS total,
             SUM(CASE WHEN COALESCE(aa.is_bot,0)=1 OR COALESCE(pa.thread_depth,0)>=? OR pa.stance='yes' THEN 1 ELSE 0 END) AS blocked
           FROM post_annotations pa LEFT JOIN team_accounts t ON t.account_id=pa.author_id
           LEFT JOIN account_annotations aa ON aa.account_id=pa.author_id
           WHERE ctn_timestamp(pa.created_at)>=? AND COALESCE(t.is_own,0)=0''',
        (MAX_REPLY_DEPTH, stamp(now - timedelta(hours=6)))).fetchone()
    total = sum(actions.values())
    post_share = round(actions['post'] / total, 3) if total else None
    blocked = round((pool['blocked'] or 0) / pool['total'], 3) if pool['total'] else None
    metrics = {
        'active_facts': conn.execute("SELECT COUNT(*) FROM facts WHERE status='active'").fetchone()[0],
        'recent_actions': total, 'recent_posts': actions['post'], 'recent_replies': actions['reply'],
        'post_share': post_share,
        # Proxy for agent_targeting's rules: bots, deep threads and Yes posts are not targets.
        'target_pool_6h': pool['total'], 'target_blocked_share': blocked}
    status = _worst(_level(post_share, POST_SHARE, lower_is_worse=True), _level(blocked, BLOCKED_SHARE))
    share = f'{post_share:.0%}' if post_share is not None else '—'
    headline = f"最近 {total} 次发布：原创 {actions['post']}、回复 {actions['reply']}（原创占 {share}）；生效事实 {metrics['active_facts']} 条"
    return _stage(status, headline, metrics, latest)


def _execute(conn, now, stamp, nav):
    six = stamp(now - timedelta(hours=6))
    kinds = Counter({row['kind']: row['n'] for row in conn.execute(
        '''SELECT kind,COUNT(*) AS n FROM agent_audit_events WHERE ctn_timestamp(at)>=?
           AND kind IN ('generation_completed','generation_failed','published') GROUP BY kind''', (six,))})
    hour = Counter({row['kind']: row['n'] for row in conn.execute(
        '''SELECT kind,COUNT(*) AS n FROM agent_audit_events WHERE ctn_timestamp(at)>=?
           AND kind IN ('generation_completed','published') GROUP BY kind''', (stamp(now - timedelta(hours=1)),))})
    last_publish = conn.execute("SELECT MAX(at) FROM agent_audit_events WHERE kind='published' AND ctn_timestamp(at)>=?",
                                (stamp(now - timedelta(days=1)),)).fetchone()[0]
    failures = [row['message'] for row in conn.execute(
        "SELECT message FROM agent_audit_events WHERE kind='generation_failed' AND ctn_timestamp(at)>=?", (six,))]
    durations, llm_errors, llm_total = {}, 0, 0
    for row in conn.execute('SELECT purpose,status,duration_ms FROM llm_calls WHERE ctn_timestamp(started_at)>=?', (six,)):
        llm_total += 1
        llm_errors += row['status'] != 'ok'
        if row['status'] == 'ok' and row['duration_ms'] is not None:
            durations.setdefault(row['purpose'], []).append(row['duration_ms'])
    mix = Counter({row['action']: row['n'] for row in conn.execute(
        'SELECT action,COUNT(*) AS n FROM post_attribution WHERE ctn_timestamp(published_at)>=? GROUP BY action', (six,))})
    ratio = post_reply_ratio(mix['post'], mix['reply'])
    decided = kinds['generation_completed'] + kinds['generation_failed']
    agents = (nav or {}).get('agents') or {}
    idle = _minutes_since(last_publish, now)
    metrics = {
        'decisions_last_hour': hour['generation_completed'], 'published_last_hour': hour['published'],
        'decisions_6h': kinds['generation_completed'], 'published_6h': kinds['published'],
        'failure_rate_6h': round(kinds['generation_failed'] / decided, 3) if decided else None,
        'top_failures_6h': [{'reason': reason[:160], 'count': count} for reason, count in Counter(failures).most_common(3)],
        'step_median_ms': {purpose: round(statistics.median(values)) for purpose, values in sorted(durations.items())},
        'llm_error_rate_6h': round(llm_errors / llm_total, 3) if llm_total else None,
        'pending_actions': agents.get('pending_actions'), 'inbox_unresolved': agents.get('inbox_unresolved'),
        'minutes_since_publish': idle,
        'posts_6h': mix['post'], 'replies_6h': mix['reply'],
        'post_reply_ratio_6h': ratio}
    recent = [(row['kind'], row['message'] or '') for row in conn.execute(
        '''SELECT kind,message FROM agent_audit_events WHERE ctn_timestamp(at)>=? AND ctn_timestamp(at)<=?
           AND kind IN ('generation_completed','generation_failed')''', (stamp(now - timedelta(minutes=30)), stamp(now)))]
    invalid = sum(kind == 'generation_failed' and INVALID_OUTPUT_MARK in message for kind, message in recent)
    hour_rows = [(row['kind'], row['details_json']) for row in conn.execute(
        '''SELECT kind,details_json FROM agent_audit_events WHERE ctn_timestamp(at)>=? AND ctn_timestamp(at)<=?
           AND kind IN ('generation_completed','generation_failed','published')''', (stamp(now - timedelta(hours=1)), stamp(now)))]
    # Published events carry the action at once; post_attribution lags by minutes.
    posts_hour = sum(kind == 'published' and _action(details) == 'post' for kind, details in hour_rows)
    attempts_hour = sum(kind != 'published' for kind, _ in hour_rows)
    metrics.update(invalid_output_30m=invalid, decisions_30m=len(recent),
                   invalid_output_share_30m=round(invalid / len(recent), 3) if len(recent) >= MIN_DECISIONS_30M else None,
                   posts_last_hour=posts_hour, attempts_last_hour=attempts_hour)
    low_posts = attempts_hour >= BUSY_DECISIONS_PER_HOUR and posts_hour < LOW_POSTS_PER_HOUR
    alerts = []
    if _level(metrics['invalid_output_share_30m'], INVALID_OUTPUT_SHARE) != 'ok':
        alerts.append(f"近 30 分钟 {invalid}/{len(recent)} 次决策的模型输出不是有效 JSON"
                      f"（{metrics['invalid_output_share_30m']:.0%}），检查角色用的 AI 连接")
    if low_posts:
        alerts.append(f"近 1 小时尝试 {attempts_hour} 次，只发了 {posts_hour} 条原创帖")
    # Roles close to suspension by hidden posts (hidden_posts, D90): the sidebar's
    # Agent 执行 dot shows them; the role and account pages read the same list.
    from .hidden_posts import risk_accounts
    risky, metrics['hidden_posts'] = risk_accounts(conn)
    metrics['risk_accounts'] = risky
    alerts.extend(risk['text'] for risk in risky)
    metrics['alerts'] = alerts
    stalled = bool(agents.get('team_enabled')) and (idle is None or idle > IDLE_HOURS * 60)
    # The 2:1 post/reply rule was cancelled (D52); the ratio is shown but no longer judged.
    status = _worst(_level(metrics['failure_rate_6h'], FAILURE_RATE), 'bad' if stalled else 'ok',
                    _level(metrics['invalid_output_share_30m'], INVALID_OUTPUT_SHARE), 'bad' if low_posts else 'ok',
                    'warn' if risky else 'ok')
    rate = f"{metrics['failure_rate_6h']:.0%}" if metrics['failure_rate_6h'] is not None else '—'
    headline = (''.join('⚠ ' + alert + '；' for alert in alerts)
                + f"近 1 小时决策 {hour['generation_completed']} 次、发布 {hour['published']} 次；6 小时失败率 {rate}；"
                f"原创:回复 {mix['post']}:{mix['reply']}")
    return _stage(status, headline, metrics, last_publish)


def _action(details):
    try:
        value = json.loads(details or '{}')
    except ValueError:
        return None
    return value.get('action') if isinstance(value, dict) else None


def _sydney_day_start(now):
    from zoneinfo import ZoneInfo
    local = now.astimezone(ZoneInfo('Australia/Sydney'))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def citizen_replies(conn, since, db_path):
    """Replies to our accounts' posts since ``since`` by citizen-proxy authors.

    Returns (citizen replies, all replies to us). The proxy is applied only to
    the authors who replied, so the cost follows our reply volume. When the
    proxy cannot judge, the citizen count is None: a failing check would
    otherwise pass every replier and report all replies as citizen replies.
    """
    replies = conn.execute('''SELECT r.post_id,r.author_id FROM posts r
        JOIN posts p ON p.post_id=r.reply_to_post_id
        JOIN team_accounts t ON t.account_id=p.author_id AND t.is_own=1
        WHERE ctn_timestamp(r.created_at)>=? AND r.author_id IS NOT NULL''', (since,)).fetchall()
    from .citizens import citizen_authors
    try:
        citizens = citizen_authors(db_path, {row['author_id'] for row in replies}, strict=True)
    except Exception as exc:
        LOGGER.warning('Citizen replies unknown: the citizen check failed (%s)', type(exc).__name__)
        return None, len(replies)
    return sum(row['author_id'] in citizens for row in replies), len(replies)


def _outcome(conn, now, stamp, db_path):
    from .scoring import score_pace
    pace = score_pace(db_path, normalize_timestamp(now))
    six, day_start = now - timedelta(hours=6), _sydney_day_start(now)
    views, views_today, changes, changes_24h = 0, 0, Counter(), Counter()
    from .official_feed import pending_rows
    exported = conn.execute("SELECT occurred_at,message FROM official_activity WHERE track='red' AND occurred_at IS NOT NULL "
                            'AND ctn_timestamp(occurred_at)>=?', (stamp(min(now - timedelta(hours=24), day_start)),)).fetchall()
    # The newest reports come from the live activity table until the CSV export has them.
    for row in exported + [row for row in pending_rows(conn) if row['track'] == 'red']:
        at, message = _parse(row['occurred_at']), row['message'] or ''
        if at is None:
            continue
        counted = sum(int(value) for value in _VIEWS.findall(message))
        found = [(direction, int(count)) for count, direction in _CHANGE.findall(message)]
        if at >= day_start:
            views_today += counted
        if at >= now - timedelta(hours=24):
            for direction, count in found:
                changes_24h[direction] += count
        if at >= six:
            views += counted
            for direction, count in found:
                changes[direction] += count
    share = lambda counts: (round(counts['towards your side'] / sum(counts.values()), 3) if sum(counts.values()) else None)
    citizen_6h, replies_6h = citizen_replies(conn, stamp(six), db_path)
    citizen_24h, replies_24h = citizen_replies(conn, stamp(now - timedelta(hours=24)), db_path)
    attribution = conn.execute('SELECT COUNT(*) AS total,COUNT(outcome_at) AS settled FROM post_attribution').fetchone()
    poll = (pace.get('polls') or [None])[0]
    rate = lambda key: ((pace.get('rates') or {}).get(key) or {}).get('points_per_hour')
    required = poll['required_points_per_hour_now'] if poll else None
    metrics = {
        'points_per_hour_6h': rate('6h'), 'points_per_hour_12h': rate('12h'),
        'required_points_per_hour': required, 'next_poll': poll['poll'] if poll else None,
        'gap_to_threshold': poll['gap'] if poll else None, 'on_track': poll['on_track'] if poll else None,
        'total': pace.get('total'), 'score_stale': pace.get('stale'),
        'views_per_hour_6h': round(views / 6, 1),
        'changes_towards_6h': changes['towards your side'], 'changes_away_6h': changes['away from your side'],
        'changes_neither_6h': changes['towards neither side'],
        # Share of all official changes of mind that moved towards our side.
        'towards_share_6h': share(changes), 'towards_share_24h': share(changes_24h),
        'views_today': views_today, 'view_cap': VIEW_CAP, 'view_cap_used': round(views_today / VIEW_CAP, 3),
        # Replies to our posts from citizen-proxy accounts (deep-analysis calibration).
        'citizen_replies_6h': citizen_6h, 'replies_to_us_6h': replies_6h,
        'citizen_replies_24h': citizen_24h, 'replies_to_us_24h': replies_24h,
        'attribution_total': attribution['total'], 'attribution_settled': attribution['settled']}
    if required is None or required == 0:
        status = 'ok'
    elif metrics['points_per_hour_12h'] is not None and metrics['points_per_hour_12h'] < required:
        status = 'bad'
    elif metrics['points_per_hour_6h'] is not None and metrics['points_per_hour_6h'] < required:
        status = 'warn'
    else:
        status = 'ok'
    if pace.get('stale'):
        status = _worst(status, 'warn')
    speed = metrics['points_per_hour_12h']
    tail = f"今日浏览额度 {views_today}/{VIEW_CAP}；6 小时市民回复 {_shown(citizen_6h)} 条"
    headline = (f"12 小时 {_shown(speed)} 分/时，Poll {poll['poll']} 需要 {_shown(required)} 分/时；{tail}" if poll else tail)
    return _stage(status, headline, metrics, pace.get('observed_at'))


def build(db_path, *, nav=None, blue=None, now=None):
    now = _parse(now) if now else datetime.now(timezone.utc)
    stamp = normalize_timestamp
    nav_value = None
    try:
        nav_value = nav.get() if nav is not None else None
    except Exception:
        pass
    blue_value = None
    try:
        blue_value = blue.peek() if blue is not None else None
    except Exception:
        pass
    computations = {'collect': lambda conn: _collect(conn, now, stamp),
                    'process': lambda conn: _process(conn, now, stamp, nav_value),
                    'analyze': lambda conn: _analyze(conn, now, stamp, blue_value),
                    'strategy': lambda conn: _strategy(conn, now, stamp),
                    'execute': lambda conn: _execute(conn, now, stamp, nav_value),
                    'outcome': lambda conn: _outcome(conn, now, stamp, db_path)}
    stages = []
    for key in STAGES:
        # Each stage reads on its own connection; one failure leaves the others intact.
        try:
            conn = connect(db_path)
            try:
                stage = computations[key](conn)
            finally:
                conn.close()
        except Exception as exc:
            stage = {'status': 'unknown', 'headline': '状态读取失败', 'metrics': {}, 'updated_at': None,
                     'error': type(exc).__name__}
        stages.append({'key': key, 'label': LABELS[key], **stage})
    known = [stage['status'] for stage in stages if stage['status'] in RANK]
    return {'generated_at': utc_now(), 'overall': _worst(*known) if known else 'unknown', 'stages': stages,
            'thresholds': {'feed_age_minutes': FEED_AGE_MINUTES, 'counter_age_minutes': COUNTER_AGE_MINUTES,
                           'task_backlog': TASK_BACKLOG, 'annotation_backlog': ANNOTATION_BACKLOG,
                           'fact_extraction_age_hours': EXTRACTION_AGE_HOURS, 'radar_age_hours': RADAR_AGE_HOURS,
                           'post_share': POST_SHARE, 'target_blocked_share': BLOCKED_SHARE,
                           'failure_rate_6h': FAILURE_RATE, 'idle_hours': IDLE_HOURS,
                           'invalid_output_share_30m': INVALID_OUTPUT_SHARE,
                           'low_posts_per_hour': LOW_POSTS_PER_HOUR,
                           'busy_decisions_per_hour': BUSY_DECISIONS_PER_HOUR,
                           'view_cap': VIEW_CAP}}


class PipelineStatus:
    def __init__(self, db_path, *, nav=None, blue=None):
        self.db_path, self.nav, self.blue = db_path, nav, blue
        self._lock = threading.Lock()
        self._cached = None

    def get(self):
        cached = self._cached
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        if not self._lock.acquire(blocking=cached is None):
            return cached[1]
        try:
            cached = self._cached
            if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
                return cached[1]
            value = build(self.db_path, nav=self.nav, blue=self.blue)
            self._cached = (time.monotonic(), value)
            return value
        finally:
            self._lock.release()
