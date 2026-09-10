"""Rapoarte lunare de productivitate — trimitere email automată în prima zi lucrătoare.

Fluxul complet:
  1. Cron (process_now) apelează send_monthly_reports_if_due(db) o dată la 5 min.
  2. is_first_working_day_and_hour(db) verifică: azi e prima zi L-V a lunii AND ora >= 10:00 AND
     nu s-a trimis deja luna aceasta (KV productivity.last_monthly_sent).
  3. Pentru fiecare grup de destinatari din productivity_notifications (enabled=true):
     - Expandează department_group → lista de sluguri
     - Generează summary HTML luna precedentă (department_report)
     - Generează forecast HTML luna curentă (forecast_report)
     - Generează text introductiv via LLM (iris_ai.run_prompt, fallback template)
     - Generează PDF cu tabel analitic zilnic (PyMuPDF/fitz, fallback HTML atașat)
     - Trimite email via noreply_sender.send_with_attachments
  4. Marchează KV productivity.last_monthly_sent = YYYY-MM luna curentă.
"""
from __future__ import annotations

import datetime as _dt
import io
import json
import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Departamente care aparțin grupului Financiar
_FINANCIAR_DEPTS = {"contabilitate", "recuperare_tva"}

_LUNA_LABELS = {
    1: "ianuarie", 2: "februarie", 3: "martie", 4: "aprilie",
    5: "mai", 6: "iunie", 7: "iulie", 8: "august",
    9: "septembrie", 10: "octombrie", 11: "noiembrie", 12: "decembrie",
}

_DEPT_LABELS = {
    "suport_1": "Suport 1", "suport_2": "Suport 2", "suport_3": "Suport 3",
    "taxe_drum": "Taxe de drum", "contabilitate": "Contabilitate",
    "recuperare_tva": "Recuperare TVA", "mobilitate": "Mobilitate",
}

_GROUP_LABELS = {
    "operational": "Operațional",
    "financiar": "Financiar",
    "toate": "Toate departamentele",
}


def _dept_label(slug: str) -> str:
    return _DEPT_LABELS.get(slug, slug.replace("_", " ").title())


def _group_label(group: str) -> str:
    return _GROUP_LABELS.get(group, _dept_label(group))


def _luna_label(month: int) -> str:
    return _LUNA_LABELS.get(month, str(month))


# ── Expandare grup → sluguri ──────────────────────────────────────────────────

def _expand_departments(db: Session, group: str) -> list:
    all_configured = [
        r[0] for r in db.execute(
            text("SELECT department FROM productivity_department_config ORDER BY department")
        ).fetchall()
    ]
    if group == "operational":
        return [d for d in all_configured if d not in _FINANCIAR_DEPTS]
    if group == "financiar":
        return [d for d in all_configured if d in _FINANCIAR_DEPTS]
    if group == "toate":
        return all_configured
    return [group] if group in all_configured else []


# ── Holidays ──────────────────────────────────────────────────────────────────

def _get_holidays(db: Session) -> set:
    row = db.execute(text("SELECT value FROM settings WHERE key='productivity.ro_holidays'")).fetchone()
    if not row:
        return set()
    raw = row[0]
    if isinstance(raw, list):
        return set(raw)
    try:
        return set(json.loads(raw))
    except Exception:
        return set()


def _is_working_day(d: _dt.date, holidays: set) -> bool:
    return d.isoweekday() < 6 and d.isoformat() not in holidays


# ── Gating: prima zi lucrătoare a lunii, ora >= 10:00 ────────────────────────

def is_first_working_day_and_hour(db: Session) -> bool:
    """True dacă azi e prima zi lucrătoare a lunii curentă și ora locală >= 10:00."""
    row = db.execute(
        text("SELECT (CURRENT_TIMESTAMP AT TIME ZONE 'Europe/Bucharest')::date, "
             "EXTRACT(hour FROM CURRENT_TIMESTAMP AT TIME ZONE 'Europe/Bucharest')::int")
    ).fetchone()
    today, hour = row[0], row[1]
    if hour < 10:
        return False
    holidays = _get_holidays(db)
    # Prima zi lucrătoare a lunii = primul d din luna cu isoweekday<6 și nu sărbătoare
    first = _dt.date(today.year, today.month, 1)
    d = first
    while d.month == today.month:
        if _is_working_day(d, holidays):
            return d == today
        d += _dt.timedelta(days=1)
    return False


def _already_sent_this_month(db: Session) -> bool:
    row = db.execute(text("SELECT value FROM settings WHERE key='productivity.last_monthly_sent'")).fetchone()
    if not row:
        return False
    val = row[0]
    if isinstance(val, list):
        val = val[0] if val else ""
    try:
        today = _dt.date.today()
        return str(val).strip('"') == f"{today.year}-{today.month:02d}"
    except Exception:
        return False


def _mark_sent(db: Session) -> None:
    """Marchează luna curentă ca trimisă + salvează momentul exact (`last_monthly_sent_at`).

    `updated_at` al rândului ar fi suficient în teorie, dar orice altă scriere pe cheia asta l-ar
    rescrie; ținem momentul explicit, ca `_recently_sent` să poată face un gard independent.
    """
    today = _dt.date.today()
    val = f"{today.year}-{today.month:02d}"
    db.execute(
        text("INSERT INTO settings(key, value, updated_by, updated_at) "
             "VALUES('productivity.last_monthly_sent', CAST(:v AS jsonb), 'cron', now()) "
             "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_by='cron', updated_at=now()"),
        {"v": json.dumps(val)},
    )
    db.execute(
        text("INSERT INTO settings(key, value, updated_by, updated_at) "
             "VALUES('productivity.last_monthly_sent_at', to_jsonb(now()::text), 'cron', now()) "
             "ON CONFLICT(key) DO UPDATE SET value=to_jsonb(now()::text), updated_by='cron', updated_at=now()"),
    )
    db.commit()


def _recently_sent(db: Session, min_days: int = 25) -> bool:
    """True dacă s-a trimis un raport în ultimele `min_days` zile.

    Gard independent de eticheta de lună: chiar dacă `last_monthly_sent` lipsește sau e coruptă,
    momentul ultimei trimiteri împiedică o a doua rundă în aceeași lună. 25 de zile lasă loc
    pentru luni scurte fără a permite două trimiteri consecutive.
    """
    try:
        row = db.execute(text(
            "SELECT (now() - (value #>> '{}')::timestamptz) < make_interval(days => :d) "
            "FROM settings WHERE key='productivity.last_monthly_sent_at'"
        ), {"d": min_days}).fetchone()
        return bool(row and row[0])
    except Exception:
        logger.warning("productivity_notifier: last_monthly_sent_at check failed", exc_info=True)
        return False


# ── Rapoarte ──────────────────────────────────────────────────────────────────

def _prev_month(year: int, month: int):
    if month == 1:
        return year - 1, 12
    return year, month - 1


def _status_label(status: Optional[str]) -> str:
    return {"atins": "✓ Atins", "partial": "~ Parțial", "sub_minim": "✗ Sub minim",
            "insufficient": "— Insuficient", "fara_obiective": "— Fără obiective"}.get(status or "", status or "—")


def _fmt_pct(v) -> str:
    if v is None:
        return "—"
    return f"{float(v):.2f}%"


def _fmt_num(v) -> str:
    if v is None:
        return "—"
    return f"{float(v):.1f}"


# ── HTML summary + forecast ───────────────────────────────────────────────────

_HTML_STYLE = """
<style>
  body { font-family: Arial, sans-serif; font-size: 14px; color: #222; margin: 0; padding: 20px; }
  h1 { color: #1a3a5c; font-size: 20px; margin-bottom: 4px; }
  h2 { color: #1a3a5c; font-size: 16px; margin: 24px 0 8px; border-bottom: 2px solid #e0e7ef; padding-bottom: 4px; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 16px; }
  th { background: #1a3a5c; color: #fff; padding: 8px 12px; text-align: left; font-size: 13px; }
  td { padding: 7px 12px; border-bottom: 1px solid #e0e7ef; font-size: 13px; }
  tr:nth-child(even) td { background: #f4f7fb; }
  .badge-atins { background: #d1fae5; color: #065f46; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 700; }
  .badge-partial { background: #fef3c7; color: #92400e; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 700; }
  .badge-sub { background: #fee2e2; color: #991b1b; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 700; }
  .badge-na { background: #f3f4f6; color: #6b7280; padding: 2px 8px; border-radius: 10px; font-size: 12px; }
  .intro { color: #374151; line-height: 1.7; margin-bottom: 20px; padding: 14px 18px; background: #f0f6ff; border-left: 4px solid #1a3a5c; border-radius: 4px; }
  .footer { margin-top: 32px; font-size: 12px; color: #9ca3af; border-top: 1px solid #e0e7ef; padding-top: 12px; }
</style>
"""


def _status_badge(status: Optional[str]) -> str:
    cls = {"atins": "badge-atins", "partial": "badge-partial",
           "sub_minim": "badge-sub"}.get(status or "", "badge-na")
    label = _status_label(status)
    return f'<span class="{cls}">{label}</span>'


def _build_summary_table(reports: list) -> str:
    rows = ""
    for r in reports:
        rows += (
            f"<tr>"
            f"<td><b>{_dept_label(r.get('department',''))}</b></td>"
            f"<td>{r.get('zile_lucratoare','—')}</td>"
            f"<td>{_fmt_num(r.get('ore_disponibile'))} h</td>"
            f"<td>{_fmt_pct(r.get('obiectiv_real'))}</td>"
            f"<td>{_fmt_pct(r.get('obiectiv_minim'))}</td>"
            f"<td>{_fmt_pct(r.get('obiectiv_atins'))}</td>"
            f"<td>{_status_badge(r.get('status'))}</td>"
            f"</tr>"
        )
    return (
        "<table>"
        "<tr><th>Departament</th><th>Zile lucr.</th><th>Ore disp.</th>"
        "<th>Obiectiv real</th><th>Obiectiv minim</th><th>Realizat</th><th>Status</th></tr>"
        + rows + "</table>"
    )


def _build_forecast_table(reports: list) -> str:
    rows = ""
    for r in reports:
        rows += (
            f"<tr>"
            f"<td><b>{_dept_label(r.get('department',''))}</b></td>"
            f"<td>{r.get('zile_lucratoare','—')}</td>"
            f"<td>{_fmt_num(r.get('ore_planificate'))} h</td>"
            f"<td>{_fmt_num(r.get('ore_concediu', 0))} h</td>"
            f"<td>{_fmt_num(r.get('ore_disponibile'))} h</td>"
            f"<td>{_fmt_pct(r.get('obiectiv_real'))}</td>"
            f"<td>{_fmt_pct(r.get('obiectiv_minim'))}</td>"
            f"</tr>"
        )
    return (
        "<table>"
        "<tr><th>Departament</th><th>Zile lucr.</th><th>Ore planif.</th>"
        "<th>Ore concediu</th><th>Ore disp.</th><th>Obiectiv real</th><th>Obiectiv minim</th></tr>"
        + rows + "</table>"
    )


# ── Generare text AI ──────────────────────────────────────────────────────────

def _generate_ai_summary(group: str, group_lbl: str, prev_year: int, prev_month: int,
                          curr_year: int, curr_month: int,
                          summary_reports: list, forecast_reports: list) -> str:
    try:
        from app.services import iris_ai
        if not iris_ai.is_configured():
            raise RuntimeError("AI not configured")

        prev_lbl = f"{_luna_label(prev_month)} {prev_year}"
        curr_lbl = f"{_luna_label(curr_month)} {curr_year}"

        # Construiesc context compact
        ctx_parts = [f"Raport productivitate {group_lbl} — {prev_lbl} (luna încheiată):\n"]
        for r in summary_reports:
            ctx_parts.append(
                f"  {_dept_label(r.get('department',''))}: "
                f"realizat={_fmt_pct(r.get('obiectiv_atins'))}, "
                f"target_real={_fmt_pct(r.get('obiectiv_real'))}, "
                f"status={r.get('status','—')}, "
                f"zile_lucratoare={r.get('zile_lucratoare','—')}, "
                f"ore_disponibile={_fmt_num(r.get('ore_disponibile'))}h"
            )
        ctx_parts.append(f"\nEstimare productivitate {group_lbl} — {curr_lbl} (luna în curs):\n")
        for r in forecast_reports:
            ctx_parts.append(
                f"  {_dept_label(r.get('department',''))}: "
                f"ore_planificate={_fmt_num(r.get('ore_planificate'))}h, "
                f"ore_concediu={_fmt_num(r.get('ore_concediu',0))}h, "
                f"ore_disponibile={_fmt_num(r.get('ore_disponibile'))}h, "
                f"obiectiv_real={_fmt_pct(r.get('obiectiv_real'))}, "
                f"zile_lucratoare={r.get('zile_lucratoare','—')}"
            )

        system = (
            "Ești IRIS, asistentul AI al CargoTrack Solutions. Scrii un scurt paragraf de introducere "
            "pentru un raport de productivitate trimis managerilor. Tonul e profesional, concis, în română. "
            "Evidențiezi punctele cheie: performanța față de obiectiv, tendințe, și capacitatea estimată "
            "pentru luna curentă. Maximum 5-6 propoziții. Fără liste, fără titluri, doar text continuu."
        )
        result = iris_ai.run_prompt(system, "\n".join(ctx_parts),
                                    task="productivity_summary", model_hint="sonnet",
                                    max_tokens=400, temperature=0.3)
        if result.get("ok") and result.get("text"):
            return result["text"].strip()
    except Exception as e:
        logger.warning("AI summary generation failed: %s", e)

    # Fallback template
    lines = []
    for r in summary_reports:
        st = r.get("status", "")
        real = _fmt_pct(r.get("obiectiv_real"))
        atins = _fmt_pct(r.get("obiectiv_atins"))
        dept = _dept_label(r.get("department", ""))
        if st == "atins":
            lines.append(f"{dept} a atins obiectivul ({atins} față de target {real}).")
        elif st == "partial":
            lines.append(f"{dept} a atins parțial obiectivul ({atins} față de target {real}).")
        else:
            lines.append(f"{dept}: realizat {atins}, target {real}.")
    return " ".join(lines)


# ── Generare PDF (PyMuPDF / fallback HTML) ────────────────────────────────────

def _generate_pdf(db: Session, group_lbl: str, prev_year: int, prev_month: int,
                  depts: list, summary_reports: list, forecast_reports: list) -> tuple:
    """Returnează (bytes, mime_type, filename) sau (None, None, None) la eșec."""
    prev_lbl = f"{_luna_label(prev_month).title()} {prev_year}"
    filename_base = f"productivitate_{prev_year}_{prev_month:02d}_{group_lbl.lower().replace(' ','_')}"

    try:
        import fitz  # PyMuPDF
        return _pdf_with_fitz(db, group_lbl, prev_year, prev_month, depts,
                              summary_reports, forecast_reports, prev_lbl, filename_base)
    except ImportError:
        pass
    except Exception as e:
        logger.warning("PDF generation with fitz failed: %s", e)

    return None, None, None


def _pdf_with_fitz(db, group_lbl, prev_year, prev_month, depts,
                   summary_reports, forecast_reports, prev_lbl, filename_base):
    import fitz
    from app.services.productivity import analytics_report

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4
    y = 40
    margin_l, margin_r = 40, 555

    def write_line(page, y, text, size=11, bold=False, color=(0, 0, 0)):
        font = "helv"
        page.insert_text((margin_l, y), text, fontname=font, fontsize=size, color=color)
        return y + size + 4

    # Titlu
    y = write_line(page, y, f"Raport productivitate — {group_lbl} — {prev_lbl}", size=14, color=(0.1, 0.22, 0.36))
    y = write_line(page, y, f"Generat: {_dt.date.today().isoformat()}", size=9, color=(0.5, 0.5, 0.5))
    y += 8

    # Tabel summary
    y = write_line(page, y, "Performanță luna precedentă", size=12, color=(0.1, 0.22, 0.36))
    y += 4
    headers = ["Departament", "Zile", "Ore disp.", "Obiectiv real", "Realizat", "Status"]
    col_w = [120, 40, 65, 85, 70, 80]
    col_x = [margin_l]
    for w in col_w[:-1]:
        col_x.append(col_x[-1] + w)

    # Header row
    for i, h in enumerate(headers):
        page.draw_rect(fitz.Rect(col_x[i], y - 2, col_x[i] + col_w[i], y + 13), color=(0.1, 0.22, 0.36), fill=(0.1, 0.22, 0.36))
        page.insert_text((col_x[i] + 3, y + 9), h, fontname="helv", fontsize=9, color=(1, 1, 1))
    y += 16

    for ri, r in enumerate(summary_reports):
        bg = (0.95, 0.97, 0.99) if ri % 2 == 0 else (1, 1, 1)
        for i in range(len(headers)):
            page.draw_rect(fitz.Rect(col_x[i], y - 2, col_x[i] + col_w[i], y + 12), color=bg, fill=bg)
        vals = [
            _dept_label(r.get("department", "")),
            str(r.get("zile_lucratoare", "—")),
            _fmt_num(r.get("ore_disponibile")) + "h",
            _fmt_pct(r.get("obiectiv_real")),
            _fmt_pct(r.get("obiectiv_atins")),
            _status_label(r.get("status")),
        ]
        for i, v in enumerate(vals):
            page.insert_text((col_x[i] + 3, y + 9), str(v)[:20], fontname="helv", fontsize=9, color=(0.1, 0.1, 0.1))
        y += 15

    y += 12
    if y > 750:
        page = doc.new_page(width=595, height=842)
        y = 40

    # Tabel forecast
    y = write_line(page, y, "Estimare luna curentă (prognoză)", size=12, color=(0.1, 0.22, 0.36))
    y += 4
    fheaders = ["Departament", "Zile", "Planif.", "Concediu", "Disponibil", "Obiectiv real", "Minim"]
    fcol_w = [110, 38, 55, 60, 65, 82, 60]
    fcol_x = [margin_l]
    for w in fcol_w[:-1]:
        fcol_x.append(fcol_x[-1] + w)

    for i, h in enumerate(fheaders):
        page.draw_rect(fitz.Rect(fcol_x[i], y - 2, fcol_x[i] + fcol_w[i], y + 13), color=(0.1, 0.22, 0.36), fill=(0.1, 0.22, 0.36))
        page.insert_text((fcol_x[i] + 3, y + 9), h, fontname="helv", fontsize=9, color=(1, 1, 1))
    y += 16

    for ri, r in enumerate(forecast_reports):
        bg = (0.95, 0.97, 0.99) if ri % 2 == 0 else (1, 1, 1)
        for i in range(len(fheaders)):
            page.draw_rect(fitz.Rect(fcol_x[i], y - 2, fcol_x[i] + fcol_w[i], y + 12), color=bg, fill=bg)
        vals = [
            _dept_label(r.get("department", "")),
            str(r.get("zile_lucratoare", "—")),
            _fmt_num(r.get("ore_planificate")) + "h",
            _fmt_num(r.get("ore_concediu", 0)) + "h",
            _fmt_num(r.get("ore_disponibile")) + "h",
            _fmt_pct(r.get("obiectiv_real")),
            _fmt_pct(r.get("obiectiv_minim")),
        ]
        for i, v in enumerate(vals):
            page.insert_text((fcol_x[i] + 3, y + 9), str(v)[:20], fontname="helv", fontsize=9, color=(0.1, 0.1, 0.1))
        y += 15

    # Grafic bare orizontale — Obiectiv atins vs Real (luna precedentă)
    y += 14
    if y > 700:
        page = doc.new_page(width=595, height=842)
        y = 40
    y = write_line(page, y, "Grafic performanță — obiectiv atins vs real", size=12, color=(0.1, 0.22, 0.36))
    y += 6
    bar_x = margin_l + 130  # start bare
    bar_max_w = 300          # lățime maximă bară
    bar_h = 11
    bar_gap = 7

    for r in summary_reports:
        dept_lbl = _dept_label(r.get("department", ""))[:22]
        atins = float(r.get("obiectiv_atins") or 0)
        real = float(r.get("obiectiv_real") or 1)
        pct = min(atins / real, 1.0) if real > 0 else 0
        status = r.get("status", "")
        if status == "atins":
            bar_color = (0.18, 0.6, 0.27)
        elif status == "partial":
            bar_color = (0.85, 0.6, 0.1)
        else:
            bar_color = (0.8, 0.2, 0.2)

        # Label departament
        page.insert_text((margin_l, y + bar_h - 1), dept_lbl, fontname="helv", fontsize=8, color=(0.2, 0.2, 0.2))
        # Background bara (gri)
        page.draw_rect(fitz.Rect(bar_x, y, bar_x + bar_max_w, y + bar_h),
                       color=(0.88, 0.88, 0.88), fill=(0.88, 0.88, 0.88))
        # Bara colorată proporțional
        fill_w = max(4, int(bar_max_w * pct))
        page.draw_rect(fitz.Rect(bar_x, y, bar_x + fill_w, y + bar_h),
                       color=bar_color, fill=bar_color)
        # Procent text
        pct_lbl = f"{atins:.1f} / {real:.1f}  ({pct*100:.0f}%)"
        page.insert_text((bar_x + bar_max_w + 6, y + bar_h - 1), pct_lbl,
                         fontname="helv", fontsize=8, color=(0.3, 0.3, 0.3))
        y += bar_h + bar_gap

    # Legendă
    y += 4
    for lbl, col in [("Atins", (0.18, 0.6, 0.27)), ("Parțial", (0.85, 0.6, 0.1)), ("Sub minim", (0.8, 0.2, 0.2))]:
        page.draw_rect(fitz.Rect(margin_l, y, margin_l + 12, y + 9), color=col, fill=col)
        page.insert_text((margin_l + 16, y + 8), lbl, fontname="helv", fontsize=8, color=(0.3, 0.3, 0.3))
        margin_l += 70
    margin_l = 40  # reset

    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue(), "application/pdf", f"{filename_base}.pdf"


def _build_pdf_html(group_lbl, prev_lbl, summary_reports, forecast_reports) -> str:
    s_table = _build_summary_table(summary_reports)
    f_table = _build_forecast_table(forecast_reports)
    return f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'>{_HTML_STYLE}</head><body>
<h1>Raport productivitate — {group_lbl} — {prev_lbl}</h1>
<h2>Performanță luna precedentă</h2>{s_table}
<h2>Estimare luna curentă (prognoză)</h2>{f_table}
<div class='footer'>Generat automat de IRIS · CargoTrack Solutions</div>
</body></html>"""


# ── Email HTML complet ────────────────────────────────────────────────────────

def _build_email_html(group_lbl: str, prev_lbl: str, curr_lbl: str,
                      intro_text: str, summary_reports: list, forecast_reports: list) -> str:
    s_table = _build_summary_table(summary_reports)
    f_table = _build_forecast_table(forecast_reports)
    return f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'>{_HTML_STYLE}</head><body>
<h1>Rezumat productivitate {prev_lbl} — {group_lbl}</h1>
<div class='intro'>{intro_text}</div>
<h2>Performanță {prev_lbl} (lună încheiată)</h2>
{s_table}
<h2>Estimare productivitate {curr_lbl} (lună în curs)</h2>
{f_table}
<div class='footer'>
  Raport generat automat de IRIS · CargoTrack Solutions<br>
  Trimis în prima zi lucrătoare a lunii. PDF analitic atașat.
</div>
</body></html>"""


# ── Trimitere email cu atașament ──────────────────────────────────────────────

def _send_email(db: Session, to_email: str, subject: str, html_body: str,
                attachment_data: Optional[bytes], attachment_mime: Optional[str],
                attachment_name: Optional[str]) -> bool:
    try:
        from app.services import noreply_sender
        cfg = noreply_sender.get_noreply_config(db)
        if not cfg:
            logger.error("noreply_smtp_config missing — cannot send productivity report")
            return False

        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.application import MIMEApplication

        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = cfg["from_address"]
        msg["To"] = to_email
        msg["X-Auto-Response-Suppress"] = "OOF, AutoReply"
        msg["Auto-Submitted"] = "auto-generated"

        # Body alternativ (text + html)
        alt = MIMEMultipart("alternative")
        plain = _strip_html(html_body)
        alt.attach(MIMEText(plain, "plain", "utf-8"))
        full_html = f"<html><body style='font-family:Arial,sans-serif;'>{html_body}</body></html>"
        alt.attach(MIMEText(full_html, "html", "utf-8"))
        msg.attach(alt)

        # Atașament PDF/HTML
        if attachment_data and attachment_name:
            part = MIMEApplication(attachment_data, Name=attachment_name)
            part["Content-Disposition"] = f'attachment; filename="{attachment_name}"'
            if attachment_mime:
                part.set_type(attachment_mime)
            msg.attach(part)

        from app.services.credential_crypto import decrypt_credentials
        password = decrypt_credentials(cfg["smtp_pass_enc"]).get("password", "")
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=20) as server:
            if cfg["use_tls"]:
                server.starttls()
            server.login(cfg["smtp_user"], password)
            server.sendmail(cfg["from_address"], [to_email], msg.as_string())
        logger.info("productivity report sent to %s", to_email)
        return True
    except Exception:
        logger.exception("failed to send productivity report to %s", to_email)
        return False


def _strip_html(html: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", html).strip()


# ── Funcție principală ────────────────────────────────────────────────────────

def send_monthly_reports(db: Session) -> dict:
    """Trimite rapoarte lunare la toți destinatarii activi. Returnează dict cu statistici."""
    from app.services.productivity import department_report, forecast_report

    today = _dt.date.today()
    curr_year, curr_month = today.year, today.month
    prev_year, prev_month = _prev_month(curr_year, curr_month)
    prev_lbl = f"{_luna_label(prev_month).title()} {prev_year}"
    curr_lbl = f"{_luna_label(curr_month).title()} {curr_year}"

    # Citește destinatari activi
    rows = db.execute(
        text("SELECT id, email, department_group FROM productivity_notifications WHERE enabled=true ORDER BY department_group, email")
    ).fetchall()
    if not rows:
        logger.info("productivity_notifier: no active recipients")
        return {"sent": 0, "errors": 0, "skipped": 0}

    # Grupează per department_group (generăm raportul o singură dată per grup)
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for _, email, group in rows:
        groups[group].append(email)

    sent = errors = 0
    for group, recipients in groups.items():
        try:
            depts = _expand_departments(db, group)
            if not depts:
                logger.warning("productivity_notifier: group %s has no configured departments", group)
                continue

            group_lbl = _group_label(group)

            # Rapoarte luna precedentă
            summary_reports = []
            for dept in depts:
                try:
                    r = department_report(db, dept, prev_year, prev_month)
                    summary_reports.append(r)
                except Exception as e:
                    logger.warning("department_report failed for %s %d-%02d: %s", dept, prev_year, prev_month, e)

            # Forecast luna curentă
            forecast_reports = []
            for dept in depts:
                try:
                    r = forecast_report(db, dept, curr_year, curr_month)
                    forecast_reports.append(r)
                except Exception as e:
                    logger.warning("forecast_report failed for %s %d-%02d: %s", dept, curr_year, curr_month, e)

            if not summary_reports and not forecast_reports:
                logger.warning("productivity_notifier: no data for group %s", group)
                continue

            # Text introductiv AI
            intro = _generate_ai_summary(group, group_lbl, prev_year, prev_month,
                                         curr_year, curr_month, summary_reports, forecast_reports)

            # PDF
            att_data, att_mime, att_name = _generate_pdf(
                db, group_lbl, prev_year, prev_month, depts, summary_reports, forecast_reports
            )

            # Email HTML
            html_body = _build_email_html(group_lbl, prev_lbl, curr_lbl,
                                          intro, summary_reports, forecast_reports)
            subject = f"Rezumat productivitate {prev_lbl} — {group_lbl}"

            # Trimite la fiecare destinatar
            for email in recipients:
                ok = _send_email(db, email, subject, html_body, att_data, att_mime, att_name)
                if ok:
                    sent += 1
                else:
                    errors += 1

            # Audit log
            # Coloana e `actor`, NU `user_id` (vezi \d audit_log). Varianta greșită arunca
            # `psycopg2.errors.SyntaxError`, ceea ce lăsa tranzacția abortată; `_mark_sent` de
            # mai jos eșua atunci în cascadă, KV-ul `productivity.last_monthly_sent` nu se
            # scria niciodată, iar gating-ul din `send_monthly_reports_if_due` nu mai reținea
            # nimic — deci cron-ul (la 5 min) retrimitea raportul la fiecare rulare.
            # Incident 2026-08-03: 5 emailuri duplicate primite, audit_log gol.
            try:
                db.execute(
                    text("INSERT INTO audit_log(action, actor, details, created_at) "
                         "VALUES('productivity_report_sent', 'cron', CAST(:d AS jsonb), now())"),
                    {"d": json.dumps({"group": group, "recipients": recipients,
                                      "month": f"{prev_year}-{prev_month:02d}",
                                      "depts": depts, "sent": len(recipients)})},
                )
                db.commit()
            except Exception:
                # Auditul e secundar: rollback explicit ca o eroare aici să NU lase sesiunea
                # abortată și să împiedice `_mark_sent` (singura protecție anti-duplicat).
                logger.warning("audit_log insert failed for productivity report", exc_info=True)
                try:
                    db.rollback()
                except Exception:
                    pass

        except Exception:
            logger.exception("productivity_notifier: error processing group %s", group)
            errors += 1
            try:
                db.rollback()
            except Exception:
                pass

    # Marcăm luna ca trimisă chiar dacă un grup a eșuat: altfel cron-ul reia la fiecare 5 minute
    # și inundă destinatarii cu duplicate ale grupurilor care AU reușit.
    if sent > 0:
        _mark_sent(db)
    return {"sent": sent, "errors": errors}


def send_monthly_reports_if_due(db: Session) -> dict:
    """Entry point pentru cron (rulează la 5 min). Verifică gating înainte de trimitere.

    Trei porți, nu una: ziua/ora, eticheta de lună, apoi momentul ultimei trimiteri. A treia e
    plasa de siguranță — dacă eticheta de lună nu se scrie (cum s-a întâmplat pe 03.08.2026, când
    un INSERT invalid în audit_log abortase tranzacția), momentul ultimei trimiteri tot oprește
    retrimiterea la următoarea rulare de cron.
    """
    if not is_first_working_day_and_hour(db):
        return {"skipped": True, "reason": "not_first_working_day_or_before_10h"}
    if _already_sent_this_month(db):
        return {"skipped": True, "reason": "already_sent_this_month"}
    if _recently_sent(db):
        return {"skipped": True, "reason": "sent_within_last_25_days"}
    return send_monthly_reports(db)
