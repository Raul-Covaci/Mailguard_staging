-- REPARAȚIE DRIFT SCHEMA (producție, 2026-08-19).
--
-- Diagnostic pe producție: `_release_migrations` marchează ca aplicate
-- `20260629_client_contract_category.sql` și baseline-ul de pe `clients`, dar coloanele NU există
-- efectiv în schemă (marcare fără execuție completă). Efect: sync-ul de clienți cădea la primul
-- INSERT (`column "category" does not exist` / `"updated_at" does not exist`), tranzacția se aborta
-- și toate instrucțiunile următoare întorceau doar „current transaction is aborted...". Rezultat:
-- `client_vehicles` și `client_contracts` cu 0 rânduri, vehiculele/contractele goale în UI.
--
-- Fișier NOU (nume nou => `migrate.sh` îl rulează chiar dacă vechile migrații sunt marcate aplicate).
-- Strict aditiv și idempotent: pe mediile unde coloanele există deja, e no-op.
-- Vezi și v3.0.1 (SAVEPOINT per client în iris_sync) — de acum eroarea reală apare direct în UI.

BEGIN;

-- clients: coloane scrise de sync (`updated_at` e în INSERT-ul de upsert), plus `created_at`.
ALTER TABLE clients ADD COLUMN IF NOT EXISTS created_at  timestamptz DEFAULT now();
ALTER TABLE clients ADD COLUMN IF NOT EXISTS updated_at  timestamptz DEFAULT now();
ALTER TABLE clients ADD COLUMN IF NOT EXISTS cui         text;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS company_id  integer;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS emails                  jsonb DEFAULT '[]'::jsonb;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS internal_contact_emails jsonb DEFAULT '[]'::jsonb;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS phones                  jsonb DEFAULT '[]'::jsonb;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS is_active      boolean DEFAULT true;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS email_priority integer;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS last_synced_at timestamptz;

-- client_contracts: `category` lipsea pe producție (indexul depinde de ea).
ALTER TABLE client_contracts ADD COLUMN IF NOT EXISTS category      text;
ALTER TABLE client_contracts ADD COLUMN IF NOT EXISTS contract_no   text;
ALTER TABLE client_contracts ADD COLUMN IF NOT EXISTS cui           text;
ALTER TABLE client_contracts ADD COLUMN IF NOT EXISTS documents     jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE client_contracts ADD COLUMN IF NOT EXISTS vehicles      jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE client_contracts ADD COLUMN IF NOT EXISTS raw           jsonb;
ALTER TABLE client_contracts ADD COLUMN IF NOT EXISTS synced_at     timestamptz NOT NULL DEFAULT now();

-- client_vehicles: complet pe producție azi, reasertat pentru siguranță.
ALTER TABLE client_vehicles ADD COLUMN IF NOT EXISTS vin       text;
ALTER TABLE client_vehicles ADD COLUMN IF NOT EXISTS status    text;
ALTER TABLE client_vehicles ADD COLUMN IF NOT EXISTS documents jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE client_vehicles ADD COLUMN IF NOT EXISTS raw       jsonb;
ALTER TABLE client_vehicles ADD COLUMN IF NOT EXISTS synced_at timestamptz NOT NULL DEFAULT now();

-- Indexurile care depind de coloanele de mai sus (cel pe `category` lipsea pe producție).
CREATE INDEX IF NOT EXISTS client_contracts_category_idx
    ON client_contracts (lower(COALESCE(category, '')));
CREATE INDEX IF NOT EXISTS client_contracts_contract_no_idx
    ON client_contracts (lower(COALESCE(contract_no, '')));
CREATE INDEX IF NOT EXISTS client_contracts_cui_idx
    ON client_contracts (lower(COALESCE(cui, '')));
CREATE INDEX IF NOT EXISTS client_vehicles_vin_idx
    ON client_vehicles (vin) WHERE vin IS NOT NULL;

-- Indexurile UNICE pe care se sprijină ON CONFLICT din sync (există pe producție; reasertate ca
-- migrația să fie autosuficientă dacă se rulează pe un mediu refăcut de la zero).
CREATE UNIQUE INDEX IF NOT EXISTS client_vehicles_uidx
    ON client_vehicles (client_id, lower(COALESCE(plate, '')));
CREATE UNIQUE INDEX IF NOT EXISTS client_contracts_uidx
    ON client_contracts (
        client_id,
        lower(COALESCE(iris_contract_id, '')),
        lower(COALESCE(contract_type, '')),
        COALESCE(start_date, '0001-01-01'::date),
        COALESCE(end_date,   '0001-01-01'::date)
    );

COMMIT;
