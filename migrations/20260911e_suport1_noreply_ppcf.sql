-- Doua rutari obligatorii pe Suport 1 (cerere business, 2026-09-11):
--   1. orice mail de pe `noreply@cargotrack.ro`;
--   2. platile cu seria PPCF (ordine de plata).
--
-- CONTEXT PPCF (fix de cod, nu de date): `PPCF` era deja in allowlist-ul de serii acreditate
-- (`op_extractor._KNOWN_SERIE_PREFIXES`), dar NU si in `_SUPORT1_PREFIXES` — iar
-- `_department_from_series()` trimite orice serie acreditata care nu e in setul Suport 1 la
-- Contabilitate. Deci regula „PPCF -> Suport 1" nu exista nicaieri; seria se extragea corect,
-- dar se ruta gresit. Reparat in cod; aici doar corectia retroactiva de mai jos.
--
-- CONTEXT noreply: regulile de departament stau in `settings.department_rules` si se seedeaza
-- din `department_rules.DEFAULT_RULES` O SINGURA DATA, la prima citire, daca store-ul lipseste.
-- Pe orice instalare care are deja store-ul, o regula noua adaugata in cod NU ajunge niciodata
-- in DB — de aceea e nevoie de aceasta migratie.
--
-- Regula se pune PRIMA in lista: `department_rules.match()` sorteaza dupa numarul de criterii
-- (mai specific intai) si, la egalitate, pastreaza ordinea din store. Fara asta, un mail de pe
-- noreply@ cu subiect "Tranzactii zilnice" ar fi plecat la Contabilitate (tot o regula cu un
-- singur criteriu, aflata mai sus in store).

-- 1) Regula de departament (idempotent pe id).
UPDATE settings
   SET value = jsonb_set(
         value, '{rules}',
         jsonb_build_array(jsonb_build_object(
           'id', 'noreply-cargotrack-01',
           'department', 'suport_1',
           'from', 'noreply@cargotrack.ro',
           'subject', '',
           'body', '',
           'enabled', true,
           'note', 'noreply@cargotrack.ro -> Suport 1',
           'by', 'migration',
           'at', '2026-09-11T00:00:00+00:00'
         )) || COALESCE(value -> 'rules', '[]'::jsonb)),
       updated_by = 'migration',
       updated_at = NOW()
 WHERE key = 'department_rules'
   AND NOT EXISTS (
         SELECT 1 FROM jsonb_array_elements(COALESCE(value -> 'rules', '[]'::jsonb)) r
          WHERE r ->> 'id' = 'noreply-cargotrack-01');

-- 2) Retroactiv — DOAR mailurile inca netrimise la CTS (ce a plecat deja ramane cum a plecat,
--    altfel departamentul din Cargo360 ar diverge de tichetul din CTS) si fara corectii manuale.
UPDATE emails
   SET ai_department = 'suport_1',
       ai_department_at = NOW(),
       ai_department_result = COALESCE(ai_department_result, '{}'::jsonb) || jsonb_build_object(
         'department', 'suport_1', 'confidence', 1.0, 'model', 'rule',
         'rule_id', 'noreply-cargotrack-01',
         'reason', 'Regula: noreply@cargotrack.ro -> Suport 1')
 WHERE lower(from_address) = 'noreply@cargotrack.ro'
   AND sent_to_cts_at IS NULL
   AND ai_department_manual IS NOT TRUE
   AND ai_department IS DISTINCT FROM 'suport_1';

UPDATE emails
   SET ai_department = 'suport_1',
       ai_department_at = NOW(),
       ai_department_result = COALESCE(ai_department_result, '{}'::jsonb) || jsonb_build_object(
         'department', 'suport_1', 'confidence', 1.0, 'model', 'rule',
         'reason', 'Serie OP PPCF -> Suport 1')
 WHERE upper(COALESCE(ai_op_series, '')) = 'PPCF'
   AND sent_to_cts_at IS NULL
   AND ai_department_manual IS NOT TRUE
   AND ai_department IS DISTINCT FROM 'suport_1';

SELECT 'migration 20260911e_suport1_noreply_ppcf applied' AS status;
