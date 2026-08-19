"""Endpoint-uri analitice apeluri: dashboard KPI, top clienți, scoruri AI, blacklist numere,
configurare prompturi de scoring.
"""
import json
import logging
import threading
import time
import uuid
from typing import Optional

import csv
import io

from fastapi import APIRouter, Depends, Query, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.api.v1.auth import get_current_admin
# Regula unica de ascundere a leg-urilor duplicate de centrala (vezi apel_no_dup_leg_sql).
from app.services import productivity as P
# BINARY_COLUMNS: cheie prompt binar -> coloana din call_ai_scores (sursa unica a setului).
from app.services import call_scorer

_BATCH_JOBS_LOCK = threading.Lock()

router = APIRouter()
logger = logging.getLogger("mailguard.calls_analytics")


# ── Helper ─────────────────────────────────────────────────────────────────────

def _bl_filter() -> str:
    """Fragment SQL pentru excludere blacklist pe caller/callee."""
    return """
        AND c.caller_number NOT IN (SELECT phone_number FROM call_phone_blacklist)
        AND c.callee_number NOT IN (SELECT phone_number FROM call_phone_blacklist)
    """


# Atribuirea unui apel unui angajat NU se poate face pe egalitate de nume: `calls.agent_extension`
# e `user_fullname` din CDR-ul While1, scris altfel decat `employee_department_mapping.name`
# ("Oana Lasca" vs "Lasca Oana-Maria", "Adriana Brasovean" vs "Buse Angelica-Adriana"). Filtrul
# vechi (`agent_extension IN (SELECT name ...)`) intorcea zero randuri pentru ORICE departament,
# de unde bug-ul „doar Operational (toate) incarca date".
#
# Se refolosesc treptele din `productivity.py` (_APEL_AGENT_CTE / _APEL_AGENT_JOIN):
#   1. maparea invatata din suprapunerea cu CTS (numele agentului -> angajatul dominant);
#   2. potrivirea de nume tolerata la ordine si la prefixe de 4 litere;
#   3. assignee-ul CTS, doar pentru apelurile fara nume de agent in CDR.
# Treptele 1-2 depind doar de numele agentului, deci se rezolva o singura data si se tin in cache
# scurt: cele 4 interogari ale dashboard-ului pleaca simultan si ar reface altfel aceeasi munca.
_AGENT_MAP_TTL_SEC = 300
_agent_map_cache: dict = {"at": 0.0, "rows": None}
_agent_map_lock = threading.Lock()

_AGENT_MAP_SQL = r"""
    WITH agent_map AS (
        SELECT agent_extension, employee_id FROM (
            SELECT c2.agent_extension, e2.id AS employee_id,
                   ROW_NUMBER() OVER (PARTITION BY c2.agent_extension
                                      ORDER BY COUNT(*) DESC, e2.id) AS rn
            FROM calls c2
            JOIN cts_calls_ground_truth g2 ON g2.call_local_id = c2.id
            JOIN employee_department_mapping e2
              ON lower(e2.email) = lower(g2.cts_assignee_email)
            WHERE c2.agent_extension IS NOT NULL AND e2.enabled = true
            GROUP BY 1, 2
        ) t WHERE rn = 1
    ),
    names AS (
        SELECT DISTINCT agent_extension AS n FROM calls
        WHERE agent_extension IS NOT NULL AND agent_extension <> ''
    )
    SELECT n.n AS agent_extension, e.id AS employee_id, e.department, lower(e.email) AS email
    FROM names n
    LEFT JOIN agent_map am ON am.agent_extension = n.n
    -- Agregatul cu CASE returneaza id-ul doar cand potrivirea de nume e unica; ambiguitatile
    -- (doi angajati cu acelasi prefix) raman nerezolvate in loc sa fie ghicite.
    LEFT JOIN LATERAL (
        SELECT CASE WHEN count(*) = 1 THEN min(e3.id) END AS id
        FROM employee_department_mapping e3
        WHERE e3.enabled = true
          AND NOT EXISTS (
              SELECT 1 FROM unnest(regexp_split_to_array(lower(trim(n.n)), '\s+')) tok
              WHERE NOT EXISTS (
                  SELECT 1 FROM unnest(
                      regexp_split_to_array(lower(regexp_replace(e3.name, '-', ' ', 'g')), '\s+')
                  ) etok
                  WHERE left(etok, 4) = left(tok, 4)
              )
          )
    ) nm ON true
    JOIN employee_department_mapping e
      ON e.id = COALESCE(am.employee_id, nm.id) AND e.enabled = true
"""


def _agent_name_map(db) -> list:
    """Numele de agent din While1, fiecare cu angajatul si departamentul lui (cache 5 min)."""
    now = time.time()
    with _agent_map_lock:
        if _agent_map_cache["rows"] is not None and now - _agent_map_cache["at"] < _AGENT_MAP_TTL_SEC:
            return _agent_map_cache["rows"]
    rows = [dict(r._mapping) for r in db.execute(text(_AGENT_MAP_SQL)).fetchall()]
    with _agent_map_lock:
        _agent_map_cache["rows"] = rows
        _agent_map_cache["at"] = now
    return rows


def dept_agent_names(db, department: Optional[str] = None, agent: Optional[str] = None) -> list:
    """Numele de agent (`calls.agent_extension`) ale unui departament sau ale unui operator."""
    rows = _agent_name_map(db)
    if agent:
        want = agent.strip().lower()
        return sorted({r["agent_extension"] for r in rows if (r["email"] or "") == want})
    if department:
        want = department.strip().lower()
        return sorted({r["agent_extension"] for r in rows if (r["department"] or "") == want})
    return []


def _agent_dept_filter(agent: Optional[str], department: Optional[str], params: dict, db=None) -> str:
    """Fragment SQL pentru filtrare agent/departament. `agent` (email) are prioritate."""
    if not agent and not department:
        return ""
    if db is None:      # apelant vechi, fara sesiune -> nu se filtreaza (mai bine tot decat nimic)
        return ""

    if agent:
        params["_ad_email"] = agent.strip().lower()
        cts_pred = "lower(e3.email) = :_ad_email"
    else:
        params["_ad_dept"] = (department or "").strip().lower()
        cts_pred = "e3.department = :_ad_dept"

    names = dept_agent_names(db, department=department, agent=agent)
    # Treapta 3: apelurile pe care centrala nu le-a legat de un agent (`agent_extension` NULL)
    # se atribuie dupa assignee-ul tichetului CTS — la fel ca in raportul de productivitate.
    cts_branch = (
        "(c.agent_extension IS NULL AND EXISTS ("
        "  SELECT 1 FROM cts_calls_ground_truth g3"
        "  JOIN employee_department_mapping e3 ON lower(e3.email) = lower(g3.cts_assignee_email)"
        f" WHERE g3.call_local_id = c.id AND e3.enabled = true AND {cts_pred}"
        "))"
    )
    if not names:
        return " AND " + cts_branch

    keys = []
    for i, nm in enumerate(names):
        k = f"_ad_n{i}"
        params[k] = nm
        keys.append(":" + k)
    return f" AND (c.agent_extension IN ({', '.join(keys)}) OR {cts_branch})"


# ── Dashboard KPI ──────────────────────────────────────────────────────────────

@router.get("/calls/analytics/dashboard")
def analytics_dashboard(
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    department: Optional[str] = Query(None),
    agent: Optional[str] = Query(None),
    exclude_blacklist: bool = Query(True),
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """KPI cards + grafice dashboard analitice apeluri."""
    params: dict = {"days": days}
    date_filter = "c.started_at >= NOW() - (INTERVAL '1 day' * :days)"
    if date_from:
        date_filter = "c.started_at >= CAST(:date_from AS date)"
        params["date_from"] = date_from
    if date_to:
        date_filter += " AND c.started_at < (CAST(:date_to AS date) + INTERVAL '1 day')"
        params["date_to"] = date_to

    agent_filter = _agent_dept_filter(agent, department, params, db)
    bl_sql = _bl_filter() if exclude_blacklist else ""
    # Leg-urile duplicate de centrala (0s, cu sibling raspuns in +/-15 min) nu se numara:
    # acelasi apel fizic aparea de doua ori si umfla toate KPI-urile.
    dup_sql = "AND " + P.apel_no_dup_leg_sql("c")

    kpi_sql = f"""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE c.direction='inbound') AS inbound,
            COUNT(*) FILTER (WHERE c.direction='outbound') AS outbound,
            COUNT(*) FILTER (WHERE c.call_status='ANSWERED') AS answered,
            ROUND(AVG(c.duration_seconds)) AS avg_duration_seconds,
            COALESCE(SUM(c.duration_seconds), 0) AS total_duration_seconds,
            COUNT(*) FILTER (WHERE c.ai_category='informatie') AS ai_informatie,
            COUNT(*) FILTER (WHERE c.ai_category='sesizare') AS ai_sesizare,
            COUNT(*) FILTER (WHERE c.ai_category='reclamatie') AS ai_reclamatie,
            COUNT(*) FILTER (WHERE c.ai_tone='tensionat') AS tone_tensionat,
            COUNT(*) FILTER (WHERE c.ai_tone='prietenos') AS tone_prietenos,
            ROUND(AVG(cas.agent_score_total)::numeric, 2) AS avg_agent_score,
            ROUND(AVG(cas.customer_score_total)::numeric, 2) AS avg_customer_score,
            COUNT(*) FILTER (WHERE cas.is_valid_call = false) AS invalid_calls,
            COUNT(*) FILTER (WHERE cas.issue_resolved = true) AS resolved_calls,
            COUNT(*) FILTER (WHERE cas.agent_next_steps_clear = true) AS next_steps_clear_calls,
            COUNT(*) FILTER (WHERE cas.agent_next_steps_clear IS NOT NULL) AS next_steps_scored_calls,
            COUNT(*) FILTER (WHERE cas.issue_within_company_scope = false) AS out_of_scope_calls,
            COALESCE(SUM(cas.customer_unacknowledged_count), 0) AS unacknowledged_requests,
            COUNT(cas.id) AS scored_calls
        FROM calls c
        LEFT JOIN call_ai_scores cas ON cas.call_id = c.id
        WHERE {date_filter} {agent_filter} {bl_sql} {dup_sql}
    """
    kpi = db.execute(text(kpi_sql), params).fetchone()

    # Serie zilnică — intervalul generate_series derivat din params disponibili
    if date_from:
        params["_s_start"] = date_from
        params["_s_end"] = date_to if date_to else date_from
        series_range = "CAST(:_s_start AS date), CAST(:_s_end AS date)"
    else:
        params["_s_days"] = days
        series_range = "CURRENT_DATE - (:_s_days - 1), CURRENT_DATE"

    series_sql = f"""
        WITH days_series AS (
            SELECT generate_series(
                {series_range},
                INTERVAL '1 day'
            )::date AS d
        ),
        agg AS (
            SELECT date(c.started_at) AS d,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE c.direction='inbound') AS inbound,
                COUNT(*) FILTER (WHERE c.call_status='ANSWERED') AS answered,
                ROUND(AVG(cas.agent_score_total)::numeric, 2) AS avg_score
            FROM calls c
            LEFT JOIN call_ai_scores cas ON cas.call_id = c.id
            WHERE {date_filter} {agent_filter} {bl_sql} {dup_sql}
            GROUP BY date(c.started_at)
        )
        SELECT to_char(ds.d, 'YYYY-MM-DD') AS day,
               COALESCE(agg.total, 0) AS total,
               COALESCE(agg.inbound, 0) AS inbound,
               COALESCE(agg.answered, 0) AS answered,
               agg.avg_score
        FROM days_series ds
        LEFT JOIN agg ON agg.d = ds.d
        ORDER BY ds.d
    """
    series_rows = db.execute(text(series_sql), params).fetchall()
    series = [dict(r._mapping) for r in series_rows]

    kpi_dict = dict(kpi._mapping) if kpi else {}
    total = kpi_dict.get("total") or 0
    answered = kpi_dict.get("answered") or 0
    resolved = kpi_dict.get("resolved_calls") or 0
    scored = kpi_dict.get("scored_calls") or 0
    kpi_dict["answered_pct"] = round(answered * 100.0 / total, 1) if total else None
    kpi_dict["resolved_pct"] = round(resolved * 100.0 / scored, 1) if scored else None
    ns_scored = kpi_dict.get("next_steps_scored_calls") or 0
    ns_clear = kpi_dict.get("next_steps_clear_calls") or 0
    kpi_dict["next_steps_clear_pct"] = round(ns_clear * 100.0 / ns_scored, 1) if ns_scored else None

    return {"kpi": kpi_dict, "series": series, "days": days}


# ── Top clienți ────────────────────────────────────────────────────────────────

@router.get("/calls/analytics/top-clients")
def analytics_top_clients(
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(10, ge=1, le=50),
    exclude_blacklist: bool = Query(True),
    department: Optional[str] = Query(None),
    agent: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    bl_sql = _bl_filter() if exclude_blacklist else ""
    # Leg-urile duplicate de centrala (0s, cu sibling raspuns in +/-15 min) nu se numara:
    # acelasi apel fizic aparea de doua ori si umfla toate KPI-urile.
    dup_sql = "AND " + P.apel_no_dup_leg_sql("c")
    params: dict = {"lim": limit}
    if date_from:
        date_filter = "c.started_at >= CAST(:date_from AS date)"
        params["date_from"] = date_from
        if date_to:
            date_filter += " AND c.started_at < (CAST(:date_to AS date) + INTERVAL '1 day')"
            params["date_to"] = date_to
    else:
        date_filter = "c.started_at >= NOW() - (INTERVAL '1 day' * :days)"
        params["days"] = days
    sql = f"""
        SELECT cl.id AS client_id,
               cl.name AS client_name,
               COUNT(c.id) AS call_count,
               COUNT(c.id) FILTER (WHERE c.direction='inbound') AS inbound,
               COUNT(c.id) FILTER (WHERE c.direction='outbound') AS outbound,
               ROUND(AVG(c.duration_seconds)) AS avg_duration_seconds
        FROM calls c
        JOIN clients cl ON cl.id = c.client_id
        WHERE {date_filter}
          AND c.client_id IS NOT NULL
          {bl_sql} {dup_sql}
        GROUP BY cl.id, cl.name
        ORDER BY call_count DESC
        LIMIT :lim
    """
    rows = db.execute(text(sql), params).fetchall()
    return {"clients": [dict(r._mapping) for r in rows]}


# ── Scoruri agenți ─────────────────────────────────────────────────────────────

@router.get("/calls/analytics/scores")
def analytics_scores(
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    days: int = Query(30, ge=1, le=365),
    agent: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    exclude_blacklist: bool = Query(True),
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Scoruri agregate per agent."""
    params: dict = {}
    if date_from:
        date_filter = "c.started_at >= CAST(:date_from AS date)"
        params["date_from"] = date_from
        if date_to:
            date_filter += " AND c.started_at < (CAST(:date_to AS date) + INTERVAL '1 day')"
            params["date_to"] = date_to
    else:
        date_filter = "c.started_at >= NOW() - (INTERVAL '1 day' * :days)"
        params["days"] = days
    agent_filter = _agent_dept_filter(agent, department, params, db)
    bl_sql = _bl_filter() if exclude_blacklist else ""
    # Leg-urile duplicate de centrala (0s, cu sibling raspuns in +/-15 min) nu se numara:
    # acelasi apel fizic aparea de doua ori si umfla toate KPI-urile.
    dup_sql = "AND " + P.apel_no_dup_leg_sql("c")

    sql = f"""
        SELECT
            c.agent_extension AS agent,
            COUNT(c.id) AS call_count,
            ROUND(AVG(cas.agent_score_total)::numeric, 2) AS agent_score_avg,
            ROUND(AVG(cas.agent_explaining_solution)::numeric, 2) AS avg_explaining,
            ROUND(AVG(cas.agent_patient)::numeric, 2) AS avg_patient,
            ROUND(AVG(cas.agent_understanding)::numeric, 2) AS avg_understanding,
            ROUND(AVG(cas.agent_politeness)::numeric, 2) AS avg_politeness,
            ROUND(AVG(cas.agent_empathy)::numeric, 2) AS avg_empathy,
            ROUND(AVG(cas.agent_transparency)::numeric, 2) AS avg_transparency,
            COUNT(cas.id) AS scored_count,
            COUNT(*) FILTER (WHERE cas.issue_resolved = true) AS resolved_count,
            COUNT(*) FILTER (WHERE cas.agent_next_steps_clear = true) AS next_steps_clear_count,
            COUNT(*) FILTER (WHERE cas.agent_next_steps_clear IS NOT NULL) AS next_steps_scored_count,
            COALESCE(SUM(cas.customer_unacknowledged_count), 0) AS unacknowledged_requests
        FROM calls c
        INNER JOIN call_ai_scores cas ON cas.call_id = c.id
        WHERE {date_filter}
          AND c.agent_extension IS NOT NULL
          {agent_filter} {bl_sql} {dup_sql}
        GROUP BY c.agent_extension
        ORDER BY agent_score_avg DESC NULLS LAST
    """
    rows = db.execute(text(sql), params).fetchall()
    return {"days": days, "agents": [dict(r._mapping) for r in rows]}


# ── Apelurile unui agent, cu scorul fiecaruia (drill-down din tabelul de scoruri) ─────────

@router.get("/calls/analytics/agent-calls")
def analytics_agent_calls(
    agent_name: str = Query(..., min_length=1, description="calls.agent_extension din tabelul de scoruri"),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    days: int = Query(30, ge=1, le=365),
    exclude_blacklist: bool = Query(True),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Apelurile scorate ale unui agent, cu scorul fiecarui apel.

    Tabelul „Scoruri Agenti" arata doar medii; de aici se vede din CE apeluri vine media si
    care apel a tras scorul in jos. `id`-ul returnat deschide modalul de apel (tab Analiza AI).
    """
    params: dict = {"agent": agent_name, "lim": limit}
    if date_from:
        date_filter = "c.started_at >= CAST(:date_from AS date)"
        params["date_from"] = date_from
        if date_to:
            date_filter += " AND c.started_at < (CAST(:date_to AS date) + INTERVAL '1 day')"
            params["date_to"] = date_to
    else:
        date_filter = "c.started_at >= NOW() - (INTERVAL '1 day' * :days)"
        params["days"] = days
    bl_sql = _bl_filter() if exclude_blacklist else ""
    # Aceeasi regula de deduplicare a leg-urilor ca in agregat, altfel lista nu s-ar
    # potrivi cu numarul de apeluri din randul agentului.
    dup_sql = "AND " + P.apel_no_dup_leg_sql("c")

    # Coloanele binare intra in raspuns sub cheia promptului, ca UI-ul sa nu le hardcodeze.
    bin_cols = ", ".join(f"cas.{col} AS bin_{key}" for key, col in call_scorer.BINARY_COLUMNS.items())

    sql = f"""
        SELECT
            c.id, c.started_at, c.direction, c.call_status,
            c.caller_number, c.callee_number, c.duration_seconds,
            c.ai_category, c.ai_tone,
            cl.name AS client_name,
            cas.agent_score_total, cas.customer_score_total,
            cas.agent_explaining_solution, cas.agent_patient, cas.agent_understanding,
            cas.agent_politeness, cas.agent_empathy, cas.agent_transparency,
            cas.issue_resolved, cas.issue_within_company_scope,
            cas.agent_next_steps_clear, cas.customer_unacknowledged_count,
            cas.issue_main_problem, cas.scored_at,
            {bin_cols}
        FROM calls c
        INNER JOIN call_ai_scores cas ON cas.call_id = c.id
        LEFT JOIN clients cl ON cl.id = c.client_id
        WHERE {date_filter}
          AND c.agent_extension = :agent
          {bl_sql} {dup_sql}
        ORDER BY cas.agent_score_total ASC NULLS LAST, c.started_at DESC
        LIMIT :lim
    """
    rows = db.execute(text(sql), params).fetchall()

    calls = []
    for r in rows:
        d = dict(r._mapping)
        d["binary"] = {k[4:]: d.pop(k) for k in list(d) if k.startswith("bin_")}
        calls.append(d)
    return {
        "agent": agent_name,
        "count": len(calls),
        "binary_labels": _binary_labels(db),
        "calls": calls,
    }


def _binary_labels(db) -> dict:
    """Etichetele intrebarilor binare active, in ordinea din BINARY_COLUMNS."""
    rows = db.execute(text(
        "SELECT key, label FROM call_scoring_prompts WHERE output_type='binary' AND enabled=true"
    )).fetchall()
    by_key = {r[0]: r[1] for r in rows}
    return {k: by_key[k] for k in call_scorer.BINARY_COLUMNS if k in by_key}


@router.get("/calls/analytics/scores/{call_id}")
def analytics_scores_call(
    call_id: int,
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Scoruri detaliate pentru un apel specific."""
    row = db.execute(text(
        "SELECT * FROM call_ai_scores WHERE call_id = :id"
    ), {"id": call_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Scoruri inexistente pentru apelul dat")
    return dict(row._mapping)


@router.post("/calls/analytics/score-now/{call_id}")
def analytics_score_now(
    call_id: int,
    force: bool = Query(False, description="Re-scorează chiar dacă există deja"),
    admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Trigger scoring manual pentru un apel."""
    if force:
        db.execute(text("DELETE FROM call_ai_scores WHERE call_id = :id"), {"id": call_id})
        db.commit()
    from app.services import call_scorer
    result = call_scorer.score_call(call_id)
    return result


def _job_key(job_id: str) -> str:
    return f"score_job.{job_id}"


def _write_job(job_id: str, data: dict):
    """Persistă starea job-ului în tabela settings (cross-worker)."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        v = json.dumps(data)
        db.execute(text(
            "INSERT INTO settings (key, value) VALUES (:k, CAST(:v AS jsonb)) "
            "ON CONFLICT (key) DO UPDATE SET value=CAST(:v AS jsonb)"
        ), {"k": _job_key(job_id), "v": v})
        db.commit()
    except Exception:
        logger.exception("_write_job failed")
    finally:
        db.close()


def _read_job(job_id: str) -> dict:
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT value FROM settings WHERE key=:k LIMIT 1"),
            {"k": _job_key(job_id)}
        ).scalar()
        if row is None:
            return {}
        return row if isinstance(row, dict) else json.loads(row)
    except Exception:
        return {}
    finally:
        db.close()


@router.post("/calls/analytics/score-batch")
def analytics_score_batch(
    body: dict = None,
    limit: int = Query(500, ge=1, le=2000),
    admin=Depends(get_current_admin),
):
    """Batch scoring async — pornește în background, returnează job_id imediat."""
    from app.services import call_scorer
    days_back = int((body or {}).get("days_back", 1))
    rescore_null = bool((body or {}).get("rescore_null", False))
    job_id = str(uuid.uuid4())[:8]

    _write_job(job_id, {"status": "running", "scored": 0, "failed": 0, "total": 0})

    def _progress(scored, failed, total):
        _write_job(job_id, {"status": "running", "scored": scored, "failed": failed, "total": total})

    def _run():
        try:
            result = call_scorer.score_batch(limit=limit, days_back=days_back, progress_cb=_progress, rescore_null=rescore_null)
            _write_job(job_id, {
                "status": "done",
                "scored": result.get("scored", 0),
                "failed": result.get("failed", 0),
                "total": result.get("total", 0),
            })
        except Exception:
            logger.exception("score_batch background failed")
            _write_job(job_id, {"status": "error", "scored": 0, "failed": 0, "total": 0})

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "job_id": job_id, "status": "running"}


@router.get("/calls/analytics/score-batch/status")
def analytics_score_batch_status(
    job_id: str = Query(...),
    admin=Depends(get_current_admin),
):
    """Status job batch scoring (din DB — cross-worker safe)."""
    data = _read_job(job_id)
    if not data:
        return {"status": "unknown", "job_id": job_id}
    return {"job_id": job_id, **data}


@router.post("/calls/analytics/rescore-missing-binary")
def analytics_rescore_missing_binary(
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Rescorează apelurile cu call_ai_scores incomplete (KPI binare vechi sau câmpuri V2 lipsă)."""
    from app.services import call_scorer
    rows = db.execute(text(
        "SELECT call_id FROM call_ai_scores "
        "WHERE ("
        "  agentul_sa_prezentat IS NULL AND clientul_aminta_judecata IS NULL "
        "  AND clientul_aminta_renuntare IS NULL AND clientul_contactat_anterior IS NULL"
        ") OR masini_care_nu_transmit IS NULL OR ("
        # scripturile V2: transparency (agentScore V3), scope/soluție (issueResolution V2),
        # pași următori (agentActions V2)
        "  agent_transparency IS NULL OR issue_within_company_scope IS NULL "
        "  OR agent_next_steps_clear IS NULL"
        ") "
        "LIMIT 50"
    )).fetchall()
    call_ids = [r[0] for r in rows]
    results = []
    for cid in call_ids:
        r = call_scorer.score_call(cid, force=True)
        results.append({"call_id": cid, "ok": r.get("ok"), "reason": r.get("reason", "-")})
    return {"rescored": len(call_ids), "results": results}


# ── Auto-score toggle ─────────────────────────────────────────────────────────

@router.get("/calls/analytics/auto-score")
def auto_score_get(
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    row = db.execute(
        text("SELECT value FROM settings WHERE key='calls.auto_score' LIMIT 1")
    ).scalar()
    enabled = bool(row) if row is not None else False
    return {"enabled": enabled}


@router.post("/calls/analytics/auto-score/toggle")
def auto_score_toggle(
    body: dict,
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    enabled = bool(body.get("enabled", False))
    db.execute(
        text(
            "INSERT INTO settings (key, value) VALUES ('calls.auto_score', :v) "
            "ON CONFLICT (key) DO UPDATE SET value=:v"
        ),
        {"v": "true" if enabled else "false"},
    )
    db.commit()
    state = "PORNIT" if enabled else "OPRIT"
    return {"ok": True, "enabled": enabled, "message": f"Scorare automată apeluri: {state}"}


# ── Blacklist numere ───────────────────────────────────────────────────────────

@router.get("/calls/analytics/phone-blacklist")
def blacklist_list(
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    rows = db.execute(text(
        "SELECT id, phone_number, label, created_at, created_by "
        "FROM call_phone_blacklist ORDER BY created_at DESC"
    )).fetchall()
    return {"items": [dict(r._mapping) for r in rows]}


@router.post("/calls/analytics/phone-blacklist")
def blacklist_add(
    body: dict,
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    phone = (body.get("phone_number") or "").strip()
    label = (body.get("label") or "").strip() or None
    if not phone:
        raise HTTPException(status_code=400, detail="phone_number obligatoriu")
    try:
        db.execute(text(
            "INSERT INTO call_phone_blacklist (phone_number, label, created_by) "
            "VALUES (:phone, :label, :by) ON CONFLICT (phone_number) DO UPDATE SET label=:label"
        ), {"phone": phone, "label": label, "by": getattr(admin, "email", None)})
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "phone_number": phone}


@router.post("/calls/analytics/phone-blacklist/import-csv")
async def blacklist_import_csv(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """CSV cu coloane: numar_telefon[,eticheta]. Header opțional. Max 5000 rânduri."""
    content = await file.read()
    try:
        text_content = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text_content = content.decode("latin-1", errors="replace")

    reader = csv.reader(io.StringIO(text_content))
    inserted = updated = skipped = 0
    errors = []
    by = getattr(admin, "email", None)

    for i, row in enumerate(reader):
        if i >= 5000:
            errors.append("Limită 5000 rânduri — restul ignorate")
            break
        if not row:
            continue
        phone = row[0].strip()
        if not phone or phone.lower() in ("numar_telefon", "numar", "telefon", "phone", "phone_number"):
            continue  # header sau gol
        label = row[1].strip() if len(row) > 1 else None
        label = label or None
        if len(phone) < 4 or len(phone) > 30:
            errors.append(f"Rând {i+1}: număr invalid ignorat ({phone!r})")
            skipped += 1
            continue
        try:
            result = db.execute(text(
                "INSERT INTO call_phone_blacklist (phone_number, label, created_by) "
                "VALUES (:phone, :label, :by) "
                "ON CONFLICT (phone_number) DO UPDATE SET label=EXCLUDED.label "
                "RETURNING (xmax = 0) AS was_inserted"
            ), {"phone": phone, "label": label, "by": by})
            row_result = result.fetchone()
            if row_result and row_result[0]:
                inserted += 1
            else:
                updated += 1
        except Exception as e:
            db.rollback()
            errors.append(f"Rând {i+1}: {str(e)[:80]}")
            skipped += 1
            continue

    db.commit()
    return {"ok": True, "inserted": inserted, "updated": updated, "skipped": skipped, "errors": errors[:20]}


@router.delete("/calls/analytics/phone-blacklist/{phone_number:path}")
def blacklist_delete(
    phone_number: str,
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    db.execute(text(
        "DELETE FROM call_phone_blacklist WHERE phone_number = :phone"
    ), {"phone": phone_number})
    db.commit()
    return {"ok": True, "phone_number": phone_number}


# ── Statistici binare (donut cards) ───────────────────────────────────────────

@router.get("/calls/analytics/binary-stats")
def analytics_binary_stats(
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    days: int = Query(30, ge=1, le=365),
    exclude_blacklist: bool = Query(True),
    agent: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Statistici pozitiv/negativ per prompt binar, pentru carduri donut."""
    params: dict = {}
    if date_from:
        date_filter = "c.started_at >= CAST(:date_from AS date)"
        params["date_from"] = date_from
        if date_to:
            date_filter += " AND c.started_at < (CAST(:date_to AS date) + INTERVAL '1 day')"
            params["date_to"] = date_to
    else:
        date_filter = "c.started_at >= NOW() - (INTERVAL '1 day' * :days)"
        params["days"] = days
    agent_filter = _agent_dept_filter(agent, department, params, db)
    bl_sql = _bl_filter() if exclude_blacklist else ""
    # Leg-urile duplicate de centrala (0s, cu sibling raspuns in +/-15 min) nu se numara:
    # acelasi apel fizic aparea de doua ori si umfla toate KPI-urile.
    dup_sql = "AND " + P.apel_no_dup_leg_sql("c")

    # Prompturile binare active din DB. Setul afisat = EXACT intrebarile binare cu prompt
    # propriu (5, vezi migratia 20260819f). Indicatorii derivati din prompturile V2 nu mai
    # apar aici: nu sunt intrebari selectate de business, iar `issueResolution` a iesit dintre
    # binare (are patru campuri, si tot el alimenteaza KPI-ul „% Rezolvate").
    prompt_rows = db.execute(text(
        "SELECT key, label FROM call_scoring_prompts WHERE output_type='binary' AND enabled=true"
    )).fetchall()
    by_key = {r[0]: r[1] for r in prompt_rows}

    # Ordinea cardurilor o da BINARY_COLUMNS (ordinea ceruta de business), nu alfabeticul.
    ordered = [k for k in call_scorer.BINARY_COLUMNS if k in by_key]
    ordered += [k for k in sorted(by_key) if k not in call_scorer.BINARY_COLUMNS]

    results = []
    for key in ordered:
        col = call_scorer.BINARY_COLUMNS.get(key)
        # O intrebare binara fara coloana dedicata se citeste din `binary_evidence`, deci
        # intra in statistici fara migratie noua.
        expr = (
            f"cas.{col}" if col
            else "(cas.binary_evidence -> :bkey_%s ->> 'result')::boolean" % key
        )
        if not col:
            params[f"bkey_{key}"] = key
        sql = f"""
            SELECT
                COUNT(*) FILTER (WHERE {expr} = true)  AS positive,
                COUNT(*) FILTER (WHERE {expr} = false) AS negative,
                COUNT(*) FILTER (WHERE {expr} IS NOT NULL) AS total
            FROM calls c
            INNER JOIN call_ai_scores cas ON cas.call_id = c.id
            WHERE {date_filter} {agent_filter} {bl_sql} {dup_sql}
        """
        row = db.execute(text(sql), params).fetchone()
        results.append({
            "key": key, "label": by_key[key],
            "positive": (row[0] if row else 0) or 0,
            "negative": (row[1] if row else 0) or 0,
            "total": (row[2] if row else 0) or 0,
        })

    return {"stats": results}


# ── Statistici scoruri agregate (pentru KPI bars) ─────────────────────────────

@router.get("/calls/analytics/score-stats")
def analytics_score_stats(
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    days: int = Query(30, ge=1, le=365),
    exclude_blacklist: bool = Query(True),
    agent: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """KPI medie per dimensiune agent + client, pentru bare colorate."""
    params: dict = {}
    if date_from:
        date_filter = "c.started_at >= CAST(:date_from AS date)"
        params["date_from"] = date_from
        if date_to:
            date_filter += " AND c.started_at < (CAST(:date_to AS date) + INTERVAL '1 day')"
            params["date_to"] = date_to
    else:
        date_filter = "c.started_at >= NOW() - (INTERVAL '1 day' * :days)"
        params["days"] = days
    agent_filter = _agent_dept_filter(agent, department, params, db)
    bl_sql = _bl_filter() if exclude_blacklist else ""
    # Leg-urile duplicate de centrala (0s, cu sibling raspuns in +/-15 min) nu se numara:
    # acelasi apel fizic aparea de doua ori si umfla toate KPI-urile.
    dup_sql = "AND " + P.apel_no_dup_leg_sql("c")

    sql = f"""
        SELECT
            COUNT(cas.id) AS scored_calls,
            ROUND(AVG(cas.agent_score_total)::numeric, 2)        AS agent_total,
            ROUND(AVG(cas.agent_explaining_solution)::numeric, 2) AS agent_explaining,
            ROUND(AVG(cas.agent_patient)::numeric, 2)            AS agent_patient,
            ROUND(AVG(cas.agent_understanding)::numeric, 2)      AS agent_understanding,
            ROUND(AVG(cas.agent_politeness)::numeric, 2)         AS agent_politeness,
            ROUND(AVG(cas.agent_empathy)::numeric, 2)            AS agent_empathy,
            ROUND(AVG(cas.agent_transparency)::numeric, 2)        AS agent_transparency,
            ROUND(AVG(cas.customer_score_total)::numeric, 2)     AS customer_total,
            ROUND(AVG(cas.customer_explaining)::numeric, 2)      AS customer_explaining,
            ROUND(AVG(cas.customer_patient)::numeric, 2)         AS customer_patient,
            ROUND(AVG(cas.customer_understanding)::numeric, 2)   AS customer_understanding,
            ROUND(AVG(cas.customer_politeness)::numeric, 2)      AS customer_politeness,
            ROUND(AVG(cas.customer_empathy)::numeric, 2)         AS customer_empathy
        FROM calls c
        INNER JOIN call_ai_scores cas ON cas.call_id = c.id
        WHERE {date_filter} {agent_filter} {bl_sql} {dup_sql}
    """
    row = db.execute(text(sql), params).fetchone()
    if not row:
        return {"scored_calls": 0, "agent": {}, "customer": {}}
    d = dict(row._mapping)
    return {
        "scored_calls": d.get("scored_calls") or 0,
        "agent": {
            "total":         d.get("agent_total"),
            "explaining":    d.get("agent_explaining"),
            "patient":       d.get("agent_patient"),
            "understanding": d.get("agent_understanding"),
            "politeness":    d.get("agent_politeness"),
            "empathy":       d.get("agent_empathy"),
            "transparency":  d.get("agent_transparency"),
        },
        "customer": {
            "total":         d.get("customer_total"),
            "explaining":    d.get("customer_explaining"),
            "patient":       d.get("customer_patient"),
            "understanding": d.get("customer_understanding"),
            "politeness":    d.get("customer_politeness"),
            "empathy":       d.get("customer_empathy"),
        },
    }


# ── Lista agenți (pentru selectorul de filtru) ────────────────────────────────

@router.get("/calls/analytics/agents")
def analytics_agents(
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Distinct agent_extension din apeluri ultimele 90 zile, pentru selector UI."""
    rows = db.execute(text(
        "SELECT DISTINCT agent_extension AS agent FROM calls "
        "WHERE agent_extension IS NOT NULL AND agent_extension != '' "
        "AND started_at >= NOW() - INTERVAL '90 days' ORDER BY agent_extension"
    )).fetchall()
    return {"agents": [r[0] for r in rows]}


@router.get("/calls/analytics/departments")
def analytics_departments(
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Departamentele care au cel putin un agent cu apeluri in centrala, pentru selector UI.

    Se filtreaza prin aceeasi mapare nume-agent -> angajat folosita de filtre: un departament
    fara niciun operator in CDR-urile While1 nu are ce afisa, iar prezenta lui in selector
    arata ca un bug („am ales departamentul si nu se incarca nimic").
    """
    with_calls = {r["department"] for r in _agent_name_map(db) if r.get("department")}
    rows = db.execute(text(
        "SELECT DISTINCT department FROM employee_department_mapping "
        "WHERE enabled=true AND department IS NOT NULL ORDER BY department"
    )).fetchall()
    all_depts = [r[0] for r in rows]
    # Daca maparea nu a produs nimic (baza goala / sync nerulat), se cade pe lista completa
    # in loc sa se goleasca selectorul.
    return {"departments": [d for d in all_depts if d in with_calls] or all_depts}


@router.get("/calls/analytics/department-users")
def analytics_department_users(
    department: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Lista operatorilor activi dintr-un departament, pentru selectorul de utilizator."""
    dep = (department or "").strip().lower()
    if not dep or dep in ("operational", "all", "toate"):
        return []
    rows = db.execute(text(
        "SELECT id, name, email FROM employee_department_mapping "
        "WHERE department=:d AND enabled=true ORDER BY name"
    ), {"d": dep}).fetchall()
    return [{"id": r[0], "name": r[1], "email": r[2]} for r in rows]


# ── Prompturi de scoring configurabile ────────────────────────────────────────

@router.get("/calls/analytics/scoring-prompts")
def scoring_prompts_list(
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    # Prompturile noi versionate în repo apar în listă fără pas manual (insert-only:
    # textele deja existente în DB — inclusiv cele editate din UI — NU se suprascriu).
    from app.services import call_scorer
    call_scorer.sync_prompts_from_repo(db, insert_only=True)
    rows = db.execute(text(
        "SELECT id, key, label, enabled, output_type, output_schema, updated_at "
        "FROM call_scoring_prompts ORDER BY key"
    )).fetchall()
    return {"prompts": [dict(r._mapping) for r in rows]}


@router.post("/calls/analytics/scoring-prompts/sync-repo")
def scoring_prompts_sync_repo(
    body: Optional[dict] = None,
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Rescrie prompturile din fișierele versionate în repo (app/services/prompts/calls/).

    Suprascrie textele editate din UI — repo-ul e sursa de adevăr.
    body: {"dry_run": bool, "keys": [...]}  (ambele opționale)
    """
    from app.services import call_scorer
    body = body or {}
    res = call_scorer.sync_prompts_from_repo(
        db,
        keys=body.get("keys") or None,
        insert_only=False,
        dry_run=bool(body.get("dry_run")),
    )
    if res.get("error"):
        raise HTTPException(status_code=500, detail="Sincronizarea prompturilor a eșuat")
    return {"ok": True, **res}


@router.get("/calls/analytics/scoring-prompts/{key}")
def scoring_prompt_get(
    key: str,
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    row = db.execute(text(
        "SELECT * FROM call_scoring_prompts WHERE key = :key"
    ), {"key": key}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Prompt inexistent")
    return dict(row._mapping)


@router.put("/calls/analytics/scoring-prompts/{key}")
def scoring_prompt_update(
    key: str,
    body: dict,
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    fields = []
    params: dict = {"key": key}
    if "prompt_text" in body:
        fields.append("prompt_text = :prompt_text")
        params["prompt_text"] = body["prompt_text"]
    if "label" in body:
        fields.append("label = :label")
        params["label"] = body["label"]
    if "enabled" in body:
        fields.append("enabled = :enabled")
        params["enabled"] = bool(body["enabled"])
    if not fields:
        raise HTTPException(status_code=400, detail="Niciun câmp de actualizat")
    fields.append("updated_at = NOW()")
    db.execute(text(
        f"UPDATE call_scoring_prompts SET {', '.join(fields)} WHERE key = :key"
    ), params)
    db.commit()
    return {"ok": True, "key": key}


@router.post("/calls/analytics/scoring-prompts")
def scoring_prompt_create(
    body: dict,
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    key = (body.get("key") or "").strip()
    label = (body.get("label") or "").strip()
    prompt_text = (body.get("prompt_text") or "").strip()
    if not key or not prompt_text:
        raise HTTPException(status_code=400, detail="key și prompt_text obligatorii")
    try:
        db.execute(text(
            "INSERT INTO call_scoring_prompts (key, label, prompt_text, enabled) "
            "VALUES (:key, :label, :pt, true)"
        ), {"key": key, "label": label, "pt": prompt_text})
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "key": key}
