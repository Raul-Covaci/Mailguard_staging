-- Fereastra de procesare documente: 1 zi, obligatoriu (decizie Raul Covaci, 2026-08-26).
--
-- Niciun document dintr-un mail mai vechi de o zi fata de azi nu se mai proceseaza — indiferent de
-- actiune (cron, „Proceseaza tot", „Reproceseaza ID-uri", reset-reimport). Plafonul e DUBLU:
--   * aici, in valoarea din settings (citita si de app/services/doc_window.py, si de
--     scripts/storage_cleanup.sh pentru retentie);
--   * in cod, `doc_window.HARD_MAX_DAYS = 1` — o valoare mai mare pusa ulterior in DB e ignorata.
-- Migratia 20260826b a seedat 2 zile; aici FORTAM 1 (UPDATE, nu ON CONFLICT DO NOTHING, tocmai ca
-- sa prinda si bazele deja seedate).

INSERT INTO settings(key, value, description, updated_by, updated_at)
VALUES ('documents.process_window',
        jsonb_build_object('days', 1),
        'Fereastra de procesare documente (zile). Plafon dur in cod: 1. Retentia din storage_cleanup.sh foloseste ACELASI numar.',
        'migration:20260826c_doc_process_window_1day',
        now())
ON CONFLICT (key) DO UPDATE SET
    value       = jsonb_set(COALESCE(settings.value, '{}'::jsonb), '{days}', '1'::jsonb),
    description = EXCLUDED.description,
    updated_by  = EXCLUDED.updated_by,
    updated_at  = now();
