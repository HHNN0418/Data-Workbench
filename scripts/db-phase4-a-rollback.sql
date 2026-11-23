-- Roll back phase 4 step A while PG is running; restart PG afterwards.
-- Before the change postgresql.auto.conf held no settings (88-byte header only),
-- so resetting everything restores the postgresql.conf values (shared_buffers 128MB).
ALTER SYSTEM RESET ALL;
