# -*- coding: utf-8 -*-
"""Raport zilnic Undeliverable (NDR) -> CTS.

Emailurile de tip "Undeliverable" sunt OPRITE de pipeline (status='ndr',
queue_status='stopped_ndr') si NU ajung niciodata in feed-ul CTS. Acest modul
strange zilnic toate aceste emailuri de IERI, le parseaza body-ul bounce
(adresa esuata + motiv), construieste un RAPORT (tabel HTML in body + atasament
CSV cu coloana "Observatii" libera pentru notite) si il INJECTEAZA ca un email
sintetic eligibil pentru CTS pe calea CLEAN (status='clean',
queue_status='ready_for_cts') — aceeasi cale pe care CTS o ingestă/confirma
pentru emailurile normale. (Calea auto_report/auto_closed era livrata de feed,
dar ingestia CTS o respingea repetat ca 'esec la salvare'.)

Planificare: self-gated pe cron-ul existent de 5 min (via /process/run-now).
Ruleaza o singura data pe zi, dupa ora 09:00 (Europe/Bucharest), pentru ZIUA DE
IERI. Idempotent: marker in tabela `settings` (ndr_report.last_report) + email
sintetic cu graph_message_id unic pe data (ndr-report-YYYY-MM-DD).
"""
import os
import io
import re
import csv
import json
import html as _html
import logging
from datetime import date as _date

from sqlalchemy import text

from app.database import SessionLocal
from app.services.manual_review import get_setting, set_setting

logger = logging.getLogger("mailguard.ndr_report")

DEFAULT_RECIPIENT = "office@cargotrack.ro"
DEFAULT_SEND_HOUR = 9
SENDER_ADDR = "iris-rapoarte@mailguard.cargotrack.ro"
SENDER_NAME = "IRIS Cargo360 — Raport Undeliverable"

# Directorul fizic de atasamente (acelasi pe care il rezolva feed-ul CTS via
# _host_path). Fisierul CSV TREBUIE scris sub acest prefix, altfel garda
# anti-traversal din cts.py il exclude.
ATTACH_BASE = os.getenv("ATTACH_HOST_PREFIX", "/home/mail-data/attachments")

_RO_MONTHS = ["", "ian.", "feb.", "mar.", "apr.", "mai", "iun.",
              "iul.", "aug.", "sep.", "oct.", "nov.", "dec."]


# --------------------------------------------------------------------------- #
# Parser bounce body_text
# --------------------------------------------------------------------------- #
_ADDR_LINE = re.compile(r'^\s+([^\s@<>]+@[^\s@<>]+\.[^\s@<>]+?)\s*$')
_EMAIL_ANY = re.compile(r'([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})')
# Cod SMTP: extras DOAR daca motivul are context de bounce real (SMTP/RCPT/mailbox/
# delivery/...), ca sa nu prinda numere oarecare din corpul emailurilor normale
# clasificate gresit ca NDR (ex. telefon "606 460 OFFICE").
_SMTP_CODE = re.compile(r'\b([45]\d\d)\b')
_BOUNCE_CTX = re.compile(
    r'smtp|rcpt|mailbox|recipient|delivery|deliver|undeliverable|unrouteable|'
    r'relay|no such|does not exist|account|not found|host ', re.IGNORECASE)
_FINAL_RECIP = re.compile(r'Final-Recipient:\s*rfc822;\s*([^\s>]+)', re.IGNORECASE)
_DIAG = re.compile(r'Diagnostic-Code:\s*[a-z]+;\s*(.+)', re.IGNORECASE)
_STATUS = re.compile(r'Status:\s*([0-9.]+)', re.IGNORECASE)


def _clean(s):
    return re.sub(r'\s+', ' ', (s or '')).strip()


def _code(reason):
    r = reason or ''
    if not _BOUNCE_CTX.search(r):
        return ''
    m = _SMTP_CODE.search(r)
    return m.group(1) if m else ''


def parse_ndr_failures(body_text):
    """Extrage perechile (adresa esuata, motiv) dintr-un bounce.

    Suporta 2 formate: DSN structurat (RFC 3464, Final-Recipient/Diagnostic-Code)
    si textul uman Exim/Postfix (adresa indentata urmata de motiv indentat mai
    mult). Fallback: prima adresa gasita + un fragment din body.
    Intoarce o lista de dict {address, reason, code}.
    """
    body = body_text or ""
    pairs = []  # (address, reason)

    # 1) DSN structurat (RFC 3464)
    if re.search(r'Final-Recipient:', body, re.IGNORECASE):
        for ch in re.split(r'(?i)(?=Final-Recipient:)', body):
            m = _FINAL_RECIP.search(ch)
            if not m:
                continue
            addr = m.group(1).strip().strip('<>')
            dm = _DIAG.search(ch)
            if dm:
                reason = dm.group(1)
            else:
                sm = _STATUS.search(ch)
                reason = ("Status " + sm.group(1)) if sm else ""
            pairs.append((addr, _clean(reason)))

    # 2) Text uman Exim/Postfix
    if not pairs:
        lines = body.splitlines()
        n = len(lines)
        i = 0
        while i < n:
            am = _ADDR_LINE.match(lines[i])
            if am:
                addr = am.group(1)
                indent = len(lines[i]) - len(lines[i].lstrip())
                j = i + 1
                parts = []
                while j < n:
                    nxt = lines[j]
                    if not nxt.strip():
                        break
                    if _ADDR_LINE.match(nxt):
                        break
                    nind = len(nxt) - len(nxt.lstrip())
                    if nind <= indent:
                        break
                    parts.append(nxt.strip())
                    j += 1
                pairs.append((addr, _clean(" ".join(parts))))
                i = j
            else:
                i += 1

    # 3) Fallback
    if not pairs:
        em = _EMAIL_ANY.search(body)
        addr = em.group(1) if em else ""
        pairs.append((addr, _clean(body)[:300]))

    seen = set()
    out = []
    for a, r in pairs:
        key = ((a or '').lower(), r)
        if key in seen:
            continue
        seen.add(key)
        out.append({"address": a, "reason": r, "code": _code(r)})
    return out


# --------------------------------------------------------------------------- #
# Colectare + randare
# --------------------------------------------------------------------------- #
# Semnatura de bounce REAL (mirror is_ndr din process_email): fie expeditor daemon
# (Mailer-Daemon/postmaster/...), fie subiect de tip non-delivery. Filtreaza emailurile
# normale clasificate GRESIT ca ndr (ex. de la un client oarecare, fara semnatura de bounce).
_BOUNCE_FROM = r'(mailer-daemon|postmaster|mail-daemon|bounce|noreply.*delivery)'
_BOUNCE_SUBJ = (r'(undeliverable|undelivered|delivery (status notification|delayed|failure|failed)'
                r'|mail (delivery|failed)|returned mail|non-delivery|nedeliverabil|nelivrat'
                r'|mesaj returnat|eroare livrare)')


def collect_rows(db, target_date):
    res = db.execute(text(
        "SELECT id, subject, from_address, received_at, body_text "
        "FROM emails "
        "WHERE status='ndr' AND queue_status='stopped_ndr' "
        "AND (received_at AT TIME ZONE 'Europe/Bucharest')::date = :d "
        "AND (COALESCE(from_address,'') ~* :bf OR COALESCE(subject,'') ~* :bs) "
        "ORDER BY received_at, id"),
        {"d": target_date, "bf": _BOUNCE_FROM, "bs": _BOUNCE_SUBJ}).fetchall()
    rows = []
    seen_addresses = set()
    for r in res:
        m = r._mapping
        recv = m["received_at"]
        recv_s = recv.strftime("%Y-%m-%d %H:%M") if recv else ""
        for f in parse_ndr_failures(m["body_text"]):
            addr_key = (f["address"] or "").lower()
            if addr_key in seen_addresses:
                continue
            seen_addresses.add(addr_key)
            rows.append({
                "email_id": m["id"],
                "subject": _clean(m["subject"] or ""),
                "received": recv_s,
                "address": f["address"],
                "reason": f["reason"],
                "code": f["code"],
            })
    return rows


def render_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=',', quoting=csv.QUOTE_MINIMAL)
    w.writerow(["Data primire", "Subiect original", "Adresa esuata (MAIL)",
                "Motiv", "Cod", "Observatii"])
    for r in rows:
        w.writerow([r["received"], r["subject"], r["address"],
                    r["reason"], r["code"], ""])
    # BOM (utf-8-sig) ca Excel sa recunoasca diacriticele
    return buf.getvalue().encode("utf-8-sig")


def _send_hour(db):
    """Ora configurata de trimitere (settings `ndr_report.send_hour`, implicit 9)."""
    try:
        return int(get_setting(db, "ndr_report.send_hour", DEFAULT_SEND_HOUR) or DEFAULT_SEND_HOUR)
    except Exception:
        return DEFAULT_SEND_HOUR


def render_html(rows, target_iso, recipient, send_hour=None):
    th = ("padding:6px 10px;border:1px solid #d0d7de;background:#f6f8fa;"
          "text-align:left;font-size:13px")
    td = "padding:6px 10px;border:1px solid #d0d7de;font-size:13px;vertical-align:top"
    # Coloana MAIL: fara wrap + latime minima, ca adresa sa se vada intreaga.
    td_mail = td + ";white-space:nowrap;min-width:240px;font-family:Consolas,monospace"
    body_rows = []
    for i, r in enumerate(rows, 1):
        body_rows.append(
            "<tr>"
            "<td style='%s'>%d</td>" % (td, i) +
            "<td style='%s'>%s</td>" % (td, _html.escape(r["subject"] or "")) +
            "<td style='%s'><b>%s</b></td>" % (td_mail, _html.escape(r["address"] or "—")) +
            "<td style='%s'>%s</td>" % (td, _html.escape(r["reason"] or "")) +
            "<td style='%s'>&nbsp;</td>" % td +
            "</tr>")
    table = (
        "<table style='border-collapse:collapse;border:1px solid #d0d7de;"
        "font-family:Arial,sans-serif'>"
        "<thead><tr>"
        "<th style='%s'>#</th>" % th +
        "<th style='%s'>Email original (subiect)</th>" % th +
        "<th style='%s'>MAIL (adresă eșuată)</th>" % (th + ";white-space:nowrap;min-width:240px") +
        "<th style='%s'>Motiv</th>" % th +
        "<th style='%s'>Observații</th>" % th +
        "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table>")
    return (
        "<div style='font-family:Arial,sans-serif;font-size:14px;color:#1f2328'>"
        "<p>Raport automat IRIS Cargo360 — emailuri <b>Undeliverable</b> oprite "
        "(nelivrate) din data de <b>%s</b>.</p>" % _html.escape(target_iso) +
        "<p>Total intrări: <b>%d</b>. Detaliile complete sunt în atașamentul CSV "
        "(coloana <i>Observații</i> este liberă pentru notițe).</p>" % len(rows) +
        table +
        "<p style='color:#656d76;font-size:12px;margin-top:14px'>Generat automat "
        "zilnic la ora %02d:00 (Europe/București). Destinatar: %s.</p></div>"
        % (int(send_hour if send_hour is not None else DEFAULT_SEND_HOUR),
           _html.escape(recipient)))


def render_text(rows, target_iso):
    out = ["Raport Undeliverable %s — %d intrari" % (target_iso, len(rows)), ""]
    for i, r in enumerate(rows, 1):
        out.append("%d. MAIL: %s | Motiv: %s" % (i, r["address"] or "-", r["reason"] or ""))
    out += ["", "Detalii complete in atasamentul CSV (coloana Observatii libera pentru notite)."]
    return "\n".join(out)


def _ro_date(d):
    try:
        return "%d %s %d" % (d.day, _RO_MONTHS[d.month], d.year)
    except Exception:
        return str(d)


# --------------------------------------------------------------------------- #
# Injectare email sintetic + atasament
# --------------------------------------------------------------------------- #
def _write_csv_file(target_iso, csv_bytes):
    d = os.path.join(ATTACH_BASE, "reports", "ndr", target_iso)
    os.makedirs(d, exist_ok=True)
    fname = "raport-undeliverable-%s.csv" % target_iso
    fpath = os.path.join(d, fname)
    with open(fpath, "wb") as f:
        f.write(csv_bytes)
    return fpath, fname


def create_report_email(db, target_date, rows, csv_bytes, recipient, force=False):
    target_iso = target_date.isoformat()
    # Message-ID valid (format RFC 5322). CTS foloseste internetMessageId/graphMessageId drept
    # cheie de salvare; un id ne-standard sau internetMessageId=null => CTS respinge ingestarea
    # ("esec la salvare", in bucla). graph_message_id == message_id, exact ca la emailurile reale.
    gmid = "<ndr-report-%s@mailguard.cargotrack.ro>" % target_iso

    existing = db.execute(text(
        "SELECT id FROM emails WHERE graph_message_id=:g"), {"g": gmid}).fetchone()
    if existing:
        if not force:
            return {"status": "already_exists", "email_id": existing[0], "date": target_iso}
        old = existing[0]
        db.execute(text("DELETE FROM attachments WHERE email_id=:id"), {"id": old})
        db.execute(text("DELETE FROM emails WHERE id=:id"), {"id": old})
        db.commit()

    fpath, fname = _write_csv_file(target_iso, csv_bytes)
    subject = "Raport zilnic Undeliverable — %s (%d intrări)" % (_ro_date(target_date), len(rows))
    body_html = render_html(rows, target_iso, recipient, _send_hour(db))
    body_text = render_text(rows, target_iso)

    # Calea CLEAN (status='clean' + queue_status='ready_for_cts' + ai_category != 'necunoscut'):
    # exact ce ingestă/confirma CTS pentru emailurile normale. ai_status='done' + ai_category setat
    # => nu mai e reprocesat. from_address @mailguard.cargotrack.ro (intern) => exclus din
    # esantionul de verificare manuala (internal_sender_not_sql).
    eid = db.execute(text(
        "INSERT INTO emails (graph_message_id, received_at, raw_graph_payload, email_headers, subject, "
        "from_address, from_name, to_addresses, body_html, body_text, has_attachments, "
        "status, queue_status, ai_status, ai_category, category) "
        "VALUES (:g, now(), '{}'::jsonb, CAST(:hdrs AS jsonb), :subj, :fa, :fn, "
        "CAST(:to AS jsonb), :bh, :bt, true, "
        "'clean', 'ready_for_cts', 'done', 'informatie', 'ndr_report') RETURNING id"),
        {"g": gmid, "hdrs": json.dumps({"message_id": gmid}), "subj": subject,
         "fa": SENDER_ADDR, "fn": SENDER_NAME,
         "to": json.dumps([recipient]), "bh": body_html, "bt": body_text}).scalar()

    db.execute(text(
        "INSERT INTO attachments (email_id, graph_attachment_id, name, content_type, "
        "size_bytes, storage_path, is_inline, is_suspicious) "
        "VALUES (:eid, :gid, :name, 'text/csv', :sz, :sp, false, false)"),
        {"eid": eid, "gid": "ndr-csv-%s" % target_iso, "name": fname,
         "sz": len(csv_bytes), "sp": fpath})
    db.commit()
    return {"status": "created", "email_id": eid, "date": target_iso,
            "rows": len(rows), "csv": fpath}


# --------------------------------------------------------------------------- #
# Orchestrare (gate zilnic + on-demand)
# --------------------------------------------------------------------------- #
def generate_for_date(target_date, force=False):
    """On-demand (endpoint run-now). Ignora gate-ul de ora/last_report."""
    if isinstance(target_date, str):
        target_date = _date.fromisoformat(target_date)
    db = SessionLocal()
    try:
        recipient = get_setting(db, "ndr_report.recipient", DEFAULT_RECIPIENT) or DEFAULT_RECIPIENT
        rows = collect_rows(db, target_date)
        if not rows:
            return {"status": "empty", "date": target_date.isoformat(), "rows": 0}
        out = create_report_email(db, target_date, rows, render_csv(rows), recipient, force=force)
        logger.info("ndr_report generate_for_date: %s", out)
        return out
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("ndr_report generate_for_date failed: %s", e)
        return {"error": str(e), "date": str(target_date)}
    finally:
        db.close()


def preview_for_date(target_date):
    if isinstance(target_date, str):
        target_date = _date.fromisoformat(target_date)
    db = SessionLocal()
    try:
        recipient = get_setting(db, "ndr_report.recipient", DEFAULT_RECIPIENT) or DEFAULT_RECIPIENT
        rows = collect_rows(db, target_date)
        return {"date": target_date.isoformat(), "count": len(rows), "rows": rows,
                "recipient": recipient,
                "html": render_html(rows, target_date.isoformat(), recipient,
                                    _send_hour(db))}
    finally:
        db.close()


def run_daily_ndr_report_if_due():
    """Best-effort: ruleaza raportul pentru IERI o singura data/zi, dupa ora
    `ndr_report.send_hour` (implicit 9, Europe/Bucharest). Nu arunca niciodata
    catre caller."""
    db = SessionLocal()
    try:
        if not get_setting(db, "ndr_report.enabled", True):
            return {"skipped": "disabled"}

        send_hour = _send_hour(db)
        cur_hour = db.execute(text(
            "SELECT EXTRACT(hour FROM (now() AT TIME ZONE 'Europe/Bucharest'))::int")).scalar()
        if cur_hour is None or int(cur_hour) < send_hour:
            return {"skipped": "too_early", "hour": int(cur_hour or -1), "send_hour": send_hour}

        target = db.execute(text(
            "SELECT ((now() AT TIME ZONE 'Europe/Bucharest')::date - 1)")).scalar()
        tiso = target.isoformat()

        last = get_setting(db, "ndr_report.last_report", None)
        if last and str(last) == tiso:
            return {"skipped": "already_done", "date": tiso}

        recipient = get_setting(db, "ndr_report.recipient", DEFAULT_RECIPIENT) or DEFAULT_RECIPIENT
        rows = collect_rows(db, target)
        if not rows:
            set_setting(db, "ndr_report.last_report", tiso)
            db.commit()
            return {"status": "empty", "date": tiso, "rows": 0}

        out = create_report_email(db, target, rows, render_csv(rows), recipient, force=False)
        set_setting(db, "ndr_report.last_report", tiso)
        db.commit()
        logger.info("ndr_report daily: %s", out)
        return out
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("run_daily_ndr_report_if_due failed: %s", e)
        return {"error": str(e)}
    finally:
        db.close()
