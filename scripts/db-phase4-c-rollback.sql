-- Roll back phase 4 step C: back to the server defaults (reloptions NULL).
ALTER TABLE checkpoints      RESET (autovacuum_vacuum_scale_factor, autovacuum_analyze_scale_factor);
ALTER TABLE posts            RESET (autovacuum_vacuum_scale_factor, autovacuum_analyze_scale_factor);
ALTER TABLE counter_sampling RESET (autovacuum_vacuum_scale_factor);
ALTER TABLE collection_tasks RESET (autovacuum_vacuum_scale_factor);
ALTER TABLE meta             RESET (autovacuum_vacuum_scale_factor);
SELECT relname, reloptions FROM pg_class WHERE relname IN ('checkpoints','posts','counter_sampling','collection_tasks','meta') ORDER BY relname;
