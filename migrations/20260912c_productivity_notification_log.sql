-- Anti-duplicat DUR pentru rapoartele lunare de productivitate. Incident 2026-09: peste 250 de
-- mailuri „Rezumat productivitate August 2026" in aceeasi casuta, trimise la fiecare rulare de
-- cron (5 min).
--
-- De ce nu erau suficiente garzile existente: TOATE trei (ziua/ora, eticheta de luna
-- `productivity.last_monthly_sent`, momentul `..._sent_at`) se scriu DUPA trimitere, prin
-- `_mark_sent`, si numai daca `sent > 0`. Orice eroare care apare DUPA ce SMTP a acceptat mesajul
-- — inchiderea conexiunii SMTP, o exceptie la scrierea marcajului, o tranzactie abortata — lasa
-- destinatarul cu mailul primit si aplicatia convinsa ca nu a trimis nimic. La urmatorul tick se
-- reia. Acelasi tipar a produs si incidentul din 03.08.2026 (5 duplicate), reparat atunci doar
-- partial: s-a tratat o cauza punctuala, nu ordinea „marcheaza dupa trimitere".
--
-- Tabela asta inverseaza ordinea: randul se REZERVA inainte de trimitere, cu o cheie unica pe
-- (luna, grup, destinatar). A doua rulare nu mai poate insera randul, deci nu mai trimite —
-- indiferent ce se strica dupa. Un esec ramane vizibil (`status='failed'`), dar NU se reia
-- automat: preferam un mail lipsa, retrimis manual din UI, decat 250 de duplicate.

CREATE TABLE IF NOT EXISTS productivity_notification_log (
    id                bigserial PRIMARY KEY,
    month_key         varchar(7)   NOT NULL,        -- luna RAPORTATA (cea precedenta), 'YYYY-MM'
    department_group  varchar(64)  NOT NULL,
    recipient_email   varchar(320) NOT NULL,
    status            varchar(16)  NOT NULL DEFAULT 'claimed',  -- claimed | sent | failed
    error             text,
    claimed_at        timestamptz  NOT NULL DEFAULT now(),
    sent_at           timestamptz,
    claimed_by        varchar(32)                                -- cron | manual
);

-- Cheia anti-duplicat. Un (luna, grup, destinatar) primeste raportul O SINGURA DATA.
CREATE UNIQUE INDEX IF NOT EXISTS prod_notif_log_uidx
    ON productivity_notification_log (month_key, department_group, lower(recipient_email));
CREATE INDEX IF NOT EXISTS prod_notif_log_month_idx
    ON productivity_notification_log (month_key, status);

COMMENT ON TABLE productivity_notification_log IS
  'Evidenta trimiterilor lunare de productivitate. Randul se rezerva INAINTE de trimitere; unicitatea pe (luna, grup, destinatar) e singura protectie reala anti-duplicat.';

-- Seed defensiv: daca `productivity.last_monthly_sent` spune ca luna precedenta a fost deja
-- trimisa, inregistram destinatarii activi ca „sent", ca un deploy la mijlocul lunii sa nu poata
-- declansa o retrimitere. Fara date tranzactionale noi — doar marcajul a ceea ce a plecat deja.
INSERT INTO productivity_notification_log
    (month_key, department_group, recipient_email, status, claimed_by, sent_at)
SELECT to_char(date_trunc('month', now()) - interval '1 month', 'YYYY-MM'),
       n.department_group, n.email, 'sent', 'migration', now()
  FROM productivity_notifications n
 WHERE n.enabled = true
   AND EXISTS (SELECT 1 FROM settings s
                WHERE s.key = 'productivity.last_monthly_sent'
                  AND (s.value #>> '{}') = to_char(now(), 'YYYY-MM'))
ON CONFLICT DO NOTHING;

SELECT 'migration 20260912c_productivity_notification_log applied' AS status;
