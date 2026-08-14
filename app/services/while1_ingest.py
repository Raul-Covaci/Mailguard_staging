"""Ingestie apeluri din While1 (telefonie, cargo.while1.biz) -> mailguard.calls.

STARE: contract CONFIRMAT de Razvan (2026-07-01), pe baza integrarii existente call-analytics.
Token dedicat Cargo360 încă în AȘTEPTARE — While1 emite tokenuri la nivel de cont (Razvan a
refuzat, corect, să reutilizeze cheia call-analytics; least-privilege înseamnă token propriu,
cerut separat de la While1/vendor). Până la primirea unui WHILE1_API_TOKEN, is_configured()
e False și sync_run() rămâne no-op sigur.

Contract confirmat:
  - Listare: POST {WHILE1_API_URL}/api/cdr, body {"page": n, "filters": {...}}, header
    Authorization: Bearer <token>. POLLING (NU webhook) — call-analytics interoghează la 300s.
    filters.from_id = cursor incremental (id > from_id); filters.date_between = [start, end]
    ("YYYY-MM-DD HH:MM:SS") pentru backfill istoric (folosit doar la bootstrap, fără cursor).
    Răspuns: {"results": [...], "pagination": {"current_page", "max_page"}, "has_error", "messages"}.
  - Câmpuri per apel: id (PK While1, int), uniqueid (PBX Asterisk, string), linkedid,
    time/time_end, caller_id, source, destination, direction ('IN'|'OUT'), user_fullname
    (numele agentului — NU o extensie), call_status (ANSWERED/NOANSWER/BUSY/FAILED),
    duration ("HH:MM:SS"), ring_time, bill_minutes, recording_url, monitor_urls, monitor_files,
    external_id, project_alias. IMPORTANT (descoperit pe apelul #342, 2026-07-02): `duration`
    include timpul de sonerie/setup — NU se potrivește cu lungimea reală a înregistrării mp3.
    Câmpul care se potrivește exact cu fișierul audio descărcat e `bill_duration` ("HH:MM:SS"),
    prezent în răspunsul brut deși nemenționat explicit de Razvan. Folosim bill_duration pentru
    calls.duration_seconds (fallback pe duration dacă bill_duration lipsește).
  - Legătură viitoare cu CTS (endpoint /cts/calls, urmează separat): uniqueid (While1) ==
    ctk_uniqueid (CTS); id (While1) == calltrack_id (CTS). Stocăm ambele identificatori acum
    (calls.call_id = str(id), calls.while1_uniqueid = uniqueid) ca să nu fie nevoie de backfill
    când vine endpointul CTS.
  - Audio: vezi app/services/call_audio.py — recording_ref (recording_url sau fallback prima
    linie din monitor_urls) e capturat aici la ingest.
  - Rate-limit: nedocumentat de While1; comportament conservator mirror call-analytics (poll la
    5 min prin cron-ul existent, MAX_PAGES_PER_RUN mic — restul se prinde la următorul tick).
  - Limbă: RO (hardcodat și de call-analytics) — deja setat implicit în iris_transcribe.transcribe().

Cursor: settings key 'while1_last_id' (ultimul While1 `id` numeric procesat, folosit ca
filters.from_id). La bootstrap (fără cursor încă), backfill ultimele 24h prin filters.date_between.

Idempotență: UNIQUE(calls.call_id) + ON CONFLICT DO NOTHING.
"""
import os
import time
import hashlib
import logging
import urllib.parse as _up
import datetime as _dt
from typing import Optional

import httpx
from sqlalchemy import text
from app.database import SessionLocal

logger = logging.getLogger("mailguard.while1")

CURSOR_KEY = "while1_last_id"
DEFAULT_TIMEOUT = 30.0   # confirmat Razvan: call-analytics foloseste timeout 30s la CDR
MAX_PAGES_PER_RUN = 5    # plafon siguranta per ciclu cron (5 min) — restul se prinde la tick-ul urmator

_NO_RECORDING_STATUSES = {"NOANSWER", "BUSY", "FAILED"}


def _cfg():
    return {
        "url": os.getenv("WHILE1_API_URL", "https://cargo.while1.biz").strip().rstrip("/"),
        "token": os.getenv("WHILE1_API_TOKEN", "").strip(),
        "key": os.getenv("WHILE1_API_KEY", "").strip(),
    }


def is_configured() -> bool:
    c = _cfg()
    return bool(c["url"] and c["token"])


def _setting_get(k) -> Optional[str]:
    db = SessionLocal()
    try:
        r = db.execute(text("SELECT value FROM settings WHERE key=:k"), {"k": k}).fetchone()
        if not r or r[0] is None:
            return None
        v = r[0]
        return v.strip('"') if isinstance(v, str) else v
    except Exception:
        return None
    finally:
        db.close()


def _setting_set(k, v):
    db = SessionLocal()
    try:
        db.execute(text(
            "INSERT INTO settings(key, value) VALUES(:k, to_jsonb(CAST(:v AS text))) "
            "ON CONFLICT(key) DO UPDATE SET value=to_jsonb(CAST(:v AS text))"), {"k": k, "v": v})
        db.commit()
    finally:
        db.close()


def _php_build_query(d, prefix=''):
    """Replică http_build_query (PHP) — identic cu call-analytics, pt calculul api_hash."""
    parts = []
    for k, v in d.items():
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, dict):
            r = _php_build_query(v, key)
            if r:
                parts.append(r)
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    parts.append(_php_build_query(item, f"{key}[{i}]"))
                else:
                    parts.append(_up.quote_plus(f"{key}[{i}]") + '=' + _up.quote_plus(str(item)))
        elif v is None:
            continue
        else:
            parts.append(_up.quote_plus(key) + '=' + _up.quote_plus(str(v)))
    return '&'.join(parts)


def _fetch_cdr_page(c, page: int, since_id=None, date_from=None, date_to=None):
    """POST /api/cdr — contract confirmat de Razvan. Returnează (results, pagination).
    api_hash = MD5(php_build_query(payload) + WHILE1_API_KEY) — cerut de While1 (identic call-analytics)."""
    filters = {}
    if since_id is not None:
        filters["from_id"] = since_id
    if date_from and date_to:
        filters["date_between"] = [date_from, date_to]
    body = {"page": page, "filters": filters}
    if c.get("key"):
        body["api_hash"] = hashlib.md5((_php_build_query(body) + c["key"]).encode()).hexdigest()
    headers = {"Authorization": "Bearer " + c["token"]}
    r = httpx.post(c["url"] + "/api/cdr", json=body, headers=headers, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if data.get("has_error"):
        raise RuntimeError("While1 CDR error: %s" % data.get("messages"))
    return data.get("results") or [], (data.get("pagination") or {})


def _parse_duration(v) -> Optional[int]:
    """duration vine ca 'HH:MM:SS' (confirmat) -> secunde."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(v)
    try:
        parts = [int(x) for x in str(v).split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        h, m, s = parts[-3:]
        return h * 3600 + m * 60 + s
    except Exception:
        return None


def _recording_ref(rec: dict) -> Optional[str]:
    """recording_url preferat; fallback prima linie din monitor_urls (multi-leg, separate prin
    newline) — confirmat de Razvan."""
    url = (rec.get("recording_url") or "").strip()
    if url:
        return url
    monitor = (rec.get("monitor_urls") or "").strip()
    if monitor:
        first = monitor.split("\n")[0].strip()
        if first:
            return first
    return None


def _rec_id_int(rec: dict) -> Optional[int]:
    rid = rec.get("id")
    if isinstance(rid, int):
        return rid
    if isinstance(rid, str) and rid.strip().isdigit():
        return int(rid.strip())
    return None


def _match_client_phone(db, phone: str) -> Optional[int]:
    """Caută clientul după număr de telefon, pe cheia canonică (ultimele 9 cifre).

    Varianta anterioară enumera doar `0xxxxxxxxx ↔ +40xxxxxxxxx`, deci rata numerele
    cu prefix `00` (MD: clients.phones='0037368295882' vs calls='+37368533883').
    """
    from app.services.phone_match import phone_key
    key = phone_key(phone)
    if not key:
        return None
    rows = db.execute(text("""
        SELECT DISTINCT k.client_id
        FROM client_phone_keys k
        JOIN clients c ON c.id = k.client_id AND c.is_active = TRUE
        WHERE k.phone_key = :k
        LIMIT 2
    """), {"k": key}).fetchall()
    return rows[0][0] if len(rows) == 1 else None


def _insert_call(db, rec: dict) -> Optional[int]:
    """Insert idempotent pe call_id (= While1 `id`, viitor calltrack_id în CTS)."""
    call_id = str(rec.get("id") or "").strip()
    if not call_id:
        return None
    direction_raw = (rec.get("direction") or "").strip().upper()
    direction = {"IN": "inbound", "OUT": "outbound"}.get(direction_raw)
    call_status = (rec.get("call_status") or "").strip().upper() or None
    ref = _recording_ref(rec)
    audio_status = "no_recording" if (not ref or call_status in _NO_RECORDING_STATUSES) else "pending"
    caller = rec.get("source") or rec.get("caller_id")
    callee = rec.get("destination")
    # Match client prin telefon la ingestie (inbound = caller, outbound = callee)
    client_phone = caller if direction == "inbound" else callee
    client_id = _match_client_phone(db, client_phone)
    row = db.execute(text("""
        INSERT INTO calls(call_id, while1_uniqueid, direction, caller_number, callee_number,
            agent_extension, call_status, started_at, duration_seconds, ring_seconds,
            recording_ref, audio_status, client_id)
        VALUES(:cid, :uid, :dir, :caller, :callee, :agent, :cstatus, :started, :dur, :ring,
            :ref, :astatus, :clid)
        ON CONFLICT (call_id) DO UPDATE SET
            client_id = COALESCE(calls.client_id, EXCLUDED.client_id),
            -- `ring_seconds` a fost adaugat dupa ce ingestul rula deja (20260813c), deci randurile
            -- vechi il au NULL. COALESCE il completeaza la o re-interogare a aceleiasi perioade,
            -- fara sa suprascrie o valoare deja cunoscuta -- restul campurilor raman neatinse,
            -- ca pana acum (un CDR nu se rescrie retroactiv).
            ring_seconds = COALESCE(calls.ring_seconds, EXCLUDED.ring_seconds)
        RETURNING id
    """), {
        "cid": call_id,
        "uid": rec.get("uniqueid"),
        "dir": direction,
        "caller": caller,
        "callee": callee,
        "agent": rec.get("user_fullname"),
        "cstatus": call_status,
        "started": rec.get("time"),
        "dur": _parse_duration(rec.get("bill_duration") or rec.get("duration")),
        # Timpul pana la raspuns = SLA-ul de apel din productivitate. While1 il trimite ca
        # `ring_time`; formatul nu e documentat explicit, dar `_parse_duration` accepta si
        # 'HH:MM:SS' si secunde brute, deci acopera ambele variante.
        "ring": _parse_duration(rec.get("ring_time") or rec.get("ring_seconds")),
        "ref": ref,
        "astatus": audio_status,
        "clid": client_id,
    }).fetchone()
    return row[0] if row else None


def sync_run(limit: int = 200) -> dict:
    """Trigger ingestie While1 -> calls. Apelat din /api/v1/sync/run-now (cron 5 min).
    No-op sigur dacă While1 nu e configurat încă (token în așteptare)."""
    c = _cfg()
    if not is_configured():
        return {"ok": False, "skipped": "while1_not_configured"}

    last_id_raw = _setting_get(CURSOR_KEY)
    since_id = int(last_id_raw) if last_id_raw and str(last_id_raw).strip().isdigit() else None
    date_from = date_to = None
    if since_id is None:
        now = _dt.datetime.utcnow()
        date_from = (now - _dt.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        date_to = now.strftime("%Y-%m-%d %H:%M:%S")

    t0 = time.time()
    inserted, fetched, page = 0, 0, 1
    max_id = since_id or 0
    db = SessionLocal()
    try:
        while page <= MAX_PAGES_PER_RUN and fetched < limit:
            try:
                results, pagination = _fetch_cdr_page(
                    c, page, since_id=since_id, date_from=date_from, date_to=date_to)
            except Exception as e:
                logger.warning("while1 fetch fail (page %s): %s", page, str(e)[:200])
                break
            if not results:
                break
            fetched += len(results)
            for rec in results:
                try:
                    cid = _insert_call(db, rec)
                    if cid:
                        inserted += 1
                        db.commit()
                    rid = _rec_id_int(rec)
                    if rid is not None and rid > max_id:
                        max_id = rid
                except Exception as e:
                    db.rollback()
                    logger.warning("while1 insert one fail: %s", str(e)[:200])
            max_page = pagination.get("max_page") or 1
            cur_page = pagination.get("current_page") or page
            if cur_page >= max_page:
                break
            page += 1
        if max_id:
            _setting_set(CURSOR_KEY, str(max_id))
    finally:
        db.close()

    out = {"ok": True, "fetched": fetched, "inserted": inserted, "pages": page,
           "ms": int((time.time() - t0) * 1000)}
    logger.info("while1 sync: %s", out)
    return out


def backfill_ring_seconds(date_from: str, date_to: str, max_pages: int = 400) -> dict:
    """Completeaza `calls.ring_seconds` pe un interval deja ingerat, re-interogand While1.

    De ce e nevoie: coloana a aparut in migrarea 20260813c, dupa ce ingestul rula de luni de zile,
    iar cursorul obisnuit (`filters.from_id`) merge doar inainte -- randurile vechi nu mai sint
    atinse niciodata. Fara timpul de raspuns, canalul "Apeluri" din productivitate ramane
    nemasurabil: pe august 2026, 2139 de apeluri PRIMITE si raspunse aveau ring_seconds NULL.

    Se refoloseste `_insert_call`, deci se aplica exact acelasi ON CONFLICT ca la ingestul normal:
    completeaza `ring_seconds` doar unde e NULL si nu rescrie restul campurilor. Rularea e
    idempotenta si se poate relua oricand.

    NU atinge cursorul incremental (`while1_last_id`) -- un backfill pe iunie nu trebuie sa faca
    sync-ul curent sa reia totul de acolo.

    date_from / date_to: 'YYYY-MM-DD HH:MM:SS' (formatul cerut de filters.date_between).
    """
    c = _cfg()
    if not is_configured():
        return {"ok": False, "skipped": "while1_not_configured"}

    t0 = time.time()
    fetched = touched = page = 0
    db = SessionLocal()
    try:
        page = 1
        while page <= max_pages:
            try:
                results, pagination = _fetch_cdr_page(c, page, date_from=date_from, date_to=date_to)
            except Exception as e:
                logger.warning("while1 backfill fetch fail (page %s): %s", page, str(e)[:200])
                break
            if not results:
                break
            fetched += len(results)
            for rec in results:
                try:
                    if _insert_call(db, rec):
                        touched += 1
                        db.commit()
                except Exception as e:
                    db.rollback()
                    logger.warning("while1 backfill one fail: %s", str(e)[:200])
            max_page = pagination.get("max_page") or 1
            if (pagination.get("current_page") or page) >= max_page:
                break
            page += 1
    finally:
        db.close()

    out = {"ok": True, "from": date_from, "to": date_to, "fetched": fetched,
           "touched": touched, "pages": page, "ms": int((time.time() - t0) * 1000)}
    logger.info("while1 backfill ring_seconds: %s", out)
    return out
