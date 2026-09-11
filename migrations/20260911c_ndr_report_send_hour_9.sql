-- Raportul zilnic Undeliverable (NDR) pleaca spre office la ora 09:00 in loc de 10:00
-- (Europe/Bucharest). Cerere business, 2026-09-11.
--
-- Ora e citita din `settings.ndr_report.send_hour` de `run_daily_ndr_report_if_due()`
-- (app/services/ndr_report.py); implicitul din cod a trecut si el de la 10 la 9. Daca randul
-- exista deja in DB, el BATE implicitul din cod, deci schimbarea trebuie facuta si aici —
-- altfel pe serverul unde cineva a salvat cindva 10, raportul ar continua sa plece la 10.
--
-- Se suprascrie DOAR valoarea veche (10), ca o eventuala ora setata manual ulterior sa nu
-- fie calcata la o re-rulare a migratiei. Idempotent: a doua rulare nu mai gaseste 10.
--
-- Gate-ul e "dupa ora X", rulat de cron-ul de 5 minute; raportul ramane pentru ZIUA DE IERI
-- si pastreaza marker-ul de idempotenta `ndr_report.last_report` (o singura trimitere/zi).

INSERT INTO settings (key, value, description, updated_by, updated_at)
VALUES ('ndr_report.send_hour', '9'::jsonb,
        'Ora (Europe/Bucharest) dupa care pleaca raportul zilnic Undeliverable',
        'migration', NOW())
ON CONFLICT (key) DO UPDATE
   SET value = '9'::jsonb,
       updated_by = 'migration',
       updated_at = NOW()
 WHERE settings.value IN ('10'::jsonb, '"10"'::jsonb);

SELECT 'migration 20260911c_ndr_report_send_hour_9 applied' AS status;
