"""CTS auto-solved — reguli deterministe pentru mailuri automate care pot pleca
direct marcate SOLVED spre CTS (feed: campul `mark_as_solved`).

O regula = {senders: [...], subject_contains: [...]}. Se potriveste daca expeditorul e
in `senders` (lowercase; match exact pe adresa SAU pe domeniu cu cheia '@domeniu') SI
(subject_contains gol => orice subiect; altfel vreun substring se regaseste in subiect,
case-insensitive). Built-in + override din settings['cts.auto_solved_rules'] (fail-safe la built-in).

Folosit de feed-ul CTS (get_emails -> mark_as_solved per mail) si de ack (update_emails ->
persistare emails.cts_mark_solved pe ce a plecat efectiv ca solved). `matches` e o functie PURA
(fara DB): incarci regulile o data per request cu `load_rules(db)`, apoi le aplici pe fiecare rand.
"""
import logging
from typing import Optional, List, Dict, Any

from sqlalchemy import text
from app.database import SessionLocal

logger = logging.getLogger("mailguard.cts_auto_solved")

_RULES_KEY = "cts.auto_solved_rules"

# Reguli built-in (fail-safe in cod; oglindesc seed-ul din migratia 20260630_cts_mark_solved.sql).
# Editabile din DB FARA deploy prin settings['cts.auto_solved_rules']. [] in DB => dezactivat.
_DEFAULT_RULES: List[Dict[str, Any]] = [
    {"senders": ["noreply@itsbulgaria.com"], "subject_contains": ["Daily summary for toll products for"]},
    # Urban & Asociatii: confirmarile de inregistrare in arhiva (numar dosar/CUI/debitor difera
    # de fiecare data). Substring fara initiala ca sa prinda si "Înregistrare" cu diacritice.
    {"senders": ["secretariat@urbansiasociatii.ro"], "subject_contains": ["nregistrare"]},
    {"senders": ["noreply@hu-go.hu"], "subject_contains": ["Vélelmezett jogosulatlan úthasználat miatti riasztás"]},
    {"senders": ["support@expert-erp.net"], "subject_contains": []},
    {"senders": ["notificari@euplatesc.ro", "mis.batch@btrl.ro", "notificari@europayment.services",
                 "decontari@europayment.services"],
     "subject_contains": ["Tranzactii zilnice", "Tranzactii ecomm", "Decontari EuPlatesc -", "Factura EuPlatesc -"]},
]


def _normalize(rules) -> List[Dict[str, Any]]:
    """Curata o lista de reguli: senders/subject_contains -> lowercase+strip, drop intrari invalide.
    O regula fara niciun expeditor e ignorata (altfel ar marca orice — periculos)."""
    out: List[Dict[str, Any]] = []
    if not isinstance(rules, list):
        return out
    for r in rules:
        if not isinstance(r, dict):
            continue
        senders = [str(s).strip().lower() for s in (r.get("senders") or []) if str(s).strip()]
        subs = [str(s).strip().lower() for s in (r.get("subject_contains") or []) if str(s).strip()]
        if not senders:
            continue
        out.append({"senders": senders, "subject_contains": subs})
    return out


def load_rules(db=None) -> List[Dict[str, Any]]:
    """Reguli efective: settings['cts.auto_solved_rules'] daca e lista valida, altfel built-in.
    Lista explicit goala [] in DB = kill-switch (respectat). Fail-safe: orice eroare -> built-in."""
    own = db is None
    try:
        if own:
            db = SessionLocal()
        row = db.execute(text("SELECT value FROM settings WHERE key=:k"), {"k": _RULES_KEY}).fetchone()
        if row is not None and isinstance(row[0], list):
            norm = _normalize(row[0])
            if norm or row[0] == []:   # config valida (inclusiv kill-switch []) -> o folosim
                return norm
    except Exception as e:
        logger.warning("load_rules: citire settings esuata, folosesc built-in: %s", e)
    finally:
        if own and db is not None:
            try:
                db.close()
            except Exception:
                pass
    return _normalize(_DEFAULT_RULES)


def _sender_match(fa: str, senders: List[str]) -> bool:
    if fa in senders:
        return True
    if "@" in fa:
        return ("@" + fa.split("@", 1)[1]) in senders
    return False


def matches(from_address: Optional[str], subject: Optional[str], rules: List[Dict[str, Any]]) -> bool:
    """True daca (from_address, subject) se potriveste vreunei reguli. Functie PURA (fara DB)."""
    fa = (from_address or "").strip().lower()
    if not fa or not rules:
        return False
    subj = (subject or "").lower()
    for r in rules:
        if not _sender_match(fa, r.get("senders") or []):
            continue
        subs = r.get("subject_contains") or []
        if not subs or any(sc in subj for sc in subs):
            return True
    return False


def match_label(from_address: Optional[str], subject: Optional[str], rules: List[Dict[str, Any]]) -> Optional[str]:
    """Eticheta primei reguli potrivite (primul expeditor din regula), pt log/debug. None daca nimic."""
    fa = (from_address or "").strip().lower()
    if not fa or not rules:
        return None
    subj = (subject or "").lower()
    for r in rules:
        if not _sender_match(fa, r.get("senders") or []):
            continue
        subs = r.get("subject_contains") or []
        if not subs or any(sc in subj for sc in subs):
            return (r.get("senders") or ["?"])[0]
    return None
