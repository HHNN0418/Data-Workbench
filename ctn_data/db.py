"""SQLite schema, provenance and consistent online backups."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 8


def timestamp_key(value):
    """An exact, indexable UTC sort key for old and new ISO timestamp spellings."""
    if not isinstance(value, str) or not value:
        return None
    try:
        try:
            stamp = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except ValueError:
            from .imports import normalize_timestamp
            stamp = datetime.fromisoformat(normalize_timestamp(value).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    except (ValueError, TypeError, OverflowError):
        return None


def configure_sqlite(connection):
    # Register on backup/restore connections too: expression indexes use this
    # deterministic function without rewriting historical observation values.
    connection.create_function('ctn_timestamp', 1, timestamp_key, deterministic=True)
    return connection


def portable_indexes(connection):
    """Portable backups must remain writable in ordinary SQLite tools.

    These derived indexes are recreated by initialize() when CTN opens a
    restored database. Removing them from a backup never changes its rows.
    """
    for name in ('idx_counters_post_utc', 'idx_counters_utc', 'idx_feed_post_utc', 'idx_feed_source_utc',
                 'idx_posts_created_utc', 'idx_posts_freshness_utc', 'idx_checkpoints_updated_utc',
                 'idx_checkpoints_retention_utc', 'idx_raw_observed_utc', 'idx_llm_calls_time', 'idx_llm_calls_purpose_time',
                 'idx_post_annotations_time', 'idx_post_likes_time', 'idx_account_followers_time'):
        connection.execute('DROP INDEX IF EXISTS ' + name)
    for name in SAMPLING_TRIGGERS:
        connection.execute('DROP TRIGGER IF EXISTS ' + name)


@contextmanager
def iter_rows(connection, sql, parameters=None, *, batch_size=1000):
    """Close streaming cursors on exhaustion, early exit, and exceptions."""
    if getattr(connection, 'dialect', None) == 'postgresql':
        with connection.stream(sql, parameters, batch_size=batch_size) as cursor:
            yield cursor
    else:
        cursor = connection.execute(sql, parameters) if parameters is not None else connection.execute(sql)
        try:
            yield cursor
        finally:
            cursor.close()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def is_postgres(path: str | Path) -> bool:
    return str(path).startswith(('postgresql://', 'postgres://'))


def resolve_database_target(path: str | Path) -> str:
    """Opt-in startup configuration; connect() never redirects explicit tests."""
    return os.environ.get('CTN_DATABASE_URL') or str(path)


def connect(path: str | Path):
    if is_postgres(path):
        from .postgres import PostgresConnection
        return PostgresConnection(str(path), pooled=True)
    path = str(path)
    if path != ':memory:':
        Path(path).resolve().parent.mkdir(parents=True, exist_ok=True)
    connection = configure_sqlite(sqlite3.connect(path, timeout=15))
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA busy_timeout=15000')
    return connection


def connection_lost(connection) -> bool:
    """True once a PostgreSQL session has died (server restart, network loss).

    Pooled connections are probed at checkout, but a loop that keeps one
    connection would fail every later statement forever. SQLite never dies.
    """
    raw = getattr(connection, 'raw', None)
    return raw is not None and bool(getattr(raw, 'closed', False) or getattr(raw, 'broken', False))


def reconnect_if_lost(connection, path):
    """For long-lived loops after a failure: replace a dead connection, else keep it.

    Never raises. While the database is still down the closed connection is
    returned, so the caller's next statement fails and retries this.
    """
    if not connection_lost(connection):
        return connection
    try:
        connection.close()
    except Exception:
        pass
    # Idle pooled sessions died with the same server, and checkout only
    # probes those idle for over 10 s; skip each dead one (pool holds 8).
    for _ in range(10):
        try:
            fresh = connect(path)
        except Exception:
            return connection
        try:
            fresh.execute('SELECT 1').fetchone()
            return fresh
        except Exception:
            try:
                fresh.close()
            except Exception:
                pass
    return connection


def begin_write(connection, scope: str):
    """Serialize one read/modify/write domain, without blocking other domains.

    Call before reading protected state. All writers of the same logical state
    must use the same scope, even if that state spans several database tables.
    PostgreSQL holds the advisory lock until commit/rollback; SQLite continues
    to use its database writer lock. The caller owns the transaction.
    """
    if not isinstance(scope, str) or not scope:
        raise ValueError('write transaction scope is required')
    if getattr(connection, 'dialect', None) != 'postgresql':
        if not getattr(connection, 'in_transaction', False):
            connection.execute('BEGIN IMMEDIATE')
        return
    if not connection.in_transaction:
        connection.execute('BEGIN ISOLATION LEVEL READ COMMITTED')
    lock_id = int.from_bytes(hashlib.sha256(('ctn:write:' + scope).encode()).digest()[:8], signed=True)
    connection.execute('SELECT pg_advisory_xact_lock(?)', (lock_id,))


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS raw_records(
 id INTEGER PRIMARY KEY,sha256 TEXT NOT NULL UNIQUE,source TEXT NOT NULL,
 endpoint TEXT NOT NULL,observed_at TEXT NOT NULL,payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts(
 account_id INTEGER PRIMARY KEY,username TEXT,display_name TEXT,verified INTEGER,
 private INTEGER,created_at TEXT,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,
 raw_id INTEGER REFERENCES raw_records(id));
CREATE TABLE IF NOT EXISTS team_accounts(
 account_id INTEGER PRIMARY KEY,team_label TEXT NOT NULL,is_own INTEGER NOT NULL DEFAULT 1,
 evidence TEXT NOT NULL,updated_at TEXT NOT NULL,CHECK(is_own IN (0,1)));
CREATE TABLE IF NOT EXISTS posts(
 post_id INTEGER PRIMARY KEY,author_id INTEGER,kind TEXT,body TEXT,
 reply_to_post_id INTEGER,root_post_id INTEGER,repost_of_post_id INTEGER,
 visibility TEXT,embed_json TEXT,created_at TEXT,edited_at TEXT,
 first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,raw_id INTEGER REFERENCES raw_records(id));
CREATE TABLE IF NOT EXISTS post_versions(
 id INTEGER PRIMARY KEY,post_id INTEGER NOT NULL REFERENCES posts(post_id),body TEXT,
 edited_at TEXT,observed_at TEXT NOT NULL,content_hash TEXT NOT NULL,
 raw_id INTEGER REFERENCES raw_records(id),UNIQUE(post_id,content_hash));
CREATE TABLE IF NOT EXISTS counter_snapshots(
 id INTEGER PRIMARY KEY,post_id INTEGER NOT NULL REFERENCES posts(post_id),
 observed_at TEXT NOT NULL,like_count INTEGER,reply_count INTEGER,repost_count INTEGER,
 view_count INTEGER,score_inputs_updated_at TEXT,raw_id INTEGER REFERENCES raw_records(id),
 UNIQUE(post_id,observed_at),CHECK(like_count IS NULL OR like_count>=0),
 CHECK(reply_count IS NULL OR reply_count>=0),CHECK(repost_count IS NULL OR repost_count>=0),
 CHECK(view_count IS NULL OR view_count>=0));
CREATE TABLE IF NOT EXISTS feed_observations(
 id INTEGER PRIMARY KEY,post_id INTEGER NOT NULL REFERENCES posts(post_id),source TEXT NOT NULL,
 observed_at TEXT NOT NULL,rank_position INTEGER,global_hot_score REAL,
 UNIQUE(post_id,source,observed_at));
CREATE TABLE IF NOT EXISTS post_likes(
 post_id INTEGER NOT NULL,account_id INTEGER NOT NULL,liked_at TEXT,
 first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,PRIMARY KEY(post_id,account_id));
CREATE TABLE IF NOT EXISTS account_followers(
 account_id INTEGER NOT NULL,follower_id INTEGER NOT NULL,followed_at TEXT,
 first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,PRIMARY KEY(account_id,follower_id));
CREATE TABLE IF NOT EXISTS npc_vote_labels(
 account_id INTEGER NOT NULL,window_end TEXT NOT NULL,choice TEXT NOT NULL,
 p_yes REAL NOT NULL,p_no REAL NOT NULL,p_unsure REAL NOT NULL,model TEXT,labelled_at TEXT NOT NULL,
 PRIMARY KEY(window_end,account_id));
CREATE TABLE IF NOT EXISTS post_stance_labels(
 post_id INTEGER PRIMARY KEY,stance TEXT NOT NULL,concerns_json TEXT NOT NULL,model TEXT,labelled_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hidden_posts(
 post_id INTEGER PRIMARY KEY,account_id INTEGER NOT NULL,created_at TEXT,detected_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS post_visibility(
 post_id INTEGER PRIMARY KEY,account_id INTEGER NOT NULL,created_at TEXT,status TEXT NOT NULL,
 first_404_at TEXT,confirmed_at TEXT,last_checked_at TEXT NOT NULL,last_http INTEGER,checks INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS idx_post_visibility_status ON post_visibility(status,first_404_at);
CREATE INDEX IF NOT EXISTS idx_post_visibility_account ON post_visibility(account_id,status);
CREATE TABLE IF NOT EXISTS post_visibility_shadow(
 post_id INTEGER NOT NULL,check_label TEXT NOT NULL,checked_at TEXT NOT NULL,http_status INTEGER,
 minutes_after_first_404 REAL,first_404_at TEXT,PRIMARY KEY(post_id,check_label));
CREATE TABLE IF NOT EXISTS news_articles(
 article_key TEXT PRIMARY KEY,site TEXT,slug TEXT,title TEXT,body TEXT,author TEXT,
 published_at TEXT,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,
 raw_id INTEGER REFERENCES raw_records(id));
CREATE TABLE IF NOT EXISTS api_call_logs(
 id INTEGER PRIMARY KEY,occurred_at TEXT NOT NULL,service TEXT,method TEXT NOT NULL,
 path TEXT NOT NULL,status INTEGER,duration_ms REAL,retry_count INTEGER DEFAULT 0,
 retry_after REAL,rate_limit REAL,rate_remaining REAL,rate_reset REAL,error TEXT,entity_id TEXT);
CREATE TABLE IF NOT EXISTS collection_runs(
 id INTEGER PRIMARY KEY,started_at TEXT NOT NULL,finished_at TEXT,status TEXT NOT NULL,
 metrics_json TEXT,error TEXT);
CREATE TABLE IF NOT EXISTS checkpoints(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS activity_imports(
 id INTEGER PRIMARY KEY,sha256 TEXT NOT NULL UNIQUE,filename TEXT NOT NULL,
 imported_at TEXT NOT NULL,row_count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS official_activity(
 id INTEGER PRIMARY KEY,import_id INTEGER REFERENCES activity_imports(id),row_number INTEGER,
 occurred_at TEXT,event_type TEXT NOT NULL,message TEXT NOT NULL,
 fingerprint TEXT NOT NULL,occurrence INTEGER NOT NULL DEFAULT 1,track TEXT,
 points_decimal TEXT,parse_status TEXT NOT NULL,parse_note TEXT,
 UNIQUE(fingerprint,occurrence));
CREATE TABLE IF NOT EXISTS activity_import_payloads(
 import_id INTEGER PRIMARY KEY REFERENCES activity_imports(id),
 raw_id INTEGER NOT NULL REFERENCES raw_records(id));
CREATE TABLE IF NOT EXISTS score_snapshots(
 id INTEGER PRIMARY KEY,observed_at TEXT NOT NULL,total_decimal TEXT NOT NULL,
 source TEXT NOT NULL,note TEXT NOT NULL DEFAULT '');
-- Each team score on the competition leaderboard, one row per team per extension read (leaderboard.py, D104).
-- The id comes first: backups verify each table ordered by its first column, which must be unique.
CREATE TABLE IF NOT EXISTS leaderboard_snapshots(
 id INTEGER PRIMARY KEY,observed_at TEXT NOT NULL,competition_id INTEGER NOT NULL,rank INTEGER,team TEXT NOT NULL,
 score REAL NOT NULL,team_id INTEGER,UNIQUE(competition_id,observed_at,team));
CREATE INDEX IF NOT EXISTS idx_leaderboard_team_time ON leaderboard_snapshots(competition_id,team,observed_at);
CREATE TABLE IF NOT EXISTS analysis_runs(
 id INTEGER PRIMARY KEY,created_at TEXT NOT NULL,kind TEXT NOT NULL,
 parameters_json TEXT NOT NULL,result_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS analysis_cache(
 cache_key TEXT PRIMARY KEY,scope_key TEXT NOT NULL,created_at TEXT NOT NULL,
 result_json TEXT NOT NULL,result_bytes INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_analysis_cache_scope ON analysis_cache(scope_key);
CREATE TABLE IF NOT EXISTS post_labels(
 id INTEGER PRIMARY KEY,post_id INTEGER NOT NULL REFERENCES posts(post_id),label TEXT NOT NULL,
 value TEXT NOT NULL,source TEXT NOT NULL,version TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counter_conflicts(
 id INTEGER PRIMARY KEY,post_id INTEGER NOT NULL REFERENCES posts(post_id),
 observed_at TEXT NOT NULL,source TEXT NOT NULL,observation_hash TEXT NOT NULL UNIQUE,
 like_count INTEGER,reply_count INTEGER,repost_count INTEGER,view_count INTEGER,
 score_inputs_updated_at TEXT,raw_id INTEGER REFERENCES raw_records(id),
 canonical_snapshot_id INTEGER NOT NULL REFERENCES counter_snapshots(id));
CREATE TABLE IF NOT EXISTS score_observation_keys(
 observation_key TEXT PRIMARY KEY,snapshot_id INTEGER NOT NULL REFERENCES score_snapshots(id));
CREATE TABLE IF NOT EXISTS collection_tasks(
 task_key TEXT PRIMARY KEY,kind TEXT NOT NULL,payload_json TEXT NOT NULL,
 priority INTEGER NOT NULL DEFAULT 0,due_at DOUBLE PRECISION NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending',lease_owner TEXT,lease_token TEXT,
 lease_until DOUBLE PRECISION,attempts INTEGER NOT NULL DEFAULT 0,
 result_json TEXT,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_audit_events(
 id TEXT PRIMARY KEY,agent_id TEXT,at TEXT NOT NULL,kind TEXT NOT NULL,
 message TEXT NOT NULL,draft_id TEXT,details_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_agent_audit_agent_time ON agent_audit_events(agent_id,at,id);
CREATE INDEX IF NOT EXISTS idx_agent_audit_agent_kind_time ON agent_audit_events(agent_id,kind,at,id);
CREATE TABLE IF NOT EXISTS agent_reviews(
 id TEXT PRIMARY KEY,draft_id TEXT NOT NULL,agent_id TEXT NOT NULL,created_at TEXT NOT NULL,
 input_hash TEXT NOT NULL,status TEXT NOT NULL,record_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_agent_reviews_time ON agent_reviews(created_at,id);
CREATE INDEX IF NOT EXISTS idx_agent_reviews_draft ON agent_reviews(draft_id,created_at,id);
CREATE TABLE IF NOT EXISTS agent_memory_records(
 id TEXT PRIMARY KEY,agent_id TEXT NOT NULL,reset_at TEXT NOT NULL,
 kind TEXT NOT NULL,record_key TEXT NOT NULL,at TEXT NOT NULL,
 payload_json TEXT NOT NULL,search_text TEXT NOT NULL,captured_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_memory_role_kind_time
 ON agent_memory_records(agent_id,reset_at,kind,at,id);
CREATE TABLE IF NOT EXISTS agent_memory_links(
 record_id TEXT NOT NULL REFERENCES agent_memory_records(id) ON DELETE CASCADE,
 agent_id TEXT NOT NULL,reset_at TEXT NOT NULL,source_id TEXT NOT NULL,
 post_id INTEGER,author_id INTEGER,PRIMARY KEY(record_id,source_id));
CREATE INDEX IF NOT EXISTS idx_memory_link_author ON agent_memory_links(agent_id,reset_at,author_id,record_id);
CREATE INDEX IF NOT EXISTS idx_memory_link_post ON agent_memory_links(agent_id,reset_at,post_id,record_id);
CREATE INDEX IF NOT EXISTS idx_memory_link_source ON agent_memory_links(agent_id,reset_at,source_id,record_id);
CREATE TABLE IF NOT EXISTS facts(
 id TEXT PRIMARY KEY,layer TEXT NOT NULL,status TEXT NOT NULL,claim TEXT NOT NULL,
 claim_hash TEXT NOT NULL UNIQUE,concerns_json TEXT NOT NULL,source_label TEXT NOT NULL,
 article_key TEXT,evidence_quote TEXT,observed_at TEXT,topic_key TEXT,supersedes_id TEXT,
 valid_until TEXT,decided_by TEXT,decision_note TEXT,created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,decided_at TEXT,revision INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS idx_facts_status ON facts(status,layer);
CREATE INDEX IF NOT EXISTS idx_facts_topic ON facts(topic_key,status,observed_at);
CREATE INDEX IF NOT EXISTS idx_facts_article ON facts(article_key);
CREATE TABLE IF NOT EXISTS fact_rebuttals(
 concern TEXT PRIMARY KEY,no_argument TEXT NOT NULL,line TEXT NOT NULL,fact_ids_json TEXT NOT NULL,
 status TEXT NOT NULL,updated_at TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS fact_guardrails(
 id TEXT PRIMARY KEY,rule TEXT NOT NULL,status TEXT NOT NULL,updated_at TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS fact_extractions(
 article_key TEXT NOT NULL,body_hash TEXT NOT NULL,status TEXT NOT NULL,
 accepted INTEGER NOT NULL DEFAULT 0,dropped INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,
 error TEXT,next_attempt_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 PRIMARY KEY(article_key,body_hash));
CREATE TABLE IF NOT EXISTS post_attribution(
 post_id INTEGER PRIMARY KEY,agent_id TEXT NOT NULL,draft_id TEXT,published_at TEXT NOT NULL,action TEXT NOT NULL,
 concern TEXT,used_radar INTEGER NOT NULL DEFAULT 0,fact_ids_json TEXT NOT NULL,target_post_id INTEGER,
 target_author_id INTEGER,target_stance TEXT,target_bot INTEGER,thread_depth INTEGER,body_chars INTEGER,
 hour_local INTEGER,pipeline TEXT,captured_at TEXT NOT NULL,outcome_at TEXT,npc_replies INTEGER,
 reply_yes INTEGER,reply_undecided INTEGER,reply_no INTEGER,likes INTEGER,reposts INTEGER,reward DOUBLE PRECISION);
CREATE INDEX IF NOT EXISTS idx_post_attribution_time ON post_attribution(published_at);
CREATE TABLE IF NOT EXISTS llm_calls(
 id TEXT PRIMARY KEY,started_at TEXT NOT NULL,finished_at TEXT NOT NULL,purpose TEXT NOT NULL,
 agent_id TEXT,job_id TEXT,provider_id TEXT,model TEXT,upstream TEXT,status TEXT NOT NULL,
 http_status INTEGER,error_code TEXT,finish_reason TEXT,attempts INTEGER NOT NULL DEFAULT 1,
 wait_ms DOUBLE PRECISION,ttft_ms DOUBLE PRECISION,duration_ms DOUBLE PRECISION,max_tokens INTEGER,
 session_hash TEXT,prompt_tokens INTEGER,cached_tokens INTEGER,cache_write_tokens INTEGER,
 completion_tokens INTEGER,reasoning_tokens INTEGER,cost DOUBLE PRECISION);
CREATE INDEX IF NOT EXISTS idx_llm_calls_time ON llm_calls(ctn_timestamp(started_at),id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_purpose_time ON llm_calls(purpose,ctn_timestamp(started_at));
CREATE TABLE IF NOT EXISTS post_annotations(
 post_id INTEGER PRIMARY KEY,author_id INTEGER,created_at TEXT,stance TEXT NOT NULL,concerns_json TEXT NOT NULL,
 thread_depth INTEGER,source_edited_at TEXT,classifier TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_post_annotations_author ON post_annotations(author_id);
CREATE INDEX IF NOT EXISTS idx_post_annotations_time ON post_annotations(ctn_timestamp(created_at),stance);
CREATE TABLE IF NOT EXISTS account_annotations(
 account_id INTEGER PRIMARY KEY,is_bot INTEGER NOT NULL DEFAULT 0,bot_reasons_json TEXT NOT NULL DEFAULT '[]',
 flagged_at TEXT,updated_at TEXT NOT NULL,bot_metrics_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS idx_collection_tasks_due ON collection_tasks(kind,state,due_at,priority);
CREATE INDEX IF NOT EXISTS idx_collection_tasks_leased ON collection_tasks(lease_owner,lease_until) WHERE state='leased';
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at);
CREATE INDEX IF NOT EXISTS idx_posts_created_utc ON posts(ctn_timestamp(created_at) DESC,post_id DESC);
CREATE INDEX IF NOT EXISTS idx_posts_freshness_utc ON posts(COALESCE(ctn_timestamp(edited_at),ctn_timestamp(created_at),ctn_timestamp(first_seen)) DESC,post_id DESC);
CREATE INDEX IF NOT EXISTS idx_posts_author_created ON posts(author_id,created_at);
CREATE INDEX IF NOT EXISTS idx_posts_reply ON posts(reply_to_post_id);
CREATE INDEX IF NOT EXISTS idx_posts_repost ON posts(repost_of_post_id);
CREATE INDEX IF NOT EXISTS idx_counters_post_time ON counter_snapshots(post_id,observed_at,id);
CREATE INDEX IF NOT EXISTS idx_feed_time ON feed_observations(source,observed_at);
CREATE INDEX IF NOT EXISTS idx_post_likes_time ON post_likes(ctn_timestamp(liked_at),post_id);
CREATE INDEX IF NOT EXISTS idx_account_followers_time ON account_followers(ctn_timestamp(followed_at),account_id);
CREATE INDEX IF NOT EXISTS idx_activity_time ON official_activity(occurred_at,id);
CREATE INDEX IF NOT EXISTS idx_logs_time ON api_call_logs(occurred_at);
CREATE INDEX IF NOT EXISTS idx_news_published ON news_articles(published_at);
CREATE INDEX IF NOT EXISTS idx_counters_post_utc ON counter_snapshots(post_id,ctn_timestamp(observed_at),id);
CREATE INDEX IF NOT EXISTS idx_counters_utc ON counter_snapshots(ctn_timestamp(observed_at),id);
CREATE INDEX IF NOT EXISTS idx_feed_post_utc ON feed_observations(post_id,ctn_timestamp(observed_at),source,id);
CREATE INDEX IF NOT EXISTS idx_feed_source_utc ON feed_observations(source,ctn_timestamp(observed_at),id);
-- Only the key families read or pruned by age. counter_attempt rows (one per
-- post, rewritten on every sample) stay out, so their updates skip this index.
DROP INDEX IF EXISTS idx_checkpoints_updated_utc;
CREATE INDEX IF NOT EXISTS idx_checkpoints_retention_utc ON checkpoints(ctn_timestamp(updated_at),key) WHERE key LIKE 'worker_result:%' OR key LIKE 'coordination:post:%' OR key LIKE 'agent_tracking:action:%';
-- The extension status page reads every browser receipt; without this each
-- request scanned all checkpoints (mostly per-post counter_attempt rows).
CREATE INDEX IF NOT EXISTS idx_checkpoints_browser_receipts ON checkpoints(key) WHERE key LIKE 'browser_receipt:%';
CREATE INDEX IF NOT EXISTS idx_raw_observed_utc ON raw_records(ctn_timestamp(observed_at),id);
CREATE INDEX IF NOT EXISTS idx_accounts_raw ON accounts(raw_id);
CREATE INDEX IF NOT EXISTS idx_posts_raw ON posts(raw_id);
CREATE INDEX IF NOT EXISTS idx_post_versions_raw ON post_versions(raw_id);
CREATE INDEX IF NOT EXISTS idx_counters_raw ON counter_snapshots(raw_id);
CREATE INDEX IF NOT EXISTS idx_news_raw ON news_articles(raw_id);
CREATE INDEX IF NOT EXISTS idx_counter_conflicts_raw ON counter_conflicts(raw_id);
CREATE INDEX IF NOT EXISTS idx_activity_import_payload_raw ON activity_import_payloads(raw_id);
"""


# Counter sampling read model (see postgres.POSTGRES_DERIVED). Derived rows are
# kept outside SCHEMA so migrations and backups never copy them. SQLite cannot
# replace a trigger body, so initialize() drops and recreates the triggers.
SAMPLING_TRIGGERS = ('trg_counter_sampling_post', 'trg_counter_sampling_snapshot',
                     'trg_counter_sampling_attempt_insert', 'trg_counter_sampling_attempt_update')
_SAMPLING_ORDER = '''UPDATE counter_sampling SET last_attempt=CASE
    WHEN COALESCE(attempted_at,first_seen)>COALESCE(sampled_at,first_seen) THEN COALESCE(attempted_at,first_seen)
    ELSE COALESCE(sampled_at,first_seen) END'''
# Trigger bodies avoid INSERT OR IGNORE: an outer UPSERT's conflict policy
# overrides it and the duplicate would abort the business write.
_SAMPLING_SEED = '''INSERT INTO counter_sampling(post_id,first_seen,last_attempt,replies_synced)
    SELECT post_id,ctn_timestamp(first_seen),ctn_timestamp(first_seen),1 FROM posts
    WHERE post_id={target} AND NOT EXISTS(SELECT 1 FROM counter_sampling WHERE post_id={target});'''
_SAMPLING_ATTEMPT = ('BEGIN ' + _SAMPLING_SEED.format(target='CAST(substr(NEW.key,17) AS INTEGER)') + '''
    UPDATE counter_sampling SET attempted_at=ctn_timestamp(NEW.updated_at),
      attempt_failed=CASE WHEN (CASE WHEN json_valid(NEW.value) THEN json_extract(NEW.value,'$.status') END)='failed'
                          THEN 1 ELSE 0 END,
      attempt_http=CASE WHEN json_valid(NEW.value) THEN CAST(json_extract(NEW.value,'$.http_status') AS INTEGER) END
    WHERE post_id=CAST(substr(NEW.key,17) AS INTEGER);
    ''' + _SAMPLING_ORDER + ' WHERE post_id=CAST(substr(NEW.key,17) AS INTEGER); END;')
_ATTEMPT_KEY = ("NEW.key GLOB 'counter_attempt:[1-9]*' AND substr(NEW.key,17) NOT GLOB '*[^0-9]*' "
                'AND length(NEW.key)<=34')
SQLITE_DERIVED = '''
CREATE TABLE IF NOT EXISTS counter_sampling(
 post_id INTEGER PRIMARY KEY,first_seen TEXT,sampled_at TEXT,has_sample INTEGER NOT NULL DEFAULT 0,
 attempted_at TEXT,attempt_failed INTEGER NOT NULL DEFAULT 0,attempt_http INTEGER,
 browser_sampled_at TEXT,last_attempt TEXT,reply_count INTEGER,replies_synced INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_counter_sampling_queue ON counter_sampling(has_sample,last_attempt,post_id DESC);
''' + ''.join('DROP TRIGGER IF EXISTS ' + name + ';\n' for name in SAMPLING_TRIGGERS) + '''
CREATE TRIGGER IF NOT EXISTS trg_counter_sampling_post AFTER INSERT ON posts BEGIN
  ''' + _SAMPLING_SEED.format(target='NEW.post_id') + '''
END;
CREATE TRIGGER IF NOT EXISTS trg_counter_sampling_snapshot AFTER INSERT ON counter_snapshots BEGIN
  ''' + _SAMPLING_SEED.format(target='NEW.post_id') + '''
  UPDATE counter_sampling SET has_sample=1,
    reply_count=CASE WHEN ctn_timestamp(NEW.observed_at) IS NOT NULL
                       AND (sampled_at IS NULL OR ctn_timestamp(NEW.observed_at)>=sampled_at)
                     THEN NEW.reply_count ELSE reply_count END,
    sampled_at=CASE WHEN ctn_timestamp(NEW.observed_at) IS NULL OR sampled_at>=ctn_timestamp(NEW.observed_at)
                    THEN sampled_at ELSE ctn_timestamp(NEW.observed_at) END,
    browser_sampled_at=CASE WHEN NOT EXISTS(SELECT 1 FROM raw_records WHERE id=NEW.raw_id AND source='browser:counters')
                              OR ctn_timestamp(NEW.observed_at) IS NULL OR browser_sampled_at>=ctn_timestamp(NEW.observed_at)
                            THEN browser_sampled_at ELSE ctn_timestamp(NEW.observed_at) END
  WHERE post_id=NEW.post_id;
  ''' + _SAMPLING_ORDER + ''' WHERE post_id=NEW.post_id;
END;
CREATE TRIGGER IF NOT EXISTS trg_counter_sampling_attempt_insert AFTER INSERT ON checkpoints
  WHEN ''' + _ATTEMPT_KEY + ' ' + _SAMPLING_ATTEMPT + '''
CREATE TRIGGER IF NOT EXISTS trg_counter_sampling_attempt_update AFTER UPDATE ON checkpoints
  WHEN ''' + _ATTEMPT_KEY + ' ' + _SAMPLING_ATTEMPT + '''
UPDATE counter_sampling SET replies_synced=1,reply_count=(SELECT c.reply_count FROM counter_snapshots c
    WHERE c.post_id=counter_sampling.post_id AND ctn_timestamp(c.observed_at) IS NOT NULL
    ORDER BY ctn_timestamp(c.observed_at) DESC,c.id DESC LIMIT 1) WHERE replies_synced=0;
INSERT INTO counter_sampling(post_id,first_seen,sampled_at,has_sample,attempted_at,attempt_failed,attempt_http,
                             browser_sampled_at,reply_count,replies_synced)
SELECT p.post_id,ctn_timestamp(p.first_seen),
  (SELECT max(ctn_timestamp(c.observed_at)) FROM counter_snapshots c WHERE c.post_id=p.post_id),
  EXISTS(SELECT 1 FROM counter_snapshots c WHERE c.post_id=p.post_id),
  ctn_timestamp(q.updated_at),
  CASE WHEN (CASE WHEN json_valid(q.value) THEN json_extract(q.value,'$.status') END)='failed' THEN 1 ELSE 0 END,
  CASE WHEN json_valid(q.value) THEN CAST(json_extract(q.value,'$.http_status') AS INTEGER) END,
  (SELECT max(ctn_timestamp(c.observed_at)) FROM counter_snapshots c JOIN raw_records r ON r.id=c.raw_id
   WHERE c.post_id=p.post_id AND r.source='browser:counters'),
  (SELECT c.reply_count FROM counter_snapshots c WHERE c.post_id=p.post_id AND ctn_timestamp(c.observed_at) IS NOT NULL
   ORDER BY ctn_timestamp(c.observed_at) DESC,c.id DESC LIMIT 1),1
FROM posts p LEFT JOIN checkpoints q ON q.key='counter_attempt:' || p.post_id
WHERE NOT EXISTS(SELECT 1 FROM counter_sampling s WHERE s.post_id=p.post_id);
''' + _SAMPLING_ORDER + ''' WHERE last_attempt IS NOT CASE
    WHEN COALESCE(attempted_at,first_seen)>COALESCE(sampled_at,first_seen) THEN COALESCE(attempted_at,first_seen)
    ELSE COALESCE(sampled_at,first_seen) END;
'''


def initialize(path: str | Path, *, force: bool = False) -> None:
    if is_postgres(path):
        from .postgres import initialize_postgres
        initialize_postgres(str(path), SCHEMA, SCHEMA_VERSION, force=force)
        return
    connection = connect(path)
    try:
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f'Database schema {version} is newer than this application')
        connection.execute('PRAGMA journal_mode=WAL')
        connection.executescript(SCHEMA)
        # v2 retains invalid timestamp rows without making them look date-valid.
        activity_info = {row[1]: row for row in connection.execute('PRAGMA table_info(official_activity)')}
        if activity_info['occurred_at'][3]:
            connection.executescript('''
                ALTER TABLE official_activity RENAME TO official_activity_v1;
                CREATE TABLE official_activity(
                    id INTEGER PRIMARY KEY,import_id INTEGER REFERENCES activity_imports(id),row_number INTEGER,
                    occurred_at TEXT,event_type TEXT NOT NULL,message TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,occurrence INTEGER NOT NULL DEFAULT 1,track TEXT,
                    points_decimal TEXT,parse_status TEXT NOT NULL,parse_note TEXT,
                    UNIQUE(fingerprint,occurrence));
                INSERT INTO official_activity SELECT * FROM official_activity_v1;
                DROP TABLE official_activity_v1;
                CREATE INDEX idx_activity_time ON official_activity(occurred_at,id);
            ''')
        sampling = {row[1] for row in connection.execute('PRAGMA table_info(counter_sampling)')}
        if sampling and 'reply_count' not in sampling:
            connection.execute('ALTER TABLE counter_sampling ADD COLUMN reply_count INTEGER')
            connection.execute('ALTER TABLE counter_sampling ADD COLUMN replies_synced INTEGER NOT NULL DEFAULT 0')
        annotated = {row[1] for row in connection.execute('PRAGMA table_info(account_annotations)')}
        if annotated and 'bot_metrics_json' not in annotated:
            connection.execute("ALTER TABLE account_annotations ADD COLUMN bot_metrics_json TEXT NOT NULL DEFAULT '{}'")
        connection.executescript(SQLITE_DERIVED)
        connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        connection.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('created_at', utc_now()))
        connection.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('mode', 'live'))
        connection.commit()
    finally:
        connection.close()


def raw_record(connection: sqlite3.Connection, source: str, endpoint: str,
               payload: object, observed_at: str | None = None) -> int:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256((source + '\n' + endpoint + '\n' + serialized).encode()).hexdigest()
    if getattr(connection, 'dialect', None) == 'postgresql':
        if not connection.in_transaction:
            connection.execute('BEGIN ISOLATION LEVEL READ COMMITTED')
        # Retention locks orphan candidates FOR UPDATE SKIP LOCKED. Protect an
        # existing payload before reusing its ID in a new business reference.
        existing = connection.execute('SELECT id FROM raw_records WHERE sha256=? FOR KEY SHARE',
                                      (digest,)).fetchone()
        if existing:
            return int(existing[0])
        # A competing insertion may appear after the read above. Lock that row
        # via a no-op conflict update rather than returning an unprotected ID.
        return int(connection.execute('''INSERT INTO raw_records
            (sha256,source,endpoint,observed_at,payload_json) VALUES(?,?,?,?,?)
            ON CONFLICT(sha256) DO UPDATE SET sha256=excluded.sha256 RETURNING id''',
            (digest, source, endpoint, observed_at or utc_now(), serialized)).fetchone()[0])
    connection.execute('INSERT OR IGNORE INTO raw_records(sha256,source,endpoint,observed_at,payload_json) VALUES(?,?,?,?,?)',
                       (digest, source, endpoint, observed_at or utc_now(), serialized))
    return int(connection.execute('SELECT id FROM raw_records WHERE sha256=?', (digest,)).fetchone()[0])


def backup(path: str | Path, directory: str | Path) -> Path:
    if is_postgres(path):
        from .migrate_postgres import backup_postgres
        return backup_postgres(str(path), directory)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    destination = directory / f'ctn-{stamp}.sqlite3'
    source = connect(path)
    target = configure_sqlite(sqlite3.connect(destination))
    try:
        source.backup(target)
        portable_indexes(target)
        if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Backup integrity verification failed')
    finally:
        target.close()
        source.close()
    return destination
