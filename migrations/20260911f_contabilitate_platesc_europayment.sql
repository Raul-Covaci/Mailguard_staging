-- Procesatorii de plati online merg pe Contabilitate (cerere business, 2026-09-11):
--   euPlatesc (noreply@euplatesc.*) si europayment.services (notificari@, contact@, noreply@,
--   suport@).
--
-- Ca la orice regula de departament: store-ul `settings.department_rules` se seedeaza din
-- `department_rules.DEFAULT_RULES` o singura data, deci o regula noua adaugata in cod nu ajunge
-- in DB pe o instalare existenta — trebuie inserata aici.
--
-- Ambele reguli sunt pe DOMENIU, nu pe adresa exacta:
--   * `euplatesc`  — adresa exacta a fost data cu o mica greseala de tipar („noreplay@"), iar
--     notificarile lor pleaca oricum de pe mai multe cutii; potrivirea pe numele de domeniu le
--     prinde pe toate (substring in from_address + from_name, fara diacritice, case-insensitive).
--   * `@europayment.services` — acopera cele 4 cutii cerute si orice alta cutie a aceluiasi
--     expeditor. Prefixul `@` tine potrivirea pe partea de domeniu.
-- Se adauga la COADA listei: nicio alta regula existenta nu potriveste acesti expeditori, deci
-- ordinea nu conteaza aici (spre deosebire de `noreply@cargotrack.ro`, care trebuia sa bata o
-- regula pe subiect).

UPDATE settings
   SET value = jsonb_set(
         value, '{rules}',
         COALESCE(value -> 'rules', '[]'::jsonb) || jsonb_build_array(
           jsonb_build_object(
             'id', 'euplatesc-01', 'department', 'contabilitate', 'from', 'euplatesc',
             'subject', '', 'body', '', 'enabled', true,
             'note', 'euPlatesc (orice adresa) -> Contabilitate',
             'by', 'migration', 'at', '2026-09-11T00:00:00+00:00'),
           jsonb_build_object(
             'id', 'europayment-services-01', 'department', 'contabilitate',
             'from', '@europayment.services',
             'subject', '', 'body', '', 'enabled', true,
             'note', 'europayment.services (orice adresa) -> Contabilitate',
             'by', 'migration', 'at', '2026-09-11T00:00:00+00:00'))),
       updated_by = 'migration',
       updated_at = NOW()
 WHERE key = 'department_rules'
   AND NOT EXISTS (
         SELECT 1 FROM jsonb_array_elements(COALESCE(value -> 'rules', '[]'::jsonb)) r
          WHERE r ->> 'id' IN ('euplatesc-01', 'europayment-services-01'));

-- Retroactiv — doar mailurile inca netrimise la CTS (ce a plecat ramane sincron cu tichetul din
-- CTS) si fara corectie manuala de departament.
UPDATE emails
   SET ai_department = 'contabilitate',
       ai_department_at = NOW(),
       ai_department_result = COALESCE(ai_department_result, '{}'::jsonb) || jsonb_build_object(
         'department', 'contabilitate', 'confidence', 1.0, 'model', 'rule',
         'reason', 'Regula: procesator de plati online -> Contabilitate')
 WHERE (position('euplatesc' in lower(COALESCE(from_address, ''))) > 0
        OR position('@europayment.services' in lower(COALESCE(from_address, ''))) > 0)
   AND sent_to_cts_at IS NULL
   AND ai_department_manual IS NOT TRUE
   AND ai_department IS DISTINCT FROM 'contabilitate';

SELECT 'migration 20260911f_contabilitate_platesc_europayment applied' AS status;
