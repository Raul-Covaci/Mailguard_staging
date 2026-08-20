"""Personal mailbox management API (T1).

All endpoints scoped to the authenticated user (user_id from JWT).
No cross-user data leaks: every query filters AND user_id = :uid.
"""
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel, Field

from app.database import get_db
from app.api.v1.auth import get_current_admin
from app.services.credential_crypto import encrypt_credentials, decrypt_credentials
from app.services import personal_imap

logger = logging.getLogger("mailguard.personal_mailboxes")
router = APIRouter()


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class MailboxCreate(BaseModel):
    label: str = Field(..., min_length=1, max_length=120)
    imap_host: str = Field(..., min_length=1, max_length=255)
    imap_port: int = Field(993, ge=1, le=65535)
    imap_ssl: bool = True
    email_address: str = Field(..., min_length=5, max_length=320)
    password: str = Field(..., min_length=1, max_length=500)
    # SMTP — necesar doar pentru redirectul VATHUB. Gol = fără redirect.
    smtp_host: Optional[str] = Field(None, max_length=255)
    smtp_port: Optional[int] = Field(None, ge=1, le=65535)
    smtp_tls: Optional[bool] = None
    smtp_user: Optional[str] = Field(None, max_length=320)
    smtp_password: Optional[str] = Field(None, min_length=1, max_length=500)
    vathub_enabled: Optional[bool] = None
    # Filtrarea spam/carantină e ON implicit; OFF = mailurile se ingerează, dar
    # nu se scanează și nu se mută în SPAM/CARANTINA.
    filter_enabled: Optional[bool] = None


class MailboxUpdate(BaseModel):
    label: Optional[str] = Field(None, min_length=1, max_length=120)
    imap_host: Optional[str] = Field(None, min_length=1, max_length=255)
    imap_port: Optional[int] = Field(None, ge=1, le=65535)
    imap_ssl: Optional[bool] = None
    email_address: Optional[str] = Field(None, min_length=5, max_length=320)
    password: Optional[str] = Field(None, min_length=1, max_length=500)
    smtp_host: Optional[str] = Field(None, max_length=255)
    smtp_port: Optional[int] = Field(None, ge=1, le=65535)
    smtp_tls: Optional[bool] = None
    smtp_user: Optional[str] = Field(None, max_length=320)
    smtp_password: Optional[str] = Field(None, min_length=1, max_length=500)
    vathub_enabled: Optional[bool] = None
    filter_enabled: Optional[bool] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_account_or_404(db: Session, account_id: int, user_id: int) -> dict:
    row = db.execute(text("""
        SELECT id, user_id, label, imap_host, imap_port, imap_ssl,
               email_address, cred_enc, status, last_error, last_poll_at, last_uid,
               smtp_host, smtp_port, smtp_tls, smtp_user, smtp_cred_enc,
               vathub_enabled, filter_enabled, created_at, updated_at
        FROM personal_mailbox_accounts
        WHERE id = :id AND user_id = :uid
    """), {"id": account_id, "uid": user_id}).fetchone()
    if not row:
        raise HTTPException(404, "Account not found")
    return dict(row._mapping)


def _row_to_public(row: dict) -> dict:
    """Strip cred_enc from API response."""
    hidden = {"cred_enc", "smtp_cred_enc"}
    return {k: v for k, v in row.items() if k not in hidden}


def _clean_host(value: Optional[str]) -> Optional[str]:
    """Câmp de host golit din UI = fără server, deci NULL în DB (nu string vid)."""
    v = (value or "").strip()
    return v or None


def _validate_and_test(host: str, port: int, ssl: bool, email: str, password: str) -> tuple[str, Optional[str]]:
    """Run IMAP test_login. Returns (status, last_error)."""
    ok, err = personal_imap.test_login({
        "imap_host": host,
        "imap_port": port,
        "imap_ssl": ssl,
        "email_address": email,
        "_password": password,
    })
    return ("active" if ok else "error"), err


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/personal-mailboxes")
def list_mailboxes(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    rows = db.execute(text("""
        SELECT id, user_id, label, imap_host, imap_port, imap_ssl,
               email_address, status, last_error, last_poll_at, last_uid,
               smtp_host, smtp_port, smtp_tls, smtp_user,
               (smtp_cred_enc IS NOT NULL) AS smtp_has_password,
               vathub_enabled, filter_enabled, created_at, updated_at
        FROM personal_mailbox_accounts
        WHERE user_id = :uid
        ORDER BY created_at DESC
    """), {"uid": int(admin["id"])}).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/personal-mailboxes", status_code=201)
def create_mailbox(body: MailboxCreate, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    uid = int(admin["id"])

    # Check duplicate
    dup = db.execute(text("""
        SELECT id FROM personal_mailbox_accounts
        WHERE user_id = :uid AND email_address = :email
    """), {"uid": uid, "email": body.email_address.lower()}).fetchone()
    if dup:
        raise HTTPException(409, f"Mailbox '{body.email_address}' already configured for this user")

    cred_enc = encrypt_credentials({"user": body.email_address, "pass": body.password})
    status, last_error = _validate_and_test(body.imap_host, body.imap_port, body.imap_ssl,
                                             body.email_address, body.password)

    smtp_cred = (encrypt_credentials({"user": body.smtp_user or body.email_address,
                                      "pass": body.smtp_password})
                 if body.smtp_password else None)

    row = db.execute(text("""
        INSERT INTO personal_mailbox_accounts
            (user_id, label, imap_host, imap_port, imap_ssl, email_address,
             cred_enc, status, last_error,
             smtp_host, smtp_port, smtp_tls, smtp_user, smtp_cred_enc,
             vathub_enabled, filter_enabled)
        VALUES (:uid, :label, :host, :port, :ssl, :email, :cred, :status, :err,
                :smtp_host, :smtp_port, :smtp_tls, :smtp_user, :smtp_cred,
                :vathub, :filter_on)
        RETURNING id, user_id, label, imap_host, imap_port, imap_ssl,
                  email_address, status, last_error, last_poll_at, last_uid,
                  smtp_host, smtp_port, smtp_tls, smtp_user, vathub_enabled,
                  filter_enabled, created_at, updated_at
    """), {
        "uid": uid, "label": body.label, "host": body.imap_host,
        "port": body.imap_port, "ssl": body.imap_ssl,
        "email": body.email_address.lower(), "cred": cred_enc,
        "status": status, "err": last_error,
        "smtp_host": _clean_host(body.smtp_host), "smtp_port": body.smtp_port,
        "smtp_tls": True if body.smtp_tls is None else body.smtp_tls,
        "smtp_user": _clean_host(body.smtp_user), "smtp_cred": smtp_cred,
        "vathub": bool(body.vathub_enabled),
        "filter_on": True if body.filter_enabled is None else body.filter_enabled,
    }).fetchone()
    db.commit()
    return dict(row._mapping)


@router.get("/personal-mailboxes/{account_id}")
def get_mailbox(account_id: int, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    row = _get_account_or_404(db, account_id, int(admin["id"]))
    return _row_to_public(row)


@router.put("/personal-mailboxes/{account_id}")
def update_mailbox(account_id: int, body: MailboxUpdate,
                   db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    uid = int(admin["id"])
    row = _get_account_or_404(db, account_id, uid)

    new_host = body.imap_host or row["imap_host"]
    new_port = body.imap_port or row["imap_port"]
    new_ssl = body.imap_ssl if body.imap_ssl is not None else row["imap_ssl"]
    new_email = (body.email_address or row["email_address"]).lower()
    new_label = body.label or row["label"]

    if body.password:
        cred_enc = encrypt_credentials({"user": new_email, "pass": body.password})
        password_for_test = body.password
    else:
        # Re-encrypt with existing credentials if host/email changed
        cred_enc = row["cred_enc"]
        try:
            existing_creds = decrypt_credentials(cred_enc)
            password_for_test = existing_creds["pass"]
        except Exception:
            raise HTTPException(400, "Cannot re-validate: stored credentials unreadable. Provide password.")

    status, last_error = _validate_and_test(new_host, new_port, new_ssl, new_email, password_for_test)

    # SMTP: câmp lăsat gol = păstrează valoarea existentă. Parola nouă se
    # criptează separat de cea IMAP.
    if body.smtp_password:
        smtp_cred = encrypt_credentials({"user": body.smtp_user or new_email,
                                         "pass": body.smtp_password})
    else:
        smtp_cred = row["smtp_cred_enc"]

    db.execute(text("""
        UPDATE personal_mailbox_accounts
        SET label=:label, imap_host=:host, imap_port=:port, imap_ssl=:ssl,
            email_address=:email, cred_enc=:cred, status=:status,
            last_error=:err,
            smtp_host=:smtp_host, smtp_port=:smtp_port, smtp_tls=:smtp_tls,
            smtp_user=:smtp_user, smtp_cred_enc=:smtp_cred, vathub_enabled=:vathub,
            filter_enabled=:filter_on, updated_at=now()
        WHERE id=:id AND user_id=:uid
    """), {
        "label": new_label, "host": new_host, "port": new_port, "ssl": new_ssl,
        "email": new_email, "cred": cred_enc, "status": status, "err": last_error,
        "smtp_host": _clean_host(body.smtp_host) if body.smtp_host is not None else row["smtp_host"],
        "smtp_port": body.smtp_port if body.smtp_port is not None else row["smtp_port"],
        "smtp_tls": body.smtp_tls if body.smtp_tls is not None else row["smtp_tls"],
        "smtp_user": _clean_host(body.smtp_user) if body.smtp_user is not None else row["smtp_user"],
        "smtp_cred": smtp_cred,
        "vathub": body.vathub_enabled if body.vathub_enabled is not None else row["vathub_enabled"],
        "filter_on": body.filter_enabled if body.filter_enabled is not None else row["filter_enabled"],
        "id": account_id, "uid": uid,
    })
    db.commit()
    updated = _get_account_or_404(db, account_id, uid)
    return _row_to_public(updated)


@router.delete("/personal-mailboxes/{account_id}", status_code=204)
def delete_mailbox(account_id: int, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    uid = int(admin["id"])
    _get_account_or_404(db, account_id, uid)  # 404 guard
    db.execute(text("""
        DELETE FROM personal_mailbox_accounts WHERE id=:id AND user_id=:uid
    """), {"id": account_id, "uid": uid})
    db.commit()


@router.post("/personal-mailboxes/{account_id}/test-connection")
def test_connection(account_id: int, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    uid = int(admin["id"])
    row = _get_account_or_404(db, account_id, uid)
    try:
        creds = decrypt_credentials(row["cred_enc"])
    except Exception as e:
        raise HTTPException(500, f"Cannot decrypt credentials: {e}")

    ok, err = personal_imap.test_login({
        "imap_host": row["imap_host"],
        "imap_port": row["imap_port"],
        "imap_ssl": row["imap_ssl"],
        "email_address": row["email_address"],
        "_password": creds["pass"],
    })
    new_status = "active" if ok else "error"
    db.execute(text("""
        UPDATE personal_mailbox_accounts
        SET status=:status, last_error=:err, updated_at=now()
        WHERE id=:id AND user_id=:uid
    """), {"status": new_status, "err": err, "id": account_id, "uid": uid})
    db.commit()
    return {"ok": ok, "status": new_status, "error": err}


# ── E2E inject test (IMAP APPEND → poller picks up within ~1 min) ────────────

@router.post("/personal-mailboxes/{account_id}/inject-test")
def inject_test(
    account_id: int,
    scenario: str,
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Inject a synthetic RFC 2822 mail into INBOX via IMAP APPEND.
    Poller classifies and moves it within ~1 minute.
    scenario: quarantine|spam
    """
    if scenario not in ("quarantine", "spam"):
        raise HTTPException(400, "scenario must be 'quarantine' or 'spam'")
    uid = int(admin["id"])
    row = _get_account_or_404(db, account_id, uid)
    try:
        creds = decrypt_credentials(row["cred_enc"])
    except Exception as e:
        raise HTTPException(500, f"Cannot decrypt credentials: {e}")

    ok, msg, imap_uid = personal_imap.inject_test_mail(row, creds["pass"], scenario)
    if not ok:
        raise HTTPException(502, f"IMAP inject failed: {msg}")
    return {"ok": True, "scenario": scenario, "message": msg, "imap_uid": imap_uid}


# ── Detection smoke-test ──────────────────────────────────────────────────────

_SYNTHETIC_MAILS = {
    "quarantine": {
        "from_address": "security-alert@micros0ft-verify.com",
        "from_name": "Microsoft Security",
        "subject": "Urgent: resetează parola contului tău acum",
        "body_text": (
            "Contul tău a fost suspendat. Resetează imediat parola accesând link-ul de mai jos.\n"
            "Click aici: http://192.168.1.1/login/verify?token=abc123\n"
            "Acțiune necesară în 24 ore sau contul va fi blocat permanent."
        ),
        "body_html": "",
    },
    "spam": {
        "from_address": "promotions@bulk-offers-newsletter.com",
        "from_name": "Super Oferte",
        "subject": "🎉 CÂȘTIGĂ acum! Ofertă limitată — URGENT răspunde!",
        "body_text": (
            "Felicitări! Ai fost selectat pentru oferta noastră exclusivă!\n"
            "Cumpără acum și primești 90% reducere. Stoc limitat!\n"
            "Dezabonează-te: http://bit.ly/unsub999\n"
            "URGENT — oferta expiră AZI! Acționează RIGHT NOW!"
        ),
        "body_html": "",
    },
}


@router.post("/personal-mailboxes/{account_id}/test-detection")
def test_detection(
    account_id: int,
    scenario: str,
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Run detection on a synthetic email (no IMAP, no DB write). scenario: quarantine|spam."""
    if scenario not in _SYNTHETIC_MAILS:
        from fastapi import HTTPException as _HTTPException
        raise _HTTPException(400, "scenario must be 'quarantine' or 'spam'")

    uid = int(admin["id"])
    _get_account_or_404(db, account_id, uid)  # 404 guard + ownership check

    email_dict = dict(_SYNTHETIC_MAILS[scenario])

    from app.services import phishing_detector, spam_detector
    from app.services.personal_mail_processor import SPAM_THRESHOLD

    try:
        _score, ph_status, ph_reasons = phishing_detector.detect_phishing(
            email_dict, attachments=[], suppress_codes=set(),
            blacklist=None, whitelist=None,
        )
    except Exception as exc:
        ph_status, ph_reasons = "clean", []

    try:
        spam_score, spam_reasons = spam_detector.detect_spam(email_dict)
    except Exception:
        spam_score, spam_reasons = 0.0, []

    if ph_status in ("quarantined", "quarantined_strict"):
        verdict = "quarantined"
        folder_action = "move_quarantine"
        reasons = ph_reasons
    elif spam_score >= SPAM_THRESHOLD:
        verdict = "spam"
        folder_action = "move_spam"
        reasons = spam_reasons
    else:
        verdict = "clean"
        folder_action = "none"
        reasons = ph_reasons + spam_reasons

    return {
        "scenario": scenario,
        "verdict": verdict,
        "folder_action": folder_action,
        "spam_score": spam_score,
        "ph_status": ph_status,
        "reasons": reasons,
        "synthetic_mail": {
            "from": email_dict["from_address"],
            "subject": email_dict["subject"],
        },
    }


# ── Poll trigger (used by systemd timer via curl) ─────────────────────────────

@router.post("/personal-mailboxes/poll", include_in_schema=False)
def trigger_poll():
    """Trigger poll for all due accounts. Called by cargo360-personal-poll.service."""
    from app.services import personal_mailbox_poller
    return personal_mailbox_poller.run()

# ── Reguli personale: blacklist / whitelist izolat de CTS ─────────────────────
# Cheie KV separată de 'phishing_manual_learning' (CTS) — fără impact cross.

import json as _json
from datetime import datetime as _datetime, timezone as _timezone
from fastapi import Query as _Query
from app.services.sender_lists import normalize as _normalize, entry_tip as _entry_tip, LISTS as _LISTS, TIPS as _TIPS

_PERSONAL_KEY = "personal_phishing_manual_learning"


def _pl_load(db: Session) -> dict:
    row = db.execute(text("SELECT value FROM settings WHERE key=:k"), {"k": _PERSONAL_KEY}).fetchone()
    store = (row[0] if row and row[0] else {}) or {}
    for lst in _LISTS:
        if not isinstance(store.get(lst), dict):
            store[lst] = {}
    return store


def _pl_save(db: Session, store: dict, by: str) -> None:
    db.execute(text(
        "INSERT INTO settings(key, value, description, updated_by, updated_at) "
        "VALUES(:k, CAST(:v AS jsonb), 'Reguli personale mailbox: blacklist + whitelist', :by, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_by=:by, updated_at=NOW()"
    ), {"k": _PERSONAL_KEY, "v": _json.dumps(store), "by": by})


def _pl_entry_out(value: str, meta: dict) -> dict:
    meta = meta or {}
    _key, scope = _normalize(value)
    source = meta.get("source") or "manual"
    return {
        "value": value,
        "scope": scope or "domain",
        "by": meta.get("by"),
        "at": meta.get("at"),
        "muted": bool(meta.get("muted")),
        "note": meta.get("note"),
        "source": source,
        "tip": _entry_tip(meta),
    }


@router.get("/personal-mailboxes/rules/sender-lists")
def pl_get_sender_lists(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    store = _pl_load(db)
    result = {}
    for lst in _LISTS:
        items = [_pl_entry_out(k, v) for k, v in (store.get(lst) or {}).items()]
        items.sort(key=lambda e: (e["muted"], e["value"]))
        result[lst] = items
    result["counts"] = {lst: len(result[lst]) for lst in _LISTS}
    return result


@router.post("/personal-mailboxes/rules/sender-lists")
def pl_add_sender_list(body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    reviewer = admin.get("username") or admin.get("email") or "admin"
    lst = (body.get("list") or "").strip().lower()
    if lst not in _LISTS:
        raise HTTPException(400, "Listă invalidă (blacklist/whitelist)")
    tip = (body.get("tip") or "").strip().lower() or None
    if tip and tip not in _TIPS:
        raise HTTPException(400, "Tip invalid (carantina/spam)")
    key, scope = _normalize(body.get("value") or "")
    if not key:
        raise HTTPException(400, "Valoare goală")
    store = _pl_load(db)
    other = "whitelist" if lst == "blacklist" else "blacklist"
    if key in (store.get(other) or {}):
        raise HTTPException(409, f"Valoarea există deja în '{other}'. Șterge-o întâi.")
    entry = dict((store.get(lst) or {}).get(key) or {})
    entry.setdefault("by", reviewer)
    entry.setdefault("at", _datetime.now(_timezone.utc).isoformat())
    entry.setdefault("source", "manual")
    entry.setdefault("muted", False)
    if body.get("note") is not None:
        entry["note"] = body["note"]
    if lst == "blacklist":
        t = (tip or "").strip().lower()
        if t in _TIPS:
            entry["tip"] = t
        elif "tip" not in entry:
            entry["tip"] = "carantina"
    store[lst][key] = entry
    _pl_save(db, store, reviewer)
    db.commit()
    return {"ok": True, "list": lst, "value": key, "scope": scope}


@router.put("/personal-mailboxes/rules/sender-lists")
def pl_edit_sender_list(body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    reviewer = admin.get("username") or admin.get("email") or "admin"
    lst = (body.get("list") or "").strip().lower()
    if lst not in _LISTS:
        raise HTTPException(400, "Listă invalidă")
    tip = (body.get("tip") or "").strip().lower() or None
    if tip and tip not in _TIPS:
        raise HTTPException(400, "Tip invalid (carantina/spam)")
    key, _ = _normalize(body.get("value") or "")
    store = _pl_load(db)
    bucket = store.get(lst) or {}
    if key not in bucket:
        raise HTTPException(404, "Intrare inexistentă")
    entry = dict(bucket[key])
    if body.get("muted") is not None:
        entry["muted"] = bool(body["muted"])
    if body.get("note") is not None:
        entry["note"] = body["note"]
    if tip and lst == "blacklist":
        entry["tip"] = tip
    new_value = body.get("new_value")
    target = key
    if new_value:
        nk, _ = _normalize(new_value)
        if nk and nk != key:
            other = "whitelist" if lst == "blacklist" else "blacklist"
            if nk in (store.get(other) or {}):
                raise HTTPException(400, "Valoarea există deja în lista opusă")
            if nk in bucket:
                raise HTTPException(400, "Valoarea există deja în această listă")
            del bucket[key]
            target = nk
    bucket[target] = entry
    store[lst] = bucket
    _pl_save(db, store, reviewer)
    db.commit()
    return {"ok": True, "value": target}


@router.delete("/personal-mailboxes/rules/sender-lists")
def pl_delete_sender_list(
    list: str = _Query(...),
    value: str = _Query(...),
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    reviewer = admin.get("username") or admin.get("email") or "admin"
    if list not in _LISTS:
        raise HTTPException(400, "Listă invalidă")
    key, _ = _normalize(value)
    store = _pl_load(db)
    if key not in (store.get(list) or {}):
        raise HTTPException(404, "Intrare inexistentă")
    del store[list][key]
    _pl_save(db, store, reviewer)
    db.commit()
    return {"ok": True, "removed": key}


# ── Reguli per-cont: blacklist / whitelist izolate per account_id ──────────────

_ACCOUNT_SL_KEY = "personal_mailbox_senderlist_{}"


def _acct_sl_load(db: Session, account_id: int) -> dict:
    key = _ACCOUNT_SL_KEY.format(account_id)
    row = db.execute(text("SELECT value FROM settings WHERE key=:k"), {"k": key}).fetchone()
    store = (row[0] if row and row[0] else {}) or {}
    for lst in _LISTS:
        if not isinstance(store.get(lst), dict):
            store[lst] = {}
    return store


def _acct_sl_save(db: Session, account_id: int, store: dict, by: str) -> None:
    key = _ACCOUNT_SL_KEY.format(account_id)
    db.execute(text(
        "INSERT INTO settings(key, value, description, updated_by, updated_at) "
        "VALUES(:k, CAST(:v AS jsonb), :desc, :by, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_by=:by, updated_at=NOW()"
    ), {"k": key, "v": _json.dumps(store), "desc": f"Sender lists cont {account_id}", "by": by})


@router.get("/personal-mailboxes/{account_id}/sender-lists")
def acct_get_sender_lists(account_id: int, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    uid = int(admin["id"])
    _get_account_or_404(db, account_id, uid)
    store = _acct_sl_load(db, account_id)
    result = {}
    for lst in _LISTS:
        items = [_pl_entry_out(k, v) for k, v in (store.get(lst) or {}).items()]
        items.sort(key=lambda e: (e["muted"], e["value"]))
        result[lst] = items
    result["counts"] = {lst: len(result[lst]) for lst in _LISTS}
    return result


@router.post("/personal-mailboxes/{account_id}/sender-lists")
def acct_add_sender_list(account_id: int, body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    uid = int(admin["id"])
    _get_account_or_404(db, account_id, uid)
    reviewer = admin.get("username") or admin.get("email") or "admin"
    lst = (body.get("list") or "").strip().lower()
    if lst not in _LISTS:
        raise HTTPException(400, "Listă invalidă (blacklist/whitelist)")
    tip = (body.get("tip") or "").strip().lower() or None
    if tip and tip not in _TIPS:
        raise HTTPException(400, "Tip invalid (carantina/spam)")
    key, scope = _normalize(body.get("value") or "")
    if not key:
        raise HTTPException(400, "Valoare goală")
    store = _acct_sl_load(db, account_id)
    other = "whitelist" if lst == "blacklist" else "blacklist"
    if key in (store.get(other) or {}):
        raise HTTPException(409, f"Valoarea există deja în '{other}'. Șterge-o întâi.")
    entry = dict((store.get(lst) or {}).get(key) or {})
    entry.setdefault("by", reviewer)
    entry.setdefault("at", _datetime.now(_timezone.utc).isoformat())
    entry.setdefault("source", "manual")
    entry.setdefault("muted", False)
    if body.get("note") is not None:
        entry["note"] = body["note"]
    if lst == "blacklist":
        t = (tip or "").strip().lower()
        entry["tip"] = t if t in _TIPS else "carantina"
    store[lst][key] = entry
    _acct_sl_save(db, account_id, store, reviewer)
    db.commit()
    return {"ok": True, "list": lst, "value": key, "scope": scope}


@router.put("/personal-mailboxes/{account_id}/sender-lists")
def acct_edit_sender_list(account_id: int, body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    uid = int(admin["id"])
    _get_account_or_404(db, account_id, uid)
    reviewer = admin.get("username") or admin.get("email") or "admin"
    lst = (body.get("list") or "").strip().lower()
    if lst not in _LISTS:
        raise HTTPException(400, "Listă invalidă")
    tip = (body.get("tip") or "").strip().lower() or None
    if tip and tip not in _TIPS:
        raise HTTPException(400, "Tip invalid (carantina/spam)")
    key, _ = _normalize(body.get("value") or "")
    store = _acct_sl_load(db, account_id)
    bucket = store.get(lst) or {}
    if key not in bucket:
        raise HTTPException(404, "Intrare inexistentă")
    entry = dict(bucket[key])
    if body.get("muted") is not None:
        entry["muted"] = bool(body["muted"])
    if body.get("note") is not None:
        entry["note"] = body["note"]
    if tip and lst == "blacklist":
        entry["tip"] = tip
    new_value = body.get("new_value")
    target = key
    if new_value:
        nk, _ = _normalize(new_value)
        if nk and nk != key:
            other = "whitelist" if lst == "blacklist" else "blacklist"
            if nk in (store.get(other) or {}):
                raise HTTPException(400, "Valoarea există deja în lista opusă")
            if nk in bucket:
                raise HTTPException(400, "Valoarea există deja în această listă")
            del bucket[key]
            target = nk
    bucket[target] = entry
    store[lst] = bucket
    _acct_sl_save(db, account_id, store, reviewer)
    db.commit()
    return {"ok": True, "value": target}


@router.delete("/personal-mailboxes/{account_id}/sender-lists")
def acct_delete_sender_list(
    account_id: int,
    list: str = _Query(...),
    value: str = _Query(...),
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    uid = int(admin["id"])
    _get_account_or_404(db, account_id, uid)
    reviewer = admin.get("username") or admin.get("email") or "admin"
    if list not in _LISTS:
        raise HTTPException(400, "Listă invalidă")
    key, _ = _normalize(value)
    store = _acct_sl_load(db, account_id)
    if key not in (store.get(list) or {}):
        raise HTTPException(404, "Intrare inexistentă")
    del store[list][key]
    _acct_sl_save(db, account_id, store, reviewer)
    db.commit()
    return {"ok": True, "removed": key}


# ── Redirect VATHUB ───────────────────────────────────────────────────────────
# Mailurile oficiale de recuperare TVA ajung pe căsuța personală a persoanei care
# a depus declarația. Aici se administrează lista de expeditori de autoritate și
# adresa generală către care se retrimit. Vezi app/services/vathub_forward.py.

from app.services import vathub_forward as _vf
from app.services import personal_smtp as _psmtp
from app.services.vathub_send_guard import ALLOWED_FORWARD_TARGETS as _VH_TARGETS

_VH_LISTS = ("domains", "addresses")


def _vh_load(db: Session) -> dict:
    row = db.execute(text("SELECT value FROM settings WHERE key=:k"),
                     {"k": _vf.SETTINGS_KEY}).fetchone()
    store = (row[0] if row and row[0] else {}) or {}
    base = {"target": "vathub@cargotrack.ro", "enabled": False,
            "max_age_hours": _vf.DEFAULT_MAX_AGE_HOURS}
    base.update(store if isinstance(store, dict) else {})
    for lst in _VH_LISTS:
        if not isinstance(base.get(lst), dict):
            base[lst] = {}
    return base


def _vh_save(db: Session, store: dict, by: str) -> None:
    db.execute(text(
        "INSERT INTO settings(key, value, description, updated_by, updated_at) "
        "VALUES(:k, CAST(:v AS jsonb), "
        "'Redirect VATHUB: expeditori de autoritate fiscala + adresa tinta', :by, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_by=:by, updated_at=NOW()"
    ), {"k": _vf.SETTINGS_KEY, "v": _json.dumps(store), "by": by})


def _vh_norm(list_name: str, value: str) -> str:
    """Domeniile se rețin fără `@`, adresele complet. Ambele lowercase."""
    v = (value or "").strip().lower().lstrip("@")
    if not v:
        raise HTTPException(400, "Valoare goală")
    if list_name == "addresses" and "@" not in v:
        raise HTTPException(400, "Adresa trebuie să conțină '@' (pentru domeniu folosește lista de domenii)")
    if list_name == "domains" and "@" in v:
        raise HTTPException(400, "Domeniul nu conține '@' (pentru adresă folosește lista de adrese)")
    return v


def _vh_entry_out(value: str, meta: dict) -> dict:
    meta = meta or {}
    return {
        "value": value,
        "muted": bool(meta.get("muted")),
        "note": meta.get("note"),
        "source": meta.get("source") or "manual",
        "by": meta.get("by"),
        "at": meta.get("at"),
    }


@router.get("/personal-mailboxes/rules/vathub")
def vh_get_rules(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Configul redirectului + listele de expeditori.

    `muted: true` = intrare extrasă din trafic, încă nevalidată de echipă;
    motorul o ignoră până e activată.
    """
    store = _vh_load(db)
    out = {
        "target": store.get("target"),
        "enabled": bool(store.get("enabled")),
        "max_age_hours": int(store.get("max_age_hours") or _vf.DEFAULT_MAX_AGE_HOURS),
        "allowed_targets": sorted(_VH_TARGETS),
    }
    for lst in _VH_LISTS:
        items = [_vh_entry_out(k, v) for k, v in (store.get(lst) or {}).items()]
        items.sort(key=lambda e: (e["muted"], e["value"]))
        out[lst] = items
    out["counts"] = {
        lst: {"total": len(out[lst]), "active": sum(1 for e in out[lst] if not e["muted"])}
        for lst in _VH_LISTS
    }
    return out


@router.put("/personal-mailboxes/rules/vathub")
def vh_update_config(body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    reviewer = admin.get("username") or admin.get("email") or "admin"
    store = _vh_load(db)

    if body.get("target") is not None:
        target = (body.get("target") or "").strip().lower()
        if target not in _VH_TARGETS:
            raise HTTPException(400, "Destinație neaprobată. Permise: " + ", ".join(sorted(_VH_TARGETS)))
        store["target"] = target
    if body.get("enabled") is not None:
        store["enabled"] = bool(body["enabled"])
    if body.get("max_age_hours") is not None:
        try:
            hours = int(body["max_age_hours"])
        except (TypeError, ValueError):
            raise HTTPException(400, "max_age_hours trebuie să fie număr")
        if not 1 <= hours <= 720:
            raise HTTPException(400, "max_age_hours între 1 și 720")
        store["max_age_hours"] = hours

    _vh_save(db, store, reviewer)
    db.commit()
    logger.info("VATHUB config actualizat de %s: enabled=%s target=%s",
                reviewer, store.get("enabled"), store.get("target"))
    return {"ok": True, "enabled": store["enabled"], "target": store["target"]}


@router.post("/personal-mailboxes/rules/vathub/entries")
def vh_add_entry(body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    reviewer = admin.get("username") or admin.get("email") or "admin"
    lst = (body.get("list") or "").strip().lower()
    if lst not in _VH_LISTS:
        raise HTTPException(400, "Listă invalidă (domains/addresses)")
    key = _vh_norm(lst, body.get("value") or "")

    store = _vh_load(db)
    if key in store[lst]:
        raise HTTPException(409, f"'{key}' există deja în listă")
    store[lst][key] = {
        "muted": bool(body.get("muted", False)),
        "note": body.get("note"),
        "source": "manual",
        "by": reviewer,
        "at": _datetime.now(_timezone.utc).isoformat(),
    }
    _vh_save(db, store, reviewer)
    db.commit()
    return {"ok": True, "list": lst, "value": key}


@router.put("/personal-mailboxes/rules/vathub/entries")
def vh_edit_entry(body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Validare/invalidare (`muted`), notă, sau redenumire a unei intrări."""
    reviewer = admin.get("username") or admin.get("email") or "admin"
    lst = (body.get("list") or "").strip().lower()
    if lst not in _VH_LISTS:
        raise HTTPException(400, "Listă invalidă (domains/addresses)")
    key = _vh_norm(lst, body.get("value") or "")

    store = _vh_load(db)
    bucket = store[lst]
    if key not in bucket:
        raise HTTPException(404, "Intrare inexistentă")

    entry = dict(bucket[key])
    if body.get("muted") is not None:
        entry["muted"] = bool(body["muted"])
        entry["validated_by"] = reviewer
        entry["validated_at"] = _datetime.now(_timezone.utc).isoformat()
    if body.get("note") is not None:
        entry["note"] = body["note"]

    target_key = key
    if body.get("new_value"):
        nk = _vh_norm(lst, body["new_value"])
        if nk != key:
            if nk in bucket:
                raise HTTPException(409, f"'{nk}' există deja în listă")
            del bucket[key]
            target_key = nk
    bucket[target_key] = entry
    store[lst] = bucket
    _vh_save(db, store, reviewer)
    db.commit()
    return {"ok": True, "value": target_key, "muted": entry.get("muted", False)}


@router.delete("/personal-mailboxes/rules/vathub/entries")
def vh_delete_entry(
    list: str = _Query(...),
    value: str = _Query(...),
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    reviewer = admin.get("username") or admin.get("email") or "admin"
    if list not in _VH_LISTS:
        raise HTTPException(400, "Listă invalidă (domains/addresses)")
    key = _vh_norm(list, value)
    store = _vh_load(db)
    if key not in store[list]:
        raise HTTPException(404, "Intrare inexistentă")
    del store[list][key]
    _vh_save(db, store, reviewer)
    db.commit()
    return {"ok": True, "removed": key}


@router.get("/personal-mailboxes/{account_id}/vathub-log")
def vh_account_log(account_id: int, limit: int = 50,
                   db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Ultimele mailuri potrivite de regulile VATHUB, trimise sau nu."""
    uid = int(admin["id"])
    _get_account_or_404(db, account_id, uid)
    limit = max(1, min(int(limit or 50), 200))

    rows = db.execute(text("""
        SELECT id, imap_uid, from_address, subject, received_at,
               vathub_match, vathub_forwarded_at, vathub_attempts, vathub_error
        FROM personal_mails
        WHERE account_id = :aid AND vathub_match IS NOT NULL
        ORDER BY imap_uid DESC
        LIMIT :lim
    """), {"aid": account_id, "lim": limit}).fetchall()

    stats = db.execute(text("""
        SELECT count(*) FILTER (WHERE vathub_forwarded_at IS NOT NULL) AS sent,
               count(*) FILTER (WHERE vathub_forwarded_at IS NULL)     AS pending,
               count(*)                                                AS matched
        FROM personal_mails
        WHERE account_id = :aid AND vathub_match IS NOT NULL
    """), {"aid": account_id}).fetchone()

    return {
        "items": [dict(r._mapping) for r in rows],
        "stats": dict(stats._mapping) if stats else {"sent": 0, "pending": 0, "matched": 0},
    }


@router.post("/personal-mailboxes/{account_id}/smtp-test")
def vh_smtp_test(account_id: int, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Verifică credențialele SMTP fără să trimită vreun mail."""
    uid = int(admin["id"])
    row = _get_account_or_404(db, account_id, uid)
    if not row.get("smtp_host"):
        raise HTTPException(400, "Contul nu are server SMTP configurat")

    try:
        imap_password = decrypt_credentials(row["cred_enc"])["pass"]
    except Exception as e:
        raise HTTPException(500, f"Cannot decrypt credentials: {e}")

    smtp_password = None
    if row.get("smtp_cred_enc"):
        try:
            smtp_password = decrypt_credentials(row["smtp_cred_enc"])["pass"]
        except Exception as e:
            raise HTTPException(500, f"Cannot decrypt SMTP credentials: {e}")

    cfg = _psmtp.resolve_smtp(row, imap_password, smtp_password)
    ok, err = _psmtp.test_login(cfg)
    return {"ok": ok, "error": err, "host": cfg["host"], "port": cfg["port"], "user": cfg["user"]}


@router.post("/personal-mailboxes/{account_id}/vathub-run")
def vh_run_now(account_id: int, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Rulează manual potrivirea + trimiterea pentru un cont, fără să aștepți poll-ul."""
    uid = int(admin["id"])
    row = _get_account_or_404(db, account_id, uid)
    if not row.get("vathub_enabled"):
        raise HTTPException(400, "Redirectul VATHUB nu e activat pe această căsuță")

    try:
        imap_password = decrypt_credentials(row["cred_enc"])["pass"]
    except Exception as e:
        raise HTTPException(500, f"Cannot decrypt credentials: {e}")
    smtp_password = None
    if row.get("smtp_cred_enc"):
        try:
            smtp_password = decrypt_credentials(row["smtp_cred_enc"])["pass"]
        except Exception:
            smtp_password = None

    # Motorul lucrează cu psycopg2 (cursor + commit propriu), nu cu sesiunea ORM.
    from app.services.personal_mailbox_poller import _conn as _pg_conn
    conn = _pg_conn()
    try:
        cur = conn.cursor()
        result = _vf.process_account(row, imap_password, smtp_password, cur, conn)
        cur.close()
    finally:
        conn.close()
    return result


# ── Comutatoare per căsuță ────────────────────────────────────────────────────

@router.post("/personal-mailboxes/{account_id}/toggles")
def account_toggles(account_id: int, body: dict,
                    db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Pornește/oprește filtrarea spam-carantină și redirectul VATHUB, separat.

    Cele două sunt independente: o căsuță poate face redirect VATHUB cu filtrarea
    oprită (cazul căsuței care primește doar corespondență oficială) sau invers.
    """
    uid = int(admin["id"])
    row = _get_account_or_404(db, account_id, uid)

    filter_enabled = row["filter_enabled"]
    vathub_enabled = row["vathub_enabled"]

    if body.get("filter_enabled") is not None:
        filter_enabled = bool(body["filter_enabled"])
    if body.get("vathub_enabled") is not None:
        vathub_enabled = bool(body["vathub_enabled"])
        if vathub_enabled and not row.get("smtp_host"):
            raise HTTPException(400, "Redirectul VATHUB are nevoie de un server SMTP configurat")

    db.execute(text("""
        UPDATE personal_mailbox_accounts
        SET filter_enabled=:f, vathub_enabled=:v, updated_at=now()
        WHERE id=:id AND user_id=:uid
    """), {"f": filter_enabled, "v": vathub_enabled, "id": account_id, "uid": uid})

    # Oprirea filtrării închide și coada de scanare: mailurile deja ingerate dar
    # neanalizate ar fi altfel scanate de următorul poll, chiar cu filtrul OFF.
    skipped = 0
    if not filter_enabled:
        res = db.execute(text("""
            UPDATE personal_mails
            SET verdict='filter_off', folder_action='none'
            WHERE account_id=:aid AND verdict='pending'
        """), {"aid": account_id})
        skipped = res.rowcount or 0

    db.commit()
    logger.info("Căsuța %s: filtrare=%s vathub=%s (de %s), %s mailuri scoase din coadă",
                account_id, filter_enabled, vathub_enabled,
                admin.get("username") or "admin", skipped)
    return {"ok": True, "filter_enabled": filter_enabled,
            "vathub_enabled": vathub_enabled, "pending_skipped": skipped}
