-- Sursa noua pentru „Raport departamente": view-ul IRIS Data Views `client_contact_email_log`.
--
-- DE CE: `cts_department_moves` (trigger pe cts_ground_truth) prinde doar tranzitiile vizibile
-- intre doua sincronizari (~5 min), deci departamentele INTERMEDIARE se pierd
-- (Suport 1 -> Contabilitate -> Taxe drum apare ca Suport 1 -> Taxe drum). Log-ul CTS are un rand
-- per alocare, deci lantul complet.
--
-- Tabela e oglinda bruta a view-ului: TOATE coloanele TEXT, `id` PK — exact forma pe care o
-- creeaza si sync-ul DV la runtime (`_local_table_name`/`_create_local_table_if_needed` din
-- app/api/v1/iris_dv.py). O declaram aici ca sa existe indexurile de la primul sync si ca schema
-- sa ajunga pe prod prin fisier, nu prin DDL ad-hoc. Sync-ul face DELETE + INSERT (nu DROP),
-- deci indexurile supravietuiesc rularilor.
--
-- Aditiv + idempotent.

BEGIN;

CREATE TABLE IF NOT EXISTS cts_dv_client_contact_email_log (
    "id"                    TEXT NOT NULL PRIMARY KEY,
    "client_id"             TEXT,
    "admin_email_folder_id" TEXT,
    "mid"                   TEXT,
    "msid"                  TEXT,
    "message_id"            TEXT,
    "priority"              TEXT,
    "to_email"              TEXT,
    "cc"                    TEXT,
    "from_email"            TEXT,
    "type_id"               TEXT,
    "category_id"           TEXT,
    "title"                 TEXT,
    "date"                  TEXT,
    "status"                TEXT,
    "responsible_id"        TEXT,
    "department_id"         TEXT,
    "is_department_email"   TEXT,
    "has_attachment"        TEXT,
    "ai_doc_status"         TEXT,
    "created_at"            TEXT,
    "created_by"            TEXT,
    "updated_at"            TEXT,
    "updated_by"            TEXT,
    "deleted_at"            TEXT,
    "deleted_by"            TEXT,
    "assigned_at"           TEXT,
    "solved_at"             TEXT
);

-- Lanturile se reconstruiesc per message_id, filtrate pe data si department_id.
CREATE INDEX IF NOT EXISTS idx_ccel_message_id ON cts_dv_client_contact_email_log ("message_id");
CREATE INDEX IF NOT EXISTS idx_ccel_mid        ON cts_dv_client_contact_email_log ("mid");
CREATE INDEX IF NOT EXISTS idx_ccel_date       ON cts_dv_client_contact_email_log ("date");
CREATE INDEX IF NOT EXISTS idx_ccel_dept       ON cts_dv_client_contact_email_log ("department_id");
CREATE INDEX IF NOT EXISTS idx_ccel_assigned   ON cts_dv_client_contact_email_log ("assigned_at");

COMMIT;
