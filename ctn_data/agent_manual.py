"""Prepare explicit operator actions without writing to the social platform.

Manual drafts retain their human origin. An authenticated, read-only preview
establishes the actor and target before the existing delivery outbox is used.
Request identities survive the bounded outbox, preventing a browser retry from
silently preparing another operation after its earlier draft was retired.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
import uuid

from . import agent_budget, agent_relationships
from .agent_control import AgentError, _field, _identifier, _integer, _text
from .db import connect, utc_now


ACTIONS = frozenset(('post', 'reply', 'repost', 'like', 'unlike', 'follow', 'unfollow', 'delete_post'))
# D26: a confirmed manual post waits for the shared post allowance this long.
QUEUE_SECONDS = 15 * 60
_QUEUE_ACTIONS = frozenset(('post', 'reply', 'repost'))
# Local refusals that prove no social write was attempted; the draft stays pending.
_QUEUE_TRANSIENT = ('该角色已有任务运行', '服务正在维护', '统一执行器正在执行其他动作',
                    '请先等待或取消该角色正在进行的决策', '角色正在阅读或生成')
_POST_TARGET_ACTIONS = frozenset(('reply', 'repost', 'like', 'unlike', 'delete_post'))
_ACCOUNT_TARGET_ACTIONS = frozenset(('follow', 'unfollow'))
_BODY_ACTIONS = frozenset(('post', 'reply', 'repost'))
_FIELDS = frozenset(('agent_id', 'action', 'body', 'target_post_id', 'target_account_id', 'idempotency_key',
                     'acknowledge_warnings', 'fact_ids', 'concern'))
_ACTIVE = frozenset(('queued', 'running', 'cancelling'))
# D29: platform mention limits, and the target conditions a member must accept
# before a manual reply is prepared or a queued one is retried.
MENTION_LIMITS = {'post': 4, 'repost': 4, 'reply': 3}
MAX_FACT_IDS = 6
_MENTION = re.compile(r'(?<![A-Za-z0-9_@])@([A-Za-z0-9_]{3,30})(?![A-Za-z0-9_])')
BLOCKING_WARNINGS = frozenset(('bot_like_author', 'thread_too_deep', 'team_already_replied', 'target_check_unavailable'))
_WARNING_TEXT = {
    'bot_like_author': '作者疑似机器人账号，回复它不计分',
    'thread_too_deep': '对话已到第 8 层或更深，NPC 很少再回',
    'team_already_replied': '我方已有账号回复过这条帖子',
    'target_check_unavailable': '暂时无法核对目标情况',
    'thread_reply_limit': '我方在这个线程里已有 2 条回复',
    'answered_recently': '6 小时内我方已回答过这位作者的同一顾虑',
    'not_persuadable': '对方已偏 Yes 或话题无关，说服机会小'}
_CONTEXT_DEPTH = 6
_LIVE_REPLY_STATES = frozenset(('pending', 'sending', 'uncertain', 'published'))


def _mentions(body):
    return len(_MENTION.findall(unicodedata.normalize('NFKC', body)))


def _fact_ids(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_FACT_IDS or any(
            not isinstance(item, str) or not item.strip() or len(item) > 80 for item in value):
        raise AgentError(f'事实编号须为最多 {MAX_FACT_IDS} 个非空字符串')
    return list(dict.fromkeys(item.strip() for item in value))


def _request(payload):
    if not isinstance(payload, dict) or set(payload) - _FIELDS:
        raise AgentError('手动操作包含未知字段')
    agent_id = _identifier(payload.get('agent_id'))
    action = payload.get('action')
    if not isinstance(action, str) or action not in ACTIONS:
        raise AgentError('手动操作类型无效')
    request_id = _identifier(payload.get('idempotency_key'))
    body = _text(payload.get('body', ''), 500, '操作正文', action not in ('post', 'reply'))
    if action not in _BODY_ACTIONS and body:
        raise AgentError('关系操作和删除帖子不能包含正文')
    limit = MENTION_LIMITS.get(action)
    if limit is not None and _mentions(body) > limit:
        raise AgentError(f'{"回复" if action == "reply" else "帖子"}最多 @ {limit} 个账号，现在有 {_mentions(body)} 个')
    acknowledged = payload.get('acknowledge_warnings', False)
    if type(acknowledged) is not bool:
        raise AgentError('acknowledge_warnings 须为布尔值')
    fact_ids, concern = _fact_ids(payload.get('fact_ids')), payload.get('concern')
    if concern is not None:
        from . import fact_bank
        if not isinstance(concern, str) or concern not in fact_bank.CONCERNS | {'none'}:
            raise AgentError('顾虑类别无效')
    if (fact_ids or concern is not None) and action not in _BODY_ACTIONS:
        raise AgentError('只有发帖、回复和转发可以记录事实和顾虑')
    post_id, account_id = payload.get('target_post_id'), payload.get('target_account_id')
    if action in _POST_TARGET_ACTIONS:
        post_id = _integer(post_id, 1, 2 ** 63 - 1, '目标帖子 ID')
    elif post_id is not None:
        raise AgentError('此操作不能指定帖子目标')
    if action in _ACCOUNT_TARGET_ACTIONS:
        account_id = _integer(account_id, 1, 2 ** 63 - 1, '目标账号 ID')
    elif account_id is not None:
        raise AgentError('此操作不能指定账号目标')
    normalized = {'agent_id': agent_id, 'action': action, 'body': body,
                  'target_post_id': post_id, 'target_account_id': account_id}
    # Optional fields join the identity only when used, so earlier request hashes still match.
    for key, value in (('acknowledge_warnings', acknowledged), ('fact_ids', fact_ids), ('concern', concern)):
        if value:
            normalized[key] = value
    fingerprint = hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False,
                                            separators=(',', ':')).encode()).hexdigest()
    return normalized, request_id, fingerprint


def _existing(data, agent, request_id, fingerprint):
    row = data['rate'].get('manual_requests', {}).get(request_id)
    if row is None:
        return None
    if not isinstance(row, dict) or not isinstance(row.get('draft_id'), str):
        raise AgentError('手动请求索引无法读取，请检查操作记录', 500)
    if (row.get('agent_id') != agent['id'] or row.get('account_id') != agent.get('account_id')
            or row.get('payload_hash') != fingerprint):
        raise AgentError('此请求标识已用于其他操作或账号，请勿重复使用', 409)
    draft = data['drafts'].get(row['draft_id'])
    if draft is None:
        raise AgentError('此请求已登记，原草稿已不在当前列表；为避免重复操作，未创建新动作', 409)
    if draft.get('agent_id') != agent['id'] or draft.get('account_id') != agent.get('account_id'):
        raise AgentError('原手动请求对应账号已改变，未创建新动作', 409)
    return _result(draft, True)


def _result(draft, idempotent):
    public = copy.deepcopy(draft)
    for name in ('send_token', 'delete_confirmation_token'):
        public.pop(name, None)
    return {'draft_id': draft['id'], 'agent_id': draft['agent_id'], 'draft': public,
            'preview': copy.deepcopy(draft.get('manual_preview', {})), 'idempotent': idempotent}


def _bound(agent):
    if (agent.get('account_state') != 'ready' or type(agent.get('account_id')) is not int
            or agent['account_id'] <= 0):
        raise AgentError('请先绑定并核对赛事账号')


def _idle(agent):
    if agent.get('generation', {}).get('status') in _ACTIVE:
        raise AgentError('角色正在阅读或生成，请先等待完成或取消该任务', 409)


def _manual_count(data, agent_id):
    return sum(row.get('agent_id') == agent_id and row.get('origin') == 'manual'
               and row.get('state') in ('pending', 'rejected') for row in data['drafts'].values())


def _public_string(remote, field, maximum):
    value = _field(remote, field)
    return value[:maximum] if isinstance(value, str) else None


def _positive(remote, field):
    value = _field(remote, field)
    return value if type(value) is int and value > 0 else None


def _post_source(remote, expected, at):
    actual = _positive(remote, 'post_id')
    if actual != expected:
        raise AgentError('远端帖子 ID 与指定目标不一致，未创建手动草稿', 409)
    body = _field(remote, 'body')
    text = body if isinstance(body, str) else ''
    author_id = _positive(remote, 'author_id')
    source = {'id': f'post:{actual}', 'kind': 'post', 'post_id': actual, 'author_id': author_id,
        'text': text[:2000], 'text_length': len(text) if isinstance(body, str) else None,
        'text_truncated': len(text) > 2000,
        'coverage': ('excerpt' if len(text) > 2000 else 'full') if isinstance(body, str) else 'metadata_only',
        'title': text[:160] or f'帖子 #{actual}', 'origin': 'manual_target', 'observed_at': at,
        'post_kind': _public_string(remote, 'kind', 30),
        'visibility': _public_string(remote, 'visibility', 30),
        'root_post_id': _positive(remote, 'root_post_id'),
        'reply_to_post_id': _positive(remote, 'reply_to_post_id') or _positive(remote, 'reply_to'),
        'repost_of_post_id': _positive(remote, 'repost_of_post_id'),
        'published_at': _public_string(remote, 'created_at', 80),
        'edited_at': _public_string(remote, 'edited_at', 80), 'notification_ids': []}
    username = _public_string(remote, 'username', 100)
    if username is not None:
        source['username'] = username
    source['version'] = hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False,
                                                 separators=(',', ':')).encode()).hexdigest()
    preview = {'post_id': actual, 'author_id': author_id, 'body': text}
    if username is not None:
        preview['username'] = username
    return source, preview


def _account_source(remote, expected, at):
    actual = _positive(remote, 'account_id')
    if actual != expected:
        raise AgentError('远端账号 ID 与指定目标不一致，未创建手动草稿', 409)
    username = _public_string(remote, 'username', 100)
    display_name = _public_string(remote, 'display_name', 200)
    # author_id is the account's actual returned identity, allowing the same
    # exact target validator as autonomous evidence without inventing a post.
    source = {'id': f'account:{actual}', 'kind': 'account', 'account_id': actual, 'author_id': actual,
        'username': username, 'display_name': display_name, 'title': display_name or username or f'账号 #{actual}',
        'text': '', 'coverage': 'profile_only', 'origin': 'manual_target', 'observed_at': at,
        'notification_ids': []}
    for key in ('private', 'verified'):
        value = _field(remote, key)
        if type(value) is bool:
            source[key] = value
    source['version'] = hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False,
                                                 separators=(',', ':')).encode()).hexdigest()
    return source, {'account_id': actual, 'username': username, 'display_name': display_name}


def _own_accounts(data, conn):
    own = {a['account_id'] for a in data.get('agents', {}).values() if type(a.get('account_id')) is int}
    try:
        own.update(row['account_id'] for row in conn.execute('SELECT account_id FROM team_accounts WHERE is_own=1'))
    except Exception:
        pass
    return own


def _team_replied(controller, data, post_id, exclude=None):
    """Whether any of our accounts already replied to this post, including drafts in flight."""
    if any(d.get('id') != exclude and d.get('action') == 'reply' and d.get('state') in _LIVE_REPLY_STATES
           and d.get('target_post_id', d.get('reply_to')) == post_id for d in data.get('drafts', {}).values()):
        return True
    conn = connect(controller.db_path)
    try:
        own = sorted(_own_accounts(data, conn))
        return bool(own) and conn.execute(
            'SELECT 1 FROM posts WHERE reply_to_post_id=? AND author_id IN (' + ','.join('?' for _ in own) + ') LIMIT 1',
            (post_id, *own)).fetchone() is not None
    finally:
        conn.close()


def _blocking_codes(controller, data, source, exclude=None):
    """Target conditions that need a member's acknowledgement; unverifiable counts as one."""
    from . import agent_targeting
    post_id, codes = source.get('post_id'), []
    try:
        if source.get('author_id') in agent_targeting.bot_accounts(controller.db_path):
            codes.append('bot_like_author')
        if agent_targeting.depths_for(controller.db_path, [post_id]).get(post_id, 0) >= agent_targeting.MAX_REPLY_DEPTH:
            codes.append('thread_too_deep')
        if _team_replied(controller, data, post_id, exclude):
            codes.append('team_already_replied')
    except Exception:
        codes.append('target_check_unavailable')
    return codes


def _target_warnings(controller, data, source):
    from . import agent_targeting
    codes = _blocking_codes(controller, data, source)
    try:
        root = agent_targeting.thread_root(source)
        if (agent_targeting.team_thread_replies(controller.db_path, data, [root]).get(root, 0)
                >= agent_targeting.MAX_TEAM_REPLIES_PER_THREAD):
            codes.append('thread_reply_limit')
        assessment = agent_targeting.classify(source.get('text'))
        answered = agent_targeting.answered_concerns(data, controller.clock()).get(source.get('author_id'))
        if answered and (not assessment['concerns'] or set(assessment['concerns']) <= answered):
            codes.append('answered_recently')
        if assessment['stance'] not in agent_targeting.PERSUADABLE_STANCES:
            codes.append('not_persuadable')
    except Exception:
        if 'target_check_unavailable' not in codes:
            codes.append('target_check_unavailable')
    return [{'code': code, 'blocking': code in BLOCKING_WARNINGS, 'message': _WARNING_TEXT[code]} for code in codes]


def _target_context(controller, source):
    """Local evidence about a reply target: its thread, author and assessed stance."""
    from . import agent_review, agent_targeting
    post_id, author = source.get('post_id'), source.get('author_id')
    assessment = agent_targeting.classify(source.get('text'))
    context = {'depth': agent_targeting.depths_for(controller.db_path, [post_id]).get(post_id),
               'stance': assessment['stance'], 'concerns': assessment['concerns'], 'thread': [], 'author_recent': []}
    conn = connect(controller.db_path)
    try:
        parent = source.get('reply_to_post_id')
        for _ in range(_CONTEXT_DEPTH):
            if type(parent) is not int:
                break
            row = conn.execute('SELECT post_id,author_id,body,reply_to_post_id FROM posts WHERE post_id=?',
                               (parent,)).fetchone()
            if row is None:
                break
            context['thread'].insert(0, {'post_id': row['post_id'], 'author_id': row['author_id'],
                                         'body': (row['body'] or '')[:500]})
            parent = row['reply_to_post_id']
        if type(author) is int:
            context['author_recent'] = [{'post_id': row['post_id'], 'body': (row['body'] or '')[:300],
                                         'created_at': row['created_at']} for row in conn.execute(
                'SELECT post_id,body,created_at FROM posts WHERE author_id=? AND post_id<>? ORDER BY post_id DESC LIMIT 3',
                (author, post_id))]
    finally:
        conn.close()
    # Display evidence must never copy a secret-looking string into the draft or backups.
    for key in ('thread', 'author_recent'):
        context[key] = [item for item in context[key] if not agent_review.secret_findings(item)]
    return context


def _active_facts(controller, fact_ids):
    if not fact_ids:
        return
    conn = connect(controller.db_path)
    try:
        active = {row['id'] for row in conn.execute(
            "SELECT id FROM facts WHERE status='active' AND id IN (" + ','.join('?' for _ in fact_ids) + ')',
            tuple(fact_ids))}
    finally:
        conn.close()
    missing = [fact_id for fact_id in fact_ids if fact_id not in active]
    if missing:
        raise AgentError('只能引用生效中的事实；以下编号不在生效事实中：' + '、'.join(missing), 400)


def _preflight(controller, request, sources):
    """Validate citations and surface target risks; blocking risks need an explicit acknowledgement."""
    from . import agent_writing
    _active_facts(controller, request.get('fact_ids'))
    extra = {}
    style = agent_writing.warnings(request['body'], request['action']) if request['action'] in _BODY_ACTIONS else []
    if style:
        extra['style_warnings'] = style
    source = next((row for row in sources if row.get('post_id') == request.get('target_post_id')), None)
    if request['action'] in ('reply', 'repost') and source is not None:
        data = controller.store.read()
        warnings = _target_warnings(controller, data, source) if request['action'] == 'reply' else []
        blocking = [row['message'] for row in warnings if row['blocking']]
        if blocking and not request.get('acknowledge_warnings'):
            raise AgentError('目标需要确认后才能准备：' + '；'.join(blocking)
                             + '。确认仍要回复时，请勾选"仍要回复"（acknowledge_warnings）后重新准备', 409)
        if warnings:
            extra['warnings'] = warnings
        try:
            extra['context'] = _target_context(controller, source)
        except Exception:
            extra['context'] = {'unavailable': True}
    return extra


def prepare(controller, payload):
    """Read and persist one human instruction; no model or social write calls."""
    request, request_id, fingerprint = _request(payload)
    from . import agent_review
    data = controller.store.read()
    if agent_review.enabled(data):
        agent_review.assert_no_secrets(request)
    agent = controller._agent(data, request['agent_id'])
    repeated = _existing(data, agent, request_id, fingerprint)
    if repeated is not None:
        return repeated
    _bound(agent)
    _idle(agent)
    if _manual_count(data, agent['id']) >= 10:
        raise AgentError('该角色已有 10 条待处理手动操作，请先执行或丢弃', 409)

    with controller._claim(agent['id']) as token:
        data = controller.store.read()
        agent = dict(controller._agent(data, request['agent_id']))
        repeated = _existing(data, agent, request_id, fingerprint)
        if repeated is not None:
            return repeated
        _bound(agent)
        _idle(agent)
        if _manual_count(data, agent['id']) >= 10:
            raise AgentError('该角色已有 10 条待处理手动操作，请先执行或丢弃', 409)
        if request['action'] in _ACCOUNT_TARGET_ACTIONS and request['target_account_id'] == agent['account_id']:
            raise AgentError('不能关注或取消关注自己', 409)
        client = None
        try:
            client = controller._sdk()
            account = controller._account(client, agent, token)
            if _positive(account, 'account_id') != agent['account_id']:
                raise AgentError('当前会话账号与绑定账号不一致，未创建手动草稿', 409)
            at = utc_now()
            preview = {'actor': {'account_id': agent['account_id'],
                'username': _public_string(account, 'username', 100) or agent.get('username'), 'name': agent['name']}}
            sources = []
            if request['action'] in _POST_TARGET_ACTIONS:
                remote = agent_budget.call(controller, 'read', account.get_post, request['target_post_id'])
                source, target_preview = _post_source(remote, request['target_post_id'], at)
                if request['action'] == 'delete_post' and source['author_id'] != agent['account_id']:
                    raise AgentError('只能删除当前绑定账号本人发布的帖子，未创建手动草稿', 409)
                sources.append(source)
                preview['target_post'] = target_preview
            elif request['action'] in _ACCOUNT_TARGET_ACTIONS:
                remote = agent_budget.call(controller, 'read', account.get_account, request['target_account_id'])
                source, target_preview = _account_source(remote, request['target_account_id'], at)
                sources.append(source)
                preview['target_account'] = target_preview
        except Exception as exc:
            raise controller._remote_failure(exc) from None
        finally:
            controller._close(client)

        if (request['action'] in ('delete_post', 'unlike', 'unfollow')
                and agent_review.secret_findings({'sources': sources, 'preview': preview})):
            # Removing leaked content must remain possible. Preserve verified
            # identity/relationship evidence without copying a secret into the
            # prepared draft, ordinary audit, or portable database backup.
            safe_fields = ('id', 'kind', 'post_id', 'account_id', 'author_id',
                           'root_post_id', 'reply_to_post_id', 'repost_of_post_id',
                           'notification_ids', 'version', 'observed_at')
            sources = [{key: source[key] for key in safe_fields if key in source} for source in sources]
            preview = {kind: {key: value[key] for key in ('post_id', 'account_id', 'author_id') if key in value}
                       for kind, value in preview.items() if isinstance(value, dict)}
        if agent_review.enabled(controller.store.read()):
            agent_review.assert_no_secrets({'request': request, 'sources': sources, 'preview': preview})
        preview.update(_preflight(controller, request, sources))
        acknowledged = [row['code'] for row in preview.get('warnings', [])
                        if row['blocking'] and request.get('acknowledge_warnings')]
        draft_id = str(uuid.uuid4())

        def save(state, conn):
            controller._check_lease(state, agent['id'], token)
            current = controller._agent(state, agent['id'])
            repeated = _existing(state, current, request_id, fingerprint)
            if repeated is not None:
                return repeated
            _bound(current)
            _idle(current)
            if current['account_id'] != agent['account_id']:
                raise AgentError('目标预览期间绑定账号已改变，未创建手动草稿', 409)
            if _manual_count(state, current['id']) >= 10:
                raise AgentError('该角色已有 10 条待处理手动操作，请先执行或丢弃', 409)
            draft = {'id': draft_id, 'agent_id': current['id'], 'account_id': current['account_id'],
                'persona_version': current['version'], 'origin': 'manual', 'manual_only': True,
                'manual_prepared': True, 'user_instruction': 'manual_operator_request',
                'idempotency_key': request_id, 'manual_preview': preview,
                'action': request['action'], 'body': request['body'], 'reason': '用户手动安排',
                'target_post_id': request['target_post_id'], 'target_account_id': request['target_account_id'],
                'reply_to': request['target_post_id'] if request['action'] == 'reply' else None,
                'sources': sources, 'source_ids': [source['id'] for source in sources], 'notification_ids': [],
                'relationship_context': agent_relationships.context_for(state, current, sources),
                'state': 'pending', 'created_at': at, 'post_id': None, 'error': '', 'version': 1,
                'style_warnings': [], 'model': '', 'usage': {}}
            if request.get('fact_ids'):
                draft['fact_ids'] = list(request['fact_ids'])
            if request.get('concern'):
                draft['concern'] = request['concern']
            if acknowledged:
                draft.update(manual_warnings_acknowledged=acknowledged, manual_warnings_acknowledged_at=at)
            from . import agent_interaction_policy
            target_result = next((row for row in sources
                if row.get('post_id') == request.get('target_post_id')), None)
            agent_interaction_policy.check(conn, state, current, draft,
                target_result=target_result, manual=True, require_author=True)
            state['drafts'][draft_id] = draft
            state['rate'].setdefault('manual_requests', {})[request_id] = {
                'agent_id': current['id'], 'account_id': current['account_id'],
                'payload_hash': fingerprint, 'draft_id': draft_id, 'created_at': at}
            controller._event(state, 'manual_prepared', '已准备手动操作，尚未发送', current['id'], draft_id,
                details={'action': request['action'], 'account_id': current['account_id'],
                    'target_post_id': request['target_post_id'], 'target_account_id': request['target_account_id'],
                    'source_count': len(sources), 'manual': True, 'origin': 'manual'})
            return _result(draft, False)
        return controller.store.change(save)


def _inventory_strings(row, fields):
    result, shortened = {}, []
    for name, maximum in fields.items():
        raw = _field(row, name)
        result[name] = raw[:maximum] if isinstance(raw, str) else None
        if isinstance(raw, str) and len(raw) > maximum:
            shortened.append(name)
    if shortened:
        result['truncated_fields'] = shortened
    return result


def _inventory_post(row, actor_id, own):
    post_id = _positive(row, 'post_id')
    if post_id is None:
        raise AgentError('账号列表返回无效帖子 ID，未推断账号现状', 502)
    author_id = _positive(row, 'author_id')
    if own and author_id is not None and author_id != actor_id:
        raise AgentError('本人帖子列表返回了其他作者，未展示错误账号内容', 409)
    result = {'post_id': post_id, 'author_id': author_id,
        **_inventory_strings(row, {'body': 10000, 'kind': 30, 'username': 100, 'author_username': 100,
                                  'created_at': 80, 'edited_at': 80, 'visibility': 30})}
    for name in ('root_post_id', 'reply_to_post_id', 'repost_of_post_id'):
        result[name] = _positive(row, name)
    for name in ('like_count', 'reply_count', 'repost_count', 'view_count'):
        value = _field(row, name)
        result[name] = value if type(value) is int and value >= 0 else None
    return result


def _inventory_account(row):
    account_id = _positive(row, 'account_id')
    if account_id is None:
        raise AgentError('关注列表返回无效账号 ID，未推断账号现状', 502)
    result = {'account_id': account_id,
        **_inventory_strings(row, {'username': 100, 'display_name': 200, 'created_at': 80})}
    for name in ('verified', 'private'):
        value = _field(row, name)
        result[name] = value if type(value) is bool else None
    return result


def inventory(controller, agent_id, kind, limit=20, cursor=None):
    """Read one authenticated account page without adding actor experiences.

    A page is not a complete inventory. Unknown fields and missing pagination
    metadata remain unknown, so an empty response never invents an absence of
    all likes or follows.
    """
    _identifier(agent_id)
    if not isinstance(kind, str) or kind not in ('posts', 'likes', 'following'):
        raise AgentError('账号现状类型须为 posts、likes 或 following')
    _integer(limit, 1, 50, '每页条数')
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 500
            or any(ord(char) < 32 or ord(char) == 127 for char in cursor)):
        raise AgentError('账号列表分页游标格式无效')
    cursor = cursor or None
    data = controller.store.read()
    agent = controller._agent(data, agent_id)
    _bound(agent)
    _idle(agent)
    with controller._claim(agent_id) as token:
        agent = dict(controller._agent(controller.store.read(), agent_id))
        _bound(agent)
        _idle(agent)
        client = None
        try:
            client = controller._sdk()
            account = controller._account(client, agent, token)
            if _positive(account, 'account_id') != agent['account_id']:
                raise AgentError('当前会话账号与绑定账号不一致，未读取账号现状', 409)
            method = getattr(account, {'posts': 'account_posts', 'likes': 'account_likes',
                                       'following': 'following'}[kind])
            page = agent_budget.call(controller, 'read', method, agent['account_id'], limit=limit, cursor=cursor)
            if not isinstance(page, (list, tuple)) or len(page) > limit:
                raise AgentError('账号列表返回格式无效，未推断完整列表', 502)
            has_cursor_metadata = hasattr(page, 'next_cursor')
            next_cursor = getattr(page, 'next_cursor', None)
            if next_cursor is not None and (not isinstance(next_cursor, str) or len(next_cursor) > 500
                    or any(ord(char) < 32 or ord(char) == 127 for char in next_cursor)):
                raise AgentError('账号列表返回无效分页信息，未推断完整列表', 502)
            next_cursor = next_cursor or None
            if next_cursor is not None and next_cursor == cursor:
                raise AgentError('账号列表分页游标未前进，未推断完整列表', 502)
            items = [_inventory_account(row) if kind == 'following' else
                     _inventory_post(row, agent['account_id'], kind == 'posts') for row in page]
            observed_at = utc_now()
        except Exception as exc:
            raise controller._remote_failure(exc) from None
        finally:
            controller._close(client)
        latest = controller.store.read()
        controller._check_lease(latest, agent_id, token)
        current = controller._agent(latest, agent_id)
        if current.get('account_id') != agent['account_id'] or current.get('account_state') != 'ready':
            raise AgentError('读取期间绑定账号已改变，未展示旧账号内容', 409)
        return {'agent_id': agent_id, 'account_id': agent['account_id'], 'kind': kind,
                'scope': 'bound_account', 'coverage': 'one_page', 'items': items,
                'next_cursor': next_cursor, 'has_more': next_cursor is not None if has_cursor_metadata else None,
                'pagination_metadata_present': has_cursor_metadata, 'observed_at': observed_at}


# D26: a member confirms a manual post once; while the shared post allowance is
# exhausted it waits here instead of being refused. Every attempt goes through
# the ordinary publish path, and a retry only follows a local refusal that left
# the draft pending, so the platform receives the post at most once.

def _body_hash(draft):
    return hashlib.sha256(str(draft.get('body', '')).encode()).hexdigest()


def _queue_wait(exc):
    """Seconds until a refused attempt may be offered again, or None when it must stop."""
    if not isinstance(exc, AgentError):
        return None
    transient = (exc.status_code == 429 or getattr(exc, 'wait_kind', None) == 'executor'
                 or exc.status_code == 409 and any(text in str(exc) for text in _QUEUE_TRANSIENT))
    if not transient:
        return None
    wait = getattr(exc, 'retry_after', None)
    # Retrying at retry_after keeps the agent_budget priority hold alive.
    return min(60.0, max(1.0, float(wait))) if isinstance(wait, (int, float)) and wait > 0 else 5.0


def _release(controller, draft_id):
    from . import agent_budget
    try:
        agent_budget.release_priority(controller, 'post', draft_id)
    except Exception:
        pass  # The hold also expires on its own.


def _queue_result(controller, draft_id):
    queue = controller._draft(controller.store.read(), draft_id).get('manual_queue', {})
    return {**controller.snapshot(), 'draft_id': draft_id, 'manual_queue': copy.deepcopy(queue)}


def _settle(controller, draft_id, error, attempted, stop_reason=None):
    """Record one attempt's outcome, or close a queue whose draft can no longer wait."""
    messages = {'sent': '排队的手动操作已发送', 'expired': '排队超时，手动草稿保留为待确认，未发送',
                'cancelled': '手动草稿已放弃，排队结束，未发送', 'stopped': '排队已停止'}

    def settle(data, conn):
        draft = controller._draft(data, draft_id)
        queue = draft.get('manual_queue')
        if not isinstance(queue, dict) or queue.get('state') != 'waiting':
            return dict(queue or {})
        now, state, reason = controller.clock(), draft.get('state'), ''
        wait = _queue_wait(error) if attempted else None
        if state == 'sending' and (not attempted or wait is not None):
            # Another attempt (or a direct publish) holds the role and is sending; it settles itself.
            return dict(queue)
        if attempted:
            queue.update(attempts=int(queue.get('attempts', 0)) + 1, last_attempt_at=utc_now())
        if state == 'published':
            final = 'sent'
            queue['post_id'] = draft.get('post_id')
        elif state == 'discarded':
            final = 'cancelled'
        elif state != 'pending':
            final = 'stopped'
            reason = str(error) if error is not None else (
                '执行结果待核对，已停止排队；请到操作记录核对，系统不会重发' if state in ('sending', 'uncertain')
                else '执行前检查未通过，已停止排队；请重新审阅后手动执行')
        elif queue.get('body_hash') != _body_hash(draft):
            final, reason = 'stopped', '正文与排队时不一致，已停止排队'
        elif stop_reason:
            final, reason = 'stopped', stop_reason
        elif now >= queue.get('expires_at', 0):
            final = 'expired'
        elif not attempted:
            return dict(queue)
        elif wait is not None:
            queue.update(next_attempt_at=now + wait, last_wait=str(error))
            return dict(queue)
        else:
            final, reason = 'stopped', str(error) if error is not None else '执行没有完成，已停止排队'
        queue.update(state=final, finished_at=utc_now(), reason=reason)
        controller._event(data, 'manual_queue_' + final, reason or messages[final], draft['agent_id'], draft_id,
            details={'action': draft.get('action'), 'manual': True, 'origin': 'manual',
                     'attempts': queue.get('attempts', 0), 'post_id': draft.get('post_id'),
                     'target_post_id': draft.get('target_post_id')})
        return dict(queue)
    queue = controller.store.change(settle)
    if queue.get('state') != 'waiting':
        _release(controller, draft_id)
    return queue


def _attempt(controller, draft_id):
    """Offer a waiting manual post to the common executor once; return (queue, error)."""
    current = controller._draft(controller.store.read(), draft_id)
    queue = current.get('manual_queue') or {}
    if (queue.get('state') != 'waiting' or current.get('state') != 'pending'
            or queue.get('body_hash') != _body_hash(current)):
        return _settle(controller, draft_id, None, False), None
    if current.get('action') == 'reply':
        # D29 / item 25: the target may have changed while waiting, e.g. a teammate replied first.
        source = next((row for row in current.get('sources') or []
                       if row.get('post_id') == current.get('target_post_id')), {'post_id': current.get('target_post_id')})
        accepted = set(current.get('manual_warnings_acknowledged') or [])
        new = [code for code in _blocking_codes(controller, controller.store.read(), source, exclude=draft_id)
               if code not in accepted]
        if new:
            reason = '目标情况已改变：' + '；'.join(_WARNING_TEXT[code] for code in new) + '。已停止排队，未发送'
            return _settle(controller, draft_id, None, False, stop_reason=reason), None
    error = None
    try:
        controller.publish(draft_id, None)
    except AgentError as exc:
        error = exc
    except Exception:
        # Unexpected failures never retry; the draft state decides what happened.
        error = AgentError('排队发布遇到未预期错误，已停止排队', 500)
    return _settle(controller, draft_id, error, True), error


def queue_publish(controller, draft_id, body=None, *, requested_by=''):
    """Confirm one prepared manual post that may wait for the shared allowance."""
    _identifier(draft_id)
    from . import agent_actions

    def request(data, conn):
        draft = controller._draft(data, draft_id)
        if not agent_actions._manual(draft):
            raise AgentError('只有成员准备的手动操作可以排队', 409)
        if draft.get('action') not in _QUEUE_ACTIONS:
            raise AgentError('只有发帖、回复和转发可以排队等待发布额度', 409)
        if draft.get('state') != 'pending':
            raise AgentError('只有待确认的手动草稿可以排队；已发送或结果待核对的不能再排队', 409)
        if body is not None and body != draft.get('body', ''):
            raise AgentError('排队只发送已预览的正文；要修改请放弃草稿后重新准备', 409)
        agent = controller._agent(data, draft['agent_id'])
        _bound(agent)
        if draft.get('account_id') != agent['account_id']:
            raise AgentError('手动草稿对应的账号已改变，未排队', 409)
        if (draft.get('manual_queue') or {}).get('state') == 'waiting':
            return
        now = controller.clock()
        draft['manual_queue'] = {'state': 'waiting', 'requested_at': utc_now(),
            'requested_by': str(requested_by or '')[:200], 'expires_at': now + QUEUE_SECONDS,
            'next_attempt_at': now, 'attempts': 0, 'body_hash': _body_hash(draft)}
        controller._event(data, 'manual_queued', '已确认手动操作，等待全队发布额度，有名额时只发送一次',
            draft['agent_id'], draft_id, details={'action': draft['action'], 'manual': True, 'origin': 'manual',
                'target_post_id': draft.get('target_post_id'), 'expires_in_seconds': QUEUE_SECONDS})
    controller.store.change(request)
    queue = controller._draft(controller.store.read(), draft_id).get('manual_queue', {})
    if queue.get('state') == 'waiting' and queue.get('next_attempt_at', 0) <= controller.clock():
        queue, error = _attempt(controller, draft_id)
        if queue.get('state') == 'stopped' and error is not None:
            raise error
    return _queue_result(controller, draft_id)


def cancel_queue(controller, draft_id):
    """Stop waiting; the draft stays pending. An attempt already in flight holds the role lease."""
    _identifier(draft_id)
    draft = controller._draft(controller.store.read(), draft_id)
    with controller._claim(draft['agent_id']) as token:
        def cancel(data, conn):
            controller._check_lease(data, draft['agent_id'], token)
            row = controller._draft(data, draft_id)
            queue = row.get('manual_queue')
            if not isinstance(queue, dict) or queue.get('state') != 'waiting':
                raise AgentError('这条手动草稿没有在排队', 409)
            queue.update(state='cancelled', finished_at=utc_now(), reason='成员取消排队，草稿保留为待确认')
            controller._event(data, 'manual_queue_cancelled', queue['reason'], row['agent_id'], draft_id,
                details={'action': row.get('action'), 'manual': True, 'origin': 'manual',
                         'attempts': queue.get('attempts', 0)})
        controller.store.change(cancel)
    _release(controller, draft_id)
    return _queue_result(controller, draft_id)


def drain_queue(controller, data=None):
    """Scheduler hook: close finished queues and retry at most one due post. Never raises."""
    try:
        data = controller.store.read() if data is None else data
        rows = sorted((draft for draft in data.get('drafts', {}).values()
                       if isinstance(draft.get('manual_queue'), dict) and draft['manual_queue'].get('state') == 'waiting'),
                      key=lambda draft: (draft['manual_queue'].get('next_attempt_at', 0), draft['id']))
        now, attempted = controller.clock(), False
        for draft in rows:
            queue = draft['manual_queue']
            if draft.get('state') != 'pending' or now >= queue.get('expires_at', 0):
                _settle(controller, draft['id'], None, False)
            elif not attempted and queue.get('next_attempt_at', 0) <= now:
                attempted = True
                _attempt(controller, draft['id'])
        return attempted
    except Exception as exc:
        from .agent_control import _log_background_failure
        _log_background_failure('Manual publish queue failed', exc)
        return False
