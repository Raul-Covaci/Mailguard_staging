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
(seed: migrations/20260826b_doc_process_window.sql). Constanta de mai jos e doar fallback-ul cand
setarea lipseste sau e coruptaa.
"""
import logging

logger = logging.getLogger(__name__)

SETTINGS_KEY = "documents.process_window"
DEFAULT_DAYS = 2
MAX_DAYS = 90          # plasa de siguranta: o valoare aberanta din UI/DB nu redeschide arhiva


def window_days(db=None) -> int:
    """Cate zile in urma se proceseaza. Fallback pe DEFAULT_DAYS la orice problema de citire."""
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
        if n < 1 or n > MAX_DAYS:
            logger.warning("%s='%s' in afara intervalului 1..%s — folosesc %s",
                           SETTINGS_KEY, v, MAX_DAYS, DEFAULT_DAYS)
            return DEFAULT_DAYS
        return n
    except Exception:
        logger.exception("citire %s esuata — folosesc %s zile", SETTINGS_KEY, DEFAULT_DAYS)
        return DEFAULT_DAYS
    finally:
        if own:
            db.close()
