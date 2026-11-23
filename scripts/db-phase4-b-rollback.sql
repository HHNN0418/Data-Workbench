-- Roll back phase 4 step B. Also use this if the concurrent build failed (indisvalid = false).
DROP INDEX CONCURRENTLY IF EXISTS idx_posts_body_trgm;
-- The extension is harmless when unused; drop it only if nothing else uses it:
-- DROP EXTENSION IF EXISTS pg_trgm;
