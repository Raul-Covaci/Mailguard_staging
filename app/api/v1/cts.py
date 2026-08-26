"""CTS feed — faza 1: backoffice-ul CTS preia emailurile „bune de trimis" din Cargo360.

CTS citea pana acum mail-urile direct din Microsoft Graph. Aici expunem un feed
identic STRUCTURAL cu Graph (mesaj reconstruit din coloanele noastre + atasamente
cu contentBytes), ca CTS sa comute doar sursa, nu parsing-ul.

Eligibilitate faza 1: emailuri clean (ready_for_cts) + rapoarte automate (auto_report/auto_closed,
trimise CA ATARE in faza 1). EXCLUSE: spam, carantina (quarantined_strict), NDR si starile in-proces.

Flux (two-phase, idempotent):
  1) GET  /api/v1/cts/get_emails     -> emailuri eligibile, ne-trimise (sent_to_cts_at IS NULL),
     FIFO dupa received_at. NU marcheaza nimic.
  2) POST /api/v1/cts/update_emails  -> CTS confirma ce a salvat. Marcam sent_to_cts_at DOAR
     pe id-urile confirmate (toate sau partial). Esecurile -> cts_send_error/attempts.
     Pentru auto_report pastram queue_status='auto_closed' (cargo360 continua procesarea automata).

Auth: header X-CTS-Token comparat cu settings.cts_feed_api_key (hmac.compare_digest).
Monitorizare: fiecare apel logat in cts_api_log (doar id-uri + counts, FARA payload-uri).
"""
import os
import json
import base64
import hmac
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.config import get_settings
from app.api.v1.auth import get_current_admin
# Traducerea caii atasamentului (container -> host) e single-source in emails.py.
from app.api.v1.emails import _host_path
# Reguli deterministe „mailuri automate -> marcheaza SOLVED in CTS" (feed: campul mark_as_solved).
from app.services import cts_auto_solved
from app.services.doc_stats import STATS_SINCE, clamp_from_date
# Etichete departament (slug -> label) pentru campurile de clasificare din feed (faza 2).
try:
    from app.services.department_classifier import DEPT_LABELS as _DEPT_LABELS
except Exception:  # feed-ul NU trebuie sa pice daca serviciul lipseste
    _DEPT_LABELS = {}

logger = logging.getLogger("mailguard.cts")
router = APIRouter()
# ── CTS send flags — switch-uri per-câmp (PS-2026-0128) ────────────────────────
_CTS_FLAGS_KEY = "cts_send_flags"
_CTS_FLAGS_DEFAULT = {
    "send_categorie":     True,
    "send_departament":   True,
    "send_prioritate":    True,
    "send_documente":     True,
    "auto_rotate_images": False,
}

def _get_cts_send_flags(db) -> dict:
    """Citește switch-urile ON/OFF per-câmp din tabelul settings. Fallback la toate ON."""
    try:
        row = db.execute(text("SELECT value FROM settings WHERE key=:k"),
                         {"k": _CTS_FLAGS_KEY}).fetchone()
        flags = dict(_CTS_FLAGS_DEFAULT)
        if row and row._mapping and row._mapping.get("value"):
            flags.update(row._mapping["value"])
        return flags
    except Exception:
        return dict(_CTS_FLAGS_DEFAULT)




def _is_production() -> bool:
    """True daca aplicatia ruleaza pe PRODUCTIE (app_env). Pe staging -> False.
    Controleaza: campurile de clasificare din get_emails (NULL pe prod, valori pe
    staging) si livrarea detaliilor de documente (skip pe prod)."""
    return (get_settings().app_env or "production").strip().lower() == "production"

DEFAULT_LIMIT = 15
MAX_LIMIT = 500

# Eligibilitate faza 1: emailuri „bune de trimis" + ne-trimise încă (sent_to_cts_at IS NULL).
#   - clean / ready_for_cts        -> corespondenta normala procesata
#   - auto_report / auto_closed    -> rapoarte automate (faza 1: trimise CA ATARE; faza 2: cu flag „solved" + date extrase)
# EXCLUSE: stopped_spam, stopped_quarantine, stopped_ndr (oprite) si starile in-proces (queued_general/intent_check/...).
# queue_status e sursa pre-existentă folosită de filtrul/badge-ul din lista de emailuri;
# astfel feed-ul, filtrul „Status CTS" și badge-ul rămân consistente.
# Gardă (2026-06-22): un email pe calea CLEAN încadrat 'necunoscut' NU pleacă spre CTS. Clasificatorul
# face deja fallback 'necunoscut' → 'informatie', deci asta acoperă doar date vechi / setări manuale.
# ai_category NULL (clasificare oprită/skip) rămâne eligibil — gardăm DOAR valoarea explicită 'necunoscut'.
_ELIGIBLE_CLEAN = "status='clean' AND queue_status='ready_for_cts' AND COALESCE(ai_category,'') <> 'necunoscut'"
_ELIGIBLE_AUTO = "status='auto_report' AND queue_status='auto_closed'"
_ELIGIBLE = f"(({_ELIGIBLE_CLEAN}) OR ({_ELIGIBLE_AUTO})) AND sent_to_cts_at IS NULL"

# Coloanele citite pentru reconstructia mesajului Graph.
_EMAIL_COLS = """
    id, graph_message_id, internet_message_id, conversation_id, subject,
    from_address, from_name, to_addresses, cc_addresses, bcc_addresses, reply_to,
    received_at, body_html, body_text, has_attachments, importance, is_read, email_headers,
    ai_category, ai_department, ai_priority
"""


# ---------------------------------------------------------------- auth
def require_cts_feed_key(x_cts_token: Optional[str] = Header(None)):
    """Valideaza tokenul CTS din settings (config/.env)."""
    settings = get_settings()
    expected = settings.cts_feed_api_key or ""
    if not expected:
        raise HTTPException(503, "CTS feed nu este configurat (lipseste cheia pe server)")
    if not x_cts_token or not hmac.compare_digest(str(x_cts_token), str(expected)):
        raise HTTPException(401, "Token CTS invalid sau lipsa (header X-CTS-Token)")
    return True


# ---------------------------------------------------------------- helpers
def _client_ip(request: Request) -> Optional[str]:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()[:64]
    return (request.client.host[:64] if request.client else None)


def _recip(val):
    """jsonb (lista de string-uri SAU de dict-uri) -> Graph recipient[]"""
    out = []
    for e in (val or []):
        if isinstance(e, str):
            if e.strip():
                out.append({"emailAddress": {"address": e.strip()}})
        elif isinstance(e, dict):
            ea = e.get("emailAddress") if isinstance(e.get("emailAddress"), dict) else {}
            addr = e.get("address") or e.get("email") or ea.get("address")
            name = e.get("name") or ea.get("name")
            if addr:
                obj = {"address": addr}
                if name:
                    obj["name"] = name
                out.append({"emailAddress": obj})
    return out


def _build_attachment(a: dict, auto_rotate: bool = False):
    """Construieste un microsoft.graph.fileAttachment cu contentBytes din fisierul de pe disc.
    Intoarce None daca fisierul lipseste/necitibil (emailul va fi exclus din feed).
    Daca auto_rotate=True si atasamentul e imagine, apeleaza AI pentru detectare orientare."""
    path = _host_path(a.get("storage_path"))
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception:
        logger.warning("cts: nu pot citi atasamentul %s (%s)", a.get("id"), path)
        return None
    ct = (a.get("content_type") or "application/octet-stream").lower().split(";")[0].strip()
    if auto_rotate and ct.startswith("image/"):
        try:
            from app.services.image_orient import maybe_rotate
            raw = maybe_rotate(raw, ct)
        except Exception as e:
            logger.warning("cts: image_orient skip att=%s: %s", a.get("id"), e)
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "id": a.get("graph_attachment_id") or str(a.get("id")),
        # ID intern al atașamentului. `id_mailguard` e numele ISTORIC, pe care CTS îl citește.
        # Redenumirea în `id_cargo360` (31.07.2026, rebranding) a rupt CTS în producție: câmpul
        # exista, dar sub alt nume, deci ajungea None fără nicio eroare vizibilă.
        # Trimitem AMBELE, cu aceeași valoare: `id_mailguard` e contractul principal, iar
        # `id_cargo360` rămâne alias ca să nu rupem un consumator care s-a adaptat între timp.
        # Nu scoate niciunul fără să verifici întâi ce citește fiecare consumator.
        "id_mailguard": a.get("id"),
        "id_cargo360": a.get("id"),
        "name": a.get("name"),
        "contentType": ct,
        "size": len(raw),
        "isInline": bool(a.get("is_inline")),
        "contentId": a.get("content_id"),  # cid-ul pt. <img src="cid:..."> in body
        "contentBytes": base64.b64encode(raw).decode("ascii"),
    }


_CID_REF_RE = re.compile(r'cid:([^"\'>\s\)]+)', re.IGNORECASE)


def _inline_cid_feed(html, atts):
    """Rescrie referintele cid: din body in data: URI base64, folosind atasamentele
    deja construite pentru feed (contentBytes e in memorie). Astfel CTS vede pozele
    inline DIRECT in body, fara sa rezolve cid. Potrivire dupa contentId; fallback
    pozitional pe imaginile inline, in ordinea documentului (cid generic fara contentId)."""
    if not html or "cid:" not in html.lower():
        return html
    by_cid = {}
    pool = []
    for a in atts:
        cb = a.get("contentBytes")
        if not cb:
            continue
        ct = a.get("contentType") or "application/octet-stream"
        duri = "data:%s;base64,%s" % (ct, cb)
        cid = (a.get("contentId") or "").strip().lstrip("<").rstrip(">").strip().lower()
        if cid:
            by_cid[cid] = duri
        if a.get("isInline") or ct.lower().startswith("image/"):
            pool.append(duri)
    if not by_cid and not pool:
        return html
    state = {"i": 0}

    def repl(m):
        raw = m.group(1).strip().lstrip("<").rstrip(">").strip().lower()
        d = by_cid.get(raw)
        if d is None:
            while state["i"] < len(pool):
                d = pool[state["i"]]
                state["i"] += 1
                break
        return d if d else m.group(0)

    return _CID_REF_RE.sub(repl, html)


def _to_graph_message(r: dict, atts: list, send_flags: dict | None = None,
                      mark_as_solved: bool = False) -> dict:
    """emails row -> microsoft.graph.message (structural identic Graph)."""
    body_html = r.get("body_html")
    body_text = r.get("body_text")
    hdrs = r.get("email_headers") if isinstance(r.get("email_headers"), dict) else {}
    internet_mid = (hdrs or {}).get("message_id") or r.get("internet_message_id")
    recv = r.get("received_at")
    recv_iso = recv.isoformat() if recv else None
    frm = {"emailAddress": {}}
    if r.get("from_address"):
        frm["emailAddress"]["address"] = r["from_address"]
    if r.get("from_name"):
        frm["emailAddress"]["name"] = r["from_name"]
    msg = {
        "@odata.type": "#microsoft.graph.message",
        "id": r["id"],                              # id intern Cargo360 (#8912) — cheia pentru update_emails
        "graphMessageId": r.get("graph_message_id"),
        "internetMessageId": internet_mid,
        "conversationId": r.get("conversation_id"),
        "createdDateTime": recv_iso,
        "lastModifiedDateTime": recv_iso,
        "receivedDateTime": recv_iso,
        "sentDateTime": recv_iso,
        "subject": r.get("subject"),
        "bodyPreview": (body_text or "")[:255],
        "importance": (r.get("importance") or "normal"),
        "hasAttachments": bool(r.get("has_attachments")),
        "isRead": bool(r.get("is_read")),
        "isDraft": False,
        "from": frm,
        "sender": frm,
        "toRecipients": _recip(r.get("to_addresses")),
        "ccRecipients": _recip(r.get("cc_addresses")),
        "bccRecipients": _recip(r.get("bcc_addresses")),
        "replyTo": _recip(r.get("reply_to")),
        "body": {
            "contentType": "html" if body_html else "text",
            "content": (_inline_cid_feed(body_html, atts) if body_html else (body_text or "")),
        },
        "attachments": atts,
    }
    # Campurile de clasificare sunt mereu PREZENTE in payload.
    # send_flags (dict gol = prod/master-off) controleaza individual ce se trimite.
    sf = send_flags or {}
    # categorie
    msg["categorie"] = r.get("ai_category") if sf.get("send_categorie") else None
    # departament + label
    dep = r.get("ai_department") if sf.get("send_departament") else None
    msg["departament"] = dep
    msg["departamentLabel"] = (_DEPT_LABELS.get(dep) if dep else None)
    # prioritate (2..5; vezi priority_classifier). Campul 'urgent' a fost eliminat (nefolosit).
    if sf.get("send_prioritate"):
        pri = r.get("ai_priority")
        try:
            msg["prioritate"] = int(pri) if pri not in (None, "") else None
        except (TypeError, ValueError):
            msg["prioritate"] = None
    else:
        msg["prioritate"] = None
    # Mailuri automate: semnal pentru CTS sa marcheze emailul direct SOLVED la ingestie.
    # Mereu prezent (default False); calculat din reguli expeditor+subiect in get_emails.
    msg["mark_as_solved"] = bool(mark_as_solved)
    return msg


def _log(db: Session, action: str, ids: list, requested: int, success: int,
         total: int, http_status: int, remote_ip, summary: str, response_meta: dict):
    try:
        db.execute(text("""
            INSERT INTO cts_api_log(action, email_ids, requested, success, total,
                                    http_status, remote_ip, summary, response_meta)
            VALUES (:a, CAST(:ids AS jsonb), :req, :suc, :tot, :hs, :ip, :sm, CAST(:meta AS jsonb))
        """), {"a": action, "ids": json.dumps(ids), "req": requested, "suc": success,
               "tot": total, "hs": http_status, "ip": remote_ip, "sm": summary,
               "meta": json.dumps(response_meta or {})})
        db.commit()
    except Exception:
        logger.exception("cts_api_log insert failed")
        db.rollback()


# ---------------------------------------------------------------- feed (get_emails)
@router.get("/cts/get_emails")
def cts_get_emails(request: Request,
             limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
             from_date: Optional[str] = Query(None, description="Doar emailuri cu received_at >= aceasta data (YYYY-MM-DD sau ISO 8601)"),
             id_email: Optional[int] = Query(None, description="Daca e setat: IGNORA eligibilitatea/sent_to_cts si intoarce EXCLUSIV acel email dupa id intern Cargo360 (body+atasamente+clasificare)."),
             db: Session = Depends(get_db),
             _k: bool = Depends(require_cts_feed_key)):
    """Feed Graph-compatibil: emailuri clean + rapoarte automate, ne-trimise inca, FIFO dupa received_at.
    Atasamentele sunt incluse cu contentBytes. Emailurile cu fisier-atasament lipsa
    sunt EXCLUSE (nu trimitem email partial) si raportate in cts_api_log."""
    # Switch-uri individuale din DB controleaza ce campuri pleaca spre CTS (comutator UI).
    _effective_flags = _get_cts_send_flags(db)
    # Reguli „mailuri automate -> SOLVED" incarcate o singura data; matches() pur pe fiecare rand.
    _solved_rules = cts_auto_solved.load_rules(db)

    # --- By-ID (re-pull punctual): daca se cere un email anume dupa id intern, IGNORAM
    # complet eligibilitatea SI starea sent_to_cts si intoarcem EXCLUSIV acel email
    # (body + atasamente + clasificare daca flag-ul e on), in acelasi envelope Graph.
    if id_email is not None:
        row = db.execute(text(f"""
            SELECT {_EMAIL_COLS}, status, sent_to_cts_at
            FROM emails WHERE id = :eid
        """), {"eid": id_email}).fetchone()
        if row is None:
            _log(db, "get_email_by_id", [], requested=1, success=0, total=0,
                 http_status=404, remote_ip=_client_ip(request),
                 summary="email %s inexistent" % id_email, response_meta={"id_email": id_email})
            raise HTTPException(404, "Email %s inexistent" % id_email)
        r = dict(row._mapping)
        atts, missing = [], []
        if r.get("has_attachments"):
            arows = db.execute(text("""
                SELECT id, graph_attachment_id, name, content_type, size_bytes, storage_path,
                       content_id, is_inline
                FROM attachments WHERE email_id=:eid ORDER BY id
            """), {"eid": r["id"]}).fetchall()
            for ar in arows:
                am = dict(ar._mapping)
                built = _build_attachment(am, auto_rotate=bool(_effective_flags.get("auto_rotate_images")))
                if built is None:
                    # Fisier lipsa: NU aruncam tot emailul (spre deosebire de feed) -> il marcam
                    # si continuam, ca re-pull-ul by-id sa fie best-effort.
                    missing.append(am.get("id"))
                    continue
                atts.append(built)
        msg = _to_graph_message(r, atts, send_flags=_effective_flags,
                                mark_as_solved=cts_auto_solved.matches(
                                    r.get("from_address"), r.get("subject"), _solved_rules))
        summary = "by-id %s livrat (status=%s, sent_to_cts=%s)" % (
            id_email, r.get("status"), ("da" if r.get("sent_to_cts_at") else "nu"))
        _log(db, "get_email_by_id", [r["id"]], requested=1, success=1, total=1,
             http_status=200, remote_ip=_client_ip(request), summary=summary,
             response_meta={"id_email": id_email, "missing_attachment_files": missing})
        return {
            "@odata.context": "https://mailguard.cargotrack.ro/api/v1/cts/$metadata#messages",
            "@odata.count": 1,
            "value": [msg],
        }

    where = _ELIGIBLE
    qp = {}
    if from_date:
        _fd = (from_date or "").strip()
        try:
            from datetime import datetime as _dt
            (_dt.fromisoformat(_fd.replace("Z", "+00:00")) if ("T" in _fd or " " in _fd)
             else _dt.strptime(_fd, "%Y-%m-%d"))
        except Exception:
            raise HTTPException(400, "from_date invalid (folositi YYYY-MM-DD sau ISO 8601)")
        where = f"({_ELIGIBLE}) AND received_at >= :from_date"
        qp["from_date"] = _fd

    total = db.execute(text(
        f"SELECT count(*) FROM emails WHERE {where}"
    ), qp).scalar() or 0

    cand_n = min(limit * 3, 1500)
    rows = db.execute(text(f"""
        SELECT {_EMAIL_COLS}
        FROM emails
        WHERE {where}
        ORDER BY COALESCE(NULLIF(ai_priority,'')::int, 9) ASC, received_at ASC NULLS LAST, id ASC
        LIMIT :n
    """), dict(qp, n=cand_n)).fetchall()

    value = []
    skipped = []
    for row in rows:
        if len(value) >= limit:
            break
        r = dict(row._mapping)
        atts = []
        ok = True
        if r.get("has_attachments"):
            arows = db.execute(text("""
                SELECT id, graph_attachment_id, name, content_type, size_bytes, storage_path,
                       content_id, is_inline
                FROM attachments WHERE email_id=:eid ORDER BY id
            """), {"eid": r["id"]}).fetchall()
            for ar in arows:
                built = _build_attachment(dict(ar._mapping), auto_rotate=bool(_effective_flags.get("auto_rotate_images")))
                if built is None:
                    ok = False
                    break
                atts.append(built)
        if not ok:
            skipped.append(r["id"])
            continue
        value.append(_to_graph_message(r, atts, send_flags=_effective_flags,
                     mark_as_solved=cts_auto_solved.matches(
                         r.get("from_address"), r.get("subject"), _solved_rules)))

    returned_ids = [m["id"] for m in value]
    summary = "%d livrate din %d eligibile" % (len(value), total)
    if skipped:
        summary += "; %d sarite (fisier atasament lipsa)" % len(skipped)
    _log(db, "get_emails", returned_ids, requested=limit, success=len(value), total=total,
         http_status=200, remote_ip=_client_ip(request), summary=summary,
         response_meta={"skipped_missing_file": skipped, "candidates_scanned": len(rows)})

    return {
        "@odata.context": "https://mailguard.cargotrack.ro/api/v1/cts/$metadata#messages",
        "@odata.count": total,
        "value": value,
    }


# ---------------------------------------------------------------- feed documente per email (faza 2)
# Constrangeri de livrare impuse de CTS (2026-08-12):
#   - orice document: MAXIM 1.6 MB
#   - vehicul + contract: OBLIGATORIU PDF
#   - sofer: PDF sau PNG/imagine acceptata, dar tot sub limita de marime
CTS_MAX_BYTES = 1_600_000
_CTS_PDF_ONLY_CATEGORIES = ("vehicul", "contract")
# Maxim documente per apel update_documents. Peste ~50.000 procesarea depaseste --timeout 60 al
# gunicorn-ului si TOT lotul se pierde; 5.000 lasa marja larga (masurat: 300 = 0.4 s).
CTS_MAX_BATCH = 5000

# Ordinea de livrare în get_email_documents (CTS procesează în ordinea array-ului):
#   0) Talon (dacă există) — mereu primul, indiferent de restul
#   1) sofer
#   2) contract
#   3) vehicul (non-talon)
#   4) rest (fără categorie)
# Tipuri Talon din document_types (RO + MD).
_CTS_TALON_TYPE_IDS = frozenset({1, 7})
_CTS_CATEGORY_ORDER = {"sofer": 1, "contract": 2, "vehicul": 3}


def _cts_is_talon(doc: dict) -> bool:
    tid = doc.get("document_type_id")
    if tid is not None and int(tid) in _CTS_TALON_TYPE_IDS:
        return True
    blob = " ".join(
        str(doc.get(k) or "") for k in ("document_type", "part_label", "attachment_name")
    ).lower()
    return "talon" in blob


def _cts_doc_sort_key(doc: dict):
    """Cheie: Talon absolut primul, apoi sofer → contract → vehicul."""
    if _cts_is_talon(doc):
        return (0, 0, int(doc.get("attachment_id") or 0), int(doc.get("extraction_id") or 0))
    cat = (doc.get("category") or "").strip().lower()
    return (
        _CTS_CATEGORY_ORDER.get(cat, 9),
        1,
        int(doc.get("attachment_id") or 0),
        int(doc.get("extraction_id") or 0),
    )


def _sort_docs_for_cts(docs: list) -> list:
    """Ordonează documentele pentru feed-ul CTS.

    - Mail mixte: Talon (dacă e) → sofer → contract → vehicul.
    - Mail un singur tip: tot Talonul primul, dacă există în lot.
    """
    if len(docs) <= 1:
        return docs
    return sorted(docs, key=_cts_doc_sort_key)


def _normalize_for_cts(data: bytes, mime: str, category, att_id=None):
    """Aduce fisierul la formatul cerut de CTS: (bytes, mime).

    Decupajul unui PDF produce JPEG (vezi _crop_to_files), deci fara pasul asta am trimite imagini
    exact acolo unde CTS cere PDF. Conversia si compresia se fac cu `_to_pdf_compressed` din
    documents.py — aceeasi functie folosita deja pe calea spre IRIS, cu aceeasi limita de 1.6MB.

    Pentru sofer pastram imaginea daca incape in limita (CTS o accepta); o convertim la PDF DOAR
    cand e prea mare, fiindca acolo conversia e si mecanismul de compresie.
    Intoarce None daca rezultatul ar fi inutilizabil (fisier gol, sau non-PDF pe o categorie unde
    CTS cere PDF): un document lipsa e raportat in `missing_files` si se poate recupera, pe cand
    unul gol „confirmat trimis" intra in statistica drept succes si nu se mai repara niciodata.
    """
    cat = (category or "").strip().lower()
    m = (mime or "").lower()
    is_pdf = "pdf" in m
    # Fara categorie => tratam ca PDF-obligatoriu. Clasificarea poate esua (masurat pe staging: 3 din
    # 39 de documente validate au category NULL, printre care doua TALOANE — clar de vehicul), iar
    # acelea pleaca totusi spre CTS. Daca am decide dupa categorie, exact documentele neclasificate
    # ar scapa de regula si CTS ar primi imagini acolo unde asteapta PDF. Sofer ramane exceptia
    # explicita, singura categorie unde CTS accepta si imagine.
    needs_pdf = (cat in _CTS_PDF_ONLY_CATEGORIES) or (cat not in ("sofer",))
    too_big = len(data) > CTS_MAX_BYTES

    if not data:
        logger.warning("cts: fisier gol dupa pregatire (att=%s, cat=%s) — NU il trimitem",
                       att_id, cat or "?")
        return None
    if is_pdf and not too_big:
        return data, (mime or "application/pdf")
    if not needs_pdf and not too_big:
        return data, mime          # sofer, imagine sub limita — acceptat ca atare
    try:
        from app.api.v1.documents import _to_pdf_compressed
        out, out_mime = _to_pdf_compressed(data, mime)
    except Exception:
        logger.exception("cts: normalizare esuata (att=%s) — trimit originalul", att_id)
        out, out_mime = data, mime
    if not out:
        logger.warning("cts: conversie cu rezultat gol (att=%s, cat=%s) — NU il trimitem",
                       att_id, cat or "?")
        return None
    if needs_pdf and "pdf" not in (out_mime or "").lower():
        # Categorie care cere PDF, dar conversia nu a reusit: mai bine raportam documentul ca lipsa
        # decat sa trimitem un format pe care CTS il respinge, marcandu-l intre timp „trimis".
        logger.warning("cts: %s cere PDF dar conversia a dat %s (att=%s) — NU il trimitem",
                       cat, out_mime, att_id)
        return None
    if len(out) > CTS_MAX_BYTES:
        # Compresia nu a reusit sa coboare sub prag (PDF cu multe pagini scanate).
        # Trimitem oricum si semnalam in log: CTS poate refuza fisierul, dar refuzul lui e vizibil
        # in update_documents, pe cand un document lipsa ar disparea tacut din statistica.
        logger.warning("cts: document peste limita CTS dupa compresie (att=%s, %d bytes, cat=%s)",
                       att_id, len(out), cat or "?")
    return out, out_mime


def _doc_piece_bytes(path, ctype, part_bbox, page_from, page_to, category=None, att_id=None):
    """(bytes, mime) cu 'bucata decupata' a documentului: bbox -> crop imagine;
    page_from/page_to -> sub-PDF; altfel atasamentul intreg. None la eroare / fisier lipsa.
    Rezultatul trece MEREU prin _normalize_for_cts (format + marime cerute de CTS)."""
    if not path or not os.path.exists(path):
        return None
    try:
        raw, raw_mime = None, None
        if part_bbox:
            from app.api.v1.documents import _crop_to_files
            files = _crop_to_files(path, ctype, part_bbox)
            if not files:
                return None
            d, fm = files[0]
            if isinstance(d, (bytes, bytearray)):
                raw, raw_mime = bytes(d), (fm or "image/jpeg")
            else:
                with open(d, "rb") as fh:  # _crop a cazut pe atasamentul intreg (path string)
                    raw, raw_mime = fh.read(), (fm or ctype or "application/octet-stream")
        elif page_from is not None and page_to is not None:
            from app.api.v1.documents import _pdf_page_subset
            sub = _pdf_page_subset(path, ctype, int(page_from), int(page_to))
            if not sub:
                return None
            raw, raw_mime = bytes(sub[0]), (sub[1] or "application/pdf")
        else:
            with open(path, "rb") as fh:
                raw, raw_mime = fh.read(), (ctype or "application/octet-stream")
        return _normalize_for_cts(raw, raw_mime, category, att_id)
    except Exception:
        logger.warning("cts: nu pot produce bucata documentului (%s)", path)
        return None


_EMISSION_MAP = {
    # text din document -> valoare int CTS (emission_class_list din CTS)
    "noneuro": 0, "non euro": 0, "non-euro": 0,
    "euro i": 1, "euro 1": 1,
    "euro ii": 2, "euro 2": 2,
    "euro iii": 3, "euro 3": 3,
    "euro iv": 4, "euro 4": 4,
    "euro v": 5, "euro 5": 5,
    "euro vi": 6, "euro 6": 6,
    # EEV = Euro 5/6 EEV -> 56
    "eev": 56, "euro v eev": 56, "euro vi eev": 56,
    "euro 5 eev": 56, "euro 6 eev": 56,
    "euro 5/6 eev": 56, "euro v/vi eev": 56,
    "euro v/vi": 56,
}


def _map_emission_class(val):
    """Converteste textul extras din document la int CTS emission_class. None daca necunoscut."""
    if val is None:
        return None
    key = str(val).strip().lower()
    return _EMISSION_MAP.get(key)


def _apply_cts_field_transforms(data, extract_fields):
    """Aplica transformari campuri speciale (ex. emission_class text -> int CTS) pe data dict."""
    if not data or not extract_fields:
        return data
    result = dict(data)
    for fld in extract_fields:
        if fld.get("cts_key") == "emission_class":
            fname = fld.get("name", "")
            if fname in result:
                mapped = _map_emission_class(result[fname])
                if mapped is not None:
                    result[fname] = mapped
    return result


def _track_sent_document(db: Session, email_id: int, m: dict, file_name: str) -> None:
    """Inregistreaza in cts_document_tracking un document care PLEACA efectiv spre CTS.

    Trasabilitatea traieste separat de document_extractions fiindca acela e curatat zilnic de
    storage_cleanup.sh, iar CTS anunta stergerile cu intarziere (batch zilnic). Cheia stabila e
    attachment_id — acelasi id pe care CTS il primeste ca `id_mailguard`.

    Re-pull-ul aceluiasi email (polling pana la 'ready') doar incrementeaza cts_retry_count.
    Garda din WHERE lasa retrimiterea peste 'extracted'/'sent'/'failed' (un document corectat dupa
    un esec CTS TREBUIE sa poata pleca din nou), dar NU peste 'saved'/'deleted' — acolo CTS a spus
    deja ultimul cuvant si un simplu poll nu are voie sa-l stearga.
    Best-effort: o eroare aici NU trebuie sa rupa feed-ul de documente. NU face commit — apelantul
    comite o singura data dupa bucla.

    SAVEPOINT (begin_nested) e OBLIGATORIU, nu o precautie: in Postgres, prima eroare ABORTEAZA
    intreaga tranzactie. Un simplu try/except ar prinde eroarea, dar toate scrierile URMATOARE din
    bucla ar esua cu InFailedSqlTransaction, iar `db.commit()` de la final ar raporta SUCCES fara sa
    scrie nimic — s-ar pierde trasabilitatea intregului email, tacut, desi documentele chiar au
    plecat spre CTS. Savepoint-ul limiteaza rollback-ul la documentul problematic.
    """
    try:
        with db.begin_nested():
            db.execute(text("""
                INSERT INTO cts_document_tracking
                    (email_id, attachment_id, part_no, extraction_id, attachment_name,
                     document_type_id, category, extracted_at, sent_to_cts_at, cts_status,
                     cts_retry_count)
                VALUES (:eid, :aid, :pno, :exid, :name, :dtid, :cat,
                        COALESCE(:extracted_at, now()), now(), 'sent', 0)
                ON CONFLICT (attachment_id, part_no) DO UPDATE SET
                    extraction_id   = EXCLUDED.extraction_id,
                    attachment_name = EXCLUDED.attachment_name,
                    document_type_id= EXCLUDED.document_type_id,
                    category        = EXCLUDED.category,
                    -- pastreaza data REALA a extragerii: altfel un document extras acum 3 zile si
                    -- trimis azi ar fi numarat ca extras azi, mutandu-l in alt interval de raport.
                    extracted_at    = LEAST(cts_document_tracking.extracted_at,
                                            EXCLUDED.extracted_at),
                    sent_to_cts_at  = now(),
                    cts_status      = 'sent',
                    cts_retry_count = cts_document_tracking.cts_retry_count
                                      + CASE WHEN cts_document_tracking.cts_status = 'extracted'
                                             THEN 0 ELSE 1 END,
                    updated_at      = now()
                WHERE cts_document_tracking.cts_status IN ('extracted', 'sent', 'failed')
            """), {
                "eid": email_id,
                "aid": m.get("attachment_id"),
                "pno": int(m.get("part_no") or 0),
                "exid": m.get("id"),
                "name": (file_name or m.get("att_name") or "")[:500] or None,
                "dtid": m.get("document_type_id"),
                "extracted_at": m.get("extraction_created_at"),
                "cat": ((m.get("type_category") or m.get("category") or "") or None) and
                       str(m.get("type_category") or m.get("category"))[:20],
            })
    except Exception:
        logger.exception("cts_document_tracking upsert failed (att=%s) — non-fatal",
                         m.get("attachment_id"))


@router.get("/cts/get_email_documents")
def cts_get_email_documents(request: Request,
             id_email: int = Query(..., description="Id intern Cargo360 al emailului. Intoarce starea procesarii documentelor + (la validare manuala) tipul, datele extrase si bucata de atasament decupata (base64)."),
             db: Session = Depends(get_db),
             _k: bool = Depends(require_cts_feed_key)):
    """Canal SEPARAT de get_emails: emailul pleaca instant la pull, dar documentele din
    atasamente se proceseaza/valideaza asincron. CTS interogheaza acest endpoint (polling)
    pana cand 'status' devine 'ready' (toate validate manual) sau 'manual_needed' (au trecut
    5 min de la sent_to_cts_at si inca nu e validat -> userii proceseaza manual).
    Datele extrase + atasamentul (bucata decupata) se trimit DOAR pentru documentele validate.
    Canal activ pe toate mediile."""
    # Switch UI: send_documente OFF → canal dezactivat (indiferent de mediu)
    _doc_flags = _get_cts_send_flags(db)
    if not _doc_flags.get("send_documente", True):
        _log(db, "get_email_documents", [id_email], requested=1, success=1, total=0,
             http_status=200, remote_ip=_client_ip(request),
             summary="doc-feed dezactivat prin switch UI",
             response_meta={"id_email": id_email, "flag": "send_documente=false"})
        return {"id_email": id_email, "status": "disabled",
                "note": "Canalul de documente este dezactivat din setări.",
                "documents": []}
    erow = db.execute(text("""
        SELECT received_at, sent_to_cts_at, has_attachments,
               COALESCE(sent_to_cts_at, received_at)                          AS anchor,
               COALESCE(sent_to_cts_at, received_at) + interval '10 minutes'   AS wait_deadline,
               now()                                                          AS now_ts,
               (now() >= COALESCE(sent_to_cts_at, received_at) + interval '10 minutes') AS deadline_passed
        FROM emails WHERE id = :eid
    """), {"eid": id_email}).fetchone()
    if erow is None:
        _log(db, "get_email_documents", [], requested=1, success=0, total=0,
             http_status=404, remote_ip=_client_ip(request),
             summary="email %s inexistent" % id_email, response_meta={"id_email": id_email})
        raise HTTPException(404, "Email %s inexistent" % id_email)
    e = dict(erow._mapping)
    deadline_passed = bool(e.get("deadline_passed"))

    rows = db.execute(text("""
        SELECT d.id, d.attachment_id, d.category, d.detected_type, d.document_type_id,
               d.confidence, d.status, d.reviewed, d.reviewed_by, d.data,
               d.part_no, d.part_label, d.page_from, d.page_to, d.part_bbox, d.error,
               d.observatii_ai, d.renamed_file,
               a.name AS att_name, a.content_type, a.storage_path,
               d.created_at AS extraction_created_at,
               dt.extract_fields AS type_extract_fields,
               dt.category AS type_category
        FROM document_extractions d
        JOIN attachments a ON a.id = d.attachment_id
        LEFT JOIN document_types dt ON dt.id = d.document_type_id
        WHERE d.email_id = :eid AND d.grouped_into IS NULL
        ORDER BY d.attachment_id, d.part_no, d.id
    """), {"eid": id_email}).fetchall()

    # Numără câte extracții (non-grouped) există per attachment — pentru a detecta
    # atasamentele originale care au fost sparte în mai multe sub-documente.
    from collections import Counter as _Counter
    _parts_per_att = _Counter(dict(r._mapping)["attachment_id"] for r in rows)

    # Fetch toate atasamentele originale ale emailului (o singură query, nu per-doc).
    _att_rows = db.execute(text("""
        SELECT id, graph_attachment_id, name, content_type, size_bytes, storage_path
        FROM attachments WHERE email_id = :eid ORDER BY id
    """), {"eid": id_email}).fetchall()
    _att_map = {dict(r._mapping)["id"]: dict(r._mapping) for r in _att_rows}

    docs, missing = [], []
    n_validated = 0
    for r in rows:
        m = dict(r._mapping)
        reviewed = bool(m.get("reviewed"))
        st = (m.get("status") or "").lower()
        if reviewed:
            ds = "validated"
            n_validated += 1
            # Validare speciala carGObox PrePaid (type_id=18): fidejusor obligatoriu
            _CARGOBOX_PREPAID_ID = 18
            if m.get("document_type_id") == _CARGOBOX_PREPAID_ID:
                _d = m.get("data") or {}
                _missing = []
                if not (_d.get("Fidejusor nume") or "").strip():
                    _missing.append("Fidejusor nume")
                if not (_d.get("Fidejusor C.I.") or "").strip():
                    _missing.append("Fidejusor C.I.")
                if not (_d.get("Fidejusor CNP") or "").strip():
                    _missing.append("Fidejusor CNP")
                if not _d.get("Semnatura fidejusor"):
                    _missing.append("Semnatura fidejusor")
                if _missing:
                    ds = "failed"
                    n_validated -= 1
                    logger.warning(
                        "cts: carGObox PrePaid ex=%s invalidat — fidejusor incomplet: %s",
                        m.get("id"), ", ".join(_missing)
                    )
        elif st == "failed" or m.get("error"):
            ds = "failed"
        elif deadline_passed:
            ds = "manual_needed"
        else:
            ds = "processing"
        part_bbox = m.get("part_bbox")
        pf, pt = m.get("page_from"), m.get("page_to")
        is_part = bool(part_bbox) or (pf is not None and pt is not None)
        conf = m.get("confidence")

        # original_attachment: prezent doar dacă atasamentul sursă a fost spart (>1 extracții).
        att_id = m.get("attachment_id")
        orig_att = None
        if _parts_per_att.get(att_id, 0) > 1:
            oa = _att_map.get(att_id)
            if oa:
                orig_path = _host_path(oa.get("storage_path"))
                orig_bytes_b64 = None
                if orig_path and os.path.exists(orig_path):
                    try:
                        with open(orig_path, "rb") as _f:
                            orig_bytes_b64 = base64.b64encode(_f.read()).decode("ascii")
                    except Exception:
                        logger.warning("cts: nu pot citi atasamentul original %s (%s)", att_id, orig_path)
                orig_att = {
                    "id": oa.get("id"),
                    "graph_attachment_id": oa.get("graph_attachment_id"),
                    "name": oa.get("name"),
                    "content_type": oa.get("content_type") or "application/octet-stream",
                    "size_bytes": oa.get("size_bytes"),
                    "contentBytes": orig_bytes_b64,
                }

        doc = {
            "extraction_id": m.get("id"),
            "attachment_id": att_id,
            "attachment_name": m.get("att_name"),
            "content_type": m.get("content_type"),
            "is_part": is_part,
            "part_label": m.get("part_label"),
            # type_category (din document_types) e mai stabilă decât category pe extracție
            "category": m.get("type_category") or m.get("category"),
            "document_type_id": m.get("document_type_id"),
            "document_type": m.get("detected_type"),
            "confidence": (float(conf) if conf is not None else None),
            "doc_status": ds,
            "reviewed": reviewed,
            "reviewed_by": m.get("reviewed_by"),
            "observatii_ai": m.get("observatii_ai") or None,
            "renamed_file": m.get("renamed_file") or None,
            "original_attachment": orig_att,
        }
        if ds == "validated":
            raw_data = m.get("data") or {}
            doc["data"] = _apply_cts_field_transforms(raw_data, m.get("type_extract_fields") or [])
            piece = _doc_piece_bytes(_host_path(m.get("storage_path")),
                                     m.get("content_type"), part_bbox, pf, pt,
                                     category=(m.get("type_category") or m.get("category")),
                                     att_id=att_id)
            if piece is None:
                doc["file"] = None
                missing.append(att_id)
            else:
                pbytes, pmime = piece
                base = (m.get("renamed_file") or m.get("part_label") or m.get("att_name") or ("document_%s" % m.get("id")))
                base = str(base)
                _stem, _cur_ext = os.path.splitext(os.path.basename(base))
                _want_ext = ".pdf" if "pdf" in (pmime or "") else (".jpg" if "image" in (pmime or "") else "")
                # Extensia trebuie sa urmeze CONTINUTUL, nu numele original: un .jpg convertit la PDF
                # pentru CTS ar ajunge altfel „talon.jpg" cu octeti de PDF inauntru.
                if _want_ext and _cur_ext.lower() != _want_ext:
                    base = (base[:-len(_cur_ext)] if _cur_ext else base) + _want_ext
                doc["file"] = {
                    "name": base,
                    "contentType": pmime,
                    "size": len(pbytes),
                    "contentBytes": base64.b64encode(pbytes).decode("ascii"),
                }
                _track_sent_document(db, id_email, m, base)
        docs.append(doc)

    # Un singur commit pentru toate randurile de trasabilitate scrise in bucla (endpoint de polling
    # apelat des de CTS — un commit per document ar inmulti inutil round-trip-urile).
    # Fiecare document a fost scris in propriul SAVEPOINT, deci o eroare izolata nu a abortat
    # tranzactia si commit-ul de aici chiar persista restul.
    try:
        db.commit()
    except Exception:
        logger.exception("cts_document_tracking commit failed — trasabilitate pierduta pt email %s",
                         id_email)
        db.rollback()

    n = len(docs)
    if n == 0:
        if bool(e.get("has_attachments")) and not deadline_passed:
            agg = "processing"
        else:
            agg = "no_documents"
    elif n_validated == n:
        agg = "ready"
    elif not deadline_passed:
        agg = "processing"
    else:
        agg = "manual_needed"

    def _iso(v):
        return v.isoformat() if (v is not None and hasattr(v, "isoformat")) else v

    docs = _sort_docs_for_cts(docs)

    summary = "doc-feed email %s: status=%s docs=%d validate=%d" % (id_email, agg, n, n_validated)
    _log(db, "get_email_documents", [id_email], requested=1, success=1, total=n,
         http_status=200, remote_ip=_client_ip(request), summary=summary,
         response_meta={"id_email": id_email, "status": agg, "validated": n_validated,
                        "missing_files": missing})
    return {
        "id_email": id_email,
        "received_at": _iso(e.get("received_at")),
        "sent_to_cts_at": _iso(e.get("sent_to_cts_at")),
        "now": _iso(e.get("now_ts")),
        "wait_deadline": _iso(e.get("wait_deadline")),
        "status": agg,
        "documents": docs,
    }


def _parse_ids(lst):
    """Id-uri interne Cargo360. Accepta 8912, '8912', '#8912'. Ce nu e numeric (ex. graph id)
    e numarat ca 'invalid' (NU ignorat tacut)."""
    nums, invalid = [], 0
    for x in (lst or []):
        s = str(x).strip().lstrip("#").strip()
        if s.isdigit():
            nums.append(int(s))
        elif str(x).strip():
            invalid += 1
    return nums, invalid


# ---------------------------------------------------------------- update_emails (confirmare CTS)
@router.post("/cts/update_emails")
def cts_update_emails(request: Request, payload: dict,
            db: Session = Depends(get_db),
            _k: bool = Depends(require_cts_feed_key)):
    """CTS confirma ce a salvat, pe baza id-ului intern Cargo360 (campul "id" din feed, ex. 8912).
    saved -> sent_to_cts_at=now() (clean: si queue_status='sent_to_cts'; auto_report: queue_status ramane
    'auto_closed' ca cargo360 sa continue procesarea automata). Doar pe cele eligibile, ne-trimise;
    failed -> cts_send_error + cts_send_attempts++. Idempotent."""
    raw_saved = payload.get("saved", payload.get("saved_ids", []))
    raw_failed = payload.get("failed", payload.get("failed_ids", []))
    if not isinstance(raw_saved, list) or not isinstance(raw_failed, list):
        raise HTTPException(400, "Campurile 'saved' si 'failed' trebuie sa fie liste de id-uri de email")
    saved, invalid = _parse_ids(raw_saved)
    failed, _f_invalid = _parse_ids(raw_failed)

    marked = already = not_eligible = unknown = failed_recorded = 0
    fresh_clean_ids = []  # id-urile clean marcate ACUM (trigger autoreply 'new_in_cts')
    if saved:
        found = db.execute(text("SELECT count(*) FROM emails WHERE id = ANY(:ids)"),
                           {"ids": saved}).scalar() or 0
        already = db.execute(text(
            "SELECT count(*) FROM emails WHERE id = ANY(:ids) AND sent_to_cts_at IS NOT NULL"
        ), {"ids": saved}).scalar() or 0
        # (1) clean/ready_for_cts -> marcam livrat SI mutam coada in sent_to_cts.
        #     RETURNING id: capturam exact emailurile clean intrate ACUM in CTS -> trigger autoreply.
        res_clean = db.execute(text(f"""
            UPDATE emails SET sent_to_cts_at = now(), queue_status = 'sent_to_cts', cts_send_error = NULL
            WHERE id = ANY(:ids) AND {_ELIGIBLE_CLEAN} AND sent_to_cts_at IS NULL
            RETURNING id
        """), {"ids": saved})
        fresh_clean_ids = [r[0] for r in res_clean]
        # (2) auto_report/auto_closed -> marcam DOAR livrat; queue_status/status RAMAN intacte
        #     ca cargo360 sa continue procesarea automata (faza 2 stie ca au plecat din sent_to_cts_at).
        res_auto = db.execute(text(f"""
            UPDATE emails SET sent_to_cts_at = now(), cts_send_error = NULL
            WHERE id = ANY(:ids) AND {_ELIGIBLE_AUTO} AND sent_to_cts_at IS NULL
        """), {"ids": saved})
        marked = len(fresh_clean_ids) + (res_auto.rowcount or 0)
        # Reconciliere drift DOAR pe clean: livrate (sent_to_cts_at setat) dar cu coada ramasa in
        # ready_for_cts -> oglindim. NU atingem auto_closed (acela ramane intentionat neschimbat).
        db.execute(text("""
            UPDATE emails SET queue_status = 'sent_to_cts'
            WHERE id = ANY(:ids) AND sent_to_cts_at IS NOT NULL AND queue_status = 'ready_for_cts'
        """), {"ids": saved})
        unknown = max(0, len(set(saved)) - found)
        not_eligible = max(0, found - already - marked)

    if failed:
        res2 = db.execute(text("""
            UPDATE emails SET cts_send_attempts = COALESCE(cts_send_attempts, 0) + 1,
                              cts_send_error = :err
            WHERE id = ANY(:ids)
        """), {"ids": failed, "err": "CTS a raportat esec la salvare"})
        failed_recorded = res2.rowcount or 0

    db.commit()

    # Auto-reply (Faza 1, dry-run): pentru emailurile clean proaspat intrate in CTS, decidem si
    # LOGAM in autoreply_send_log (would_send / throttled / skipped_*). In dry-run NU se trimite nimic.
    # Best-effort, izolat: o eroare aici NU trebuie sa afecteze feed-ul CTS.
    if fresh_clean_ids:
        try:
            from app.services import autoreply_dispatch
            autoreply_dispatch.dispatch_for_ids(fresh_clean_ids)
        except Exception:
            logger.exception("autoreply dispatch (new_in_cts) failed — non-fatal")

        # No-reply auto-reply: trimite confirmare de primire către expeditorul fiecărui email
        # proaspăt intrat în CTS. Best-effort — o eroare nu blochează feed-ul.
        try:
            from app.services import noreply_sender
            for eid in fresh_clean_ids:
                noreply_sender.maybe_send_autoreply(db, eid)
        except Exception:
            logger.exception("noreply autoreply (new_in_cts) failed — non-fatal")

    # Mailuri automate: persistam flag-ul „a plecat ca SOLVED" pe id-urile confirmate de CTS care
    # se potrivesc unei reguli auto-solved (clean + auto_report, doar cele chiar livrate). Idempotent,
    # best-effort, izolat: o eroare aici NU afecteaza ack-ul CTS (deja comis mai sus).
    if saved:
        try:
            _rules = cts_auto_solved.load_rules(db)
            _rows = db.execute(text(
                "SELECT id, from_address, subject FROM emails "
                "WHERE id = ANY(:ids) AND sent_to_cts_at IS NOT NULL AND cts_mark_solved = FALSE"
            ), {"ids": saved}).fetchall()
            _solved = [rr._mapping["id"] for rr in _rows
                       if cts_auto_solved.matches(rr._mapping["from_address"],
                                                  rr._mapping["subject"], _rules)]
            if _solved:
                db.execute(text("UPDATE emails SET cts_mark_solved = TRUE WHERE id = ANY(:ids)"),
                           {"ids": _solved})
                db.commit()
        except Exception:
            logger.exception("cts_mark_solved persist (update_emails) failed — non-fatal")
            db.rollback()

    result = {
        "marked_sent": marked,
        "already_sent": already,
        "not_eligible": not_eligible,
        "unknown": unknown,
        "invalid": invalid,
        "failed_recorded": failed_recorded,
    }
    summary = "update_emails: %d marcate trimise, %d deja trimise, %d neeligibile, %d necunoscute, %d invalide, %d esecuri" % (
        marked, already, not_eligible, unknown, invalid, failed_recorded)
    _log(db, "update_emails", saved, requested=len(saved) + invalid + len(failed),
         success=marked, total=len(saved) + invalid, http_status=200, remote_ip=_client_ip(request),
         summary=summary, response_meta={**result, "failed_ids": failed})
    return result


# ---------------------------------------------------------------- update_documents (feedback CTS)
_BIGINT_MAX = 9223372036854775807
_INT_MAX = 2147483647


def _as_int(val, max_val=_BIGINT_MAX):
    """int sau None. Orice valoare care NU incape in coloana (text, float, peste range) -> None.
    Fara asta, un `entity_id` aiurea de la CTS ar arunca 'value out of range for type bigint'
    DIRECT din Postgres, abortand tranzactia si pierzand tot batch-ul (vezi _parse_iso_ts)."""
    if val is None or isinstance(val, bool):
        return None
    try:
        n = int(str(val).strip())
    except (TypeError, ValueError):
        return None
    return n if -max_val - 1 <= n <= max_val else None


def _parse_iso_ts(val):
    """Timestamp ISO 8601 -> datetime, ORICE altceva -> None (apelantul cade pe now()).

    Critic: CAST(:x AS timestamptz) pe un string invalid ('ieri', '13/08/2026') arunca eroarea din
    Postgres si ABORTEAZA intreaga tranzactie — intr-un batch zilnic cu sute de confirmari valide,
    toate s-ar pierde, iar CTS nu le retrimite. In plus DateStyle=MDY interpreteaza '12/08/2026'
    ca 8 decembrie, nu 12 august: o data gresita tacut e mai rea decat una respinsa. Deci acceptam
    EXCLUSIV ISO 8601, parsat in Python, inainte sa ajunga la baza de date.
    """
    if val is None:
        return None
    from datetime import datetime as _dt, timezone as _tz
    s = str(val).strip()
    # Cerem si componenta de ora: `fromisoformat("2026-08-12")` trece, dar o data fara ora inseamna
    # miezul noptii, adica pana la 24h eroare pe momentul stergerii.
    if not s or ("T" not in s and " " not in s):
        return None
    try:
        r = _dt.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    # Timestamp naiv (fara fus orar) ar fi interpretat de Postgres in fusul sesiunii. Azi acela e
    # UTC, dar e o dependenta nescrisa: fixam explicit UTC, cum cere si documentatia pentru CTS.
    return r.replace(tzinfo=_tz.utc) if r.tzinfo is None else r


def _doc_entries(raw, field_name):
    """Normalizeaza o lista de intrari CTS la ((attachment_id, part_no|None), dict-ul original).

    `part_no` (optional) tinteste UN document anume dintr-un atasament care contine mai multe acte
    — cheia reala a tabelei e (attachment_id, part_no). Fara el, confirmarea se aplica tuturor
    actelor din fisier, ceea ce e corect doar cand fisierul are un singur document.
    CTS poate trimite si `extraction_id` (livrat in feed, unic per document); il rezolvam la part_no
    in endpoint, unde avem acces la baza de date.

    Deduplica pe cheia efectiva (ultima aparitie castiga). Id-urile nevalide sunt numarate, nu
    ignorate tacut. Sortata la final: doua apeluri concurente iau lacatele in aceeasi ordine, deci
    nu se pot bloca reciproc.
    """
    if raw is None:
        return [], 0
    if not isinstance(raw, list):
        raise HTTPException(400, "Campul '%s' trebuie sa fie o lista de obiecte" % field_name)
    seen, invalid = {}, 0
    for item in raw:
        if isinstance(item, dict):
            rawid = item.get("attachment_id", item.get("id"))
            payload = item
        else:
            rawid, payload = item, {}
        s = str(rawid if rawid is not None else "").strip().lstrip("#").strip()
        # isascii(): '٥'.isdigit() e True in Python, dar un id scris cu cifre arabe nu e un id real.
        if s.isascii() and s.isdigit():
            n = int(s)
            if 0 < n <= _BIGINT_MAX:
                pno = _as_int(payload.get("part_no"), 32767) if isinstance(payload, dict) else None
                seen[(n, pno)] = payload
                continue
        invalid += 1
    return sorted(seen.items(), key=lambda kv: (kv[0][0], kv[0][1] if kv[0][1] is not None else -1)), invalid


@router.post("/cts/update_documents")
def cts_update_documents(request: Request, payload: dict,
            db: Session = Depends(get_db),
            _k: bool = Depends(require_cts_feed_key)):
    """CTS raporteaza soarta documentelor primite prin get_email_documents. Cheia e attachment_id
    (campul `id_mailguard` livrat in feed). Doua fluxuri, procesate independent in acelasi apel:

      saved   -> documentul a fost atasat unei entitati CTS (entity_type + entity_id)
      failed  -> documentul NU a putut fi procesat; 'reason' explica de ce
      deleted -> batch zilnic: documente salvate anterior, sterse de un operator CTS
                 (admin_id + deleted_at). Se accepta DOAR peste randuri deja 'saved'.

    Idempotent: re-trimiterea aceluiasi payload nu dubleaza nimic. Un attachment_id necunoscut e
    numarat, nu respins — statistica ramane best-effort, la fel ca update_emails."""
    saved, inv_saved = _doc_entries(payload.get("saved"), "saved")
    failed, inv_failed = _doc_entries(payload.get("failed"), "failed")
    deleted, inv_deleted = _doc_entries(payload.get("deleted"), "deleted")
    invalid = inv_saved + inv_failed + inv_deleted

    if not saved and not failed and not deleted and not invalid:
        raise HTTPException(400, "Payload gol: trimiteti cel putin una dintre listele "
                                 "'saved', 'failed' sau 'deleted'")

    # Plafon pe marimea lotului. Masurat pe staging: 20.000 intrari = 21 s, 50.000 = 52 s, iar
    # gunicorn are --timeout 60 — peste prag workerul e omorat si CTS pierde TOT lotul, inclusiv
    # confirmarile valide. Mai bine il refuzam explicit, cu instructiunea de a-l imparti.
    _n_total = len(saved) + len(failed) + len(deleted)
    if _n_total > CTS_MAX_BATCH:
        raise HTTPException(413, "Lot prea mare: %d documente (maxim %d per apel). "
                                 "Impartiti-l in loturi mai mici — un lot peste limita ar depasi "
                                 "timpul maxim de raspuns si s-ar pierde in intregime."
                                 % (_n_total, CTS_MAX_BATCH))

    # CTS poate trimite `extraction_id` (unic per document, livrat in feed) in loc de `part_no`.
    # Il traducem aici, unde avem baza de date, ca restul logicii sa lucreze cu o singura cheie.
    def _resolve(entries):
        out = []
        for (aid, pno), item in entries:
            if pno is None and isinstance(item, dict) and item.get("extraction_id") is not None:
                _ex = _as_int(item.get("extraction_id"))
                if _ex is not None:
                    _p = db.execute(text(
                        "SELECT part_no FROM cts_document_tracking "
                        " WHERE attachment_id = :aid AND extraction_id = :ex"
                    ), {"aid": aid, "ex": _ex}).scalar()
                    if _p is not None:
                        pno = int(_p)
            out.append(((aid, pno), item))
        return out

    saved, failed, deleted = _resolve(saved), _resolve(failed), _resolve(deleted)

    # Precedenta intre liste: deleted > failed > saved. Daca aceeasi cheie apare in doua liste
    # (retry CTS, doua sub-sisteme), starea cea mai avansata castiga si documentul e numarat
    # O SINGURA data — altfel un singur document ar aparea si ca salvat si ca esuat.
    _d_ids = {k for k, _ in deleted}
    _f_ids = {k for k, _ in failed}
    saved = [(k, it) for k, it in saved if k not in _d_ids and k not in _f_ids]
    failed = [(k, it) for k, it in failed if k not in _d_ids]

    # Contoarele numara CONFIRMARI (cate intrari trimise de CTS au avut efect), nu randuri atinse.
    # Un atasament cu 3 acte confirmat printr-o singura intrare inseamna 1 confirmare, nu 3 —
    # altfel `success` din cts_api_log ar depasi `total` si orice raport ar da peste 100%.
    marked_saved = marked_failed = marked_deleted = 0
    rows_saved = rows_failed = rows_deleted = 0
    unknown = orphan_deleted = not_saved_yet = already_deleted = already_final = 0
    partially_deleted = bad_timestamp = 0

    def _where(pno):
        """Filtru pe cheie: cu part_no tintim UN document; fara el, toate actele atasamentului."""
        return ("attachment_id = :aid AND part_no = :pno" if pno is not None
                else "attachment_id = :aid")

    def _params(aid, pno, **extra):
        p = {"aid": aid}
        if pno is not None:
            p["pno"] = pno
        p.update(extra)
        return p

    def _statuses(aid, pno):
        return [r[0] for r in db.execute(text(
            "SELECT cts_status FROM cts_document_tracking WHERE " + _where(pno)
        ), _params(aid, pno)).fetchall()]

    for (aid, pno), item in saved:
        # Garda `sent_to_cts_at IS NOT NULL`: confirmarea se aplica DOAR documentelor care chiar au
        # plecat spre CTS. Fara ea, un act inca in validare (stare 'extracted') din acelasi fisier
        # ar fi marcat 'saved' fara sa fi fost trimis vreodata — si ar ramane blocat acolo pentru
        # totdeauna, fiindca garda din _track_sent_document nu mai retrimite peste 'saved'.
        n = db.execute(text("""
            UPDATE cts_document_tracking
               SET cts_status = 'saved',
                   cts_entity_type = :etype,
                   cts_entity_id   = :eid,
                   cts_fail_reason = NULL,
                   updated_at      = now()
             WHERE """ + _where(pno) + """
               AND sent_to_cts_at IS NOT NULL
               AND cts_status IN ('sent', 'failed', 'saved')
        """), _params(aid, pno,
                      etype=(str(item.get("entity_type"))[:20] if item.get("entity_type") else None),
                      eid=_as_int(item.get("entity_id")))).rowcount or 0
        if n:
            marked_saved += 1
            rows_saved += n
        else:
            st = _statuses(aid, pno)
            if not st:
                unknown += 1
            elif any(s in ("deleted",) for s in st):
                already_final += 1
            else:
                not_saved_yet += 1   # exista, dar nu a plecat inca spre CTS

    for (aid, pno), item in failed:
        # cts_entity_* se golesc explicit: un document esuat nu are entitate CTS. Fara asta, un id
        # confirmat intai 'saved' si apoi 'failed' ar ramane cu entitatea veche lipita pe el.
        n = db.execute(text("""
            UPDATE cts_document_tracking
               SET cts_status = 'failed',
                   cts_fail_reason = :reason,
                   cts_entity_type = NULL,
                   cts_entity_id   = NULL,
                   updated_at = now()
             WHERE """ + _where(pno) + """
               AND sent_to_cts_at IS NOT NULL
               AND cts_status IN ('sent', 'failed', 'saved')
        """), _params(aid, pno,
                      reason=(str(item.get("reason"))[:2000] if item.get("reason") else None))).rowcount or 0
        if n:
            marked_failed += 1
            rows_failed += n
        else:
            st = _statuses(aid, pno)
            if not st:
                unknown += 1
            elif any(s == "deleted" for s in st):
                already_final += 1
            else:
                not_saved_yet += 1

    for (aid, pno), item in deleted:
        # Stergerea se aplica DOAR peste un document confirmat salvat. Daca CTS anunta o stergere
        # pentru un document care la noi e inca 'sent' (ack-ul de salvare s-a pierdut), NU deducem
        # ca a fost salvat — l-am contamina statistica de succes. Il numaram separat.
        _raw_ts = item.get("deleted_at")
        _ts = _parse_iso_ts(_raw_ts)
        if _raw_ts and _ts is None:
            bad_timestamp += 1
            logger.warning("update_documents: deleted_at invalid (att=%s, primit=%r) — folosim now()",
                           aid, str(_raw_ts)[:64])
        _before = _statuses(aid, pno)
        n = db.execute(text("""
            UPDATE cts_document_tracking
               SET cts_status     = 'deleted',
                   cts_admin_id   = :admin_id,
                   cts_deleted_at = COALESCE(:deleted_at, now()),
                   updated_at     = now()
             WHERE """ + _where(pno) + """ AND cts_status = 'saved'
        """), _params(aid, pno,
                      admin_id=_as_int(item.get("admin_id"), _INT_MAX),
                      deleted_at=_ts)).rowcount or 0
        if n:
            marked_deleted += 1
            rows_deleted += n
            # Atasament cu mai multe acte din care doar o parte erau salvate: semnalam explicit,
            # altfel „1 sters" ar sugera ca tot fisierul a fost sters.
            if pno is None and n < len([s for s in _before if s != "deleted"]):
                partially_deleted += 1
        else:
            if not _before:
                orphan_deleted += 1
            elif all(s == "deleted" for s in _before):
                already_deleted += 1
            else:
                not_saved_yet += 1

    db.commit()

    result = {
        "marked_saved": marked_saved,
        "marked_failed": marked_failed,
        "marked_deleted": marked_deleted,
        "rows_saved": rows_saved,
        "rows_failed": rows_failed,
        "rows_deleted": rows_deleted,
        "partially_deleted": partially_deleted,
        "unknown": unknown,
        "already_final": already_final,
        "orphan_deleted": orphan_deleted,
        "not_saved_yet": not_saved_yet,
        "already_deleted": already_deleted,
        "invalid": invalid,
        "bad_timestamp": bad_timestamp,
    }
    summary = ("update_documents: %d salvate (%d randuri), %d esuate, %d sterse, %d partial sterse, "
               "%d necunoscute, %d deja finalizate, %d fara istoric, %d netrimise inca, "
               "%d deja sterse, %d invalide, %d cu data invalida") % (
        marked_saved, rows_saved, marked_failed, marked_deleted, partially_deleted,
        unknown, already_final, orphan_deleted, not_saved_yet, already_deleted,
        invalid, bad_timestamp)
    # success/total in ACEEASI unitate (confirmari), altfel raportul din UI depaseste 100%.
    _log(db, "update_documents", [k[0] for k, _ in saved],
         requested=len(saved) + len(failed) + len(deleted) + invalid,
         success=marked_saved + marked_failed + marked_deleted,
         total=len(saved) + len(failed) + len(deleted) + invalid,
         http_status=200, remote_ip=_client_ip(request), summary=summary,
         response_meta={**result,
                        "failed_ids": [k[0] for k, _ in failed],
                        "deleted_ids": [k[0] for k, _ in deleted]})
    return result


@router.get("/cts/document-stats")
def cts_document_stats(from_date: Optional[str] = Query(None, description="Doar documente trimise la/dupa aceasta data (YYYY-MM-DD sau ISO 8601)"),
            db: Session = Depends(get_db),
            admin=Depends(get_current_admin)):
    """Palnia completa a unui document, pe categorie (sofer / vehicul / contract):

        extras  ->  trimis spre CTS  ->  salvat pe entitate  ->  sters de operator

    Bazele procentelor sunt DIFERITE si de aceea sunt numite explicit — un procent fara numitor
    stiut se citeste gresit:
      sent_pct    = trimise / extrase          (cate documente extrase apuca sa plece spre CTS)
      saved_pct   = ever_saved / total_sent    (din cele plecate, cate s-au atasat pe entitate)
      deleted_pct = deleted_after / ever_saved (din cele ajunse pe entitate, cate au fost sterse)

    `ever_saved` = saved + deleted_after: un document sters a fost, prin definitie, salvat inainte.
    Fara asta, o stergere ar scadea si rata de succes a asocierii, desi asocierea chiar reusise.

    Fereastra: `from_date` e plafonat la `doc_stats.STATS_SINCE` (24.08.2026) — documentele mai
    vechi nu intra in statistica nici daca apelantul cere o data mai veche.
    """
    if from_date:
        _fd = (from_date or "").strip()
        try:
            from datetime import datetime as _dt
            (_dt.fromisoformat(_fd.replace("Z", "+00:00")) if ("T" in _fd or " " in _fd)
             else _dt.strptime(_fd, "%Y-%m-%d"))
        except Exception:
            raise HTTPException(400, "from_date invalid (folositi YYYY-MM-DD sau ISO 8601)")
    # Plafon dur la fereastra de raportare a paginii „Procesare documente" (STATS_SINCE): un
    # from_date lipsa sau mai vechi e ridicat la ea, deci nimic dinainte nu se contorizeaza.
    from_date = clamp_from_date(from_date)
    # Fereastra se masoara pe DATA MAILULUI, ca in /documents/extractions/stats — nu pe
    # `extracted_at` (momentul procesarii): un mail vechi reprocesat azi ar fi intrat altfel in
    # statistica. `email_id` n-are FK (randurile din emails/attachments pot fi curatate de
    # storage_cleanup, tracking-ul ramane), deci JOIN-ul e LEFT si cade inapoi pe `extracted_at`
    # cand mailul nu mai exista — altfel documentele vechi ar disparea din numitor pe tacute.
    where, qp = ("COALESCE(e.received_at, t.extracted_at) >= :from_date",
                 {"from_date": from_date})

    rows = db.execute(text(f"""
        SELECT COALESCE(t.category, 'necunoscut')                   AS category,
               count(*)                                             AS extracted,
               count(*) FILTER (WHERE t.cts_status <> 'extracted')  AS total_sent,
               count(*) FILTER (WHERE t.cts_status = 'saved')       AS saved,
               count(*) FILTER (WHERE t.cts_status = 'failed')      AS failed,
               count(*) FILTER (WHERE t.cts_status = 'sent')        AS pending_ack,
               count(*) FILTER (WHERE t.cts_status = 'deleted')     AS deleted_after
        FROM cts_document_tracking t
        LEFT JOIN emails e ON e.id = t.email_id
        WHERE {where}
        GROUP BY 1 ORDER BY 1
    """), qp).fetchall()

    def _pct(part, whole):
        return round(100.0 * part / whole, 1) if whole else None

    _KEYS = ["extracted", "total_sent", "saved", "failed", "pending_ack", "deleted_after"]
    items, tot = [], {k: 0 for k in _KEYS}
    for r in rows:
        m = dict(r._mapping)
        for k in _KEYS:
            tot[k] += m[k]
        m["ever_saved"] = m["saved"] + m["deleted_after"]
        m["sent_pct"] = _pct(m["total_sent"], m["extracted"])
        m["saved_pct"] = _pct(m["ever_saved"], m["total_sent"])
        m["deleted_pct"] = _pct(m["deleted_after"], m["ever_saved"])
        items.append(m)

    tot["ever_saved"] = tot["saved"] + tot["deleted_after"]
    tot["sent_pct"] = _pct(tot["total_sent"], tot["extracted"])
    tot["saved_pct"] = _pct(tot["ever_saved"], tot["total_sent"])
    tot["deleted_pct"] = _pct(tot["deleted_after"], tot["ever_saved"])
    return {"ok": True, "from_date": from_date, "since": STATS_SINCE,
            "by_category": items, "total": tot}


# ---------------------------------------------------------------- monitorizare (admin)
@router.get("/cts/log")
def cts_log(limit: int = Query(50, ge=1, le=500),
            db: Session = Depends(get_db),
            admin=Depends(get_current_admin)):
    rows = db.execute(text("""
        SELECT id, ts, action, email_ids, requested, success, total,
               http_status, remote_ip, summary, response_meta
        FROM cts_api_log ORDER BY ts DESC, id DESC LIMIT :l
    """), {"l": limit}).fetchall()
    return {"items": [dict(r._mapping) for r in rows]}


@router.get("/cts/stats")
def cts_stats(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    row = db.execute(text(f"""
        SELECT
          count(*) FILTER (WHERE {_ELIGIBLE}) AS ready,
          count(*) FILTER (WHERE sent_to_cts_at IS NOT NULL) AS sent,
          count(*) FILTER (WHERE cts_send_error IS NOT NULL) AS errors
        FROM emails
    """)).fetchone()
    m = dict(row._mapping)
    m["last_pull_at"] = db.execute(text("SELECT max(ts) FROM cts_api_log WHERE action='pull'")).scalar()
    m["last_ack_at"] = db.execute(text("SELECT max(ts) FROM cts_api_log WHERE action='ack'")).scalar()
    return m


@router.get("/cts/key")
def cts_key(admin=Depends(get_current_admin)):
    """Cheia X-CTS-Token curenta (doar admin) — pentru afisare view/hide in UI."""
    return {"header": "X-CTS-Token", "key": get_settings().cts_feed_api_key or ""}
