"""T2: Snapshot lunar satisfacție per client.

Logică:
- Calculează scorul lunar pentru toți clienții activi (sau un subset by client_id).
- Dacă clientul n-a avut activitate în luna curentă → carry-forward din luna precedentă.
- Idempotent: ON CONFLICT (client_id, month_key) DO NOTHING → rulare repetată = no-op.
- Procesare batch pentru volum mare de clienți.

Activitate = cel puțin un email sau un apel sau un task în intervalul lunii.
"""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import Optional

import psycopg2
import psycopg2.extras

from app.config import get_settings
from app.services import satisfaction_engine

logger = logging.getLogger("mailguard.satisfaction_snapshot")

BATCH_SIZE = 1  # flush imediat — UI vede fiecare client după IRIS

# Câți clienți se procesează în paralel. Sursa de adevăr e `settings.satisfaction.v6` →
# `max_workers`; valoarea de aici e doar plasa de siguranță când settings-ul lipsește.
#
# De ce paralel (2026-08-20): ~94% din durata snapshot-ului e latență IRIS serializată — 300 de
# clienți × ~3 apeluri × ~4s ≈ o oră, din care sleep-urile și interogările DB erau ~7%.
# Clienții sunt complet independenți (nu se mai reportează stare între ei), deci singura limită
# reală e gateway-ul IRIS, folosit în același timp și de clasificarea mailurilor și de scorarea
# apelurilor. Numărul de fire ESTE rate-limit-ul; `iris_ai` are deja retry cu backoff pe 429/5xx,
# deci pauzele fixe de dinainte au fost scoase.
DEFAULT_MAX_WORKERS = 6
MAX_WORKERS_CAP = 16

# Conexiune DB per fir: psycopg2 NU e thread-safe pe o conexiune partajată, iar motorul face
# ~11 interogări per client. Firele fac doar CITIRI; scrierea rămâne pe conexiunea principală,
# într-un singur fir, ca să nu apară contenție pe INSERT.
_tls = threading.local()
_worker_conns = []
_worker_conns_lock = threading.Lock()


def _worker_conn():
    """Conexiunea firului curent (creată la prima folosire, reutilizată apoi)."""
    conn = getattr(_tls, "conn", None)
    if conn is not None and not conn.closed:
        return conn
    s = get_settings()
    conn = psycopg2.connect(
        host=s.db_host, port=s.db_port,
        dbname=s.db_name, user=s.db_user, password=s.db_password,
    )
    # Doar citiri — autocommit ca să nu țină o tranzacție deschisă cât durează apelurile IRIS
    # (altfel 6 tranzacții „idle in transaction" de câteva minute fiecare).
    conn.autocommit = True
    _tls.conn = conn
    with _worker_conns_lock:
        _worker_conns.append(conn)
    return conn


def _close_worker_conns():
    """Închide conexiunile firelor după terminarea pool-ului."""
    with _worker_conns_lock:
        conns, _worker_conns[:] = list(_worker_conns), []
    for c in conns:
        try:
            c.close()
        except Exception:
            pass
    _tls.conn = None


def _month_interval(month_key: str):
    """Returnează (start, end) UTC pentru o lună YYYY-MM."""
    year, month = int(month_key[:4]), int(month_key[5:7])
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start, end


def _has_activity(client_id: int, iris_client_id: Optional[int], cur, start: datetime, end: datetime) -> bool:
    """True dacă clientul a avut cel puțin o interacțiune în intervalul dat.

    v4: sursa de adevăr e motorul (care mapează și interacțiunile orfane prin domeniu/telefon).
    Verificarea rapidă de mai jos prinde clienții cu client_id setat; pentru orfane (client_id NULL,
    ~jumătate din date) ne bazăm pe motorul v4 — dacă snapshot-ul rulează cu force=True verificarea
    e oricum ignorată. Fără force, un client cu DOAR interacțiuni orfane ar putea fi ratat aici, dar
    domeniul + telefonul sunt verificate în plus.
    """
    cur.execute(
        "SELECT 1 FROM emails WHERE client_id = %s AND received_at >= %s AND received_at < %s LIMIT 1",
        (client_id, start, end),
    )
    if cur.fetchone():
        return True
    try:
        cur.execute(
            "SELECT 1 FROM calls WHERE client_id = %s AND started_at >= %s AND started_at < %s LIMIT 1",
            (client_id, start, end),
        )
        if cur.fetchone():
            return True
    except Exception:
        pass
    # Mailuri orfane pe domeniul clientului (client_id NULL dar expeditor din domeniul lui)
    try:
        from app.services.satisfaction_engine import _client_email_domains
        domains = list(_client_email_domains(cur, client_id))
        if domains:
            cur.execute(
                "SELECT 1 FROM emails WHERE client_id IS NULL AND received_at >= %s AND received_at < %s "
                "AND LOWER(SPLIT_PART(from_address, '@', 2)) = ANY(%s) LIMIT 1",
                (start, end, domains),
            )
            if cur.fetchone():
                return True
    except Exception:
        pass
    if iris_client_id:
        try:
            cur.execute(
                "SELECT 1 FROM cts_task_ground_truth WHERE client_id = %s AND cts_created_at >= %s AND cts_created_at < %s LIMIT 1",
                (iris_client_id, start, end),
            )
            if cur.fetchone():
                return True
        except Exception:
            pass
    # Apeluri orfane (client_id NULL) prin ultimele 9 cifre ale numerelor clientului
    try:
        import json as _json
        from app.services import phone_match
        cur.execute("SELECT phones FROM clients WHERE id = %s", (client_id,))
        row = cur.fetchone()
        phones_raw = row[0] if row else None
        if isinstance(phones_raw, str):
            try:
                phones_raw = _json.loads(phones_raw)
            except Exception:
                phones_raw = []
        if isinstance(phones_raw, list) and phones_raw:
            suffixes = []
            for p in phones_raw:
                n = phone_match.normalize_phone(str(p))
                if n and len(n) >= 9:
                    suffixes.append(n[-9:])
            if suffixes:
                placeholders = ",".join(["%s"] * len(suffixes))
                cur.execute(
                    f"SELECT 1 FROM calls WHERE client_id IS NULL AND started_at >= %s AND started_at < %s "
                    f"AND (RIGHT(REGEXP_REPLACE(caller_number,'\\D','','g'),9) IN ({placeholders})"
                    f" OR RIGHT(REGEXP_REPLACE(callee_number,'\\D','','g'),9) IN ({placeholders})) LIMIT 1",
                    (start, end, *suffixes, *suffixes),
                )
                if cur.fetchone():
                    return True
    except Exception:
        pass
    return False


def _get_previous_snapshot(client_id: int, month_key: str, cur) -> Optional[dict]:
    """Returnează snapshot-ul din luna precedentă (sau None)."""
    year, month = int(month_key[:4]), int(month_key[5:7])
    if month == 1:
        prev_key = f"{year - 1}-12"
    else:
        prev_key = f"{year}-{month - 1:02d}"
    cur.execute(
        "SELECT satisfaction_pct, is_unsatisfied, breakdown, month_key FROM client_satisfaction_snapshots "
        "WHERE client_id = %s AND month_key = %s",
        (client_id, prev_key),
    )
    row = cur.fetchone()
    if row:
        return {"satisfaction_pct": row[0], "is_unsatisfied": row[1],
                "breakdown": row[2], "source_month_key": row[3]}
    return None


def _process_client(client_id: int, iris_client_id: Optional[int], month_key: str,
                    start: datetime, end: datetime, force: bool, computed_at: str) -> dict:
    """Calculează snapshot-ul unui client. Rulează pe firul lui, cu conexiunea lui.

    NU ridică excepții și NU scrie în DB — întoarce rândul de inserat, ca scrierea să rămână
    într-un singur fir (vezi `run_monthly_snapshot`). `kind` spune apelantului ce s-a întâmplat:
    'scored' (motorul a produs scor), 'carry' (carry-forward din luna precedentă),
    'skipped' (fără activitate și fără istoric), 'error'.
    """
    try:
        cur = _worker_conn().cursor()
        try:
            # force=True: recalculăm indiferent de activitatea calendaristică
            has_act = force or _has_activity(client_id, iris_client_id, cur, start, end)
            ai_calls = 0
            pct = None
            carry_forward = False
            source_month_key = None
            breakdown = {}
            is_unsatisfied = False
            config_used = {}

            if has_act:
                result = satisfaction_engine.compute_satisfaction_v6(
                    client_id, iris_client_id, cur, start, end)
                mode = (result.get("breakdown") or {}).get("scoring_mode") or ""
                if str(mode).startswith("v6_trajectory"):
                    ai_calls = int((result.get("breakdown") or {}).get("iris_calls") or 1)
                pct = result.get("satisfaction_pct")
                breakdown = result.get("breakdown", {})
                is_unsatisfied = result.get("is_unsatisfied", False)
                config_used = result.get("config_used", {})
                if pct is None and not (breakdown or {}).get("store_null"):
                    # Motorul a rulat, dar fără date utilizabile — încearcă carry-forward
                    has_act = False

            kind = "scored"
            if not has_act:
                prev = _get_previous_snapshot(client_id, month_key, cur)
                if prev is None:
                    return {"client_id": client_id, "kind": "skipped", "ai_calls": ai_calls, "row": None}
                pct = prev["satisfaction_pct"]
                is_unsatisfied = prev["is_unsatisfied"]
                breakdown = prev["breakdown"]
                carry_forward = True
                source_month_key = prev["source_month_key"]
                config_used = {}
                kind = "carry"

            return {
                "client_id": client_id,
                "kind": kind,
                "ai_calls": ai_calls,
                "row": {
                    "client_id": client_id,
                    "month_key": month_key,
                    "satisfaction_pct": pct,
                    "is_unsatisfied": is_unsatisfied,
                    "breakdown": json.dumps(breakdown) if breakdown else None,
                    "carry_forward": carry_forward,
                    "source_month_key": source_month_key,
                    "config_used": json.dumps(config_used) if config_used else None,
                    "computed_at": computed_at,
                },
            }
        finally:
            cur.close()
    except Exception:
        logger.exception("satisfaction_snapshot: eroare client_id=%s", client_id)
        return {"client_id": client_id, "kind": "error", "ai_calls": 0, "row": None}


def run_monthly_snapshot(
    month_key: Optional[str] = None,
    client_ids: Optional[list] = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """
    Rulează snapshot-ul lunar.

    Args:
        month_key: 'YYYY-MM'; default = luna curentă.
        client_ids: lista de IDs dacă vrei subset; None = toți clienții activi.
        dry_run: dacă True, calculează dar nu persistă.

    Returnează: {"processed": int, "skipped": int, "carry_forward": int, "errors": int, "month_key": str}
    """
    now = datetime.now(timezone.utc)
    if not month_key:
        month_key = now.strftime("%Y-%m")

    start, end = _month_interval(month_key)
    # Momentul de referință pentru recency decay = ultima zi a lunii (sau now dacă luna e curentă)
    ref_now = min(end - timedelta(seconds=1), now)

    s = get_settings()
    conn = psycopg2.connect(
        host=s.db_host, port=s.db_port,
        dbname=s.db_name, user=s.db_user, password=s.db_password,
    )
    conn.autocommit = False

    stats = {"processed": 0, "skipped": 0, "carry_forward": 0, "errors": 0, "ai_calls": 0, "month_key": month_key}

    try:
        cur = conn.cursor()

        # Construiește lista de clienți de procesat
        if client_ids:
            cur.execute(
                "SELECT id, iris_client_id FROM clients WHERE id = ANY(%s) AND is_active = TRUE AND satisfaction_exclude = FALSE ORDER BY id",
                (client_ids,),
            )
        else:
            cur.execute(
                "SELECT id, iris_client_id FROM clients WHERE is_active = TRUE AND satisfaction_exclude = FALSE ORDER BY id"
            )
        clients = cur.fetchall()
        logger.info("satisfaction_snapshot: start month=%s clients=%d dry_run=%s", month_key, len(clients), dry_run)

        # Numărul de fire vine din settings (`satisfaction.v6` → `max_workers`), ca să se
        # poată urca/coborî fără redeploy dacă gateway-ul IRIS o cere.
        try:
            _cfg = satisfaction_engine._load_v6_config(cur)
            workers = int(_cfg.get("max_workers") or DEFAULT_MAX_WORKERS)
        except Exception:
            workers = DEFAULT_MAX_WORKERS
        workers = max(1, min(MAX_WORKERS_CAP, workers))
        if dry_run:
            workers = 1     # dry-run e pentru inspecție, nu pentru viteză
        logger.info("satisfaction_snapshot: %d clienți, %d fire în paralel", len(clients), workers)

        # Clienții se procesează în paralel (citiri + IRIS), dar SCRIEREA rămâne aici, pe un
        # singur fir: se păstrează `BATCH_SIZE=1` (flush după fiecare client, ca UI-ul să vadă
        # progresul) fără contenție pe INSERT și fără tranzacții concurente.
        batch = []
        computed_at = now.isoformat()
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(_process_client, client_id, iris_client_id,
                                month_key, start, end, force, computed_at)
                    for client_id, iris_client_id in clients
                ]
                done = 0
                for fut in as_completed(futures):
                    res = fut.result()      # _process_client nu ridică niciodată
                    kind = res["kind"]
                    stats["ai_calls"] = stats.get("ai_calls", 0) + res.get("ai_calls", 0)

                    if kind == "error":
                        stats["errors"] += 1
                    elif kind == "skipped":
                        stats["skipped"] += 1
                    else:
                        if kind == "carry":
                            stats["carry_forward"] += 1
                        batch.append(res["row"])
                        stats["processed"] += 1
                        if len(batch) >= BATCH_SIZE and not dry_run:
                            _flush_batch(cur, batch, force=force)
                            conn.commit()
                            batch.clear()

                    done += 1
                    if done % 50 == 0:
                        logger.info("satisfaction_snapshot: %d/%d clienți procesați (%s)",
                                    done, len(clients), stats)
        finally:
            _close_worker_conns()

        # Flush final
        if batch and not dry_run:
            _flush_batch(cur, batch, force=force)
            conn.commit()

        logger.info("satisfaction_snapshot: done %s", stats)
    except Exception:
        conn.rollback()
        logger.exception("satisfaction_snapshot: eroare fatală")
        raise
    finally:
        conn.close()

    return stats


def _flush_batch(cur, batch: list, force: bool = False):
    """Insert batch idempotent.

    force=False (default, cron): ON CONFLICT DO NOTHING — rulare repetată = no-op.
    force=True (recompute manual): ON CONFLICT DO UPDATE — recalculează snapshot-ul
    existent al lunii cu valorile motorului curent (util după modificarea engine-ului).
    """
    conflict = (
        "ON CONFLICT (client_id, month_key) DO NOTHING"
        if not force
        else """ON CONFLICT (client_id, month_key) DO UPDATE SET
            satisfaction_pct = EXCLUDED.satisfaction_pct,
            is_unsatisfied   = EXCLUDED.is_unsatisfied,
            breakdown        = EXCLUDED.breakdown,
            carry_forward    = EXCLUDED.carry_forward,
            source_month_key = EXCLUDED.source_month_key,
            config_used      = EXCLUDED.config_used,
            computed_at      = EXCLUDED.computed_at"""
    )
    cur.executemany(
        """
        INSERT INTO client_satisfaction_snapshots
            (client_id, month_key, satisfaction_pct, is_unsatisfied, breakdown,
             carry_forward, source_month_key, config_used, computed_at)
        VALUES
            (%(client_id)s, %(month_key)s, %(satisfaction_pct)s, %(is_unsatisfied)s,
             %(breakdown)s, %(carry_forward)s, %(source_month_key)s, %(config_used)s, %(computed_at)s)
        """ + conflict,
        batch,
    )
