"""Choose a public-read exit; task ownership and writes stay in the backend."""
import json
import math
import time
from pathlib import Path

from .db import begin_write, utc_now

PROXY_ENABLED_PATH = Path(__file__).resolve().parent.parent / 'data' / 'proxy-enabled'
PUBLIC_EXITS = (
    {'proxy_url': None, 'actor': 'backend'},
    {'proxy_url': 'http://127.0.0.1:18881', 'actor': 'proxy:docker-au-a'},
    {'proxy_url': 'http://127.0.0.1:18882', 'actor': 'proxy:docker-au-b'},
)


def choose_public_egress(conn):
    """Round-robin healthy fixed exits under one persistent atomic cursor.

    A successful recent worker heartbeat is only eligibility evidence, not a
    promise that its next request succeeds. Once selected, transport failures
    never fall back to direct within that task.
    """
    if not PROXY_ENABLED_PATH.exists():
        return dict(PUBLIC_EXITS[0])
    now = time.time()
    with conn:
        begin_write(conn, 'public_egress')
        available = {0}
        for index, exit in enumerate(PUBLIC_EXITS[1:], 1):
            worker = exit['actor'].removeprefix('proxy:')
            row = conn.execute('SELECT value FROM meta WHERE key=?', ('worker_status:' + worker,)).fetchone()
            try:
                value = json.loads(row['value']) if row else {}
                seen = float(value.get('last_seen_epoch', 0))
                if math.isfinite(seen) and 0 <= now - seen <= 60 and value.get('last_http_status') == 200:
                    available.add(index)
            except (ValueError, TypeError, AttributeError):
                pass
        row = conn.execute('SELECT value FROM checkpoints WHERE key=?', ('egress:public',)).fetchone()
        try:
            cursor = int(json.loads(row['value']).get('next_index', 0)) if row else 0
        except (ValueError, TypeError, AttributeError):
            cursor = 0
        selected = next((cursor + step) % len(PUBLIC_EXITS) for step in range(len(PUBLIC_EXITS))
                        if (cursor + step) % len(PUBLIC_EXITS) in available)
        conn.execute('''INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at''',
            ('egress:public', json.dumps({'next_index': (selected + 1) % len(PUBLIC_EXITS)}), utc_now()))
        return dict(PUBLIC_EXITS[selected])
