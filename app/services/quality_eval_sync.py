"""Sincronizare reclamatii CTS (Quality Evaluation) din IRIS DV -> cts_quality_evaluation.

SURSA: view-ul `quality_evaluation` din IRIS Data Views (expus de Razvan, 2026-08-13), acelasi
canal si aceeasi cheie ca `view_device_operations`. E tabela CTS bruta: doar id-uri, fara nume.
Traducerea id-urilor de admin in angajatii nostri se face la interogare, prin
`cts_dv_employee.admin_id` -> email -> `employee_department_mapping`.

CE INSEAMNA DATELE: un rand = o reclamatie inregistrata in CTS pe o lucrare deja facuta
(email, task sau apel). Sint "Reclamatiile" de pe Monitorul Operational si intra in
productivitatea Suport 3.

FEREASTRA: din SYNC_FROM (1 iulie 2026) pana azi. View-ul accepta `since`, dar filtreaza pe
`updated_at`, deci o reclamatie mai veche care se misca azi reintra corect in fereastra.

CADENTA: throttle intern de RECENT_MIN_INTERVAL_S secunde, apelat din lantul de cron
(POST /process/run-now). Cadenta reala e cea a cronului de pe server -- vezi nota de la
`run_recent_if_due`.

IDEMPOTENT: upsert pe `id` (cheia din CTS). O reclamatie care isi schimba statusul se rescrie,
nu se dubleaza. Nu stergem nimic local: `deleted_at` vine din sursa si se pastreaza ca atare.
"""
from __future__ import annotations

import json
import logging
import threading
import datetime as _dt
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.api.v1.iris_dv import DV_BASE, _dv_headers, _get_api_key

logger = logging.getLogger("mailguard.quality_eval_sync")

VIEW_NAME = "quality_evaluation"
SYNC_FROM = "2026-07-01"
LAST_RECENT_KEY = "quality_eval.last_recent_sync_at"
LAST_RESULT_KEY = "quality_eval.last_result"

# 50s, ca la sync-ul de mailuri: destul de mic incat NICIUN tick de cron sa nu fie aruncat de
# throttle, deci reclamatiile apar cu intirzierea cronului, nu cu inca una peste. Cadenta ceruta
# (1 min) se obtine daca si cronul serverului bate la 1 min.
RECENT_MIN_INTERVAL_S = 50

_recent_lock = threading.Lock()

# Coloanele preluate din view, in ordinea din INSERT. Tinute explicit ca un cimp nou aparut in
# view sa nu intre tacit in tabela locala cu alt inteles.
_FIELDS = (
    "id", "entity", "entity_id", "client_id", "category_id", "responsible_id",
    "department_id", "status", "score", "is_according_to_the_procedure",
    "has_modification", "observations", "created_at", "created_by",
    "in_progress_at", "solved_at", "updated_at", "updated_by", "deleted_at", "deleted_by",
)

_INT_FIELDS = {"id", "entity_id", "client_id", "category_id", "responsible_id", "department_id",
               "status", "score", "is_according_to_the_procedure", "has_modification",
               "created_by", "updated_by", "deleted_by"}
_TS_FIELDS = {"created_at", "in_progress_at", "solved_at", "updated_at", "deleted_at"}


def _coerce(field: str, value):
    if value in (None, ""):
        return None
    if field in _INT_FIELDS:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if field in _TS_FIELDS:
        # View-ul trimite ISO naiv, in UTC (ca restul feed-urilor CTS).
        try:
            return _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return value


def _fetch_all_rows(api_key: str) -> List[Dict[str, Any]]:
    """Paginare completa /data cu `since`. Continua pe `next_cursor` prezent, nu pe `has_more`
    (poate fi None cu date ramase -- bug confirmat pe view-ul de device operations)."""
    all_rows: List[Dict[str, Any]] = []
    cursor = None
    page = 0
    while True:
        params = {"limit": "5000", "since": SYNC_FROM}
        if cursor:
            params["cursor"] = cursor
        resp = httpx.get(f"{DV_BASE}/{VIEW_NAME}/data", params=params,
                         headers=_dv_headers(api_key), timeout=60, follow_redirects=True)
        if resp.status_code != 200:
            raise RuntimeError(f"{VIEW_NAME}/data raspuns {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()
        rows = payload if isinstance(payload, list) else payload.get("rows", [])
        cursor = payload.get("next_cursor") if isinstance(payload, dict) else None
        all_rows.extend(rows)
        page += 1
        if not rows or not cursor or page >= 50:
            break
    return all_rows


_UPSERT_SQL = text("""
    INSERT INTO cts_quality_evaluation
        (id, entity, entity_id, client_id, category_id, responsible_id, department_id,
         status, score, is_according_to_the_procedure, has_modification, observations,
         created_at, created_by, in_progress_at, solved_at, updated_at, updated_by,
         deleted_at, deleted_by, synced_at)
    VALUES
        (:id, :entity, :entity_id, :client_id, :category_id, :responsible_id, :department_id,
         :status, :score, :is_according_to_the_procedure, :has_modification, :observations,
         :created_at, :created_by, :in_progress_at, :solved_at, :updated_at, :updated_by,
         :deleted_at, :deleted_by, now())
    ON CONFLICT (id) DO UPDATE SET
        entity = EXCLUDED.entity, entity_id = EXCLUDED.entity_id,
        client_id = EXCLUDED.client_id, category_id = EXCLUDED.category_id,
        responsible_id = EXCLUDED.responsible_id, department_id = EXCLUDED.department_id,
        status = EXCLUDED.status, score = EXCLUDED.score,
        is_according_to_the_procedure = EXCLUDED.is_according_to_the_procedure,
        has_modification = EXCLUDED.has_modification, observations = EXCLUDED.observations,
        created_at = EXCLUDED.created_at, created_by = EXCLUDED.created_by,
        in_progress_at = EXCLUDED.in_progress_at, solved_at = EXCLUDED.solved_at,
        updated_at = EXCLUDED.updated_at, updated_by = EXCLUDED.updated_by,
        deleted_at = EXCLUDED.deleted_at, deleted_by = EXCLUDED.deleted_by,
        synced_at = now()
""")


def sync_quality_evaluations(db: Session) -> Dict[str, Any]:
    """Trage tot ce s-a miscat din SYNC_FROM incoace si face upsert. Nu arunca la esec de retea."""
    api_key = _get_api_key(db)
    if not api_key:
        return {"ok": False, "skipped": "iris_dv.api_key nesetat"}

    try:
        rows = _fetch_all_rows(api_key)
    except Exception as e:
        logger.warning("quality_eval fetch esuat: %s", str(e)[:200])
        return {"ok": False, "error": str(e)[:200]}

    upserted = skipped = 0
    for rec in rows:
        if not isinstance(rec, dict) or rec.get("id") in (None, ""):
            skipped += 1
            continue
        params = {f: _coerce(f, rec.get(f)) for f in _FIELDS}
        if params["id"] is None:
            skipped += 1
            continue
        try:
            db.execute(_UPSERT_SQL, params)
            upserted += 1
        except Exception as e:
            db.rollback()
            logger.warning("quality_eval upsert id=%s esuat: %s", rec.get("id"), str(e)[:200])
            skipped += 1
    db.commit()

    result = {"ok": True, "received": len(rows), "upserted": upserted, "skipped": skipped,
              "since": SYNC_FROM}
    _set_setting(db, LAST_RESULT_KEY, result)
    db.commit()
    logger.info("quality_eval sync: %s", result)
    return result


def _set_setting(db: Session, key: str, value) -> None:
    db.execute(text(
        "INSERT INTO settings(key, value, updated_by, updated_at) "
        "VALUES (:k, CAST(:v AS jsonb), 'cron', now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
        "updated_by = 'cron', updated_at = now()"
    ), {"k": key, "v": json.dumps(value, default=str)})


def _seconds_since_last(db: Session) -> Optional[float]:
    row = db.execute(text("SELECT value FROM settings WHERE key=:k"),
                     {"k": LAST_RECENT_KEY}).fetchone()
    if not row or row[0] is None:
        return None
    try:
        ts = _dt.datetime.fromisoformat(str(row[0]).strip().strip('"'))
        return (_dt.datetime.utcnow() - ts.replace(tzinfo=None)).total_seconds()
    except (ValueError, AttributeError):
        return None


def run_recent_if_due() -> Dict[str, Any]:
    """Apelat din lantul de cron (POST /process/run-now). Nu arunca niciodata.

    Throttle-ul intern e de 50s, deci nu el limiteaza prospetimea -- o limiteaza cadenta cronului
    de pe server. Pentru cerinta de "la 1 minut", cronul trebuie sa bata la 1 minut.
    """
    if not _recent_lock.acquire(blocking=False):
        return {"ok": True, "skipped": "already_running"}
    try:
        db = SessionLocal()
        try:
            elapsed = _seconds_since_last(db)
            if elapsed is not None and elapsed < RECENT_MIN_INTERVAL_S:
                return {"ok": True, "skipped": "throttled", "elapsed_s": int(elapsed)}
            _set_setting(db, LAST_RECENT_KEY, _dt.datetime.utcnow().isoformat())
            db.commit()
            return sync_quality_evaluations(db)
        except Exception as e:
            logger.warning("quality_eval run_recent esuat: %s", str(e)[:200])
            return {"ok": False, "error": str(e)[:200]}
        finally:
            db.close()
    finally:
        _recent_lock.release()
