"""Native O365 ingestion for Cargo360 — replaces parser-email-op's email fetch.

Pulls emails from Microsoft Graph (Inbox, delta sync), downloads + filters
attachments to disk, and inserts DIRECTLY into mailguard.emails / mailguard.attachments
using the SAME shape Cargo360 already consumes (graph_message_id = SMTP Message-ID,
status='pending'). This makes Cargo360 independent of parser-email-op.

Auth modes (env MS_AUTH_MODE):
  - 'app'       : client-credentials (Application Mail.Read + admin consent) — robust, unattended.
  - 'delegated' : refresh-token grant using a stored RT (settings key 'graph_refresh_token').

Cursor: settings key 'o365_delta_link' (Graph @odata.deltaLink). 410 -> reset (full resync, dedup protects).

Faithful to the parser contract (see audit 2026-06-19): per-message fields + attachment
filtering (content-type whitelist, >2KB, skip signature/logo names, keep inline images).
"""
import os
import re
import json
import time
import logging
import datetime as _dt

import httpx
from sqlalchemy import text

from app.database import SessionLocal

logger = logging.getLogger("mailguard.o365_ingest")

GRAPH = "https://graph.microsoft.com/v1.0"
SELECT_FIELDS = ("id,internetMessageId,internetMessageHeaders,subject,from,sender,"
                 "toRecipients,ccRecipients,conversationId,receivedDateTime,hasAttachments,"
                 "body,bodyPreview")

# attachment filtering — mirrors parser isRealAttachment()
_ALLOWED_CT = {
    # Images
    "image/jpeg", "image/png", "image/gif", "image/bmp", "image/webp", "image/tiff",
    "image/jpg", "image/pjpeg", "image/x-png",                                     # variante nestandard
    # HEIC/HEIF (poze iPhone) — convertite la JPEG la salvare, vezi heic_convert.py.
    # Lipseau din listă: atașamentul era aruncat tăcut, deci documentul nu ajungea nici în
    # Cargo360, nici în CTS (2026-09-18).
    "image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence",
    "image/x-heic", "image/x-heif",
    # PDF
    "application/pdf", "application/x-pdf",
    # Microsoft Office (legacy + OOXML)
    "application/msword",                                                           # .doc
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",     # .docx
    "application/vnd.ms-excel",                                                    # .xls
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",           # .xlsx
    "application/vnd.ms-powerpoint",                                               # .ppt
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",   # .pptx
    "application/rtf",                                                             # .rtf
    # Archives
    "application/zip", "application/x-zip-compressed", "application/x-zip",
    "application/x-rar-compressed", "application/x-7z-compressed",
    # Text/data
    "text/plain", "text/csv",
    # Generic binary (last resort — size+name filters apply)
    "application/octet-stream",
}
# Plasă pe EXTENSIE: Graph ia content-type-ul din ce a declarat clientul de mail al
# expeditorului, iar acolo apar valori exotice (ex. `image/x-citrix-jpeg`) pentru fișiere
# perfect normale. Un content-type necunoscut nu mai aruncă un fișier cu extensie cunoscută.
_ALLOWED_EXT = (
    ".jpg", ".jpeg", ".jpe", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff",
    ".heic", ".heif", ".hif",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".rtf",
    ".zip", ".rar", ".7z", ".txt", ".csv",
)
_SKIP_NAME = ("pixel", "logo", "signature", "footer", "header",
              "facebook", "twitter", "linkedin", "instagram")
_MIN_SIZE = 2048


# ───────────────────────── config ─────────────────────────
def _cfg():
    return {
        "tenant": os.getenv("MS_TENANT_ID", "").strip(),
        "client_id": os.getenv("MS_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("MS_CLIENT_SECRET", "").strip(),
        "user": os.getenv("MS_USER_EMAIL", "").strip(),
        "mode": (os.getenv("MS_AUTH_MODE", "app").strip().lower() or "app"),
        "attach_root": os.getenv("ATTACH_HOST_PREFIX", "/home/mail-data/attachments").rstrip("/"),
        "page_limit": int(os.getenv("O365_PAGE_LIMIT", "50")),
    }


def is_configured():
    c = _cfg()
    if not (c["tenant"] and c["client_id"]):
        return False
    if c["mode"] == "app":
        return bool(c["client_secret"] and c["user"])
    return True  # delegated: RT checked at call time


# ───────────────────────── settings (cursor / RT) ─────────────────────────
def _setting_get(k):
    db = SessionLocal()
    try:
        r = db.execute(text("SELECT value FROM settings WHERE key=:k"), {"k": k}).fetchone()
        if not r or r[0] is None:
            return None
        v = r[0]
        if isinstance(v, str):
            return v.strip('"')
        return v
    except Exception:
        return None
    finally:
        db.close()


def _setting_set(k, v):
    db = SessionLocal()
    try:
        db.execute(text(
            "INSERT INTO settings(key, value) VALUES(:k, to_jsonb(CAST(:v AS text))) "
            "ON CONFLICT(key) DO UPDATE SET value=to_jsonb(CAST(:v AS text))"), {"k": k, "v": v})
        db.commit()
    finally:
        db.close()


# ───────────────────────── auth ─────────────────────────
def _token_app(c):
    r = httpx.post(
        f"https://login.microsoftonline.com/{c['tenant']}/oauth2/v2.0/token",
        data={"client_id": c["client_id"], "client_secret": c["client_secret"],
              "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"},
        timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def _token_delegated(c):
    rt = _setting_get("graph_refresh_token")
    if not rt:
        raise RuntimeError("delegated mode: settings.graph_refresh_token lipsește (importă RT-ul mai întâi)")
    data = {"client_id": c["client_id"], "refresh_token": rt, "grant_type": "refresh_token",
            "scope": "https://graph.microsoft.com/Mail.Read offline_access"}
    if c["client_secret"]:
        data["client_secret"] = c["client_secret"]
    r = httpx.post(f"https://login.microsoftonline.com/{c['tenant']}/oauth2/v2.0/token", data=data, timeout=30)
    r.raise_for_status()
    j = r.json()
    if j.get("refresh_token"):
        _setting_set("graph_refresh_token", j["refresh_token"])  # rotate-and-persist
    return j["access_token"]


def _access_token(c):
    return _token_app(c) if c["mode"] == "app" else _token_delegated(c)


def _base(c):
    # app-only must target a concrete mailbox; delegated acts as the signed-in user
    return f"{GRAPH}/users/{c['user']}" if c["mode"] == "app" else f"{GRAPH}/me"


# ───────────────────────── helpers ─────────────────────────
def _msgid(m):
    """SMTP Message-ID (same dedup key as parser): header -> internetMessageId -> graph id."""
    for h in (m.get("internetMessageHeaders") or []):
        if (h.get("name") or "").lower() == "message-id":
            return h.get("value")
    return m.get("internetMessageId") or m.get("id")


def _addr(node):
    ea = (node or {}).get("emailAddress") or {}
    return ea.get("address"), ea.get("name")


def _auth_flags(m):
    flags = []
    for h in (m.get("internetMessageHeaders") or []):
        n = (h.get("name") or "").lower()
        if n in ("authentication-results", "received-spf", "dkim-signature", "arc-authentication-results"):
            flags.append(f"{h.get('name')}: {(h.get('value') or '')[:200]}")
    return flags


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]+")


def _html_to_text(s):
    if not s:
        return None
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</p>", "\n\n", s)
    s = _TAG.sub("", s)
    import html as _h
    s = _h.unescape(s)
    s = _WS.sub(" ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip() or None


def _is_real_attachment(a):
    if a.get("@odata.type", "").endswith("itemAttachment"):
        return False
    name = (a.get("name") or "").lower()
    ct = (a.get("contentType") or "").lower().split(";")[0].strip()
    inline = bool(a.get("isInline"))  # contentId poate exista si pe atasamente non-inline in Graph
    if inline and a.get("contentId") and ct.startswith("image/"):
        pass  # keep inline images
    elif ct not in _ALLOWED_CT and not name.endswith(_ALLOWED_EXT):
        # Logat, nu tăcut: altfel un format nou (cum a fost HEIC) se pierde fără urmă.
        logger.info("o365: atasament ignorat (tip nepermis) name=%r ct=%r size=%s",
                    a.get("name"), a.get("contentType"), a.get("size"))
        return False
    if any(p in name for p in _SKIP_NAME):
        return False
    if int(a.get("size") or 0) < _MIN_SIZE and not (inline and ct.startswith("image/")):
        return False
    return True


# ───────────────────────── Graph fetch ─────────────────────────
def _delta_fetch(c, tok):
    """Return (messages[], new_delta_link). Uses stored deltaLink if present."""
    headers = {"Authorization": "Bearer " + tok}
    link = _setting_get("o365_delta_link")
    if link:
        url = link
    else:
        url = f"{_base(c)}/mailFolders/Inbox/messages/delta?$select={SELECT_FIELDS}&$top={c['page_limit']}"
    msgs, delta_link = [], None
    while url:
        r = httpx.get(url, headers=headers, timeout=60)
        if r.status_code == 410:  # delta expired -> reset, full resync next run
            _setting_set("o365_delta_link", "")
            logger.warning("o365 delta 410 (expirat) — reset, full resync data viitoare")
            return [], None
        r.raise_for_status()
        j = r.json()
        msgs.extend(j.get("value") or [])
        delta_link = j.get("@odata.deltaLink") or delta_link
        url = j.get("@odata.nextLink")
        if len(msgs) >= 500:  # safety cap per run
            break
    return msgs, delta_link


def raw_message_fetcher():
    """Returnează o funcție `graph_id -> bytes|None` care aduce MIME-ul BRUT din Graph.

    Folosit de redirectul VATHUB: mesajul autorității pleacă spre vathub@cargotrack.ro
    INTACT (atașamente, headere, semnături), nu reconstruit din `body_html`.
    Tokenul se ia O SINGURĂ DATĂ, la construirea funcției — un lot de forwarduri ar
    face altfel un login OAuth per mail.
    Returnează None dacă O365 nu e configurat (instalările pe parser-email-op);
    apelantul cade atunci pe reconstrucția din DB.
    """
    c = _cfg()
    if not is_configured():
        return None
    try:
        tok = _access_token(c)
    except Exception as e:
        logger.warning("o365 raw fetcher: auth fail: %s", str(e)[:200])
        return None

    def _fetch(graph_id):
        if not graph_id:
            return None
        try:
            r = httpx.get(f"{_base(c)}/messages/{graph_id}/$value",
                          headers={"Authorization": "Bearer " + tok}, timeout=60)
        except Exception as e:
            logger.warning("o365 raw fetch %s: %s", graph_id, str(e)[:160])
            return None
        if r.status_code != 200:
            logger.warning("o365 raw fetch %s: HTTP %s", graph_id, r.status_code)
            return None
        return r.content

    return _fetch


def _fetch_attachments(c, tok, gid):
    r = httpx.get(f"{_base(c)}/messages/{gid}/attachments", headers={"Authorization": "Bearer " + tok}, timeout=60)
    if r.status_code != 200:
        return []
    return r.json().get("value") or []


# ───────────────────────── DB writes (same contract as parser_email_op_reader) ─────────────────────────
def _insert_email(db, m):
    gid_smtp = _msgid(m)
    if not gid_smtp:
        return None
    from_addr, from_name = _addr(m.get("from") or m.get("sender"))
    tos = [a for a in ((_addr(x)[0]) for x in (m.get("toRecipients") or [])) if a]
    ccs = [a for a in ((_addr(x)[0]) for x in (m.get("ccRecipients") or [])) if a]
    body = m.get("body") or {}
    ctype = (body.get("contentType") or "").lower()
    body_html = body.get("content") if ctype == "html" else None
    body_text = _html_to_text(body.get("content")) if ctype == "html" else (body.get("content") or None)
    headers = {"authentication_flags": _auth_flags(m), "message_id": gid_smtp}
    # conversationId = firul Graph. Îl persistăm ca punte email↔client pe emailurile TRIMISE:
    # răspunsul nostru moștenește clientul din mesajul primit din același fir. Coloana exista
    # dar rămăsese NULL pe toate rândurile, iar raw_graph_payload nu păstra nimic recuperabil.
    raw = {"source": "o365-native", "graph_id": m.get("id"),
           "conversationId": m.get("conversationId"),
           "internetMessageId": m.get("internetMessageId")}
    row = db.execute(text("""
        INSERT INTO emails(graph_message_id, subject, from_address, from_name, to_addresses,
            cc_addresses, conversation_id, internet_message_id,
            received_at, body_text, body_html, raw_graph_payload, email_headers, status, fetched_at)
        VALUES(:gid, :subj, :fa, :fn, CAST(:toaddrs AS jsonb),
            CAST(:ccaddrs AS jsonb), :conv, :imid,
            :rcv, :bt, :bh, CAST(:raw AS jsonb), CAST(:hdr AS jsonb), 'pending', NOW())
        ON CONFLICT (graph_message_id) DO NOTHING
        RETURNING id"""), {
        "gid": gid_smtp, "subj": m.get("subject"), "fa": from_addr, "fn": from_name,
        "toaddrs": json.dumps(tos), "ccaddrs": json.dumps(ccs),
        "conv": m.get("conversationId"), "imid": m.get("internetMessageId"),
        "rcv": m.get("receivedDateTime"),
        "bt": body_text, "bh": body_html, "raw": json.dumps(raw), "hdr": json.dumps(headers),
    }).fetchone()
    return row[0] if row else None  # None => already existed (dedup)


def _save_attachments(c, tok, db, mg_email_id, m):
    # Graph raporteaza hasAttachments=FALSE pentru emailuri cu DOAR imagini inline (cid:).
    # Cerem /attachments si cand body-ul referentiaza cid:, altfel imaginea inline se pierde.
    body_content = ((m.get("body") or {}).get("content")) or ""
    if not m.get("hasAttachments") and "cid:" not in body_content.lower():
        return 0
    return _store_attachments(c, db, mg_email_id, _fetch_attachments(c, tok, m.get("id")))


def _store_attachments(c, db, mg_email_id, atts, skip_ids=()):
    """Filtrează + scrie pe disc + inserează în `attachments`. Întoarce câte s-au salvat.
    `skip_ids` = graph_attachment_id deja prezente (refacerea nu dublează rânduri)."""
    import base64
    from app.services import heic_convert
    saved = 0
    base = os.path.join(c["attach_root"], "native", str(mg_email_id))
    for a in atts:
        try:
            if a.get("id") and a.get("id") in skip_ids:
                continue
            if not _is_real_attachment(a):
                continue
            content_b64 = a.get("contentBytes")
            if not content_b64:
                logger.warning("o365: atasament fara contentBytes eid=%s name=%r size=%s",
                               mg_email_id, a.get("name"), a.get("size"))
                continue
            data = base64.b64decode(content_b64)
            name, ctype = a.get("name"), a.get("contentType")
            os.makedirs(base, exist_ok=True)
            if heic_convert.is_heif(name, ctype, data):
                jpg = heic_convert.to_jpeg(data)
                if jpg:
                    # Originalul rămâne pe disc lângă JPEG (audit), dar în DB intră doar JPEG-ul:
                    # tot lanțul de după (OCR, vision, previzualizare, CTS) știe JPEG, nu HEIC.
                    with open(os.path.join(base, re.sub(r"[/\\]", "_", name or "original.heic")), "wb") as fh:
                        fh.write(data)
                    data, name, ctype = jpg, heic_convert.jpeg_name(name), "image/jpeg"
                else:
                    logger.warning("o365: HEIC neconvertit eid=%s name=%r — salvat ca atare",
                                   mg_email_id, name)
            fname = name or f"attachment-{int(time.time()*1000)}"
            fname = re.sub(r"[/\\]", "_", fname)
            fpath = os.path.join(base, fname)
            with open(fpath, "wb") as fh:
                fh.write(data)
            cid_raw = a.get("contentId")
            cid_norm = (cid_raw or "").strip().lstrip("<").rstrip(">").strip() or None
            inl = bool(a.get("isInline"))  # cid_raw se salveaza in content_id, dar nu inseamna inline
            db.execute(text("""
                INSERT INTO attachments(email_id, graph_attachment_id, name, content_type, storage_path, is_suspicious, content_id, is_inline)
                VALUES(:eid, :gid, :name, :ct, :sp, FALSE, :cid, :inl)"""), {
                "eid": mg_email_id, "gid": a.get("id"), "name": name,
                "ct": ctype, "sp": fpath, "cid": cid_norm, "inl": inl})
            saved += 1
        except Exception as e:
            logger.warning("attach save failed eid=%s: %s", mg_email_id, str(e)[:160])
    return saved


def refetch_attachments(days=14, email_ids=None, apply=False):
    """Readuce din Graph atașamentele care LIPSESC pe emailuri deja ingerate (ex. HEIC aruncat
    de filtrul vechi). Emailul există în DB (dedup pe Message-ID), deci re-sync-ul normal nu-l
    mai atinge — de aici calea separată. `apply=False` = doar raportează ce ar adăuga.

    Lista din Graph se cere FĂRĂ contentBytes (ieftin); conținutul se descarcă doar pentru
    atașamentele lipsă, și doar cu `apply=True`."""
    c = _cfg()
    if not is_configured():
        return {"ok": False, "error": "o365 neconfigurat"}
    tok = _access_token(c)
    headers = {"Authorization": "Bearer " + tok}
    db = SessionLocal()
    found, saved, errors = [], 0, 0
    try:
        if email_ids:
            rows = db.execute(text(
                "SELECT id, raw_graph_payload->>'graph_id' FROM emails WHERE id = ANY(:ids)"),
                {"ids": list(email_ids)}).fetchall()
        else:
            rows = db.execute(text(
                "SELECT id, raw_graph_payload->>'graph_id' FROM emails "
                "WHERE raw_graph_payload->>'source' = 'o365-native' "
                "AND received_at >= NOW() - make_interval(days => :d) ORDER BY id"),
                {"d": int(days)}).fetchall()
        for eid, gid in rows:
            if not gid:
                continue
            try:
                r = httpx.get(f"{_base(c)}/messages/{gid}/attachments",
                              params={"$select": "id,name,contentType,size,isInline"},
                              headers=headers, timeout=60)
                if r.status_code != 200:
                    continue
                have = {x[0] for x in db.execute(text(
                    "SELECT graph_attachment_id FROM attachments WHERE email_id=:e"), {"e": eid})}
                # `name` al unui HEIC convertit s-a schimbat, dar graph_attachment_id rămâne → dedup corect
                missing = [a for a in (r.json().get("value") or [])
                           if a.get("id") not in have and _is_real_attachment(a)]
                if not missing:
                    continue
                found.append({"email_id": eid, "attachments": [
                    {"name": a.get("name"), "contentType": a.get("contentType"), "size": a.get("size")}
                    for a in missing]})
                if apply:
                    full = [x for x in _fetch_attachments(c, tok, gid)
                            if x.get("id") in {a.get("id") for a in missing}]
                    n = _store_attachments(c, db, eid, full, skip_ids=have)
                    if n:
                        db.execute(text("UPDATE emails SET has_attachments=TRUE WHERE id=:id"), {"id": eid})
                    db.commit()
                    saved += n
            except Exception as e:
                db.rollback()
                errors += 1
                logger.warning("refetch eid=%s: %s", eid, str(e)[:200])
    finally:
        db.close()
    return {"ok": True, "scanned": len(rows), "emails_with_missing": len(found),
            "saved": saved, "errors": errors, "apply": apply, "details": found}


# ───────────────────────── entrypoint ─────────────────────────
def sync_run(limit=None):
    """Pull new emails from O365 -> mailguard.emails (+attachments). Returns summary dict.
    Drop-in replacement for parser_email_op_reader.sync_run when MAILGUARD_NATIVE_INGEST=on."""
    c = _cfg()
    if not is_configured():
        return {"ok": False, "error": "o365 neconfigurat (MS_TENANT_ID/CLIENT_ID/SECRET/USER)"}
    t0 = time.time()
    try:
        tok = _access_token(c)
    except Exception as e:
        logger.warning("o365 auth fail: %s", str(e)[:200])
        return {"ok": False, "error": f"auth: {str(e)[:200]}"}
    try:
        msgs, delta_link = _delta_fetch(c, tok)
    except Exception as e:
        logger.warning("o365 delta fail: %s", str(e)[:200])
        return {"ok": False, "error": f"delta: {str(e)[:200]}"}

    inserted, attach = 0, 0
    db = SessionLocal()
    try:
        for m in msgs:
            if m.get("@removed"):  # deleted in mailbox — ignore (we don't delete)
                continue
            try:
                eid = _insert_email(db, m)
                if eid:
                    inserted += 1
                    db.commit()
                    attach += _save_attachments(c, tok, db, eid, m)
                    if attach:
                        db.execute(text("UPDATE emails SET has_attachments=TRUE WHERE id=:id"), {"id": eid})
                    db.commit()
            except Exception as e:
                db.rollback()
                logger.warning("ingest one fail: %s", str(e)[:200])
        if delta_link:
            _setting_set("o365_delta_link", delta_link)
    finally:
        db.close()
    out = {"ok": True, "fetched": len(msgs), "inserted": inserted, "attachments": attach,
           "ms": int((time.time() - t0) * 1000), "mode": c["mode"]}
    logger.info("o365 sync: %s", out)
    return out


# ───────────────────────── reimport one-shot (backfill pe data) ─────────────────────────
def backfill_from_date(from_date, to_date=None):
    """Reimport one-shot din O365: emailuri din Inbox cu receivedDateTime >= from_date [<= to_date].
    Folosit de butonul 'Reset & reimport' (echivalentul O365 al parser sync_run(from_date=...)).
    Non-delta (query cu $filter); NU atinge cursorul delta. Returneaza acelasi shape ca sync_run."""
    c = _cfg()
    if not is_configured():
        return {"ok": False, "error": "o365 neconfigurat (MS_TENANT_ID/CLIENT_ID/SECRET/USER)"}
    t0 = time.time()
    try:
        tok = _access_token(c)
    except Exception as e:
        logger.warning("o365 auth fail: %s", str(e)[:200])
        return {"ok": False, "error": f"auth: {str(e)[:200]}"}
    headers = {"Authorization": "Bearer " + tok}
    url = f"{_base(c)}/mailFolders/Inbox/messages"
    odata_filter = f"receivedDateTime ge {from_date}T00:00:00Z"
    if to_date:
        odata_filter += f" and receivedDateTime le {to_date}T23:59:59Z"
    params = {
        "$select": SELECT_FIELDS,
        "$top": str(c["page_limit"]),
        "$filter": odata_filter,
        "$orderby": "receivedDateTime desc",
    }
    msgs = []
    try:
        while url:
            r = httpx.get(url, headers=headers, params=params, timeout=60)
            r.raise_for_status()
            j = r.json()
            msgs.extend(j.get("value") or [])
            url = j.get("@odata.nextLink")
            params = None  # nextLink contine deja toti parametrii
            if len(msgs) >= 5000:  # garda anti-bucla
                logger.warning("o365 backfill cap 5000 atins (from_date=%s)", from_date)
                break
    except Exception as e:
        logger.warning("o365 backfill fetch fail: %s", str(e)[:200])
        return {"ok": False, "error": f"fetch: {str(e)[:200]}"}

    inserted, attach = 0, 0
    db = SessionLocal()
    try:
        for m in msgs:
            if m.get("@removed"):
                continue
            try:
                eid = _insert_email(db, m)
                if eid:
                    inserted += 1
                    db.commit()
                    n = _save_attachments(c, tok, db, eid, m)
                    if n:
                        attach += n
                        db.execute(text("UPDATE emails SET has_attachments=TRUE WHERE id=:id"), {"id": eid})
                    db.commit()
            except Exception as e:
                db.rollback()
                logger.warning("backfill ingest one fail: %s", str(e)[:200])
    finally:
        db.close()
    out = {"ok": True, "fetched": len(msgs), "inserted": inserted, "attachments": attach,
           "ms": int((time.time() - t0) * 1000), "mode": c["mode"], "from_date": str(from_date)}
    logger.info("o365 backfill: %s", out)
    return out
