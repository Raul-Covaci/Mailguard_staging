-- DOWN pentru migrations/20260929_ai_result_cache.sql (T3-L1). Manual, NU prin migrate.sh.
-- Oprește întâi cache-ul (settings['ai_cache.enabled']=false), altfel run_prompt() doar va loga
-- WARNING la fiecare apel (eșecul cache-ului nu oprește procesarea, dar umple logul).
--
--   psql ... -v ON_ERROR_STOP=1 -f migrations/down/20260929_ai_result_cache.down.sql
--   psql ... -c "DELETE FROM _release_migrations WHERE filename='20260929_ai_result_cache.sql'"

DROP TABLE IF EXISTS ai_cache_hit_log;
DROP TABLE IF EXISTS ai_result_cache;
DELETE FROM settings WHERE key IN ('ai_cache.enabled', 'ai_cache.prefixes', 'ai_cache.epoch',
                                   'ai_cache.ttl_days', 'ai_cache.last_purge_at');

SELECT 'migration 20260929_ai_result_cache reverted' AS status;
