-- DOWN pentru migrations/20260929d_call_score_min_transcript.sql (T3-S1). Manual, NU prin migrate.sh.
-- Pune întâi pragul pe 0. Rândurile marcate too_short (fără scoruri) se șterg, altfel ar apărea ca
-- apeluri scorate cu toate câmpurile goale; la următorul tick se scorează normal.
--
--   psql ... -v ON_ERROR_STOP=1 -f migrations/down/20260929d_call_score_min_transcript.down.sql
--   psql ... -c "DELETE FROM _release_migrations WHERE filename='20260929d_call_score_min_transcript.sql'"

DELETE FROM call_ai_scores WHERE skip_reason IS NOT NULL;
ALTER TABLE call_ai_scores DROP COLUMN IF EXISTS skip_reason;
DELETE FROM settings WHERE key = 'calls.score_min_transcript_chars';

SELECT 'migration 20260929d_call_score_min_transcript reverted' AS status;
