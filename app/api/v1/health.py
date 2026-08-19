"""Health check + basic stats."""
from datetime import datetime, date as _date
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.database import get_db
# Leg-urile duplicate de centrala (acelasi apel logat pe doua canale) nu se numara in
# statistici — regula unica, vezi productivity.apel_no_dup_leg_sql.
from app.services import productivity as _prod
_SRC_CALLS = "(SELECT * FROM calls _c WHERE " + _prod.apel_no_dup_leg_sql("_c") + ")"
from app.config import get_settings

router = APIRouter()


def _valid_date(value: Optional[str], field: str) -> Optional[str]:
    """Validează o dată ISO (YYYY-MM-DD) primită din query string.

    Fără asta, o valoare de tip 'abc' ajunge în CAST(... AS date) și Postgres ridică
    DataError => HTTP 500. Preferăm 400 cu mesaj clar (UI-ul trimite mereu format ISO,
    dar un URL editat manual nu trebuie să dea eroare de server).
    """
    if value is None or value == "":
        return None
    try:
        return _date.fromisoformat(value.strip()).isoformat()
    except (ValueError, AttributeError):
        raise HTTPException(400, f"{field} invalid: se așteaptă formatul YYYY-MM-DD")


def _range_filter(col: str, date_from: Optional[str], date_to: Optional[str]):
    """Construieste conditia SQL pentru filtrul de perioada personalizata (de la / pana la).

    Returneaza (conditie_sql, params). Fara date_from/date_to => conditie neutra ("TRUE"),
    deci comportamentul implicit al endpoint-urilor ramane neschimbat (cumulat / tot istoricul).
    date_to e inclusiv (< date_to + 1 zi), ca sa prinda toata ziua selectata.
    """
    date_from = _valid_date(date_from, "date_from")
    date_to = _valid_date(date_to, "date_to")
    parts, params = [], {}
    if date_from:
        parts.append(f"{col} >= CAST(:_rf_from AS date)")
        params["_rf_from"] = date_from
    if date_to:
        parts.append(f"{col} < (CAST(:_rf_to AS date) + INTERVAL '1 day')")
        params["_rf_to"] = date_to
    return (" AND ".join(parts) if parts else "TRUE"), params


def _day_window(col: str, days: int, date_from: Optional[str], date_to: Optional[str]):
    """Fereastra pentru seriile zilnice (grafice gap-filled).

    Returneaza (bounds_sql, where_sql, params) unde bounds_sql se pune direct in
    generate_series(...). Cu date_from/date_to => interval explicit; altfel se pastreaza
    fereastra pe ultimele N zile, exact ca inainte.
    """
    date_from = _valid_date(date_from, "date_from")
    date_to = _valid_date(date_to, "date_to")
    if date_from or date_to:
        lo = "CAST(:_dw_from AS date)" if date_from else f"(CAST(:_dw_to AS date) - ({days} - 1))"
        hi = "CAST(:_dw_to AS date)" if date_to else "CURRENT_DATE"
        params = {}
        if date_from:
            params["_dw_from"] = date_from
        if date_to:
            params["_dw_to"] = date_to
        where = f"{col} >= {lo} AND {col} < ({hi} + INTERVAL '1 day')"
        return f"{lo}, {hi}", where, params
    return ("(CURRENT_DATE - (:days - 1)), CURRENT_DATE",
            f"{col} >= CURRENT_DATE - (:days - 1)",
            {"days": days})


@router.get("/health")
def health(db: Session = Depends(get_db)):
    s = get_settings()
    status = {
        "status": "healthy",
        "service": s.app_name,
        "version": s.app_version,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "checks": {}
    }
    try:
        db.execute(text("SELECT 1"))
        status["checks"]["database"] = "ok"
    except Exception as e:
        status["status"] = "degraded"
        status["checks"]["database"] = f"error: {str(e)[:100]}"
    try:
        import redis
        r = redis.Redis(host=s.redis_host, port=s.redis_port, db=s.redis_db, socket_connect_timeout=2)
        r.ping()
        status["checks"]["redis"] = "ok"
    except Exception as e:
        status["checks"]["redis"] = f"error: {str(e)[:100]}"
    return status


@router.get("/stats/dashboard")
def stats_dashboard(date_from: Optional[str] = Query(None),
                    date_to: Optional[str] = Query(None),
                    db: Session = Depends(get_db)):
    rf, rp = _range_filter("received_at", date_from, date_to)
    row = db.execute(text(f"""
        SELECT
          COUNT(*) FILTER (WHERE status='pending') AS pending,
          COUNT(*) FILTER (WHERE status='clean') AS clean,
          COUNT(*) FILTER (WHERE status='quarantined') AS quarantined,
          COUNT(*) FILTER (WHERE status='quarantined_strict') AS quarantined_strict,
          COUNT(*) FILTER (WHERE status='ndr') AS ndr,
          COUNT(*) FILTER (WHERE received_at > NOW() - INTERVAL '24 hours') AS last_24h,
          COUNT(*) FILTER (WHERE received_at > NOW() - INTERVAL '7 days') AS last_7d,
          COUNT(*) AS total,
          COUNT(*) FILTER (WHERE ai_category='informatie') AS ai_informatie,
          COUNT(*) FILTER (WHERE ai_category='sesizare') AS ai_sesizare,
          COUNT(*) FILTER (WHERE ai_category='reclamatie') AS ai_reclamatie,
          COUNT(*) FILTER (WHERE ai_category='necunoscut') AS ai_necunoscut,
          COUNT(*) FILTER (WHERE ai_category IS NOT NULL) AS ai_classified,
          COUNT(*) FILTER (WHERE ai_department='suport_1') AS dept_suport_1,
          COUNT(*) FILTER (WHERE ai_department='contabilitate') AS dept_contabilitate,
          COUNT(*) FILTER (WHERE ai_department='taxe_drum') AS dept_taxe_drum,
          COUNT(*) FILTER (WHERE ai_department='mobilitate') AS dept_mobilitate,
          COUNT(*) FILTER (WHERE ai_department='recuperare_tva') AS dept_recuperare_tva,
          COUNT(*) FILTER (WHERE ai_department='comercial') AS dept_comercial,
          COUNT(*) FILTER (WHERE ai_department='suport_2') AS dept_suport_2
        FROM emails
        WHERE {rf}
    """), rp).fetchone()
    d = dict(row._mapping) if row else {}
    d["date_from"] = date_from
    d["date_to"] = date_to
    return d


@router.get("/stats/daily-category")
def stats_daily_category(days: int = Query(31, ge=1, le=92),
                         date_from: Optional[str] = Query(None),
                         date_to: Optional[str] = Query(None),
                         db: Session = Depends(get_db)):
    """Per-day AI category counts (informatie/sesizare/reclamatie/necunoscut).
    Returnează doar zilele care au emailuri clasificate (fără zile goale)."""
    _b, wsql, wp = _day_window("received_at", days, date_from, date_to)
    sql = f"""
        SELECT to_char(date(received_at), 'YYYY-MM-DD') AS day,
               COUNT(*) FILTER (WHERE ai_category='informatie') AS informatie,
               COUNT(*) FILTER (WHERE ai_category='sesizare')   AS sesizare,
               COUNT(*) FILTER (WHERE ai_category='reclamatie') AS reclamatie,
               COUNT(*) FILTER (WHERE ai_category='necunoscut') AS necunoscut
        FROM emails
        WHERE {wsql} AND ai_category IS NOT NULL
        GROUP BY date(received_at)
        HAVING COUNT(*) FILTER (WHERE ai_category IS NOT NULL) > 0
        ORDER BY date(received_at)
    """
    rows = db.execute(text(sql), wp).fetchall()
    series = [dict(r._mapping) for r in rows]
    totals = {k: sum(r[k] for r in series) for k in ("informatie", "sesizare", "reclamatie", "necunoscut")}
    totals["total"] = sum(totals.values())
    return {"days": days, "series": series, "totals": totals,
            "date_from": date_from, "date_to": date_to}


@router.get("/stats/daily")
def stats_daily(days: int = Query(14, ge=1, le=90),
                threshold: int = Query(50, ge=0, le=100),
                date_from: Optional[str] = Query(None),
                date_to: Optional[str] = Query(None),
                db: Session = Depends(get_db)):
    """Per-day email counts split into the 3 product types (Email / Carantinate /
    Spam), gap-filled across the window. Spam classification mirrors /spam."""
    # spammy = same rule as the Spam list (override, or score >= threshold and not legit)
    sp = "(s.override = TRUE OR (s.override IS DISTINCT FROM FALSE AND s.spam_score >= :thr))"
    bounds, wsql, wp = _day_window("e.received_at", days, date_from, date_to)
    sql = f"""
        WITH days AS (
            SELECT generate_series(
                {bounds}, INTERVAL '1 day'
            )::date AS d
        ),
        agg AS (
            SELECT date(e.received_at) AS d,
                COUNT(*) FILTER (WHERE e.status IN ('quarantined','quarantined_strict')) AS carantinate,
                COUNT(*) FILTER (WHERE e.status NOT IN ('quarantined','quarantined_strict','ndr','pending','deleted')
                                 AND COALESCE({sp}, FALSE)) AS spam,
                COUNT(*) FILTER (WHERE e.status = 'clean'
                                 AND NOT COALESCE({sp}, FALSE)) AS email
            FROM emails e
            LEFT JOIN email_spam s ON s.email_id = e.id
            WHERE {wsql}
            GROUP BY date(e.received_at)
        )
        SELECT to_char(days.d, 'YYYY-MM-DD') AS day,
               COALESCE(agg.email, 0)       AS email,
               COALESCE(agg.carantinate, 0) AS carantinate,
               COALESCE(agg.spam, 0)        AS spam
        FROM days LEFT JOIN agg ON agg.d = days.d
        ORDER BY days.d
    """
    rows = db.execute(text(sql), dict(wp, thr=threshold)).fetchall()
    series = [dict(r._mapping) for r in rows]
    totals = {
        "email": sum(r["email"] for r in series),
        "carantinate": sum(r["carantinate"] for r in series),
        "spam": sum(r["spam"] for r in series),
    }
    totals["total"] = totals["email"] + totals["carantinate"] + totals["spam"]
    return {"days": days, "threshold": threshold, "series": series, "totals": totals,
            "date_from": date_from, "date_to": date_to}


@router.get("/stats/document-processing")
def stats_document_processing(days: int = Query(60, ge=1, le=365),
                              date_from: Optional[str] = Query(None),
                              date_to: Optional[str] = Query(None),
                              db: Session = Depends(get_db)):
    """Acuratețea procesării documentelor (atașamente) pe fereastra aleasă, din document_extractions.
    Bază = documente top-level (grouped_into IS NULL) create în interval. Read-only.
    „corectate" (operatorul a modificat date/tip la salvare) necesită coloana `corrected`
    (migrație 20260622_doc_corrections) — dacă lipsește, tracking-ul e marcat indisponibil."""
    has_corr = db.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='document_extractions' AND column_name='corrected'"
    )).fetchone() is not None
    # Perioada personalizata (date_from/date_to) are prioritate peste fereastra pe zile.
    if date_from or date_to:
        rf, qp = _range_filter("created_at", date_from, date_to)
        base = f"FROM document_extractions WHERE grouped_into IS NULL AND {rf}"
    else:
        qp = {"days": days}
        base = ("FROM document_extractions WHERE grouped_into IS NULL "
                "AND created_at >= CURRENT_DATE - (:days - 1)")
    corr_all = "COUNT(*) FILTER (WHERE reviewed AND corrected)" if has_corr else "NULL"

    row = db.execute(text(
        "SELECT COUNT(*) AS total,"
        " COUNT(*) FILTER (WHERE status IN ('classified','extracted','needs_review')) AS incadrate,"
        " COUNT(*) FILTER (WHERE status='extracted') AS extrase,"
        " COUNT(*) FILTER (WHERE reviewed) AS verificate,"
        " COUNT(*) FILTER (WHERE status='failed') AS esuate,"
        " COUNT(*) FILTER (WHERE status='needs_vision') AS needs_vision,"
        " COUNT(*) FILTER (WHERE status='neidentificat') AS neidentificate,"
        f" {corr_all} AS corectate "
        + base), qp).fetchone()
    m = dict(row._mapping)
    total = m["total"] or 0
    rev = m["verificate"] or 0

    def pct(n, d):
        return round((n or 0) * 100.0 / d) if d else None

    summary = {
        "total": total,
        "incadrate": m["incadrate"], "incadrate_pct": pct(m["incadrate"], total),
        "extrase": m["extrase"], "extrase_pct": pct(m["extrase"], total),
        "verificate": rev, "verificate_pct": pct(rev, total),
        "esuate": m["esuate"], "esuate_pct": pct(m["esuate"], total),
        "neidentificate": m["neidentificate"], "neidentificate_pct": pct(m["neidentificate"], total),
        "needs_vision": m["needs_vision"],
        "corectate": m["corectate"],
        "corectate_pct": (pct(m["corectate"], rev) if (has_corr and rev) else None),
        "confirmate_corecte_pct": (pct(rev - (m["corectate"] or 0), rev) if (has_corr and rev) else None),
        "corrections_tracked": has_corr,
    }

    corr_sel = "COUNT(*) FILTER (WHERE reviewed AND corrected)" if has_corr else "NULL"
    trows = db.execute(text(
        "SELECT COALESCE(NULLIF(detected_type,''),'(fără tip)') AS tip,"
        " COUNT(*) AS total,"
        " COUNT(*) FILTER (WHERE reviewed) AS reviewed,"
        f" {corr_sel} AS corrected "
        + base + " AND status NOT IN ('neidentificat','failed') "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT 25"), qp).fetchall()
    by_type = []
    for r in trows:
        t = dict(r._mapping)
        rv = t["reviewed"] or 0
        t["correct_pct"] = (pct(rv - (t["corrected"] or 0), rv) if (has_corr and rv) else None)
        by_type.append(t)

    return {"days": days, "summary": summary, "by_type": by_type}


@router.get("/stats/overview")
def stats_overview(threshold: int = Query(50, ge=0, le=100),
                   date_from: Optional[str] = Query(None),
                   date_to: Optional[str] = Query(None),
                   db: Session = Depends(get_db)):
    """Extra dashboard aggregates (toate din tabelul emails / email_spam):
    distributie verdict (cu spam), atasamente, distributie confidenta AI,
    scor mediu, si volum pe ora in ultimele 24h. Read-only."""
    rf, rp = _range_filter("received_at", date_from, date_to)
    rf_e, _ = _range_filter("e.received_at", date_from, date_to)
    conf = "CASE WHEN jsonb_typeof(ai_result->'confidence')='number' THEN (ai_result->>'confidence')::float END"
    row = db.execute(text(f"""
        SELECT
          COUNT(*) AS total,
          COUNT(*) FILTER (WHERE has_attachments) AS att_with,
          COUNT(*) FILTER (WHERE NOT COALESCE(has_attachments, FALSE)) AS att_without,
          COUNT(*) FILTER (WHERE {conf} IS NOT NULL AND {conf} >= 0.75) AS conf_high,
          COUNT(*) FILTER (WHERE {conf} IS NOT NULL AND {conf} >= 0.5 AND {conf} < 0.75) AS conf_med,
          COUNT(*) FILTER (WHERE {conf} IS NOT NULL AND {conf} < 0.5) AS conf_low,
          ROUND(AVG({conf})::numeric, 3) AS conf_avg,
          ROUND(AVG(phishing_score)::numeric, 1) AS score_avg,
          COUNT(*) FILTER (WHERE queue_status='sent_to_cts') AS cts_sent,
          COUNT(*) FILTER (WHERE queue_status='ready_for_cts' AND cts_send_error IS NULL) AS cts_ready,
          COUNT(*) FILTER (WHERE queue_status='ready_for_cts' AND cts_send_error IS NOT NULL) AS cts_send_error,
          COUNT(*) FILTER (WHERE queue_status='error_nova') AS cts_error_nova,
          COUNT(*) FILTER (WHERE queue_status IN ('queued_general','intent_check','categorized')) AS cts_in_progress,
          COUNT(*) FILTER (WHERE queue_status IN ('stopped_spam','stopped_quarantine','stopped_ndr','stopped_duplicate')) AS cts_stopped
        FROM emails
        WHERE {rf}
    """), rp).fetchone()
    sp = "(s.override = TRUE OR (s.override IS DISTINCT FROM FALSE AND s.spam_score >= :thr))"
    spam = db.execute(text(f"""
        SELECT COUNT(*) FROM emails e LEFT JOIN email_spam s ON s.email_id = e.id
        WHERE e.status NOT IN ('quarantined','quarantined_strict','ndr','pending','deleted')
          AND COALESCE({sp}, FALSE)
          AND {rf_e}
    """), dict(rp, thr=threshold)).scalar()
    # Volumul pe ora: implicit ultimele 24h. Cu perioada selectata, ancora devine
    # sfarsitul perioadei (date_to), ca sa arate ultima zi din intervalul cerut.
    if date_to:
        anchor = "(CAST(:_rf_to AS date) + INTERVAL '1 day')"
    else:
        anchor = "NOW()"
    hrows = db.execute(text(f"""
        WITH hours AS (
            SELECT generate_series(
                date_trunc('hour', {anchor} - INTERVAL '23 hours'),
                date_trunc('hour', {anchor}), INTERVAL '1 hour'
            ) AS h
        ),
        agg AS (
            SELECT date_trunc('hour', received_at) AS h, COUNT(*) AS n
            FROM emails WHERE received_at > {anchor} - INTERVAL '24 hours'
              AND received_at <= {anchor}
            GROUP BY 1
        )
        SELECT to_char(hours.h, 'HH24:00') AS hour, COALESCE(agg.n, 0) AS n
        FROM hours LEFT JOIN agg ON agg.h = hours.h
        ORDER BY hours.h
    """), ({"_rf_to": date_to} if date_to else {})).fetchall()
    try:
        crows = db.execute(text(f"""
            SELECT c.name AS name, COUNT(*) AS n
            FROM emails e JOIN clients c ON c.id = e.client_id
            WHERE e.client_id IS NOT NULL
              AND {rf_e}
            GROUP BY c.name ORDER BY n DESC, c.name LIMIT 8
        """), rp).fetchall()
        top_clients = [dict(r._mapping) for r in crows]
    except Exception:
        top_clients = []
    d = dict(row._mapping) if row else {}
    d["spam"] = int(spam or 0)
    d["hourly"] = [dict(r._mapping) for r in hrows]
    d["top_clients"] = top_clients
    return d


# ── Statistici apeluri (While1) — mirror 1:1 pe seturile de mai sus, dar din `calls` ──────

@router.get("/stats/calls-dashboard")
def stats_calls_dashboard(date_from: Optional[str] = Query(None),
                          date_to: Optional[str] = Query(None),
                          db: Session = Depends(get_db)):
    rf, rp = _range_filter("started_at", date_from, date_to)
    row = db.execute(text(f"""
        SELECT
          COUNT(*) AS total,
          COUNT(*) FILTER (WHERE direction='inbound') AS inbound,
          COUNT(*) FILTER (WHERE direction='outbound') AS outbound,
          COUNT(*) FILTER (WHERE started_at > NOW() - INTERVAL '24 hours') AS last_24h,
          COUNT(*) FILTER (WHERE started_at > NOW() - INTERVAL '7 days') AS last_7d,
          COUNT(*) FILTER (WHERE ai_category='informatie') AS ai_informatie,
          COUNT(*) FILTER (WHERE ai_category='sesizare') AS ai_sesizare,
          COUNT(*) FILTER (WHERE ai_category='reclamatie') AS ai_reclamatie,
          COUNT(*) FILTER (WHERE ai_category='necunoscut') AS ai_necunoscut,
          COUNT(*) FILTER (WHERE ai_category IS NOT NULL) AS ai_classified,
          COUNT(*) FILTER (WHERE transcript_status='success') AS transcribed,
          COUNT(*) FILTER (WHERE client_id IS NOT NULL) AS client_matched,
          COUNT(*) FILTER (WHERE call_status='ANSWERED') AS status_answered,
          COUNT(*) FILTER (WHERE call_status='NO ANSWER') AS status_no_answer,
          COUNT(*) FILTER (WHERE call_status='BUSY') AS status_busy,
          COUNT(*) FILTER (WHERE call_status IS NOT NULL
                           AND call_status NOT IN ('ANSWERED','NO ANSWER','BUSY')) AS status_other,
          ROUND(AVG(duration_seconds)) AS avg_duration_seconds,
          COALESCE(SUM(duration_seconds), 0) AS total_duration_seconds
        FROM {_SRC_CALLS} calls
        WHERE {rf}
    """), rp).fetchone()
    d = dict(row._mapping) if row else {}
    d["date_from"] = date_from
    d["date_to"] = date_to
    return d


@router.get("/stats/calls-daily")
def stats_calls_daily(days: int = Query(14, ge=1, le=90),
                      date_from: Optional[str] = Query(None),
                      date_to: Optional[str] = Query(None),
                      db: Session = Depends(get_db)):
    """Volum zilnic de apeluri, gap-filled, pe direcție (inbound/outbound) + rată răspuns."""
    bounds, wsql, wp = _day_window("started_at", days, date_from, date_to)
    sql = f"""
        WITH days AS (
            SELECT generate_series(
                {bounds}, INTERVAL '1 day'
            )::date AS d
        ),
        agg AS (
            SELECT date(started_at) AS d,
                COUNT(*) FILTER (WHERE direction='inbound') AS inbound,
                COUNT(*) FILTER (WHERE direction='outbound') AS outbound,
                COUNT(*) FILTER (WHERE call_status='ANSWERED') AS answered,
                COUNT(*) AS total
            FROM {_SRC_CALLS} calls
            WHERE {wsql}
            GROUP BY date(started_at)
        )
        SELECT to_char(days.d, 'YYYY-MM-DD') AS day,
               COALESCE(agg.inbound, 0)  AS inbound,
               COALESCE(agg.outbound, 0) AS outbound,
               COALESCE(agg.answered, 0) AS answered,
               COALESCE(agg.total, 0)    AS total
        FROM days LEFT JOIN agg ON agg.d = days.d
        ORDER BY days.d
    """
    rows = db.execute(text(sql), wp).fetchall()
    series = [dict(r._mapping) for r in rows]
    for r in series:
        r["answered_pct"] = round(r["answered"] * 100.0 / r["total"], 1) if r["total"] else None
    totals = {
        "inbound": sum(r["inbound"] for r in series),
        "outbound": sum(r["outbound"] for r in series),
        "answered": sum(r["answered"] for r in series),
    }
    totals["total"] = totals["inbound"] + totals["outbound"]
    return {"days": days, "series": series, "totals": totals,
            "date_from": date_from, "date_to": date_to}


@router.get("/stats/calls-daily-category")
def stats_calls_daily_category(days: int = Query(31, ge=1, le=92),
                               date_from: Optional[str] = Query(None),
                               date_to: Optional[str] = Query(None),
                               db: Session = Depends(get_db)):
    """Per-day AI category counts pentru apeluri (informatie/sesizare/reclamatie/necunoscut).
    Returnează doar zilele care au apeluri clasificate (fără zile goale)."""
    _b, wsql, wp = _day_window("started_at", days, date_from, date_to)
    sql = f"""
        SELECT to_char(date(started_at), 'YYYY-MM-DD') AS day,
               COUNT(*) FILTER (WHERE ai_category='informatie') AS informatie,
               COUNT(*) FILTER (WHERE ai_category='sesizare')   AS sesizare,
               COUNT(*) FILTER (WHERE ai_category='reclamatie') AS reclamatie,
               COUNT(*) FILTER (WHERE ai_category='necunoscut') AS necunoscut
        FROM {_SRC_CALLS} calls
        WHERE {wsql} AND ai_category IS NOT NULL
        GROUP BY date(started_at)
        HAVING COUNT(*) FILTER (WHERE ai_category IS NOT NULL) > 0
        ORDER BY date(started_at)
    """
    rows = db.execute(text(sql), wp).fetchall()
    series = [dict(r._mapping) for r in rows]
    totals = {k: sum(r[k] for r in series) for k in ("informatie", "sesizare", "reclamatie", "necunoscut")}
    totals["total"] = sum(totals.values())
    return {"days": days, "series": series, "totals": totals,
            "date_from": date_from, "date_to": date_to}


@router.get("/stats/calls-overview")
def stats_calls_overview(date_from: Optional[str] = Query(None),
                         date_to: Optional[str] = Query(None),
                         db: Session = Depends(get_db)):
    """Volum orar (24h), top clienți și top agenți după nr. de apeluri. Read-only."""
    rf, rp = _range_filter("started_at", date_from, date_to)
    rf_c, _ = _range_filter("c.started_at", date_from, date_to)
    anchor = "(CAST(:_rf_to AS date) + INTERVAL '1 day')" if date_to else "NOW()"
    hrows = db.execute(text(f"""
        WITH hours AS (
            SELECT generate_series(
                date_trunc('hour', {anchor} - INTERVAL '23 hours'),
                date_trunc('hour', {anchor}), INTERVAL '1 hour'
            ) AS h
        ),
        agg AS (
            SELECT date_trunc('hour', started_at) AS h, COUNT(*) AS n
            FROM {_SRC_CALLS} calls WHERE started_at > {anchor} - INTERVAL '24 hours'
              AND started_at <= {anchor}
            GROUP BY 1
        )
        SELECT to_char(hours.h, 'HH24:00') AS hour, COALESCE(agg.n, 0) AS n
        FROM hours LEFT JOIN agg ON agg.h = hours.h
        ORDER BY hours.h
    """), ({"_rf_to": date_to} if date_to else {})).fetchall()
    try:
        crows = db.execute(text(f"""
            SELECT cl.name AS name, COUNT(*) AS n
            FROM {_SRC_CALLS} c JOIN clients cl ON cl.id = c.client_id
            WHERE c.client_id IS NOT NULL
              AND {rf_c}
            GROUP BY cl.name ORDER BY n DESC, cl.name LIMIT 8
        """), rp).fetchall()
        top_clients = [dict(r._mapping) for r in crows]
    except Exception:
        top_clients = []
    arows = db.execute(text(f"""
        SELECT ai_assignee AS name, COUNT(*) AS n
        FROM {_SRC_CALLS} calls WHERE ai_assignee IS NOT NULL
          AND {rf}
        GROUP BY ai_assignee ORDER BY n DESC LIMIT 8
    """), rp).fetchall()
    top_agents = [dict(r._mapping) for r in arows]
    return {
        "hourly": [dict(r._mapping) for r in hrows],
        "top_clients": top_clients,
        "top_agents": top_agents,
    }


# ── Statistici task-uri (CTS ground-truth) — cumulatul (total/status/departament) e deja
# expus de GET /cts-tasks-training/stats; aici doar seria zilnică + trafic, care lipseau. ──

@router.get("/stats/tasks-daily")
def stats_tasks_daily(days: int = Query(14, ge=1, le=90),
                      date_from: Optional[str] = Query(None),
                      date_to: Optional[str] = Query(None),
                      db: Session = Depends(get_db)):
    """Volum zilnic de task-uri (create vs rezolvate) + timp mediu de rezolvare pe zi,
    gap-filled. `created`/`resolved` sunt pe zile diferite (data creării, resp. a
    ultimei actualizări la solved/closed) — pot diverge de la o zi la alta."""
    bounds, w_created, wp = _day_window("cts_created_at", days, date_from, date_to)
    _b2, w_resolved, _p2 = _day_window("gt.cts_updated_at", days, date_from, date_to)
    sql = f"""
        WITH days AS (
            SELECT generate_series(
                {bounds}, INTERVAL '1 day'
            )::date AS d
        ),
        created_agg AS (
            SELECT date(cts_created_at) AS d, COUNT(*) AS created
            FROM cts_task_ground_truth
            WHERE {w_created}
            GROUP BY date(cts_created_at)
        ),
        resolved_agg AS (
            SELECT date(gt.cts_updated_at) AS d, COUNT(*) AS resolved,
                   AVG(business_minutes_emp(edm.department, edm.id, gt.cts_created_at, gt.cts_updated_at, ARRAY[]::date[])) AS avg_resolution_minutes
            FROM cts_task_ground_truth gt
            JOIN employee_department_mapping edm ON edm.id = gt.assignee_employee_id
            WHERE lower(gt.status) IN ('solved', 'closed')
              AND {w_resolved}
              AND gt.cts_created_at IS NOT NULL
              AND gt.assignee_employee_id IS NOT NULL
            GROUP BY date(gt.cts_updated_at)
        )
        SELECT to_char(days.d, 'YYYY-MM-DD') AS day,
               COALESCE(created_agg.created, 0) AS created,
               COALESCE(resolved_agg.resolved, 0) AS resolved,
               resolved_agg.avg_resolution_minutes AS avg_resolution_minutes
        FROM days
        LEFT JOIN created_agg ON created_agg.d = days.d
        LEFT JOIN resolved_agg ON resolved_agg.d = days.d
        ORDER BY days.d
    """
    rows = db.execute(text(sql), wp).fetchall()
    series = [dict(r._mapping) for r in rows]
    for r in series:
        m = r.pop("avg_resolution_minutes")
        r["avg_resolution_hours"] = round(float(m) / 60.0, 1) if m is not None else None
    totals = {
        "created": sum(r["created"] for r in series),
        "resolved": sum(r["resolved"] for r in series),
    }
    return {"days": days, "series": series, "totals": totals,
            "date_from": date_from, "date_to": date_to}


@router.get("/stats/tasks-overview")
def stats_tasks_overview(date_from: Optional[str] = Query(None),
                         date_to: Optional[str] = Query(None),
                         db: Session = Depends(get_db)):
    """Volum orar (24h, pe data creării), top clienți și top agenți după nr. de task-uri,
    plus timpul mediu de rezolvare cumulat (all-time, solved/closed).
    Cu date_from/date_to, toate agregatele se restrang la perioada ceruta."""
    rf, rp = _range_filter("gt.cts_created_at", date_from, date_to)
    avg_resolution = db.execute(text(f"""
        SELECT AVG(business_minutes_emp(edm.department, edm.id, gt.cts_created_at, gt.cts_updated_at, ARRAY[]::date[]))
        FROM cts_task_ground_truth gt
        JOIN employee_department_mapping edm ON edm.id = gt.assignee_employee_id
        WHERE lower(gt.status) IN ('solved', 'closed') AND gt.cts_created_at IS NOT NULL AND gt.cts_updated_at IS NOT NULL
          AND {rf}
    """), rp).scalar()
    if date_to:
        anchor = "(CAST(:_rf_to AS date) + INTERVAL '1 day')"
    else:
        anchor = "NOW()"
    hrows = db.execute(text(f"""
        WITH hours AS (
            SELECT generate_series(
                date_trunc('hour', {anchor} - INTERVAL '23 hours'),
                date_trunc('hour', {anchor}), INTERVAL '1 hour'
            ) AS h
        ),
        agg AS (
            SELECT date_trunc('hour', cts_created_at) AS h, COUNT(*) AS n
            FROM cts_task_ground_truth WHERE cts_created_at > {anchor} - INTERVAL '24 hours'
              AND cts_created_at <= {anchor}
            GROUP BY 1
        )
        SELECT to_char(hours.h, 'HH24:00') AS hour, COALESCE(agg.n, 0) AS n
        FROM hours LEFT JOIN agg ON agg.h = hours.h
        ORDER BY hours.h
    """), ({"_rf_to": date_to} if date_to else {})).fetchall()
    crows = db.execute(text(f"""
        SELECT COALESCE(cl.name, gt.client_name) AS name, COUNT(*) AS n
        FROM cts_task_ground_truth gt LEFT JOIN clients cl ON cl.iris_client_id = gt.client_id
        WHERE COALESCE(cl.name, gt.client_name) IS NOT NULL
          AND upper(COALESCE(cl.name, gt.client_name)) <> 'UNKNOWN CLIENT'
          AND {rf}
        GROUP BY 1 ORDER BY n DESC, 1 LIMIT 8
    """), rp).fetchall()
    arows = db.execute(text(f"""
        SELECT COALESCE(edm.name, gt.assignee_raw) AS name, COUNT(*) AS n
        FROM cts_task_ground_truth gt
        LEFT JOIN employee_department_mapping edm ON edm.id = gt.assignee_employee_id
        WHERE COALESCE(edm.name, gt.assignee_raw) IS NOT NULL
          AND {rf}
        GROUP BY 1 ORDER BY n DESC, 1 LIMIT 8
    """), rp).fetchall()
    return {
        "hourly": [dict(r._mapping) for r in hrows],
        "top_clients": [dict(r._mapping) for r in crows],
        "top_agents": [dict(r._mapping) for r in arows],
        "avg_resolution_minutes": (round(float(avg_resolution), 1) if avg_resolution is not None else None),
    }
