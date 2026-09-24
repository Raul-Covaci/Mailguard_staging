-- Regulă deterministă: confirmările „Depunere Formular 150" (SPV) -> Recuperare TVA.
-- Expeditor admin.portal@mfinante.ro + subiect „Depunere Formular 150" => department recuperare_tva.
--
-- Erau clasificate de AI inconsecvent, fiindcă nicio regulă nu acoperea expeditorul: din 62 de
-- mailuri, 58 pe Suport 1, 3 pe Mobilitate, 1 pe Contabilitate (măsurat 2026-09-24).
-- Cerere business: 2026-09-24.
--
-- Pe expeditor + subiect, nu doar pe expeditor: toate cele 62 de mailuri de la această adresă au
-- subiectul „Depunere Formular 150", dar e o cutie de portal care poate începe oricând să trimită
-- și altceva. Potrivirea e case- și diacritice-insensitive (`department_rules._fold`), deci
-- subiectul se scrie cu minuscule, fără diacritice.
--
-- Idempotent: adaugă regula DOAR dacă id-ul ei nu există deja în settings->'rules'.
-- Necesar pe orice mediu (staging + prod): `DEFAULT_RULES` din cod se seedează o SINGURĂ dată, la
-- prima citire, deci pe un store existent modificarea din cod NU ar avea niciun efect.

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
                'id','mfinante-f150-01','department','recuperare_tva',
                'from','admin.portal@mfinante.ro','subject','depunere formular 150','body','',
                'enabled', true,
                'note','Depunere Formular 150 (SPV) -> recuperare_tva',
                'by','migration_20260924','at','2026-09-24T00:00:00+00:00')
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
           updated_by = 'migration_20260924_dept_rule_formular150',
           updated_at = NOW()
     WHERE key = 'department_rules';
END $$;
