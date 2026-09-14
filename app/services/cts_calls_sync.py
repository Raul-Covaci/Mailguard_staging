"""Sync ground-truth CTS -> mailguard.calls (modul "Apeluri CTS", mirror cts_groundtruth_sync.py).

STARE: contract CONFIRMAT de Razvan (2026-07-02). Endpoint live pe IRIS Gateway, sursa
cts_replica.client_call_log (+ join admin/department/issue).

Contract confirmat:
  - GET {IRIS_API_URL}/cts/calls, header X-Mailguard-Key (ACEEASI cheie ca la ground-truth
    emailuri — IRIS_MAILGUARD_API_KEY, deja setat in .env, nu a fost nevoie de credentiale noi).
    Params: since (ISO8601, rolling pe updated_at) + limit (clamp 1..20000, default 5000).
    Ordine: updated_at ASC.
  - Campuri per apel: cts_call_id (PK intern CTS), ctk_uniqueid (= uniqueid PBX Asterisk = While1
    uniqueid), calltrack_id (= id numeric While1), category_id, issue_id, issue_name, status,
    department, department_slug, assignee_id, assignee_email, assignee_username, assignee_name,
    assignee_active, assigned_at, started_at, solved_at, ring_seconds, duration_seconds,
    time_to_solved_seconds, has_recording, priority, client_id, updated_at.
  - Legatura cu `calls` (ingerate din While1): PRIMAR pe ctk_uniqueid == calls.while1_uniqueid
    (recomandat de Razvan, stabil la nivel PBX), FALLBACK pe calltrack_id == calls.call_id.
  - "response_seconds": NU exista camp unic. Razvan a clarificat exact:
      ring_seconds          = timp pana la RASPUNS (cat a sunat pana a raspuns agentul)
      time_to_solved_seconds = started_at -> solved_at (timp pana la solutionare)
      duration_seconds       = durata apelului
    Folosim ring_seconds pentru `cts_response_seconds` (coloana existenta, "timp de raspuns").
    assigned_at e adesea degenerat/null (~= started_at sau lipsa) — NU se foloseste pt timing.
  - category_id: enum CTS, INSPECTAT EMPIRIC pe date reale (2026-07-02, 2000 apeluri, since
    2026-06-01) — NU e acelasi enum ca la /cts/ground-truth (emailuri), desi Razvan a descris-o
    similar structural. Distributie observata: 5=1036 (issue_name variat: "Solicitare oferta",
    "Verificare dispozitiv", "Vanzare echipament" — cereri generale/administrative/comerciale,
    departamente diverse) -> informatie; 4=138 (issue_name: "Dispozitive care nu transmit
    informatii corecte", "Dispozitive blocate" — probleme tehnice active) -> sesizare; 1=4
    (issue_name: "- Client recalcitrant -" — client dificil) -> reclamatie; None=822 (~41%,
    inca neincadrat de operatorul CTS) -> ramane necomparabil, ca la emailuri. Editabil fara
    redeploy din settings['cts_calls_gt.category_map'] daca se dovedeste gresit pe volum mai mare.

Idempotenta: UNIQUE(source, cts_call_id) + upsert; call_local_id se recalculeaza doar daca
lipsea (nu suprascrie un match anterior bun).
"""
import os
import json
import logging
import datetime as _dt
import threading
from typing import Dict, Any, Optional, List

from sqlalchemy import text
from app.database import SessionLocal
from app.services.call_classifier import CATEGORIES

logger = logging.getLogger("mailguard.cts_calls_sync")

SYNC_ENABLED_KEY = "cts_calls_gt.sync_enabled"
LAST_RECENT_KEY = "cts_calls_gt.last_recent_sync_at"
CATEGORY_MAP_KEY = "cts_calls_gt.category_map"
SOURCE = "iris_sync"
GATEWAY_PATH = "/cts/calls"
RECENT_WINDOW_HOURS = 72   # 3 zile: acopera si fisele atinse de operator peste noapte / in weekend
FULL_SYNC_MAX_DAYS = 400   # ancora pentru sync-ul "complet" (since=None), vezi sync_ground_truth
RECENT_MIN_INTERVAL_S = 50    # vezi cts_groundtruth_sync: cron la 2 min, throttle sub un tick
_recent_lock = threading.Lock()

# Vezi docstring modul — mapare derivata empiric din date reale (issue_name/department),
# NU confirmata de un business owner (spre deosebire de email). De revizuit daca divergentele
# din UI arata sistematic gresit pe volum mai mare.
_DEFAULT_CTS_CALL_CATEGORY_MAP: Dict[str, str] = {
    "1": "reclamatie",
    "4": "sesizare",
    "5": "informatie",
}


# ---------------------------------------------------------------- gating

def _kv_enabled() -> bool:
    db = SessionLocal()
    try:
        row = db.execute(text("SELECT value FROM settings WHERE key=:k"),
                         {"k": SYNC_ENABLED_KEY}).fetchone()
    except Exception as e:
        logger.warning("cts_calls_gt _kv_enabled DB failed: %s", e)
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


def is_enabled() -> bool:
    """Sync activ DOAR daca e bifat in settings SI gateway-ul e configurat."""
    return _kv_enabled() and _gateway_configured()


def status() -> Dict[str, Any]:
    return {
        "enabled_flag": _kv_enabled(),
        "gateway_configured": _gateway_configured(),
        "active": is_enabled(),
        "auto_interval_s": RECENT_MIN_INTERVAL_S,
        "window_hours": RECENT_WINDOW_HOURS,
        "category_map": _load_cat_id_map(),
    }


# ---------------------------------------------------------------- mapping

def _load_cat_id_map() -> Dict[str, str]:
    """Mapa CTS category_id -> categorie MG, din KV (CATEGORY_MAP_KEY) peste default empiric."""
    m = dict(_DEFAULT_CTS_CALL_CATEGORY_MAP)
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


def _map_category_id(category_id, cat_id_map: Optional[Dict[str, str]] = None) -> Optional[str]:
    """category_id numeric CTS -> categorie MG, sau None daca lipseste/nemapat (nu presupunem)."""
    if category_id is None:
        return None
    key = str(category_id).strip()
    if not key or key.lower() == "none":
        return None
    m = cat_id_map if cat_id_map is not None else _load_cat_id_map()
    mapped = m.get(key)
    if mapped and mapped in CATEGORIES:
        return mapped
    return "cts_cat:" + key  # nemapat: pastram referinta, NU contam ca potrivire


def _g(rec: Dict[str, Any], *keys):
    for k in keys:
        if k in rec and rec[k] not in (None, ""):
            return rec[k]
    return None


# ---------------------------------------------------------------- fetch

def _fetch_from_gateway(limit: int, since=None) -> List[Dict[str, Any]]:
    """GET pe IRIS Gateway, reutilizand cheia Cargo360 (X-Mailguard-Key), exact ca la
    cts_groundtruth_sync. Read-only. Filtru rolling via ?since=<ISO8601>, ordine updated_at ASC."""
    from app.config import get_settings
    from app.services.iris_http import get_with_retry
    base = (get_settings().iris_api_url or "").rstrip("/")
    key = os.getenv("IRIS_MAILGUARD_API_KEY", "")
    if not base or not key:
        raise RuntimeError("Gateway IRIS neconfigurat (iris_api_url / IRIS_MAILGUARD_API_KEY).")
    params = {"limit": limit}
    if since is not None:
        params["since"] = since if isinstance(since, str) else since.isoformat()
    # Reincercare pe 5xx/transport: gateway-ul IRIS are pene de cateva secunde (vezi iris_http).
    r = get_with_retry(base + GATEWAY_PATH, params=params,
                       headers={"X-Mailguard-Key": key}, label=GATEWAY_PATH)
    data = r.json()
    if isinstance(data, list):
        return data
    return data.get("items") or data.get("records") or data.get("data") or []


# ---------------------------------------------------------------- upsert

_UPSERT_SQL = text("""
    INSERT INTO cts_calls_ground_truth(
        call_local_id, cts_call_id, cts_category, cts_status,
        cts_assignee_email, cts_assignee_name, cts_assignee_id, cts_assigned_at,
        cts_response_seconds, cts_started_at, cts_duration_seconds, cts_client_id,
        source, raw, fetched_at, last_synced_at)
    VALUES(
        :call_local_id, :cts_call_id, :cts_category, :cts_status,
        :cts_assignee_email, :cts_assignee_name, :cts_assignee_id, :cts_assigned_at,
        :cts_response_seconds, :cts_started_at, :cts_duration_seconds, :cts_client_id,
        :source, CAST(:raw AS jsonb), now(), now())
    ON CONFLICT (source, cts_call_id) DO UPDATE SET
        call_local_id = COALESCE(cts_calls_ground_truth.call_local_id, EXCLUDED.call_local_id),
        cts_client_id = COALESCE(EXCLUDED.cts_client_id, cts_calls_ground_truth.cts_client_id),
        cts_category_prev = CASE
            WHEN cts_calls_ground_truth.cts_category IS DISTINCT FROM EXCLUDED.cts_category
            THEN cts_calls_ground_truth.cts_category ELSE cts_calls_ground_truth.cts_category_prev END,
        changed_at = CASE
            WHEN cts_calls_ground_truth.cts_category IS DISTINCT FROM EXCLUDED.cts_category
            THEN now() ELSE cts_calls_ground_truth.changed_at END,
        cts_category = EXCLUDED.cts_category,
        cts_status = EXCLUDED.cts_status,
        cts_assignee_email = EXCLUDED.cts_assignee_email,
        cts_assignee_name = EXCLUDED.cts_assignee_name,
        cts_assignee_id = EXCLUDED.cts_assignee_id,
        cts_assigned_at = EXCLUDED.cts_assigned_at,
        cts_response_seconds = EXCLUDED.cts_response_seconds,
        cts_started_at = EXCLUDED.cts_started_at,
        cts_duration_seconds = EXCLUDED.cts_duration_seconds,
        raw = EXCLUDED.raw,
        last_synced_at = now()
    RETURNING (xmax = 0) AS inserted
""")


def _normalize_record(rec: Dict[str, Any], cat_id_map: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    cts_call_id = str(_g(rec, "cts_call_id") or "").strip()
    calltrack_id = _g(rec, "calltrack_id")
    return {
        "cts_call_id": cts_call_id or None,
        "_ctk_uniqueid": _g(rec, "ctk_uniqueid"),           # doar pt matching, nu se persista
        "_calltrack_id": str(calltrack_id).strip() if calltrack_id is not None else None,
        "cts_category": _map_category_id(rec.get("category_id"), cat_id_map),
        "cts_status": _g(rec, "status"),
        "cts_assignee_email": (str(_g(rec, "assignee_email") or "").strip() or None),
        "cts_assignee_name": _g(rec, "assignee_name"),
        "cts_assignee_id": rec.get("assignee_id"),
        "cts_assigned_at": _g(rec, "assigned_at"),
        "cts_response_seconds": rec.get("ring_seconds"),
        "cts_started_at": _g(rec, "started_at"),
        "cts_duration_seconds": rec.get("duration_seconds"),
        "raw": rec,
    }


def _match_call_local_id(db, ctk_uniqueid, calltrack_id) -> Optional[int]:
    """PRIMAR: ctk_uniqueid == calls.while1_uniqueid (recomandat de Razvan, stabil PBX).
    FALLBACK: calltrack_id == calls.call_id (id numeric While1)."""
    if ctk_uniqueid:
        row = db.execute(text("SELECT id FROM calls WHERE while1_uniqueid=:u"),
                         {"u": ctk_uniqueid}).fetchone()
        if row:
            return row[0]
    if calltrack_id:
        row = db.execute(text("SELECT id FROM calls WHERE call_id=:c"),
                         {"c": calltrack_id}).fetchone()
        if row:
            return row[0]
    return None


def _upsert_records(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    ins = upd = skipped = changed = matched = linked = 0
    cat_id_map = _load_cat_id_map()
    db = SessionLocal()
    try:
        for rec in records:
            try:
                n = _normalize_record(rec, cat_id_map)
            except Exception as e:
                logger.warning("cts_calls_gt normalize failed: %s", e)
                skipped += 1
                continue
            if not n.get("cts_call_id"):
                skipped += 1
                continue
            call_local_id = _match_call_local_id(db, n.pop("_ctk_uniqueid"), n.pop("_calltrack_id"))
            if call_local_id:
                matched += 1
            n["call_local_id"] = call_local_id
            n["source"] = SOURCE
            # client_id CTS (= clients.iris_client_id) — citit ÎNAINTE de serializarea raw.
            cts_client_id = None
            try:
                _raw_cid = (n["raw"] or {}).get("client_id")
                if _raw_cid not in (None, "", 0, "0"):
                    cts_client_id = int(_raw_cid)
            except (TypeError, ValueError):
                cts_client_id = None
            # Persistat pe rand: lista trebuie sa poata afisa clientul si pentru apelurile CTS
            # fara corespondent While1 (call_local_id NULL), unde join-ul pe calls da NULL.
            n["cts_client_id"] = cts_client_id
            n["raw"] = json.dumps(n["raw"])
            try:
                row = db.execute(_UPSERT_SQL, n).fetchone()
                if row and row[0]:
                    ins += 1
                else:
                    upd += 1
            except Exception as e:
                logger.warning("cts_calls_gt upsert failed cts_call_id=%s: %s", n.get("cts_call_id"), e)
                skipped += 1
                continue
            # Forward-fix: CTS e sursa autoritativă pentru legătura apel↔client. Propagăm imediat
            # în calls.client_id, altfel rămâne pe seama unui backfill ulterior (10.935 apeluri
            # orfane la 2026-07-29). Doar completăm NULL-uri — nu suprascriem o legătură existentă.
            if call_local_id and cts_client_id:
                try:
                    res = db.execute(text("""
                        UPDATE calls c SET client_id = cl.id, updated_at = NOW()
                        FROM clients cl
                        WHERE c.id = :cid AND c.client_id IS NULL
                          AND cl.iris_client_id = :ics
                    """), {"cid": call_local_id, "ics": cts_client_id})
                    linked += (res.rowcount or 0)
                except Exception as e:
                    logger.warning("cts_calls_gt client link failed call_id=%s: %s", call_local_id, e)
        db.commit()
    finally:
        db.close()
    return {"ok": True, "inserted": ins, "updated": upd, "changed": changed, "matched_local": matched,
            "clients_linked": linked, "skipped": skipped, "fetched": len(records)}


# ---------------------------------------------------------------- entrypoints

def sync_ground_truth(limit: int = 5000, since=None) -> Dict[str, Any]:
    """Trage ground-truth-ul CTS pentru apeluri si il upsert-eaza. Best-effort, idempotent.

    `since=None` NU inseamna "tot": sursa livreaza in ordine updated_at ASC si taie la `limit`,
    deci fara ancora primeam cele mai VECHI apeluri (verificat 2026-08-04: limit=5000 fara since
    returna pana la cts_call_id=5148, started 2020-04-10) si niciodata pe cele recente. Ancoram
    la FULL_SYNC_MAX_DAYS ca sync-ul "complet" sa acopere si prezentul.
    """
    if since is None:
        since = _source_now() - _dt.timedelta(days=FULL_SYNC_MAX_DAYS)
    if not _gateway_configured():
        return {"ok": False, "inserted": 0, "updated": 0,
                "reason": "Gateway IRIS neconfigurat (IRIS_MAILGUARD_API_KEY / iris_api_url)."}
    if not _kv_enabled():
        return {"ok": False, "inserted": 0, "updated": 0,
                "reason": "Sync CTS apeluri dezactivat (settings['cts_calls_gt.sync_enabled'] != 1)."}
    try:
        records = _fetch_from_gateway(limit, since=since)
    except Exception as e:
        logger.warning("cts_calls_gt fetch failed: %s", e)
        return {"ok": False, "inserted": 0, "updated": 0,
                "reason": "Eroare la citirea sursei CTS: %s" % e}
    return _upsert_records(records)


def _source_now() -> _dt.datetime:
    """"Acum" in cadranul de timp al sursei CTS, ales ca sa NU pierdem apeluri.

    Endpointul /cts/calls compara `since` LITERAL cu `updated_at`, iar `updated_at` din sursa
    e INCONSECVENT intre doua fusuri (verificat empiric 2026-08-04, ceas UTC 06:11 / local 09:11):
      - apel abia intrat, neatins de operator (status='new'): updated_at == started_at, ambele UTC
        ex. cts_call_id=721493 started=06:10:14 upd=06:10:14
      - apel atins/modificat de operator: updated_at rescris in ora LOCALA Romania
        ex. cts_call_id=721447 started=04:51:23 upd=07:54:25 (in "viitor" fata de UTC-ul curent)
    Adica updated_at e UTC la INSERT si devine local la UPDATE.

    Consecinta pe versiunea anterioara (aliniere la ora locala): pentru un apel nou, `since` local
    era cu 3h INAINTEA lui updated_at=UTC, deci apelul cadea din fereastra si devenea vizibil abia
    cand operatorul il atingea si updated_at sarea in local -- de aici impresia de "sync agatat"
    peste noapte / decalaj de cateva ore pe pagina Apeluri CTS.

    Fix: ancoram in cadranul cel mai DEVREME (UTC). Fereastra iese mai larga cu offset-ul zonei
    (3h vara / 2h iarna), deci acopera ambele variante de updated_at. Costul e nul: upsert-ul e
    idempotent pe UNIQUE(source, cts_call_id), iar volumul e ~350 apeluri/zi.
    NU inlocui cu ora locala fara a verifica din nou ambele forme de updated_at in payload.
    """
    return _dt.datetime.utcnow()


def sync_recent(hours: int = RECENT_WINDOW_HOURS, limit: int = 5000) -> Dict[str, Any]:
    if not is_enabled():
        return sync_ground_truth(limit=limit)
    since = _source_now() - _dt.timedelta(hours=max(1, hours))
    res = sync_ground_truth(limit=limit, since=since)
    if isinstance(res, dict):
        res["window_hours"] = hours
        res["window_since"] = since.isoformat()
    return res


def sync_recent_guarded(hours=RECENT_WINDOW_HOURS, limit=5000):
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
    """Apelat de cron (POST /process/run-now, la 5 min). No-op ieftin daca sync-ul nu e activ
    sau daca ultimul sync rolling a fost acum < RECENT_MIN_INTERVAL_S. Nu arunca niciodata."""
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
        logger.info("cts_calls_gt rolling sync: %s", res)
        return res
    except Exception as e:
        logger.warning("cts_calls_gt run_recent_if_due failed: %s", e)
        return {"ok": False, "error": str(e)[:200]}
