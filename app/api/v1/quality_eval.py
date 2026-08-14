"""Modul "Reclamații (Quality Evaluation)" — listă + statistici peste `cts_quality_evaluation`.

SURSA e oglinda locală a view-ului IRIS DV `quality_evaluation` (vezi
app/services/quality_eval_sync.py). Un rând = o reclamație înregistrată în CTS pe o lucrare
deja făcută (email / task / apel). Aceleași rânduri alimentează Monitorul Operațional și
productivitatea Suport 3 — aici sînt doar expuse ca listă filtrabilă.

SEMANTICĂ (confirmată în sursa CTS, src/Tss/Entities/Cts/QualityEvaluation.php):
  status: 1=new, 2=in progress, 3=solved
  is_according_to_the_procedure: 1 = s-a respectat procedura => reclamație NEFONDATĂ
                                 0 = nu s-a respectat        => FONDATĂ
  responsible_id = adminul EVALUAT (nu cine rezolvă). updated_by = cine a mutat statusul.

DEPARTAMENTUL afișat/filtrat e al persoanei EVALUATE — identic cu Monitorul. Pentru
productivitate toate reclamațiile merg la Suport 3 (echipa care le procesează), deci NU
folosi departamentul de aici ca sursă pentru scor.

CAPCANĂ (aceeași ca în productivity.py): `cts_dv_employee` are admin_id ȘI email duplicate
(contracte succesive) → orice join pe ea trece prin LATERAL ... LIMIT 1, altfel lista se
dublează. Iar `cts_dv_employee.department_id` e TEXT, în timp ce `qe.department_id` e INT →
cast obligatoriu.
"""
import datetime as _dt
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.api.v1.auth import get_current_admin
from app.api.v1.sorting import sort_dir
from app.services import quality_eval_sync as SYNC

logger = logging.getLogger("mailguard.quality_eval")
router = APIRouter()

_TZ = "Europe/Bucharest"

# Eticheta statusului CTS, în aceleași cuvinte ca pe restul modulelor CTS (new / in progress /
# solved), ca badge-ul din UI să fie același component.
_STATUS_LABEL = {1: "new", 2: "in progress", 3: "solved"}
_STATUS_VALUE = {v: k for k, v in _STATUS_LABEL.items()}

# `entity` spune pe CE lucrare s-a făcut reclamația.
_ENTITY_LABEL = {
    "client_contact_email_log": "email",
    "task": "task",
    "client_call_log": "apel",
}

# Persoana evaluată + departamentul ei + cine a procesat reclamația. Ținut o singură dată,
# ca lista și statisticile să se uite garantat la aceleași rânduri.
_JOINS = f"""
    FROM cts_quality_evaluation qe
    LEFT JOIN LATERAL (
        SELECT e.id, e.name, e.email, e.department
        FROM cts_dv_employee dv
        JOIN employee_department_mapping e ON lower(e.email) = lower(dv.email)
        WHERE dv.admin_id = qe.responsible_id::text
        ORDER BY e.enabled DESC, e.id
        LIMIT 1
    ) ev ON true
    -- fallback de departament pentru reclamațiile al căror `responsible_id` nu e mapabil pe un
    -- angajat de-al nostru: departamentul dominant al `department_id`-ului din CTS.
    LEFT JOIN LATERAL (
        SELECT e.department
        FROM cts_dv_employee dv
        JOIN employee_department_mapping e ON lower(e.email) = lower(dv.email)
        WHERE dv.department_id = qe.department_id::text AND e.enabled = true
        GROUP BY e.department
        ORDER BY count(*) DESC, e.department
        LIMIT 1
    ) dep ON true
    LEFT JOIN LATERAL (
        SELECT e.name, e.email
        FROM cts_dv_employee dv
        JOIN employee_department_mapping e ON lower(e.email) = lower(dv.email)
        WHERE dv.admin_id = qe.updated_by::text
        ORDER BY e.enabled DESC, e.id
        LIMIT 1
    ) pr ON true
    LEFT JOIN clients cl ON cl.iris_client_id = qe.client_id
"""


def _valid_date(value: str, field: str):
    """Validează o dată ISO (YYYY-MM-DD) — altfel CAST(... AS date) dă 500 în loc de 400."""
    if not value or not str(value).strip():
        return ""
    try:
        return _dt.date.fromisoformat(str(value).strip()).isoformat()
    except (ValueError, AttributeError):
        raise HTTPException(400, f"{field} invalid: se așteaptă formatul YYYY-MM-DD")


def _filters(status: str, department: str, assignee: str, entity: str,
             date_from: str, date_to: str, fondata: str):
    """WHERE comun pentru listă și statistici — o singură sursă, ca sumarul de sus să reflecte
    exact tabelul de dedesubt.

    Perioada se aplică pe data ÎNREGISTRĂRII reclamației (`created_at`), pe ora Europe/Bucharest:
    utilizatorul caută „reclamațiile de ieri", nu fișele modificate ieri.
    """
    where = ["qe.deleted_at IS NULL"]
    params = {}

    st = (status or "").strip().lower()
    if st:
        code = _STATUS_VALUE.get(st)
        if code is None:
            try:
                code = int(st)
            except ValueError:
                raise HTTPException(400, "status invalid (new|in progress|solved)")
        where.append("qe.status = :status")
        params["status"] = code

    if (department or "").strip():
        where.append("COALESCE(ev.department, dep.department) = :department")
        params["department"] = department.strip()

    if (assignee or "").strip():
        where.append("lower(ev.email) = lower(:assignee)")
        params["assignee"] = assignee.strip()

    if (entity or "").strip():
        where.append("qe.entity = :entity")
        params["entity"] = entity.strip()

    f = (fondata or "").strip()
    if f in ("1", "true", "da"):
        # 1 = s-a respectat procedura => nefondată; fondată e complementul (0).
        where.append("qe.is_according_to_the_procedure = 0")
    elif f in ("0", "false", "nu"):
        where.append("qe.is_according_to_the_procedure = 1")

    df = _valid_date(date_from, "date_from")
    dtv = _valid_date(date_to, "date_to")
    if df:
        where.append(f"DATE(qe.created_at AT TIME ZONE '{_TZ}') >= CAST(:date_from AS date)")
        params["date_from"] = df
    if dtv:
        where.append(f"DATE(qe.created_at AT TIME ZONE '{_TZ}') <= CAST(:date_to AS date)")
        params["date_to"] = dtv

    return " AND ".join(where), params


@router.get("/quality-eval/list")
def quality_eval_list(
    status: str = Query("", description="new | in progress | solved (gol = toate)"),
    department: str = Query("", description="slug departament al persoanei EVALUATE"),
    assignee: str = Query("", description="email angajat evaluat (gol = toți)"),
    entity: str = Query("", description="client_contact_email_log | task | client_call_log"),
    fondata: str = Query("", description="1 = doar fondate, 0 = doar nefondate"),
    date_from: str = Query("", description="YYYY-MM-DD, pe data înregistrării"),
    date_to: str = Query("", description="YYYY-MM-DD, inclusiv"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    dir: str = Query("desc", description="ordonare dupa data inregistrarii: 'asc' | 'desc'"),
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Listă paginată de reclamații, cu filtre pe dată / departament / utilizator / status."""
    where_sql, params = _filters(status, department, assignee, entity, date_from, date_to, fondata)

    total = db.execute(text("SELECT count(*) " + _JOINS + " WHERE " + where_sql), params).scalar()

    params["lim"] = page_size
    params["off"] = (page - 1) * page_size
    rows = db.execute(text(f"""
        SELECT qe.id, qe.entity, qe.entity_id, qe.status, qe.score,
               qe.is_according_to_the_procedure AS conform,
               qe.observations, qe.created_at, qe.in_progress_at, qe.solved_at, qe.updated_at,
               COALESCE(cl.name, '') AS client_name, qe.client_id,
               ev.name AS evaluat_name, ev.email AS evaluat_email,
               COALESCE(ev.department, dep.department) AS department,
               pr.name AS procesat_de
        {_JOINS}
        WHERE {where_sql}
        ORDER BY qe.created_at {sort_dir(dir)} NULLS LAST, qe.id {sort_dir(dir)}
        LIMIT :lim OFFSET :off
    """), params).fetchall()

    items = []
    for r in rows:
        m = r._mapping
        items.append({
            "id": m["id"],
            "entity": m["entity"],
            "entity_label": _ENTITY_LABEL.get(m["entity"], m["entity"]),
            "entity_id": m["entity_id"],
            "status": m["status"],
            "status_label": _STATUS_LABEL.get(m["status"], str(m["status"] or "")),
            "score": m["score"],
            # is_according_to_the_procedure: 1 = procedură respectată => NEFONDATĂ.
            "fondata": (None if m["conform"] is None else (m["conform"] == 0)),
            "observations": (m["observations"] or "").strip() or None,
            "created_at": m["created_at"].isoformat() if m["created_at"] else None,
            "in_progress_at": m["in_progress_at"].isoformat() if m["in_progress_at"] else None,
            "solved_at": m["solved_at"].isoformat() if m["solved_at"] else None,
            "updated_at": m["updated_at"].isoformat() if m["updated_at"] else None,
            "client_id": m["client_id"],
            "client_name": m["client_name"] or None,
            "evaluat": m["evaluat_name"],
            "evaluat_email": m["evaluat_email"],
            "department": m["department"],
            "procesat_de": m["procesat_de"],
        })

    return {"page": page, "page_size": page_size, "total": total or 0, "items": items}


@router.get("/quality-eval/stats")
def quality_eval_stats(
    status: str = Query(""),
    department: str = Query(""),
    assignee: str = Query(""),
    entity: str = Query(""),
    fondata: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Sumar peste ACELEAȘI filtre ca lista (altfel cardurile de sus ar minți)."""
    where_sql, params = _filters(status, department, assignee, entity, date_from, date_to, fondata)

    row = db.execute(text(f"""
        SELECT count(*)                                          AS total,
               count(*) FILTER (WHERE qe.status = 1)             AS noi,
               count(*) FILTER (WHERE qe.status = 2)             AS in_lucru,
               count(*) FILTER (WHERE qe.status = 3)             AS solutionate,
               count(*) FILTER (WHERE qe.is_according_to_the_procedure = 0) AS fondate,
               count(*) FILTER (WHERE qe.is_according_to_the_procedure = 1) AS nefondate,
               count(*) FILTER (WHERE qe.is_according_to_the_procedure IS NULL) AS neevaluate
        {_JOINS}
        WHERE {where_sql}
    """), params).fetchone()

    by_dept = db.execute(text(f"""
        SELECT COALESCE(ev.department, dep.department) AS department, count(*) AS n
        {_JOINS}
        WHERE {where_sql}
        GROUP BY 1
        ORDER BY 2 DESC
    """), params).fetchall()

    by_entity = db.execute(text(f"""
        SELECT qe.entity AS entity, count(*) AS n
        {_JOINS}
        WHERE {where_sql}
        GROUP BY 1
        ORDER BY 2 DESC
    """), params).fetchall()

    last_synced = db.execute(text(
        "SELECT max(synced_at) FROM cts_quality_evaluation"
    )).scalar()

    m = row._mapping if row else {}
    return {
        "total": int(m.get("total") or 0),
        "noi": int(m.get("noi") or 0),
        "in_lucru": int(m.get("in_lucru") or 0),
        "solutionate": int(m.get("solutionate") or 0),
        "fondate": int(m.get("fondate") or 0),
        "nefondate": int(m.get("nefondate") or 0),
        "neevaluate": int(m.get("neevaluate") or 0),
        "by_department": [{"department": r[0], "count": int(r[1])} for r in by_dept if r[0]],
        "by_entity": [{"entity": r[0], "label": _ENTITY_LABEL.get(r[0], r[0]),
                       "count": int(r[1])} for r in by_entity if r[0]],
        "last_synced_at": last_synced.isoformat() if last_synced else None,
        "since": SYNC.SYNC_FROM,
    }


@router.get("/quality-eval/filters")
def quality_eval_filters(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Valorile care există efectiv în date — pentru selecturile de filtru.

    Doar angajații care APAR ca evaluați: un select cu toți cei 300 de angajați ar fi inutil.
    """
    rows = db.execute(text(f"""
        SELECT DISTINCT ev.email AS email, ev.name AS name,
               COALESCE(ev.department, dep.department) AS department
        {_JOINS}
        WHERE qe.deleted_at IS NULL AND ev.email IS NOT NULL
        ORDER BY 2
    """)).fetchall()
    depts = db.execute(text(f"""
        SELECT DISTINCT COALESCE(ev.department, dep.department) AS department
        {_JOINS}
        WHERE qe.deleted_at IS NULL
    """)).fetchall()
    return {
        "assignees": [{"email": r[0], "name": r[1], "department": r[2]} for r in rows],
        "departments": sorted([r[0] for r in depts if r[0]]),
        "statuses": [{"value": v, "label": l} for v, l in
                     ((1, "new"), (2, "in progress"), (3, "solved"))],
        "entities": [{"value": k, "label": v} for k, v in _ENTITY_LABEL.items()],
    }


@router.post("/quality-eval/sync")
def quality_eval_sync_now(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Sync manual din IRIS DV. Rulează oricum din cron (vezi quality_eval_sync.run_recent_if_due);
    butonul e doar pentru „vreau acum"."""
    try:
        return SYNC.sync_quality_evaluations(db)
    except Exception as e:  # nu lăsăm 500 pe o problemă de rețea a sursei
        logger.warning("quality_eval sync manual eșuat: %s", str(e)[:200])
        return {"ok": False, "error": str(e)[:200]}


@router.get("/quality-eval/sync-config")
def quality_eval_sync_config(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Starea sursei: cheia IRIS DV setată + rezultatul ultimului sync (scris de cron)."""
    key = db.execute(text("SELECT value FROM settings WHERE key = 'iris_dv.api_key'")).fetchone()
    last = db.execute(text("SELECT value FROM settings WHERE key = :k"),
                      {"k": SYNC.LAST_RESULT_KEY}).fetchone()
    when = db.execute(text("SELECT value FROM settings WHERE key = :k"),
                      {"k": SYNC.LAST_RECENT_KEY}).fetchone()
    return {
        "source_configured": bool(key and key[0]),
        "since": SYNC.SYNC_FROM,
        "last_result": last[0] if last else None,
        "last_recent_sync_at": when[0] if when else None,
        "throttle_s": SYNC.RECENT_MIN_INTERVAL_S,
    }
