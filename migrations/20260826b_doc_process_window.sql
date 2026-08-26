-- Fereastra de procesare a documentelor (zile) — vezi app/services/doc_window.py.
--
-- Drain-ul proceseaza doar atasamente din mailuri primite in ultimele N zile. Retentia din
-- scripts/storage_cleanup.sh (DELETE din document_extractions) citeste ACEEASI valoare: daca
-- retentia ar fi mai scurta decat fereastra, un mail inca in fereastra si-ar pierde randurile la
-- miezul noptii si ar fi reprocesat (si replatit) a doua zi.

INSERT INTO settings(key, value, description, updated_by, updated_at)
VALUES ('documents.process_window',
        jsonb_build_object('days', 2),
        'Fereastra de procesare documente (zile). Retentia din storage_cleanup.sh foloseste ACELASI numar.',
        'migration:20260826b_doc_process_window',
        now())
ON CONFLICT (key) DO NOTHING;
