"""Extragere serie OP din atașamente — flux auxiliar routing departament.

Detectează emailurile cu ordin de plată (OP), extrage seria facturii din atașament
(text local sau vision AI) și rutează emailul la departamentul corect:
  PPCB / PPBG / PPHU / ASCF / PPCF → suport_1
  orice altă serie sau nicio serie → suport_1 (fallback safe)
  serie ACTS/ECTS/alt prefix non-PPCB → contabilitate

Fluxul este COMPLET SEPARAT de modulul Procesare documente (vehicule/șoferi/contracte).
"""
import os
import re
import logging
from typing import Optional

from app.database import SessionLocal
from sqlalchemy import text

from app.services import priority_rules, iris_ai

logger = logging.getLogger("mailguard.op_extractor")

# ── Serie → departament ──────────────────────────────────────────────────────
# ALLOWLIST OFICIAL de serii de factură acreditate (sursă: user, 2026-07-23). O progresie
# LITERE+CIFRE care NU e aici NU e considerată factură → nu declanșează fluxul de contabilitate.
_KNOWN_SERIE_PREFIXES = {
    "ARC", "GCTS", "CCTS", "FS", "TCTS", "FSA", "CC", "ACTS", "ECTS", "SCTS",
    "DCTS", "PPBG", "FFBG", "PECTS", "ASHU", "FFRO", "PPHU", "FDBG", "ASCF",
    "FFHU", "SCFF", "SAHU", "PPCF", "FFCB", "FDCB", "CPCB", "FGCB", "PCTS",
    "HUCB", "ATCB", "FRCB", "BGST", "BGSS", "FRD", "FRBG", "CCHU", "SKCB",
    "ECHU", "FFCF", "FRACB", "AAT", "BP", "BPA", "BCTS", "ECOL", "ATRK",
    "PPCB",
}
# Din cele acreditate, DOAR acestea merg pe Suport 1; restul din allowlist → Contabilitate.
# PPCF adaugat 2026-09-11 (cerere business: platile PPCF -> Suport 1). Era in allowlist-ul de
# serii acreditate, dar nu si aici, deci `_department_from_series` il trimitea pe Contabilitate.
_SUPORT1_PREFIXES = {"PPCB", "PPHU", "PPBG", "ASCF", "PPCF"}
# Serii acreditate care aparțin departamentului Recuperare TVA (nu Contabilitate).
# Decizie user 2026-07-24 (email #53528, serie FS „Factură servicii Recuperare TVA").
_RECUPERARE_TVA_PREFIXES = {"FS"}

# ── Regex extragere serie din text ──────────────────────────────────────────
# Caută PREFIX (2-6 litere majuscule) urmat de cifre (≥3). Ex: PPCB00123, ACTS939046.
# Varianta cu cratimă „P-ECTS939046" e normalizată separat (cratima e scoasă înainte de match).
_SERIE_RE = re.compile(r'\b([A-Z]{2,6})\d{3,}')

# Cuvinte comune care NU sunt serii de factură (prefix-uri de evitat)
# Include coduri BIC bancare frecvente în OP-urile românești
_SERIE_BLACKLIST = {
    "OP", "CUI", "CIF", "CF", "RO", "MD", "CRT", "ID", "NR", "REG", "TVA",
    "VAT", "IBAN", "BIC", "SWIFT", "STR", "JUD", "ORC", "CAE", "COD",
    # BIC-uri bănci România
    "BTRL", "BTRLR", "RNCB", "BRDE", "INGB", "BPOS", "PIRB", "CECE", "EXIM",
    "BITR", "MIRO", "OTPV", "BFER", "FNNB", "CRDB", "UGBI", "PRTT",
    # Alte prefixe false pozitive frecvente în documente bancare
    "SEPA", "EUR", "RON", "USD", "LEI", "BCR", "BRD", "CEC", "ING", "BNR",
    "CONT", "TREZ", "DATA", "FACT", "INV", "REF", "POS", "ATM",
    # CUI cu prefix de țară fuzionat (ex: CUIRO31656014 → nu e serie de factură)
    "CUIRO", "CUIMD", "CUIPL", "CUIBG", "CUIHU", "CUIDE", "CUIAT", "CUIIT",
}

# ── Prompt vision pentru extragere serie + monedă ───────────────────────────
_VISION_OP_SYSTEM = (
    "Esti un extractor de date din ordine de plata. Din documentul atasat extrage DOUA informatii:\n"
    "1. SERIA facturii: prefix din litere majuscule urmat de cifre (ex: PPCB00123 → PPCB, ACTS939046 → ACTS). "
    "Daca nu gasesti serie clara → NONE.\n"
    "2. MONEDA platii: MDL (lei moldovenesti), RON, EUR, USD sau NONE daca nu e clara.\n"
    "Raspunde EXCLUSIV in formatul: SERIE|MONEDA (ex: PPCB|RON, NONE|MDL, ACTS|NONE). Niciun alt text."
)

# ── Regex monedă MDL ─────────────────────────────────────────────────────────
# Prinde "MDL", "lei moldovenești", "lei moldoveni", "leu moldovenesc" în text
_MDL_RE = re.compile(
    r'\bMDL\b|lei\s+moldove[a-zăîșț]{2,}|leu\s+moldovenesc',
    re.IGNORECASE
)

# Limite
VISION_MAX_BYTES = 14 * 1024 * 1024   # sub limita gateway-ului
MAX_ATTACHMENTS = 3                     # primele N atașamente procesate per email
MAX_EXTRACT_ATTEMPTS = 3               # fallback după N încercări eșuate

# ── Path mapping container → host (identic cu emails.py) ────────────────────
_CONTAINER_PREFIX = "/app/storage/attachments"
_HOST_PREFIX = os.getenv("ATTACH_HOST_PREFIX",
                          "/home/sergiu/parser-email-op/storage/attachments")


def _host_path(storage_path: str) -> Optional[str]:
    if not storage_path:
        return None
    if storage_path.startswith(_CONTAINER_PREFIX):
        return _HOST_PREFIX + storage_path[len(_CONTAINER_PREFIX):]
    return storage_path


# ── Detecție OP ──────────────────────────────────────────────────────────────

# Cuvinte cheie care apar în corpul unui ordin de plată / dovadă de plată.
# Pre-scan rapid în primele 1000 caractere de text extras local din atașament.
_OP_CONTENT_KEYWORDS = [
    "ordin de plata", "ordin de plată",
    "beneficiar", "platitor", "plătitor",
    "suma de plata", "suma de plată", "suma platita", "suma plătită",
    "cont beneficiar", "cont platitor",
    "referinta plata", "referinta platii", "referinta plății",
    "detalii plata", "detalii plată",
    "confirmare transfer", "confirmare plata", "confirmare plată",
    "dovada plata", "dovada plată", "dovada de plata",
    "extras de cont", "extras cont",
    "swift transfer", "wire transfer",
    "cod iban", "nr. op", "nr op ",
]


def _attachment_names_str(attachments: list) -> str:
    """Construiește stringul cu nume atașamente pentru priority_rules.match()."""
    names = []
    for a in (attachments or []):
        n = a.get("name") or a.get("filename") or ""
        if n:
            names.append(str(n))
    return ", ".join(names[:10])


def _quick_scan_attachments(attachments: list) -> bool:
    """Pre-scan rapid al conținutului atașamentelor — fără AI, fără OCR.
    Extrage primele 1000 caractere de text din fiecare PDF/imagine și caută
    keywords specifice unui ordin de plată. Fail-safe: returnează False la orice eroare."""
    for a in (attachments or [])[:MAX_ATTACHMENTS]:
        storage_path = a.get("storage_path") or a.get("path") or ""
        mime = a.get("content_type") or a.get("mime") or ""
        path = _host_path(storage_path)
        if not path or not os.path.exists(path):
            continue
        try:
            txt = _doc_text_local(path, mime)
            if not txt:
                continue
            snippet = txt[:1000].lower()
            for kw in _OP_CONTENT_KEYWORDS:
                if kw in snippet:
                    logger.info("op_extractor quick_scan hit kw=%r path=%s", kw, path)
                    return True
        except Exception as e:
            logger.debug("_quick_scan_attachments skip %s: %s", path, e)
    return False


def is_op_email(email: dict, attachments: list = None) -> bool:
    """Returnează True dacă emailul conține un ordin de plată detectat determinist.

    Strategie în ordine crescătoare de cost:
    1. priority_rules.match() pe subiect/body/nume atașament (zero I/O)
       — NB: match() sare peste mailuri cu subiect template CargoTrack (| cargotrack etc.),
         dar noi verificăm body-ul direct în pasul 2 pentru a prinde reply-urile clienților
         pe astfel de thread-uri (ex: "Am achitat conform op").
    2. Body direct cu _PAYPROOF_RE — prinde "am achitat", "am platit", "conform op" etc.
       în reply-ul clientului, chiar dacă subiectul e template CargoTrack.
    3. Serie PPBG/PPCB/PPHU/ASCF/ACTS/ECTS vizibilă direct în body_text.
    4. Pre-scan rapid al conținutului PDF/imagine (pdfplumber local, fără AI).
    """
    try:
        att_str = _attachment_names_str(attachments or [])
        result = priority_rules.match(email, att_names=att_str)
        if result is not None and result.get("id") in {
            priority_rules.RULE_OP,
            priority_rules.RULE_PAYMENT,
            priority_rules.RULE_ATTACHMENT,
        }:
            return True

        # Pasul 2: body direct cu _PAYPROOF_RE (ocolim filtrul automated din match())
        body_full = ((email.get("body_text") or "") + " " + (email.get("body_html") or ""))
        body_fold = priority_rules._fold(body_full)
        for rx in priority_rules._PAYPROOF_RE:
            if rx.search(body_fold):
                logger.info("is_op_email payproof hit in body email=%s", email.get("id"))
                return True

        # Pasul 3: serie de factură vizibilă direct în body_text (PPBG303753, PPCB00123 etc.)
        body_upper = body_full.upper()
        if _extract_series_from_text(body_upper):
            logger.info("is_op_email series hit in body email=%s", email.get("id"))
            return True

        # Pasul 4: conținut PDF/imagine — prinde OP-uri cu subiect gol și fișier numeric
        return _quick_scan_attachments(attachments or [])
    except Exception as e:
        logger.warning("is_op_email failed for email %s: %s", email.get("id"), e)
        return False


# ── Extragere text local din atașament ──────────────────────────────────────

def _doc_text_local(path: str, mime: str) -> str:
    """Extrage text din atașament local (PDF nativ sau OCR imagine).
    Reutilizează librăriile deja disponibile (pdfplumber, PyMuPDF).
    Returnează string gol la eșec — nu aruncă niciodată."""
    if not path or not os.path.exists(path):
        return ""
    mime = (mime or "").lower()
    ext = (os.path.splitext(path)[1] or "").lower()
    is_pdf = ("pdf" in mime) or (ext == ".pdf")
    try:
        if is_pdf:
            txt = ""
            try:
                import pdfplumber
                with pdfplumber.open(path) as pdf:
                    txt = "\n".join((pg.extract_text() or "") for pg in pdf.pages[:5])
            except Exception:
                pass
            if len((txt or "").strip()) < 20:
                try:
                    import fitz
                    d = fitz.open(path)
                    txt = "\n".join(d[i].get_text() for i in range(min(5, d.page_count)))
                    d.close()
                except Exception:
                    pass
            return (txt or "").strip()
        # imagine → OCR rapid (fără multi-PSM, vrem doar seria)
        try:
            import pytesseract
            from PIL import Image
            img = Image.open(path)
            return (pytesseract.image_to_string(img, lang="ron+eng",
                                                config="--psm 6", timeout=25) or "").strip()
        except Exception:
            return ""
    except Exception as e:
        logger.warning("_doc_text_local failed %s: %s", path, e)
        return ""


# ── Extragere text via vision AI ────────────────────────────────────────────

def _attachment_mime(path: str, mime: str) -> str:
    """Detectează MIME-ul real al fișierului din magic bytes."""
    ext = (os.path.splitext(path or "")[1] or "").lower()
    m = (mime or "").lower()
    if "pdf" in m or ext == ".pdf":
        return "application/pdf"
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
        if head[:4] == b'\x89PNG':
            return "image/png"
        if head[:3] == b'\xff\xd8\xff':
            return "image/jpeg"
        if head[:6] in (b'GIF87a', b'GIF89a'):
            return "image/gif"
        if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
            return "image/webp"
    except Exception:
        pass
    if m.startswith("image/"):
        return m
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp"}.get(ext, "image/jpeg")


def _vision_extract_series(path: str, mime: str) -> dict:
    """Trimite atașamentul la vision AI și extrage seria facturii + moneda.
    Returnează {"series": str|None, "currency": str|None}."""
    import base64
    import hashlib
    amime = _attachment_mime(path, mime)
    empty = {"series": None, "currency": None}
    try:
        sz = os.path.getsize(path)
        if sz > VISION_MAX_BYTES:
            logger.info("op_extractor vision skip: file too large (%d bytes) %s", sz, path)
            return empty
        with open(path, "rb") as fh:
            raw = fh.read()
    except Exception as e:
        logger.warning("op_extractor vision read failed %s: %s", path, e)
        return empty

    digest = hashlib.sha1(raw).hexdigest()[:12]
    b64 = base64.b64encode(raw).decode("ascii")
    task = "cargo360:op_series:" + digest

    try:
        res = iris_ai.run_prompt(
            _VISION_OP_SYSTEM, "", response_format="text", model_hint="sonnet",
            temperature=0.0, max_tokens=50, task=task,
            attachments=[{"mime_type": amime, "data_base64": b64}])
        if res and res.get("ok"):
            answer = (res.get("text") or "").strip().upper()
            # Format așteptat: SERIE|MONEDA (ex: PPCB|RON, NONE|MDL)
            parts = answer.split("|", 1)
            raw_series = parts[0].strip() if parts else ""
            raw_currency = parts[1].strip() if len(parts) > 1 else ""
            _ISO_CURRENCIES = {"RON", "EUR", "USD", "MDL", "HUF", "BGN", "PLN", "CZK", "CHF", "GBP", "SEK", "NOK", "DKK"}
            currency = raw_currency if (raw_currency and raw_currency != "NONE"
                                        and re.match(r'^[A-Z]{2,3}$', raw_currency)) else None
            series_invalid = (not raw_series or raw_series == "NONE"
                              or not re.match(r'^[A-Z]{2,6}$', raw_series)
                              or raw_series in _ISO_CURRENCIES
                              or raw_series == raw_currency)
            series = None if series_invalid else raw_series
            return {"series": series, "currency": currency}
    except Exception as e:
        logger.warning("op_extractor vision call failed %s: %s", path, e)
    return empty


# ── Extragere serie + monedă din text ───────────────────────────────────────

def _extract_series_from_text(text: str, known_only: bool = True) -> Optional[str]:
    """Caută prima serie de factură validă în primele 2000 caractere de text.
    Returnează prefixul (ex: 'PPCB') sau None.

    known_only=True (implicit): acceptă DOAR prefixe din allowlist-ul acreditat
    (_KNOWN_SERIE_PREFIXES). Astfel plăcuțele de camion (EWN064, YCE345) sau alte
    progresii LITERE+CIFRE nu mai sunt confundate cu serii de factură.
    known_only=False păstrează comportamentul vechi (orice prefix ne-blacklistat).
    """
    if not text:
        return None
    # Normalizează cratima interioară din serii de tip „P-ECTS" -> „PECTS" înainte de match.
    scan = text[:2000].replace("-", "")
    for m in _SERIE_RE.finditer(scan):
        prefix = m.group(1)
        if prefix in _SERIE_BLACKLIST:
            continue
        if known_only:
            if prefix in _KNOWN_SERIE_PREFIXES:
                return prefix
            continue
        return prefix
    return None


def _is_mdl_currency(text: str) -> bool:
    """Returnează True dacă textul conține indicatori de monedă MDL (lei moldovenești)."""
    if not text:
        return False
    return bool(_MDL_RE.search(text[:3000]))


# ── Determinare departament din serie ────────────────────────────────────────

def _department_from_series(series: Optional[str]) -> str:
    """Mapează seria facturii la departament — decizie STRICTĂ pe allowlist acreditat.

    - fără serie → suport_1
    - serie în _RECUPERARE_TVA_PREFIXES (FS) → recuperare_tva
    - serie în _SUPORT1_PREFIXES (PPCB/PPHU/PPBG/ASCF/PPCF) → suport_1
    - serie acreditată (în _KNOWN_SERIE_PREFIXES) → contabilitate
    - serie detectată dar NE-acreditată (plăcuță camion, gunoi) → suport_1 (NU contabilitate)
    """
    if not series:
        return "suport_1"
    s = series.upper().replace("-", "")
    if s in _RECUPERARE_TVA_PREFIXES:
        return "recuperare_tva"
    if s in _SUPORT1_PREFIXES:
        return "suport_1"
    if s in _KNOWN_SERIE_PREFIXES:
        return "contabilitate"
    return "suport_1"


# ── Extragere serie pentru un email ─────────────────────────────────────────

def extract_op_series(email_id: int) -> dict:
    """Încearcă extragerea seriei OP — mai întâi din subiect/body, apoi din atașamente.

    Strategia:
    0. Subiect + body_text: regex direct (zero cost, zero latență)
    1. Per atașament: text local (pdfplumber / PyMuPDF / OCR)
    2. Dacă text ≥ 10 caractere → regex
    3. Altfel → vision AI (model multimodal)

    Returnează {"series": str|None, "department": "suport_1"|"contabilitate"}.
    """
    db = SessionLocal()
    try:
        email_row = db.execute(
            text("SELECT subject, body_text FROM emails WHERE id=:id"),
            {"id": email_id}
        ).fetchone()
        rows = db.execute(
            text("SELECT name, content_type, storage_path FROM attachments "
                 "WHERE email_id=:id ORDER BY id LIMIT :lim"),
            {"id": email_id, "lim": MAX_ATTACHMENTS}
        ).fetchall()
    except Exception as e:
        logger.warning("op_extractor fetch failed email %d: %s", email_id, e)
        db.close()
        return {"series": None, "department": "suport_1"}
    finally:
        db.close()

    # Pasul 0: caută seria + MDL în subiect + body (cel mai rapid, zero cost)
    if email_row:
        subject = email_row._mapping.get("subject") or ""
        body = email_row._mapping.get("body_text") or ""
        text_from_email = subject + " " + body
        if _is_mdl_currency(text_from_email):
            logger.info("op_extractor email=%d MDL detected in subject/body → contabilitate", email_id)
            series = _extract_series_from_text(text_from_email.upper())
            return {"series": series, "department": "contabilitate", "currency": "MDL"}
        series = _extract_series_from_text(text_from_email.upper())
        if series:
            logger.info("op_extractor email=%d series=%s (subject/body)", email_id, series)
            return {"series": series, "department": _department_from_series(series)}

    for row in rows:
        storage_path = row._mapping.get("storage_path") or ""
        mime = row._mapping.get("content_type") or ""
        name = row._mapping.get("name") or ""

        path = _host_path(storage_path)
        if not path:
            continue

        # Pasul 1: text local
        txt = _doc_text_local(path, mime)
        series = None

        if len(txt.strip()) >= 10:
            # MDL în text local → contabilitate imediat
            if _is_mdl_currency(txt):
                logger.info("op_extractor email=%d att=%s MDL detected (text) → contabilitate", email_id, name)
                series = _extract_series_from_text(txt)
                return {"series": series, "department": "contabilitate", "currency": "MDL"}
            # Pasul 2: regex serie pe text local
            series = _extract_series_from_text(txt)
            if series:
                logger.info("op_extractor email=%d att=%s series=%s (text)", email_id, name, series)
                return {"series": series, "department": _department_from_series(series)}

        # Pasul 3: vision AI (doar dacă text local nu a dat rezultat)
        vision_result = _vision_extract_series(path, mime)
        v_series = vision_result.get("series")
        v_currency = vision_result.get("currency")
        if v_currency == "MDL":
            logger.info("op_extractor email=%d att=%s MDL detected (vision) → contabilitate", email_id, name)
            return {"series": v_series, "department": "contabilitate", "currency": "MDL"}
        if v_series:
            logger.info("op_extractor email=%d att=%s series=%s (vision)", email_id, name, v_series)
            return {"series": v_series, "department": _department_from_series(v_series)}

    logger.info("op_extractor email=%d no series found → suport_1 fallback", email_id)
    return {"series": None, "department": "suport_1"}


# ── Cine bate extragerea de serie ────────────────────────────────────────────
# Extragerea ruleaza ASINCRON (worker), deci scrie ai_department DUPA pipeline. Fara garda de
# mai jos ar calca doua decizii mai puternice decat o serie ghicita dintr-un document:
#   - corectia manuala a operatorului (`ai_department_manual`);
#   - o regula determinista de departament (`department_rules`, model='rule') — reguli scrise
#     explicit de business, ex. „noreply@cargotrack.ro -> Suport 1 obligatoriu".
# Restul (AI, fallback) raman suprascrise de serie, ca pana acum.
_DEPT_PINNED_SQL = ("(ai_department_manual IS TRUE OR "
                    "ai_department_result->>'model' = 'rule')")


# ── Worker periodic ──────────────────────────────────────────────────────────

def advance_op_extract_batch(limit: int = 20) -> dict:
    """Preia emailurile în așteptare pentru extragere serie OP și le procesează.

    Pentru fiecare email cu queue_status='pending_op_extract':
    - Extrage seria din atașament
    - Setează ai_department + ai_op_series
    - Avansează la ready_for_cts
    - La MAX_EXTRACT_ATTEMPTS încercări eșuate → fallback suport_1 + ready_for_cts
    """
    from app.services.process_email import _conn, _set_queue, _has_queue_cols

    results = {"processed": 0, "series_found": 0, "fallback": 0, "error": 0}

    with _conn() as conn:
        cur = conn.cursor()
        if not _has_queue_cols(cur):
            return results
        cur.execute(
            "SELECT id FROM emails "
            "WHERE status='clean' AND queue_status='pending_op_extract' "
            "ORDER BY ai_op_extract_attempts ASC NULLS FIRST, received_at DESC "
            "LIMIT %s",
            (limit,)
        )
        ids = [r[0] for r in cur.fetchall()]

    for eid in ids:
        try:
            _process_one_op(eid, results)
        except Exception as e:
            logger.exception("advance_op_extract_batch error email %d: %s", eid, e)
            results["error"] += 1

    return results


def _process_one_op(email_id: int, results: dict):
    """Procesează un singur email pending_op_extract."""
    from app.services.process_email import _conn, _set_queue

    with _conn() as conn:
        cur = conn.cursor()

        # Citim attempts curent
        cur.execute("SELECT ai_op_extract_attempts FROM emails WHERE id=%s", (email_id,))
        row = cur.fetchone()
        if not row:
            return
        attempts = (row[0] or 0)

        if attempts >= MAX_EXTRACT_ATTEMPTS:
            # Fallback: prea multe încercări → suport_1 (fără să calce o decizie mai tare)
            cur.execute(
                "UPDATE emails SET ai_department=CASE WHEN " + _DEPT_PINNED_SQL +
                " THEN ai_department ELSE 'suport_1' END, ai_op_extract_at=NOW() WHERE id=%s",
                (email_id,)
            )
            _set_queue(cur, email_id, 'ready_for_cts')
            conn.commit()
            results["fallback"] += 1
            logger.info("op_extractor email=%d max attempts → fallback suport_1", email_id)
            return

        # Incrementăm attempts înainte de procesare (protecție la crash)
        cur.execute(
            "UPDATE emails SET ai_op_extract_attempts=COALESCE(ai_op_extract_attempts,0)+1 WHERE id=%s",
            (email_id,)
        )
        conn.commit()

    # Extragere (fără să ținem conexiunea deschisă — poate dura)
    result = extract_op_series(email_id)
    series = result.get("series")
    department = result.get("department", "suport_1")
    currency = result.get("currency")

    with _conn() as conn:
        cur = conn.cursor()
        # Considerăm "rezolvat" dacă avem serie SAU dacă MDL a forțat departamentul
        resolved = bool(series) or (currency == "MDL")
        if resolved:
            # Seria se salveaza MEREU (e un fapt extras din document). Departamentul se scrie
            # doar daca nu e deja fixat de o decizie mai tare — vezi `_DEPT_PINNED_SQL`.
            cur.execute(
                "UPDATE emails SET ai_department=CASE WHEN " + _DEPT_PINNED_SQL +
                " THEN ai_department ELSE %s END, "
                "ai_op_series=%s, ai_op_extract_at=NOW() WHERE id=%s",
                (department, series, email_id)
            )
            _set_queue(cur, email_id, 'ready_for_cts')
            conn.commit()
            results["series_found"] += 1
            results["processed"] += 1
            # Prioritatea a fost calculata SINCRON in pipeline, cand ai_op_series era inca NULL.
            # Acum ca seria e persistata, regula pay_op_series poate lovi -> P2 (plata).
            _recalc_priority_after_op(conn, cur, email_id)
        else:
            # Serie negăsită la această tentativă — rămâne pending_op_extract pentru retry
            # dacă mai are attempts disponibile; altfel va prinde fallback la runda viitoare
            cur.execute(
                "UPDATE emails SET ai_op_extract_at=NOW() WHERE id=%s",
                (email_id,)
            )
            conn.commit()
            results["processed"] += 1


def _recalc_priority_after_op(conn, cur, email_id: int) -> None:
    """Recalculeaza prioritatea dupa ce seria OP a fost persistata pe email.

    Fara asta, emailurile cu ordin de plata in atasament raman pe prioritatea calculata
    inainte de extragere (tipic P4/P5 ghicit din numele fisierului), desi regula
    pay_op_series le-ar incadra corect pe P2.

    Cost AI zero: regula determinista intoarce inainte de orice apel la model.
    Corectiile manuale sunt protejate in _maybe_classify_priority (ai_priority_manual).
    Import LAZY — acelasi pattern ca in advance_op_extract_batch (evita ciclul de import).
    Best-effort: o eroare aici NU trebuie sa strice extragerea de serie, deja commit-uita.
    """
    try:
        from app.services.process_email import _maybe_classify_priority
        cur.execute(
            "SELECT id, subject, from_address, from_name, body_text, body_html, "
            "conversation_id, received_at, ai_op_series FROM emails WHERE id=%s",
            (email_id,)
        )
        row = cur.fetchone()
        if not row:
            return
        em = dict(row) if isinstance(row, dict) else dict(zip([d[0] for d in cur.description], row))
        _maybe_classify_priority(cur, conn, email_id, em)
        logger.info("op_extractor email=%d priority recalculated after series persist", email_id)
    except Exception:
        logger.exception("op_extractor: recalc priority failed email=%d", email_id)
