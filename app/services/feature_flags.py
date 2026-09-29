"""Flag-uri booleene din `settings` (comutatoare T3, implicit OFF).

Valoare acceptată ca ON: jsonb `true` sau `{"enabled": true}` — aceeași formă ca celelalte
comutatoare `processing.*`. Orice altceva (cheie lipsă, string, eroare de citire) = OFF, adică
comportamentul de dinainte. Citirea se memorează 30 s per proces: flag-urile se consultă pe căi
fierbinți (fiecare imagine trimisă la vision, fiecare atașament din drain).
"""
import logging
import threading
import time

from sqlalchemy import text

logger = logging.getLogger("mailguard.feature_flags")

_TTL_S = 30.0
_lock = threading.Lock()
_cache: dict = {}


def _session():
    from app.database import SessionLocal
    return SessionLocal()


def reset_cache() -> None:
    with _lock:
        _cache.clear()


def _read(key: str) -> bool:
    db = None
    try:
        db = _session()
        row = db.execute(text("SELECT value FROM settings WHERE key = :k"), {"k": key}).fetchone()
    except Exception:
        logger.warning("feature_flags: nu pot citi %s — OFF", key)
        return False
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
    if not row:
        return False
    v = row[0]
    if isinstance(v, dict):
        v = v.get("enabled")
    return v is True


def is_enabled(key: str) -> bool:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < _TTL_S:
            return hit[1]
    val = _read(key)
    with _lock:
        _cache[key] = (now, val)
    return val
