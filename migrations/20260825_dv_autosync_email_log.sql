-- Sincronizare automata la 5 minute pentru sursele raportului de departamente.
--
-- `iris_dv_state.auto_sync` exista din 20260723_iris_dv_autosync.sql, dar nimic nu o citea:
-- nu exista nici endpoint (butonul din „Surse date" dadea 404), nici rulare din cron. Din
-- 2026-08-25 cronul de 5 min (POST /process/run-now -> iris_dv_autosync.run_due_syncs)
-- sincronizeaza view-urile marcate aici — inclusiv pe productie, unde nimeni nu apasa butonul.
--
-- Randurile se creeaza chiar daca view-ul nu a fost inca sincronizat manual: `last_sync_at`
-- NULL inseamna „due imediat", deci prima rulare de cron dupa deploy il aduce.
-- `mode` ramane NULL -> se rezolva din /onboarding la prima rulare (client_contact_email_log =
-- snapshot, client_contact_email_department_log = incremental).

INSERT INTO iris_dv_state (view_name, auto_sync, auto_sync_interval_minutes)
VALUES ('client_contact_email_log', TRUE, 5),
       ('client_contact_email_department_log', TRUE, 5)
ON CONFLICT (view_name) DO UPDATE
   SET auto_sync = TRUE,
       auto_sync_interval_minutes = LEAST(iris_dv_state.auto_sync_interval_minutes, 5),
       updated_at = NOW();
