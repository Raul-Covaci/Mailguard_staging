#!/bin/bash
# deploy-pull.sh — deploy MailGuard/Cargo360 prin `git pull` pe serverul aplicatiei.
#
# Rulare (pe serverul mailguard-staging):
#     sudo /opt/iris-mailguard/deploy-pull.sh
#     sudo /opt/iris-mailguard/deploy-pull.sh --force   # trece peste modificarile locale
#
# Etape: verificare arbore -> pull --ff-only -> backup DB -> pip (doar la nevoie)
#        -> gzip mg-app.js -> restart servicii -> health check -> rollback la eroare.
#
# NU face `git reset --hard`. Un hotfix aplicat manual pe server nu se pierde tacit:
# scriptul se opreste si il arata.
set -Eeuo pipefail

APP_DIR="/opt/iris-mailguard"
BRANCH="main"
DB_CONTAINER="${MAILGUARD_DB_CONTAINER:-mailguard-db}"
HEALTH_URL="http://127.0.0.1:8500/healthz"     # gunicorn direct; 8501 = nginx (are IP allowlist)
BACKUP_DIR="$APP_DIR/backups/pre-deploy"
RETENTION=3                                     # dump-uri DB pastrate (10 ocupau 2,3 GB pe staging)
SERVICES=("mailguard-api")                      # primul aplica migrarile prin ExecStartPre
TIMERS=("mailguard-cron.timer" "mailguard-personal-poll.timer")
VENDOR_JS="$APP_DIR/app/ui/vendor/mg-app.js"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

log(){ echo "[deploy $(date +%H:%M:%S)] $*"; }
die(){ echo "[deploy EROARE] $*" >&2; exit 1; }
trap 'echo "[deploy EROARE] a esuat la linia $LINENO" >&2' ERR

cd "$APP_DIR" || die "nu pot intra in $APP_DIR"
[ -d .git ] || die "$APP_DIR nu e repo Git — vezi DEPLOY.md (instalare pe server nou)"

# ---------------------------------------------------------------------------
# 1. Arborele e curat? Un hotfix manual necommitat s-ar pierde la pull.
# ---------------------------------------------------------------------------
DIRTY="$(git status --porcelain | grep -v '^?? ' || true)"
if [ -n "$DIRTY" ]; then
  echo "[deploy] ATENTIE: modificari locale necommitate:"
  echo "$DIRTY" | sed 's/^/    /'
  if [ "$FORCE" != "1" ]; then
    die "opresc. Fa commit, sau ruleaza cu --force ca sa le suprascrii."
  fi
  log "--force activ — modificarile de mai sus vor fi suprascrise de pull"
fi

OLD_REV="$(git rev-parse HEAD)"
log "revizia curenta: ${OLD_REV:0:8}"

# ---------------------------------------------------------------------------
# 2. Backup DB — INAINTE de pull. Migrarile ruleaza la restart si pot fi
#    ireversibile; fara backup nu exista drum de intoarcere.
# ---------------------------------------------------------------------------
_envval(){ grep -E "^$1=" "$APP_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'"; }
DB_USER="$(_envval DB_USER)"; DB_USER="${DB_USER:-mailguard}"
DB_NAME="$(_envval DB_NAME)"; DB_NAME="${DB_NAME:-mailguard}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d_%H%M%S)"
DUMP="$BACKUP_DIR/${DB_NAME}_${STAMP}.dump"

# pg_dump -Fc din container (PostgreSQL, NU SQLite). Formatul custom permite
# restaurare selectiva pe tabel cu pg_restore.
log "backup DB $DB_NAME din containerul $DB_CONTAINER..."
if ! docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc 2>/dev/null > "$DUMP"; then
  rm -f "$DUMP"
  die "backup DB esuat — NU continui fara punct de restaurare"
fi
[ -s "$DUMP" ] || { rm -f "$DUMP"; die "backup DB e gol — opresc"; }
log "backup: $DUMP ($(du -h "$DUMP" | cut -f1))"

# Retentie: pastreaza ultimele N.
ls -1t "$BACKUP_DIR"/*.dump 2>/dev/null | tail -n "+$((RETENTION+1))" | xargs -r rm -f

# ---------------------------------------------------------------------------
# 3. Pull — fast-forward doar. La istoric divergent: oprire, nu reset.
# ---------------------------------------------------------------------------
log "git fetch origin $BRANCH..."
git fetch origin "$BRANCH" --quiet || die "fetch esuat (verifica deploy key)"

if ! git merge-base --is-ancestor HEAD "origin/$BRANCH" 2>/dev/null; then
  echo "[deploy] Istoric divergent: HEAD nu e stramosul lui origin/$BRANCH."
  echo "[deploy] Local:  $(git rev-parse --short HEAD)"
  echo "[deploy] Remote: $(git rev-parse --short "origin/$BRANCH")"
  die "opresc. Rezolva manual (git log --oneline HEAD..origin/$BRANCH). NU fac reset --hard automat."
fi

git merge --ff-only "origin/$BRANCH" --quiet || die "merge --ff-only esuat"
NEW_REV="$(git rev-parse HEAD)"

if [ "$OLD_REV" = "$NEW_REV" ]; then
  log "deja la zi (${NEW_REV:0:8}) — nimic de tras"
else
  log "actualizat ${OLD_REV:0:8} -> ${NEW_REV:0:8}"
  git log --oneline "$OLD_REV..$NEW_REV" | sed 's/^/    /'
fi

CHANGED="$(git diff --name-only "$OLD_REV" "$NEW_REV" 2>/dev/null || true)"

# ---------------------------------------------------------------------------
# 4. Dependinte — doar daca requirements.txt s-a schimbat.
# ---------------------------------------------------------------------------
if echo "$CHANGED" | grep -qx "requirements.txt"; then
  log "requirements.txt s-a schimbat — instalez..."
  "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt" \
    || die "pip install esuat"
  log "dependinte actualizate"
else
  log "requirements.txt neschimbat — sar peste pip"
fi

# ---------------------------------------------------------------------------
# 5. Frontend: fara bundler (React prin CDN), dar mg-app.js are varianta .gz
#    servita direct de aplicatie (_GZIP_FILES in app/main.py). Daca .gz rimine
#    vechi, browserul primeste cod vechi desi sursa e noua — exact capcana
#    "restartul nu recompileaza frontend-ul".
#
#    Verificarea e pe CONTINUT, nu pe mtime: `git pull` scrie fisierele cu ora
#    checkout-ului, deci `-nt` poate raporta ".gz e la zi" peste un .gz vechi — exact
#    cum s-a intimplat pe staging (25.08: .js nou, .gz din 19.08, interfata veche in
#    browser fiindca aplicatia serveste .gz cind browserul accepta gzip).
# ---------------------------------------------------------------------------
if [ -f "$VENDOR_JS" ]; then
  if [ ! -f "${VENDOR_JS}.gz" ] || ! gzip -dc "${VENDOR_JS}.gz" 2>/dev/null | cmp -s - "$VENDOR_JS"; then
    log "regenerez mg-app.js.gz (continut diferit de sursa)..."
    gzip -9 -c "$VENDOR_JS" > "${VENDOR_JS}.gz.tmp" && mv "${VENDOR_JS}.gz.tmp" "${VENDOR_JS}.gz"
    log "gzip ok ($(du -h "${VENDOR_JS}.gz" | cut -f1))"
  else
    log "mg-app.js.gz e la zi"
  fi
fi

# ---------------------------------------------------------------------------
# 6. Restart. Migrarile ruleaza automat ca ExecStartPre (10-migrate.conf) si
#    sint fail-fast: la o migrare esuata serviciul NU porneste.
# ---------------------------------------------------------------------------
for svc in "${SERVICES[@]}"; do
  log "restart $svc..."
  systemctl restart "$svc" || {
    echo "[deploy] $svc nu a pornit. Ultimele linii din jurnal:"
    journalctl -u "$svc" -n 30 --no-pager | sed 's/^/    /'
    echo "[deploy] ROLLBACK: git reset --hard $OLD_REV && systemctl restart $svc"
    echo "[deploy] Backup DB pentru restaurare: $DUMP"
    die "$svc a esuat la pornire"
  }
done

for t in "${TIMERS[@]}"; do
  systemctl is-enabled "$t" >/dev/null 2>&1 && systemctl restart "$t" || true
done

# ---------------------------------------------------------------------------
# 7. Health check — endpoint + starea serviciilor.
# ---------------------------------------------------------------------------
log "health check pe $HEALTH_URL..."
OK=0
for i in $(seq 1 20); do
  BODY="$(curl -sS --max-time 5 "$HEALTH_URL" 2>/dev/null || true)"
  if echo "$BODY" | grep -q '"ok"\|"healthy"\|"status"'; then OK=1; break; fi
  sleep 2
done

if [ "$OK" != "1" ]; then
  echo "[deploy] health check ESUAT dupa 40s. Jurnal:"
  journalctl -u mailguard-api -n 40 --no-pager | grep -iE "error|traceback|exception" | tail -20 | sed 's/^/    /'
  echo "[deploy] ROLLBACK: git reset --hard $OLD_REV && systemctl restart mailguard-api"
  echo "[deploy] Backup DB: $DUMP"
  die "aplicatia nu raspunde sanatos"
fi
log "health: $BODY"

for svc in "${SERVICES[@]}"; do
  systemctl is-active --quiet "$svc" || die "$svc nu e activ dupa deploy"
done

ERRS="$(journalctl -u mailguard-api --since "2 min ago" --no-pager 2>/dev/null \
        | grep -icE "traceback|critical" || true)"
[ "${ERRS:-0}" -gt 0 ] && log "ATENTIE: $ERRS linii de eroare in jurnal — verifica manual"

log "toate serviciile active"
log "DEPLOY OK — revizia ${NEW_REV:0:8} (versiune $(cat "$APP_DIR/VERSION" 2>/dev/null || echo '?'))"
