"""Our accounts the platform has suspended, read from the official activity log.

The platform announces it in the team log, for example "Moderation: your
account @maya_collins (id 5554) has been suspended after 3 of its posts were
hidden as inauthentic. It can no longer log in or post." (22 Oct 23:49). Both
the CSV export (official_activity) and the live activity table preview are
read, so a suspension shows within minutes of the announcement. Read-only.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from .db import connect

_SUSPENDED = re.compile(r'your account @(?P<username>[\w.\-]+) \(id (?P<account_id>\d+)\) has been suspended', re.I)
CACHE_SECONDS = 60
_cache_lock = threading.Lock()
_cache = {}


def parse(rows):
    """{account_id: {'at', 'username', 'message'}} from (occurred_at, message) rows; the first notice wins."""
    found = {}
    for occurred_at, message in sorted(rows, key=lambda row: str(row[0] or '')):
        match = _SUSPENDED.search(message or '')
        if match:
            found.setdefault(int(match.group('account_id')), {
                'at': occurred_at, 'username': match.group('username'), 'message': (message or '')[:500]})
    return found


def suspended(source, *, strict=False):
    """Suspended account ids with the notice; cached for a minute per path.

    When the log cannot be read: empty, or None with ``strict`` (callers that must then
    fall back to a conservative rule, e.g. the role limit counting every role).
    """
    key = str(source) if isinstance(source, (str, Path)) else None
    clock = time.monotonic()
    if key is not None:
        with _cache_lock:
            cached = _cache.get(key)
            if cached and cached[0] > clock:
                return cached[1] if cached[1] is not None else (None if strict else {})
    from .npc_calibration import preview_rows
    try:
        conn = connect(source) if key is not None else source
        try:
            rows = [(row[0], row[1]) for row in conn.execute(
                "SELECT occurred_at,message FROM official_activity WHERE message LIKE '%has been suspended%'")]
            rows += [(item.get('occurred_at'), item.get('message')) for item in preview_rows(conn)]
        finally:
            if key is not None:
                conn.close()
        found = parse(rows)
    except Exception:
        found = None
    if key is not None:
        with _cache_lock:
            _cache[key] = (clock + CACHE_SECONDS, found)
    return found if found is not None else (None if strict else {})
