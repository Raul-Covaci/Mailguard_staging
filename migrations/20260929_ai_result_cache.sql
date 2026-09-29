-- T3-L1: cache de rezultat AI (documente + serie OP). Vezi app/services/ai_cache.py.
--
-- Același document / atașament ajungea la model de mai multe ori cu intrare identică (bucla
-- retry_transient a drain-ului de documente, reclasificări, reîncercările op_series). Cheia e
-- sha256 pe payload-ul EFECTIV trimis din iris_ai.run_prompt(), deci o intrare diferită nu poate
-- lua răspunsul altei intrări, indiferent cum își construiește apelantul numele de task.
--
-- Aditiv și inert: cât timp settings['ai_cache.enabled'] = false (implicit), nimic nu citește și
-- nimic nu scrie în aceste tabele.
--
-- Down: migrations/down/20260929_ai_result_cache.down.sql (NU stă în migrations/, altfel
-- scripts/migrate.sh l-ar aplica automat).

CREATE TABLE IF NOT EXISTS ai_result_cache (
    cache_key            char(64)     PRIMARY KEY,           -- sha256 hex, vezi ai_cache.cache_key()
    task_prefix          varchar(80)  NOT NULL,              -- ex. doc_segment, op_series
    result               json         NOT NULL,              -- exact ce întoarce run_prompt(), cu model-ul ORIGINAL; json (nu jsonb): păstrează ordinea cheilor
    original_cost_usd    numeric(12,6),
    original_tokens_in   integer,
    original_tokens_out  integer,
    created_at           timestamptz  NOT NULL DEFAULT now(),
    expires_at           timestamptz  NOT NULL,
    hit_count            integer      NOT NULL DEFAULT 0,
    last_hit_at          timestamptz
);
CREATE INDEX IF NOT EXISTS ai_result_cache_expires_idx ON ai_result_cache (expires_at);

COMMENT ON TABLE ai_result_cache IS
  'T3-L1: răspunsuri AI reușite și validate de apelant, cheiate pe sha256(payload). TTL settings[ai_cache.ttl_days]. Conține date extrase din documente: se curăță la expirare.';

-- Un rând per apel EVITAT. Rapoartele existente (ai_call_log) rămân neatinse: un hit nu scrie acolo.
CREATE TABLE IF NOT EXISTS ai_cache_hit_log (
    id              bigserial    PRIMARY KEY,
    created_at      timestamptz  NOT NULL DEFAULT now(),
    task            varchar(120),                            -- task-ul complet care ar fi fost apelat
    task_prefix     varchar(80)  NOT NULL,
    cache_key       char(64)     NOT NULL,
    saved_cost_usd  numeric(12,6)                            -- = original_cost_usd al rândului lovit
);
CREATE INDEX IF NOT EXISTS ai_cache_hit_log_day_idx ON ai_cache_hit_log (created_at, task_prefix);

-- Configurare (inertă: enabled=false). Codul are aceleași implicite; rândurile există ca să fie
-- vizibile și editabile.
INSERT INTO settings (key, value, description, updated_by, updated_at) VALUES
  ('ai_cache.enabled', 'false'::jsonb,
   'T3-L1: cache de rezultat AI activ (true/false).', 'migration', now()),
  ('ai_cache.prefixes',
   '["doc_segment","doc_classify_vision","doc_classify","doc_extract","doc_extract_vision","doc_vision_ocr","doc_rename","doc_autogroup","op_series"]'::jsonb,
   'T3-L1: prefixele de task (funcția, fără slug/hash) pentru care se folosește cache-ul.', 'migration', now()),
  ('ai_cache.epoch', '1'::jsonb,
   'T3-L1: intră în cheie; crescut manual invalidează tot cache-ul.', 'migration', now()),
  ('ai_cache.ttl_days', '10'::jsonb,
   'T3-L1: câte zile e valid un rezultat din cache.', 'migration', now())
ON CONFLICT (key) DO NOTHING;

SELECT 'migration 20260929_ai_result_cache applied' AS status;
