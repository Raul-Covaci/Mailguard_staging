"""Endpoints modul Apeluri (While1) — mirror emails.py (listă/detaliu/audio/sync)."""
import os
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.api.v1.auth import get_current_admin
from app.api.v1.sorting import sort_dir
from app.services import call_audio
# Atribuirea apel -> angajat (3 trepte: mapare invatata din CTS, potrivire pe nume, assignee CTS)
# e definita o singura data, in modulul de productivitate. O reimplementare aici ar diverge de
# cifrele din Productivitate exact pe cazurile grele (nume scrise altfel de While1).
from app.services import productivity as P
from app.services.productivity import _APEL_AGENT_CTE, _APEL_AGENT_JOIN_LEFT

logger = logging.getLogger("mailguard.calls")
router = APIRouter()

# Statusurile de procesare, exact cum le calculeaza UI-ul in callStatusLabel() — filtrul din
# tabel trebuie sa se potriveasca pe coloana afisata, altfel "Status: Procesat" ar da alt set.
_STATUS_SQL = {
    "procesat": "c.audio_status = 'downloaded' AND c.transcript_status = 'success' AND c.ai_category IS NOT NULL",
    "neclasificat": "c.ai_category IS NULL",
    "fara_inregistrare": "c.audio_status = 'no_recording'",
    "in_asteptare": "c.audio_status = 'pending' OR c.transcript_status = 'pending'",
    "eroare": "c.audio_status = 'error' OR c.transcript_status = 'error'",
    # call_status vine din While1: ANSWERED / NO ANSWER / BUSY / FAILED. Apelurile fara raspuns
    # sint apeluri reale primite (intra in volum), de-aia merita filtru propriu.
    "raspuns": "c.call_status = 'ANSWERED'",
    "fara_raspuns": "c.call_status IN ('NO ANSWER', 'BUSY', 'FAILED')",
}


@router.get("/calls/agents")
def list_call_agents(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Lista distinctă de agenți (agent_extension) pentru selectul de filtru."""
    rows = db.execute(text(
        "SELECT DISTINCT agent_extension FROM calls "
        "WHERE agent_extension IS NOT NULL AND agent_extension != '' ORDER BY agent_extension"
    )).fetchall()
    return {"agents": [r[0] for r in rows]}


@router.get("/calls")
def list_calls(
    direction: Optional[str] = None,
    ai_category: Optional[str] = None,
    client_id: Optional[int] = None,
    agent: Optional[str] = None,
    department: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    q: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    dir: str = Query("desc", description="ordonare dupa started_at: 'asc' | 'desc'"),
    include_legs: int = Query(0, description="1 = arata si leg-urile duplicate de centrala (debug)"),
    db: Session = Depends(get_db),
):
    where = ["1=1"]
    params = {}
    # Centrala logheaza un CDR per canal apelat, deci acelasi apel fizic apare de doua ori: leg-ul
    # 'NO ANSWER' de 0s si cel raspuns. Lista ascunde primul cand exista sibling raspuns in +/-15
    # min (regula unica, din productivity: apel_no_dup_leg_sql). Apelurile pierdute REALE ramin.
    if not include_legs:
        where.append(P.apel_no_dup_leg_sql("c"))
    if direction:
        where.append("c.direction = :dir"); params["dir"] = direction
    if ai_category:
        if ai_category == "__none__":
            where.append("c.ai_category IS NULL")
        else:
            where.append("c.ai_category = :cat"); params["cat"] = ai_category
    if client_id:
        where.append("c.client_id = :cid"); params["cid"] = client_id
    if agent and agent.strip():
        where.append("c.agent_extension = :agent"); params["agent"] = agent.strip()
    if department and department.strip():
        # Departamentul operatorului care a raspuns, prin aceeasi atribuire ca Productivitatea.
        where.append("edm.department = :department"); params["department"] = department.strip()
    if status and status.strip():
        cond = _STATUS_SQL.get(status.strip().lower())
        if cond is None:
            raise HTTPException(400, "status invalid: " + ", ".join(sorted(_STATUS_SQL)))
        where.append("(" + cond + ")")
    if date_from and date_from.strip():
        where.append("c.started_at >= CAST(:date_from AS date)"); params["date_from"] = date_from.strip()
    if date_to and date_to.strip():
        where.append("c.started_at < (CAST(:date_to AS date) + INTERVAL '1 day')"); params["date_to"] = date_to.strip()
    if q and q.strip():
        qs = q.strip()
        params["q"] = "%" + qs + "%"
        # Coloanele se califica pe `c`: de cind lista are si JOIN-ul de atribuire pe operator,
        # `id` singur ar fi ambiguu (calls / clients / employee_department_mapping).
        clauses = ["c.caller_number ILIKE :q", "c.callee_number ILIKE :q",
                   "c.client_id IN (SELECT id FROM clients WHERE name ILIKE :q)"]
        qid = qs.lstrip("#").strip()
        if qid.isdigit():
            clauses.append("c.id = :qid"); params["qid"] = int(qid)
        where.append("(" + " OR ".join(clauses) + ")")
    where_sql = " AND ".join(where)

    # Atribuirea pe operator intra in TOATE interogarile, nu doar cind se filtreaza pe departament:
    # asa lista arata cine a raspuns (numele nostru, nu cum il scrie While1) fara a doua cerere.
    # Cost masurat local: 0.16s pe 24k apeluri, cu numaratoare cu tot.
    sql = f"""
        {_APEL_AGENT_CTE}
        SELECT c.id, c.call_id, c.direction, c.caller_number, c.callee_number,
               c.started_at, c.duration_seconds, c.audio_status, c.transcript_status,
               c.ai_category, c.ai_result, c.ai_priority, c.ai_assignee,
               c.client_id, cl.name AS client_name, c.queue_status, c.call_status,
               c.ring_seconds, c.agent_extension,
               edm.name AS operator_name, edm.department AS operator_department
        FROM calls c
        {_APEL_AGENT_JOIN_LEFT}
        LEFT JOIN clients cl ON cl.id = c.client_id
        WHERE {where_sql}
        ORDER BY c.started_at {sort_dir(dir)} NULLS LAST, c.id {sort_dir(dir)}
        LIMIT :limit OFFSET :offset
    """
    params["limit"] = limit
    params["offset"] = (page - 1) * limit
    rows = db.execute(text(sql), params).fetchall()
    total = db.execute(text(f"""
        {_APEL_AGENT_CTE}
        SELECT COUNT(*) FROM calls c {_APEL_AGENT_JOIN_LEFT} WHERE {where_sql}
    """), {k: v for k, v in params.items() if k not in ("limit", "offset")}).scalar()
    items = [dict(r._mapping) for r in rows]
    return {"page": page, "limit": limit, "total": total, "items": items}


@router.get("/calls/ai-classification")
def call_ai_classification_get(admin=Depends(get_current_admin)):
    """Starea switch-ului de clasificare AI (categorie) pentru apeluri."""
    from app.services import call_classifier
    return {"ok": True, "enabled": call_classifier.call_ai_classification_status()}


@router.post("/calls/ai-classification/toggle")
def call_ai_classification_toggle(body: dict = None, admin=Depends(get_current_admin)):
    """START/STOP clasificarea AI (categorie) pentru apeluri (runtime, fara restart)."""
    from app.services import call_classifier
    body = body or {}
    enabled = bool(body.get("enabled"))
    by = admin.get("username") or admin.get("email") or "admin"
    call_classifier.set_call_ai_classification(enabled, by=by)
    return {"ok": True, "enabled": enabled,
            "message": ("Clasificare AI pornita - apelurile noi se incadreaza in categorie."
                        if enabled else
                        "Clasificare AI oprita - apelurile noi NU se mai incadreaza (fara cost AI).")}


@router.get("/calls/diarize")
def call_diarize_get(admin=Depends(get_current_admin)):
    """Starea switch-ului de diarizare automată AGENT/CLIENT."""
    from app.services import call_classifier
    return {"ok": True, "enabled": call_classifier.call_diarize_status()}


@router.post("/calls/diarize/toggle")
def call_diarize_toggle(body: dict = None, admin=Depends(get_current_admin)):
    """START/STOP diarizarea automată AGENT/CLIENT (runtime, fara restart)."""
    from app.services import call_classifier
    body = body or {}
    enabled = bool(body.get("enabled"))
    by = admin.get("username") or admin.get("email") or "admin"
    call_classifier.set_call_diarize(enabled, by=by)
    return {"ok": True, "enabled": enabled,
            "message": ("Diarizare pornita - transcriptul apare segmentat AGENT/CLIENT (consuma AI)."
                        if enabled else
                        "Diarizare oprita - transcriptul apare ca text plat, fara segmentare (fara cost AI).")}


@router.get("/calls/{call_id}")
def get_call(call_id: int, db: Session = Depends(get_db)):
    row = db.execute(text("""
        SELECT c.*, cl.name AS client_name
        FROM calls c LEFT JOIN clients cl ON cl.id = c.client_id
        WHERE c.id = :id
    """), {"id": call_id}).fetchone()
    if not row:
        raise HTTPException(404, "Apel inexistent")
    out = dict(row._mapping)
    # Analiza AI a apelului (scoruri, problemă/soluție, pași următori, cereri suplimentare).
    # Inclusă aici ca modalul de detaliu să nu mai facă un al doilea fetch.
    scores = db.execute(text(
        "SELECT * FROM call_ai_scores WHERE call_id = :id"
    ), {"id": call_id}).fetchone()
    out["ai_scores"] = dict(scores._mapping) if scores else None
    return out


@router.get("/calls/{call_id}/audio")
def download_call_audio(call_id: int, db: Session = Depends(get_db),
                        admin=Depends(get_current_admin)):
    row = db.execute(text("SELECT call_id, audio_path FROM calls WHERE id=:id"),
                     {"id": call_id}).fetchone()
    if not row:
        raise HTTPException(404, "Apel inexistent")
    m = row._mapping
    hp = call_audio.host_path(m["audio_path"])
    if not hp or not os.path.exists(hp):
        raise HTTPException(404, "Fișierul audio nu este disponibil pe disc")
    return FileResponse(hp, media_type="audio/mpeg",
                        filename=(m["call_id"] or "apel") + ".mp3")


@router.post("/calls/sync-now")
def calls_sync_now(limit: int = Query(200, ge=1, le=1000)):
    from app.services import while1_ingest
    return while1_ingest.sync_run(limit=limit)


@router.post("/calls/backfill-ring")
def calls_backfill_ring(date_from: str = Query(..., description="'YYYY-MM-DD' sau 'YYYY-MM-DD HH:MM:SS'"),
                        date_to: str = Query(..., description="'YYYY-MM-DD' sau 'YYYY-MM-DD HH:MM:SS'"),
                        admin=Depends(get_current_admin)):
    """Completeaza `calls.ring_seconds` pe un interval deja ingerat, re-interogand While1.

    De ce e nevoie: coloana a aparut in migrarea 20260813c, dupa ce ingestul rula de luni, iar
    cursorul incremental nu mai atinge randurile vechi. Fara timpul de raspuns, apelurile ies
    „nemasurate" in productivitate desi sint apeluri reale (pe august 2026: 922 din 2127 de
    apeluri primite si raspunse). Idempotent: completeaza doar unde e NULL, nu rescrie CDR-ul.
    """
    from app.services import while1_ingest
    def _norm(v: str) -> str:
        v = (v or "").strip()
        return v if len(v) > 10 else (v + " 00:00:00")
    return while1_ingest.backfill_ring_seconds(_norm(date_from), _norm(date_to))


@router.post("/calls/process-now")
def calls_process_now(limit: int = Query(50, ge=1, le=500)):
    from app.services import call_audio as _ca, call_transcribe, call_classifier
    res = {}
    try:
        res["audio"] = _ca.process_pending_batch(limit=limit)
    except Exception:
        logger.exception("call audio download batch failed")
    try:
        res["transcribe"] = call_transcribe.process_pending_batch(limit=limit)
    except Exception:
        logger.exception("call transcribe batch failed")
    try:
        res["classify"] = call_classifier.process_pending_batch(limit=limit)
    except Exception:
        logger.exception("call classify batch failed")
    try:
        res["diarize"] = call_classifier.process_diarize_batch(limit=limit)
    except Exception:
        logger.exception("call diarize batch failed")
    return res
