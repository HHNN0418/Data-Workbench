"""Connect confirmed actor operations to the existing public-data collection.

The caller owns the action's short transaction. This module performs no network
I/O, starts no transaction, commits nothing, and never refreshes AI/analysis
caches. A receipt proves an operation, not its counters or publication time.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json

from .db import raw_record
from .ingest import _atomic, observed_time, save_accounts, save_posts
from .task_queue import enqueue


_POST_FIELDS = ('post_id', 'author_id', 'kind', 'body', 'reply_to_post_id',
                'root_post_id', 'repost_of_post_id', 'visibility', 'embed',
                'created_at', 'edited_at')
_ACCOUNT_FIELDS = ('account_id', 'username', 'display_name', 'verified', 'private', 'created_at')


def _id(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(label + ' must be a positive integer')
    return value


def _remote(value):
    if value is None:
        return {}
    if isinstance(value, Mapping):
        result = {key: value[key] for key in _POST_FIELDS if key in value}
        if 'reply_to_post_id' not in result and 'reply_to' in value:
            result['reply_to_post_id'] = value['reply_to']
        return result
    return {key: getattr(value, key) for key in _POST_FIELDS if hasattr(value, key)}


def _load(conn, key):
    row = conn.execute('SELECT value FROM checkpoints WHERE key=?', (key,)).fetchone()
    return json.loads(row['value']) if row else None


def _checkpoint(conn, key, value, at):
    conn.execute('''INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at''',
        (key, json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False), at))


def _team(conn, account_id, at):
    conn.execute('''INSERT INTO team_accounts(account_id,team_label,is_own,evidence,updated_at)
        VALUES(?,?,1,?,?) ON CONFLICT(account_id) DO UPDATE SET is_own=1,
        evidence=CASE WHEN team_accounts.is_own=1 THEN team_accounts.evidence ELSE excluded.evidence END,
        updated_at=excluded.updated_at''',
        (account_id, 'Agent Zero', 'authenticated managed competition actor operation', at))


def record_account(conn, agent, account, observed_at):
    """Index an authenticated SDK account using public fields only.

    Registration/binding callers already hold their transaction. Neither the
    session, its role/team details, nor arbitrary nested profile fields cross
    this whitelist into public analytics or raw records.
    """
    if not getattr(conn, 'in_transaction', False):
        raise ValueError('account tracking requires the existing binding transaction')
    at = observed_time(observed_at)
    if isinstance(account, Mapping):
        profile = account.get('profile', account.get('account', account))
        account_id = account.get('account_id')
    else:
        profile = getattr(account, 'profile', {})
        account_id = getattr(account, 'account_id', None)
    profile = profile if isinstance(profile, Mapping) else {}
    profile_id = profile.get('account_id')
    if account_id is None:
        account_id = profile_id
    account_id = _id(account_id, 'authenticated account_id')
    if profile_id is not None and (type(profile_id) is not int or profile_id != account_id):
        raise ValueError('profile account differs from the authenticated account')
    if agent.get('account_id') is not None and agent['account_id'] != account_id:
        raise ValueError('profile account differs from the actor binding')
    public = {key: profile[key] for key in _ACCOUNT_FIELDS if key in profile}
    public['account_id'] = account_id
    if not isinstance(account, Mapping):
        for key in ('username', 'display_name'):
            if key not in public:
                value = getattr(account, key, None)
                if value is not None:
                    public[key] = value
    for key in ('username', 'display_name'):
        if public.get(key) is not None and not isinstance(public[key], str):
            raise ValueError('public profile names must be text')
    if public.get('created_at') is not None:
        observed_time(public['created_at'])
    with _atomic(conn):
        save_accounts(conn, [public], at)
        _team(conn, account_id, at)
    return public


def _expected_root(draft, target, confirmed_root=None):
    execution_target = draft.get('execution_target')
    if isinstance(execution_target, dict) and execution_target.get('post_id') == target:
        if 'root_post_id' in execution_target:
            root = execution_target['root_post_id']
            return _id(root, 'execution root_post_id') if root is not None else target
    for source in draft.get('sources', []):
        if isinstance(source, dict) and source.get('post_id') == target:
            root = source.get('root_post_id')
            if root is not None:
                return _id(root, 'source root_post_id')
    # Older sparse source records did not retain thread metadata. The remote
    # post itself can confirm its root; do not contradict it with a guessed one.
    return _id(confirmed_root, 'confirmed root_post_id') if confirmed_root is not None else target


def _post(draft, account_id, post_id, target, remote):
    action = draft['action']
    if remote.get('author_id') is not None:
        _id(remote['author_id'], 'remote author_id')
    if remote.get('author_id') is not None and remote['author_id'] != account_id:
        raise ValueError('confirmed post author differs from the authenticated actor')
    if remote.get('kind') is not None and remote['kind'] != action:
        raise ValueError('confirmed post kind differs from the action')
    for key in ('reply_to_post_id', 'root_post_id', 'repost_of_post_id'):
        if remote.get(key) is not None:
            _id(remote[key], key)
    parent, original = remote.get('reply_to_post_id'), remote.get('repost_of_post_id')
    if action == 'reply':
        if parent is not None and parent != target or original is not None:
            raise ValueError('confirmed reply relationship differs from the action')
        root = _expected_root(draft, target, remote.get('root_post_id'))
        if remote.get('root_post_id') is not None and remote['root_post_id'] != root:
            raise ValueError('confirmed reply root differs from the observed thread')
    elif action == 'repost':
        if original is not None and original != target or parent is not None:
            raise ValueError('confirmed repost relationship differs from the action')
    elif parent is not None or original is not None:
        raise ValueError('confirmed original post has an unexpected relationship')
    if action in ('post', 'reply') and remote.get('body') is not None:
        if not isinstance(remote['body'], str) or remote['body'].strip() != draft.get('body', '').strip():
            raise ValueError('confirmed post body differs from the submitted action')
    post = {key: value for key, value in remote.items() if value is not None}
    # Authenticated acknowledgement establishes authorship and relationship;
    # it does not establish missing content, visibility, time, or engagement.
    post.update(post_id=post_id, author_id=account_id, kind=action)
    if action == 'reply':
        post.update(reply_to_post_id=target, root_post_id=root)
    elif action == 'repost':
        post['repost_of_post_id'] = target
    for key in ('created_at', 'edited_at'):
        if key in post:
            observed_time(post[key])  # validate the offset, retaining story time
    if 'body' in post and not isinstance(post['body'], str):
        raise ValueError('confirmed post body must be text')
    return post


def _counter(conn, post_id, epoch, at, *, own_post=False):
    # Reuse the same exact task key as the ordinary planner. Pending/leased
    # work is never overwritten, and recent samples need no second request.
    cutoff = datetime.fromtimestamp(epoch - 60, timezone.utc).isoformat().replace('+00:00', 'Z')
    recent = conn.execute('''SELECT 1 FROM counter_snapshots WHERE post_id=?
        AND ctn_timestamp(observed_at)>ctn_timestamp(?) LIMIT 1''', (post_id, cutoff)).fetchone()
    if recent:
        return None
    key = 'counter:' + str(post_id)
    enqueue(conn, key, 'counter', {'post_id': post_id, 'sampling_queue': 'new' if own_post else 'tracked',
            'service': 'social', 'path': f'/api/posts/{post_id}/counters',
            'requested_by': 'agent_action'}, priority=30, due_at=epoch)
    return key


def _relationship_success(data, conn, draft, agent, at, epoch):
    from . import agent_relationships
    account_id = _id(agent.get('account_id'), 'actor account_id')
    if draft.get('account_id') != account_id:
        raise ValueError('action account differs from the bound actor')
    if not isinstance(draft.get('id'), str) or not draft['id'] or not isinstance(agent.get('id'), str) or not agent['id']:
        raise ValueError('action and actor IDs are required')
    target = _id(agent_relationships.target_for(draft), 'relationship target')
    action = draft['action']
    if action in ('follow', 'unfollow') and target == account_id:
        raise ValueError('cannot follow the authenticated actor itself')
    identity = {'draft_id': draft['id'], 'agent_id': agent['id'], 'account_id': account_id,
                'action': action, 'post_id': None,
                'target_account_id': target if action in ('follow', 'unfollow') else None,
                'target_post_id': target if action == 'unlike' else None}
    receipt_key = 'agent_tracking:action:' + draft['id']
    previous = _load(conn, receipt_key)
    if previous is not None:
        if any(previous.get(name) != value for name, value in identity.items()):
            raise ValueError('confirmed action identity changed after tracking')
        draft['tracking'] = previous['tracking']
        return dict(previous['tracking'])
    with _atomic(conn):
        raw_record(conn, 'agent:confirmed', '/actions/' + draft['id'],
                   {**identity, 'evidence': 'authenticated relationship operation confirmed'}, at)
        if not conn.execute('SELECT 1 FROM accounts WHERE account_id=?', (account_id,)).fetchone():
            save_accounts(conn, [{'account_id': account_id}], at)
        _team(conn, account_id, at)
        relation = agent_relationships.record_success(data, draft, agent, at)
        relation_key = f'agent_tracking:{relation["kind"]}:{account_id}:{target}'
        _checkpoint(conn, relation_key, {**identity, **relation, 'confirmed_at': at}, at)
        tasks = []
        if action == 'unlike' and conn.execute('SELECT 1 FROM posts WHERE post_id=?', (target,)).fetchone():
            task = _counter(conn, target, epoch, at)
            if task:
                tasks.append(task)
        tracking = {'tracked_at': at, 'post_id': None, 'target_account_id': identity['target_account_id'],
                    'target_post_id': identity['target_post_id'], 'relationship': dict(relation),
                    'content_confirmed': False, 'counter_task_keys': tasks,
                    'analytics_refresh_required': False, 'official_score_inferred': False}
        _checkpoint(conn, receipt_key, {**identity, 'tracking': tracking}, at)
    draft['tracking'] = tracking
    return dict(tracking)


def _delete_success(data, conn, draft, agent, at):
    """Record remote absence while retaining all historical local observations."""
    account_id = _id(agent.get('account_id'), 'actor account_id')
    target = _id(draft.get('target_post_id'), 'deleted post_id')
    execution = draft.get('execution_target', {})
    if (draft.get('origin') != 'manual' or draft.get('manual_only') is not True
            or draft.get('manual_prepared') is not True or draft.get('delete_confirmed') is not True
            or draft.get('account_id') != account_id or execution.get('author_id') != account_id
            or execution.get('post_id') != target):
        raise ValueError('deletion requires a confirmed manual operation and exact authenticated authorship')
    draft_id, agent_id = draft.get('id'), agent.get('id')
    if not isinstance(draft_id, str) or not draft_id or not isinstance(agent_id, str) or not agent_id:
        raise ValueError('action and actor IDs are required')
    evidence = draft.get('delete_verification')
    if evidence not in ('authenticated_delete_acknowledgement', 'authenticated_owner_get_404'):
        raise ValueError('remote deletion requires authenticated acknowledgement or owner 404')
    identity = {'draft_id': draft_id, 'agent_id': agent_id, 'account_id': account_id,
                'action': 'delete_post', 'post_id': None, 'target_post_id': target}
    receipt_key = 'agent_tracking:action:' + draft_id
    previous = _load(conn, receipt_key)
    if previous is not None:
        if any(previous.get(name) != value for name, value in identity.items()):
            raise ValueError('confirmed action identity changed after tracking')
        draft['tracking'] = previous['tracking']
        return dict(previous['tracking'])
    tombstone = {'account_id': account_id, 'post_id': target, 'delete_draft_id': draft_id,
                 'agent_id': agent_id, 'remote_deleted': True, 'confirmed_at': at, 'source': evidence}
    with _atomic(conn):
        raw_record(conn, 'agent:confirmed', '/actions/' + draft_id,
                   {**identity, 'evidence': evidence, 'remote_deleted': True}, at)
        _checkpoint(conn, f'agent_tracking:deleted:{account_id}:{target}', tombstone, at)
        tracking = {'tracked_at': at, 'post_id': None, 'target_post_id': target,
                    'remote_deleted': True, 'deleted_post_id': target,
                    'content_confirmed': False, 'counter_task_keys': [],
                    'analytics_refresh_required': True, 'official_score_inferred': False,
                    'deletion': tombstone}
        _checkpoint(conn, receipt_key, {**identity, 'tracking': tracking}, at)
        # Preserve body, timestamps, counters, published state and source data.
        # The marker is a separate fact, not an erasure of the original event.
        data.setdefault('rate', {}).setdefault('remote_deleted_posts', {})[f'{account_id}:{target}'] = tombstone
        for original in data.get('drafts', {}).values():
            if (original.get('account_id') == account_id and original.get('post_id') == target
                    and original.get('action') in ('post', 'reply', 'repost')):
                original.update(remote_deleted_at=at, remote_delete_draft_id=draft_id)
    draft['tracking'] = tracking
    return dict(tracking)


def record_success(data, conn, draft, agent, remote_result, observed_at):
    """Atomically index a confirmed operation, returning safe tracking metadata.

    ``draft.post_id`` or the result must carry the confirmed resulting ID;
    likes use their target ID. A sparse repost result intentionally leaves the
    public body and created_at unknown. Exceptions must propagate so the caller
    retains the write as uncertain rather than sending it a second time.
    """
    if not getattr(conn, 'in_transaction', False):
        raise ValueError('agent tracking requires the existing action transaction')
    at = observed_time(observed_at)
    epoch = datetime.fromisoformat(at.replace('Z', '+00:00')).timestamp()
    action = draft.get('action')
    if action == 'delete_post':
        return _delete_success(data, conn, draft, agent, at)
    if action in ('unlike', 'follow', 'unfollow'):
        return _relationship_success(data, conn, draft, agent, at, epoch)
    if action not in ('post', 'reply', 'repost', 'like'):
        raise ValueError('only confirmed social operations can be tracked')
    account_id = _id(agent.get('account_id'), 'actor account_id')
    if draft.get('account_id') != account_id:
        raise ValueError('action account differs from the bound actor')
    draft_id, agent_id = draft.get('id'), agent.get('id')
    if not isinstance(draft_id, str) or not draft_id or not isinstance(agent_id, str) or not agent_id:
        raise ValueError('action and actor IDs are required')
    target = draft.get('target_post_id', draft.get('reply_to'))
    target = _id(target, 'target_post_id') if action != 'post' else None
    remote = _remote(remote_result)
    post_id = target if action == 'like' else (
        draft.get('post_id') if draft.get('post_id') is not None else remote.get('post_id'))
    post_id = _id(post_id, 'confirmed post_id')
    if remote.get('post_id') is not None:
        _id(remote['post_id'], 'remote post_id')
    if remote.get('post_id') is not None and remote['post_id'] != post_id:
        raise ValueError('remote and confirmed post IDs differ')
    if action in ('reply', 'repost') and post_id == target:
        raise ValueError('resulting post cannot equal its target')
    identity = {'draft_id': draft_id, 'agent_id': agent_id, 'account_id': account_id,
                'action': action, 'post_id': post_id, 'target_post_id': target}
    receipt_key = 'agent_tracking:action:' + draft_id
    previous = _load(conn, receipt_key)
    if previous is not None:
        if any(previous.get(key) != value for key, value in identity.items()):
            raise ValueError('confirmed action identity changed after tracking')
        draft['tracking'] = previous['tracking']
        return dict(previous['tracking'])
    post = None if action == 'like' else _post(draft, account_id, post_id, target, remote)
    if post is not None:
        old = conn.execute('SELECT author_id,kind FROM posts WHERE post_id=?', (post_id,)).fetchone()
        if old and (old['author_id'] is not None and old['author_id'] != account_id
                    or old['kind'] is not None and old['kind'] != action):
            raise ValueError('existing public post conflicts with the confirmed actor action')
    with _atomic(conn):
        raw_record(conn, 'agent:confirmed', '/actions/' + draft_id,
                   {**identity, 'remote_post': remote, 'evidence': 'authenticated operation confirmed'}, at)
        if post is not None:
            save_posts(conn, [post], 'agent:' + agent_id, at)
        # Only the authenticated ID is known here. Never use a fictional role's
        # display name as a remotely observed account profile.
        if not conn.execute('SELECT 1 FROM accounts WHERE account_id=?', (account_id,)).fetchone():
            save_accounts(conn, [{'account_id': account_id}], at)
        _team(conn, account_id, at)
        if action == 'like':
            from . import agent_relationships
            current_relation = agent_relationships.record_success(data, draft, agent, at)
            relation_key = f'agent_tracking:like:{account_id}:{target}'
            _checkpoint(conn, relation_key, {**identity, **current_relation, 'confirmed_at': at,
                'evidence': 'authenticated like operation or exact my_likes relationship'}, at)
        tasks = []
        for candidate in dict.fromkeys([post_id, target]):
            if candidate is None:
                continue
            # No fabricated target row: observations/relations already supply
            # targets, and the usual enrichment lane fills missing references.
            if conn.execute('SELECT 1 FROM posts WHERE post_id=?', (candidate,)).fetchone():
                task = _counter(conn, candidate, epoch, at, own_post=action != 'like' and candidate == post_id)
                if task:
                    tasks.append(task)
        content = conn.execute('SELECT body FROM posts WHERE post_id=?', (post_id,)).fetchone()
        tracking = {'tracked_at': at, 'post_id': post_id, 'target_post_id': target,
                    'content_confirmed': bool(content and content['body'] is not None),
                    'counter_task_keys': tasks, 'analytics_refresh_required': True,
                    'profile_refresh': 'shared auxiliary collector', 'official_score_inferred': False}
        _checkpoint(conn, receipt_key, {**identity, 'tracking': tracking}, at)
    draft['tracking'] = tracking
    return dict(tracking)
