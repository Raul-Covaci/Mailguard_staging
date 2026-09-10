-- Reasigura coloanele `emails.ai_intent` / `ai_intent_at`.
--
-- Exista din `20260610_ai_intent.sql`, dar pe staging lipsesc din tabela desi migratia e
-- marcata aplicata in `_release_migrations` (baza a fost restaurata dintr-un dump anterior
-- migratiei, impreuna cu tabelul de evidenta — deci `migrate.sh` o sare la fiecare pornire).
-- Consecinta: `POST /emails/{id}/reprocess` cade cu UndefinedColumn pe UPDATE-ul care le
-- reseteaza (6 erori in error.log), deci butonul „Reproceseaza email" e complet nefunctional.
--
-- Un fisier NOU e singura cale de reparare prin fluxul normal: `migrate.sh` decide dupa numele
-- fisierului, deci re-rularea celui vechi nu se poate forta fara interventie manuala in DB.
-- Idempotent si aditiv — pe bazele unde coloanele exista deja nu face nimic.

ALTER TABLE emails ADD COLUMN IF NOT EXISTS ai_intent jsonb;
ALTER TABLE emails ADD COLUMN IF NOT EXISTS ai_intent_at timestamptz;
CREATE INDEX IF NOT EXISTS idx_emails_ai_intent ON emails USING gin(ai_intent);

SELECT 'migration 20260910_ai_intent_ensure applied' AS status;
