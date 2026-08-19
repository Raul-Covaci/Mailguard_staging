-- Raport departamente (Mail-uri CTS) — istoricul mutarilor unui mail intre departamente.
--
-- PROBLEMA: `cts_ground_truth` pastreaza doar UN pas de istorie (cts_department_prev + changed_at),
-- deci un lant Suport 1 -> Contabilitate -> Taxe drum pierde departamentul intermediar. Aici tinem
-- un rand per EVENIMENT (alocare initiala + fiecare mutare observata), ca sa putem calcula:
--   1) de cate ori a fost mutat un mail pana la inchidere,
--   2) topul departamentelor care initiaza mutari,
--   3) topul departamentelor intermediare (nici primele alocate, nici cele care inchid).
--
-- Captura se face din trigger pe cts_ground_truth (orice cale de scriere — sync, backfill, manual),
-- nu din Python: sync-ul face upsert in lot si nu vedem tranzitiile decat in DB.
-- LIMITARE cunoscuta: sync-ul ruleaza la ~5 min, deci doua mutari intre doua sincronizari se vad
-- ca una singura. Preluarea completa a istoricului din CTS ramane pentru un task viitor.

BEGIN;

CREATE TABLE IF NOT EXISTS cts_department_moves (
    id              bigserial PRIMARY KEY,
    message_id      text,
    cts_ticket_id   bigint,
    email_id        bigint,
    from_department varchar(32),          -- NULL = alocarea initiala (intrarea mailului)
    to_department   varchar(32) NOT NULL,
    moved_at        timestamptz NOT NULL DEFAULT now(),
    detected_by     varchar(16) NOT NULL DEFAULT 'trigger',  -- 'trigger' | 'backfill'
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cts_dept_moves_msg    ON cts_department_moves (message_id);
CREATE INDEX IF NOT EXISTS idx_cts_dept_moves_at     ON cts_department_moves (moved_at);
CREATE INDEX IF NOT EXISTS idx_cts_dept_moves_from   ON cts_department_moves (from_department) WHERE from_department IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cts_dept_moves_to     ON cts_department_moves (to_department);
CREATE INDEX IF NOT EXISTS idx_cts_dept_moves_ticket ON cts_department_moves (cts_ticket_id) WHERE cts_ticket_id IS NOT NULL;
-- Deduplicare: acelasi tichet nu poate avea doua evenimente identice in aceeasi milisecunda.
CREATE UNIQUE INDEX IF NOT EXISTS uq_cts_dept_moves_event
    ON cts_department_moves (message_id, COALESCE(cts_ticket_id, -1), to_department, moved_at);

CREATE OR REPLACE FUNCTION cts_gt_track_department_move() RETURNS trigger AS $fn$
BEGIN
    -- Doar mailuri PRIMITE si nesterse: cele trimise de noi nu se incadreaza pe departament.
    IF COALESCE(NEW.cts_direction, 'received') <> 'received' OR NEW.cts_deleted_at IS NOT NULL THEN
        RETURN NULL;
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.cts_department IS NOT NULL THEN
            INSERT INTO cts_department_moves
                (message_id, cts_ticket_id, email_id, from_department, to_department, moved_at, detected_by)
            VALUES (NEW.message_id, NEW.cts_ticket_id, NEW.email_id, NULL, NEW.cts_department,
                    COALESCE(NEW.cts_assigned_at, now()), 'trigger')
            ON CONFLICT DO NOTHING;
        END IF;
    ELSIF NEW.cts_department IS NOT NULL
          AND NEW.cts_department IS DISTINCT FROM OLD.cts_department THEN
        INSERT INTO cts_department_moves
            (message_id, cts_ticket_id, email_id, from_department, to_department, moved_at, detected_by)
        VALUES (NEW.message_id, NEW.cts_ticket_id, NEW.email_id, OLD.cts_department, NEW.cts_department,
                now(), 'trigger')
        ON CONFLICT DO NOTHING;
    END IF;
    RETURN NULL;
END;
$fn$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_cts_gt_department_move') THEN
        CREATE TRIGGER trg_cts_gt_department_move
            AFTER INSERT OR UPDATE OF cts_department ON cts_ground_truth
            FOR EACH ROW EXECUTE PROCEDURE cts_gt_track_department_move();
    END IF;
END $$;

-- Backfill din datele existente (o singura data — doar cand tabela e goala):
--   * alocarea initiala = cts_department_prev daca exista, altfel cts_department;
--   * mutarea (singura pastrata) = prev -> current la changed_at.
INSERT INTO cts_department_moves
    (message_id, cts_ticket_id, email_id, from_department, to_department, moved_at, detected_by)
SELECT g.message_id, g.cts_ticket_id, g.email_id, NULL,
       COALESCE(g.cts_department_prev, g.cts_department),
       COALESCE(g.cts_assigned_at, g.fetched_at, now()), 'backfill'
  FROM cts_ground_truth g
 WHERE COALESCE(g.cts_direction, 'received') = 'received'
   AND g.cts_deleted_at IS NULL
   AND COALESCE(g.cts_department_prev, g.cts_department) IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM cts_department_moves)
ON CONFLICT DO NOTHING;

INSERT INTO cts_department_moves
    (message_id, cts_ticket_id, email_id, from_department, to_department, moved_at, detected_by)
SELECT g.message_id, g.cts_ticket_id, g.email_id, g.cts_department_prev, g.cts_department,
       COALESCE(g.changed_at, g.fetched_at, now()), 'backfill'
  FROM cts_ground_truth g
 WHERE COALESCE(g.cts_direction, 'received') = 'received'
   AND g.cts_deleted_at IS NULL
   AND g.cts_department IS NOT NULL
   AND g.cts_department_prev IS NOT NULL
   AND g.cts_department_prev IS DISTINCT FROM g.cts_department
   AND NOT EXISTS (SELECT 1 FROM cts_department_moves m WHERE m.detected_by = 'trigger')
ON CONFLICT DO NOTHING;

COMMIT;
