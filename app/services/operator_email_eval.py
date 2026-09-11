# -*- coding: utf-8 -*-
"""Analiza Operatori — evaluarea AI a raspunsurilor pe email catre clienti.

Fiecare pereche (mail primit de la client, raspuns trimis de operator) trece prin promptul din
`prompts/emails/operator_eval.txt` si primeste 5 scoruri 1-5, un scor general, lista punctelor
ramase neadresate si pana la 3 sugestii. Rezultatul se scrie in `email_operator_evaluations`.

Sursa promptului e FISIERUL din repo (ca la satisfactia V6), nu o tabela — o singura sursa de
adevar, propagata prin git pe staging/productie. Configul de rulare sta in `settings`, cheia
`emails.operator_eval` (model, paralelism, plafoane) si bate implicitele din cod.

Rularea e LA CERERE (job de fundal pornit din UI), nu automata: fiecare pereche e un apel la
gateway-ul AI, partajat cu clasificarea mailurilor, scorarea apelurilor si satisfactia.

── Trei capcane ale modelului de date, ocolite aici ────────────────────────────────────────────
1. `cts_ground_truth.email_id` e NULL pe randurile `sent` — ingeram doar Inbox-ul, deci mailul
   nostru trimis nu are rand local. Un `JOIN emails ON e.id = gt.email_id` pe randuri trimise
   intoarce ZERO (vezi `satisfaction_engine.py`, care face exact asta). Mailul clientului se
   gaseste prin imperechere (`pair_received`), nu prin `email_id`.
2. `cts_assignee_email` se scrie DOAR pe randurile `received` (`cts_groundtruth_sync.py`).
   Operatorul care a raspuns se ia de pe tichetul PRIMIT imperecheat.
3. `cts_is_replica` — CTS face un tichet per destinatar. Fara deduplicare pe `message_id`, un mail
   catre 3 colegi tripleaza numaratoarea operatorului.

Corpul raspunsului (`cts_reply_text`) NU e populat sistematic: feed-ul nu-l trimite, se cere la
cerere din gateway (`cts_groundtruth_sync.fetch_email_content`, max 200 id-uri/apel). De aceea
pasul 0 al jobului aduce corpurile lipsa — altfel jobul ar sari tacut peste majoritatea mailurilor.
"""
import json
import logging
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from app.services import iris_ai

logger = logging.getLogger("mailguard.operator_eval")

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "emails" / "operator_eval.txt"

# Cheie noua de advisory lock — 778231 (documente), 778240 (apeluri), 778251 (IRIS DV) sunt luate.
LOCK_KEY = 778262

_DEFAULTS = {
    "model_hint": "claude-sonnet-4-6",
    "max_workers": 4,
    "max_per_run": 300,
    "min_reply_chars": 80,
    "allow_subject_match": True,
    "prompt_version": "v1",
}
MAX_WORKERS_CAP = 8
CONTENT_CAP = 14000      # per text trimis modelului (mail client / raspuns)
FETCH_CHUNK = 200        # limita gateway-ului /cts/email-content

_prompt_cache = None


# --------------------------------------------------------------------------- #
# Config + prompt
# --------------------------------------------------------------------------- #
def load_prompt() -> str:
    """Promptul, citit o singura data din repo. Fisierul lipsa = eroare explicita, nu prompt gol."""
    global _prompt_cache
    if _prompt_cache is None:
        _prompt_cache = PROMPT_PATH.read_text(encoding="utf-8")
    return _prompt_cache


def load_config(db) -> dict:
    """`settings['emails.operator_eval']` peste implicitele din cod. Tiparul `_load_v6_config`."""
    cfg = dict(_DEFAULTS)
    try:
        row = db.execute(text("SELECT value FROM settings WHERE key='emails.operator_eval'")).fetchone()
        raw = row[0] if row else None
        if isinstance(raw, str):
            raw = json.loads(raw)
        for k, v in (raw or {}).items():
            if k not in _DEFAULTS:
                continue
            if isinstance(_DEFAULTS[k], bool):
                cfg[k] = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "da", "yes", "on")
            elif isinstance(_DEFAULTS[k], int):
                cfg[k] = int(v)
            else:
                cfg[k] = str(v)
    except Exception:
        logger.warning("config emails.operator_eval necitit — se folosesc implicitele", exc_info=True)
    cfg["max_workers"] = max(1, min(MAX_WORKERS_CAP, int(cfg["max_workers"])))
    return cfg


# --------------------------------------------------------------------------- #
# Imperechere raspuns -> mail primit (sursa unica; `cts_training.py` importa de aici)
# --------------------------------------------------------------------------- #
_SEL_EMAIL = ("SELECT id, subject, from_address, from_name, received_at, body_text, body_html, "
              "ai_autoreply, ai_autoreply_confidence, ai_autoreply_status FROM emails ")


def norm_subject(s):
    """Subiect fara prefixele Re:/Fwd:/Fw:/R: (oricate, limbile uzuale)."""
    s = (s or "").strip()
    prev = None
    while s and s != prev:
        prev = s
        s = re.sub(r'^\s*(re|r|fw|fwd|rspns|răspuns|raspuns)\s*[:\-]\s*', '', s, flags=re.I)
    return s.lower().strip()


def pair_received(db, msid, to_email, title, ref_at, allow_subject_match=True):
    """Mailul PRIMIT caruia i s-a raspuns. Returneaza (row_mapping, match_by) sau (None, None).

    (1) exact pe Message-ID: `raw->'extra'->>'msid'` de pe randul TRIMIS = Message-ID-ul mailului
        primit, cautat in `emails.email_headers->>'message_id'`;
    (2) euristic: expeditor = destinatarul reply-ului, subiect normalizat egal, primit INAINTE de
        trimitere (cel mai recent dinainte, din ultimele 25).

    Euristica (2) poate gresi (doua fire cu acelasi subiect de la acelasi client), de aceea
    `match_by` se persista si se poate dezactiva din config (`allow_subject_match=false`).
    """
    if msid:
        e = db.execute(text(_SEL_EMAIL +
            "WHERE email_headers->>'message_id' = :m ORDER BY id DESC LIMIT 1"),
            {"m": msid}).mappings().first()
        if e:
            return e, "msid"
    if not allow_subject_match:
        return None, None
    to_email = (to_email or "").strip().lower()
    norm = norm_subject(title)
    if to_email and norm:
        cands = db.execute(text(_SEL_EMAIL +
            "WHERE lower(from_address) = :to AND received_at IS NOT NULL "
            "  AND (CAST(:ref AS timestamptz) IS NULL OR received_at <= CAST(:ref AS timestamptz)) "
            "ORDER BY received_at DESC LIMIT 25"),
            {"to": to_email, "ref": ref_at}).mappings().all()
        for e in cands:
            if norm_subject(e["subject"]) == norm:
                return e, "subject"
    return None, None


# --------------------------------------------------------------------------- #
# Selectia perechilor
# --------------------------------------------------------------------------- #
# Randurile TRIMISE din fereastra, deduplicate pe mail (CTS face un tichet per destinatar).
# `ref_at` = momentul raspunsului. Operatorul si clientul se rezolva pe tichetul PRIMIT, mai jos.
_CANDIDATES_SQL = """
WITH sent AS (
    SELECT DISTINCT ON (COALESCE(g.message_id, 'tid:' || COALESCE(g.cts_ticket_id::text, g.id::text)))
           g.id, g.message_id, g.cts_ticket_id, g.cts_reply_text, g.cts_reply_html,
           g.raw->'extra'->>'msid'       AS msid,
           g.raw->'extra'->>'to_email'   AS to_email,
           g.raw->'extra'->>'title'      AS title,
           g.raw->'extra'->>'client_id'  AS cts_client_id,
           COALESCE(g.cts_reply_at, g.cts_solved_at, g.fetched_at) AS ref_at
      FROM cts_ground_truth g
     WHERE COALESCE(g.cts_direction, 'received') = 'sent'
       AND g.cts_deleted_at IS NULL
       AND COALESCE(g.cts_reply_at, g.cts_solved_at, g.fetched_at) >= CAST(:df AS timestamptz)
       AND COALESCE(g.cts_reply_at, g.cts_solved_at, g.fetched_at) <  CAST(:dt AS timestamptz)
     ORDER BY COALESCE(g.message_id, 'tid:' || COALESCE(g.cts_ticket_id::text, g.id::text)),
              g.id ASC
)
SELECT s.* FROM sent s
 WHERE NOT EXISTS (SELECT 1 FROM email_operator_evaluations ev WHERE ev.cts_gt_id = s.id {force})
 ORDER BY s.ref_at DESC
 LIMIT :lim
"""


def _candidates(db, date_from, date_to, limit, force=False):
    sql = _CANDIDATES_SQL.format(force="AND FALSE" if force else "")
    return db.execute(text(sql), {"df": date_from, "dt": date_to, "lim": limit}).mappings().all()


# Tichetul PRIMIT (acelasi `message_id`) — de acolo vin operatorul, clientul si marcajul de
# auto-reply. Randul trimis nu are assignee; `raw->'extra'->>'msid'` leaga cele doua tichete.
_RECEIVED_TICKET_SQL = """
SELECT g.id, g.cts_assignee_email, g.cts_assignee_name, g.cts_solved_auto_reply,
       g.raw->'extra'->>'client_id' AS cts_client_id
  FROM cts_ground_truth g
 WHERE COALESCE(g.cts_direction, 'received') = 'received'
   AND g.message_id = :mid
 ORDER BY g.id ASC LIMIT 1
"""

# Operator: adresa CTS -> angajat. NICIODATA pe nume. Departamentul e cel ISTORIC, la data
# raspunsului (`employee_department_history`), nu cel de azi — un om mutat intre timp trebuie sa-si
# pastreze evaluarile in departamentul in care lucra atunci. `enabled` nu se filtreaza: e o
# proprietate a lui azi, iar cine a plecat isi pastreaza lunile trecute.
_EMPLOYEE_SQL = """
SELECT e.id, e.name, e.email,
       (SELECT h.department FROM employee_department_history h
         WHERE h.employee_id = e.id
           AND h.valid_from <= CAST(:day AS date)
           AND (h.valid_to IS NULL OR h.valid_to > CAST(:day AS date))
         ORDER BY h.valid_from DESC LIMIT 1) AS dept_at
  FROM employee_department_mapping e
 WHERE lower(e.email) = lower(:addr)
 LIMIT 1
"""

# Clientul: ID-ul IRIS din CTS e autoritativ; `emails.client_id` e deductia locala pe adresa.
# `raw->'extra'->>'client_id'` e TEXT, `clients.iris_client_id` e bigint — comparatia se face pe
# text (ca in `clients.py`), nu prin cast la bigint: un `client_id` ne-numeric din feed ar arunca
# eroare de cast si ar doborî evaluarea intregii perechi.
_CLIENT_BY_IRIS_SQL = ("SELECT id, name, COALESCE(productivity_exclude, FALSE) AS excluded "
                       "FROM clients WHERE iris_client_id IS NOT NULL "
                       "  AND iris_client_id::text = :cid LIMIT 1")
_CLIENT_BY_ID_SQL = ("SELECT id, name, COALESCE(productivity_exclude, FALSE) AS excluded "
                     "FROM clients WHERE id = :cid LIMIT 1")


# --------------------------------------------------------------------------- #
# Aducerea corpurilor lipsa din gateway
# --------------------------------------------------------------------------- #
def fetch_missing_bodies(db, rows) -> dict:
    """Aduce `cts_reply_text` pentru randurile care nu-l au si il scrie inapoi in DB.

    Corpul NU vine in feed — se cere per tichet din gateway-ul CTS. Fara pasul asta, jobul ar
    marca drept `no_reply_text` majoritatea raspunsurilor, doar pentru ca nimeni nu le-a deschis
    inca in UI. Best-effort: o eroare de gateway lasa randurile fara text, nu opreste jobul.
    """
    from app.services import cts_groundtruth_sync as SYNC
    need = [r for r in rows if not (r["cts_reply_text"] or r["cts_reply_html"]) and r["cts_ticket_id"]]
    out = {"requested": 0, "fetched": 0, "errors": 0}
    if not need:
        return out
    by_log = {str(r["cts_ticket_id"]): r["id"] for r in need}
    ids = list(by_log.keys())
    out["requested"] = len(ids)
    for i in range(0, len(ids), FETCH_CHUNK):
        chunk = ids[i:i + FETCH_CHUNK]
        try:
            items = SYNC.fetch_email_content(chunk) or {}
        except Exception:
            logger.warning("fetch_email_content a esuat pentru %d id-uri", len(chunk), exc_info=True)
            out["errors"] += len(chunk)
            continue
        for lid, rec in items.items():
            body = (rec or {}).get("reply_text")
            gt_id = by_log.get(str(lid))
            if not body or not gt_id:
                continue
            db.execute(text("UPDATE cts_ground_truth SET cts_reply_text = :b WHERE id = :id"),
                       {"b": body[:300000], "id": gt_id})
            out["fetched"] += 1
        db.commit()
    return out


# --------------------------------------------------------------------------- #
# Curatarea textelor
# --------------------------------------------------------------------------- #
def _clean_body(body_text, body_html):
    """Text curat pentru model: fara HTML, fara istoricul citat.

    Reutilizeaza `category_classifier._email_body`, care la randul lui foloseste quote-stripper-ul
    din `phishing_detector._new_content` (RO+EN) si cade pe corpul integral daca taierea ar lasa
    aproape nimic. Fara taierea istoricului, modelul ar evalua mailul clientului (citat sub
    raspuns) ca si cum ar fi scris de operator.
    """
    try:
        from app.services import category_classifier as CC
        txt = CC._email_body({"body_text": body_text or "", "body_html": body_html or ""})
    except Exception:
        logger.warning("curatare corp esuata — se foloseste textul brut", exc_info=True)
        txt = (body_text or "") or (body_html or "")
    return (txt or "").strip()[:CONTENT_CAP]


# --------------------------------------------------------------------------- #
# Evaluarea unei perechi
# --------------------------------------------------------------------------- #
def _as_int_score(v):
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 5 else None


def _as_list(v, cap=None):
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    out = [str(x).strip() for x in v if str(x or "").strip()]
    return out[:cap] if cap else out


_CRIT_KEYS = (
    ("corectitudine_lingvistica", "s_lingvistic"),
    ("ton_si_adresare", "s_ton"),
    ("claritate_si_structura", "s_claritate"),
    ("acoperire_sesizare", "s_acoperire"),
    ("empatie_si_solutie", "s_empatie"),
)


def evaluate_pair(email_client: str, raspuns_angajat: str, cfg: dict) -> dict:
    """Un apel AI. Returneaza {"ok": bool, "data": {...}} — NU ridica niciodata exceptii."""
    try:
        system = load_prompt().replace("{email_client}", email_client or "") \
                              .replace("{raspuns_angajat}", raspuns_angajat or "")
        res = iris_ai.run_prompt(
            system=system,
            content="Evalueaza perechea de mai sus si raspunde strict in formatul JSON cerut.",
            response_format="json",
            model_hint=cfg.get("model_hint") or None,
            temperature=0.0,
            max_tokens=1800,
            client="Cargo360-OperatorEval",
            task="operator_email_eval",
            no_cache=True)
    except Exception:
        logger.exception("apel AI esuat (exceptie neasteptata)")
        return {"ok": False, "error": "exception"}

    if not res.get("ok"):
        return {"ok": False, "error": ((res.get("error") or {}).get("code") or "ai_error"),
                "model": res.get("model")}
    parsed = res.get("parsed")
    if not isinstance(parsed, dict):
        return {"ok": False, "error": "unparsable", "model": res.get("model")}

    crit = parsed.get("criterii") if isinstance(parsed.get("criterii"), dict) else {}
    scores = {}
    for key, col in _CRIT_KEYS:
        item = crit.get(key)
        val = item.get("scor") if isinstance(item, dict) else item
        scores[col] = _as_int_score(val)

    have = [v for v in scores.values() if v is not None]
    general = parsed.get("scor_general")
    try:
        general = round(float(general), 1)
    except (TypeError, ValueError):
        # Modelul nu a dat media (sau a dat text) — o calculam noi din criteriile valide.
        general = round(sum(have) / len(have), 1) if have else None

    return {"ok": True, "model": res.get("model"), "data": {
        **scores,
        "score_general": general,
        "puncte_neadresate": _as_list(parsed.get("puncte_neadresate")),
        "sugestii": _as_list(parsed.get("sugestii"), cap=3),
        "criterii": crit,
        "mentiune": parsed.get("mentiune") or None,
    }}


# --------------------------------------------------------------------------- #
# Context per pereche (fara AI) — folosit si de /coverage
# --------------------------------------------------------------------------- #
def build_context(db, row, cfg) -> dict:
    """Rezolva mailul primit, operatorul, clientul si textele. Fara niciun apel AI.

    Returneaza dict cu `skip` setat cand perechea nu se poate evalua — motivul se persista, ca
    randul sa nu fie reincercat la fiecare rulare.
    """
    ctx = {"cts_gt_id": row["id"], "cts_ticket_id": row["cts_ticket_id"],
           "message_id": row["message_id"], "reply_at": row["ref_at"],
           "employee_id": None, "employee_email": None, "employee_name": None,
           "department": None, "client_id": None, "client_name": None,
           "received_email_id": None, "match_by": None, "skip": None,
           "client_text": None, "reply_text": None}

    reply = _clean_body(row["cts_reply_text"], row["cts_reply_html"])
    if not reply:
        ctx["skip"] = "no_reply_text"
        return ctx

    paired, match_by = pair_received(db, row["msid"], row["to_email"], row["title"], row["ref_at"],
                                     allow_subject_match=bool(cfg.get("allow_subject_match", True)))
    if paired is None:
        ctx["skip"] = "no_pair"
        return ctx
    ctx["received_email_id"] = paired["id"]
    ctx["match_by"] = match_by

    # Tichetul primit: operatorul + marcajul de auto-reply.
    rec = None
    if row["message_id"]:
        rec = db.execute(text(_RECEIVED_TICKET_SQL), {"mid": row["message_id"]}).mappings().first()
    if rec and rec["cts_solved_auto_reply"]:
        # Raspuns generat automat la marcarea „solved" — nu e textul unui om, nu se evalueaza.
        ctx["skip"] = "auto_reply"
        return ctx

    asg = (rec["cts_assignee_email"] if rec else None) or None
    if asg:
        day = (row["ref_at"] or datetime.now(timezone.utc)).date().isoformat()
        emp = db.execute(text(_EMPLOYEE_SQL), {"addr": asg, "day": day}).mappings().first()
        ctx["employee_email"] = asg
        ctx["employee_name"] = (rec["cts_assignee_name"] if rec else None)
        if emp:
            ctx["employee_id"] = emp["id"]
            ctx["employee_name"] = emp["name"] or ctx["employee_name"]
            ctx["department"] = emp["dept_at"]

    cid = (row["cts_client_id"] or (rec["cts_client_id"] if rec else None))
    cl = None
    if cid:
        cl = db.execute(text(_CLIENT_BY_IRIS_SQL), {"cid": str(cid)}).mappings().first()
    if cl is None and paired.get("id"):
        loc = db.execute(text("SELECT client_id FROM emails WHERE id = :id"),
                         {"id": paired["id"]}).scalar()
        if loc:
            cl = db.execute(text(_CLIENT_BY_ID_SQL), {"cid": loc}).mappings().first()
    if cl is not None:
        if cl["excluded"]:
            ctx["skip"] = "client_excluded"
            return ctx
        ctx["client_id"] = cl["id"]
        ctx["client_name"] = cl["name"]

    client_text = _clean_body(paired["body_text"], paired["body_html"])
    if not client_text:
        ctx["skip"] = "no_pair"
        return ctx

    if len(reply) < int(cfg.get("min_reply_chars", 80)):
        ctx["skip"] = "too_short"
        return ctx

    ctx["client_text"] = client_text
    ctx["reply_text"] = reply
    return ctx


# --------------------------------------------------------------------------- #
# Scriere
# --------------------------------------------------------------------------- #
_INSERT_SQL = """
INSERT INTO email_operator_evaluations
    (cts_gt_id, cts_ticket_id, message_id, reply_at, employee_id, employee_email, employee_name,
     department, client_id, client_name, received_email_id, match_by,
     score_general, s_lingvistic, s_ton, s_claritate, s_acoperire, s_empatie,
     puncte_neadresate, sugestii, criterii, mentiune, skipped_reason, model, prompt_version)
VALUES
    (:cts_gt_id, :cts_ticket_id, :message_id, :reply_at, :employee_id, :employee_email,
     :employee_name, :department, :client_id, :client_name, :received_email_id, :match_by,
     :score_general, :s_lingvistic, :s_ton, :s_claritate, :s_acoperire, :s_empatie,
     CAST(:puncte AS jsonb), CAST(:sugestii AS jsonb), CAST(:criterii AS jsonb),
     :mentiune, :skipped_reason, :model, :prompt_version)
ON CONFLICT (cts_gt_id) DO UPDATE SET
    reply_at = EXCLUDED.reply_at, employee_id = EXCLUDED.employee_id,
    employee_email = EXCLUDED.employee_email, employee_name = EXCLUDED.employee_name,
    department = EXCLUDED.department, client_id = EXCLUDED.client_id,
    client_name = EXCLUDED.client_name, received_email_id = EXCLUDED.received_email_id,
    match_by = EXCLUDED.match_by, score_general = EXCLUDED.score_general,
    s_lingvistic = EXCLUDED.s_lingvistic, s_ton = EXCLUDED.s_ton,
    s_claritate = EXCLUDED.s_claritate, s_acoperire = EXCLUDED.s_acoperire,
    s_empatie = EXCLUDED.s_empatie, puncte_neadresate = EXCLUDED.puncte_neadresate,
    sugestii = EXCLUDED.sugestii, criterii = EXCLUDED.criterii, mentiune = EXCLUDED.mentiune,
    skipped_reason = EXCLUDED.skipped_reason, model = EXCLUDED.model,
    prompt_version = EXCLUDED.prompt_version, evaluated_at = now()
"""


def _persist(db, ctx, evaluation, cfg):
    data = (evaluation or {}).get("data") or {}
    db.execute(text(_INSERT_SQL), {
        "cts_gt_id": ctx["cts_gt_id"], "cts_ticket_id": ctx["cts_ticket_id"],
        "message_id": ctx["message_id"], "reply_at": ctx["reply_at"],
        "employee_id": ctx["employee_id"], "employee_email": ctx["employee_email"],
        "employee_name": ctx["employee_name"], "department": ctx["department"],
        "client_id": ctx["client_id"], "client_name": ctx["client_name"],
        "received_email_id": ctx["received_email_id"], "match_by": ctx["match_by"],
        "score_general": data.get("score_general"),
        "s_lingvistic": data.get("s_lingvistic"), "s_ton": data.get("s_ton"),
        "s_claritate": data.get("s_claritate"), "s_acoperire": data.get("s_acoperire"),
        "s_empatie": data.get("s_empatie"),
        "puncte": json.dumps(data.get("puncte_neadresate") or [], ensure_ascii=False),
        "sugestii": json.dumps(data.get("sugestii") or [], ensure_ascii=False),
        "criterii": json.dumps(data.get("criterii") or {}, ensure_ascii=False),
        "mentiune": data.get("mentiune"),
        "skipped_reason": ctx.get("skip"),
        "model": (evaluation or {}).get("model") or (cfg.get("model_hint") if not ctx.get("skip") else None),
        "prompt_version": cfg.get("prompt_version"),
    })


# --------------------------------------------------------------------------- #
# Estimare (fara AI) — ce ar face o rulare
# --------------------------------------------------------------------------- #
def coverage(db, date_from, date_to, limit=None) -> dict:
    """Cate raspunsuri sunt in fereastra si cate s-ar evalua efectiv. ZERO apeluri AI.

    Se apeleaza inainte de rulare, ca operatorul sa vada costul (un apel AI per pereche) inainte
    sa-l plateasca. Nu aduce corpurile din gateway — doar le numara pe cele lipsa.
    """
    cfg = load_config(db)
    lim = int(limit or cfg["max_per_run"])
    rows = _candidates(db, date_from, date_to, lim)
    total = db.execute(text("""
        SELECT count(DISTINCT COALESCE(g.message_id, 'tid:' || COALESCE(g.cts_ticket_id::text, g.id::text)))
          FROM cts_ground_truth g
         WHERE COALESCE(g.cts_direction, 'received') = 'sent'
           AND g.cts_deleted_at IS NULL
           AND COALESCE(g.cts_reply_at, g.cts_solved_at, g.fetched_at) >= CAST(:df AS timestamptz)
           AND COALESCE(g.cts_reply_at, g.cts_solved_at, g.fetched_at) <  CAST(:dt AS timestamptz)
    """), {"df": date_from, "dt": date_to}).scalar() or 0
    already = db.execute(text("""
        SELECT count(*) FROM email_operator_evaluations
         WHERE reply_at >= CAST(:df AS timestamptz) AND reply_at < CAST(:dt AS timestamptz)
    """), {"df": date_from, "dt": date_to}).scalar() or 0
    missing_body = sum(1 for r in rows if not (r["cts_reply_text"] or r["cts_reply_html"]))
    return {"total_replies": int(total), "already_evaluated": int(already),
            "pending": len(rows), "missing_body": missing_body,
            "ai_calls_estimate": len(rows), "limit": lim,
            "model": cfg["model_hint"], "max_workers": cfg["max_workers"]}


# --------------------------------------------------------------------------- #
# Job de fundal
# --------------------------------------------------------------------------- #
_tls = threading.local()
_worker_sessions = []
_worker_sessions_lock = threading.Lock()


def _worker_session():
    """Sesiune proprie per FIR (tiparul `_worker_conn` din satisfaction_snapshot.py).

    O sesiune SQLAlchemy nu se imparte intre fire, iar fiecare pereche face cateva interogari.
    Firele DOAR CITESC — scrierea ramane pe firul principal, ca sa nu tinem o tranzactie deschisa
    cat dureaza apelul AI (secunde bune).
    """
    sess = getattr(_tls, "sess", None)
    if sess is not None:
        return sess
    from app.database import SessionLocal
    sess = SessionLocal()
    _tls.sess = sess
    with _worker_sessions_lock:
        _worker_sessions.append(sess)
    return sess


def _close_worker_sessions():
    with _worker_sessions_lock:
        for s_ in _worker_sessions:
            try:
                s_.close()
            except Exception:
                pass
        _worker_sessions.clear()
    _tls.sess = None


def _job_key(job_id):
    return "operator_eval_job.%s" % job_id


def _write_job(db, job_id, payload):
    db.execute(text(
        "INSERT INTO settings(key, value, updated_by, updated_at) "
        "VALUES(:k, CAST(:v AS jsonb), 'operator_eval', NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"),
        {"k": _job_key(job_id), "v": json.dumps(payload, ensure_ascii=False, default=str)})
    db.commit()


def read_job(db, job_id):
    row = db.execute(text("SELECT value FROM settings WHERE key = :k"),
                     {"k": _job_key(job_id)}).fetchone()
    if not row:
        return None
    val = row[0]
    return json.loads(val) if isinstance(val, str) else val


def run_job(date_from, date_to, limit=None, force=False, job_id=None):
    """Ruleaza evaluarea pe fereastra data. Apelata pe un fir de fundal.

    Pasi: (0) aduce corpurile lipsa din gateway, (1) construieste contextul fiecarei perechi,
    (2) apeleaza AI-ul in paralel, (3) scrie rezultatele pe firul principal.
    """
    from app.database import SessionLocal
    db = SessionLocal()
    job_id = job_id or uuid.uuid4().hex[:12]
    stats = {"job_id": job_id, "status": "running", "scored": 0, "skipped": 0, "errors": 0,
             "total": 0, "started_at": datetime.now(timezone.utc).isoformat(),
             "date_from": str(date_from), "date_to": str(date_to)}
    got_lock = False
    try:
        got_lock = bool(db.execute(text("SELECT pg_try_advisory_lock(:k)"),
                                   {"k": LOCK_KEY}).scalar())
        if not got_lock:
            stats.update(status="skipped", reason="already_running")
            _write_job(db, job_id, stats)
            return stats

        if not iris_ai.is_configured():
            stats.update(status="error", reason="ai_not_configured")
            _write_job(db, job_id, stats)
            return stats

        cfg = load_config(db)
        lim = int(limit or cfg["max_per_run"])
        rows = _candidates(db, date_from, date_to, lim, force=force)
        stats["total"] = len(rows)
        _write_job(db, job_id, stats)
        if not rows:
            stats.update(status="done", finished_at=datetime.now(timezone.utc).isoformat())
            _write_job(db, job_id, stats)
            return stats

        stats["bodies"] = fetch_missing_bodies(db, rows)
        rows = _candidates(db, date_from, date_to, lim, force=force)  # reciteste textele aduse
        _write_job(db, job_id, stats)

        def _work(row):
            """Rulat pe fir: context + apel AI. NU ridica exceptii si NU scrie nimic."""
            try:
                ctx = build_context(_worker_session(), row, cfg)
                if ctx.get("skip"):
                    return ctx, None
                ev = evaluate_pair(ctx["client_text"], ctx["reply_text"], cfg)
                if not ev.get("ok"):
                    ctx["skip"] = "ai_error"
                return ctx, ev
            except Exception:
                logger.exception("evaluare esuata pentru cts_gt_id=%s", row["id"])
                return ({"cts_gt_id": row["id"], "cts_ticket_id": row["cts_ticket_id"],
                         "message_id": row["message_id"], "reply_at": row["ref_at"],
                         "employee_id": None, "employee_email": None, "employee_name": None,
                         "department": None, "client_id": None, "client_name": None,
                         "received_email_id": None, "match_by": None, "skip": "ai_error"}, None)

        try:
            with ThreadPoolExecutor(max_workers=cfg["max_workers"]) as pool:
                futures = [pool.submit(_work, r) for r in rows]
                for fut in as_completed(futures):
                    ctx, ev = fut.result()
                    try:
                        _persist(db, ctx, ev, cfg)
                        db.commit()
                    except Exception:
                        db.rollback()
                        logger.exception("scriere esuata cts_gt_id=%s", ctx.get("cts_gt_id"))
                        stats["errors"] += 1
                        continue
                    if ctx.get("skip") == "ai_error":
                        stats["errors"] += 1
                    elif ctx.get("skip"):
                        stats["skipped"] += 1
                    else:
                        stats["scored"] += 1
                    if (stats["scored"] + stats["skipped"] + stats["errors"]) % 5 == 0:
                        _write_job(db, job_id, stats)
        finally:
            _close_worker_sessions()

        stats.update(status="done", finished_at=datetime.now(timezone.utc).isoformat())
        _write_job(db, job_id, stats)
        return stats
    except Exception as e:
        logger.exception("run_job a esuat")
        try:
            db.rollback()
        except Exception:
            pass
        stats.update(status="error", reason=str(e))
        try:
            _write_job(db, job_id, stats)
        except Exception:
            pass
        return stats
    finally:
        if got_lock:
            try:
                db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
                db.commit()
            except Exception:
                pass
        db.close()


def start_job(date_from, date_to, limit=None, force=False) -> str:
    """Porneste jobul pe un fir daemon si intoarce `job_id` imediat.

    Rularea dureaza minute intregi (un apel AI per pereche) — nu are ce cauta intr-un request HTTP;
    UI-ul urmareste progresul prin `GET .../run/status`.
    """
    job_id = uuid.uuid4().hex[:12]
    threading.Thread(target=run_job, daemon=True,
                     kwargs={"date_from": date_from, "date_to": date_to, "limit": limit,
                             "force": force, "job_id": job_id}).start()
    return job_id
