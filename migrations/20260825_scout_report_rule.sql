-- Email Scout Report (office@cargotrack.ro) -> Departament SUPORT 1, obligatoriu.
--
-- Regulile de departament sunt seedate O SINGURA DATA in settings['department_rules'], deci
-- adaugarea in DEFAULT_RULES (app/services/department_rules.py) NU ajunge pe o baza deja
-- initializata. Migratia adauga regula in store-ul existent, idempotent (dupa id).
-- Prioritatea P4 pentru acelasi mail e in COD (priority_rules.match_forced) — regulile de
-- prioritate nu sunt editabile din UI, deci nu au nevoie de migratie.

DO $$
DECLARE
    v      jsonb;
    r_new  jsonb;
BEGIN
    SELECT value INTO v FROM settings WHERE key = 'department_rules';

    -- Store inexistent: seed-ul din cod ruleaza la prima citire si include deja regula.
    IF v IS NULL THEN
        RETURN;
    END IF;

    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(coalesce(v->'rules', '[]'::jsonb)) AS e
        WHERE e->>'id' = 'scout-report-01'
    ) THEN
        RETURN;
    END IF;

    r_new := jsonb_build_object(
        'id',         'scout-report-01',
        'department', 'suport_1',
        'from',       'office@cargotrack.ro',
        'subject',    'Email Scout Report',
        'body',       '',
        'enabled',    true,
        'note',       'Email Scout Report (office@) -> Suport 1',
        'by',         'migration:20260825_scout_report_rule',
        'at',         to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"+00:00"')
    );

    UPDATE settings
       SET value      = jsonb_set(v, '{rules}', coalesce(v->'rules', '[]'::jsonb) || r_new),
           updated_by = 'migration:20260825_scout_report_rule',
           updated_at = NOW()
     WHERE key = 'department_rules';
END $$;
