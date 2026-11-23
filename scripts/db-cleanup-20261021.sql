-- One-time cleanup of expired operational rows (2026-10-21 database load fix).
-- Safe while CTN runs: deletes in 5000-row batches with SKIP LOCKED and only
-- touches rows retention.py now treats as disposable. Business data is untouched.
--
-- Run from the project root:
--   Get-Content scripts/db-cleanup-20261021.sql -Raw | docker exec -i ctn-postgres-1 psql -U ctn -d ctn -X
\set ON_ERROR_STOP on
SET lock_timeout = '5s';

-- Workers look up their own lease on every claim poll (was a full table scan).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_collection_tasks_leased
    ON collection_tasks(lease_owner, lease_until) WHERE state = 'leased';
SELECT indisvalid AS leased_index_valid FROM pg_index
    WHERE indexrelid = 'idx_collection_tasks_leased'::regclass;

DO $$
DECLARE
    stamp CONSTANT text := 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"';
    task_cutoff text := to_char((now() - interval '1 hour') AT TIME ZONE 'UTC', stamp);
    lease_cutoff text := to_char((now() - interval '24 hours') AT TIME ZONE 'UTC', stamp);
    now_epoch double precision := extract(epoch FROM now());
    n bigint;
    tasks bigint := 0;
    receipts bigint := 0;
    leases bigint := 0;
BEGIN
    -- Finished queue rows: nothing reads them; enqueue() recreates the row.
    LOOP
        DELETE FROM collection_tasks WHERE task_key IN (
            SELECT task_key FROM collection_tasks
            WHERE state = 'done' AND updated_at < task_cutoff
            LIMIT 5000 FOR UPDATE SKIP LOCKED);
        GET DIAGNOSTICS n = ROW_COUNT;
        tasks := tasks + n;
        COMMIT;
        EXIT WHEN n = 0;
    END LOOP;
    -- Worker result receipts: only replayed within the 120 s lease.
    LOOP
        DELETE FROM checkpoints WHERE key IN (
            SELECT key FROM checkpoints
            WHERE key LIKE 'worker_result:%' AND updated_at < lease_cutoff
            LIMIT 5000 FOR UPDATE SKIP LOCKED);
        GET DIAGNOSTICS n = ROW_COUNT;
        receipts := receipts + n;
        COMMIT;
        EXIT WHEN n = 0;
    END LOOP;
    -- Per-post request leases (30 s) that have expired.
    LOOP
        DELETE FROM checkpoints WHERE key IN (
            SELECT key FROM checkpoints
            WHERE key LIKE 'coordination:post:%' AND updated_at < lease_cutoff
              AND ctn_json_number(value, '$.until') < now_epoch
            LIMIT 5000 FOR UPDATE SKIP LOCKED);
        GET DIAGNOSTICS n = ROW_COUNT;
        leases := leases + n;
        COMMIT;
        EXIT WHEN n = 0;
    END LOOP;
    RAISE NOTICE 'deleted: % finished tasks, % worker receipts, % expired post leases',
        tasks, receipts, leases;
END $$;

VACUUM (ANALYZE) collection_tasks;
VACUUM (ANALYZE) checkpoints;
VACUUM (ANALYZE) meta;
SELECT relname, n_live_tup, n_dead_tup, pg_size_pretty(pg_total_relation_size(relid)) AS total_size
    FROM pg_stat_user_tables WHERE relname IN ('collection_tasks', 'checkpoints', 'meta');
