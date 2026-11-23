"""Bounded anonymous GET checks before a Blue suggestion or report.

Only the existing transport's request logs and cooldowns are written. A failed
check never changes collected posts or classifies a post as hidden.
"""
from __future__ import annotations

from .db import connect, utc_now
from .transport import ReadOnlyTransport


def check(db_path, post_ids, *, deadline_seconds=30):
    """Return availability for each distinct post ID; uncertainty blocks reporting."""
    post_ids = list(dict.fromkeys(post_ids))
    if any(type(post_id) is not int or post_id <= 0 for post_id in post_ids):
        raise ValueError('帖子 ID 无效')
    if not post_ids:
        return {}
    conn = connect(db_path)
    try:
        transport = ReadOnlyTransport(conn, timeout=5, max_retries=0,
                                      request_budget=len(post_ids), deadline_seconds=deadline_seconds,
                                      max_wait_seconds=0, actor='blue-check')
        results = {}
        for post_id in post_ids:
            try:
                payload = transport.get_json('social', f'/api/posts/{post_id}', entity_id=post_id)
            except Exception as exc:
                status = getattr(exc, 'status', None)
                results[post_id] = {'status': 'unavailable' if status in (404, 410) else 'unverified',
                                    'http_status': status, 'error': str(exc) or type(exc).__name__,
                                    'checked_at': utc_now()}
            else:
                available = isinstance(payload, dict) and type(payload.get('post_id')) is int and payload['post_id'] == post_id
                results[post_id] = {'status': 'available' if available else 'unverified', 'http_status': 200,
                                    'error': '' if available else '平台返回的帖子数据无法确认',
                                    'checked_at': utc_now()}
        return results
    finally:
        conn.close()
