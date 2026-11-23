-- Phase 4 step A: memory and statistics settings. Written to postgresql.auto.conf;
-- they take effect only after the PG restart in docs/DB_PHASE4_PLAN.md.
-- Rollback: db-phase4-a-rollback.sql (PG running) or the backup copy of postgresql.auto.conf.
ALTER SYSTEM SET shared_buffers = '4GB';
ALTER SYSTEM SET effective_cache_size = '16GB';
ALTER SYSTEM SET maintenance_work_mem = '512MB';
ALTER SYSTEM SET work_mem = '16MB';
ALTER SYSTEM SET random_page_cost = 1.1;
ALTER SYSTEM SET effective_io_concurrency = 200;
ALTER SYSTEM SET max_wal_size = '4GB';
ALTER SYSTEM SET checkpoint_timeout = '15min';
ALTER SYSTEM SET wal_compression = 'lz4';
ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements';
ALTER SYSTEM SET pg_stat_statements.max = 5000;
ALTER SYSTEM SET track_io_timing = on;
ALTER SYSTEM SET jit = off;
