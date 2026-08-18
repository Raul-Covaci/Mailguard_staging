-- Satisfacție clienți — motor V6 (prompt V6 + traiectorie continuă), SINGURA versiune de încadrare.
-- Idempotentă, aditivă. Fără DDL — doar configurarea motorului.
--
-- Motorul citește DOAR cheia `satisfaction.v6` (fallback-ul pe `satisfaction.v4` a fost eliminat
-- din cod odată cu ștergerea motoarelor vechi). Revert fără redeploy, editând valoarea:
--   month_aggregation = "weighted_avg_weeks"  → scorul lunii redevine media ponderată pe interacțiuni
--   carry_start_state = false                 → fiecare lună repornește de la neutru (50)
--   model_hint        = "claude-haiku-4-5-20251001" → revenire la modelul folosit până la V6

INSERT INTO settings (key, value, description)
VALUES (
    'satisfaction.v6',
    '{"mode": "iris_trajectory_v6",
      "prompt_version": "V6",
      "single_kpi": "iris_stare_finala",
      "carry_start_state": true,
      "start_lookback_months": 3,
      "month_aggregation": "last_week_final",
      "model_hint": "claude-sonnet-4-6"}'::jsonb,
    'Config motor satisfacție V6 — punct de start reportat, agregare pe recență, model IRIS'
)
ON CONFLICT (key) DO NOTHING;

-- Cheia veche nu mai e citită de nimeni; o lăsăm în tabelă ar însemna o configurare fantomă,
-- care pare activă la o inspecție viitoare. Valorile ei sunt documentate în
-- migrations/20260724_satisfaction_v4_config.sql, deci nu se pierde nimic.
DELETE FROM settings WHERE key = 'satisfaction.v4';
