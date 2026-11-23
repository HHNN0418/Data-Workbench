"""Official activity for readers: the CSV export plus the newer live-table rows.

The team page's activity CSV (official_activity) lags the page by up to two
hours; the browser extension also sends the page's live activity table, kept
apart in meta (npc_calibration.PREVIEW_KEY). Score and exposure readers append
the live rows the export has not reached yet, parsed exactly like an import and
marked ``preview``. Once the export contains a row's time they are no longer
added, so nothing is counted twice. Read-only.
"""
from __future__ import annotations

from .db import timestamp_key


def pending_rows(conn):
    """Live-table rows newer than the newest exported row, as official_activity-shaped dicts (oldest first)."""
    from .imports import parse_activity
    from .npc_calibration import preview_rows
    latest = conn.execute('SELECT MAX(ctn_timestamp(occurred_at)) FROM official_activity').fetchone()[0]
    rows = []
    for item in preview_rows(conn):
        stamp = timestamp_key(item.get('occurred_at'))
        event_type, message = item.get('event_type') or '', item.get('message') or ''
        if not stamp or (latest and stamp <= latest) or not isinstance(message, str):
            continue
        parsed = parse_activity(event_type, message)
        rows.append({'id': None, 'import_id': None, 'occurred_at': stamp, 'event_type': event_type, 'message': message,
                     'track': parsed['track'], 'points_decimal': parsed['points_decimal'],
                     'parse_status': parsed['parse_status'], 'parse_note': parsed.get('parse_note'), 'preview': True})
    return sorted(rows, key=lambda row: row['occurred_at'])
