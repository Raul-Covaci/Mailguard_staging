#!/usr/bin/env python3
"""Sincronizează prompturile de scoring apeluri din repo în tabela call_scoring_prompts.

Sursa de adevăr: app/services/prompts/calls/<key>.txt (versionate în git).
Rulează după orice deploy care modifică fișierele de prompt. Același lucru se poate
face din UI: Apeluri → Întrebări AI → „Sincronizează din repo".

Utilizare:
  python3 scripts/sync_call_prompts.py                 # sincronizează toate fișierele
  python3 scripts/sync_call_prompts.py agentScore ...  # doar cheile date
  python3 scripts/sync_call_prompts.py --dry-run       # arată diferențele, fără scriere
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import argparse

from app.services.call_scorer import sync_prompts_from_repo


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("keys", nargs="*", help="Chei de sincronizat (implicit: toate)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    res = sync_prompts_from_repo(keys=args.keys or None, dry_run=args.dry_run)
    if res.get("error"):
        print("EROARE la sincronizare — vezi logurile aplicației.", file=sys.stderr)
        return 1

    for key in res["inserted"]:
        print(f"[NOU]        {key}")
    for key in res["updated"]:
        print(f"[ACTUALIZAT] {key}")

    prefix = "DRY-RUN: " if args.dry_run else "Sincronizat: "
    print(f"\n{prefix}{len(res['inserted'])} noi, {len(res['updated'])} actualizate, "
          f"{len(res['unchanged'])} neschimbate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
