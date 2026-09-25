-- Regulă deterministă: „Documente CargoTrack, <client>" de la no-reply@cargotrack.ro -> Suport 1.
-- Cerere business 2026-09-25. Regula existentă noreply-cargotrack-01 prinde doar noreply@ (fără
-- cratimă); documentele pleacă de pe no-reply@, deci decidea AI-ul (măsurat pe staging, 90 zile:
-- 140 pe suport_1, 7 comercial, 5 contabilitate, 3 taxe_drum, 6 neîncadrate).
-- Prioritatea P4 e în cod (priority_rules.match_forced), nu aici.
--
-- Idempotent: adaugă regula DOAR dacă id-ul ei nu există deja în settings->'rules'.
-- Necesar pe orice mediu: DEFAULT_RULES se seedează o singură dată, la prima citire.

DO $$
DECLARE
    v_rules jsonb;
    r jsonb;
BEGIN
    SELECT value->'rules' INTO v_rules FROM settings WHERE key = 'department_rules';
    IF v_rules IS NULL THEN
        RAISE NOTICE 'department_rules absent — seed-ul din cod se va ocupa la prima citire; skip.';
        RETURN;
    END IF;

    FOR r IN SELECT * FROM jsonb_array_elements(
        jsonb_build_array(
            jsonb_build_object(
                'id','noreply-docs-01','department','suport_1',
                'from','no-reply@cargotrack.ro','subject','documente cargotrack','body','',
                'enabled', true,
                'note','Documente CargoTrack (no-reply@) -> Suport 1',
                'by','migration_20260925','at','2026-09-25T00:00:00+00:00')
        )
    )
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM jsonb_array_elements(v_rules) AS e
            WHERE e->>'id' = r->>'id'
        ) THEN
            v_rules := v_rules || jsonb_build_array(r);
            RAISE NOTICE 'Adaugat regula %', r->>'id';
        ELSE
            RAISE NOTICE 'Regula % exista deja — skip', r->>'id';
        END IF;
    END LOOP;

    UPDATE settings
       SET value = jsonb_set(value, '{rules}', v_rules),
           updated_by = 'migration_20260925_dept_rule_cargotrack_docs',
           updated_at = NOW()
     WHERE key = 'department_rules';
END $$;
