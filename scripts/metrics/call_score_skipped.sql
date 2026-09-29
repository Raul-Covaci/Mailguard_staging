-- T3-S1 — pe zi (Europe/Bucharest): apeluri telefonice scorate vs. sărite ca „prea scurt pentru
-- scorare" (call_ai_scores.skip_reason='too_short'). Fereastra: ultimele 30 de zile. Doar citire.
--   psql ... -f scripts/metrics/call_score_skipped.sql

WITH params AS (SELECT now() - interval '30 days' AS since)
SELECT (cas.scored_at AT TIME ZONE 'Europe/Bucharest')::date                  AS day,
       count(*) FILTER (WHERE cas.skip_reason IS NULL)                         AS scored,
       count(*) FILTER (WHERE cas.skip_reason = 'too_short')                   AS skipped_too_short,
       round(100.0 * count(*) FILTER (WHERE cas.skip_reason = 'too_short')
             / NULLIF(count(*), 0), 2)                                          AS skipped_pct
  FROM call_ai_scores cas, params p
 WHERE cas.scored_at >= p.since
 GROUP BY 1
 ORDER BY 1 DESC;
