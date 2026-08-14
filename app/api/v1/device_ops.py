"""Modul "Device Operations" — listă + statistici pentru operațiunile pe echipamente,
sincronizate prin IRIS Gateway (GET /cts/device-operations, LIVE din 2026-07-02).

Mirror pe app/api/v1/cts_tasks_training.py (modulul "Task-uri"). Sursa e populată de
app/services/device_ops_sync. Departamentul vine doar prin operator (nicio tabelă sursă
CTS n-are department_id propriu) — distribuția reală e majoritar "Instalari", nu "Suport 2"
(vezi docstring device_ops_sync.py), de-aia list/stats nu filtrează implicit pe departament.
"""
import datetime as _dt
import logging
import threading as _th

from fastapi import APIRouter, Depends, HTTPException, Query, Body, Request
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.api.v1.auth import get_current_admin
from app.api.v1.sorting import sort_dir, sort_expr
from app.services import device_ops_sync as SYNC
from app.services import device_ops_suport2_sync as SUPORT2_SYNC

logger = logging.getLogger("mailguard.device_ops")
router = APIRouter()

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo as _ZoneInfo

_TZ_UTC = _ZoneInfo("UTC")
_TZ_RO = _ZoneInfo("Europe/Bucharest")


def _ts_ro(ts):
    """Convertește timestamp la Europe/Bucharest înainte de isoformat, indiferent de TZ browser."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_TZ_UTC)
    return ts.astimezone(_TZ_RO).isoformat()


def _valid_date(value: str, field: str):
    """Validează o dată ISO (YYYY-MM-DD) — altfel CAST(... AS date) dă 500 în loc de 400."""
    if not value or not str(value).strip():
        return ""
    try:
        return _dt.date.fromisoformat(str(value).strip()).isoformat()
    except (ValueError, AttributeError):
        raise HTTPException(400, f"{field} invalid: se așteaptă formatul YYYY-MM-DD")


def _ops_filters(status: str, department: str, action_type: str,
                 date_from: str = "", date_to: str = "", alias: str = "do_"):
    """WHERE comun pentru listă și statistici, ca informațiile de sus să reflecte filtrele.

    `alias` permite refolosirea în interogările de statistici, care nu au alias pe tabel.
    Perioada se aplică pe data creării operațiunii în CTS (cts_created_at), inclusiv ziua de final.
    """
    a = (alias + ".") if alias else ""
    where = ["1=1"]
    params = {}
    if (status or "").strip():
        where.append(f"{a}status = :status")
        params["status"] = status.strip()
    if (department or "").strip():
        where.append(f"{a}department = :department")
        params["department"] = department.strip()
    if (action_type or "").strip():
        where.append(f"{a}action_type = :action_type")
        params["action_type"] = action_type.strip()
    df = _valid_date(date_from, "date_from")
    dtv = _valid_date(date_to, "date_to")
    if df:
        where.append(f"{a}cts_created_at >= CAST(:date_from AS date)")
        params["date_from"] = df
    if dtv:
        where.append(f"{a}cts_created_at < (CAST(:date_to AS date) + INTERVAL '1 day')")
        params["date_to"] = dtv
    return " AND ".join(where), params


# Coloane sortabile din UI -> expresia SQL. 'default' pastreaza ordinea istorica.
_OPS_SORTS = {
    "default":     "COALESCE(do_.closed_at, do_.cts_updated_at, do_.last_synced_at)",
    "operation":   "do_.operation_id",
    "finished":    "do_.finished_at",
    "closed":      "do_.closed_at",
    "created":     "do_.cts_created_at",
    "client":      "COALESCE(cl.name, do_.client_name)",
    "type":        "do_.action_type",
    "department":  "do_.department",
    "assignee":    "COALESCE(edm.name, do_.assignee_raw)",
    "status":      "do_.status",
    "duration":    "resolution_minutes",
    "device":      "COALESCE(do_.device_serial, do_.device_imei)",
}


@router.get("/device-ops/list")
def device_ops_list(
    status: str = Query("", description="filtru status (gol = toate)"),
    department: str = Query("", description="filtru departament (gol = toate)"),
    action_type: str = Query("", description="filtru tip acțiune (gol = toate)"),
    date_from: str = Query("", description="perioada: data de la (YYYY-MM-DD)"),
    date_to: str = Query("", description="perioada: data pana la, inclusiv (YYYY-MM-DD)"),
    assignee: str = Query("", description="filtru assignee (nume exact, gol = toti)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    sort: str = Query("default", description="coloana de sortare (vezi _OPS_SORTS)"),
    dir: str = Query("desc", description="'asc' | 'desc'"),
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Listă paginată de operațiuni pe echipamente, cu filtre pe status/departament/tip/perioadă/assignee."""
    where_sql, params = _ops_filters(status, department, action_type, date_from, date_to)
    if (assignee or "").strip():
        where_sql += " AND do_.assignee_raw = :assignee"
        params["assignee"] = assignee.strip()

    base = ("FROM device_operations do_ "
            "LEFT JOIN employee_department_mapping edm ON edm.id = do_.assignee_employee_id "
            "LEFT JOIN clients cl ON cl.iris_client_id = do_.client_id "
            "LEFT JOIN productivity_objective po ON po.department = 'suport_2' AND po.tip = 'device_ops' "
            "                                    AND po.categorie = do_.action_type")
    total = db.execute(text("SELECT count(*) " + base + " WHERE " + where_sql), params).scalar()

    params["lim"] = page_size
    params["off"] = (page - 1) * page_size
    rows = db.execute(text(
        "SELECT do_.id, do_.operation_id, do_.action_type, do_.status, do_.terminal, "
        "       COALESCE(cl.name, do_.client_name) AS client_name, "
        "       do_.department, do_.device_serial, do_.device_imei, do_.description, "
        "       do_.assignee_raw, do_.assignee_employee_id, edm.name AS assignee_name, "
        "       do_.cts_created_at, do_.cts_updated_at, "
        "       do_.finished_at, do_.closed_at, do_.closed_by_raw, po.limita_minute, "
        "       CASE WHEN do_.status = 'finalizat' AND do_.cts_created_at IS NOT NULL "
        "                 AND do_.cts_updated_at IS NOT NULL AND do_.assignee_employee_id IS NOT NULL "
        "            THEN business_minutes_emp(edm.department, edm.id, do_.cts_created_at, do_.cts_updated_at, ARRAY[]::date[]) "
        "            ELSE NULL END AS resolution_minutes, "
        "       CASE WHEN do_.finished_at IS NOT NULL AND do_.closed_at IS NOT NULL "
        "            THEN EXTRACT(EPOCH FROM (do_.closed_at - do_.finished_at)) / 60.0 "
        "            ELSE NULL END AS suport2_duration_minutes "
        + base + " WHERE " + where_sql +
        f" ORDER BY {sort_expr(sort, _OPS_SORTS, 'default')} {sort_dir(dir)} NULLS LAST, do_.id {sort_dir(dir)} "
        "LIMIT :lim OFFSET :off"), params).fetchall()

    items = []
    for r in rows:
        m = r._mapping
        items.append({
            "id": m["id"],
            "operation_id": m["operation_id"],
            "action_type": m["action_type"],
            "action_type_label": SYNC.ACTION_TYPE_LABELS.get(m["action_type"], m["action_type"]),
            "status": m["status"],
            "terminal": m["terminal"],
            "client_name": m["client_name"],
            "department": m["department"],
            "device_serial": m["device_serial"],
            "device_imei": m["device_imei"],
            "description": m["description"],
            "resolution_minutes": round(float(m["resolution_minutes"]), 1) if m["resolution_minutes"] is not None else None,
            "assignee_raw": m["assignee_raw"],
            "assignee_name": m["assignee_name"],
            "assignee_resolved": m["assignee_employee_id"] is not None,
            "created_at": _ts_ro(m["cts_created_at"]),
            "updated_at": _ts_ro(m["cts_updated_at"]),
            "finished_at": _ts_ro(m["finished_at"]),
            "closed_at": _ts_ro(m["closed_at"]),
            "closed_by_raw": m["closed_by_raw"],
            "closed_suport2": m["closed_at"] is not None,
            "suport2_duration_minutes": round(float(m["suport2_duration_minutes"]), 1) if m["suport2_duration_minutes"] is not None else None,
            "limita_minute": float(m["limita_minute"]) if m["limita_minute"] is not None else None,
            "within_limit": (m["suport2_duration_minutes"] is not None and m["limita_minute"] is not None
                             and float(m["suport2_duration_minutes"]) <= float(m["limita_minute"]))
                            if (m["suport2_duration_minutes"] is not None and m["limita_minute"] is not None) else None,
        })
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/device-ops/stats")
def device_ops_stats(
    status: str = Query("", description="filtru status (gol = toate)"),
    department: str = Query("", description="filtru departament (gol = toate)"),
    action_type: str = Query("", description="filtru tip acțiune (gol = toate)"),
    date_from: str = Query("", description="perioada: data de la (YYYY-MM-DD)"),
    date_to: str = Query("", description="perioada: data pana la, inclusiv (YYYY-MM-DD)"),
    assignee: str = Query("", description="filtru assignee (nume exact, gol = toti)"),
    db: Session = Depends(get_db), admin=Depends(get_current_admin),
):
    """Statistici pe operațiuni de echipamente, cu ACELEAȘI filtre ca /list, ca informațiile
    din partea de sus să se actualizeze la selectarea intervalului sau a celorlalte filtre."""
    w, params = _ops_filters(status, department, action_type, date_from, date_to, alias="")
    if (assignee or "").strip():
        w += " AND assignee_raw = :assignee"
        params["assignee"] = assignee.strip()
    base = "FROM device_operations WHERE " + w
    total = db.execute(text("SELECT count(*) " + base), params).scalar() or 0
    unresolved_assignee = db.execute(text(
        "SELECT count(*) " + base + " AND assignee_raw IS NOT NULL AND assignee_employee_id IS NULL"
    ), params).scalar() or 0
    by_status = db.execute(text(
        "SELECT COALESCE(status,'necunoscut') AS status, count(*) AS n " + base +
        " GROUP BY 1 ORDER BY n DESC"), params).fetchall()
    by_department = db.execute(text(
        "SELECT COALESCE(department,'necunoscut') AS department, count(*) AS n " + base +
        " GROUP BY 1 ORDER BY n DESC"), params).fetchall()
    by_action_type = db.execute(text(
        "SELECT COALESCE(action_type,'necunoscut') AS action_type, count(*) AS n " + base +
        " GROUP BY 1 ORDER BY n DESC"), params).fetchall()
    last_synced = db.execute(text("SELECT max(last_synced_at) " + base), params).scalar()

    return {
        "total": total,
        "unresolved_assignee": unresolved_assignee,
        "filters": {"status": status or None, "department": department or None,
                    "action_type": action_type or None,
                    "date_from": date_from or None, "date_to": date_to or None},
        "by_status": [{"status": m["status"], "count": m["n"]} for m in (r._mapping for r in by_status)],
        "by_department": [{"department": m["department"], "count": m["n"]} for m in (r._mapping for r in by_department)],
        "by_action_type": [{"action_type": m["action_type"],
                             "label": SYNC.ACTION_TYPE_LABELS.get(m["action_type"], m["action_type"]),
                             "count": m["n"]} for m in (r._mapping for r in by_action_type)],
        "last_synced_at": last_synced.isoformat() if last_synced else None,
        "source_configured": SYNC._source_configured(),
    }


@router.get("/device-ops/assignees")
def device_ops_assignees(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Lista distinctă de assignees (nume) pentru selectul de filtru."""
    rows = db.execute(text(
        "SELECT DISTINCT assignee_raw FROM device_operations "
        "WHERE assignee_raw IS NOT NULL ORDER BY assignee_raw"
    )).fetchall()
    return {"assignees": [r[0] for r in rows]}


@router.post("/device-ops/sync")
def device_ops_sync(limit: int = Query(2000, ge=1, le=5000),
                    admin=Depends(get_current_admin)):
    """Sync complet (o singură pagină, `limit` rânduri). Grațios (ok:False + reason) dacă
    endpoint-ul IRIS /cts/device-operations nu există încă."""
    return SYNC.sync_paged(since=None, batch=limit, max_batches=1)


@router.post("/device-ops/sync-recent")
def device_ops_sync_recent(hours: int = Query(24, ge=1, le=168),
                           wait: bool = Query(False),
                           admin=Depends(get_current_admin)):
    """Re-sincronizează fereastra rolling (default 24h). Grațios până IRIS expune endpoint-ul."""
    if wait:
        return SYNC.sync_recent(hours=hours)
    _th.Thread(target=SYNC.sync_recent_guarded, kwargs={"hours": hours}, daemon=True).start()
    return {"ok": True, "started": True, "async": True, "window_hours": hours,
            "message": "Sync pornit în fundal. Lista se actualizează în câteva momente."}


@router.get("/device-ops/sync-config")
def device_ops_sync_config(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    return SYNC.get_sync_config(db)


@router.put("/device-ops/sync-config")
def device_ops_set_sync_config(payload: dict = Body(...),
                               db: Session = Depends(get_db),
                               admin=Depends(get_current_admin)):
    return SYNC.set_sync_config(db, bool(payload.get("enabled")))


@router.post("/device-ops/suport2/sync")
def device_ops_suport2_sync(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Truncate + repopulare device_operations din view_device_operations (whitelist Suport 2).
    Inlocuieste complet sursa veche (montatori) — vezi app/services/device_ops_suport2_sync.py."""
    return SUPORT2_SYNC.run_full_sync(db)


@router.post("/device-ops/suport2/sync-internal")
def device_ops_suport2_sync_internal(request: Request, db: Session = Depends(get_db)):
    """Endpoint intern pentru cron — accesibil doar din localhost (fără auth JWT)."""
    from fastapi import Request as _Req
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="localhost only")
    return SUPORT2_SYNC.run_full_sync(db)
