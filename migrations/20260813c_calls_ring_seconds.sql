-- Timpul de asteptare pana la raspuns pe apelurile While1.
--
-- CONTEXT. Canalul "Apeluri" din productivitate se muta de pe `cts_calls_ground_truth` pe
-- ingestul While1 (`calls`), pentru ca CTS logheaza doar apelurile devenite tichet -- circa 15%
-- din cele raspunse. Masurat pe date reale: pe 12.08 While1 are 470 de apeluri primite, CTS are
-- 272 in total; pentru Oana Lasca, 16 primite pe 10.08 in While1 fata de 2 in CTS.
--
-- PROBLEMA pe care o rezolva migrarea: SLA-ul de apel se masoara pe timpul pana la raspuns.
-- In CTS acesta exista (`cts_response_seconds` = ring_seconds), dar `calls` NU il stocheaza,
-- desi While1 il trimite in CDR ca `ring_time` (vezi antetul while1_ingest.py). Fara coloana
-- de mai jos, mutarea pe While1 ar aduce volumul corect dar ar lasa obiectivul nemasurabil.
--
-- Coloana ramane NULL pe randurile deja ingerate: While1 se interogheaza cu un cursor pe id
-- care merge doar inainte, iar `ON CONFLICT (call_id)` nu actualiza decat `client_id`. Pana la
-- backfill, timpul de raspuns se ia din suprapunerea cu CTS (COALESCE in productivity.py), deci
-- procentul "in timp" se calculeaza pe subsetul cunoscut, iar volumul e complet.

ALTER TABLE calls
    ADD COLUMN IF NOT EXISTS ring_seconds INT;

-- Interogarile de productivitate filtreaza apelurile primite pe luna/interval; indexul acopera
-- exact acest tipar (directie + moment), fara sa creasca inutil scrierea la ingest.
CREATE INDEX IF NOT EXISTS idx_calls_direction_started
    ON calls (direction, started_at);
