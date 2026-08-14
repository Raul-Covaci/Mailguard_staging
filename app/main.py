"""IRIS Cargo360 — FastAPI entrypoint."""
import os
import logging
import sys
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from fastapi import Request
from app.config import get_settings
from app.api.v1 import health, emails, clients, auth, settings as settings_api, spam, ai, ai_category, ai_department, ai_priority, ai_assignee, ai_autoreply, reports, documents, cts, admin_reset, cts_training
from app.api.v1 import employees as employees_api
from app.api.v1 import personal_mailboxes as personal_mailboxes_api
from app.api.v1 import iris_dv as iris_dv_api
from app.api.v1 import cts_sync_control as cts_sync_control_api
from app.api.v1 import noreply as noreply_api
from app.api.v1 import db_export
from app.api.graph import messages as graph_messages
from app.services import access_control as _ac

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","name":"%(name)s","msg":%(message)r}',
    stream=sys.stdout,
)
logger = logging.getLogger("mailguard")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    logger.info(f"Starting {s.app_name} v{s.app_version} on port {s.app_port}")
    yield
    logger.info("Shutting down...")


settings = get_settings()
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Email proxy Office 365 ↔ CTS ADMIN cu anti-phishing și categorizare AI.",
    lifespan=lifespan,
)

app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://95.216.144.102:8501",
        "https://mailguard.cargotrack.ro",
        "http://mailguard.cargotrack.ro",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# UI/Admin API (auth via JWT for protected endpoints)
app.include_router(auth.router, prefix="/api/v1", tags=["auth"])
# /health ramine public (monitorizare); /stats/* = datele Dashboard-ului, cer acces.
app.include_router(health.router, prefix="/api/v1", tags=["health"],
                   dependencies=[Depends(_ac.require_module_for_paths(
                       "dashboard", prefixes=("/stats/",)))])
app.include_router(emails.router, prefix="/api/v1", tags=["emails"])
app.include_router(clients.router, prefix="/api/v1", tags=["clients"])
app.include_router(settings_api.router, prefix="/api/v1", tags=["settings"],
                   dependencies=[Depends(_ac.require_module("settings"))])
# /settings/employees* alimenteaza pagina Utilizatori, nu zona de Setari — router
# separat, altfel gate-ul "settings" (developer-only) ar bloca si adminii.
app.include_router(employees_api.router, prefix="/api/v1", tags=["employees"],
                   dependencies=[Depends(_ac.require_module("utilizatori"))])
app.include_router(spam.router, prefix="/api/v1", tags=["spam"])
app.include_router(ai.router, prefix="/api/v1", tags=["ai"])
# Paza pe ADMINISTRAREA prompturilor AI (regenerare, statistici, corectii globale,
# rapoarte de cost) — NU pe actiunile per-email (/ai/<x>/{id}/run|correct), care se
# fac din fisa unui email si trebuie sa ramina la indemina operatorului.
_PROMPT_ADMIN_PATHS = (
    "/corrections", "/regenerate-prompt", "/reclassify", "/backfill",
    "/reset", "/stats", "/prompt", "/ai/analytics", "/ai/cost-report",
    "/dispatch-now", "/dispatch-log", "/rejections", "/feedback",
    "/solved-sample", "/ai-enabled",
)
_PROMPT_GATE = [Depends(_ac.require_module_for_paths("prompturi-ai", _PROMPT_ADMIN_PATHS))]
app.include_router(ai_category.router, prefix="/api/v1", tags=["ai_category"], dependencies=_PROMPT_GATE)
app.include_router(ai_department.router, prefix="/api/v1", tags=["ai_department"], dependencies=_PROMPT_GATE)
app.include_router(ai_priority.router, prefix="/api/v1", tags=["ai_priority"], dependencies=_PROMPT_GATE)
app.include_router(ai_assignee.router, prefix="/api/v1", tags=["ai_assignee"], dependencies=_PROMPT_GATE)
app.include_router(ai_autoreply.router, prefix="/api/v1", tags=["ai_autoreply"], dependencies=_PROMPT_GATE)
app.include_router(reports.router, prefix="/api/v1", tags=["reports"],
                   dependencies=[Depends(_ac.require_module("reports"))])
app.include_router(documents.router, prefix="/api/v1", tags=["documents"],
                   dependencies=[Depends(_ac.require_module("documents"))])
# CTS feed (faza 1: backoffice CTS preia emailurile clean; X-CTS-Token)
app.include_router(cts.router, prefix="/api/v1", tags=["cts"])
app.include_router(cts_training.router, prefix="/api/v1", tags=["cts_training"],
                   dependencies=[Depends(_ac.require_module("cts-training"))])
app.include_router(admin_reset.router, prefix="/api/v1", tags=["admin_reset"],
                   dependencies=[Depends(_ac.require_role(_ac.ROLE_DEVELOPER))])
# Export DB pentru dezvoltare locala — doar developer (paza si in router, dubla).
app.include_router(db_export.router, prefix="/api/v1", tags=["db_export"],
                   dependencies=[Depends(_ac.require_role(_ac.ROLE_DEVELOPER))])
app.include_router(personal_mailboxes_api.router, prefix="/api/v1", tags=["personal_mailboxes"],
                   dependencies=[Depends(_ac.require_module("personal-mailboxes"))])
app.include_router(iris_dv_api.router, prefix="/api/v1", tags=["iris_dv"])
app.include_router(cts_sync_control_api.router, prefix="/api/v1", tags=["cts_sync_control"],
                   dependencies=[Depends(_ac.require_module("surse-date"))])
# No-reply auto-reply + public unsubscribe (fara prefix — /noreply/unsubscribe e public)
app.include_router(noreply_api.router, tags=["noreply"])
# Graph-compatible API for CTS ADMIN (X-Cargo360-API-Key)
app.include_router(graph_messages.router, tags=["graph"])


# Serve UI (relativ la pachet — merge local si pe server /opt/iris-mailguard)
UI_DIR = Path(__file__).resolve().parent / "ui"

_GZIP_FILES = {"mg-app.js"}


class _GzipStaticFiles(StaticFiles):
    """StaticFiles cu gzip pre-comprimat pentru fișierele mari (mg-app.js)."""

    async def get_response(self, path: str, scope):
        from starlette.requests import Request as StarletteRequest
        request = StarletteRequest(scope)
        accept = request.headers.get("accept-encoding", "")
        if "gzip" in accept and path in _GZIP_FILES:
            gz = self.directory / (path + ".gz")
            if gz.exists():
                data = gz.read_bytes()
                from starlette.responses import Response as StarletteResponse
                return StarletteResponse(
                    content=data,
                    media_type="application/javascript",
                    headers={
                        "Content-Encoding": "gzip",
                        "Cache-Control": "public, max-age=3600",
                        "Vary": "Accept-Encoding",
                    },
                )
        return await super().get_response(path, scope)


app.mount("/vendor", _GzipStaticFiles(directory=UI_DIR / "vendor"), name="vendor")


@app.get("/")
def root_ui():
    """Serve admin UI single-page app."""
    if (UI_DIR / "index.html").exists():
        return FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return {
        "service": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
        "health": "/api/v1/health",
    }


@app.get("/admin")
def admin_ui():
    return FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/f/{token}")
def feedback_form_ui(token: str):
    """Pagina publică de feedback (T4) — link tokenizat trimis clientului, fără login."""
    return FileResponse(UI_DIR / "feedback.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


# ═══════════════════════════════════════════════════════════════════════
# IRIS Health Monitoring — standard CargoFuel-identic (adăugat 2026-05-28)
# Public: /healthz, /version, /healthz/cron · X-API-Key: /admin-api/*
# ═══════════════════════════════════════════════════════════════════════
import time as _hc_time, shutil as _hc_shutil, hashlib as _hc_hashlib
from datetime import datetime as _hc_dt
from fastapi import Request as _HCRequest
from fastapi.responses import JSONResponse as _HCJSON
from sqlalchemy import text as _hc_text
from app.database import SessionLocal as _HCSession

_HC_START = _hc_time.time()
APP_HEALTH_VERSION = f"cargo360-v{settings.app_version}"


def _hc_db_ok():
    try:
        d = _HCSession(); d.execute(_hc_text("SELECT 1")); d.close(); return True
    except Exception:
        return False


def _hc_caller(request):
    key = request.headers.get("X-API-Key", "")
    if not key:
        return None
    kh = _hc_hashlib.sha256(key.encode()).hexdigest()
    try:
        d = _HCSession()
        row = d.execute(_hc_text("SELECT label FROM api_keys WHERE key_hash=:h AND is_active=true"),
                        {"h": kh}).first()
        d.close()
        return row[0] if row else None
    except Exception:
        return None


def _hc_cron():
    """Cron cargo360 = sync+process emails la 5min (systemd timer). Semnal = freshness emails.fetched_at."""
    jobs = []; overall = "ok"
    try:
        d = _HCSession()
        row = d.execute(_hc_text("SELECT MAX(fetched_at) FROM emails")).first()
        d.close()
        last = row[0] if row else None
        lag = None
        if last:
            try:
                lag = int((_hc_dt.utcnow() - last.replace(tzinfo=None)).total_seconds())
            except Exception:
                lag = None
        lagging = bool(lag is not None and lag > 3 * 3600)
        jobs.append({"name": "email_sync_process", "interval_s": 300,
                     "last_run_at": last.isoformat() if last else None,
                     "last_run_status": "ok" if last else "unknown", "lag_s": lag,
                     "is_critical": True, "is_lagging": lagging, "last_run_error": None})
        if lagging:
            overall = "critical"
    except Exception as e:
        overall = "critical"
        jobs.append({"name": "email_sync_process", "interval_s": 300, "last_run_at": None,
                     "last_run_status": "failed", "lag_s": None, "is_critical": True,
                     "is_lagging": True, "last_run_error": str(e)[:200]})
    return overall, jobs


@app.get("/healthz")
def hc_healthz():
    ok = _hc_db_ok()
    return {"status": "ok" if ok else "degraded", "db": ok, "version": APP_HEALTH_VERSION, "env": os.getenv("APP_ENV", "production")}


@app.get("/version")
def hc_version():
    return {"version": APP_HEALTH_VERSION}


@app.get("/healthz/cron")
def hc_cron_ep():
    overall, jobs = _hc_cron()
    lg = sum(1 for j in jobs if j.get("is_lagging"))
    fl = sum(1 for j in jobs if j.get("last_run_status") == "failed")
    return _HCJSON({"status": overall, "overall": overall, "jobs": jobs,
                    "lagging_count": lg, "failing_count": fl},
                   status_code=503 if overall == "critical" else 200)


@app.get("/admin-api/health-deep")
def hc_health_deep(request: _HCRequest):
    caller = _hc_caller(request)
    if not caller:
        return _HCJSON({"ok": False, "error": "X-API-Key invalid sau lipsă"}, status_code=401)
    ok = _hc_db_ok()
    try:
        du = _hc_shutil.disk_usage("/"); root_pct = int(du.used * 100 / du.total)
    except Exception:
        root_pct = None
    overall, jobs = _hc_cron()
    lg = sum(1 for j in jobs if j.get("is_lagging"))
    fl = sum(1 for j in jobs if j.get("last_run_status") == "failed")
    status = "critical" if (not ok or overall == "critical") else ("ok" if overall == "ok" else "degraded")
    return {"version": APP_HEALTH_VERSION, "now": _hc_dt.utcnow().isoformat() + "Z",
            "db": {"ok": ok}, "disk": {"root_pct": root_pct},
            "uptime_s": int(_hc_time.time() - _HC_START),
            "cron_critical": {"overall": overall, "jobs": jobs, "lagging_count": lg, "failing_count": fl},
            "status": status, "caller_api_key": caller}


@app.get("/admin-api/emails")
def hc_smoke_emails(request: _HCRequest, limit: int = 1):
    caller = _hc_caller(request)
    if not caller:
        return _HCJSON({"ok": False, "error": "X-API-Key invalid sau lipsă"}, status_code=401)
    try:
        d = _HCSession()
        rows = d.execute(_hc_text("SELECT id, received_at FROM emails ORDER BY received_at DESC NULLS LAST LIMIT :l"),
                         {"l": max(1, min(limit, 5))}).fetchall()
        d.close()
        return {"ok": True, "count": len(rows), "caller_api_key": caller,
                "sample": [{"id": r[0], "received_at": (r[1].isoformat() if r[1] else None)} for r in rows]}
    except Exception as e:
        return _HCJSON({"ok": False, "error": str(e)[:200]}, status_code=500)


# ═══════════════════════════════════════════════════════════════════════
# Modul Productivitate (mailuri) — adăugat 2026-07-01
# Înregistrare separată (append) ca să nu interfereze cu restul router-elor.
# ═══════════════════════════════════════════════════════════════════════
from app.api.v1 import productivity as _productivity_api
app.include_router(_productivity_api.router, prefix="/api/v1", tags=["productivity"])

# T5: Endpoint extern satisfacție clienți (X-API-Key, rate limit)
from app.api.v1 import satisfaction_api as _satisfaction_api
app.include_router(_satisfaction_api.router, tags=["satisfaction-ext"])

# Feed extern satisfacție GRUPAT PE CLIENT (medie + istoric lunar + nume/CUI/id IRIS).
# Separat de /ext/v1/satisfaction, care rămâne pe forma plată client×lună.
from app.api.v1 import client_satisfaction_feed as _client_satisfaction_feed
app.include_router(_client_satisfaction_feed.router, prefix="/api/v1", tags=["client-satisfaction-feed"])

# ═══════════════════════════════════════════════════════════════════════
# Modul Apeluri (While1) — 2026-07-01
# Înregistrare separată (append) ca să nu interfereze cu restul router-elor.
# ═══════════════════════════════════════════════════════════════════════
from app.api.v1 import calls as _calls_api
app.include_router(_calls_api.router, prefix='/api/v1', tags=['calls'],
                   dependencies=[Depends(_ac.require_module("apeluri"))])

# ═══════════════════════════════════════════════════════════════════════
# Modul Apeluri CTS — 2026-07-01
# Înregistrare separată (append) ca să nu interfereze cu restul router-elor.
# ═══════════════════════════════════════════════════════════════════════
from app.api.v1 import cts_calls_training as _cts_calls_training_api
app.include_router(_cts_calls_training_api.router, prefix='/api/v1', tags=['cts_calls_training'],
                   dependencies=[Depends(_ac.require_module("cts-calls-training"))])

# ═══════════════════════════════════════════════════════════════════════
# Auto-learning prompturi AI — Apeluri (regenerare din divergențe CTS) — 2026-07-02
# Înregistrare separată (append) ca să nu interfereze cu restul router-elor.
# ═══════════════════════════════════════════════════════════════════════
from app.api.v1 import ai_call_category as _ai_call_category_api
app.include_router(_ai_call_category_api.router, prefix='/api/v1', tags=['ai_call_category'])

from app.api.v1 import calls_analytics as _calls_analytics_api
app.include_router(_calls_analytics_api.router, prefix='/api/v1', tags=['calls_analytics'],
                   dependencies=[Depends(_ac.require_module("calls-analitice"))])

from app.api.v1 import calls_analyze as _calls_analyze_api
app.include_router(_calls_analyze_api.router, prefix='/api/v1', tags=['calls_analyze'],
                   dependencies=[Depends(_ac.require_module("apeluri"))])

# ═══════════════════════════════════════════════════════════════════════
# Modul Task-uri — 2026-07-02
# Înregistrare separată (append) ca să nu interfereze cu restul router-elor.
# ═══════════════════════════════════════════════════════════════════════
from app.api.v1 import cts_tasks_training as _cts_tasks_training_api
app.include_router(_cts_tasks_training_api.router, prefix='/api/v1', tags=['cts_tasks_training'],
                   dependencies=[Depends(_ac.require_module("taskuri"))])

# ═══════════════════════════════════════════════════════════════════════
# Modul Device Operations — 2026-07-02
# Inregistrare separata (append) ca sa nu interfereze cu restul router-elor.
# Sursa (endpoint IRIS /cts/device-operations) e ceruta lui Razvan -- sync-ul e
# gracios (404) pana la raspuns, vezi docs/device_operations_endpoint_request.md.
# ═══════════════════════════════════════════════════════════════════════
from app.api.v1 import device_ops as _device_ops_api
app.include_router(_device_ops_api.router, prefix="/api/v1", tags=["device_ops"],
                   dependencies=[Depends(_ac.require_module("device-ops"))])

# ═══════════════════════════════════════════════════════════════════════
# Modul Reclamatii / Quality Evaluation — 2026-08-14
# Lista peste `cts_quality_evaluation` (oglinda view-ului IRIS DV, vezi quality_eval_sync).
# Aceleasi randuri alimenteaza Monitorul si productivitatea Suport 3.
# ═══════════════════════════════════════════════════════════════════════
from app.api.v1 import quality_eval as _quality_eval_api
app.include_router(_quality_eval_api.router, prefix="/api/v1", tags=["quality_eval"],
                   dependencies=[Depends(_ac.require_module("quality-eval"))])

# ═══════════════════════════════════════════════════════════════════════
# Program departamente (SLA in program de lucru, nu 24/7) — 2026-07-02
# Inregistrare separata (append) ca sa nu interfereze cu restul router-elor.
# ═══════════════════════════════════════════════════════════════════════
from app.api.v1 import department_schedule as _department_schedule_api
app.include_router(_department_schedule_api.router, prefix="/api/v1", tags=["department_schedule"],
                   dependencies=[Depends(_ac.require_module("utilizatori"))])

# ═══════════════════════════════════════════════════════════════════════
# Feedback clienți — configurare KPI & scală (T1) — 2026-07-16
# ═══════════════════════════════════════════════════════════════════════
from app.api.v1 import feedback_config as _feedback_config_api
app.include_router(_feedback_config_api.router, prefix="/api/v1", tags=["feedback_config"],
                   dependencies=[Depends(_ac.require_module("feedback-config"))])

# ═══════════════════════════════════════════════════════════════════════
# Feedback clienți — segmente & campanii (T2) — 2026-07-16
# ═══════════════════════════════════════════════════════════════════════
from app.api.v1 import feedback_campaigns as _feedback_campaigns_api
app.include_router(_feedback_campaigns_api.router, prefix="/api/v1", tags=["feedback_campaigns"],
                   dependencies=[Depends(_ac.require_module("feedback-campaigns"))])

# ═══════════════════════════════════════════════════════════════════════
# Feedback clienți — reguli de frecvență & opt-out (T3) — 2026-07-16
# ═══════════════════════════════════════════════════════════════════════
from app.api.v1 import feedback_frequency as _feedback_frequency_api
app.include_router(_feedback_frequency_api.router, prefix="/api/v1", tags=["feedback_frequency"],
                   dependencies=[Depends(_ac.require_module("feedback-config"))])

# ═══════════════════════════════════════════════════════════════════════
# Feedback clienți — formular public, link tokenizat (T4) — 2026-07-16
# ═══════════════════════════════════════════════════════════════════════
from app.api.v1 import feedback_public as _feedback_public_api
app.include_router(_feedback_public_api.router, prefix="/api/v1", tags=["feedback_public"])

# ═══════════════════════════════════════════════════════════════════════
# Feedback clienți — dashboard statistici (T7) — 2026-07-17
# ═══════════════════════════════════════════════════════════════════════
from app.api.v1 import feedback_dashboard as _feedback_dashboard_api
app.include_router(_feedback_dashboard_api.router, prefix="/api/v1", tags=["feedback_dashboard"],
                   dependencies=[Depends(_ac.require_module("feedback-dashboard"))])

# ═══════════════════════════════════════════════════════════════════════
# No-reply auto-reply — config SMTP, toggle, template, blacklist, unsubscribe
# ═══════════════════════════════════════════════════════════════════════
from app.api.v1 import noreply as _noreply_api
app.include_router(_noreply_api.router, tags=["noreply"])
