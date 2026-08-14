# DEPLOY — MailGuard / Cargo360 (staging)

Fluxul de lucru: cod local → `git push` → GitHub → `git pull` pe server → deploy.

```
laptop / workspace  ──push──>  GitHub  ──pull──>  mailguard-staging  ──restart──>  live
```

| | |
|---|---|
| Repo | `git@github.com:Raul-Covaci/Mailguard_staging.git` (privat) |
| Server | `mailguard-staging`, aplicatia in `/opt/iris-mailguard` |
| URL public | http://95.216.144.102:8501 |
| Gunicorn | `127.0.0.1:8500` (nginx face proxy pe 8501, cu IP allowlist) |
| Health | `http://127.0.0.1:8500/healthz` |

---

## Fluxul zilnic

**Pe laptop / in workspace:**
```bash
git add -A
git commit -m "fix: descriere"
git push
```

**Deploy pe server:**
```bash
ssh mailguard-staging "sudo /opt/iris-mailguard/deploy-pull.sh"
```

Scriptul face, in ordine: verifica arborele → backup DB → `pull --ff-only` →
`pip` (doar daca `requirements.txt` s-a schimbat) → regenereaza `mg-app.js.gz`
→ restart → health check. La eroare afiseaza comanda de rollback si calea
backup-ului.

`--force` suprascrie modificarile locale necommitate de pe server (implicit se
opreste, ca sa nu pierzi un hotfix aplicat manual).

---

## Ce se propaga prin push si ce nu

| Editezi | Ajunge pe live prin push? |
|---|---|
| Cod Python (`app/**.py`) | **Da** |
| Frontend (`app/ui/vendor/mg-app.js`) | **Da** — `.gz` se regenereaza la deploy |
| Migrari SQL (`migrations/*.sql`) | **Da** — se aplica la restart |
| Unitati systemd (`systemd/*.service`) | **Nu** — vezi „Server nou" mai jos |
| Configurare (`.env`) | **Nu** — per mediu, se editeaza manual pe server |
| Date (rinduri in DB) | **Nu** |
| Documente clienti (`data/doc_templates/`) | **Nu** |

### Regula care evita cel mai frecvent dezastru

**Orice schimbare de schema se scrie ca fisier in `migrations/`.** DDL rulat
ad-hoc pe staging (`psql -c "ALTER TABLE ..."`) NU ajunge niciodata pe
productie — pe staging merge, la release lipseste, aplicatia cade.

Acelasi lucru pentru **date de configurare**: un rol de utilizator schimbat din
interfata trateste doar in DB-ul de staging. Daca vrei sa existe si pe
productie, adauga-l intr-o migrare (vezi `migrations/20260812e_*.sql` — exact
cazul in care Tudor Huza ar fi ajuns `operator` pe productie).

Migrarile trebuie **aditive si idempotente**: `CREATE TABLE IF NOT EXISTS`,
`ADD COLUMN IF NOT EXISTS`, `INSERT ... ON CONFLICT DO NOTHING`.

`scripts/migrate.sh` **sare peste fisierele deja inregistrate** in
`_release_migrations`. Daca o migrare a fost aplicata si vrei sa adaugi ceva,
scrie un **fisier nou**, nu edita cel vechi — modificarea nu s-ar mai rula.

---

## Ce e exclus din repo si de ce

`.gitignore` are motivul lingi fiecare regula. Pe scurt:

| Ce | Cit | De ce |
|---|---|---|
| `.env`, `app/.env`, `deploy/` | 9 KB | chei API, credentiale DB, cheia SSO, deploy key |
| `data/doc_templates/` | 114 MB | 41 documente reale de client |
| `logs/` | 684 MB | contin adrese si continut de mesaje |
| `venv/` | 238 MB | reproductibil din `requirements.txt` |
| `backups/`, `storage/` | — | dump-uri cu date reale; `storage/backups` e symlink spre `/home/mail-data` |
| `*.bak*` | 65 MB | ~90 copii manuale (40+ de `mg-app.js`); Git face istoricul |
| `deploy.sh`, `CLAUDE.md`, `OUTBOX_*`, `.iris_*` | — | specifice mediului de lucru IRIS, nu aplicatiei |

Repo-ul rezultat: **~7 MB, 313 fisiere** (din 1,2 GB pe disc).

`app/ui/vendor/mg-app.js.gz` **rimine in repo** — nu e artefact de build.
Aplicatia il serveste direct (`_GZIP_FILES` in `app/main.py`) cind browserul
accepta gzip.

---

## Instalare pe un clone nou (local)

```bash
git clone git@github.com:Raul-Covaci/Mailguard_staging.git
cd Mailguard_staging
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
```

Completeaza `.env`. Valorile reale se iau de pe server, **nu prin chat**:
```bash
scp mailguard-staging:/opt/iris-mailguard/.env .env
```

Puncte de atentie:

- **Fara chei AI** — porneste cu `AI_DISABLED=true`. Aplicatia merge, doar
  functiile AI lipsesc.
- **DB** — aplicatia cere PostgreSQL. Local: container propriu, sau tunel spre
  staging (`ssh -L 5440:127.0.0.1:5440 mailguard-staging`). Datele contin
  informatii reale de client: disc criptat, fara sincronizare in cloud.
- **`PERSONAL_MAILBOX_KEY`** — daca difera de cea din mediul unde au fost
  salvate credentialele cutiilor personale, acelea devin necitibile.

---

## Server nou (bootstrap)

Repo-ul nu contine `.env`, `venv/` si nici drop-in-ul systemd. Pe un server nou:

```bash
git clone git@github.com:Raul-Covaci/Mailguard_staging.git /opt/iris-mailguard
cd /opt/iris-mailguard
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env    # completeaza valorile

# unitatile systemd (din repo)
sudo cp systemd/mailguard-api.service /etc/systemd/system/
sudo cp systemd/mailguard-personal-poll.* /etc/systemd/system/

# drop-in care aplica migrarile inainte de pornire — NU e in repo, il creezi:
sudo mkdir -p /etc/systemd/system/mailguard-api.service.d
sudo tee /etc/systemd/system/mailguard-api.service.d/10-migrate.conf >/dev/null <<'EOF'
[Service]
ExecStartPre=/opt/iris-mailguard/scripts/migrate.sh
TimeoutStartSec=300
EOF

sudo systemctl daemon-reload && sudo systemctl enable --now mailguard-api
sudo systemctl enable --now mailguard-personal-poll.timer
```

Fara drop-in, migrarile **nu se aplica** la restart si aplicatia porneste pe o
schema veche.

### Sync-ul de clienti (vehicule + contracte)

Ruleaza in aplicatie (`_client_sync_loop` din `app/main.py`), pornit de `lifespan` —
nu are nevoie de cron sau timer systemd. Pana la 2026-08-14 nu exista nimic periodic:
`POST /api/v1/clients/sync-now` se declansa doar la apasarea butonului din UI, iar
vehiculele si contractele au ramas inghetate din 29.07 (datele erau in DB — 43k
vehicule, 32k contracte — si endpoint-urile raspundeau corect).

Intervalul se schimba din DB, fara redeploy:

```sql
UPDATE settings SET value = to_jsonb(30), updated_at = NOW()
WHERE key = 'client_sync_interval_minutes';       -- minute; podea 5
```

API-ul ruleaza cu 4 workeri gunicorn, deci fiecare proces are propria bucla. Ca sa nu
porneasca patru pull-uri deodata, scadenta se ia atomic din DB
(`iris_sync.claim_client_sync()`, cheia `client_assets.next_sync_at`): cistiga exact
un worker per interval. Scadenta persista, deci un restart nu reporneste numaratoarea.

Verificare:

```bash
# prospetimea reala a datelor
psql -c "SELECT max(last_synced_at) FROM clients;"
# starea ultimei rulari + urmatoarea scadenta
psql -c "SELECT key, value FROM settings WHERE key LIKE 'client_assets%';"
journalctl -u mailguard-api | grep 'client sync periodic'
```

---

## Depanare

**Serviciul nu porneste dupa deploy**
```bash
ssh mailguard-staging "sudo journalctl -u mailguard-api -n 50 --no-pager | grep -iE 'error|traceback'"
```
Cauza frecventa: o migrare a esuat. `migrate.sh` e fail-fast — serviciul refuza
sa porneasca pe o schema incompatibila. Asta e comportament dorit.

**Rollback**
```bash
ssh mailguard-staging "sudo git -C /opt/iris-mailguard reset --hard <rev-veche> && sudo systemctl restart mailguard-api"
```
Backup-urile DB: `/opt/iris-mailguard/backups/pre-deploy/` (ultimele 10).
Restaurare: `pg_restore -U mailguard -d mailguard -c <fisier>.dump`.

**Interfata arata cod vechi desi push-ul a mers**
`mg-app.js.gz` a rimas nesincronizat. Deploy-ul il regenereaza automat; manual:
```bash
ssh mailguard-staging "cd /opt/iris-mailguard/app/ui/vendor && sudo gzip -9 -c mg-app.js > mg-app.js.gz"
```
Apoi `Ctrl+Shift+R` in browser.

**„dubious ownership" la comenzi git**
```bash
sudo git config --global --add safe.directory /opt/iris-mailguard
```
Atentie: cind `git add` cade din acest motiv, indexul rimine gol — iar o
verificare de fisiere sensibile raporteaza „curat" pentru ca n-a testat nimic.
Verifica mereu si numarul de fisiere.

**Istoric divergent la pull**
Scriptul se opreste si NU face `reset --hard`. Investigheaza:
```bash
ssh mailguard-staging "sudo git -C /opt/iris-mailguard log --oneline HEAD..origin/main"
```

---

## Promovare spre productie

Deploy-ul manual pe productie **nu se face**. Promovarea se face prin butonul
**Release** din IRIS, care ruleaza migrarile in ordine pe DB-ul de productie.

De aceea orice schimbare de schema sau de date de configurare trebuie sa existe
ca fisier in `migrations/` — altfel exista pe staging si lipseste pe productie.
