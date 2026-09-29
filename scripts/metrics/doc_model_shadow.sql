-- T3-O14 — Sonnet vs Haiku pe vision-ul de clasificare (doc_model_shadow). Doar citire.
-- Fereastra: ultimele 14 zile (de schimbat în `params`). Trei rezultate, rulate împreună:
--   psql ... -f scripts/metrics/doc_model_shadow.sql
--
-- Pragul de comutare pe „Haiku întâi" îl decide Raul (propunere: match_pct >= 95 pe type_id,
-- pe un eșantion de 5-7 zile).

-- 1) Concordanța pe task: match (type_id + documents[] / starts_new), invalid = Haiku respins de apelant.
WITH params AS (SELECT now() - interval '14 days' AS since)
SELECT s.task_prefix,
       count(*)                                                      AS samples,
       count(*) FILTER (WHERE NOT s.haiku_valid)                     AS haiku_invalid,
       round(100.0 * count(*) FILTER (WHERE s.match) / NULLIF(count(*), 0), 2) AS match_pct,
       round(100.0 * count(*) FILTER (WHERE s.sonnet_type_id IS NOT DISTINCT FROM s.haiku_type_id)
             / NULLIF(count(*), 0), 2)                               AS type_id_match_pct
  FROM doc_model_shadow s, params p
 WHERE s.created_at >= p.since
 GROUP BY 1
 ORDER BY 1;

-- 2) Matricea diferențelor pe type_id (doar unde modelele NU sunt de acord).
WITH params AS (SELECT now() - interval '14 days' AS since)
SELECT s.task_prefix, s.sonnet_type_id, s.haiku_type_id, count(*) AS n
  FROM doc_model_shadow s, params p
 WHERE s.created_at >= p.since
   AND s.sonnet_type_id IS DISTINCT FROM s.haiku_type_id
 GROUP BY 1, 2, 3
 ORDER BY 1, n DESC;

-- 3) Costul pe model, pe aceleași apeluri-eșantion (media per apel și totalul).
WITH params AS (SELECT now() - interval '14 days' AS since)
SELECT s.task_prefix,
       count(*)                                  AS samples,
       round(avg(s.sonnet_cost_usd), 6)          AS sonnet_cost_per_call,
       round(avg(s.haiku_cost_usd), 6)           AS haiku_cost_per_call,
       COALESCE(sum(s.sonnet_cost_usd), 0)       AS sonnet_cost_total,
       COALESCE(sum(s.haiku_cost_usd), 0)        AS haiku_cost_total
  FROM doc_model_shadow s, params p
 WHERE s.created_at >= p.since
 GROUP BY 1
 ORDER BY 1;
