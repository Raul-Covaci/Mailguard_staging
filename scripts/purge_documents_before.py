#!/usr/bin/env python3
"""Golire unica: extrageri de documente + fisiere native pentru mailuri de dinainte de o data.

De ce nu e migratie: sterge date TRANZACTIONALE. Migratiile din `migrations/` duc pe productie
doar schema si config (vezi regula din CLAUDE.md), nu stergeri de continut.

Ce face (in aceasta ordine):
  1. document_extractions pentru mailuri cu `emails.received_at < --before`, in DOUA treceri
     (intai `grouped_into IS NOT NULL`, apoi root-urile) — FK-ul `grouped_into` cere ordinea asta,
     la fel ca in scripts/storage_cleanup.sh.
  2. fisierele native ale atasamentelor acelor mailuri; randul din `attachments` RAMANE
     (storage_path intact), ca in pasul 1 din storage_cleanup.sh.

Ce NU face: nu sterge mailuri, nu atinge `cts_document_tracking` (acolo e trasabilitatea CTS) si nu
marcheaza atasamentele ca `doc_discarded` — reprocesarea e blocata de fereastra de procesare
(app/services/doc_window.py), nu de un flag per atasament.

Implicit ruleaza in DRY-RUN (doar numara). Scrie efectiv doar cu --apply.

Se ruleaza DIN directorul aplicatiei (config-ul citeste `.env` relativ la CWD):

    cd /opt/iris-mailguard
    venv/bin/python3 scripts/purge_documents_before.py                 # dry-run, 2026-08-24
    venv/bin/python3 scripts/purge_documents_before.py --apply
    venv/bin/python3 scripts/purge_documents_before.py --before 2026-08-01 --apply --no-files
"""
import os
import sys
import argparse
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# `.env` in os.environ INAINTE de importurile aplicatiei: `_host_path` citeste ATTACH_HOST_PREFIX cu
# os.getenv la momentul importului, iar pydantic-settings nu populeaza os.environ. Fara asta am
# cauta fisierele native sub prefixul implicit si n-am sterge nimic (sau, mai rau, altceva).
_ENV = os.path.join(_ROOT, ".env")
if os.path.isfile(_ENV):
    for _line in open(_ENV, encoding="utf-8"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from sqlalchemy import text                      # noqa: E402
from app.database import SessionLocal            # noqa: E402
from app.api.v1.emails import _host_path         # noqa: E402

DEFAULT_BEFORE = "2026-08-24"


def _fmt_mb(n):
    return "%.1f MB" % (n / 1048576.0)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--before", default=DEFAULT_BEFORE,
                    help="data limita (YYYY-MM-DD), exclusiva; implicit " + DEFAULT_BEFORE)
    ap.add_argument("--apply", action="store_true", help="executa; fara el ruleaza dry-run")
    ap.add_argument("--no-files", action="store_true",
                    help="nu atinge fisierele native, sterge doar randurile din DB")
    args = ap.parse_args()

    try:
        datetime.strptime(args.before, "%Y-%m-%d")
    except ValueError:
        ap.error("--before invalid, foloseste YYYY-MM-DD")

    mode = "APPLY" if args.apply else "DRY-RUN"
    print("[%s] mailuri cu received_at < %s" % (mode, args.before))

    db = SessionLocal()
    try:
        n_ext = db.execute(text(
            "SELECT count(*) FROM document_extractions d JOIN emails e ON e.id=d.email_id "
            "WHERE e.received_at < CAST(:b AS date)"), {"b": args.before}).scalar() or 0
        print("  extrageri de sters: %d" % n_ext)

        files = []
        if not args.no_files:
            rows = db.execute(text(
                "SELECT a.storage_path FROM attachments a JOIN emails e ON e.id=a.email_id "
                "WHERE e.received_at < CAST(:b AS date) AND a.storage_path IS NOT NULL"),
                {"b": args.before}).fetchall()
            seen = set()
            total = 0
            for (sp,) in rows:
                p = _host_path(sp)
                if not p or p in seen or not os.path.isfile(p):
                    continue
                seen.add(p)
                try:
                    total += os.path.getsize(p)
                except OSError:
                    pass
                files.append(p)
            print("  fisiere native de sters: %d (%s)" % (len(files), _fmt_mb(total)))

        if not args.apply:
            print("DRY-RUN — nu s-a sters nimic. Reia cu --apply.")
            return 0

        # 1) extrageri: non-root intai (FK grouped_into), apoi root
        del1 = db.execute(text(
            "DELETE FROM document_extractions d USING emails e "
            "WHERE e.id=d.email_id AND e.received_at < CAST(:b AS date) "
            "AND d.grouped_into IS NOT NULL"), {"b": args.before}).rowcount
        del2 = db.execute(text(
            "DELETE FROM document_extractions d USING emails e "
            "WHERE e.id=d.email_id AND e.received_at < CAST(:b AS date) "
            "AND d.grouped_into IS NULL"), {"b": args.before}).rowcount
        db.commit()
        print("  sterse: %d grupate + %d root = %d extrageri" % (del1, del2, del1 + del2))

        # 2) fisiere native (randul din attachments ramane)
        removed, freed, errors = 0, 0, 0
        for p in files:
            try:
                freed += os.path.getsize(p)
                os.remove(p)
                removed += 1
            except OSError as e:
                errors += 1
                print("  EROARE la %s: %s" % (p, e), file=sys.stderr)
        if files:
            print("  fisiere sterse: %d (%s), erori: %d" % (removed, _fmt_mb(freed), errors))
        print("Gata.")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
