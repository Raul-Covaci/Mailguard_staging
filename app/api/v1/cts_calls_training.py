"""Modul "Apeluri CTS" — comparatie ground-truth CTS vs clasificare interna Cargo360 (training).

Mirror pe app/api/v1/cts_training.py (mail), adaptat la apeluri: fara concepte de
"sent"/reply/departament care nu au sens pentru telefonie. Read-only fata de fluxul curent —
NU retrimite nimic spre CTS, NU modifica `calls`.

Sursa e populata de app/services/cts_calls_sync (inert pana Razvan expune endpointul
GET /cts/calls pe IRIS Gateway).
"""
import logging
import datetime as _dt

from fastapi import APIRouter, Depends, Query, Body, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.api.v1.auth import get_current_admin
from app.services import cts_calls_sync as SYNC

logger = logging.getLogger("mailguard.cts_calls_training")
router = APIRouter()

SYNC_ENABLED_KEY = "cts_calls_gt.sync_enabled"


def _conf(result) -> float:
    try:
        if isinstance(result, dict) and result.get("confidence") is not None:
            return round(float(result["confidence"]), 4)
    except (TypeError, ValueError):
        pass
    return None


def _call_filters(date_from: str, date_to: str, department: str, assignee: str, status: str):
    """Filtrele comune listei si statisticilor (perioada / departament / utilizator / status).

    Perioada se aplica pe DATA APELULUI, pe ora Europe/Bucharest: `calls.started_at` (While1,
    ora locala) intai, `cts_started_at` (UTC) doar ca fallback pentru apelurile fara corespondent
    While1. Fara conversie, apelurile de dupa 21:00 ar cadea in ziua urmatoare (decalaj 3h vara).

    Utilizator / departament = cine a preluat apelul in CTS. Departamentul nu exista pe rand: se
    deduce din angajatul nostru cu acelasi email (`employee_department_mapping`, email unic).
    """
    where = ["1=1"]
    params = {}

    _day_expr = ("COALESCE(c.started_at::date, "
                 "(gt.cts_started_at AT TIME ZONE 'Europe/Bucharest')::date)")
    for _raw, _op, _key in ((date_from, ">=", "date_from"), (date_to, "<=", "date_to")):
        _v = (_raw or "").strip()
        if not _v:
            continue
        try:
            _dt.date.fromisoformat(_v)
        except ValueError:
            raise HTTPException(400, "Data '%s' nu e in format YYYY-MM-DD." % _v)
        where.append("%s %s :%s" % (_day_expr, _op, _key))
        params[_key] = _v

    if (assignee or "").strip():
        where.append("lower(gt.cts_assignee_email) = lower(:assignee)")
        params["assignee"] = assignee.strip()
    if (department or "").strip():
        where.append("edm.department = :department")
        params["department"] = department.strip()
    if (status or "").strip():
        where.append("lower(gt.cts_status) = lower(:status)")
        params["status"] = status.strip()

    return where, params


@router.get("/cts-calls-training/list")
def cts_calls_training_list(
    only_mismatch: int = Query(0),
    axis: str = Query("", description="'', 'category', 'assignee'"),
    search_id: str = Query("", description="cauta dupa ID apel (gt.id sau c.id)"),
    date_from: str = Query("", description="YYYY-MM-DD, inclusiv, ora Europe/Bucharest"),
    date_to: str = Query("", description="YYYY-MM-DD, inclusiv, ora Europe/Bucharest"),
    department: str = Query("", description="slug departament al assignee-ului CTS"),
    assignee: str = Query("", description="email assignee CTS (gol = toti)"),
    status: str = Query("", description="status CTS ('new'|'in progress'|'solved')"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Lista comparativa MG vs CTS pentru apeluri, cu filtre (doar divergente / axa / interval).

    Filtrul de dată se aplică pe DATA APELULUI, nu pe data actualizării în CTS: utilizatorul
    caută "apelurile de ieri", nu "fișele modificate ieri". Se compară pe ora Europe/Bucharest,
    ca ziua din filtru să fie ziua calendaristică locală, nu ziua UTC (decalaj de 3h vara ->
    apelurile de după 21:00 ar cădea în ziua urmatoare).

    Sursa preferată pentru ora apelului e `calls.started_at` (While1, ora locală, fără fus) —
    e sursa cu ora exactă a apelului. `cts_started_at` (timestamptz, UTC) e doar fallback pentru
    apelurile care există în CTS dar nu au corespondent While1.
    """
    where, params = _call_filters(date_from, date_to, department, assignee, status)

    if search_id.strip().isdigit():
        where.append("(gt.call_local_id = :search_id OR gt.id = :search_id)")
        params["search_id"] = int(search_id.strip())

    cat_diff = "(gt.cts_category IS NOT NULL AND c.ai_category IS NOT NULL AND gt.cts_category <> c.ai_category)"
    asg_diff = ("(gt.cts_assignee_email IS NOT NULL AND c.ai_assignee IS NOT NULL "
               "AND lower(gt.cts_assignee_email) <> lower(c.ai_assignee))")
    if only_mismatch:
        if axis == "assignee":
            where.append(asg_diff)
        else:
            where.append(cat_diff)

    where_sql = " AND ".join(where)
    # edm intra in JOIN-ul de baza (nu doar cand se filtreaza) ca departamentul sa fie si in
    # randurile returnate. `employee_department_mapping.email` e unic (57/57 verificat), deci
    # LEFT JOIN-ul simplu nu dubleaza randuri — spre deosebire de cts_dv_employee.
    base = ("FROM cts_calls_ground_truth gt LEFT JOIN calls c ON c.id = gt.call_local_id "
            "LEFT JOIN employee_department_mapping edm "
            "       ON lower(edm.email) = lower(gt.cts_assignee_email)")
    total = db.execute(text("SELECT count(*) " + base + " WHERE " + where_sql), params).scalar()

    params["lim"] = page_size
    params["off"] = (page - 1) * page_size
    rows = db.execute(text(
        "SELECT gt.id, gt.call_local_id, gt.cts_call_id, gt.cts_category, gt.cts_category_prev, "
        "       gt.cts_status, gt.cts_assignee_email, gt.cts_assignee_name, gt.cts_response_seconds, "
        "       gt.cts_started_at, gt.cts_duration_seconds, gt.changed_at, "
        "       c.caller_number, c.callee_number, c.client_id, c.started_at, c.duration_seconds, "
        "       c.ai_category, c.ai_tone, c.ai_result, c.ai_assignee, cl.name AS client_name, "
        "       gt.cts_client_id, ccts.id AS cts_client_local_id, ccts.name AS cts_client_name, "
        "       edm.department AS assignee_department, edm.name AS assignee_edm_name "
        + base + " LEFT JOIN clients cl ON cl.id = c.client_id "
        " LEFT JOIN clients ccts ON ccts.iris_client_id = gt.cts_client_id "
        "WHERE " + where_sql +
        " ORDER BY COALESCE(c.started_at, gt.cts_started_at, gt.fetched_at) DESC NULLS LAST, gt.id DESC "
        "LIMIT :lim OFFSET :off"), params).fetchall()

    items = []
    for r in rows:
        m = r._mapping
        ai_cat = m["ai_category"]
        cts_cat = m["cts_category"]
        cat_match = None if (ai_cat is None or cts_cat is None) else (ai_cat == cts_cat)
        ai_asg = m["ai_assignee"]
        cts_asg = m["cts_assignee_email"]
        asg_match = None if (not ai_asg or not cts_asg) else (ai_asg.lower() == cts_asg.lower())
        items.append({
            "id": m["id"],
            "call_id": m["call_local_id"],
            "in_cargo360": m["call_local_id"] is not None,
            "cts_call_id": m["cts_call_id"],
            "caller_number": m["caller_number"],
            "callee_number": m["callee_number"],
            # Clientul: While1 intai (legatura locala), altfel clientul din fisa CTS. Sursa CTS
            # trimite client_id pentru FIECARE apel, deci un apel fara client in UI insemna doar
            # ca lipsea corespondentul While1 (call_local_id NULL) -- nu ca apelul n-are client.
            "client_id": m["client_id"] or m["cts_client_local_id"],
            "client_name": m["client_name"] or m["cts_client_name"],
            "client_source": ("while1" if m["client_id"] else ("cts" if m["cts_client_local_id"] else None)),
            "started_at": m["started_at"].isoformat() if m["started_at"] else (
                m["cts_started_at"].isoformat() if m["cts_started_at"] else None),
            "duration_seconds": m["duration_seconds"] or m["cts_duration_seconds"],
            "changed_at": m["changed_at"].isoformat() if m["changed_at"] else None,
            "cts_category_prev": m["cts_category_prev"],
            "ai_category": ai_cat,
            "ai_category_confidence": _conf(m["ai_result"]),
            "ai_tone": m["ai_tone"],
            "cts_category": cts_cat,
            "cat_match": cat_match,
            "ai_assignee": ai_asg,
            "cts_assignee": cts_asg,
            "cts_assignee_name": m["cts_assignee_name"] or m["assignee_edm_name"],
            "assignee_department": m["assignee_department"],
            "asg_match": asg_match,
            "cts_status": m["cts_status"],
            "cts_response_seconds": m["cts_response_seconds"],
        })

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/cts-calls-training/stats")
def cts_calls_training_stats(
    date_from: str = Query(""),
    date_to: str = Query(""),
    department: str = Query(""),
    assignee: str = Query(""),
    status: str = Query(""),
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Rate de potrivire MG vs CTS pentru apeluri, pe ACELEASI filtre ca lista.

    Fara filtrele astea cardurile de sus ar arata mereu tot istoricul, in timp ce tabelul de
    dedesubt ar arata subsetul — exact confuzia evitata deja pe Mail-uri CTS / Task-uri.
    """
    where, params = _call_filters(date_from, date_to, department, assignee, status)
    where_sql = " AND ".join(where)
    base = ("FROM cts_calls_ground_truth gt LEFT JOIN calls c ON c.id = gt.call_local_id "
            "LEFT JOIN employee_department_mapping edm "
            "       ON lower(edm.email) = lower(gt.cts_assignee_email)")
    total = db.execute(text("SELECT count(*) " + base + " WHERE " + where_sql), params).scalar() or 0
    only_in_cts = db.execute(text("SELECT count(*) " + base + " WHERE " + where_sql +
                                  " AND gt.call_local_id IS NULL"), params).scalar() or 0

    cat = db.execute(text(
        "SELECT count(*) FILTER (WHERE gt.cts_category IS NOT NULL AND c.ai_category IS NOT NULL) AS comparable, "
        "       count(*) FILTER (WHERE gt.cts_category IS NOT NULL AND c.ai_category IS NOT NULL "
        "                        AND gt.cts_category = c.ai_category) AS matched " + base +
        " WHERE " + where_sql), params).fetchone()
    asg = db.execute(text(
        "SELECT count(*) FILTER (WHERE gt.cts_assignee_email IS NOT NULL AND c.ai_assignee IS NOT NULL) AS comparable, "
        "       count(*) FILTER (WHERE gt.cts_assignee_email IS NOT NULL AND c.ai_assignee IS NOT NULL "
        "                        AND lower(gt.cts_assignee_email) = lower(c.ai_assignee)) AS matched, "
        "       count(*) FILTER (WHERE gt.cts_assignee_email IS NOT NULL AND c.ai_assignee IS NULL) AS missed_unassigned "
        + base + " WHERE " + where_sql), params).fetchone()
    avg_resp = db.execute(text(
        "SELECT avg(gt.cts_response_seconds) " + base + " WHERE " + where_sql +
        " AND gt.cts_response_seconds IS NOT NULL"), params).scalar()
    changed_24h = db.execute(text(
        "SELECT count(*) FROM cts_calls_ground_truth WHERE changed_at >= now() - interval '24 hours'")).scalar() or 0
    pending_unclassified = db.execute(text(
        "SELECT count(*) FROM cts_calls_ground_truth WHERE cts_category IS NULL")).scalar() or 0

    def _rate(matched, comparable):
        return round(100.0 * matched / comparable, 1) if comparable else None

    cat_cmp, cat_ok = cat._mapping["comparable"], cat._mapping["matched"]
    asg_cmp, asg_ok, asg_missed = asg._mapping["comparable"], asg._mapping["matched"], asg._mapping["missed_unassigned"]

    return {
        "total": total,
        "only_in_cts": only_in_cts,
        "category": {"comparable": cat_cmp, "matched": cat_ok, "match_rate": _rate(cat_ok, cat_cmp)},
        "assignee": {"comparable": asg_cmp, "matched": asg_ok, "match_rate": _rate(asg_ok, asg_cmp),
                     "missed_unassigned": asg_missed},
        "avg_response_seconds": (round(float(avg_resp), 1) if avg_resp is not None else None),
        "changed_24h": changed_24h,
        "pending_unclassified": pending_unclassified,
        "freshness": _freshness(db),
    }


def _freshness(db) -> dict:
    """Cât de veche e cea mai nouă informație PRIMITĂ din CTS, vs. apelurile reale din While1.

    Motiv (diagnostic 2026-07-31): sincronizarea rula corect la 5 min, dar sursa CTS se oprise
    de ~1h45m — apelurile existau în While1, fișele CTS nu se mai scriau. Din interfață asta era
    invizibil: "ultimul apel la 13:08" arăta ca o problemă de sincronizare, deși aduceam 100% din
    ce exista. Expunem explicit ambele capete, ca blocajul în amonte să fie evident.

    `lag_minutes` = distanța dintre ultimul apel While1 și cel mai nou apel primit din CTS. Peste
    ~60 min în timpul programului de lucru înseamnă că blocajul e în amonte, nu în Cargo360.

    ATENȚIE la interpretare (verificat 2026-08-04): un lag mare NU înseamnă automat "operatorul nu
    a completat încă apelul în CTS". Măsurat la ora 10:40 local, `calltrack_id` (id secvențial
    generat de CTS) era 1329233 în /cts/calls, dar 1329629 în While1 — ~400 de apeluri cărora CTS
    le alocase deja un id nu erau returnate de endpoint. Cele mai noi 6 apeluri din centrală,
    căutate individual după ctk_uniqueid ȘI calltrack_id, lipseau complet din răspuns.
    Cauza posibilă: ingestia în cts_replica.client_call_log oprită, SAU un filtru în /cts/calls.
    Nedecidabil din Cargo360 (infra IRIS) — vezi outbox #58. NU afirma o cauză neverificată în UI.
    """
    row = db.execute(text("""
        SELECT
          (SELECT max(last_synced_at) FROM cts_calls_ground_truth)          AS last_sync_at,
          (SELECT max(cts_started_at) FROM cts_calls_ground_truth)          AS last_cts_call_at,
          (SELECT max(started_at)     FROM calls)                          AS last_while1_call_at,
          (SELECT count(*) FROM calls WHERE started_at > now() - interval '2 hours') AS while1_last_2h,
          (SELECT count(*) FROM calls c WHERE c.started_at > now() - interval '2 hours'
             AND NOT EXISTS (SELECT 1 FROM cts_calls_ground_truth g WHERE g.call_local_id = c.id)
          ) AS while1_last_2h_without_cts
    """)).fetchone()
    m = row._mapping
    last_cts = m["last_cts_call_at"]
    last_w1 = m["last_while1_call_at"]
    lag_min = None
    if last_cts is not None and last_w1 is not None:
        # cts_started_at e timestamptz (UTC), calls.started_at e timestamp fara fus (ora locala).
        # Aducem ambele pe ora locala inainte de scadere, altfel diferenta include offset-ul.
        last_cts_local = db.execute(
            text("SELECT (:v AT TIME ZONE 'Europe/Bucharest')"), {"v": last_cts}).scalar()
        if last_cts_local is not None:
            lag_min = int((last_w1 - last_cts_local).total_seconds() // 60)
    return {
        "last_sync_at": m["last_sync_at"].isoformat() if m["last_sync_at"] else None,
        "last_cts_call_at": last_cts.isoformat() if last_cts else None,
        "last_while1_call_at": last_w1.isoformat() if last_w1 else None,
        "lag_minutes": lag_min,
        "while1_last_2h": m["while1_last_2h"] or 0,
        "while1_last_2h_without_cts": m["while1_last_2h_without_cts"] or 0,
    }


@router.get("/cts-calls-training/divergences")
def cts_calls_training_divergences(axis: str = Query("category", description="'category' | 'assignee'"),
                                   limit: int = Query(200, ge=1, le=2000),
                                   db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Lista CONCRETA a apelurilor unde AI != CTS (adevar de teren)."""
    ax = (axis or "category").strip().lower()
    if ax == "assignee":
        sql = ("SELECT gt.call_local_id AS call_id, c.caller_number, c.ai_assignee AS old_val, "
               "gt.cts_assignee_email AS new_val, COALESCE(gt.cts_assignee_name, 'CTS') AS by_who, "
               "to_char(gt.changed_at,'YYYY-MM-DD HH24:MI') AS changed_at "
               "FROM cts_calls_ground_truth gt JOIN calls c ON c.id=gt.call_local_id "
               "WHERE gt.cts_assignee_email IS NOT NULL AND c.ai_assignee IS NOT NULL "
               "  AND lower(gt.cts_assignee_email) <> lower(c.ai_assignee) "
               "ORDER BY gt.changed_at DESC NULLS LAST, gt.id DESC LIMIT :l")
    else:
        ax = "category"
        sql = ("SELECT gt.call_local_id AS call_id, c.caller_number, c.ai_category AS old_val, "
               "gt.cts_category AS new_val, COALESCE(gt.cts_assignee_name, 'CTS') AS by_who, "
               "to_char(gt.changed_at,'YYYY-MM-DD HH24:MI') AS changed_at "
               "FROM cts_calls_ground_truth gt JOIN calls c ON c.id=gt.call_local_id "
               "WHERE gt.cts_category IS NOT NULL AND c.ai_category IS NOT NULL "
               "  AND gt.cts_category <> c.ai_category "
               "ORDER BY gt.changed_at DESC NULLS LAST, gt.id DESC LIMIT :l")
    rows = db.execute(text(sql), {"l": limit}).fetchall()
    items = [{
        "call_id": m["call_id"], "caller_number": m["caller_number"],
        "old_val": m["old_val"], "new_val": m["new_val"], "by": m["by_who"],
        "changed_at": m["changed_at"],
    } for m in (r._mapping for r in rows)]
    return {"axis": ax, "total": len(items), "items": items}


@router.post("/cts-calls-training/sync")
def cts_calls_training_sync(limit: int = Query(500, ge=1, le=5000),
                            admin=Depends(get_current_admin)):
    """Declanseaza sync-ul COMPLET din CTS pentru apeluri. Inert (ok:False + reason) pana la grant."""
    return SYNC.sync_ground_truth(limit=limit)


@router.post("/cts-calls-training/sync-recent")
def cts_calls_training_sync_recent(hours: int = Query(24, ge=1, le=168),
                                   wait: bool = Query(False),
                                   admin=Depends(get_current_admin)):
    """Re-sincronizeaza fereastra rolling (default 24h). Ruleaza automat si din cron
    (POST /process/run-now, la 5 min). Inert pana la grant."""
    if not SYNC.is_enabled():
        return SYNC.sync_recent(hours=hours)
    if wait:
        return SYNC.sync_recent(hours=hours)
    import threading as _th
    _th.Thread(target=SYNC.sync_recent_guarded, kwargs={"hours": hours}, daemon=True).start()
    return {"ok": True, "started": True, "async": True, "window_hours": hours,
            "message": "Sync pornit in fundal. Lista se actualizeaza in cateva momente."}


@router.get("/cts-calls-training/assignees")
def cts_calls_training_assignees(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Utilizatorii care apar ca assignee CTS pe apeluri + departamentul lor — pentru filtre."""
    rows = db.execute(text("""
        SELECT lower(gt.cts_assignee_email) AS email,
               COALESCE(max(gt.cts_assignee_name), max(edm.name)) AS name,
               max(edm.department) AS department,
               count(*) AS n
        FROM cts_calls_ground_truth gt
        LEFT JOIN employee_department_mapping edm ON lower(edm.email) = lower(gt.cts_assignee_email)
        WHERE gt.cts_assignee_email IS NOT NULL AND gt.cts_assignee_email <> ''
        GROUP BY 1
        ORDER BY 2
    """)).fetchall()
    return {"assignees": [{"email": r[0], "name": r[1] or r[0], "department": r[2],
                           "count": int(r[3])} for r in rows]}


@router.get("/cts-calls-training/sync-config")
def cts_calls_training_sync_config(admin=Depends(get_current_admin)):
    return SYNC.status()


@router.put("/cts-calls-training/sync-config")
def cts_calls_training_set_sync_config(payload: dict = Body(...),
                                       db: Session = Depends(get_db),
                                       admin=Depends(get_current_admin)):
    enabled = bool(payload.get("enabled"))
    by = admin.get("username") or admin.get("email") or "admin"
    db.execute(text(
        "INSERT INTO settings(key, value, updated_by, updated_at) "
        "VALUES (:k, CAST(:v AS jsonb), :by, now()) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_by=EXCLUDED.updated_by, "
        "updated_at=now()"),
        {"k": SYNC_ENABLED_KEY, "v": ("true" if enabled else "false"), "by": by})
    db.commit()
    return SYNC.status()
