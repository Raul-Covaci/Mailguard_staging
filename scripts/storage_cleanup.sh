#!/bin/bash
# Cleanup storage zilnic la 00:00:
#   1. Fișiere native atașamente >10 zile — șterge conținut, păstrează numele în DB (storage_path intact)
#   2. Audio apeluri >3 zile cu transcript success — șterge MP3/WAV, nullează audio_path în DB
#   3. document_extractions procesate (status != pending) mai vechi decat FEREASTRA de procesare — DELETE din DB
#
# Loguri: /opt/iris-mailguard/storage/logs/storage_cleanup.log (rotit la 10MB)
set -euo pipefail

APP_DIR="/opt/iris-mailguard"
LOG="$APP_DIR/storage/logs/storage_cleanup.log"
NATIVE_DIR="/home/mail-data/attachments/native"
AUDIO_DIR="/home/mail-data/call_audio"
DB_CMD="sudo docker exec mailguard-db psql -U mailguard -d mailguard -t -A"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

# Rotire log dacă depășește 10MB
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 10485760 ]; then
    mv "$LOG" "${LOG}.1"
fi

log "=== START storage_cleanup ==="

# ── 1. ATAȘAMENTE NATIVE >10 zile ──────────────────────────────────────────
# Șterge fișierele fizice dar păstrează folderul și înregistrarea din DB intactă.
# storage_path rămâne în attachments — referința există, conținutul nu.
log "1/3 Cleanup native attachments >10 zile..."
ATTACH_DELETED=0
ATTACH_BYTES=0
# Normalizează ieșirea unei comenzi la UN SINGUR număr.
# `du` poate tipări o valoare ȘI ieși cu cod != 0 (ex. un subdirector necitibil); cu `set -o pipefail`
# asta face `|| echo 0` să adauge un al DOILEA "0" pe linie nouă, iar `$(( x + val ))` aruncă
# „syntax error in expression". Sub `set -e` scriptul MOARE aici, deci pașii 2 și 3 (inclusiv
# curățenia din document_extractions) nu mai rulează niciodată, iar logul se oprește tăcut.
_num() {
    local v
    v=$(cat)
    v=$(printf '%s' "$v" | head -1 | tr -cd '0-9')
    printf '%s' "${v:-0}"
}

while IFS= read -r -d '' dir; do
    # verifică vârsta folderului
    age_days=$(( ( $(date +%s) - $(stat -c %Y "$dir" 2>/dev/null || date +%s) ) / 86400 ))
    if [ "$age_days" -lt 10 ]; then
        continue
    fi
    # Folder deja golit la o rulare anterioară: `find -delete` nu-i schimbă mtime-ul, deci ar intra
    # în raport în FIECARE noapte, la infinit (14k+ foldere × du recursiv, degeaba).
    if [ -z "$(ls -A "$dir" 2>/dev/null)" ]; then
        continue
    fi
    # șterge fișierele, nu folderul
    bytes=$(du -sb "$dir" 2>/dev/null | cut -f1 | _num)
    find "$dir" -type f -delete 2>/dev/null || true
    ATTACH_DELETED=$(( ATTACH_DELETED + 1 ))
    ATTACH_BYTES=$(( ATTACH_BYTES + bytes ))
done < <(find "$NATIVE_DIR" -mindepth 1 -maxdepth 1 -type d -print0 2>/dev/null)

log "  Atașamente: $ATTACH_DELETED foldere curățate, $(( ATTACH_BYTES / 1048576 )) MB eliberate"

# ── 2. AUDIO APELURI >3 zile cu transcript success ─────────────────────────
# Șterge fișierul fizic + nullează audio_path în DB (transcriptul rămâne intact).
log "2/3 Cleanup call_audio >3 zile cu transcript success..."
AUDIO_DELETED=0
AUDIO_BYTES=0
AUDIO_ERRORS=0

# Aduce lista: call_id|audio_path pentru apeluri eligibile
while IFS='|' read -r call_id audio_path; do
    [ -z "$audio_path" ] && continue
    # call_id vine din stdout-ul psql și e interpolat direct în SQL mai jos. Orice linie neașteptată
    # (NOTICE, antet, \timing dintr-un ~/.psqlrc) ar produce un UPDATE invalid — ignorat de `|| true` —
    # și fișierul s-ar șterge fizic în timp ce audio_path rămâne setat, lăsând un pointer mort în DB.
    if ! [[ "$call_id" =~ ^[0-9]+$ ]]; then
        continue
    fi
    # audio_path în DB poate fi relativ sau absolut
    if [[ "$audio_path" = /* ]]; then
        full_path="$audio_path"
    else
        full_path="$AUDIO_DIR/$audio_path"
    fi
    if [ -f "$full_path" ]; then
        bytes=$(stat -c%s "$full_path" 2>/dev/null | _num)
        if rm -f "$full_path" 2>/dev/null; then
            # nullează audio_path în DB (fișierul e deja șters — dacă UPDATE-ul pică, semnalăm,
            # altfel DB-ul rămâne cu o cale către un fișier inexistent, fără nicio urmă în log)
            if ! $DB_CMD -c "UPDATE calls SET audio_path=NULL WHERE id=$call_id" > /dev/null 2>&1; then
                log "  ATENȚIE: audio șters de pe disc dar UPDATE calls id=$call_id a eșuat"
            fi
            AUDIO_DELETED=$(( AUDIO_DELETED + 1 ))
            AUDIO_BYTES=$(( AUDIO_BYTES + bytes ))
        else
            AUDIO_ERRORS=$(( AUDIO_ERRORS + 1 ))
        fi
    else
        # fișier deja absent — nullează oricum
        $DB_CMD -c "UPDATE calls SET audio_path=NULL WHERE id=$call_id" > /dev/null 2>&1 || true
    fi
done < <($DB_CMD -c \
    "SELECT id, audio_path FROM calls
     WHERE transcript_status='success'
       AND audio_path IS NOT NULL
       AND created_at < NOW() - INTERVAL '3 days'" 2>/dev/null || true)

log "  Audio: $AUDIO_DELETED fișiere șterse, $(( AUDIO_BYTES / 1048576 )) MB eliberate, $AUDIO_ERRORS erori"

# ── 3. DOCUMENT_EXTRACTIONS procesate, mai vechi decât fereastra ───────────
# Șterge rândurile cu status != pending create înainte de (azi - RETENTION_DAYS).
# raw_text și data (jsonb) sunt câmpurile mari — asta e memoria principală.
# grouped_into FK: ștergem mai întâi non-root (grouped_into IS NOT NULL), apoi root.
#
# ⚠️ RETENTION_DAYS = fereastra de procesare (settings['documents.process_window'], vezi
# app/services/doc_window.py). Drain-ul citește „am procesat asta" EXCLUSIV din prezența rândurilor
# de aici. Dacă retenția e mai scurtă decât fereastra, un mail încă aflat în fereastră își pierde
# rândurile la miezul nopții și e reprocesat (și replătit) a doua zi — bucla care a generat costuri
# mari până în 26.08.2026. Nu scurta retenția fără să scurtezi fereastra în același timp.
# `|| true`: sub `set -o pipefail` un psql picat (sau SIGPIPE de la `head`) ar face atribuirea sa
# iasa cu cod != 0, iar `set -e` ar OMORI scriptul exact ca bug-ul reparat mai sus.
RETENTION_DAYS=$($DB_CMD -c "SELECT (value->>'days')::int FROM settings WHERE key='documents.process_window'" 2>/dev/null | head -1 | tr -cd '0-9' || true)
if [ -z "$RETENTION_DAYS" ] || [ "$RETENTION_DAYS" -lt 1 ] 2>/dev/null; then
    RETENTION_DAYS=2
    log "  ATENTIE: nu am putut citi documents.process_window — folosesc $RETENTION_DAYS zile"
fi
log "3/3 Cleanup document_extractions procesate, mai vechi de $RETENTION_DAYS zile..."
CLEANUP_FAILED=0

# Întoarce nr. de rânduri șterse, sau "ERR" dacă interogarea însăși a eșuat.
# Distincția contează: cu `2>/dev/null` un DB oprit / o coloană redenumită arăta identic cu
# „nimic de șters" (0), deci tabela putea crește săptămâni fără nicio alertă în log.
_count_deleted() {
    local out rc
    out=$($DB_CMD -c "$1" 2>&1); rc=$?
    if [ $rc -ne 0 ]; then
        # `log` scrie pe stdout, care AICI e capturat de $(...) — mesajul s-ar amesteca in valoarea
        # returnata si ar rupe comparatia de mai jos. Deci raportam pe stderr (ajunge tot in log
        # prin redirectarea din cron) si intoarcem doar marcajul.
        log "  EROARE la DELETE (cod $rc): $(printf '%s' "$out" | head -2 | tr '\n' ' ')" >&2
        printf 'ERR'
        return 0
    fi
    # `grep -c` iese cu cod 1 cand nu gaseste nimic (zero randuri sterse — caz normal). Cu
    # `set -o pipefail` + `set -e` asta ar OPRI scriptul exact ca bug-ul reparat mai sus, asa ca
    # neutralizam codul de iesire fara sa mai adaugam un "0" pe a doua linie.
    local n
    n=$(printf '%s' "$out" | grep -c '^[0-9]' || true)
    printf '%s' "$(printf '%s' "$n" | head -1 | tr -cd '0-9')"
}

# Pass 1: copii grupate (grouped_into != NULL) — FK constraint
DEL1=$(_count_deleted "DELETE FROM document_extractions
     WHERE status NOT IN ('pending')
       AND created_at < CURRENT_DATE - make_interval(days => $RETENTION_DAYS)
       AND grouped_into IS NOT NULL
     RETURNING id")

# Pass 2: root-uri (grouped_into NULL)
DEL2=$(_count_deleted "DELETE FROM document_extractions
     WHERE status NOT IN ('pending')
       AND created_at < CURRENT_DATE - make_interval(days => $RETENTION_DAYS)
       AND grouped_into IS NULL
     RETURNING id")

if [ "$DEL1" = "ERR" ] || [ "$DEL2" = "ERR" ]; then
    log "  document_extractions: CURĂȚENIE EȘUATĂ — vezi eroarea de mai sus (tabela NU a fost curățată)"
    CLEANUP_FAILED=1
else
    DOC_DELETED=$(( DEL1 + DEL2 ))
    log "  document_extractions: $DOC_DELETED rânduri șterse"
fi

# ── RAPORT FINAL ────────────────────────────────────────────────────────────
DISK_FREE=$(df -h / | awk 'NR==2{print $4}')
log "Disc liber după cleanup: $DISK_FREE"
log "=== DONE storage_cleanup ==="
# Cod de ieșire != 0 doar dacă o etapă chiar a eșuat — cron/monitorizarea trebuie să poată face
# diferența între „noapte curată" și „curățenia nu s-a făcut".
[ "${CLEANUP_FAILED:-0}" -eq 0 ] || exit 1
