-- T3-S1: apelurile telefonice cu transcript prea scurt nu se mai scorează.
--
-- Sub pragul settings['calls.score_min_transcript_chars'] (0 = oprit, implicit) nu se face niciun
-- apel AI; apelul primește un rând în call_ai_scores cu skip_reason='too_short' și fără scoruri, ca
-- score_batch / rescore_null / rescore-missing-binary să nu-l mai reselecteze. Agregările din
-- calls_analytics exclud rândurile cu skip_reason (nu le numără ca zero). Aditiv: cu pragul 0 coloana
-- rămâne NULL peste tot.
--
-- Down: migrations/down/20260929d_call_score_min_transcript.down.sql (NU în migrations/).

ALTER TABLE call_ai_scores ADD COLUMN IF NOT EXISTS skip_reason varchar(40);

INSERT INTO settings (key, value, description, updated_by, updated_at) VALUES
  ('calls.score_min_transcript_chars', '0'::jsonb,
   'T3-S1: sub atâtea caractere de transcript apelul nu se scorează (0 = oprit).', 'migration', now())
ON CONFLICT (key) DO NOTHING;

SELECT 'migration 20260929d_call_score_min_transcript applied' AS status;
