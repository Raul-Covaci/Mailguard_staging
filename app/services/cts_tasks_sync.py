"""Sync ground-truth CTS (task-uri) -> Cargo360 (modul "Task-uri").

Mirror 1:1 pe app/services/cts_groundtruth_sync.py (modulul "Mailuri CTS"), adaptat pt task-uri.
Task-urile vin DOAR din CTS, prin IRIS Gateway (acelasi canal, aceeasi cheie X-Mailguard-Key,
NICIUN secret nou). Le stocheaza in `cts_task_ground_truth`, legate best-effort de mail (prin
message_id) si/sau apel (prin calls.call_id) daca informatia exista in payload.

SCOPE (2026-07-02): endpoint-ul IRIS `/cts/tasks` era deja LIVE cand am verificat (Razvan il
construise inainte sa apuce sa raspunda la outbox). Raspunsuri confirmate pe date reale (1.173.773
task-uri, din 2021):
  - Statusuri (6): unallocated, new, in_progress, postponed, closed, solved. TERMINALE = solved SI
    closed (nu doar solved). postponed NU e terminal.
  - operator_asignat = assignee_email (confirmat, exact cum era presupus).
  - Legatura structurata cu mail/apel NU EXISTA in CTS (task.links mereu NULL, 0% populare pe tot
    istoricul; client_contact_email_log/client_call_log nu au coloana task_id). email_id/call_id
    raman NULL — Razvan a propus sa ceara CTS PO o coloana noua, gap real, nu bug la noi.
  - Volum: 398 categorii distincte, covarsitor zgomot operational automat (alerte echipamente,
    facturare, contracte).

FILTRU LA INGESTIE (schimbat 2026-07-31, v0.63.0): SINGURUL criteriu e "task-ul e asignat unui
angajat din employee_department_mapping". Categoria CTS e IRELEVANTA. Varianta anterioara filtra pe
o allowlist de 6 categorii din 398 si arunca ~56% din task-uri (filtered_noise 5325/9519), inclusiv
tot ce facea contabilitatea pe categorii nelistate -- raportul unui om din CTS nu se potrivea cu
Cargo360. Zgomotul automat e oprit oricum de criteriul de assignee (alertele nu au assignee real).
  - 7% din task-uri nu au assignee, <0.1% nu au departament — tratate ca "neasignat", nu eroare.

PASS2 (backfill pe "pending") e ACTIV: pending = status NOT IN ('solved','closed'), plafonat la
RECENT_MAX_BACKFILL_HOURS ca la mailuri.

ASSIGNEE ABSENT: daca `assignee_email` nu exista in `employee_department_mapping`, se declanseaza
un import punctual din /cts/employees (deja existent, fara schimbare IRIS) via
`iris_employee_sync.import_employee_by_email`, chiar daca departamentul lui nu e in whitelist-ul
normal de sync (vezi acel modul pt detalii).
"""
from __future__ import annotations

import os
import re
import html
import json
import logging
import datetime as _dt
import threading
from typing import Dict, Any, Optional, List

from sqlalchemy import text, bindparam
from app.database import SessionLocal
from app.services import iris_employee_sync

logger = logging.getLogger("mailguard.cts_tasks_sync")

SOURCE = "iris_sync"
SYNC_ENABLED_KEY = "cts_tasks.sync_enabled"
LAST_RECENT_KEY = "cts_tasks.last_recent_sync_at"
GATEWAY_PATH = "/cts/tasks"          # endpoint IRIS Gateway -- LIVE (confirmat 2026-07-02)

RECENT_WINDOW_HOURS = 24
RECENT_MAX_BACKFILL_HOURS = 1440     # 60 zile. Ridicat de la 168h (7 zile) pe 2026-07-31 odata cu
                                     # schimbarea filtrului de ingestie (v0.63.0): task-urile respinse
                                     # de vechea allowlist de categorii nu exista in baza, iar o
                                     # fereastra de 7 zile nu le readuce pe cele mai vechi.
PAGE_BATCH = 5000                    # default-ul CTS insusi (clamp real 1..20000, confirmat Razvan)
PAGE_MAX_BATCHES = 40

# Familii de categorii "device management" (cargobox/bgtoll/etoll/hugo) -- folosite in calculul de
# productivitate, dar excluse de _DEFAULT_CATEGORY_ALLOWLIST (zgomot volumic). CTS trimite zeci de
# variante de scriere pentru fiecare (ex. "carGObox: device discrepancy", "Setare carGObox",
# "BGTOLL: ...", "BGToll: ...", "ETOLL:"/"EToll:"/"Etoll:", "HU-GO: ..."/"Hu-GO: ...") -- de-aia
# clasificarea e pe substring normalizat (fara spatii/liniute/':'), nu pe lista exacta de string-uri.
_FAMILY_LABELS = {
    "cargobox": "CargoBox",
    "bgtoll": "BG Toll",
    "etoll": "E-Toll",
    "hugo": "HU-GO",
}
_FAMILY_KEYWORDS = ("cargobox", "bgtoll", "etoll", "hugo")


def _device_family(category_name) -> Optional[str]:
    """Intoarce cheia familiei (cargobox/bgtoll/etoll/hugo) daca task_type apartine uneia dintre
    categoriile de gestiune echipamente, altfel None."""
    if not category_name:
        return None
    norm = re.sub(r"[^a-z0-9]", "", str(category_name).lower())
    for kw in _FAMILY_KEYWORDS:
        if kw in norm:
            return kw
    return None

_recent_lock = threading.Lock()      # lock separat -- nu se blocheaza dupa sync-ul de mailuri/apeluri


# ---------------------------------------------------------------- helpers defensive

def _g(rec: Dict[str, Any], *keys):
    """Extractie defensiva de camp: prima cheie prezenta si ne-goala din `rec`."""
    for k in keys:
        if k in rec and rec[k] not in (None, ""):
            return rec[k]
    return None


def _coerce_ts(v):
    if v is None:
        return None
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def _norm_mid_key(mid):
    m = str(mid or "").strip()
    if not m:
        return None
    return m.strip("<>")


# ---------------------------------------------------------------- gating

def _kv_enabled() -> bool:
    db = SessionLocal()
    try:
        row = db.execute(text("SELECT value FROM settings WHERE key=:k"),
                         {"k": SYNC_ENABLED_KEY}).fetchone()
    except Exception as e:
        logger.warning("cts_tasks _kv_enabled DB failed: %s", e)
        return False
    finally:
        db.close()
    if not row or row[0] is None:
        return False
    return str(row[0]).strip().lower() in ("1", "true", "yes", "on")


def _gateway_configured() -> bool:
    if not os.getenv("IRIS_MAILGUARD_API_KEY"):
        return False
    try:
        from app.config import get_settings
        return bool((get_settings().iris_api_url or "").strip())
    except Exception:
        return False


def _get_roster_emails(db) -> set:
    """Emailurile angajatilor cunoscuti local (employee_department_mapping).

    Folosit ca filtru principal la ingestie: un task asignat unui om real din roster e relevant
    pentru productivitate INDIFERENT de categoria CTS. Vechea filtrare pe CATEGORY_ALLOWLIST arunca
    ~56% din task-uri (filtered_noise 5325/9519 pe iulie), inclusiv tot ce facea contabilitatea pe
    categorii nelistate -- de-aia raportul Adelina (200 in CTS) nu se potrivea cu 85 la noi.
    """
    try:
        rows = db.execute(text(
            "SELECT lower(email) FROM employee_department_mapping WHERE email IS NOT NULL AND email <> ''"
        )).fetchall()
        return {r[0] for r in rows if r[0]}
    except Exception as e:
        logger.warning("cts_tasks roster emails read failed: %s", e)
        return set()


def _source_configured() -> bool:
    return _gateway_configured()


def is_enabled(db=None) -> bool:
    return _kv_enabled() and _source_configured()


# ---------------------------------------------------------------- fetch (gateway)

class _GatewayNotBuiltYet(Exception):
    """404 de la IRIS Gateway -- endpoint-ul nu exista inca. Rezultat ASTEPTAT, nu eroare reala."""


def _fetch_from_gateway(limit: int, since=None) -> List[Dict[str, Any]]:
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
                       headers={"X-Mailguard-Key": key}, label=GATEWAY_PATH,
                       allow_statuses=(404,))
    if r.status_code == 404:
        # Endpoint neconstruit inca de IRIS -- ASTEPTAT (vezi OUTBOX_tasks_endpoint.md), nu eroare.
        raise _GatewayNotBuiltYet()
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        return data
    return data.get("items") or data.get("records") or data.get("data") or []


def _fetch_raw_records(limit: int, since=None) -> List[Dict[str, Any]]:
    return _fetch_from_gateway(limit, since=since)


# ---------------------------------------------------------------- resolvere legaturi

_MATCH_EMAIL_SQL = text(
    "SELECT id FROM emails "
    "WHERE email_headers->>'message_id' IN :cands "
    "   OR internet_message_id IN :cands "
    "ORDER BY id DESC LIMIT 1").bindparams(bindparam("cands", expanding=True))


def _resolve_email_id(db, message_id: Optional[str]) -> Optional[int]:
    """Rezolva emailul local dupa message_id (cheie stabila, NU un id intern IRIS)."""
    k = _norm_mid_key(message_id)
    if not k:
        return None
    cands = list({k, "<" + k + ">"})
    row = db.execute(_MATCH_EMAIL_SQL, {"cands": cands}).fetchone()
    return row[0] if row else None


def _resolve_call_id(db, call_ref: Optional[str]) -> Optional[int]:
    """Rezolva apelul local dupa call_id extern (While1) -- mirror cts_calls_sync.py."""
    if not call_ref:
        return None
    row = db.execute(text("SELECT id FROM calls WHERE call_id=:cid"), {"cid": str(call_ref)}).fetchone()
    return row[0] if row else None


def _resolve_assignee_id(db, email: Optional[str], roster_cache: Optional[list]) -> Optional[int]:
    """Rezolva employee_department_mapping.id dupa email. Daca nu exista local, incearca import
    punctual din rosterul IRIS complet (fetch o singura data per rulare, pasat in roster_cache)."""
    if not email:
        return None
    email = str(email).strip()
    if not email or "@" not in email:
        return None
    row = db.execute(text(
        "SELECT id FROM employee_department_mapping WHERE lower(email)=lower(:e)"), {"e": email}).fetchone()
    if row:
        return row[0]
    try:
        return iris_employee_sync.import_employee_by_email(db, email, roster=roster_cache)
    except Exception as e:
        logger.warning("cts_tasks assignee import failed (%s): %s", email, e)
        return None


# ---------------------------------------------------------------- normalizare + upsert

# Aliasuri department: numele brut din CTS -> slug-ul canonic folosit in
# employee_department_mapping.department. Fara maparea asta, `_slug("Taxe de drum")` da
# `taxe_de_drum`, iar ecranele care filtreaza pe `cts_task_ground_truth.department='taxe_drum'`
# gaseau 0 task-uri, in timp ce cele care fac JOIN pe angajat gaseau 21.870 — aceeasi luna,
# acelasi departament, doua cifre diferite in aceeasi aplicatie (constatat 2026-07-29).
_DEPT_ALIASES = {
    "taxe_de_drum": "taxe_drum",
    "operational": "management_operational",
}


def _slug(s) -> Optional[str]:
    """Normalizare (spatiu/cratima -> underscore, lowercase) + aliasare la slug-ul canonic.
    Fara filtrare pe whitelist: un departament necunoscut se pastreaza ca atare, ca sa nu
    pierdem task-uri — dar denumirile care AU un canonic sunt aduse la el."""
    if not s:
        return None
    slug = str(s).strip().lower().replace(" ", "_").replace("-", "_")
    return _DEPT_ALIASES.get(slug, slug)


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s) -> Optional[str]:
    """Continutul complet de la CTS (campul `description`) poate fi HTML (ex. un link
    admin.php care invelie titlul). Curatam taguri + entitati, ca sa afisam text simplu, sigur
    (fara dangerouslySetInnerHTML pe date externe). Unele randuri au \\r\\n literal (dublu-escapat
    din sursa CTS, nu newline real) -- il curatam separat, altfel ramane vizibil ca text brut."""
    if not s:
        return None
    s = str(s).replace("\\r\\n", " ").replace("\\n", " ").replace("\\r", " ")
    txt = html.unescape(_HTML_TAG_RE.sub(" ", s))
    return " ".join(txt.split()) or None


def _normalize_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Camp reale confirmate din /cts/tasks (live, 2026-07-02): cts_task_id, category_id,
    category_name, status, department, department_slug, client_id (numeric -- NU nume),
    assignee_id/assignee_email/assignee_username/assignee_name, task_name (titlu, adesea
    TRUNCHIAT de CTS insusi -- se termina cu "..." literal), description (continut COMPLET,
    uneori HTML), priority (numeric), created_at, updated_at, startdate, duedate, postponed_to,
    parent_id."""
    task_id = _g(rec, "cts_task_id", "task_id", "id", "iris_task_id")
    task_type = _g(rec, "category_name", "tip", "task_type", "type")
    status = _g(rec, "status", "stare")
    priority = _g(rec, "priority", "prioritate")
    client_id = _g(rec, "client_id")
    department = _g(rec, "department_slug", "department", "departament", "dept")
    title = _g(rec, "task_name", "tip_scurt")
    description = _strip_html(_g(rec, "description", "descriere", "desc")) or title
    created_at = _g(rec, "created_at", "data_creare", "cts_created_at")
    updated_at = _g(rec, "updated_at", "data_actualizare", "cts_updated_at")

    assignee_raw = _g(rec, "assignee_email", "operator_asignat", "operator_email", "assignee")

    # Legatura structurata cu mail/apel: /cts/tasks NU expune inca asa ceva (confirmat -- descrierea
    # HTML poate contine un link admin.php cu un emailLog intern CTS, NU un message_id matchabil).
    # Ramane NULL pana Razvan confirma un camp dedicat.
    ent = rec.get("entitate_legata") if isinstance(rec.get("entitate_legata"), dict) else {}
    ent_type = _g(ent, "tip", "type") or _g(rec, "entitate_tip")
    message_id = _g(ent, "message_id", "mid") or _g(rec, "message_id")
    call_ref = _g(ent, "call_id", "cid") or _g(rec, "call_id")
    if ent_type == "apel" and not call_ref:
        call_ref = _g(ent, "id")
    if ent_type == "mail" and not message_id:
        message_id = _g(ent, "id")

    try:
        client_id = int(client_id) if client_id is not None else None
    except (TypeError, ValueError):
        client_id = None

    return {
        "iris_task_id": str(task_id).strip() if task_id is not None else None,
        "task_type": str(task_type).strip() if task_type else None,
        "status": str(status).strip() if status else None,
        "priority": str(priority).strip() if priority is not None else None,
        "assignee_raw": str(assignee_raw).strip() if assignee_raw else None,
        "source_message_id": str(message_id).strip() if message_id else None,
        "source_call_ref": str(call_ref).strip() if call_ref else None,
        "client_id": client_id,
        "client_name": None,   # nume rezolvat prin JOIN pe clients.iris_client_id (router), nu la sync
        "department": _slug(department),
        "title": str(title).strip() if title else None,
        "description": str(description).strip() if description else None,
        "cts_created_at": _coerce_ts(created_at),
        "cts_updated_at": _coerce_ts(updated_at),
        "raw": rec,
    }


_UPSERT_SQL = text(
    "INSERT INTO cts_task_ground_truth "
    "(iris_task_id, task_type, status, priority, assignee_raw, assignee_employee_id, "
    " source_message_id, email_id, source_call_ref, call_id, client_id, client_name, department, title, description, "
    " cts_created_at, cts_updated_at, cts_in_progress_at, source, raw_payload, last_synced_at) "
    "VALUES (:iris_task_id, :task_type, :status, :priority, :assignee_raw, :assignee_employee_id, "
    " :source_message_id, :email_id, :source_call_ref, :call_id, :client_id, :client_name, :department, :title, :description, "
    " CAST(:cts_created_at AS timestamptz), CAST(:cts_updated_at AS timestamptz), "
    " CASE WHEN :status = 'in_progress' THEN now() ELSE NULL END, "
    " :source, CAST(:raw_payload AS jsonb), now()) "
    "ON CONFLICT (iris_task_id) DO UPDATE SET "
    " task_type=EXCLUDED.task_type, status=EXCLUDED.status, priority=EXCLUDED.priority, "
    " assignee_raw=EXCLUDED.assignee_raw, assignee_employee_id=EXCLUDED.assignee_employee_id, "
    " source_message_id=EXCLUDED.source_message_id, email_id=EXCLUDED.email_id, "
    " source_call_ref=EXCLUDED.source_call_ref, call_id=EXCLUDED.call_id, "
    " client_id=EXCLUDED.client_id, "
    " client_name=COALESCE(EXCLUDED.client_name, cts_task_ground_truth.client_name), "
    " department=EXCLUDED.department, title=EXCLUDED.title, description=EXCLUDED.description, "
    " cts_created_at=EXCLUDED.cts_created_at, cts_updated_at=EXCLUDED.cts_updated_at, "
    " cts_in_progress_at=CASE WHEN EXCLUDED.status='in_progress' "
    "                          AND cts_task_ground_truth.cts_in_progress_at IS NULL "
    "                         THEN now() "
    "                         ELSE cts_task_ground_truth.cts_in_progress_at END, "
    # Oglinda liniei din cts_groundtruth_sync._UPSERT_SQL — se schimba IMPREUNA.
    # `monitor_closed_at` e inchiderea noastra (junk vechi taiat din monitor). Sync-ul n-o scrie
    # niciodata; o STERGE doar cand cineva chiar preia task-ul, ca munca sa reapara pe monitor.
    " monitor_closed_at=CASE WHEN EXCLUDED.status IN ('in_progress','in progress') "
    "                        THEN NULL ELSE cts_task_ground_truth.monitor_closed_at END, "
    " raw_payload=EXCLUDED.raw_payload, last_synced_at=now(), updated_at=now() "
    "RETURNING (xmax = 0) AS inserted")


def _upsert_records(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    ins = upd = skipped = filtered = 0
    db = SessionLocal()
    roster_cache = None  # fetch rosterul IRIS complet o singura data per rulare, doar daca e nevoie
    roster_emails = _get_roster_emails(db)
    try:
        for rec in records:
            try:
                n = _normalize_record(rec)
            except Exception as e:
                logger.warning("cts_tasks normalize record failed: %s", e)
                skipped += 1
                continue
            if not n.get("iris_task_id"):
                skipped += 1
                continue
            # SINGURUL criteriu de ingestie: task-ul e asignat unui angajat real din roster
            # (employee_department_mapping). Categoria CTS e IRELEVANTA -- productivitatea se
            # calculeaza pe tot ce a facut omul, nu doar pe categoriile "de interactiune client".
            # Se arunca doar zgomotul automat neasignat sau asignat unui email necunoscut local.
            _asg = (n.get("assignee_raw") or "").strip().lower()
            if not _asg or _asg not in roster_emails:
                filtered += 1
                continue
            try:
                n["email_id"] = _resolve_email_id(db, n.get("source_message_id"))
                n["call_id"] = _resolve_call_id(db, n.get("source_call_ref"))
                if n.get("assignee_raw") and roster_cache is None:
                    try:
                        roster_cache = iris_employee_sync.fetch_employees(db)
                    except Exception as e:
                        logger.warning("cts_tasks roster fetch failed: %s", e)
                        roster_cache = []
                n["assignee_employee_id"] = _resolve_assignee_id(db, n.get("assignee_raw"), roster_cache)
                n["source"] = SOURCE
                n["raw_payload"] = json.dumps(n["raw"])
                del n["raw"]
                row = db.execute(_UPSERT_SQL, n).fetchone()
                if row and row[0]:
                    ins += 1
                else:
                    upd += 1
            except Exception as e:
                logger.warning("cts_tasks upsert record failed: %s", e)
                skipped += 1
        db.commit()
    finally:
        db.close()
    return {"ok": True, "inserted": ins, "updated": upd, "skipped": skipped, "filtered_noise": filtered,
            "fetched": len(records)}


# ---------------------------------------------------------------- sync entrypoints

def _rec_updated_key(rec) -> Optional[str]:
    if not isinstance(rec, dict):
        return None
    v = rec.get("data_actualizare") or rec.get("updated_at") or rec.get("cts_updated_at")
    if not v:
        extra = rec.get("extra") if isinstance(rec.get("extra"), dict) else {}
        v = extra.get("updated_at")
    return str(v).strip() if v else None


def _parse_iso(s) -> Optional[_dt.datetime]:
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


def sync_tasks_paged(since=None, batch: int = PAGE_BATCH, max_batches: int = PAGE_MAX_BATCHES) -> Dict[str, Any]:
    """Sync paginat pe cursor (data_actualizare), mirror sync_ground_truth_paged (mailuri).
    Trateaza 404 (endpoint IRIS neconstruit inca) ca rezultat ASTEPTAT -- nu eroare."""
    if not _source_configured():
        return {"ok": False, "inserted": 0, "updated": 0,
                "reason": "Gateway IRIS neconfigurat (iris_api_url / IRIS_MAILGUARD_API_KEY)."}
    if not _kv_enabled():
        return {"ok": False, "inserted": 0, "updated": 0,
                "reason": "Sync task-uri dezactivat (settings['cts_tasks.sync_enabled'] != 1)."}

    agg = {"ok": True, "inserted": 0, "updated": 0, "skipped": 0, "filtered_noise": 0, "fetched": 0, "pages": 0}
    cursor_s = since.isoformat() if isinstance(since, (_dt.datetime, _dt.date)) else (str(since).strip() or None) if since is not None else None

    for _ in range(max(1, max_batches)):
        try:
            recs = _fetch_raw_records(batch, since=cursor_s)
        except _GatewayNotBuiltYet:
            logger.info("cts_tasks: /cts/tasks nu exista inca la IRIS (404) -- astept endpoint-ul.")
            agg["reason"] = "Endpoint IRIS /cts/tasks nu exista inca."
            break
        except Exception as e:
            logger.warning("cts_tasks paged fetch failed (since=%s): %s", cursor_s, e)
            agg["ok"] = agg["pages"] > 0
            agg["reason"] = "Eroare la citirea sursei CTS: %s" % e
            break
        if not recs:
            break
        res = _upsert_records(recs)
        for k in ("inserted", "updated", "skipped", "filtered_noise", "fetched"):
            agg[k] += (res.get(k) or 0)
        agg["pages"] += 1
        if len(recs) < batch:
            break
        keys = [k for k in (_rec_updated_key(r) for r in recs) if k]
        nxt_dt = _parse_iso(max(keys)) if keys else None
        if nxt_dt is None:
            logger.warning("cts_tasks paged: lipsa cursor pe pagina plina -> stop (since=%s)", cursor_s)
            break
        new_cursor_s = (nxt_dt - _dt.timedelta(seconds=1)).isoformat()
        if cursor_s is not None and new_cursor_s <= cursor_s:
            logger.warning("cts_tasks paged: cursor blocat la %s -> stop", cursor_s)
            break
        cursor_s = new_cursor_s
    return agg


_TERMINAL_STATUSES = {"solved", "closed"}  # confirmat de Razvan (2026-07-02) -- restul = pending


def _pending_task_anchor(db) -> Optional[_dt.datetime]:
    """Cel mai vechi task NEterminal deja vazut local -- pana unde trebuie sa re-acoperim backfill-ul
    (mirror _pending_anchor din cts_groundtruth_sync). Returneaza naive-UTC."""
    val = db.execute(text(
        "SELECT min(COALESCE(cts_created_at, first_synced_at)) FROM cts_task_ground_truth "
        "WHERE lower(COALESCE(status,'')) NOT IN ('solved','closed')")).scalar()
    if val is None:
        return None
    if isinstance(val, _dt.datetime) and val.tzinfo is not None:
        val = val.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return val


def sync_recent(hours: int = RECENT_WINDOW_HOURS) -> Dict[str, Any]:
    """Rolling sync -- PASS1 (fereastra proaspata) MEREU + PASS2 (backfill pe task-uri PENDING,
    adica status NOT IN ('solved','closed'), plafonat la RECENT_MAX_BACKFILL_HOURS). Statusurile
    confirmate de Razvan (2026-07-02): unallocated, new, in_progress, postponed, closed, solved --
    terminale = solved + closed."""
    if not _source_configured():
        return {"ok": False, "inserted": 0, "updated": 0,
                "reason": "Gateway IRIS neconfigurat (iris_api_url / IRIS_MAILGUARD_API_KEY)."}
    now = _dt.datetime.utcnow()
    floor = now - _dt.timedelta(hours=max(1, hours))
    cap = now - _dt.timedelta(hours=RECENT_MAX_BACKFILL_HOURS)
    backfill_since = None
    extended = False
    db = SessionLocal()
    try:
        anchor = _pending_task_anchor(db)
    finally:
        db.close()
    if anchor is not None and anchor < floor:
        backfill_since = anchor if anchor > cap else cap
        extended = True

    res = sync_tasks_paged(since=floor)

    if extended and backfill_since is not None and isinstance(res, dict) and res.get("ok"):
        res_bf = sync_tasks_paged(since=backfill_since)
        if isinstance(res_bf, dict):
            for k in ("inserted", "updated", "skipped", "filtered_noise", "fetched", "pages"):
                res[k] = (res.get(k) or 0) + (res_bf.get(k) or 0)
            res["backfill_since"] = backfill_since.isoformat()

    if isinstance(res, dict):
        res["window_floor_hours"] = hours
        res["window_since"] = floor.isoformat()
        res["window_extended_for_pending"] = extended
    return res


def sync_recent_guarded(hours: int = RECENT_WINDOW_HOURS) -> Dict[str, Any]:
    if not _recent_lock.acquire(blocking=False):
        return {"ok": True, "skipped": "already_running"}
    try:
        return sync_recent(hours=hours)
    finally:
        _recent_lock.release()


RECENT_MIN_INTERVAL_S = 50   # vezi cts_groundtruth_sync: cron la 2 min, throttle sub un tick


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
    """Apelat de cron la 5 min. No-op daca sync nu e activ sau throttled. Nu arunca niciodata."""
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
        logger.info("cts_tasks rolling sync: %s", res)
        return res
    except Exception as e:
        logger.warning("cts_tasks run_recent_if_due failed: %s", e)
        return {"ok": False, "error": str(e)[:200]}


# ---------------------------------------------------------------- config KV (sync-config route)

def get_sync_config(db) -> Dict[str, Any]:
    row = db.execute(text("SELECT value FROM settings WHERE key=:k"), {"k": SYNC_ENABLED_KEY}).fetchone()
    enabled = False
    if row and row[0] is not None:
        enabled = str(row[0]).strip().lower() in ("1", "true", "yes", "on")
    return {"enabled": enabled, "gateway_configured": _gateway_configured()}


def set_sync_config(db, enabled: bool) -> Dict[str, Any]:
    db.execute(text(
        "INSERT INTO settings(key, value) VALUES (:k, to_jsonb(:v)) "
        "ON CONFLICT (key) DO UPDATE SET value=to_jsonb(:v)"), {"k": SYNC_ENABLED_KEY, "v": bool(enabled)})
    db.commit()
    return get_sync_config(db)
