-- 2026-09-11: `business_minutes_emp` trece pe ISTORICUL de departament.
--
-- OGLINDA obligatorie pentru `_BizCache._dept_window` din app/services/productivity.py, care de la
-- migratia 20260911_employee_department_history rezolva apartenenta la departament LA DATA ZILEI
-- (`employee_department_history`), nu din `employee_department_mapping.department`. Fara schimbarea
-- asta, mailurile/apelurile (Python) si task-urile (SQL) ar raspunde diferit la aceeasi intrebare:
-- „cine era in departament in ziua X".
--
-- Ce se schimba, punctual:
--   * acoperirea zilei (numar prezenti, „ziua a fost pontata", uniunea turelor) se ia de la
--     angajatii care APARTINEAU departamentului in ziua respectiva, nu de la cei care apartin azi.
--     `employee_attendance.department` ramane doar fallback pentru randurile fara `employee_id`
--     (absente sintetice vechi) — coloana e scrisa de pontaj_sync din maparea CURENTA, deci o
--     re-sincronizare a unei luni vechi dupa o promovare ar muta si orele.
--   * verificarea „toti angajatii sunt in concediu" foloseste `employee_dept_members(dept, zi)`
--     in loc de `department = p_dept AND enabled` — un om plecat din firma nu mai apare in lunile
--     de dupa plecare, dar ramane in cele dinainte.
--
-- Restul logicii (precedenta pontaj > program, ziua inactiva, tura individuala) e NESCHIMBATA fata
-- de 20260819e. Idempotent: CREATE OR REPLACE. Rollback: se re-aplica 20260819e.

CREATE OR REPLACE FUNCTION public.business_minutes_emp(
    p_dept text,
    p_employee_id integer,
    p_start timestamp with time zone,
    p_end timestamp with time zone,
    p_holidays date[] DEFAULT ARRAY[]::date[]
)
RETURNS numeric
LANGUAGE plpgsql
STABLE
AS $function$
DECLARE
    v_start_local timestamp;
    v_end_local   timestamp;
    v_day         date;
    v_last_day    date;
    v_work_start  timestamp;
    v_work_end    timestamp;
    v_sched       RECORD;
    v_att         RECORD;
    v_ov_start    timestamp;
    v_ov_end      timestamp;
    v_total       numeric := 0;
    v_holidays    date[];
    v_iter        integer := 0;
    v_dept_window boolean;
    v_present_cnt integer;
BEGIN
    IF p_start IS NULL OR p_end IS NULL OR p_end <= p_start THEN
        RETURN NULL;
    END IF;
    v_holidays    := COALESCE(p_holidays, ARRAY[]::date[]);
    v_start_local := p_start AT TIME ZONE 'Europe/Bucharest';
    v_end_local   := p_end   AT TIME ZONE 'Europe/Bucharest';
    v_day         := v_start_local::date;
    v_last_day    := v_end_local::date;

    -- Departamentele care masoara pe acoperirea departamentului (vezi comentariul de sus).
    v_dept_window := p_dept IN ('suport_1','suport_2','suport_3','taxe_drum',
                                'contabilitate','recuperare_tva');

    WHILE v_day <= v_last_day AND v_iter < 731 LOOP
        v_iter := v_iter + 1;

        -- Sarbatoare legala → skip
        IF v_day = ANY(v_holidays) THEN
            v_day := v_day + 1;
            CONTINUE;
        END IF;

        IF v_dept_window THEN
            -- ── Fereastra DEPARTAMENTULUI ────────────────────────────────────────────────
            -- Zi cu 0 prezenti in departament = nu curge. Daca nu exista NICIO inregistrare
            -- de pontaj pentru ziua asta (pontaj neimportat / zi viitoare) nu penalizam:
            -- tratam ca zi potential lucratoare, ca in `is_working_day_for_dept`.
            SELECT count(*) FILTER (WHERE a.present) INTO v_present_cnt
              FROM employee_attendance a
             WHERE a.work_date = v_day
               AND (EXISTS (SELECT 1 FROM employee_department_history h
                        WHERE h.employee_id = a.employee_id
                          AND h.department = p_dept
                          AND h.valid_from <= v_day
                          AND (h.valid_to IS NULL OR h.valid_to > v_day))
               OR (a.employee_id IS NULL AND a.department = p_dept));

            IF v_present_cnt = 0 THEN
                -- (a) ziua a fost pontata, dar n-a fost nimeni prezent -> zi inactiva
                IF EXISTS (SELECT 1 FROM employee_attendance a
                            WHERE a.work_date = v_day
                              AND (EXISTS (SELECT 1 FROM employee_department_history h
                        WHERE h.employee_id = a.employee_id
                          AND h.department = p_dept
                          AND h.valid_from <= v_day
                          AND (h.valid_to IS NULL OR h.valid_to > v_day))
               OR (a.employee_id IS NULL AND a.department = p_dept))) THEN
                    v_day := v_day + 1;
                    CONTINUE;
                END IF;
                -- (b) ziua nu a fost pontata deloc: e zi inactiva DOAR daca TOTI angajatii
                --     activi ai departamentului sunt in concediu aprobat. Altfel e gaura de
                --     pontaj si nu penalizam (fallback pe program).
                IF EXISTS (SELECT 1 FROM employee_department_mapping e
                            JOIN employee_dept_members(p_dept, v_day) m ON m.employee_id = e.id)
                   AND NOT EXISTS (
                        SELECT 1 FROM employee_department_mapping e
                         JOIN employee_dept_members(p_dept, v_day) m ON m.employee_id = e.id
                         WHERE true
                           AND NOT EXISTS (
                                SELECT 1 FROM employee_schedule es
                                 WHERE es.employee_id = e.id
                                   AND v_day BETWEEN es.start_date AND es.end_date)
                           AND NOT EXISTS (
                                SELECT 1 FROM cts_dv_employee_vacation_request v
                                 WHERE v.employee_id = e.iris_id
                                   AND v.deleted_at IS NULL
                                   AND v.status::int IN (1, 2)
                                   AND v_day BETWEEN v.period_begin::date AND v.period_end::date))
                THEN
                    v_day := v_day + 1;
                    CONTINUE;
                END IF;
            END IF;

            -- 1) PONTAJ (sursa de adevar): uniunea turelor reale ale celor prezenti.
            SELECT min((a.begin_time AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Bucharest'),
                   max((a.end_time   AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Bucharest')
              INTO v_work_start, v_work_end
              FROM employee_attendance a
             WHERE a.work_date = v_day AND a.present = true
               AND a.begin_time IS NOT NULL AND a.end_time IS NOT NULL
               AND (EXISTS (SELECT 1 FROM employee_department_history h
                        WHERE h.employee_id = a.employee_id
                          AND h.department = p_dept
                          AND h.valid_from <= v_day
                          AND (h.valid_to IS NULL OR h.valid_to > v_day))
               OR (a.employee_id IS NULL AND a.department = p_dept));

            IF v_work_start IS NULL OR v_work_end IS NULL THEN
                -- 2) fallback: programul configurat al departamentului
                SELECT start_time, end_time, requires_attendance INTO v_sched
                  FROM department_schedule
                 WHERE department = p_dept
                   AND weekday = EXTRACT(ISODOW FROM v_day)::smallint
                   AND active = true;

                IF NOT FOUND THEN
                    -- 3) nici pontaj, nici program -> ziua nu curge
                    v_day := v_day + 1;
                    CONTINUE;
                END IF;
                IF v_sched.requires_attendance AND v_present_cnt = 0 THEN
                    v_day := v_day + 1;
                    CONTINUE;
                END IF;
                v_work_start := v_day::timestamp + v_sched.start_time;
                v_work_end   := v_day::timestamp + v_sched.end_time;
            END IF;
        ELSE
            -- ── Comportament ANTERIOR: tura individuala a operatorului ───────────────────
            SELECT present, begin_time, end_time
              INTO v_att
              FROM employee_attendance
             WHERE employee_id = p_employee_id AND work_date = v_day;

            IF FOUND THEN
                IF NOT v_att.present THEN
                    v_day := v_day + 1;
                    CONTINUE;
                END IF;
                IF v_att.begin_time IS NOT NULL AND v_att.end_time IS NOT NULL THEN
                    v_work_start := (v_att.begin_time AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Bucharest';
                    v_work_end   := (v_att.end_time   AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Bucharest';
                ELSE
                    SELECT start_time, end_time INTO v_sched
                      FROM department_schedule
                     WHERE department = p_dept
                       AND weekday = EXTRACT(ISODOW FROM v_day)::smallint
                       AND active = true;
                    IF NOT FOUND THEN
                        v_day := v_day + 1;
                        CONTINUE;
                    END IF;
                    v_work_start := v_day::timestamp + v_sched.start_time;
                    v_work_end   := v_day::timestamp + v_sched.end_time;
                END IF;
            ELSE
                SELECT start_time, end_time, requires_attendance INTO v_sched
                  FROM department_schedule
                 WHERE department = p_dept
                   AND weekday = EXTRACT(ISODOW FROM v_day)::smallint
                   AND active = true;
                IF NOT FOUND THEN
                    v_day := v_day + 1;
                    CONTINUE;
                END IF;
                IF v_sched.requires_attendance THEN
                    PERFORM 1 FROM department_attendance
                     WHERE department = p_dept AND work_date = v_day AND present = true;
                    IF NOT FOUND THEN
                        v_day := v_day + 1;
                        CONTINUE;
                    END IF;
                END IF;
                v_work_start := v_day::timestamp + v_sched.start_time;
                v_work_end   := v_day::timestamp + v_sched.end_time;
            END IF;
        END IF;

        v_ov_start := GREATEST(v_work_start, v_start_local);
        v_ov_end   := LEAST(v_work_end, v_end_local);
        IF v_ov_end > v_ov_start THEN
            v_total := v_total + EXTRACT(EPOCH FROM (v_ov_end - v_ov_start)) / 60.0;
        END IF;

        v_day := v_day + 1;
    END LOOP;

    RETURN v_total;
END;
$function$;
