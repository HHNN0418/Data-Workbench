"""Notification progress derived from durable actions, separate from remote ack.

Reconsidering an abandoned notification only reopens model input. It never
resends its abandoned action; the operation-level intent guard remains intact.
"""
from __future__ import annotations

from .db import utc_now


ACTIVE_STATES = frozenset(('pending', 'rejected', 'sending', 'uncertain'))


def _positive(value):
    return value if type(value) is int and value > 0 else None


def _actor_account_id(notice, conn=None):
    """A like's target author is not necessarily the person who liked it."""
    actor_id = _positive(notice.get('actor_account_id')) or _positive(notice.get('actor_id'))
    if actor_id:
        return actor_id
    actor = notice.get('actor')
    if isinstance(actor, dict):
        actor_id = _positive(actor.get('account_id')) or _positive(actor.get('id'))
        if actor_id:
            return actor_id
    # The SDK documents reply/mention notifications as containing the post
    # written by their actor. Other notification kinds have no such guarantee.
    kind = str(notice.get('kind') or notice.get('type') or '').casefold()
    if kind in ('reply', 'mention'):
        source = notice.get('source') or notice.get('post')
        if isinstance(source, dict):
            actor_id = _positive(source.get('author_id'))
            if actor_id:
                return actor_id
        if conn is not None and _positive(notice.get('post_id')):
            row = conn.execute('SELECT author_id FROM posts WHERE post_id=?', (notice['post_id'],)).fetchone()
            if row and _positive(row['author_id']):
                return row['author_id']
    username = notice.get('actor_username') or (actor.get('username') if isinstance(actor, dict) else None)
    if conn is not None and isinstance(username, str) and username.lstrip('@'):
        row = conn.execute('SELECT account_id FROM accounts WHERE LOWER(username)=LOWER(?)',
                           (username.lstrip('@'),)).fetchone()
        if row:
            return _positive(row['account_id'])
    return None


def _blocked_ids(data, agent_id, conn=None):
    from .agent_interaction_policy import blocked_account_ids
    return blocked_account_ids(conn, data, data.get('agents', {}).get(agent_id, {'id': agent_id}))


def _blocked_names(data, agent_id, conn=None):
    from .agent_interaction_policy import blocked_usernames
    return blocked_usernames(conn, data, data.get('agents', {}).get(agent_id, {'id': agent_id}))


def notice_is_policy_ignored(data, agent_id, notice, conn=None, *, blocked_ids=None, blocked_names=None):
    if not data.get('settings', {}).get('block_agent_interactions', True):
        return False
    blocked = _blocked_ids(data, agent_id, conn) if blocked_ids is None else blocked_ids
    actor_id = _actor_account_id(notice, conn)
    if actor_id:
        return actor_id in blocked
    actor = notice.get('actor')
    username = notice.get('actor_username') or (actor.get('username') if isinstance(actor, dict) else '')
    names = _blocked_names(data, agent_id, conn) if blocked_names is None else blocked_names
    return isinstance(username, str) and username.lstrip('@').casefold() in names


def apply_interaction_policy(data, agent_id, conn=None):
    """Ignore internal notices locally, without confirming anything remotely.

    Cancelled acknowledgements stay cancelled when the setting is disabled.
    A new decision can explicitly enqueue them again, while its older social
    operation remains protected by the normal durable intent guard.
    """
    own = data.get('memory', {}).get(agent_id)
    if own is None:
        return
    blocked = (_blocked_ids(data, agent_id, conn)
               if data.get('settings', {}).get('block_agent_interactions', True) else set())
    names = _blocked_names(data, agent_id, conn) if blocked else set()
    outbox = own.get('notification_outbox', {})
    for notice in own.get('inbox', []):
        actor_id = _actor_account_id(notice, conn)
        if actor_id:
            notice['actor_account_id'] = actor_id
        ignored = notice_is_policy_ignored(data, agent_id, notice, conn,
                                            blocked_ids=blocked, blocked_names=names)
        notice['policy_ignored'] = ignored
        if ignored:
            notice['policy_ignore_reason'] = '内部互动，已忽略'
            notice.setdefault('policy_ignored_at', utc_now())
            notice_id = notice.get('id', notice.get('notification_id'))
            # Keep cancellation tied to the decisions that already existed.
            # Disabling the setting permits new decisions, but cannot revive a
            # successful old draft's cancelled acknowledgement on maintenance.
            prior_drafts = [draft['id'] for draft in data.get('drafts', {}).values()
                            if draft.get('agent_id') == agent_id
                            and notice_id in draft.get('notification_ids', [])]
            notice['policy_ignored_draft_ids'] = list(dict.fromkeys(
                notice.get('policy_ignored_draft_ids', []) + prior_drafts))
            queued = outbox.get(str(notice_id))
            if queued and queued.get('state') == 'pending':
                queued.update(state='policy_ignored', next_retry_at=0, last_error='内部互动，已忽略')
                queued['policy_ignored_draft_ids'] = list(notice['policy_ignored_draft_ids'])
            if notice.get('status') == 'ack_pending':
                notice['status'] = 'unresolved'
        else:
            notice.pop('policy_ignore_reason', None)
    own.setdefault('observation', {})['unresolved_count'] = sum(
        notice.get('status') != 'resolved' and not notice.get('policy_ignored')
        for notice in own.get('inbox', []))


def _related(data, agent_id, notification_id):
    rows = [row for row in data.get('drafts', {}).values()
            if row.get('agent_id') == agent_id
            and notification_id in row.get('notification_ids', [])]
    # An unfinished action takes precedence over older completed/abandoned ones.
    return sorted(rows, key=lambda row: (row.get('state') in ACTIVE_STATES,
                  row.get('created_at', ''), row.get('id', '')), reverse=True)


def enrich_inbox(data, agent_id):
    """Return a public view without changing polling or confirmation state."""
    agent = data.get('agents', {}).get(agent_id, {})
    own = data.get('memory', {}).get(agent_id, {})
    read_ids = set(own.get('read_notification_ids', []))
    blocked = (_blocked_ids(data, agent_id)
               if data.get('settings', {}).get('block_agent_interactions', True) else set())
    names = _blocked_names(data, agent_id) if blocked else set()
    result = []
    for notice in own.get('inbox', []):
        item = dict(notice)
        notification_id = notice.get('id', notice.get('notification_id'))
        related = _related(data, agent_id, notification_id)
        draft = related[0] if related else {}
        state = draft.get('state')
        platform = notice.get('status', 'unresolved')
        progress = 'read_awaiting_decision' if notification_id in read_ids else 'awaiting_decision'
        if state == 'pending':
            progress = 'awaiting_review' if agent.get('mode') == 'review' or draft.get('requires_review') else 'queued'
        elif state == 'rejected':
            progress = 'failed'
        elif state == 'sending':
            progress = 'executing'
        elif state == 'uncertain':
            progress = 'needs_verification'
        elif state == 'published':
            progress = 'completed'
        elif state == 'skipped':
            progress = ('skipped' if platform == 'resolved' or draft.get('notification_confirmation_authorized_at')
                        else 'skipped_awaiting_confirmation')
        elif state == 'discarded':
            progress = ('reconsidered' if notice.get('reconsidered_draft_id') == draft.get('id') else 'discarded')
        ignored = notice_is_policy_ignored(data, agent_id, notice, blocked_ids=blocked, blocked_names=names)
        if ignored:
            progress = 'policy_ignored'
        item.update(policy_ignored=ignored, policy_ignore_reason='内部互动，已忽略' if ignored else '')
        item.update(local_progress=progress,
                    related_draft_id=draft.get('id'), related_action=draft.get('action'),
                    related_draft_state=state,
                    can_confirm_skip=bool(not ignored and state == 'skipped' and platform == 'unresolved'
                                          and not draft.get('notification_confirmation_authorized_at')),
                    can_reconsider=bool(not ignored and state == 'discarded' and platform == 'unresolved'
                                        and progress != 'reconsidered'))
        result.append(item)
    return result


def confirm_skip(controller, draft_id):
    """Explicitly authorize only the notifications selected by a skipped draft."""
    from .agent_control import AgentError
    from . import agent_actions
    initial = controller._draft(controller.store.read(), draft_id)
    with controller._claim(initial['agent_id']) as token:
        def authorize(data, conn):
            controller._check_lease(data, initial['agent_id'], token)
            draft = controller._draft(data, draft_id)
            agent = controller._agent(data, draft['agent_id'])
            if draft.get('state') != 'skipped' or draft.get('action') != 'skip':
                raise AgentError('只有已决定跳过的通知可以在这里确认', 409)
            ids = draft.get('notification_ids', [])
            if not ids:
                raise AgentError('这次跳过没有选定通知，无需确认', 409)
            inbox = {row.get('id', row.get('notification_id')): row
                     for row in data.get('memory', {}).get(agent['id'], {}).get('inbox', [])}
            if any(value not in inbox for value in ids):
                raise AgentError('关联通知已不可用，请刷新后核对', 409)
            if any(notice_is_policy_ignored(data, agent['id'], inbox[value], conn) for value in ids):
                raise AgentError('内部互动通知已由全局策略忽略，无需确认', 409)
            if not agent.get('account_id') or agent.get('account_state') != 'ready':
                raise AgentError('请先连接角色账号后确认通知', 409)
            if draft.get('account_id') is not None and draft['account_id'] != agent['account_id']:
                raise AgentError('通知操作对应的账号已改变，未确认', 409)
            if not draft.get('notification_confirmation_authorized_at'):
                draft['notification_confirmation_authorized_at'] = utc_now()
                controller._event(data, 'notification_skip_confirmed', '已确认跳过选定通知，平台确认将单独重试',
                                  agent['id'], draft_id, details={'notification_count': len(ids)})
            draft['requires_review'] = False
            draft['review_approved'] = True
            draft['review_approved_at'] = draft['notification_confirmation_authorized_at']
            return dict(agent)
        agent = controller.store.change(authorize)
        # The existing durable acknowledgement outbox owns retries. Its failure
        # cannot turn this skipped decision into a failed social operation.
        agent_actions.resolve_notifications(controller, agent, token, draft_id, automatic=False)
    return controller.snapshot()


def reconsider(controller, agent_id, notification_id):
    """Release input caches for one abandoned notice, preserving intent guards."""
    from .agent_control import AgentError
    if type(notification_id) is not int or notification_id <= 0:
        raise AgentError('通知 ID 须为正整数')
    with controller._claim(agent_id) as token:
        def release(data, conn):
            controller._check_lease(data, agent_id, token)
            controller._agent(data, agent_id)
            own = data['memory'].setdefault(agent_id, {})
            notice = next((row for row in own.get('inbox', [])
                           if row.get('id', row.get('notification_id')) == notification_id), None)
            if notice is None:
                raise AgentError('通知不存在，请刷新后重试', 404)
            if notice_is_policy_ignored(data, agent_id, notice, conn):
                raise AgentError('内部互动通知已由全局策略忽略，不能重新判断', 409)
            if notice.get('status', 'unresolved') != 'unresolved':
                raise AgentError('已确认或确认中的通知不能重新判断', 409)
            related = _related(data, agent_id, notification_id)
            if any(row.get('state') in ACTIVE_STATES for row in related):
                raise AgentError('此通知仍有关联操作，请先审阅或核对该操作', 409)
            discarded = next((row for row in related if row.get('state') == 'discarded'), None)
            if discarded is None:
                raise AgentError('只有已放弃操作的通知可以重新判断', 409)
            if notice.get('reconsidered_draft_id') == discarded['id']:
                return
            own['decision_notification_ids'] = [value for value in own.get('decision_notification_ids', [])
                                                if value != notification_id]
            source_id = notice.get('source_id') or ('post:' + str(notice['post_id']) if notice.get('post_id') else None)
            if source_id:
                own.setdefault('decision_versions', {}).pop(source_id, None)
            notice.update(reconsidered_draft_id=discarded['id'],
                          reconsidered_draft_ids=[row['id'] for row in related if row.get('state') == 'discarded'],
                          reconsidered_at=utc_now())
            controller._event(data, 'notification_reconsidered',
                              '通知已重新开放判断，原已放弃动作仍受保护，不会直接重放',
                              agent_id, discarded['id'], details={'notification_count': 1})
        controller.store.change(release)
    return controller.snapshot()
