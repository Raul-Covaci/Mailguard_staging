"""CTS auto-solved — reguli deterministe pentru mailuri automate care pot pleca
direct marcate SOLVED spre CTS (feed: campul `mark_as_solved`).

O regula = {senders: [...], subject_contains: [...], subject_not_contains: [...]}. Se potriveste
daca expeditorul e in `senders` (lowercase; adresa exacta SAU cheia '@domeniu' — domeniul
expeditorului sau orice domeniu-parinte, `spam_detector.sender_scopes`, deci '@akcenta.eu'
prinde si 'info@email.akcenta.eu') SI (subject_contains gol => orice subiect; altfel vreun
substring se regaseste in subiect) SI niciun substring din `subject_not_contains` nu apare in
subiect. Totul case-insensitive.

⚠️ `subject_not_contains` e EXCEPTIA, si e evaluata ULTIMA: regula „tot de la expeditorul X
pleaca SOLVED, in afara de mailurile Y" (Akcenta, 2026-09-22 — „Decontarea nr." si
„Confirmarea nr." trebuie lucrate de om, deci pleaca ca NEW). Fara ea ar trebui enumerate toate
subiectele care SE marcheaza — imposibil pentru un expeditor care trimite orice.

Built-in + override din settings['cts.auto_solved_rules'] (fail-safe la built-in).

Folosit de feed-ul CTS (get_emails -> mark_as_solved per mail) si de ack (update_emails ->
persistare emails.cts_mark_solved pe ce a plecat efectiv ca solved). `matches` e o functie PURA
(fara DB): incarci regulile o data per request cu `load_rules(db)`, apoi le aplici pe fiecare rand.
"""
import logging
from typing import Optional, List, Dict, Any

from sqlalchemy import text
from app.database import SessionLocal
# Sursa UNICA pentru „adresa + domeniu + domenii-parinte" (aceeasi folosita de sender_block).
from app.services.spam_detector import sender_scopes

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
    # AKCENTA (2026-09-22): tot ce vine de pe domeniu (inclusiv subdomeniile de trimitere, ex.
    # info@email.akcenta.eu) pleaca SOLVED — sunt notificari/marketing pe care nu le lucreaza
    # nimeni. EXCEPTIE: „Decontarea nr." si „Confirmarea nr." sunt documente contabile reale,
    # trebuie sa ajunga NEW in CTS. Vezi migratia 20260922b_akcenta_unblock_auto_solved.sql.
    {"senders": ["@akcenta.eu"], "subject_contains": [],
     "subject_not_contains": ["decontarea nr", "confirmarea nr"]},
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
        nots = [str(s).strip().lower() for s in (r.get("subject_not_contains") or []) if str(s).strip()]
        if not senders:
            continue
        out.append({"senders": senders, "subject_contains": subs, "subject_not_contains": nots})
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
    """Adresa exacta SAU '@domeniu' — domeniul expeditorului ori oricare domeniu-parinte.

    Domeniile-parinte vin din `sender_scopes` (aceeasi sursa ca `sender_block`): o cheie
    '@akcenta.eu' prinde si 'info@email.akcenta.eu'. TLD-ul singur ('@eu') e exclus acolo,
    deci o cheie de tip TLD nu poate marca tot traficul.
    """
    if fa in senders:
        return True
    addr, doms = sender_scopes(fa)
    return any(("@" + d) in senders for d in doms)


def _rule_hit(fa: str, subj: str, r: Dict[str, Any]) -> bool:
    """Un rand (expeditor, subiect deja lowercase) se potriveste regulii `r`.

    Sursa UNICA a potrivirii — `matches` si `match_label` o refolosesc, ca eticheta de log sa
    nu poata diverge de decizia reala.
    """
    if not _sender_match(fa, r.get("senders") or []):
        return False
    if any(nc in subj for nc in (r.get("subject_not_contains") or [])):
        return False       # exceptia bate includerea
    subs = r.get("subject_contains") or []
    return (not subs) or any(sc in subj for sc in subs)


def matches(from_address: Optional[str], subject: Optional[str], rules: List[Dict[str, Any]]) -> bool:
    """True daca (from_address, subject) se potriveste vreunei reguli. Functie PURA (fara DB)."""
    return match_label(from_address, subject, rules) is not None


def match_label(from_address: Optional[str], subject: Optional[str], rules: List[Dict[str, Any]]) -> Optional[str]:
    """Eticheta primei reguli potrivite (primul expeditor din regula), pt log/debug. None daca nimic."""
    fa = (from_address or "").strip().lower()
    if not fa or not rules:
        return None
    subj = (subject or "").lower()
    for r in rules:
        if _rule_hit(fa, subj, r):
            return (r.get("senders") or ["?"])[0]
    return None
