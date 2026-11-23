"""Who each recently active account is (GET /api/accounts/identity).

One label per account from the shared rules the Agents act on, so this page
shows exactly what targeting sees:

  ours           accounts our Agents run (Agent store)
  teammate       our team's accounts run by teammates (team_accounts only): watched, never operated
  public_figure  verified accounts: public figures and media
  bot            account_annotations / agent_targeting.bot_accounts
  non_citizen    agent_targeting.non_citizen_reasons: heavy, repost ring,
                 profile or writing style unlike the NPC generator,
                 batch-numbered name or LLM artefacts
  citizen        everyone else: agent_targeting.citizen_accounts

Our own accounts also go through the bot and non-citizen rules
(``rule_reasons``, and ``rule_label``: what the rules would call them — bot,
non_citizen or citizen), so the page shows which of them a detector like ours
would flag and how exposed they are to reports. annotations never stores our
accounts as bots but keeps their reasons, so the bot rule is applied to those
here. ``own_risks`` lists our roles near suspension by hidden posts
(hidden_posts). It is risk information only: it is never used to tune what the
Agents write to avoid detection (D47).

Read-only; nothing here changes data. Computing it takes a few seconds on live
data, so one result is kept and refreshed in the background.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import agent_targeting
from .annotations import is_bot
from .db import connect, utc_now
from .imports import normalize_timestamp

LOGGER = logging.getLogger(__name__)

WINDOW_HOURS = 24
CACHE_SECONDS = 300
EXCERPT_CHARS = 140
LABELS = ('citizen', 'bot', 'non_citizen', 'ours', 'teammate', 'public_figure')
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
AGENTS_META_KEY = 'agent_control_v1:agents'
CAVEATS = ['标签用的是 Agent 选回复对象的同一套规则（agent_targeting），不是官方名单',
           '刚注册、帖子很少的别队机器人，在行为暴露之前会被算成市民',
           '机器人标记每分钟更新一次；高频、转发圈、批量用户名和 AI 套话按全部已采集数据判断',
           '「我方 · 队友管理」是本队队友运行的账号：我们只监控，不登录也不操作；Agent 不会把它们当作目标']


def _excerpt(text):
    text = ' '.join((text or '').split())
    return text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS - 1] + '…'


def _chunks(values, size=500):
    values = sorted(values)
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def _own(conn):
    """(all our team's accounts, the ones our Agents run); the rest are teammates' accounts."""
    own = {row[0] for row in conn.execute('SELECT account_id FROM team_accounts WHERE is_own=1')}
    row = conn.execute('SELECT value FROM meta WHERE key=?', (AGENTS_META_KEY,)).fetchone()
    try:
        agents = json.loads(row[0]) if row else {}
    except (TypeError, ValueError):
        agents = {}
    run = {a['account_id'] for a in agents.values()
           if isinstance(a, dict) and type(a.get('account_id')) is int} if isinstance(agents, dict) else set()
    return own | run, run


def _rule_verdict(non_citizen, bot):
    """What the rules would call one of our accounts: its reasons and bot / non_citizen / citizen."""
    reasons = sorted(set(non_citizen) | set(bot))
    return {'rule_reasons': reasons, 'rule_label': 'bot' if bot else 'non_citizen' if reasons else 'citizen'}


def _stance(counts):
    sided = {key: counts[key] for key in ('yes', 'no', 'undecided') if counts.get(key)}
    return max(sided, key=lambda key: (sided[key], key == 'undecided')) if sided else 'unrelated'


def build(db_path, *, now=None):
    now = datetime.fromisoformat(normalize_timestamp(now).replace('Z', '+00:00')) if now else datetime.now(timezone.utc)
    since, until = normalize_timestamp(now - timedelta(hours=WINDOW_HOURS)), normalize_timestamp(now)
    conn = connect(db_path)
    try:
        # Stances come from post_annotations (annotations.AnnotationWorker); only
        # posts it has not reached yet are classified here (about 1 ms each).
        posts = [dict(row) for row in conn.execute(
            'SELECT p.post_id,p.author_id,p.reply_to_post_id,p.repost_of_post_id,p.body,'
            'ctn_timestamp(p.created_at) AS created,pa.stance FROM posts p '
            'LEFT JOIN post_annotations pa ON pa.post_id=p.post_id '
            'WHERE ctn_timestamp(p.created_at)>=? AND ctn_timestamp(p.created_at)<=? AND p.author_id IS NOT NULL',
            (since, until))]
        authors_of = {post['post_id']: post['author_id'] for post in posts}
        for batch in _chunks({p['reply_to_post_id'] for p in posts if p['reply_to_post_id']} - set(authors_of)):
            authors_of.update((row[0], row[1]) for row in conn.execute(
                'SELECT post_id,author_id FROM posts WHERE post_id IN (' + ','.join('?' for _ in batch) + ')', batch))
        active = {post['author_id'] for post in posts}
        own, run = _own(conn)
        accounts, totals, bot_reasons = {}, {}, {}
        recent_since = normalize_timestamp(now - timedelta(hours=agent_targeting.CITIZEN_RECENT_HOURS))
        for batch in _chunks(active):
            marks = ','.join('?' for _ in batch)
            accounts.update((row[0], {'username': row[1], 'verified': bool(row[2])}) for row in conn.execute(
                f'SELECT account_id,username,verified FROM accounts WHERE account_id IN ({marks})', batch))
            for row in conn.execute(
                    'SELECT author_id,COUNT(*),SUM(CASE WHEN ctn_timestamp(created_at)>=? THEN 1 ELSE 0 END) '
                    f'FROM posts WHERE author_id IN ({marks}) GROUP BY author_id', (recent_since, *batch)):
                # PostgreSQL returns SUM as numeric.
                totals[row[0]] = (int(row[1]), int(row[2] or 0))
            for row in conn.execute(
                    f'SELECT account_id,bot_reasons_json FROM account_annotations WHERE is_bot=1 AND account_id IN ({marks})',
                    batch):
                bot_reasons[row[0]] = json.loads(row[1] or '[]')
        # Our accounts keep is_bot=0 whatever their reasons; apply the same bot rule.
        own_bots = {}
        for batch in _chunks(active & own):
            marks = ','.join('?' for _ in batch)
            for row in conn.execute(
                    f'SELECT account_id,bot_reasons_json FROM account_annotations WHERE account_id IN ({marks})', batch):
                why = json.loads(row[1] or '[]')
                if is_bot(why):
                    own_bots[row[0]] = [reason for reason in why if reason != 'duplicate_text']
        from .hidden_posts import risk_accounts
        own_risks, _ = risk_accounts(conn)
    finally:
        conn.close()
    bots = agent_targeting.bot_accounts(db_path) & active
    outsiders = agent_targeting.non_citizen_reasons(db_path, active - own, now=now.timestamp())
    insiders = agent_targeting.non_citizen_reasons(db_path, active & own, now=now.timestamp()) if active & own else {}
    stances, stats, ours_to, answered = defaultdict(Counter), defaultdict(Counter), defaultdict(list), Counter()
    latest = {}
    for post in posts:
        author = post['author_id']
        kind = 'repost' if post['repost_of_post_id'] else 'reply' if post['reply_to_post_id'] else 'post'
        stats[author][kind] += 1
        if kind != 'repost':
            stances[author][post['stance'] or agent_targeting.classify(post['body'])['stance']] += 1
            if post['created'] and (author not in latest or post['created'] > latest[author]['created']):
                latest[author] = post
        parent = authors_of.get(post['reply_to_post_id']) if post['reply_to_post_id'] else None
        if parent is not None and parent != author:
            if author in own:
                ours_to[parent].append(post['created'])
            elif parent in own:
                answered[author] += 1
    from . import account_status
    suspended = account_status.suspended(db_path)
    rows = []
    for author in active:
        account = accounts.get(author, {})
        if author in run:
            label, reasons = 'ours', []
        elif author in own:
            label, reasons = 'teammate', []
        elif account.get('verified'):
            label, reasons = 'public_figure', ['verified']
        elif author in bots:
            label = 'bot'
            reasons = [r for r in bot_reasons.get(author, ['behavior']) if r != 'duplicate_text'] or ['behavior']
        elif author in outsiders:
            label, reasons = 'non_citizen', outsiders[author]
        else:
            label, reasons = 'citizen', []
        total, recent = totals.get(author, (0, 0))
        last = latest.get(author)
        replies_by_us = sorted(t for t in ours_to.get(author, []) if t)
        rows.append({
            'account_id': author, 'username': account.get('username'), 'label': label, 'reasons': reasons,
            'figure_mentioned': (account.get('username') or '') in agent_targeting.PUBLIC_FIGURES,
            'posts_24h': stats[author]['post'], 'replies_24h': stats[author]['reply'],
            'reposts_24h': stats[author]['repost'], 'actions_total': total, 'writes_48h': recent,
            'stance': _stance(stances[author]), 'stance_counts': dict(stances[author]),
            'last_at': last['created'] if last else None, 'last_post_id': last['post_id'] if last else None,
            'last_excerpt': _excerpt(last['body']) if last else '',
            'our_replies_24h': len(replies_by_us), 'our_last_reply_at': replies_by_us[-1] if replies_by_us else None,
            'answered_us_24h': answered[author],
            'suspended_at': (suspended.get(author) or {}).get('at'),
            **(_rule_verdict(insiders.get(author, []), set(own_bots.get(author, [])) | (
                {'behavior'} if author in bots else set())) if author in own else {})})
    rows.sort(key=lambda row: (-(row['posts_24h'] + row['replies_24h']), row['account_id']))
    summary = Counter(row['label'] for row in rows)
    return {'generated_at': utc_now(), 'window': {'hours': WINDOW_HOURS, 'from': since, 'to': until},
            'summary': {label: summary.get(label, 0) for label in LABELS}, 'active_accounts': len(rows),
            'citizens_answering_us': sum(1 for row in rows if row['label'] == 'citizen' and row['answered_us_24h']),
            'own_flagged': {label: {'flagged': sum(1 for row in rows if row['label'] == label and row['rule_reasons']),
                                    'total': summary.get(label, 0)} for label in ('ours', 'teammate')},
            'own_risks': own_risks, 'rows': rows, 'caveats': CAVEATS, 'read_only': True}


def select(result, *, label='', q='', limit=DEFAULT_LIMIT):
    """Filter a built result by label and username or account id; summary stays that of all accounts."""
    if label and label not in LABELS:
        raise ValueError('label 只能是 ' + '、'.join(LABELS))
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f'limit 必须是 1–{MAX_LIMIT} 的整数')
    needle = (q or '').strip().lstrip('@#').lower()
    rows = [row for row in result['rows'] if (not label or row['label'] == label) and (
        not needle or needle == str(row['account_id']) or needle in (row['username'] or '').lower())]
    return {**{key: value for key, value in result.items() if key != 'rows'},
            'rows': rows[:limit], 'matched': len(rows)}


class AccountIdentity:
    """One cached result, refreshed in the background when stale."""
    def __init__(self, db_path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._refreshing = False
        self._cached = None

    def _refresh(self):
        try:
            self._cached = (time.monotonic(), build(self.db_path))
        except Exception:
            # Keep serving the last result (still marked stale); the next request retries.
            LOGGER.exception('account identity refresh failed')
        finally:
            with self._lock:
                self._refreshing = False

    def get(self, **filters):
        cached = self._cached
        if cached is None:
            with self._lock:
                if self._cached is None:
                    self._cached = (time.monotonic(), build(self.db_path))
            return {**select(self._cached[1], **filters), 'stale': False}
        stale = time.monotonic() - cached[0] >= CACHE_SECONDS
        if stale:
            with self._lock:
                if not self._refreshing:
                    self._refreshing = True
                    threading.Thread(target=self._refresh, name='ctn-account-identity', daemon=True).start()
        return {**select(cached[1], **filters), 'stale': stale}
