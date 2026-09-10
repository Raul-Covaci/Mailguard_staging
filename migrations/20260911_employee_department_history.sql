-- 2026-09-11: Istoricul apartenentei unui angajat la departamente (efectiv-datat pe LUNA).
--
-- PROBLEMA: `employee_department_mapping.department` e un scalar mutabil, iar TOATE interogarile de
-- productivitate rezolva departamentul "acum". Cand cineva e promovat (Ticus Ovidiu Alexandru:
-- suport_2 pana in august 2026, suport_3 din septembrie), tot istoricul lui — mailuri, task-uri,
-- apeluri, reclamatii — se re-atribuie retroactiv noului departament: lunile deja raportate ale
-- Suport 2 pierd volum, iar Suport 3 primeste munca pe care n-a facut-o. Acelasi efect il produce
-- sync-ul zilnic IRIS, care face UPDATE ... SET department=... in loc (iris_employee_sync.py).
--
-- SOLUTIA: un rand per interval (angajat, departament, [valid_from, valid_to)), cu granularitate
-- LUNA (intervalele incep pe ziua 1, valid_to exclusiv). Apartenenta la luna M:
--     valid_from <= last_day(M) AND (valid_to IS NULL OR valid_to > first_day(M))
-- Captura automata se face din TRIGGER, nu din Python — la fel ca la `cts_department_moves`
-- (migrations/20260819_cts_department_moves.sql): sync-ul scrie in lot si tranzitiile nu se vad
-- decat in DB. Corectiile/backdatarea se fac manual din UI (Utilizatori -> modalul angajatului).
--
-- `employee_department_mapping.department` RAMANE sursa de adevar pentru "acum" (dashboard-uri,
-- monitor live, clasificare, atribuire). Istoricul il oglindeste, nu il inlocuieste. Invariant:
-- pentru un angajat enabled, intervalul deschis are acelasi departament ca scalarul.
--
-- LIMITARE asumata: granularitate LUNA. Doua mutari in aceeasi luna se colapseaza la ultima.
--
-- Idempotent + aditiv.

BEGIN;

CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE IF NOT EXISTS employee_department_history (
    id          bigserial PRIMARY KEY,
    employee_id integer NOT NULL REFERENCES employee_department_mapping(id) ON DELETE CASCADE,
    department  text NOT NULL,      -- slug; NU se valideaza pe whitelist (slugurile vechi raman citibile)
    valid_from  date NOT NULL,      -- ziua 1 a primei luni IN departament
    valid_to    date,               -- ziua 1 a primei luni IN AFARA (exclusiv); NULL = interval deschis
    source      text NOT NULL DEFAULT 'trigger',   -- 'seed' | 'trigger' | 'backfill' | 'manual'
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text
);

CREATE INDEX IF NOT EXISTS employee_dept_hist_emp_idx  ON employee_department_history (employee_id, valid_from);
CREATE INDEX IF NOT EXISTS employee_dept_hist_dept_idx ON employee_department_history (department, valid_from, valid_to);

-- Cel mult UN interval deschis per angajat — invariantul pe care se bazeaza trigger-ul.
CREATE UNIQUE INDEX IF NOT EXISTS employee_dept_hist_open_uidx
    ON employee_department_history (employee_id) WHERE valid_to IS NULL;

-- O singura schimbare per luna per angajat (granularitatea deciziei de business).
CREATE UNIQUE INDEX IF NOT EXISTS employee_dept_hist_start_uidx
    ON employee_department_history (employee_id, valid_from);

-- Granularitate luna: ambele capete cad pe ziua 1, iar intervalul e nevid.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'employee_dept_hist_month_chk'
                      AND conrelid = 'employee_department_history'::regclass) THEN
        ALTER TABLE employee_department_history ADD CONSTRAINT employee_dept_hist_month_chk CHECK (
            extract(day from valid_from) = 1
            AND (valid_to IS NULL OR (extract(day from valid_to) = 1 AND valid_to > valid_from))
        );
    END IF;
END $$;

-- Suprapunerile sunt imposibile la nivel de DB, nu doar prin cod: un angajat prezent simultan in
-- doua departamente si-ar dubla volumul la nivel de firma.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'employee_dept_hist_no_overlap'
                      AND conrelid = 'employee_department_history'::regclass) THEN
        ALTER TABLE employee_department_history
            ADD CONSTRAINT employee_dept_hist_no_overlap EXCLUDE USING gist (
                employee_id WITH =,
                daterange(valid_from, valid_to, '[)') WITH &&
            );
    END IF;
END $$;

-- ── Seed: un interval per angajat existent, cu departamentul de azi ──────────────────────────
-- valid_from = 2000-01-01 (santinela, cu mult inaintea oricarei date ingerate): migratia trebuie
-- sa fie un NO-OP comportamental. Cu orice alt start, prima luna istorica ar ramane fara membri
-- si rapoartele vechi ar iesi goale.
-- Angajatii deja dezactivati primesc interval INCHIS la inceputul lunii curente: lunile lor
-- trecute intra in rapoarte (exact corectia dorita), dar nu reapar in rosterul lunii curente si
-- nu umfla ore_planificate. Luna exacta a plecarii se corecteaza din UI sau din backfill.
INSERT INTO employee_department_history (employee_id, department, valid_from, valid_to, source, created_by, note)
SELECT e.id, e.department, date '2000-01-01',
       CASE WHEN e.enabled THEN NULL ELSE date_trunc('month', CURRENT_DATE)::date END,
       'seed', 'migration', 'seed migrare — departamentul curent'
  FROM employee_department_mapping e
 WHERE NOT EXISTS (SELECT 1 FROM employee_department_history h WHERE h.employee_id = e.id);

-- ── Rezolvare: apartenenta pe luna (folosita in filtre) ──────────────────────────────────────
CREATE OR REPLACE FUNCTION employee_dept_members(p_dept text, p_month_start date)
RETURNS TABLE (employee_id integer) LANGUAGE sql STABLE AS $fn$
    SELECT h.employee_id
      FROM employee_department_history h
     WHERE h.department = p_dept
       AND h.valid_from <= (date_trunc('month', p_month_start) + interval '1 month - 1 day')::date
       AND (h.valid_to IS NULL OR h.valid_to > date_trunc('month', p_month_start)::date)
$fn$;

-- ── Rezolvare: departamentul unui angajat la o zi (etichetare per rand) ──────────────────────
CREATE OR REPLACE FUNCTION employee_dept_at(p_emp integer, p_day date)
RETURNS text LANGUAGE sql STABLE AS $fn$
    SELECT h.department
      FROM employee_department_history h
     WHERE h.employee_id = p_emp
       AND h.valid_from <= p_day
       AND (h.valid_to IS NULL OR h.valid_to > p_day)
     ORDER BY h.valid_from DESC
     LIMIT 1
$fn$;

-- ── Trigger: captura schimbarilor de departament si a plecarilor ─────────────────────────────
-- MUTARE: efectiva de la ziua 1 a lunii IN CURS — o promovare pe 15 septembrie duce toata luna
-- septembrie la noul departament (granularitate luna). Corectia se face din UI.
-- PLECARE: intervalul se inchide la ziua 1 a lunii URMATOARE, nu a celei curente — omul chiar a
-- lucrat in luna curenta, iar volumul lui nu are alt departament in care sa cada.
CREATE OR REPLACE FUNCTION edm_track_department_history() RETURNS trigger AS $fn$
DECLARE
    m_cur  date := date_trunc('month', CURRENT_DATE)::date;
    m_next date := (date_trunc('month', CURRENT_DATE) + interval '1 month')::date;
    op     employee_department_history%ROWTYPE;
BEGIN
    -- Portita pentru scripturile care rescriu lantul explicit (backfill):
    -- SET LOCAL mailguard.skip_dept_history = 'on';
    IF COALESCE(current_setting('mailguard.skip_dept_history', true), '') = 'on' THEN
        RETURN NULL;
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.enabled THEN
            INSERT INTO employee_department_history (employee_id, department, valid_from, source, created_by)
            VALUES (NEW.id, NEW.department,
                    date_trunc('month', COALESCE(NEW.productivity_start_date, CURRENT_DATE))::date,
                    'trigger', COALESCE(NEW.created_by, 'trigger'))
            ON CONFLICT DO NOTHING;
        END IF;
        RETURN NULL;
    END IF;

    SELECT * INTO op FROM employee_department_history
     WHERE employee_id = NEW.id AND valid_to IS NULL
     ORDER BY valid_from DESC LIMIT 1;

    -- PLECARE (enabled true -> false)
    IF OLD.enabled AND NOT NEW.enabled THEN
        IF op.id IS NOT NULL THEN
            UPDATE employee_department_history SET valid_to = m_next WHERE id = op.id;
        END IF;
        RETURN NULL;
    END IF;

    -- REVENIRE (false -> true). Reconcilierea sync-ului IRIS poate dezactiva in masa la un feed
    -- trunchiat, iar rularea urmatoare reactiveaza: REDESCHIDEM intervalul inchis recent, nu
    -- inseram unul nou (ar suprapune si ar pica pe employee_dept_hist_no_overlap, adica ar
    -- pica tot UPDATE-ul sync-ului).
    IF NOT OLD.enabled AND NEW.enabled AND op.id IS NULL THEN
        UPDATE employee_department_history SET valid_to = NULL
         WHERE id = (SELECT id FROM employee_department_history
                      WHERE employee_id = NEW.id AND valid_to >= m_cur
                      ORDER BY valid_to DESC, valid_from DESC LIMIT 1);
        IF NOT FOUND THEN
            INSERT INTO employee_department_history (employee_id, department, valid_from, source)
            VALUES (NEW.id, NEW.department, m_cur, 'trigger')
            ON CONFLICT (employee_id, valid_from)
            DO UPDATE SET department = EXCLUDED.department, valid_to = NULL;
        END IF;
        SELECT * INTO op FROM employee_department_history
         WHERE employee_id = NEW.id AND valid_to IS NULL
         ORDER BY valid_from DESC LIMIT 1;
    END IF;

    -- MUTARE
    IF NEW.enabled AND NEW.department IS DISTINCT FROM OLD.department THEN
        IF op.id IS NULL THEN
            INSERT INTO employee_department_history (employee_id, department, valid_from, source)
            VALUES (NEW.id, NEW.department, m_cur, 'trigger')
            ON CONFLICT (employee_id, valid_from)
            DO UPDATE SET department = EXCLUDED.department, valid_to = NULL;
        ELSIF op.department = NEW.department THEN
            NULL;   -- editarea manuala din UI a aplicat deja schimbarea pe lant
        ELSIF op.valid_from >= m_cur THEN
            -- A doua mutare in aceeasi luna: ultima castiga (nu lasam interval vid).
            UPDATE employee_department_history SET department = NEW.department WHERE id = op.id;
        ELSE
            UPDATE employee_department_history SET valid_to = m_cur WHERE id = op.id;
            INSERT INTO employee_department_history (employee_id, department, valid_from, source)
            VALUES (NEW.id, NEW.department, m_cur, 'trigger')
            ON CONFLICT (employee_id, valid_from)
            DO UPDATE SET department = EXCLUDED.department, valid_to = NULL;
        END IF;
    END IF;
    RETURN NULL;
END;
$fn$ LANGUAGE plpgsql;

-- Doua trigger-e, nu unul: clauza WHEN care citeste OLD nu poate sta pe un trigger care prinde si
-- INSERT. WHEN-ul e esential — `iris_employee_sync._upsert_one_employee` rescrie `department=` pe
-- FIECARE rand la FIECARE sync, iar `AFTER UPDATE OF department` singur ar rula functia degeaba.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_edm_dept_hist_ins') THEN
        CREATE TRIGGER trg_edm_dept_hist_ins
            AFTER INSERT ON employee_department_mapping
            FOR EACH ROW EXECUTE PROCEDURE edm_track_department_history();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_edm_dept_hist_upd') THEN
        CREATE TRIGGER trg_edm_dept_hist_upd
            AFTER UPDATE OF department, enabled ON employee_department_mapping
            FOR EACH ROW
            WHEN (OLD.department IS DISTINCT FROM NEW.department
                  OR OLD.enabled IS DISTINCT FROM NEW.enabled)
            EXECUTE PROCEDURE edm_track_department_history();
    END IF;
END $$;

COMMIT;
