-- T3-D1: limita de reluari pentru erorile AI tranzitorii din drain-ul de documente.
--
-- `retry_transient` iese fara rand in document_extractions, deci drain-ul (la fiecare tick de 5 min)
-- relua atasamentul complet — resegmentarea tuturor paginilor inclusiv — fara limita. Contorul si
-- momentul ultimei incercari stau pe atasament; le citeste doar codul din spatele flag-ului
-- settings['processing.doc_retry_limit_enabled'] (implicit OFF). Aditiv: cu flag-ul OFF nimic nu le
-- scrie si nimic nu le citeste.
--
-- Down: migrations/down/20260929b_doc_transient_attempts.down.sql (NU in migrations/, altfel
-- scripts/migrate.sh l-ar aplica automat).

ALTER TABLE attachments ADD COLUMN IF NOT EXISTS doc_transient_attempts integer NOT NULL DEFAULT 0;
ALTER TABLE attachments ADD COLUMN IF NOT EXISTS doc_transient_last_at timestamptz;

INSERT INTO settings (key, value, description, updated_by, updated_at) VALUES
  ('processing.doc_retry_limit_enabled', 'false'::jsonb,
   'T3-D1: max 3 reluari (debounce 10 min) pentru erorile AI tranzitorii din drain-ul de documente.',
   'migration', now()),
  ('processing.vision_image_normalize_enabled', 'false'::jsonb,
   'T3-D1: TIFF/BMP -> PNG si micsorare (4,5 MB / 2000 px) inainte de orice apel vision.',
   'migration', now())
ON CONFLICT (key) DO NOTHING;

SELECT 'migration 20260929b_doc_transient_attempts applied' AS status;
