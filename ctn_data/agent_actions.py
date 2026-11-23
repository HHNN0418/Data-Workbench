"""Durable, single-attempt delivery for the fictional competition's actions.

All social SDK calls use the same persistent team budget. A timeout can never
turn into an automatic retry: its outbox entry remains uncertain until verified.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo

from .db import utc_now
from .agent_control import AgentError
from .agent_lifecycle import draft_matches_persona


class DeliveryNotStarted(AgentError):
    """Final authorization failed before any remote SDK mutation was called."""

    def __init__(self, message, code='authorization_changed', status_code=409):
        super().__init__(message, status_code)
        self.delivery_reason = code
        self.remote_started = False


def _api():
    # Imported lazily to keep AgentController's public imports compatible.
    from . import agent_budget
    from .agent_control import AgentError, LEASE_SECONDS, _field, _hash, _status, _text
    return agent_budget, AgentError, LEASE_SECONDS, _field, _hash, _status, _text


def target_id(draft):
    return draft.get('target_post_id', draft.get('reply_to'))


def _manual(draft, automatic=False):
    """Manual capability is server-created metadata, never a model action."""
    _, Error, _, _, _, _, _ = _api()
    marked = draft.get('origin') == 'manual' or draft.get('manual_only') is True or draft.get('manual_prepared') is True
    if not marked:
        return False
    if (draft.get('origin') != 'manual' or draft.get('manual_only') is not True
            or draft.get('manual_prepared') is not True
            or not isinstance(draft.get('user_instruction'), str) or not draft['user_instruction'].strip()):
        raise Error('人工指令来源无法确认，未执行', 409)
    if automatic:
        raise Error('人工指令只能由成员手动执行，不能进入自动调度', 409)
    return True


def _delete_guard(draft, token, manual):
    if draft.get('action') != 'delete_post':
        return
    _, Error, _, _, _, _, _ = _api()
    if not manual or draft.get('delete_confirmed') is not True or draft.get('delete_confirmation_token') != token:
        raise Error('请明确确认要删除的本人帖子后再执行', 409)


def fingerprint(draft, account_id):
    _, _, _, _, body_hash, _, _ = _api()
    if draft['action'] in ('like', 'unlike', 'follow', 'unfollow'):
        # Current remote state prevents duplicate writes without forbidding
        # a later intentional like/unlike/like or follow/unfollow/follow.
        return f"relationship:{account_id}:{draft['id']}"
    if draft['action'] == 'repost':
        return f"{draft['action']}:{account_id}:{target_id(draft)}"
    if draft['action'] == 'delete_post':
        return f"delete_post:{account_id}:{target_id(draft)}"
    return body_hash(draft['body'])


def remember_outcome(data, draft, state, message, result=None):
    memory = data['memory'].setdefault(draft['agent_id'], {})
    activity = memory.setdefault('activity', [])
    # One outcome per durable action, including reconciliation and ack retries.
    activity[:] = [row for row in activity if row.get('draft_id') != draft['id']]
    activity.append({'draft_id': draft['id'], 'action': draft['action'], 'state': state,
                     'target_post_id': target_id(draft), 'post_id': draft.get('post_id'),
                     'target_account_id': draft.get('target_account_id'),
                     'at': utc_now(), 'body': draft.get('body', '')[:500],
                     'reason': draft.get('reason', ''), 'message': message})
    memory['activity'] = activity[-100:]
    draft['result'] = result or {'action': draft['action'], 'target_post_id': target_id(draft),
                                 'target_account_id': draft.get('target_account_id'),
                                 'post_id': draft.get('post_id'), 'message': message}
    from . import agent_memory
    agent_memory.record_outcome(data, draft, state, message, at=activity[-1]['at'])


def restore_reservation(data, draft):
    """Return only this proven-unsent draft's local reservation and publisher."""
    from . import agent_relationships
    reservation = draft.pop('reservation', {})
    if reservation:
        data['rate'].get('hashes', {}).pop(reservation.get('key'), None)
        counters = data['rate'].setdefault(reservation.get('counter_bucket', 'accounts'), {})
        previous = reservation.get('counter', {})
        if previous:
            counters[draft['agent_id']] = previous
        else:
            counters.pop(draft['agent_id'], None)
        if draft['action'] in ('like', 'unlike', 'follow', 'unfollow'):
            relation_key = agent_relationships.key(draft['account_id'], agent_relationships.kind_for(draft['action']),
                                                   agent_relationships.target_for(draft))
            previous_relation = draft.pop('relationship_preflight', {})
            if previous_relation:
                data['rate'].setdefault('relationships', {})[relation_key] = previous_relation
            else:
                data['rate'].get('relationships', {}).pop(relation_key, None)
    if draft.get('send_token') and data.get('leases', {}).get('publisher', {}).get('token') == draft['send_token']:
        data['leases'].pop('publisher', None)


# The platform's answer when the target post was deleted or hidden by moderation.
TARGET_GONE_STATUSES = (400, 403, 404, 410, 422)


def _target_gone(controller, draft_id, token, status):
    """Skip an automatic action whose target the platform no longer serves; nothing was sent.

    Left pending, the scheduler retried it every minute and it blocked the
    role's later actions: three roles lost most of the night of 22 Oct to
    replies whose No-side bot targets had been taken down.
    """
    message = f'互动目标已被删除或隐藏（平台返回 {status}），未执行'

    def skip(data, conn):
        controller._check_lease(data, controller._draft(data, draft_id)['agent_id'], token)
        draft = controller._draft(data, draft_id)
        if draft['state'] not in ('pending', 'rejected'):
            return
        draft.update(state='skipped', target_unavailable=True, target_status=status, error='',
                     version=draft.get('version', 0) + 1)
        for key in ('next_attempt_at', 'quota_wait_bucket'):
            draft.pop(key, None)
        remember_outcome(data, draft, 'skipped', message,
            {'action': draft['action'], 'remote_started': False, 'message': message,
             'target_post_id': target_id(draft)})
        controller._event(data, 'target_unavailable', message, draft['agent_id'], draft_id,
            details={'action': draft['action'], 'target_post_id': target_id(draft), 'status': status})
    controller.store.change(skip)
    return message


def _reject_unsent(controller, draft_id, token, reason):
    def reject(data, conn):
        draft = controller._draft(data, draft_id)
        if draft.get('state') != 'sending' or draft.get('send_token') != token:
            return False
        restore_reservation(data, draft)
        message = str(reason)
        if reason.delivery_reason.startswith('review_'):
            from . import agent_review
            agent_review.remember_hold(draft)
        draft.update(state='rejected', error=message, requires_review=True, review_approved=False,
                     review_reason='执行前检查未通过，操作尚未发送；请重新审阅后手动执行',
                     delivery_not_started=True, delivery_reason=reason.delivery_reason,
                     delivery_not_started_at=utc_now(), version=draft.get('version', 0) + 1)
        if reason.delivery_reason.startswith('review_') and isinstance(draft.get('review'), dict):
            draft['review'] = {**draft['review'], 'status': 'stale',
                               'reason': '发送前审核依据已改变，请重新检查'}
            draft['review_summary'] = '发送前审核依据已改变，请重新检查'
        for key in ('send_token', 'lease_until', 'next_attempt_at', 'quota_wait_bucket'):
            draft.pop(key, None)
        remember_outcome(data, draft, 'rejected', message,
            {'action': draft['action'], 'remote_started': False, 'message': message,
             'target_post_id': target_id(draft), 'target_account_id': draft.get('target_account_id')})
        controller._event(data, 'delivery_not_started', message, draft['agent_id'], draft_id,
            details={'action': draft['action'], 'remote_started': False,
                     'delivery_reason': reason.delivery_reason, 'target_post_id': target_id(draft),
                     'target_account_id': draft.get('target_account_id')})
        return True
    return controller.store.change(reject)


def _check_interaction_policy(controller, draft_id, *, body=None, target_result=None, require_author=False,
                              sending_token=None, automatic=False):
    from . import agent_interaction_policy as policy
    def verify(data, conn):
        draft = controller._draft(data, draft_id)
        agent = controller._agent(data, draft['agent_id'])
        if sending_token is not None and (draft.get('state') != 'sending' or draft.get('send_token') != sending_token):
            raise DeliveryNotStarted('动作状态已改变，未发送新的操作', 'draft_no_longer_sendable')
        if sending_token is not None and draft.get('account_id') != agent.get('account_id'):
            raise DeliveryNotStarted('动作绑定账号已改变，未发送操作', 'account_binding_changed')
        if sending_token is not None:
            try:
                controller._check_lease(data, draft['agent_id'], sending_token)
            except AgentError as exc:
                raise DeliveryNotStarted('执行前角色租约已失效，操作尚未发送', 'role_lease_lost') from exc
            try:
                controller.generations.check_publish(draft['agent_id'], data, draft, automatic=automatic)
            except AgentError as exc:
                raise DeliveryNotStarted('执行前生成授权已改变，操作尚未发送：' + str(exc),
                                         'generation_authorization_changed', exc.status_code) from exc
            from . import agent_review
            try:
                agent_review.check(conn, data, draft, body if body is not None else draft.get('body', ''),
                                   target_result=target_result)
            except AgentError as exc:
                raise DeliveryNotStarted(str(exc), 'review_approval_changed', exc.status_code) from exc
        policy.check(conn, data, agent, draft, body=body, target_result=target_result,
                     manual=_manual(draft), require_author=require_author)
    try:
        controller.store.change(verify)
    except policy.PolicyBlocked as exc:
        controller.store.change(lambda data, conn: policy.mark_blocked(
            controller, data, conn, controller._draft(data, draft_id), exc))
        raise


def _guarded_write(controller, draft_id, function, *, body, target_result, token, automatic, target_loader=None):
    """Authorize after budget waiting, serialized with policy/binding changes.

    The dispatch lock covers the SDK request, so a settings update waits for an
    already-started request. No database transaction spans that network call.
    """
    @contextmanager
    def gate():
        # Refresh outside the transaction/dispatch lock. Comparing the same
        # pre-model snapshot twice would miss an edit during a long review.
        latest = target_result
        from . import agent_review
        if target_loader is not None and agent_review.enabled(controller.store.read()):
            try:
                latest = target_loader()
            except Exception as exc:
                if agent_review.enabled(controller.store.read()):
                    raise DeliveryNotStarted('发送前暂时无法重新核对目标，草稿已保留',
                                             'review_target_unavailable', 409) from exc
        with controller._interaction_dispatch_lock:
            _check_interaction_policy(controller, draft_id, body=body,
                                      target_result=latest, require_author=True, sending_token=token,
                                      automatic=automatic)
            yield

    @wraps(function)
    def guarded(*args, **kwargs):
        with gate():
            return function(*args, **kwargs)
    # The budget wrapper opens the gate before recording a remote attempt.
    # Keeping this callable usable directly also supports injected SDK callers.
    guarded._agent_dispatch_context = gate
    guarded._agent_dispatch_function = function
    return guarded


def reserve(controller, draft_id, body, token, automatic, *, target_result=None, relationship_present=None):
    from . import agent_interaction_policy, agent_relationships, agent_writing, agent_review
    _, Error, lease_seconds, field, _, _, text = _api()
    now = controller.clock()
    def change(data, conn):
        controller._recover(data)
        draft = controller._draft(data, draft_id)
        agent = controller._agent(data, draft['agent_id'])
        controller._check_lease(data, agent['id'], token)
        manual = _manual(draft, automatic)
        _delete_guard(draft, token, manual)
        controller.generations.check_publish(agent['id'], data, draft, automatic=automatic)
        if draft['state'] == 'published':
            return None
        if draft['state'] not in ('pending', 'rejected') or automatic and draft['state'] == 'rejected':
            raise Error('该动作不是待执行状态；不确定动作必须先核对', 409)
        if automatic and (controller._stop.is_set() or not data['settings']['enabled']
                          or not agent['enabled'] or agent.get('mode') != 'auto'):
            raise Error('自动运行已暂停，动作已保留', 409)
        if not manual and not draft_matches_persona(draft, agent):
            raise Error('角色配置已改变，请重新生成动作', 409)
        if draft.get('account_id') is not None and draft['account_id'] != agent.get('account_id'):
            raise Error('动作对应的账号已改变，请重新生成', 409)
        if agent['account_state'] != 'ready' or not agent.get('account_id'):
            raise Error('请先绑定赛事账号')
        if not manual and (draft['action'] not in agent['allowed_actions'] or draft['action'] == 'skip'):
            raise Error('当前角色未允许该动作', 409)
        if any(d['agent_id'] == agent['id'] and d['id'] != draft_id and d['state'] in ('sending', 'uncertain')
               for d in data['drafts'].values()):
            raise Error('该角色有操作结果待核对，已暂停新增操作', 409)
        content = text(body if body is not None else draft['body'], 500, '动作正文',
                       draft['action'] in ('like', 'unlike', 'follow', 'unfollow', 'repost', 'delete_post'))
        if draft['action'] in ('like', 'unlike', 'follow', 'unfollow', 'delete_post') and content:
            raise Error('关系互动动作不能包含正文')
        issues = [] if manual else agent_writing.warnings(content, draft['action'])
        target = target_id(draft)
        allowed = agent_relationships.allowed_post_targets(draft, include_policy_blocked=True)
        if draft['action'] in ('reply', 'repost', 'like', 'unlike', 'delete_post') and (type(target) is not int or target not in allowed):
            raise Error('互动目标无效')
        if draft['action'] == 'delete_post' and (target_result is None or field(target_result, 'author_id') != agent['account_id']
                or type(field(target_result, 'author_id')) is not int or field(target_result, 'post_id') != target
                or type(field(target_result, 'post_id')) is not int):
            raise Error('只能删除已核对属于当前绑定账号的帖子', 409)
        if draft['action'] in ('follow', 'unfollow'):
            account_target = draft.get('target_account_id')
            if (type(account_target) is not int or account_target not in agent_relationships.allowed_account_targets(draft, include_policy_blocked=True)
                    or account_target == agent['account_id']):
                raise Error('关注目标不在阅读资料或已证实关系中，未执行', 409)
        if draft['action'] not in ('post', 'reply', 'repost', 'like', 'unlike', 'follow', 'unfollow', 'delete_post'):
            raise Error('动作类型无效')
        agent_interaction_policy.check(conn, data, agent, draft, body=content,
                                       target_result=target_result, manual=manual)
        agent_review.check(conn, data, draft, content, target_result=target_result)
        relationship = draft['action'] in ('like', 'unlike', 'follow', 'unfollow')
        if relationship:
            if type(relationship_present) is not bool:
                raise Error('尚未完整核对当前关系，未执行', 409)
        rate = data['rate']
        until = rate.get('not_before', 0)
        if until > now:
            error = Error(f'平台限流等待中，约 {int(until - now) + 1} 秒后再试', 429)
            error.retry_after = until - now
            raise error
        if data['leases'].get('publisher', {}).get('until', 0) > now:
            error = Error('统一执行器正在执行其他动作，稍后继续', 409)
            error.wait_kind = 'executor'
            error.retry_after = min(5.0, max(.05, data['leases']['publisher']['until'] - now))
            raise error
        day = datetime.fromtimestamp(now, ZoneInfo(data['settings']['timezone'])).date().isoformat()
        counter_bucket = 'manual_accounts' if manual else 'accounts'
        counters = rate.setdefault(counter_bucket, {})
        old_counter = dict(counters.get(agent['id'], {}))
        attempts = old_counter.get('attempts', 0) if old_counter.get('day') == day else 0
        # Retain counts for audit and old clients, never as an activity quota.
        draft['body'] = content
        draft['style_warnings'] = issues
        if target_result is not None:
            draft['execution_target'] = {key: field(target_result, key) for key in
                ('post_id', 'root_post_id', 'author_id') if type(field(target_result, key)) is int and field(target_result, key) > 0}
        key = fingerprint(draft, agent['account_id'])
        hashes = {k: at for k, at in rate.get('hashes', {}).items() if at > now - 7 * 86400}
        if key in hashes:
            raise Error('最近已经提交过相同内容或目标动作，已阻止重复操作', 409)
        # Reposts remain non-reversible here; keep their exact durable dedup.
        if draft['action'] == 'repost' and any(
                d['id'] != draft_id and d['action'] == draft['action'] and d.get('account_id') == agent['account_id']
                and target_id(d) == target and d['state'] == 'published' for d in data['drafts'].values()):
            raise Error('该账号已经完成此转发，已阻止重复操作', 409)
        hashes[key] = now
        # This transition and cancellation use the same store transaction.
        # A cancellation either retains this draft for review or is rejected
        # after the draft has entered sending; it cannot race the actual write.
        controller.generations.before_publish(agent['id'], data, draft, automatic=automatic)
        if automatic and draft['action'] in ('post', 'reply', 'repost') and not manual:
            from . import agent_competition
            permit = agent_competition.admit(conn, data, agent['id'], 'publish',
                draft_id + ':' + str(draft.get('version', 1)), now)
            if not permit['allowed']:
                error = Error('正在等待竞赛分配的发文机会', 429)
                error.retry_after = max(1, permit.get('retry_after', 2))
                raise error
            draft['competition_allocation'] = permit
        draft['competition_automatic'] = bool(automatic and not manual)
        rate['hashes'] = hashes
        counters[agent['id']] = {'day': day, 'attempts': attempts + 1, 'last_at': now}
        data['leases']['publisher'] = {'token': token, 'owner': controller.owner, 'until': now + lease_seconds}
        draft.update(state='sending', account_id=agent['account_id'], send_token=token,
                     lease_until=now + lease_seconds, send_started_at=now, error='',
                     version=draft['version'] + 1, target_post_id=target,
                     reservation={'counter': old_counter, 'counter_bucket': counter_bucket, 'key': key})
        for key in ('delivery_not_started', 'delivery_reason', 'delivery_not_started_at'):
            draft.pop(key, None)
        draft.pop('requires_review', None)
        draft.pop('review_reason', None)
        if not automatic:
            draft.update(review_approved=True, review_approved_at=utc_now())
        if relationship:
            draft['relationship_already_applied'] = relationship_present == agent_relationships.desired(draft['action'])
            if not draft['relationship_already_applied']:
                relation_key = agent_relationships.key(agent['account_id'],
                    agent_relationships.kind_for(draft['action']), agent_relationships.target_for(draft))
                draft['relationship_preflight'] = dict(data['rate'].get('relationships', {}).get(relation_key, {}))
                # If this process dies after the remote accepts the write, the
                # durable state must not keep presenting its preflight value.
                agent_relationships.observe(data, agent['account_id'], agent_relationships.kind_for(draft['action']),
                    agent_relationships.target_for(draft), None, utc_now(), 'relationship operation in progress')
        controller._event(data, 'sending', '已持久记录操作，正在发送一次请求', agent['id'], draft_id,
            details={'action': draft['action'], 'target_account_id': draft.get('target_account_id'),
                     'target_post_id': target, 'post_id': None, 'manual': manual, 'automatic': automatic})
        return dict(draft)
    return controller.store.change(change)


def resolve_notifications(controller, agent, token, draft_id, *, automatic=True):
    """Acknowledgements cannot change a successful primary action's outcome."""
    from . import agent_observation
    draft = controller._draft(controller.store.read(), draft_id)
    if (draft.get('policy_blocked') or draft.get('requires_review')
            or draft.get('notifications_resolved') or not draft.get('notification_ids')):
        return
    try:
        def authorize(data, conn):
            controller._check_lease(data, agent['id'], token)
            current = controller._draft(data, draft_id)
            if current.get('policy_blocked') or current.get('requires_review') or current.get('notifications_resolved'):
                return False
            current_agent = controller._agent(data, agent['id'])
            if (automatic and current.get('action') == 'skip' and current.get('review_approved') is not True
                    and (controller._stop.is_set() or not data['settings']['enabled']
                         or not current_agent['enabled'] or current_agent.get('mode') != 'auto')):
                return False
            controller.generations.check_publish(agent['id'], data, current, automatic=automatic)
            # Skip has no primary social write, so its acknowledgement is the
            # first external mutation and must close cancellation atomically.
            controller.generations.before_publish(agent['id'], data, current, automatic=automatic)
            return True
        if not controller.store.change(authorize):
            return
        complete = agent_observation.resolve_selected(controller, agent, token, draft['notification_ids'], draft_id=draft_id)
        def done(data, conn):
            controller._check_lease(data, agent['id'], token)
            controller._draft(data, draft_id).update(notifications_resolved=bool(complete),
                notification_error='' if complete else '通知确认暂未完成，将单独重试')
        controller.store.change(done)
    except Exception:
        def failed(data, conn):
            row = controller._draft(data, draft_id)
            row.update(notifications_resolved=False, notification_error='通知确认暂未完成，将单独重试')
        controller.store.change(failed)


def _competition_receipt(controller, data, conn, draft):
    # Optional competition accounting cannot turn an acknowledged delivery into
    # an uncertain remote result. The next maintenance pass also syncs receipts.
    from . import agent_competition
    from .ingest import _atomic
    try:
        with _atomic(conn):
            agent_competition.record_published(conn, data, draft, controller.clock())
    except Exception:
        controller._event(data, 'competition_error', '竞赛归属记录待重试；发布回执已保留',
                          draft['agent_id'], draft['id'])


def _finish(controller, draft_id, agent, token, post_id, already_applied=False, remote_result=None):
    _, Error, _, _, _, _, _ = _api()
    def change(data, conn):
        controller._check_lease(data, agent['id'], token)
        draft = controller._draft(data, draft_id)
        if draft.get('send_token') != token or draft['state'] != 'sending':
            raise Error('操作租约已改变，请核对远端结果', 409)
        draft['post_id'] = post_id
        if draft['action'] == 'delete_post':
            draft['delete_verification'] = 'authenticated_delete_acknowledgement'
        if draft['action'] in ('like', 'unlike', 'follow', 'unfollow'):
            draft['relationship_already_applied'] = already_applied
        if already_applied and draft['action'] == 'repost':
            # Exact my_reposts proves the relationship, not this new comment.
            draft['requested_body'] = draft['body']
            draft['body'] = remote_result.get('body', '') if isinstance(remote_result, dict) else ''
        from . import agent_tracking
        agent_tracking.record_success(data, conn, draft, agent, remote_result, utc_now())
        message = ('远端关系已符合此动作，已核对并避免重复操作' if already_applied and
                   draft['action'] in ('like', 'unlike', 'follow', 'unfollow') else
                   '远端已存在该互动，已核对并避免重复操作' if already_applied else '赛事操作执行成功')
        draft.update(state='published', post_id=post_id, published_at=utc_now(), error='')
        from . import agent_waiting
        if agent_waiting.complete_action(data, draft, controller.clock()):
            controller._event(data, 'generation_completed', '等待中的动作已执行成功，本轮任务已完成',
                agent['id'], draft_id, details={'job_id': draft.get('generation_job_id'), 'stage': 'complete'})
        _competition_receipt(controller, data, conn, draft)
        draft.pop('reservation', None)
        remember_outcome(data, draft, 'published', message,
                         {'action': draft['action'], 'target_post_id': target_id(draft),
                          'target_account_id': draft.get('target_account_id'),
                          'post_id': post_id, 'already_applied': already_applied, 'message': message})
        controller._event(data, 'published', message, agent['id'], draft_id,
            details={'action': draft['action'], 'target_account_id': draft.get('target_account_id'),
                     'target_post_id': target_id(draft), 'post_id': post_id,
                     'already_applied': already_applied, 'reconciled': False,
                     'manual': draft.get('origin') == 'manual'})
    controller.store.change(change)


def _following(controller, account, account_id, target):
    """A positive match is exact; an absent relation requires every page."""
    from . import agent_relationships
    budget, Error, _, field, _, _, _ = _api()
    cursor, seen, present = None, set(), False
    for _ in range(agent_relationships.MAX_FOLLOWING_PAGES):
        page = budget.call(controller, 'read', account.following, account_id, limit=50, cursor=cursor)
        if not isinstance(page, (list, tuple)):
            raise Error('关注列表返回不完整，当前关系未知', 502)
        for row in page:
            remote_id = field(row, 'account_id')
            if type(remote_id) is not int or remote_id <= 0:
                raise Error('关注列表存在无效记录，当前关系未知', 502)
            if remote_id == target:
                return True
        cursor = getattr(page, 'next_cursor', None)
        if cursor is None:
            return present
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise Error('关注列表分页无法完整核对，当前关系未知', 502)
        seen.add(cursor)
    raise Error('关注列表过长，本轮未完整核对，当前关系未知', 409)


def _observe_relationship(controller, agent, draft, account):
    from . import agent_relationships
    budget, _, _, _, _, _, _ = _api()
    action, target = draft['action'], agent_relationships.target_for(draft)
    kind = agent_relationships.kind_for(action)
    try:
        if kind == 'follow':
            present = _following(controller, account, agent['account_id'], target)
            source = ('authenticated following exact target match' if present else
                      'authenticated following complete pagination')
        else:
            liked = budget.call(controller, 'read', account.my_likes, [target])
            if (not isinstance(liked, (set, list, tuple)) or
                    any(type(post_id) is not int or post_id != target for post_id in liked)):
                _, Error, _, _, _, _, _ = _api()
                raise Error('点赞关系返回无效，当前关系未知', 502)
            present = target in liked
            source = 'authenticated exact my_likes'
    except Exception:
        controller.store.change(lambda data, conn: agent_relationships.observe(
            data, agent['account_id'], kind, target, None, utc_now(), 'incomplete authenticated relationship read'))
        raise
    controller.store.change(lambda data, conn: agent_relationships.observe(
        data, agent['account_id'], kind, target, present, utc_now(), source))
    return present


def _authenticated_identity(controller, account, account_id):
    budget, Error, _, field, _, _, _ = _api()
    result = budget.call(controller, 'read', account.me)
    profile = field(result, 'account', {})
    if (type(field(profile, 'account_id')) is not int or field(profile, 'account_id') != account_id
            or type(field(account, 'account_id')) is not int or field(account, 'account_id') != account_id):
        raise Error('认证账号与原删除操作不一致，未执行或结算', 409)


def _reliable_missing(exc, post_id):
    from urllib.parse import urlsplit
    from capturethenarrative._http import APIError
    return (isinstance(exc, APIError) and exc.status == 404 and exc.method.upper() == 'GET'
            and urlsplit(exc.url).path.rstrip('/').endswith('/posts/' + str(post_id)))


def execute(controller, draft_id, body, token, automatic=False):
    from . import agent_interaction_policy as policy, agent_relationships
    budget, Error, _, field, _, status_of, text = _api()
    current = controller.store.read()
    original = controller._draft(current, draft_id)
    manual = _manual(original, automatic)
    if original['state'] == 'published':
        return
    # Do not establish sessions or fetch targets for forbidden retries.
    if original['state'] not in ('pending', 'rejected'):
        raise Error('该动作不能重新发送；请先核对不确定结果', 409)
    _delete_guard(original, token, manual)
    agent = controller._agent(current, original['agent_id'])
    if any(d['agent_id'] == agent['id'] and d['id'] != draft_id and d['state'] in ('sending', 'uncertain')
           for d in current['drafts'].values()):
        raise Error('该角色有操作结果待核对，已暂停新增操作', 409)
    controller.generations.check_publish(agent['id'], current, original, automatic=automatic)
    if not manual and (not draft_matches_persona(original, agent) or original['action'] not in agent['allowed_actions']):
        raise Error('角色配置或允许动作已改变，请重新生成动作', 409)
    if original.get('account_id') is not None and original['account_id'] != agent.get('account_id'):
        raise Error('动作对应的账号已改变，请重新生成', 409)
    if manual and (type(original.get('account_id')) is not int or original['account_id'] != agent.get('account_id')):
        raise Error('人工指令缺少原绑定账号，未执行', 409)
    target = target_id(original)
    if original['action'] in ('reply', 'repost', 'like', 'unlike', 'delete_post') and (
            type(target) is not int or target not in agent_relationships.allowed_post_targets(original, include_policy_blocked=True)):
        raise Error('互动目标不在原阅读资料中，未执行', 409)
    if original['action'] in ('follow', 'unfollow'):
        account_target = original.get('target_account_id')
        if (type(account_target) is not int or account_target not in agent_relationships.allowed_account_targets(original, include_policy_blocked=True)
                or account_target == agent.get('account_id')):
            raise Error('关注目标不在原阅读资料或已证实关系中，未执行', 409)
    if automatic and (controller._stop.is_set() or not controller.store.read()['settings']['enabled']
                      or not agent['enabled'] or agent.get('mode') != 'auto'):
        raise Error('自动运行已暂停，动作已保留', 409)
    content = text(body if body is not None else original['body'], 500, '动作正文',
                   original['action'] in ('like', 'unlike', 'follow', 'unfollow', 'repost', 'delete_post'))
    if original['action'] in ('like', 'unlike', 'follow', 'unfollow', 'delete_post') and content:
        raise Error('关系互动动作不能包含正文')
    _check_interaction_policy(controller, draft_id, body=content)
    if automatic and original['action'] in ('post', 'reply', 'repost') and not manual:
        controller._competition_publish_permit(draft_id, token)
    client = controller._sdk()
    reserved = None
    try:
        account = controller._account(client, agent, token)
        remote_target = None
        existing = None
        relationship_present = None
        if original['action'] in ('reply', 'repost', 'like', 'unlike'):
            try:
                remote_target = budget.call(controller, 'read', account.get_post if manual else client.get_post, target)
            except Exception as exc:
                if status_of(exc) not in TARGET_GONE_STATUSES:
                    raise
                if manual:
                    raise Error(f'互动目标已被删除或隐藏（平台返回 {status_of(exc)}），未执行', 409) from None
                raise Error(_target_gone(controller, draft_id, token, status_of(exc)), 409) from None
            if field(remote_target, 'post_id') != target:
                raise Error('远端互动目标不一致，未执行', 409)
            _check_interaction_policy(controller, draft_id, body=content,
                                      target_result=remote_target, require_author=True)
            if (not manual and original['action'] in ('like', 'repost', 'unlike')
                    and field(remote_target, 'author_id') == agent['account_id']):
                raise Error('跳过对自己帖子的重复互动', 409)
        elif original['action'] == 'delete_post':
            _authenticated_identity(controller, account, agent['account_id'])
            remote_target = budget.call(controller, 'read', account.get_post, target)
            if (type(field(remote_target, 'post_id')) is not int or field(remote_target, 'post_id') != target
                    or type(field(remote_target, 'author_id')) is not int or field(remote_target, 'author_id') != agent['account_id']):
                raise Error('只能删除当前绑定账号本人发布的帖子', 409)
        if original['action'] in ('follow', 'unfollow'):
            profile = budget.call(controller, 'read', account.get_account if manual else client.get_account, account_target)
            if field(profile, 'account_id') != account_target:
                raise Error('远端关注目标不一致，未执行', 409)
            remote_target = profile
        if original['action'] in ('like', 'unlike', 'follow', 'unfollow'):
            relationship_present = _observe_relationship(controller, agent, original, account)
            existing = relationship_present == agent_relationships.desired(original['action'])
        elif original['action'] == 'repost':
            existing = budget.call(controller, 'read', account.my_reposts, [target]).get(target)
        from . import agent_review
        agent_review.ensure(controller, draft_id, content, target_result=remote_target,
                            automatic=automatic, token=token)
        reserved = reserve(controller, draft_id, body, token, automatic, target_result=remote_target,
                           relationship_present=relationship_present)
        if reserved is None:
            return
        try:
            action = reserved['action']
            confirmed_result = None
            def write(bucket, method, *args, **kwargs):
                loader = None
                if original['action'] in ('reply', 'repost', 'like'):
                    loader = lambda: budget.call(controller, 'read',
                        account.get_post if manual else client.get_post, target)
                elif original['action'] == 'follow':
                    loader = lambda: budget.call(controller, 'read',
                        account.get_account if manual else client.get_account, account_target)
                # D26: manual posts, then replies to notifications, other replies and posts.
                with budget.publish_priority(budget.priority_for(original), key=draft_id):
                    return budget.call(controller, bucket, _guarded_write(controller, draft_id, method,
                        body=reserved['body'], target_result=remote_target, token=token, automatic=automatic,
                        target_loader=loader), *args, **kwargs)
            if existing:
                post_id = target if action == 'like' else existing if action == 'repost' else None
                confirmed_result = {'post_id': post_id} if action == 'repost' else None
            elif action == 'like':
                write('like', account.like, target)
                # The official SDK intentionally returns None for a successful like.
                post_id = target
            elif action in ('unlike', 'follow', 'unfollow'):
                relationship_target = agent_relationships.target_for(reserved)
                write(action, getattr(account, action), relationship_target)
                post_id = None
            elif action == 'delete_post':
                write('delete_post', account.delete_post, target)
                post_id = None
            elif action == 'repost':
                result = write('post', account.repost, target, comment=reserved['body'] or None)
                post_id = field(result, 'post_id')
                confirmed_result = result
            elif action == 'reply':
                result = write('post', account.reply, target, reserved['body'],
                                     root_post_id=field(remote_target, 'root_post_id') or target)
                post_id = field(result, 'post_id')
                confirmed_result = result
            else:
                result = write('post', account.post, reserved['body'])
                post_id = field(result, 'post_id')
                confirmed_result = result
            if action not in ('unlike', 'follow', 'unfollow', 'delete_post') and (type(post_id) is not int or post_id <= 0):
                raise Error('平台已接收请求但返回结果无法确认，请核对操作结果', 502)
            _finish(controller, draft_id, agent, token, post_id, bool(existing), confirmed_result)
        except (policy.PolicyBlocked, DeliveryNotStarted):
            raise
        except Exception as exc:
            safe = controller._remote_failure(exc)
            def failed(data, conn):
                draft = controller._draft(data, draft_id)
                if draft.get('send_token') != token or draft['state'] != 'sending':
                    return
                status = status_of(exc)
                definite = not isinstance(exc, Error) and status in (400, 401, 403, 404, 409, 422, 429)
                state = ('pending' if status == 429 else 'rejected') if definite else 'uncertain'
                message = ('平台明确拒绝操作，可修改后重新提交' if definite else
                           '请求结果无法确认，请核对远端结果；禁止自动重发')
                if status == 429:
                    message = '操作预算等待中，动作已保留'
                draft.update(state=state, error=message)
                if state == 'uncertain' and draft['action'] in ('like', 'unlike', 'follow', 'unfollow'):
                    agent_relationships.observe(data, agent['account_id'], agent_relationships.kind_for(draft['action']),
                        agent_relationships.target_for(draft), None, utc_now(), 'operation outcome not yet confirmed')
                if definite:
                    previous_relation = draft.get('relationship_preflight')
                    if isinstance(previous_relation, dict) and previous_relation:
                        relation_key = agent_relationships.key(agent['account_id'],
                            agent_relationships.kind_for(draft['action']), agent_relationships.target_for(draft))
                        data['rate'].setdefault('relationships', {})[relation_key] = dict(previous_relation)
                    reservation = draft.pop('reservation', {})
                    data['rate'].get('hashes', {}).pop(reservation.get('key'), None)
                    # A local budget deferral never reached the remote endpoint.
                    if isinstance(exc, budget.Deferred):
                        data['rate'].setdefault(reservation.get('counter_bucket', 'accounts'), {})[agent['id']] = reservation.get('counter', {})
                remember_outcome(data, draft, state, message)
                controller._event(data, state, message, agent['id'], draft_id,
                    details={'action': draft['action'], 'target_account_id': draft.get('target_account_id'),
                             'target_post_id': target_id(draft), 'post_id': draft.get('post_id'),
                             'reconciled': False})
            controller.store.change(failed)
            raise safe from None
        # Separate idempotent ack outbox from the successful primary write.
        resolve_notifications(controller, agent, token, draft_id, automatic=automatic)
    except DeliveryNotStarted as exc:
        _reject_unsent(controller, draft_id, token, exc)
        raise
    except policy.PolicyBlocked as exc:
        controller.store.change(lambda data, conn: policy.mark_blocked(
            controller, data, conn, controller._draft(data, draft_id), exc))
        raise
    except Exception as exc:
        raise controller._remote_failure(exc) from None
    finally:
        controller._close(client)
        if reserved:
            def release(data, conn):
                if data['leases'].get('publisher', {}).get('token') == token:
                    data['leases'].pop('publisher', None)
            controller.store.change(release)


def reconcile(controller, draft_id, post_id=None):
    budget, Error, _, field, _, _, _ = _api()
    draft = controller._draft(controller.store.read(), draft_id)
    with controller._claim(draft['agent_id']) as token:
        data = controller.store.read()
        current = controller._draft(data, draft_id)
        agent = controller._agent(data, current['agent_id'])
        if current['state'] == 'published':
            return controller.snapshot()
        if current['state'] != 'uncertain':
            raise Error('仅操作结果待核对的动作需要结算', 409)
        target = target_id(current)
        client = controller._sdk()
        try:
            action = current['action']
            confirmed_result = None
            if action == 'delete_post':
                if not _manual(current) or current.get('delete_confirmed') is not True:
                    raise Error('原删除操作缺少明确人工确认，保持待核对', 409)
                if current.get('account_id') != agent.get('account_id'):
                    raise Error('原删除操作账号与当前绑定不同，请恢复原账号后核对', 409)
                execution_target = current.get('execution_target', {})
                if (type(target) is not int or execution_target.get('post_id') != target
                        or execution_target.get('author_id') != agent['account_id']):
                    raise Error('原删除目标缺少已核对的本人作者证据，保持待核对', 409)
                account = controller._account(client, agent, token)
                _authenticated_identity(controller, account, agent['account_id'])
                try:
                    budget.call(controller, 'read', account.get_post, target)
                except Exception as exc:
                    if not _reliable_missing(exc, target):
                        raise Error('尚未通过已认证账号确认帖子缺失，保持待核对', 409) from None
                else:
                    raise Error('远端帖子仍然存在，删除结果保持待核对，禁止重发', 409)
                post_id = None
            elif action in ('like', 'repost', 'unlike', 'follow', 'unfollow'):
                if current.get('account_id') != agent.get('account_id'):
                    raise Error('原动作账号与当前绑定不同，请恢复原账号后核对', 409)
                account = controller._account(client, agent, token)
                if action in ('like', 'unlike', 'follow', 'unfollow'):
                    from . import agent_relationships
                    remote_present = _observe_relationship(controller, agent, current, account)
                    present = remote_present == agent_relationships.desired(action)
                    confirmed_id = target if action == 'like' else None
                else:
                    confirmed_id = budget.call(controller, 'read', account.my_reposts, [target]).get(target)
                    present = bool(confirmed_id)
                    if present and post_id is not None and post_id != confirmed_id:
                        raise Error('远端转发 ID 与输入不一致，未结算', 409)
                if not present:
                    raise Error('远端尚未确认此账号的互动关系，保持待核对并禁止自动重发', 409)
                post_id = confirmed_id
                if action == 'repost':
                    confirmed_result = {'post_id': post_id}
            else:
                from .agent_control import _integer
                post_id = _integer(post_id, 1, 2**63 - 1, '远端帖子 ID')
                if _manual(current):
                    if current.get('account_id') != agent.get('account_id'):
                        raise Error('原人工操作账号与当前绑定不同，请恢复原账号后核对', 409)
                    account = controller._account(client, agent, token)
                    result = budget.call(controller, 'read', account.get_post, post_id)
                else:
                    result = budget.call(controller, 'read', client.get_post, post_id)
                confirmed_result = result
                if field(result, 'author_id') != current['account_id'] or field(result, 'body', '').strip() != current['body'].strip():
                    raise Error('远端作者或正文与原操作不一致，未结算', 409)
                parent = field(result, 'reply_to_post_id', field(result, 'reply_to'))
                if (action == 'reply' and parent != target) or (action == 'post' and parent is not None):
                    raise Error('远端回复关系与原操作不一致，未结算', 409)
                if field(result, 'repost_of_post_id') is not None or field(result, 'kind') == 'repost':
                    raise Error('远端帖类型与原操作不一致，未结算', 409)
        except Exception as exc:
            raise controller._remote_failure(exc) from None
        finally:
            controller._close(client)
        def verified(state, conn):
            controller._check_lease(state, draft['agent_id'], token)
            row = controller._draft(state, draft_id)
            if row['state'] != 'uncertain':
                raise Error('动作状态已改变，请刷新', 409)
            row['post_id'] = post_id
            if row['action'] == 'delete_post':
                row['delete_verification'] = 'authenticated_owner_get_404'
            if row['action'] == 'repost':
                row['requested_body'] = row['body']
                row['body'] = confirmed_result.get('body', '') if isinstance(confirmed_result, dict) else ''
            from . import agent_tracking
            agent_tracking.record_success(state, conn, row, agent, confirmed_result, utc_now())
            row.update(state='published', post_id=post_id, published_at=utc_now(), error='')
            from . import agent_waiting
            if agent_waiting.complete_action(state, row, controller.clock()):
                controller._event(state, 'generation_completed', '已核对动作执行成功，本轮任务已完成',
                    row['agent_id'], draft_id, details={'job_id': row.get('generation_job_id'), 'stage': 'complete'})
            _competition_receipt(controller, state, conn, row)
            row.pop('reservation', None)
            remember_outcome(state, row, 'published', '已通过远端只读核对确认操作结果')
            controller._event(state, 'reconciled', '已通过远端只读核对确认操作结果', row['agent_id'], draft_id,
                details={'action': row['action'], 'target_account_id': row.get('target_account_id'),
                         'target_post_id': target_id(row), 'post_id': post_id, 'reconciled': True,
                         'already_applied': bool(row.get('relationship_already_applied')),
                         'manual': row.get('origin') == 'manual'})
        try:
            controller.store.change(verified)
        except Exception as exc:
            raise controller._remote_failure(exc) from None
        resolve_notifications(controller, agent, token, draft_id)
    return controller.snapshot()


def discard_unexecuted(controller, draft_id, *, confirmed_not_executed, reason,
                       expected_version, confirmed_by):
    """Resolve an ambiguous delivery only after an explicit human attestation.

    This neither repeats nor reverses the remote request. Keep the original
    attempted action and the member's evidence in the durable audit trail.
    """
    from .agent_control import _integer, _text
    _, Error, _, _, _, _, _ = _api()
    if confirmed_not_executed is not True:
        raise Error('请明确确认已检查远端且该操作未执行')
    reason = _text(reason, 1000, '未执行确认依据', empty=False)
    if len(reason) < 3:
        raise Error('请填写至少 3 个字的未执行确认依据')
    confirmed_by = _text(confirmed_by, 200, '确认人', empty=False)
    expected_version = _integer(expected_version, 1, 2**31 - 1, '草稿版本')
    draft = controller._draft(controller.store.read(), draft_id)
    with controller._claim(draft['agent_id']) as token:
        def abandon(data, conn):
            controller._check_lease(data, draft['agent_id'], token)
            row = controller._draft(data, draft_id)
            resolution = row.get('manual_resolution', {})
            if (row['state'] == 'discarded' and resolution.get('kind') == 'confirmed_not_executed'
                    and resolution.get('previous_version') == expected_version):
                return
            if row['state'] != 'uncertain' or row.get('version', 1) != expected_version:
                raise Error('草稿状态或版本已改变，请刷新后重新核对', 409)
            at = utc_now()
            row['manual_resolution'] = {
                'kind': 'confirmed_not_executed', 'confirmed_by': confirmed_by,
                'confirmed_at': at, 'reason': reason, 'previous_version': expected_version,
                'previous_state': 'uncertain',
            }
            reservation = row.pop('reservation', {})
            hashes = data['rate'].get('hashes', {})
            key = reservation.get('key')
            if key and hashes.get(key) == row.get('send_started_at'):
                hashes.pop(key, None)
            publisher = data['leases'].get('publisher', {})
            if publisher.get('token') == row.get('send_token'):
                data['leases'].pop('publisher', None)
            row.update(state='discarded', error='', version=expected_version + 1)
            message = f'成员 {confirmed_by} 人工确认操作未执行并放弃。依据：{reason}'
            remember_outcome(data, row, 'discarded', message)
            controller._event(data, 'discard_unexecuted', message, row['agent_id'], draft_id,
                details={'action': row['action'], 'account_id': row.get('account_id'),
                         'target_post_id': target_id(row),
                         'target_account_id': row.get('target_account_id'),
                         'draft_state': 'discarded', 'result': 'human_confirmed_not_executed',
                         'manual': True, 'reconciled': False})
        controller.store.change(abandon)
    return controller.snapshot()
