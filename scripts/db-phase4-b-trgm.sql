-- Phase 4 step B: trigram index for LOWER(body) LIKE '%term%' searches. No restart.
-- Run with psql WITHOUT -1/--single-transaction: CREATE INDEX CONCURRENTLY cannot run in a transaction.
-- Rollback: db-phase4-b-rollback.sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
SET maintenance_work_mem = '512MB';
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_posts_body_trgm ON posts USING gin (LOWER(body) gin_trgm_ops);
SELECT indexrelid::regclass, indisvalid, pg_size_pretty(pg_relation_size(indexrelid)) FROM pg_index WHERE indexrelid='idx_posts_body_trgm'::regclass;
