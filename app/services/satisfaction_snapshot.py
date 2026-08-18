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
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import psycopg2
import psycopg2.extras

from app.config import get_settings
from app.services import satisfaction_engine

logger = logging.getLogger("mailguard.satisfaction_snapshot")

BATCH_SIZE = 1  # flush imediat — UI vede fiecare client după IRIS

# Rate-limit apeluri IRIS AI: pauză între clienți ca să nu bombardăm gateway-ul
# (fiecare client cu activitate face 1 apel IRIS in compute_satisfaction_v6 / traiectorie V4).
AI_CALL_SPACING_SECONDS = 0.3


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

        batch = []
        for client_id, iris_client_id in clients:
            try:
                # force=True: recalculăm indiferent de activitatea calendaristică
                # (engine-ul folosește fereastra proprie 90 zile)
                has_act = force or _has_activity(client_id, iris_client_id, cur, start, end)

                if has_act:
                    # v4: fereastră strict calendaristică [start, end)
                    result = satisfaction_engine.compute_satisfaction_v6(client_id, iris_client_id, cur, start, end)
                    mode = (result.get("breakdown") or {}).get("scoring_mode") or ""
                    # v6_trajectory = IRIS pe săptămâni înlănțuite, fără cache (singurul motor)
                    if str(mode).startswith("v6_trajectory"):
                        n_calls = int((result.get("breakdown") or {}).get("iris_calls") or 1)
                        stats["ai_calls"] = stats.get("ai_calls", 0) + n_calls
                        # Rate-limit: pauză după fiecare client (apelurile intra-client au deja spacing în engine)
                        time.sleep(AI_CALL_SPACING_SECONDS)
                    pct = result.get("satisfaction_pct")
                    carry_forward = False
                    source_month_key = None
                    breakdown = result.get("breakdown", {})
                    is_unsatisfied = result.get("is_unsatisfied", False)
                    config_used = result.get("config_used", {})

                    if pct is None and not (breakdown or {}).get("store_null"):
                        # Motor a calculat dar fără date — încearcă carry-forward
                        has_act = False

                if not has_act:
                    prev = _get_previous_snapshot(client_id, month_key, cur)
                    if prev is None:
                        stats["skipped"] += 1
                        continue
                    pct = prev["satisfaction_pct"]
                    is_unsatisfied = prev["is_unsatisfied"]
                    breakdown = prev["breakdown"]
                    carry_forward = True
                    source_month_key = prev["source_month_key"]
                    config_used = {}
                    stats["carry_forward"] += 1

                batch.append({
                    "client_id": client_id,
                    "month_key": month_key,
                    "satisfaction_pct": pct,
                    "is_unsatisfied": is_unsatisfied,
                    "breakdown": json.dumps(breakdown) if breakdown else None,
                    "carry_forward": carry_forward,
                    "source_month_key": source_month_key,
                    "config_used": json.dumps(config_used) if config_used else None,
                    "computed_at": now.isoformat(),
                })
                stats["processed"] += 1

                # Flush batch
                if len(batch) >= BATCH_SIZE and not dry_run:
                    _flush_batch(cur, batch, force=force)
                    conn.commit()
                    batch.clear()

            except Exception:
                logger.exception("satisfaction_snapshot: eroare client_id=%s", client_id)
                stats["errors"] += 1

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
