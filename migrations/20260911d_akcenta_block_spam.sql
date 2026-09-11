-- Akcenta nu mai ajunge in CTS: expeditorii @akcenta.eu (inclusiv subdomeniile de trimitere,
-- ex. info@email.akcenta.eu) sunt oprite ca SPAM. Cerere business, 2026-09-11.
--
-- De ce nu era suficienta intrarea de blocklist existenta — trei cauze, toate reparate impreuna
-- cu aceasta migratie:
--   1. Potrivirea pe domeniu era pe EGALITATE exacta. O regula pe `akcenta.eu` nu prindea
--      `info@email.akcenta.eu`, iar una pe adresa exacta nu prindea alte cutii de pe acelasi
--      domeniu. Reparat in `spam_detector.sender_scopes` (domeniu + domenii-parinte).
--   2. Whitelist-ul manual BATE blocklist-ul (regula de precedenta, nemodificata). Daca cineva a
--      dat candva „Legit" pe un mail Akcenta, expeditorul a intrat in
--      `settings.phishing_manual_learning.whitelist` si de acolo excepta TOATE mailurile lui,
--      oricat de explicit ar fi fost pus pe blocklist. Intrarile Akcenta se sterg mai jos.
--   3. Gate-ul „Automat" (report_patterns) iese din `process_one` INAINTE de poarta de spam, iar
--      `auto_report/auto_closed` e livrat la CTS. Mailurile sablon care prindeau un pattern
--      treceau, restul erau oprite — exact „unele tot trec". Reparat in `process_email.py`
--      (verdictul pe expeditor se calculeaza inaintea gate-ului).
--
-- Retroactiv se ating DOAR mailurile NEtrimise inca la CTS (`sent_to_cts_at IS NULL`); ce a plecat
-- deja ramane cum e (nu se poate retrage din CTS).

-- 1) Blocklist pe domeniu (prinde si subdomeniile, dupa fix-ul din spam_detector).
INSERT INTO spam_sender_reputation
  (scope_type, scope_value, reputation, created_by, last_action, action_count)
VALUES ('domain', 'akcenta.eu', 'blocklist', 'migration', 'mark_spam', 1)
ON CONFLICT (scope_type, scope_value) DO UPDATE
   SET reputation = 'blocklist',
       last_action = 'mark_spam',
       updated_at = NOW();

-- 2) Orice allowlist Akcenta ramasa ar bate blocklist-ul pe adresa exacta -> se sterge.
DELETE FROM spam_sender_reputation
 WHERE reputation = 'allowlist'
   AND right(lower(scope_value), 10) = 'akcenta.eu';

-- 3) Whitelist manual (jsonb) — se scot cheile Akcenta (adresa sau domeniu). Whitelist-ul bate
--    blocklist-ul, deci o intrare ramasa aici ar anula tot restul migratiei.
UPDATE settings
   SET value = jsonb_set(
         value, '{whitelist}',
         COALESCE((SELECT jsonb_object_agg(e.key, e.value)
                     FROM jsonb_each(value -> 'whitelist') AS e
                    WHERE right(lower(e.key), 10) <> 'akcenta.eu'), '{}'::jsonb)),
       updated_at = NOW(),
       updated_by = 'migration'
 WHERE key = 'phishing_manual_learning'
   AND value ? 'whitelist'
   AND EXISTS (SELECT 1 FROM jsonb_each(value -> 'whitelist') AS e
                WHERE right(lower(e.key), 10) = 'akcenta.eu');

-- 4) Retroactiv: mailurile Akcenta inca netrimise la CTS -> spam fortat (override=TRUE).
INSERT INTO email_spam (email_id, spam_score, override, reviewed_by, reviewed_at)
SELECT e.id, 0, TRUE, 'migration', NOW()
  FROM emails e
 WHERE right(lower(e.from_address), 10) = 'akcenta.eu'
   AND e.sent_to_cts_at IS NULL
ON CONFLICT (email_id) DO UPDATE
   SET override = TRUE, reviewed_by = 'migration', reviewed_at = NOW();

-- 5) Si scoase de pe calea spre CTS (terminal). Starile imuabile de securitate raman neatinse.
UPDATE emails
   SET queue_status = 'stopped_spam', manual_clean = FALSE
 WHERE right(lower(from_address), 10) = 'akcenta.eu'
   AND sent_to_cts_at IS NULL
   AND status NOT IN ('quarantined', 'quarantined_strict', 'ndr', 'deleted')
   AND queue_status IS DISTINCT FROM 'stopped_spam';

SELECT 'migration 20260911d_akcenta_block_spam applied' AS status;
