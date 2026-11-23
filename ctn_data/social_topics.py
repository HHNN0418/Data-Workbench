"""Shared public topic catalogue and explicitly identified news account feeds.

Versioned JSON read models use the existing metadata/checkpoint tables. All
remote responses retain raw provenance; readers never trigger remote requests.
"""
from __future__ import annotations

import json
from urllib.parse import urlencode

from .db import raw_record, timestamp_key, utc_now
from .ingest import save_accounts, save_posts
from .transport import BudgetExceeded, TransportError, validate_path

NEWS_USERNAMES = ('DailyKingstonia', 'KingstoniaHerald')
TAGS_KEY = 'social_context:tags:v1'
POPULAR_KEY = 'social_context:popular:v1'
ALL_KEY = 'social_context:all_tags'


def _get(conn, table, key):
    row = conn.execute('SELECT value FROM ' + table + ' WHERE key=?', (key,)).fetchone()
    try:
        value = json.loads(row['value']) if row else {}
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _put(conn, table, key, value, at):
    encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    if table == 'meta':
        conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (key, encoded))
    else:
        conn.execute('''INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at''', (key, encoded, at))


def _page(payload, path):
    if not isinstance(payload, dict) or not isinstance(payload.get('items'), list):
        raise ValueError('public page must contain an items list')
    items, cursor = payload['items'], payload.get('next_cursor')
    if len(items) > 200 or any(not isinstance(item, dict) for item in items):
        raise ValueError('public page must contain at most 200 objects')
    if cursor is not None and not isinstance(cursor, str):
        raise ValueError('public cursor must be text or null')
    if cursor:
        validate_path('social', path + '?' + urlencode({'cursor': cursor}))
    return items, cursor


def _tag(item, at, old=None, rank=None):
    tag_id, name = item.get('tag_id'), item.get('name')
    if type(tag_id) is not int or tag_id <= 0:
        raise ValueError('tag_id must be a positive integer')
    if not isinstance(name, str) or not name or len(name) > 200:
        raise ValueError('tag requires a name of at most 200 characters')
    count = item.get('post_count')
    if count is not None and (type(count) is not int or count < 0):
        raise ValueError('tag post_count must be a nonnegative integer or null')
    return {'tag_id': tag_id, 'name': name, 'post_count': count, 'popular_rank': rank,
            'created_at': item.get('created_at', (old or {}).get('created_at')),
            'first_seen': (old or {}).get('first_seen', at), 'last_seen': at, 'observed_at': at}


def _popular(conn, transport, result):
    path = '/api/tags/popular'
    payload = transport.get_json('social', path)
    items, cursor = _page(payload, path)
    if cursor:
        raise ValueError('popular tags must be a single page')
    at = utc_now()
    rows = [_tag(item, at, rank=index + 1) for index, item in enumerate(items)]
    if len({row['tag_id'] for row in rows}) != len(rows):
        raise ValueError('popular tags contain duplicate IDs')
    with conn:
        raw_record(conn, 'social:popular_tags', path, payload, at)
        # Empty successful responses are real snapshots and clear the old list.
        _put(conn, 'meta', POPULAR_KEY, {'version': 1, 'items': rows, 'observed_at': at}, at)
    result['popular_tags'] = len(rows)


def _all_tags(conn, transport, result, page_limit):
    state = _get(conn, 'checkpoints', ALL_KEY)
    cursor = state.get('cursor')
    path = '/api/tags'
    query = {'limit': page_limit}
    if cursor:
        query['cursor'] = cursor
    try:
        payload = transport.get_json('social', path + '?' + urlencode(query))
    except TransportError as exc:
        if cursor and exc.status in (400, 404, 410):
            with conn:
                _put(conn, 'checkpoints', ALL_KEY, {**state, 'cursor': None, 'cycle_ids': []}, utc_now())
        raise
    items, next_cursor = _page(payload, path)
    if next_cursor and next_cursor in {cursor, *state.get('visited_cursors', [])}:
        raise ValueError('server repeated a tag pagination cursor')
    at = utc_now()
    stored = _get(conn, 'meta', TAGS_KEY)
    catalog = {str(row['tag_id']): row for row in stored.get('items', [])}
    rows = [_tag(item, at, catalog.get(str(item.get('tag_id')))) for item in items]
    cycle_ids = set(state.get('cycle_ids', []) if cursor else [])
    for row in rows:
        catalog[str(row['tag_id'])] = row
        cycle_ids.add(row['tag_id'])
    complete = not items or not next_cursor
    if complete:
        catalog = {key: row for key, row in catalog.items() if row['tag_id'] in cycle_ids}
    new_state = {**state, 'version': 1, 'cursor': None if complete else next_cursor,
                 'observed_at': at, 'pages_completed': state.get('pages_completed', 0) + 1,
                 'completed_at': at if complete else state.get('completed_at'),
                 'cycle_ids': [] if complete else sorted(cycle_ids),
                 'visited_cursors': [] if complete else (state.get('visited_cursors', []) if cursor else []) + ([cursor] if cursor else [])}
    with conn:
        raw_record(conn, 'social:tags', path, payload, at)
        _put(conn, 'meta', TAGS_KEY, {'version': 1, 'items': list(catalog.values()), 'observed_at': at}, at)
        _put(conn, 'checkpoints', ALL_KEY, new_state, at)
    result['tags'] = len(rows)


def _position(post):
    key = timestamp_key(post.get('created_at'))
    return (key, post['post_id']) if key and type(post.get('post_id')) is int else None


def _account_key(username):
    return 'social_context:account:' + username.lower()


def _news_account(conn, transport, result, username, page_limit):
    key = _account_key(username)
    state = _get(conn, 'checkpoints', key)
    account_id = state.get('account_id')
    if type(account_id) is not int:
        path = '/api/accounts?' + urlencode({'q': username, 'limit': 50})
        payload = transport.get_json('social', path)
        candidates, _ = _page(payload, '/api/accounts')
        matches = [row for row in candidates if row.get('username') == username]
        if len(matches) != 1:
            raise ValueError('exact news account username was not uniquely found')
        account = matches[0]
        at = utc_now()
        with conn:
            raw_record(conn, 'social:news_accounts', '/api/accounts', payload, at)
            save_accounts(conn, [account], at)
            state.update(username=username, account_id=account['account_id'],
                         verified=account.get('verified'), observed_at=at)
            _put(conn, 'checkpoints', key, state, at)
        account_id = account['account_id']
        result['accounts'] = result.get('accounts', 0) + 1
    path = f'/api/accounts/{account_id}/posts'

    def fetch(cursor=None, stream='head'):
        query = {'limit': page_limit}
        if cursor:
            query['cursor'] = cursor
        try:
            payload = transport.get_json('social', path + '?' + urlencode(query), entity_id=account_id)
        except TransportError as exc:
            if cursor and exc.status in (400, 404, 410):
                with conn:
                    reset = {**state, 'live_cursor' if stream == 'live' else 'cursor': None}
                    if stream == 'history':
                        reset.update(reached_end=False, history_started=False, history_visited_cursors=[])
                    else:
                        reset['live_cycle_head'] = None
                        reset['live_visited_cursors'] = []
                    _put(conn, 'checkpoints', key, reset, utc_now())
            raise
        posts, next_cursor = _page(payload, path)
        visited_key = 'live_visited_cursors' if stream == 'live' else 'history_visited_cursors'
        visited = state.get(visited_key, []) if cursor else []
        if next_cursor and (next_cursor == cursor or (cursor and next_cursor in visited)):
            raise ValueError('server repeated a news account pagination cursor')
        if any(post.get('author_id') != account_id for post in posts):
            raise ValueError('news account feed contains a different author')
        at = utc_now()
        positions = [value for post in posts if (value := _position(post)) is not None]
        head = max(positions, default=None)
        updated = dict(state)
        updated.update(observed_at=at, pages_completed=state.get('pages_completed', 0) + 1)
        if head:
            updated['last_post_at'] = max(state.get('last_post_at') or '', head[0])
        if stream == 'head' and not state.get('history_started'):
            updated.update(history_started=True, cursor=next_cursor if posts else None,
                           reached_end=not posts or not next_cursor, watermark=head,
                           completed_at=at if not posts or not next_cursor else state.get('completed_at'),
                           history_visited_cursors=[])
        elif stream == 'history':
            updated.update(cursor=next_cursor if posts else None, reached_end=not posts or not next_cursor,
                           completed_at=at if not posts or not next_cursor else state.get('completed_at'),
                           history_visited_cursors=visited + [cursor] if posts and next_cursor else [])
        elif stream == 'live' or not state.get('live_cursor'):
            boundary = tuple(state['watermark']) if state.get('watermark') else None
            cycle_head = tuple(state['live_cycle_head']) if state.get('live_cycle_head') else head
            complete = not posts or not next_cursor or (boundary and any(pos <= boundary for pos in positions))
            updated.update(live_cursor=None if complete else next_cursor,
                           live_cycle_head=None if complete else cycle_head,
                           live_visited_cursors=[] if complete else visited + ([cursor] if cursor else []))
            if complete:
                updated['watermark'] = max([pos for pos in (boundary, cycle_head) if pos], default=None)
        with conn:
            raw_record(conn, 'social:news_account:' + username, path, payload, at)
            save_posts(conn, posts, 'news_account:' + username, at)
            _put(conn, 'checkpoints', key, updated, at)
        state.clear()
        state.update(updated)
        result.setdefault('post_ids', set()).update(post['post_id'] for post in posts)

    was_started = state.get('history_started')
    old_live = state.get('live_cursor')
    fetch()
    if old_live:
        fetch(old_live, 'live')
    # First head also starts history; subsequent rounds advance that history
    # independently from the live watermark, including a live gap > one page.
    if was_started and not state.get('reached_end') and state.get('cursor'):
        fetch(state['cursor'], 'history')


def collect_social_context(conn, transport, cfg, result):
    """One page of tags plus independent public news feed work per bounded run."""
    if not cfg.get('collect_social_context', True):
        return
    jobs = [('popular_tags', lambda: _popular(conn, transport, result)),
            ('tags', lambda: _all_tags(conn, transport, result, cfg.get('page_limit', 50)))]
    # Rotate every route so a slow early endpoint cannot starve another route
    # on every run. The route cursors are independent and survive deferral.
    turn = _get(conn, 'checkpoints', 'social_context:turn').get('turn', 0)
    jobs += [('news_account:' + name, lambda name=name: _news_account(conn, transport, result, name, cfg.get('page_limit', 50)))
             for name in NEWS_USERNAMES]
    start = turn % len(jobs)
    jobs = jobs[start:] + jobs[:start]
    with conn:
        _put(conn, 'checkpoints', 'social_context:turn', {'turn': turn + 1}, utc_now())
    for stage, call in jobs:
        try:
            call()
        except BudgetExceeded as exc:
            # Transport uses the same exception for a route whose persisted
            # cooldown cannot fit this run and for an actually exhausted run.
            # The former must leave unrelated public routes free to proceed.
            clock = getattr(transport, 'clock', None)
            deadline = getattr(transport, 'deadline', None)
            exhausted = (transport.attempts >= transport.request_budget or
                         (callable(clock) and deadline is not None and clock() >= deadline))
            if exhausted:
                raise
            result.setdefault('errors', []).append({'stage': stage, 'entity_id': None,
                'status': getattr(exc, 'status', None), 'message': str(exc)[:180], 'deferred': True})
        except Exception as exc:
            message = str(exc) if isinstance(exc, (TransportError, ValueError, TypeError)) else type(exc).__name__
            result.setdefault('errors', []).append({'stage': stage, 'entity_id': None,
                'status': getattr(exc, 'status', None), 'message': message[:180]})


def read_social_context(conn, q='', limit=50, offset=0, popular=False):
    """Read a local catalogue page and independent popular observation metadata."""
    limit, offset = max(1, min(500, int(limit))), max(0, int(offset))
    catalog = _get(conn, 'meta', TAGS_KEY)
    ranking = _get(conn, 'meta', POPULAR_KEY)
    all_state = _get(conn, 'checkpoints', ALL_KEY)
    popular_items = ranking.get('items', [])
    popularity = {row['tag_id']: row for row in popular_items}
    rows = list(popular_items) if popular else [{**row,
        'post_count': popularity.get(row['tag_id'], {}).get('post_count'),
        'popular_rank': popularity.get(row['tag_id'], {}).get('popular_rank'),
        'popular_observed_at': ranking.get('observed_at') if row['tag_id'] in popularity else None}
        for row in catalog.get('items', [])]
    query = str(q).strip().lstrip('#').casefold()
    rows = [row for row in rows if query in row['name'].casefold()]
    if not popular:
        rows.sort(key=lambda row: (row['name'].casefold(), row['tag_id']))
    all_meta = {field: all_state.get(field) for field in ('cursor', 'completed_at', 'observed_at')}
    all_meta['pages_completed'] = all_state.get('pages_completed', 0)
    popular_meta = {'observed_at': ranking.get('observed_at'), 'count': len(popular_items)}
    accounts = []
    for username in NEWS_USERNAMES:
        state = _get(conn, 'checkpoints', _account_key(username))
        # Existing whole-feed observations are useful even before the first
        # dedicated discovery, but never fabricate a completed account crawl.
        account = conn.execute('SELECT account_id,verified,last_seen FROM accounts WHERE username=?', (username,)).fetchone()
        account_id = state.get('account_id') or (account['account_id'] if account else None)
        count = conn.execute('SELECT COUNT(*) FROM posts WHERE author_id=?', (account_id,)).fetchone()[0] if account_id else 0
        accounts.append({'username': username, 'account_id': account_id,
            'verified': state.get('verified', bool(account['verified']) if account and account['verified'] is not None else None),
            'observed_at': state.get('observed_at'), 'last_post_at': state.get('last_post_at'),
            'posts_collected': count, 'cursor': state.get('cursor'), 'live_cursor': state.get('live_cursor'),
            'completed_at': state.get('completed_at')})
    return {'version': 1, 'items': rows[offset:offset + limit], 'total': len(rows),
        'limit': limit, 'offset': offset, 'popular': bool(popular),
        'observed_at': (ranking if popular else catalog).get('observed_at'),
        'all_tags': all_meta, 'popular_tags': popular_meta, 'popular_items': popular_items,
        'coverage': {'all_tags': all_meta, 'popular_tags': popular_meta}, 'news_accounts': accounts}


def topical_context(conn, limit=15):
    snapshot = read_social_context(conn, limit=limit, popular=True)
    return {'version': 1, 'tags': snapshot['items'], 'observed_at': snapshot['observed_at'],
        'popular_observed_at': snapshot['popular_tags']['observed_at'],
        'all_tags_completed_at': snapshot['all_tags']['completed_at'], 'news_accounts': snapshot['news_accounts']}
