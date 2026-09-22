-- AKCENTA — expeditorul iese din blacklist si merge INTEGRAL spre CTS, dar aproape tot
-- marcat SOLVED. Cerere business, Raul Covaci, 2026-09-22. REVOCA blocarea totala decisa pe
-- 2026-09-11 (`20260911d_akcenta_block_spam.sql`), pastrand INTACTA regula generala
-- „blacklist-ul bate orice" din `sender_block` — Akcenta doar nu mai e pe blacklist.
--
-- Regula ceruta:
--   * subiect „Decontarea nr. ..." sau „Confirmarea nr. ..." -> flux NORMAL, tichet NEW in CTS
--     (sunt documente contabile reale, le lucreaza Contabilitatea);
--   * ORICE alt mail Akcenta -> tot pleaca spre CTS, dar marcat SOLVED (`mark_as_solved`), deci
--     nu intra in coada nimanui;
--   * NIMIC nu se mai opreste ca spam.
--
-- ⚠️ De ce whitelist SI stergere din blacklist, nu doar stergere: `cts_spam_sync` re-adauga in
-- blacklist (tip=spam) orice adresa aflata in lista CTS din IRIS, la fiecare sync. O valoare
-- prezenta in lista OPUSA e refuzata de `sender_lists.add_entry` ({"conflict": ...}), deci
-- intrarea de whitelist e ce impiedica Akcenta sa reapara blocat maine. Vezi pasul 4 pentru
-- limita ei: conflictul se verifica pe cheie EXACTA, nu pe domeniu.
--
-- ⚠️ Potrivirea pe domeniu prinde si subdomeniile de trimitere (`info@email.akcenta.eu`), cu
-- boundary strict: `evilakcenta.eu` NU se potriveste (vezi `spam_detector.sender_scopes`).
--
-- Aditiv + idempotent (re-rulabil fara efect).

-- 1) Blocklist de reputatie pe Akcenta (orice nivel) -> STERS. Atat timp cat randul exista,
--    `sender_block` blocheaza expeditorul indiferent de restul migratiei.
DELETE FROM spam_sender_reputation
 WHERE reputation = 'blocklist'
   AND (lower(btrim(scope_value)) = 'akcenta.eu'
     OR right(lower(btrim(scope_value)), 11) = '.akcenta.eu'
     OR split_part(lower(btrim(scope_value)), '@', 2) = 'akcenta.eu'
     OR right(split_part(lower(btrim(scope_value)), '@', 2), 11) = '.akcenta.eu');

-- 2) Allowlist pe domeniu: poarta de spam din `process_one` (`classify_spam_gate`) da
--    score 0 + override=FALSE, deci scoringul de continut (newsletter/bulk) nu mai poate opri
--    mailurile Akcenta. Fara randul asta, „nu trebuie oprit ca spam" ar tine doar pana la
--    primul mail de marketing cu destui markeri de bulk.
INSERT INTO spam_sender_reputation
  (scope_type, scope_value, reputation, created_by, last_action, action_count)
VALUES ('domain', 'akcenta.eu', 'allowlist', 'migration', 'legit', 1)
ON CONFLICT (scope_type, scope_value) DO UPDATE
   SET reputation = 'allowlist',
       last_action = 'legit',
       updated_at = NOW();

-- 3) Blacklist manual (jsonb, ORICE tip: carantina sau spam) -> se scot cheile Akcenta.
UPDATE settings
   SET value = jsonb_set(
         value, '{blacklist}',
         COALESCE((SELECT jsonb_object_agg(e.key, e.value)
                     FROM jsonb_each(value -> 'blacklist') AS e
                    WHERE lower(e.key) <> 'akcenta.eu'
                      AND right(lower(e.key), 11) <> '.akcenta.eu'
                      AND split_part(lower(e.key), '@', 2) <> 'akcenta.eu'
                      AND right(split_part(lower(e.key), '@', 2), 11) <> '.akcenta.eu'), '{}'::jsonb)),
       updated_at = NOW(),
       updated_by = 'migration'
 WHERE key = 'phishing_manual_learning'
   AND value ? 'blacklist'
   AND EXISTS (SELECT 1 FROM jsonb_each(value -> 'blacklist') AS e
                WHERE lower(e.key) = 'akcenta.eu'
                   OR right(lower(e.key), 11) = '.akcenta.eu'
                   OR split_part(lower(e.key), '@', 2) = 'akcenta.eu'
                   OR right(split_part(lower(e.key), '@', 2), 11) = '.akcenta.eu');

-- 4) Whitelist manual: suprima semnalele slabe de phishing (deci nici carantina) SI blocheaza
--    re-adaugarea in blacklist de catre `cts_spam_sync` — `sender_lists.add_entry` refuza tacut
--    o valoare aflata in lista OPUSA.
--    ⚠️ Conflictul din `add_entry` se verifica pe CHEIE EXACTA, nu pe domeniu: intrarea
--    'akcenta.eu' NU opreste adaugarea lui 'info@email.akcenta.eu' in blacklist. De aceea sunt
--    trecute si cele doua cutii cunoscute. Daca Akcenta incepe sa trimita de pe alta adresa si
--    aceea ajunge in lista de spam din CTS, sync-ul o poate re-bloca — se adauga aici.
--    Cheile existente NU se suprascriu (`noi || existente`), ca o nota/mute pus de om sa ramana.
UPDATE settings
   SET value = jsonb_set(
         COALESCE(value, '{}'::jsonb), '{whitelist}',
         (SELECT jsonb_object_agg(k, jsonb_build_object(
                    'by', 'migration', 'at', '2026-09-22T00:00:00+00:00',
                    'muted', false, 'source', 'manual',
                    'note', 'Akcenta: nu se mai opreste ca spam; auto-SOLVED spre CTS, exceptie Decontarea/Confirmarea nr.'))
            FROM unnest(ARRAY['akcenta.eu', 'info@akcenta.eu', 'info@email.akcenta.eu']) AS k)
         || COALESCE(value -> 'whitelist', '{}'::jsonb)),
       updated_at = NOW(),
       updated_by = 'migration'
 WHERE key = 'phishing_manual_learning'
   AND NOT (COALESCE(value -> 'whitelist', '{}'::jsonb)
            ?& ARRAY['akcenta.eu', 'info@akcenta.eu', 'info@email.akcenta.eu']);

-- 5) Regula auto-SOLVED spre CTS. `subject_contains` gol = orice subiect; `subject_not_contains`
--    e EXCEPTIA (cele doua tipologii care trebuie lucrate de om pleaca NEW).
--    Oglindeste `_DEFAULT_RULES` din `app/services/cts_auto_solved.py`.
UPDATE settings s
   SET value = s.value || '[{"senders":["@akcenta.eu"],"subject_contains":[],"subject_not_contains":["decontarea nr","confirmarea nr"]}]'::jsonb,
       updated_at = NOW(),
       updated_by = 'migration'
 WHERE s.key = 'cts.auto_solved_rules'
   AND jsonb_typeof(s.value) = 'array'
   AND NOT (s.value @> '[{"senders":["@akcenta.eu"]}]'::jsonb);

-- 6) Departament: Akcenta -> Contabilitate (regula determinista, `from` = substring pe
--    from_address + from_name, deci prinde si subdomeniile de trimitere).
UPDATE settings
   SET value = jsonb_set(
         value, '{rules}',
         COALESCE(value -> 'rules', '[]'::jsonb) || jsonb_build_array(jsonb_build_object(
           'id', 'akcenta-01', 'department', 'contabilitate',
           'from', 'akcenta.eu', 'subject', '', 'body', '', 'enabled', true,
           'note', 'akcenta.eu -> Contabilitate',
           'by', 'migration', 'at', '2026-09-22T00:00:00+00:00'))),
       updated_by = 'migration',
       updated_at = NOW()
 WHERE key = 'department_rules'
   AND NOT EXISTS (
         SELECT 1 FROM jsonb_array_elements(COALESCE(value -> 'rules', '[]'::jsonb)) r
          WHERE r ->> 'id' = 'akcenta-01');

-- 7) Retroactiv — mailurile oprite de migratia din 11.09, NEtrimise inca la CTS.
--    ⚠️ Cele DEJA plecate la CTS nu se ating (repunerea pe coada = tichet duplicat).
--    ⛔ Blocajele HARD (malware / macro / dubla extensie / impersonare domeniu intern) NU se
--       elibereaza in masa: un cont legitim poate fi compromis. Raman in carantina.

-- 7a) Carantina (fara blocaje hard) -> clean.
UPDATE emails e
   SET status = 'clean', review_decision = 'whitelist_release',
       reviewed_by = 'migration', reviewed_at = NOW(), needs_human_review = FALSE
 WHERE (split_part(lower(btrim(COALESCE(e.from_address, ''))), '@', 2) = 'akcenta.eu'
     OR right(split_part(lower(btrim(COALESCE(e.from_address, ''))), '@', 2), 11) = '.akcenta.eu')
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
   AND (split_part(lower(btrim(COALESCE(e.from_address, ''))), '@', 2) = 'akcenta.eu'
     OR right(split_part(lower(btrim(COALESCE(e.from_address, ''))), '@', 2), 11) = '.akcenta.eu')
   AND e.status = 'clean';

-- 7b) Iesirea din SPAM: override=FALSE explicit (randul ramane, cu scorul si istoricul lui).
--     Anuleaza exact pasul 4 din `20260911d_akcenta_block_spam.sql`.
INSERT INTO email_spam (email_id, spam_score, override, reviewed_by, reviewed_at)
SELECT e.id, 0, FALSE, 'migration', NOW()
  FROM emails e
 WHERE (split_part(lower(btrim(COALESCE(e.from_address, ''))), '@', 2) = 'akcenta.eu'
     OR right(split_part(lower(btrim(COALESCE(e.from_address, ''))), '@', 2), 11) = '.akcenta.eu')
   AND e.status NOT IN ('ndr', 'deleted', 'quarantined', 'quarantined_strict')
   AND e.sent_to_cts_at IS NULL
ON CONFLICT (email_id) DO UPDATE
   SET override = FALSE, reviewed_by = 'migration', reviewed_at = NOW();

-- 7c) Inapoi pe calea clean. `queued_general` + `manual_clean` = tick-ul de 5 min ruleaza DOAR
--     categoria (securitatea e deja decisa), deci nu pot cadea din nou in spam/carantina.
--     Anuleaza pasul 5 din `20260911d_akcenta_block_spam.sql`.
UPDATE emails
   SET queue_status = 'queued_general', manual_clean = TRUE,
       sent_to_cts_at = NULL, cts_send_error = NULL
 WHERE (split_part(lower(btrim(COALESCE(from_address, ''))), '@', 2) = 'akcenta.eu'
     OR right(split_part(lower(btrim(COALESCE(from_address, ''))), '@', 2), 11) = '.akcenta.eu')
   AND status = 'clean'
   AND sent_to_cts_at IS NULL
   AND COALESCE(queue_status, '') NOT IN ('ready_for_cts', 'sent', 'queued_general');

-- 7d) Departamentul, fixat direct (regula de la pasul 6 il da oricum la reclasificare; asta
--     acopera si mailurile care nu mai trec prin clasificator).
UPDATE emails
   SET ai_department = 'contabilitate',
       ai_department_at = NOW(),
       ai_department_result = COALESCE(ai_department_result, '{}'::jsonb) || jsonb_build_object(
         'department', 'contabilitate', 'confidence', 1.0, 'model', 'rule',
         'rule_id', 'akcenta-01',
         'reason', 'Regula: akcenta.eu -> Contabilitate')
 WHERE (split_part(lower(btrim(COALESCE(from_address, ''))), '@', 2) = 'akcenta.eu'
     OR right(split_part(lower(btrim(COALESCE(from_address, ''))), '@', 2), 11) = '.akcenta.eu')
   AND sent_to_cts_at IS NULL
   AND ai_department_manual IS NOT TRUE
   AND ai_department IS DISTINCT FROM 'contabilitate';

SELECT 'migration 20260922b_akcenta_unblock_auto_solved applied' AS status;
