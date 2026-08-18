-- v2.17.0: coloane noi pentru scripturile de analiză apeluri V2
-- (issueResolution V2, agentScore V3 + transparency, agentActions V2,
--  agentAdviceNextSteps NOU, customerAdditionalRequests NOU)

ALTER TABLE call_ai_scores
  -- agentScore V3: a 6-a dimensiune
  ADD COLUMN IF NOT EXISTS agent_transparency            integer,
  -- issueResolution V2
  ADD COLUMN IF NOT EXISTS issue_main_problem            text,
  ADD COLUMN IF NOT EXISTS issue_main_solution           text,
  ADD COLUMN IF NOT EXISTS issue_within_company_scope    boolean,
  -- agentActions V2
  ADD COLUMN IF NOT EXISTS agent_next_steps_clear        boolean,
  ADD COLUMN IF NOT EXISTS agent_next_steps_observation  text,
  -- agentAdviceNextSteps (prompt nou)
  ADD COLUMN IF NOT EXISTS agent_advice_next_steps       text,
  -- customerAdditionalRequests (prompt nou)
  ADD COLUMN IF NOT EXISTS customer_additional_requests  jsonb,
  ADD COLUMN IF NOT EXISTS customer_unacknowledged_count integer;

-- Prompturile noi: se inserează goale dacă lipsesc; textul real se încarcă din
-- app/services/prompts/calls/*.txt via scripts/sync_call_prompts.py (sau din UI).
INSERT INTO call_scoring_prompts (key, label, prompt_text, enabled, output_type, output_schema)
VALUES
  ('agentAdviceNextSteps', 'Sfat agent – pași următori',
   '[Prompt agentAdviceNextSteps — rulează scripts/sync_call_prompts.py]', true, 'advice',
   '{"observation": "str", "advice": "str"}'::jsonb),
  ('customerAdditionalRequests', 'Cereri suplimentare client',
   '[Prompt customerAdditionalRequests — rulează scripts/sync_call_prompts.py]', true, 'json',
   '{"additionalRequests": "list", "unacknowledgedCount": "int"}'::jsonb)
ON CONFLICT (key) DO NOTHING;
