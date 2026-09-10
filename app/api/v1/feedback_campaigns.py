"""Feedback clienți — segmente & campanii (T2).

Definește segmente de clienți (pe criterii precum gradul de satisfacție) și
campanii care trag lunar un eșantion random din segment, pentru KPI-urile
selectate (T1). Excluderile globale (parteneri, clienți inactivi, frecvență
& opt-out din T3) se aplică ÎNAINTE de selecția random, ca dimensiunea
finală a eșantionului să fie deja curată.
"""
import json
import logging
import secrets
import time
from typing import Optional
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel, Field

from app.database import get_db
from app.api.v1.auth import get_current_admin
from app.api.v1.feedback_frequency import apply_frequency_rules, mark_feedback_sent
from app.services.feedback_email_sender import send_feedback_email, EmailConfigMissing
from app.services.feedback_send_guard import FeedbackSendBlocked

_TOKEN_EXPIRY_DAYS = 14
_SEND_BATCH_SIZE = 10
_SEND_BATCH_PAUSE_S = 5

logger = logging.getLogger("mailguard.feedback_campaigns")
router = APIRouter()


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class SegmentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=2000)
    satisfaction_min: Optional[float] = Field(None, ge=0, le=100)
    satisfaction_max: Optional[float] = Field(None, ge=0, le=100)
    exclude_partners: bool = True
    active_clients_only: bool = True
    active: bool = True


class SegmentUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=2000)
    satisfaction_min: Optional[float] = Field(None, ge=0, le=100)
    satisfaction_max: Optional[float] = Field(None, ge=0, le=100)
    exclude_partners: Optional[bool] = None
    active_clients_only: Optional[bool] = None
    active: Optional[bool] = None


class CampaignCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    segment_id: int
    kpi_ids: list[int] = Field(default_factory=list)
    sample_size: int = Field(20, gt=0, le=10000)
    frequency: str = Field("monthly", pattern=r"^(monthly|on_demand)$")
    day_of_month: int = Field(1, ge=1, le=28)
    active: bool = True


class CampaignUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    segment_id: Optional[int] = None
    kpi_ids: Optional[list[int]] = None
    sample_size: Optional[int] = Field(None, gt=0, le=10000)
    frequency: Optional[str] = Field(None, pattern=r"^(monthly|on_demand)$")
    day_of_month: Optional[int] = Field(None, ge=1, le=28)
    active: Optional[bool] = None


_SEGMENT_COLUMNS = """id, name, description, satisfaction_min, satisfaction_max,
                      exclude_partners, active_clients_only, active, created_at, updated_at"""

_CAMPAIGN_COLUMNS = """id, name, segment_id, kpi_ids, sample_size, frequency,
                       day_of_month, active, created_at, updated_at"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_segment_or_404(db: Session, segment_id: int) -> dict:
    row = db.execute(text(f"""
        SELECT {_SEGMENT_COLUMNS} FROM feedback_segments WHERE id = :id
    """), {"id": segment_id}).fetchone()
    if not row:
        raise HTTPException(404, "Segment not found")
    return dict(row._mapping)


def _get_campaign_or_404(db: Session, campaign_id: int) -> dict:
    row = db.execute(text(f"""
        SELECT {_CAMPAIGN_COLUMNS} FROM feedback_campaigns WHERE id = :id
    """), {"id": campaign_id}).fetchone()
    if not row:
        raise HTTPException(404, "Campaign not found")
    return dict(row._mapping)


def _segment_candidates(db: Session, segment: dict) -> list[dict]:
    """Clienți care satisfac criteriile segmentului, ÎNAINTE de excluderi globale."""
    where = ["1=1"]
    params: dict = {}
    if segment["active_clients_only"]:
        where.append("c.is_active = true")
    if segment["satisfaction_min"] is not None:
        where.append("latest_sat.satisfaction_pct >= :smin")
        params["smin"] = segment["satisfaction_min"]
    if segment["satisfaction_max"] is not None:
        where.append("latest_sat.satisfaction_pct <= :smax")
        params["smax"] = segment["satisfaction_max"]
    rows = db.execute(text(f"""
        SELECT c.id, c.name, latest_sat.satisfaction_pct, c.satisfaction_exclude, c.is_active
        FROM clients c
        LEFT JOIN LATERAL (
            SELECT s.satisfaction_pct FROM client_satisfaction_snapshots s
            WHERE s.client_id = c.id AND s.satisfaction_pct IS NOT NULL
            ORDER BY s.month_key DESC LIMIT 1
        ) latest_sat ON true
        WHERE {' AND '.join(where)}
        ORDER BY c.id
    """), params).fetchall()
    return [dict(r._mapping) for r in rows]


def _apply_global_exclusions(db: Session, candidates: list[dict], segment: dict) -> tuple[list[dict], list[dict]]:
    """Aplică excluderile globale ÎNAINTE de sampling random.

    Ordinea impusă de T2: excluderi -> random -> dimensiune X. Excludem
    parteneri/furnizori (satisfaction_exclude, per segment), apoi regulile
    globale de frecvență + opt-out (T3), transversale peste toate campaniile.
    """
    still_candidate, excluded = [], []
    for c in candidates:
        if segment["exclude_partners"] and c["satisfaction_exclude"]:
            excluded.append({**c, "excluded_reason": "partner_furnizor"})
            continue
        still_candidate.append(c)

    eligible, excluded_by_frequency = apply_frequency_rules(db, still_candidate)
    excluded.extend(excluded_by_frequency)
    return eligible, excluded


def _generate_form_tokens(db: Session, campaign_id: int, client_ids: list[int], month_key: str) -> dict[int, str]:
    """Generează un token unic, neghicibil, per (campanie, client, lună) — folosit de T4
    ca link public de feedback. Token lung, generat criptografic (nu e derivat din date
    predictibile), astfel încât să nu poată fi ghicit sau enumerat.
    """
    tokens: dict[int, str] = {}
    for client_id in client_ids:
        token = secrets.token_urlsafe(32)
        db.execute(text("""
            INSERT INTO feedback_form_tokens (token, campaign_id, client_id, month_key, expires_at)
            VALUES (:token, :cid, :client_id, :mk, now() + (:days || ' days')::interval)
        """), {"token": token, "cid": campaign_id, "client_id": client_id,
                "mk": month_key, "days": _TOKEN_EXPIRY_DAYS})
        tokens[client_id] = token
    return tokens


def _random_sample(db: Session, eligible: list[dict], sample_size: int) -> list[dict]:
    if len(eligible) <= sample_size:
        return eligible
    ids = [c["id"] for c in eligible]
    rows = db.execute(text("""
        SELECT unnest(:ids)::bigint AS id ORDER BY random() LIMIT :n
    """), {"ids": ids, "n": sample_size}).fetchall()
    picked_ids = {r._mapping["id"] for r in rows}
    return [c for c in eligible if c["id"] in picked_ids]


# ── Endpoints — segmente CRUD ─────────────────────────────────────────────────

@router.get("/feedback/segments")
def list_segments(include_inactive: bool = True, db: Session = Depends(get_db),
                   admin=Depends(get_current_admin)):
    where = "" if include_inactive else "WHERE active = true"
    rows = db.execute(text(f"""
        SELECT {_SEGMENT_COLUMNS} FROM feedback_segments {where} ORDER BY name ASC
    """)).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/feedback/segments", status_code=201)
def create_segment(body: SegmentCreate, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    row = db.execute(text(f"""
        INSERT INTO feedback_segments
            (name, description, satisfaction_min, satisfaction_max, exclude_partners, active_clients_only, active)
        VALUES (:name, :description, :satisfaction_min, :satisfaction_max, :exclude_partners, :active_clients_only, :active)
        RETURNING {_SEGMENT_COLUMNS}
    """), body.model_dump()).fetchone()
    db.commit()
    return dict(row._mapping)


@router.get("/feedback/segments/{segment_id}")
def get_segment(segment_id: int, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    return _get_segment_or_404(db, segment_id)


@router.put("/feedback/segments/{segment_id}")
def update_segment(segment_id: int, body: SegmentUpdate, db: Session = Depends(get_db),
                    admin=Depends(get_current_admin)):
    current = _get_segment_or_404(db, segment_id)
    fields = body.model_dump(exclude_unset=True)
    merged = {**current, **fields}
    db.execute(text("""
        UPDATE feedback_segments
        SET name=:name, description=:description, satisfaction_min=:satisfaction_min,
            satisfaction_max=:satisfaction_max, exclude_partners=:exclude_partners,
            active_clients_only=:active_clients_only, active=:active, updated_at=now()
        WHERE id=:id
    """), {**merged, "id": segment_id})
    db.commit()
    return _get_segment_or_404(db, segment_id)


@router.delete("/feedback/segments/{segment_id}", status_code=204)
def delete_segment(segment_id: int, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    _get_segment_or_404(db, segment_id)
    used = db.execute(text("SELECT 1 FROM feedback_campaigns WHERE segment_id = :id LIMIT 1"),
                       {"id": segment_id}).fetchone()
    if used:
        raise HTTPException(409, "Segmentul e folosit de o campanie — dezactivează-l în loc să-l ștergi")
    db.execute(text("DELETE FROM feedback_segments WHERE id = :id"), {"id": segment_id})
    db.commit()


@router.get("/feedback/segments/{segment_id}/preview")
def preview_segment(segment_id: int, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Câți clienți candidează în segment, câți rămân eligibili după excluderi globale."""
    segment = _get_segment_or_404(db, segment_id)
    candidates = _segment_candidates(db, segment)
    eligible, excluded = _apply_global_exclusions(db, candidates, segment)
    return {
        "candidates_count": len(candidates),
        "eligible_count": len(eligible),
        "excluded_count": len(excluded),
        "excluded_breakdown": _count_by_reason(excluded),
    }


def _count_by_reason(excluded: list[dict]) -> dict:
    out: dict = {}
    for e in excluded:
        out[e["excluded_reason"]] = out.get(e["excluded_reason"], 0) + 1
    return out


# ── Endpoints — campanii CRUD ─────────────────────────────────────────────────

@router.get("/feedback/campaigns")
def list_campaigns(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    rows = db.execute(text(f"""
        SELECT {_CAMPAIGN_COLUMNS} FROM feedback_campaigns ORDER BY name ASC
    """)).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/feedback/campaigns", status_code=201)
def create_campaign(body: CampaignCreate, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    _get_segment_or_404(db, body.segment_id)
    params = body.model_dump()
    params["kpi_ids"] = json.dumps(params["kpi_ids"])
    row = db.execute(text(f"""
        INSERT INTO feedback_campaigns
            (name, segment_id, kpi_ids, sample_size, frequency, day_of_month, active)
        VALUES (:name, :segment_id, CAST(:kpi_ids AS jsonb), :sample_size, :frequency, :day_of_month, :active)
        RETURNING {_CAMPAIGN_COLUMNS}
    """), params).fetchone()
    db.commit()
    return dict(row._mapping)


@router.get("/feedback/campaigns/{campaign_id}")
def get_campaign(campaign_id: int, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    return _get_campaign_or_404(db, campaign_id)


@router.put("/feedback/campaigns/{campaign_id}")
def update_campaign(campaign_id: int, body: CampaignUpdate, db: Session = Depends(get_db),
                     admin=Depends(get_current_admin)):
    current = _get_campaign_or_404(db, campaign_id)
    if body.segment_id is not None:
        _get_segment_or_404(db, body.segment_id)
    fields = body.model_dump(exclude_unset=True)
    merged = {**current, **fields}
    merged["kpi_ids"] = json.dumps(merged["kpi_ids"])
    db.execute(text("""
        UPDATE feedback_campaigns
        SET name=:name, segment_id=:segment_id, kpi_ids=CAST(:kpi_ids AS jsonb), sample_size=:sample_size,
            frequency=:frequency, day_of_month=:day_of_month, active=:active, updated_at=now()
        WHERE id=:id
    """), {**merged, "id": campaign_id})
    db.commit()
    return _get_campaign_or_404(db, campaign_id)


@router.delete("/feedback/campaigns/{campaign_id}", status_code=204)
def delete_campaign(campaign_id: int, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    _get_campaign_or_404(db, campaign_id)
    db.execute(text("DELETE FROM feedback_campaigns WHERE id = :id"), {"id": campaign_id})
    db.commit()


# ── Endpoint — rulare eșantionare ─────────────────────────────────────────────

@router.post("/feedback/campaigns/{campaign_id}/run-sample")
def run_sample(campaign_id: int, month_key: Optional[str] = None,
                db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Excluderi globale -> random sampling -> persistă eșantionul (dimensiune X).

    Idempotent per (campaign_id, month_key): dacă eșantionul lunii curente
    există deja, îl întoarce fără să genereze un altul.
    """
    campaign = _get_campaign_or_404(db, campaign_id)
    if not month_key:
        month_key = db.execute(text("SELECT to_char(now(), 'YYYY-MM')")).scalar()

    existing = db.execute(text("""
        SELECT client_id FROM feedback_campaign_samples
        WHERE campaign_id = :cid AND month_key = :mk AND excluded_reason IS NULL
    """), {"cid": campaign_id, "mk": month_key}).fetchall()
    if existing:
        existing_ids = [r._mapping["client_id"] for r in existing]
        tok_rows = db.execute(text("""
            SELECT client_id, token FROM feedback_form_tokens
            WHERE campaign_id = :cid AND month_key = :mk
        """), {"cid": campaign_id, "mk": month_key}).fetchall()
        return {
            "campaign_id": campaign_id, "month_key": month_key,
            "sample_size": len(existing), "already_generated": True,
            "client_ids": existing_ids,
            "tokens": {r._mapping["client_id"]: r._mapping["token"] for r in tok_rows},
        }

    segment = _get_segment_or_404(db, campaign["segment_id"])
    candidates = _segment_candidates(db, segment)
    eligible, excluded = _apply_global_exclusions(db, candidates, segment)
    sampled = _random_sample(db, eligible, campaign["sample_size"])
    sampled_ids = {c["id"] for c in sampled}
    not_sampled = [c for c in eligible if c["id"] not in sampled_ids]

    rows_to_insert = (
        [{"client_id": c["id"], "excluded_reason": None} for c in sampled]
        + [{"client_id": c["id"], "excluded_reason": c["excluded_reason"]} for c in excluded]
        + [{"client_id": c["id"], "excluded_reason": "not_sampled"} for c in not_sampled]
    )
    for r in rows_to_insert:
        db.execute(text("""
            INSERT INTO feedback_campaign_samples (campaign_id, client_id, month_key, excluded_reason)
            VALUES (:cid, :client_id, :mk, :excluded_reason)
        """), {"cid": campaign_id, "mk": month_key, **r})
    mark_feedback_sent(db, sorted(sampled_ids))
    tokens = _generate_form_tokens(db, campaign_id, sorted(sampled_ids), month_key)
    db.commit()

    return {
        "campaign_id": campaign_id, "month_key": month_key,
        "sample_size": len(sampled), "already_generated": False,
        "candidates_count": len(candidates), "eligible_count": len(eligible),
        "excluded_count": len(excluded),
        "client_ids": sorted(sampled_ids),
        "tokens": tokens,
    }


@router.get("/feedback/campaigns/{campaign_id}/samples")
def list_samples(campaign_id: int, month_key: Optional[str] = None,
                  db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    _get_campaign_or_404(db, campaign_id)
    where = "WHERE s.campaign_id = :cid"
    params: dict = {"cid": campaign_id}
    if month_key:
        where += " AND s.month_key = :mk"
        params["mk"] = month_key
    rows = db.execute(text(f"""
        SELECT s.id, s.client_id, c.name AS client_name, s.month_key, s.excluded_reason, s.created_at
        FROM feedback_campaign_samples s
        JOIN clients c ON c.id = s.client_id
        {where}
        ORDER BY s.month_key DESC, s.excluded_reason IS NULL DESC, c.name ASC
    """), params).fetchall()
    return [dict(r._mapping) for r in rows]


# ── Endpoint — trimitere efectivă (T5) ────────────────────────────────────────
# Trimite emailurile pentru tokenurile deja generate de run-sample (nu generează
# tokenuri noi). Eșalonat în loturi mici, ca să nu supraîncărcăm contul SMTP și
# să nu fim marcați spam. Fiecare adresă trece prin gardă (feedback_send_guard)
# înainte de send — pe staging, doar whitelist-ul de test poate primi efectiv.

def _bg_send_campaign_batch(campaign_id: int, month_key: str):
    """Rulat în BackgroundTasks (off request): trimite emailurile rămase, în
    loturi mici cu pauză între ele. Best-effort per destinatar — o eroare la un
    client (SMTP, gardă blocată) nu oprește trimiterea către restul lotului."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT t.id, t.token, cl.emails->>0 AS client_email
            FROM feedback_form_tokens t
            JOIN clients cl ON cl.id = t.client_id
            WHERE t.campaign_id = :cid AND t.month_key = :mk AND t.sent_at IS NULL
            ORDER BY t.id
        """), {"cid": campaign_id, "mk": month_key}).fetchall()
        pending = [dict(r._mapping) for r in rows]
        campaign = db.execute(text("SELECT name FROM feedback_campaigns WHERE id = :id"),
                               {"id": campaign_id}).fetchone()
        campaign_name = campaign._mapping["name"] if campaign else "Feedback"

        for i, row in enumerate(pending):
            if not row["client_email"]:
                db.execute(text("UPDATE feedback_form_tokens SET send_error = 'client fără adresă email' WHERE id = :id"),
                           {"id": row["id"]})
                db.commit()
                continue
            link = f"http://95.216.144.102:8501/f/{row['token']}"
            pixel = f"http://95.216.144.102:8501/api/v1/public/feedback/pixel/{row['token']}.gif"
            html = (
                f"<p>Bună ziua,</p>"
                f"<p>Părerea dumneavoastră ne ajută să fim mai buni. Vă rugăm completați "
                f"formularul scurt de feedback pentru campania <b>{campaign_name}</b>:</p>"
                f"<p><a href=\"{link}\">{link}</a></p>"
                f"<p>Mulțumim!</p>"
                f"<img src=\"{pixel}\" width=\"1\" height=\"1\" alt=\"\" style=\"display:none\">"
            )
            try:
                send_feedback_email(db, row["client_email"], f"Feedback — {campaign_name}", html)
                db.execute(text("UPDATE feedback_form_tokens SET sent_at = now(), send_error = NULL WHERE id = :id"),
                           {"id": row["id"]})
            except FeedbackSendBlocked as e:
                db.execute(text("UPDATE feedback_form_tokens SET send_error = :err WHERE id = :id"),
                           {"id": row["id"], "err": str(e)[:300]})
                logger.warning("Send blocat de gardă pentru token id=%s: %s", row["id"], e)
            except Exception as e:
                db.execute(text("UPDATE feedback_form_tokens SET send_error = :err WHERE id = :id"),
                           {"id": row["id"], "err": str(e)[:300]})
                logger.exception("Eroare trimitere feedback token id=%s", row["id"])
            db.commit()

            is_last = (i == len(pending) - 1)
            if not is_last and (i + 1) % _SEND_BATCH_SIZE == 0:
                time.sleep(_SEND_BATCH_PAUSE_S)
    except EmailConfigMissing as e:
        logger.warning("Trimitere campanie %s oprită: %s", campaign_id, e)
    except Exception:
        logger.exception("Eroare la trimiterea lotului pentru campania %s/%s", campaign_id, month_key)
    finally:
        db.close()


@router.post("/feedback/campaigns/{campaign_id}/send")
def send_campaign(campaign_id: int, background_tasks: BackgroundTasks, month_key: Optional[str] = None,
                   db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Declanșează trimiterea emailurilor pentru eșantionul deja generat (run-sample)
    al lunii curente. Nu re-generează tokenuri. Idempotent: doar tokenurile
    netrimise (sent_at IS NULL) sunt luate în lot — apelarea repetată nu retrimite
    cui a primit deja."""
    _get_campaign_or_404(db, campaign_id)
    if not month_key:
        month_key = db.execute(text("SELECT to_char(now(), 'YYYY-MM')")).scalar()

    pending_count = db.execute(text("""
        SELECT count(*) FROM feedback_form_tokens
        WHERE campaign_id = :cid AND month_key = :mk AND sent_at IS NULL
    """), {"cid": campaign_id, "mk": month_key}).scalar()

    if pending_count == 0:
        return {"campaign_id": campaign_id, "month_key": month_key, "queued": 0,
                "message": "Nimic de trimis — eșantionul nu există sau a fost deja trimis integral."}

    background_tasks.add_task(_bg_send_campaign_batch, campaign_id, month_key)
    return {"campaign_id": campaign_id, "month_key": month_key, "queued": pending_count,
            "message": "Trimitere pornită în fundal, eșalonat."}


@router.get("/feedback/campaigns/{campaign_id}/send-status")
def campaign_send_status(campaign_id: int, month_key: Optional[str] = None,
                          db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Stare per destinatar: trimis / deschis / a răspuns — pentru afișare în UI campanie."""
    _get_campaign_or_404(db, campaign_id)
    where = "WHERE t.campaign_id = :cid"
    params: dict = {"cid": campaign_id}
    if month_key:
        where += " AND t.month_key = :mk"
        params["mk"] = month_key
    rows = db.execute(text(f"""
        SELECT t.id, t.client_id, cl.name AS client_name, cl.emails->>0 AS client_email,
               t.month_key, t.sent_at, t.send_error, t.opened_at, t.opened_via, t.used_at,
               CASE
                   WHEN t.used_at IS NOT NULL THEN 'a_raspuns'
                   WHEN t.opened_at IS NOT NULL THEN 'deschis'
                   WHEN t.sent_at IS NOT NULL THEN 'trimis'
                   WHEN t.send_error IS NOT NULL THEN 'eroare'
                   ELSE 'in_asteptare'
               END AS status
        FROM feedback_form_tokens t
        JOIN clients cl ON cl.id = t.client_id
        {where}
        ORDER BY t.month_key DESC, cl.name ASC
    """), params).fetchall()
    return [dict(r._mapping) for r in rows]
