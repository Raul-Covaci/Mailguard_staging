#!/usr/bin/env python
"""Completeaza `calls.ring_seconds` pe apelurile deja ingerate, re-interogand While1.

De rulat pe serverul unde While1 e configurat (staging/productie), din radacina aplicatiei:

    ./venv/bin/python scripts/backfill_call_ring_seconds.py 2026-08-01 2026-08-31
    ./venv/bin/python scripts/backfill_call_ring_seconds.py 2026-08-01 2026-08-31 --dry-run

Fara argumente: luna curenta.

Ce face: pentru fiecare zi din interval interogheaza CDR-ul While1 (`filters.date_between`) si
reaplica upsert-ul de ingestie. `ON CONFLICT` completeaza doar `ring_seconds` acolo unde e NULL
si `client_id` acolo unde lipseste -- restul campurilor raman neatinse, deci un CDR deja ingerat
nu se rescrie. Idempotent: se poate relua de cate ori e nevoie.

Se merge ZI CU ZI, nu pe tot intervalul deodata: paginarea While1 e limitata, iar o luna intreaga
ar depasi numarul de pagini inainte sa acopere toate apelurile.

NU atinge cursorul incremental `while1_last_id`, deci sync-ul normal nu e afectat.
"""
import sys
import datetime as _dt

sys.path.insert(0, ".")

from app.database import SessionLocal          # noqa: E402
from sqlalchemy import text                    # noqa: E402
from app.services import while1_ingest as w1   # noqa: E402


def _count_missing(db, d_from: _dt.date, d_to: _dt.date) -> tuple:
    row = db.execute(text("""
        SELECT COUNT(*) FILTER (WHERE call_status = 'ANSWERED'),
               COUNT(*) FILTER (WHERE call_status = 'ANSWERED' AND ring_seconds IS NULL)
        FROM calls
        WHERE direction = 'inbound'
          AND started_at >= :df AND started_at < (CAST(:dt AS date) + 1)
    """), {"df": d_from, "dt": d_to}).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv

    today = _dt.date.today()
    if len(args) >= 2:
        d_from = _dt.date.fromisoformat(args[0])
        d_to = _dt.date.fromisoformat(args[1])
    else:
        d_from = today.replace(day=1)
        d_to = today
    if d_to < d_from:
        print("Interval invalid: data de final e inaintea celei de start.")
        return 2

    db = SessionLocal()
    try:
        # Raportul se poate cere oriunde (inclusiv pe o baza locala fara token While1), ca sa se
        # vada dimensiunea golului inainte de a rula ceva pe server.
        ans, missing = _count_missing(db, d_from, d_to)
        print(f"Interval {d_from} .. {d_to}")
        print(f"  apeluri primite si raspunse : {ans}")
        print(f"  fara timp de raspuns        : {missing}")
        if dry:
            print("  --dry-run: nu interoghez While1.")
            return 0
        if missing == 0:
            print("  nimic de completat.")
            return 0
        if not w1.is_configured():
            print("  While1 nu e configurat (WHILE1_API_URL / WHILE1_API_TOKEN) —")
            print("  ruleaza scriptul pe serverul unde exista tokenul.")
            return 1

        total_fetched = total_touched = 0
        day = d_from
        while day <= d_to:
            res = w1.backfill_ring_seconds(
                day.strftime("%Y-%m-%d 00:00:00"),
                day.strftime("%Y-%m-%d 23:59:59"),
            )
            if not res.get("ok"):
                print(f"  {day}: OPRIT — {res}")
                return 1
            total_fetched += res.get("fetched", 0)
            total_touched += res.get("touched", 0)
            print(f"  {day}: {res.get('fetched', 0):5} CDR-uri, {res.get('pages', 0)} pagini")
            day += _dt.timedelta(days=1)

        ans2, missing2 = _count_missing(db, d_from, d_to)
        print(f"\nGata. CDR-uri citite: {total_fetched}, randuri atinse: {total_touched}")
        print(f"  fara timp de raspuns: {missing} -> {missing2}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
