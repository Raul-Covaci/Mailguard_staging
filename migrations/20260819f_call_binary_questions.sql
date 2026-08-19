-- 2026-08-19: intrebarile binare de scoring apeluri — set final, decis de business.
--
-- Context. `score_call()` citea deja patru chei binare (`agentulSaPrezentat`,
-- `clientulAmintaJudecata`, `clientulAmintaRenuntare`, `clientulContactatAnterior`), dar ele nu
-- existau nici in SEED_PROMPTS, nici ca fisier in app/services/prompts/calls/ — deci nu rulau
-- niciodata si coloanele adaugate de 20260724_call_ai_scores_binary_kpis.sql au ramas NULL.
-- Cardurile „Analiza AI - intrebari binare" afisau in schimb `issueResolution` (marcat gresit
-- ca binar, desi are patru campuri) plus trei indicatori derivati hardcodati in cod.
--
-- Setul cerut (5 intrebari, fiecare cu prompt propriu versionat in repo):
--   masiniCareNuTransmit, clientulAmintaJudecata, agentulSaPrezentat,
--   clientulAmintaRenuntare, clientulContactatAnterior
--
-- Textele prompturilor NU se seedeaza de aici: sursa de adevar sunt fisierele din
-- app/services/prompts/calls/, urcate cu scripts/sync_call_prompts.py (vezi CLAUDE.md).
-- Migratia pregateste doar schema si corecteaza `output_type` acolo unde e gresit.

-- 1. Coloana pentru intrebarea noua + justificarile modelului pentru toate intrebarile binare.
--    `binary_evidence` face inutila o migratie la fiecare intrebare binara viitoare: o cheie
--    fara coloana proprie ramane oricum interogabila din jsonb.
ALTER TABLE call_ai_scores
  ADD COLUMN IF NOT EXISTS masini_care_nu_transmit boolean,
  ADD COLUMN IF NOT EXISTS binary_evidence         jsonb;

COMMENT ON COLUMN call_ai_scores.binary_evidence IS
  'Rezultatul brut al intrebarilor binare: {cheiePrompt: {result: bool, evidence: text}}.';

-- 2. `issueResolution` iese dintre intrebarile binare (ramane sursa pentru % Rezolvate si
--    pentru problema/solutia din panoul de apel, dar nu mai produce card donut).
UPDATE call_scoring_prompts
   SET output_type = 'json', updated_at = NOW()
 WHERE key = 'issueResolution' AND output_type = 'binary';

-- 3. Cheile binare deja existente in DB (inserate cu output_type implicit la un sync anterior)
--    primesc tipul corect; cele lipsa sunt inserate de sync-ul din repo, cu tipul din
--    SEED_PROMPTS. Fara pasul asta, o cheie inserata gresit nu ar aparea printre carduri.
UPDATE call_scoring_prompts
   SET output_type = 'binary', updated_at = NOW()
 WHERE key IN ('masiniCareNuTransmit', 'clientulAmintaJudecata', 'agentulSaPrezentat',
               'clientulAmintaRenuntare', 'clientulContactatAnterior')
   AND output_type IS DISTINCT FROM 'binary';

DO $$
DECLARE n INT;
BEGIN
    SELECT count(*) INTO n FROM call_scoring_prompts WHERE output_type = 'binary' AND enabled;
    RAISE NOTICE 'intrebari binare active: % (asteptat 5 dupa sync_call_prompts.py)', n;
END $$;
