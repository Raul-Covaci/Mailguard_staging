#!/usr/bin/env python3
"""Readuce din O365 atașamentele care lipsesc pe emailuri deja ingerate.

Caz tipic: pozele .heic aruncate de filtrul vechi de content-type (fix 2026-09-18). Emailul
există deja în DB, deci sync-ul normal nu-l mai reia — atașamentele se recuperează de aici.
Implicit DRY-RUN: listează ce ar adăuga, fără scriere.

Utilizare:
  python3 scripts/o365_refetch_attachments.py --days 30            # dry-run, ultimele 30 zile
  python3 scripts/o365_refetch_attachments.py --ids 123 456        # dry-run, emailuri anume
  python3 scripts/o365_refetch_attachments.py --days 30 --apply    # scrie efectiv
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import argparse
import json

from app.services.o365_ingest import refetch_attachments


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14, help="Fereastra pe received_at (implicit 14)")
    ap.add_argument("--ids", type=int, nargs="*", help="ID-uri de email (bat --days)")
    ap.add_argument("--apply", action="store_true", help="Scrie efectiv (implicit dry-run)")
    a = ap.parse_args()
    out = refetch_attachments(days=a.days, email_ids=a.ids or None, apply=a.apply)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
