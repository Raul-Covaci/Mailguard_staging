"""Sincronizare AUTOMATA a view-urilor IRIS Data Views, rulata de cron.

Pana acum sincronizarea unui view se facea DOAR din butonul „Sincronizeaza" (pagina „Surse
date"): comutatorul de auto-sync exista in UI si in schema (`iris_dv_state.auto_sync`), dar nu
avea nici endpoint, nici rulare — deci un view ramanea la ultima apasare manuala. Raportul de
departamente citeste direct tabelele astea, deci pe productie ar fi aratat date inghetate.

Cum ruleaza: cronul de 5 minute (`POST /process/run-now`) apeleaza `run_due_syncs()`. Fiecare
view are propriul interval (`auto_sync_interval_minutes`, minim 5 = cadenta cronului); se
sincronizeaza doar cele la care a trecut intervalul de la `last_sync_at`.

Modul (snapshot / incremental) NU se decide aici — il rezolva `iris_dv.sync_view` din ce declara
view-ul in /onboarding.

Concurenta: un `pg_advisory_lock` global (o rulare de cron poate depasi 5 minute pe un view mare;
fara lock, urmatorul tick ar porni acelasi sync peste el).
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict

from sqlalchemy import text

logger = logging.getLogger("mailguard.iris_dv_autosync")

LOCK_KEY = 778251           # pg_advisory_lock global pentru auto-sync-ul DV
MAX_VIEWS_PER_TICK = 6      # plafon per rulare: un tick de cron nu trebuie sa tina minute intregi


def _due(state: dict, now: datetime) -> bool:
    interval = int(state.get("auto_sync_interval_minutes") or 60)
    last = state.get("last_sync_at")
    if not last:
        return True
    lt = last if hasattr(last, "tzinfo") else None
    if lt is None:
        try:
            lt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        except Exception:
            return True
    if lt.tzinfo is None:
        lt = lt.replace(tzinfo=timezone.utc)
    return (now - lt) >= timedelta(minutes=max(1, interval))


def run_due_syncs() -> Dict[str, Any]:
    """Sincronizeaza view-urile cu auto_sync=TRUE la care a expirat intervalul.
    Best-effort: un view care pica nu opreste restul. Apelat din cron."""
    from app.database import SessionLocal
    from app.api.v1.iris_dv import _get_api_key, sync_view

    db = SessionLocal()
    try:
        got = db.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar()
        if not got:
            return {"skipped": "already running"}
        try:
            api_key = _get_api_key(db)
            if not api_key:
                return {"skipped": "iris_dv.api_key nesetat"}

            rows = db.execute(text(
                "SELECT * FROM iris_dv_state WHERE auto_sync = TRUE ORDER BY last_sync_at NULLS FIRST"
            )).fetchall()
            now = datetime.now(timezone.utc)
            due = [dict(r._mapping) for r in rows]
            due = [st for st in due if _due(st, now)][:MAX_VIEWS_PER_TICK]
            if not due:
                return {"ok": True, "checked": len(rows), "synced": 0}

            results = {}
            for st in due:
                name = st["view_name"]
                try:
                    results[name] = sync_view(name, api_key, db, mode=st.get("mode"))
                except Exception as e:      # eroarea e deja scrisa in iris_dv_state.last_error
                    logger.warning("auto-sync %s a esuat: %s", name, e)
                    results[name] = {"error": str(e)}
                    db.rollback()
            return {"ok": True, "checked": len(rows), "synced": len(due), "results": results}
        finally:
            db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
            db.commit()
    except Exception as e:
        logger.warning("run_due_syncs: %s", e)
        return {"error": str(e)}
    finally:
        db.close()
