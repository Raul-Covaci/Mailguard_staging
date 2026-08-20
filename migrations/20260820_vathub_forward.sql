-- VATHUB redirect (2026-08-20)
-- Mailurile oficiale de recuperare TVA ajung pe căsuța personală a Dianei.
-- Poller-ul de căsuțe personale le identifică după expeditor și le retrimite
-- prin SMTP către adresa generală vathub@cargotrack.ro, de unde le citește
-- aplicația VATHUB. Mailul original rămâne necitit în inbox.
--
-- Idempotentă și aditivă — se poate rula de mai multe ori.

BEGIN;

-- ── Cont personal: credențiale SMTP pentru forward ────────────────────────────
-- Parola SMTP e criptată separat de cea IMAP (credential_crypto). Dacă
-- smtp_cred_enc e NULL dar smtp_host e completat, se refolosește parola IMAP —
-- cazul obișnuit, unde furnizorul cere aceleași credențiale pe ambele protocoale.
-- smtp_port e INTEGER, nu SMALLINT: porturile valide urcă la 65535, iar SMALLINT
-- se oprește la 32767 (limitare pe care `imap_port` o are din start).
ALTER TABLE personal_mailbox_accounts ADD COLUMN IF NOT EXISTS smtp_host      VARCHAR(255);
ALTER TABLE personal_mailbox_accounts ADD COLUMN IF NOT EXISTS smtp_port      INTEGER;
ALTER TABLE personal_mailbox_accounts ALTER COLUMN smtp_port TYPE INTEGER;
ALTER TABLE personal_mailbox_accounts ADD COLUMN IF NOT EXISTS smtp_tls       BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE personal_mailbox_accounts ADD COLUMN IF NOT EXISTS smtp_user      VARCHAR(320);
ALTER TABLE personal_mailbox_accounts ADD COLUMN IF NOT EXISTS smtp_cred_enc  TEXT;
ALTER TABLE personal_mailbox_accounts ADD COLUMN IF NOT EXISTS vathub_enabled BOOLEAN NOT NULL DEFAULT FALSE;

-- ── Comutator de filtrare spam/carantină, per căsuță ──────────────────────────
-- OFF = mailurile se mai ingerează (metadata), dar NU se scanează și NU se mută
-- în SPAM/CARANTINA. Implicit ON, ca să nu schimbe comportamentul căsuțelor
-- existente. Redirectul VATHUB e independent: merge și cu filtrarea oprită.
ALTER TABLE personal_mailbox_accounts ADD COLUMN IF NOT EXISTS filter_enabled BOOLEAN NOT NULL DEFAULT TRUE;

-- ── Mail personal: starea redirectului ────────────────────────────────────────
-- vathub_match = intrarea din listă care a potrivit (domeniu sau adresă), NULL
-- dacă mailul nu e de la o autoritate. vathub_forwarded_at NULL + match nenul
-- = de trimis; se reia la fiecare poll până reușește sau până trece de
-- vathub_attempts maxim.
ALTER TABLE personal_mails ADD COLUMN IF NOT EXISTS vathub_match        TEXT;
ALTER TABLE personal_mails ADD COLUMN IF NOT EXISTS vathub_matched_at   TIMESTAMPTZ;
ALTER TABLE personal_mails ADD COLUMN IF NOT EXISTS vathub_forwarded_at TIMESTAMPTZ;
ALTER TABLE personal_mails ADD COLUMN IF NOT EXISTS vathub_attempts     INTEGER NOT NULL DEFAULT 0;
ALTER TABLE personal_mails ADD COLUMN IF NOT EXISTS vathub_error        TEXT;

-- Coadă de trimis: rândurile potrivite dar netrimise, per cont.
CREATE INDEX IF NOT EXISTS idx_personal_mails_vathub_queue
    ON personal_mails (account_id, imap_uid)
    WHERE vathub_match IS NOT NULL AND vathub_forwarded_at IS NULL;

-- ── Lista de expeditori + configul redirectului ───────────────────────────────
-- Format identic cu celelalte liste de expeditori (settings KV, valoare -> meta),
-- ca să refolosească normalizarea și UI-ul existent.
--   muted = true  → intrare EXTRASĂ DIN TRAFIC, dar NEVALIDATĂ încă de fete.
--                   Motorul o ignoră; devine activă când e debifată din UI.
-- Cele 13 domenii de mai jos vin din analiza traficului 15 iul – 13 aug 2026
-- (392 forwarduri interne + 204 mailuri directe între cele 5 adrese).
INSERT INTO settings (key, value, description, updated_by, updated_at)
VALUES (
    'vathub.redirect',
    '{
      "target": "vathub@cargotrack.ro",
      "enabled": false,
      "domains": {
        "mfinante.ro":       {"muted": true, "source": "analiza_2026-08-20", "note": "MF — decizii declaratia 318 (369 mailuri)"},
        "anaf.ro":           {"muted": true, "source": "analiza_2026-08-20", "note": "ANAF — SPV, formular 150 (6)"},
        "financnasprava.sk": {"muted": true, "source": "analiza_2026-08-20", "note": "SK — Financna sprava (126)"},
        "nav.gov.hu":        {"muted": true, "source": "analiza_2026-08-20", "note": "HU — NAV, incl. elekafa.nav.gov.hu (94)"},
        "nra.bg":            {"muted": true, "source": "analiza_2026-08-20", "note": "BG — NAP, incl. ro22.nra.bg (47)"},
        "fs.gov.cz":         {"muted": true, "source": "analiza_2026-08-20", "note": "CZ — Financni sprava (20)"},
        "aade.gr":           {"muted": true, "source": "analiza_2026-08-20", "note": "GR — Greek Tax Administration (5)"},
        "correo.aeat.es":    {"muted": true, "source": "analiza_2026-08-20", "note": "ES — Agencia Tributaria (4)"},
        "bmf.gv.at":         {"muted": true, "source": "analiza_2026-08-20", "note": "AT — Bundesministerium fur Finanzen (4)"},
        "mf.gov.pl":         {"muted": true, "source": "analiza_2026-08-20", "note": "PL — Ministerstwo Finansow (4)"},
        "vmi.lt":            {"muted": true, "source": "analiza_2026-08-20", "note": "LT — VMI (2)"},
        "at.gov.pt":         {"muted": true, "source": "analiza_2026-08-20", "note": "PT — Autoridade Tributaria (2)"},
        "porezna-uprava.hr": {"muted": true, "source": "analiza_2026-08-20", "note": "HR — Porezna uprava (1)"}
      },
      "addresses": {}
    }'::jsonb,
    'Redirect VATHUB: expeditori de autoritate fiscala + adresa tinta',
    'migration_20260820',
    NOW()
)
ON CONFLICT (key) DO NOTHING;

COMMIT;
