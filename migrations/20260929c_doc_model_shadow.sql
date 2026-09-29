-- T3-O14: comparație Sonnet vs Haiku pe vision-ul de clasificare a documentelor (mod shadow).
--
-- Un rând per apel-eșantion: ce etichete a dat fiecare model pe ACEEAȘI intrare. Doar etichete
-- (type_id, categorie, limite de pagină, încredere) — niciun text extras, niciun `reason`, nicio dată
-- personală. Scris doar când documents.haiku_shadow_tasks conține task-ul (implicit listă goală).
--
-- Down: migrations/down/20260929c_doc_model_shadow.down.sql (NU în migrations/, altfel
-- scripts/migrate.sh l-ar aplica automat).

CREATE TABLE IF NOT EXISTS doc_model_shadow (
    id               bigserial    PRIMARY KEY,
    created_at       timestamptz  NOT NULL DEFAULT now(),
    task_prefix      varchar(40)  NOT NULL,          -- doc_classify_vision | doc_segment
    input_hash       char(64)     NOT NULL,          -- sha256 al atașamentului / paginii trimise
    sonnet_model     varchar(80),
    haiku_model      varchar(80),
    sonnet_labels    jsonb,                          -- {type_id, confidence, category, documents | starts_new}
    haiku_labels     jsonb,                          -- idem; NULL dacă Haiku a dat un răspuns respins de apelant
    sonnet_type_id   integer,
    haiku_type_id    integer,
    haiku_valid      boolean      NOT NULL,
    match            boolean      NOT NULL,
    sonnet_cost_usd  numeric(12,6),
    haiku_cost_usd   numeric(12,6)
);
CREATE INDEX IF NOT EXISTS doc_model_shadow_task_idx ON doc_model_shadow (task_prefix, created_at);

INSERT INTO settings (key, value, description, updated_by, updated_at) VALUES
  ('documents.haiku_first_tasks', '[]'::jsonb,
   'T3-O14: task-uri vision rulate întâi pe Haiku (doc_classify_vision, doc_segment). Goală = Sonnet.',
   'migration', now()),
  ('documents.haiku_min_confidence', '0.90'::jsonb,
   'T3-O14: încrederea minimă ca un răspuns Haiku să fie acceptat fără Sonnet.', 'migration', now()),
  ('documents.haiku_shadow_tasks', '[]'::jsonb,
   'T3-O14: task-uri pe care Haiku rulează în paralel, doar pentru comparație (doc_model_shadow).',
   'migration', now()),
  ('documents.haiku_shadow_sample', '0.3'::jsonb,
   'T3-O14: fracțiunea din apelurile Sonnet comparate cu Haiku în mod shadow.', 'migration', now())
ON CONFLICT (key) DO NOTHING;

SELECT 'migration 20260929c_doc_model_shadow applied' AS status;
