"""Bounded referenced-post enrichment and paginated public replies."""
import time
import urllib.parse
from .collector import _checkpoint, _set_checkpoint, _error
from .db import utc_now
from .ingest import save_posts
from .transport import BudgetExceeded


def enrich_relations(conn, transport, cfg, result):
    stats = result.setdefault('relations', {'targets_saved': 0, 'reply_pages': 0, 'replies_saved': 0})
    # Drop references to collected posts by primary key before de-duplicating:
    # only the few missing targets are distinct-ed, not every reference.
    targets = conn.execute('''SELECT r.target FROM (
          SELECT DISTINCT refs.target FROM (
            SELECT repost_of_post_id AS target FROM posts WHERE repost_of_post_id IS NOT NULL
            UNION ALL SELECT reply_to_post_id FROM posts WHERE reply_to_post_id IS NOT NULL
            UNION ALL SELECT root_post_id FROM posts WHERE root_post_id IS NOT NULL) refs
          WHERE NOT EXISTS(SELECT 1 FROM posts p WHERE p.post_id=refs.target)) r
        LEFT JOIN checkpoints q ON q.key='relation:' || r.target
        WHERE COALESCE(json_extract(q.value,'$.retry_after_epoch'),0)<=?
        ORDER BY q.updated_at IS NOT NULL,q.updated_at,r.target LIMIT ?''',
        (time.time(), cfg['relation_limit'])).fetchall()
    for row in targets:
        target = row['target']
        key = f'relation:{target}'
        try:
            post = transport.get_json('social', f'/api/posts/{target}', entity_id=target)
            if not isinstance(post, dict) or post.get('post_id') != target:
                raise ValueError('referenced post ID mismatch')
            with conn:
                save_posts(conn, [post], 'relation')
                _set_checkpoint(conn, key, {'status': 'available'}, utc_now())
            stats['targets_saved'] += 1
        except BudgetExceeded:
            raise
        except Exception as exc:
            status = getattr(exc, 'status', None)
            with conn:
                _set_checkpoint(conn, key, {'status': 'unavailable' if status in (403,404,410) else 'retry',
                    'http_status': status, 'retry_after_epoch': time.time() + (3600 if status in (403,404,410) else 300)}, utc_now())
            _error(result, 'relation', exc, target)

    # Rotate known threads rather than repeatedly loading one popular parent.
    # A thread has replies when its latest snapshot counts any (kept in the
    # counter_sampling read model) or a collected post replies to it.
    parents = conn.execute('''SELECT p.post_id FROM posts p
        JOIN (SELECT post_id FROM counter_sampling WHERE reply_count>0
              UNION SELECT reply_to_post_id FROM posts WHERE reply_to_post_id IS NOT NULL) replied
          ON replied.post_id=p.post_id
        LEFT JOIN checkpoints q ON q.key='replies:' || p.post_id
        WHERE p.kind IN ('post','reply')
        AND COALESCE(json_extract(q.value,'$.retry_after_epoch'),0)<=?
        ORDER BY q.updated_at IS NOT NULL,q.updated_at,p.created_at DESC,p.post_id LIMIT ?''',
        (time.time(), cfg['reply_parent_limit'])).fetchall()
    for row in parents:
        pid = row['post_id']
        key = f'replies:{pid}'
        state = _checkpoint(conn, key)
        cursor = state.get('cursor')
        params = {'limit': 50}
        if cursor:
            params['cursor'] = cursor
        try:
            payload = transport.get_json('social', f'/api/posts/{pid}/replies?' + urllib.parse.urlencode(params), entity_id=pid)
            if not isinstance(payload, dict) or not isinstance(payload.get('items'), list):
                raise ValueError('replies must contain items')
            next_cursor = payload.get('next_cursor')
            if next_cursor is not None and (not isinstance(next_cursor, str) or next_cursor == cursor):
                raise ValueError('invalid or repeated replies cursor')
            if any(not isinstance(post, dict) or post.get('reply_to_post_id') != pid for post in payload['items']):
                raise ValueError('reply parent ID mismatch')
            with conn:
                save_posts(conn, payload['items'], 'replies')
                _set_checkpoint(conn, key, {'cursor': next_cursor, 'status': 'partial' if next_cursor else 'observed_end'}, utc_now())
            stats['reply_pages'] += 1
            stats['replies_saved'] += len(payload['items'])
        except BudgetExceeded:
            raise
        except Exception as exc:
            status = getattr(exc, 'status', None)
            with conn:
                _set_checkpoint(conn, key, {**state, 'cursor': None if status in (400,404,410) else cursor,
                    'status': 'unavailable' if status in (403,404,410) else 'retry', 'http_status': status,
                    'retry_after_epoch': time.time() + (3600 if status in (403,404,410) else 300)}, utc_now())
            _error(result, 'replies', exc, pid)
