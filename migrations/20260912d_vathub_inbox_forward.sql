-- VATHUB — redirectul se mută din căsuțele personale în CĂSUȚA PRINCIPALĂ (2026-09-12)
--
-- Până acum mailurile autorităților fiscale erau citite din căsuța personală a
-- persoanei care depusese declarația 318 (poller IMAP, `personal_mails`). Cerința
-- nouă: sursa e căsuța principală — aceeași din care se alimentează pagina
-- „Email-uri" (`emails`) — iar orice mail de la o adresă/domeniu din listă se
-- forwardează spre vathub@cargotrack.ro.
--
-- Idempotentă și aditivă. NU șterge nimic din calea veche: coloanele
-- `personal_mails.vathub_*` și configul rămân, iar comutarea se face prin cheia
-- `source` din config ("inbox" | "personal" | "both"), deci e reversibilă fără deploy.

BEGIN;

-- ── Coada de forward pentru căsuța principală ────────────────────────────────
-- Tabelă separată, NU coloane pe `emails`: `emails` are milioane de rânduri, iar
-- un `vathub_matched_at IS NULL` ar fi adevărat pe TOATE la instalare — index
-- inutilizabil și un UPDATE de masă ca să-l cureți. Aici intră doar mailurile
-- care au potrivit o regulă, deci tabela rămâne mică.
CREATE TABLE IF NOT EXISTS vathub_inbox_forward (
    id            BIGSERIAL PRIMARY KEY,
    email_id      BIGINT NOT NULL UNIQUE REFERENCES emails(id) ON DELETE CASCADE,
    from_address  VARCHAR(320),
    subject       TEXT,
    received_at   TIMESTAMPTZ,
    matched_rule  TEXT NOT NULL,
    target        VARCHAR(320),
    status        VARCHAR(16) NOT NULL DEFAULT 'pending',  -- pending|sent|failed|blocked
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    source        VARCHAR(16) NOT NULL DEFAULT 'auto',     -- auto|manual|backfill
    matched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    forwarded_at  TIMESTAMPTZ
);

-- Coada de trimis: doar rândurile nerezolvate, deci indexul rămâne minuscul.
CREATE INDEX IF NOT EXISTS idx_vathub_inbox_pending
    ON vathub_inbox_forward (id)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_vathub_inbox_matched_at
    ON vathub_inbox_forward (matched_at DESC);

COMMENT ON TABLE vathub_inbox_forward IS 'Mailuri din casuta principala potrivite de regulile VATHUB si retrimise spre vathub@cargotrack.ro';

-- ── Config: aceeași cheie, extinsă ───────────────────────────────────────────
-- `source` decide CINE face redirectul. Implicit "inbox" — calea din căsuțele
-- personale devine no-op fără să fie ștearsă.
-- Lista de mai jos e cea validată de Raul Covaci (2026-09-11, 29 poziții +
-- domeniile lor). Intrările seedate anterior ca `muted` sunt ACTIVATE aici:
-- merge-ul jsonb `||` lasă partea din dreapta să câștige.
-- Notă: `gov.si` și `anaf.ro` sunt domenii largi (tot guvernul sloven, respectiv
-- tot ANAF-ul) — asumat, la cererea explicită din listă; se pot restrânge din UI.
INSERT INTO settings (key, value, description, updated_by, updated_at)
VALUES (
    'vathub.redirect',
    '{
      "target": "vathub@cargotrack.ro",
      "enabled": true,
      "source": "inbox",
      "max_age_hours": 24,
      "domains": {
        "mfinante.ro":                        {"muted": false, "note": "RO — ANAF / MF, decizii declaratia 318"},
        "anaf.ro":                            {"muted": false, "note": "RO — ANAF, SPV"},
        "bmf.gv.at":                          {"muted": false, "note": "AT — Bundesministerium fur Finanzen"},
        "minfin.fed.be":                      {"muted": false, "note": "BE — SPF Finances"},
        "nra.bg":                             {"muted": false, "note": "BG — NAP (prinde si ro22.nra.bg)"},
        "fs.gov.cz":                          {"muted": false, "note": "CZ — Financni sprava"},
        "fs.mfcr.cz":                         {"muted": false, "note": "CZ — VAT refund"},
        "fa-chemnitz-sued.smf.sachsen.de":    {"muted": false, "note": "DE — Finanzamt Chemnitz-Sud"},
        "bzst.bund.de":                       {"muted": false, "note": "DE — BZSt VAT refund"},
        "sktst.dk":                           {"muted": false, "note": "DK — Skattestyrelsen"},
        "correo.aeat.es":                     {"muted": false, "note": "ES — Agencia Tributaria"},
        "dgfip.finances.gouv.fr":             {"muted": false, "note": "FR — DGFiP, SR-TVA DINR"},
        "aade.gr":                            {"muted": false, "note": "GR — AADE"},
        "porezna-uprava.hr":                  {"muted": false, "note": "HR — Porezna uprava"},
        "nav.gov.hu":                         {"muted": false, "note": "HU — NAV (prinde si elekafa.nav.gov.hu)"},
        "agenziaentrate.it":                  {"muted": false, "note": "IT — Agenzia delle Entrate"},
        "vmi.lt":                             {"muted": false, "note": "LT — VMI"},
        "en.etat.lu":                         {"muted": false, "note": "LU — Administration de l Enregistrement"},
        "mf.gov.pl":                          {"muted": false, "note": "PL — Ministerstwo Finansow"},
        "at.gov.pt":                          {"muted": false, "note": "PT — Autoridade Tributaria"},
        "skatteverket.se":                    {"muted": false, "note": "SE — Skatteverket"},
        "gov.si":                             {"muted": false, "note": "SI — FURS (domeniu larg, asumat)"},
        "financnasprava.sk":                  {"muted": false, "note": "SK — Financna sprava"}
      },
      "addresses": {
        "monitorizare.vatrefund@mfinante.ro":                 {"muted": false, "note": "ANAF"},
        "admin.portal@mfinante.ro":                           {"muted": false, "note": "ANAF 2"},
        "autoritate.mfp@mfinante.ro":                         {"muted": false, "note": "ANAF 3"},
        "portal.anaf@anaf.ro":                                {"muted": false, "note": "ANAF 4"},
        "lorenz.hofer@bmf.gv.at":                             {"muted": false, "note": "AT"},
        "foreigners.team2@minfin.fed.be":                     {"muted": false, "note": "BE"},
        "odop_sofia@nra.bg":                                  {"muted": false, "note": "BG"},
        "b.stoilova@ro22.nra.bg":                             {"muted": false, "note": "BG 2"},
        "kamila.maresova2@fs.gov.cz":                         {"muted": false, "note": "CZ"},
        "cz_vat_refund@fs.mfcr.cz":                           {"muted": false, "note": "CZ 2"},
        "poststelle@fa-chemnitz-sued.smf.sachsen.de":         {"muted": false, "note": "DE"},
        "vatrefund-de@bzst.bund.de":                          {"muted": false, "note": "DE 2"},
        "ewa.binder-rogacka@sktst.dk":                        {"muted": false, "note": "DK"},
        "ivanes@correo.aeat.es":                              {"muted": false, "note": "ES"},
        "agenciatributaria@correo.aeat.es":                   {"muted": false, "note": "ES 2"},
        "sr-tva.dinr@dgfip.finances.gouv.fr":                 {"muted": false, "note": "FR"},
        "e.pappas1@aade.gr":                                  {"muted": false, "note": "GR"},
        "odjel.stranci@porezna-uprava.hr":                    {"muted": false, "note": "HR"},
        "kavig@nav.gov.hu":                                   {"muted": false, "note": "HU"},
        "elekafa@elekafa.nav.gov.hu":                         {"muted": false, "note": "HU 2"},
        "stefania.smargiassi@agenziaentrate.it":              {"muted": false, "note": "IT"},
        "vilniaus.apskr.rastai@vmi.lt":                       {"muted": false, "note": "LT"},
        "vatrefund@en.etat.lu":                               {"muted": false, "note": "LU"},
        "marta.franczak@mf.gov.pl":                           {"muted": false, "note": "PL"},
        "dsr@at.gov.pt":                                      {"muted": false, "note": "PT"},
        "dsiva-vatrefund@at.gov.pt":                          {"muted": false, "note": "PT 2"},
        "svar@skatteverket.se":                               {"muted": false, "note": "SE"},
        "sonja.svetanic@gov.si":                              {"muted": false, "note": "SI"},
        "vatrefundslovakia@financnasprava.sk":                {"muted": false, "note": "SK"}
      }
    }'::jsonb,
    'Redirect VATHUB: expeditori de autoritate fiscala + adresa tinta (sursa: casuta principala)',
    'migration_20260912d',
    NOW()
)
ON CONFLICT (key) DO UPDATE SET
    value = settings.value
            || jsonb_build_object(
                 'source',  'inbox',
                 'enabled', TRUE,
                 'target',  COALESCE(settings.value ->> 'target', 'vathub@cargotrack.ro'),
                 'max_age_hours', COALESCE((settings.value ->> 'max_age_hours')::int, 24),
                 'domains',   COALESCE(settings.value -> 'domains',   '{}'::jsonb) || (EXCLUDED.value -> 'domains'),
                 'addresses', COALESCE(settings.value -> 'addresses', '{}'::jsonb) || (EXCLUDED.value -> 'addresses')
               ),
    description = EXCLUDED.description,
    updated_by  = EXCLUDED.updated_by,
    updated_at  = NOW();

-- ── Cursorul de scanare ──────────────────────────────────────────────────────
-- Motorul scanează `emails` DUPĂ id (index PK), nu după un flag pe rând. Cursorul
-- pornește de la ultimul id existent: la instalare NU se retrimite istoricul.
-- Backfill-ul se face explicit, din UI (buton „Caută în ultimele N zile").
-- ON CONFLICT DO NOTHING — o re-rulare a migrației nu are voie să dea cursorul înapoi.
INSERT INTO settings (key, value, description, updated_by, updated_at)
SELECT 'vathub.inbox_cursor',
       to_jsonb(COALESCE(MAX(id), 0)),
       'VATHUB inbox: ultimul emails.id scanat pentru potrivire',
       'migration_20260912d',
       NOW()
  FROM emails
ON CONFLICT (key) DO NOTHING;

COMMIT;
