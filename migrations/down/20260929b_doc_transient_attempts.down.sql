-- DOWN pentru migrations/20260929b_doc_transient_attempts.sql (T3-D1). Manual, NU prin migrate.sh.
-- Oprește întâi flag-ul processing.doc_retry_limit_enabled (codul din spatele lui citește coloanele).
--
--   psql ... -v ON_ERROR_STOP=1 -f migrations/down/20260929b_doc_transient_attempts.down.sql
--   psql ... -c "DELETE FROM _release_migrations WHERE filename='20260929b_doc_transient_attempts.sql'"

ALTER TABLE attachments DROP COLUMN IF EXISTS doc_transient_last_at;
ALTER TABLE attachments DROP COLUMN IF EXISTS doc_transient_attempts;
DELETE FROM settings WHERE key IN ('processing.doc_retry_limit_enabled',
                                   'processing.vision_image_normalize_enabled');

SELECT 'migration 20260929b_doc_transient_attempts reverted' AS status;
