-- Reclamatii CTS (modulul "Quality Evaluation") -- oglinda locala a view-ului IRIS DV
-- `quality_evaluation`, expus de Razvan la cererea din 2026-08-13.
--
-- CE SINT. Un rand = o reclamatie inregistrata in CTS pe o lucrare deja facuta (`entity` +
-- `entity_id`: client_contact_email_log, task sau client_call_log). Sint exact "Reclamatiile"
-- afisate pe Monitorul Operational si intra in productivitatea Suport 3.
--
-- CIMPURI care conteaza pentru calcul:
--   status: 1=new, 2=in progress, 3=solved (confirmat in sursa CTS,
--           src/Tss/Entities/Cts/QualityEvaluation.php)
--   created_at -> in_progress_at : timpul de CONTACT (preluare)
--   created_at -> solved_at      : timpul de SOLUTIONARE
--   is_according_to_the_procedure: 1 = s-a respectat procedura => reclamatie NEFONDATA
--                                  0 = nu s-a respectat        => reclamatie FONDATA
--           (confirmat in ControllerQuality.php: `CASE WHEN ...=1 THEN 'Yes' ELSE 'No' END`)
--   responsible_id: adminul EVALUAT (CTS il ia din closed_by/installed_by al lucrarii reclamate),
--           NU cine rezolva reclamatia. Cine o proceseaza = `updated_by`.
--   department_id: departamentul persoanei evaluate -- folosit pe Monitor, per departament.
--
-- Id-urile de admin se traduc in angajatii nostri prin `cts_dv_employee.admin_id` -> email ->
-- `employee_department_mapping.email` (59 de angajati mapabili azi).
--
-- Cheia primara e id-ul din CTS: sincronizarea e un upsert idempotent, nu insert orb.

CREATE TABLE IF NOT EXISTS cts_quality_evaluation (
    id                            BIGINT PRIMARY KEY,
    entity                        TEXT,
    entity_id                     BIGINT,
    client_id                     BIGINT,
    category_id                   INT,
    responsible_id                INT,
    department_id                 INT,
    status                        INT,
    score                         INT,
    is_according_to_the_procedure SMALLINT,
    has_modification              SMALLINT,
    observations                  TEXT,
    created_at                    TIMESTAMPTZ,
    created_by                    INT,
    in_progress_at                TIMESTAMPTZ,
    solved_at                     TIMESTAMPTZ,
    updated_at                    TIMESTAMPTZ,
    updated_by                    INT,
    deleted_at                    TIMESTAMPTZ,
    deleted_by                    INT,
    synced_at                     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Raportul lunar filtreaza pe luna solutionarii/crearii; monitorul pe departament si pe zi.
CREATE INDEX IF NOT EXISTS idx_cts_qe_created_at   ON cts_quality_evaluation (created_at);
CREATE INDEX IF NOT EXISTS idx_cts_qe_solved_at    ON cts_quality_evaluation (solved_at);
CREATE INDEX IF NOT EXISTS idx_cts_qe_department   ON cts_quality_evaluation (department_id);
CREATE INDEX IF NOT EXISTS idx_cts_qe_status       ON cts_quality_evaluation (status);
