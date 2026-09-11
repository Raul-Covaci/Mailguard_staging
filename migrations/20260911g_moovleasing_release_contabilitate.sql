-- office@moovleasing.ro: mailurile blocate se elibereaza retroactiv si merg pe Contabilitate.
-- Cerere business, 2026-09-11.
--
-- De ce nu s-au reprocesat singure dupa adaugarea in whitelist: intrarea din
-- `settings.phishing_manual_learning.whitelist` schimba DOAR clasificarea VIITOARE. Pipeline-ul
-- ruleaza o singura data per email (la ingestie), deci mailurile deja oprite raman oprite —
-- nimic nu le reevalueaza. Butonul „Legit" din pagina Spam facea o eliberare retroactiva, dar
-- numai pentru SPAM si numai pentru expeditorul mailului pe care s-a dat click; carantina era
-- exclusa explicit (`status NOT IN ('quarantined','quarantined_strict',...)`). De aici „nu s-au
-- reprocesat toate": cele oprite ca spam au plecat, cele carantinate au ramas.
-- Reparat sistemic in `app/services/sender_release.py` (adaugarea in whitelist elibereaza acum
-- retroactiv, iar `POST /settings/sender-lists/reprocess` acopera intrarile puse inainte).
-- Migratia asta face acelasi lucru, o data, pentru expeditorul raportat.
--
-- ⛔ Mailurile blocate HARD nu se elibereaza: malware, executabil/macro/dubla extensie,
-- impersonare de domeniu intern. Un cont legitim poate fi compromis — whitelist-ul e o decizie
-- despre expeditor, nu despre un atasament infectat. Raman in carantina, se elibereaza individual.
-- ⚠️ Mailurile DEJA trimise la CTS nu se ating (repunerea pe coada = tichet duplicat in CTS).

-- 1) Regula de departament (store-ul nu se re-seedeaza din cod — vezi 20260911e).
UPDATE settings
   SET value = jsonb_set(
         value, '{rules}',
         COALESCE(value -> 'rules', '[]'::jsonb) || jsonb_build_array(jsonb_build_object(
           'id', 'moovleasing-01', 'department', 'contabilitate',
           'from', 'office@moovleasing.ro', 'subject', '', 'body', '', 'enabled', true,
           'note', 'office@moovleasing.ro -> Contabilitate',
           'by', 'migration', 'at', '2026-09-11T00:00:00+00:00'))),
       updated_by = 'migration',
       updated_at = NOW()
 WHERE key = 'department_rules'
   AND NOT EXISTS (
         SELECT 1 FROM jsonb_array_elements(COALESCE(value -> 'rules', '[]'::jsonb)) r
          WHERE r ->> 'id' = 'moovleasing-01');

-- 2) Eliberare din CARANTINA (fara blocajele hard).
UPDATE emails e
   SET status = 'clean', review_decision = 'whitelist_release',
       reviewed_by = 'migration', reviewed_at = NOW(), needs_human_review = FALSE
 WHERE lower(COALESCE(e.from_address, '')) = 'office@moovleasing.ro'
   AND e.status IN ('quarantined', 'quarantined_strict')
   AND e.sent_to_cts_at IS NULL
   AND NOT EXISTS (
         SELECT 1 FROM jsonb_array_elements(COALESCE(e.phishing_reasons, '[]'::jsonb)) r
          WHERE r ->> 'code' IN ('executable_attachment', 'macro_attachment',
                                 'double_extension', 'attachment_malware',
                                 'auth_spoof_internal_domain'));

UPDATE quarantine_strict q
   SET review_status = 'released', decision = 'whitelist_release',
       reviewed_by = 'migration', reviewed_at = NOW()
  FROM emails e
 WHERE q.email_id = e.id
   AND q.review_status = 'pending'
   AND lower(COALESCE(e.from_address, '')) = 'office@moovleasing.ro'
   AND e.status = 'clean';

-- 3) Scoaterea din SPAM (override=FALSE explicit; randul ramane, cu scorul lui).
INSERT INTO email_spam (email_id, spam_score, override, reviewed_by, reviewed_at)
SELECT e.id, 0, FALSE, 'migration', NOW()
  FROM emails e
 WHERE lower(COALESCE(e.from_address, '')) = 'office@moovleasing.ro'
   AND e.status NOT IN ('ndr', 'deleted', 'quarantined', 'quarantined_strict')
   AND e.sent_to_cts_at IS NULL
ON CONFLICT (email_id) DO UPDATE
   SET override = FALSE, reviewed_by = 'migration', reviewed_at = NOW();

-- 4) Inapoi pe calea clean: tick-ul de 5 min le duce prin categorie -> departament -> CTS.
UPDATE emails
   SET queue_status = 'queued_general', manual_clean = TRUE,
       sent_to_cts_at = NULL, cts_send_error = NULL
 WHERE lower(COALESCE(from_address, '')) = 'office@moovleasing.ro'
   AND status = 'clean'
   AND sent_to_cts_at IS NULL
   AND COALESCE(queue_status, '') NOT IN ('ready_for_cts', 'sent', 'queued_general');

-- 5) Departamentul, fixat direct (regula de la pasul 1 il da oricum la reclasificare; asta
--    acopera si mailurile care nu mai trec prin clasificator).
UPDATE emails
   SET ai_department = 'contabilitate',
       ai_department_at = NOW(),
       ai_department_result = COALESCE(ai_department_result, '{}'::jsonb) || jsonb_build_object(
         'department', 'contabilitate', 'confidence', 1.0, 'model', 'rule',
         'rule_id', 'moovleasing-01',
         'reason', 'Regula: office@moovleasing.ro -> Contabilitate')
 WHERE lower(COALESCE(from_address, '')) = 'office@moovleasing.ro'
   AND sent_to_cts_at IS NULL
   AND ai_department_manual IS NOT TRUE
   AND ai_department IS DISTINCT FROM 'contabilitate';

SELECT 'migration 20260911g_moovleasing_release_contabilitate applied' AS status;
