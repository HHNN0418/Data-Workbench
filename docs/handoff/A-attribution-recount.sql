-- Recount post_attribution with the citizen-only filter (user approved 2026-10-22).
-- Run ONLY AFTER the reload that deploys A's post_attribution change: before that, the running
-- server would re-settle these rows with the old bot filter within 10 minutes.
-- Backup: backups/post-attribution-before-recount-20261022-112750.json (663 rows, 375 settled, 84 npc_replies).
-- Run: docker exec -i ctn-postgres-1 sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < docs/handoff/A-attribution-recount.sql
BEGIN;
UPDATE post_attribution SET outcome_at=NULL, npc_replies=NULL, reply_yes=NULL, reply_undecided=NULL, reply_no=NULL,
  likes=NULL, reposts=NULL, reward=NULL
WHERE outcome_at IS NOT NULL;
SELECT count(*) AS waiting_to_resettle FROM post_attribution WHERE outcome_at IS NULL;
COMMIT;
-- settle() handles 500 rows per 10-minute loop; after about 20 minutes check:
-- SELECT count(*) FILTER (WHERE outcome_at IS NOT NULL) settled, sum(npc_replies) citizen_replies, sum(reply_yes) yes FROM post_attribution;
