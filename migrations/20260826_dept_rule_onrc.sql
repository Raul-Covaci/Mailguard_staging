-- onrc_notificari@onrc.ro -> Departament CONTABILITATE, obligatoriu.
--
-- Regulile de departament sunt seedate O SINGURA DATA in settings['department_rules'], deci
-- adaugarea in DEFAULT_RULES (app/services/department_rules.py) NU ajunge pe o baza deja
-- initializata. Migratia adauga regula in store-ul existent, idempotent (dupa id).

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
        WHERE e->>'id' = 'onrc-notificari-01'
    ) THEN
        RETURN;
    END IF;

    r_new := jsonb_build_object(
        'id',         'onrc-notificari-01',
        'department', 'contabilitate',
        'from',       'onrc_notificari@onrc.ro',
        'subject',    '',
        'body',       '',
        'enabled',    true,
        'note',       'Notificari ONRC -> Contabilitate',
        'by',         'migration:20260826_dept_rule_onrc',
        'at',         to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"+00:00"')
    );

    UPDATE settings
       SET value      = jsonb_set(v, '{rules}', coalesce(v->'rules', '[]'::jsonb) || r_new),
           updated_by = 'migration:20260826_dept_rule_onrc',
           updated_at = NOW()
     WHERE key = 'department_rules';
END $$;
