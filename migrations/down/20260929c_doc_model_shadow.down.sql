-- DOWN pentru migrations/20260929c_doc_model_shadow.sql (T3-O14). Manual, NU prin migrate.sh.
-- Golește întâi documents.haiku_shadow_tasks (altfel scrierile shadow doar loghează WARNING).
--
--   psql ... -v ON_ERROR_STOP=1 -f migrations/down/20260929c_doc_model_shadow.down.sql
--   psql ... -c "DELETE FROM _release_migrations WHERE filename='20260929c_doc_model_shadow.sql'"

DROP TABLE IF EXISTS doc_model_shadow;
DELETE FROM settings WHERE key IN ('documents.haiku_first_tasks', 'documents.haiku_min_confidence',
                                   'documents.haiku_shadow_tasks', 'documents.haiku_shadow_sample');

SELECT 'migration 20260929c_doc_model_shadow reverted' AS status;
