"""v0.6.0 — Settings / rule catalog + backups API.

GET  /api/v1/settings/rules                  → admin catalog of phishing detection rules.
GET  /api/v1/settings/backups                → admin: full list of code backups + freshness.
POST /api/v1/settings/backups/run-now        → admin: force-create a backup now.
POST /api/v1/settings/backups/{name}/restore → admin: restore code from an archive.
GET  /api/v1/settings/backups/{name}/worklog → admin: human worklog for an archive.
"""
import os
import re
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.services.phishing_detector import RULES_CATALOG, GLOBAL_POLICY
from app.api.v1.auth import get_current_admin
from app.database import get_db
from app.services import sender_lists
from app.services import learning_guidance

router = APIRouter()

APP_DIR = Path("/opt/iris-mailguard")
SCRIPTS = APP_DIR / "scripts"
BACKUP_DIR = APP_DIR / "storage" / "backups"
BACKUP_GLOB = "mailguard_code_*.tar.gz"
ARCHIVE_RE = re.compile(r"^mailguard_code_[0-9]{8}_[0-9]{6}\.tar\.gz$")

# Dirs/files excluded when measuring "latest code change" (mirror backup excludes).
_PRUNE_DIRS = {"venv", "storage", "logs", ".git", "__pycache__"}


def _latest_code_mtime() -> float:
    """Newest mtime among code files (epoch), excluding runtime/secret dirs."""
    latest = 0.0
    for base, dirs, files in os.walk(APP_DIR):
        dirs[:] = [d for d in dirs if d not in _PRUNE_DIRS]
        for f in files:
            if f == ".env" or f.endswith(".pyc") or ".bak_" in f:
                continue
            try:
                m = os.path.getmtime(os.path.join(base, f))
                if m > latest:
                    latest = m
            except OSError:
                continue
    return latest


def _read_meta(arc: Path) -> dict:
    try:
        return json.loads((arc.parent / (arc.name + ".meta")).read_text(encoding="utf-8"))
    except Exception:
        return {}


@router.get("/settings/rules")
def list_rules(_admin=Depends(get_current_admin)):
    """Return the catalog of all detection rules + global policy."""
    return {"policy": GLOBAL_POLICY, "rules": RULES_CATALOG}


@router.get("/settings/backups")
def list_backups(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Full list of code-backup archives + freshness based on pending code changes."""
    items = []
    if BACKUP_DIR.is_dir():
        for f in BACKUP_DIR.glob(BACKUP_GLOB):
            st = f.stat()
            meta = _read_meta(f)
            items.append({
                "name": f.name,
                "size_bytes": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                "mtime_epoch": st.st_mtime,
                "reason": meta.get("reason"),
                "note": meta.get("note"),
                "actor": meta.get("actor"),
                "has_worklog": (BACKUP_DIR / (f.name + ".worklog.json")).exists(),
            })
    items.sort(key=lambda x: x["mtime"], reverse=True)

    latest = items[0] if items else None
    latest_age_s = None
    if latest:
        latest_age_s = (datetime.now(timezone.utc)
                        - datetime.fromisoformat(latest["mtime"])).total_seconds()

    # "fresh" = no un-backed-up code changes (NOT wall-clock age — conditional
    # backups mean idle nights are expected, not stale).
    code_mtime = _latest_code_mtime()
    latest_backup_epoch = latest["mtime_epoch"] if latest else 0.0
    pending_changes = bool(code_mtime > latest_backup_epoch + 1)
    fresh = bool(latest and not pending_changes)

    for it in items:
        it.pop("mtime_epoch", None)

    # Attach last restore reason from audit_log for each backup archive.
    if items:
        try:
            rows_q = db.execute(text("""
                SELECT DISTINCT ON (details->>'archive')
                       details->>'archive' AS archive_name,
                       details->>'restore_reason' AS restore_reason
                FROM audit_log
                WHERE action = 'restore_backup'
                ORDER BY details->>'archive', created_at DESC
            """)).fetchall()
            restore_reasons = {row[0]: row[1] for row in rows_q}
            for it in items:
                it["restore_reason"] = restore_reasons.get(it["name"])
        except Exception:
            for it in items:
                it["restore_reason"] = None

    return {
        "dir": str(BACKUP_DIR),
        "count": len(items),
        "total_bytes": sum(i["size_bytes"] for i in items),
        "latest_mtime": latest["mtime"] if latest else None,
        "latest_age_seconds": int(latest_age_s) if latest_age_s is not None else None,
        "fresh": fresh,
        "pending_changes": pending_changes,
        "schedule": "orar, doar la modificări (via mailguard-cron)",
        "retention": "3 zile (min 3 păstrate)",
        "backups": items,
    }


@router.post("/settings/backups/run-now")
def backup_now(note: str = Query("", max_length=500), admin=Depends(get_current_admin)):
    """Force-create a backup immediately (bypasses change-detection)."""
    try:
        subprocess.Popen(
            [str(SCRIPTS / "backup_code.sh"), "--force", "manual", note, (admin.get("username") or admin.get("email") or "")],
            cwd=str(APP_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
    except Exception as e:
        raise HTTPException(500, f"backup spawn failed: {e}")
    return {"ok": True, "message": "Backup pornit. Reîncarcă lista în câteva secunde."}


@router.post("/settings/backups/{name}/restore", status_code=202)
def restore_backup(name: str, reason: str = Query("", max_length=500), db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Restore code from an archive. Creates a pre-restore backup, then restarts the API."""
    if not ARCHIVE_RE.match(name):
        raise HTTPException(400, "Nume arhivă invalid")
    arc = BACKUP_DIR / name
    if not arc.is_file():
        raise HTTPException(404, "Arhivă inexistentă")

    reviewer = admin.get("username") or admin.get("email") or "admin"
    db.execute(text("""
        INSERT INTO audit_log(actor, action, entity_type, entity_id, details)
        VALUES (:a, 'restore_backup', 'backup', 0, CAST(:d AS jsonb))
    """), {"a": reviewer, "d": json.dumps({"archive": name, "restore_reason": reason})})
    db.commit()

    try:
        subprocess.Popen(
            [str(SCRIPTS / "restore_code.sh"), name, reviewer],
            cwd=str(APP_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
    except Exception as e:
        raise HTTPException(500, f"restore spawn failed: {e}")

    return {
        "ok": True,
        "archive": name,
        "message": "Restaurare pornită. Se creează un backup al versiunii curente, apoi API-ul repornește (~10s).",
    }


@router.get("/settings/backups/{name}/worklog")
def backup_worklog(name: str, _admin=Depends(get_current_admin)):
    """Return the human worklog (summary) for an archive."""
    if not ARCHIVE_RE.match(name):
        raise HTTPException(400, "Nume arhivă invalid")
    wl = BACKUP_DIR / (name + ".worklog.json")
    if not wl.is_file():
        return {"archive": name, "summary": [], "summary_status": "missing",
                "files_changed": [], "note": None,
                "message": "Niciun worklog pentru acest backup."}
    try:
        return json.loads(wl.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"worklog citire eșuată: {e}")


SPAM_RULES_CATALOG = [
    {
        "code": "unsubscribe_link",
        "label": "Link dezabonare",
        "weight": 40,
        "scope": "subiect + corp",
        "description": "Conține text/link de dezabonare (cel mai puternic semnal bulk): unsubscribe, dezabon, opt-out, renunță la abonare, gestionează preferințele."
    },
    {
        "code": "bulk_sender",
        "label": "Expeditor automat",
        "weight": 20,
        "scope": "adresa expeditorului",
        "description": "Adresa de la are prefix automat: noreply, no-reply, newsletter, marketing, promo, mailer, campaign, notifications, hello etc."
    },
    {
        "code": "marketing_language",
        "label": "Limbaj promoțional",
        "weight": 20,
        "scope": "subiect + corp",
        "description": "Conține vocabular promoțional: reducere, discount, ofertă, voucher, gratuit, early access, ultima șansă, doar azi, exclusiv pentru tine, black friday, webinar."
    },
    {
        "code": "view_in_browser",
        "label": "Vizualizare în browser",
        "weight": 20,
        "scope": "subiect + corp",
        "description": "Conține link 'vizualizează în browser' sau 'view in browser' — indicator tipic newsletter."
    },
    {
        "code": "autogenerated_noreply",
        "label": "Auto-generated / No-reply",
        "weight": 60,
        "scope": "header IMAP + expeditor + subiect + corp",
        "description": "Mailuri automate de sistem, ticketing, autoreply corporate. Detectate prin: header Auto-Submitted/X-Auto-Response-Suppress, adresă no-reply/donotreply, subiect 'automatic reply'/'out of office', fraze corp 'this is an auto generated message'/'please do not reply'. Doar căsuțe personale — nu afectează pipeline-ul CTS.",
        "scope_note": "personal_mailboxes_only",
    },
]

SPAM_REPUTATION_RULES = [
    {
        "code": "allowlist_bypass",
        "effect": "scor → 0",
        "description": "Expeditor marcat 'Legit' de admin — scor forțat la 0, nu mai apare în lista Spam."
    },
    {
        "code": "blocklist_boost",
        "effect": "scor + 40",
        "description": "Expeditor marcat 'SPAM' de admin — boost +40 adăugat la scorul calculat, acoperind pragul."
    },
]


@router.get("/settings/spam-rules")
def list_spam_rules(_admin=Depends(get_current_admin)):
    """Catalog de reguli spam + scoring, pentru afișare UI."""
    return {
        "threshold": 50,
        "max_score": 100,
        "rules": SPAM_RULES_CATALOG,
        "reputation_rules": SPAM_REPUTATION_RULES,
        "score_examples": [
            {"signals": ["unsubscribe_link", "bulk_sender", "marketing_language"], "score": 80, "label": "Marketing tipic"},
            {"signals": ["unsubscribe_link", "marketing_language"], "score": 60, "label": "Newsletter promoțional"},
            {"signals": ["unsubscribe_link", "view_in_browser"], "score": 60, "label": "Newsletter fără bulk sender"},
        ]
    }


# ── AI category prompts (informatie / sesizare / reclamatie) — editable from Settings ──

@router.get("/settings/ai-category-prompts")
def get_ai_category_prompts(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Prompturile editabile pe categorie (DB peste default-uri din cod)."""
    from app.services import category_classifier as C
    prompts = dict(C.DEFAULT_PROMPTS)
    meta = {}
    try:
        rows = db.execute(text("SELECT category, prompt_text, updated_at, updated_by "
                               "FROM ai_category_prompts")).fetchall()
        for r in rows:
            m = r._mapping
            if m["category"] in C.EDITABLE and (m["prompt_text"] or "").strip():
                prompts[m["category"]] = m["prompt_text"]
                meta[m["category"]] = {"updated_at": str(m["updated_at"]), "updated_by": m["updated_by"]}
    except Exception:
        pass
    return {"categories": C.EDITABLE, "all_categories": C.CATEGORIES,
            "prompts": {k: prompts[k] for k in C.EDITABLE}, "meta": meta,
            "defaults": {k: C.DEFAULT_PROMPTS[k] for k in C.EDITABLE}}


@router.put("/settings/ai-category-prompts")
def update_ai_category_prompts(body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Salvează prompturi (informatie/sesizare/reclamatie) + versionează automat în istoric.
    Body opțional: source ('manual'|'ai_regenerate'), explicatie, based_on."""
    from app.services import category_classifier as C
    reviewer = admin.get("username") or admin.get("email") or "admin"
    source = (str(body.get("source") or "manual")).strip()[:20]
    explicatie = body.get("explicatie")
    try:
        based_on = int(body["based_on"]) if body.get("based_on") is not None else None
    except (ValueError, TypeError):
        based_on = None
    cur_rows = db.execute(text("SELECT category, prompt_text FROM ai_category_prompts")).fetchall()
    current = {r._mapping["category"]: r._mapping["prompt_text"] for r in cur_rows}
    updated = []
    for cat in C.EDITABLE:
        if cat in body and isinstance(body[cat], str) and body[cat].strip():
            new_text = body[cat].strip()
            if (current.get(cat) or "").strip() == new_text:
                continue  # neschimbat -> nu versionăm
            db.execute(text("""
                INSERT INTO ai_category_prompts(category, prompt_text, updated_at, updated_by)
                VALUES (:c, :t, NOW(), :by)
                ON CONFLICT (category) DO UPDATE SET
                  prompt_text=EXCLUDED.prompt_text, updated_at=NOW(), updated_by=EXCLUDED.updated_by
            """), {"c": cat, "t": new_text, "by": reviewer})
            db.execute(text("""
                INSERT INTO ai_category_prompt_versions(category, prompt_text, source, explicatie, based_on, created_by)
                VALUES (:c, :t, :s, :e, :b, :by)
            """), {"c": cat, "t": new_text, "s": source, "e": explicatie, "b": based_on, "by": reviewer})
            updated.append(cat)
    if not updated:
        return {"ok": True, "updated": [], "note": "Niciun prompt schimbat (text identic)."}
    db.execute(text("""
        INSERT INTO audit_log(actor, action, entity_type, entity_id, details)
        VALUES (:a, 'update_ai_prompts', 'settings', 0, CAST(:d AS jsonb))
    """), {"a": reviewer, "d": json.dumps({"updated": updated, "source": source})})
    db.commit()
    return {"ok": True, "updated": updated, "source": source,
            "note": "Versiune salvată în istoric. Rerulează pe emailurile dorite (Reclasifică de la…)."}


@router.get("/settings/ai-category-prompts/versions")
def list_ai_prompt_versions(limit: int = Query(60, ge=1, le=300),
                            db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Istoricul versiunilor de prompt (snapshot la fiecare salvare/regenerare)."""
    rows = db.execute(text("""
        SELECT id, category, source, explicatie, based_on, created_by,
               to_char(created_at,'YYYY-MM-DD HH24:MI') AS created_at,
               length(prompt_text) AS len, prompt_text
        FROM ai_category_prompt_versions ORDER BY id DESC LIMIT :l
    """), {"l": limit}).fetchall()
    return {"items": [dict(r._mapping) for r in rows]}


# ── AI call category prompts (informatie / sesizare / reclamatie, pe baza transcriptului) ──

@router.get("/settings/ai-call-category-prompts")
def get_ai_call_category_prompts(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Prompturile editabile pe categorie pentru apeluri (DB peste default-uri din cod)."""
    from app.services import call_classifier as C
    prompts = dict(C.DEFAULT_CALL_PROMPTS)
    meta = {}
    try:
        rows = db.execute(text("SELECT category, prompt_text, updated_at, updated_by "
                               "FROM ai_call_category_prompts")).fetchall()
        for r in rows:
            m = r._mapping
            if m["category"] in C.EDITABLE and (m["prompt_text"] or "").strip():
                prompts[m["category"]] = m["prompt_text"]
                meta[m["category"]] = {"updated_at": str(m["updated_at"]), "updated_by": m["updated_by"]}
    except Exception:
        pass
    return {"categories": C.EDITABLE, "all_categories": C.CATEGORIES,
            "prompts": {k: prompts[k] for k in C.EDITABLE}, "meta": meta,
            "defaults": {k: C.DEFAULT_CALL_PROMPTS[k] for k in C.EDITABLE}}


@router.put("/settings/ai-call-category-prompts")
def update_ai_call_category_prompts(body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Salvează prompturi de categorie pentru apeluri + versionează automat în istoric."""
    from app.services import call_classifier as C
    reviewer = admin.get("username") or admin.get("email") or "admin"
    source = (str(body.get("source") or "manual")).strip()[:20]
    cur_rows = db.execute(text("SELECT category, prompt_text FROM ai_call_category_prompts")).fetchall()
    current = {r._mapping["category"]: r._mapping["prompt_text"] for r in cur_rows}
    updated = []
    for cat in C.EDITABLE:
        if cat in body and isinstance(body[cat], str) and body[cat].strip():
            new_text = body[cat].strip()
            if (current.get(cat) or "").strip() == new_text:
                continue
            db.execute(text("""
                INSERT INTO ai_call_category_prompts(category, prompt_text, updated_at, updated_by)
                VALUES (:c, :t, NOW(), :by)
                ON CONFLICT (category) DO UPDATE SET
                  prompt_text=EXCLUDED.prompt_text, updated_at=NOW(), updated_by=EXCLUDED.updated_by
            """), {"c": cat, "t": new_text, "by": reviewer})
            db.execute(text("""
                INSERT INTO ai_call_category_prompt_versions(category, prompt_text, source, created_by)
                VALUES (:c, :t, :s, :by)
            """), {"c": cat, "t": new_text, "s": source, "by": reviewer})
            updated.append(cat)
    if not updated:
        return {"ok": True, "updated": [], "note": "Niciun prompt schimbat (text identic)."}
    db.commit()
    return {"ok": True, "updated": updated, "source": source,
            "note": "Versiune salvată în istoric. Se aplică la următoarele apeluri clasificate."}


@router.get("/settings/ai-call-category-prompts/versions")
def list_ai_call_prompt_versions(limit: int = Query(60, ge=1, le=300),
                                 db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Istoricul versiunilor de prompt de categorie pentru apeluri."""
    rows = db.execute(text("""
        SELECT id, category, source, created_by,
               to_char(created_at,'YYYY-MM-DD HH24:MI') AS created_at,
               length(prompt_text) AS len, prompt_text
        FROM ai_call_category_prompt_versions ORDER BY id DESC LIMIT :l
    """), {"l": limit}).fetchall()
    return {"items": [dict(r._mapping) for r in rows]}


# ── AI department prompts (8 departamente) — editable from Settings (mirror al categoriei) ──

@router.get("/settings/ai-department-prompts")
def get_ai_department_prompts(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Prompturile editabile pe departament (DB peste default-uri din cod)."""
    from app.services import department_classifier as D
    prompts = dict(D.DEFAULT_PROMPTS)
    meta = {}
    try:
        rows = db.execute(text("SELECT department, prompt_text, updated_at, updated_by "
                               "FROM ai_department_prompts")).fetchall()
        for r in rows:
            m = r._mapping
            if m["department"] in D.EDITABLE and (m["prompt_text"] or "").strip():
                prompts[m["department"]] = m["prompt_text"]
                meta[m["department"]] = {"updated_at": str(m["updated_at"]), "updated_by": m["updated_by"]}
    except Exception:
        pass
    return {"departments": D.EDITABLE, "labels": D.DEPT_LABELS,
            "prompts": {k: prompts[k] for k in D.EDITABLE}, "meta": meta,
            "defaults": {k: D.DEFAULT_PROMPTS[k] for k in D.EDITABLE}}


@router.put("/settings/ai-department-prompts")
def update_ai_department_prompts(body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Salveaza prompturi pe departament + versioneaza automat in istoric.
    Body optional: source ('manual'|'ai_regenerate'), explicatie, based_on."""
    from app.services import department_classifier as D
    reviewer = admin.get("username") or admin.get("email") or "admin"
    source = (str(body.get("source") or "manual")).strip()[:20]
    explicatie = body.get("explicatie")
    try:
        based_on = int(body["based_on"]) if body.get("based_on") is not None else None
    except (ValueError, TypeError):
        based_on = None
    cur_rows = db.execute(text("SELECT department, prompt_text FROM ai_department_prompts")).fetchall()
    current = {r._mapping["department"]: r._mapping["prompt_text"] for r in cur_rows}
    updated = []
    for dep in D.EDITABLE:
        if dep in body and isinstance(body[dep], str) and body[dep].strip():
            new_text = body[dep].strip()
            if (current.get(dep) or "").strip() == new_text:
                continue  # neschimbat -> nu versionam
            db.execute(text("""
                INSERT INTO ai_department_prompts(department, prompt_text, updated_at, updated_by)
                VALUES (:c, :t, NOW(), :by)
                ON CONFLICT (department) DO UPDATE SET
                  prompt_text=EXCLUDED.prompt_text, updated_at=NOW(), updated_by=EXCLUDED.updated_by
            """), {"c": dep, "t": new_text, "by": reviewer})
            db.execute(text("""
                INSERT INTO ai_department_prompt_versions(department, prompt_text, source, explicatie, based_on, created_by)
                VALUES (:c, :t, :s, :e, :b, :by)
            """), {"c": dep, "t": new_text, "s": source, "e": explicatie, "b": based_on, "by": reviewer})
            updated.append(dep)
    if not updated:
        return {"ok": True, "updated": [], "note": "Niciun prompt schimbat (text identic)."}
    db.execute(text("""
        INSERT INTO audit_log(actor, action, entity_type, entity_id, details)
        VALUES (:a, 'update_ai_dept_prompts', 'settings', 0, CAST(:d AS jsonb))
    """), {"a": reviewer, "d": json.dumps({"updated": updated, "source": source})})
    db.commit()
    return {"ok": True, "updated": updated, "source": source,
            "note": "Versiune salvata in istoric. Reincadreaza emailurile dorite."}


@router.get("/settings/ai-department-prompts/versions")
def list_ai_dept_prompt_versions(limit: int = Query(60, ge=1, le=300),
                                 db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Istoricul versiunilor de prompt de departament."""
    rows = db.execute(text("""
        SELECT id, department, source, explicatie, based_on, created_by,
               to_char(created_at,'YYYY-MM-DD HH24:MI') AS created_at,
               length(prompt_text) AS len, prompt_text
        FROM ai_department_prompt_versions ORDER BY id DESC LIMIT :l
    """), {"l": limit}).fetchall()
    return {"items": [dict(r._mapping) for r in rows]}


# ── Reguli deterministe de departament (CRUD, ca sender-lists) ──

@router.get("/settings/department-rules")
def get_department_rules(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    from app.services import department_rules, department_classifier as D
    return {"rules": department_rules.list_all(db), "labels": D.DEPT_LABELS}


@router.post("/settings/department-rules")
def add_department_rule(body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    from app.services import department_rules
    reviewer = admin.get("username") or admin.get("email") or "admin"
    res = department_rules.add_rule(db, body, reviewer)
    if res.get("error"):
        raise HTTPException(400, res["error"])
    return res


@router.put("/settings/department-rules")
def update_department_rule(body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    from app.services import department_rules
    rid = (body.get("id") or "").strip()
    if not rid:
        raise HTTPException(400, "Lipseste id-ul regulii")
    reviewer = admin.get("username") or admin.get("email") or "admin"
    res = department_rules.update_rule(db, rid, body, reviewer)
    if res.get("error"):
        raise HTTPException(400, res["error"])
    return res


@router.delete("/settings/department-rules")
def delete_department_rule(id: str = Query(...), db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    from app.services import department_rules
    reviewer = admin.get("username") or admin.get("email") or "admin"
    res = department_rules.remove_rule(db, id, reviewer)
    if res.get("error"):
        raise HTTPException(404, res["error"])
    return res


# ── Prompt intent-detection IRIS (editabil din Settings > Prompturi AI) ──
# Stocat in tabela `prompts` (code=nova_intent_detection), istoric in prompt_history.
# Default = constanta din strict_intent_gate.SYSTEM_PROMPT (cod).

@router.get("/settings/nova-intent-prompt")
def get_nova_intent_prompt(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Promptul editabil de verificare a intentiei (NOVA), cu default-ul din cod."""
    from app.services.strict_intent_gate import SYSTEM_PROMPT, INTENT_PROMPT_CODE
    row = db.execute(text(
        "SELECT system_prompt, updated_at, created_by, version FROM prompts WHERE code=:c"),
        {"c": INTENT_PROMPT_CODE}).fetchone()
    meta = {}
    prompt = SYSTEM_PROMPT
    if row and (row._mapping["system_prompt"] or "").strip():
        prompt = row._mapping["system_prompt"]
        meta = {"updated_at": str(row._mapping["updated_at"]),
                "updated_by": row._mapping["created_by"],
                "version": row._mapping["version"]}
    return {"code": INTENT_PROMPT_CODE, "prompt": prompt,
            "default": SYSTEM_PROMPT, "is_custom": bool(meta), "meta": meta}


@router.put("/settings/nova-intent-prompt")
def update_nova_intent_prompt(body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Salveaza promptul de intent IRIS (upsert in prompts + snapshot in prompt_history + audit)."""
    from app.services.strict_intent_gate import INTENT_PROMPT_CODE
    new_text = (body or {}).get("prompt")
    if not isinstance(new_text, str) or not new_text.strip():
        raise HTTPException(400, "prompt gol sau invalid")
    new_text = new_text.strip()
    reviewer = admin.get("username") or admin.get("email") or "admin"

    row = db.execute(text(
        "SELECT id, system_prompt, version FROM prompts WHERE code=:c"),
        {"c": INTENT_PROMPT_CODE}).fetchone()
    if row:
        if (row._mapping["system_prompt"] or "").strip() == new_text:
            return {"ok": True, "updated": False, "note": "Text identic, nimic de salvat."}
        new_ver = (row._mapping["version"] or 1) + 1
        db.execute(text(
            "UPDATE prompts SET system_prompt=:t, version=:v, is_active=TRUE, "
            "updated_at=NOW() WHERE id=:id"),
            {"t": new_text, "v": new_ver, "id": row._mapping["id"]})
        db.execute(text(
            "INSERT INTO prompt_history(prompt_id, version, system_prompt, user_prompt_template, changed_by, changed_at) "
            "VALUES (:pid, :v, :t, '', :by, NOW())"),
            {"pid": row._mapping["id"], "v": new_ver, "t": new_text, "by": reviewer})
        pid = row._mapping["id"]
    else:
        ins = db.execute(text(
            "INSERT INTO prompts(code, name, description, system_prompt, user_prompt_template, "
            "model, version, is_active, created_by) "
            "VALUES (:c, 'Verificare intentie NOVA', "
            "'Prompt trimis NOVA pentru a verifica intentia mailurilor candidate la carantina.', "
            ":t, '', '', 1, TRUE, :by) RETURNING id"),
            {"c": INTENT_PROMPT_CODE, "t": new_text, "by": reviewer}).fetchone()
        pid = ins._mapping["id"]
        db.execute(text(
            "INSERT INTO prompt_history(prompt_id, version, system_prompt, user_prompt_template, changed_by, changed_at) "
            "VALUES (:pid, 1, :t, '', :by, NOW())"),
            {"pid": pid, "t": new_text, "by": reviewer})

    db.execute(text(
        "INSERT INTO audit_log(actor, action, entity_type, entity_id, details) "
        "VALUES (:a, 'update_intent_prompt', 'settings', :id, CAST(:d AS jsonb))"),
        {"a": reviewer, "id": pid, "d": json.dumps({"code": INTENT_PROMPT_CODE, "len": len(new_text)})})
    db.commit()
    return {"ok": True, "updated": True, "prompt_id": pid,
            "note": "Prompt salvat. Verificatorul de intentie IRIS il foloseste imediat."}


# ── Learning din carantinarea manuala (read-only, pentru validare umana) ──

@router.get("/settings/learning-proposals")
def get_learning_proposals(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Continutul learning-ului manual: blacklist + exemple periculoase + propuneri IRIS."""
    row = db.execute(text(
        "SELECT value, updated_at, updated_by FROM settings "
        "WHERE key='phishing_manual_learning'")).fetchone()
    if not row or not row._mapping["value"]:
        return {"blacklist": {}, "examples": [], "proposals": [], "accepted_keys": [],
                "counts": {"blacklist": 0, "examples": 0, "proposals": 0, "accepted": 0}, "meta": {}}
    v = row._mapping["value"] or {}
    bl = v.get("blacklist") or {}
    ex = v.get("examples") or []
    pr = v.get("proposals") or []
    acc_map = v.get("accepted_suggestions") or {}
    accepted = [{"email_id": a.get("email_id"), "type": a.get("type"), "summary": a.get("summary")}
                for a in acc_map.values()]
    return {
        "blacklist": bl,
        "examples": ex[-50:][::-1],
        "proposals": pr[-50:][::-1],
        "accepted": accepted,
        "accepted_keys": list(acc_map.keys()),
        "counts": {"blacklist": len(bl), "examples": len(ex), "proposals": len(pr),
                   "accepted": len(acc_map)},
        "meta": {"updated_at": str(row._mapping["updated_at"]),
                 "updated_by": row._mapping["updated_by"]},
    }


@router.post("/settings/learning-proposals/toggle")
def toggle_learning_proposal(body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Bifează/debifează o sugestie IRIS (per-linie) pentru ghidul porții de intenție."""
    reviewer = admin.get("username") or admin.get("email") or "admin"
    type_ = (body.get("type") or "").strip().lower()
    summary = body.get("summary") or ""
    if not summary.strip():
        raise HTTPException(400, "Sugestie goală")
    res = learning_guidance.toggle(db, body.get("email_id"), type_, summary,
                                   bool(body.get("accepted")), reviewer)
    if res.get("error"):
        raise HTTPException(400, res["error"])
    return res


# ── Liste expeditori (blacklist / whitelist) pentru detecție ──────────────────
# Store canonic: settings['phishing_manual_learning'].{blacklist, whitelist}
# (vezi app/services/sender_lists.py). Blacklist = enforcement hard (carantină
# strictă); Whitelist = suprimare soft a semnalelor slabe. Auto-populate din
# acțiunile de spam (mark_spam → blacklist, legit → whitelist).

@router.get("/settings/sender-lists")
def get_sender_lists(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    data = sender_lists.list_all(db)
    return {
        "blacklist": data["blacklist"],
        "whitelist": data["whitelist"],
        "counts": {"blacklist": len(data["blacklist"]), "whitelist": len(data["whitelist"])},
    }


@router.post("/settings/sender-lists")
def add_sender_list(body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    reviewer = admin.get("username") or admin.get("email") or "admin"
    lst = (body.get("list") or "").strip().lower()
    if lst not in sender_lists.LISTS:
        raise HTTPException(400, "Listă invalidă (blacklist/whitelist)")
    tip = (body.get("tip") or "").strip().lower() or None
    if tip and tip not in sender_lists.TIPS:
        raise HTTPException(400, "Tip invalid (carantina/spam)")
    res = sender_lists.add_entry(db, lst, body.get("value") or "", reviewer,
                                 source="manual", note=body.get("note"), tip=tip)
    if res.get("conflict"):
        raise HTTPException(409, "Valoarea există deja în lista '%s'. Șterge-o întâi de acolo."
                            % res["conflict"])
    if res.get("error"):
        raise HTTPException(400, res["error"])
    return res


@router.put("/settings/sender-lists")
def edit_sender_list(body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    reviewer = admin.get("username") or admin.get("email") or "admin"
    lst = (body.get("list") or "").strip().lower()
    if lst not in sender_lists.LISTS:
        raise HTTPException(400, "Listă invalidă")
    tip = (body.get("tip") or "").strip().lower() or None
    if tip and tip not in sender_lists.TIPS:
        raise HTTPException(400, "Tip invalid (carantina/spam)")
    res = sender_lists.set_flags(db, lst, body.get("value") or "", reviewer,
                                 muted=body.get("muted"), note=body.get("note"),
                                 new_value=body.get("new_value"), tip=tip)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "Eroare")
    return res


@router.delete("/settings/sender-lists")
def delete_sender_list(list: str = Query(...), value: str = Query(...),
                       db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    reviewer = admin.get("username") or admin.get("email") or "admin"
    if list not in sender_lists.LISTS:
        raise HTTPException(400, "Listă invalidă")
    res = sender_lists.remove_entry(db, list, value, reviewer)
    if not res.get("ok"):
        raise HTTPException(404, res.get("error") or "Inexistent")
    return res


# ── Securitate: politici anti-spoofing (auth_policy) + antivirus (av_policy) ──
_SEC_DEFAULTS = {
    "auth_policy": {
        "enabled": True, "fail_action": "quarantine_strict", "escalate_external_fail": False,
        "protect_domains": [],
        "weights": {"dmarc_fail": 45, "spf_hardfail": 25, "spf_softfail": 8, "dkim_fail": 15,
                    "no_auth_results": 6, "from_unaligned": 20, "returnpath_mismatch": 12},
        "suspicious_at": 12, "fail_at": 30,
    },
    "av_policy": {"enabled": True, "malware_action": "quarantine_strict", "suspicious_score": 20},
}


@router.get("/settings/security-policies")
def get_security_policies(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Politici de securitate: anti-spoofing (SPF/DKIM/DMARC) + antivirus (atașamente)."""
    out = {}
    for key, default in _SEC_DEFAULTS.items():
        row = db.execute(text("SELECT value FROM settings WHERE key=:k"), {"k": key}).fetchone()
        out[key] = (row._mapping["value"] if row and row._mapping["value"] is not None else default)
    out["defaults"] = _SEC_DEFAULTS
    return out


@router.put("/settings/security-policies")
def update_security_policies(body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Salvează auth_policy și/sau av_policy (merge peste valoarea curentă)."""
    reviewer = admin.get("username") or admin.get("email") or "admin"
    updated = []
    for key in ("auth_policy", "av_policy"):
        if key in body and isinstance(body[key], dict):
            cur = db.execute(text("SELECT value FROM settings WHERE key=:k"), {"k": key}).fetchone()
            merged = dict(_SEC_DEFAULTS[key])
            if cur and cur._mapping["value"]:
                merged.update(cur._mapping["value"])
            merged.update(body[key])
            db.execute(text("""
                INSERT INTO settings(key, value) VALUES (:k, CAST(:v AS jsonb))
                ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
            """), {"k": key, "v": json.dumps(merged)})
            updated.append(key)
    if updated:
        db.execute(text("""
            INSERT INTO audit_log(actor, action, entity_type, entity_id, details)
            VALUES (:a, 'update_security_policies', 'settings', 0, CAST(:d AS jsonb))
        """), {"a": reviewer, "d": json.dumps({"updated": updated})})
        db.commit()
    return {"ok": True, "updated": updated}


# ── CTS send flags — switch-uri ON/OFF câmpuri API (PS-2026-0128) ──────────────
_CTS_SEND_FLAGS_DEFAULT = {
    "send_categorie":      True,
    "send_departament":    True,
    "send_prioritate":     True,
    "send_documente":      True,
    "auto_rotate_images":  False,
}



@router.get("/docs/cts-types", response_class=HTMLResponse)
def get_cts_types_doc(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """HTML cu toate tipurile de documente + câmpurile extrase — pentru integrarea CTS."""
    from collections import defaultdict

    rows = db.execute(text(
        "SELECT id, category, name, extract_fields FROM document_types "
        "WHERE status='active' ORDER BY category, id"
    ), {}).fetchall()

    def _ftype(t):
        return {'text': 'string', 'date': 'date (YYYY-MM-DD)', 'number': 'number', 'boolean': 'boolean'}.get(t or 'text', t or 'text')

    def _ex(fld):
        t = (fld.get('type') or 'text').lower()
        n = (fld.get('name') or '').lower()
        if t == 'boolean': return True
        if t == 'date': return '2026-04-24'
        if t == 'number':
            if any(x in n for x in ['weight', 'masa', 'laden', 'unladen']): return 18000
            if any(x in n for x in ['capacity', 'engine', 'p.1']): return 10837
            if any(x in n for x in ['power', 'p.2']): return 330
            if 'co2' in n: return 195
            if 'axle' in n or 'axa' in n: return 2
            if 'length' in n or 'lungime' in n: return 6000
            if 'width' in n or 'latime' in n: return 2490
            if 'height' in n or 'inaltime' in n: return 3800
            if 'gross' in n: return 44000
            if 'year' in n or 'an' in n: return 2022
            return 1000
        # text
        if 'vin' in n or 'serie sasiu' in n: return 'XLRTEF5100G403349'
        if 'plate' in n or 'inmatriculare' in n and 'numar' in n: return 'TM-99-EKO'
        if 'cnp' in n: return '1850315123456'
        if 'cui' in n: return 'RO21317878'
        if 'manufacturer' in n or 'd.1' in n or 'marca' in n: return 'DAF'
        if 'type' in n and 'd.2' in n: return 'XF'
        if 'commercial' in n or 'd.3' in n or 'model' in n: return 'XF 480'
        if 'fuel' in n or 'p.3' in n or 'combustibil' in n: return 'MOTORINA'
        if 'emission' in n and ('class' in n or 'v.9' in n): return 'Euro 6'
        if 'emission' in n and 'cemt' in n: return 'EURO VI'
        if 'category' in n or 'categorie' in n: return 'N3'
        if 'country' in n or 'tara' in n: return 'RO'
        if 'numar contract' in n: return '1874867'
        if 'prestator' in n: return 'CARGO TRACK SOLUTIONS SRL'
        if 'client' in n and 'cui' not in n: return 'TRANSPORT XYZ SRL'
        if 'seria' in n or 'serie' in n and 'permis' in n: return 'TM123456'
        if 'adresa' in n: return 'Str. Florilor nr. 5, Timișoara'
        if 'nume' in n: return 'POPESCU ION'
        if 'nastere' in n: return 'Timișoara, Timiș'
        if 'fidejusor' in n and ('c.i' in n or 'ci' in n): return 'TM 123456'
        if 'fidejusor' in n and 'cnp' in n: return '1780512123456'
        if 'fidejusor' in n and 'nume' in n: return 'IONESCU GHEORGHE'
        if 'vehicle group' in n: return None
        return 'valoare'

    def _section(cat, cat_rows):
        cat_labels = {'vehicul': 'vehicul', 'sofer': 'șofer', 'contract': 'contract'}
        cat_color = {'vehicul': '#3b82f6', 'sofer': '#10b981', 'contract': '#f59e0b'}
        clr = cat_color.get(cat, '#888')
        lbl = cat_labels.get(cat, cat)
        out = (f'<h2><span style="display:inline-block;padding:2px 10px;border-radius:6px;'
               f'font-size:12px;font-weight:700;color:#fff;background:{clr};margin-right:8px">'
               f'{lbl}</span>Documente categorie &ldquo;{lbl}&rdquo; ({len(cat_rows)} tipuri)</h2>\n')
        for r in cat_rows:
            m = r._mapping
            fields = m['extract_fields'] or []
            out += f'<h3>ID {m["id"]} &mdash; {m["name"]}</h3>\n'
            if not fields:
                out += '<p style="color:#888;font-style:italic;font-size:12px">F&aelig;r&aelig; câmpuri de extracție definite &mdash; <code>data</code> va fi <code>{}</code></p>\n'
            else:
                ex = {}
                out += '<table><tr><th>Câmp</th><th>Tip</th><th>Descriere</th></tr>\n'
                for fld in fields:
                    fname = fld.get('name', '')
                    out += (f'<tr><td><code>{fname}</code></td>'
                            f'<td>{_ftype(fld.get("type"))}</td>'
                            f'<td style="color:#555">{fld.get("description","")}</td></tr>\n')
                    ex[fname] = _ex(fld)
                out += '</table>\n'
                out += f'<pre>{json.dumps(ex, ensure_ascii=False, indent=2)}</pre>\n'
        return out

    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r._mapping['category']].append(r)

    body_html = ''
    for cat in ['vehicul', 'sofer', 'contract']:
        if cat in by_cat:
            body_html += _section(cat, by_cat[cat])

    total = sum(len(v) for v in by_cat.values())

    css = (
        'body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:1000px;'
        'margin:24px auto;padding:0 20px;color:#1a1a1a;line-height:1.5}'
        'h1{font-size:20px;margin-bottom:6px}h2{font-size:15px;margin-top:30px;'
        'border-bottom:1px solid #ddd;padding-bottom:4px}h3{font-size:13px;margin:18px 0 4px}'
        'table{width:100%;border-collapse:collapse;font-size:12.5px;margin:6px 0}'
        'th{background:#f3f4f6;text-align:left;padding:6px 10px;border:1px solid #ddd;font-weight:600}'
        'td{padding:5px 10px;border:1px solid #eee;vertical-align:top}'
        'code{background:#f3f4f6;padding:1px 5px;border-radius:4px;font-size:90%}'
        'pre{background:#0f172a;color:#e2e8f0;padding:10px 14px;border-radius:6px;'
        'font-size:12px;line-height:1.4;margin:6px 0;overflow:auto;white-space:pre-wrap;word-break:break-all}'
        '.noprint{background:#eef2ff;border:1px solid #c7d2fe;padding:8px 12px;'
        'border-radius:8px;font-size:12px;margin-bottom:16px}'
        '@media print{.noprint{display:none}body{margin:8mm}}'
    )

    html = (
        '<!doctype html><html lang="ro"><head><meta charset="utf-8">'
        f'<title>CTS &mdash; Mapare tipuri documente (Cargo360)</title>'
        f'<style>{css}</style></head><body>'
        '<div class="noprint">&#128196; Pentru PDF: in dialogul de printare alege Destina&#x21B8;ie &rarr; <b>Salveaz&#x103; ca PDF</b>.</div>'
        '<h1>CTS &mdash; Mapare tipuri documente &#x15F;i c&acirc;mpuri extrase</h1>'
        f'<p>Document de referin&#x21B;&#x103; pentru integrarea CTS cu endpoint-ul <code>GET /cts/get_email_documents</code>. '
        f'Generat automat din baza de date. {total} tipuri active.</p>'
        '<p>C&acirc;mpul <code>data</code> din r&#x103;spunsul fiec&#x103;rui document con&#x21B;ine cheile specifice tipului. '
        'C&acirc;mpurile cu valoarea <code>null</code> nu au putut fi extrase sau nu sunt prezente pe document. '
        '<code>data</code> poate fi <code>{}</code> pentru tipuri f&#x103;r&#x103; c&acirc;mpuri de extrac&#x21B;ie definite.</p>'
        f'{body_html}'
        '<p style="margin-top:32px;color:#999;font-size:11px">Generat din Cargo360 &mdash; Set&#x103;ri &rarr; Conexiune API</p>'
        '</body></html>'
    )
    return HTMLResponse(content=html)


@router.get("/settings/cts-send-flags")
def get_cts_send_flags(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Switch-uri individuale pentru câmpurile trimise în feed-ul CTS."""
    row = db.execute(text("SELECT value FROM settings WHERE key='cts_send_flags'"), {}).fetchone()
    flags = dict(_CTS_SEND_FLAGS_DEFAULT)
    if row and row._mapping["value"]:
        flags.update(row._mapping["value"])
    return {"flags": flags, "defaults": _CTS_SEND_FLAGS_DEFAULT}


@router.put("/settings/cts-send-flags")
def update_cts_send_flags(body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Actualizează switch-urile individuale pentru câmpurile API CTS."""
    reviewer = admin.get("username") or admin.get("email") or "admin"
    valid_keys = set(_CTS_SEND_FLAGS_DEFAULT.keys())
    update = {k: bool(v) for k, v in body.items() if k in valid_keys}
    if not update:
        raise HTTPException(400, "Niciun câmp valid. Câmpuri acceptate: " + str(sorted(valid_keys)))
    cur = db.execute(text("SELECT value FROM settings WHERE key='cts_send_flags'"), {}).fetchone()
    merged = dict(_CTS_SEND_FLAGS_DEFAULT)
    if cur and cur._mapping["value"]:
        merged.update(cur._mapping["value"])
    merged.update(update)
    db.execute(text(
        "INSERT INTO settings(key, value) VALUES ('cts_send_flags', CAST(:v AS jsonb)) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value"
    ), {"v": json.dumps(merged)})
    db.execute(text(
        "INSERT INTO audit_log(actor, action, entity_type, entity_id, details) "
        "VALUES (:a, 'update_cts_send_flags', 'settings', 0, CAST(:d AS jsonb))"
    ), {"a": reviewer, "d": json.dumps({"updated": update})})
    db.commit()
    return {"ok": True, "flags": merged}
