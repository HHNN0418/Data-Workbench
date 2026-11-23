"""Bounded housekeeping for disposable history, never canonical business data.

Raw provenance referenced by a business row, official CSV evidence, Agent
confirmations, browser receipts, research, publication/tracking receipts and
collection cursors have no automatic expiry. Only old diagnostic API logs,
orphan raw payloads, expired worker/object-lease checkpoints and finished queue
rows are disposable. Lease records and finished tasks are only valid for
minutes, so they use short horizons instead of the diagnostic retention days.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import logging
import threading
import time

from .db import begin_write, connect

LOGGER = logging.getLogger(__name__)
DEFAULT_DAYS = 30
# Worker leases last 120 s and object leases 30 s; a day covers any replay.
LEASE_RECORD_HOURS = 24
# Nothing reads a finished task; enqueue() recreates the row when needed.
FINISHED_TASK_HOURS = 1
RAW_REFERENCES = ('accounts', 'posts', 'post_versions', 'counter_snapshots',
                  'news_articles', 'counter_conflicts', 'activity_import_payloads')


def prune_history(db_path, *, dry_run=True, retention_days=DEFAULT_DAYS, batch_size=1000, now=None):
    """Inspect/delete at most one bounded batch per table; never initialize DB.

    Counts are limited to this batch, not an expensive whole-history count.
    The caller can repeat if a result reaches batch_size. A dry run is read-only.
    """
    if type(dry_run) is not bool:
        raise ValueError('dry_run must be a boolean')
    if type(retention_days) is not int or not 7 <= retention_days <= 3650:
        raise ValueError('retention_days must be between 7 and 3650')
    if type(batch_size) is not int or not 1 <= batch_size <= 2000:
        raise ValueError('batch_size must be between 1 and 2000')
    at = time.time() if now is None else float(now)
    def before(delta):
        return (datetime.fromtimestamp(at, timezone.utc) - delta
                ).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    cutoff = before(timedelta(days=retention_days))
    lease_cutoff = before(timedelta(hours=LEASE_RECORD_HOURS))
    task_cutoff = before(timedelta(hours=FINISHED_TASK_HOURS))
    raw_where = ("ctn_timestamp(r.observed_at)<ctn_timestamp(?) "
                 "AND r.source NOT LIKE 'official_activity%' AND r.source NOT LIKE 'agent:%' "
                 + ''.join(f'AND NOT EXISTS(SELECT 1 FROM {table} ref WHERE ref.raw_id=r.id) '
                           for table in RAW_REFERENCES))
    # Worker results are acknowledgements for expired immutable lease tokens.
    # Browser receipts are intentionally retained because clients can replay
    # their stable IDs after a long offline period.
    checkpoint_where = ("ctn_timestamp(updated_at)<ctn_timestamp(?) AND ("
        "key LIKE 'worker_result:%' OR "
        "(key LIKE 'coordination:post:%' "
        "AND CASE WHEN json_valid(value) THEN json_extract(value,'$.until') END<?))")
    selections = (
        ('api_call_logs', 'id', 'SELECT id FROM api_call_logs WHERE occurred_at<? '
         'AND ctn_timestamp(occurred_at)<ctn_timestamp(?) ORDER BY occurred_at,id LIMIT ?',
         (cutoff, cutoff, batch_size)),
        ('checkpoints', 'key', 'SELECT key FROM checkpoints WHERE ' + checkpoint_where +
         ' ORDER BY ctn_timestamp(updated_at),key LIMIT ?', (lease_cutoff, at, batch_size)),
        ('raw_records', 'id', 'SELECT r.id FROM raw_records r WHERE ' + raw_where +
         'ORDER BY ctn_timestamp(r.observed_at),r.id LIMIT ?', (cutoff, batch_size)),
        # Every queue writer stamps updated_at with utc_now(), so text order is time order.
        ('collection_tasks', 'task_key', "SELECT task_key FROM collection_tasks WHERE state='done' "
         'AND updated_at<? ORDER BY updated_at,task_key LIMIT ?', (task_cutoff, batch_size)),
    )
    result = {'dry_run': dry_run, 'retention_days': retention_days, 'cutoff': cutoff,
              'batch_size': batch_size, 'counts': {}}
    # A separate transaction for each table bounds lock duration. Raw references
    # are indexed; rows currently reused by ingestion hold KEY SHARE locks and
    # are skipped by PostgreSQL cleanup.
    with closing(connect(db_path)) as conn:
        for table, key, sql, params in selections:
            with conn:
                if dry_run:
                    conn.execute('BEGIN')
                else:
                    begin_write(conn, 'retention:' + table)
                if not dry_run and getattr(conn, 'dialect', None) == 'postgresql':
                    sql += ' FOR UPDATE SKIP LOCKED'
                candidates = [row[0] for row in conn.execute(sql, params)]
                if not dry_run:
                    for offset in range(0, len(candidates), 400):
                        batch = candidates[offset:offset + 400]
                        conn.execute(f'DELETE FROM {table} WHERE {key} IN (' +
                                     ','.join('?' for _ in batch) + ')', batch)
                result['counts'][table] = len(candidates)
    return result


class RetentionWorker:
    """Lifecycle companion; one bounded pass every five minutes."""
    def __init__(self, db_path, *, interval_seconds=300, retention_days=DEFAULT_DAYS):
        self.db_path = db_path
        self.interval_seconds = max(30, interval_seconds)
        self.retention_days = retention_days
        self._stop = threading.Event()
        self._thread = None
        self.last_result = None

    def run_once(self):
        self.last_result = prune_history(self.db_path, dry_run=False,
                                         retention_days=self.retention_days)
        return self.last_result

    def _run(self):
        # Allow startup/migration and collectors to establish their state first.
        while not self._stop.wait(self.interval_seconds):
            try:
                self.run_once()
            except Exception as exc:
                LOGGER.error('History retention failed (%s); retrying on the next maintenance pass',
                             type(exc).__name__)

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='ctn-retention', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
