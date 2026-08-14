"""Modul "Task-uri" — listă + statistici pentru task-urile CTS sincronizate prin IRIS Gateway.

Mirror pe app/api/v1/cts_calls_training.py, adaptat la task-uri. Spre deosebire de mailuri/apeluri,
NU există o axă AI-vs-CTS de comparat (task-urile vin ca fapte direct de la CTS, nu sunt clasificate
independent de Cargo360) — de aceea NU există rute `divergences`/`accuracy-daily` aici, doar listă +
statistici operaționale.

Sursa e populată de app/services/cts_tasks_sync (inert — 404 grațios — până Razvan expune endpoint-ul
GET /cts/tasks pe IRIS Gateway, vezi OUTBOX_tasks_endpoint.md).
"""
import datetime as _dt
import logging
import threading as _th

from fastapi import APIRouter, Depends, HTTPException, Query, Body
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.api.v1.auth import get_current_admin
from app.api.v1.sorting import sort_dir, sort_expr
from app.services.productivity import _extract_device
from app.services import cts_tasks_sync as SYNC

logger = logging.getLogger("mailguard.cts_tasks_training")
router = APIRouter()


def _valid_date(value: str, field: str):
    """Validează o dată ISO (YYYY-MM-DD). Fără asta, o valoare de tip 'abc' ajunge în
    CAST(... AS date) și Postgres ridică DataError => HTTP 500 în loc de 400."""
    if not value or not str(value).strip():
        return ""
    try:
        return _dt.date.fromisoformat(str(value).strip()).isoformat()
    except (ValueError, AttributeError):
        raise HTTPException(400, f"{field} invalid: se așteaptă formatul YYYY-MM-DD")


def _task_filters(status: str, department: str, task_type: str,
                  date_from: str = "", date_to: str = ""):
    """Construieste WHERE-ul comun pentru lista si statistici, ca sa fie garantat identice.

    KPI-urile din capul paginii Task-uri trebuie sa reflecte EXACT aceleasi filtre ca
    tabelul de dedesubt (status/departament/tip/perioada) — de aceea o singura sursa.
    Returneaza (where_sql, params).
    """
    where = ["1=1"]
    params = {}
    if (status or "").strip():
        where.append("gt.status = :status")
        params["status"] = status.strip()
    if (department or "").strip():
        where.append("gt.department = :department")
        params["department"] = department.strip()
    if (task_type or "").strip():
        tt = task_type.strip()
        if tt in SYNC._FAMILY_KEYWORDS:
            # filtru pe familie (cargobox/bgtoll/etoll/hugo) -- multe variante de scriere CTS,
            # comparate pe forma normalizata (fara spatii/liniute/':'), mirror pe SYNC._device_family.
            where.append("regexp_replace(lower(gt.task_type), '[^a-z0-9]', '', 'g') LIKE :tt_like")
            params["tt_like"] = "%" + tt + "%"
        else:
            where.append("gt.task_type = :task_type")
            params["task_type"] = tt
    df = _valid_date(date_from, "date_from")
    dtv = _valid_date(date_to, "date_to")
    if df:
        where.append("gt.cts_created_at >= CAST(:date_from AS date)")
        params["date_from"] = df
    if dtv:
        where.append("gt.cts_created_at < (CAST(:date_to AS date) + INTERVAL '1 day')")
        params["date_to"] = dtv
    return " AND ".join(where), params


def _task_filters_with_assignee(status, department, task_type, date_from, date_to, assignee):
    where_sql, params = _task_filters(status, department, task_type, date_from, date_to)
    if (assignee or "").strip():
        where_sql += " AND gt.assignee_raw = :assignee"
        params["assignee"] = assignee.strip()
    return where_sql, params


# Coloane sortabile din UI -> expresia SQL. 'default' pastreaza ordinea istorica
# (ultima modificare CTS), ca lista sa arate identic cand nu se cere nimic explicit.
# resolution/claim sint alias-uri de SELECT — Postgres accepta alias in ORDER BY.
_TASK_SORTS = {
    "default":    "COALESCE(gt.cts_updated_at, gt.last_synced_at)",
    "task_id":    "gt.iris_task_id",
    "created":    "gt.cts_created_at",
    "updated":    "gt.cts_updated_at",
    "client":     "COALESCE(cl.name, gt.client_name)",
    "title":      "gt.title",
    "type":       "gt.task_type",
    "department": "gt.department",
    "assignee":   "COALESCE(edm.name, gt.assignee_raw)",
    "status":     "gt.status",
    "claim":      "time_to_claim_minutes",
    "resolve":    "time_to_solve_minutes",
}


@router.get("/cts-tasks-training/list")
def cts_tasks_training_list(
    status: str = Query("", description="filtru status (gol = toate)"),
    department: str = Query("", description="filtru departament (gol = toate)"),
    task_type: str = Query("", description="filtru tip task (gol = toate)"),
    date_from: str = Query("", description="perioada: data de la (YYYY-MM-DD)"),
    date_to: str = Query("", description="perioada: data pana la, inclusiv (YYYY-MM-DD)"),
    assignee: str = Query("", description="filtru assignee (email exact, gol = toti)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    sort: str = Query("default", description="coloana de sortare (vezi _TASK_SORTS)"),
    dir: str = Query("desc", description="'asc' | 'desc'"),
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Listă paginată de task-uri, cu filtre pe status/departament/tip/perioadă/assignee."""
    where_sql, params = _task_filters_with_assignee(status, department, task_type, date_from, date_to, assignee)

    base = ("FROM cts_task_ground_truth gt "
            "LEFT JOIN employee_department_mapping edm ON edm.id = gt.assignee_employee_id "
            "LEFT JOIN clients cl ON cl.iris_client_id = gt.client_id")
    total = db.execute(text("SELECT count(*) " + base + " WHERE " + where_sql), params).scalar()

    params["lim"] = page_size
    params["off"] = (page - 1) * page_size
    rows = db.execute(text(
        "SELECT gt.id, gt.iris_task_id, gt.task_type, gt.status, gt.priority, "
        "       COALESCE(cl.name, gt.client_name) AS client_name, "
        "       gt.department, gt.title, gt.description, gt.assignee_raw, gt.assignee_employee_id, "
        "       edm.name AS assignee_name, gt.email_id, gt.call_id, "
        "       gt.cts_created_at, gt.cts_updated_at, gt.cts_in_progress_at, "
        "       CASE WHEN lower(COALESCE(gt.status,'')) IN ('solved','closed') AND gt.cts_created_at IS NOT NULL "
        "                 AND gt.cts_updated_at IS NOT NULL AND gt.assignee_employee_id IS NOT NULL "
        "            THEN business_minutes_emp(edm.department, edm.id, gt.cts_created_at, gt.cts_updated_at, ARRAY[]::date[]) "
        "            ELSE NULL END AS resolution_minutes, "
        "       CASE WHEN gt.cts_in_progress_at IS NOT NULL AND gt.cts_created_at IS NOT NULL "
        "                 AND gt.assignee_employee_id IS NOT NULL "
        "            THEN business_minutes_emp(edm.department, edm.id, gt.cts_created_at, gt.cts_in_progress_at, ARRAY[]::date[]) "
        "            ELSE NULL END AS time_to_claim_minutes, "
        "       CASE WHEN gt.cts_in_progress_at IS NOT NULL AND gt.cts_updated_at IS NOT NULL "
        "                 AND lower(COALESCE(gt.status,'')) IN ('solved','closed') "
        "                 AND gt.assignee_employee_id IS NOT NULL "
        "            THEN business_minutes_emp(edm.department, edm.id, gt.cts_in_progress_at, gt.cts_updated_at, ARRAY[]::date[]) "
        "            ELSE NULL END AS time_to_solve_minutes "
        + base +
        " WHERE " + where_sql +
        f" ORDER BY {sort_expr(sort, _TASK_SORTS, 'default')} {sort_dir(dir)} NULLS LAST, gt.id {sort_dir(dir)} "
        "LIMIT :lim OFFSET :off"), params).fetchall()

    items = []
    for r in rows:
        m = r._mapping
        fam = SYNC._device_family(m["task_type"])
        items.append({
            "id": m["id"],
            "task_id": m["iris_task_id"],
            "task_type": m["task_type"],
            "type_family": fam,
            "type_label": SYNC._FAMILY_LABELS.get(fam, m["task_type"]),
            "status": m["status"],
            "priority": m["priority"],
            "client_name": m["client_name"],
            # Task-urile fara client sint legate de un echipament. CTS nu trimite niciun camp
            # de device in /cts/tasks (verificat pe raw_payload), deci numarul se extrage din
            # titlu/descriere — acelasi extractor ca in Productivitate, ca cele doua pagini sa
            # nu arate identificatori diferiti pentru acelasi task.
            "device": _extract_device(m["title"], m["description"]),
            "department": m["department"],
            "title": m["title"],
            "description": m["description"],
            "resolution_minutes": round(float(m["resolution_minutes"]), 1) if m["resolution_minutes"] is not None else None,
            "time_to_claim_minutes": round(float(m["time_to_claim_minutes"]), 1) if m["time_to_claim_minutes"] is not None else None,
            "time_to_solve_minutes": round(float(m["time_to_solve_minutes"]), 1) if m["time_to_solve_minutes"] is not None else None,
            "assignee_raw": m["assignee_raw"],
            "assignee_name": m["assignee_name"],
            "assignee_resolved": m["assignee_employee_id"] is not None,
            "email_id": m["email_id"],
            "call_id": m["call_id"],
            "created_at": m["cts_created_at"].isoformat() if m["cts_created_at"] else None,
            "in_progress_at": m["cts_in_progress_at"].isoformat() if m["cts_in_progress_at"] else None,
            "updated_at": m["cts_updated_at"].isoformat() if m["cts_updated_at"] else None,
        })
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/cts-tasks-training/stats")
def cts_tasks_training_stats(
    status: str = Query("", description="filtru status (gol = toate)"),
    department: str = Query("", description="filtru departament (gol = toate)"),
    task_type: str = Query("", description="filtru tip task (gol = toate)"),
    date_from: str = Query("", description="perioada: data de la (YYYY-MM-DD)"),
    date_to: str = Query("", description="perioada: data pana la, inclusiv (YYYY-MM-DD)"),
    assignee: str = Query("", description="filtru assignee (email exact, gol = toti)"),
    db: Session = Depends(get_db), admin=Depends(get_current_admin),
):
    """Statistici operaționale pt modulul Task-uri (nu accuracy AI-vs-CTS — nu se aplică aici).

    Accepta EXACT aceleasi filtre ca /list, ca KPI-urile din capul paginii sa se
    actualizeze cand utilizatorul schimba tipul de task / departamentul / perioada.
    """
    where_sql, params = _task_filters_with_assignee(status, department, task_type, date_from, date_to, assignee)
    base = "FROM cts_task_ground_truth gt WHERE " + where_sql
    total = db.execute(text("SELECT count(*) " + base), params).scalar() or 0
    unresolved_assignee = db.execute(text(
        "SELECT count(*) " + base + " AND gt.assignee_raw IS NOT NULL AND gt.assignee_employee_id IS NULL"
    ), params).scalar() or 0
    linked = db.execute(text(
        "SELECT count(*) " + base + " AND (gt.email_id IS NOT NULL OR gt.call_id IS NOT NULL)"
    ), params).scalar() or 0
    by_status = db.execute(text(
        "SELECT COALESCE(gt.status,'necunoscut') AS status, count(*) AS n " + base +
        " GROUP BY 1 ORDER BY n DESC"), params).fetchall()
    by_department = db.execute(text(
        "SELECT COALESCE(gt.department,'necunoscut') AS department, count(*) AS n " + base +
        " GROUP BY 1 ORDER BY n DESC"), params).fetchall()
    last_synced = db.execute(text("SELECT max(gt.last_synced_at) " + base), params).scalar()

    # Corespondenta status cu CTS: 'solved' = rezolvat efectiv, 'closed' = inchis FARA rezolvare
    # ("closed but not solved" in CTS). Le raportam separat, ca rata de rezolvare sa nu fie
    # umflata cu task-uri doar inchise. Verificat pe datele din CTS: nu exista un flag separat
    # de rezolvare in payload — statusul e singura sursa.
    cs = db.execute(text(
        "SELECT "
        " count(*) FILTER (WHERE lower(COALESCE(gt.status,'')) = 'solved') AS solved,"
        " count(*) FILTER (WHERE lower(COALESCE(gt.status,'')) = 'closed') AS closed_not_solved "
        + base), params).fetchone()
    closed_split = {
        "solved": (cs._mapping["solved"] if cs else 0) or 0,
        "closed_not_solved": (cs._mapping["closed_not_solved"] if cs else 0) or 0,
    }

    by_task_type = db.execute(text(
        "SELECT COALESCE(gt.task_type,'necunoscut') AS task_type, count(*) AS n " + base +
        " GROUP BY 1"), params).fetchall()
    type_buckets = {}
    for r in by_task_type:
        m = r._mapping
        fam = SYNC._device_family(m["task_type"])
        key = fam or m["task_type"]
        label = SYNC._FAMILY_LABELS.get(fam, m["task_type"])
        b = type_buckets.setdefault(key, {"key": key, "label": label, "count": 0})
        b["count"] += m["n"]
    by_type = sorted(type_buckets.values(), key=lambda x: -x["count"])

    return {
        "total": total,
        "unresolved_assignee": unresolved_assignee,
        "linked_to_mail_or_call": linked,
        "by_status": [{"status": m["status"], "count": m["n"]} for m in (r._mapping for r in by_status)],
        "by_department": [{"department": m["department"], "count": m["n"]} for m in (r._mapping for r in by_department)],
        "by_type": by_type,
        "solved": closed_split["solved"],
        "closed_not_solved": closed_split["closed_not_solved"],
        "last_synced_at": last_synced.isoformat() if last_synced else None,
        "source_configured": SYNC._source_configured(),
        "filters": {"status": status or None, "department": department or None,
                    "task_type": task_type or None,
                    "date_from": date_from or None, "date_to": date_to or None},
    }


@router.post("/cts-tasks-training/sync")
def cts_tasks_training_sync(limit: int = Query(2000, ge=1, le=5000),
                            admin=Depends(get_current_admin)):
    """Sync complet (o singură pagină, `limit` rânduri). Grațios (ok:False + reason) dacă
    endpoint-ul IRIS /cts/tasks nu există încă."""
    return SYNC.sync_tasks_paged(since=None, batch=limit, max_batches=1)


@router.post("/cts-tasks-training/backfill")
def cts_tasks_training_backfill(
    since: str = Query(..., description="reia ingestia de la data asta (YYYY-MM-DD)"),
    batch: int = Query(5000, ge=100, le=20000),
    max_batches: int = Query(40, ge=1, le=200),
    wait: bool = Query(True),
    admin=Depends(get_current_admin),
):
    """Re-ingestie paginată de la o dată explicită, fără plafonul de 168h al sync-ului rolling.

    Necesar după schimbarea regulilor de filtrare la ingestie: task-urile respinse anterior nu
    există în bază, iar fereastra rolling (7 zile) nu le readuce pe cele mai vechi.
    """
    since_v = _valid_date(since, "since")
    if not since_v:
        raise HTTPException(400, "since e obligatoriu (YYYY-MM-DD)")
    if wait:
        return SYNC.sync_tasks_paged(since=since_v, batch=batch, max_batches=max_batches)
    _th.Thread(target=SYNC.sync_tasks_paged,
               kwargs={"since": since_v, "batch": batch, "max_batches": max_batches},
               daemon=True).start()
    return {"ok": True, "started": True, "async": True, "since": since_v}


@router.post("/cts-tasks-training/sync-recent")
def cts_tasks_training_sync_recent(hours: int = Query(24, ge=1, le=168),
                                   wait: bool = Query(False),
                                   admin=Depends(get_current_admin)):
    """Re-sincronizează fereastra rolling (default 24h). Grațios până IRIS expune endpoint-ul."""
    if wait:
        return SYNC.sync_recent(hours=hours)
    _th.Thread(target=SYNC.sync_recent_guarded, kwargs={"hours": hours}, daemon=True).start()
    return {"ok": True, "started": True, "async": True, "window_hours": hours,
            "message": "Sync pornit în fundal. Lista se actualizează în câteva momente."}


@router.get("/cts-tasks-training/sync-config")
def cts_tasks_training_sync_config(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    return SYNC.get_sync_config(db)


@router.put("/cts-tasks-training/sync-config")
def cts_tasks_training_set_sync_config(payload: dict = Body(...),
                                       db: Session = Depends(get_db),
                                       admin=Depends(get_current_admin)):
    return SYNC.set_sync_config(db, bool(payload.get("enabled")))


@router.get("/cts-tasks-training/assignees")
def cts_tasks_training_assignees(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Lista distinctă de assignees pentru selectul de filtru, cu nume din employee_department_mapping."""
    rows = db.execute(text(
        "SELECT DISTINCT ON (gt.assignee_raw) gt.assignee_raw, COALESCE(edm.name, gt.assignee_raw) AS display_name "
        "FROM cts_task_ground_truth gt "
        "LEFT JOIN employee_department_mapping edm ON edm.email = gt.assignee_raw "
        "WHERE gt.assignee_raw IS NOT NULL "
        "ORDER BY gt.assignee_raw"
    )).fetchall()
    return {"assignees": [{"email": r[0], "name": r[1]} for r in rows]}
