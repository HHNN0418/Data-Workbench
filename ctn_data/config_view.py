"""Read-only views of the rules and settings in force, and of maintenance history.

  strategy()     GET /api/strategy/rules        the mix, citizen-only targeting, public-figure
                                                 mentions, shift schedule and reply delay, read
                                                 from the code constants the Agents act on
  settings()     GET /api/settings/summary      team settings and roles, with where each is edited
  maintenance()  GET /api/maintenance/history   reloads from reports/generated/unified-*-deployment.json,
                                                 the current Agent maintenance hold, retention, backups

Values come straight from the modules that enforce them (agent_decision,
agent_targeting, agent_schedule, agent_control), so the pages cannot drift
from the code. Settings are read from the Agent store rows in meta without
constructing AgentStore. Nothing here writes, reloads or deletes anything.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import agent_decision, agent_schedule, agent_targeting
from .db import connect, utc_now
from .imports import normalize_timestamp
from .results_view import _parse

PREFIX = 'agent_control_v1:'
RECENT_ACTIONS = 50
REPORT_GLOB = 'unified-*-deployment.json'
REPORT_FIELDS = ('started_at', 'purpose', 'result', 'maintenance_phase', 'failed_stage', 'error_type',
                 'maintenance_started_at', 'drained_at', 'ready_at', 'finished_at')
MAINTENANCE_EVENTS = ('maintenance_started', 'maintenance_completed', 'maintenance_aborted')
BACKUP_NAME = re.compile(r'ctn-(?:pg-)?\d{8}T\d{12}Z\.sqlite3')
BACKUPS_SHOWN = 5
EVENTS_SHOWN = 20


def _meta(conn, key):
    row = conn.execute('SELECT value FROM meta WHERE key=?', (PREFIX + key,)).fetchone()
    try:
        value = json.loads(row[0]) if row else {}
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


def _minutes(seconds):
    return round(seconds / 60, 1)


def _rule(key, title, value, text):
    return {'key': key, 'title': title, 'value': value, 'text': text}


def _rules():
    t = agent_targeting
    low, high = _reply_delay()
    from .annotations import FEATURES_FOR_BOT
    mix = [
        _rule('answer_first', '市民回复我们时先回', True,
              '有市民回复了我们的帖子，这一轮一定先回复他，不受下面的配比限制'),
        _rule('originals_per_reply', '原创与回复配比', agent_decision.ORIGINALS_PER_REPLY,
              f'除此之外，每个角色每写 {agent_decision.ORIGINALS_PER_REPLY} 条原创，配 1 条回复'),
        _rule('precise_target_seconds', '配比里的回复只给精准目标', agent_decision.PRECISE_TARGET_SECONDS,
              f'这条回复只给回复机会队列里的帖子，或发出不到 {_minutes(agent_decision.PRECISE_TARGET_SECONDS):g} '
              '分钟的市民帖；没有精准目标就写原创'),
        _rule('reply_delay_seconds', '回复发送前的等待', [low, high],
              f'自动回复生成后随机等 {_minutes(low):g}–{_minutes(high):g} 分钟再发' if high else
              '自动回复写好就发，不再故意等待：NPC 发帖后 2 分钟内的回复，NPC 回过来的比例最高（D72）'),
    ]
    targeting = [
        _rule('persuadable_stances', '只回复可以被说服的市民', list(t.PERSUADABLE_STANCES),
              '只回复态度犹豫或偏 No 的帖子；偏 Yes 和与公投无关的帖子不回（市民回复我们时例外）'),
        _rule('citizen_proxy', '只回复市民 NPC',
              {'max_writes': t.CITIZEN_MAX_WRITES, 'recent_hours': t.CITIZEN_RECENT_HOURS,
               'recent_max_writes': t.CITIZEN_RECENT_MAX_WRITES},
              f'市民：已采集的动作不超过 {t.CITIZEN_MAX_WRITES} 条，或 {t.CITIZEN_RECENT_HOURS} 小时内不超过 '
              f'{t.CITIZEN_RECENT_MAX_WRITES} 条；转发圈、批量编号用户名和 AI 套话账号不算市民'),
        _rule('behavior_bots', '机器人：行为规则',
              {'window_hours': t.BEHAVIOR_WINDOW_HOURS, 'min_received': t.BEHAVIOR_MIN_RECEIVED,
               'answer_rate': t.BEHAVIOR_ANSWER_RATE, 'deep_depth': t.BEHAVIOR_DEEP_DEPTH,
               'deep_posts': t.BEHAVIOR_DEEP_POSTS},
              f'{t.BEHAVIOR_WINDOW_HOURS} 小时内收到至少 {t.BEHAVIOR_MIN_RECEIVED} 条回复且回答了 '
              f'{t.BEHAVIOR_ANSWER_RATE:.0%} 以上，或有 {t.BEHAVIOR_DEEP_POSTS} 次以上在第 {t.BEHAVIOR_DEEP_DEPTH} '
              '层以下回帖，就算机器人，不回复'),
        _rule('feature_bots', '机器人：全天候特征',
              {'active_hours': t.FEATURE_ACTIVE_HOURS, 'daily_writes': t.FEATURE_DAILY_WRITES,
               'hourly_writes': t.FEATURE_HOURLY_WRITES, 'fast_reply_seconds': t.FEATURE_FAST_REPLY_SECONDS,
               'rhythm_cv': t.FEATURE_RHYTHM_CV, 'features_for_bot': FEATURES_FOR_BOT},
              f'24 小时里 {t.FEATURE_ACTIVE_HOURS} 个小时以上在发、一天超过 {t.FEATURE_DAILY_WRITES} 条或某小时超过 '
              f'{t.FEATURE_HOURLY_WRITES} 条、回复中位时间不到 {t.FEATURE_FAST_REPLY_SECONDS // 60} 分钟、发帖间隔非常规律：'
              f'同时有 {FEATURES_FOR_BOT} 项就算机器人'),
        _rule('max_reply_depth', '线程层数上限', t.MAX_REPLY_DEPTH,
              f'原帖是第 0 层；对方帖子在第 {t.MAX_REPLY_DEPTH} 层或更深时不回复（市民很少回第 8 条以后的消息）'),
        _rule('max_team_replies_per_thread', '同一线程的回复上限', t.MAX_TEAM_REPLIES_PER_THREAD,
              f'整个队伍在同一个线程里最多回复 {t.MAX_TEAM_REPLIES_PER_THREAD} 次，来回太长会被当成机器人'),
        _rule('crowded_competitor_replies', '别队已经扎堆的帖子不回', t.CROWDED_COMPETITOR_REPLIES,
              f'已有 {t.CROWDED_COMPETITOR_REPLIES} 条以上别队回复的帖子不回复（这种帖子市民几乎不再回应）'),
        _rule('fresh_post_seconds', '新帖优先', t.FRESH_POST_SECONDS,
              f'发出不到 {_minutes(t.FRESH_POST_SECONDS):g} 分钟、还没有别队抢答的市民帖是最好的回复目标'),
        _rule('hot_post_views', '浏览太少的帖子不主动回复', t.HOT_POST_VIEWS,
              f'浏览少于 {t.HOT_POST_VIEWS} 的帖子不主动回复（市民回复我们、回复机会队列里的帖子除外），'
              '这些人用原创帖更容易覆盖到'),
        _rule('answer_window_seconds', '同一顾虑不重复回答', t.ANSWER_WINDOW_SECONDS,
              f'同一位市民的同一个顾虑，{_minutes(t.ANSWER_WINDOW_SECONDS) / 60:g} 小时内只回答一次'),
        _rule('citizen_owner_seconds', '每位市民每天一个角色', t.CITIZEN_OWNER_SECONDS,
              f'每位市民 {_minutes(t.CITIZEN_OWNER_SECONDS) / 60:g} 小时内只由我方一个角色回复'),
    ]
    return mix, targeting


def _reply_delay():
    from .agent_control import REPLY_DELAY_SECONDS
    return REPLY_DELAY_SECONDS


def _figures():
    order = getattr(agent_targeting, '_FIGURE_ORDER', {})
    groups = {}
    for concern, group in agent_targeting.FIGURE_GROUP.items():
        groups.setdefault(group, []).append(concern)
    rows = []
    for name, info in agent_targeting.PUBLIC_FIGURES.items():
        first = sorted(group for group, names in order.items() if names and names[0] == name)
        backup = sorted(group for group, names in order.items() if name in names[1:])
        rows.append({'username': name, 'who': info.get('who'), 'how': info.get('how'),
                     'first_for': first, 'second_for': backup,
                     'concerns': sorted(concern for group in first for concern in groups.get(group, []))})
    return {'figures': rows, 'groups': {group: sorted(concerns) for group, concerns in groups.items()},
            'order': {group: list(names) for group, names in order.items()},
            'rule': '原创帖 @ 一位公众人物，按这条帖子的顾虑选人，并且和这个角色上一条原创 @ 的人不同；不再 @ 市民'}


def _hour_limits(schedule, group):
    """Per Sydney hour 0-23: the group's hourly cap at the start of that hour, or None off shift."""
    covers = agent_schedule._covers
    limits = []
    for hour in range(24):
        minute = hour * 60
        peak = any(covers(window, minute) for window in schedule.get('peaks', []))
        values = [shift['max_per_hour'] if peak or shift.get('off_peak_per_hour') is None
                  else min(shift['off_peak_per_hour'], shift['max_per_hour'])
                  for shift in schedule['shifts'] if shift['group'] == group and covers(shift, minute)]
        limits.append(max(values) if values else None)
    return limits


def _schedule(settings, agents, now):
    schedule = agent_schedule.configuration({'settings': settings})
    by_account = {agent.get('account_id'): agent for agent in agents.values() if isinstance(agent, dict)}
    ts = now.timestamp()
    groups = []
    for name, accounts in schedule['groups'].items():
        shift = agent_schedule.current_shift(schedule, accounts[0], ts) if accounts else None
        groups.append({'group': name,
                       'members': [{'account_id': account, 'name': (by_account.get(account) or {}).get('name'),
                                    'username': (by_account.get(account) or {}).get('username'),
                                    'enabled': (by_account.get(account) or {}).get('enabled')} for account in accounts],
                       'on_shift': shift is not None,
                       'current_limit': agent_schedule.hourly_limit(schedule, shift, ts) if shift else None,
                       'hour_limits': _hour_limits(schedule, name)})
    return {'timezone': schedule['timezone'], 'custom': bool((settings or {}).get(agent_schedule.SETTINGS_KEY)),
            'groups': groups, 'shifts': schedule['shifts'], 'peaks': schedule.get('peaks', []),
            'peak_now': agent_schedule.is_peak(schedule, ts),
            'publish_grace_seconds': agent_schedule.PUBLISH_GRACE_SECONDS,
            'unscheduled': sorted(agent.get('name') or key for key, agent in agents.items() if isinstance(agent, dict)
                                  and agent.get('account_id') not in {a for accounts in schedule['groups'].values()
                                                                      for a in accounts})}


def strategy(db_path, *, now=None):
    now = _parse(now) if now else datetime.now(timezone.utc)
    conn = connect(db_path)
    try:
        settings, agents = _meta(conn, 'settings'), _meta(conn, 'agents')
        recent = Counter(row['action'] for row in conn.execute(
            'SELECT action FROM post_attribution ORDER BY published_at DESC LIMIT ?', (RECENT_ACTIONS,)))
    finally:
        conn.close()
    mix, targeting = _rules()
    allowed = Counter(action for agent in agents.values() if isinstance(agent, dict) and agent.get('enabled')
                      for action in agent.get('allowed_actions') or ())
    total = sum(recent.values())
    return {'generated_at': utc_now(), 'mix': mix, 'targeting': targeting, 'public_figures': _figures(),
            'schedule': _schedule(settings, agents, now),
            'allowed_actions': {'roles_enabled': sum(1 for agent in agents.values()
                                                     if isinstance(agent, dict) and agent.get('enabled')),
                                'counts': dict(allowed)},
            'recent_mix': {'actions': total, 'posts': recent['post'], 'replies': recent['reply'],
                           'post_share': round(recent['post'] / total, 3) if total else None,
                           'originals_per_reply': round(recent['post'] / recent['reply'], 2) if recent['reply'] else None},
            'read_only': True}


def _providers(providers):
    try:
        public = providers.list_public() if providers is not None else {}
    except Exception:
        public = {}
    rows = {row.get('id'): row for row in public.get('providers') or [] if isinstance(row, dict)}
    return rows, public.get('default_provider_id')


def _provider(rows, provider_id):
    row = rows.get(provider_id) or {}
    return {'provider_id': provider_id, 'provider_name': row.get('name'), 'provider_model': row.get('model'),
            'provider_enabled': row.get('enabled')}


def settings(db_path, *, nav=None, providers=None):
    from .agent_control import DEFAULT_SETTINGS
    conn = connect(db_path)
    try:
        stored, agents = _meta(conn, 'settings'), _meta(conn, 'agents')
    finally:
        conn.close()
    team = {**DEFAULT_SETTINGS, **stored}
    schedule = agent_schedule.configuration({'settings': stored})
    group_of = {account: name for name, accounts in schedule['groups'].items() for account in accounts}
    rows, default = _providers(providers)
    roles = []
    for key, agent in agents.items():
        if not isinstance(agent, dict):
            continue
        provider_id = agent.get('provider_id') or default
        roles.append({'id': key, 'name': agent.get('name'), 'username': agent.get('username'),
                      'account_id': agent.get('account_id'), 'enabled': bool(agent.get('enabled')),
                      'mode': agent.get('mode'), 'group': group_of.get(agent.get('account_id')),
                      'reading_selection_mode': agent.get('reading_selection_mode') or 'rules',
                      **_provider(rows, provider_id), 'provider_missing': bool(provider_id) and provider_id not in rows
                      and bool(rows),
                      'allowed_actions': list(agent.get('allowed_actions') or []),
                      'daily_limit': agent.get('daily_limit'), 'interval_seconds': agent.get('interval_seconds'),
                      'stance': agent.get('stance'), 'language': agent.get('language'),
                      'has_error': bool(agent.get('last_error'))})
    roles.sort(key=lambda role: (role['group'] or '~', str(role['name'] or '')))
    review = team.get('ai_review') if isinstance(team.get('ai_review'), dict) else {}
    facts = team.get('fact_bank') if isinstance(team.get('fact_bank'), dict) else {}
    status = {}
    try:
        status = nav.get() if nav is not None else {}
    except Exception:
        status = {}
    return {'generated_at': utc_now(),
            'team': {key: team.get(key) for key in ('enabled', 'team_side', 'max_accounts', 'global_interval_seconds',
                                                    'timezone', 'block_agent_interactions')},
            'review': {'enabled': review.get('enabled'), 'english_only': review.get('english_only'),
                       **_provider(rows, review.get('provider_id'))},
            'fact_bank': {'review_mode': facts.get('review_mode', 'manual'), 'extract_enabled': bool(facts.get('extract_enabled')),
                          'pipeline': facts.get('pipeline', 'single'), **_provider(rows, facts.get('provider_id'))},
            'providers': {'count': len(rows), 'default': _provider(rows, default)},
            'roles': roles,
            'summary': {'roles': len(roles), 'enabled': sum(role['enabled'] for role in roles),
                        'groups': dict(Counter(role['group'] or 'none' for role in roles)),
                        'reading_modes': dict(Counter(role['reading_selection_mode'] for role in roles)),
                        'providers': dict(Counter(role['provider_name'] or role['provider_id'] or 'none' for role in roles))},
            'status': {key: status.get(key) for key in ('agents', 'ai', 'review', 'facts', 'research',
                                                       'collection', 'score_sync')},
            'read_only': True}


def _seconds(start, end):
    a, b = _parse(start), _parse(end)
    return round((b - a).total_seconds(), 1) if a and b else None


def reload_history(reports_dir):
    entries = []
    for path in Path(reports_dir).glob(REPORT_GLOB):
        name = path.name[:-len('-deployment.json')]
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            entries.append({'name': name, 'unreadable': True})
            continue
        if not isinstance(data, dict):
            entries.append({'name': name, 'unreadable': True})
            continue
        entry = {'name': name, **{field: data.get(field) for field in REPORT_FIELDS}}
        # PowerShell writes local times with seven fractional digits; serve UTC like every other API.
        for field in REPORT_FIELDS:
            if field.endswith('_at') and _parse(entry[field]):
                entry[field] = normalize_timestamp(entry[field])
        entry['ok'] = data.get('result') == 'ready'
        entry['drain_seconds'] = _seconds(data.get('started_at'), data.get('drained_at'))
        entry['total_seconds'] = _seconds(data.get('started_at'), data.get('finished_at'))
        entries.append(entry)
    entries.sort(key=lambda entry: (_parse(entry.get('started_at')) or datetime.min.replace(tzinfo=timezone.utc),
                                    entry['name']), reverse=True)
    return entries


def _backups(backup_dir):
    if backup_dir is None or not Path(backup_dir).is_dir():
        return []
    files = []
    for path in Path(backup_dir).iterdir():
        if BACKUP_NAME.fullmatch(path.name) and path.is_file():
            stat = path.stat()
            files.append({'name': path.name, 'bytes': stat.st_size, 'url': '/api/backups/' + path.name,
                          'modified_at': datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                          .isoformat(timespec='seconds').replace('+00:00', 'Z')})
    files.sort(key=lambda row: row['name'], reverse=True)
    return files[:BACKUPS_SHOWN]


def maintenance(db_path, *, reports_dir, backup_dir=None, retention=None):
    conn = connect(db_path)
    try:
        # Only the hold itself: the whole rate row is several hundred kB.
        stored = conn.execute("SELECT json_extract(value,'$.maintenance') FROM meta WHERE key=?",
                              (PREFIX + 'rate',)).fetchone()
        events = [dict(row) for row in conn.execute(
            'SELECT at,kind,message FROM agent_audit_events WHERE kind IN (' + ','.join('?' for _ in MAINTENANCE_EVENTS)
            + ') ORDER BY ctn_timestamp(at) DESC LIMIT ?', (*MAINTENANCE_EVENTS, EVENTS_SHOWN))]
    finally:
        conn.close()
    try:
        hold = json.loads(stored[0]) if stored and stored[0] else {}
    except (TypeError, ValueError):
        hold = {}
    hold = hold if isinstance(hold, dict) else {}
    history = reload_history(reports_dir)
    results = Counter(entry.get('result') or 'unknown' for entry in history if not entry.get('unreadable'))
    return {'generated_at': utc_now(),
            'state': {'active': hold.get('active') is True,
                      **{key: hold.get(key) for key in ('phase', 'reason', 'started_at', 'finished_at')}},
            'events': events, 'history': history,
            'history_summary': {'reloads': len(history), 'results': dict(results),
                                'last_ready_at': next((entry.get('finished_at') for entry in history if entry.get('ok')),
                                                      None)},
            'retention': {'retention_days': getattr(retention, 'retention_days', None),
                          'interval_seconds': getattr(retention, 'interval_seconds', None),
                          'last_result': getattr(retention, 'last_result', None)},
            'backups': _backups(backup_dir), 'read_only': True}
