"""Durable, bounded analysis results with a short freshness window."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import hashlib
import json
import threading

from .analytics import PostFactsCache, analyze, analysis_parameters
from .db import connect, utc_now


# Bump when analysis semantics change so saved results from older code are not reused.
CACHE_VERSION = 3
VIEW_OPTIONS = frozenset(('sections', 'include_exportable', 'focus_post_id',
                          'sample_status', 'sample_stage', 'sample_batch', 'detail_limit',
                          'cohort_limit'))


def _key(params):
    value = json.dumps([CACHE_VERSION, params], sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class AnalysisCache:
    def __init__(self, db, *, analyze_fn=None, max_entries=64, max_bytes=128 * 1024 * 1024,
                 max_age_seconds=60):
        self.db = db
        # Per-post facts survive between requests: a recomputation only reads
        # the snapshots of posts that changed since the previous analysis.
        self.facts = PostFactsCache()
        self.analyze = analyze_fn or (lambda db, params: analyze(db, params, facts_cache=self.facts))
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.max_age_seconds = max_age_seconds
        # Fixed-size stripes avoid accumulating a lock for every visited filter.
        self.locks = [threading.Lock() for _ in range(32)]
        self.write_lock = threading.Lock()

    def _read(self, cache_key):
        with closing(connect(self.db)) as conn:
            row = conn.execute('SELECT created_at,result_json FROM analysis_cache WHERE cache_key=?',
                               (cache_key,)).fetchone()
        if row is None:
            return None
        try:
            result = json.loads(row['result_json'])
            if not isinstance(result, dict):
                return None
        except (ValueError, TypeError):
            return None
        return {**result, 'cache': {'hit': True, 'policy': 'ttl', 'stored_at': row['created_at'],
                                  'max_age_seconds': self.max_age_seconds}}

    def _fresh(self, saved, now):
        if saved is None:
            return False
        try:
            age = (datetime.fromisoformat(now.replace('Z', '+00:00')) -
                   datetime.fromisoformat(saved['cache']['stored_at'].replace('Z', '+00:00'))).total_seconds()
        except (ValueError, TypeError):
            return False
        # A future timestamp (e.g. a corrected system clock) must not pin a result.
        return 0 <= age < self.max_age_seconds

    def get(self, params=None, *, refresh=False):
        params = dict(params or {})
        refresh_value = params.pop('refresh', False)
        if isinstance(refresh_value, str) and refresh_value.lower() in ('true', '1', 'false', '0'):
            refresh_value = refresh_value.lower() in ('true', '1')
        if not isinstance(refresh_value, bool) or not isinstance(refresh, bool):
            raise ValueError('refresh 必须为布尔值')
        refresh = refresh or refresh_value
        # Shared validation keeps invalid inputs from ever aliasing a valid cache key.
        options = analysis_parameters(params)
        cache_key = _key(options)
        scope_key = _key({key: value for key, value in options.items() if key not in VIEW_OPTIONS})
        requested_at = utc_now()
        if not refresh:
            saved = self._read(cache_key)
            if self._fresh(saved, requested_at):
                return saved
        lock = self.locks[int(scope_key[:8], 16) % len(self.locks)]
        with lock:
            saved = self._read(cache_key)
            # A refresh completed after this request arrived satisfies concurrent callers too.
            if self._fresh(saved, utc_now()) and (not refresh or saved['cache']['stored_at'] >= requested_at):
                return saved
            result = self.analyze(self.db, params)
            serialized = json.dumps(result, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            size = len(serialized.encode('utf-8'))
            stored_at = utc_now()
            # Compute first, then replace atomically: failures leave the prior results usable.
            with self.write_lock, closing(connect(self.db)) as conn, conn:
                if refresh:
                    conn.execute('DELETE FROM analysis_cache WHERE scope_key=?', (scope_key,))
                if self.max_entries > 0 and size <= self.max_bytes:
                    conn.execute('''INSERT INTO analysis_cache(cache_key,scope_key,created_at,result_json,result_bytes)
                        VALUES(?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET
                        scope_key=excluded.scope_key,created_at=excluded.created_at,
                        result_json=excluded.result_json,result_bytes=excluded.result_bytes''',
                        (cache_key, scope_key, stored_at, serialized, size))
                    rows = conn.execute('SELECT cache_key,result_bytes FROM analysis_cache ORDER BY created_at DESC,cache_key').fetchall()
                    total = 0
                    for index, row in enumerate(rows):
                        total += row['result_bytes']
                        if index >= self.max_entries or total > self.max_bytes:
                            conn.execute('DELETE FROM analysis_cache WHERE cache_key=?', (row['cache_key'],))
            return {**result, 'cache': {'hit': False, 'policy': 'ttl', 'stored_at': stored_at,
                                      'max_age_seconds': self.max_age_seconds}}
