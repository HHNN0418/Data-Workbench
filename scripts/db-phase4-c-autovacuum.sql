-- Phase 4 step C: earlier autovacuum on hot tables. No restart; takes effect immediately.
-- Before (2026-10-22): reloptions was NULL on all five tables. Rollback: db-phase4-c-rollback.sql
ALTER TABLE checkpoints      SET (autovacuum_vacuum_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.02);
ALTER TABLE posts            SET (autovacuum_vacuum_scale_factor = 0.05, autovacuum_analyze_scale_factor = 0.02);
ALTER TABLE counter_sampling SET (autovacuum_vacuum_scale_factor = 0.05);
ALTER TABLE collection_tasks SET (autovacuum_vacuum_scale_factor = 0.05);
ALTER TABLE meta             SET (autovacuum_vacuum_scale_factor = 0.05);
SELECT relname, reloptions FROM pg_class WHERE relname IN ('checkpoints','posts','counter_sampling','collection_tasks','meta') ORDER BY relname;
