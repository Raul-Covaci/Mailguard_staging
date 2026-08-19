"""v0.9.8 — Clients: list, detail, emails per client, satisfaction estimation.
OPS-2026-0124: vehicule + contracte per client (sincronizate din CTS via IRIS).
T1: satisfaction_engine (motor determinist, toate sursele, ponderi configurabile).
T2: satisfaction_snapshot — job lunar + trigger manual."""
import csv
import io
import json
import logging
import re
import uuid
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.config import get_settings
from app.database import get_db
from app.services import iris_sync
from app.services import iris_ai
from app.services import satisfaction_engine
from app.services import satisfaction_snapshot
from app.services import cts_groundtruth_sync as _cts_sync

logger = logging.getLogger("mailguard.clients")
router = APIRouter()

# Promptul de satisfacție a fost mutat în satisfaction_engine.SATISFACTION_SYSTEM
# (sursă unică folosită și de buton, și de snapshot-ul lunar).


@router.post("/clients/sync-now")
def sync_now(wait: bool = False):
    """Sync clienti + vehicule + contracte din IRIS (include=vehicles,contracts).
    Ruleaza in FUNDAL (daemon thread) si intoarce IMEDIAT — pull-ul complet (~16k clienti
    cu liste) poate dura ~60-90s si ar pica pe timeout-ul worker-ului (60s). wait=true ->
    sincron (debug/cron). Starea se vede in settings['client_assets.last_result']."""
    if wait:
        return iris_sync.sync_clients_guarded()
    import threading as _th
    _th.Thread(target=iris_sync.sync_clients_guarded, daemon=True).start()
    return {"status": "ok", "started": True, "async": True,
            "message": "Sync pornit in fundal. Datele se actualizeaza in cateva momente."}


@router.get("/clients/sync-status")
def sync_status(db: Session = Depends(get_db)):
    """Starea ultimului sync de clienti (scrisa in fundal de sync_clients_guarded).
    Folosit de UI pt. polling dupa pornirea sync-ului async."""
    row = db.execute(text(
        "SELECT value FROM settings WHERE key = 'client_assets.last_result'"
    )).fetchone()
    if not row or row[0] is None:
        return {"status": "unknown"}
    val = row[0]
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except Exception:
            return {"status": "unknown"}
    return val


@router.get("/clients")
def list_clients(db: Session = Depends(get_db), page: int = 1, q: str = "",
                 priority: str = "", sort: str = ""):
    per_page = 50
    offset = (page - 1) * per_page

    where_parts = ["c.is_active = true"]
    params: dict = {"lim": per_page, "off": offset}

    if q:
        where_parts.append(
            "(c.name ILIKE :q OR c.emails::text ILIKE :q OR c.phones::text ILIKE :q)"
        )
        params["q"] = f"%{q}%"

    if priority in ("1", "2"):
        where_parts.append("c.email_priority = :priority")
        params["priority"] = int(priority)

    order_by = "c.name"
    if sort == "email_count_desc":
        order_by = "email_count DESC, c.name"
    elif sort == "email_count_asc":
        order_by = "email_count ASC, c.name"

    where = " AND ".join(where_parts)
    count_params = {k: v for k, v in params.items() if k not in ("lim", "off")}

    rows = db.execute(text(f"""
        SELECT c.id, c.iris_client_id, c.name, c.emails, c.phones,
               c.last_synced_at, c.email_priority,
               (SELECT s.satisfaction_pct
                FROM client_satisfaction_snapshots s
                WHERE s.client_id = c.id AND s.satisfaction_pct IS NOT NULL
                ORDER BY s.month_key DESC LIMIT 1
               ) AS satisfaction_pct,
               (SELECT COUNT(*) FROM emails e WHERE e.client_id = c.id)
               + (SELECT COUNT(*) FROM (
                    SELECT e2.id FROM cts_ground_truth g
                    JOIN emails e2 ON e2.id = g.email_id
                    WHERE e2.client_id IS NULL
                      AND COALESCE(g.cts_direction,'received') = 'received'
                      AND c.iris_client_id IS NOT NULL
                      AND g.raw->'extra'->>'client_id' = c.iris_client_id::text
                  ) orp
               ) AS email_count,
               (SELECT COUNT(*) FROM cts_ground_truth g
                  WHERE g.cts_direction = 'sent'
                    AND c.iris_client_id IS NOT NULL
                    AND g.raw->'extra'->>'client_id' = c.iris_client_id::text
               ) AS sent_count,
               -- Apeluri: aceeasi imagine ca la mailuri (primite / date), ca lista sa arate
               -- toata conversatia cu clientul, nu doar canalul de email. `direction` e
               -- 'inbound'/'outbound' in tabela calls.
               (SELECT COUNT(*) FROM calls ca
                  WHERE ca.client_id = c.id AND ca.direction = 'inbound') AS call_in_count,
               (SELECT COUNT(*) FROM calls ca
                  WHERE ca.client_id = c.id AND ca.direction = 'outbound') AS call_out_count,
               (SELECT COUNT(*) FROM client_vehicles v WHERE v.client_id = c.id) AS vehicle_count,
               (SELECT COUNT(*) FROM client_contracts ct WHERE ct.client_id = c.id) AS contract_count
        FROM clients c
        WHERE {where}
        ORDER BY {order_by}
        LIMIT :lim OFFSET :off
    """), params).fetchall()

    total = db.execute(
        text(f"SELECT COUNT(*) FROM clients c WHERE {where}"), count_params
    ).scalar()

    total_pages = max(1, (total + per_page - 1) // per_page)

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "items": [dict(r._mapping) for r in rows],
    }


@router.get("/clients/satisfaction-sample")
def get_satisfaction_sample(
    n_very_active: int = Query(default=10, ge=0, le=50),
    n_active: int = Query(default=10, ge=0, le=50),
    n_low: int = Query(default=10, ge=0, le=50),
    n_inactive: int = Query(default=10, ge=0, le=50),
    n_random: int = Query(default=10, ge=0, le=50),
    window_days: int = Query(default=90, ge=7, le=365),
    db: Session = Depends(get_db),
):
    """Returnează un eșantion stratificat de clienți pentru rulare satisfacție selectivă.

    Activitate = mailuri + apeluri + task-uri în ultimele window_days zile.
    Praguri: very_active>=20, active>=5, low_active>=1, inactive=0.
    Returnează client_id + name + activity_score per bucket + random.
    """
    sample_sql = """
    WITH activity AS (
        SELECT c.id, c.iris_client_id, c.name,
            (
                SELECT COUNT(*) FROM emails e
                WHERE e.client_id = c.id
                  AND e.received_at > NOW() - (:wd * INTERVAL '1 day')
            ) +
            (
                SELECT COUNT(*) FROM calls cl
                WHERE cl.client_id = c.id
                  AND cl.started_at > NOW() - (:wd * INTERVAL '1 day')
            ) AS activity_score
        FROM clients c WHERE c.is_active = true
    ),
    bucketed AS (
        SELECT id, iris_client_id, name, activity_score,
            CASE
                WHEN activity_score >= 20 THEN 'very_active'
                WHEN activity_score >= 5  THEN 'active'
                WHEN activity_score >= 1  THEN 'low_active'
                ELSE 'inactive'
            END AS bucket
        FROM activity
    )
    (SELECT id, iris_client_id, name, activity_score, bucket
     FROM bucketed WHERE bucket = 'very_active'
     ORDER BY activity_score DESC LIMIT :nva)
    UNION ALL
    (SELECT id, iris_client_id, name, activity_score, bucket
     FROM bucketed WHERE bucket = 'active'
     ORDER BY activity_score DESC LIMIT :na)
    UNION ALL
    (SELECT id, iris_client_id, name, activity_score, bucket
     FROM bucketed WHERE bucket = 'low_active'
     ORDER BY activity_score DESC LIMIT :nl)
    UNION ALL
    (SELECT id, iris_client_id, name, activity_score, bucket
     FROM bucketed WHERE bucket = 'inactive'
     ORDER BY RANDOM() LIMIT :ni)
    UNION ALL
    (SELECT id, iris_client_id, name, activity_score, 'random' AS bucket
     FROM bucketed ORDER BY RANDOM() LIMIT :nr)
    """
    rows = db.execute(text(sample_sql), {
        "wd": window_days, "nva": n_very_active, "na": n_active,
        "nl": n_low, "ni": n_inactive, "nr": n_random,
    }).fetchall()
    # dedup dacă un client apare în mai multe bucket-uri (random poate suprapune)
    seen = set()
    items = []
    for r in rows:
        if r._mapping["id"] not in seen:
            seen.add(r._mapping["id"])
            items.append(dict(r._mapping))
    return {"total": len(items), "items": items, "window_days": window_days}


@router.post("/clients/satisfaction-snapshot/run")
def run_satisfaction_snapshot(
    month: str = Query(default=None, description="YYYY-MM (default: luna curentă)"),
    dry_run: bool = Query(default=False),
    client_ids: str = Query(default=None, description="CSV de client_id — ex: 1,2,3 (default: toți)"),
    force: bool = Query(default=False, description="Recalculează snapshot-urile existente ale lunii (ON CONFLICT DO UPDATE). Default False = idempotent no-op."),
    db: Session = Depends(get_db),
):
    """T2: Trigger manual snapshot lunar satisfacție.

    client_ids: CSV opțional — rulează doar pe clienții specificați.
    Util pentru verificare selectivă pe eșantion (ex: din /clients/satisfaction-sample).
    """
    if month and not re.match(r"^\d{4}-\d{2}$", month):
        raise HTTPException(400, "Format month invalid — folosiți YYYY-MM")
    ids_list = None
    if client_ids:
        try:
            ids_list = [int(x.strip()) for x in client_ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "client_ids trebuie să fie numere întregi separate prin virgulă")
        if len(ids_list) > 500:
            raise HTTPException(400, "Maxim 500 client_ids per rulare selectivă")
    try:
        stats = satisfaction_snapshot.run_monthly_snapshot(
            month_key=month or None, client_ids=ids_list, dry_run=dry_run, force=force
        )
    except Exception as e:
        logger.exception("satisfaction_snapshot trigger error")
        raise HTTPException(500, str(e))
    return stats


@router.get("/clients/satisfaction-stats")
def get_satisfaction_stats(
    months: int = Query(default=12, ge=1, le=36),
    db: Session = Depends(get_db),
):
    """T3: Date agregate din snapshot-uri pentru dashboard satisfacție."""
    trend_rows = db.execute(text("""
        SELECT month_key,
               ROUND(AVG(satisfaction_pct)::numeric, 1)  AS avg_pct,
               COUNT(*)                                   AS total_clients,
               SUM(CASE WHEN is_unsatisfied THEN 1 ELSE 0 END) AS unsatisfied_count
        FROM client_satisfaction_snapshots
        WHERE satisfaction_pct IS NOT NULL
          AND month_key >= TO_CHAR(NOW() - (CAST(:m AS int) * INTERVAL '1 month'), 'YYYY-MM')
          AND client_id NOT IN (SELECT id FROM clients WHERE satisfaction_exclude = TRUE)
        GROUP BY month_key
        ORDER BY month_key
    """), {"m": months}).fetchall()

    trend = [
        {"month": r._mapping["month_key"], "avg_pct": float(r._mapping["avg_pct"] or 0),
         "total_clients": r._mapping["total_clients"], "unsatisfied_count": r._mapping["unsatisfied_count"]}
        for r in trend_rows
    ]

    last_month_row = db.execute(text(
        "SELECT MAX(month_key) FROM client_satisfaction_snapshots WHERE satisfaction_pct IS NOT NULL"
        " AND client_id NOT IN (SELECT id FROM clients WHERE satisfaction_exclude = TRUE)"
    )).fetchone()
    last_month = (last_month_row[0] if last_month_row else None) or ""

    unsatisfied_rows = db.execute(text("""
        SELECT s.client_id, c.name, s.satisfaction_pct, s.carry_forward, s.breakdown
        FROM client_satisfaction_snapshots s
        JOIN clients c ON c.id = s.client_id
        WHERE s.month_key = :mk AND s.is_unsatisfied = TRUE
          AND c.satisfaction_exclude = FALSE
        ORDER BY s.satisfaction_pct ASC LIMIT 50
    """), {"mk": last_month}).fetchall()
    def _parse_bd(raw):
        if raw is None:
            return None
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return None
        return raw
    unsatisfied = [{"client_id": r._mapping["client_id"], "name": r._mapping["name"],
                    "satisfaction_pct": float(r._mapping["satisfaction_pct"] or 0),
                    "carry_forward": r._mapping["carry_forward"],
                    "breakdown": _parse_bd(r._mapping["breakdown"])} for r in unsatisfied_rows]

    dist_row = db.execute(text("""
        SELECT
            SUM(CASE WHEN satisfaction_pct < 40  THEN 1 ELSE 0 END) AS sub40,
            SUM(CASE WHEN satisfaction_pct >= 40 AND satisfaction_pct < 60 THEN 1 ELSE 0 END) AS p4060,
            SUM(CASE WHEN satisfaction_pct >= 60 AND satisfaction_pct < 75 THEN 1 ELSE 0 END) AS p6075,
            SUM(CASE WHEN satisfaction_pct >= 75 AND satisfaction_pct < 90 THEN 1 ELSE 0 END) AS p7590,
            SUM(CASE WHEN satisfaction_pct >= 90  THEN 1 ELSE 0 END) AS peste90,
            COUNT(*) AS total
        FROM client_satisfaction_snapshots
        WHERE month_key = :mk AND satisfaction_pct IS NOT NULL
          AND client_id NOT IN (SELECT id FROM clients WHERE satisfaction_exclude = TRUE)
    """), {"mk": last_month}).fetchone()
    distribution = {}
    if dist_row:
        dm = dict(dist_row._mapping)
        distribution = {"labels": ["<40%", "40–60%", "60–75%", "75–90%", ">90%"],
                        "values": [dm.get("sub40",0), dm.get("p4060",0), dm.get("p6075",0),
                                   dm.get("p7590",0), dm.get("peste90",0)],
                        "total": dm.get("total", 0)}

    prev_month = trend[-2]["month"] if len(trend) >= 2 else None
    movers = []
    if prev_month and last_month and prev_month != last_month:
        mover_rows = db.execute(text("""
            SELECT c.id, c.name, curr.satisfaction_pct AS curr_pct, prev.satisfaction_pct AS prev_pct,
                   (curr.satisfaction_pct - prev.satisfaction_pct) AS delta
            FROM client_satisfaction_snapshots curr
            JOIN client_satisfaction_snapshots prev ON prev.client_id = curr.client_id AND prev.month_key = :pm
            JOIN clients c ON c.id = curr.client_id
            WHERE curr.month_key = :cm AND curr.satisfaction_pct IS NOT NULL AND prev.satisfaction_pct IS NOT NULL
              AND c.satisfaction_exclude = FALSE
            ORDER BY ABS(curr.satisfaction_pct - prev.satisfaction_pct) DESC LIMIT 10
        """), {"pm": prev_month, "cm": last_month}).fetchall()
        movers = [{"client_id": r._mapping["id"], "name": r._mapping["name"],
                   "curr_pct": float(r._mapping["curr_pct"] or 0),
                   "prev_pct": float(r._mapping["prev_pct"] or 0),
                   "delta": float(r._mapping["delta"] or 0)} for r in mover_rows]

    summary_row = db.execute(text("""
        SELECT COUNT(*) AS total_with_snapshot,
               SUM(CASE WHEN is_unsatisfied THEN 1 ELSE 0 END) AS total_unsatisfied,
               ROUND(AVG(satisfaction_pct)::numeric, 1) AS avg_pct
        FROM client_satisfaction_snapshots
        WHERE month_key = :mk AND satisfaction_pct IS NOT NULL
          AND client_id NOT IN (SELECT id FROM clients WHERE satisfaction_exclude = TRUE)
    """), {"mk": last_month}).fetchone()
    summary = {}
    if summary_row:
        sm = dict(summary_row._mapping)
        summary = {"last_month": last_month, "total_with_snapshot": sm.get("total_with_snapshot", 0),
                   "total_unsatisfied": sm.get("total_unsatisfied", 0), "avg_pct": float(sm.get("avg_pct") or 0)}

    # ── Top 10 satisfăcuți ────────────────────────────────────────────────
    top_satisfied_rows = db.execute(text("""
        SELECT s.client_id, c.name, s.satisfaction_pct, s.breakdown
        FROM client_satisfaction_snapshots s
        JOIN clients c ON c.id = s.client_id
        WHERE s.month_key = :mk AND s.satisfaction_pct IS NOT NULL
          AND c.satisfaction_exclude = FALSE
        ORDER BY s.satisfaction_pct DESC LIMIT 10
    """), {"mk": last_month}).fetchall()
    top_satisfied = []
    for r in top_satisfied_rows:
        bd = _parse_bd(r._mapping["breakdown"])
        reasoning = ""
        segment = ""
        confidence = None
        red_flags = []
        if bd and isinstance(bd, dict):
            # v3 engine: iris_reasoning direct în breakdown
            reasoning = bd.get("iris_reasoning") or ""
            if not reasoning:
                # fallback v2
                ih = bd.get("iris_holistic") or {}
                reasoning = ih.get("reasoning", "")
            segment = bd.get("segment", "")
            confidence = bd.get("confidence")
            red_flags = bd.get("red_flags_active") or []
        top_satisfied.append({
            "client_id": r._mapping["client_id"],
            "name": r._mapping["name"],
            "satisfaction_pct": float(r._mapping["satisfaction_pct"] or 0),
            "reasoning": reasoning,
            "segment": segment,
            "confidence": float(confidence) if confidence is not None else None,
            "red_flags": red_flags,
        })

    # ── Distribuție pe segmente ───────────────────────────────────────────
    seg_rows = db.execute(text("""
        SELECT (breakdown->'segment') #>> '{}' AS segment, COUNT(*) AS cnt
        FROM client_satisfaction_snapshots
        WHERE month_key = :mk AND satisfaction_pct IS NOT NULL AND breakdown IS NOT NULL
          AND client_id NOT IN (SELECT id FROM clients WHERE satisfaction_exclude = TRUE)
        GROUP BY (breakdown->'segment') #>> '{}'
    """), {"mk": last_month}).fetchall()
    segment_distribution = {r._mapping["segment"]: r._mapping["cnt"] for r in seg_rows if r._mapping["segment"]}

    # ── Distribuție red flags (normalizat pe prefix înainte de ' — ') ──────
    rf_rows = db.execute(text("""
        SELECT split_part(flag_raw, ' — ', 1) AS flag_key, COUNT(*) AS cnt
        FROM (
            SELECT jsonb_array_elements_text(breakdown->'red_flags_active') AS flag_raw
            FROM client_satisfaction_snapshots
            WHERE month_key = :mk AND satisfaction_pct IS NOT NULL
              AND breakdown IS NOT NULL
              AND jsonb_array_length(breakdown->'red_flags_active') > 0
              AND client_id NOT IN (SELECT id FROM clients WHERE satisfaction_exclude = TRUE)
        ) sub
        GROUP BY flag_key ORDER BY cnt DESC
    """), {"mk": last_month}).fetchall()
    signal_distribution = {r._mapping["flag_key"]: int(r._mapping["cnt"]) for r in rf_rows if r._mapping["flag_key"]}

    # ── Distribuție interacțiuni pe buckets ───────────────────────────────
    ti_rows = db.execute(text("""
        SELECT (breakdown->>'total_interactions')::int AS ti
        FROM client_satisfaction_snapshots
        WHERE month_key = :mk AND satisfaction_pct IS NOT NULL
          AND breakdown IS NOT NULL
          AND breakdown->>'total_interactions' IS NOT NULL
          AND client_id NOT IN (SELECT id FROM clients WHERE satisfaction_exclude = TRUE)
    """), {"mk": last_month}).fetchall()
    ti_buckets = {"1-2": 0, "3-5": 0, "6-10": 0, "11+": 0}
    for r in ti_rows:
        ti = r._mapping["ti"] or 0
        if ti <= 2:
            ti_buckets["1-2"] += 1
        elif ti <= 5:
            ti_buckets["3-5"] += 1
        elif ti <= 10:
            ti_buckets["6-10"] += 1
        else:
            ti_buckets["11+"] += 1
    trend_assessment_distribution = ti_buckets

    return {
        "trend": trend,
        "unsatisfied": unsatisfied,
        "distribution": distribution,
        "movers": movers,
        "summary": summary,
        "top_satisfied": top_satisfied,
        "segment_distribution": segment_distribution,
        "signal_distribution": signal_distribution,
        "trend_assessment_distribution": trend_assessment_distribution,
    }


@router.get("/clients/export-duplicates")
def export_duplicate_contacts(db: Session = Depends(get_db)):
    """Exportă CSV cu clienți care împart aceeași adresă de email sau același număr de telefon."""
    rows = db.execute(text("""
        WITH email_map AS (
            SELECT c.id, c.name, lower(trim(e.val)) AS contact, 'email' AS tip
            FROM clients c, jsonb_array_elements_text(c.emails) AS e(val)
            WHERE jsonb_typeof(c.emails) = 'array' AND c.emails != 'null'::jsonb
              AND lower(trim(e.val)) != '' AND lower(trim(e.val)) != '-'
        ),
        phone_map AS (
            SELECT c.id, c.name, regexp_replace(trim(p.val), '[^0-9+]', '', 'g') AS contact, 'telefon' AS tip
            FROM clients c, jsonb_array_elements_text(c.phones) AS p(val)
            WHERE jsonb_typeof(c.phones) = 'array' AND c.phones != 'null'::jsonb
              AND trim(p.val) != ''
        ),
        combined AS (
            SELECT * FROM email_map
            UNION ALL
            SELECT * FROM phone_map
        ),
        duplicates AS (
            SELECT contact, tip FROM combined
            GROUP BY contact, tip HAVING COUNT(DISTINCT id) > 1
        )
        SELECT c.id, c.name, c.contact, c.tip
        FROM combined c
        JOIN duplicates d ON d.contact = c.contact AND d.tip = c.tip
        ORDER BY c.tip, c.contact, c.id
    """)).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Tip duplicat", "Contact comun", "ID client", "Nume client"])
    for r in rows:
        writer.writerow([r._mapping["tip"], r._mapping["contact"], r._mapping["id"], r._mapping["name"]])

    output.seek(0)
    filename = f"clienti_duplicate_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/clients/{client_id}")
def get_client(client_id: int, db: Session = Depends(get_db)):
    row = db.execute(text("""
        SELECT id, iris_client_id, name, emails, phones, is_active,
               last_synced_at, satisfaction_exclude, email_priority,
               feedback_opt_out,
               (SELECT s.satisfaction_pct FROM client_satisfaction_snapshots s
                WHERE s.client_id = c.id AND s.satisfaction_pct IS NOT NULL
                ORDER BY s.month_key DESC LIMIT 1) AS satisfaction_pct,
               (SELECT s.breakdown FROM client_satisfaction_snapshots s
                WHERE s.client_id = c.id AND s.satisfaction_pct IS NOT NULL
                ORDER BY s.month_key DESC LIMIT 1) AS satisfaction_breakdown,
               (SELECT COUNT(*) FROM emails e WHERE e.client_id = c.id)
               + (SELECT COUNT(*) FROM (
                    SELECT e2.id FROM cts_ground_truth g
                    JOIN emails e2 ON e2.id = g.email_id
                    WHERE e2.client_id IS NULL
                      AND COALESCE(g.cts_direction,'received') = 'received'
                      AND c.iris_client_id IS NOT NULL
                      AND g.raw->'extra'->>'client_id' = c.iris_client_id::text
                  ) orp
               ) AS email_count,
               (SELECT COUNT(*) FROM cts_ground_truth g
                  WHERE g.cts_direction = 'sent'
                    AND c.iris_client_id IS NOT NULL
                    AND g.raw->'extra'->>'client_id' = c.iris_client_id::text
               ) AS sent_count,
               (SELECT COUNT(*) FROM client_vehicles v WHERE v.client_id = c.id) AS vehicle_count,
               (SELECT COUNT(*) FROM client_contracts ct WHERE ct.client_id = c.id) AS contract_count,
               (SELECT COUNT(*) FROM calls cl WHERE cl.client_id = c.id) AS call_count,
               (SELECT COUNT(*) FROM cts_task_ground_truth t WHERE t.client_id = c.iris_client_id) AS task_count
        FROM clients c WHERE c.id = :cid AND c.is_active = true
    """), {"cid": client_id}).fetchone()
    if not row:
        raise HTTPException(404, "Client negăsit")

    cats = db.execute(text("""
        SELECT ai_category, COUNT(*) AS n
        FROM emails WHERE client_id = :cid AND ai_category IS NOT NULL
        GROUP BY ai_category
    """), {"cid": client_id}).fetchall()

    cat_counts = {r._mapping["ai_category"]: r._mapping["n"] for r in cats}

    d = dict(row._mapping)
    # breakdown poate fi dict (jsonb) sau string — normalizăm
    bd = d.get("satisfaction_breakdown")
    if isinstance(bd, str):
        try:
            bd = json.loads(bd)
        except Exception:
            bd = None
    d["satisfaction_breakdown"] = bd

    # emails_analyzed = suma data_points din breakdown (pentru compatibilitate UI)
    emails_analyzed = 0
    if bd and isinstance(bd, dict):
        emails_analyzed = sum(
            v.get("data_points", 0) for v in bd.values() if isinstance(v, dict)
        )
    d["emails_analyzed"] = emails_analyzed

    return {
        **d,
        "cat_counts": cat_counts,
        "cat_total": sum(cat_counts.values()),
    }


@router.get("/clients/{client_id}/emails")
def client_emails(client_id: int, db: Session = Depends(get_db), page: int = 1):
    """Conversatia COMPLETA cu clientul: emailurile PRIMITE (tabela emails, client_id) UNION
    reply-urile TRIMISE de operatori (cts_ground_truth, cts_direction='sent', legate pe
    raw.extra.client_id = clients.iris_client_id). Sortat cronologic descrescator. Trimisele
    poarta direction='sent' + campurile pe care le asteapta modalul CtsSentModal."""
    per_page = 40
    offset = (page - 1) * per_page

    irow = db.execute(text("SELECT iris_client_id FROM clients WHERE id = :cid"),
                      {"cid": client_id}).fetchone()
    iris_cid = irow[0] if irow else None

    base = """
        SELECT e.id::text AS id, e.subject, e.received_at AS ts,
               e.from_address, e.from_name, e.status,
               e.ai_category, e.ai_department, e.ai_priority,
               'received' AS direction,
               NULL::text AS cts_email_log_id, NULL::text AS cts_from_email,
               NULL::text AS cts_to_email, NULL::text AS cts_status,
               NULL::timestamptz AS cts_reply_at, NULL::timestamptz AS cts_date
        FROM emails e WHERE e.client_id = :cid
        UNION ALL
        SELECT e.id::text AS id, e.subject, e.received_at AS ts,
               e.from_address, e.from_name, e.status,
               g.cts_category AS ai_category, g.cts_department AS ai_department,
               e.ai_priority,
               'received' AS direction,
               NULL::text AS cts_email_log_id, NULL::text AS cts_from_email,
               NULL::text AS cts_to_email, NULL::text AS cts_status,
               NULL::timestamptz AS cts_reply_at, NULL::timestamptz AS cts_date
        FROM cts_ground_truth g
        JOIN emails e ON e.id = g.email_id
        WHERE e.client_id IS NULL
          AND COALESCE(g.cts_direction, 'received') = 'received'
          AND :iris_cid IS NOT NULL
          AND g.raw->'extra'->>'client_id' ~ '^[0-9]+$'
          AND (g.raw->'extra'->>'client_id')::bigint = :iris_cid
        UNION ALL
        SELECT 'gt_' || g.id::text AS id,
               COALESCE(NULLIF(g.raw->'extra'->>'title',''), '(reply)') AS subject,
               COALESCE(g.cts_reply_at,
                        CAST(NULLIF(g.raw->'extra'->>'email_date','') AS timestamptz),
                        g.cts_solved_at,
                        CAST(NULLIF(g.raw->'extra'->>'created_at','') AS timestamptz),
                        g.fetched_at) AS ts,
               g.raw->'extra'->>'from_email' AS from_address,
               NULL AS from_name, g.cts_status AS status,
               g.cts_category AS ai_category, g.cts_department AS ai_department,
               NULL AS ai_priority, 'sent' AS direction,
               g.raw->'extra'->>'cts_email_log_id' AS cts_email_log_id,
               g.raw->'extra'->>'from_email' AS cts_from_email,
               g.raw->'extra'->>'to_email' AS cts_to_email,
               g.cts_status AS cts_status,
               g.cts_reply_at AS cts_reply_at,
               CAST(NULLIF(g.raw->'extra'->>'email_date','') AS timestamptz) AS cts_date
        FROM cts_ground_truth g
        WHERE g.cts_direction = 'sent'
          AND :iris_cid IS NOT NULL
          AND g.raw->'extra'->>'client_id' ~ '^[0-9]+$'
          AND (g.raw->'extra'->>'client_id')::bigint = :iris_cid
    """

    rows = db.execute(text(f"""
        SELECT * FROM ({base}) conv
        ORDER BY ts DESC NULLS LAST
        LIMIT :lim OFFSET :off
    """), {"cid": client_id, "iris_cid": iris_cid,
           "lim": per_page, "off": offset}).fetchall()

    total = db.execute(text(f"SELECT COUNT(*) FROM ({base}) conv"),
                       {"cid": client_id, "iris_cid": iris_cid}).scalar()

    items = []
    for r in rows:
        d = dict(r._mapping)
        d["received_at"] = d.pop("ts")   # UI foloseste received_at pt afisare/sortare
        items.append(d)

    total_pages = max(1, (total + per_page - 1) // per_page)

    return {
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "items": items,
    }


@router.get("/clients/{client_id}/vehicles")
def client_vehicles(client_id: int, db: Session = Depends(get_db)):
    """OPS-0124: vehiculele clientului sincronizate din CTS (via IRIS). Gol pana
    cand feed-ul /clients/contact-list include vehicles[]."""
    rows = db.execute(text("""
        SELECT id, plate, vin, status, documents, synced_at
        FROM client_vehicles WHERE client_id = :cid
        ORDER BY lower(COALESCE(plate, ''))
    """), {"cid": client_id}).fetchall()
    return {"items": [dict(r._mapping) for r in rows], "total": len(rows)}


@router.get("/clients/{client_id}/contracts")
def client_contracts(client_id: int, db: Session = Depends(get_db)):
    """OPS-0124: contractele clientului sincronizate din CTS (via IRIS). Gol pana
    cand feed-ul /clients/contact-list include contracts[]."""
    rows = db.execute(text("""
        SELECT id, iris_contract_id, contract_type, category, start_date, end_date,
               status, documents, vehicles, synced_at
        FROM client_contracts WHERE client_id = :cid
        ORDER BY end_date DESC NULLS LAST, start_date DESC NULLS LAST
    """), {"cid": client_id}).fetchall()
    return {"items": [dict(r._mapping) for r in rows], "total": len(rows)}


@router.get("/clients/{client_id}/calls")
def client_calls(client_id: int, db: Session = Depends(get_db), page: int = 1):
    """T4: Apelurile clientului, paginate, ordonate descrescător."""
    per_page = 40
    offset = (page - 1) * per_page
    rows = db.execute(text("""
        SELECT id, call_id, direction, caller_number, callee_number, agent_extension,
               started_at, duration_seconds, ai_category, ai_tone, ai_department,
               ai_priority, call_status, transcript_status
        FROM calls
        WHERE client_id = :cid
        ORDER BY started_at DESC NULLS LAST
        LIMIT :lim OFFSET :off
    """), {"cid": client_id, "lim": per_page, "off": offset}).fetchall()
    total = db.execute(
        text("SELECT COUNT(*) FROM calls WHERE client_id = :cid"), {"cid": client_id}
    ).scalar() or 0
    total_pages = max(1, (total + per_page - 1) // per_page)
    return {"total": total, "page": page, "total_pages": total_pages,
            "items": [dict(r._mapping) for r in rows]}


@router.get("/clients/{client_id}/tasks")
def client_tasks(client_id: int, db: Session = Depends(get_db), page: int = 1):
    """T4: Task-urile CTS ale clientului (via iris_client_id), paginate."""
    irow = db.execute(
        text("SELECT iris_client_id FROM clients WHERE id = :cid AND is_active = true"),
        {"cid": client_id},
    ).fetchone()
    if not irow:
        raise HTTPException(404, "Client negăsit")
    iris_cid = irow[0]
    if not iris_cid:
        return {"total": 0, "page": 1, "total_pages": 1, "items": []}

    per_page = 40
    offset = (page - 1) * per_page
    rows = db.execute(text("""
        SELECT id, iris_task_id, title, task_type, status, priority,
               department, description, assignee_raw,
               cts_created_at, cts_updated_at, source
        FROM cts_task_ground_truth
        WHERE client_id = :iris_cid
        ORDER BY cts_created_at DESC NULLS LAST
        LIMIT :lim OFFSET :off
    """), {"iris_cid": iris_cid, "lim": per_page, "off": offset}).fetchall()
    total = db.execute(
        text("SELECT COUNT(*) FROM cts_task_ground_truth WHERE client_id = :iris_cid"),
        {"iris_cid": iris_cid},
    ).scalar() or 0
    total_pages = max(1, (total + per_page - 1) // per_page)
    return {"total": total, "page": page, "total_pages": total_pages,
            "items": [dict(r._mapping) for r in rows]}


@router.get("/clients/{client_id}/satisfaction-history")
def client_satisfaction_history(client_id: int, db: Session = Depends(get_db)):
    """T4: Istoricul lunar al gradului de satisfacție (din snapshot-uri)."""
    rows = db.execute(text("""
        SELECT month_key, satisfaction_pct, is_unsatisfied, carry_forward,
               breakdown, source_month_key, computed_at
        FROM client_satisfaction_snapshots
        WHERE client_id = :cid AND satisfaction_pct IS NOT NULL
        ORDER BY month_key DESC
        LIMIT 24
    """), {"cid": client_id}).fetchall()
    items = []
    for r in rows:
        d = dict(r._mapping)
        bd = d.get("breakdown")
        if isinstance(bd, str):
            try:
                bd = json.loads(bd)
            except Exception:
                bd = None
        d["breakdown"] = bd
        d["satisfaction_pct"] = float(d["satisfaction_pct"]) if d["satisfaction_pct"] is not None else None
        items.append(d)
    return {"items": items, "total": len(items)}


@router.post("/clients/{client_id}/estimate-satisfaction")
def estimate_satisfaction(client_id: int, month: str = None, db: Session = Depends(get_db)):
    """Motor V6 — traiectorie IRIS pe săptămâni înlănțuite, scor lunar = starea finală a
    ultimei săptămâni scorate. Param opțional `month=YYYY-MM` (default: luna curentă)."""
    row = db.execute(text("""
        SELECT id, iris_client_id, name FROM clients WHERE id = :cid AND is_active = true
    """), {"cid": client_id}).fetchone()
    if not row:
        raise HTTPException(404, "Client negăsit")

    iris_client_id = row._mapping.get("iris_client_id")
    # Fereastra lunii: parametrul `month` sau luna curentă
    now = datetime.now(timezone.utc)
    month_key = month or now.strftime("%Y-%m")
    try:
        start, end = satisfaction_snapshot._month_interval(month_key)
    except Exception:
        raise HTTPException(400, "Parametru month invalid (format YYYY-MM)")

    s = get_settings()
    conn = psycopg2.connect(
        host=s.db_host, port=s.db_port,
        dbname=s.db_name, user=s.db_user, password=s.db_password,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    try:
        cur = conn.cursor()
        result = satisfaction_engine.compute_satisfaction_v6(client_id, iris_client_id, cur, start, end)
    finally:
        conn.close()

    pct = result.get("satisfaction_pct")
    if result.get("error") == "excluded":
        raise HTTPException(400, "Clientul este exclus din calculul de satisfacție (partener/furnizor/emailuri automate).")
    if pct is None:
        raise HTTPException(400, "Date insuficiente pentru calculul satisfacției (nicio activitate înregistrată)")

    breakdown = result.get("breakdown", {})
    is_unsatisfied = result.get("is_unsatisfied", pct < 70.0)

    # Sursă unică: scrie direct în client_satisfaction_snapshots (aceeași tabelă citită
    # de sidebar și de dashboard-ul de evoluție) — altfel cele două UI-uri divergeau.
    db.execute(text("""
        INSERT INTO client_satisfaction_snapshots
            (client_id, month_key, satisfaction_pct, is_unsatisfied, breakdown,
             carry_forward, config_used, computed_at)
        VALUES
            (:cid, :mk, :pct, :unsat, :breakdown, FALSE, :config, :computed_at)
        ON CONFLICT (client_id, month_key) DO UPDATE SET
            satisfaction_pct = EXCLUDED.satisfaction_pct,
            is_unsatisfied   = EXCLUDED.is_unsatisfied,
            breakdown        = EXCLUDED.breakdown,
            carry_forward    = EXCLUDED.carry_forward,
            config_used      = EXCLUDED.config_used,
            computed_at      = EXCLUDED.computed_at
    """), {
        "cid": client_id,
        "mk": month_key,
        "pct": pct,
        "unsat": is_unsatisfied,
        "breakdown": json.dumps(breakdown),
        "config": json.dumps(result.get("config_used", {})),
        "computed_at": result.get("computed_at"),
    })
    db.commit()

    total_interactions = breakdown.get("total_interactions", 0)
    return {
        "satisfaction_pct": pct,
        "is_unsatisfied": is_unsatisfied,
        "breakdown": breakdown,
        "config_used": result.get("config_used", {}),
        "computed_at": result.get("computed_at"),
        "emails_analyzed": total_interactions,
        "iris_holistic_applied": str(breakdown.get("scoring_mode") or "").startswith("v6_trajectory"),
    }


def _strip_html_to_text(html: str) -> str:
    """Converteste HTML email (inclusiv Outlook MSO) in text plain lizibil."""
    if not html:
        return ""
    s = html
    # 1. Scoate conditional comments Outlook
    s = re.sub(r"<!--\[if[^\]]*\]>.*?<!\[endif\]-->", " ", s, flags=re.DOTALL | re.IGNORECASE)
    # 2. Scoate <style> si <script>
    s = re.sub(r"<(style|script)[^>]*>.*?</(style|script)>", " ", s, flags=re.DOTALL | re.IGNORECASE)
    # 3. Sterge ORICE date: base64 (imagini, fonturi, etc.) - INAINTE de strip taguri
    #    Pattern permisiv: data:<tip>;base64,<caractere base64 inclusiv whitespace>
    s = re.sub(r"data:[a-zA-Z0-9][a-zA-Z0-9!#$&\-^_]*(?:/[a-zA-Z0-9][a-zA-Z0-9!#$&\-^_]*)?(?:;[^,]*)?;base64,[A-Za-z0-9+/=\s]+", "[date-binare]", s, flags=re.DOTALL)
    # 4. <img> -> [imagine]
    s = re.sub(r"<img[^>]*>", "[imagine]", s, flags=re.DOTALL | re.IGNORECASE)
    # 5. Paragrafe / headings / li / br -> newline
    s = re.sub(r"<br\s*/?>|</(p|div|tr|td|li|h[1-6]|blockquote)>", "\n", s, flags=re.IGNORECASE)
    # 6. Scoate toate tagurile HTML ramase
    s = re.sub(r"<[^>]+>", " ", s)
    # 7. Decodifica entitati HTML
    for ent, char in [("&amp;","&"),("&lt;","<"),("&gt;",">"),("&nbsp;"," "),
                      ("&quot;",'"'),("&#39;","'"),("&apos;","'"),("&#160;"," "),
                      ("&mdash;","\u2014"),("&ndash;","\u2013"),("&laquo;","\u00ab"),("&raquo;","\u00bb")]:
        s = s.replace(ent, char)
    # 8. Curata whitespace: max o linie goala consecutiva
    lines = [l.rstrip() for l in s.splitlines()]
    out, prev_blank = [], False
    for l in lines:
        is_blank = not l.strip()
        if is_blank and prev_blank:
            continue
        out.append(l)
        prev_blank = is_blank
    return "\n".join(out).strip()


@router.get("/clients/{client_id}/export-conversation")
def export_conversation(
    client_id: int,
    date_from: str = Query(..., description="YYYY-MM-DD"),
    date_to: str = Query(..., description="YYYY-MM-DD"),
    db: Session = Depends(get_db),
):
    """Exporta conversatia unui client pe un interval: mailuri (primite+trimise) + apeluri,
    ordonate cronologic, cu body_text/transcript incluse."""
    client_row = db.execute(
        text("SELECT id, iris_client_id, name, emails, phones FROM clients WHERE id = :cid AND is_active = true"),
        {"cid": client_id},
    ).fetchone()
    if not client_row:
        raise HTTPException(404, "Client negasit")

    client = dict(client_row._mapping)
    iris_cid = client.get("iris_client_id")

    try:
        dt_from = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        dt_to_end = datetime.strptime(date_to, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc
        )
    except ValueError:
        raise HTTPException(400, "Format data invalid. Folositi YYYY-MM-DD.")

    email_rows = db.execute(text("""
        SELECT e.id::text AS id, e.subject, e.received_at AS ts,
               e.from_address, e.from_name, e.status, e.ai_category,
               e.body_text, 'received' AS direction
        FROM emails e
        WHERE e.client_id = :cid
          AND e.received_at >= :dt_from AND e.received_at <= :dt_to
        ORDER BY e.received_at ASC
    """), {"cid": client_id, "dt_from": dt_from, "dt_to": dt_to_end}).fetchall()

    orphan_rows = []
    if iris_cid:
        orphan_rows = db.execute(text("""
            SELECT e.id::text AS id, e.subject, e.received_at AS ts,
                   e.from_address, e.from_name, e.status,
                   g.cts_category AS ai_category, e.body_text,
                   'received' AS direction
            FROM cts_ground_truth g
            JOIN emails e ON e.id = g.email_id
            WHERE e.client_id IS NULL
              AND COALESCE(g.cts_direction, 'received') = 'received'
              AND g.raw->'extra'->>'client_id' ~ '^[0-9]+$'
              AND (g.raw->'extra'->>'client_id')::bigint = :iris_cid
              AND e.received_at >= :dt_from AND e.received_at <= :dt_to
            ORDER BY e.received_at ASC
        """), {"iris_cid": iris_cid, "dt_from": dt_from, "dt_to": dt_to_end}).fetchall()

    sent_rows = []
    if iris_cid:
        sent_rows = db.execute(text("""
            SELECT g.id AS gt_id,
                   'gt_' || g.id::text AS id,
                   COALESCE(NULLIF(g.raw->'extra'->>'title',''), '(reply)') AS subject,
                   COALESCE(g.cts_reply_at,
                            CAST(NULLIF(g.raw->'extra'->>'email_date','') AS timestamptz),
                            g.cts_solved_at, g.fetched_at) AS ts,
                   g.raw->'extra'->>'from_email' AS from_address,
                   NULL AS from_name, g.cts_status AS status,
                   g.cts_category AS ai_category,
                   g.cts_reply_text AS body_text,
                   g.raw->'extra'->>'cts_email_log_id' AS cts_email_log_id,
                   'sent' AS direction
            FROM cts_ground_truth g
            WHERE g.cts_direction = 'sent'
              AND g.raw->'extra'->>'client_id' ~ '^[0-9]+$'
              AND (g.raw->'extra'->>'client_id')::bigint = :iris_cid
              AND COALESCE(g.cts_reply_at,
                           CAST(NULLIF(g.raw->'extra'->>'email_date','') AS timestamptz),
                           g.cts_solved_at, g.fetched_at) >= :dt_from
              AND COALESCE(g.cts_reply_at,
                           CAST(NULLIF(g.raw->'extra'->>'email_date','') AS timestamptz),
                           g.cts_solved_at, g.fetched_at) <= :dt_to
            ORDER BY ts ASC NULLS LAST
        """), {"iris_cid": iris_cid, "dt_from": dt_from, "dt_to": dt_to_end}).fetchall()

    # Fetch body pentru trimisele fara cache (batch, max 200/apel)
    if sent_rows:
        sent_list = [dict(r._mapping) for r in sent_rows]
        missing = [
            s["cts_email_log_id"] for s in sent_list
            if s.get("cts_email_log_id") and not s.get("body_text")
        ]
        fetched_bodies = {}
        if missing:
            try:
                batch = missing[:200]
                fetched_bodies = _cts_sync.fetch_email_content(batch) or {}
                # Salveaza in cache
                for log_id, content in fetched_bodies.items():
                    rt = (content or {}).get("reply_text") or ""
                    if rt:
                        db.execute(text(
                            "UPDATE cts_ground_truth SET cts_reply_text = :b "
                            "WHERE raw->'extra'->>'cts_email_log_id' = :lid "
                            "  AND cts_direction = 'sent'"
                        ), {"b": rt[:300000], "lid": str(log_id)})
                db.commit()
            except Exception:
                pass  # Fetch esuat — continuam fara corp
        # Reconstruim sent_rows ca lista de dict-uri cu body_text populat
        sent_rows = []
        for s in sent_list:
            lid = s.get("cts_email_log_id")
            body = s.get("body_text") or ""
            if not body and lid and lid in fetched_bodies:
                body = (fetched_bodies[lid] or {}).get("reply_text") or ""
            s["body_text"] = body
            sent_rows.append(s)

    call_rows = db.execute(text("""
        SELECT id::text AS id, call_id, direction, caller_number, callee_number,
               agent_extension, started_at AS ts, duration_seconds,
               ai_category, ai_tone, transcript, transcript_status, call_status
        FROM calls
        WHERE client_id = :cid
          AND started_at >= :dt_from AND started_at <= :dt_to
        ORDER BY started_at ASC
    """), {"cid": client_id, "dt_from": dt_from, "dt_to": dt_to_end}).fetchall()

    items = []
    # email_rows si orphan_rows sunt SQLAlchemy rows; sent_rows poate fi lista de dict-uri (dupa fetch)
    for r in list(email_rows) + list(orphan_rows):
        d = dict(r._mapping)
        items.append({
            "type": "email",
            "id": d["id"],
            "ts": d["ts"].isoformat() if d["ts"] else None,
            "subject": d.get("subject") or "(fara subiect)",
            "from_address": d.get("from_address") or "",
            "from_name": d.get("from_name") or "",
            "direction": d.get("direction", "received"),
            "ai_category": d.get("ai_category") or "",
            "body_text": d.get("body_text") or "",
        })
    for d in sent_rows:
        if not isinstance(d, dict):
            d = dict(d._mapping)
        ts_val = d.get("ts")
        items.append({
            "type": "email",
            "id": d["id"],
            "ts": ts_val.isoformat() if ts_val and hasattr(ts_val, "isoformat") else (str(ts_val) if ts_val else None),
            "subject": d.get("subject") or "(fara subiect)",
            "from_address": d.get("from_address") or "",
            "from_name": d.get("from_name") or "",
            "direction": "sent",
            "ai_category": d.get("ai_category") or "",
            "body_text": _strip_html_to_text(d.get("body_text") or ""),
        })

    for r in call_rows:
        d = dict(r._mapping)
        items.append({
            "type": "call",
            "id": d["id"],
            "ts": d["ts"].isoformat() if d["ts"] else None,
            "direction": d.get("direction") or "inbound",
            "caller_number": d.get("caller_number") or "",
            "callee_number": d.get("callee_number") or "",
            "duration_seconds": d.get("duration_seconds") or 0,
            "ai_category": d.get("ai_category") or "",
            "ai_tone": d.get("ai_tone") or "",
            "transcript": d.get("transcript") or "",
            "transcript_status": d.get("transcript_status") or "",
        })

    items.sort(key=lambda x: x["ts"] or "")

    return {
        "client": {
            "id": client["id"],
            "name": client["name"],
            "emails": client.get("emails") or [],
            "phones": client.get("phones") or [],
        },
        "date_from": date_from,
        "date_to": date_to,
        "total": len(items),
        "items": items,
    }


@router.post("/clients/{client_id}/satisfaction-exclude")
def toggle_satisfaction_exclude(client_id: int, exclude: bool, db: Session = Depends(get_db)):
    """Marchează sau demarchează un client ca exclus din calculul de satisfacție (parteneri, furnizori)."""
    row = db.execute(text("SELECT id FROM clients WHERE id = :cid"), {"cid": client_id}).fetchone()
    if not row:
        raise HTTPException(404, "Client negăsit")
    db.execute(text("UPDATE clients SET satisfaction_exclude = :ex WHERE id = :cid"),
               {"ex": exclude, "cid": client_id})
    db.commit()
    return {"client_id": client_id, "satisfaction_exclude": exclude}


