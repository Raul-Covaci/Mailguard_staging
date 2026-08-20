"""Redirect VATHUB — mailurile oficiale de recuperare TVA din căsuțele personale.

Autoritățile fiscale (MF, ANAF, NAV, NAP, Finančná správa etc.) scriu pe adresa
personală a persoanei care a depus declarația 318, nu pe o adresă de firmă. Până
acum fetele își trimiteau mailurile una alteia manual. Redirectul le duce automat
în căsuța generală `vathub@cargotrack.ro`, de unde le citește aplicația VATHUB.

Traseul unui mail:
  1. poller-ul de căsuțe personale ingerează metadata (`personal_mails`)
  2. `mark_matches()` compară expeditorul cu lista validată → `vathub_match`
  3. `forward_pending()` ia mesajul BRUT prin IMAP (PEEK, rămâne necitit),
     îl atașează intact într-un mail nou și îl trimite prin SMTP-ul căsuței

Lista de expeditori și adresa țintă stau în `settings.vathub.redirect`. O intrare
cu `muted: true` e extrasă din trafic dar NEVALIDATĂ încă — motorul o ignoră.
"""
import json
import logging
from typing import Optional

from app.services import personal_imap, personal_smtp
from app.services.vathub_send_guard import assert_forward_target_allowed, VathubForwardBlocked

logger = logging.getLogger("mailguard.vathub_forward")

SETTINGS_KEY = "vathub.redirect"
MAX_ATTEMPTS = 5          # după atâtea eșecuri, rândul nu mai e reîncercat
DEFAULT_MAX_AGE_HOURS = 24  # nu inunda VATHUB cu tot istoricul la prima activare
BATCH_PER_POLL = 12      # câte forwarduri pe rulare, per cont
# Fiecare forward costă 2 conexiuni IMAP (citire mesaj brut + marcaj) plus una
# SMTP. Poller-ul rulează la fiecare minut, deci 12/rulare ține numărul de
# login-uri sub limitele de rată ale furnizorilor (Gmail/O365) și tot drenează
# un backlog de 24h în câteva minute.


def _defaults() -> dict:
    return {"target": "vathub@cargotrack.ro", "enabled": False,
            "domains": {}, "addresses": {}, "max_age_hours": DEFAULT_MAX_AGE_HOURS}


def load_rules(cur) -> dict:
    """Citește configul din settings. Cursor psycopg2 (RealDict sau standard)."""
    cfg = _defaults()
    try:
        cur.execute("SELECT value FROM settings WHERE key=%s", (SETTINGS_KEY,))
        row = cur.fetchone()
        if row:
            stored = row["value"] if isinstance(row, dict) else row[0]
            if isinstance(stored, str):
                stored = json.loads(stored)
            if isinstance(stored, dict):
                cfg.update(stored)
    except Exception:
        logger.exception("vathub: load_rules failed — folosesc valorile implicite")
    for k in ("domains", "addresses"):
        if not isinstance(cfg.get(k), dict):
            cfg[k] = {}
    return cfg


def active_entries(cfg: dict) -> tuple[set, set]:
    """(domenii, adrese) validate — intrările `muted` sunt sărite."""
    def _live(bucket: dict) -> set:
        out = set()
        for key, meta in (bucket or {}).items():
            if not key:
                continue
            if isinstance(meta, dict) and meta.get("muted"):
                continue
            out.add(key.strip().lower().lstrip("@"))
        return out
    return _live(cfg.get("domains")), _live(cfg.get("addresses"))


def match_sender(from_address: str, domains: set, addresses: set) -> Optional[str]:
    """Regula care potrivește expeditorul, sau None.

    Adresa exactă bate domeniul. Domeniul potrivește și subdomeniile
    (`nav.gov.hu` prinde `elekafa@elekafa.nav.gov.hu`, `nra.bg` prinde
    `b.stoilova@ro22.nra.bg`) — inspectorii scriu de pe subdomenii per birou.
    """
    addr = (from_address or "").strip().lower()
    if "@" not in addr:
        return None
    if addr in addresses:
        return addr
    domain = addr.rsplit("@", 1)[1]
    for rule in domains:
        if domain == rule or domain.endswith("." + rule):
            return rule
    return None


def mark_matches(cur, conn, account_id: int, cfg: dict) -> int:
    """Etichetează mailurile neexaminate ale unui cont. Returnează nr. potriviri.

    `vathub_matched_at` marchează „examinat", indiferent de rezultat. Un mail
    examinat nu se reevaluează: dacă lista se extinde ulterior, regula nouă se
    aplică de la mailurile următoare, nu retroactiv.
    """
    domains, addresses = active_entries(cfg)
    if not domains and not addresses:
        return 0

    max_age = int(cfg.get("max_age_hours") or DEFAULT_MAX_AGE_HOURS)
    # `received_at` e nullable (Date nevalid în mail); pe rândurile alea cade pe
    # created_at, altfel mailul n-ar fi examinat NICIODATĂ.
    cur.execute("""
        SELECT id, from_address
        FROM personal_mails
        WHERE account_id = %s
          AND vathub_matched_at IS NULL
          AND coalesce(received_at, created_at) >= now() - (%s * interval '1 hour')
        ORDER BY imap_uid
    """, (account_id, max_age))
    rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        return 0

    matched = 0
    for row in rows:
        rule = match_sender(row.get("from_address"), domains, addresses)
        cur.execute("""
            UPDATE personal_mails
            SET vathub_match = %s, vathub_matched_at = now()
            WHERE id = %s
        """, (rule, row["id"]))
        if rule:
            matched += 1
    conn.commit()

    if matched:
        logger.info(f"vathub: cont {account_id} — {matched} mailuri potrivite")
    return matched


def forward_pending(account: dict, imap_password: str, smtp_password: Optional[str],
                    cur, conn, cfg: dict) -> dict:
    """Trimite mailurile potrivite dar netrimise ale unui cont.

    Fiecare eșec incrementează `vathub_attempts` și se reia la următorul poll,
    până la MAX_ATTEMPTS. Un mail blocat de gardă e marcat definitiv (nu are rost
    reîncercat cu aceeași destinație greșită).
    """
    account_id = account["id"]
    target = (cfg.get("target") or "").strip().lower()
    result = {"sent": 0, "failed": 0, "blocked": 0}

    if not account.get("smtp_host"):
        logger.warning(f"vathub: cont {account_id} n-are SMTP configurat — nu se trimite nimic")
        return result

    cur.execute("""
        SELECT id, imap_uid, from_address, subject, vathub_match, vathub_attempts
        FROM personal_mails
        WHERE account_id = %s
          AND vathub_match IS NOT NULL
          AND vathub_forwarded_at IS NULL
          AND vathub_attempts < %s
        ORDER BY imap_uid
        LIMIT %s
    """, (account_id, MAX_ATTEMPTS, BATCH_PER_POLL))
    pending = [dict(r) for r in cur.fetchall()]
    if not pending:
        return result

    smtp_cfg = personal_smtp.resolve_smtp(account, imap_password, smtp_password)

    for mail in pending:
        uid = mail["imap_uid"]

        # Încercarea se contorizează ÎNAINTE de orice I/O. Dacă procesul moare
        # între SMTP și commit, mailul se reia — dar cel mult MAX_ATTEMPTS ori,
        # deci un eventual duplicat în VATHUB rămâne mărginit în loc să se repete
        # la infinit. Compromisul e deliberat: mai bine o copie în plus decât o
        # decizie 318 pierdută.
        cur.execute("""
            UPDATE personal_mails SET vathub_attempts = vathub_attempts + 1
            WHERE id = %s
        """, (mail["id"],))
        conn.commit()

        try:
            # Garda se verifică per mail, chiar înainte de SMTP: configul se
            # poate schimba din UI între două rulări.
            assert_forward_target_allowed(target)

            raw = personal_imap.fetch_raw_message(account, imap_password, uid)
            if not raw:
                raise RuntimeError("mesajul original nu a putut fi citit prin IMAP")

            msg = personal_smtp.build_forward(
                raw, smtp_cfg["from_address"], target,
                mail.get("vathub_match") or "", account["email_address"],
            )
            personal_smtp.send_forward(smtp_cfg, msg, target)

            cur.execute("""
                UPDATE personal_mails
                SET vathub_forwarded_at = now(), vathub_error = NULL
                WHERE id = %s
            """, (mail["id"],))
            conn.commit()
            result["sent"] += 1
            logger.info(
                f"vathub: cont {account_id} uid={uid} → {target} "
                f"(regulă {mail.get('vathub_match')})"
            )

            # Marcaj în căsuța proprietarului — mailul rămâne NECITIT.
            personal_imap.add_keyword(account, imap_password, uid)

        except VathubForwardBlocked as e:
            # Destinație greșită: nu are rost reîncercat cu același config.
            cur.execute("""
                UPDATE personal_mails
                SET vathub_attempts = %s, vathub_error = %s
                WHERE id = %s
            """, (MAX_ATTEMPTS, str(e)[:400], mail["id"]))
            conn.commit()
            result["blocked"] += 1
            logger.warning(f"vathub: cont {account_id} uid={uid} blocat: {e}")

        except Exception as e:
            cur.execute("""
                UPDATE personal_mails SET vathub_error = %s WHERE id = %s
            """, (str(e)[:400], mail["id"]))
            conn.commit()
            result["failed"] += 1
            logger.warning(f"vathub: cont {account_id} uid={uid} eșuat: {e}")

    return result


def process_account(account: dict, imap_password: str, smtp_password: Optional[str],
                    cur, conn) -> dict:
    """Pasul VATHUB pentru un cont: potrivire + trimitere. Nu ridică excepții."""
    out = {"matched": 0, "sent": 0, "failed": 0, "blocked": 0}
    try:
        if not account.get("vathub_enabled"):
            return out
        cfg = load_rules(cur)
        if not cfg.get("enabled"):
            return out
        out["matched"] = mark_matches(cur, conn, account["id"], cfg)
        out.update(forward_pending(account, imap_password, smtp_password, cur, conn, cfg))
    except Exception:
        logger.exception(f"vathub: process_account eșuat pentru contul {account.get('id')}")
    return out
