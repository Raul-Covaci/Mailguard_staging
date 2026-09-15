"""Blocaj pe EXPEDITOR — sursa unica pentru „un expeditor din blacklist nu trece NICIODATA".

Decizie Raul Covaci, 2026-09-15: mailurile Akcenta (`info@email.akcenta.eu`) intrau in CTS desi
expeditorul era in blacklist. Blacklist-ul BATE orice exceptare: whitelist, allowlist, eliberarea
AI din carantina (intent gate), amprenta de decarantinare, calea „Automat". Singurele cai de a
lasa din nou un expeditor sa treaca: stergerea intrarii din blacklist sau marcarea ei `muted`
(Setari -> Liste expeditori).

„Pe blacklist" inseamna:
  - `settings['phishing_manual_learning'].blacklist`, ORICE tip (carantina SAU spam), ne-muted;
  - `spam_sender_reputation` cu reputation='blocklist' (butonul „Marcheaza ca SPAM"), pe ORICE
    nivel de specificitate — un allowlist pe adresa NU mai anuleaza un blocklist pe domeniu.
Potrivire: adresa exacta SAU domeniul expeditorului SAU orice domeniu-parinte
(`spam_detector.sender_scopes`) — o intrare `akcenta.eu` prinde `info@email.akcenta.eu`.

Aplicat in doua locuri, deliberat redundant:
  1. `process_email.process_one` — la clasificare: spam fortat, fara eliberare automata;
  2. feed-ul CTS (`cts.cts_get_emails`) — ULTIMA poarta: orice mail eligibil al unui expeditor
     blocat e scos din feed si mutat in `stopped_spam`, oricum ar fi devenit eligibil (Legit,
     decarantinare, whitelist retroactiv, date vechi, o cale noua adaugata mai tarziu).
"""
import json
import logging
from typing import Iterable, Optional

from sqlalchemy import text

from app.services.spam_detector import sender_scopes

logger = logging.getLogger("mailguard.sender_block")

_ML_KEY = "phishing_manual_learning"

# Predicat SQL pe `emails.from_address`, oglinda lui `match()`: adresa exacta, domeniu sau
# domeniu-parinte. Domeniile fara punct (TLD singur) sunt excluse din `doms`, ca in sender_scopes.
BLOCKED_SQL = """(
    lower(btrim(COALESCE(from_address, ''))) = ANY(CAST(:bl_addrs AS text[]))
    OR EXISTS (
        SELECT 1 FROM unnest(CAST(:bl_doms AS text[])) AS b(dom)
         WHERE split_part(lower(btrim(COALESCE(from_address, ''))), '@', 2) = b.dom
            OR right(split_part(lower(btrim(COALESCE(from_address, ''))), '@', 2),
                     length(b.dom) + 1) = '.' || b.dom)
)"""


def manual_blacklist_keys(ml_value) -> set:
    """Cheile blacklist-ului manual, AMBELE tipuri, fara intrarile muted."""
    keys = set()
    for k, v in ((ml_value or {}).get("blacklist") or {}).items():
        if not k or (isinstance(v, dict) and v.get("muted")):
            continue
        keys.add(str(k).strip().lower().lstrip("@"))
    return keys


def match(from_address, keys: Iterable[str]) -> Optional[str]:
    """Cheia care blocheaza expeditorul (adresa > domeniu > domeniu-parinte) sau None."""
    if not keys:
        return None
    addr, doms = sender_scopes(from_address)
    if addr and addr in keys:
        return addr
    return next((d for d in doms if d in keys), None)


class BlockKeys:
    """Toate cheile blocate (blacklist manual + blocklist reputatie), incarcate o data per cerere."""

    def __init__(self, keys: Iterable[str]):
        self.keys = frozenset(k for k in keys if k)

    @property
    def addrs(self):
        return sorted(k for k in self.keys if "@" in k)

    @property
    def doms(self):
        return sorted(k for k in self.keys if "@" not in k and "." in k)

    def match(self, from_address) -> Optional[str]:
        return match(from_address, self.keys)


def load_keys_sa(db) -> BlockKeys:
    """Incarca toate cheile blocate. Ridica exceptia mai departe — apelantul decide fail-closed."""
    row = db.execute(text("SELECT value FROM settings WHERE key=:k"), {"k": _ML_KEY}).fetchone()
    keys = manual_blacklist_keys(row[0] if row and row[0] else None)
    for r in db.execute(text(
            "SELECT lower(btrim(scope_value)) FROM spam_sender_reputation "
            "WHERE reputation = 'blocklist'")).fetchall():
        if r[0]:
            keys.add(r[0].lstrip("@"))
    return BlockKeys(keys)


_REP_BLOCK_WHERE = (
    "reputation = 'blocklist' AND ("
    " (scope_type = 'sender_exact' AND {exact})"
    " OR (scope_type = 'domain' AND lower(btrim(scope_value)) = ANY(CAST({doms} AS text[]))))")
_REP_BLOCK_ORDER = (" ORDER BY CASE scope_type WHEN 'sender_exact' THEN 0 ELSE 1 END,"
                    " length(scope_value) DESC LIMIT 1")


def reputation_block_pg(cur, from_address) -> Optional[str]:
    """Blocklist de reputatie pe adresa SAU domeniu/parinte (psycopg2, pipeline). Ignora allowlist."""
    addr, doms = sender_scopes(from_address)
    if not addr:
        return None
    cur.execute(
        "SELECT lower(scope_value) AS v FROM spam_sender_reputation WHERE "
        + _REP_BLOCK_WHERE.format(exact="lower(btrim(scope_value)) = %s", doms="%s")
        + _REP_BLOCK_ORDER, (addr, doms or ['']))
    row = cur.fetchone()
    if not row:
        return None
    return row['v'] if isinstance(row, dict) else row[0]


def blocked_by_sa(db, from_address, include_reputation_exact: bool = True) -> Optional[str]:
    """Cheia care blocheaza expeditorul (SQLAlchemy), pentru gardele din UI; None = liber.

    `include_reputation_exact=False` e folosit DOAR de „Legit": blocklist-ul de reputatie pe
    adresa exacta e chiar randul scris de „Marcheaza ca SPAM", iar „Legit" e inversul lui
    documentat (il suprascrie). Blacklist-ul manual si blocklist-ul pe domeniu raman obligatorii.
    """
    addr, doms = sender_scopes(from_address)
    if not addr:
        return None
    row = db.execute(text("SELECT value FROM settings WHERE key=:k"), {"k": _ML_KEY}).fetchone()
    hit = match(from_address, manual_blacklist_keys(row[0] if row and row[0] else None))
    if hit:
        return hit
    exact = "lower(btrim(scope_value)) = :addr" if include_reputation_exact else "FALSE"
    rep = db.execute(text(
        "SELECT lower(scope_value) FROM spam_sender_reputation WHERE "
        + _REP_BLOCK_WHERE.format(exact=exact, doms=":doms") + _REP_BLOCK_ORDER),
        {"addr": addr, "doms": doms or ['']}).fetchone()
    return rep[0] if rep else None


def demote_blocked_eligible(db, eligible_sql: str, bk: BlockKeys) -> list:
    """Scoate din eligibilitatea CTS mailurile expeditorilor blocati: `stopped_spam` + override spam.

    Nu comite (apelantul comite). Intoarce id-urile mutate. `eligible_sql` = predicatul de
    eligibilitate al feed-ului, pe tabela `emails` fara alias.
    """
    if not bk.keys:
        return []
    rows = db.execute(text(f"""
        UPDATE emails SET queue_status = 'stopped_spam'
         WHERE ({eligible_sql}) AND {BLOCKED_SQL}
        RETURNING id, from_address, status
    """), {"bl_addrs": bk.addrs, "bl_doms": bk.doms}).fetchall()
    ids = []
    for r in rows:
        m = r._mapping
        key = bk.match(m["from_address"]) or ""
        reason = [{"code": "blacklist_cts_gate", "weight": 0, "match_text": key}]
        db.execute(text("""
            INSERT INTO email_spam (email_id, spam_score, spam_reasons, override)
            VALUES (:id, 0, CAST(:rs AS jsonb), TRUE)
            ON CONFLICT (email_id) DO UPDATE SET override = TRUE, computed_at = now(),
                spam_reasons = COALESCE(email_spam.spam_reasons, CAST('[]' AS jsonb))
                               || EXCLUDED.spam_reasons
        """), {"id": m["id"], "rs": json.dumps(reason)})
        db.execute(text(
            "INSERT INTO audit_log(actor, action, entity_type, entity_id, details) "
            "VALUES ('cts_gate', 'blocked_sender_cts_gate', 'email', :id, CAST(:d AS jsonb))"),
            {"id": m["id"], "d": json.dumps({"from_address": m["from_address"], "matched": key,
                                             "from_status": m["status"]})})
        ids.append(m["id"])
    if ids:
        logger.warning("cts gate: %d mail(uri) de la expeditori blocati scoase din feed: %s",
                       len(ids), ids[:50])
    return ids
