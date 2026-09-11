"""v0.5.0 — Email processing pipeline.
Runs after sync from parser-email-op: phishing detection + NDR detection + categorization.
"""
import json
import logging
import os
import re
import threading
import psycopg2
import psycopg2.extras
from typing import Dict, Any, List

from app.config import get_settings
from app.services import phishing_detector

logger = logging.getLogger("mailguard.process")
settings = get_settings()

# NDR detection patterns
NDR_FROM_PATTERNS = re.compile(r'(mailer-daemon|postmaster|mail-daemon|bounce|noreply.*delivery)', re.IGNORECASE)
NDR_SUBJECT_PATTERNS = re.compile(
    r'(undeliverable|undelivered|delivery (status notification|failure|failed)|'
    r'mail (delivery|failed)|returned mail|\bNDR\b|non-delivery|'
    r'nedeliverabil|nelivrat|mesaj returnat|eroare livrare)',
    re.IGNORECASE
)


def _conn():
    return psycopg2.connect(
        host=settings.db_host, port=settings.db_port,
        dbname=settings.db_name, user=settings.db_user, password=settings.db_password,
    )


# ── Queue-status support (migrația 20260611_queue_status.sql) ───────────────────
# Defensiv: dacă migrația NU a rulat încă, coloana `queue_status` lipsește și
# `_set_queue` devine no-op → pipeline-ul se comportă byte-for-byte ca înainte.
SPAM_THRESHOLD = 50  # oglindă a DEFAULT_SPAM_THRESHOLD (spam_detector)
_QUEUE_COLS = None   # cache: are tabela `emails` coloana queue_status?


def _has_queue_cols(cur) -> bool:
    """True dacă schema de cozi e aplicată. Cache-uit pe durata procesului."""
    global _QUEUE_COLS
    if _QUEUE_COLS is None:
        try:
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='emails' AND column_name='queue_status'")
            _QUEUE_COLS = cur.fetchone() is not None
        except Exception:
            _QUEUE_COLS = False
    return _QUEUE_COLS


def _set_queue(cur, email_id: int, queue_status: str, **extra):
    """Setează queue_status (+ optional sent_to_cts_at / cts_send_error / cts_send_attempts).
    No-op dacă schema nu e aplicată. Nu face commit — caller-ul decide."""
    if not _has_queue_cols(cur):
        return
    sets = ["queue_status=%s"]
    vals = [queue_status]
    for col in ("sent_to_cts_at", "cts_send_error", "cts_send_attempts", "manual_clean"):
        if col in extra:
            sets.append(f"{col}=%s")
            vals.append(extra[col])
    vals.append(email_id)
    try:
        cur.execute(f"UPDATE emails SET {', '.join(sets)} WHERE id=%s", vals)
    except Exception:
        logger.exception("set_queue failed email_id=%s qs=%s", email_id, queue_status)


def _is_spam_now(cur, email_id: int) -> bool:
    """Citește verdictul spam derivat (același predicat ca endpoint-ul /spam):
    override=TRUE, sau (override != FALSE și spam_score>=prag)."""
    try:
        cur.execute("SELECT override, spam_score FROM email_spam WHERE email_id=%s", (email_id,))
        row = cur.fetchone()
        if not row:
            return False
        override = row['override'] if isinstance(row, dict) else row[0]
        score = row['spam_score'] if isinstance(row, dict) else row[1]
        if override is True:
            return True
        if override is False:
            return False
        return (score or 0) >= SPAM_THRESHOLD
    except Exception:
        logger.exception("is_spam_now failed email_id=%s", email_id)
        return False


# Expeditori interni exceptați de la detecția NDR (plasă de siguranță): raportul
# zilnic „Undeliverable" e injectat de ndr_report.py ca email sintetic clean
# (status='clean'/ready_for_cts) și NU trebuie confundat cu un bounce dacă vreodată
# ar trece prin pipeline — subiectul lui conține cuvântul „Undeliverable".
NDR_SENDER_EXEMPT = {"iris-rapoarte@mailguard.cargotrack.ro"}


def is_ndr(email: Dict[str, Any]) -> bool:
    """Quick NDR detection. Expeditorul raportului zilnic e exceptat explicit."""
    from_addr = (email.get('from_address') or '').strip()
    subject = email.get('subject') or ''
    if from_addr.lower() in NDR_SENDER_EXEMPT:
        return False
    if NDR_FROM_PATTERNS.search(from_addr):
        return True
    if NDR_SUBJECT_PATTERNS.search(subject):
        return True
    return False


def extract_ndr_address(email: Dict[str, Any]) -> str:
    """Extract failed recipient address from NDR body."""
    body = (email.get('body_text') or '')
    # RFC 3464 Final-Recipient header
    m = re.search(r'Final-Recipient:\s*rfc822;\s*([^\s>]+)', body, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Fallback: any email with error context
    m = re.search(r'<([^>\s]+@[^>\s]+)>', body)
    return m.group(1).strip() if m else ''


def _addr_list(value) -> list:
    """Normalizează un câmp de destinatari (to/cc) la o listă de adrese text.

    Pe staging formatul e `["a@b.ro", ...]`, dar acceptă și forma Graph
    (`[{"emailAddress": {"address": "..."}}]`) sau JSON venit ca string, ca să nu depindă
    de varianta de ingestie.
    """
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return [value]
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            ea = item.get('emailAddress') or item
            addr = ea.get('address') or ea.get('email') if isinstance(ea, dict) else None
            if addr:
                out.append(addr)
    return out


def _is_internal_address(addr: str) -> bool:
    """True dacă adresa e într-un domeniu CargoTrack (inclusiv subdomenii: mail1.cargotrack.ro).
    Refolosește lista din autoreply_generator ca să nu existe două definiții divergente."""
    try:
        from app.services.autoreply_generator import _is_internal_sender
        return _is_internal_sender(addr)
    except Exception:
        a = (addr or "").strip().lower()
        return a.endswith("cargotrack.ro")


def _lookup_client_by_email(cur, address: str):
    """Un singur lookup pe clients.emails. Returnează client_id sau None."""
    if not address:
        return None
    cur.execute("""
        SELECT id FROM clients
        WHERE emails @> %s::jsonb AND is_active = TRUE
        LIMIT 1
    """, (f'["{address.strip().lower()}"]',))
    row = cur.fetchone()
    return row[0] if row else None


def match_client(from_address: str, to_addresses=None) -> int:
    """Match email la client. Returns client_id or None.

    Pe emailurile PRIMITE clientul e expeditorul. Pe cele TRIMISE de noi expeditorul e o
    adresă CargoTrack, deci clientul e destinatarul — fără `to_addresses` toate emailurile
    trimise rămâneau cu client_id NULL (978 pe staging la 2026-07-29), iar satisfacția
    pierdea jumătatea de conversație care ne aparține.

    Adresele interne se sar la pasul pe destinatari (un email intern nu are client).
    """
    if not from_address and not to_addresses:
        return None
    with _conn() as conn:
        cur = conn.cursor()
        # Expeditorul: doar dacă e o adresă EXTERNĂ. Adresele CargoTrack apar în
        # clients.emails ca „agentul care gestionează clientul" (office@ la 26 de clienți,
        # adrese de colegi la 3-8 fiecare) — un match pe ele atribuie un client arbitrar.
        if from_address and not _is_internal_address(from_address):
            cid = _lookup_client_by_email(cur, from_address)
            if cid:
                return cid
        for addr in (to_addresses or []):
            a = (addr or '').strip().lower()
            if not a or _is_internal_address(a):
                continue
            cid = _lookup_client_by_email(cur, a)
            if cid:
                return cid
        return None


def _apply_dept_rules(cur, conn, email_id, em):
    """Aplică DOAR regulile deterministe de departament (fără AI, fără fallback suport_1).
    Rulează pe ORICE email (inclusiv auto_report / spam / carantină), ÎNAINTEA gate-urilor,
    ca rutarea pe expeditor/subiect (ex. toll alert -> taxe_drum) să nu depindă de calea clean.
    Update doar dacă o regulă lovește; altfel lasă neatins (AI-ul + suport_1 decid pe calea clean)."""
    dep_flag = (os.getenv('AI_DEPARTMENT_ENABLED', '1') or '').strip().lower()
    if dep_flag in ('0', 'false', 'no', 'off', ''):
        return
    try:
        from app.services import department_rules
        hit = department_rules.match(em)
        if not hit:
            return
        dep, rule = hit
        dres = {"department": dep, "confidence": 1.0, "model": "rule", "rule_id": rule.get("id"),
                "reason": "Regulă: " + (rule.get("note") or "")}
        cur.execute("UPDATE emails SET ai_department=%s, ai_department_result=%s::jsonb, "
                    "ai_department_at=NOW() WHERE id=%s",
                    (dep, psycopg2.extras.Json(dres), email_id))
        conn.commit()
    except Exception:
        logger.exception("dept rules-only failed email_id=%s", email_id)


def _maybe_classify_department(cur, conn, email_id, em, attachments=None):
    """Incadrare pe DEPARTAMENT (best-effort) pe calea CLEAN, dupa categorie. Rule-first +
    AI + fallback suport_1 (clasificatorul intoarce mereu un departament). Gate
    AI_DEPARTMENT_ENABLED=0. Izolat: NU trebuie sa rupa categoria/pipeline-ul."""
    dep_flag = (os.getenv('AI_DEPARTMENT_ENABLED', '1') or '').strip().lower()
    if dep_flag in ('0', 'false', 'no', 'off', ''):
        return
    # SPAM (status clean dar spam_score peste prag, sau override) NU se incadreaza pe departament —
    # nu apartine niciunui flux de lucru. Pragul = DEFAULT_SPAM_THRESHOLD (50), ca in _SPAM_PREDICATE.
    try:
        cur.execute("SELECT 1 FROM email_spam WHERE email_id=%s AND (override=TRUE "
                    "OR (override IS DISTINCT FROM FALSE AND spam_score>=50))", (email_id,))
        if cur.fetchone():
            return
    except Exception:
        pass
    try:
        from app.services import department_classifier
        dres = department_classifier.classify_department(em, attachments=attachments)
        if dres and dres.get('department'):
            cur.execute("UPDATE emails SET ai_department=%s, ai_department_result=%s::jsonb, "
                        "ai_department_at=NOW() WHERE id=%s",
                        (dres['department'], psycopg2.extras.Json(dres), email_id))
            conn.commit()
    except Exception:
        logger.exception("ai department classify failed email_id=%s", email_id)


def _maybe_classify_priority(cur, conn, email_id, em, attachments=None):
    """Incadrare pe PRIORITATE (best-effort) pe calea CLEAN, dupa categorie+departament. Schema
    P2..P5: plati->P2; sesizare/reclamatie->P3; documente sofer/vehicul/contract->P4; restul->P5.
    Reguli deterministe (plata->P2, urgenta->P3) au prioritate; altfel AI; fallback P5. Foloseste
    ai_category (deja stabilit) ca semnal pentru P3. Gate AI_PRIORITY_ENABLED=0.
    Izolat: NU trebuie sa rupa categoria/departamentul/pipeline-ul."""
    pr_flag = (os.getenv('AI_PRIORITY_ENABLED', '1') or '').strip().lower()
    if pr_flag in ('0', 'false', 'no', 'off', ''):
        return
    # Prioritatea se calculează DOAR pentru suport_1; restul primesc null
    try:
        cur.execute("SELECT ai_department FROM emails WHERE id=%s", (email_id,))
        _dept_row = cur.fetchone()
        _dept = (_dept_row['ai_department'] if isinstance(_dept_row, dict) else _dept_row[0]) if _dept_row else None
        if _dept and _dept != 'suport_1':
            return
    except Exception:
        pass
    # Client P1 (urgent in CRM) -> prioritate fortata fara AI
    try:
        cur.execute(
            "SELECT c.email_priority FROM emails e "
            "JOIN clients c ON c.id = e.client_id WHERE e.id = %s", (email_id,)
        )
        row = cur.fetchone()
        if row and (row['email_priority'] if isinstance(row, dict) else row[0]) == 1:
            cur.execute(
                "UPDATE emails SET ai_priority='2', ai_priority_result=%s::jsonb, "
                "ai_priority_at=NOW() WHERE id=%s",
                (psycopg2.extras.Json({"priority": "2", "source": "client_crm",
                                       "reason": "Client urgent in CRM -> P2 (prioritar)"}), email_id)
            )
            conn.commit()
            logger.info("email_id=%s: ai_priority=2 din client_crm", email_id)
            return
    except Exception:
        logger.exception("client priority check failed email_id=%s", email_id)
    # SPAM NU se incadreaza pe prioritate (acelasi criteriu ca la departament).
    try:
        cur.execute("SELECT 1 FROM email_spam WHERE email_id=%s AND (override=TRUE "
                    "OR (override IS DISTINCT FROM FALSE AND spam_score>=50))", (email_id,))
        if cur.fetchone():
            return
    except Exception:
        pass
    try:
        from app.services import priority_classifier
        # Categoria deja stabilita (sesizare/reclamatie = semnal puternic pentru P3).
        _cat = None
        try:
            cur.execute("SELECT ai_category FROM emails WHERE id=%s", (email_id,))
            _r = cur.fetchone()
            _cat = (_r['ai_category'] if isinstance(_r, dict) else _r[0]) if _r else None
        except Exception:
            _cat = None
        # ai_op_series se scrie ASINCRON de op_extractor, dupa ce pipeline-ul a calculat deja
        # prioritatea o data. Fara re-citirea proaspata de aici, regula pay_op_series din
        # priority_classifier (linia ~198) ar ramane cod mort si OP-urile ar cadea pe P4/P5.
        try:
            cur.execute("SELECT ai_op_series FROM emails WHERE id=%s", (email_id,))
            _r2 = cur.fetchone()
            _ops = (_r2['ai_op_series'] if isinstance(_r2, dict) else _r2[0]) if _r2 else None
            if _ops:
                em = dict(em)
                em['ai_op_series'] = _ops
        except Exception:
            pass
        pres = priority_classifier.classify_priority(em, attachments=attachments, category=_cat)
        if pres and pres.get('priority'):
            cur.execute("UPDATE emails SET ai_priority=%s, ai_priority_result=%s::jsonb, "
                        "ai_priority_at=NOW() "
                        "WHERE id=%s AND ai_priority_manual IS NOT TRUE",
                        (pres['priority'], psycopg2.extras.Json(pres), email_id))
            conn.commit()
    except Exception:
        logger.exception("ai priority classify failed email_id=%s", email_id)



def _maybe_classify_assignee(cur, conn, email_id, em, attachments=None):
    """Asignare pe UTILIZATOR (best-effort) pe calea CLEAN, dupa prioritate. STRICT: asigneaza doar
    cand o persoana CargoTrack e identificabila cert in fir (email/semnatura) si nu e in concediu;
    altfel ai_assignee=NULL (neasignat). Gate AI_ASSIGNEE_ENABLED=0. SPAM exclus. NU suprascrie o
    asignare manuala. Izolat: NU trebuie sa rupa categoria/departamentul/pipeline-ul."""
    asg_flag = (os.getenv('AI_ASSIGNEE_ENABLED', '1') or '').strip().lower()
    if asg_flag in ('0', 'false', 'no', 'off', ''):
        return
    # SPAM NU se asigneaza (acelasi criteriu ca la departament/prioritate).
    try:
        cur.execute("SELECT 1 FROM email_spam WHERE email_id=%s AND (override=TRUE "
                    "OR (override IS DISTINCT FROM FALSE AND spam_score>=50))", (email_id,))
        if cur.fetchone():
            return
    except Exception:
        pass
    try:
        # ai_department proaspat (asignarea il foloseste ca indiciu de coerenta pt. dezambiguizare)
        try:
            cur.execute("SELECT ai_department FROM emails WHERE id=%s", (email_id,))
            r = cur.fetchone()
            if r is not None:
                em = dict(em)
                em['ai_department'] = (r['ai_department'] if isinstance(r, dict) else r[0])
        except Exception:
            pass
        from app.services import assignee_classifier
        ares = assignee_classifier.classify_assignee(em, attachments=attachments)
        if ares is not None:
            cur.execute("UPDATE emails SET ai_assignee=%s, ai_assignee_result=%s::jsonb, "
                        "ai_assignee_at=NOW() WHERE id=%s AND ai_assignee_manual IS NOT TRUE",
                        (ares.get('assignee_email'), psycopg2.extras.Json(ares), email_id))
            conn.commit()
    except Exception:
        logger.exception("ai assignee classify failed email_id=%s", email_id)


def _maybe_generate_autoreply(cur, conn, email_id, em, attachments=None):
    """Genereaza SUGESTIA de reply auto (best-effort) pe calea CLEAN, dupa prioritate. Faza 1:
    doar sugestie informativa (nu se trimite nimic). Gate AI_AUTOREPLY_ENABLED=0. SPAM exclus.
    Nu suprascrie un verdict uman existent (accepted/rejected). Izolat: NU rupe restul pipeline-ului."""
    ar_flag = (os.getenv('AI_AUTOREPLY_ENABLED', '1') or '').strip().lower()
    if ar_flag in ('0', 'false', 'no', 'off', ''):
        return
    from app.services import autoreply_generator as _ag
    if not _ag.autoreply_ai_status():
        return
    # SPAM NU primeste sugestie de reply (acelasi criteriu ca la departament/prioritate).
    try:
        cur.execute("SELECT 1 FROM email_spam WHERE email_id=%s AND (override=TRUE "
                    "OR (override IS DISTINCT FROM FALSE AND spam_score>=50))", (email_id,))
        if cur.fetchone():
            return
    except Exception:
        pass
    # Nu regenera daca operatorul a dat deja un verdict (accepted/rejected).
    try:
        cur.execute("SELECT ai_autoreply_status FROM emails WHERE id=%s", (email_id,))
        r = cur.fetchone()
        st = (r['ai_autoreply_status'] if isinstance(r, dict) else (r[0] if r else None))
        if st in ('accepted', 'rejected'):
            return
    except Exception:
        pass
    try:
        from app.services import autoreply_generator
        ares = autoreply_generator.generate_autoreply(em, attachments=attachments)
        if ares and ares.get('ok') and ares.get('text'):
            cur.execute("UPDATE emails SET ai_autoreply=%s, ai_autoreply_result=%s::jsonb, "
                        "ai_autoreply_confidence=%s, ai_autoreply_at=NOW(), ai_autoreply_status='pending' WHERE id=%s",
                        (ares['text'], psycopg2.extras.Json(ares), ares.get('confidence'), email_id))
            conn.commit()
    except Exception:
        logger.exception("ai autoreply generate failed email_id=%s", email_id)


_DEDUP_EXEMPT_KEY = "dedup.exempt_senders"
# Expeditori automați care trimit LEGITIM mai multe mailuri identice în același moment
# (notificări bulk) — NU trebuie deduplicate, altfel le-am bloca pe nedrept.
# Listă de bază (activă imediat) + extensibilă din DB:
#   settings['dedup.exempt_senders'] = {"senders": ["adresa@x.ro", "@domeniu.ro"]}
# Intrările care încep cu '@' = domenii întregi (match pe sufix).
_DEDUP_EXEMPT_DEFAULT = {"noreply@hu-go.hu", "registru.release@cargotrack.ro"}


def _dedup_exempt_senders(cur) -> set:
    """Set de expeditori (lowercase) exceptați de la deduplicare: built-in + settings.
    Fail-safe: la orice eroare întoarce doar lista de bază."""
    exempt = set(_DEDUP_EXEMPT_DEFAULT)
    try:
        cur.execute("SELECT value FROM settings WHERE key=%s", (_DEDUP_EXEMPT_KEY,))
        r = cur.fetchone()
        v = (r["value"] if isinstance(r, dict) else r[0]) if r else None
        items = v.get("senders") if isinstance(v, dict) else (v if isinstance(v, list) else None)
        if items:
            exempt |= {str(s).strip().lower() for s in items if str(s).strip()}
    except Exception:
        logger.exception("citire %s esuata — folosesc doar lista de baza", _DEDUP_EXEMPT_KEY)
    return exempt


def _is_dedup_exempt(from_addr: str, exempt: set) -> bool:
    """True dacă expeditorul e exceptat: match exact pe adresă SAU pe domeniu (intrare '@dom')."""
    if not from_addr:
        return False
    if from_addr in exempt:
        return True
    dom = from_addr.rsplit("@", 1)[-1] if "@" in from_addr else ""
    return bool(dom) and ("@" + dom) in exempt


# Fereastra de timp în care două mailuri identice (același expeditor+subiect+corp+atașamente)
# sunt considerate duplicate. Redusă de la 3 min la 60s (mailuri automate legitime pot veni la
# 1-2 min distanță cu conținut DIFERIT — ex. plăți separate). Configurabil fără deploy:
#   settings['dedup.window_seconds'] = {"seconds": 60}  (sau direct un număr)
_DEDUP_WINDOW_KEY = "dedup.window_seconds"
_DEDUP_WINDOW_DEFAULT = 60


def _dedup_window_seconds(cur) -> int:
    """Fereastra dedup în secunde: settings sau default. Fail-safe la default (clamp 5..600)."""
    secs = _DEDUP_WINDOW_DEFAULT
    try:
        cur.execute("SELECT value FROM settings WHERE key=%s", (_DEDUP_WINDOW_KEY,))
        r = cur.fetchone()
        v = (r["value"] if isinstance(r, dict) else r[0]) if r else None
        raw = v.get("seconds") if isinstance(v, dict) else v
        if raw is not None:
            secs = int(raw)
    except Exception:
        logger.exception("citire %s esuata — folosesc default %ss", _DEDUP_WINDOW_KEY, _DEDUP_WINDOW_DEFAULT)
        secs = _DEDUP_WINDOW_DEFAULT
    return max(5, min(int(secs), 600))


_AI_CLASSIFY_KEY = "processing.ai_classification"


def _ai_classify_enabled(cur) -> bool:
    """Switch RUNTIME (settings['processing.ai_classification'] = {"enabled": bool}) pentru
    clasificarea AI categorie + departament. Absent/eroare => ON (fail-open). OFF = testare/import
    fara cost AI; emailurile clean devin TOTUSI eligibile CTS (vezi process_one/advance_one_clean)."""
    try:
        cur.execute("SELECT value FROM settings WHERE key=%s", (_AI_CLASSIFY_KEY,))
        r = cur.fetchone()
        if not r:
            return True
        v = r["value"] if isinstance(r, dict) else r[0]
        if v is None:
            return True
        return bool(v.get("enabled", True)) if isinstance(v, dict) else True
    except Exception:
        logger.exception("citire %s esuata — fail-open ON", _AI_CLASSIFY_KEY)
        return True


def ai_classification_status() -> bool:
    """Citeste switch-ul (pentru endpoint). Fail-open ON."""
    try:
        with _conn() as conn:
            return _ai_classify_enabled(conn.cursor())
    except Exception:
        logger.exception("ai_classification_status esuat — ON")
        return True


def set_ai_classification(enabled: bool, by: str = None) -> bool:
    """START/STOP clasificare AI categorie+departament (settings, runtime, fara restart)."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO settings(key, value, description, updated_by, updated_at) "
            "VALUES (%s, %s, %s, %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, "
            "  updated_by=EXCLUDED.updated_by, updated_at=NOW()",
            (_AI_CLASSIFY_KEY, psycopg2.extras.Json({"enabled": bool(enabled)}),
             "Clasificare AI categorie+departament. OFF = testare/import fara cost AI "
             "(emailurile clean raman eligibile CTS).", by))
        conn.commit()
    return bool(enabled)


_INTENT_KEY = "processing.intent_detection"


def _intent_detection_enabled(cur) -> bool:
    """Switch RUNTIME (settings['processing.intent_detection'] = {"enabled": bool}) pentru pasul
    AI de intentie (NOVA strict_intent_gate). Absent/eroare => ON (fail-open). OFF = fara cost AI
    pe intentie; detectia algoritmica (auth/AV/scoring) ramane activa si poate carantina, dar
    emailurile carantinate NU se mai elibereaza automat de AI."""
    try:
        cur.execute("SELECT value FROM settings WHERE key=%s", (_INTENT_KEY,))
        r = cur.fetchone()
        if not r:
            return True
        v = r["value"] if isinstance(r, dict) else r[0]
        if v is None:
            return True
        return bool(v.get("enabled", True)) if isinstance(v, dict) else True
    except Exception:
        logger.exception("citire %s esuata — fail-open ON", _INTENT_KEY)
        return True


def intent_detection_status() -> bool:
    """Citeste switch-ul de detectie intentie (pentru endpoint). Fail-open ON."""
    try:
        with _conn() as conn:
            return _intent_detection_enabled(conn.cursor())
    except Exception:
        logger.exception("intent_detection_status esuat — ON")
        return True


def set_intent_detection(enabled: bool, by: str = None) -> bool:
    """START/STOP pasul AI de intentie NOVA (settings, runtime, fara restart)."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO settings(key, value, description, updated_by, updated_at) "
            "VALUES (%s, %s, %s, %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, "
            "  updated_by=EXCLUDED.updated_by, updated_at=NOW()",
            (_INTENT_KEY, psycopg2.extras.Json({"enabled": bool(enabled)}),
             "Detectie intentie AI (NOVA intent-gate). OFF = fara cost AI pe intentie; "
             "detectia algoritmica ramane activa.", by))
        conn.commit()
    return bool(enabled)


_AI_CONTEXT_KEY = "processing.ai_context_enabled"


def _ai_context_enabled(cur) -> bool:
    """Switch RUNTIME pentru contextul unificat client (T1-T3). Absent/eroare => OFF (fail-closed).
    Feature experimental — implicit inactiv; se activeaza din UI Prompturi AI."""
    try:
        cur.execute("SELECT value FROM settings WHERE key=%s", (_AI_CONTEXT_KEY,))
        r = cur.fetchone()
        if not r:
            return False
        v = r["value"] if isinstance(r, dict) else r[0]
        if v is None:
            return False
        return bool(v.get("enabled", False)) if isinstance(v, dict) else False
    except Exception:
        logger.exception("citire %s esuata — fail-closed OFF", _AI_CONTEXT_KEY)
        return False


def ai_context_status() -> bool:
    """Citeste switch-ul context client (pentru endpoint). Fail-closed OFF."""
    try:
        with _conn() as conn:
            return _ai_context_enabled(conn.cursor())
    except Exception:
        logger.exception("ai_context_status esuat — OFF")
        return False


def set_ai_context(enabled: bool, by: str = None) -> bool:
    """START/STOP agregare context client la clasificare (settings, runtime, fara restart)."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO settings(key, value, description, updated_by, updated_at) "
            "VALUES (%s, %s, %s, %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, "
            "  updated_by=EXCLUDED.updated_by, updated_at=NOW()",
            (_AI_CONTEXT_KEY, psycopg2.extras.Json({"enabled": bool(enabled)}),
             "Context unificat client la incadrare (mailuri+apeluri+task-uri, 5 zile). "
             "T1: agregare. T2: summary IRIS. T3: ponderare clasificare.", by))
        conn.commit()
    return bool(enabled)


_OP_EXTRACT_KEY = "processing.op_extract_enabled"


def _op_extract_enabled(cur) -> bool:
    """Switch RUNTIME pentru fluxul auxiliar de extragere serie OP (vision AI pe atasamente).
    Absent/eroare => ON (fail-open, default activ). OFF = skip vision AI pe OP;
    emailul merge direct la suport_1 fara sa mai astepte extragerea seriei."""
    try:
        cur.execute("SELECT value FROM settings WHERE key=%s", (_OP_EXTRACT_KEY,))
        r = cur.fetchone()
        if not r:
            return True
        v = r["value"] if isinstance(r, dict) else r[0]
        if v is None:
            return True
        return bool(v.get("enabled", True)) if isinstance(v, dict) else True
    except Exception:
        logger.exception("citire %s esuata — fail-open ON", _OP_EXTRACT_KEY)
        return True


def op_extract_status() -> bool:
    """Citeste switch-ul extragere serie OP (pentru endpoint). Fail-open ON."""
    try:
        with _conn() as conn:
            return _op_extract_enabled(conn.cursor())
    except Exception:
        logger.exception("op_extract_status esuat — ON")
        return True


def set_op_extract(enabled: bool, by: str = None) -> bool:
    """START/STOP fluxul vision AI de extragere serie OP (settings, runtime, fara restart)."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO settings(key, value, description, updated_by, updated_at) "
            "VALUES (%s, %s, %s, %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, "
            "  updated_by=EXCLUDED.updated_by, updated_at=NOW()",
            (_OP_EXTRACT_KEY, psycopg2.extras.Json({"enabled": bool(enabled)}),
             "Flux auxiliar detectie + extragere serie OP din atasamente (vision AI). "
             "OFF = skip vision AI; OP-urile merg direct la suport_1.", by))
        conn.commit()
    return bool(enabled)


def _inject_client_context(em: dict, cur) -> None:
    """Injecteaza _client_context si _context_summary pe email dict daca toggle-ul e activ. Fara exceptie."""
    if not _ai_context_enabled(cur):
        em['_client_context'] = {}
        return
    try:
        from app.services.client_context import get_client_context, get_context_summary
        from datetime import timezone
        received = em.get('received_at')
        if received and received.tzinfo is None:
            from datetime import timezone as _tz
            received = received.replace(tzinfo=_tz.utc)
        now = received or __import__('datetime').datetime.now(__import__('datetime').timezone.utc)
        client_ctx = get_client_context(em.get('from_address', ''), now, cur)
        em['_client_context'] = client_ctx
        em['_context_summary'] = get_context_summary(client_ctx, email_id=em.get('id'))
    except Exception:
        logger.exception("inject_client_context esuat pentru email_id=%s", em.get('id'))
        em['_client_context'] = {}


def _find_duplicate_of(cur, email_id: int, em: dict, attachments: list):
    """Returnează (original_id, note) sau (None, None).
    Duplicat: același from_address + același subject + corp similar + atașamente identice
    (același număr ȘI aceleași nume), primit în fereastra dedup (default 60s, configurabilă)
    față de un email ANTERIOR care nu e el însuși duplicat. Expeditorii automați din lista
    de excepții sunt săriți complet."""
    from_addr = (em.get('from_address') or '').strip().lower()
    subject = (em.get('subject') or '').strip()
    if not from_addr or not subject:
        return None, None
    # Expeditori automați (notificări bulk) exceptați — trimit legitim N mailuri
    # identice simultan; nu le deduplicam ca să nu le blocăm pe nedrept.
    if _is_dedup_exempt(from_addr, _dedup_exempt_senders(cur)):
        return None, None
    received_at = em.get('received_at')
    if received_at is None:
        return None, None
    window = _dedup_window_seconds(cur)
    # Caută emailuri mai timpurii (sau cu id mai mic la timp egal): același expeditor+subiect,
    # NU deja marcate duplicate, sosite cu cel mult `window` secunde înainte de emailul curent.
    cur.execute("""
        SELECT id, body_text, has_attachments
        FROM emails
        WHERE LOWER(from_address) = %s
          AND subject = %s
          AND id != %s
          AND status != 'duplicate'
          AND received_at >= %s - make_interval(secs => %s)
          AND (received_at < %s OR (received_at = %s AND id < %s))
        ORDER BY received_at ASC, id ASC
        LIMIT 5
    """, (from_addr, subject, email_id, received_at, window, received_at, received_at, email_id))
    candidates = cur.fetchall()
    if not candidates:
        return None, None

    def _nb(b):
        """Normalizare corp: whitespace colaps, lowercase, trunchiat la 400 ch."""
        if not b:
            return ''
        return re.sub(r'\s+', ' ', b).strip().lower()[:400]

    curr_body = _nb(em.get('body_text'))
    curr_att_count = len(attachments)
    curr_att_names = sorted((a.get('filename') or a.get('name') or '').lower() for a in attachments)

    for cand in candidates:
        cand_id = cand['id'] if isinstance(cand, dict) else cand[0]
        cand_body = _nb(cand['body_text'] if isinstance(cand, dict) else cand[1])

        # Similaritate corp: >= 75% potrivire caracter-cu-caracter pe primele 200 ch
        if curr_body and cand_body:
            cmp_len = min(200, len(curr_body), len(cand_body))
            if cmp_len > 20:
                match = sum(a == b for a, b in zip(curr_body[:cmp_len], cand_body[:cmp_len]))
                if match / cmp_len < 0.75:
                    continue  # corp prea diferit — nu e duplicat

        # Atașamente: același număr + aceleași nume (case-insensitive, sortat)
        cur.execute("SELECT name FROM attachments WHERE email_id=%s", (cand_id,))
        cand_atts = sorted((r['name'] if isinstance(r, dict) else r[0] or '').lower()
                           for r in cur.fetchall())
        if len(cand_atts) != curr_att_count or cand_atts != curr_att_names:
            continue

        return cand_id, ("sender+subject+body+%datt matched within %ds"
                         % (curr_att_count, window))

    return None, None


def process_one(email_id: int) -> Dict[str, Any]:
    """Process a single pending email: phishing + NDR + client matching."""
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM emails WHERE id=%s AND status='pending'", (email_id,))
        em = cur.fetchone()
        if not em:
            return {"status": "skipped", "reason": "not_found_or_not_pending"}
        em = dict(em)

        # Get attachments
        cur.execute("SELECT * FROM attachments WHERE email_id=%s", (email_id,))
        attachments = [dict(r) for r in cur.fetchall()]

        # ── Deduplicare: emailuri identice trimise de mai ori în < 3 minute sunt oprite.
        # Cel mai timpuriu email din grup devine „originalul"; restul primesc status='duplicate'.
        # Checkpoint devreme — niciun apel AI, niciun cost, înainte de orice clasificare.
        try:
            _orig_id, _dup_note = _find_duplicate_of(cur, email_id, em, attachments)
            if _orig_id is not None:
                cur.execute(
                    "UPDATE emails SET status='duplicate', processed_at=NOW(), dedup_of=%s, "
                    "needs_human_review=FALSE WHERE id=%s",
                    (_orig_id, email_id))
                _set_queue(cur, email_id, 'stopped_duplicate')
                conn.commit()
                logger.info("email_id=%s duplicate of %s: %s", email_id, _orig_id, _dup_note)
                return {"status": "duplicate", "duplicate_of": _orig_id}
        except Exception:
            # Verificarea de dedup nu a scris nimic încă (doar SELECT-uri). Dacă a eșuat,
            # facem rollback ca tranzacția aborted să nu otrăvească restul procesării
            # (reguli dept / gate auto-report / clasificare) și să lase emailul în pending.
            try:
                conn.rollback()
            except Exception:
                pass
            logger.exception("dedup check failed email_id=%s — continuing", email_id)

        # Reguli de departament deterministe — se aplică pe ORICE email, ÎNAINTEA gate-urilor
        # (auto_report/NDR/spam/carantină ies devreme), ca rutarea pe expeditor/subiect
        # (ex. toll alert alert@ct.its-pro.hu -> taxe_drum) să nu depindă de calea clean.
        _apply_dept_rules(cur, conn, email_id, em)

        # Expeditor + liste (reputatie, suprimari, liste manuale) — incarcate AICI, INAINTEA
        # gate-ului „Automat", nu mai jos la detectia de phishing: verdictul pe expeditor
        # trebuie sa poata opri un mail INAINTE sa iasa devreme pe o cale livrata la CTS.
        # (Auto-learning suppressions pentru phishing, Treapta 1, se incarca in acelasi loc.)
        _from = (em.get('from_address') or '').lower().strip()
        _dom = _from.split('@', 1)[-1] if '@' in _from else ''
        suppress_codes = set()
        if _from or _dom:
            cur.execute("""
                SELECT suppressed_codes FROM suppression_rules
                WHERE active = TRUE
                  AND (expires_at IS NULL OR expires_at > NOW())
                  AND ((scope_type='sender_exact' AND scope_value=%s)
                    OR (scope_type='domain' AND scope_value=%s))
            """, (_from, _dom))
            for _r in cur.fetchall():
                for _c in (_r['suppressed_codes'] or []):
                    suppress_codes.add(_c)
        # Liste manuale (settings['phishing_manual_learning']): blacklist tipizat + whitelist.
        #  - blacklist tip=carantina → semnal Layer-4 la detecția de carantină (detect_phishing);
        #  - blacklist tip=spam → forțează override spam (NU carantină) — vezi blocul de spam;
        #  - whitelist → suprimare soft a semnalelor slabe.
        # Intrările `muted` (ignorate) sunt excluse din toate seturile.
        from app.services.sender_lists import entry_tip as _entry_tip
        manual_blacklist = set()      # doar tip=carantina
        manual_spamlist = set()       # doar tip=spam
        manual_whitelist = set()
        try:
            cur.execute("SELECT value FROM settings WHERE key='phishing_manual_learning'")
            _ml = cur.fetchone()
            if _ml and _ml['value']:
                for _k, _v in (_ml['value'].get('blacklist') or {}).items():
                    if not _k or (isinstance(_v, dict) and _v.get('muted')):
                        continue
                    if _entry_tip(_v if isinstance(_v, dict) else {}) == 'spam':
                        manual_spamlist.add(_k.lower())
                    else:
                        manual_blacklist.add(_k.lower())
                for _k, _v in (_ml['value'].get('whitelist') or {}).items():
                    if _k and not (isinstance(_v, dict) and _v.get('muted')):
                        manual_whitelist.add(_k.lower())
        except Exception:
            logger.exception("manual lists load failed email_id=%s", email_id)

        # Verdict pe EXPEDITOR (fara scoring de continut): allowlist/whitelist => exceptat,
        # blocklist/blacklist-spam => blocat. Aceeasi precedenta ca poarta de spam de mai jos,
        # din aceeasi sursa unica (`spam_detector`), ca sa nu poata diverge.
        from app.services import spam_detector as _spam_det
        try:
            _rep = _spam_det.get_sender_reputation_pg(em.get('from_address'), cur)
        except Exception:
            logger.exception("sender reputation lookup failed email_id=%s", email_id)
            _rep = None
        _sender_blocked = _spam_det.sender_gate_verdict(
            em, reputation=_rep, manual_whitelist=manual_whitelist,
            manual_spamlist=manual_spamlist) is True

        # ── Gate „Automat" (pattern confirmat din pagina Rapoarte) — ÎNAINTE de orice clasificare.
        # Dacă emailul se potrivește unui pattern „automat" (acelaşi criteriu ca regenerarea:
        # expeditor ∈ pattern ŞI amprenta SimHash a conţinutului), e EXCLUS din spam/carantină/
        # categorie şi procesat direct aici: atașat la pattern (creşte numărul), pus în coada de
        # extragere, marcat auto_report/auto_closed (terminal, „închis", ascuns din listă, fără
        # procesare umană). Doar mailuri FĂRĂ ataşament — pattern-urile s-au învăţat doar pe astfel
        # de mailuri (paritate cu _fetch_emails din reports). Fail-safe: la eroare cade în
        # clasificarea normală.
        # Bounce-urile NDR câștigă MEREU: nu intra pe calea „Automat" (auto_report/
        # auto_closed → livrat la CTS) pentru un email NDR. Lasă-l să cadă pe poarta NDR
        # de mai jos (terminal, stopped_ndr). Altfel un report_pattern învățat din greșeală
        # pe mailer-daemon ar scurtcircuita bounce-ul spre backoffice.
        # La fel si expeditorii BLOCATI (blocklist / blacklist tip=spam): calea „Automat" iese
        # devreme, INAINTE de poarta de spam, si e livrata la CTS (`cts._ELIGIBLE_AUTO`) — deci
        # fara aceasta conditie un expeditor pus pe blocklist continua sa ajunga in CTS ori de
        # cate ori mailul lui se potriveste unui pattern „automat" invatat. Mailul cade mai jos,
        # pe poarta de spam -> stopped_spam (terminal).
        if not attachments and not is_ndr(em) and not _sender_blocked:
            try:
                from app.api.v1 import reports as _reports
                _ph = _reports.try_auto_handle_pg(cur, em)
                if _ph:
                    # Alertele HU-GO (auto_report) primesc categoria 'sesizare' (cerere user); dept ramane
                    # (taxe_drum prin regulile deterministe). Semnatura subiectului, robusta la diacritice.
                    _is_hugo = "lelmezett jogosulatlan" in (em.get("subject") or "").lower()
                    if _is_hugo:
                        cur.execute("UPDATE emails SET status='auto_report', needs_human_review=FALSE, "
                                    "ai_category='sesizare', ai_category_manual=TRUE, "
                                    "processed_at=NOW() WHERE id=%s", (email_id,))
                    else:
                        cur.execute("UPDATE emails SET status='auto_report', needs_human_review=FALSE, "
                                    "processed_at=NOW() WHERE id=%s", (email_id,))
                    _set_queue(cur, email_id, 'auto_closed')
                    conn.commit()
                    if _ph.get('extract_enabled'):
                        threading.Thread(target=_reports._drain_queue,
                                         args=(_ph['pattern_id'],), daemon=True).start()
                    return {"status": "auto_report", "pattern_id": _ph['pattern_id']}
            except Exception:
                logger.exception("auto-report gate failed email_id=%s", email_id)

        # NDR detection
        if is_ndr(em):
            failed_addr = extract_ndr_address(em)
            cur.execute("""
                UPDATE emails SET status='ndr', processed_at=NOW(), category='ndr',
                       ai_category='necunoscut', ai_status='done', ai_processed_at=NOW()
                WHERE id=%s
            """, (email_id,))
            if failed_addr:
                cur.execute("""
                    INSERT INTO ndr_log(email_id, failed_address, original_subject)
                    VALUES(%s, %s, %s)
                """, (email_id, failed_addr, em.get('subject')))
            _set_queue(cur, email_id, 'stopped_ndr')  # terminal: NDR nu merge la CTS, nu se categoriseaza
            conn.commit()
            return {"status": "ndr", "failed_address": failed_addr}

        score, ph_status, reasons = phishing_detector.detect_phishing(
            em, attachments, suppress_codes=suppress_codes, blacklist=manual_blacklist,
            whitelist=manual_whitelist)
        needs_review = ph_status == 'quarantined_strict' or score >= 85

        # Client matching — expeditor, iar pentru emailurile trimise de noi: destinatari
        client_id = match_client(em.get('from_address') or '',
                                _addr_list(em.get('to_addresses')) + _addr_list(em.get('cc_addresses')))

        # ── Anti-spoofing (SPF/DKIM/DMARC) — aditiv în scorul de phishing + regulile de carantină.
        # Aplică rezultatele de autentificare deja capturate la ingestie (email_headers.
        # authentication_flags). Respectă regulile existente: whitelist manual (operator) și client
        # cunoscut nu se escaladează automat; intent-gate-ul AI de mai jos poate elibera. Închide
        # bypass-ul Layer-4 pentru domeniile „de încredere": un From=domeniu intern care EȘUEAZĂ
        # autentificarea = impersonare (CEO-fraud) → carantină strictă.
        _hard_block = False   # malware / impersonare domeniu intern — NU se eliberează automat
        _auth_result = None
        try:
            from app.services import auth_checker
            _authpol = None
            cur.execute("SELECT value FROM settings WHERE key='auth_policy'")
            _apr = cur.fetchone()
            if _apr and _apr.get('value'):
                _authpol = _apr['value']
            if (_authpol or {}).get('enabled', True):
                _auth_result = auth_checker.evaluate(em, _authpol)
                if _auth_result.get('reasons'):
                    _on_wl = (_from in manual_whitelist) or (_dom in manual_whitelist)
                    for _ar in _auth_result['reasons']:
                        reasons.append({'layer': 1, 'code': 'auth_' + _ar.get('code', 'x'),
                                        'weight': _ar.get('weight'), 'details': _ar.get('detail')})
                    if not _on_wl:
                        score += int(_auth_result.get('score') or 0)
                        _protect = set(d.lower() for d in
                                       getattr(phishing_detector, 'TRUSTED_SENDER_DOMAINS', set()))
                        for _pd in ((_authpol or {}).get('protect_domains') or []):
                            _protect.add(str(_pd).lower())
                        _escalate_ext = bool((_authpol or {}).get('escalate_external_fail', False))
                        _hf = (_auth_result.get('header_from_domain') or '').lower()
                        if _auth_result.get('verdict') == 'fail':
                            if _hf and _hf in _protect:
                                ph_status = 'quarantined_strict'
                                needs_review = True
                                _hard_block = True
                                reasons.append({'layer': 4, 'code': 'auth_spoof_internal_domain',
                                                'weight': None,
                                                'details': ('From %s eșuează autentificarea (DMARC/SPF/DKIM)'
                                                            ' — impersonare domeniu de încredere' % _hf)})
                            elif _escalate_ext and client_id is None and ph_status == 'clean':
                                ph_status = 'quarantined'
                        # scorul aditiv poate împinge clean→carantină pe pragul existent (upgrade only)
                        if ph_status == 'clean' and score >= phishing_detector.GLOBAL_POLICY['score_quarantine_threshold']:
                            ph_status = 'quarantined'
                        if score >= phishing_detector.GLOBAL_POLICY['score_review_threshold']:
                            needs_review = True
        except Exception:
            logger.exception("auth_checker integration failed email_id=%s", email_id)

        # ── Antivirus — scanează TOATE atașamentele (ClamAV + macro Office + arhive).
        # Malware → carantină strictă HARD (nu se eliberează automat, NICI pentru expeditori de
        # încredere — un cont legitim poate fi compromis). Suspect → scor aditiv (respectă whitelist).
        if attachments:
            try:
                from app.services import file_scanner
                cur.execute("SELECT value FROM settings WHERE key='av_policy'")
                _avp = cur.fetchone()
                _avpol = (_avp.get('value') if _avp else None) or {}
                if _avpol.get('enabled', True):
                    _susp_score = int(_avpol.get('suspicious_score', 20))
                    _mal_action = _avpol.get('malware_action', 'quarantine_strict')
                    _on_wl2 = (_from in manual_whitelist) or (_dom in manual_whitelist)
                    for _att in attachments:
                        _sc = file_scanner.scan_attachment(_att)
                        try:
                            cur.execute("SAVEPOINT sp_av")
                            cur.execute("UPDATE attachments SET scan_verdict=%s, scan_threats=%s::jsonb, "
                                        "scanned_at=NOW() WHERE id=%s",
                                        (_sc.get('verdict'), psycopg2.extras.Json(_sc.get('threats')),
                                         _att.get('id')))
                            cur.execute("RELEASE SAVEPOINT sp_av")
                        except Exception:
                            cur.execute("ROLLBACK TO SAVEPOINT sp_av")
                        _v = _sc.get('verdict')
                        if _v == 'malware':
                            _sigs = sorted({(t.get('signature') or t.get('code'))
                                            for t in (_sc.get('threats') or [])})
                            reasons.append({'layer': 4, 'code': 'attachment_malware', 'weight': None,
                                            'details': ('Atașament „%s": %s' %
                                                        (_att.get('name'), ', '.join(_sigs)[:140]))})
                            needs_review = True
                            _hard_block = True
                            ph_status = ('quarantined_strict' if _mal_action == 'quarantine_strict'
                                         else 'quarantined')
                        elif _v == 'suspicious' and not _on_wl2:
                            score += _susp_score
                            reasons.append({'layer': 2, 'code': 'attachment_suspicious',
                                            'weight': _susp_score,
                                            'details': ('Atașament „%s" suspect: %s' %
                                                        (_att.get('name'),
                                                         ', '.join(t.get('code') for t in
                                                                   (_sc.get('threats') or []))[:120]))})
                    if ph_status == 'clean' and score >= phishing_detector.GLOBAL_POLICY['score_quarantine_threshold']:
                        ph_status = 'quarantined'; needs_review = needs_review or score >= 85
            except Exception:
                logger.exception("file_scanner integration failed email_id=%s", email_id)

        # FAZA 3 — learning whitelist din DECARANTINARE: daca emailul candideaza la carantina,
        # vine de la un client cunoscut, iar amprenta continutului nou se potriveste cu un mail
        # decarantinat anterior de operator (acelasi expeditor/domeniu) -> auto-clean. Anti
        # false-positive recurent, fara portita de phishing (cere si client cunoscut SI amprenta).
        if ph_status in ('quarantined', 'quarantined_strict') and client_id is not None and not _hard_block:
            try:
                from app.services import template_fingerprint as TFP
                _wt, _wh, _wq = phishing_detector._new_content(em)
                _wbody = _wt or ''
                if not _wbody and _wh:
                    _wbody = re.sub(r'<[^>]+>', ' ', _wh)
                _fp = TFP.fingerprint(_wbody)
                if _fp is not None:
                    _from2 = (em.get('from_address') or '').lower().strip()
                    _dom2 = _from2.split('@', 1)[-1] if '@' in _from2 else ''
                    cur.execute("SELECT value FROM settings WHERE key='decarantine_fingerprints'")
                    _wl = cur.fetchone()
                    _wlmap = (_wl['value'] if _wl and _wl['value'] else {}) or {}
                    _cands = list(_wlmap.get(_from2) or []) + list(_wlmap.get(_dom2) or [])
                    _matched = None
                    for _c in _cands:
                        try:
                            if TFP.matches(_fp, int(_c.get('fp')), k=3):
                                _matched = _c
                                break
                        except (TypeError, ValueError):
                            continue
                    if _matched:
                        wl_note = {'layer': 3, 'code': 'learned_whitelist_match', 'weight': None,
                                   'details': ('Amprenta se potriveste cu email decarantinat anterior '
                                               '(id=%s) de la client cunoscut' % _matched.get('email_id')),
                                   'from_status': ph_status, 'to_status': 'clean'}
                        reasons = reasons + [wl_note]
                        cur.execute("INSERT INTO audit_log(actor, action, entity_type, entity_id, details) "
                                    "VALUES(%s,%s,%s,%s,%s::jsonb)",
                                    ('learning', 'learned_whitelist_match', 'email', email_id,
                                     psycopg2.extras.Json({'from': ph_status, 'to': 'clean',
                                                           'matched_email_id': _matched.get('email_id'),
                                                           'client_id': client_id})))
                        ph_status = 'clean'
                        needs_review = False
            except Exception:
                logger.exception("learning whitelist check failed email_id=%s", email_id)

        # Update
        cur.execute("""
            UPDATE emails SET
              status=%s, phishing_score=%s, phishing_reasons=%s::jsonb,
              needs_human_review=%s, client_id=%s, processed_at=NOW()
            WHERE id=%s
        """, (ph_status, score, psycopg2.extras.Json(reasons), needs_review, client_id, email_id))

        # Persistă rezultatul de autentificare (anti-spoofing) — defensiv (coloane opționale).
        if _auth_result is not None:
            try:
                cur.execute("SAVEPOINT sp_auth")
                cur.execute(
                    "UPDATE emails SET auth_verdict=%s, auth_result=%s::jsonb WHERE id=%s",
                    (_auth_result.get('verdict'), psycopg2.extras.Json(_auth_result), email_id))
                cur.execute("RELEASE SAVEPOINT sp_auth")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT sp_auth")  # coloane absente — ignoră fără a strica tranzacția

        # Native spam classification (independent of phishing) -> side table.
        # Reputatia expeditorului (allowlist/blocklist) e POARTA EXTERIOARA, consultata
        # INAINTE de scoringul de continut:
        #   allowlist / whitelist manuala -> score 0 + override=FALSE (NU spam, indiferent de prag)
        #   blocklist -> override=TRUE (apare in lista spam la orice prag, simetric cu allowlist)
        #   niciuna   -> scoring normal pe continut; NU atingem override (pastram decizii manuale)
        # Whitelist BATE blocklist/spamlist (cf. cerinta: whitelist ⇒ niciodata spam).
        try:
            from app.services import spam_detector
            # _rep e rezolvat o singura data, inainte de gate-ul „Automat" (vezi mai sus).
            # SURSĂ UNICĂ: poarta de spam (allowlist/whitelist ⇒ NU spam; blocklist/blacklist-spam ⇒
            # spam; altfel scorul decide). Identică cu /spam/backfill — vezi spam_detector.classify_spam_gate.
            _sp_score, _sp_reasons, _override = spam_detector.classify_spam_gate(
                em, reputation=_rep, manual_whitelist=manual_whitelist,
                manual_spamlist=manual_spamlist)
            if _override is False:
                cur.execute(
                    "INSERT INTO email_spam (email_id, spam_score, spam_reasons, override) "
                    "VALUES (%s, 0, %s::jsonb, FALSE) "
                    "ON CONFLICT (email_id) DO UPDATE SET spam_score=0, "
                    "spam_reasons=EXCLUDED.spam_reasons, override=FALSE, computed_at=now()",
                    (email_id, psycopg2.extras.Json(_sp_reasons)))
            elif _override is True:
                cur.execute(
                    "INSERT INTO email_spam (email_id, spam_score, spam_reasons, override) "
                    "VALUES (%s, %s, %s::jsonb, TRUE) "
                    "ON CONFLICT (email_id) DO UPDATE SET spam_score=EXCLUDED.spam_score, "
                    "spam_reasons=EXCLUDED.spam_reasons, override=TRUE, computed_at=now()",
                    (email_id, _sp_score, psycopg2.extras.Json(_sp_reasons)))
            else:
                cur.execute(
                    "INSERT INTO email_spam (email_id, spam_score, spam_reasons) "
                    "VALUES (%s, %s, %s::jsonb) "
                    "ON CONFLICT (email_id) DO UPDATE SET spam_score=EXCLUDED.spam_score, "
                    "spam_reasons=EXCLUDED.spam_reasons, computed_at=now()",
                    (email_id, _sp_score, psycopg2.extras.Json(_sp_reasons)))
        except Exception:
            logger.exception("spam classification failed email_id=%s", email_id)

        # If strict, log to quarantine_strict
        if ph_status == 'quarantined_strict':
            strict_reasons = [r for r in reasons if r.get('layer') == 4]
            reason_code = strict_reasons[0]['code'] if strict_reasons else 'unknown'
            cur.execute("""
                INSERT INTO quarantine_strict(email_id, reason, detected_indicators)
                VALUES(%s, %s, %s::jsonb)
            """, (email_id, reason_code, psycopg2.extras.Json(reasons)))

        # ── Queue cascade (early-exit, ieftin→scump) — PRIORITATE: carantină > spam > clean.
        # Carantina e decizie de securitate și bate spam-ul (un mail și phishing și spam =
        # tratat ca periculos). Intent-gate-ul de mai jos poate elibera carantina → re-evaluăm.
        if ph_status in ('quarantined', 'quarantined_strict'):
            _set_queue(cur, email_id, 'stopped_quarantine'); _qdest = 'stopped_quarantine'
        elif _is_spam_now(cur, email_id):
            _set_queue(cur, email_id, 'stopped_spam'); _qdest = 'stopped_spam'  # STOP: nu IRIS, nu FC
        else:
            _set_queue(cur, email_id, 'intent_check'); _qdest = 'intent_check'   # clean → calea AI

        conn.commit()

        # FAZA 2 -- verificator de intentie IRIS pe carantina SIMPLA + STRICTA (best-effort,
        # dupa commit-ul durabil). Default ON; dezactiveaza cu STRICT_INTENT_GATE_ENABLED=0.
        # NOVA NU decide singur: elibereaza doar benign + fara blockeri + client de incredere.
        # La eroare/timeout/IRIS neconfigurat ramane carantinat (conservator).
        gate_flag = (os.getenv('STRICT_INTENT_GATE_ENABLED', '1') or '').strip().lower()
        if (ph_status in ('quarantined', 'quarantined_strict') and gate_flag not in ('0', 'false', 'no', 'off', '')
                and _intent_detection_enabled(cur) and not _hard_block):
            try:
                from app.services import strict_intent_gate
                _gt, _gh, _gq = phishing_detector._new_content(em)
                _trusted = client_id is not None
                gate = strict_intent_gate.evaluate(em, reasons, _gt, _gh, trusted=_trusted)
                if gate and gate.get('decision') == 'release':
                    _from_status = ph_status
                    new_status = 'clean'
                    gate_note = {'layer': 3, 'code': 'ai_intent_gate', 'weight': None,
                                 'details': 'NOVA intent=%s conf=%.2f: %s' % (gate.get('intent'), gate.get('confidence') or 0.0, gate.get('reason') or ''),
                                 'decision': 'release', 'from_status': _from_status, 'to_status': new_status}
                    merged = reasons + [gate_note]
                    # Recalculeaza scorul pe findings NON-stricte ca sa nu ramana un rand 'clean'
                    # cu scor mare; marcheaza decizia (consistent cu calea de decarantinare).
                    _nonstrict = [r for r in reasons if r.get('layer') != 4]
                    _newscore = min(sum(r['weight'] for r in _nonstrict if r.get('weight') is not None), 100)
                    cur.execute("UPDATE emails SET status=%s, phishing_score=%s, phishing_reasons=%s::jsonb, "
                                "review_decision='ai_intent_release', needs_human_review=FALSE WHERE id=%s",
                                (new_status, float(_newscore), psycopg2.extras.Json(merged), email_id))
                    cur.execute("UPDATE quarantine_strict SET review_status='auto_released', "
                                "decision='ai_release', reviewed_by='ai_intent_gate', reviewed_at=NOW() "
                                "WHERE email_id=%s AND review_status='pending'", (email_id,))
                    # Carantina eliberată de NOVA → re-intră pe calea clean (spam-stop nu se aplică:
                    # decizia de securitate a fost deja luată de intent-gate cu blockeri hard).
                    _set_queue(cur, email_id, 'intent_check')
                    _qdest = 'intent_check'
                    cur.execute("INSERT INTO audit_log(actor, action, entity_type, entity_id, details) "
                                "VALUES(%s,%s,%s,%s,%s::jsonb)",
                                ('ai_intent_gate', 'intent_release', 'email', email_id,
                                 psycopg2.extras.Json({'from': _from_status, 'to': new_status,
                                                       'intent': gate.get('intent'), 'confidence': gate.get('confidence'),
                                                       'reason': gate.get('reason'), 'strict_codes': gate.get('strict_codes'),
                                                       'blockers': gate.get('blockers'), 'trusted': gate.get('trusted')})))
                    ph_status = new_status
                    conn.commit()
                elif gate:
                    cur.execute("INSERT INTO audit_log(actor, action, entity_type, entity_id, details) "
                                "VALUES(%s,%s,%s,%s,%s::jsonb)",
                                ('ai_intent_gate', 'intent_keep', 'email', email_id,
                                 psycopg2.extras.Json({'intent': gate.get('intent'), 'confidence': gate.get('confidence'),
                                                       'reason': gate.get('reason'), 'blockers': gate.get('blockers'),
                                                       'strict_codes': gate.get('strict_codes'), 'trusted': gate.get('trusted')})))
                    conn.commit()
            except Exception:
                logger.exception("intent gate failed email_id=%s", email_id)

        # AI categorization (informatie/sesizare/reclamatie/necunoscut) — best-effort,
        # AFTER the main commit so phishing/spam results stay durable even if the AI
        # call is slow. Toggle AI_CATEGORIZE_ENABLED=0.
        #
        # Cascadă: categoria se cere DOAR pe calea CLEAN (queue_status='intent_check'),
        # NICIODATĂ pe spam/carantină (early-exit — economisește apeluri NOVA, cf. spec).
        # Pre-migrare (fără queue_status) păstrăm comportamentul vechi: categorisim tot
        # ce nu e NDR, ca să nu schimbăm nimic înainte ca Andrei să ruleze migrația.
        _on_clean_path = (not _has_queue_cols(cur)) or _qdest == 'intent_check'
        if _on_clean_path:
            _inject_client_context(em, cur)  # T1: injecteaza _client_context pe em (noop daca toggle off)
        ai_flag = (os.getenv('AI_CATEGORIZE_ENABLED', '1') or '').strip().lower()
        from app.services.iris_ai import _ai_disabled as _global_ai_disabled
        _ai_off = (ai_flag in ('0', 'false', 'no', 'off', '')) or (not _ai_classify_enabled(cur)) or _global_ai_disabled()
        if _on_clean_path and _ai_off:
            # Switch OFF (testare/import): NU clasificam categorie/departament (fara cost AI), DAR
            # emailul clean devine ELIGIBIL CTS ca sa poata fie preluat. Reclasificarea se face cand
            # reactivezi switch-ul (sau dupa reset+reimport).
            cur.execute("UPDATE emails SET ai_status='skipped', ai_processed_at=NOW() WHERE id=%s",
                        (email_id,))
            from app.services import op_extractor as _ope
            if _op_extract_enabled(cur) and _ope.is_op_email(em, attachments=attachments):
                _set_queue(cur, email_id, 'pending_op_extract')
            else:
                _set_queue(cur, email_id, 'ready_for_cts')
            conn.commit()
            if attachments:
                try:
                    from app.api.v1 import documents
                    documents._kick_drain("auto")
                except Exception:
                    logger.exception("doc drain kick on ai_off path failed email_id=%s", email_id)
        elif _on_clean_path:
            try:
                from app.services import category_classifier
                res = category_classifier.classify_category(em, attachments=attachments)
                if res:
                    cur.execute("UPDATE emails SET ai_category=%s, ai_result=%s::jsonb, "
                                "ai_status='done', ai_processed_at=NOW() WHERE id=%s",
                                (res['category'], psycopg2.extras.Json(res), email_id))
                    from app.services import op_extractor as _ope
                    if _op_extract_enabled(cur) and _ope.is_op_email(em, attachments=attachments):
                        _set_queue(cur, email_id, 'pending_op_extract')
                    else:
                        # categorized → pregătit de CTS. Trimiterea reală setează sent_to_cts (FAZA 3/4).
                        _set_queue(cur, email_id, 'ready_for_cts')
                else:
                    cur.execute("UPDATE emails SET ai_status='error', ai_processed_at=NOW() WHERE id=%s",
                                (email_id,))
                    # NOVA a picat → blocat înainte de CTS, nu se pierde, se reia la următorul tick.
                    _set_queue(cur, email_id, 'error_nova')
                conn.commit()
            except Exception:
                logger.exception("ai categorize failed email_id=%s", email_id)
                try:
                    _set_queue(cur, email_id, 'error_nova'); conn.commit()
                except Exception:
                    pass

            # Departament (independent de categorie; nu schimba queue_status).
            _maybe_classify_department(cur, conn, email_id, em, attachments=attachments)
            # Prioritate (independent de categorie/departament; nu schimba queue_status).
            _maybe_classify_priority(cur, conn, email_id, em, attachments=attachments)
            _maybe_classify_assignee(cur, conn, email_id, em, attachments=attachments)
            _maybe_generate_autoreply(cur, conn, email_id, em, attachments=attachments)

        if attachments:
            try:
                from app.api.v1 import documents
                documents._kick_drain("auto")
            except Exception:
                logger.exception("doc drain kick on ingest failed email_id=%s", email_id)

        return {"status": ph_status, "score": score, "client_id": client_id, "reasons_count": len(reasons)}


def process_pending_batch(limit: int = 50) -> Dict[str, int]:
    """Process up to `limit` pending emails."""
    results = {"processed": 0, "clean": 0, "quarantined": 0, "quarantined_strict": 0, "ndr": 0, "error": 0}
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM emails WHERE status='pending' ORDER BY received_at DESC LIMIT %s", (limit,))
        ids = [r[0] for r in cur.fetchall()]

    for eid in ids:
        try:
            r = process_one(eid)
            results["processed"] += 1
            st = r.get("status", "error")
            if st in results:
                results[st] += 1
        except Exception as e:
            logger.exception(f"Error processing email {eid}: {e}")
            results["error"] += 1
    return results


def advance_one_clean(email_id: int) -> Dict[str, Any]:
    """Avansează un email aflat pe calea CLEAN care așteaptă categorizare:
      - manual_clean repus de operator (queued_general, status=clean), SAU
      - retry după o eroare NOVA (error_nova).
    Rulează DOAR categoria (un singur apel NOVA) — securitatea a fost deja decisă (operator
    sau pipeline), deci NU re-clasificăm phishing și NU putem trimite înapoi în carantină.
    categorized → ready_for_cts. La eșec NOVA → error_nova (se reia la următorul tick)."""
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, subject, from_address, from_name, body_text, body_html, conversation_id, received_at "
                    "FROM emails WHERE id=%s", (email_id,))
        em = cur.fetchone()
        if not em:
            return {"status": "skipped"}
        em = dict(em)
        _set_queue(cur, email_id, 'intent_check'); conn.commit()
        _inject_client_context(em, cur)  # T1: injecteaza _client_context pe em (noop daca toggle off)
        ai_flag = (os.getenv('AI_CATEGORIZE_ENABLED', '1') or '').strip().lower()
        from app.services.iris_ai import _ai_disabled as _global_ai_disabled
        if (ai_flag in ('0', 'false', 'no', 'off', '')) or (not _ai_classify_enabled(cur)) or _global_ai_disabled():
            # Switch OFF: nu clasificam, dar facem emailul eligibil CTS (nu il blocam in coada).
            cur.execute("UPDATE emails SET ai_status='skipped', ai_processed_at=NOW() WHERE id=%s",
                        (email_id,))
            _set_queue(cur, email_id, 'ready_for_cts'); conn.commit()
            return {"status": "ready_for_cts", "reason": "ai_classification_off"}
        try:
            from app.services import category_classifier
            res = category_classifier.classify_category(em)
            if res:
                cur.execute("UPDATE emails SET ai_category=%s, ai_result=%s::jsonb, "
                            "ai_status='done', ai_processed_at=NOW() WHERE id=%s",
                            (res['category'], psycopg2.extras.Json(res), email_id))
                _set_queue(cur, email_id, 'ready_for_cts')
                conn.commit()
                _maybe_classify_department(cur, conn, email_id, em)
                _maybe_classify_priority(cur, conn, email_id, em)
                _maybe_classify_assignee(cur, conn, email_id, em)
                _maybe_generate_autoreply(cur, conn, email_id, em)
                return {"status": "ready_for_cts", "category": res['category']}
            else:
                cur.execute("UPDATE emails SET ai_status='error', ai_processed_at=NOW() WHERE id=%s",
                            (email_id,))
                _set_queue(cur, email_id, 'error_nova')
                conn.commit()
                return {"status": "error_nova"}
        except Exception:
            logger.exception("advance_one_clean failed email_id=%s", email_id)
            try:
                _set_queue(cur, email_id, 'error_nova'); conn.commit()
            except Exception:
                pass
            return {"status": "error_nova"}


def advance_queue_batch(limit: int = 50) -> Dict[str, int]:
    """Preia mailurile de pe calea clean care așteaptă categorizare (manual_clean repus de
    operator + retry error_nova). No-op dacă schema de cozi nu e aplicată (queue_status absent)."""
    results = {"advanced": 0, "ready_for_cts": 0, "error_nova": 0, "skipped": 0}
    with _conn() as conn:
        cur = conn.cursor()
        if not _has_queue_cols(cur):
            return results
        cur.execute(
            "SELECT id FROM emails "
            "WHERE status='clean' AND queue_status IN ('queued_general','error_nova') "
            "ORDER BY received_at DESC LIMIT %s", (limit,))
        ids = [r[0] for r in cur.fetchall()]
    for eid in ids:
        try:
            r = advance_one_clean(eid)
            results["advanced"] += 1
            st = r.get("status")
            if st in results:
                results[st] += 1
        except Exception as e:
            logger.exception("Error advancing email %s: %s", eid, e)
    return results


def advance_op_extract_batch(limit: int = 20) -> Dict[str, int]:
    """Preia emailurile pending_op_extract și extrage seria OP din atașamente.
    Wrapper thin — logica reală e în op_extractor.py."""
    from app.services import op_extractor as _ope
    return _ope.advance_op_extract_batch(limit=limit)
