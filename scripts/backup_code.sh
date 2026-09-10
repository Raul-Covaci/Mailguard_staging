#!/usr/bin/env bash
# NOVA MailGuard — application CODE backup.
# Modes:
#   backup_code.sh --auto                          -> gated: only if >=55min since last backup AND code changed
#   backup_code.sh --force [reason] [note] [actor] -> always create (reason: manual|prerestore|auto)
# Excludes secrets (.env), venv, logs, storage, caches, prior backups.
# After archiving, generates a human worklog sidecar (best-effort, never fails the backup).
set -euo pipefail

APP_DIR="/opt/iris-mailguard"
BACKUP_DIR="$APP_DIR/storage/backups"
LOG="$BACKUP_DIR/backup.log"
GLOB="mailguard_code_*.tar.gz"
MIN_INTERVAL_S=3300          # ~55 min: do not re-backup more often than hourly

# ⛔ DEZACTIVAT — 2026-09-10, decizie Raul Covaci.
# Versionarea codului se face in GitHub (Mailguard_staging); snapshot-urile tar.gz nu mai au rost.
# Ocupau ~17,8 GB pe staging (6,8 GB in storage/backups + 11 GB in /home/mail-data/backups).
#
# De ce kill-switch AICI si nu in cron: intrarea de cron traieste pe SERVER, in afara repo-ului,
# deci un deploy nu o poate opri. Cat timp scriptul iese devreme, o intrare de cron ramasa (sau
# reaparuta la o reinstalare) e inofensiva. Scoaterea din crontab ramane de facut, dar nu mai e
# o conditie de siguranta.
#
# NU acopera dump-urile DB din backups/pre-deploy/ — alea raman, GitHub nu tine DATE.
# Reactivare temporara: MAILGUARD_CODE_BACKUP=on scripts/backup_code.sh --force
if [ "${MAILGUARD_CODE_BACKUP:-off}" != "on" ]; then
  echo "$(date -Is) SKIP: backup cod dezactivat (MAILGUARD_CODE_BACKUP != on)" >> "$LOG" 2>/dev/null || true
  exit 0
fi

MODE="${1:---auto}"
REASON="${2:-auto}"
NOTE="${3:-}"
ACTOR="${4:-}"
[[ "$MODE" == "--force" && "$REASON" == "auto" ]] && REASON="manual"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR" 2>/dev/null || true

log(){ echo "$(date -Is) $*" >> "$LOG"; }
json_str(){ "$APP_DIR/venv/bin/python" -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1" 2>/dev/null || echo '""'; }

code_mtime(){
  find "$APP_DIR" \
    -path "$APP_DIR/venv" -prune -o \
    -path "$APP_DIR/storage" -prune -o \
    -path "$APP_DIR/logs" -prune -o \
    -path "$APP_DIR/.git" -prune -o \
    -name '.env' -prune -o \
    -type f -printf '%T@\n' 2>/dev/null | sort -nr | head -1 | cut -d. -f1
}

latest_archive(){ ls -1t "$BACKUP_DIR"/$GLOB 2>/dev/null | head -1; }

PREV="$(latest_archive || true)"

if [[ "$MODE" != "--force" ]]; then
  if [[ -n "$PREV" ]]; then
    prev_mtime="$(stat -c %Y "$PREV")"
    now="$(date +%s)"
    if (( now - prev_mtime < MIN_INTERVAL_S )); then
      exit 0
    fi
    cm="$(code_mtime)"
    if [[ -n "$cm" ]] && (( cm <= prev_mtime )); then
      exit 0
    fi
  fi
fi

TS="$(date +%Y%m%d_%H%M%S)"
OUT="$BACKUP_DIR/mailguard_code_${TS}.tar.gz"

if tar \
    --exclude='./venv' \
    --exclude='./storage' \
    --exclude='./logs' \
    --exclude='./.git' \
    --exclude='./.env' \
    --exclude='*/__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.bak_*' \
    -czf "$OUT" -C "$APP_DIR" . 2>>"$LOG"; then
  chmod 600 "$OUT" 2>/dev/null || true
  SZ="$(du -h "$OUT" | cut -f1)"
  printf '{"reason":"%s","created_at":"%s","note":%s,"actor":%s}\n' \
    "$REASON" "$(date -Is)" "$(json_str "$NOTE")" "$(json_str "$ACTOR")" > "$OUT.meta" 2>/dev/null || true
  log "OK   $(basename "$OUT") $SZ reason=$REASON actor=${ACTOR:-system}"
else
  log "FAIL $(basename "$OUT")"
  rm -f "$OUT"
  exit 1
fi

if [[ -x "$APP_DIR/venv/bin/python" ]]; then
  ( cd "$APP_DIR" && "$APP_DIR/venv/bin/python" -m app.services.worklog "$OUT" "${PREV:-}" >> "$LOG" 2>&1 ) || log "WARN worklog failed for $(basename "$OUT")"
fi

exit 0
