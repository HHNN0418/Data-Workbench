"""Who engaged with our team: likers of our posts and followers of our accounts.

The official hourly report counts the NPC likes and follows our team's content
received, never who they were. The platform lists both, newest first, with the
time of each (``liked_at``, ``followed_at``), so npc_calibration can treat an
hour's likes and follows as bags with a known number of NPCs, exactly as it
does for replies (D70).

Requests stay small: a post's likers are read again only after its like
counter (already sampled by the collector) passes the count seen last time,
and each team account's followers at most every FOLLOWER_REFRESH_SECONDS.
Both lists are read from the newest end and stop at the first page that adds
no one. A like or follow that is later withdrawn keeps its row; ``last_seen``
says when it was last listed.
"""
from __future__ import annotations

import json
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from .collector import _checkpoint, _error, _set_checkpoint
from .db import utc_now
from .transport import BudgetExceeded

LIKER_POSTS_PER_RUN = 3
FOLLOWER_ACCOUNTS_PER_RUN = 1
FOLLOWER_REFRESH_SECONDS = 1200
# Enough for every team post the hourly reports still cover, with room for backfill.
POST_WINDOW_DAYS = 4
PAGES_PER_TARGET = 6
PAGE_SIZE = 50
UNAVAILABLE = (403, 404, 410)


def _page(transport, path, entity_id, cursor):
    params = {'limit': PAGE_SIZE}
    if cursor:
        params['cursor'] = cursor
    payload = transport.get_json('social', path + '?' + urllib.parse.urlencode(params), entity_id=entity_id)
    if not isinstance(payload, dict) or not isinstance(payload.get('items'), list):
        raise ValueError('list must contain items')
    next_cursor = payload.get('next_cursor')
    if next_cursor is not None and (not isinstance(next_cursor, str) or next_cursor == cursor):
        raise ValueError('invalid or repeated cursor')
    for item in payload['items']:
        if not isinstance(item, dict) or type(item.get('account_id')) is not int or item['account_id'] <= 0:
            raise ValueError('list item without a valid account_id')
    return payload['items'], next_cursor


def _read_list(conn, transport, path, entity_id, known, save, stats, page_key, *, complete):
    """Pages from the newest end, each saved as it arrives.

    After one complete read, a page that adds nobody ends it; until then (a first
    read cut short by the request budget) every page is read again.
    """
    cursor, added = None, 0
    for _ in range(PAGES_PER_TARGET):
        items, cursor = _page(transport, path, entity_id, cursor)
        stats[page_key] += 1
        new = [item for item in items if item['account_id'] not in known]
        with conn:
            save(items)
        known.update(item['account_id'] for item in items)
        added += len(new)
        if not cursor or (complete and not new):
            break
    return added


def _failed(conn, key, state, exc, result, stage, entity_id):
    status = getattr(exc, 'status', None)
    with conn:
        _set_checkpoint(conn, key, {**state, 'status': 'unavailable' if status in UNAVAILABLE else 'retry',
            'http_status': status, 'retry_after_epoch': time.time() + (3600 if status in UNAVAILABLE else 300)}, utc_now())
    _error(result, stage, exc, entity_id)


def _team(conn):
    """The accounts whose engagement the official reports count, as npc_calibration reads them."""
    from .npc_calibration import team_accounts
    return sorted(team_accounts(conn))


def _liker_targets(conn, team, now):
    """Team posts whose like counter passed the count seen when their likers were last read."""
    if not team:
        return []
    since = (datetime.fromtimestamp(now, timezone.utc) - timedelta(days=POST_WINDOW_DAYS)).isoformat().replace('+00:00', 'Z')
    rows = conn.execute(f'''SELECT p.post_id, p.created_at, q.value AS state,
          (SELECT s.like_count FROM counter_snapshots s WHERE s.post_id=p.post_id
           ORDER BY s.observed_at DESC LIMIT 1) AS likes
        FROM posts p LEFT JOIN checkpoints q ON q.key='likers:' || p.post_id
        WHERE p.author_id IN ({','.join('?' * len(team))}) AND p.created_at>=?''', (*team, since)).fetchall()
    due = []
    for row in rows:
        try:
            state = json.loads(row['state']) if row['state'] else {}
        except ValueError:
            state = {}
        likes = row['likes'] or 0
        if likes > state.get('like_count', 0) and state.get('retry_after_epoch', 0) <= now:
            due.append((bool(state), row['created_at'] or '', row['post_id'], likes, state))
    # New likes on posts already read go first, then the backfill, newest posts first.
    due.sort(key=lambda item: item[1], reverse=True)
    due.sort(key=lambda item: not item[0])
    return due


def _collect_likers(conn, transport, result, stats, team, now):
    for _, _, post_id, likes, state in _liker_targets(conn, team, now)[:LIKER_POSTS_PER_RUN]:
        key = 'likers:' + str(post_id)
        known = {row[0] for row in conn.execute('SELECT account_id FROM post_likes WHERE post_id=?', (post_id,))}
        seen = utc_now()

        def save(items, post_id=post_id, seen=seen):
            for item in items:
                liked_at = item.get('liked_at') if isinstance(item.get('liked_at'), str) else None
                conn.execute('''INSERT INTO post_likes(post_id,account_id,liked_at,first_seen,last_seen)
                    VALUES(?,?,?,?,?) ON CONFLICT(post_id,account_id) DO UPDATE SET last_seen=excluded.last_seen,
                    liked_at=COALESCE(post_likes.liked_at,excluded.liked_at)''',
                    (post_id, item['account_id'], liked_at, seen, seen))
        try:
            stats['likes_saved'] += _read_list(conn, transport, f'/api/posts/{post_id}/likes', post_id,
                                               known, save, stats, 'liker_pages', complete=bool(state.get('like_count')))
            with conn:
                _set_checkpoint(conn, key, {'like_count': likes, 'status': 'observed'}, utc_now())
        except BudgetExceeded:
            raise
        except Exception as exc:
            _failed(conn, key, state, exc, result, 'likers', post_id)


def _collect_followers(conn, transport, result, stats, team, now):
    due = []
    for account_id in team:
        state = _checkpoint(conn, 'followers:' + str(account_id))
        if max(state.get('read_at_epoch', 0) + FOLLOWER_REFRESH_SECONDS, state.get('retry_after_epoch', 0)) <= now:
            due.append((state.get('read_at_epoch', 0), account_id, state))
    for _, account_id, state in sorted(due)[:FOLLOWER_ACCOUNTS_PER_RUN]:
        key = 'followers:' + str(account_id)
        known = {row[0] for row in conn.execute('SELECT follower_id FROM account_followers WHERE account_id=?', (account_id,))}
        seen = utc_now()

        def save(items, account_id=account_id, seen=seen):
            for item in items:
                followed_at = item.get('followed_at') if isinstance(item.get('followed_at'), str) else None
                conn.execute('''INSERT INTO account_followers(account_id,follower_id,followed_at,first_seen,last_seen)
                    VALUES(?,?,?,?,?) ON CONFLICT(account_id,follower_id) DO UPDATE SET last_seen=excluded.last_seen,
                    followed_at=COALESCE(account_followers.followed_at,excluded.followed_at)''',
                    (account_id, item['account_id'], followed_at, seen, seen))
        try:
            stats['follows_saved'] += _read_list(conn, transport, f'/api/accounts/{account_id}/followers', account_id,
                                                 known, save, stats, 'follower_pages',
                                                 complete=bool(state.get('read_at_epoch')))
            with conn:
                _set_checkpoint(conn, key, {'read_at_epoch': now, 'status': 'observed'}, utc_now())
        except BudgetExceeded:
            raise
        except Exception as exc:
            _failed(conn, key, state, exc, result, 'followers', account_id)


def collect(conn, transport, cfg, result, now=None):
    """One bounded pass: likers of a few team posts, then one team account's followers."""
    now = time.time() if now is None else now
    stats = result.setdefault('engagement', {'liker_pages': 0, 'likes_saved': 0,
                                             'follower_pages': 0, 'follows_saved': 0})
    team = _team(conn)
    _collect_likers(conn, transport, result, stats, team, now)
    _collect_followers(conn, transport, result, stats, team, now)
    return stats
