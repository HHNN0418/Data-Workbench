"""Which of our roles' posts the platform has hidden (D90).

Moderation hides a reported post when the report is ruled genuine, and three
hidden posts suspend the account; one hidden post plus a report on an account
with 100 or more actions suspends it too. Nothing in our own data shows this:
collected posts keep visibility 'public', and the official log reports only the
suspension. On 23 Oct the audit found 21 hidden posts, Leah and Tomás each one
short of suspension (reports/generated/ban-audit-20261023.md).

Once an hour each role account's public post listing is compared with the
posts we collected for it in the last two days. The platform's answer for a
post missing from the listing goes to risk_assessment.observe, which owns the
rule: a first 404 makes it a suspect, and only a 404 again
risk_assessment.CONFIRM_MINUTES later (10 since 24 Oct) puts it into hidden_posts. risk_assessment's own feed scan covers every
account; this one stays as a second check for our roles. Anonymous public GETs only;
nothing is posted or changed. The pipeline's execute stage (the sidebar's
Agent 执行 dot) and the role, Agent list and account pages show the risks.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from . import risk_assessment
from .db import connect, utc_now
from .imports import normalize_timestamp

LOGGER = logging.getLogger(__name__)

WINDOW_HOURS = 48
# The listing can lag a fresh post by a few minutes.
MIN_AGE_MINUTES = 15
INTERVAL_SECONDS = 3600
FIRST_RUN_SECONDS = 120
PAUSE_SECONDS = 0.5
MAX_PAGES = 12
# The suspension rules: three hidden posts, or one hidden post and an account
# report once the account has 100 actions.
SUSPEND_AT = 3
REPORT_RISK_ACTIONS = 100
AGENTS_KEY = 'agent_control_v1:agents'


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _active_agents(conn):
    """[(agent_id, account_id, name)] for roles bound to an account that is not suspended.

    The role rows do not store suspension; the official activity log does
    (account_status), so banned roles are left out from there.
    """
    row = conn.execute('SELECT value FROM meta WHERE key=?', (AGENTS_KEY,)).fetchone()
    try:
        agents = json.loads(row['value']) if row else {}
    except (TypeError, ValueError):
        return []
    from .account_status import suspended
    banned = suspended(conn)
    return [(str(agent.get('id') or key), agent['account_id'], agent.get('name') or str(agent['account_id']))
            for key, agent in (agents.items() if isinstance(agents, dict) else ())
            if isinstance(agent, dict) and type(agent.get('account_id')) is int
            and agent['account_id'] not in banned and not agent.get('account_suspended')]


def roles(conn):
    """{account_id: name} for roles bound to an account that is not suspended."""
    return {account_id: name for _, account_id, name in _active_agents(conn)}


def _post_id(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def own_actions(conn, owners, since):
    """({account_id: platform actions from our own records}, post ids our roles deleted).

    owners is {agent_id: account_id}, since {account_id: first collected post
    time}; risk_assessment reads our action counts and deletions from here too.

    The official count includes likes, follows and reposts, which the posts
    table does not show; every executed action leaves a 'published' audit event.
    Events that only confirmed an existing state (already_applied) did nothing.
    A role may have been bound to an earlier account, so only events from the
    account's first collected post on are counted.
    """
    actions, deleted = {}, set()
    if not owners:
        return actions, deleted
    marks = ','.join('?' for _ in owners)
    for row in conn.execute(f"SELECT agent_id,at,details_json FROM agent_audit_events "
                            f"WHERE kind='published' AND agent_id IN ({marks})", tuple(owners)):
        try:
            details = json.loads(row['details_json'] or '{}')
        except (TypeError, ValueError):
            continue
        if not isinstance(details, dict) or details.get('already_applied'):
            continue
        account = owners[row['agent_id']]
        if details.get('action') == 'delete_post':
            deleted.update(pid for pid in (_post_id(details.get('target_post_id')), _post_id(details.get('post_id')))
                           if pid is not None)
        first = since.get(account)
        if first is None or normalize_timestamp(row['at']) >= first:
            actions[account] = actions.get(account, 0) + 1
    return actions, deleted


def scan_account(client, db_path, account_id, *, now=None, pause=PAUSE_SECONDS):
    """Pass this account's posts that its public listing no longer serves to risk_assessment.observe.

    Returns the ids now suspect or hidden. Posts already suspect or hidden are
    left to risk_assessment's recheck.
    """
    now = now or datetime.now(timezone.utc)
    since, until = _iso(now - timedelta(hours=WINDOW_HOURS)), _iso(now - timedelta(minutes=MIN_AGE_MINUTES))
    conn = connect(db_path)
    try:
        ours = {row['post_id']: row['created_at'] for row in conn.execute(
            'SELECT post_id,created_at FROM posts WHERE author_id=? AND created_at>=? AND created_at<=?',
            (account_id, since, until))}
        known = {row['post_id'] for row in conn.execute('SELECT post_id FROM hidden_posts WHERE account_id=?',
                                                         (account_id,))}
        known |= {row['post_id'] for row in conn.execute(
            "SELECT post_id FROM post_visibility WHERE account_id=? AND status IN ('suspect','hidden')", (account_id,))}
    finally:
        conn.close()
    if not set(ours) - known:
        return []
    listed, cursor = set(), None
    for _ in range(MAX_PAGES):
        page = client.account_posts(account_id, limit=50, cursor=cursor)
        listed.update(item['post_id'] for item in page if isinstance(item, dict) and 'post_id' in item)
        oldest = min((str(item.get('created_at') or '') for item in page if isinstance(item, dict)), default='')
        cursor = page.next_cursor
        if not cursor or (oldest and oldest < since):
            break
        time.sleep(pause)
    answers = []
    for post_id in sorted(set(ours) - listed - known):
        time.sleep(pause)
        answers.append((post_id, risk_assessment.check(client, post_id), utc_now()))
    missing = []
    if answers:
        conn = connect(db_path)
        try:
            skip = risk_assessment.own_deleted(conn)
            for post_id, status, at in answers:
                with conn:
                    state = risk_assessment.observe(conn, post_id, account_id, ours[post_id], status, at, skip=skip)
                if state in ('suspect', 'hidden'):
                    missing.append(post_id)
        finally:
            conn.close()
    return missing


def run_once(db_path, client=None, *, now=None, pause=PAUSE_SECONDS):
    if client is None:
        import capturethenarrative
        client = capturethenarrative.LegitSocial(timeout=20, max_retries=0)
    conn = connect(db_path)
    try:
        accounts = roles(conn)
    finally:
        conn.close()
    found = {}
    for account_id in sorted(accounts):
        try:
            new = scan_account(client, db_path, account_id, now=now, pause=pause)
        except Exception as exc:
            LOGGER.warning('Hidden-post check failed for account %s (%s)', account_id, type(exc).__name__)
            continue
        if new:
            found[account_id] = new
            LOGGER.warning('Account %s has posts missing from its listing (404, rechecked after %s minutes): %s',
                           account_id, risk_assessment.CONFIRM_MINUTES, new)
    return found


def risk_accounts(conn):
    """Active roles near suspension, worst first, plus the hidden counts of all active roles.

    Each risk is {account_id, name, hidden, actions, level, text}; level is
    'suspend_next' (one more hide may suspend) or 'report' (one hide, 100+
    actions: one account report may suspend). A 404 cannot tell a temporary
    hide from a permanent one, and only permanent hides count, so the texts say
    "may". Posts our own roles deleted also answer 404 and are left out.
    Actions are the larger of our collected posts and our own action records.
    """
    agents = _active_agents(conn)
    if not agents:
        return [], {}
    names = {account_id: name for _, account_id, name in agents}
    marks = ','.join('?' for _ in names)
    hidden = {}
    for row in conn.execute(f'SELECT post_id,account_id FROM hidden_posts WHERE account_id IN ({marks})',
                            tuple(names)):
        hidden.setdefault(row['account_id'], set()).add(row['post_id'])
    if not hidden:
        return [], {}
    marks = ','.join('?' for _ in hidden)
    posts, since = {}, {}
    for row in conn.execute(f'SELECT author_id,COUNT(*) AS n,MIN(ctn_timestamp(created_at)) AS first FROM posts '
                            f'WHERE author_id IN ({marks}) GROUP BY author_id', tuple(hidden)):
        posts[row['author_id']] = int(row['n'])
        if row['first']:
            since[row['author_id']] = normalize_timestamp(row['first'])
    recorded, deleted = own_actions(conn, {agent_id: account_id for agent_id, account_id, _ in agents
                                            if account_id in hidden}, since)
    counts = {account_id: len(ids - deleted) for account_id, ids in hidden.items() if ids - deleted}
    found = []
    for account_id, count in sorted(counts.items(), key=lambda item: -item[1]):
        name, done = names[account_id], max(posts.get(account_id, 0), recorded.get(account_id, 0))
        reportable = done >= REPORT_RISK_ACTIONS
        if count >= SUSPEND_AT:
            # Not yet in the official suspension log: some hides may be temporary.
            level, text = 'suspend_next', (f'{name}（{account_id}）已有 {count} 条帖子被隐藏，已到 {SUSPEND_AT} 条封号线：'
                                           f'有的可能是临时隐藏，或者封号还没出现在官方日志里')
        elif count == SUSPEND_AT - 1:
            level, text = 'suspend_next', f'{name}（{account_id}）已有 {count} 条帖子被隐藏，再被隐藏 1 条就可能封号'
            if reportable:
                text += f'；操作 {done} 次，账号也可能被直接举报'
        elif reportable:
            level, text = 'report', (f'{name}（{account_id}）已有 1 条帖子被隐藏、操作 {done} 次：'
                                     f'账号再被举报一次就可能封号')
        else:
            continue
        found.append({'account_id': account_id, 'name': name, 'hidden': count, 'actions': done,
                      'level': level, 'text': text})
    return found, {str(account_id): count for account_id, count in counts.items()}


def risks(conn):
    """Alert texts for active roles near suspension, worst first, plus the counts."""
    found, counts = risk_accounts(conn)
    return [risk['text'] for risk in found], counts


class HiddenPostMonitor:
    """Lifecycle companion: checks our roles' posts for hidden ones every hour."""

    def __init__(self, db_path, *, interval_seconds=INTERVAL_SECONDS, first_run_seconds=FIRST_RUN_SECONDS):
        self.db_path, self.interval_seconds, self.first_run_seconds = db_path, interval_seconds, first_run_seconds
        self._stop = threading.Event()
        self._thread = None

    def _run(self):
        wait = self.first_run_seconds
        while not self._stop.wait(wait):
            try:
                run_once(self.db_path)
            except Exception as exc:
                LOGGER.error('Hidden-post check failed (%s); retrying next hour', type(exc).__name__)
            wait = self.interval_seconds

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='ctn-hidden-posts', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
