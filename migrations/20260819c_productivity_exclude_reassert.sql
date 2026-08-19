-- Re-asertare a excluderilor din productivitate (pseudo-clienti / furnizori / placeholdere).
--
-- Sursa de adevar pentru „ce NU intra in calculul de productivitate" e `clients.productivity_exclude`
-- (vezi 20260804_productivity_exclude_and_email_start.sql). Migratia de fata:
--   1. re-ruleaza marcarea pe lista de nume — pe productie s-a constatat drift (migratii marcate
--      aplicate fara efect real, vezi 20260819b), deci flagul poate lipsi acolo;
--   2. adauga explicit „00-FIRMA NECUNOSCUTA LA MONTAJ" — placeholderul de montaj din operatiuni
--      (device_operations), cerut de business 2026-08-19: pe august 2026 intra cu 38 de randuri
--      masurabile in obiectivul „Operatiuni — Instalare noua" al Suport 2, desi nu e o firma.
--
-- Codul citeste flagul si pentru operatiuni de la aceasta versiune (app/services/productivity.py:
-- excluderea pe device_operations se face pe NUME, fiindca `device_operations.client_id` e NULL).
-- Idempotent: `WHERE NOT productivity_exclude` -> ruleaza doar pe randurile nemarcate.

UPDATE clients SET productivity_exclude = TRUE, updated_at = NOW()
WHERE NOT productivity_exclude
  AND (
       name ILIKE 'HU-GO%'                 -- sistem de taxare rutiera Ungaria
    OR name ILIKE 'LOCATOR BG%'            -- partener/integrator, nu client de suport
    OR name ILIKE 'RUPTELA%'               -- furnizor de dispozitive
    OR name ILIKE 'TOLL4EUROPE%'           -- sistem de taxare rutiera
    OR name ILIKE '00-FIRMA NECUNOSCUTA%'  -- placeholder de montaj (operatiuni), nu o firma
    OR name ILIKE 'ORANGE ROMANIA%'        -- furnizor telecom
    OR name ILIKE 'HELP DESK CTS%'         -- pseudo-client intern
    OR name ILIKE 'CTS INTERNAL%'          -- pseudo-client intern
  );

DO $$
DECLARE n INT;
BEGIN
    SELECT count(*) INTO n FROM clients WHERE productivity_exclude;
    RAISE NOTICE 'clienti exclusi din productivitate: %', n;
END $$;
