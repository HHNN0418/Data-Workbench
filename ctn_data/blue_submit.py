"""File one planned Blue report from our reporter account (POST /api/blue/report).

The user files each report by hand: a button on the Blue page and a
confirmation dialog (D58, 「给我弄一个手动一键提交按钮，用一个账号…我自己确认」).
Only a post in today's plan may be reported (blue_reports.daily_plan: p at
or above the threshold, verified authors skipped, the per-cluster full-rate
cap), and each post once. An account is reported the same way (POST
/api/blue/report-account) once blue_view finds its account report ready.
Every attempt, submitted or failed, is logged in meta under SUBMITTED_KEY
('target' is 'post' or 'account'; entries before 24 Oct have no target and are
posts); blue_view feeds today's submitted reports back into the plan so caps
count them.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import blue_reports
from .db import begin_write, connect, utc_now

SUBMITTED_KEY = 'blue_reports_submitted'
# Simon Mercer's account files the team's reports.
REPORTER_ACCOUNT_ID = 6503
REASON = 'inauthentic'


def log(db_path):
    conn = connect(db_path)
    try:
        row = conn.execute('SELECT value FROM meta WHERE key=?', (SUBMITTED_KEY,)).fetchone()
    finally:
        conn.close()
    try:
        value = json.loads(row[0]) if row else []
    except (TypeError, ValueError):
        value = []
    return value if isinstance(value, list) else []


def today(db_path, now=None):
    """Every report attempt of the current Sydney day, newest first."""
    day = blue_reports.sydney_day(now)
    return [entry for entry in reversed(log(db_path)) if entry.get('day') == day]


def submitted_today(db_path, now=None):
    """Report records of the current Sydney day that the platform accepted."""
    return [entry for entry in today(db_path, now) if entry.get('status') == 'submitted']


def submitted(db_path):
    """All recorded reports accepted by the platform, across Sydney dates."""
    return [entry for entry in log(db_path) if entry.get('status') == 'submitted']


def history(db_path, *, limit=20, offset=0):
    """Paginated report attempts, newest first, including earlier dates."""
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ValueError('limit 必须为 1–100，offset 必须为非负整数')
    entries = sorted(log(db_path), key=lambda entry: str(entry.get('at') or ''), reverse=True)
    return {'reports': entries[offset:offset + limit], 'total': len(entries), 'limit': limit, 'offset': offset}


def _append(db_path, entry):
    conn = connect(db_path)
    try:
        with conn:
            begin_write(conn, 'blue_reports')
            row = conn.execute('SELECT value FROM meta WHERE key=?', (SUBMITTED_KEY,)).fetchone()
            try:
                entries = json.loads(row[0]) if row else []
            except (TypeError, ValueError):
                entries = []
            entries = [*(entries if isinstance(entries, list) else []), entry]
            conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (SUBMITTED_KEY, json.dumps(entries, ensure_ascii=False)))
    finally:
        conn.close()


def reporter(controller):
    """The ready role bound to REPORTER_ACCOUNT_ID, or None."""
    return next((agent for agent in controller.store.read()['agents'].values()
                 if agent.get('account_id') == REPORTER_ACCOUNT_ID and agent.get('account_state') == 'ready'), None)


def submit(db_path, controller, plan, post_id):
    """Report a planned post once, after a fresh platform availability check."""
    if type(post_id) is not int or post_id <= 0:
        raise ValueError('帖子 ID 无效')
    item = next((item for item in (plan.get('plan') or {}).get('items', []) if item.get('post_id') == post_id), None)
    if item is None:
        raise ValueError('这条帖子不在今天的 Blue 举报计划里')
    if any(entry.get('post_id') == post_id for entry in submitted(db_path)):
        raise ValueError('这条帖子已经举报过，请查看历史举报记录')
    if not item.get('suggested_note'):
        raise ValueError('这条帖子没有足够强的证据生成举报说明')
    agent = reporter(controller)
    if agent is None:
        raise ValueError(f'举报账号 {REPORTER_ACCOUNT_ID} 没有可用的角色')
    entry = {'target': 'post', 'post_id': post_id, 'author_id': item.get('author_id'), 'username': item.get('username'),
             'cluster_id': item.get('cluster_id'), 'style_group': item.get('style_group'), 'p': item.get('p'),
             'side': item.get('side'),
             'agent_id': agent['id'], 'account_id': REPORTER_ACCOUNT_ID, 'reason': REASON,
             'detail': item['suggested_note'], 'at': utc_now(), 'day': blue_reports.sydney_day()}
    try:
        from . import blue_availability
        from .agent_control import AgentError
        verification = blue_availability.check(db_path, [post_id], deadline_seconds=10)[post_id]
        entry['availability'] = verification
        if verification['status'] != 'available':
            reason = verification.get('error') or '平台核对未完成'
            message = ('帖子当前不存在或已不可见' if verification['status'] == 'unavailable'
                       else '暂时无法确认帖子仍可访问')
            raise AgentError(f'举报前核对失败：#{post_id} {message}；{reason}。本次未提交举报。', 409)
        response = controller.report_post(agent['id'], post_id, REASON, item['suggested_note'])
    except Exception as exc:
        _append(db_path, {**entry, 'status': 'failed', 'error': str(exc)})
        raise
    entry.update(status='submitted', response={key: response.get(key) for key in ('id', 'status', 'created_at')
                                               if key in response})
    _append(db_path, entry)
    return entry


def submit_account(db_path, controller, view, account_id):
    """Report a candidate account whose account report is ready (blue_view: hidden post, 100 actions, cap).

    The hidden posts are read again here, since the view may be minutes old;
    an account reported in the last ACCOUNT_REREPORT_HOURS waits for its ruling.
    """
    from .blue_view import ACCOUNT_REPORT_MIN_HIDDEN, ACCOUNT_REREPORT_HOURS, _recent
    from .risk_assessment import account_risk
    if type(account_id) is not int or account_id <= 0:
        raise ValueError('账号 ID 无效')
    account = next((row for row in view.get('accounts') or [] if row.get('account_id') == account_id), None)
    if account is None:
        raise ValueError('这个账号不在当前的 Blue 候选里，请先刷新列表')
    report = account.get('account_report') or {}
    if not report.get('eligible'):
        raise ValueError('这个账号还不满足账号举报条件：' + '、'.join(report.get('missing') or ['未知']))
    now = datetime.now(timezone.utc)
    if any(entry.get('target') == 'account' and entry.get('author_id') == account_id
           and _recent(entry.get('at'), now, ACCOUNT_REREPORT_HOURS) for entry in submitted(db_path)):
        raise ValueError(f'这个账号 {ACCOUNT_REREPORT_HOURS} 小时内已经举报过，等裁定后再看')
    conn = connect(db_path)
    try:
        hidden = account_risk(conn, [account_id]).get(account_id, {}).get('hidden', 0)
    finally:
        conn.close()
    if hidden < ACCOUNT_REPORT_MIN_HIDDEN:
        raise ValueError('这个账号已经没有确认隐藏的帖子，账号举报会被驳回')
    agent = reporter(controller)
    if agent is None:
        raise ValueError(f'举报账号 {REPORTER_ACCOUNT_ID} 没有可用的角色')
    entry = {'target': 'account', 'post_id': None, 'author_id': account_id, 'username': account.get('username'),
             'cluster_id': account.get('cluster_id'), 'style_group': account.get('style_group'), 'p': account.get('p'),
             'side': account.get('side'),
             'hidden': hidden, 'observed_actions': report.get('actions'),
             'agent_id': agent['id'], 'account_id': REPORTER_ACCOUNT_ID, 'reason': REASON,
             'detail': report['suggested_note'], 'at': utc_now(), 'day': blue_reports.sydney_day()}
    try:
        response = controller.report_account(agent['id'], account_id, REASON, report['suggested_note'])
    except Exception as exc:
        _append(db_path, {**entry, 'status': 'failed', 'error': str(exc)})
        raise
    entry.update(status='submitted', response={key: response.get(key) for key in ('id', 'status', 'created_at')
                                               if key in response})
    _append(db_path, entry)
    return entry
