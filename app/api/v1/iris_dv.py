"""IRIS Data Views — proxy + sincronizare snapshot locală.

Toate view-urile CTS vin prin https://iris.cargotrack.ro/api/dv/*.
Cheia API se stochează în settings(key='iris_dv.api_key').
Două moduri de sincronizare, alese după `mode` declarat de view în /onboarding:
  * `snapshot`    — freshness ETag → înlocuire integrală a tabelei locale (DELETE + INSERT).
  * `incremental` — se cer DOAR rândurile schimbate de la ultima rulare (`since` = ultimul sync
                    minus o fereastră de suprapunere) și se face UPSERT pe `id`. NU se șterge
                    nimic: un view incremental nu retrimite istoricul, deci un DELETE ar goli
                    tabela la prima rulare care aduce 3 rânduri.

Paginile se scriu în DB pe măsură ce vin (`_iter_pages` + `_stream_into_table`), nu după ce
s-a adunat tot view-ul în memorie: consumul e O(o pagină), nu O(tot view-ul).

Fereastra de date: 2026-01-01 → azi, cu refresh pe ultimele 10 zile (overlap) la snapshot.
Sincronizarea automată: `iris_dv_state.auto_sync` + `auto_sync_interval_minutes`, rulate de cron
(POST /process/run-now, la 5 min) prin `app/services/iris_dv_autosync.py`.
"""
import json
import logging
import re
import time
import random
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.api.v1.auth import get_current_admin

logger = logging.getLogger(__name__)
router = APIRouter()

DV_BASE = "https://iris.cargotrack.ro/api/dv"
APP_NAME = "mailguard-staging"
SYNC_FROM = "2026-01-01"  # data de start import date CTS
# Cat de mult se suprapune fereastra ceruta la un sync incremental peste ce am luat deja.
# Acopera decalajul de ceas dintre noi si CTS si randurile scrise fix in timpul rularii
# precedente; duplicatele nu strica nimic, UPSERT-ul e idempotent pe `id`.
INCREMENTAL_OVERLAP_MINUTES = 30
DEFAULT_AUTO_SYNC_MINUTES = 60


# ─── helpers ────────────────────────────────────────────────────────────────

def _get_api_key(db: Session) -> Optional[str]:
    row = db.execute(
        text("SELECT value FROM settings WHERE key='iris_dv.api_key'")
    ).fetchone()
    if not row:
        return None
    val = row._mapping["value"]
    if isinstance(val, str):
        return val.strip('"')
    if isinstance(val, dict):
        return val.get("key")
    return None


def _dv_headers(api_key: str) -> dict:
    return {
        "X-Api-Key": api_key,
        "X-App-Name": APP_NAME,
        "Accept-Encoding": "gzip",
    }


def _require_key(db: Session) -> str:
    key = _get_api_key(db)
    if not key:
        raise HTTPException(
            status_code=403,
            detail="Cheia API IRIS Data Views nu este configurată. Adaugă-o în pagina 'Surse date'."
        )
    return key


def _update_state(db: Session, view_name: str, **kwargs):
    kwargs["updated_at"] = datetime.now(timezone.utc)
    cols = ", ".join(f"{k}=:{k}" for k in kwargs)
    db.execute(
        text(f"""
            INSERT INTO iris_dv_state (view_name, {', '.join(kwargs)})
            VALUES (:view_name, {', '.join(':' + k for k in kwargs)})
            ON CONFLICT (view_name) DO UPDATE SET {cols}
        """),
        {"view_name": view_name, **kwargs}
    )
    db.commit()


def _get_state(db: Session, view_name: str) -> dict:
    row = db.execute(
        text("SELECT * FROM iris_dv_state WHERE view_name=:v"),
        {"v": view_name}
    ).fetchone()
    if not row:
        return {}
    return dict(row._mapping)


def _http_get_with_retry(url: str, headers: dict, extra_headers: dict = None) -> httpx.Response:
    h = {**headers, **(extra_headers or {})}
    backoff = 1
    last_exc = None
    for attempt in range(5):
        try:
            resp = httpx.get(url, headers=h, timeout=30, follow_redirects=True)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "10"))
                time.sleep(retry_after)
                continue
            if resp.status_code >= 500:
                jitter = backoff * (0.8 + 0.4 * random.random())
                time.sleep(min(jitter, 60))
                backoff = min(backoff * 2, 60)
                continue
            return resp
        except Exception as exc:
            last_exc = exc
            jitter = backoff * (0.8 + 0.4 * random.random())
            time.sleep(min(jitter, 60))
            backoff = min(backoff * 2, 60)
    raise RuntimeError(f"Toate cele 5 încercări au eșuat pentru {url}: {last_exc}")


# ─── API key CRUD ────────────────────────────────────────────────────────────

@router.get("/iris-dv/config")
def get_dv_config(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Returnează dacă cheia e configurată (nu cheia în sine)."""
    key = _get_api_key(db)
    return {"configured": bool(key), "masked": ("***" + key[-4:]) if key and len(key) > 4 else None}


@router.put("/iris-dv/config")
def set_dv_config(body: dict, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Salvează cheia API. Body: {api_key: string}"""
    api_key = (body.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key lipsă")
    db.execute(
        text("""
            INSERT INTO settings(key, value) VALUES ('iris_dv.api_key', CAST(:v AS jsonb))
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
        """),
        {"v": json.dumps(api_key)}
    )
    db.commit()
    return {"ok": True}


# ─── Onboarding — lista view-uri ─────────────────────────────────────────────

@router.get("/iris-dv/views")
def list_views(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Listează view-urile disponibile de pe /onboarding + starea locală."""
    api_key = _require_key(db)
    try:
        resp = _http_get_with_retry(f"{DV_BASE}/onboarding", _dv_headers(api_key))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    if resp.status_code == 401:
        raise HTTPException(status_code=403, detail="Cheie API invalidă — verifică configurarea.")
    if resp.status_code not in (200,):
        raise HTTPException(status_code=502, detail=f"IRIS DV răspuns {resp.status_code}")

    data = resp.json()
    views = data.get("views", [])

    # enrichează cu starea locală
    for v in views:
        name = v.get("name") or v.get("view_name") or ""
        state = _get_state(db, name)
        v["local_state"] = {
            "last_sync_at": state.get("last_sync_at").isoformat() if state.get("last_sync_at") else None,
            "last_error": state.get("last_error"),
            "total_rows": state.get("total_rows"),
            "freshness_at": state.get("freshness_at").isoformat() if state.get("freshness_at") else None,
            "etag": bool(state.get("etag")),
            "mode": state.get("mode"),
            "auto_sync": bool(state.get("auto_sync")),
            "auto_sync_interval_minutes": state.get("auto_sync_interval_minutes"),
        }

    return {"views": views, "links": data.get("links", {})}


# ─── Prompt per view ──────────────────────────────────────────────────────────

@router.get("/iris-dv/views/{view_name}/prompt")
def get_view_prompt(view_name: str, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    api_key = _require_key(db)
    try:
        resp = _http_get_with_retry(f"{DV_BASE}/{view_name}/prompt", _dv_headers(api_key))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if resp.status_code == 401:
        raise HTTPException(status_code=403, detail="Cheie API invalidă.")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"View '{view_name}' inexistent sau inaccesibil.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"IRIS DV răspuns {resp.status_code}")
    return resp.json()


# ─── Freshness per view ───────────────────────────────────────────────────────

@router.get("/iris-dv/views/{view_name}/freshness")
def get_view_freshness(view_name: str, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    api_key = _require_key(db)
    state = _get_state(db, view_name)
    try:
        resp = _http_get_with_retry(
            f"{DV_BASE}/{view_name}/freshness",
            _dv_headers(api_key),
            {"If-None-Match": state.get("etag", "")} if state.get("etag") else {}
        )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if resp.status_code == 304:
        return {"fresh": True, "not_modified": True, "state": {
            "last_sync_at": state.get("last_sync_at").isoformat() if state.get("last_sync_at") else None,
            "total_rows": state.get("total_rows"),
            "freshness_at": state.get("freshness_at").isoformat() if state.get("freshness_at") else None,
        }}
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"IRIS DV răspuns {resp.status_code}")
    return {**resp.json(), "fresh": False, "not_modified": False}


# ─── Sync snapshot ────────────────────────────────────────────────────────────

_IDENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,60}$")


def _validate_view_name(view_name: str) -> str:
    """Numele de view devine identificator SQL (nume de tabelă) — un identificator
    nu poate fi trecut prin bind param, deci se validează strict la intrare."""
    if not _IDENT_RE.match(view_name or ""):
        raise HTTPException(status_code=400, detail="Nume de view invalid")
    return view_name


def _local_table_name(view_name: str) -> str:
    _validate_view_name(view_name)
    safe = view_name.replace("-", "_").replace(".", "_")
    return f"cts_dv_{safe}"


def _create_local_table_if_needed(db: Session, view_name: str, columns: list):
    tbl = _local_table_name(view_name)
    # gardă defensivă — apelantul filtrează deja, dar identificatorii nu pot fi bind params
    safe_cols = [c for c in columns if c != "id" and _IDENT_RE.match(c or "")]
    col_defs = ", ".join(f'"{c}" TEXT' for c in safe_cols)
    db.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {tbl} (
            "id" TEXT NOT NULL PRIMARY KEY,
            {col_defs}
        )
    """))
    # Drift de schemă: `CREATE TABLE IF NOT EXISTS` nu adaugă coloanele apărute în view DUPĂ
    # prima sincronizare (sau declarate într-o migrație cu mai puține coloane), iar INSERT-ul
    # de mai jos le enumeră pe toate -> sync-ul ar pica pe „column does not exist".
    for c in safe_cols:
        db.execute(text(f'ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS "{c}" TEXT'))
    db.commit()


BATCH_ROWS = 500          # cate randuri intr-un executemany (INSERT rand-cu-rand era ~10x mai lent)


def _freshness(view_name: str, api_key: str, db: Session, etag: str):
    """(response, freshness_json|None). Actualizeaza starea la eroare si arunca."""
    try:
        resp = _http_get_with_retry(
            f"{DV_BASE}/{view_name}/freshness",
            _dv_headers(api_key),
            {"If-None-Match": etag} if etag else {}
        )
    except RuntimeError as e:
        _update_state(db, view_name, last_error=str(e), last_error_at=datetime.now(timezone.utc))
        raise
    if resp.status_code == 304:
        return resp, None
    if resp.status_code != 200:
        msg = f"freshness raspuns {resp.status_code}"
        _update_state(db, view_name, last_error=msg, last_error_at=datetime.now(timezone.utc))
        raise RuntimeError(msg)
    return resp, resp.json()


PAGE_LIMIT = 10000        # cate randuri se cer per pagina
MAX_PAGES = 500           # plafon de siguranta: 500 x 10.000 = 5M randuri


def _dig(payload, *names):
    """Prima valoare nenula gasita pentru oricare din `names`, la top-level sau in
    containerele uzuale (`meta`, `pagination`, `links`, `page_info`).

    De ce: forma raspunsului nu e garantata. Varianta initiala citea DOAR top-level
    `has_more`/`next_cursor`, deci pe un raspuns care le tine sub `meta` paginarea se oprea
    silentios dupa prima pagina — exact simptomul de pe productie (10.000 randuri aduse, toate
    din 2020, restul istoricului niciodata cerut)."""
    if not isinstance(payload, dict):
        return None
    for n in names:
        if payload.get(n) is not None:
            return payload[n]
    for box in ("meta", "pagination", "links", "page_info"):
        sub = payload.get(box)
        if isinstance(sub, dict):
            for n in names:
                if sub.get(n) is not None:
                    return sub[n]
    return None


def _extract_rows(payload):
    """Randurile din raspuns, oricare din formele uzuale (lista simpla sau obiect cu
    `rows`/`data`/`items`/`results`)."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("rows", "data", "items", "results", "records"):
        val = payload.get(key)
        if isinstance(val, list):
            return val
    return []


class DVFetchError(RuntimeError):
    """Esec la aducerea paginilor din /data, cu starea de scris in `iris_dv_state`.

    De ce poarta starea in loc sa o scrie: de cand paginile se scriu pe masura ce vin,
    generatorul ruleaza in INTERIORUL tranzactiei de scriere, iar `_update_state()` face
    `commit()` — un commit de acolo ar consfinti un DELETE ramas fara INSERT-urile care il
    urmau (tabela locala golita). Apelantul face intai rollback, abia apoi scrie starea."""

    def __init__(self, message: str, **state):
        super().__init__(message)
        self.state = state


def _iter_pages(view_name: str, api_key: str, since: str, schema_ver: int = 0):
    """Generator: `(randuri_pagina, coloane, nr_pagina)` pentru fiecare pagina din /data.

    Varianta anterioara (`_fetch_pages`) aduna toate paginile intr-o lista Python
    (`all_rows.extend(rows)`) si abia apoi scria in DB. Pe `client_contact_email_log`
    (1,07M randuri = 108 pagini) asta insemna 2-4 GB in heap-ul worker-ului la FIECARE
    rulare, iar heap-ul CPython nu intoarce integral memoria la OS: RSS-ul urca in trepte
    pana la OOM killer (workeri ucisi la 5,7 / 7,5 / 7,8 / 8,8 GB, aug-sept 2026).
    Cu `yield` per pagina memoria e O(PAGE_LIMIT), indiferent de marimea view-ului — deci
    plafonul `MAX_PAGES` (5M randuri) nu mai e o garantie de OOM.

    Un singur GET per pagina: varianta veche cerea pagina de doua ori (o data prin
    _http_get_with_retry, al carui raspuns se arunca, apoi inca o data cu httpx.get) — dublu
    trafic si dublu timp pe view-urile mari."""
    headers = _dv_headers(api_key)
    if schema_ver:
        headers = {**headers, "X-DV-Schema-Version": str(schema_ver)}

    columns_seen, cursor, page_num, rows_total = None, None, 0, 0
    seen_cursors = set()
    while True:
        params = {"since": since, "limit": str(PAGE_LIMIT)}
        if cursor:
            params["cursor"] = cursor
        else:
            # Fara cursor oferit de server, avansam pe offset — altfel un view care pagineaza
            # clasic ne-ar da mereu prima pagina. Contorul tine locul lui `len(all_rows)`:
            # nu mai exista lista acumulata din care sa aflam cate randuri am luat.
            if page_num:
                params["offset"] = str(rows_total)
        try:
            resp = httpx.get(f"{DV_BASE}/{view_name}/data", params=params, headers=headers,
                             timeout=60, follow_redirects=True)
        except Exception as e:
            raise DVFetchError(str(e), last_error=str(e),
                               last_error_at=datetime.now(timezone.utc)) from e

        if resp.status_code == 410:
            # cursor expirat sau view apus
            raise DVFetchError(
                "410 Gone — re-sync necesar", etag=None, cursor_val=None,
                last_error="410 cursor expirat — re-sync complet la urmatoarea rulare")
        if resp.status_code != 200:
            msg = f"data raspuns {resp.status_code}: {resp.text[:200]}"
            raise DVFetchError(msg, last_error=msg, last_error_at=datetime.now(timezone.utc))

        payload = resp.json()
        rows = _extract_rows(payload)
        has_more = _dig(payload, "has_more", "hasMore", "more", "has_next")
        cursor = _dig(payload, "next_cursor", "nextCursor", "cursor", "next", "next_page_cursor")

        if rows and columns_seen is None:
            # numele de coloane ajung in DDL/DML ca identificatori — se filtreaza
            # o singura data aici, ca CREATE si INSERT sa ramana consistente.
            raw_cols = list(rows[0].keys())
            columns_seen = [c for c in raw_cols if _IDENT_RE.match(c or "")]
            if len(columns_seen) != len(raw_cols):
                logger.warning("iris_dv %s: coloane cu nume invalid ignorate: %s",
                               view_name, [c for c in raw_cols if c not in columns_seen])

        page_num += 1
        rows_total += len(rows)
        logger.info("iris_dv sync %s: pagina %d, %d randuri acum (has_more=%s, cursor=%s)",
                    view_name, page_num, rows_total, has_more, bool(cursor))

        yield rows, (columns_seen or ["id"]), page_num

        # Oprire. Ordinea conteaza: un `has_more` explicit False e autoritar; altfel continuam
        # cat timp pagina a venit PLINA (semnul clasic ca mai exista date) — asa nu ne mai
        # oprim la prima pagina pe un view care nu trimite metadate de paginare.
        if has_more is False:
            break
        if not rows or len(rows) < PAGE_LIMIT:
            break
        if page_num >= MAX_PAGES:
            logger.warning("iris_dv sync %s: oprit la plafonul de %d pagini (%d randuri) — "
                           "view-ul pare sa aiba mai multe date decat putem aduce intr-o rulare",
                           view_name, MAX_PAGES, rows_total)
            break
        if cursor:
            if cursor in seen_cursors:
                logger.warning("iris_dv sync %s: cursor repetat (%s) — oprit ca sa nu buclam",
                               view_name, cursor)
                break
            seen_cursors.add(cursor)


def _row_values(row: dict, columns: list) -> dict:
    return {c: (str(row[c]) if row.get(c) is not None else None) for c in columns}


def _insert_rows(db: Session, tbl: str, columns: list, rows: list, upsert: bool):
    """Scrie randurile in loturi. `upsert=False` (snapshot, dupa DELETE) ignora coliziunile;
    `upsert=True` (incremental) SUPRASCRIE randul existent — altfel o actualizare venita din
    CTS (ex. mutarea pe alt departament) nu s-ar vedea niciodata local."""
    if not rows:
        return
    cols_quoted = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join(f":{c}" for c in columns)
    if upsert:
        setters = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in columns if c != "id")
        conflict = f'DO UPDATE SET {setters}' if setters else "DO NOTHING"
    else:
        conflict = "DO NOTHING"
    stmt = text(f'INSERT INTO {tbl} ({cols_quoted}) VALUES ({placeholders}) '
                f'ON CONFLICT ("id") {conflict}')
    for i in range(0, len(rows), BATCH_ROWS):
        chunk = [_row_values(r, columns) for r in rows[i:i + BATCH_ROWS]]
        db.execute(stmt, chunk)


def _disable_idle_timeout(db: Session):
    """Scoate `idle_in_transaction_session_timeout` pe DURATA tranzactiei curente.

    De cand paginile se scriu pe masura ce vin, sesiunea sta „idle in transaction" intre doua
    pagini (cat dureaza GET-ul catre IRIS, pana la 60 s) — inainte tranzactia era deschisa doar
    cat tineau INSERT-urile. Daca serverul are timeout-ul setat (nu e implicit, dar Postgres-ul
    asta e reglat manual: `temp_file_limit`, `max_wal_size`), sesiunea ar fi omorata in mijlocul
    sync-ului si NICIUN snapshot nu s-ar mai termina vreodata.

    `SET LOCAL` = doar pentru tranzactia in curs, se anuleaza singur la COMMIT/ROLLBACK, deci nu
    scapa in pool pe alta cerere. Doar pe PostgreSQL — pe alt dialect (teste) e no-op."""
    try:
        if db.get_bind().dialect.name != "postgresql":
            return
    except Exception:
        return
    db.execute(text("SET LOCAL idle_in_transaction_session_timeout = 0"))


def _stream_into_table(db: Session, view_name: str, pages, *, upsert: bool, purge: bool):
    """Scrie paginile in tabela locala PE MASURA ce vin. -> (randuri, pagini, coloane).

    Tranzactia de scriere se deschide dupa PRIMA pagina: abia atunci se cunosc coloanele,
    deci abia atunci se poate crea/alinia tabela — iar `_create_local_table_if_needed()`
    face `commit()`, deci nu are ce cauta in interiorul ei. `purge=True` (snapshot) sterge
    continutul vechi in ACEEASI tranzactie cu inserarile, ca inlocuirea sa ramana atomica
    pentru cititori (rapoartele care citesc tabela nu vad niciodata tabela goala).

    ⚠️ Tranzactia ramane deschisa cat dureaza descarcarea (minute, pe un view de 1M randuri),
    fata de secunde inainte. E pretul asumat pentru a nu mai tine tot setul in RAM. Din
    acelasi motiv NIMIC din bucla nu are voie sa faca `commit` — vezi `DVFetchError`."""
    tbl = _local_table_name(view_name)
    rows_total, pages_done, columns = 0, 0, ["id"]
    tx = None
    try:
        for rows, cols, pages_done in pages:
            if tx is None:
                columns = cols
                _create_local_table_if_needed(db, view_name, columns)   # face commit
                tx = db.begin_nested()
                _disable_idle_timeout(db)
                if purge:
                    db.execute(text(f'DELETE FROM {tbl}'))
            _insert_rows(db, tbl, columns, rows, upsert=upsert)
            rows_total += len(rows)
        if tx is None:
            # Niciun raspuns cu randuri. La snapshot tabela tot trebuie golita — acelasi
            # comportament ca inainte de streaming (view gol => tabela locala goala).
            _create_local_table_if_needed(db, view_name, columns)
            tx = db.begin_nested()
            _disable_idle_timeout(db)
            if purge:
                db.execute(text(f'DELETE FROM {tbl}'))
        tx.commit()
    except DVFetchError as e:
        db.rollback()
        _update_state(db, view_name, **e.state)     # abia dupa rollback: _update_state comite
        raise
    except Exception:
        db.rollback()
        raise
    return rows_total, pages_done, columns


def _sync_view_snapshot(view_name: str, api_key: str, db: Session):
    """mode=snapshot — inlocuire integrala atomica a tabelei locale."""
    state = _get_state(db, view_name)
    etag = state.get("etag") or ""

    fresh_resp, fresh_data = _freshness(view_name, api_key, db, etag)
    remote_schema_ver = int(fresh_resp.headers.get("X-DV-Schema-Version", 0) or 0)
    remote_prompt_ver = int(fresh_resp.headers.get("X-DV-Prompt-Version", 0) or 0)
    if fresh_data is None:
        logger.info("iris_dv sync %s: 304 not modified, skip", view_name)
        return {"skipped": True, "reason": "not_modified"}

    new_freshness_at = fresh_data.get("view_updated_at")
    new_etag = fresh_resp.headers.get("ETag") or fresh_resp.headers.get("etag") or ""

    rows_loaded, page_num, _cols = _stream_into_table(
        db, view_name,
        _iter_pages(view_name, api_key, SYNC_FROM, remote_schema_ver),
        upsert=False, purge=True)

    _update_state(db, view_name,
        etag=new_etag,
        last_sync_at=datetime.now(timezone.utc),
        last_error=None,
        last_error_at=None,
        schema_version=remote_schema_ver,
        prompt_version=remote_prompt_ver,
        total_rows=rows_loaded,
        freshness_at=new_freshness_at,
        mode="snapshot"
    )
    db.commit()
    return {"synced": True, "mode": "snapshot", "rows_loaded": rows_loaded,
            "pages": page_num, "schema_version": remote_schema_ver}


def _since_for_incremental(state: dict) -> str:
    """De unde se cer randurile la un sync incremental: ultimul sync reusit minus overlap.
    Fara sync anterior -> SYNC_FROM (prima rulare aduce tot istoricul disponibil)."""
    last = state.get("last_sync_at")
    if not last:
        return SYNC_FROM
    try:
        lt = last if hasattr(last, "tzinfo") else datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        if lt.tzinfo is None:
            lt = lt.replace(tzinfo=timezone.utc)
    except Exception:
        return SYNC_FROM
    lt -= timedelta(minutes=INCREMENTAL_OVERLAP_MINUTES)
    return lt.strftime("%Y-%m-%dT%H:%M:%S")


def _sync_view_incremental(view_name: str, api_key: str, db: Session):
    """mode=incremental — UPSERT pe `id`, fara DELETE.

    Diferenta esentiala fata de snapshot: view-ul intoarce DOAR ce s-a schimbat de la `since`,
    deci tabela locala e acumulatorul. Un DELETE + INSERT ar pastra numai ultimul delta."""
    state = _get_state(db, view_name)
    etag = state.get("etag") or ""

    fresh_resp, fresh_data = _freshness(view_name, api_key, db, etag)
    remote_schema_ver = int(fresh_resp.headers.get("X-DV-Schema-Version", 0) or 0)
    remote_prompt_ver = int(fresh_resp.headers.get("X-DV-Prompt-Version", 0) or 0)
    if fresh_data is None:
        # Nimic nou de la ultimul ETag. Marcam rularea ca reusita ca sa nu para „blocat" in UI.
        _update_state(db, view_name, last_sync_at=datetime.now(timezone.utc),
                      last_error=None, last_error_at=None, mode="incremental")
        logger.info("iris_dv sync %s: 304 not modified, skip", view_name)
        return {"skipped": True, "reason": "not_modified", "mode": "incremental"}

    new_freshness_at = fresh_data.get("view_updated_at")
    new_etag = fresh_resp.headers.get("ETag") or fresh_resp.headers.get("etag") or ""
    since = _since_for_incremental(state)

    rows_received, page_num, _cols = _stream_into_table(
        db, view_name,
        _iter_pages(view_name, api_key, since, remote_schema_ver),
        upsert=True, purge=False)

    tbl = _local_table_name(view_name)

    try:
        total_local = int(db.execute(text(f'SELECT count(*) FROM {tbl}')).scalar() or 0)
    except Exception:
        total_local = None

    _update_state(db, view_name,
        etag=new_etag,
        last_sync_at=datetime.now(timezone.utc),
        last_error=None,
        last_error_at=None,
        schema_version=remote_schema_ver,
        prompt_version=remote_prompt_ver,
        total_rows=total_local if total_local is not None else rows_received,
        freshness_at=new_freshness_at,
        mode="incremental"
    )
    db.commit()
    return {"synced": True, "mode": "incremental", "rows_received": rows_received,
            "rows_total_local": total_local, "since": since, "pages": page_num,
            "schema_version": remote_schema_ver}


def _remote_mode(view_name: str, api_key: str) -> Optional[str]:
    """`mode` declarat de view in /onboarding (snapshot | incremental | query). None la esec."""
    try:
        resp = _http_get_with_retry(f"{DV_BASE}/onboarding", _dv_headers(api_key))
        if resp.status_code != 200:
            return None
        for v in (resp.json().get("views") or []):
            if (v.get("name") or v.get("view_name")) == view_name:
                m = (v.get("mode") or "").strip().lower()
                return m or None
    except Exception as e:
        logger.info("iris_dv _remote_mode(%s): %s", view_name, e)
    return None


def sync_view(view_name: str, api_key: str, db: Session, mode: Optional[str] = None):
    """Sincronizeaza un view in modul potrivit. Ordinea de rezolvare a modului:
    argument explicit -> ce a scris ultima rulare in `iris_dv_state.mode` -> /onboarding ->
    'snapshot' (implicit istoric). Modul rezolvat se salveaza, deci /onboarding se interogheaza
    o singura data per view."""
    _validate_view_name(view_name)
    m = (mode or "").strip().lower() or None
    if not m:
        m = (_get_state(db, view_name).get("mode") or "").strip().lower() or None
    if not m:
        m = _remote_mode(view_name, api_key)
    if m == "incremental":
        return _sync_view_incremental(view_name, api_key, db)
    return _sync_view_snapshot(view_name, api_key, db)


@router.post("/iris-dv/views/{view_name}/sync")
def trigger_sync(view_name: str, background_tasks: BackgroundTasks,
                 mode: str = "",
                 db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Declanșează sincronizarea unui view, în modul declarat de el (snapshot | incremental).
    `mode` forțează modul (depanare); implicit se rezolvă automat — vezi `sync_view`."""
    _validate_view_name(view_name)
    api_key = _require_key(db)

    def _run():
        db2 = next(get_db())
        try:
            sync_view(view_name, api_key, db2, mode=mode or None)
            # După sync vacation_request, populează employee_schedule (vacation_approved)
            if view_name == "employee_vacation_request":
                try:
                    from app.services.iris_employee_sync import sync_vacation_from_dv
                    n = sync_vacation_from_dv(db2)
                    logger.info("iris_dv post-sync vacation_approved: %d rows written", n)
                except Exception as ve:
                    logger.warning("post-sync vacation_from_dv failed: %s", ve)
        except Exception as e:
            logger.error("iris_dv sync %s failed: %s", view_name, e)
        finally:
            db2.close()

    background_tasks.add_task(_run)
    return {"ok": True, "message": f"Sincronizare pornită pentru {view_name}"}


@router.get("/iris-dv/views/{view_name}/sync-status")
def get_sync_status(view_name: str, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Returnează starea curentă a sincronizării."""
    state = _get_state(db, view_name)
    return {
        "view_name": view_name,
        "last_sync_at": state.get("last_sync_at").isoformat() if state.get("last_sync_at") else None,
        "last_error": state.get("last_error"),
        "total_rows": state.get("total_rows"),
        "freshness_at": state.get("freshness_at").isoformat() if state.get("freshness_at") else None,
        "schema_version": state.get("schema_version"),
        "etag_present": bool(state.get("etag")),
        "mode": state.get("mode"),
        "auto_sync": bool(state.get("auto_sync")),
        "auto_sync_interval_minutes": state.get("auto_sync_interval_minutes") or DEFAULT_AUTO_SYNC_MINUTES,
    }


@router.put("/iris-dv/views/{view_name}/auto-sync")
def set_auto_sync(view_name: str, body: dict,
                  db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Pornește/oprește sincronizarea automată a unui view. Body: {enabled, interval_minutes}.

    Rularea efectivă o face cronul (POST /process/run-now, la 5 min) prin
    `app/services/iris_dv_autosync.py` — aici doar se scrie intenția în `iris_dv_state`.
    Intervalul se rotunjeste in sus la cadenta cronului: sub 5 minute nu are ce sa insemne."""
    _validate_view_name(view_name)
    enabled = bool(body.get("enabled"))
    try:
        interval = int(body.get("interval_minutes") or DEFAULT_AUTO_SYNC_MINUTES)
    except (TypeError, ValueError):
        interval = DEFAULT_AUTO_SYNC_MINUTES
    interval = max(5, min(interval, 1440))
    db.execute(text("""
        INSERT INTO iris_dv_state (view_name, auto_sync, auto_sync_interval_minutes, updated_at)
        VALUES (:v, :e, :i, NOW())
        ON CONFLICT (view_name) DO UPDATE
           SET auto_sync = EXCLUDED.auto_sync,
               auto_sync_interval_minutes = EXCLUDED.auto_sync_interval_minutes,
               updated_at = NOW()
    """), {"v": view_name, "e": enabled, "i": interval})
    db.commit()
    return {"ok": True, "view_name": view_name, "auto_sync": enabled,
            "auto_sync_interval_minutes": interval}


@router.get("/iris-dv/states")
def get_all_states(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Returnează starea tuturor view-urilor sincronizate local."""
    rows = db.execute(text("SELECT * FROM iris_dv_state ORDER BY view_name")).fetchall()
    result = []
    for r in rows:
        d = dict(r._mapping)
        for k in ("last_sync_at", "last_error_at", "freshness_at", "created_at", "updated_at"):
            if d.get(k):
                d[k] = d[k].isoformat()
        d["etag_present"] = bool(d.pop("etag", None))
        result.append(d)
    return result
