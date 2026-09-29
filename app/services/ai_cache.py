"""Cache de rezultat AI (T3-L1) — interceptare centrală în `iris_ai.run_prompt()`.

Același document / atașament ajungea la model de mai multe ori cu intrare identică: bucla
`retry_transient` a drain-ului de documente (resegmentează TOATE paginile la fiecare 5 minute),
reclasificările, reîncercările `op_series` (aceeași imagine, același prompt, până la 3 ori).

⛔ Cheia e sha256 pe payload-ul EFECTIV trimis la gateway, NU pe numele task-ului. Numele de task
din documents.py nu garantează intrare identică (`doc_segment` nu include catalogul și pagina
anterioară, `doc_rename` doar att_id+part_no, `doc_autogroup` doar ID-urile) — pe ele, un cache
ar servi răspunsul altui document.

Reguli (decizie T3-L1, 2026-09-29):
- activ doar cu settings['ai_cache.enabled']=true ȘI prefixul funcției în settings['ai_cache.prefixes'];
- niciodată cu temperature > 0 (generare intenționat variabilă: doc_prompt_gen, doc_detect_gen);
- în cache intră doar ok:true validat de apelant (`cache_ok`), niciodată erori;
- `ai_cache_bypass` (ContextVar) ocolește complet cache-ul — setat doar de „Reidentifică";
- TTL settings['ai_cache.ttl_days'] (implicit 10), curățare din tick, cel mult o dată pe oră;
- la hit: nimic spre gateway, nimic în ai_call_log; un rând în ai_cache_hit_log (măsurare);
- ORICE eșec al cache-ului = comportamentul de azi (apel normal) + WARNING. Cache-ul nu are voie
  să oprească procesarea.
"""
from __future__ import annotations

import base64
import contextvars
import hashlib
import json
import logging
import threading
import time
from typing import Callable, Optional

from sqlalchemy import text

logger = logging.getLogger("mailguard.ai_cache")

# Ocolire explicită (acțiune manuală pe un singur document). Nici citire, nici scriere.
ai_cache_bypass: contextvars.ContextVar = contextvars.ContextVar("ai_cache_bypass", default=False)

KEY_ENABLED = "ai_cache.enabled"
KEY_PREFIXES = "ai_cache.prefixes"
KEY_EPOCH = "ai_cache.epoch"
KEY_TTL = "ai_cache.ttl_days"
KEY_LAST_PURGE = "ai_cache.last_purge_at"

DEFAULT_EPOCH = 1
DEFAULT_TTL_DAYS = 10
_CONFIG_TTL_S = 30.0          # configul se recitește cel mult o dată la 30 s per proces
_PURGE_EVERY = "1 hour"

_cfg_lock = threading.Lock()
_cfg_cache: dict = {"at": 0.0, "cfg": None}


def _session():
    from app.database import SessionLocal
    return SessionLocal()


def reset_config_cache() -> None:
    with _cfg_lock:
        _cfg_cache["at"], _cfg_cache["cfg"] = 0.0, None


def task_prefix(task) -> Optional[str]:
    """Funcția din task, fără prefixul `cargo360:`, slug sau hash: 'cargo360:doc_segment:ab:_' -> 'doc_segment'."""
    t = (str(task).strip() if task else "")
    if t.startswith("cargo360:"):
        t = t[len("cargo360:"):]
    p = t.split(":", 1)[0].strip()
    return p or None


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "on", "yes")
    return False


def _as_pos_int(v, default: int) -> int:
    try:
        if isinstance(v, bool):
            return default
        n = int(v)
        return n if n > 0 else default
    except Exception:
        return default


def load_config() -> dict:
    """{enabled, prefixes, epoch, ttl_days}. Eroare de citire = dezactivat (comportamentul de azi)."""
    now = time.monotonic()
    with _cfg_lock:
        if _cfg_cache["cfg"] is not None and now - _cfg_cache["at"] < _CONFIG_TTL_S:
            return _cfg_cache["cfg"]
    cfg = {"enabled": False, "prefixes": frozenset(), "epoch": DEFAULT_EPOCH,
           "ttl_days": DEFAULT_TTL_DAYS}
    db = None
    try:
        db = _session()
        rows = db.execute(text("SELECT key, value FROM settings WHERE key IN (:a, :b, :c, :d)"),
                          {"a": KEY_ENABLED, "b": KEY_PREFIXES, "c": KEY_EPOCH, "d": KEY_TTL}).fetchall()
        vals = {r[0]: r[1] for r in rows}
        pref = vals.get(KEY_PREFIXES)
        cfg = {
            "enabled": _as_bool(vals.get(KEY_ENABLED)),
            "prefixes": frozenset(str(p).strip() for p in pref if isinstance(p, str) and p.strip())
                        if isinstance(pref, list) else frozenset(),
            "epoch": _as_pos_int(vals.get(KEY_EPOCH), DEFAULT_EPOCH),
            "ttl_days": _as_pos_int(vals.get(KEY_TTL), DEFAULT_TTL_DAYS),
        }
    except Exception:
        logger.warning("ai_cache: nu pot citi configul — cache dezactivat", exc_info=True)
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
    with _cfg_lock:
        _cfg_cache["at"], _cfg_cache["cfg"] = now, cfg
    return cfg


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def cache_key(prefix: str, payload: dict, temperature: float, epoch: int) -> str:
    """sha256 pe payload-ul efectiv: prefix | model_hint | response_format | max_tokens |
    temperature | sha256(prompt) | sha256(transcript) | [(mime, sha256(octeți))…] | epoch.
    Serializare JSON (nu concatenare cu „|"), ca o componentă să nu se poată „prelinge" în alta."""
    atts = []
    for a in (payload.get("attachments") or []):
        data = a.get("data_base64") or ""
        try:
            raw = base64.b64decode(data, validate=False)
        except Exception:
            raw = data.encode("utf-8", "ignore")
        atts.append([str(a.get("mime_type") or ""), _sha(raw)])
    parts = [
        prefix,
        payload.get("model_hint") or "default",
        str(payload.get("response_format") or ""),
        int(payload.get("max_tokens") or 0),
        format(float(temperature), ".6g"),
        _sha(str(payload.get("prompt") or "").encode("utf-8")),
        _sha(str(payload.get("transcript") or "").encode("utf-8")),
        atts,
        int(epoch),
    ]
    return _sha(json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode("ascii"))


def prepare(task, payload: dict, temperature) -> Optional[dict]:
    """Context de cache pentru acest apel sau None (cache neaplicabil). Nu aruncă niciodată."""
    try:
        if ai_cache_bypass.get():
            return None
        if temperature is None or float(temperature) > 0:
            return None
        prefix = task_prefix(task)
        if not prefix:
            return None
        cfg = load_config()
        if not cfg["enabled"] or prefix not in cfg["prefixes"]:
            return None
        return {"prefix": prefix, "task": (str(task) if task else "")[:120],
                "key": cache_key(prefix, payload, float(temperature), cfg["epoch"]),
                "ttl_days": cfg["ttl_days"]}
    except Exception:
        logger.warning("ai_cache: pregătire eșuată — apel normal", exc_info=True)
        return None


def lookup(ctx: dict) -> Optional[dict]:
    """Rezultatul stocat (model ORIGINAL) sau None. Hit = contor + rând în ai_cache_hit_log, în
    aceeași tranzacție: dacă evidența nu se poate scrie, e miss (apel normal), nu hit nemăsurat."""
    db = None
    try:
        db = _session()
        row = db.execute(text(
            "SELECT result, original_cost_usd FROM ai_result_cache "
            " WHERE cache_key = :k AND expires_at > now()"), {"k": ctx["key"]}).fetchone()
        if not row:
            db.rollback()
            return None
        result = row[0]
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict):
            db.rollback()
            return None
        db.execute(text(
            "UPDATE ai_result_cache SET hit_count = hit_count + 1, last_hit_at = now() "
            " WHERE cache_key = :k"), {"k": ctx["key"]})
        db.execute(text(
            "INSERT INTO ai_cache_hit_log (task, task_prefix, cache_key, saved_cost_usd) "
            "VALUES (:t, :p, :k, :c)"),
            {"t": ctx["task"], "p": ctx["prefix"], "k": ctx["key"], "c": row[1]})
        db.commit()
        return result
    except Exception:
        logger.warning("ai_cache: citire eșuată (%s) — apel normal", ctx.get("prefix"), exc_info=True)
        _safe_rollback(db)
        return None
    finally:
        _safe_close(db)


def default_ok(result: dict, response_format: str) -> bool:
    """Fără validator de la apelant: ok:true cu `parsed` nenul (json) sau `text` nevid (text)."""
    if not result.get("ok"):
        return False
    if (response_format or "text").lower() == "json":
        return result.get("parsed") is not None
    return bool((result.get("text") or "").strip())


def store(ctx: dict, result: dict, response_format: str,
          cache_ok: Optional[Callable[[dict], bool]] = None) -> bool:
    """Scrie rezultatul dacă e ok:true și validat. Un rând expirat cu aceeași cheie se înlocuiește;
    unul valid rămâne (două miss-uri simultane: câștigă primul). Nu aruncă niciodată."""
    try:
        if not result.get("ok"):
            return False
        valid = cache_ok(result) if cache_ok is not None else default_ok(result, response_format)
        if not valid:
            return False
    except Exception:
        logger.warning("ai_cache: validatorul a eșuat (%s) — nu scriu", ctx.get("prefix"), exc_info=True)
        return False
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    db = None
    try:
        db = _session()
        db.execute(text(
            "INSERT INTO ai_result_cache (cache_key, task_prefix, result, original_cost_usd, "
            "  original_tokens_in, original_tokens_out, created_at, expires_at) "
            "VALUES (:k, :p, CAST(:r AS jsonb), :c, :ti, :to, now(), "
            "        now() + make_interval(days => :ttl)) "
            "ON CONFLICT (cache_key) DO UPDATE SET task_prefix = EXCLUDED.task_prefix, "
            "  result = EXCLUDED.result, original_cost_usd = EXCLUDED.original_cost_usd, "
            "  original_tokens_in = EXCLUDED.original_tokens_in, "
            "  original_tokens_out = EXCLUDED.original_tokens_out, created_at = now(), "
            "  expires_at = EXCLUDED.expires_at, hit_count = 0, last_hit_at = NULL "
            " WHERE ai_result_cache.expires_at <= now()"),
            {"k": ctx["key"], "p": ctx["prefix"], "r": json.dumps(result, default=str),
             "c": usage.get("cost_usd"), "ti": usage.get("input_tokens"),
             "to": usage.get("output_tokens"), "ttl": int(ctx["ttl_days"])})
        db.commit()
        return True
    except Exception:
        logger.warning("ai_cache: scriere eșuată (%s)", ctx.get("prefix"), exc_info=True)
        _safe_rollback(db)
        return False
    finally:
        _safe_close(db)


def purge_expired_if_due() -> Optional[int]:
    """Șterge rândurile expirate, cel mult o dată pe oră pe tot clusterul (poarta e un UPSERT
    condiționat pe settings[ai_cache.last_purge_at], atomic între workerii gunicorn).
    Doar cu cache-ul activ: cu flag-ul OFF, tick-ul rămâne identic cu azi. Nu aruncă niciodată.
    Întoarce numărul de rânduri șterse, sau None dacă nu era momentul / cache oprit / eroare."""
    try:
        if not load_config()["enabled"]:
            return None
    except Exception:
        return None
    db = None
    try:
        db = _session()
        due = db.execute(text(
            "INSERT INTO settings (key, value, description, updated_by, updated_at) "
            "VALUES (:k, to_jsonb(now()::text), 'T3-L1: ultima curățare a cache-ului AI', "
            "        'ai_cache', now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now() "
            " WHERE settings.updated_at < now() - CAST(:every AS interval) "
            "RETURNING 1"), {"k": KEY_LAST_PURGE, "every": _PURGE_EVERY}).fetchone()
        if not due:
            db.rollback()
            return None
        n = db.execute(text("DELETE FROM ai_result_cache WHERE expires_at <= now()")).rowcount
        db.commit()
        if n:
            logger.info("ai_cache: %d rânduri expirate șterse", n)
        return int(n or 0)
    except Exception:
        logger.warning("ai_cache: curățare eșuată", exc_info=True)
        _safe_rollback(db)
        return None
    finally:
        _safe_close(db)


def _safe_rollback(db) -> None:
    if db is not None:
        try:
            db.rollback()
        except Exception:
            pass


def _safe_close(db) -> None:
    if db is not None:
        try:
            db.close()
        except Exception:
            pass
