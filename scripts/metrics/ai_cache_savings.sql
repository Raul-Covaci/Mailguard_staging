-- T3-L1 — economia cache-ului de rezultat AI, pe zi (Europe/Bucharest) și pe prefix de task.
--
--   hits           apeluri evitate (ai_cache_hit_log)
--   saved_cost_usd costul lor la apelul original
--   misses         apeluri REALE la gateway pe aceleași prefixe (ai_call_log; ok și eșuate)
--   miss_cost_usd  costul apelurilor reale
--   hit_ratio      hits / (hits + misses)
--
-- Prefixele = settings['ai_cache.prefixes'] (lista activă acum). Fereastra: ultimele 30 de zile —
-- de schimbat în `params` mai jos. Doar citire.
--
--   psql ... -f scripts/metrics/ai_cache_savings.sql

WITH params AS (
    SELECT now() - interval '30 days' AS since
),
prefixes AS (
    SELECT jsonb_array_elements_text(value) AS task_prefix
      FROM settings WHERE key = 'ai_cache.prefixes' AND jsonb_typeof(value) = 'array'
),
hits AS (
    SELECT (h.created_at AT TIME ZONE 'Europe/Bucharest')::date AS day, h.task_prefix,
           count(*) AS hits, COALESCE(sum(h.saved_cost_usd), 0) AS saved_cost_usd
      FROM ai_cache_hit_log h, params p
     WHERE h.created_at >= p.since
     GROUP BY 1, 2
),
calls AS (
    -- Același prefix ca ai_cache.task_prefix(): fără „cargo360:", până la primul „:".
    SELECT (c.created_at AT TIME ZONE 'Europe/Bucharest')::date AS day,
           split_part(regexp_replace(c.task, '^cargo360:', ''), ':', 1) AS task_prefix,
           count(*) AS misses, COALESCE(sum(c.cost_usd), 0) AS miss_cost_usd
      FROM ai_call_log c, params p
     WHERE c.created_at >= p.since
     GROUP BY 1, 2
)
SELECT COALESCE(h.day, c.day)                  AS day,
       COALESCE(h.task_prefix, c.task_prefix)  AS task_prefix,
       COALESCE(h.hits, 0)                     AS hits,
       COALESCE(h.saved_cost_usd, 0)           AS saved_cost_usd,
       COALESCE(c.misses, 0)                   AS misses,
       COALESCE(c.miss_cost_usd, 0)            AS miss_cost_usd,
       round(COALESCE(h.hits, 0)::numeric
             / NULLIF(COALESCE(h.hits, 0) + COALESCE(c.misses, 0), 0), 4) AS hit_ratio
  FROM hits h
  FULL JOIN calls c ON c.day = h.day AND c.task_prefix = h.task_prefix
 WHERE COALESCE(h.task_prefix, c.task_prefix) IN (SELECT task_prefix FROM prefixes)
 ORDER BY 1 DESC, 2;
