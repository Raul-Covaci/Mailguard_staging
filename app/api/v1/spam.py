"""Native spam view + actions. Spam is distinct from phishing: bulk/marketing
mail that is unwanted but not a security threat. Scores live in the side table
email_spam (no schema change to emails). See app/services/spam_detector.py.

Actions:
  legit     — marchează ca legitim + adaugă expeditorul în allowlist persistent
  mark_spam — confirmă ca spam + adaugă expeditorul în blocklist persistent
              + încearcă dezabonare one-click (best-effort, via List-Unsubscribe din email_headers)
"""
import json
import re
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel

from app.database import get_db
from app.api.v1.auth import get_current_admin
from app.services.spam_detector import (detect_spam, DEFAULT_SPAM_THRESHOLD,
                                         classify_spam_gate, get_sender_reputation)
from app.services import sender_block, sender_lists

logger = logging.getLogger("mailguard.spam")

router = APIRouter()

# Statuses that are NOT eligible for the spam view (phishing/system states).
_EXCLUDED = ('quarantined', 'quarantined_strict', 'released', 'ndr', 'deleted', 'pending')

# ── Queue-status support (migrația 20260611_queue_status.sql), defensiv ─────────
_QUEUE_COLS_SPAM = None


def _queue_cols_exist(db) -> bool:
    global _QUEUE_COLS_SPAM
    if _QUEUE_COLS_SPAM is None:
        try:
            _QUEUE_COLS_SPAM = db.execute(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='emails' AND column_name='queue_status'")).fetchone() is not None
        except Exception:
            _QUEUE_COLS_SPAM = False
    return _QUEUE_COLS_SPAM


class SpamAction(BaseModel):
    action: str  # 'legit' | 'mark_spam'


def _get_from_address(email_id: int, db: Session) -> str:
    row = db.execute(text("SELECT from_address FROM emails WHERE id=:id"), {"id": email_id}).fetchone()
    return (row[0] or '').lower().strip() if row else ''


def _attempt_unsubscribe(email_id: int, actor: str, db: Session) -> None:
    """Best-effort RCTS 8058 one-click unsubscribe. Loghează rezultatul, nu aruncă excepții."""
    from_addr = _get_from_address(email_id, db)
    method, url, http_status, success, error = 'none', None, None, None, None

    try:
        row = db.execute(text("SELECT email_headers FROM emails WHERE id=:id"), {"id": email_id}).fetchone()
        headers_json = (row[0] or {}) if row else {}

        unsub_header = headers_json.get('list_unsubscribe') or ''
        unsub_post = headers_json.get('list_unsubscribe_post') or ''

        # RCTS 8058: dezabonăm DOAR dacă există explicit One-Click + URL HTTPS
        if unsub_header and 'List-Unsubscribe=One-Click' in unsub_post:
            m = re.search(r'<(https://[^>]+)>', unsub_header)
            if m:
                url = m.group(1)
                method = 'one_click'
                r = httpx.post(
                    url,
                    data={'List-Unsubscribe': 'One-Click'},
                    timeout=10.0,
                    follow_redirects=False,
                )
                http_status = r.status_code
                success = 200 <= r.status_code < 300
                logger.info("unsubscribe one-click %s email_id=%d status=%d", url, email_id, http_status)
        # else: method='none' — List-Unsubscribe nu e disponibil în pipeline-ul actual
        # (mail-parser nu persistă headerele brute; va fi extins în faza viitoare)

    except Exception as exc:
        error = str(exc)[:200]
        logger.warning("unsubscribe attempt failed email_id=%d: %s", email_id, error)

    try:
        db.execute(text("""
            INSERT INTO spam_unsubscribe_log
              (email_id, from_address, method, url, http_status, success, error_message)
            VALUES (:eid, :addr, :meth, :url, :hs, :ok, :err)
        """), {
            "eid": email_id, "addr": from_addr,
            "meth": method, "url": url,
            "hs": http_status, "ok": success, "err": error,
        })
    except Exception as exc:
        logger.warning("unsubscribe log insert failed email_id=%d: %s", email_id, exc)


@router.get("/spam")
def list_spam(threshold: int = Query(DEFAULT_SPAM_THRESHOLD, ge=0, le=100),
              page: int = Query(1, ge=1),
              db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Emails classified as spam (override=true, or score>=threshold and not marked legit),
    excluding phishing-quarantined and system states."""
    per = 50
    off = (page - 1) * per
    where = ("e.status NOT IN :exc AND ("
             "s.override = TRUE OR (s.override IS DISTINCT FROM FALSE AND s.spam_score >= :thr))")
    params = {"exc": _EXCLUDED, "thr": threshold}
    total = db.execute(text(
        "SELECT count(*) FROM email_spam s JOIN emails e ON e.id = s.email_id WHERE " + where
    ), params).scalar()
    rows = db.execute(text(
        "SELECT e.id, e.subject, e.from_address, e.from_name, e.received_at, e.status, "
        "e.ai_category, e.ai_status, e.ai_result, "
        "(SELECT COUNT(*) FROM attachments a WHERE a.email_id = e.id) AS attachment_count, "
        "(SELECT model FROM ai_call_log WHERE email_id = e.id ORDER BY id DESC LIMIT 1) AS ai_model, "
        "s.spam_score, s.spam_reasons, s.override, cl.name AS client_name "
        "FROM email_spam s JOIN emails e ON e.id = s.email_id "
        "LEFT JOIN clients cl ON cl.id = e.client_id "
        "WHERE " + where +
        " ORDER BY e.received_at DESC NULLS LAST, s.spam_score DESC LIMIT :lim OFFSET :off"
    ), dict(params, lim=per, off=off)).fetchall()
    # Toate randurile din /spam sunt, prin definitia where, emailuri spam — derivam statusul
    # afisat 'spam' (oglindeste list_emails). status-ul real din DB ramane neschimbat; campul
    # serveste doar badge-ul din lista (modalul foloseste mode='spam' explicit).
    items = []
    for r in rows:
        d = dict(r._mapping)
        d["status"] = "spam"
        items.append(d)
    return {"total": total, "page": page, "threshold": threshold,
            "items": items}


@router.post("/spam/backfill")
def backfill(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Recalculează spam pentru toate emailurile în email_spam (re-rulabil, idempotent).

    Aplică ACEEAȘI poartă de liste expeditori ca pipeline-ul live (`classify_spam_gate`):
      - allowlist / whitelist manuală (expeditor real SAU adresă-email citată în thread) ⇒ override=FALSE (NU spam);
      - blocklist / blacklist de orice tip (sender_block) ⇒ override=TRUE (spam forțat) — BATE whitelist-ul;
      - altfel ⇒ doar scorul, fără a atinge override (păstrează deciziile manuale „legit"/„mark_spam").
    Folosește acest endpoint ca BACKFILL retroactiv după activarea analizei full-thread / whitelist.
    Scorul reflectă ANALYZE_FULL_THREAD (tot thread-ul când e ON). Pur SQL, fără apeluri AI.
    """
    # Liste manuale (whitelist + blacklist tip=spam), o singură dată.
    manual_whitelist, manual_spamlist = set(), set()
    try:
        _mlrow = db.execute(text(
            "SELECT value FROM settings WHERE key='phishing_manual_learning'")).fetchone()
        _ml = (_mlrow[0] if _mlrow and _mlrow[0] else {}) or {}
        for _k, _v in (_ml.get('whitelist') or {}).items():
            if _k and not (isinstance(_v, dict) and _v.get('muted')):
                manual_whitelist.add(_k.lower())
        for _k, _v in (_ml.get('blacklist') or {}).items():
            if not _k or (isinstance(_v, dict) and _v.get('muted')):
                continue
            if sender_lists.entry_tip(_v if isinstance(_v, dict) else {}) == 'spam':
                manual_spamlist.add(_k.lower())
    except Exception:
        logger.exception("backfill: manual lists load failed")

    # Blacklist de orice tip + blocklist reputație — BAT whitelist-ul (sender_block, 2026-09-15).
    block_keys = sender_block.load_keys_sa(db)

    rows = db.execute(text(
        "SELECT id, from_address, subject, body_text, body_html FROM emails")).fetchall()
    n = bypassed = forced = 0
    for r in rows:
        em = dict(r._mapping)
        rep = get_sender_reputation(em.get("from_address"), db)
        score, reasons, override = classify_spam_gate(
            em, reputation=rep, manual_whitelist=manual_whitelist,
            manual_spamlist=manual_spamlist,
            blocked=block_keys.match(em.get("from_address")) is not None)
        if override is False:
            db.execute(text(
                "INSERT INTO email_spam (email_id, spam_score, spam_reasons, override) "
                "VALUES (:id, 0, CAST(:rs AS jsonb), FALSE) "
                "ON CONFLICT (email_id) DO UPDATE SET spam_score=0, "
                "spam_reasons=EXCLUDED.spam_reasons, override=FALSE, computed_at=now()"),
                {"id": em["id"], "rs": json.dumps(reasons)})
            bypassed += 1
        elif override is True:
            db.execute(text(
                "INSERT INTO email_spam (email_id, spam_score, spam_reasons, override) "
                "VALUES (:id, :sc, CAST(:rs AS jsonb), TRUE) "
                "ON CONFLICT (email_id) DO UPDATE SET spam_score=EXCLUDED.spam_score, "
                "spam_reasons=EXCLUDED.spam_reasons, override=TRUE, computed_at=now()"),
                {"id": em["id"], "sc": score, "rs": json.dumps(reasons)})
            forced += 1
        else:
            db.execute(text(
                "INSERT INTO email_spam (email_id, spam_score, spam_reasons) "
                "VALUES (:id, :sc, CAST(:rs AS jsonb)) "
                "ON CONFLICT (email_id) DO UPDATE SET spam_score=EXCLUDED.spam_score, "
                "spam_reasons=EXCLUDED.spam_reasons, computed_at=now()"),
                {"id": em["id"], "sc": score, "rs": json.dumps(reasons)})
        n += 1
    db.commit()
    return {"ok": True, "processed": n, "whitelist_bypassed": bypassed, "forced_spam": forced}


@router.post("/spam/{email_id}/action")
def spam_action(email_id: int, body: SpamAction,
                db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Acțiuni pe un email spam.

    legit     — marchează expeditorul ca de încredere (allowlist) + scoate emailul din spam
    mark_spam — confirmă ca spam (blocklist) + încearcă dezabonare one-click
    """
    row = db.execute(text("SELECT id, status FROM emails WHERE id=:id"),
                     {"id": email_id}).fetchone()
    if not row:
        raise HTTPException(404, "Email not found")

    reviewer = admin.get("username") or admin.get("email") or "admin"
    act = body.action

    if act not in ("legit", "mark_spam"):
        raise HTTPException(400, f"Acțiune necunoscută: {act!r}. Valori acceptate: legit, mark_spam")

    from_addr = _get_from_address(email_id, db)

    if act == "legit":
        # ⛔ Blacklist-ul BATE „Legit" (sender_block, 2026-09-15). Excepție: blocklist-ul de reputație
        # pe ADRESA EXACTĂ — e rândul scris de „Marchează ca SPAM", iar „Legit" e inversul lui și îl
        # suprascrie mai jos. Blacklist-ul manual și blocklist-ul pe domeniu se scot din Setări.
        blocked_by = sender_block.blocked_by_sa(db, from_addr, include_reputation_exact=False)
        if blocked_by:
            raise HTTPException(409, "Expeditorul e blocat prin blacklist (%s). Scoate intrarea din "
                                     "Setări → Liste expeditori (sau marcheaz-o ignorată), apoi "
                                     "reîncearcă." % blocked_by)

        # 1. Upsert allowlist — last-write-wins
        db.execute(text("""
            INSERT INTO spam_sender_reputation
              (scope_type, scope_value, reputation, created_by, last_action, action_count)
            VALUES ('sender_exact', :addr, 'allowlist', :by, 'legit', 1)
            ON CONFLICT (scope_type, scope_value) DO UPDATE SET
              reputation  = 'allowlist',
              updated_at  = NOW(),
              last_action = 'legit',
              action_count = spam_sender_reputation.action_count + 1,
              created_by  = :by
        """), {"addr": from_addr, "by": reviewer})

        # 2. Override=FALSE pe TOATE emailurile acestui expeditor (retroactiv)
        db.execute(text("""
            UPDATE email_spam SET override = FALSE, reviewed_by = :by, reviewed_at = NOW()
            FROM emails e
            WHERE email_spam.email_id = e.id
              AND LOWER(e.from_address) = :addr
        """), {"addr": from_addr, "by": reviewer})

        # 3. Status → clean pentru TOATE emailurile aceluiași expeditor (nu doar cel curent):
        #    „Legit" e o decizie despre EXPEDITOR, deci toate mailurile lui ies din spam, nu doar
        #    cel pe care a dat click operatorul. Stările imuabile de securitate rămân neatinse
        #    (carantina bate Legit). Guard pe from_addr ne-gol ca să nu prindem expeditori vizi.
        if from_addr:
            db.execute(text("""
                UPDATE emails SET status = 'clean'
                WHERE LOWER(from_address) = :addr
                  AND status NOT IN ('quarantined','quarantined_strict','ndr','deleted')
            """), {"addr": from_addr})
        else:
            db.execute(text("""
                UPDATE emails SET status = 'clean'
                WHERE id = :id
                  AND status NOT IN ('quarantined','quarantined_strict','ndr','deleted')
            """), {"id": email_id})

        # 4. Repune pe calea de categorizare (manual_clean) TOATE mailurile expeditorului blocate
        #    ca spam (queue_status='stopped_spam'), plus cel curent — ca să nu mai rămână „stuck"
        #    fără categorie/ CTS doar cele neclicate. NOVA face DOAR categorie (nu re-carantinează).
        #    advance_queue_batch (tick la 5 min) le preia → ai_category → ready_for_cts. Mailurile
        #    deja pe calea sănătoasă (ready_for_cts/sent) NU se ating (evităm re-trimitere la CTS).
        if _queue_cols_exist(db):
            if from_addr:
                db.execute(text("""
                    UPDATE emails SET queue_status='queued_general', manual_clean=TRUE,
                           sent_to_cts_at=NULL, cts_send_error=NULL
                    WHERE LOWER(from_address) = :addr
                      AND status NOT IN ('quarantined','quarantined_strict','ndr','deleted')
                      AND (queue_status='stopped_spam' OR id = :id)
                """), {"addr": from_addr, "id": email_id})
            else:
                db.execute(text("""
                    UPDATE emails SET queue_status='queued_general', manual_clean=TRUE,
                           sent_to_cts_at=NULL, cts_send_error=NULL
                    WHERE id=:id AND status NOT IN ('quarantined','quarantined_strict','ndr','deleted')
                """), {"id": email_id})

        # 5. Sync în whitelist-ul de detecție (învățare: expeditor de încredere).
        #    Soft — suprimă semnale slabe la categorizările viitoare. Best-effort.
        try:
            if from_addr:
                sender_lists.add_entry(db, "whitelist", from_addr, reviewer,
                                       email_id=email_id, source="legit", commit=False)
        except Exception:
            logger.exception("whitelist sync (legit) failed email_id=%s", email_id)

    elif act == "mark_spam":
        # 1. Upsert blocklist — last-write-wins
        db.execute(text("""
            INSERT INTO spam_sender_reputation
              (scope_type, scope_value, reputation, created_by, last_action, action_count)
            VALUES ('sender_exact', :addr, 'blocklist', :by, 'mark_spam', 1)
            ON CONFLICT (scope_type, scope_value) DO UPDATE SET
              reputation  = 'blocklist',
              updated_at  = NOW(),
              last_action = 'mark_spam',
              action_count = spam_sender_reputation.action_count + 1,
              created_by  = :by
        """), {"addr": from_addr, "by": reviewer})

        # 2. Override=TRUE pe emailul curent (spam confirmat)
        db.execute(text("""
            INSERT INTO email_spam (email_id, override, reviewed_by, reviewed_at)
            VALUES (:id, TRUE, :by, NOW())
            ON CONFLICT (email_id) DO UPDATE SET
              override = TRUE, reviewed_by = :by, reviewed_at = NOW()
        """), {"id": email_id, "by": reviewer})

        # 3. Dezabonare best-effort (nu aruncă excepție)
        _attempt_unsubscribe(email_id, reviewer, db)

        # 4. Spam confirmat manual → terminal stopped_spam (nu merge la CTS, nu apelează IRIS)
        if _queue_cols_exist(db):
            db.execute(text("UPDATE emails SET queue_status='stopped_spam', manual_clean=FALSE "
                            "WHERE id=:id AND status NOT IN ('quarantined','quarantined_strict','ndr','deleted')"),
                       {"id": email_id})

        # 5. Sync în blacklist-ul de detecție cu tip='spam' → influențează DOAR scoringul de spam
        #    (override), NU carantina. Best-effort. Dacă e deja pe whitelist (decizie umană
        #    explicită), add_entry NU îl mută.
        try:
            if from_addr:
                sender_lists.add_entry(db, "blacklist", from_addr, reviewer,
                                       email_id=email_id, source="spam_confirm",
                                       tip="spam", commit=False)
        except Exception:
            logger.exception("blacklist sync (spam) failed email_id=%s", email_id)

    # Audit log cu from_address pentru trasabilitate
    db.execute(text("""
        INSERT INTO audit_log(actor, action, entity_type, entity_id, details)
        VALUES (:a, :ac, 'email', :id, CAST(:d AS jsonb))
    """), {
        "a": reviewer,
        "ac": "spam_" + act,
        "id": email_id,
        "d": json.dumps({"action": act, "from_address": from_addr}),
    })
    db.commit()
    return {"ok": True, "email_id": email_id, "action": act}


# ── CTS Spam Sync endpoints ────────────────────────────────────────────────────

@router.get("/spam/cts-sync/status")
def cts_sync_status(db: Session = Depends(get_db), _=Depends(get_current_admin)):
    """Starea sincronizării spam CTS → Cargo360: configurație + ultimul run."""
    from app.services.cts_spam_sync import is_configured, get_sync_state
    state = get_sync_state(db)
    return {"configured": is_configured(), "state": state}


@router.post("/spam/cts-sync")
def cts_sync_now(db: Session = Depends(get_db), user=Depends(get_current_admin)):
    """Declanșează manual sincronizarea spam CTS → Cargo360."""
    from app.services.cts_spam_sync import run_sync
    actor = (user.get("sub") or user.get("username") or "admin") if isinstance(user, dict) else str(user)
    return run_sync(triggered_by=actor)
