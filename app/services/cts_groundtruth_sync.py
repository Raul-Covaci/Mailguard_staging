"""Sync ground-truth CTS -> Cargo360 (modul training).

Trage din CTS ADMIN (sincronizat in IRIS) categoria + departamentul setate MANUAL de suport
si reply-ul trimis de colegi ("TRIMIS"), le stocheaza in tabelul `cts_ground_truth` si le leaga
de emailurile Cargo360 dupa MESSAGE_ID (identitatea stabila pe ambele medii — pe staging
ground-truth-ul vine din Cargo360 PROD, unde mg_id-ul e ALTUL, deci NU ne legam pe el).
E sursa de adevar pentru comparatia cu
clasificarile Cargo360 (vezi app/api/v1/cts_training.py) si pt ajustarea prompturilor de
categorie/departament + autoreply. NU atinge fluxul curent, NU re-trimite nimic spre CTS.

DIRECTIE (TRIMIS vs PRIMIT): CTS contine si mailurile pe care le-am TRIMIS noi (reply-urile
colegilor, from=*@cargotrack.ro / Message-ID @cts.cargotrack.ro). Acelea NU sunt incadrari de
mail primit -> NU le clasificam (cts_category/cts_department = NULL) si NU le numaram in statistici;
le pastram cu cts_direction='sent' ca sa se vada reply-ul cu flag TRIMIS. Restul = 'received'.

SPAM: incadrarile CTS de tip SPAM (category_id=7) adauga automat expeditorul in blacklist-ul de
spam (sender_lists, tip='spam') — exact ca actiunea 'mark_spam' din pagina de Spam. Nu le
clasificam pe axa categorie (nu fac parte din taxonomia MG informatie/sesizare/reclamatie).

SCOPE: Cargo360 NU are inca acces la sursa CTS-in-IRIS (cross-app, necesita grant via outbox).
Pana atunci serviciul e INERT: is_enabled() e False si sync_ground_truth() intoarce {ok:False,
reason:...} fara sa atinga nimic. Dupa aprobare, credentialele vin din env (NICIUN secret in cod):
  CTS_GT_DSN    + CTS_GT_QUERY        -> DSN postgres read-only catre DB-ul IRIS cu tabelele CTS
  (sau) CTS_GT_URL + CTS_GT_TOKEN     -> endpoint HTTP read-only
  (sau) IRIS Gateway (X-Mailguard-Key)-> canalul preferat, reutilizeaza cheia Cargo360 existenta
Modulul de comparatie functioneaza independent pe randurile deja stocate (inclusiv fixture).

AUTOMATIZARE (cerinta business owner): inlocuieste verificarea manuala (~20% esantion) cu
comparatie pe 100% din mailuri. Sync-ul ruleaza AUTOMAT din cron (POST /process/run-now, la 5 min)
prin run_recent_if_due(). CTS incadreaza un email la ~1h dupa receptie, iar uneori mai tarziu, deci:
  - fereastra rolling de baza = ultimele RECENT_WINDOW_HOURS (24h);
  - daca exista mailuri inca NEINCADRATE in CTS (categorie SAU departament NULL), fereastra se
    extinde inapoi pana le acopera (plafon RECENT_MAX_BACKFILL_HOURS) -> le re-interogam pana cand
    operatorul uman le seteaza, ca sa prindem 100% din incadrari, nu doar pe cele clasificate rapid.
"""
import os
import json
import logging
import datetime as _dt
import threading
from typing import Dict, Any, Optional, List

from sqlalchemy import text, bindparam
from app.database import SessionLocal
from app.services.department_classifier import DEPT_LABELS, DEPARTMENTS
from app.services.category_classifier import CATEGORIES
from app.services import sender_lists

logger = logging.getLogger("mailguard.cts_gt_sync")

SYNC_ENABLED_KEY = "cts_gt.sync_enabled"
LAST_RECENT_KEY = "cts_gt.last_recent_sync_at"   # throttle pt run_recent_if_due (cron 5 min)
CATEGORY_MAP_KEY = "cts_gt.category_map"          # JSON editabil: {"1":"reclamatie",...} CTS enum -> categorie MG
# Faza 2 (dry-run): reply automat de INCHIDERE la tranzitia unui email in 'solved'. Default TRUE
# (dam date de validare pe trafic real); dezactivabil fara redeploy.
SOLVED_TRIGGER_KEY = "autoreply.solved_trigger_enabled"
SOURCE = "iris_sync"
_recent_lock = threading.Lock()  # impiedica suprapunerea sync-urilor rolling (buton + cron)
GATEWAY_PATH = "/cts/ground-truth"               # endpoint IRIS Gateway (read-only), reutilizeaza X-Mailguard-Key

# Maparea enum-ului CTS category_id -> categoriile MG. Confirmata cu business owner-ul:
#   1=COMPLAINT->reclamatie, 2=REQUEST->sesizare, 6=INFO->informatie (cele mai dese).
#   3=BRIEFING->informatie, 4=NOTICE->sesizare (PROVIZORII, KV-editabile).
#   5=REPLY  -> mail TRIMIS de noi (nu se incadreaza, vezi cts_direction).
#   7=SPAM   -> blacklist (vezi SPAM_CATEGORY_ID), nu se mapeaza pe taxonomia MG.
# Override fara redeploy via KV CATEGORY_MAP_KEY. Cat timp un id NU e in mapa -> "nemapat"
# (NU falsificam potriviri).
_DEFAULT_CTS_CATEGORY_MAP: Dict[str, str] = {
    "1": "reclamatie",
    "2": "sesizare",
    "3": "informatie",
    "4": "sesizare",
    "6": "informatie",
}
SPAM_CATEGORY_ID = 7  # CTS CATEGORY_SPAM: nu se clasifica, expeditorul intra in blacklist

# Domeniile NOASTRE: un mail de la ele (sau cu Message-ID @cts.cargotrack.ro) e TRIMIS de noi.
_INTERNAL_DOMAINS = {"cargotrack.ro", "cts.cargotrack.ro"}
_SENT_MID_HOST = "@cts.cargotrack.ro"

RECENT_WINDOW_HOURS = 24       # fereastra rolling de baza: ce s-a (re)incadrat in CTS in ultimele 24h
RECENT_MIN_INTERVAL_S = 50     # cron ruleaza la 2 min; 50s = niciun tick nu e aruncat de throttle,
                               # deci statusurile CTS ajung in monitor cu maxim ~2 min intarziere
                               # (cerut 2026-08-06: operatorii compara live cu dashboardul CTS)
RECENT_MAX_BACKFILL_HOURS = 168  # plafon: nu cautam mai in urma de 7 zile chiar daca ceva ramane neincadrat
PAGE_BATCH = 2000              # cat tragem per PAGINA in fetch-ul paginat (gateway = ordine crescatoare pe updated_at)
PAGE_MAX_BATCHES = 40          # plafon de siguranta anti-runaway: 40 x 2000 = 80k inregistrari / pass

_CAT_ALIASES = {
    "informatii": "informatie", "information": "informatie", "info": "informatie",
    "complaint": "reclamatie", "reclamatii": "reclamatie",
    "sesizari": "sesizare", "issue": "sesizare",
}
_DEPT_ALIASES = {
    "suport 1": "suport_1", "suport1": "suport_1",
    "suport 2": "suport_2", "suport2": "suport_2",
    "suport 3": "suport_3", "suport3": "suport_3",
    "taxe de drum": "taxe_drum", "taxe drum": "taxe_drum", "taxe": "taxe_drum",
    "accounting": "contabilitate",
    "recuperare tva": "recuperare_tva", "tva": "recuperare_tva",
    "sales": "comercial",
    # Departamente care exista in employee_department_mapping (16) dar NU in DEPT_LABELS (8,
    # lista pe care alege clasificatorul AI). Fara ele, `_map_department` cadea pe fallback si
    # pastra valoarea BRUTA din CTS ("Administrativ", "Product Management", "IT Team 1"), care
    # nu se potrivea cu niciun slug in rapoarte -> volumul lor nu se agrega nicaieri.
    "administrativ": "administrativ",
    "hr": "hr", "resurse umane": "hr",
    "instalari": "instalari", "instalări": "instalari",
    "marketing": "marketing",
    "product management": "product_management",
    "management general": "management_general",
    "operational": "management_operational",
    "management operational": "management_operational",
    "management operațional": "management_operational",
    "account management": "account_management",
    "it": "it", "it team 1": "it", "it team": "it",
}


# ---------------------------------------------------------------- gating

def _kv_enabled() -> bool:
    db = SessionLocal()
    try:
        row = db.execute(text("SELECT value FROM settings WHERE key=:k"),
                         {"k": SYNC_ENABLED_KEY}).fetchone()
    except Exception as e:
        logger.warning("cts_gt _kv_enabled DB failed: %s", e)
        return False
    finally:
        db.close()
    if not row or row[0] is None:
        return False
    return str(row[0]).strip().lower() in ("1", "true", "yes", "on")


def _gateway_configured() -> bool:
    """Canalul preferat: endpoint IRIS Gateway reutilizand cheia Cargo360 existenta (ca iris_sync)."""
    if not os.getenv("IRIS_MAILGUARD_API_KEY"):
        return False
    try:
        from app.config import get_settings
        return bool((get_settings().iris_api_url or "").strip())
    except Exception:
        return False


def _source_configured() -> bool:
    return bool(os.getenv("CTS_GT_DSN")
                or (os.getenv("CTS_GT_URL") and os.getenv("CTS_GT_TOKEN"))
                or _gateway_configured())


def source_mode() -> Optional[str]:
    if os.getenv("CTS_GT_DSN"):
        return "dsn"
    if os.getenv("CTS_GT_URL") and os.getenv("CTS_GT_TOKEN"):
        return "http"
    if _gateway_configured():
        return "gateway"
    return None


def is_enabled() -> bool:
    """Sync activ DOAR daca e bifat in settings SI sursa e configurata (grant primit)."""
    return _kv_enabled() and _source_configured()


def _solved_trigger_enabled(db) -> bool:
    """Faza 2: declansam reply de INCHIDERE (dry-run) la tranzitia in solved? Default TRUE; se poate
    opri prin settings['autoreply.solved_trigger_enabled']=false, fara redeploy. Fail-safe -> True."""
    try:
        row = db.execute(text("SELECT value FROM settings WHERE key=:k"),
                         {"k": SOLVED_TRIGGER_KEY}).fetchone()
    except Exception:
        return True
    if not row or row[0] is None:
        return True
    return str(row[0]).strip().strip('"').lower() not in ("0", "false", "no", "off")


def status() -> Dict[str, Any]:
    """Stare pt UI/diagnostic, fara a expune secrete."""
    cat_map = _load_cat_id_map()
    return {
        "enabled_flag": _kv_enabled(),
        "source_configured": _source_configured(),
        "source_mode": source_mode(),
        "active": is_enabled(),
        "auto_interval_s": RECENT_MIN_INTERVAL_S,
        "window_hours": RECENT_WINDOW_HOURS,
        "max_backfill_hours": RECENT_MAX_BACKFILL_HOURS,
        "category_map": cat_map,
        "category_map_configured": bool(cat_map),
    }


# ---------------------------------------------------------------- mapping

def _load_cat_id_map() -> Dict[str, str]:
    """Mapa CTS category_id -> categorie MG, din KV (CATEGORY_MAP_KEY) peste default. Cheile = string."""
    m = dict(_DEFAULT_CTS_CATEGORY_MAP)
    db = SessionLocal()
    try:
        row = db.execute(text("SELECT value FROM settings WHERE key=:k"),
                         {"k": CATEGORY_MAP_KEY}).fetchone()
    except Exception:
        row = None
    finally:
        db.close()
    if row and row[0] is not None:
        val = row[0]
        try:
            if isinstance(val, str):
                val = json.loads(val)
            if isinstance(val, dict):
                m.update({str(k): str(v) for k, v in val.items()})
        except Exception:
            pass
    return m


def _cat_int(v) -> Optional[int]:
    """Intoarce category_id ca int daca valoarea e numerica (enum CTS), altfel None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return None


def _map_category(v, cat_id_map: Optional[Dict[str, str]] = None) -> (Optional[str], bool):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None, False
    # category_id numeric (enum CTS) -> mapa configurabila
    ci = _cat_int(v)
    if ci is not None:
        key = str(ci)
        m = cat_id_map if cat_id_map is not None else _load_cat_id_map()
        if key in m and m[key] in CATEGORIES:
            return m[key], True
        return "cts_cat:" + key, False  # nemapat: pastram referinta, NU contam ca potrivire
    # valoare text (label/alias)
    n = str(v).strip().lower()
    if n in CATEGORIES:
        return n, True
    if n in _CAT_ALIASES:
        return _CAT_ALIASES[n], True
    return str(v), False


def _map_department(v) -> (Optional[str], bool):
    """Normalizeaza un departament CTS (label SAU slug) la slug-ul canonic local.

    CTS trimite ambele forme: label ("Suport 1") pe unele tichete, slug cu cratima ("suport-2",
    "recuperare-tva", "taxe-de-drum") pe altele. Fara normalizarea cratimei, forma cu cratima cadea
    pe fallback si `cts_department` ramanea NULL/text brut: 23 din 243 de tichete DESCHISE, toate
    fara assignee, deci invizibile in monitor (constatat 2026-08-06, dupa fixul de atribuire).
    """
    n = (str(v) if v is not None else "").strip().lower()
    if not n:
        return None, False
    if n in DEPARTMENTS:
        return n, True
    for slug, label in DEPT_LABELS.items():
        if n == label.lower():
            return slug, True
    if n in _DEPT_ALIASES:
        return _DEPT_ALIASES[n], True
    # Forma slug cu cratime ("suport-2", "recuperare-tva", "taxe-de-drum"). Se incearca atat cu
    # underscore cat si cu spatiu, pentru ca `_DEPT_ALIASES` are chei in ambele stiluri
    # ("taxe de drum", dar si "instalari"/"marketing"/"it" care nu sunt in DEPARTMENTS).
    u = n.replace("-", "_").replace(" ", "_")
    if u in DEPARTMENTS:
        return u, True
    if u in _DEPT_ALIASES:
        return _DEPT_ALIASES[u], True
    sp = u.replace("_", " ")
    if sp in _DEPT_ALIASES:
        return _DEPT_ALIASES[sp], True
    return str(v), False


def _g(rec: Dict[str, Any], *keys):
    for k in keys:
        if k in rec and rec[k] not in (None, ""):
            return rec[k]
    return None


def _email_domain(addr) -> str:
    a = (str(addr) if addr is not None else "").strip().lower()
    # ia prima adresa daca sunt mai multe, scoate <...>
    a = a.replace("<", "").replace(">", "").split(",")[0].strip()
    return a.rsplit("@", 1)[-1] if "@" in a else ""


def _is_sent(from_email, mid) -> bool:
    """TRIMIS de noi daca expeditorul e pe un domeniu intern SAU Message-ID e de pe serverul nostru."""
    if _email_domain(from_email) in _INTERNAL_DOMAINS:
        return True
    if mid and _SENT_MID_HOST in str(mid).lower():
        return True
    return False


def _coerce_ts(v):
    if v is None:
        return None
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def _coerce_pos_int(v):
    """Id CTS (client_id, cts_email_log_id) -> int pozitiv, sau None.
    Tratează 0/""/"0" ca absent (CTS trimite 0 = fără valoare)."""
    if v in (None, "", 0, "0"):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _as_bool(v):
    """Parser tolerant pt flag-ul CTS 'trimite mail automat la solved'. True/False/None.
    Necunoscut / absent -> None (CTS inca nu trimite campul)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().strip('"').lower()
    if s in ("1", "true", "yes", "on", "da", "t", "y"):
        return True
    if s in ("0", "false", "no", "off", "nu", "f", "n"):
        return False
    return None


def _norm_attachments(v) -> Optional[list]:
    """Normalizeaza lista de atasamente din feed -> [{id,name,mime,filesize}]. Continutul (base64)
    NU se stocheaza aici; se aduce la cerere dupa id (ca la /cts/get_email_documents)."""
    if not isinstance(v, list):
        return None
    out = []
    for a in v:
        if not isinstance(a, dict):
            continue
        out.append({
            "id": a.get("id") or a.get("attachment_id"),
            "name": a.get("name") or a.get("filename") or a.get("file_name"),
            "mime": a.get("mime") or a.get("mime_type") or a.get("content_type"),
            "filesize": a.get("filesize") or a.get("size") or a.get("filesize_bytes"),
        })
    return out or None


def _normalize_record(rec: Dict[str, Any], cat_id_map: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    extra = rec.get("extra") if isinstance(rec.get("extra"), dict) else {}
    mid = _g(rec, "message_id", "msid", "internetMessageId", "internet_message_id", "msgid")
    eid = _g(rec, "email_id", "mg_id", "cargo360_id", "id_cargo360")
    from_email = (_g(rec, "from_email", "from", "sender", "from_address")
                  or extra.get("from_email") or extra.get("from"))
    # category_id (enum CTS) e cheia reala; pastram si fallback la label text
    cat_raw = _g(rec, "category_id", "category", "categorie", "cts_category")
    # Departament: pe tichetele mai noi, top-level `department` lipseste si singura sursa e
    # `assignment.department_slug` (prezent chiar si cand tichetul NU are assignee). Fara acest
    # fallback, `cts_department` ramanea NULL pe 23 din 243 de tichete deschise, iar in monitor
    # dispareau complet (nu au nici assignee pe care sa cada atribuirea) — 2026-08-06.
    _asg_dep = rec.get("assignment") if isinstance(rec.get("assignment"), dict) else {}
    dep_raw = (_g(rec, "department", "departament", "cts_department")
               or _asg_dep.get("department_slug") or _asg_dep.get("department_label"))
    reply = _g(rec, "reply_text", "reply", "raspuns", "cts_reply_text", "response")
    reply_html = _g(rec, "reply_html", "body_html", "html", "cts_reply_html")
    reply_at = _g(rec, "reply_at", "solved_at", "sent_at", "data_raspuns", "cts_reply_at")
    cstatus = _g(rec, "status", "stare", "cts_status")
    # Faza 2: optiunea operatorului din CTS "trimite mail automat la solved" (bifa). Numele exact al
    # campului din feed nu e inca fixat -> incercam mai multe chei + `extra`. Parser tolerant -> True/False/None.
    # NB: nu folosim `or` cu fallback (un False explicit ar fi inghitit) — verificam None mai intai.
    auto_reply_raw = _g(rec, "solved_auto_reply", "auto_reply", "send_auto_reply",
                        "auto_reply_on_solved", "auto_send", "trimite_auto", "send_solved_auto")
    if auto_reply_raw is None and extra:
        auto_reply_raw = extra.get("solved_auto_reply",
                                   extra.get("auto_reply", extra.get("send_auto_reply")))
    solved_auto_reply = _as_bool(auto_reply_raw)
    # campuri aditive din feed-ul extins (outbox #9) — backward-compatible (NULL daca lipsesc)
    # FIX 2026-07-01: gateway trimite momentul solutionarii in `assignment.solved_at` (+ top-level
    # reply_at), NU la top-level `solved_at`/`extra` → captureaza-l si de acolo, altfel cts_solved_at
    # ramane MEREU NULL (bug: modulul Productivitate ramane inert). assignment.solved_at = autoritativ.
    _asg_solved = rec.get("assignment").get("solved_at") if isinstance(rec.get("assignment"), dict) else None
    solved_at = _g(rec, "solved_at", "rezolvat_at") or extra.get("solved_at") or _asg_solved
    deleted_at = _g(rec, "deleted_at", "sters_at") or extra.get("deleted_at")
    thread_key = (_g(rec, "thread_key", "conversation_id", "in_reply_to", "references", "thread_id")
                  or extra.get("conversation_id") or extra.get("thread_id") or extra.get("in_reply_to"))
    attachments = _norm_attachments(_g(rec, "attachments", "atasamente") or extra.get("attachments"))

    try:
        eid = int(eid) if eid is not None else None
    except (TypeError, ValueError):
        eid = None

    mid = str(mid).strip() if mid else (("mgid:%d" % eid) if eid else None)

    direction = "sent" if _is_sent(from_email, mid) else "received"
    ci = _cat_int(cat_raw)
    is_spam = (direction == "received" and ci == SPAM_CATEGORY_ID)

    if direction == "sent":
        # TRIMIS de noi: NU e incadrare de mail primit -> nu clasificam, nu comparam.
        cat, cat_ok = None, False
        dep, dep_ok = None, False
    elif is_spam:
        # SPAM: nu intra in taxonomia de categorie MG; expeditorul -> blacklist (in _upsert_records).
        cat, cat_ok = None, False
        dep, dep_ok = _map_department(dep_raw)
        dep = None  # spam nu se incadreaza pe departament
    else:
        cat, cat_ok = _map_category(cat_raw, cat_id_map)
        dep, dep_ok = _map_department(dep_raw)

    raw = dict(rec)
    raw["_unmapped"] = {
        "category": bool(cat_raw) and not cat_ok and direction == "received" and not is_spam,
        "department": bool(dep_raw) and not dep_ok and direction == "received" and not is_spam,
    }
    raw["_direction"] = direction
    if is_spam:
        raw["_spam"] = True

    # OPS-2026-0131: asignare CTS (cine e responsabil de mail), DOAR pe mail-uri primite.
    asg = rec.get("assignment") if isinstance(rec.get("assignment"), dict) else {}
    asg_email = asg_name = asg_id = asg_at = None
    if direction == "received" and asg:
        _ae = asg.get("assignee_email")
        asg_email = str(_ae).strip()[:320] if _ae else None
        _an = asg.get("assignee_name")
        asg_name = str(_an).strip()[:255] if _an else None
        try:
            asg_id = int(asg.get("assignee_id")) if asg.get("assignee_id") not in (None, "") else None
        except (TypeError, ValueError):
            asg_id = None
        asg_at = _coerce_ts(asg.get("assigned_at"))

    return {
        "message_id": mid,
        "email_id": eid,
        "cts_category": cat,
        "cts_department": dep,
        "cts_direction": direction,
        "cts_reply_text": (str(reply)[:20000] if reply else None),
        "cts_reply_html": (str(reply_html)[:200000] if reply_html else None),
        "cts_reply_at": _coerce_ts(reply_at),
        "cts_solved_at": _coerce_ts(solved_at),
        "cts_deleted_at": _coerce_ts(deleted_at),
        "cts_thread_key": (str(thread_key)[:255] if thread_key else None),
        "cts_attachments": (json.dumps(attachments, ensure_ascii=False) if attachments else None),
        "cts_status": (str(cstatus)[:32] if cstatus else None),
        "cts_solved_auto_reply": solved_auto_reply,
        "cts_assignee_email": asg_email,
        "cts_assignee_name": asg_name,
        "cts_assignee_id": asg_id,
        "cts_assigned_at": asg_at,
        # Id-ul TICHETULUI CTS -- unic per destinatar, deci face parte din cheia de unicitate:
        # un mail catre 3 colegi produce 3 tichete cu acelasi message_id (vezi _UPSERT_SQL).
        "cts_ticket_id": _coerce_pos_int(extra.get("cts_email_log_id")),
        "raw": json.dumps(raw, default=str, ensure_ascii=False),
        # transient (NU merge in SQL): expeditorul de blacklistat cand e spam
        "spam_sender": (from_email if is_spam else None),
        # transient (NU merge in SQL): client_id CTS (= clients.iris_client_id) pentru
        # propagarea in emails.client_id. CTS e sursa autoritativa a legaturii email<->client;
        # match_client() pe adresa rateaza cand expeditorul foloseste o adresa nedeclarata.
        "_cts_client_id": _coerce_pos_int(extra.get("client_id") or rec.get("client_id")),
    }


# ---------------------------------------------------------------- source readers

_ext_engine = None


def _get_ext_engine():
    global _ext_engine
    if _ext_engine is None:
        from sqlalchemy import create_engine
        _ext_engine = create_engine(os.environ["CTS_GT_DSN"], pool_pre_ping=True,
                                    pool_size=2, max_overflow=2)
    return _ext_engine


def _fetch_from_dsn(limit: int, since=None) -> List[Dict[str, Any]]:
    q = os.getenv("CTS_GT_QUERY")
    if not q or not q.strip():
        raise RuntimeError(
            "CTS_GT_QUERY nesetat — definiti SELECT-ul read-only catre tabelele CTS sincronizate "
            "(coloane asteptate: message_id, [email_id], category, department, reply_text, reply_at, status). "
            "Pt fereastra rolling, folositi :since (ex. WHERE updated_at >= :since OR :since IS NULL).")
    eng = _get_ext_engine()
    with eng.connect() as c:
        rows = c.execute(text(q), {"limit": limit, "since": since}).mappings().all()
    return [dict(r) for r in rows]


def _fetch_from_http(limit: int, since=None) -> List[Dict[str, Any]]:
    from app.services.iris_http import get_with_retry
    url = os.environ["CTS_GT_URL"]
    tok = os.environ["CTS_GT_TOKEN"]
    params = {"limit": limit}
    if since is not None:
        params["since"] = since if isinstance(since, str) else since.isoformat()
    r = get_with_retry(url, params=params,
                       headers={"Authorization": "Bearer " + tok}, verify=True, label="CTS_GT_URL")
    data = r.json()
    if isinstance(data, list):
        return data
    return data.get("items") or data.get("records") or data.get("data") or []


def _fetch_from_gateway(limit: int, since=None) -> List[Dict[str, Any]]:
    """Canalul preferat: GET pe IRIS Gateway, reutilizand cheia Cargo360 (X-Mailguard-Key),
    exact ca iris_sync.py. Read-only. Filtru rolling via ?since=<ISO8601>."""
    from app.config import get_settings
    from app.services.iris_http import get_with_retry
    base = (get_settings().iris_api_url or "").rstrip("/")
    key = os.getenv("IRIS_MAILGUARD_API_KEY", "")
    if not base or not key:
        raise RuntimeError("Gateway IRIS neconfigurat (iris_api_url / IRIS_MAILGUARD_API_KEY).")
    params = {"limit": limit}
    if since is not None:
        params["since"] = since if isinstance(since, str) else since.isoformat()
    r = get_with_retry(base + GATEWAY_PATH, params=params,
                       headers={"X-Mailguard-Key": key}, label=GATEWAY_PATH)
    data = r.json()
    if isinstance(data, list):
        return data
    return data.get("items") or data.get("records") or data.get("data") or []


def fetch_email_content(log_ids) -> Dict[str, Any]:
    """Companion la /cts/ground-truth: aduce LIVE din CTS, prin IRIS Gateway, corpul
    (reply_text) + lista de atasamente (name/mime/filesize) pentru log_id-urile date.
    Endpoint: GET /cts/email-content?log_ids=1,2,3 (≤200/apel), header X-Mailguard-Key.
    Read-only. Returneaza dict { "<log_id>": {"reply_text": str|None, "attachments": [...] } }.
    Continutul binar al atasamentelor NU e servit de gateway (unavailable_via_gateway)."""
    from app.config import get_settings
    from app.services.iris_http import get_with_retry
    ids = [str(x).strip() for x in (log_ids or []) if str(x).strip()]
    if not ids:
        return {}
    base = (get_settings().iris_api_url or "").rstrip("/")
    key = os.getenv("IRIS_MAILGUARD_API_KEY", "")
    if not base or not key:
        raise RuntimeError("Gateway IRIS neconfigurat (iris_api_url / IRIS_MAILGUARD_API_KEY).")
    r = get_with_retry(base + "/cts/email-content",
                       params={"log_ids": ",".join(ids[:200])},
                       headers={"X-Mailguard-Key": key},
                       timeout=60, label="/cts/email-content")
    data = r.json()
    if isinstance(data, dict):
        return data.get("items") or {}
    return {}


def _fetch_raw_records(limit: int, since=None) -> List[Dict[str, Any]]:
    if os.getenv("CTS_GT_DSN"):
        return _fetch_from_dsn(limit, since=since)
    if os.getenv("CTS_GT_URL") and os.getenv("CTS_GT_TOKEN"):
        return _fetch_from_http(limit, since=since)
    return _fetch_from_gateway(limit, since=since)


# ---------------------------------------------------------------- upsert

_MATCH_SQL = text(
    "SELECT id FROM emails "
    "WHERE email_headers->>'message_id' IN :cands "
    "   OR internet_message_id IN :cands "
    "ORDER BY id DESC LIMIT 1").bindparams(bindparam("cands", expanding=True))


def _match_email_id(db, mid: Optional[str]) -> Optional[int]:
    """Rezolva email-ul LOCAL (staging) dupa MESSAGE_ID — singura identitate stabila pe ambele medii.
    NU ne legam pe mg_id din sursa: pe staging, ground-truth-ul CTS vine din Cargo360 PROD, unde
    id-urile Cargo360 sunt ALTELE. mg_id-ul prod ramane doar in `raw`, ca referinta. Match-ul e
    robust la parantezele unghiulare `<...>` (CTS poate trimite cu sau fara)."""
    if not mid:
        return None
    m = str(mid).strip()
    if not m or m.startswith("mgid:"):
        return None  # fara message_id real nu putem lega cross-mediu -> ramane "doar in CTS"
    nb = m.strip("<>")
    cands = list({m, nb, "<" + nb + ">"})
    row = db.execute(_MATCH_SQL, {"cands": cands}).fetchone()
    return row[0] if row else None


# Upsert cu CATCH la schimbari: daca CTS a re-incadrat categoria/departamentul fata de ce aveam,
# salvam valoarea veche in *_prev si marcam changed_at=now() (semnal de training "s-a schimbat").
# `changed` in RETURNING = a fost modificata categoria SAU departamentul la acest upsert.
_UPSERT_SQL = text(
    "INSERT INTO cts_ground_truth "
    "(email_id, message_id, cts_category, cts_department, cts_direction, cts_reply_text, cts_reply_at, "
    " cts_reply_html, cts_solved_at, cts_deleted_at, cts_thread_key, cts_attachments, "
    " cts_status, cts_solved_auto_reply, cts_solved_seen_at, cts_in_progress_at, "
    " cts_assignee_email, cts_assignee_name, cts_assignee_id, cts_assigned_at, cts_ticket_id, source, raw, fetched_at, last_synced_at) "
    "VALUES (:email_id, :message_id, :cts_category, :cts_department, :cts_direction, :cts_reply_text, "
    " CAST(:cts_reply_at AS timestamptz), :cts_reply_html, CAST(:cts_solved_at AS timestamptz), "
    " CAST(:cts_deleted_at AS timestamptz), :cts_thread_key, CAST(:cts_attachments AS jsonb), "
    " :cts_status, :cts_solved_auto_reply, "
    " CASE WHEN :cts_status = 'solved' THEN now() ELSE NULL END, "
    " CASE WHEN :cts_status = 'in_progress' THEN COALESCE(CAST(:cts_assigned_at AS timestamptz), now()) ELSE NULL END, "
    " :cts_assignee_email, :cts_assignee_name, :cts_assignee_id, CAST(:cts_assigned_at AS timestamptz), "
    " :cts_ticket_id, :source, CAST(:raw AS jsonb), now(), now()) "
    # Cheia include tichetul: CTS creeaza un tichet PER DESTINATAR, toate cu acelasi message_id.
    # Pe (source, message_id) replicile se suprascriau reciproc si rămânea arbitrar ultima procesata
    # (mail 58176: rămânea `new` pe Maria, desi Vanessa rezolvase in 36 min).
    # Vezi migrations/20260805_cts_ticket_replicas.sql.
    "ON CONFLICT (source, message_id, cts_ticket_id) DO UPDATE SET "
    " email_id=EXCLUDED.email_id, "
    " cts_category_prev=CASE WHEN cts_ground_truth.cts_category IS DISTINCT FROM EXCLUDED.cts_category "
    "                        THEN cts_ground_truth.cts_category ELSE cts_ground_truth.cts_category_prev END, "
    " cts_department_prev=CASE WHEN cts_ground_truth.cts_department IS DISTINCT FROM EXCLUDED.cts_department "
    "                          THEN cts_ground_truth.cts_department ELSE cts_ground_truth.cts_department_prev END, "
    " changed_at=CASE WHEN (cts_ground_truth.cts_category IS DISTINCT FROM EXCLUDED.cts_category "
    "                       OR cts_ground_truth.cts_department IS DISTINCT FROM EXCLUDED.cts_department) "
    "                 THEN now() ELSE cts_ground_truth.changed_at END, "
    " cts_category=EXCLUDED.cts_category, "
    " cts_department=EXCLUDED.cts_department, cts_direction=EXCLUDED.cts_direction, "
    " cts_reply_text=EXCLUDED.cts_reply_text, "
    " cts_reply_html=EXCLUDED.cts_reply_html, cts_solved_at=EXCLUDED.cts_solved_at, "
    " cts_deleted_at=EXCLUDED.cts_deleted_at, cts_thread_key=EXCLUDED.cts_thread_key, "
    " cts_attachments=EXCLUDED.cts_attachments, "
    " cts_reply_at=EXCLUDED.cts_reply_at, cts_status=EXCLUDED.cts_status, "
    " cts_solved_auto_reply=EXCLUDED.cts_solved_auto_reply, "
    " cts_solved_seen_at=CASE WHEN EXCLUDED.cts_status='solved' "
    "                         AND cts_ground_truth.cts_status IS DISTINCT FROM 'solved' "
    "                        THEN now() ELSE cts_ground_truth.cts_solved_seen_at END, "
    " cts_in_progress_at=CASE WHEN EXCLUDED.cts_status='in_progress' "
    "                          AND cts_ground_truth.cts_in_progress_at IS NULL "
    "                         THEN COALESCE(EXCLUDED.cts_assigned_at, now()) "
    "                         ELSE cts_ground_truth.cts_in_progress_at END, "
    " cts_assignee_email=EXCLUDED.cts_assignee_email, cts_assignee_name=EXCLUDED.cts_assignee_name, cts_assignee_id=EXCLUDED.cts_assignee_id, cts_assigned_at=EXCLUDED.cts_assigned_at, "
    " raw=EXCLUDED.raw, fetched_at=now(), last_synced_at=now() "
    "RETURNING (xmax = 0) AS inserted, "
    " (xmax <> 0 AND changed_at IS NOT NULL AND changed_at >= now() - interval '5 seconds') AS changed, "
    " (xmax <> 0 AND cts_status='solved' AND cts_solved_seen_at IS NOT NULL "
    "  AND cts_solved_seen_at >= now() - interval '5 seconds') AS newly_solved")


_MARK_REPLICAS_SQL = text("""
    WITH ranked AS (
        SELECT id,
               row_number() OVER (
                   PARTITION BY source, message_id
                   ORDER BY cts_assigned_at NULLS LAST, cts_ticket_id NULLS LAST, id
               ) AS rn
          FROM cts_ground_truth
         WHERE source = :source AND message_id IN :mids
    )
    UPDATE cts_ground_truth g
       SET cts_is_replica = (r.rn > 1)
      FROM ranked r
     WHERE r.id = g.id
       AND g.cts_is_replica IS DISTINCT FROM (r.rn > 1)
""").bindparams(bindparam("mids", expanding=True))


def _mark_replicas(db, message_ids) -> int:
    """Marcheaza care tichet e ORIGINALUL si care sunt replicile, pe mailurile atinse in lot.

    CTS creeaza un tichet per destinatar, toate cu acelasi message_id, si NU marcheaza care e
    originalul: toate au acelasi `to_email` si acelasi `created_at`. Singurul criteriu disponibil e
    momentul atribuirii -> original = cel mai vechi `cts_assigned_at` (la egalitate, cel mai mic
    `cts_ticket_id`, ca sa fie determinist). Pe mailul 58176 asta da Vanessa original (01.08 07:59),
    iar Madalina + Maria replici (03.08 05:46).

    Nu se poate calcula in upsert-ul per rand: depinde de TOATE tichetele aceluiasi mail, deci ruleaza
    o data pe lot, inainte de commit. Se limiteaza la message_id-urile din lot (nu rescrie tabela).
    """
    mids = sorted({str(m) for m in (message_ids or []) if m})
    if not mids:
        return 0
    total = 0
    for i in range(0, len(mids), 1000):   # evita un IN (...) uriaș pe loturile mari
        try:
            res = db.execute(_MARK_REPLICAS_SQL, {"source": SOURCE, "mids": mids[i:i + 1000]})
            total += (res.rowcount or 0)
        except Exception as e:
            logger.warning("cts_gt mark replicas failed: %s", e)
    return total


def _blacklist_spam_sender(db, sender: str) -> bool:
    """Adauga expeditorul de spam in blacklist (tip='spam'), idempotent. Nu comite (comitem in lot)."""
    if not sender:
        return False
    try:
        res = sender_lists.add_entry(db, "blacklist", sender, by="cts_groundtruth",
                                     source="cts_spam", tip="spam", commit=False)
        return bool(res.get("ok"))
    except Exception as e:
        logger.warning("cts_gt blacklist spam sender failed (%s): %s", sender, e)
        return False


def _norm_mid_key(mid):
    """Cheie stabila de match pe message_id: fara <>, ignora 'mgid:' (placeholder fara id real)."""
    m = str(mid or "").strip()
    if not m or m.startswith("mgid:"):
        return None
    return m.strip("<>")


def _match_email_ids_bulk(db, mids):
    """Rezolva email-ul LOCAL pentru o LISTA de message_id-uri intr-un SINGUR query (in loc de N).
    Acelasi criteriu ca _match_email_id (match pe email_headers->>'message_id' SAU
    internet_message_id, robust la <>). La coliziune pastram id-ul cel mai mare (cel mai nou),
    identic cu 'ORDER BY id DESC LIMIT 1' din varianta unitara."""
    keys = set()
    for mid in mids:
        k = _norm_mid_key(mid)
        if k:
            keys.add(k)
    if not keys:
        return {}
    cands = []
    for k in keys:
        cands.append(k)
        cands.append("<" + k + ">")
    sql = text(
        "SELECT id, email_headers->>'message_id' AS mh, internet_message_id AS imid "
        "FROM emails "
        "WHERE email_headers->>'message_id' IN :c OR internet_message_id IN :c "
        "ORDER BY id ASC").bindparams(bindparam("c", expanding=True))
    out = {}
    for r in db.execute(sql, {"c": cands}).fetchall():
        eid = r[0]
        for v in (r[1], r[2]):
            if v:
                kk = str(v).strip().strip("<>")
                if kk:
                    out[kk] = eid   # id ASC -> ultimul (cel mai mare) castiga = cel mai nou
    return out


def _upsert_records(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalizeaza + upsert-eaza lista de inregistrari brute. Numara si cate au fost CHANGED
    (CTS a re-incadrat categoria/departamentul fata de ce aveam) — semnal de training. Mailurile
    de tip SPAM adauga expeditorul in blacklist.
    OPTIM: matching-ul message_id->email_id se face BULK (1 query), nu per-rand."""
    ins = upd = skipped = changed = sent = blacklisted = linked = 0
    newly_solved_ids = []   # Faza 2: emailuri proaspat trecute in 'solved' (tranzitie noua) + legate local
    solved_enabled = True
    cat_id_map = _load_cat_id_map()
    db = SessionLocal()
    try:
        # Pass 1 — normalizeaza tot (pur, fara DB); pastreaza doar ce are message_id.
        norm = []
        for rec in records:
            try:
                n = _normalize_record(rec, cat_id_map)
            except Exception as e:
                logger.warning("cts_gt normalize record failed: %s", e)
                skipped += 1
                continue
            if not n.get("message_id"):
                skipped += 1
                continue
            norm.append(n)
        # Match BULK message_id -> email_id (1 query in loc de N).
        id_map = _match_email_ids_bulk(db, [n["message_id"] for n in norm])
        # Pass 2 — upsert.
        for n in norm:
            try:
                if n.get("cts_direction") == "sent":
                    sent += 1
                spam_sender = n.pop("spam_sender", None)
                cts_client_id = n.pop("_cts_client_id", None)
                # Legarea LOCALA se face DOAR pe message_id (stabil prod+staging); mg_id-ul din
                # sursa (prod) e ignorat aici si pastrat doar in raw.
                n["email_id"] = id_map.get(_norm_mid_key(n["message_id"]))
                n["source"] = SOURCE
                row = db.execute(_UPSERT_SQL, n).fetchone()
                if row and row[0]:
                    ins += 1
                else:
                    upd += 1
                    if row and len(row) > 1 and row[1]:
                        changed += 1
                # Faza 2: tranzitie NOUA in 'solved' (marcata o singura data) + email legat local
                # -> candidat pentru reply de inchidere. Re-sync-ul/backfill-ul NU re-declanseaza.
                if row and len(row) > 2 and row[2] and n.get("email_id"):
                    newly_solved_ids.append(n["email_id"])
                # Forward-fix: propaga legatura email<->client din CTS in emails.client_id.
                # Doar completeaza NULL-uri (nu suprascrie un match existent).
                if n.get("email_id") and cts_client_id:
                    try:
                        res = db.execute(text("""
                            UPDATE emails e SET client_id = cl.id, updated_at = NOW()
                            FROM clients cl
                            WHERE e.id = :eid AND e.client_id IS NULL
                              AND cl.iris_client_id = :ics
                        """), {"eid": n["email_id"], "ics": cts_client_id})
                        linked += (res.rowcount or 0)
                    except Exception as e:
                        logger.warning("cts_gt client link failed email_id=%s: %s", n["email_id"], e)
                if spam_sender and _blacklist_spam_sender(db, spam_sender):
                    blacklisted += 1
            except Exception as e:
                logger.warning("cts_gt upsert record failed: %s", e)
                skipped += 1
        solved_enabled = _solved_trigger_enabled(db)
        _mark_replicas(db, [n["message_id"] for n in norm if n.get("message_id")])
        db.commit()
    finally:
        db.close()

    # Faza 2 (DRY-RUN): reply automat de INCHIDERE pt emailurile proaspat trecute in 'solved'.
    # Idempotent per (email,'solved') in dispecer; best-effort, izolat — un esec aici NU rupe sync-ul.
    if newly_solved_ids and solved_enabled:
        try:
            from app.services import autoreply_dispatch
            r = autoreply_dispatch.dispatch_for_ids(newly_solved_ids, trigger="solved")
            logger.info("cts_gt solved auto-reply (dry-run): %d candidat(i) -> %s",
                        len(newly_solved_ids), (r or {}).get("counts"))
        except Exception:
            logger.exception("cts_gt solved auto-reply dispatch failed (non-fatal)")

    return {"ok": True, "inserted": ins, "updated": upd, "changed": changed,
            "sent": sent, "blacklisted": blacklisted, "newly_solved": len(newly_solved_ids),
            "clients_linked": linked, "skipped": skipped, "fetched": len(records)}


# ---------------------------------------------------------------- pending (neincadrate)

def _pending_count(db) -> int:
    """Cate mailuri PRIMITE stim deja dar inca NU sunt incadrate complet in CTS (categorie SAU dept
    NULL). Mailurile TRIMISE de noi sunt excluse — ele nu se incadreaza niciodata."""
    return db.execute(text(
        "SELECT count(*) FROM cts_ground_truth WHERE source=:s "
        "AND COALESCE(cts_direction,'received')='received' "
        "AND (cts_category IS NULL OR cts_department IS NULL)"), {"s": SOURCE}).scalar() or 0


def _pending_anchor(db) -> Optional[_dt.datetime]:
    """Cel mai vechi moment pe care trebuie sa-l re-acoperim: emailuri PRIMITE deja vazute dar inca
    NEINCADRATE in CTS. Le re-interogam pana cand operatorul le seteaza. Returneaza naive-UTC."""
    val = db.execute(text(
        "SELECT min(COALESCE(e.received_at, gt.fetched_at)) "
        "FROM cts_ground_truth gt LEFT JOIN emails e ON e.id = gt.email_id "
        "WHERE gt.source=:s AND COALESCE(gt.cts_direction,'received')='received' "
        "AND (gt.cts_category IS NULL OR gt.cts_department IS NULL)"),
        {"s": SOURCE}).scalar()
    if val is None:
        return None
    if isinstance(val, _dt.datetime) and val.tzinfo is not None:
        val = val.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return val


# ---------------------------------------------------------------- sync entrypoints

def sync_ground_truth(limit: int = 500, since=None) -> Dict[str, Any]:
    """Trage ground-truth-ul CTS si il upsert-eaza in cts_ground_truth. Best-effort, idempotent.
    `since` (datetime/iso str) = fereastra rolling: doar emailurile (re)actualizate dupa acel moment."""
    if not _source_configured():
        return {"ok": False, "inserted": 0, "updated": 0,
                "reason": ("Sursa CTS neconfigurata (lipsesc CTS_GT_DSN / CTS_GT_URL+CTS_GT_TOKEN). "
                           "Necesita grant cross-app de la Razvan — vezi cererea din outbox.")}
    if not _kv_enabled():
        return {"ok": False, "inserted": 0, "updated": 0,
                "reason": "Sync CTS dezactivat (settings['cts_gt.sync_enabled'] != 1)."}
    try:
        records = _fetch_raw_records(limit, since=since)
    except Exception as e:
        logger.warning("cts_gt fetch failed: %s", e)
        return {"ok": False, "inserted": 0, "updated": 0,
                "reason": "Eroare la citirea sursei CTS: %s" % e}
    return _upsert_records(records)


def _parse_iso(s) -> Optional[_dt.datetime]:
    """Parser tolerant ISO8601 -> datetime (sau None). Accepta 'Z', cu/fara fractii/tz."""
    if not s:
        return None
    t = str(s).strip().replace("Z", "+00:00")
    try:
        return _dt.datetime.fromisoformat(t)
    except Exception:
        try:
            return _dt.datetime.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None


def _rec_updated_key(rec) -> Optional[str]:
    """Cheia de CURSOR a unei inregistrari brute = momentul ei de actualizare in CTS.
    Gateway-ul filtreaza (since) si ordoneaza CRESCATOR pe 'updated_at'; folosim ACELASI camp ca sa
    avansam cursorul intre pagini. Fallback: extra.updated_at/created_at/email_date."""
    if not isinstance(rec, dict):
        return None
    v = rec.get("updated_at") or rec.get("changed_at")
    if not v:
        ex = rec.get("extra") if isinstance(rec.get("extra"), dict) else {}
        v = ex.get("updated_at") or ex.get("created_at") or ex.get("email_date")
    return str(v).strip() if v else None


def sync_ground_truth_paged(since=None, batch: int = PAGE_BATCH,
                            max_batches: int = PAGE_MAX_BATCHES) -> Dict[str, Any]:
    """Ca sync_ground_truth, dar PAGINAT pe cursor (updated_at) ca sa NU se opreasca la `batch`:
    trage pagini succesive avansand since=max(updated_at) - 1s (overlap mic, idempotent prin
    ON CONFLICT) pana cand o pagina vine < batch (epuizat) sau pana la plafonul de siguranta.
    Upsert PER PAGINA (memorie marginita), contoare AGREGATE. Inlocuieste fetch-ul unic limit=5000
    care trunchia coada pe volume mari (incident 2026-06-30)."""
    if not _source_configured():
        return sync_ground_truth(limit=batch, since=since)   # intoarce direct motivul (inert)
    if not _kv_enabled():
        return sync_ground_truth(limit=batch, since=since)
    agg = {"ok": True, "inserted": 0, "updated": 0, "changed": 0, "sent": 0,
           "blacklisted": 0, "newly_solved": 0, "skipped": 0, "fetched": 0, "pages": 0}
    if isinstance(since, (_dt.datetime, _dt.date)):
        cursor_s = since.isoformat()
    else:
        cursor_s = (str(since).strip() or None) if since is not None else None
    for _ in range(max(1, max_batches)):
        try:
            recs = _fetch_raw_records(batch, since=cursor_s)
        except Exception as e:
            logger.warning("cts_gt paged fetch failed (since=%s): %s", cursor_s, e)
            agg["ok"] = agg["pages"] > 0      # daca am adus deja pagini, pastram ce s-a sincronizat
            agg["reason"] = "Eroare la citirea sursei CTS: %s" % e
            break
        if not recs:
            break
        res = _upsert_records(recs)
        for k in ("inserted", "updated", "changed", "sent", "blacklisted",
                  "newly_solved", "skipped", "fetched"):
            agg[k] += (res.get(k) or 0)
        agg["pages"] += 1
        if len(recs) < batch:
            break                              # ultima pagina (coada) -> gata
        keys = [k for k in (_rec_updated_key(r) for r in recs) if k]
        nxt_dt = _parse_iso(max(keys)) if keys else None
        if nxt_dt is None:
            logger.warning("cts_gt paged: lipsa updated_at pe pagina plina -> stop (since=%s)", cursor_s)
            break
        # overlap de 1s: re-acoperim granita ca sa NU pierdem randuri cu acelasi updated_at (since
        # poate fi exclusiv); ON CONFLICT face re-upsert-ul idempotent.
        new_cursor_s = (nxt_dt - _dt.timedelta(seconds=1)).isoformat()
        if cursor_s is not None and new_cursor_s <= cursor_s:
            # cursorul nu avanseaza (toata pagina pe acelasi timestamp) -> oprim anti-bucla-infinita
            logger.warning("cts_gt paged: cursor blocat la %s (pagina plina, %d randuri) -> stop",
                           cursor_s, len(recs))
            break
        cursor_s = new_cursor_s
    return agg


def sync_recent(hours: int = RECENT_WINDOW_HOURS, limit: int = 5000) -> Dict[str, Any]:
    """Re-sincronizeaza fereastra rolling. CTS poate (re)incadra un email la 1h dupa ce intra (uneori
    mai tarziu), asa ca:
      - PASS 1 (MEREU): fereastra PROASPATA = ultimele `hours` ore -> garanteaza ca cele mai NOI
        inregistrari CTS sunt aduse de fiecare data;
      - PASS 2 (doar daca exista mailuri NEINCADRATE mai vechi decat fereastra): backfill pe o
        fereastra SEPARATA, pana la cel mai vechi pending (plafon RECENT_MAX_BACKFILL_HOURS).
    IMPORTANT: cele doua pass-uri sunt SEPARATE si fiecare e PAGINAT (sync_ground_truth_paged) — trage
    pagini succesive avansand cursorul pe updated_at, deci NU se mai opreste la 5000. Inainte, cu un
    singur fetch pe fereastra extinsa, backfill-ul de pana la 7 zile depasea plafonul de `limit` si
    gateway-ul (ordine crescatoare) trunchia COADA -> emailurile noi nu mai erau aduse deloc, pagina
    „Mail-uri CTS" ingheta la ultima inregistrare adusa (incident 2026-06-30: blocat la email 39059)."""
    if not _source_configured():
        # inert: nu deschidem sesiuni DB degeaba, intoarcem direct motivul
        return sync_ground_truth(limit=limit)

    now = _dt.datetime.utcnow()
    floor = now - _dt.timedelta(hours=max(1, hours))
    cap = now - _dt.timedelta(hours=RECENT_MAX_BACKFILL_HOURS)
    backfill_since = None
    pending_n = 0
    extended = False
    try:
        db = SessionLocal()
        try:
            pending_n = _pending_count(db)
            anchor = _pending_anchor(db)
        finally:
            db.close()
        if anchor is not None and anchor < floor:
            backfill_since = anchor if anchor > cap else cap   # extinde inapoi, dar nu sub plafon
            extended = True
    except Exception as e:
        logger.warning("cts_gt pending anchor failed: %s", e)

    # PASS 1 — fereastra proaspata, PAGINATA: chiar daca intr-o zi intra >batch inregistrari, coada
    # NOUA e adusa integral (nu se mai trunchiaza).
    res = sync_ground_truth_paged(since=floor)

    # PASS 2 — backfill pending vechi, PAGINAT si SEPARAT (nu fura din PASS 1): parcurge tot intervalul
    # pana la cel mai vechi pending, in pagini, fara plafon de 5000.
    if extended and backfill_since is not None and isinstance(res, dict) and res.get("ok"):
        res_bf = sync_ground_truth_paged(since=backfill_since)
        if isinstance(res_bf, dict):
            for k in ("inserted", "updated", "changed", "sent", "blacklisted",
                      "skipped", "fetched", "newly_solved", "pages"):
                res[k] = (res.get(k) or 0) + (res_bf.get(k) or 0)
            res["backfill_since"] = backfill_since.isoformat()
            res["backfill_fetched"] = res_bf.get("fetched")
            res["backfill_pages"] = res_bf.get("pages")

    if isinstance(res, dict):
        res["window_floor_hours"] = hours
        res["window_since"] = floor.isoformat()
        res["window_extended_for_pending"] = extended
        res["pending_before"] = pending_n
    return res


def sync_recent_guarded(hours=RECENT_WINDOW_HOURS, limit=5000):
    """sync_recent cu lock NON-blocant: daca deja ruleaza un sync rolling (buton sau cron),
    sare imediat in loc sa porneasca al doilea in paralel (evita pile-up pe ferestre mari)."""
    if not _recent_lock.acquire(blocking=False):
        return {"ok": True, "skipped": "already_running"}
    try:
        return sync_recent(hours=hours, limit=limit)
    finally:
        _recent_lock.release()


def _set_kv(db, key: str, value_json: str):
    db.execute(text(
        "INSERT INTO settings(key, value, updated_by, updated_at) "
        "VALUES (:k, CAST(:v AS jsonb), 'cron', now()) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_by='cron', updated_at=now()"),
        {"k": key, "v": value_json})


def _seconds_since_last_recent(db) -> Optional[float]:
    row = db.execute(text("SELECT value FROM settings WHERE key=:k"), {"k": LAST_RECENT_KEY}).fetchone()
    if not row or row[0] is None:
        return None
    try:
        ts = _dt.datetime.fromisoformat(str(row[0]).strip().strip('"'))
        return (_dt.datetime.utcnow() - ts.replace(tzinfo=None)).total_seconds()
    except Exception:
        return None


def run_recent_if_due() -> Dict[str, Any]:
    """Apelat de cron (POST /process/run-now, la 5 min). No-op ieftin daca sursa nu e configurata
    sau daca ultimul sync rolling a fost acum < RECENT_MIN_INTERVAL_S. Nu arunca niciodata.
    ASTA e mecanismul AUTOMAT care inlocuieste verificarea manuala — ruleaza singur, fara buton."""
    try:
        if not is_enabled():
            return {"ok": False, "skipped": "inactive"}
        db = SessionLocal()
        try:
            elapsed = _seconds_since_last_recent(db)
            if elapsed is not None and elapsed < RECENT_MIN_INTERVAL_S:
                return {"ok": True, "skipped": "throttled", "elapsed_s": int(elapsed)}
            _set_kv(db, LAST_RECENT_KEY, '"%s"' % _dt.datetime.utcnow().isoformat())
            db.commit()
        finally:
            db.close()
        res = sync_recent_guarded()
        logger.info("cts_gt rolling sync: %s", res)
        return res
    except Exception as e:
        logger.warning("cts_gt run_recent_if_due failed: %s", e)
        return {"ok": False, "error": str(e)[:200]}
