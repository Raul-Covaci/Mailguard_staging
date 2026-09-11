-- Analiza Operatori — evaluarea AI a raspunsurilor pe email catre clienti (tab nou in Mail-uri CTS).
--
-- Fiecare rand = o pereche (mail primit de la client, raspuns trimis de operator), trecuta prin
-- promptul din `app/services/prompts/emails/operator_eval.txt` (sursa de adevar = fisierul din
-- repo, ca la satisfactia V6; NU se seedeaza text de prompt din SQL).
--
-- Perechile care NU se pot evalua se scriu TOT aici, cu `skipped_reason` si fara scoruri. Altfel
-- fiecare rulare le-ar reincerca la nesfarsit: aducerea corpului din gateway costa un apel, iar
-- imperecherea costa doua interogari. Randul e marcajul „am examinat asta" — aceeasi idee ca
-- `vathub_matched_at` la redirectul VATHUB.
--
-- Cheia unica e `cts_gt_id` (randul CTS TRIMIS), nu `message_id`: CTS face un tichet per
-- destinatar, deci acelasi `message_id` poate avea mai multe randuri. Deduplicarea replicilor se
-- face la SELECTIE (in motor), nu aici.

CREATE TABLE IF NOT EXISTS email_operator_evaluations (
    id                 bigserial PRIMARY KEY,
    cts_gt_id          bigint NOT NULL REFERENCES cts_ground_truth(id) ON DELETE CASCADE,
    cts_ticket_id      bigint,
    message_id         text,
    reply_at           timestamptz,

    -- Operatorul: luat de pe tichetul PRIMIT imperecheat (`cts_assignee_email`), fiindca randurile
    -- `sent` nu au assignee. `department` e cel ISTORIC, la data raspunsului.
    employee_id        integer REFERENCES employee_department_mapping(id) ON DELETE SET NULL,
    employee_email     varchar(320),
    employee_name      text,
    department         text,

    client_id          bigint REFERENCES clients(id) ON DELETE SET NULL,
    client_name        text,
    received_email_id  bigint REFERENCES emails(id) ON DELETE SET NULL,
    match_by           varchar(16),          -- 'msid' (exact) | 'subject' (euristic)

    score_general      numeric(3,1),
    s_lingvistic       smallint,
    s_ton              smallint,
    s_claritate        smallint,
    s_acoperire        smallint,
    s_empatie          smallint,
    puncte_neadresate  jsonb,
    sugestii           jsonb,
    criterii           jsonb,                -- justificarile per criteriu, brut din model
    mentiune           text,

    skipped_reason     varchar(32),          -- no_pair | no_reply_text | too_short | auto_reply | ai_error
    model              varchar(64),
    prompt_version     varchar(16),
    evaluated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS email_operator_eval_gt_uidx
    ON email_operator_evaluations (cts_gt_id);
CREATE INDEX IF NOT EXISTS email_operator_eval_emp_idx
    ON email_operator_evaluations (employee_id, reply_at);
CREATE INDEX IF NOT EXISTS email_operator_eval_client_idx
    ON email_operator_evaluations (client_id, reply_at);
CREATE INDEX IF NOT EXISTS email_operator_eval_at_idx
    ON email_operator_evaluations (reply_at);
-- Doar randurile efectiv evaluate — toate agregarile filtreaza pe `score_general IS NOT NULL`.
CREATE INDEX IF NOT EXISTS email_operator_eval_scored_idx
    ON email_operator_evaluations (reply_at) WHERE score_general IS NOT NULL;

COMMENT ON COLUMN email_operator_evaluations.skipped_reason IS
  'De ce nu s-a evaluat perechea. NULL = evaluata. Randul exista ca sa nu fie reincercata.';
COMMENT ON COLUMN email_operator_evaluations.match_by IS
  'Cum s-a gasit mailul original: msid = Message-ID exact, subject = euristica pe subiect normalizat.';

-- Config rulare — modificabila din DB fara redeploy (tiparul `satisfaction.v6`).
--   model_hint        : modelul folosit. Sonnet, nu Haiku: promptul cere distinctii fine
--                       („ce puncte au ramas neadresate"), exact motivul pentru care satisfactia
--                       V6 a fost mutata de pe implicitul gateway-ului.
--   max_workers       : cate perechi se evalueaza IN PARALEL (plafon 8 in cod). Gateway-ul e
--                       partajat cu clasificarea mailurilor, scorarea apelurilor si satisfactia.
--   max_per_run       : plafon dur de apeluri AI per rulare.
--   min_reply_chars   : sub atat, raspunsul e considerat prea scurt pentru evaluare.
--   allow_subject_match: daca se accepta si imperecherea euristica pe subiect (false = doar Message-ID).
INSERT INTO settings (key, value, description)
VALUES ('emails.operator_eval',
        '{"model_hint": "claude-sonnet-4-6", "max_workers": 4, "max_per_run": 300,
          "min_reply_chars": 80, "allow_subject_match": true, "prompt_version": "v1"}'::jsonb,
        'Analiza Operatori — evaluarea AI a raspunsurilor pe email')
ON CONFLICT (key) DO NOTHING;

SELECT 'migration 20260912_operator_email_eval applied' AS status;
