"""IRIS AI client — generic access to the IRIS gateway for arbitrary tasks.

Cargo360 formats prompts locally and sends them here; this module forwards them
to the IRIS gateway's external-ai/run-prompt endpoint and normalizes the result.

Verified against IRIS gateway contract (scripts/iris_gateway.py:26964):
  POST {IRIS_AI_URL}                         (default: {IRIS_API_URL}/external-ai/run-prompt)
  Auth:  Authorization: Bearer <api_key>     (validated vs apps table, enabled=true)
  Body:  { client, prompt (=system), transcript (<=50000), response_format,
           model_hint?, temperature, max_tokens }
  Resp:  { ok:true, raw_text, parsed, usage, ... } | { ok:false, error:{code,message} }

Config (env, /opt/iris-mailguard/.env):
  IRIS_API_URL   base IRIS URL (already present, e.g. https://iris.cargotrack.ro)
  IRIS_AI_URL    full run-prompt URL — overrides the derived one (optional)
  IRIS_API_KEY   bearer key (already present); IRIS_AI_KEY/NOVA_LLM_KEY override

All failures are returned as a structured dict (never raised) so callers can
degrade gracefully.
"""
import os
import time
import logging
import httpx

from app.services import ai_cache

logger = logging.getLogger("mailguard.iris_ai")

# IRIS gateway hard limit on the transcript field.
TRANSCRIPT_LIMIT = 50000
TRANSCRIPT_CAP = 48000   # stay safely under the limit
DEFAULT_TIMEOUT = 120.0
DEFAULT_CLIENT = "Cargo360"

# Retry doar pe erori tranzitorii (rețea/timeout/rate-limit/5xx) — nu pe erori de
# configurare sau request invalid, unde a re-încerca n-ar schimba rezultatul.
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = (1.0, 3.0)   # pauză înainte de încercarea #2 și #3
_RETRYABLE_HTTP_CODES = {"HTTP_429", "HTTP_500", "HTTP_502", "HTTP_503", "HTTP_504"}


def _resolve_url() -> str:
    explicit = os.getenv("IRIS_AI_URL", "").strip()
    if explicit:
        return explicit
    base = os.getenv("IRIS_API_URL", "").strip().rstrip("/")
    return f"{base}/external-ai/run-prompt" if base else ""


def _resolve_key() -> str:
    return (os.getenv("IRIS_AI_KEY", "").strip()
            or os.getenv("IRIS_API_KEY", "").strip()
            or os.getenv("NOVA_LLM_KEY", "").strip()
            or os.getenv("IRIS_LLM_KEY", "").strip())


def _ai_disabled() -> bool:
    """Kill-switch pt. mediul curent (setat in .env, NU pe gateway-ul IRIS).
    AI_DISABLED=true opreste orice apel run_prompt() inainte de retea."""
    return (os.getenv("AI_DISABLED", "") or "").strip().lower() in ("1", "true", "yes", "on")


def is_configured() -> bool:
    return bool(_resolve_url() and _resolve_key())


def status() -> dict:
    """Non-secret config snapshot for a health/diagnostic endpoint."""
    url = _resolve_url()
    return {
        "configured": is_configured(),
        "url_set": bool(url),
        "url": url or None,
        "key_set": bool(_resolve_key()),
    }


def _model_hint(raw_model):
    """Forward Anthropic model ids AND the local-LLM aliases. Gateway routes
    'gemma'/'local'/'iris-local' to the local vLLM (gemma); others → Anthropic."""
    m = (raw_model or "").strip()
    if m.lower() in ("gemma", "local", "iris-local"):
        return m.lower()
    if m.startswith("claude-") or m.startswith("us.anthropic."):
        return m
    return None


def _log_call(task, model, usage, ok, error_code, email_id=None):
    """Best-effort: record one AI call (task/model/tokens/cost/email) into ai_call_log.

    `email_id` links the call to the email it was made for (queue-status work,
    2026-06-11). Defensive: if the `email_id` column does not exist yet (migration
    20260611_queue_status.sql not applied), fall back to the legacy INSERT so AI
    calls keep working during the rollout.
    """
    # MUST NEVER RAISE — run_prompt guarantees no exception escapes. Whole body guarded.
    try:
        from app.database import SessionLocal
        from sqlalchemy import text
        u = usage or {}
        base_cols = {"t": task, "m": model, "ti": u.get("input_tokens"),
                     "to": u.get("output_tokens"), "c": u.get("cost_usd"),
                     "ok": ok, "ec": error_code}
        # Try the new schema first (with email_id), then degrade gracefully if the
        # column does not exist yet (migration not applied during rollout window).
        for with_email in (True, False):
            db = SessionLocal()
            try:
                if with_email:
                    db.execute(text(
                        "INSERT INTO ai_call_log(task, model, tokens_in, tokens_out, cost_usd, ok, error_code, email_id) "
                        "VALUES (:t, :m, :ti, :to, :c, :ok, :ec, :eid)"),
                        {**base_cols, "eid": email_id})
                else:
                    db.execute(text(
                        "INSERT INTO ai_call_log(task, model, tokens_in, tokens_out, cost_usd, ok, error_code) "
                        "VALUES (:t, :m, :ti, :to, :c, :ok, :ec)"),
                        base_cols)
                db.commit()
                return
            except Exception as e:
                db.rollback()
                # Retry without email_id only when that column is genuinely missing.
                if with_email and "email_id" in str(e).lower():
                    continue
                logger.warning("ai_call_log insert failed: %s", e)
                return
            finally:
                db.close()
    except Exception as e:
        logger.warning("ai_call_log logging skipped: %s", e)


def run_prompt(system: str, content: str, *,
               response_format: str = "text",
               model_hint: str = None,
               attachments: list = None,  # [{mime_type, data_base64}] → Vision extern (PDF/imagini)
               temperature: float = 0.0,
               max_tokens: int = 2000,
               client: str = DEFAULT_CLIENT,
               task: str = None,
               email_id: int = None,
               use_cache: bool = False,
               learn: bool = False,
               learn_scope: str = None,
               no_cache: bool = False,
               timeout: float = None,
               cache_ok=None) -> dict:
    """Run a formatted prompt through IRIS. Returns a normalized result dict:

      { "ok": bool,
        "text": str,            # raw model text ("" on failure)
        "parsed": any | None,   # parsed JSON when response_format='json'
        "usage": dict | None,
        "error": {code,message} | None,
        "task": str | None }

    Never raises — transport/HTTP/gateway errors are reported in `error`.

    `cache_ok` (T3-L1): validatorul apelantului pentru cache-ul de rezultat (`ai_cache`) — un
    rezultat ok:true intră în cache doar dacă `cache_ok(rezultat)` e True. Relevant doar când
    cache-ul e activ pentru prefixul task-ului; altfel ignorat.
    """
    if _ai_disabled():
        return {"ok": False, "text": "", "parsed": None, "usage": None, "task": task,
                "error": {"code": "AI_DISABLED", "message": "External AI calls disabled on this environment (AI_DISABLED=true in .env)"}}
    url = _resolve_url()
    key = _resolve_key()
    if not url:
        return {"ok": False, "text": "", "parsed": None, "usage": None, "task": task,
                "error": {"code": "NOT_CONFIGURED", "message": "IRIS_AI_URL / IRIS_API_URL not set"}}
    if not key:
        return {"ok": False, "text": "", "parsed": None, "usage": None, "task": task,
                "error": {"code": "NOT_CONFIGURED", "message": "IRIS_AI_KEY / IRIS_API_KEY not set"}}

    transcript = (content or "")
    truncated = len(transcript) > TRANSCRIPT_CAP
    transcript = transcript[:TRANSCRIPT_CAP]
    if not transcript.strip() and not attachments:
        return {"ok": False, "text": "", "parsed": None, "usage": None, "task": task,
                "error": {"code": "INVALID_REQUEST", "message": "content/transcript is empty"}}

    # Sonnet 5 and newer models reject the `temperature` parameter entirely.
    _no_temp_models = ("claude-sonnet-5", "claude-opus-5", "claude-fable-5")
    _skip_temperature = any((model_hint or "").startswith(m) for m in _no_temp_models)
    payload = {
        "client": client or DEFAULT_CLIENT,
        "prompt": system or "Răspunde clar și concis.",   # gateway uses this as the system prompt
        "transcript": transcript,
        "language": "ro",
        "response_format": (response_format or "text").lower(),
        "max_tokens": int(max_tokens),
    }
    if not _skip_temperature:
        payload["temperature"] = float(temperature)
    mh = _model_hint(model_hint)
    if mh:
        payload["model_hint"] = mh
    # Vision: atașamentele (PDF/imagini base64) merg DOAR pe modelul extern (gemma local e off).
    if attachments:
        payload["attachments"] = attachments
        payload["require_attachments"] = True
        if not payload.get("model_hint"):
            payload["model_hint"] = "sonnet"
    # Observabilitate: task-ul trimis la gateway e ÎNTOTDEAUNA prefixat `cargo360:<functie>`
    # (IRIS grupează cost/calitate per task; fără task-uri neprefixate, fără 'inline'). Idempotent
    # (nu dublează prefixul). Logarea LOCALĂ (_log_call) păstrează task-ul brut — reports.py
    # interoghează ai_call_log local pe nume brute (ex. task='extract').
    _t = (str(task).strip() if task else "") or "other"
    if not _t.startswith("cargo360:"):
        _t = "cargo360:" + _t
    payload["task"] = _t[:80]
    # Învățare scoped (cascade gemma→Claude): use_cache caută un răspuns deja învățat
    # pentru o întrebare ~identică; learn salvează răspunsul (gateway salvează DOAR de la
    # modelul puternic). Vezi iris_gateway.py _curated_ext_search/_curated_ext_save.
    if no_cache:
        # Gateway-ul IRIS bypass-eaza curated-cache cu skip_cache (vezi /ask, voice: skip_cache=True).
        # Trimitem ambele denumiri ca sa fim robusti indiferent de versiunea gateway-ului.
        payload["no_cache"] = True
        payload["skip_cache"] = True
    if learn_scope:
        payload["learn_scope"] = learn_scope
        if use_cache:
            payload["use_cache"] = True
        if learn:
            payload["learn"] = True

    # T3-L1: cache de rezultat, cheiat pe payload-ul de mai sus. Inert cât timp
    # settings['ai_cache.enabled'] e false; orice eșec al lui = apel normal (vezi ai_cache).
    # Ocolirea („Reidentifică") sare doar citirea; scrierea de mai jos înlocuiește intrarea veche.
    _cache = ai_cache.prepare(task, payload, temperature)
    if _cache is not None and not _cache.get("bypass"):
        _hit = ai_cache.lookup(_cache)
        if _hit is not None:
            return {**_hit, "task": task}

    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}

    last_transport_err = None
    r = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = httpx.post(url, json=payload, headers=headers,
                           timeout=(timeout if timeout and timeout > 0 else DEFAULT_TIMEOUT))
        except Exception as e:
            last_transport_err = e
            r = None
        else:
            if r.status_code == 200 or ("HTTP_" + str(r.status_code)) not in _RETRYABLE_HTTP_CODES:
                break
        if attempt < MAX_RETRIES:
            wait_s = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
            logger.warning("IRIS AI retry task=%s attempt=%d/%d wait=%.1fs (%s)",
                            task, attempt, MAX_RETRIES, wait_s,
                            last_transport_err or ("HTTP_" + str(r.status_code)))
            time.sleep(wait_s)

    if r is None:
        logger.warning("IRIS AI transport error task=%s: %s", task, last_transport_err)
        _log_call(task, None, None, False, "TRANSPORT", email_id=email_id)
        return {"ok": False, "text": "", "parsed": None, "usage": None, "task": task,
                "error": {"code": "TRANSPORT", "message": str(last_transport_err)[:200]}}

    if r.status_code != 200:
        logger.warning("IRIS AI HTTP %s task=%s: %s", r.status_code, task, r.text[:200])
        _log_call(task, None, None, False, "HTTP_" + str(r.status_code), email_id=email_id)
        return {"ok": False, "text": "", "parsed": None, "usage": None, "task": task,
                "error": {"code": "HTTP_" + str(r.status_code), "message": r.text[:200]}}

    try:
        data = r.json()
    except Exception as e:
        return {"ok": False, "text": "", "parsed": None, "usage": None, "task": task,
                "error": {"code": "BAD_JSON", "message": str(e)[:200]}}

    if isinstance(data, dict) and data.get("ok") is False:
        _log_call(task, data.get("model"), data.get("usage"), False, (data.get("error") or {}).get("code"), email_id=email_id)
        return {"ok": False, "text": (data.get("raw_text") or ""), "parsed": None,
                "usage": data.get("usage"), "model": data.get("model"), "task": task, "error": data.get("error")}

    _model = data.get("model") if isinstance(data, dict) else None
    _usage = data.get("usage") if isinstance(data, dict) else None
    _log_call(task, _model, _usage, True, None, email_id=email_id)
    result = {
        "ok": True,
        "text": (data.get("raw_text") or "") if isinstance(data, dict) else "",
        "parsed": data.get("parsed") if isinstance(data, dict) else None,
        "usage": _usage,
        "model": _model,
        "error": None,
        "task": task,
        "truncated": truncated,
    }
    if _cache is not None:
        ai_cache.store(_cache, result, payload["response_format"], cache_ok)
    return result
