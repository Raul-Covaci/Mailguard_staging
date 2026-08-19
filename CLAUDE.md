# Cargo360 — Documentație Aplicație

**Versiune:** 1.0.0
**Data actualizare:** 2026-08-06
**URL public:** http://95.216.144.102:8501

---

## ⚠️ OBLIGATORIU după orice deploy care modifică `mg-app.js`

După orice `scp` / scriere a fișierului `app/ui/vendor/mg-app.js` pe server, rulează imediat:
```bash
ssh mailguard-staging "gzip -k -6 -f /opt/iris-mailguard/app/ui/vendor/mg-app.js"
```
Fără acest pas browserul primește fișierul necomprimat (1.2MB vs 270KB gzip) — pagina se încarcă de 4-5x mai greu.

Verificare: `ssh mailguard-staging "curl -sI -H 'Accept-Encoding: gzip' http://localhost:8501/vendor/mg-app.js | grep content-encoding"` → trebuie `content-encoding: gzip`.

---

## ⛔ REGULĂ CRITICĂ — trimitere feedback pe staging (decisă manual, 2026-07-16)

**NU trimite NICIODATĂ email de feedback către clienți reali pe staging.**
Singurele adrese permise pentru testare: `raul.covaci@cargotrack.ro`, `raul.covaci@trakosoft.ro`.

Decizie manuală a lui Raul Covaci — nu se relaxează fără aprobare explicită directă de la el.
Se aplică oricărui task viitor care trimite efectiv linkuri de feedback către clienți (ex. T5 — "trimitere efectivă").

Gardă tehnică: `app/services/feedback_send_guard.py` — funcția `assert_send_allowed(to_address)` TREBUIE
apelată chiar înainte de orice trimitere reală de email de feedback. Ridică `FeedbackSendBlocked` pentru
orice adresă în afara whitelist-ului, cât timp `MAILGUARD_ENV` nu e explicit `"production"`.

Orice agent care implementează trimiterea efectivă a linkurilor de feedback (T5+) TREBUIE să integreze
această gardă înainte de a apela furnizorul real de email (SMTP/O365/etc).

---


## 🎙️ ANALIZĂ APELURI — prompturi de scoring (2026-08-18, V2)

Sursa de adevăr pentru prompturile de scoring apeluri: **`app/services/prompts/calls/<key>.txt`**
(versionate în git). Tabela `call_scoring_prompts` este doar cache-ul rulat de motor.

După orice deploy care modifică fișierele de prompt:
```bash
ssh mailguard-staging "cd /opt/iris-mailguard && venv/bin/python3 scripts/sync_call_prompts.py"
```
(`--dry-run` arată diferențele fără scriere; se pot da chei individuale ca argumente.)

Prompturi curente: `issueResolution` (V2), `agentScore` (V3, 6 dimensiuni cu `transparency`),
`agentActions` (V2), `agentAdviceNextSteps` (nou), `customerAdditionalRequests` (nou).
Motor: `app/services/call_scorer.py` → `score_call()` / `score_batch()`.

**Întrebări binare (2026-08-19).** Setul e definit de `call_scorer.BINARY_COLUMNS` — sursa unică
pentru cardurile donut, pentru ordinea lor și pentru maparea în `call_ai_scores`. Cele 5 chei:
`masiniCareNuTransmit`, `clientulAmintaJudecata`, `agentulSaPrezentat`, `clientulAmintaRenuntare`,
`clientulContactatAnterior`. O întrebare binară e afișată DOAR dacă are `output_type='binary'` în
`call_scoring_prompts` **și** fișier de prompt în repo. Justificarea modelului se salvează în
`call_ai_scores.binary_evidence` (jsonb) — o întrebare binară nouă nu cere migrație, statisticile
o citesc din jsonb când nu are coloană proprie. `issueResolution` NU e binar (are 4 câmpuri).

**Atribuire apel → angajat: NICIODATĂ pe egalitate de nume.** `calls.agent_extension` e
`user_fullname` din CDR-ul While1, scris altfel decât `employee_department_mapping.name`
("Oana Lasca" vs "Lasca Oana-Maria"). Se folosesc cele 3 trepte din `productivity.py`
(`_APEL_AGENT_CTE`/`_APEL_AGENT_JOIN`); în analitice sunt în `calls_analytics._AGENT_MAP_SQL` +
`_agent_dept_filter()` (cache 5 min). Un filtru pe nume exact returnează 0 rânduri — a fost
exact bug-ul „doar Operational (toate) încarcă date".
Editarea din UI (Apeluri → Prompturi AI) rămâne posibilă, dar **se pierde la următorul sync** —
modificările durabile se fac în fișierul din repo.

---

## 🔀 MAIL-URI CTS — tab „Raport departamente" (2026-08-19)

Traseul unui mail prin departamente. Sursa: **`cts_department_moves`** — un rând per eveniment
(alocare inițială + fiecare schimbare de departament), populat de **trigger-ul**
`trg_cts_gt_department_move` pe `cts_ground_truth` (migrația `20260819_cts_department_moves.sql`,
singurul trigger din proiect; face și backfill din `cts_department_prev`/`changed_at`).

- API: `GET /cts-training/dept-report` (3 statistici + trasee) și `/dept-report/cases` (drill-down).
- UI: `CtsMailsShell` (tab-uri) → `CtsDeptReport` în `app/ui/vendor/mg-app.js`.
- Lanțul se reconstruiește per `message_id`; alocările inițiale ale **replicilor** (CTS face un tichet
  per destinatar) se colapsează la cea mai veche, altfel apar mutări inexistente.
- Limitare: sync la ~5 min ⇒ mutările din același interval se văd ca una singură; istoricul complet
  de alocări trebuie cerut de la CTS (task viitor). Cifrele sunt un minim, nu exact.

---

## ⏱️ PRODUCTIVITATE — fereastra de timp = PONTAJ (2026-08-19)

Minutele de lucru (SLA mailuri/task-uri/apeluri/operațiuni) se numără pe **acoperirea
departamentului**, iar sursa de adevăr e **`employee_attendance`** („Utilizatori → Pontaj pe
departamente", preluat din CTS sau ajustat manual), NU `department_schedule` (populată manual).

Precedență: pontaj cu ore în ziua respectivă → uniunea turelor celor prezenți; fără pontaj
utilizabil → `department_schedule`; fără nici program → ziua nu curge.

**Zi activă = ≥1 angajat pontat prezent.** Zi cu rânduri de pontaj dar 0 prezenți → inactivă. Zi
fără niciun rând → inactivă DOAR dacă toți angajații activi ai departamentului sunt în concediu
aprobat (`employee_schedule` / `cts_dv_employee_vacation_request`); altfel e gaură de sync și se
cade pe program (altfel o pană de pontaj ar face toate scorurile 100%).

⚠️ Logica există în DOUĂ locuri și trebuie schimbată în OGLINDĂ:
`_BizCache._dept_window` din `app/services/productivity.py` (mailuri, apeluri, operațiuni) și
funcția SQL `business_minutes_emp` (task-uri) — vezi
`migrations/20260819d_business_minutes_pontaj_first.sql`.

📞 **Apeluri — leg-uri duplicate.** Centrala scrie un CDR per canal apelat, deci un apel apare de
două ori: leg `NO ANSWER` de 0s + leg-ul răspuns. În LISTE/statistici se ascunde leg-ul nerăspuns
doar dacă are semnătura de ring paralel: `callee_number IS NULL` + sibling răspuns în ±2 min, sau
sibling în ±30 s (`productivity.apel_no_dup_leg_sql`). Reapelările clientului (sibling la minute
distanță, cu callee completat) rămân vizibile ca apeluri pierdute reale. **Ieșirile nu se
deduplică** — acolo un `BUSY`/`NO ANSWER` urmat de reușită e reapelare reală a operatorului. În CALCUL nu conta niciodată (se folosește `_APEL_REAL_CALL_SQL`). Timpul de răspuns vine din
`calls.ring_seconds`; pe rândurile vechi e NULL → „nemăsurat", se completează cu
`POST /calls/backfill-ring?date_from&date_to`.

Excluderi din calcul: **doar** flagul `clients.productivity_exclude` (fără liste hardcodate în cod).
Se aplică pe toate canalele, cu legături diferite per sursă: mail = `emails.client_id` + clientul
atribuit în CTS (`extra.client_id` = ID IRIS); task = ID IRIS + nume; apel = cheia locală;
operațiuni = doar nume (`device_operations.client_id` e NULL). Reclamațiile nu au client în feed.

---

> Istoric dezvoltare (note tehnice cronologice pre-2026-06-15): vezi CLAUDE-HISTORY.md

---

## 🔴 SATISFACȚIE CLIENȚI — motor curent: traiectorie V6 (2026-08-18)

Motorul activ NU mai e cel cu 5 factori de mai jos (rămas ca istoric) — e traiectoria IRIS:

- Prompt: `app/services/prompts/satisfaction_trajectory_v6.txt` — **singurul**; V4 și motoarele vechi (v1/v2 piloni, v3 holistic) au fost șterse.
- Cod: `compute_satisfaction_v6()` din `app/services/satisfaction_engine.py`.
- 1 apel IRIS per **săptămână ISO** cu interacțiuni; fiecare apel primește `stare_initiala`
  (prima săptămână = ultima lună cu scor din `client_satisfaction_snapshots`, restul = săptămâna
  precedentă). Scor lunar = **starea finală a ultimei săptămâni scorate** (nu media).
- **Model: `claude-sonnet-4-6`** (până la V6 se folosea implicitul gateway-ului = Claude Haiku 4.5).
  Se schimbă din `settings.satisfaction.v6` → `model_hint`, fără redeploy.
- Config/revert (fără redeploy), cheia `settings.satisfaction.v6`:
  `month_aggregation` (`last_week_final` | `weighted_avg_weeks`), `carry_start_state`,
  `start_lookback_months`, `model_hint`, `prompt_version`.
- Sursa datelor: `cts_ground_truth` (mailuri) + `cts_calls_ground_truth` (apeluri), lună calendaristică.

---

## 🔴 SATISFACȚIE CLIENȚI — Implementare completă (2026-07-15)

### Motor de scor (`app/services/satisfaction_engine.py`)

5 factori cu recency decay exponențial (half-life 14 zile):

| Factor | Cheie | Pondere | Sursa de date |
|---|---|---|---|
| Timp răspuns | `response_time` | 30% | `emails.sent_to_cts_at - received_at` (target 120 min) + `cts_task_ground_truth` rezolvate (target 120 min) |
| Reclamații | `negative_ratio` | 25% | `emails.ai_category` (reclamatie/sesizare/informatie) |
| Sentiment | `sentiment` | 25% | `calls.ai_tone` (prietenos/neutru/tensionat) 70% + `emails.ai_category` 30% |
| Task-uri deschise | `open_tasks` | 10% | `cts_task_ground_truth` deschise >7 zile |
| Frecvență contact | `contact_frequency` | 10% | nr emailuri + apeluri în 30 zile (target max 1.5/zi) |

**Threshold nesatisfăcut: 90%** (sub 90% = client nesatisfăcut)

**Bug critic rezolvat**: `ai_tone` în DB are valori `prietenos`/`neutru`/`tensionat` (nu `pozitiv`/`negativ` cum presupunea engine-ul anterior). Fix: `_TONE_VALUE = {"prietenos": 1.0, "neutru": 0.5, "tensionat": 0.0}`.

**Compatibilitate cursor**: engine-ul funcționează cu ambele tipuri de cursor psycopg2 (RealDictCursor din `clients.py` și cursor standard din `satisfaction_snapshot.py`) via funcțiile helper `_first(row)` și `_row_get(row, *keys)`.

**Ponderi configurabile** din tabela `settings` (key: `satisfaction.weights` JSON).

### Excludere clienți parteneri/furnizori

Coloană `clients.satisfaction_exclude BOOLEAN DEFAULT FALSE`:
- Clienții excluși nu apar în snapshot-uri și nu li se calculează satisfacția
- Migrație: `migrations/20260715_satisfaction_exclude.sql`
- API toggle: `POST /api/v1/clients/{id}/satisfaction-exclude?exclude=true|false`
- UI: buton toggle „Partener/furnizor" în sidebar client

### Snapshot lunar (`app/services/satisfaction_snapshot.py`)

Script: `scripts/satisfaction_monthly.py 2026-07`
Cron user-level (instalat pe mailguard-staging):
```
0 3 1 * * /opt/iris-mailguard/venv/bin/python3 /opt/iris-mailguard/scripts/satisfaction_monthly.py >> /opt/iris-mailguard/storage/logs/satisfaction_monthly.log 2>&1
```
Idempotent: `ON CONFLICT (client_id, month_key) DO NOTHING`. Carry-forward dacă clientul nu are activitate.

### Pagina Satisfacție (UI full-width)

Dashboard complet în `SatisfactieDashboard`:
- KPI cards: total clienți, % nesatisfăcuți, medie scor, trend față de luna precedentă
- MultiLineChart: evoluție scor mediu + nr nesatisfăcuți pe 6 luni
- HBarChart: distribuție scoruri + variații lunare top clienți
- Tabel expandabil clienți nesatisfăcuți cu `BreakdownPanel` per factor
- Buton „?" popup Swal cu explicații complete pentru fiecare factor
- Popup explicativ actualizat: threshold 90% menționat explicit

### Sidebar client (secțiunea Satisfacție)

- Badge scor + link „istoric"
- Label „exclus" dacă `satisfaction_exclude = true`
- Toggle „Partener/furnizor" pentru excludere/includere
- Breakdown factori cu bare colorate (verde/galben/roșu)

### Endpoints API

```
POST /api/v1/clients/{id}/estimate-satisfaction      — calculează și persistă scorul
POST /api/v1/clients/{id}/satisfaction-exclude?exclude=bool  — marchează excludere
GET  /api/v1/clients/satisfaction-stats              — stats dashboard satisfacție
GET  /api/v1/clients/{id}/satisfaction-history       — istoricul lunar
```

### Schema DB

```sql
-- client_satisfaction_snapshots (T2)
CREATE TABLE IF NOT EXISTS client_satisfaction_snapshots (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT REFERENCES clients(id),
    month_key VARCHAR(7) NOT NULL,    -- 'YYYY-MM'
    satisfaction_pct NUMERIC(5,1),
    is_unsatisfied BOOLEAN DEFAULT FALSE,
    breakdown JSONB,
    carry_forward BOOLEAN DEFAULT FALSE,
    source_month_key VARCHAR(7),
    config_used JSONB,
    computed_at TIMESTAMPTZ,
    UNIQUE (client_id, month_key)
);

-- Coloane adăugate pe clients
ALTER TABLE clients ADD COLUMN satisfaction_breakdown JSONB;
ALTER TABLE clients ADD COLUMN satisfaction_exclude BOOLEAN NOT NULL DEFAULT FALSE;
```

---

## 📋 Descriere Generală

Cargo360 este o platformă integrată de gestionare și clasificare a emailurilor, cu focus pe:
- Detectare spam bazată pe scoring (content, reputație, authentication)
- Gestionare avansată a listelor de încredere (allowlist) și blocări (blocklist)
- Audit și logging complet
- Backup/restore cod și schema DB

Arhitectură: FastAPI + PostgreSQL, UI single-file React SPA.

---

## 🎯 Module Principale

### 1. SPAM — Clasificare și Gestionare

#### 📌 Faza 1: Consolidare pagină SPAM (2026-06-09)

**Funcionalități:**
- **Buton "Legit"** — reclasifică email ca legitim, adaugă expeditorul în `allowlist`
  - Efecte: override=FALSE pe TOATE emailurile expeditorului (retroactiv), status='clean' pe emailul curent
  - Prioritate: allowlist bypass completă scorul de spam (score → 0.0)
  
- **Buton "Marchează ca SPAM"** — confirmă spam, adaugă expeditorul în `blocklist`
  - Efecte: override=TRUE, incearcă dezabonarea automată (one-click RCTS 8058)
  - Prioritate: la clasificare, blocklist -> override=TRUE (apare in lista spam la ORICE prag).
    NOTA v0.26.0: vechiul boost +40 a fost inlocuit cu override=TRUE (simetric cu allowlist).
  
- **Eliminat: Buton "Sterge"** — nu mai este disponibil (nu face parte din flux standard)

**Dezabonare Automată (Best-Effort):**
- Citește `List-Unsubscribe` din `email_headers.list_unsubscribe` (dacă disponibil)
- Validează RFC 8058 One-Click (`List-Unsubscribe-Post: List-Unsubscribe=One-Click`)
- POST HTTPS la URL din header (no redirects, timeout 10s)
- Loghează rezultat în `spam_unsubscribe_log` (method, status, error)
- Dacă header absent → method='none', blocklist rămâne mecanismul primar

**Schema DB — Tabele Noi:**

```sql
-- email_headers: persisă pentru fiecare email nou
ALTER TABLE emails ADD COLUMN email_headers jsonb NOT NULL DEFAULT '{}';

-- Reputație persistă (allowlist/blocklist per expeditor)
CREATE TABLE spam_sender_reputation (
    id BIGSERIAL PRIMARY KEY,
    scope_type VARCHAR(20) DEFAULT 'sender_exact',  -- 'sender_exact' | 'domain'
    scope_value VARCHAR(320) NOT NULL,               -- adresă expeditor exact | domeniu
    reputation VARCHAR(20) NOT NULL,                 -- 'allowlist' | 'blocklist'
    created_by VARCHAR(100),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    last_action VARCHAR(20),                        -- 'legit' | 'mark_spam'
    action_count INT DEFAULT 1,
    UNIQUE(scope_type, scope_value)
);

-- Audit dezabonări (14 zile retenție)
CREATE TABLE spam_unsubscribe_log (
    id BIGSERIAL PRIMARY KEY,
    email_id BIGINT REFERENCES emails(id),
    from_address VARCHAR(320),
    method VARCHAR(20),      -- 'one_click' | 'mailto' | 'none'
    url TEXT,                -- NULL dacă nu e one-click
    http_status INT,         -- HTTP status POST
    success BOOLEAN,         -- 2xx?
    error_message TEXT,      -- încercare eșuată
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

**Funcții de Detectare — `app/services/spam_detector.py`:**

```python
def get_sender_reputation(from_address: str, db) -> Optional[str]:
    """Caută reputație (allowlist/blocklist) per sender exact sau domeniu."""
    # Returns: 'allowlist' | 'blocklist' | None

def detect_spam_with_reputation(email: Dict, db) -> Tuple[float, List[Dict]]:
    """[SQLAlchemy] Scoring cu prioritate reputatie. NEUTILIZAT la clasificare (vezi mai jos)."""
    # Returns: (score, reasons)

def get_sender_reputation_pg(from_address: str, cur) -> Optional[str]:
    """[psycopg2, v0.26.0] Geaman al get_sender_reputation; CONSULTAT in process_one ca
    poarta exterioara INAINTE de scoring. allowlist->score 0+override=FALSE, blocklist->override=TRUE."""
    # Returns: 'allowlist' | 'blocklist' | None
```

**Endpoints API:**

```
POST /api/v1/spam/{email_id}/action
  Body: { "action": "legit" | "mark_spam" }
  Auth: admin JWT
  Efecte: upsert spam_sender_reputation, update email_spam override, log audit
```

**Persistență și Idempotență:**
- Upsert pe `spam_sender_reputation`: ON CONFLICT DO UPDATE (last-write-wins)
- Dublu-click aceași acțiune → update counteri, fără eroare
- Conflict allowlist vs blocklist → ultima acțiune câștigă

**UI — Componenta Spam:**
- Dialog SweetAlert2 (verde pentru Legit, galben pentru mark_spam)
- Descriere accesibilă: "expeditor adăugat în lista de încredere" | "va încerca dezabonare automată"
- Text alb pe buton "Marchează ca SPAM"
- Fără `window.confirm` nativ

**Limitare Actuală:**
- `email_headers.list_unsubscribe` gol pentru emailuri vechi (mail-parser nu persistă headere RCTS)
- Dezabonarea returnează method='none' pentru majoritate email-uri
- Planificat: Extindere mail-parser pentru a salva headerele brute (task separat, CC project)

#### 📌 Integrare în Pipeline

**Moment evaluare reputație:** Acum — doar la display în UI  
**Planificat:** Faza 2 — integrare în `process_email.py` pentru scoring real-time

---

### 2. BACKUP / RESTORE — Code Snapshots & Audit

#### 📌 Modulul de Backups (2026-06-09)

**Funcionalități:**
- Snapshot automat cod (tar.gz) la modificări, pe orar
- Manual force-backup via API
- Restore snapshot precedent (creează pre-restore backup, repornește API)
- Worklog human-readable per backup (rezumat modificări, fișiere changed)
- Audit log complet (actor, archive, restore reason, timestamp)

**Schema DB:**
```sql
-- Backups sunt simple fișiere, metadate salvate în file system
-- Mirroring: /opt/iris-mailguard/storage/backups/mailguard_code_YYYYMMDD_HHMMSS.tar.gz
--           /opt/iris-mailguard/storage/backups/mailguard_code_*.meta (JSON)
--           /opt/iris-mailguard/storage/backups/mailguard_code_*.worklog.json (human summary)

-- Audit în audit_log tabela:
-- action='backup_create' | 'restore_backup'
-- details: {"archive": "name", "restore_reason": "...", ...}
```

**Endpoints API — `app/api/v1/settings.py`:**

```
GET /api/v1/settings/rules
  → { policy: {...}, rules: [{...}] }
  Admin-only, ruluri phishing detection

GET /api/v1/settings/backups
  → { dir: path, count: N, fresh: bool, pending_changes: bool, backups: [{name, mtime, ...}] }
  Admin-only, listă snapshot-uri cu freshness indicator

POST /api/v1/settings/backups/run-now?note=...
  → { ok: true, message: "Backup pornit..." }
  Admin-only, force snapshot (bypass change-detection)
  Note: opțional, max 500 chars

POST /api/v1/settings/backups/{name}/restore?reason=...
  → { ok: true, archive: name, message: "Restaurare pornită..." }
  Admin-only, restore din snapshot
  Reason: opțional, max 500 chars
  Status 202 (async, API repornește ~10s)

GET /api/v1/settings/backups/{name}/worklog
  → { archive: name, summary: [...], files_changed: [...] }
  Admin-only, rezumat uman per backup
```

**Metrici Freshness:**
- `fresh: true` ⟺ (latest backup mtime) ≥ (lateste code file mtime)
- `pending_changes: true` ⟺ fișiere cod mai noi decât ultimul backup
- **NOT wall-clock age** — backups conditionale, nopți idle sunt OK

**Retenție:**
- Retenție: 7 zile, min 3 snapshot-uri păstrate (pe cron)
- Purge automat via `mailguard-cron`

**Fișiere Suport:**
```
scripts/backup_code.sh      — creează snapshot + metadata
scripts/restore_code.sh     — restaurează din archive + restart
scripts/mailguard-cron      — scheduler orar + purge
```

**Workflow Restore:**
1. Frontend POST → audit log insert + spawn restore script async
2. Script creează pre-restore backup (snapshot versiunea curentă)
3. Extract archive pe /opt/iris-mailguard
4. `sudo systemctl restart mailguard-api`
5. API online cu codul restaurat (~10s)

**Audit Trail:**
- Actor, timestamp, archive name, restore reason (dacă furnizat)
- Query: `SELECT * FROM audit_log WHERE action LIKE 'restore_backup%' ORDER BY created_at DESC`

---

## 🏗️ Structură Fișiere

```
/opt/iris-mailguard/
├── app/
│   ├── api/v1/
│   │   ├── spam.py              — endpoints spam actions + unsubscribe
│   │   ├── settings.py          — settings, rules, backups API
│   │   ├── auth.py              — JWT, admin checks
│   │   └── ...
│   ├── services/
│   │   ├── spam_detector.py     — scoring + reputație (allowlist/blocklist)
│   │   ├── phishing_detector.py — phishing rules catalog
│   │   ├── parser_email_op_reader.py — ingestie email + email_headers populate
│   │   └── ...
│   ├── ui/
│   │   └── index.html           — SPA React + SweetAlert2 (dialog-uri replace)
│   └── database.py              — SQLAlchemy models, connection
├── migrations/
│   └── 20260609_spam_phase1.sql — tabele spam_sender_reputation, spam_unsubscribe_log + column email_headers
├── scripts/
│   ├── backup_code.sh           — create snapshot
│   ├── restore_code.sh          — restore from archive
│   └── mailguard-cron           — scheduler
├── storage/
│   ├── backups/                 — snapshot-uri tar.gz + metadata json + worklog
│   └── logs/                    — journalctl archiv
├── docs/
│   └── API.md                   — referință endpoints
├── CLAUDE.md                    — this file (architecture, modulele, conventions)
├── CHANGELOG.md                 — user-facing release notes
├── requirements.txt             — pip dependencies
└── VERSION                      — semantic version
```

---

## 🔐 Permisiuni & Audit

**Admin-only endpoints:**
- Toate `/settings/**` endpoints
- `POST /spam/{id}/action` — clasificare email

**Audit logging:**
- `spam_action` → `audit_log(action='spam_legit'|'spam_mark_spam', details={from_address, ...})`
- `backup_create` → `audit_log(action='backup_create', ...)`
- `restore_backup` → `audit_log(action='restore_backup', details={archive, reason, ...})`

**Auth:**
- JWT via NOVA SSO (`NOVA_SSO_SECRET`)
- Admin: user cu role='admin' din tabela `admin_users`
- Password login: DISABLED (HTTP 410)

---

## 🚀 Inițializare & Deploy

### 1. Migrare DB (prima dată)
```bash
ssh mailguard-server "sudo docker exec -i \$(docker ps --filter expose=5432 -q | head -1) \
  psql -U mailguard -d mailguard < /opt/iris-mailguard/migrations/20260609_spam_phase1.sql"
```

### 2. Restart API
```bash
ssh mailguard-server "sudo systemctl restart mailguard-api"
```

### 3. Health check
```bash
curl http://191.66.151.5:8501/api/v1/health
# → { status: "healthy ok" }
```

### 4. Test spam action
```bash
curl -X POST http://191.66.151.5:8501/api/v1/spam/123/action \
  -H "Authorization: Bearer <JWT>" \
  -H "Content-Type: application/json" \
  -d '{"action":"legit"}'
```

---

## 📝 Convenții Cod

### Python
- Type hints (Pydantic models, Optional, Dict, etc.)
- Docstring pe funcții publice (one-liner, not verbose)
- Logging: `logger.info()`, `logger.error()` — fără print
- DB: SQLAlchemy ORM + parameterized queries (no raw SQL concat)
- Error handling: HTTPException + audit trail pentru user actions

### SQL
- Migrations în `/migrations/` cu timestamp format `YYYYMMDD`
- Indexuri pe colonele de lookup (reputație, email_id)
- Constraints UNIQUE + FOREIGN KEY

> ⚠ **REGULĂ OBLIGATORIE (migrații → producție).** Release-ul duce pe prod DOAR fișierele din `migrations/`
> (aplicate de `scripts/migrate.sh`, urmărite în `_release_migrations`). DDL rulat direct pe staging fără
> fișier de migrație **NU ajunge pe prod** → 500 pe prod (tabelă/coloană lipsă).
> - ORICE tabelă nouă → `migrations/YYYYMMDD_*.sql` cu `CREATE TABLE IF NOT EXISTS`.
> - ORICE coloană adăugată/modificată/redenumită/retipată → în migrație (`ADD COLUMN IF NOT EXISTS` / `ALTER`).
> - ORICE index/constraint/secvență nou → în migrație, idempotent (`CREATE INDEX IF NOT EXISTS`, gardă `pg_constraint`).
> - Seed/config → `INSERT ... ON CONFLICT DO NOTHING` (fără duplicate). NU seeda date tranzacționale/secrete.
> - Migrațiile: idempotente + aditive/backward-compatible. Niciodată DDL ad-hoc „doar pe staging".

### React (UI)
- UMD CDN imports (no bundler)
- Hooks: useState, useEffect (minimal, React 18)
- SweetAlert2 pentru dialog-uri (no `window.confirm`/`alert`)
- CSS variables: `--tx` (text), `--bg2` (bg), `--am` (accent), `--bd` (border)
- Dark/light theme toggle in sidebar

---

## 🐛 Debugging

### Logs API
```bash
ssh mailguard-server "sudo journalctl -u mailguard-api -n 100 --no-pager"
```

### DB Query (SELECT-only)
```bash
ssh mailguard-server "sudo docker exec \$(docker ps --filter expose=5432 -q | head -1) \
  psql -U mailguard -d mailguard -c \"SELECT * FROM spam_sender_reputation LIMIT 5;\""
```

### Test SweetAlert2 rendering
- Open `http://191.66.151.5:8501` → Click spam action → inspect modal

---

## 📅 Planificat (Fază 2+)

- [ ] Integrare reputație în scoring real-time (`process_email.py`)
- [ ] Pagina UI: manage allowlist/blocklist (view, edit, delete)
- [ ] Extindere mail-parser: salvare headerelor RFC brute
- [ ] Phishing detection rules improvement (reputation + content combo)
- [ ] Dashboard: spam statistics, trends

---

## 🔗 Resurse

- **API Docs:** `/opt/iris-mailguard/docs/API.md`
- **GitHub:** https://github.com/—
- **CHANGELOG:** `/opt/iris-mailguard/CHANGELOG.md`

---

## 🔢 CONVENȚIE VERSIUNI (obligatorie din 2026-08-06)

Schema: `MAJOR.MINOR.PATCH`

| Nivel | Când crește | Exemplu |
|---|---|---|
| **MAJOR** | La fiecare Release spre producție (buton Release din IRIS) | v1.0.0 → v2.0.0 |
| **MINOR** | Feature nou între release-uri (pe staging) | v1.0.0 → v1.1.0 |
| **PATCH** | Fix între release-uri (pe staging) | v1.0.0 → v1.0.1 |

**Versiunea curentă:** `v3.4.0` (staging, 2026-08-19)
**Ultimul release pe producție:** `v3.0.0`.

Reguli impuse agentului:
- Orice livrare → incrementează `VERSION` + entry în `CHANGELOG.md`
- MAJOR crește DOAR la release explicit spre producție (nu la orice deploy pe staging)
- Nu folosi schema veche `0.x`

---

