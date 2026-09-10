-- Sync-ul snapshot al view-ului `client_contact_email_log` revine de la 5 la 60 de minute.
--
-- De ce: view-ul are ~1,07M randuri si e in mod `snapshot`, deci FIECARE rulare aduce tot
-- setul si face DELETE + INSERT integral. La 5 minute inseamna ~34 rulari complete pe zi.
-- Impreuna cu acumularea in memorie din `_fetch_pages` (reparata separat, prin streaming
-- per pagina) asta a dus la workeri gunicorn ucisi de OOM killer: 27 aug (5,7 GB),
-- 28 aug (7,5 GB), 7 sept 04:26 (7,8 GB) si 7 sept 13:11 (8,8 GB) — 15 GB RAM pe server.
--
-- Cei 5 minute nu au fost pusi de mana: vin din `20260825_dv_autosync_email_log.sql`
-- (`LEAST(auto_sync_interval_minutes, 5)`). De aceea corectia trebuie sa fie tot o migratie —
-- un UPDATE dat direct pe staging nu ar ajunge niciodata pe productie.
--
-- 60 = valoarea implicita a aplicatiei (`DEFAULT_AUTO_SYNC_MINUTES`). Prospetimea raportului
-- de departamente scade la max 60 min. Cand view-ul trece pe `incremental` pe partea IRIS
-- (rundile schimbate, nu tot istoricul), intervalul poate cobori la loc in siguranta.
--
-- `client_contact_email_department_log` NU se atinge: e deja incremental, deci o rulare la
-- 5 minute aduce doar delta.

UPDATE iris_dv_state
   SET auto_sync_interval_minutes = 60,
       updated_at = NOW()
 WHERE view_name = 'client_contact_email_log'
   AND auto_sync_interval_minutes < 60;

SELECT 'migration 20260910_dv_email_log_sync_interval applied' AS status;
