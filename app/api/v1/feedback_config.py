"""Feedback clienți — configurare KPI & scală (T1).

Fundația modulului „Feedback clienți": KPI-uri de rating configurabile fără cod,
cu scală proprie (1–X stele) și comentariu opțional. Reutilizat de campanii (T2),
formular (T4) și dashboard (T7).
"""
import json
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel, Field

from app.database import get_db
from app.api.v1.auth import get_current_admin
from app.services.feedback_email_sender import get_email_config, save_email_config

logger = logging.getLogger("mailguard.feedback_config")
router = APIRouter()


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class KpiCreate(BaseModel):
    key: str = Field(..., min_length=1, max_length=60, pattern=r"^[a-z0-9_-]+$")
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=2000)
    scale_max: int = Field(5, ge=2, le=10)
    comment_enabled: bool = True
    comment_label: Optional[str] = Field(None, max_length=200)
    sort_order: int = 0
    active: bool = True


class EmailConfigInput(BaseModel):
    smtp_host: str = Field(..., min_length=1, max_length=255)
    smtp_port: int = Field(587, ge=1, le=65535)
    smtp_user: str = Field(..., min_length=1, max_length=255)
    smtp_password: Optional[str] = Field(None, max_length=500)
    from_address: str = Field(..., min_length=3, max_length=255)
    use_tls: bool = True


class GoogleConfigInput(BaseModel):
    review_url: str = Field(..., min_length=10, max_length=500, pattern=r"^https://.+")
    active: bool = True


class KpiUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=2000)
    scale_max: Optional[int] = Field(None, ge=2, le=10)
    comment_enabled: Optional[bool] = None
    comment_label: Optional[str] = Field(None, max_length=200)
    sort_order: Optional[int] = None
    active: Optional[bool] = None


_KPI_COLUMNS = """id, key, name, description, scale_max, comment_enabled,
                  comment_label, sort_order, active, created_at, updated_at"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_kpi_or_404(db: Session, kpi_id: int) -> dict:
    row = db.execute(text(f"""
        SELECT {_KPI_COLUMNS} FROM feedback_kpis WHERE id = :id
    """), {"id": kpi_id}).fetchone()
    if not row:
        raise HTTPException(404, "KPI not found")
    return dict(row._mapping)


# ── Endpoints — KPI CRUD ──────────────────────────────────────────────────────

@router.get("/feedback/kpis")
def list_kpis(include_inactive: bool = True, db: Session = Depends(get_db),
               admin=Depends(get_current_admin)):
    where = "" if include_inactive else "WHERE active = true"
    rows = db.execute(text(f"""
        SELECT {_KPI_COLUMNS} FROM feedback_kpis {where}
        ORDER BY sort_order ASC, name ASC
    """)).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/feedback/kpis", status_code=201)
def create_kpi(body: KpiCreate, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    dup = db.execute(text("SELECT id FROM feedback_kpis WHERE key = :key"),
                      {"key": body.key}).fetchone()
    if dup:
        raise HTTPException(409, f"KPI cu key '{body.key}' există deja")

    row = db.execute(text(f"""
        INSERT INTO feedback_kpis
            (key, name, description, scale_max, comment_enabled, comment_label, sort_order, active)
        VALUES (:key, :name, :description, :scale_max, :comment_enabled, :comment_label, :sort_order, :active)
        RETURNING {_KPI_COLUMNS}
    """), body.model_dump()).fetchone()
    db.commit()
    return dict(row._mapping)


@router.get("/feedback/kpis/{kpi_id}")
def get_kpi(kpi_id: int, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    return _get_kpi_or_404(db, kpi_id)


@router.put("/feedback/kpis/{kpi_id}")
def update_kpi(kpi_id: int, body: KpiUpdate, db: Session = Depends(get_db),
                admin=Depends(get_current_admin)):
    current = _get_kpi_or_404(db, kpi_id)
    fields = body.model_dump(exclude_unset=True)
    merged = {**current, **fields}

    db.execute(text(f"""
        UPDATE feedback_kpis
        SET name=:name, description=:description, scale_max=:scale_max,
            comment_enabled=:comment_enabled, comment_label=:comment_label,
            sort_order=:sort_order, active=:active, updated_at=now()
        WHERE id=:id
    """), {**merged, "id": kpi_id})
    db.commit()
    return _get_kpi_or_404(db, kpi_id)


@router.delete("/feedback/kpis/{kpi_id}", status_code=204)
def delete_kpi(kpi_id: int, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    _get_kpi_or_404(db, kpi_id)  # 404 guard
    used = db.execute(text("SELECT 1 FROM feedback_kpi_ratings WHERE kpi_id = :id LIMIT 1"),
                       {"id": kpi_id}).fetchone()
    if used:
        raise HTTPException(409, "KPI are rating-uri înregistrate — dezactivează-l în loc să-l ștergi")
    db.execute(text("DELETE FROM feedback_kpis WHERE id = :id"), {"id": kpi_id})
    db.commit()


# ── Endpoint — setări implicite globale (fallback scală/comentariu) ──────────

@router.get("/feedback/defaults")
def get_defaults(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    row = db.execute(text("SELECT value FROM settings WHERE key = 'feedback.defaults'")).fetchone()
    if not row:
        return {"scale_max": 5, "comment_enabled": True, "comment_label": None}
    return row._mapping["value"]


@router.put("/feedback/defaults")
def update_defaults(body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    db.execute(text("""
        INSERT INTO settings (key, value, updated_at)
        VALUES ('feedback.defaults', to_jsonb(CAST(:v AS json)), now())
        ON CONFLICT (key) DO UPDATE SET value = to_jsonb(CAST(:v AS json)), updated_at = now()
    """), {"v": json.dumps(body)})
    db.commit()
    return body


# ── Endpoint — cont email trimitere campanii (T5) ─────────────────────────────
# Parola SMTP nu e NICIODATĂ întoarsă în clar către UI, nici la citire.

@router.get("/feedback/email-config")
def get_feedback_email_config(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    cfg = get_email_config(db)
    if not cfg:
        return {"configured": False}
    return {
        "configured": True,
        "smtp_host": cfg["smtp_host"],
        "smtp_port": cfg["smtp_port"],
        "smtp_user": cfg["smtp_user"],
        "from_address": cfg["from_address"],
        "use_tls": cfg["use_tls"],
        "smtp_password": "••••••••",
    }


@router.put("/feedback/email-config")
def update_feedback_email_config(body: EmailConfigInput, db: Session = Depends(get_db),
                                  admin=Depends(get_current_admin)):
    try:
        save_email_config(db, smtp_host=body.smtp_host, smtp_port=body.smtp_port,
                           smtp_user=body.smtp_user, from_address=body.from_address,
                           use_tls=body.use_tls, smtp_password=body.smtp_password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "configured": True,
        "smtp_host": body.smtp_host,
        "smtp_port": body.smtp_port,
        "smtp_user": body.smtp_user,
        "from_address": body.from_address,
        "use_tls": body.use_tls,
        "smtp_password": "••••••••",
    }


# ── Endpoint — link Google Reviews (T6) ───────────────────────────────────────
# Fără gating pe rating: linkul e afișat necondiționat oricărui client care
# trimite feedback, indiferent de scor. Vezi CLAUDE.md nota T6 pentru decizie.

@router.get("/feedback/google-config")
def get_feedback_google_config(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    row = db.execute(text("""
        SELECT review_url, active FROM feedback_google_config ORDER BY id LIMIT 1
    """)).fetchone()
    if not row:
        return {"configured": False, "active": False, "review_url": None}
    info = dict(row._mapping)
    return {"configured": True, "active": info["active"], "review_url": info["review_url"]}


@router.put("/feedback/google-config")
def update_feedback_google_config(body: GoogleConfigInput, db: Session = Depends(get_db),
                                   admin=Depends(get_current_admin)):
    existing = db.execute(text("SELECT id FROM feedback_google_config ORDER BY id LIMIT 1")).fetchone()
    if existing:
        db.execute(text("""
            UPDATE feedback_google_config SET review_url=:url, active=:active, updated_at=now()
            WHERE id=:id
        """), {"url": body.review_url, "active": body.active, "id": existing._mapping["id"]})
    else:
        db.execute(text("""
            INSERT INTO feedback_google_config (review_url, active) VALUES (:url, :active)
        """), {"url": body.review_url, "active": body.active})
    db.commit()
    return {"configured": True, "active": body.active, "review_url": body.review_url}
