"""Fereastra de procesare a documentelor — cate zile in urma se uita drain-ul.

De ce exista: drain-ul considera „neprocesat" = „nu are randuri in `document_extractions`"
(`documents.py`, predicatul candidatilor). Curatenia nocturna sterge exact acele randuri. Fara o
limita de data, in fiecare noapte dispare dovada procesarii si TOATE atasamentele redevin candidate
— reprocesare la infinit, cu cost AI pe fiecare tura.

⚠️ Numarul de aici si retentia din `scripts/storage_cleanup.sh` (pasul 3, DELETE din
`document_extractions`) TREBUIE sa ramana EGALE. Daca retentia e mai scurta decat fereastra, un mail
aflat inca in fereastra isi pierde randurile la miezul noptii si se re-plateste a doua zi — exact
bucla pe care fereastra o repara. De aceea shell-ul citeste aceeasi setare din DB, nu o constanta
proprie.

Sursa de adevar: settings['documents.process_window'] = {"days": N}
(seed: migrations/20260826b_doc_process_window.sql). Constanta de mai jos e fallback-ul cand
setarea lipseste sau e corupta.

⛔ PLAFON DUR: `HARD_MAX_DAYS = 1` (decizie Raul Covaci, 2026-08-26). Nu se proceseaza NICIODATA
documente din mailuri mai vechi de o zi fata de azi — indiferent de setare, de scope si de actiunea
operatorului (cron, „Proceseaza tot", „Reproceseaza ID-uri", reset-reimport). Setarea poate doar
STRANGE fereastra, niciodata largi: `window_days()` intoarce min(setare, HARD_MAX_DAYS). O valoare
mai mare in DB e ignorata in tacere de plafon — asta e si rostul ei, sa nu poata fi relaxata din UI
sau dintr-un UPDATE grabit.

Consecinta asumata: daca procesarea sta mai mult de ~24h (pana de API/gateway AI, automatizare
lasata pe STOP), documentele din intervalul respectiv NU se mai proceseaza deloc, nici manual.
"""
import logging

logger = logging.getLogger(__name__)

SETTINGS_KEY = "documents.process_window"
DEFAULT_DAYS = 1
HARD_MAX_DAYS = 1      # plafon dur — vezi docstring; setarea poate doar strange, nu largi


def window_days(db=None) -> int:
    """Cate zile in urma se proceseaza: min(setare, HARD_MAX_DAYS).

    Fallback pe DEFAULT_DAYS la orice problema de citire. Rezultatul e mereu >= 1 (ziua curenta
    plus ziua precedenta), niciodata mai mare decat HARD_MAX_DAYS."""
    own = False
    if db is None:
        from app.database import SessionLocal
        db = SessionLocal()
        own = True
    try:
        from sqlalchemy import text
        v = db.execute(text("SELECT (value->>'days')::int FROM settings WHERE key=:k"),
                       {"k": SETTINGS_KEY}).scalar()
        n = int(v) if v is not None else DEFAULT_DAYS
        if n < 1:
            logger.warning("%s='%s' sub 1 — folosesc %s", SETTINGS_KEY, v, DEFAULT_DAYS)
            n = DEFAULT_DAYS
        if n > HARD_MAX_DAYS:
            logger.warning("%s='%s' peste plafonul dur — folosesc %s zile",
                           SETTINGS_KEY, v, HARD_MAX_DAYS)
            return HARD_MAX_DAYS
        return n
    except Exception:
        logger.exception("citire %s esuata — folosesc %s zile", SETTINGS_KEY, DEFAULT_DAYS)
        return min(DEFAULT_DAYS, HARD_MAX_DAYS)
    finally:
        if own:
            db.close()
