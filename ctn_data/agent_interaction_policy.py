"""Identity-based isolation for accounts managed by autonomous agents.

The durable registry deliberately contains only agent bindings, never every
human account in the same team. Pausing or replacing a binding does not remove
that account or its previously observed usernames from the protected set.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

from .agent_control import AgentError
from .db import utc_now


_USERNAME = re.compile(r'[A-Za-z0-9_]{3,30}\Z')
_MENTION = re.compile(r'(?<![A-Za-z0-9_@])@([A-Za-z0-9_]{3,30})(?![A-Za-z0-9_])')
_POST_ACTIONS = frozenset(('reply', 'repost', 'like', 'unlike'))
_BODY_ACTIONS = frozenset(('post', 'reply', 'repost'))


class PolicyBlocked(AgentError):
    """A local refusal proving that no social write has been attempted."""

    def __init__(self, message, code='managed_agent_target'):
        super().__init__(message, 409)
        self.policy_blocked = True
        self.policy_reason = code
        self.remote_started = False


def enabled(data):
    return data.get('settings', {}).get('block_agent_interactions', True) is not False


def _positive(value):
    return value if type(value) is int and value > 0 else None


def _username(value):
    return value.casefold() if isinstance(value, str) and _USERNAME.fullmatch(value) else None


def remember_account(data, agent, previous=None):
    """Retain public identities on both sides of a binding/profile change."""
    registry = data.setdefault('rate', {}).setdefault('managed_agent_accounts', {})
    for source in (previous, agent):
        if not isinstance(source, dict) or not _positive(source.get('account_id')):
            continue
        account_id = source['account_id']
        row = registry.setdefault(str(account_id), {'account_id': account_id, 'usernames': [], 'agent_ids': []})
        row['account_id'] = account_id
        username = _username(source.get('username'))
        row['usernames'] = list(dict.fromkeys([name for name in row.get('usernames', []) if _username(name)] +
                                             ([username] if username else [])))
        agent_id = source.get('id')
        if isinstance(agent_id, str) and agent_id:
            row['agent_ids'] = list(dict.fromkeys(row.get('agent_ids', []) + [agent_id]))


def _identities(data):
    rows = {}
    for key, stored in data.get('rate', {}).get('managed_agent_accounts', {}).items():
        if not isinstance(stored, dict):
            continue
        account_id = _positive(stored.get('account_id'))
        if account_id is None and isinstance(key, str) and key.isascii() and key.isdigit():
            account_id = _positive(int(key))
        if account_id:
            rows.setdefault(account_id, set()).update(filter(None, (_username(name) for name in stored.get('usernames', []))))
    for source in data.get('agents', {}).values():
        account_id = _positive(source.get('account_id'))
        if account_id:
            names = rows.setdefault(account_id, set())
            username = _username(source.get('username'))
            if username:
                names.add(username)
    return rows


def blocked_account_ids(conn, data, agent):
    """Current and historical peer IDs, including disabled/paused actors."""
    if not enabled(data):
        return set()
    return set(_identities(data)) - {agent.get('account_id')}


def managed_accounts(data):
    """Public identity map for policy-independent managed account counts."""
    return _identities(data)


def blocked_usernames(conn, data, agent):
    ids = blocked_account_ids(conn, data, agent)
    identities = _identities(data)
    result = {name for account_id in ids for name in identities.get(account_id, ())}
    if conn is not None and ids:
        placeholders = ','.join('?' for _ in ids)
        for row in conn.execute(f'SELECT account_id,username FROM accounts WHERE account_id IN ({placeholders})', tuple(sorted(ids))):
            username = _username(row['username'])
            if username:
                result.add(username)
    return result


def check_body(conn, data, agent, body):
    if not enabled(data) or not isinstance(body, str) or '@' not in unicodedata.normalize('NFKC', body):
        return
    names = blocked_usernames(conn, data, agent)
    mentions = {match.group(1).casefold() for match in _MENTION.finditer(unicodedata.normalize('NFKC', body))}
    if names.intersection(mentions):
        raise PolicyBlocked('已禁止受管 Agent 互相互动：正文不能 @ 其他受管角色', 'managed_agent_mention')


def check(conn, data, agent, draft, *, body=None, target_result=None, manual=False, require_author=False):
    if not enabled(data):
        return
    action = draft.get('action')
    if action in _BODY_ACTIONS:
        check_body(conn, data, agent, draft.get('body', '') if body is None else body)
    blocked = blocked_account_ids(conn, data, agent)
    if action in ('follow', 'unfollow'):
        if action == 'unfollow' and manual:
            return
        if draft.get('target_account_id') in blocked:
            raise PolicyBlocked('已禁止受管 Agent 互相互动：不能关注或自动取消关注其他受管角色')
        return
    if action not in _POST_ACTIONS:
        return
    target = draft.get('target_post_id', draft.get('reply_to'))
    if target_result is not None:
        author = target_result.get('author_id') if isinstance(target_result, dict) else getattr(target_result, 'author_id', None)
        author = _positive(author)
    else:
        author = next((_positive(row.get('author_id')) for row in draft.get('sources', [])
                       if isinstance(row, dict) and row.get('post_id') == target and _positive(row.get('author_id'))), None)
        if author is None and conn is not None and _positive(target):
            row = conn.execute('SELECT author_id FROM posts WHERE post_id=?', (target,)).fetchone()
            author = _positive(row['author_id']) if row else None
    if require_author and (target_result is None or author is None):
        raise PolicyBlocked('互动目标作者尚未确认，未发送操作', 'unknown_target_author')
    if action == 'unlike' and manual:
        return
    if author in blocked:
        raise PolicyBlocked('已禁止受管 Agent 互相互动：不能回复、点赞、转发或自动取消点赞其他受管角色的帖子')


def mark_blocked(controller, data, conn, draft, reason):
    """Cancel a proven-unsent draft and release its local delivery reservation."""
    if draft.get('state') not in ('pending', 'rejected', 'sending'):
        return False
    from . import agent_actions
    agent_actions.restore_reservation(data, draft)
    message = str(reason)
    draft.update(state='skipped', policy_blocked=True, policy_reason=message,
                 policy_code=getattr(reason, 'policy_reason', 'managed_agent_target'), error='',
                 policy_blocked_at=utc_now(), version=draft.get('version', 0) + 1)
    day = datetime.fromtimestamp(controller.clock(), ZoneInfo(data.get('settings', {}).get('timezone', 'Australia/Sydney'))).date().isoformat()
    stats = data['rate'].setdefault('interaction_policy_stats', {}).setdefault(day, {'blocked': 0})
    stats['blocked'] = stats.get('blocked', 0) + 1
    for key in ('send_token', 'lease_until', 'next_attempt_at', 'quota_wait_bucket'):
        draft.pop(key, None)
    agent_actions.remember_outcome(data, draft, 'skipped', message,
        {'action': draft['action'], 'policy_blocked': True, 'message': message,
         'target_post_id': agent_actions.target_id(draft), 'target_account_id': draft.get('target_account_id')})
    controller._event(data, 'policy_blocked', message, draft['agent_id'], draft['id'],
        details={'action': draft['action'], 'policy_code': draft['policy_code'], 'remote_started': False,
                 'target_post_id': agent_actions.target_id(draft), 'target_account_id': draft.get('target_account_id')})
    return True
