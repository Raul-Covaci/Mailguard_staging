"""Sync "Device Operations" (CTS) -> Cargo360 (modul productivitate).

Mirror pe app/services/cts_tasks_sync.py (modulul "Task-uri"), adaptat pt operatiunile pe
echipamente (instalari, calibrari, interventii, inlocuiri, demontari, mutari, periferice).
Sursa: IRIS Gateway, GET /cts/device-operations -- contract CONFIRMAT de Razvan 2026-07-02
(raspuns in urma cererii din docs/device_operations_endpoint_request.md). CTS NU are un
subsistem unic -- sunt 7 tabele separate in cts_replica, cate una per tip (tipul e chiar
tabela sursa); gateway-ul le unifica si expune direct enum-ul cerut.

CAMPURI confirmate: `operation_id`, `action_type` (unul din cele 7 -- vezi ACTION_TYPE_LABELS,
mapate direct pe numele tabelelor sursa), `status` (dedus din timestamp-uri, fara enum
documentat in CTS) + `terminal` (bool, expus direct de gateway), `client_id` (la `mutare`
e deja destinatia -- `new_client_id` -- rezolvata de gateway), `device_serial` (98%) +
`device_imei` (87%), `assignee_email`/`assignee_id`/`assignee_username`/`assignee_name`/
`assignee_active` (JOIN pe admin, mirror pe mailuri/apeluri/task-uri), `department`/
`department_slug` (vine DOAR prin operator -- nicio tabela sursa n-are department_id
propriu), `description`, `created_at`/`updated_at`/`planned_at`/`finished_at`/`canceled_at`.
`_normalize_record` pastreaza totusi extractia defensiva (`_g`, alternative de cheie) ca
plasa de siguranta, dar cheile primare de mai jos sunt cele reale, nu presupuse.

NOTA departament (gasita de Razvan, nu presupusa de noi): 76% din operatiuni au operatorul
in departamentul "Instalari", doar 1.1% (793/72028) in "Suport 2" -- vezi discutia cu userul
inainte de a lega acest feed de modulul Productivitate pe un anume departament implicit.
"""
from __future__ import annotations

import os
import re
import json
import logging
import datetime as _dt
import threading
from typing import Dict, Any, Optional, List

from sqlalchemy import text
from app.database import SessionLocal
from app.services import iris_employee_sync

logger = logging.getLogger("mailguard.device_ops_sync")

SOURCE = "iris_sync"
SYNC_ENABLED_KEY = "device_ops.sync_enabled"
GATEWAY_PATH = "/cts/device-operations"   # endpoint IRIS Gateway -- LIVE (confirmat 2026-07-02)

RECENT_WINDOW_HOURS = 24
RECENT_MAX_BACKFILL_HOURS = 168    # acelasi plafon ca la mailuri/task-uri (incidentul 2026-06-30)
PAGE_BATCH = 2000                  # default confirmat de Razvan (clamp gateway 1..20000)
PAGE_MAX_BATCHES = 40

# Cele 7 tipuri de actiuni, mapate de Razvan direct pe numele celor 7 tabele sursa din
# cts_replica (tipul e chiar tabela, nu un camp FK catre un enum comun -- vezi docstring modul).
ACTION_TYPE_LABELS = {
    "instalare_noua": "Instalare nouă",
    "calibrare": "Calibrare",
    "interventie": "Intervenție",
    "inlocuire": "Înlocuire",
    "demontare": "Demontare",
    "mutare": "Mutare",
    "periferice": "Periferice",
}

_recent_lock = threading.Lock()


# ---------------------------------------------------------------- helpers defensive

def _g(rec: Dict[str, Any], *keys):
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


def _slug(s) -> Optional[str]:
    if not s:
        return None
    return str(s).strip().lower().replace(" ", "_").replace("-", "_")


# ---------------------------------------------------------------- gating

def _kv_enabled() -> bool:
    db = SessionLocal()
    try:
        row = db.execute(text("SELECT value FROM settings WHERE key=:k"),
                         {"k": SYNC_ENABLED_KEY}).fetchone()
    except Exception as e:
        logger.warning("device_ops _kv_enabled DB failed: %s", e)
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
        raise _GatewayNotBuiltYet()
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        return data
    return data.get("items") or data.get("records") or data.get("data") or []


def _fetch_raw_records(limit: int, since=None) -> List[Dict[str, Any]]:
    return _fetch_from_gateway(limit, since=since)


# ---------------------------------------------------------------- resolvere assignee

#: Placeholder-e CTS care nu sunt persoane — nu se importă, nu se raportează ca „nemapat".
_ASSIGNEE_PLACEHOLDERS = {"client@cargotrack.ro", "nealocat@cargotrack.ro", "client", "nealocat"}

#: Typo-uri de domeniu observate în datele CTS (2026-07-29: `adrian.jurca@cagrotrack.ro`,
#: 132 operațiuni pierdute din productivitate doar din cauza literelor inversate).
_DOMAIN_TYPOS = {
    "cagrotrack.ro": "cargotrack.ro",
    "cargotrak.ro": "cargotrack.ro",
    "cargotrack.com": "cargotrack.ro",
}

_DEFAULT_ASSIGNEE_DOMAIN = "cargotrack.ro"

#: Typo-uri în partea locală a adresei. CTS scrie `cristian.gotonoaca@`, rosterul IRIS are
#: `cristian.gotonoaga@` (c/g) — 65 operațiuni nemapate din cauza unei litere.
_LOCALPART_TYPOS = {
    "cristian.gotonoaca": "cristian.gotonoaga",
}

#: Aliasuri department -> slug canonic din employee_department_mapping. Folosite doar cand
#: operatiunea nu are angajat rezolvat (atunci departamentul angajatului e sursa de adevar).
_DEPT_ALIASES = {
    "operational": "management_operational",
    "taxe_de_drum": "taxe_drum",
    "instalări": "instalari",
}


def _normalize_assignee_email(email: Optional[str]) -> Optional[str]:
    """Curăță adresa de assignee înainte de rezolvare. Întoarce None pentru placeholder-e.

    Trei defecte reale în datele CTS: domeniu cu litere inversate (`cagrotrack.ro`), username
    fără domeniu (`cosmin.margauan`) și placeholder-e (`client@`, `nealocat@`). Primele două
    sunt persoane reale a căror muncă nu se contoriza.
    """
    if not email:
        return None
    e = str(email).strip().lower()
    if not e or e in _ASSIGNEE_PLACEHOLDERS:
        return None
    if "@" not in e:
        # username fără domeniu — completăm domeniul intern (`cosmin.margauan`)
        if not re.fullmatch(r"[a-z0-9._%+-]+", e):
            return None
        e = f"{e}@{_DEFAULT_ASSIGNEE_DOMAIN}"
    local, _, domain = e.rpartition("@")
    domain = _DOMAIN_TYPOS.get(domain, domain)
    local = _LOCALPART_TYPOS.get(local, local)
    e = f"{local}@{domain}"
    return e if e not in _ASSIGNEE_PLACEHOLDERS else None


def _resolve_assignee_id(db, email: Optional[str], roster_cache: Optional[list]) -> Optional[int]:
    email = _normalize_assignee_email(email)
    if not email:
        return None
    row = db.execute(text(
        "SELECT id FROM employee_department_mapping WHERE lower(email)=lower(:e)"), {"e": email}).fetchone()
    if row:
        return row[0]
    try:
        return iris_employee_sync.import_employee_by_email(db, email, roster=roster_cache)
    except Exception as e:
        logger.warning("device_ops assignee import failed (%s): %s", email, e)
        return None


# ---------------------------------------------------------------- normalizare + upsert

def _coerce_bool(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("true", "1", "t", "yes"):
        return True
    if s in ("false", "0", "f", "no", ""):
        return False
    return None


def _normalize_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Campuri conform contractului CONFIRMAT de Razvan -- vezi docstring modul. Pastreaza
    extractie defensiva (`_g`, alternative de nume) ca plasa de siguranta, nu ca presupunere."""
    operation_id = _g(rec, "operation_id", "id", "cts_operation_id", "device_operation_id")
    action_type = _g(rec, "action_type", "tip", "type", "operation_type")
    status = _g(rec, "status", "stare")
    terminal = _g(rec, "terminal")
    client_id = _g(rec, "client_id")
    department = _g(rec, "department_slug", "department", "departament", "dept")
    assignee_raw = _g(rec, "assignee_email", "operator_asignat", "operator_email", "assignee")
    device_serial = _g(rec, "device_serial", "serial", "asset_id")
    device_imei = _g(rec, "device_imei", "imei")
    description = _g(rec, "descriere", "description", "desc")
    created_at = _g(rec, "created_at", "data_creare", "cts_created_at")
    updated_at = _g(rec, "updated_at", "data_actualizare", "cts_updated_at")

    try:
        client_id = int(client_id) if client_id is not None else None
    except (TypeError, ValueError):
        client_id = None

    return {
        "operation_id": str(operation_id).strip() if operation_id is not None else None,
        "action_type": str(action_type).strip() if action_type else None,
        "status": str(status).strip() if status else None,
        "terminal": _coerce_bool(terminal),
        "client_id": client_id,
        "client_name": None,
        # Persistam adresa NORMALIZATA (typo de domeniu corectat, domeniu completat), nu bruta:
        # altfel fiecare sync reintroduce `adrian.jurca@cagrotrack.ro` & co in coloana, iar
        # rapoartele care citesc assignee_raw direct arata din nou date murdare. Valoarea
        # originala din CTS rămâne in raw_payload.
        "assignee_raw": (_normalize_assignee_email(assignee_raw)
                         or (str(assignee_raw).strip().lower() if assignee_raw else None)),
        "department": _slug(department),
        "device_serial": str(device_serial).strip() if device_serial else None,
        "device_imei": str(device_imei).strip() if device_imei else None,
        "description": str(description).strip() if description else None,
        "cts_created_at": _coerce_ts(created_at),
        "cts_updated_at": _coerce_ts(updated_at),
        "raw": rec,
    }


_UPSERT_SQL = text(
    "INSERT INTO device_operations "
    "(operation_id, action_type, status, terminal, client_id, client_name, assignee_raw, assignee_employee_id, "
    " department, device_serial, device_imei, description, cts_created_at, cts_updated_at, source, raw_payload, last_synced_at) "
    "VALUES (:operation_id, :action_type, :status, :terminal, :client_id, :client_name, :assignee_raw, :assignee_employee_id, "
    " :department, :device_serial, :device_imei, :description, CAST(:cts_created_at AS timestamptz), CAST(:cts_updated_at AS timestamptz), "
    " :source, CAST(:raw_payload AS jsonb), now()) "
    "ON CONFLICT (operation_id) DO UPDATE SET "
    " action_type=EXCLUDED.action_type, status=EXCLUDED.status, terminal=EXCLUDED.terminal, client_id=EXCLUDED.client_id, "
    " client_name=COALESCE(EXCLUDED.client_name, device_operations.client_name), "
    " assignee_raw=EXCLUDED.assignee_raw, assignee_employee_id=EXCLUDED.assignee_employee_id, "
    " department=EXCLUDED.department, device_serial=EXCLUDED.device_serial, device_imei=EXCLUDED.device_imei, description=EXCLUDED.description, "
    " cts_created_at=EXCLUDED.cts_created_at, cts_updated_at=EXCLUDED.cts_updated_at, "
    " raw_payload=EXCLUDED.raw_payload, last_synced_at=now(), updated_at=now() "
    "RETURNING (xmax = 0) AS inserted")


def _upsert_records(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    ins = upd = skipped = 0
    db = SessionLocal()
    roster_cache = None
    try:
        for rec in records:
            try:
                n = _normalize_record(rec)
            except Exception as e:
                logger.warning("device_ops normalize record failed: %s", e)
                skipped += 1
                continue
            if not n.get("operation_id"):
                skipped += 1
                continue
            try:
                if n.get("assignee_raw") and roster_cache is None:
                    try:
                        roster_cache = iris_employee_sync.fetch_employees(db)
                    except Exception as e:
                        logger.warning("device_ops roster fetch failed: %s", e)
                        roster_cache = []
                n["assignee_employee_id"] = _resolve_assignee_id(db, n.get("assignee_raw"), roster_cache)
                # Departamentul: al angajatului asignat e sursa de adevar (acelasi criteriu ca in
                # rapoartele de productivitate). CTS trimite si denumiri neconforme
                # (`operational` in loc de `management_operational`), care nu se potrivesc cu
                # niciun slug in rapoarte.
                if n["assignee_employee_id"]:
                    _dept_row = db.execute(text(
                        "SELECT department FROM employee_department_mapping WHERE id=:i"),
                        {"i": n["assignee_employee_id"]}).fetchone()
                    if _dept_row and _dept_row[0]:
                        n["department"] = _dept_row[0]
                elif n.get("department"):
                    n["department"] = _DEPT_ALIASES.get(n["department"], n["department"])
                n["source"] = SOURCE
                n["raw_payload"] = json.dumps(n["raw"])
                del n["raw"]
                row = db.execute(_UPSERT_SQL, n).fetchone()
                if row and row[0]:
                    ins += 1
                else:
                    upd += 1
            except Exception as e:
                logger.warning("device_ops upsert record failed: %s", e)
                skipped += 1
        db.commit()
    finally:
        db.close()
    return {"ok": True, "inserted": ins, "updated": upd, "skipped": skipped, "fetched": len(records)}


# ---------------------------------------------------------------- sync entrypoints

def _rec_updated_key(rec) -> Optional[str]:
    if not isinstance(rec, dict):
        return None
    v = rec.get("updated_at") or rec.get("data_actualizare") or rec.get("cts_updated_at")
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


def sync_paged(since=None, batch: int = PAGE_BATCH, max_batches: int = PAGE_MAX_BATCHES) -> Dict[str, Any]:
    """Sync paginat pe cursor (updated_at), mirror sync_tasks_paged. Trateaza 404 (endpoint IRIS
    neconstruit inca) ca rezultat ASTEPTAT -- nu eroare."""
    if not _source_configured():
        return {"ok": False, "inserted": 0, "updated": 0,
                "reason": "Gateway IRIS neconfigurat (iris_api_url / IRIS_MAILGUARD_API_KEY)."}
    if not _kv_enabled():
        return {"ok": False, "inserted": 0, "updated": 0,
                "reason": "Sync device operations dezactivat (settings['device_ops.sync_enabled'] != 1)."}

    agg = {"ok": True, "inserted": 0, "updated": 0, "skipped": 0, "fetched": 0, "pages": 0}
    cursor_s = since.isoformat() if isinstance(since, (_dt.datetime, _dt.date)) else (str(since).strip() or None) if since is not None else None

    for _ in range(max(1, max_batches)):
        try:
            recs = _fetch_raw_records(batch, since=cursor_s)
        except _GatewayNotBuiltYet:
            logger.info("device_ops: /cts/device-operations nu exista inca la IRIS (404) -- astept endpoint-ul.")
            agg["reason"] = "Endpoint IRIS /cts/device-operations nu exista inca."
            break
        except Exception as e:
            logger.warning("device_ops paged fetch failed (since=%s): %s", cursor_s, e)
            agg["ok"] = agg["pages"] > 0
            agg["reason"] = "Eroare la citirea sursei CTS: %s" % e
            break
        if not recs:
            break
        res = _upsert_records(recs)
        for k in ("inserted", "updated", "skipped", "fetched"):
            agg[k] += (res.get(k) or 0)
        agg["pages"] += 1
        if len(recs) < batch:
            break
        keys = [k for k in (_rec_updated_key(r) for r in recs) if k]
        nxt_dt = _parse_iso(max(keys)) if keys else None
        if nxt_dt is None:
            logger.warning("device_ops paged: lipsa cursor pe pagina plina -> stop (since=%s)", cursor_s)
            break
        new_cursor_s = (nxt_dt - _dt.timedelta(seconds=1)).isoformat()
        if cursor_s is not None and new_cursor_s <= cursor_s:
            logger.warning("device_ops paged: cursor blocat la %s -> stop", cursor_s)
            break
        cursor_s = new_cursor_s
    return agg


def _pending_anchor(db) -> Optional[_dt.datetime]:
    val = db.execute(text(
        "SELECT min(COALESCE(cts_created_at, first_synced_at)) FROM device_operations "
        "WHERE COALESCE(terminal, false) = false")).scalar()
    if val is None:
        return None
    if isinstance(val, _dt.datetime) and val.tzinfo is not None:
        val = val.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return val


def sync_recent(hours: int = RECENT_WINDOW_HOURS) -> Dict[str, Any]:
    """Rolling sync -- PASS1 (fereastra proaspata) MEREU + PASS2 (backfill pe pending, plafonat),
    mirror pe cts_tasks_sync.sync_recent."""
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
        anchor = _pending_anchor(db)
    finally:
        db.close()
    if anchor is not None and anchor < floor:
        backfill_since = anchor if anchor > cap else cap
        extended = True

    res = sync_paged(since=floor)

    if extended and backfill_since is not None and isinstance(res, dict) and res.get("ok"):
        res_bf = sync_paged(since=backfill_since)
        if isinstance(res_bf, dict):
            for k in ("inserted", "updated", "skipped", "fetched", "pages"):
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


LAST_RECENT_KEY = "device_ops.last_recent_sync_at"
RECENT_MIN_INTERVAL_S = 240


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
        logger.info("device_ops rolling sync: %s", res)
        return res
    except Exception as e:
        logger.warning("device_ops run_recent_if_due failed: %s", e)
        return {"ok": False, "error": str(e)[:200]}


# ---------------------------------------------------------------- config KV

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
