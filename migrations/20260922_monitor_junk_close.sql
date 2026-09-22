-- 2026-09-22 — INCHIDEREA RESTANTEI JUNK PE MONITOR (cutoff 01.09.2026)
--
-- CONTEXT. `restanta` de pe monitorul operational nu are limita de vechime (decizie 2026-09-10,
-- corecta in principiu: un mail din 02.09 ramas deschis trebuie sa se vada si pe 03.09). Efectul
-- secundar e ca tichetele pe care CTS le lasa deschise la nesfarsit — notificari automate,
-- tichete abandonate, mailuri pe care nu le mai lucreaza nimeni — se acumuleaza pe veci. Suport 1
-- ajunsese la 26 de restante raportate, din care colegii recunosteau ~4 ca fiind munca reala.
--
-- ⛔ DE CE O COLOANA PROPRIE SI NU UN `UPDATE ... SET cts_status='closed'`.
-- `cts_ground_truth` si `cts_task_ground_truth` sunt OGLINZI ale CTS. Upsert-ul de sync
-- suprascrie statusul la FIECARE rulare:
--     cts_groundtruth_sync.py  ->  cts_status = EXCLUDED.cts_status
--     cts_tasks_sync.py        ->  status     = EXCLUDED.status
-- iar `cts_tasks_sync._pending_task_anchor` re-interogheaza EXACT randurile deschise, pana la 60
-- de zile in urma (RECENT_MAX_BACKFILL_HOURS = 1440). Un UPDATE pe status s-ar anula singur la
-- urmatorul tick de 5 minute pentru task-uri, si inconsistent pentru mailuri (ancora lor e pe
-- „neclasificat", plafonata la 7 zile) — adica cel mai rau caz: jumatate se intorc, jumatate nu.
-- Marcajul nostru sta separat: oglinda ramane fidela CTS-ului, iar inchiderea e reversibila.
--
-- ⚠️ Randurile raman DESCHISE IN CTS. Aici nu scriem nimic spre ei. Inchiderea e „pentru monitor".

BEGIN;

ALTER TABLE cts_ground_truth
    ADD COLUMN IF NOT EXISTS monitor_closed_at     timestamptz,
    ADD COLUMN IF NOT EXISTS monitor_closed_reason text;

ALTER TABLE cts_task_ground_truth
    ADD COLUMN IF NOT EXISTS monitor_closed_at     timestamptz,
    ADD COLUMN IF NOT EXISTS monitor_closed_reason text;

-- Indexuri partiale pe fractia DESCHISA a tabelelor: predicatul e cel folosit de monitor si de
-- drill-down, iar `emails`/`cts_ground_truth` au milioane de randuri. Predicatele sunt IMMUTABLE
-- (lower/btrim/COALESCE), deci sunt permise intr-un index partial.
CREATE INDEX IF NOT EXISTS idx_cts_gt_monitor_open
    ON cts_ground_truth (cts_department)
 WHERE monitor_closed_at IS NULL
   AND cts_deleted_at IS NULL
   AND lower(btrim(COALESCE(cts_status,''))) NOT IN ('solved','closed');

CREATE INDEX IF NOT EXISTS idx_cts_task_monitor_open
    ON cts_task_ground_truth (department)
 WHERE monitor_closed_at IS NULL
   AND lower(btrim(COALESCE(status,''))) NOT IN ('solved','closed');

-- ── TAIEREA UNICA: tot ce e deschis si a INTRAT inainte de 01.09.2026 ───────────────────────
-- Data de sosire e aceeasi expresie ca in monitor (productivity._EMAIL_START_SQL): intai
-- `extra.email_date` din payload-ul CTS, apoi `emails.received_at`. Rezerva pe `fetched_at`
-- exista pentru randurile fara NICIO data — fara ea ar ramane pe veci in restanta (predicatul
-- monitorului e `IS DISTINCT FROM CURRENT_DATE`, deci NULL cade mereu la restanta), tocmai
-- randurile cele mai probabil junk.
UPDATE cts_ground_truth g
   SET monitor_closed_at     = now(),
       monitor_closed_reason = 'junk_cutoff_2026-09-01'
 WHERE g.monitor_closed_at IS NULL
   AND g.cts_deleted_at IS NULL
   AND lower(btrim(COALESCE(g.cts_status,''))) NOT IN ('solved','closed')
   AND COALESCE(
         CASE WHEN g.raw->'extra'->>'email_date' ~ '^\d{4}-\d\d-\d\d'
              THEN (g.raw->'extra'->>'email_date')::timestamp AT TIME ZONE 'UTC' END,
         (SELECT e.received_at FROM emails e WHERE e.id = g.email_id),
         g.fetched_at
       ) < TIMESTAMPTZ '2026-09-01 00:00:00+03';

-- Task-urile n-au coloana de stergere si n-au `cts_solved_at`; ancora de vechime e
-- `cts_created_at`, cu rezerva pe `first_synced_at` (NOT NULL, deci taierea e totala).
UPDATE cts_task_ground_truth t
   SET monitor_closed_at     = now(),
       monitor_closed_reason = 'junk_cutoff_2026-09-01'
 WHERE t.monitor_closed_at IS NULL
   AND lower(btrim(COALESCE(t.status,''))) NOT IN ('solved','closed')
   AND COALESCE(t.cts_created_at, t.first_synced_at) < TIMESTAMPTZ '2026-09-01 00:00:00+03';

DO $$
DECLARE m bigint; k bigint;
BEGIN
    SELECT count(*) INTO m FROM cts_ground_truth
     WHERE monitor_closed_reason = 'junk_cutoff_2026-09-01';
    SELECT count(*) INTO k FROM cts_task_ground_truth
     WHERE monitor_closed_reason = 'junk_cutoff_2026-09-01';
    RAISE NOTICE 'monitor junk cutoff 2026-09-01: % mailuri, % task-uri inchise pentru monitor', m, k;
    RAISE NOTICE 'anulare: DELETE /productivity/monitor/close-backlog?reason=junk_cutoff_2026-09-01';
END $$;

COMMIT;
