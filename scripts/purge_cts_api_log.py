#!/usr/bin/env python3
"""Curata cts_api_log de apelurile de POLLING fara rezultat.

CTS interogheaza continuu `get_email_documents`/`get_emails`; marea majoritate a apelurilor nu au
ce livra, dar fiecare scria un rand. Pe productie tabela ajunsese 18 GB din 22 GB (2026-09-23) si
rupea backup-ul pre-release prin timeout (pg_dump >3h). Din 2026-09-23 apelurile goale nu se mai
scriu (`cts._log`); scriptul asta curata ISTORICUL deja acumulat si tine tabela in frau.

Se sterge DOAR: actiune de polling + total=0 + http_status=200 + mai vechi de --days.
Se pastreaza: orice apel care a livrat ceva, orice eroare (http != 200), orice alta actiune,
si tot ce e mai nou de --days (diagnostic curent).

Stergerea merge in LOTURI, cu pauza intre ele: un DELETE unic de milioane de randuri pe productie
umfla WAL-ul, tine lock lung si poate bloca aplicatia.

    python3 scripts/purge_cts_api_log.py                 # dry-run: doar raporteaza
    python3 scripts/purge_cts_api_log.py --apply         # sterge efectiv
    python3 scripts/purge_cts_api_log.py --apply --days 30 --batch 20000 --sleep 1.0

⚠ Spatiul NU se intoarce la sistemul de operare doar prin DELETE — randurile devin "dead tuples",
iar fisierul ramane la fel de mare (deci si dump-ul). Dupa purge ruleaza VACUUM (vezi --help-vacuum).
"""
import argparse
import logging
import sys
import time

sys.path.insert(0, "/opt/iris-mailguard")

from sqlalchemy import text  # noqa: E402

from app.database import SessionLocal  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("purge_cts_api_log")

POLL_ACTIONS = ("get_email_documents", "get_emails")

# Randul e gunoi daca: e polling, n-a livrat nimic, n-a fost eroare si e mai vechi de N zile.
WHERE_JUNK = """
    action = ANY(:actions)
    AND COALESCE(total, 0) = 0
    AND COALESCE(http_status, 200) = 200
    AND ts < now() - make_interval(days => :days)
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="sterge efectiv (implicit: dry-run)")
    ap.add_argument("--days", type=int, default=7,
                    help="pastreaza ultimele N zile integral (implicit 7)")
    ap.add_argument("--batch", type=int, default=20000,
                    help="randuri sterse per lot (implicit 20000)")
    ap.add_argument("--sleep", type=float, default=0.5,
                    help="pauza in secunde intre loturi (implicit 0.5)")
    ap.add_argument("--max-batches", type=int, default=100000,
                    help="plafon de siguranta la numarul de loturi")
    args = ap.parse_args()

    if args.days < 1:
        ap.error("--days trebuie sa fie >= 1 (nu stergem ziua curenta)")

    db = SessionLocal()
    params = {"actions": list(POLL_ACTIONS), "days": args.days}
    try:
        total_rows, junk_rows, size_before = db.execute(text(
            "SELECT (SELECT count(*) FROM cts_api_log), "
            "       (SELECT count(*) FROM cts_api_log WHERE " + WHERE_JUNK + "), "
            "       pg_size_pretty(pg_total_relation_size('cts_api_log'))"), params).fetchone()

        keep = total_rows - junk_rows
        pct = (junk_rows * 100.0 / total_rows) if total_rows else 0
        logger.info("cts_api_log: %s randuri, %s pe disc", f"{total_rows:,}", size_before)
        logger.info("de sters (polling gol, mai vechi de %d zile): %s (%.2f%%)",
                    args.days, f"{junk_rows:,}", pct)
        logger.info("raman: %s randuri", f"{keep:,}")

        if not junk_rows:
            logger.info("nimic de sters.")
            return 0
        if not args.apply:
            logger.info("DRY-RUN — nimic nu s-a sters. Ruleaza cu --apply pentru a sterge.")
            return 0

        deleted, batches, t0 = 0, 0, time.time()
        while batches < args.max_batches:
            n = db.execute(text(
                "WITH doomed AS (SELECT id FROM cts_api_log WHERE " + WHERE_JUNK +
                " ORDER BY id LIMIT :lim) "
                "DELETE FROM cts_api_log t USING doomed d WHERE t.id = d.id"),
                dict(params, lim=args.batch)).rowcount
            db.commit()
            if not n:
                break
            deleted += n
            batches += 1
            if batches % 10 == 0 or n < args.batch:
                logger.info("... sterse %s / %s (%d loturi, %.0fs)",
                            f"{deleted:,}", f"{junk_rows:,}", batches, time.time() - t0)
            time.sleep(args.sleep)

        size_after = db.execute(text(
            "SELECT pg_size_pretty(pg_total_relation_size('cts_api_log'))")).scalar()
        logger.info("GATA: %s randuri sterse in %d loturi, %.0fs",
                    f"{deleted:,}", batches, time.time() - t0)
        logger.info("dimensiune tabela: %s -> %s", size_before, size_after)
        logger.info("⚠ Spatiul se intoarce la OS abia dupa VACUUM. Pe o tabela mare, in fereastra "
                    "de mentenanta:  VACUUM (FULL, ANALYZE) cts_api_log;  (BLOCHEAZA tabela) "
                    "sau, fara blocare:  pg_repack -t cts_api_log")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
