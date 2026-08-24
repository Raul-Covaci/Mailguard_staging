-- Auto-SOLVED spre CTS: largeste regula Urban & Asociatii de la un singur subiect
-- ("Inregistrare: Dosar CARGO TRACK SOLUTIONS SRL") la ORICE subiect care contine
-- "Inregistrare" (confirmarile de inregistrare in arhiva vin cu numar de dosar, CUI si
-- debitor diferite de fiecare data — subiectul exact nu se poate lista).
-- Substringul e "nregistrare" ca sa prinda si varianta cu diacritice ("Înregistrare").
-- Aditiv + idempotent (re-rulabil fara efect). Oglindeste _DEFAULT_RULES din cts_auto_solved.py.

UPDATE settings s
SET value = (
    SELECT jsonb_agg(
        CASE WHEN r->'senders' ? 'secretariat@urbansiasociatii.ro'
             THEN jsonb_set(r, '{subject_contains}', '["nregistrare"]'::jsonb)
             ELSE r END
        ORDER BY ord
    )
    FROM jsonb_array_elements(s.value) WITH ORDINALITY AS t(r, ord)
)
WHERE s.key = 'cts.auto_solved_rules'
  AND jsonb_typeof(s.value) = 'array'
  AND s.value @> '[{"senders":["secretariat@urbansiasociatii.ro"]}]'::jsonb;

-- Cazul in care regula lipseste cu totul (config editata din UI fara Urban): o adaugam.
UPDATE settings s
SET value = s.value || '[{"senders":["secretariat@urbansiasociatii.ro"],"subject_contains":["nregistrare"]}]'::jsonb
WHERE s.key = 'cts.auto_solved_rules'
  AND jsonb_typeof(s.value) = 'array'
  AND s.value <> '[]'::jsonb
  AND NOT (s.value @> '[{"senders":["secretariat@urbansiasociatii.ro"]}]'::jsonb);
