-- Fereastra de contorizare = PONTAJUL departamentului; `department_schedule` doar ca fallback.
--
-- PROBLEMA (2026-08-19). Precedenta era invers: daca exista rand in `department_schedule`, acela
-- batea pontajul, iar pontajul servea doar la verificarea „>=1 prezent". Tabela e populata MANUAL
-- si divergase de realitate: `suport_2` era configurat 07:00-22:00, desi turele reale din pontaj
-- sunt 08:00-16:30 si 12:30-21:00 (nimeni la 07:00, nimeni dupa 21:00).
--   Caz real: mail intrat sambata 01.08 19:04, rezolvat luni 03.08 09:36 -> 156.7 min (2h37),
--   fiindca numaratoarea porneste luni la 07:00. Cu pontajul real (08:00): 96.7 min (1h37).
--   Idem 02.08 00:31 -> 03.08 09:30: 150.8 min (2h31) in loc de 90.8 min (1h31).
--
-- REGULA NOUA (decizie business, 2026-08-19): sursa de adevar e „Utilizatori -> Pontaj pe
-- departamente" (`employee_attendance`, preluat din CTS sau ajustat manual):
--   1. exista pontaj cu ore in ziua respectiva -> fereastra = uniunea turelor celor prezenti
--      (primul inceput -> ultimul final);
--   2. nu exista pontaj utilizabil (zi neimportata / viitoare / prezenti fara ore) -> fallback pe
--      `department_schedule`;
--   3. nici program configurat -> ziua nu curge.
-- Zi cu inregistrari de pontaj dar 0 prezenti -> nu curge (neschimbat).
--
-- Oglindeste `_BizCache._dept_window` din app/services/productivity.py -- orice schimbare aici
-- trebuie facuta si acolo, altfel mailurile (Python) si task-urile (SQL) ar raspunde diferit la
-- aceeasi intrebare.
--
-- Idempotent: CREATE OR REPLACE. Rollback: se re-aplica 20260804b_business_minutes_dept_window.sql.

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
            SELECT count(*) FILTER (WHERE present) INTO v_present_cnt
              FROM employee_attendance
             WHERE department = p_dept AND work_date = v_day;

            IF v_present_cnt = 0 AND EXISTS (SELECT 1 FROM employee_attendance
                                              WHERE department = p_dept AND work_date = v_day) THEN
                v_day := v_day + 1;
                CONTINUE;
            END IF;

            -- 1) PONTAJ (sursa de adevar): uniunea turelor reale ale celor prezenti.
            SELECT min((begin_time AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Bucharest'),
                   max((end_time   AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Bucharest')
              INTO v_work_start, v_work_end
              FROM employee_attendance
             WHERE department = p_dept AND work_date = v_day AND present = true
               AND begin_time IS NOT NULL AND end_time IS NOT NULL;

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
