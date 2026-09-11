# -*- coding: utf-8 -*-
"""Redirect VATHUB din CĂSUȚA PRINCIPALĂ — mailurile de recuperare TVA spre vathub@cargotrack.ro.

De ce aici și nu în căsuțele personale (mutare cerută 2026-09-12): mailurile
autorităților fiscale ajung oricum în căsuța din care se alimentează pagina
„Email-uri" (`emails`), iar acolo redirectul nu mai depinde de credențialele
IMAP/SMTP personale ale nimănui, nu mai cere un poller separat și se vede în
aceeași pagină cu restul mailurilor. Calea veche (`vathub_forward.py`,
`personal_mails`) rămâne în cod, dezactivată prin `source` din config.

Traseul unui mail:
  1. ingestul obișnuit scrie rândul în `emails` (O365 native sau parser-email-op);
  2. `scan()` trece mailurile NOI (id > cursor) prin lista de expeditori și scrie
     doar potrivirile în `vathub_inbox_forward`;
  3. `forward_pending()` reconstituie mesajul original și îl trimite spre țintă,
     de pe contul SMTP no-reply.

⛔ Destinația e whitelist-ată în COD (`vathub_send_guard`), verificată per mail,
chiar înainte de SMTP — configul se poate schimba din UI între două rulări.

⚠️ Scanarea merge pe CURSOR de id (`settings.vathub.inbox_cursor`), nu pe un flag
per rând: `emails` are milioane de rânduri, deci un „neexaminat încă" ar fi
adevărat pe toate la instalare. Cursorul pornește de la ultimul id existent —
istoricul NU se retrimite; pentru el există `backfill()`, pornit explicit din UI.
"""
import json
import logging
import os
import smtplib

from sqlalchemy import text

from app.services import personal_smtp, vathub_forward
from app.services.credential_crypto import decrypt_credentials
from app.services.vathub_send_guard import assert_forward_target_allowed, VathubForwardBlocked

logger = logging.getLogger("mailguard.vathub_inbox")

SETTINGS_KEY = vathub_forward.SETTINGS_KEY          # 'vathub.redirect' — aceeași listă
CURSOR_KEY = "vathub.inbox_cursor"
MAX_ATTEMPTS = vathub_forward.MAX_ATTEMPTS          # 5
SCAN_BATCH = 2000        # câte rânduri noi din `emails` se examinează pe rulare
FORWARD_BATCH = 20       # câte mailuri se trimit pe rulare (tick de 5 min)
MAX_ATTACH_BYTES = 25 * 1024 * 1024   # cap de mărime, ca la personal_imap.MAX_RAW_BYTES


# ── Config ───────────────────────────────────────────────────────────────────

def load_config(db) -> dict:
    cfg = vathub_forward._defaults()
    row = db.execute(text("SELECT value FROM settings WHERE key=:k"),
                     {"k": SETTINGS_KEY}).fetchone()
    if row and row[0]:
        stored = row[0]
        if isinstance(stored, str):
            try:
                stored = json.loads(stored)
            except Exception:
                stored = None
        if isinstance(stored, dict):
            cfg.update(stored)
    for k in ("domains", "addresses"):
        if not isinstance(cfg.get(k), dict):
            cfg[k] = {}
    return cfg


def is_active(cfg: dict) -> bool:
    """Redirectul e pornit ȘI sursa e căsuța principală."""
    return bool(cfg.get("enabled")) and vathub_forward.inbox_enabled(cfg)


def _cursor_get(db) -> int:
    row = db.execute(text("SELECT value FROM settings WHERE key=:k"),
                     {"k": CURSOR_KEY}).fetchone()
    if not row or row[0] is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _cursor_set(db, value: int) -> None:
    db.execute(text(
        "INSERT INTO settings(key, value, description, updated_by, updated_at) "
        "VALUES(:k, to_jsonb(CAST(:v AS bigint)), "
        "'VATHUB inbox: ultimul emails.id scanat pentru potrivire', 'vathub_inbox', NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()"
    ), {"k": CURSOR_KEY, "v": int(value)})


# ── Pasul 1: potrivire ───────────────────────────────────────────────────────

_ENQUEUE_SQL = text("""
    INSERT INTO vathub_inbox_forward
        (email_id, from_address, subject, received_at, matched_rule, target, source)
    VALUES (:eid, :fa, :subj, :rcv, :rule, :target, :src)
    ON CONFLICT (email_id) DO NOTHING
""")


def _enqueue(db, row, rule, target, src) -> bool:
    res = db.execute(_ENQUEUE_SQL, {
        "eid": row["id"], "fa": row["from_address"],
        "subj": (row["subject"] or "")[:500], "rcv": row["received_at"],
        "rule": rule, "target": target, "src": src})
    return bool(res.rowcount)


def scan(db, limit: int = SCAN_BATCH) -> dict:
    """Examinează mailurile noi (id > cursor) și pune potrivirile la coadă.

    Cursorul avansează chiar dacă nu s-a potrivit nimic — altfel aceleași rânduri
    ar fi recitite la fiecare tick. Consecința asumată, identică celei din calea
    veche: o regulă adăugată azi se aplică mailurilor VIITOARE; pentru cele deja
    scanate există `backfill()`.
    """
    cfg = load_config(db)
    out = {"scanned": 0, "matched": 0, "cursor": _cursor_get(db)}
    if not is_active(cfg):
        return out
    domains, addresses = vathub_forward.active_entries(cfg)
    if not domains and not addresses:
        return out

    max_age = int(cfg.get("max_age_hours") or vathub_forward.DEFAULT_MAX_AGE_HOURS)
    target = (cfg.get("target") or "").strip().lower()

    # `fresh` se calculează în aceeași interogare, dar NU ca filtru: rândurile prea
    # vechi trebuie totuși returnate, ca să avanseze cursorul peste ele. Un filtru
    # în WHERE le-ar lăsa mereu înaintea cursorului și s-ar rescana la fiecare tick.
    # Fereastra de vechime apără de un resync delta care reintroduce mailuri vechi
    # cu id-uri noi: o decizie din martie n-are ce căuta azi în VATHUB.
    rows = db.execute(text("""
        SELECT id, from_address, subject, received_at,
               (COALESCE(received_at, fetched_at, created_at)
                    >= now() - (:h * interval '1 hour')) AS fresh
          FROM emails
         WHERE id > :cur
         ORDER BY id
         LIMIT :lim
    """), {"cur": out["cursor"], "lim": int(limit), "h": max_age}).fetchall()
    if not rows:
        return out

    last_id = out["cursor"]
    for r in rows:
        m = r._mapping
        last_id = max(last_id, m["id"])
        out["scanned"] += 1
        if not m["fresh"]:
            continue
        rule = vathub_forward.match_sender(m["from_address"], domains, addresses)
        if rule and _enqueue(db, m, rule, target, "auto"):
            out["matched"] += 1

    _cursor_set(db, last_id)
    db.commit()
    out["cursor"] = last_id
    if out["matched"]:
        logger.info("vathub inbox: %d mailuri potrivite din %d scanate (cursor %d)",
                    out["matched"], out["scanned"], last_id)
    return out


def backfill(db, days: int = 7, limit: int = 500) -> dict:
    """Caută retroactiv, într-o fereastră de zile, fără să atingă cursorul.

    Rulează la cerere din UI: o regulă nouă nu se aplică singură pe trecut, dar
    omul trebuie să poată recupera mailurile deja intrate.
    """
    cfg = load_config(db)
    out = {"scanned": 0, "matched": 0, "days": int(days)}
    if not is_active(cfg):
        out["error"] = "Redirectul VATHUB nu e activ pe căsuța principală"
        return out
    domains, addresses = vathub_forward.active_entries(cfg)
    if not domains and not addresses:
        out["error"] = "Lista de expeditori e goală"
        return out
    target = (cfg.get("target") or "").strip().lower()

    rows = db.execute(text("""
        SELECT id, from_address, subject, received_at
          FROM emails
         WHERE COALESCE(received_at, fetched_at, created_at) >= now() - (:d * interval '1 day')
         ORDER BY id DESC
         LIMIT :lim
    """), {"d": int(days), "lim": int(limit)}).fetchall()

    for r in rows:
        m = r._mapping
        out["scanned"] += 1
        rule = vathub_forward.match_sender(m["from_address"], domains, addresses)
        if rule and _enqueue(db, m, rule, target, "backfill"):
            out["matched"] += 1
    db.commit()
    logger.info("vathub inbox backfill %dz: %d potriviri din %d mailuri",
                days, out["matched"], out["scanned"])
    return out


# ── Pasul 2: construirea mesajului ───────────────────────────────────────────

def _rebuild_original(db, email_id: int):
    """Reconstituie mesajul original din DB, când MIME-ul brut nu e disponibil.

    Nu e identic cu originalul (headerele de transport se pierd), dar păstrează ce
    folosește VATHUB: expeditorul real, data, subiectul cu numărul de referință și
    atașamentele. Se folosește pe instalările fără ingest O365 nativ.
    """
    import email.policy
    from email.message import EmailMessage
    from email.utils import format_datetime

    row = db.execute(text("""
        SELECT graph_message_id, subject, from_address, from_name, to_addresses,
               cc_addresses, received_at, body_text, body_html
          FROM emails WHERE id = :id
    """), {"id": email_id}).fetchone()
    if not row:
        return None
    m = row._mapping

    def _join(v):
        if isinstance(v, list):
            return ", ".join([str(x) for x in v if x])
        return ""

    orig = EmailMessage(policy=email.policy.default)
    frm = m["from_address"] or ""
    orig["From"] = f'{m["from_name"]} <{frm}>' if m["from_name"] and frm else (frm or "necunoscut")
    if _join(m["to_addresses"]):
        orig["To"] = _join(m["to_addresses"])
    if _join(m["cc_addresses"]):
        orig["Cc"] = _join(m["cc_addresses"])
    orig["Subject"] = m["subject"] or "(fără subiect)"
    if m["received_at"]:
        orig["Date"] = format_datetime(m["received_at"])
    if m["graph_message_id"]:
        orig["Message-ID"] = m["graph_message_id"]

    body = m["body_text"] or ""
    orig.set_content(body or "(mesaj fără text)")
    if m["body_html"]:
        orig.add_alternative(m["body_html"], subtype="html")

    atts = db.execute(text("""
        SELECT name, content_type, storage_path
          FROM attachments
         WHERE email_id = :id AND storage_path IS NOT NULL
         ORDER BY id
    """), {"id": email_id}).fetchall()
    total = 0
    for a in atts:
        am = a._mapping
        path = am["storage_path"]
        try:
            if not path or not os.path.isfile(path):
                continue
            size = os.path.getsize(path)
            if total + size > MAX_ATTACH_BYTES:
                logger.warning("vathub inbox: email %s — atașamentul %s sare peste capul de mărime",
                               email_id, am["name"])
                continue
            with open(path, "rb") as fh:
                data = fh.read()
            total += size
            ct = (am["content_type"] or "application/octet-stream").split(";")[0].strip()
            maintype, _, subtype = ct.partition("/")
            orig.add_attachment(data, maintype=maintype or "application",
                                subtype=subtype or "octet-stream",
                                filename=am["name"] or "attachment")
        except Exception:
            logger.exception("vathub inbox: atașament necitit (email %s, %s)", email_id, path)
    return orig


def _graph_id(db, email_id: int):
    row = db.execute(text(
        "SELECT raw_graph_payload->>'graph_id' FROM emails WHERE id=:id"
    ), {"id": email_id}).fetchone()
    return row[0] if row else None


def build_message(db, email_id: int, from_address: str, target: str,
                  matched_rule: str, raw_fetch=None):
    """Mesajul de trimis: original INTACT când se poate, reconstituit altfel."""
    raw = None
    if raw_fetch is not None:
        gid = _graph_id(db, email_id)
        if gid:
            raw = raw_fetch(gid)
    if raw:
        return personal_smtp.build_forward(raw, from_address, target,
                                           matched_rule, "casuta principala"), "raw"
    original = _rebuild_original(db, email_id)
    if original is None:
        raise RuntimeError("emailul nu mai există în baza de date")
    return personal_smtp.build_forward_msg(original, from_address, target,
                                           matched_rule, "casuta principala"), "rebuilt"


# ── Pasul 3: trimiterea ──────────────────────────────────────────────────────

def _noreply_cfg(db):
    row = db.execute(text(
        "SELECT smtp_host, smtp_port, smtp_user, smtp_pass_enc, from_address, use_tls "
        "FROM noreply_smtp_config ORDER BY id LIMIT 1"
    )).fetchone()
    if not row:
        return None
    cfg = dict(row._mapping)
    cfg["password"] = decrypt_credentials(cfg["smtp_pass_enc"]).get("password", "")
    return cfg


def smtp_ready(db) -> bool:
    """Contul SMTP no-reply e configurat și decriptabil? Fără el nimic nu pleacă."""
    try:
        cfg = _noreply_cfg(db)
        return bool(cfg and cfg.get("smtp_host"))
    except Exception:
        logger.exception("vathub inbox: contul SMTP no-reply nu poate fi citit")
        return False


def _smtp_send(cfg, msg, to_address: str) -> None:
    """Trimite mesajul. Ridică doar dacă livrarea NU a avut loc.

    ⚠️ `sendmail`/`send_message` întors fără excepție înseamnă că serverul a
    ACCEPTAT mesajul. Închiderea se face separat, iar un `QUIT` eșuat se
    loghează, nu se raportează ca eșec — altfel mailul ar fi retrimis la
    următorul tick, deși a plecat (exact bucla de duplicate din
    `productivity_notifier`, 2026-09-12).
    """
    port = int(cfg["smtp_port"] or 587)
    if port == 465:
        server = smtplib.SMTP_SSL(cfg["smtp_host"], port, timeout=30)
    else:
        server = smtplib.SMTP(cfg["smtp_host"], port, timeout=30)
        if cfg.get("use_tls"):
            server.starttls()
    try:
        server.login(cfg["smtp_user"], cfg["password"])
        server.send_message(msg, from_addr=cfg["from_address"], to_addrs=[to_address])
    except Exception:
        try:
            server.close()
        except Exception:
            pass
        raise
    try:
        server.quit()
    except Exception:
        logger.warning("vathub inbox: QUIT eșuat după o livrare reușită către %s", to_address,
                       exc_info=True)


def forward_pending(db, limit: int = FORWARD_BATCH) -> dict:
    """Trimite mailurile puse la coadă. Nu ridică excepții."""
    cfg = load_config(db)
    out = {"sent": 0, "failed": 0, "blocked": 0, "pending": 0}
    if not is_active(cfg):
        return out

    target = (cfg.get("target") or "").strip().lower()

    # Verificarea SMTP stă ÎNAINTE de rezervare: rezervarea consumă o încercare, iar
    # un cont SMTP neconfigurat ar arde astfel toate cele 5 încercări în 25 de minute
    # și ar marca definitiv `failed` mailuri care n-au fost nici măcar încercate.
    smtp_cfg = _noreply_cfg(db)
    if not smtp_cfg or not smtp_cfg.get("smtp_host"):
        logger.warning("vathub inbox: contul SMTP no-reply nu e configurat — nu se trimite nimic")
        out["error"] = "SMTP no-reply neconfigurat (Setări → Auto-reply)"
        return out

    # REZERVARE ATOMICĂ. Contorul de încercări crește în ACEEAȘI instrucțiune care
    # alege rândurile, iar `FOR UPDATE SKIP LOCKED` le scoate din calea altui worker:
    # tick-ul de 5 minute și butonul „Rulează acum" pot cădea simultan pe același
    # rând, iar un SELECT urmat de UPDATE le-ar lăsa pe amândouă să trimită.
    # Creșterea ÎNAINTE de SMTP e deliberată: dacă procesul moare între trimitere și
    # commit, mailul se reia cel mult MAX_ATTEMPTS ori, nu la infinit.
    rows = db.execute(text("""
        UPDATE vathub_inbox_forward
           SET attempts = attempts + 1
         WHERE id IN (
                SELECT id FROM vathub_inbox_forward
                 WHERE status = 'pending' AND attempts < :max
                 ORDER BY id
                 LIMIT :lim
                 FOR UPDATE SKIP LOCKED)
     RETURNING id, email_id, matched_rule, from_address, attempts
    """), {"max": MAX_ATTEMPTS, "lim": int(limit)}).fetchall()
    db.commit()
    if not rows:
        return out

    raw_fetch = None
    try:
        from app.services import o365_ingest
        raw_fetch = o365_ingest.raw_message_fetcher()
    except Exception:
        logger.exception("vathub inbox: nu pot pregăti citirea MIME brut din Graph")

    for r in rows:
        m = r._mapping
        try:
            assert_forward_target_allowed(target)
            msg, mode = build_message(db, m["email_id"], smtp_cfg["from_address"],
                                      target, m["matched_rule"] or "", raw_fetch)
            _smtp_send(smtp_cfg, msg, target)
            db.execute(text("""
                UPDATE vathub_inbox_forward
                   SET status='sent', forwarded_at=now(), error=NULL, target=:t
                 WHERE id=:id
            """), {"id": m["id"], "t": target})
            db.commit()
            out["sent"] += 1
            logger.info("vathub inbox: email %s (%s) → %s [%s, regulă %s]",
                        m["email_id"], m["from_address"], target, mode, m["matched_rule"])
        except VathubForwardBlocked as e:
            db.execute(text("""
                UPDATE vathub_inbox_forward
                   SET status='blocked', attempts=:max, error=:err
                 WHERE id=:id
            """), {"id": m["id"], "max": MAX_ATTEMPTS, "err": str(e)[:400]})
            db.commit()
            out["blocked"] += 1
            logger.warning("vathub inbox: email %s blocat: %s", m["email_id"], e)
        except Exception as e:
            db.rollback()
            db.execute(text("""
                UPDATE vathub_inbox_forward
                   SET error=:err,
                       status = CASE WHEN attempts >= :max THEN 'failed' ELSE 'pending' END
                 WHERE id=:id
            """), {"id": m["id"], "err": str(e)[:400], "max": MAX_ATTEMPTS})
            db.commit()
            out["failed"] += 1
            logger.warning("vathub inbox: email %s eșuat: %s", m["email_id"], str(e)[:200])

    out["pending"] = db.execute(text(
        "SELECT count(*) FROM vathub_inbox_forward WHERE status='pending'"
    )).scalar() or 0
    return out


# ── Orchestrare ──────────────────────────────────────────────────────────────

def run_once(db) -> dict:
    """Pasul VATHUB al tick-ului de 5 minute. Best-effort, nu ridică niciodată."""
    out = {"scanned": 0, "matched": 0, "sent": 0, "failed": 0, "blocked": 0}
    try:
        cfg = load_config(db)
        if not is_active(cfg):
            return {"skipped": "inactiv"}
        out.update(scan(db))
        out.update(forward_pending(db))
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("vathub inbox: run_once eșuat")
    return out


def stats(db) -> dict:
    row = db.execute(text("""
        SELECT count(*) FILTER (WHERE status='sent')    AS sent,
               count(*) FILTER (WHERE status='pending') AS pending,
               count(*) FILTER (WHERE status='failed')  AS failed,
               count(*) FILTER (WHERE status='blocked') AS blocked,
               count(*)                                 AS total
          FROM vathub_inbox_forward
    """)).fetchone()
    return dict(row._mapping) if row else {
        "sent": 0, "pending": 0, "failed": 0, "blocked": 0, "total": 0}


def recent(db, limit: int = 50) -> list:
    rows = db.execute(text("""
        SELECT v.id, v.email_id, v.from_address, v.subject, v.received_at,
               v.matched_rule, v.status, v.attempts, v.error, v.source,
               v.matched_at, v.forwarded_at, v.target
          FROM vathub_inbox_forward v
         ORDER BY v.id DESC
         LIMIT :lim
    """), {"lim": max(1, min(int(limit or 50), 200))}).fetchall()
    return [dict(r._mapping) for r in rows]
