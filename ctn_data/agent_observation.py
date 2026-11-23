"""Recent, bounded observations for fictional competition actors.

Public collections are reused from SQL. Only an actor's own notifications and
home timeline are polled here. Refreshing an inbox never marks it resolved, and
no database transaction is held while an SDK method runs.
"""
from __future__ import annotations

from datetime import datetime, timezone
import copy
import hashlib
import json
import threading
import uuid
from urllib.parse import quote

from .db import connect, timestamp_key
from .ingest import save_posts
from .social_topics import NEWS_USERNAMES

MAX_SOURCES = 20
MAX_CONTEXT = 12000
MAX_INBOX = 200
MAX_CANDIDATES = 250
CHANNEL_LEASE_SECONDS = 90


def _at(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _epoch(value):
    value = timestamp_key(value)
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() if value else 0


def _positive(value):
    return value if type(value) is int and value > 0 else None


def _text(value, limit=1000):
    return value[:limit] if isinstance(value, str) else ''


def _version(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _field(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _call(controller, bucket, method, *args, **kwargs):
    from .agent_budget import call
    return call(controller, bucket, method, *args, **kwargs)


def _is_deferred(exc):
    from .agent_budget import Deferred
    return isinstance(exc, Deferred)


def _delay(exc, minimum=5):
    try:
        return max(minimum, min(float(getattr(exc, 'retry_after', 0) or minimum), 3600))
    except (ValueError, TypeError):
        return minimum


def _check(controller, agent, token):
    # Leases are runtime rows: no need to decode the whole store (19 MB) each time.
    read = getattr(controller.store, 'read_runtime', None) or controller.store.read
    controller._check_lease(read(), agent['id'], token)


def _change_observation(controller, agent, fields, callback):
    """Channel bookkeeping changes only leases and this role's listed memory fields."""
    scoped = getattr(controller.store, 'change_role_memory', None)
    if scoped is None:
        return controller.store.change(callback)
    return scoped(agent['id'], fields, callback)


def _take_channel(controller, agent, token, channel):
    """Persistent refresh TTLs are separate from the model decision schedule."""
    now = controller.clock()
    interval = agent.get('notification_interval_seconds', 60)
    key = 'observation:' + agent['id'] + ':' + channel
    claim = str(uuid.uuid4())

    def take(data, conn):
        controller._check_lease(data, agent['id'], token)
        memory = data['memory'].setdefault(agent['id'], {})
        observation = memory.setdefault('observation', {})
        lease = data['leases'].get(key, {})
        if observation.get(channel + '_next_at', 0) > now or lease.get('until', 0) > now:
            return None
        data['leases'][key] = {'token': claim, 'until': now + CHANNEL_LEASE_SECONDS}
        observation[channel + '_next_at'] = now + interval
        observation['next_refresh_at'] = min(observation.get(c + '_next_at', now) for c in ('notifications', 'home'))
        return claim

    return _change_observation(controller, agent, ('observation',), take)


def _finish_channel(controller, agent, token, channel, claim, *, status, items=None, error='', retry_after=None):
    now, at = controller.clock(), _at(controller.clock())

    def finish(data, conn):
        controller._check_lease(data, agent['id'], token)
        key = 'observation:' + agent['id'] + ':' + channel
        if data['leases'].get(key, {}).get('token') != claim:
            return
        data['leases'].pop(key, None)
        memory = data['memory'].setdefault(agent['id'], {})
        observation = memory.setdefault('observation', {})
        observation[channel + '_status'] = status
        observation['last_refresh_at'] = at
        observation[channel + '_error'] = error
        observation['error'] = next((observation.get(c + '_error', '') for c in ('notifications', 'home')
                                     if observation.get(c + '_error')), '')
        if retry_after is not None:
            observation[channel + '_next_at'] = now + retry_after
        if status == 'ok':
            observation[channel + '_refreshed_at'] = at
            if channel == 'notifications':
                existing = {row['id']: row for row in memory.get('inbox', []) if _positive(row.get('id'))}
                for item in items or []:
                    old = existing.get(item['id'], {})
                    # A refresh cannot undo an accepted decision or acknowledgement.
                    # observed_at is the latest fetch; first_seen_at keeps the first
                    # one, so detection latency stays measurable (S9).
                    existing[item['id']] = {**old, **{key: value for key, value in item.items() if value is not None},
                                            'status': old.get('status', 'unresolved'),
                                            'first_seen_at': old.get('first_seen_at') or old.get('observed_at')
                                                             or item.get('observed_at')}
                protected = {value['notification_id'] for value in memory.get('notification_outbox', {}).values()
                             if value.get('state') == 'pending'}
                protected.update(value for draft in data['drafts'].values() if draft['agent_id'] == agent['id']
                                 and not draft.get('notifications_resolved')
                                 and draft['state'] in ('pending', 'sending', 'uncertain', 'published', 'skipped')
                                 for value in draft.get('notification_ids', []))
                ordered = sorted(existing.values(), key=lambda row: (row.get('status') == 'resolved', -row['id']))
                active = [row for row in ordered if row['id'] in protected]
                # Old history is bounded, but never discard a notification
                # needed by a pending action or its acknowledgement outbox.
                memory['inbox'] = active + [row for row in ordered if row['id'] not in protected][:max(0, MAX_INBOX - len(active))]
                from .agent_notifications import apply_interaction_policy
                apply_interaction_policy(data, agent['id'], conn)
            elif channel == 'home':
                memory['home_sources'] = items or []
        observation['next_refresh_at'] = min(observation.get(c + '_next_at', now) for c in ('notifications', 'home'))

    if status == 'ok' and channel == 'notifications':
        # Merging the inbox reads drafts, the outbox and the interaction policy.
        controller.store.change(finish)
    else:
        _change_observation(controller, agent, ('observation', 'home_sources'), finish)


def _post(raw, *, origin, at, notification_ids=None):
    if not isinstance(raw, dict) or not _positive(raw.get('post_id')):
        return None
    post_id = raw['post_id']
    original_body = raw.get('body') if isinstance(raw.get('body'), str) else ''
    body = original_body[:2000]
    username = _text(raw.get('username'), 100).lstrip('@')
    news_username = next((value for value in NEWS_USERNAMES if value.casefold() == username.casefold()), None)
    notifications = sorted(set(notification_ids or []))
    source = {
        'id': 'post:' + str(post_id), 'kind': 'post', 'post_id': post_id,
        'post_kind': _text(raw.get('kind'), 30) or 'post',
        'author_id': _positive(raw.get('author_id')),
        'username': username or None,
        'title': 'Post ' + str(post_id), 'text': body,
        'text_length': len(original_body), 'text_truncated': len(original_body) > len(body),
        'coverage': 'excerpt' if len(original_body) > len(body) else 'full',
        'url': 'https://social.legitreal.com/posts/' + str(post_id),
        'created_at': _text(raw.get('created_at'), 80),
        'edited_at': _text(raw.get('edited_at'), 80),
        'first_seen': _text(raw.get('first_seen'), 80) or at,
        'observed_at': _text(raw.get('last_seen'), 80) or at,
        'origin': origin, 'source_role': 'news_account' if news_username else origin,
        'news_account': news_username, 'notification_ids': notifications,
        'root_post_id': _positive(raw.get('root_post_id')),
        'reply_to_post_id': _positive(raw.get('reply_to_post_id')),
        'repost_of_post_id': _positive(raw.get('repost_of_post_id')),
    }
    source['version'] = _version(raw.get('body'), raw.get('edited_at'), raw.get('kind'),
                                 source['reply_to_post_id'], source['root_post_id'])
    return source


def _save_public(controller, posts, origin, at):
    public = []
    for raw in posts:
        if isinstance(raw, dict) and _positive(raw.get('post_id')) and raw.get('visibility', 'public') in ('public', '', None):
            public.append({key: value for key, value in raw.items() if key in {
                'post_id', 'author_id', 'kind', 'body', 'reply_to_post_id', 'root_post_id',
                'repost_of_post_id', 'visibility', 'created_at', 'edited_at'}})
    if not public:
        return
    conn = connect(controller.db_path)
    try:
        with conn:
            save_posts(conn, public, origin, observed_at=at)
    finally:
        conn.close()


def _notifications(raw_items, at):
    inbox, posts = [], []
    for raw in list(raw_items)[:50]:
        if not isinstance(raw, dict):
            continue
        notification_id = _positive(raw.get('notification_id')) or _positive(raw.get('id'))
        if not notification_id:
            continue
        post = raw.get('post') or raw.get('target_post')
        post = post if isinstance(post, dict) else {}
        post_id = _positive(raw.get('post_id')) or _positive(post.get('post_id'))
        actor = raw.get('actor') if isinstance(raw.get('actor'), dict) else {}
        actor_id = (_positive(raw.get('actor_account_id')) or _positive(raw.get('actor_id'))
                    or _positive(actor.get('account_id')) or _positive(actor.get('id')))
        item = {
            'id': notification_id, 'notification_id': notification_id, 'post_id': post_id,
            'kind': _text(raw.get('kind') or raw.get('type') or raw.get('notification_type'), 40) or 'notification',
            'status': 'unresolved', 'created_at': _text(raw.get('created_at'), 80),
            'observed_at': at, 'source_id': 'post:' + str(post_id) if post_id else None,
            'actor_account_id': actor_id,
            'actor_username': _text(raw.get('actor_username') or actor.get('username'), 100).lstrip('@') or None,
        }
        if post and post_id:
            source = _post({**post, 'post_id': post_id}, origin='notification', at=at, notification_ids=[notification_id])
            item['source'] = source
            posts.append({**post, 'post_id': post_id})
        from .agent_notifications import _actor_account_id
        item['actor_account_id'] = _actor_account_id(item)
        inbox.append(item)
    return inbox, posts


def refresh(controller, agent, token):
    """Poll due authenticated channels without consuming any read versions."""
    claims = {name: _take_channel(controller, agent, token, name) for name in ('notifications', 'home')}
    claims = {name: claim for name, claim in claims.items() if claim}
    if not claims:
        return
    if agent.get('account_state') != 'ready' or not agent.get('account_id'):
        for channel, claim in claims.items():
            _finish_channel(controller, agent, token, channel, claim, status='unbound')
        return
    client = None
    try:
        client = controller._sdk()
        _check(controller, agent, token)
        account = controller._account(client, agent, token)
    except Exception as exc:
        for channel, claim in claims.items():
            _finish_channel(controller, agent, token, channel, claim,
                            status='deferred' if _is_deferred(exc) else 'error',
                            error='认证资料暂未刷新，继续使用已有资料', retry_after=_delay(exc))
        if client is not None:
            controller._close(client)
        return
    try:
        for channel, claim in claims.items():
            method = getattr(account, 'notifications' if channel == 'notifications' else 'feed_home', None)
            if not callable(method):
                _finish_channel(controller, agent, token, channel, claim, status='unavailable')
                continue
            try:
                _check(controller, agent, token)
                if channel == 'notifications':
                    page = _call(controller, 'notification', method, unresolved=True, limit=50, cursor=None)
                    at = _at(controller.clock())
                    inbox, posts = _notifications(page, at)
                    _save_public(controller, posts, 'agent_notification', at)
                    _finish_channel(controller, agent, token, channel, claim, status='ok', items=inbox)
                else:
                    page = _call(controller, 'feed', method, limit=30, cursor=None)
                    at = _at(controller.clock())
                    posts = [row for row in list(page)[:30] if isinstance(row, dict)]
                    _save_public(controller, posts, 'agent_home', at)
                    sources = [_post(row, origin='home', at=at) for row in posts]
                    _finish_channel(controller, agent, token, channel, claim, status='ok', items=[row for row in sources if row])
            except Exception as exc:
                _finish_channel(controller, agent, token, channel, claim,
                                status='deferred' if _is_deferred(exc) else 'error',
                                error='资料刷新暂未完成，继续使用已有资料', retry_after=_delay(exc))
    finally:
        controller._close(client)


def _tick_rows(controller):
    """The scheduler tick's shared local-window rows (agent_control.tick), on its own thread only.

    Every idle role's pending_revision ran the same three queries: 688 in one tick on
    24 Oct, two thirds of the tick. Within one tick they run once, with the tick's now;
    other threads and calls query as before.
    """
    shared = getattr(controller, '_tick_local', None)
    return shared if isinstance(shared, dict) and shared.get('thread') == threading.get_ident() else None


def _local(controller, agent, memory):
    shared = _tick_rows(controller)
    now = shared['now'] if shared is not None else controller.clock()
    post_cutoff = _at(now - agent.get('freshness_minutes', 60) * 60)
    news_cutoff = now - agent.get('news_freshness_hours', 24) * 3600
    news_post_cutoff = _at(now - max(24, agent.get('news_freshness_hours', 24)) * 3600)
    key = (post_cutoff, news_post_cutoff, MAX_CANDIDATES)
    rows = shared['rows'].get(key) if shared is not None else None
    conn = connect(controller.db_path) if rows is None else None
    try:
        if rows is None:
            # The freshness expression matches idx_posts_freshness_utc exactly, so
            # the newest candidates come from the index instead of a full scan.
            posts = conn.execute('''SELECT p.*,a.username FROM posts p
                LEFT JOIN accounts a ON a.account_id=p.author_id
                WHERE (p.visibility IS NULL OR LOWER(p.visibility) IN ('','public'))
                AND (a.private IS NULL OR a.private=0)
                AND COALESCE(ctn_timestamp(p.edited_at),ctn_timestamp(p.created_at),ctn_timestamp(p.first_seen))>=?
                ORDER BY COALESCE(ctn_timestamp(p.edited_at),ctn_timestamp(p.created_at),ctn_timestamp(p.first_seen)) DESC,p.post_id DESC
                LIMIT ?''', (post_cutoff, MAX_CANDIDATES)).fetchall()
            news_posts = conn.execute('''SELECT p.*,a.username FROM posts p
                JOIN accounts a ON a.account_id=p.author_id
                WHERE (p.visibility IS NULL OR LOWER(p.visibility) IN ('','public'))
                AND (a.private IS NULL OR a.private=0)
                AND LOWER(a.username) IN (?,?)
                AND COALESCE(ctn_timestamp(p.edited_at),ctn_timestamp(p.created_at),ctn_timestamp(p.first_seen))>=?
                ORDER BY COALESCE(ctn_timestamp(p.edited_at),ctn_timestamp(p.created_at),ctn_timestamp(p.first_seen)) DESC,p.post_id DESC
                LIMIT ?''', (*[name.casefold() for name in NEWS_USERNAMES], news_post_cutoff, MAX_CANDIDATES)).fetchall()
            news = conn.execute('''SELECT n.*,r.observed_at AS payload_observed_at FROM news_articles n
                LEFT JOIN raw_records r ON r.id=n.raw_id
                ORDER BY COALESCE(ctn_timestamp(r.observed_at),ctn_timestamp(n.first_seen)) DESC,
                n.article_key LIMIT ?''', (MAX_CANDIDATES,)).fetchall()
            rows = (posts, news_posts, news)
            if shared is not None:
                shared['rows'][key] = rows
        posts, news_posts, news = rows
        # The post sources depend only on these rows and the tick's now, so one tick builds them once;
        # every role gets its own copies (news below stays per role: it reads the role's news_versions).
        built = shared.setdefault('sources', {}).get(key) if shared is not None else None
        if built is None:
            built = []
            seen_posts = set()
            for record in list(news_posts) + list(posts):
                row = dict(record)
                if row['post_id'] in seen_posts:
                    continue
                seen_posts.add(row['post_id'])
                is_news = any(row.get('username', '').casefold() == name.casefold() for name in NEWS_USERNAMES) if row.get('username') else False
                source = _post(row, origin='news_account' if is_news else 'recent', at=_at(now))
                source['title'] = ('@' + row['username']) if row.get('username') else source['title']
                source['freshness_at'] = row.get('edited_at') or row.get('created_at') or row['first_seen']
                source['freshness_basis'] = 'edited_at' if row.get('edited_at') else 'created_at' if row.get('created_at') else 'first_seen'
                built.append(source)
            if shared is not None:
                shared['sources'][key] = built
        sources = [dict(source) for source in built] if shared is not None else built
        known = dict(memory.get('news_versions', {}))
        for record in news:
            row = dict(record)
            source_id = 'news:' + row['article_key']
            version = _version(row.get('title'), row.get('body'))
            old = known.get(source_id, {})
            if old.get('version') == version:
                content_at = old.get('observed_at') or row['first_seen']
            elif old:
                # A reverted body can reuse an older raw payload hash. The
                # actual observation of this change is the collector's current
                # timestamp, rather than that payload's first-ever occurrence.
                content_at = row['last_seen']
            else:
                # The raw payload hash also covers publication metadata. An
                # actor seeing this article for the first time cannot infer a
                # body edit from a newer raw record, so use first observation.
                content_at = row['first_seen']
            known[source_id] = {'version': version, 'observed_at': content_at}
            if _epoch(content_at) < news_cutoff:
                continue
            body = row.get('body') or ''
            site, slug = _text(row.get('site'), 80), _text(row.get('slug'), 200)
            url = ('https://sites.legitreal.com/' + quote(site, safe='') + '/articles/' + quote(slug, safe='') + '.json') if site and slug else None
            news_account = {'daily-kingstonia': 'DailyKingstonia', 'kingstonia-herald': 'KingstoniaHerald'}.get(site)
            sources.append({
                'id': source_id, 'kind': 'news', 'title': _text(row.get('title'), 200),
                'text': body[:2000], 'text_length': len(body), 'text_truncated': len(body) > 2000,
                'coverage': 'excerpt' if len(body) > 2000 else 'full' if body.strip() else 'title_only',
                'version': version, 'origin': 'news', 'source_role': 'game_news',
                'site': site, 'slug': slug, 'url': url, 'news_account': news_account,
                'first_seen': row['first_seen'], 'observed_at': row['last_seen'],
                'published_at': _text(row.get('published_at'), 80),
                'freshness_at': content_at, 'freshness_basis': 'content_observation',
                'notification_ids': [],
            })
        return sources, known
    finally:
        if conn is not None:
            conn.close()


def _local_posts(controller, post_ids, *, origin='notification', notification_ids=None):
    result = []
    ids = list(dict.fromkeys(post_id for post_id in post_ids if _positive(post_id)))[:MAX_INBOX]
    if not ids:
        return result
    conn = connect(controller.db_path)
    try:
        rows = conn.execute('''SELECT p.*,a.username FROM posts p LEFT JOIN accounts a ON a.account_id=p.author_id
            WHERE (a.private IS NULL OR a.private=0) AND p.post_id IN (''' + ','.join('?' for _ in ids) + ')', ids).fetchall()
        for row in rows:
            raw = dict(row)
            if raw.get('visibility') not in ('public', None, ''):
                continue
            result.append(_post(raw, origin=origin, at=_at(controller.clock()), notification_ids=(notification_ids or {}).get(raw['post_id'], [])))
    finally:
        conn.close()
    return result


def _enrich_notifications(controller, agent, token, inbox):
    """Missing notification targets are fetched at most once per observation."""
    from .agent_notifications import notice_is_policy_ignored
    policy_data, policy_memory = _policy_memory(controller, agent, {'inbox': inbox})
    inbox = policy_memory.get('inbox', [])
    mapping = {}
    for item in inbox:
        if item.get('status') == 'unresolved' and not item.get('policy_ignored') and _positive(item.get('post_id')):
            mapping.setdefault(item['post_id'], []).append(item['id'])
    sources = _local_posts(controller, mapping, notification_ids=mapping)
    found = {source['post_id'] for source in sources}
    for item in inbox:
        if (item.get('status') == 'unresolved' and not item.get('policy_ignored')
                and item.get('source') and item.get('post_id') not in found):
            source = dict(item['source'])
            source['notification_ids'] = mapping.get(item['post_id'], [])
            sources.append(source)
            found.add(item['post_id'])
    missing = [post_id for post_id in mapping if post_id not in found]
    if not missing or agent.get('account_state') != 'ready':
        return sources
    client = None
    try:
        client = controller._sdk()
        _check(controller, agent, token)
        account = controller._account(client, agent, token)
        method = getattr(account, 'get_post', None) or getattr(client, 'get_post', None)
        if callable(method):
            # The read bucket may defer the next call immediately. No sleeps.
            for post_id in missing[:3]:
                _check(controller, agent, token)
                raw = _call(controller, 'read', method, post_id)
                source = _post(raw, origin='notification', at=_at(controller.clock()), notification_ids=mapping[post_id])
                if source:
                    _save_public(controller, [raw], 'agent_notification', _at(controller.clock()))
                    # The full post can reveal the actor of a historical notice
                    # whose trimmed payload omitted both actor and author IDs.
                    allowed = [item['id'] for item in inbox if item.get('post_id') == post_id
                               and item['id'] in mapping[post_id]
                               and not notice_is_policy_ignored(policy_data, agent['id'], {**item, 'source': source})]
                    if allowed:
                        source['notification_ids'] = allowed
                        sources.append(source)
    except Exception:
        pass  # Keep inbox entries unresolved and retry through a later observation.
    finally:
        if client is not None:
            controller._close(client)
    return sources


def _reading_order(candidates):
    """Bounded manual reading order without language-dependent interest gates."""
    ordered = sorted(candidates, key=lambda source: _epoch(
        source.get('freshness_at') or source.get('edited_at')
        or source.get('created_at') or source.get('first_seen')), reverse=True)
    lanes = [[], [], []]
    for source in ordered:
        lane = (0 if source.get('notification_ids') else
                1 if source.get('kind') == 'news' or source.get('news_account') else 2)
        lanes[lane].append(source)
    return [lane[index] for index in range(max(map(len, lanes), default=0))
            for lane in lanes if index < len(lane)]


_refresh_channels = refresh


def _policy_memory(controller, agent, memory=None, data=None):
    """Apply current settings to a read snapshot, including pre-policy inboxes.

    ``data`` is a snapshot the caller already holds (the scheduler tick asks
    every idle role on every tick; a fresh read decodes the whole store each
    time). The policy writes only this role's memory, which is copied here.
    """
    from .agent_notifications import apply_interaction_policy
    if data is None:
        data = controller.store.read()
    else:
        data = {**data, 'memory': dict(data.get('memory', {}))}
        if memory is None:
            memory = data['memory'].get(agent['id'])
    if memory is not None:
        from .agent_memory_rows import LazyRole
        # A lazy role copies only the fields it already holds (D89).
        data['memory'][agent['id']] = memory.detached() if isinstance(memory, LazyRole) else copy.deepcopy(memory)
    conn = connect(controller.db_path)
    try:
        apply_interaction_policy(data, agent['id'], conn)
    finally:
        conn.close()
    return data, data.get('memory', {}).get(agent['id'], {})


def _new_sources(controller, agent, memory, local, notification_sources, for_decision, operational=None):
    """Select changed input without recording reads or consuming decisions."""
    post_cutoff = controller.clock() - agent.get('freshness_minutes', 60) * 60
    home = []
    for source in memory.get('home_sources', []):
        if _epoch(source.get('edited_at') or source.get('created_at') or source.get('first_seen')) >= post_cutoff:
            home.append(source)
    combined = {}
    for source in notification_sources + home + local:
        if source['id'] not in combined:
            combined[source['id']] = dict(source)
        elif source.get('news_account'):
            # The local account join can identify a news author omitted from a
            # home/notification payload without losing notification priority.
            combined[source['id']].update(source_role='news_account', news_account=source['news_account'],
                                           username=source.get('username'))
    versions = memory.get('decision_versions' if for_decision else 'read_versions', {})
    read_notifications = set(memory.get('decision_notification_ids' if for_decision else 'read_notification_ids', []))
    reserved_versions = set()
    if for_decision:
        from . import agent_intents
        if operational is None:
            operational = controller.store.read()
        _, reserved_notices = agent_intents.claims(operational, agent['id'])
        # A pending reply does not ban liking the same source. Only abandoned
        # input still awaiting explicit reconsideration is withheld wholesale.
        reserved_versions, _ = agent_intents.claims(operational, agent['id'], abandoned_only=True)
        read_notifications.update(reserved_notices)
    candidates = [source for source in combined.values()
                  if (versions.get(source['id']) != source['version']
                      and (source['id'], source['version']) not in reserved_versions)
                  or any(value not in read_notifications for value in source.get('notification_ids', []))]
    # A post can carry several notifications. Offer only the notifications
    # still awaiting this read/decision, so a new mention cannot reselect an
    # already handled notification from the same post.
    for source in candidates:
        source['notification_ids'] = [value for value in source.get('notification_ids', [])
                                      if value not in read_notifications]
    return combined, candidates


def pending_revision(controller, agent, memory, data=None):
    """Local-only wakeup identity; unchanged input cannot wake an idle actor twice."""
    operational, memory = _policy_memory(controller, agent, memory, data)
    local, _ = _local(controller, agent, memory)
    read_notifications = set(memory.get('decision_notification_ids', []))
    from . import agent_intents
    _, reserved_notices = agent_intents.claims(operational, agent['id'])
    read_notifications.update(reserved_notices)
    notices = [item for item in memory.get('inbox', [])
               if item.get('status') == 'unresolved' and not item.get('policy_ignored') and _positive(item.get('post_id'))
               and item.get('id') not in read_notifications]
    mapping = {}
    for item in notices:
        mapping.setdefault(item['post_id'], []).append(item['id'])
    notification_sources = _local_posts(controller, mapping, notification_ids=mapping)
    found = {source['post_id'] for source in notification_sources}
    for item in notices:
        if item.get('source') and item['post_id'] not in found:
            notification_sources.append({**item['source'], 'notification_ids': mapping[item['post_id']]})
            found.add(item['post_id'])
    _, candidates = _new_sources(controller, agent, memory, local, notification_sources, True,
                                 operational if data is not None else None)
    inputs = sorted((source['id'], source['version'], sorted(source.get('notification_ids', [])))
                    for source in candidates)
    # A newly observed notice may need its target fetched during the decision.
    # Include it even when its post is not available in the local cache yet.
    missing = sorted(item['id'] for item in notices if item['post_id'] not in found)
    return _version(inputs, missing) if inputs or missing else ''


def _reply_queue(controller, agent, data):
    """Rules-mode reply opportunities older than the local window can still reach (D48)."""
    from . import agent_opportunities
    if agent_opportunities.mode(agent) != 'rules':
        return []
    try:
        return agent_opportunities.queue(controller.db_path, data, agent, controller.clock(),
                                         limit=agent_opportunities.QUEUE_SELECTED * 3)
    except Exception:
        # The queue is an addition; without it the role still reads the local window.
        import logging
        logging.getLogger(__name__).warning('Reply-opportunity queue unavailable for %s', agent.get('id'),
                                            exc_info=True)
        return []


def observe(controller, agent, token, refresh=True, for_decision=False):
    """Select reading material, then budget targets together with their context."""
    from . import reading_selection, agent_thread_context
    if refresh:
        # Public feeds/news are deliberately not collected once per actor.
        _refresh_channels(controller, agent, token)
    operational, memory = _policy_memory(controller, agent)
    local, known_news = _local(controller, agent, memory)
    notification_sources = _enrich_notifications(controller, agent, token, memory.get('inbox', []))
    queue = _reply_queue(controller, agent, operational) if for_decision else []
    combined, candidates = _new_sources(controller, agent, memory,
                                        local + [entry['source'] for entry in queue],
                                        notification_sources, for_decision)
    if queue:
        from . import agent_opportunities
        agent_opportunities.mark(candidates, queue)
    selection = None
    if for_decision:
        targets, selection = reading_selection.select(controller, agent, token, candidates)
    else:
        # The explicit read-only command keeps its no-model behavior.
        targets = _reading_order(candidates)
    selected, thread_context = agent_thread_context.pack(
        targets, lambda ids: _local_posts(controller, ids, origin='thread'),
        max_sources=MAX_SOURCES, max_context=MAX_CONTEXT)
    at = _at(controller.clock())

    def remember(data, conn):
        controller._check_lease(data, agent['id'], token)
        controller.generations.check_cancelled(agent['id'], data)
        if selection is not None:
            reading_selection.check_result(controller, selection, data, conn)
        old = data['memory'].setdefault(agent['id'], {})
        read_versions = old.setdefault('read_versions', {})
        for source in selected:
            read_versions.pop(source['id'], None)
            read_versions[source['id']] = source['version']
        old['read_versions'] = dict(list(read_versions.items())[-600:])
        old['read_ids'] = list(dict.fromkeys(old.get('read_ids', []) + [source['id'] for source in selected]))[-600:]
        old['read_notification_ids'] = list(dict.fromkeys(old.get('read_notification_ids', []) + [value
            for source in selected for value in source.get('notification_ids', [])]))[-600:]
        old['news_versions'] = dict(list(known_news.items())[-600:])
        from . import agent_memory
        agent_memory.record_read(data, agent['id'], selected, at)
        old.update(last_sources=selected, last_read_at=at)
        observation = old.setdefault('observation', {})
        from .agent_notifications import apply_interaction_policy
        apply_interaction_policy(data, agent['id'], conn)
        observation.update(observed_at=at, source_count=len(selected),
                           new_source_count=sum(not row.get('context_only') for row in selected),
                           thread_context=thread_context,
                           unresolved_count=sum(row.get('status') != 'resolved' and not row.get('policy_ignored')
                                                for row in old.get('inbox', [])),
                           latest_post_at=max((row.get('freshness_at') or row.get('created_at') or '' for row in combined.values() if row['kind'] == 'post'), key=_epoch, default=''),
                           latest_news_at=max((row.get('freshness_at') or '' for row in local if row['kind'] == 'news'), key=_epoch, default=''))
        if selection is not None:
            observation['reading_selection'] = selection
        else:
            observation.pop('reading_selection', None)
        controller._agent(data, agent['id']).update(last_read_at=at, last_read_count=len(selected))
        message = ''
        if selection is not None:
            count = selection.get('candidate_count', 0)
            if selection.get('cache_hit'):
                message = f'复用此前 {count} 条候选的选读结果；'
            elif selection.get('status') == 'fallback':
                message = f'AI 选读未完成，从 {count} 条候选中采用备用选择；'
            elif count:
                message = f"AI 预览 {count} 条候选，选中 {selection.get('selected_count', 0)} 条；"
        controller._event(data, 'read', message + f'已阅读 {len(selected)} 条资料（含讨论上下文）', agent['id'])

    controller.store.change(remember)
    return selected


def resolve_selected(controller, agent, token, notification_ids, *, draft_id=None):
    """Persist acknowledgements separately from the already completed action."""
    ids = list(dict.fromkeys(value for value in notification_ids if _positive(value)))[:MAX_INBOX]
    now, at = controller.clock(), _at(controller.clock())

    def queue(data, conn):
        controller._check_lease(data, agent['id'], token)
        memory = data['memory'].setdefault(agent['id'], {})
        from .agent_notifications import apply_interaction_policy
        apply_interaction_policy(data, agent['id'], conn)
        inbox = {row['id']: row for row in memory.get('inbox', [])}
        outbox = memory.setdefault('notification_outbox', {})
        closed = set()
        for notification_id in ids:
            if notification_id not in inbox:
                continue
            row = inbox[notification_id]
            if row.get('status') == 'resolved':
                continue
            if row.get('policy_ignored') or (draft_id and draft_id in row.get('policy_ignored_draft_ids', [])):
                if draft_id:
                    closed.add(notification_id)
                continue
            previous = outbox.get(str(notification_id), {})
            if previous.get('state') == 'policy_ignored':
                if not draft_id or draft_id in previous.get('policy_ignored_draft_ids', []):
                    if draft_id:
                        closed.add(notification_id)
                    continue
                outbox.pop(str(notification_id))
            outbox.setdefault(str(notification_id), {'notification_id': notification_id, 'state': 'pending',
                'attempts': 0, 'next_retry_at': now, 'last_error': '', 'created_at': at, 'draft_id': draft_id})
            row['status'] = 'ack_pending'
        return closed

    closed = controller.store.change(queue)
    drain_resolutions(controller, agent, token)
    memory = controller.store.read()['memory'].get(agent['id'], {})
    states = {row['id']: row.get('status') for row in memory.get('inbox', [])}
    return all(states.get(notification_id) == 'resolved' or notification_id in closed for notification_id in ids)


def drain_resolutions(controller, agent, token):
    """Retry only previously queued, individual notification acknowledgements."""
    _, memory = _policy_memory(controller, agent)
    ignored_ids = {notice.get('id', notice.get('notification_id')) for notice in memory.get('inbox', [])
                   if notice.get('policy_ignored')}
    due = [row for row in memory.get('notification_outbox', {}).values()
           if row.get('state') == 'pending' and row.get('next_retry_at', 0) <= controller.clock()
           and row.get('notification_id') not in ignored_ids]
    if not due or agent.get('account_state') != 'ready':
        return
    client = None
    try:
        client = controller._sdk()
        _check(controller, agent, token)
        account = controller._account(client, agent, token)
        method = getattr(account, 'resolve_notification', None)
        if not callable(method):
            return
        for row in due[:5]:
            notification_id = row['notification_id']
            success, wait, message = False, 5, ''
            try:
                _check(controller, agent, token)
                # Re-read policy immediately before each acknowledgement; the
                # global setting or a binding may have changed since queuing.
                _, current_memory = _policy_memory(controller, agent)
                if any(notice.get('id', notice.get('notification_id')) == notification_id
                       and notice.get('policy_ignored') for notice in current_memory.get('inbox', [])):
                    continue
                _call(controller, 'resolve', method, notification_id)
                success = True
            except Exception as exc:
                wait = _delay(exc)
                message = '通知确认等待下一次重试'
            now, at = controller.clock(), _at(controller.clock())

            def finish(data, conn):
                controller._check_lease(data, agent['id'], token)
                own = data['memory'].setdefault(agent['id'], {})
                item = own.setdefault('notification_outbox', {}).get(str(notification_id))
                if item is None:
                    return
                # A concurrent settings change can cancel this acknowledgement
                # while its SDK request is in flight. A failed response must
                # not restore its retry, even if the policy was disabled again.
                if item.get('state') == 'policy_ignored' and not success:
                    return
                item.update(state='resolved' if success else 'pending', attempts=item.get('attempts', 0) + 1,
                            next_retry_at=0 if success else now + wait, last_error=message)
                if success:
                    item['resolved_at'] = at
                    for notice in own.get('inbox', []):
                        if notice['id'] == notification_id:
                            notice.update(status='resolved', resolved_at=at)
                from .agent_notifications import apply_interaction_policy
                apply_interaction_policy(data, agent['id'], conn)
                completed = [key for key, value in own['notification_outbox'].items() if value.get('state') == 'resolved']
                for key in completed[:-MAX_INBOX]:
                    own['notification_outbox'].pop(key, None)

            controller.store.change(finish)
            if not success:
                break
    except Exception as exc:
        # Even an authentication failure gets a durable retry time. Resolution
        # itself is idempotent after a remote success and a lost local lease.
        now, wait = controller.clock(), _delay(exc)
        try:
            def defer(data, conn):
                controller._check_lease(data, agent['id'], token)
                pending = data['memory'].setdefault(agent['id'], {}).setdefault('notification_outbox', {})
                for old in due[:5]:
                    item = pending.get(str(old['notification_id']))
                    if item and item.get('state') == 'pending':
                        item.update(next_retry_at=now + wait, last_error='通知确认等待下一次重试')
            controller.store.change(defer)
        except Exception:
            pass
        return
    finally:
        if client is not None:
            controller._close(client)
