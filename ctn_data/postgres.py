"""PostgreSQL storage with a deliberately small SQLite DB-API compatibility layer.

Application IDs, timestamps and decimal strings retain their original meaning.
Connections are thread-owned; reads outside an explicit transaction autocommit,
while writes commit through the existing ``with connection`` convention.
"""
from __future__ import annotations

import re
import functools
import hashlib
import sqlite3
import threading
import uuid
from contextlib import contextmanager

IDENTITY_TABLES = frozenset(('raw_records', 'post_versions', 'counter_snapshots',
    'feed_observations', 'api_call_logs', 'collection_runs', 'activity_imports',
    'official_activity', 'score_snapshots', 'analysis_runs', 'post_labels', 'counter_conflicts', 'leaderboard_snapshots'))
COORDINATION_LOCK = 485187108
_INITIALIZED = set()
_INITIALIZE_LOCK = threading.Lock()


class Row:
    def __init__(self, names, values):
        self._names, self._values = tuple(names), tuple(values)
        self._mapping = dict(zip(self._names, self._values))

    def __getitem__(self, key):
        return self._values[key] if isinstance(key, (int, slice)) else self._mapping[key]

    def __iter__(self):
        return iter(self._values)

    def keys(self):
        return self._mapping.keys()

    def __len__(self):
        return len(self._mapping)


def _row_factory(cursor):
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Row(names, values)


def sqlite_errors(method):
    """Keep existing validation/HTTP error handlers database independent."""
    @functools.wraps(method)
    def wrapped(*args, **kwargs):
        import psycopg
        try:
            return method(*args, **kwargs)
        except psycopg.Error as exc:
            kind = sqlite3.IntegrityError if isinstance(exc, psycopg.IntegrityError) else sqlite3.OperationalError
            raise kind(str(exc)) from exc
    return wrapped


def translate_sql(sql: str, parameterized: bool = True) -> str:
    """Translate only the SQLite syntax used by this application.

    Quoted strings/identifiers and SQL comments are kept intact when replacing
    qmark placeholders. JSON numeric extraction is explicit to avoid changing
    strings such as numeric-looking status labels.
    """
    sql = sql.strip().rstrip(';')
    ignored = bool(re.match(r'INSERT\s+OR\s+IGNORE\b', sql, re.I))
    if ignored:
        sql = re.sub(r'^INSERT\s+OR\s+IGNORE\b', 'INSERT', sql, flags=re.I)
        sql += ' ON CONFLICT DO NOTHING'
    sql = re.sub(r"\bjson_extract\(([^,()]+),\s*'\$\.(http_status|retry_after_epoch|until)'\)",
                 r"ctn_json_number(\1,'$.\2')", sql, flags=re.I)
    sql = re.sub(r"\b([\w.]+)\s+GLOB\s+'\*Z'", r"\1 LIKE '%Z'", sql, flags=re.I)
    sql = re.sub(r'\bSUBSTR\(([^,()]+),\s*(-\d+)\s*,\s*(\d+)\s*\)',
                 r'ctn_substr(\1,\2,\3)', sql, flags=re.I)
    if not parameterized:
        return sql
    # Psycopg's placeholder parser interprets % even inside SQL literals.
    sql = sql.replace('%', '%%')
    tokens = re.split(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|--[^\n]*|/\*[\s\S]*?\*/)", sql)
    return ''.join(token if index % 2 else token.replace('?', '%s') for index, token in enumerate(tokens))


class Cursor:
    def __init__(self, cursor, lastrowid=None, buffered=None):
        self._cursor, self.lastrowid = cursor, lastrowid
        self._buffered = list(buffered or [])

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description

    def fetchone(self):
        return self._buffered.pop(0) if self._buffered else self._cursor.fetchone()

    def fetchmany(self, size=1):
        result = []
        while len(result) < size:
            row = self.fetchone()
            if row is None:
                break
            result.append(row)
        return result

    def fetchall(self):
        result, self._buffered = self._buffered, []
        return result + self._cursor.fetchall()

    def __iter__(self):
        while (row := self.fetchone()) is not None:
            yield row

    def close(self):
        self._cursor.close()


def _open(dsn):
    import psycopg
    # JIT compiles the per-row plpgsql helper calls of each large scheduling
    # query from scratch; measured, it only adds latency to this workload.
    # The server default work_mem (4MB) spilled routine sorts to temp files.
    return psycopg.connect(dsn, autocommit=True, row_factory=_row_factory,
        connect_timeout=10, options='-c timezone=UTC -c statement_timeout=120000 -c lock_timeout=15000 -c jit=off -c work_mem=16MB')


# Every request, queue poll and counter task used to open a new session (TCP,
# SCRAM, a forked backend and recompiled plpgsql). Closed connections return
# here instead. Return rolls back and releases session-level advisory locks;
# set_config() calls are transaction-scoped.
POOL_MAX_IDLE = 8
POOL_IDLE_SECONDS = 300
POOL_CHECK_AFTER_SECONDS = 10
_POOL = {}
_POOL_LOCK = threading.Lock()


def _checkout(dsn):
    import time
    while True:
        with _POOL_LOCK:
            idle = _POOL.get(dsn)
            raw, returned = idle.pop() if idle else (None, 0)
        if raw is None:
            return _open(dsn)
        age = time.monotonic() - returned
        if raw.closed or age > POOL_IDLE_SECONDS:
            raw.close()
            continue
        if age > POOL_CHECK_AFTER_SECONDS:
            # The server may have restarted or dropped an idle session.
            try:
                raw.execute('SELECT 1')
            except Exception:
                raw.close()
                continue
        return raw


def _release(dsn, raw):
    import time
    from psycopg.pq import TransactionStatus
    try:
        if not raw.closed and raw.info.transaction_status != TransactionStatus.IDLE:
            raw.rollback()
        # Session-level advisory locks (content research's owner lock) rely on
        # close() ending the session; release them before reuse.
        if not raw.closed:
            raw.execute('SELECT pg_advisory_unlock_all()')
        reusable = not raw.closed and not raw.broken and raw.info.transaction_status == TransactionStatus.IDLE
    except Exception:
        reusable = False
    if reusable:
        with _POOL_LOCK:
            idle = _POOL.setdefault(dsn, [])
            if len(idle) < POOL_MAX_IDLE:
                idle.append((raw, time.monotonic()))
                return
    raw.close()


class _Released:
    closed = True

    def __getattr__(self, name):
        import psycopg
        raise psycopg.InterfaceError('the connection is closed')


class PostgresConnection:
    dialect = 'postgresql'

    def __init__(self, dsn: str, *, pooled=False):
        self._dsn, self._pooled = dsn, pooled
        self.raw = _checkout(dsn) if pooled else _open(dsn)
        self.total_changes = 0

    @property
    def in_transaction(self):
        from psycopg.pq import TransactionStatus
        return self.raw.info.transaction_status != TransactionStatus.IDLE

    @sqlite_errors
    def execute(self, sql, parameters=None):
        command = sql.strip().rstrip(';').upper()
        if command in ('BEGIN', 'BEGIN TRANSACTION', 'BEGIN IMMEDIATE'):
            if not self.in_transaction:
                self.raw.execute('BEGIN ISOLATION LEVEL ' + ('READ COMMITTED' if command == 'BEGIN IMMEDIATE' else 'REPEATABLE READ'))
            if command == 'BEGIN IMMEDIATE':
                return Cursor(self.raw.execute('SELECT pg_advisory_xact_lock(%s)', (COORDINATION_LOCK,)))
            return Cursor(self.raw.cursor())
        statement = translate_sql(sql, parameters is not None)
        writes = bool(re.match(r'\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|SAVEPOINT)\b', statement, re.I))
        if writes and not self.in_transaction:
            self.raw.execute('BEGIN')
        inserted = re.match(r'INSERT\s+INTO\s+(\w+)\b', statement, re.I)
        add_returning = bool(inserted and inserted.group(1).lower() in IDENTITY_TABLES and not re.search(r'\bRETURNING\b', statement, re.I))
        if add_returning:
            statement += ' RETURNING id'
        cursor = self.raw.execute(statement, parameters)
        if writes:
            self.total_changes += max(0, cursor.rowcount)
        if add_returning:
            row = cursor.fetchone()
            return Cursor(cursor, row[0] if row else None, [row] if row else [])
        return Cursor(cursor)

    @sqlite_errors
    def executemany(self, sql, rows):
        if not self.in_transaction:
            self.raw.execute('BEGIN')
        cursor = self.raw.cursor()
        cursor.executemany(translate_sql(sql), rows)
        self.total_changes += max(0, cursor.rowcount)
        return Cursor(cursor)

    @contextmanager
    def stream(self, sql, parameters=None, *, batch_size=1000):
        """Fetch bounded batches using a server cursor, preserving caller transactions."""
        import psycopg
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
            raise ValueError('batch_size must be a positive integer')
        owned = not self.in_transaction
        cursor = None
        try:
            if owned:
                self.raw.execute('BEGIN READ ONLY')
            cursor = self.raw.cursor(name='ctn_stream_' + uuid.uuid4().hex)
            cursor.itersize = batch_size
            cursor.execute(translate_sql(sql, parameters is not None), parameters)
            yield cursor
        except psycopg.Error as exc:
            kind = sqlite3.IntegrityError if isinstance(exc, psycopg.IntegrityError) else sqlite3.OperationalError
            raise kind(str(exc)) from exc
        finally:
            try:
                if cursor is not None:
                    cursor.close()
            finally:
                # Read-only owned transactions never need to persist changes.
                if owned:
                    self.raw.rollback()

    @sqlite_errors
    def executescript(self, sql):
        if not self.in_transaction:
            self.raw.execute('BEGIN')
        self.raw.execute(sql, prepare=False)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        raw = self.raw
        if isinstance(raw, _Released):
            return
        # A pooled session now belongs to the next caller; later use of this
        # wrapper must fail rather than act on someone else's connection.
        self.raw = _Released()
        if self._pooled:
            _release(self._dsn, raw)
        else:
            raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.rollback() if exc_type else self.commit()


COMPATIBILITY_FUNCTIONS = r"""
CREATE OR REPLACE FUNCTION ctn_timestamp_parse(value TEXT) RETURNS TEXT
LANGUAGE plpgsql IMMUTABLE SET timezone='UTC' SET datestyle='ISO, DMY' AS $$
DECLARE stamp TIMESTAMPTZ;
BEGIN
  value := btrim(value);
  IF value IS NULL OR NOT (
    value ~ '^\d{4}-\d{2}-\d{2}([Tt ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([Zz]|[+-]\d{2}:?\d{2}| UTC)?)?$'
    OR value ~ '^\d{1,2}/\d{1,2}/\d{4} \d{2}:\d{2}:\d{2}$'
    OR value ~* '^(\w{3},? )?\d{1,2} \w{3} \d{4} \d{2}:\d{2}(:\d{2})?( [A-Z]+| [+-]\d{4})?$'
    OR value ~* '^\w{3} \d{1,2}, \d{4}, \d{1,2}:\d{2} [AP]M$'
  ) THEN RETURN NULL; END IF;
  stamp := regexp_replace(value, ',', '', 'g')::timestamptz;
  RETURN to_char(stamp AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"');
EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN RETURN NULL;
END $$;
-- Collected UTC stamps ('YYYY-MM-DDTHH:MI:SS.f{1,6}Z') parse to themselves
-- with the fraction padded to six digits. Build that key directly when the
-- date and time are certainly valid (Feb 29 and other edge dates still parse);
-- the parser's SET clauses and EXCEPTION block cost ~14 us per call. Verified
-- identical on every stored timestamp on 2026-10-21. As an inlinable SQL
-- function, expression indexes and queries are simplified identically, so
-- index matching is unchanged. Index maintenance runs with
-- search_path=pg_catalog (PostgreSQL 17), hence the schema-qualified parser.
CREATE OR REPLACE FUNCTION ctn_timestamp(value TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN length(value) BETWEEN 22 AND 27
      AND value LIKE '____-__-__T__:__:__._%Z'
      AND translate(substr(value,1,19),'0123456789','') = '--T::'
      AND translate(substr(value,21,length(value)-21),'0123456789','') = ''
      AND substr(value,1,4) <> '0000' AND substr(value,6,2) BETWEEN '01' AND '12'
      AND substr(value,12,2) <= '23' AND substr(value,15,2) <= '59' AND substr(value,18,2) <= '59'
      AND (substr(value,9,2) BETWEEN '01' AND '28'
           OR (substr(value,9,2) BETWEEN '29' AND '30' AND substr(value,6,2) <> '02')
           OR (substr(value,9,2) = '31' AND substr(value,6,2) IN ('01','03','05','07','08','10','12')))
    THEN substr(value,1,20) || rpad(substr(value,21,length(value)-21),6,'0') || 'Z'
    ELSE public.ctn_timestamp_parse(value) END
$$;
-- The helpers below run per row in scheduling queries. An EXCEPTION block
-- starts a subtransaction on every call, so input is validated with
-- pg_input_is_valid() (PostgreSQL 16+) before converting instead.
CREATE OR REPLACE FUNCTION julianday(value TEXT, modifier TEXT DEFAULT NULL)
RETURNS DOUBLE PRECISION LANGUAGE plpgsql STABLE AS $$
DECLARE stamp TIMESTAMPTZ;
BEGIN
  IF value IS NULL THEN RETURN NULL; END IF;
  IF value='now' THEN stamp := statement_timestamp();
  ELSIF pg_input_is_valid(value,'timestamptz') THEN stamp := value::timestamptz;
  ELSE RETURN NULL; END IF;
  IF modifier IS NOT NULL THEN
    IF NOT pg_input_is_valid(modifier,'interval') THEN RETURN NULL; END IF;
    stamp := stamp + modifier::interval;
  END IF;
  RETURN EXTRACT(EPOCH FROM stamp)::double precision / 86400.0 + 2440587.5;
END $$;
CREATE OR REPLACE FUNCTION json_valid(value TEXT) RETURNS BOOLEAN
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
  RETURN value IS NOT NULL AND pg_input_is_valid(value,'jsonb');
END $$;
CREATE OR REPLACE FUNCTION json_extract(value TEXT, path TEXT) RETURNS TEXT
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
  IF value IS NULL OR NOT pg_input_is_valid(value,'jsonb') THEN RETURN NULL; END IF;
  RETURN value::jsonb #>> string_to_array(substr(path,3),'.');
END $$;
CREATE OR REPLACE FUNCTION ctn_json_number(value TEXT, path TEXT) RETURNS DOUBLE PRECISION
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE item TEXT := json_extract(value,path);
BEGIN
  IF item IS NULL OR NOT pg_input_is_valid(item,'double precision') THEN RETURN NULL; END IF;
  RETURN item::double precision;
END $$;
CREATE OR REPLACE FUNCTION ctn_substr(value TEXT, start_at INTEGER, take INTEGER) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$
  SELECT substr(value, CASE WHEN start_at<0 THEN length(value)+start_at+1 ELSE start_at END, take)
$$;
"""


# Counter sampling read model: one row per post, maintained by triggers on
# every writer (collector, proxy workers, browser capture, direct SQL), so the
# planner reads the next N posts by index instead of probing every post.
# Rows are derived; backups and migrations never copy this table.
POSTGRES_DERIVED = r"""
CREATE TABLE IF NOT EXISTS counter_sampling(
  post_id BIGINT PRIMARY KEY, first_seen TEXT, sampled_at TEXT,
  has_sample BIGINT NOT NULL DEFAULT 0, attempted_at TEXT,
  attempt_failed BIGINT NOT NULL DEFAULT 0, attempt_http BIGINT,
  browser_sampled_at TEXT, last_attempt TEXT);
-- reply_count is the latest valid snapshot's value (relations picks threads).
-- replies_synced=0 marks rows created before the column existed.
ALTER TABLE counter_sampling ADD COLUMN IF NOT EXISTS reply_count BIGINT;
ALTER TABLE counter_sampling ADD COLUMN IF NOT EXISTS replies_synced BIGINT NOT NULL DEFAULT 0;
-- account_annotations (in SCHEMA) gained this column after it was created live.
ALTER TABLE account_annotations ADD COLUMN IF NOT EXISTS bot_metrics_json TEXT NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS idx_counter_sampling_queue
  ON counter_sampling(has_sample,last_attempt,post_id DESC);
CREATE OR REPLACE FUNCTION ctn_sampling_seed(target BIGINT) RETURNS void LANGUAGE sql AS $$
  INSERT INTO counter_sampling(post_id,first_seen,last_attempt,replies_synced)
  SELECT post_id,ctn_timestamp(first_seen),ctn_timestamp(first_seen),1 FROM posts WHERE post_id=target
  ON CONFLICT(post_id) DO NOTHING
$$;
CREATE OR REPLACE FUNCTION ctn_sampling_order(target BIGINT) RETURNS void LANGUAGE sql AS $$
  UPDATE counter_sampling SET last_attempt=CASE
      WHEN COALESCE(attempted_at,first_seen)>COALESCE(sampled_at,first_seen) THEN COALESCE(attempted_at,first_seen)
      ELSE COALESCE(sampled_at,first_seen) END
  WHERE post_id=target
$$;
CREATE OR REPLACE FUNCTION ctn_sampling_post() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM ctn_sampling_seed(NEW.post_id);
  RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION ctn_sampling_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  stamp TEXT := ctn_timestamp(NEW.observed_at);
  browser BOOLEAN := EXISTS(SELECT 1 FROM raw_records WHERE id=NEW.raw_id AND source='browser:counters');
BEGIN
  PERFORM ctn_sampling_seed(NEW.post_id);
  UPDATE counter_sampling SET has_sample=1,
    reply_count=CASE WHEN stamp IS NOT NULL AND (sampled_at IS NULL OR stamp>=sampled_at)
                     THEN NEW.reply_count ELSE reply_count END,
    sampled_at=CASE WHEN stamp IS NULL OR sampled_at>=stamp THEN sampled_at ELSE stamp END,
    browser_sampled_at=CASE WHEN NOT browser OR stamp IS NULL OR browser_sampled_at>=stamp
                            THEN browser_sampled_at ELSE stamp END
  WHERE post_id=NEW.post_id;
  PERFORM ctn_sampling_order(NEW.post_id);
  RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION ctn_sampling_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  target BIGINT := substr(NEW.key,17)::BIGINT;
  http DOUBLE PRECISION := CASE WHEN json_valid(NEW.value) THEN ctn_json_number(NEW.value,'$.http_status') END;
BEGIN
  PERFORM ctn_sampling_seed(target);
  UPDATE counter_sampling SET attempted_at=ctn_timestamp(NEW.updated_at),
    attempt_failed=CASE WHEN (CASE WHEN json_valid(NEW.value) THEN json_extract(NEW.value,'$.status') END)='failed'
                        THEN 1 ELSE 0 END,
    attempt_http=CASE WHEN http BETWEEN -1000000 AND 1000000 THEN CAST(http AS BIGINT) END
  WHERE post_id=target;
  PERFORM ctn_sampling_order(target);
  RETURN NULL;
END $$;
CREATE OR REPLACE TRIGGER trg_counter_sampling_post AFTER INSERT ON posts
  FOR EACH ROW EXECUTE FUNCTION ctn_sampling_post();
CREATE OR REPLACE TRIGGER trg_counter_sampling_snapshot AFTER INSERT ON counter_snapshots
  FOR EACH ROW EXECUTE FUNCTION ctn_sampling_snapshot();
CREATE OR REPLACE TRIGGER trg_counter_sampling_attempt AFTER INSERT OR UPDATE ON checkpoints
  FOR EACH ROW WHEN (NEW.key ~ '^counter_attempt:[1-9][0-9]{0,17}$') EXECUTE FUNCTION ctn_sampling_attempt();
UPDATE counter_sampling s SET reply_count=l.reply_count,replies_synced=1
FROM (SELECT DISTINCT ON (post_id) post_id,reply_count FROM counter_snapshots
      WHERE ctn_timestamp(observed_at) IS NOT NULL
      ORDER BY post_id,ctn_timestamp(observed_at) DESC,id DESC) l
WHERE s.replies_synced=0 AND l.post_id=s.post_id;
UPDATE counter_sampling SET replies_synced=1 WHERE replies_synced=0;
INSERT INTO counter_sampling(post_id,first_seen,sampled_at,has_sample,attempted_at,attempt_failed,attempt_http,
                             browser_sampled_at,reply_count,replies_synced)
SELECT p.post_id,ctn_timestamp(p.first_seen),
  (SELECT max(ctn_timestamp(c.observed_at)) FROM counter_snapshots c WHERE c.post_id=p.post_id),
  CASE WHEN EXISTS(SELECT 1 FROM counter_snapshots c WHERE c.post_id=p.post_id) THEN 1 ELSE 0 END,
  ctn_timestamp(q.updated_at),
  CASE WHEN (CASE WHEN json_valid(q.value) THEN json_extract(q.value,'$.status') END)='failed' THEN 1 ELSE 0 END,
  (SELECT CASE WHEN h.n BETWEEN -1000000 AND 1000000 THEN CAST(h.n AS BIGINT) END
   FROM (SELECT CASE WHEN json_valid(q.value) THEN ctn_json_number(q.value,'$.http_status') END AS n) h),
  (SELECT max(ctn_timestamp(c.observed_at)) FROM counter_snapshots c JOIN raw_records r ON r.id=c.raw_id
   WHERE c.post_id=p.post_id AND r.source='browser:counters'),
  (SELECT c.reply_count FROM counter_snapshots c WHERE c.post_id=p.post_id AND ctn_timestamp(c.observed_at) IS NOT NULL
   ORDER BY ctn_timestamp(c.observed_at) DESC,c.id DESC LIMIT 1),1
FROM posts p LEFT JOIN checkpoints q ON q.key='counter_attempt:' || p.post_id
WHERE NOT EXISTS(SELECT 1 FROM counter_sampling s WHERE s.post_id=p.post_id);
UPDATE counter_sampling SET last_attempt=CASE
    WHEN COALESCE(attempted_at,first_seen)>COALESCE(sampled_at,first_seen) THEN COALESCE(attempted_at,first_seen)
    ELSE COALESCE(sampled_at,first_seen) END
WHERE last_attempt IS DISTINCT FROM CASE
    WHEN COALESCE(attempted_at,first_seen)>COALESCE(sampled_at,first_seen) THEN COALESCE(attempted_at,first_seen)
    ELSE COALESCE(sampled_at,first_seen) END;
"""


def postgres_schema(sql):
    sql = re.sub(r'\bINTEGER\b', 'BIGINT', sql)
    sql = re.sub(r'\bREAL\b', 'DOUBLE PRECISION', sql)
    sql = re.sub(r'\bid BIGINT PRIMARY KEY\b', 'id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY', sql)
    # SQLite's descending index puts NULL last; PostgreSQL defaults to first.
    # Match the dashboard's exact ordering so pagination can stop at the limit.
    sql = sql.replace('posts(ctn_timestamp(created_at) DESC,post_id DESC)',
                      'posts(ctn_timestamp(created_at) DESC NULLS LAST,post_id DESC)')
    return sql


SCHEMA_FINGERPRINT_KEY = 'schema_fingerprint'


def schema_fingerprint(schema, version):
    return hashlib.sha256((str(version) + '\n' + schema + COMPATIBILITY_FUNCTIONS + POSTGRES_DERIVED).encode()).hexdigest()


def initialize_postgres(dsn, schema, version, *, force=False):
    """Apply DDL only when the database is not already at this exact schema.

    Every process first reads the recorded version and fingerprint without
    locks. CREATE INDEX IF NOT EXISTS still takes a table lock, so repeating
    the DDL from each new process blocked writers and could deadlock with
    them. ``force=True`` explicitly repairs/recreates a reset database. Failed
    DDL is never cached. Hashing the DSN avoids storing credentials in keys.
    """
    key = hashlib.sha256((dsn + '\n' + str(version) + '\n' + schema + COMPATIBILITY_FUNCTIONS + POSTGRES_DERIVED).encode()).digest()
    with _INITIALIZE_LOCK:
        if not force and key in _INITIALIZED:
            return
        fingerprint = schema_fingerprint(schema, version)
        if force or not _schema_current(dsn, version, fingerprint):
            _initialize_postgres(dsn, schema, version, fingerprint)
        _INITIALIZED.add(key)


def _schema_current(dsn, version, fingerprint):
    import psycopg
    try:
        conn = PostgresConnection(dsn)
    except psycopg.Error:
        return False
    try:
        if not conn.raw.execute("SELECT to_regclass('_ctn_schema') IS NOT NULL AND to_regclass('meta') IS NOT NULL").fetchone()[0]:
            return False
        current = conn.raw.execute('SELECT version FROM _ctn_schema').fetchall()
        saved = conn.raw.execute('SELECT value FROM meta WHERE key=%s', (SCHEMA_FINGERPRINT_KEY,)).fetchone()
        return len(current) == 1 and current[0][0] == version and saved is not None and saved[0] == fingerprint
    except psycopg.Error:
        return False
    finally:
        conn.close()


def _initialize_postgres(dsn, schema, version, fingerprint):
    from .db import utc_now
    conn = PostgresConnection(dsn)
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('CREATE TABLE IF NOT EXISTS _ctn_schema(version BIGINT NOT NULL)')
            current = conn.execute('SELECT version FROM _ctn_schema').fetchone()
            if current and current[0] > version:
                raise RuntimeError(f'Database schema {current[0]} is newer than this application')
            conn.executescript(COMPATIBILITY_FUNCTIONS)
            conn.executescript(postgres_schema(schema))
            conn.executescript(POSTGRES_DERIVED)
            conn.execute('DELETE FROM _ctn_schema')
            conn.execute('INSERT INTO _ctn_schema(version) VALUES(?)', (version,))
            conn.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('created_at', utc_now()))
            conn.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('mode', 'live'))
            conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (SCHEMA_FINGERPRINT_KEY, fingerprint))
    finally:
        conn.close()
