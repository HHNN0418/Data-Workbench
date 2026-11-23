"""Durable shared work queue. One task key, one expiring ownership token.

All timestamps used for scheduling are UTC epoch seconds. Completion is fenced
by the lease token, so a slow worker cannot complete a reassigned task. Workers
use the same endpoint coordinator regardless of their network exit.
"""
import json
import time
import uuid

from .db import connect, utc_now


SCHEMA = '''CREATE TABLE IF NOT EXISTS collection_tasks(
    task_key TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0, due_at DOUBLE PRECISION NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending', lease_owner TEXT, lease_token TEXT,
    lease_until DOUBLE PRECISION, attempts INTEGER NOT NULL DEFAULT 0,
    result_json TEXT, updated_at TEXT NOT NULL)'''


def ensure_schema(conn):
    with conn:
        conn.execute(SCHEMA)
        conn.execute('CREATE INDEX IF NOT EXISTS idx_collection_tasks_due ON collection_tasks(kind,state,due_at,priority)')
        # Workers look up their own lease on every claim poll.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_collection_tasks_leased ON collection_tasks(lease_owner,lease_until) WHERE state='leased'")


def enqueue(conn, task_key, kind, payload=None, *, priority=0, due_at=None):
    """Requeue finished tasks, but never replace pending or in-flight work."""
    at = time.time() if due_at is None else due_at
    conn.execute('''INSERT INTO collection_tasks
        (task_key,kind,payload_json,priority,due_at,state,updated_at)
        VALUES(?,?,?,?,?,'pending',?) ON CONFLICT(task_key) DO UPDATE SET
        kind=excluded.kind,payload_json=excluded.payload_json,priority=excluded.priority,
        due_at=excluded.due_at,state='pending',result_json=NULL,updated_at=excluded.updated_at
        WHERE collection_tasks.state='done' ''',
        (task_key, kind, json.dumps(payload or {}), priority, at, utc_now()))


def begin_completion(conn, task_key=None):
    """Start the caller's write transaction and fence one task, not all lanes.

    PostgreSQL row locks serialize completion with reclaiming that same task.
    Other tasks and the endpoint coordinator remain available during ingestion.
    SQLite retains its database writer lock. The caller commits or rolls back.
    """
    if getattr(conn, 'dialect', None) != 'postgresql':
        conn.execute('BEGIN IMMEDIATE')
        return
    if not conn.in_transaction:
        conn.execute('BEGIN ISOLATION LEVEL READ COMMITTED')
    if task_key is not None:
        conn.execute('SELECT task_key FROM collection_tasks WHERE task_key=? FOR UPDATE',
                     (task_key,)).fetchone()


def claim(conn, worker, kinds, *, lease_seconds=90, now=None):
    """Atomically reclaim expired work or claim due work in selected lanes."""
    if not kinds:
        return None
    now = time.time() if now is None else now
    marks = ','.join('?' for _ in kinds)
    with conn:
        begin_completion(conn)
        # Skip a task being ingested instead of blocking all queue consumers.
        locked = ' FOR UPDATE SKIP LOCKED' if getattr(conn, 'dialect', None) == 'postgresql' else ''
        row = conn.execute('''SELECT * FROM collection_tasks WHERE kind IN (''' + marks + ''')
            AND due_at<=? AND (state='pending' OR (state='leased' AND lease_until<=?))
            ORDER BY due_at,priority DESC,task_key LIMIT 1''' + locked, (*kinds, now, now)).fetchone()
        if row is None:
            return None
        token = uuid.uuid4().hex
        cur = conn.execute('''UPDATE collection_tasks SET state='leased',lease_owner=?,lease_token=?,
            lease_until=?,attempts=attempts+1,updated_at=? WHERE task_key=?
            AND (state='pending' OR (state='leased' AND lease_until<=?))''',
            (worker, token, now + lease_seconds, utc_now(), row['task_key'], now))
        if cur.rowcount != 1:
            return None
        task = dict(row)
        task.update(lease_owner=worker, lease_token=token, lease_until=now+lease_seconds,
                    state='leased', attempts=row['attempts']+1,
                    payload=json.loads(row['payload_json']))
        return task


def owned(conn, task_key, token, *, now=None):
    now = time.time() if now is None else now
    return conn.execute('''SELECT 1 FROM collection_tasks WHERE task_key=?
        AND state='leased' AND lease_token=? AND lease_until>?''', (task_key, token, now)).fetchone() is not None


def finish(conn, task_key, token, result=None, *, next_due=None, now=None):
    """Call inside the same transaction as result ingestion for atomic commit."""
    now = time.time() if now is None else now
    cur = conn.execute('''UPDATE collection_tasks SET state=?,due_at=?,lease_owner=NULL,
        lease_token=NULL,lease_until=NULL,result_json=?,updated_at=? WHERE task_key=?
        AND state='leased' AND lease_token=? AND lease_until>?''',
        ('done' if next_due is None else 'pending', now if next_due is None else next_due,
         json.dumps(result or {}, ensure_ascii=False), utc_now(), task_key, token, now))
    return cur.rowcount == 1


def defer(conn, task_key, token, due_at, reason='deferred', *, now=None):
    return finish(conn, task_key, token, {'status': reason}, next_due=due_at, now=now)


def complete(conn, task_key, token, result=None, *, ingest_callback=None, next_due=None, now=None):
    """Fence stale workers and commit their data together with task completion."""
    with conn:
        begin_completion(conn, task_key)
        now = time.time() if now is None else now
        if not owned(conn, task_key, token, now=now):
            return False
        if ingest_callback is not None:
            ingest_callback(conn)
        return finish(conn, task_key, token, result, next_due=next_due, now=now)


def snapshot(conn, *, now=None):
    now = time.time() if now is None else now
    rows = conn.execute('''SELECT kind,state,COUNT(*) AS count,MIN(due_at) AS next_due,
        SUM(CASE WHEN due_at<=? THEN 1 ELSE 0 END) AS due
        FROM collection_tasks GROUP BY kind,state ORDER BY kind,state''', (now,)).fetchall()
    return [dict(row) for row in rows]
