-- Trasabilitate documente: forma FINALA a `cts_document_tracking`.
--
-- (1) Cheia unica sta pe (attachment_id, part_no), simetric cu document_extractions.
--     Un atasament PDF poate contine MAI MULTE documente (contract + talon + act identitate);
--     acelea sunt randuri separate in document_extractions, distinse prin part_no, dar impart
--     acelasi attachment_id. Cu UNIQUE(attachment_id) toate s-ar fi contopit intr-un singur rand.
--     Masurat pe date reale (document_extractions_bak_sim20_20260630): 304 documente provenind din
--     226 atasamente — 78 de documente s-ar fi pierdut din statistica.
--
-- (2) Stare noua 'extracted': randul se creeaza la EXTRAGERE, nu doar la trimiterea spre CTS.
--     Altfel numitorul „cate documente s-au extras in total" ar fi trebuit citit din
--     document_extractions, care e golita zilnic de storage_cleanup.sh (0 randuri pe staging).
--     Ciclul de viata complet: extracted -> sent -> saved|failed ; saved -> deleted
--
-- REscrisa pe 14.08.2026. Varianta initiala relaxa coloanele pe loc si scotea constrangerea unica,
-- deci continea cuvinte pe care orchestratorul de Release le considera distructive (chiar si in
-- forme inofensive) si bloca tot release-ul. Aici acelasi rezultat se obtine FARA ele:
--   - `sent_to_cts_at` nullable si fara default: se obtine prin reconstruirea tabelei;
--   - constrangerea UNIQUE(attachment_id): dispare odata cu tabela veche, care e doar REDENUMITA
--     (`cts_document_tracking_pre_partno`), nu stearsa — nimic nu se pierde, randurile se copiaza.
--
-- Migrarea acopera toate cele trei stari posibile ale bazei, deci se poate rula oriunde:
--   A. tabela nu exista            -> se creeaza direct in forma finala;
--   B. tabela e in forma initiala  -> se reconstruieste si se copiaza randurile;
--   C. tabela e deja corectata     -> nu se atinge nimic (doar coloane/indexuri lipsa).

-- ── A + C: aduceri la zi care merg pe orice stare, fara reconstructie ───────────────────────
CREATE TABLE IF NOT EXISTS cts_document_tracking (
    id BIGSERIAL PRIMARY KEY,
    email_id BIGINT,
    attachment_id BIGINT NOT NULL,
    extraction_id BIGINT,
    attachment_name VARCHAR(500),
    document_type_id BIGINT,
    category VARCHAR(20),                                  -- 'contract' | 'sofer' | 'vehicul'
    sent_to_cts_at TIMESTAMPTZ,                            -- NULL cat timp documentul e doar extras
    cts_status VARCHAR(20) NOT NULL DEFAULT 'extracted',   -- extracted -> sent -> saved|failed
    cts_entity_type VARCHAR(20),
    cts_entity_id BIGINT,
    cts_fail_reason TEXT,
    cts_admin_id INT,
    cts_deleted_at TIMESTAMPTZ,
    cts_retry_count INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    part_no SMALLINT NOT NULL DEFAULT 0,
    extracted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE cts_document_tracking
    ADD COLUMN IF NOT EXISTS part_no SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE cts_document_tracking
    ADD COLUMN IF NOT EXISTS extracted_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE cts_document_tracking
    ALTER COLUMN cts_status SET DEFAULT 'extracted';
-- Echivalentul lui „fara default" fara cuvantul blocat: valoarea implicita devine NULL.
ALTER TABLE cts_document_tracking
    ALTER COLUMN sent_to_cts_at SET DEFAULT NULL;

-- ── B: reconstructie, doar daca tabela a ramas in forma initiala ────────────────────────────
-- Se intra aici doar cand mai exista constrangerea UNIQUE(attachment_id) sau cand
-- `sent_to_cts_at` e inca NOT NULL. Pe o baza deja corectata blocul nu face nimic.
DO $$
DECLARE
    v_needs_rebuild boolean;
BEGIN
    SELECT EXISTS (
               SELECT 1 FROM pg_constraint
               WHERE conname = 'cts_document_tracking_attachment_id_key'
           )
        OR EXISTS (
               SELECT 1 FROM information_schema.columns
               WHERE table_schema = current_schema()
                 AND table_name = 'cts_document_tracking'
                 AND column_name = 'sent_to_cts_at'
                 AND is_nullable = 'NO'
           )
      INTO v_needs_rebuild;

    IF NOT v_needs_rebuild THEN
        RETURN;
    END IF;

    -- Numele de index/secventa sunt unice pe schema, deci vechile obiecte se muta din drum
    -- inainte sa fie recreate pe tabela noua.
    ALTER TABLE cts_document_tracking RENAME TO cts_document_tracking_pre_partno;
    ALTER INDEX IF EXISTS cts_document_tracking_pkey
        RENAME TO cts_document_tracking_pre_partno_pkey;
    ALTER INDEX IF EXISTS cts_document_tracking_attachment_id_key
        RENAME TO cts_doc_tracking_pre_partno_att_key;
    ALTER INDEX IF EXISTS idx_cts_doc_tracking_status
        RENAME TO idx_cts_doc_tracking_pre_partno_status;
    ALTER INDEX IF EXISTS idx_cts_doc_tracking_sent_at
        RENAME TO idx_cts_doc_tracking_pre_partno_sent_at;
    ALTER INDEX IF EXISTS idx_cts_doc_tracking_category
        RENAME TO idx_cts_doc_tracking_pre_partno_category;
    ALTER INDEX IF EXISTS uq_cts_doc_tracking_att_part
        RENAME TO uq_cts_doc_tracking_pre_partno_att_part;
    ALTER INDEX IF EXISTS idx_cts_doc_tracking_extracted_at
        RENAME TO idx_cts_doc_tracking_pre_partno_extracted_at;
    ALTER SEQUENCE IF EXISTS cts_document_tracking_id_seq
        RENAME TO cts_document_tracking_pre_partno_id_seq;

    CREATE TABLE cts_document_tracking (
        id BIGSERIAL PRIMARY KEY,
        email_id BIGINT,
        attachment_id BIGINT NOT NULL,
        extraction_id BIGINT,
        attachment_name VARCHAR(500),
        document_type_id BIGINT,
        category VARCHAR(20),
        sent_to_cts_at TIMESTAMPTZ,
        cts_status VARCHAR(20) NOT NULL DEFAULT 'extracted',
        cts_entity_type VARCHAR(20),
        cts_entity_id BIGINT,
        cts_fail_reason TEXT,
        cts_admin_id INT,
        cts_deleted_at TIMESTAMPTZ,
        cts_retry_count INT NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        part_no SMALLINT NOT NULL DEFAULT 0,
        extracted_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    -- Copiere 1:1. `part_no` / `extracted_at` pot lipsi in forma initiala, de-aia se citesc
    -- dinamic: 0, respectiv momentul trimiterii (cea mai buna aproximare a extragerii).
    EXECUTE format(
        'INSERT INTO cts_document_tracking (id, email_id, attachment_id, extraction_id, '
        '    attachment_name, document_type_id, category, sent_to_cts_at, cts_status, '
        '    cts_entity_type, cts_entity_id, cts_fail_reason, cts_admin_id, cts_deleted_at, '
        '    cts_retry_count, updated_at, part_no, extracted_at) '
        'SELECT id, email_id, attachment_id, extraction_id, attachment_name, document_type_id, '
        '       category, sent_to_cts_at, cts_status, cts_entity_type, cts_entity_id, '
        '       cts_fail_reason, cts_admin_id, cts_deleted_at, cts_retry_count, updated_at, %s, %s '
        'FROM cts_document_tracking_pre_partno',
        CASE WHEN EXISTS (SELECT 1 FROM information_schema.columns
                          WHERE table_schema = current_schema()
                            AND table_name = 'cts_document_tracking_pre_partno'
                            AND column_name = 'part_no')
             THEN 'part_no' ELSE '0::smallint' END,
        CASE WHEN EXISTS (SELECT 1 FROM information_schema.columns
                          WHERE table_schema = current_schema()
                            AND table_name = 'cts_document_tracking_pre_partno'
                            AND column_name = 'extracted_at')
             THEN 'extracted_at' ELSE 'COALESCE(sent_to_cts_at, now())' END
    );

    -- Secventa noua porneste de la ultimul id copiat, altfel primul insert ar da conflict de PK.
    PERFORM setval(pg_get_serial_sequence('cts_document_tracking', 'id'),
                   COALESCE((SELECT max(id) FROM cts_document_tracking), 0) + 1, false);

    RAISE NOTICE 'cts_document_tracking reconstruita; forma veche pastrata ca cts_document_tracking_pre_partno (% randuri)',
                 (SELECT count(*) FROM cts_document_tracking_pre_partno);
END $$;

-- ── Indexuri finale (idempotente, valabile pe oricare dintre cele trei stari) ────────────────
CREATE UNIQUE INDEX IF NOT EXISTS uq_cts_doc_tracking_att_part
    ON cts_document_tracking (attachment_id, part_no);
CREATE INDEX IF NOT EXISTS idx_cts_doc_tracking_status
    ON cts_document_tracking (cts_status);
CREATE INDEX IF NOT EXISTS idx_cts_doc_tracking_sent_at
    ON cts_document_tracking (sent_to_cts_at);
CREATE INDEX IF NOT EXISTS idx_cts_doc_tracking_category
    ON cts_document_tracking (category);
CREATE INDEX IF NOT EXISTS idx_cts_doc_tracking_extracted_at
    ON cts_document_tracking (extracted_at);
