"""IRIS Data Views — proxy + sincronizare snapshot locală.

Toate view-urile CTS vin prin https://iris.cargotrack.ro/api/dv/*.
Cheia API se stochează în settings(key='iris_dv.api_key').
Sincronizarea e mode=snapshot: freshness ETag → înlocuire integrală tabelă locală.
Fereastra de date: 2026-01-01 → azi, cu refresh pe ultimele 10 zile (overlap).
"""
import json
import logging
import re
import time
import random
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.api.v1.auth import get_current_admin

logger = logging.getLogger(__name__)
router = APIRouter()

DV_BASE = "https://iris.cargotrack.ro/api/dv"
APP_NAME = "mailguard-staging"
SYNC_FROM = "2026-01-01"  # data de start import date CTS


# ─── helpers ────────────────────────────────────────────────────────────────

def _get_api_key(db: Session) -> Optional[str]:
    row = db.execute(
        text("SELECT value FROM settings WHERE key='iris_dv.api_key'")
    ).fetchone()
    if not row:
        return None
    val = row._mapping["value"]
    if isinstance(val, str):
        return val.strip('"')
    if isinstance(val, dict):
        return val.get("key")
    return None


def _dv_headers(api_key: str) -> dict:
    return {
        "X-Api-Key": api_key,
        "X-App-Name": APP_NAME,
        "Accept-Encoding": "gzip",
    }


def _require_key(db: Session) -> str:
    key = _get_api_key(db)
    if not key:
        raise HTTPException(
            status_code=403,
            detail="Cheia API IRIS Data Views nu este configurată. Adaugă-o în pagina 'Surse date'."
        )
    return key


def _update_state(db: Session, view_name: str, **kwargs):
    kwargs["updated_at"] = datetime.now(timezone.utc)
    cols = ", ".join(f"{k}=:{k}" for k in kwargs)
    db.execute(
        text(f"""
            INSERT INTO iris_dv_state (view_name, {', '.join(kwargs)})
            VALUES (:view_name, {', '.join(':' + k for k in kwargs)})
            ON CONFLICT (view_name) DO UPDATE SET {cols}
        """),
        {"view_name": view_name, **kwargs}
    )
    db.commit()


def _get_state(db: Session, view_name: str) -> dict:
    row = db.execute(
        text("SELECT * FROM iris_dv_state WHERE view_name=:v"),
        {"v": view_name}
    ).fetchone()
    if not row:
        return {}
    return dict(row._mapping)


def _http_get_with_retry(url: str, headers: dict, extra_headers: dict = None) -> httpx.Response:
    h = {**headers, **(extra_headers or {})}
    backoff = 1
    last_exc = None
    for attempt in range(5):
        try:
            resp = httpx.get(url, headers=h, timeout=30, follow_redirects=True)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "10"))
                time.sleep(retry_after)
                continue
            if resp.status_code >= 500:
                jitter = backoff * (0.8 + 0.4 * random.random())
                time.sleep(min(jitter, 60))
                backoff = min(backoff * 2, 60)
                continue
            return resp
        except Exception as exc:
            last_exc = exc
            jitter = backoff * (0.8 + 0.4 * random.random())
            time.sleep(min(jitter, 60))
            backoff = min(backoff * 2, 60)
    raise RuntimeError(f"Toate cele 5 încercări au eșuat pentru {url}: {last_exc}")


# ─── API key CRUD ────────────────────────────────────────────────────────────

@router.get("/iris-dv/config")
def get_dv_config(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Returnează dacă cheia e configurată (nu cheia în sine)."""
    key = _get_api_key(db)
    return {"configured": bool(key), "masked": ("***" + key[-4:]) if key and len(key) > 4 else None}


@router.put("/iris-dv/config")
def set_dv_config(body: dict, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Salvează cheia API. Body: {api_key: string}"""
    api_key = (body.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key lipsă")
    db.execute(
        text("""
            INSERT INTO settings(key, value) VALUES ('iris_dv.api_key', CAST(:v AS jsonb))
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
        """),
        {"v": json.dumps(api_key)}
    )
    db.commit()
    return {"ok": True}


# ─── Onboarding — lista view-uri ─────────────────────────────────────────────

@router.get("/iris-dv/views")
def list_views(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Listează view-urile disponibile de pe /onboarding + starea locală."""
    api_key = _require_key(db)
    try:
        resp = _http_get_with_retry(f"{DV_BASE}/onboarding", _dv_headers(api_key))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    if resp.status_code == 401:
        raise HTTPException(status_code=403, detail="Cheie API invalidă — verifică configurarea.")
    if resp.status_code not in (200,):
        raise HTTPException(status_code=502, detail=f"IRIS DV răspuns {resp.status_code}")

    data = resp.json()
    views = data.get("views", [])

    # enrichează cu starea locală
    for v in views:
        name = v.get("name") or v.get("view_name") or ""
        state = _get_state(db, name)
        v["local_state"] = {
            "last_sync_at": state.get("last_sync_at").isoformat() if state.get("last_sync_at") else None,
            "last_error": state.get("last_error"),
            "total_rows": state.get("total_rows"),
            "freshness_at": state.get("freshness_at").isoformat() if state.get("freshness_at") else None,
            "etag": bool(state.get("etag")),
        }

    return {"views": views, "links": data.get("links", {})}


# ─── Prompt per view ──────────────────────────────────────────────────────────

@router.get("/iris-dv/views/{view_name}/prompt")
def get_view_prompt(view_name: str, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    api_key = _require_key(db)
    try:
        resp = _http_get_with_retry(f"{DV_BASE}/{view_name}/prompt", _dv_headers(api_key))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if resp.status_code == 401:
        raise HTTPException(status_code=403, detail="Cheie API invalidă.")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"View '{view_name}' inexistent sau inaccesibil.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"IRIS DV răspuns {resp.status_code}")
    return resp.json()


# ─── Freshness per view ───────────────────────────────────────────────────────

@router.get("/iris-dv/views/{view_name}/freshness")
def get_view_freshness(view_name: str, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    api_key = _require_key(db)
    state = _get_state(db, view_name)
    try:
        resp = _http_get_with_retry(
            f"{DV_BASE}/{view_name}/freshness",
            _dv_headers(api_key),
            {"If-None-Match": state.get("etag", "")} if state.get("etag") else {}
        )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if resp.status_code == 304:
        return {"fresh": True, "not_modified": True, "state": {
            "last_sync_at": state.get("last_sync_at").isoformat() if state.get("last_sync_at") else None,
            "total_rows": state.get("total_rows"),
            "freshness_at": state.get("freshness_at").isoformat() if state.get("freshness_at") else None,
        }}
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"IRIS DV răspuns {resp.status_code}")
    return {**resp.json(), "fresh": False, "not_modified": False}


# ─── Sync snapshot ────────────────────────────────────────────────────────────

_IDENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,60}$")


def _validate_view_name(view_name: str) -> str:
    """Numele de view devine identificator SQL (nume de tabelă) — un identificator
    nu poate fi trecut prin bind param, deci se validează strict la intrare."""
    if not _IDENT_RE.match(view_name or ""):
        raise HTTPException(status_code=400, detail="Nume de view invalid")
    return view_name


def _local_table_name(view_name: str) -> str:
    _validate_view_name(view_name)
    safe = view_name.replace("-", "_").replace(".", "_")
    return f"cts_dv_{safe}"


def _create_local_table_if_needed(db: Session, view_name: str, columns: list):
    tbl = _local_table_name(view_name)
    # gardă defensivă — apelantul filtrează deja, dar identificatorii nu pot fi bind params
    safe_cols = [c for c in columns if c != "id" and _IDENT_RE.match(c or "")]
    col_defs = ", ".join(f'"{c}" TEXT' for c in safe_cols)
    db.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {tbl} (
            "id" TEXT NOT NULL PRIMARY KEY,
            {col_defs}
        )
    """))
    # Drift de schemă: `CREATE TABLE IF NOT EXISTS` nu adaugă coloanele apărute în view DUPĂ
    # prima sincronizare (sau declarate într-o migrație cu mai puține coloane), iar INSERT-ul
    # de mai jos le enumeră pe toate -> sync-ul ar pica pe „column does not exist".
    for c in safe_cols:
        db.execute(text(f'ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS "{c}" TEXT'))
    db.commit()


def _sync_view_snapshot(view_name: str, api_key: str, db: Session):
    """Algoritmul §A.5 mode=snapshot — înlocuire integrală atomică."""
    state = _get_state(db, view_name)
    etag = state.get("etag") or ""

    # 1. freshness check
    try:
        fresh_resp = _http_get_with_retry(
            f"{DV_BASE}/{view_name}/freshness",
            _dv_headers(api_key),
            {"If-None-Match": etag} if etag else {}
        )
    except RuntimeError as e:
        _update_state(db, view_name, last_error=str(e), last_error_at=datetime.now(timezone.utc))
        raise

    # versiune schemă
    remote_schema_ver = int(fresh_resp.headers.get("X-DV-Schema-Version", 0) or 0)
    remote_prompt_ver = int(fresh_resp.headers.get("X-DV-Prompt-Version", 0) or 0)

    if fresh_resp.status_code == 304:
        logger.info("iris_dv sync %s: 304 not modified, skip", view_name)
        return {"skipped": True, "reason": "not_modified"}

    if fresh_resp.status_code != 200:
        msg = f"freshness răspuns {fresh_resp.status_code}"
        _update_state(db, view_name, last_error=msg, last_error_at=datetime.now(timezone.utc))
        raise RuntimeError(msg)

    fresh_data = fresh_resp.json()
    new_freshness_at = fresh_data.get("view_updated_at")
    total_rows_hint = fresh_data.get("total_rows")

    # 2. paginare completă /data cu since=SYNC_FROM și overlap 10 zile
    overlap_date = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")
    all_rows = []
    cursor = None
    new_etag = fresh_resp.headers.get("ETag") or fresh_resp.headers.get("etag") or ""
    columns_seen = None

    page_num = 0
    while True:
        url = f"{DV_BASE}/{view_name}/data?since={SYNC_FROM}&limit=10000"
        if cursor:
            url += f"&cursor={cursor}"
        try:
            resp = _http_get_with_retry(f"{DV_BASE}/{view_name}/data", _dv_headers(api_key),
                                         {"X-DV-Schema-Version": str(remote_schema_ver)} if remote_schema_ver else {})
        except RuntimeError as e:
            _update_state(db, view_name, last_error=str(e), last_error_at=datetime.now(timezone.utc))
            raise

        # reconstruiesc URL cu parametri (httpx nu acceptă URL cu parametri și extra headers)
        params = {"since": SYNC_FROM, "limit": "10000"}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = httpx.get(
                f"{DV_BASE}/{view_name}/data",
                params=params,
                headers={**_dv_headers(api_key)},
                timeout=60,
                follow_redirects=True
            )
        except Exception as e:
            _update_state(db, view_name, last_error=str(e), last_error_at=datetime.now(timezone.utc))
            raise

        if resp.status_code == 410:
            # cursor expirat sau view apus
            _update_state(db, view_name, etag=None, last_error="410 cursor expirat — re-sync complet la următoarea rulare")
            raise RuntimeError("410 Gone — re-sync necesar")

        if resp.status_code != 200:
            msg = f"data răspuns {resp.status_code}: {resp.text[:200]}"
            _update_state(db, view_name, last_error=msg, last_error_at=datetime.now(timezone.utc))
            raise RuntimeError(msg)

        payload = resp.json()
        rows = payload if isinstance(payload, list) else payload.get("rows", [])
        has_more = payload.get("has_more", False) if isinstance(payload, dict) else False
        cursor = payload.get("next_cursor") if isinstance(payload, dict) else None

        if rows and columns_seen is None:
            # numele de coloane ajung în DDL/DML ca identificatori — se filtrează
            # o singură dată aici, ca CREATE și INSERT să rămână consistente.
            raw_cols = list(rows[0].keys())
            columns_seen = [c for c in raw_cols if _IDENT_RE.match(c or "")]
            if len(columns_seen) != len(raw_cols):
                logger.warning("iris_dv %s: coloane cu nume invalid ignorate: %s",
                               view_name, [c for c in raw_cols if c not in columns_seen])

        all_rows.extend(rows)
        page_num += 1
        logger.info("iris_dv sync %s: pagina %d, %d rânduri acum", view_name, page_num, len(all_rows))

        if not has_more or not cursor:
            break

    if not columns_seen:
        columns_seen = ["id"]

    # 3. înlocuire integrală atomică
    tbl = _local_table_name(view_name)
    _create_local_table_if_needed(db, view_name, columns_seen)

    with db.begin_nested():
        db.execute(text(f'DELETE FROM {tbl}'))
        if all_rows:
            cols_quoted = ", ".join(f'"{c}"' for c in columns_seen)
            placeholders = ", ".join(f":{c}" for c in columns_seen)
            for row in all_rows:
                row_data = {c: (str(row[c]) if row.get(c) is not None else None) for c in columns_seen}
                db.execute(
                    text(f'INSERT INTO {tbl} ({cols_quoted}) VALUES ({placeholders}) ON CONFLICT ("id") DO NOTHING'),
                    row_data
                )

    # 4. salvează starea (în aceeași tranzacție cu datele — commit separat)
    _update_state(db, view_name,
        etag=new_etag,
        last_sync_at=datetime.now(timezone.utc),
        last_error=None,
        last_error_at=None,
        schema_version=remote_schema_ver,
        prompt_version=remote_prompt_ver,
        total_rows=len(all_rows),
        freshness_at=new_freshness_at,
        mode="snapshot"
    )
    db.commit()

    return {
        "synced": True,
        "rows_loaded": len(all_rows),
        "pages": page_num,
        "schema_version": remote_schema_ver,
    }


@router.post("/iris-dv/views/{view_name}/sync")
def trigger_sync(view_name: str, background_tasks: BackgroundTasks,
                 db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Declanșează sincronizare snapshot (mode=snapshot) pentru un view."""
    _validate_view_name(view_name)
    api_key = _require_key(db)

    def _run():
        db2 = next(get_db())
        try:
            _sync_view_snapshot(view_name, api_key, db2)
            # După sync vacation_request, populează employee_schedule (vacation_approved)
            if view_name == "employee_vacation_request":
                try:
                    from app.services.iris_employee_sync import sync_vacation_from_dv
                    n = sync_vacation_from_dv(db2)
                    logger.info("iris_dv post-sync vacation_approved: %d rows written", n)
                except Exception as ve:
                    logger.warning("post-sync vacation_from_dv failed: %s", ve)
        except Exception as e:
            logger.error("iris_dv sync %s failed: %s", view_name, e)
        finally:
            db2.close()

    background_tasks.add_task(_run)
    return {"ok": True, "message": f"Sincronizare pornită pentru {view_name}"}


@router.get("/iris-dv/views/{view_name}/sync-status")
def get_sync_status(view_name: str, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Returnează starea curentă a sincronizării."""
    state = _get_state(db, view_name)
    return {
        "view_name": view_name,
        "last_sync_at": state.get("last_sync_at").isoformat() if state.get("last_sync_at") else None,
        "last_error": state.get("last_error"),
        "total_rows": state.get("total_rows"),
        "freshness_at": state.get("freshness_at").isoformat() if state.get("freshness_at") else None,
        "schema_version": state.get("schema_version"),
        "etag_present": bool(state.get("etag")),
    }


@router.get("/iris-dv/states")
def get_all_states(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Returnează starea tuturor view-urilor sincronizate local."""
    rows = db.execute(text("SELECT * FROM iris_dv_state ORDER BY view_name")).fetchall()
    result = []
    for r in rows:
        d = dict(r._mapping)
        for k in ("last_sync_at", "last_error_at", "freshness_at", "created_at", "updated_at"):
            if d.get(k):
                d[k] = d[k].isoformat()
        d["etag_present"] = bool(d.pop("etag", None))
        result.append(d)
    return result
