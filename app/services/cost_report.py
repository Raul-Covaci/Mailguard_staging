"""Generare raport costuri AI (PDF) din ai_call_log — tabele + grafice.

Folosește PyMuPDF (fitz). Fontul Unicode (DejaVuSans) e împachetat în
app/assets/fonts/ ca raportul să arate identic pe staging și producție; dacă
lipsește, se cade pe Helvetica + translitere ASCII (fără diacritice).

Date de intrare (deja agregate de endpoint):
  totals        : {"calls","cost","tokens_in","tokens_out","errors"}
  by_model      : [{"model","calls","cost","tokens_in","tokens_out"}]
  by_task       : [{"task","calls","cost","tokens_in","tokens_out","errors","top_model","top_share"}]
  by_task_model : [{"task","model","calls","cost"}]
  meta          : {"app_name","app_env","app_version","date_from","date_to","generated_at"}
"""
import os
import fitz  # PyMuPDF

# ── Paletă ──────────────────────────────────────────────────────────────────
INK    = (0.17, 0.17, 0.16)
MUTED  = (0.42, 0.42, 0.40)
BORDER = (0.80, 0.80, 0.78)
HEADBG = (0.93, 0.93, 0.91)
ACCENT = (0.15, 0.39, 0.92)
GREEN  = (0.20, 0.55, 0.20)
RED    = (0.74, 0.18, 0.18)
BAR_PALETTE = [(0.15, 0.39, 0.92), (0.20, 0.55, 0.20), (0.85, 0.45, 0.10),
               (0.55, 0.30, 0.75), (0.10, 0.60, 0.65), (0.80, 0.25, 0.45),
               (0.45, 0.45, 0.45), (0.60, 0.50, 0.10)]

PAGE_W, PAGE_H = 595.0, 842.0
MARGIN = 42.0
CONTENT_W = PAGE_W - 2 * MARGIN

_HERE = os.path.dirname(os.path.abspath(__file__))
_FONT_REG_CANDIDATES = [
    os.path.join(_HERE, "..", "assets", "fonts", "DejaVuSans.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_FONT_BOLD_CANDIDATES = [
    os.path.join(_HERE, "..", "assets", "fonts", "DejaVuSans-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
ASCII_MAP = str.maketrans({
    "ă": "a", "â": "a", "î": "i", "ș": "s", "ş": "s", "ț": "t", "ţ": "t",
    "Ă": "A", "Â": "A", "Î": "I", "Ș": "S", "Ş": "S", "Ț": "T", "Ţ": "T",
    "„": '"', "”": '"', "…": "...", "→": "->", "×": "x", "·": "-",
})


def _first_existing(paths):
    for p in paths:
        if os.path.exists(p):
            return p
    return None


def model_label(m):
    if not m or m == "?":
        return "necunoscut"
    if m == "curated":
        return "IRIS (curated)"
    if m == "gemma":
        return "Gemma (local)"
    return m


def usd(v, dec=4):
    try:
        return "$" + format(float(v or 0), ",." + str(dec) + "f")
    except Exception:
        return "$0"


def grp(n):
    try:
        return format(int(n or 0), ",d")
    except Exception:
        return str(n)


class Report:
    def __init__(self, meta):
        self.doc = fitz.open()
        self.meta = meta
        reg = _first_existing(_FONT_REG_CANDIDATES)
        bold = _first_existing(_FONT_BOLD_CANDIDATES)
        if reg and bold:
            self.reg_path, self.bold_path = reg, bold
            self.font_reg = fitz.Font(fontfile=reg)
            self.font_bold = fitz.Font(fontfile=bold)
            self.reg_tag, self.bold_tag = "F0", "F1"
            self.unicode = True
        else:
            self.reg_path = self.bold_path = None
            self.font_reg = fitz.Font("helv")
            self.font_bold = fitz.Font("hebo")
            self.reg_tag, self.bold_tag = "helv", "hebo"
            self.unicode = False
        self.ell = "…" if self.unicode else "..."
        self.page = None
        self.y = 0.0
        self.pageno = 0
        self.new_page()

    # ── text helpers ─────────────────────────────────────────────────────────
    def _tx(self, s):
        s = "" if s is None else str(s)
        return s if self.unicode else s.translate(ASCII_MAP)

    def _font(self, bold):
        return self.font_bold if bold else self.font_reg

    def width(self, s, fs, bold=False):
        return self._font(bold).text_length(self._tx(s), fontsize=fs)

    def clip(self, s, maxw, fs, bold=False):
        s = self._tx(s)
        if self.width(s, fs, bold) <= maxw:
            return s
        while s and self.width(s + self.ell, fs, bold) > maxw:
            s = s[:-1]
        return s + self.ell

    def text(self, pt, s, fs, bold=False, color=INK):
        self.page.insert_text(pt, self._tx(s), fontsize=fs, fontname=(self.bold_tag if bold else self.reg_tag), color=color)

    def cell(self, x, y, w, h, s, fs, align="l", color=INK, bold=False, pad=5):
        s = self.clip(s, w - 2 * pad, fs, bold)
        tw = self.width(s, fs, bold)
        if align == "r":
            tx = x + w - pad - tw
        elif align == "c":
            tx = x + (w - tw) / 2.0
        else:
            tx = x + pad
        self.text((tx, y + h - (h - fs) / 2.0 - 1), s, fs, bold, color)

    # ── pagină ───────────────────────────────────────────────────────────────
    def new_page(self):
        self.page = self.doc.new_page(width=PAGE_W, height=PAGE_H)
        if self.unicode:
            self.page.insert_font(fontname=self.reg_tag, fontfile=self.reg_path)
            self.page.insert_font(fontname=self.bold_tag, fontfile=self.bold_path)
        self.pageno += 1
        self.y = MARGIN
        self._footer()
        if self.pageno == 1:
            self._title()
        else:
            self.text((MARGIN, self.y + 4), "Raport costuri AI — continuare", 9, color=MUTED)
            self.y += 16
        return self.page

    def _footer(self):
        yy = PAGE_H - 26
        self.page.draw_line((MARGIN, yy), (PAGE_W - MARGIN, yy), color=BORDER, width=0.5)
        left = self.meta.get("app_name", "Cargo360") + " · raport costuri AI"
        right = "pagina " + str(self.pageno)
        self.cell(MARGIN, yy, CONTENT_W - 80, 16, left, 7.5, "l", MUTED)
        self.cell(PAGE_W - MARGIN - 80, yy, 80, 16, right, 7.5, "r", MUTED)

    def _title(self):
        self.text((MARGIN, self.y + 8), "Raport costuri AI", 20, bold=True)
        env = (self.meta.get("app_env") or "").upper()
        self.text((MARGIN, self.y + 26), self.meta.get("app_name", "Cargo360") + ("  ·  " + env if env else ""), 10, color=MUTED)
        self.text((MARGIN, self.y + 42),
                  "Perioadă: " + self.meta.get("date_from", "?") + "  →  " + self.meta.get("date_to", "?")
                  + "      Generat: " + self.meta.get("generated_at", ""), 10)
        self.y += 60
        self.page.draw_line((MARGIN, self.y), (PAGE_W - MARGIN, self.y), color=BORDER, width=0.7)
        self.y += 16

    def ensure(self, need):
        if self.y + need > PAGE_H - 40:
            self.new_page()

    def section(self, title, note=None):
        self.ensure(46)
        self.text((MARGIN, self.y + 10), title, 13, bold=True)
        self.y += 16
        if note:
            self.text((MARGIN, self.y + 8), note, 8.5, color=MUTED)
            self.y += 12
        self.y += 4

    def cards(self, items):
        n = len(items)
        gap = 10
        cw = (CONTENT_W - gap * (n - 1)) / n
        ch = 50
        self.ensure(ch + 10)
        x = MARGIN
        for (lbl, val, sub, col) in items:
            r = fitz.Rect(x, self.y, x + cw, self.y + ch)
            self.page.draw_rect(r, color=BORDER, fill=(0.985, 0.985, 0.975), width=0.6)
            self.page.draw_line((x, self.y), (x, self.y + ch), color=col, width=2.5)
            self.cell(x + 4, self.y + 4, cw - 8, 14, lbl, 8, "l", MUTED)
            self.cell(x + 4, self.y + 18, cw - 8, 18, str(val), 14, "l", INK, bold=True)
            if sub:
                self.cell(x + 4, self.y + 36, cw - 8, 12, sub, 7.5, "l", MUTED)
            x += cw + gap
        self.y += ch + 14

    def table(self, headers, col_w, rows, aligns, fs=9, total_row=None, money_cols=()):
        rowh = fs + 9
        x0 = MARGIN
        tot_w = sum(col_w)

        def header():
            self.page.draw_rect(fitz.Rect(x0, self.y, x0 + tot_w, self.y + rowh), fill=HEADBG, color=BORDER, width=0.5)
            cx = x0
            for j, htext in enumerate(headers):
                self.cell(cx, self.y, col_w[j], rowh, htext, fs, aligns[j], INK, bold=True)
                cx += col_w[j]
            self.y += rowh

        self.ensure(rowh * 2)
        header()
        for r in rows:
            if self.y + rowh > PAGE_H - 40:
                self.new_page(); header()
            self.page.draw_rect(fitz.Rect(x0, self.y, x0 + tot_w, self.y + rowh), color=BORDER, width=0.3)
            cx = x0
            for j, val in enumerate(r):
                s = str(val)
                col = RED if (j in money_cols and s not in ("$0.0000", "$0.00", "$0")) else INK
                self.cell(cx, self.y, col_w[j], rowh, s, fs, aligns[j], col)
                cx += col_w[j]
            self.y += rowh
        if total_row is not None:
            if self.y + rowh > PAGE_H - 40:
                self.new_page(); header()
            self.page.draw_rect(fitz.Rect(x0, self.y, x0 + tot_w, self.y + rowh), fill=(0.96, 0.96, 0.94), color=BORDER, width=0.6)
            cx = x0
            for j, val in enumerate(total_row):
                self.cell(cx, self.y, col_w[j], rowh, str(val), fs, aligns[j], INK, bold=True)
                cx += col_w[j]
            self.y += rowh
        self.y += 12

    def hbar(self, title, items, value_fmt):
        items = [it for it in items if it[1] is not None]
        rows = max(len(items), 1)
        rowh = 18
        self.ensure(rows * rowh + 30)
        self.text((MARGIN, self.y + 9), title, 11, bold=True)
        self.y += 16
        top = self.y
        label_w, val_w = 150.0, 70.0
        chart_x = MARGIN + label_w
        chart_w = CONTENT_W - label_w - val_w
        maxv = max([v for _, v, _ in items] + [0.000001])
        for i, (lbl, val, col) in enumerate(items):
            cy = top + i * rowh
            bh = 12.0
            self.cell(MARGIN, cy, label_w, rowh, lbl, 8.5, "l", INK, pad=0)
            bw = (chart_w * (val / maxv)) if maxv else 0
            if val > 0 and bw < 1.5:
                bw = 1.5
            self.page.draw_rect(fitz.Rect(chart_x, cy + (rowh - bh) / 2, chart_x + max(bw, 0.5), cy + (rowh - bh) / 2 + bh),
                                fill=col, color=col, width=0)
            self.text((chart_x + bw + 5, cy + rowh / 2 + 3), value_fmt(val), 8, color=MUTED)
        self.y = top + rows * rowh + 12

    def share_bar(self, title, segments, note=None):
        """Bară 100% stacked: segmente proporționale + legendă cu count & %."""
        segments = [(l, float(v or 0), c) for (l, v, c) in segments]
        total = sum(v for _, v, _ in segments) or 1.0
        self.ensure(34 + 16 * len(segments) + 30)
        self.text((MARGIN, self.y + 9), title, 11, bold=True)
        self.y += 16
        if note:
            self.text((MARGIN, self.y + 8), note, 8.5, color=MUTED)
            self.y += 12
        bar_h = 26.0
        x = MARGIN
        top = self.y
        for (lbl, val, col) in segments:
            seg_w = CONTENT_W * (val / total)
            if seg_w <= 0:
                continue
            self.page.draw_rect(fitz.Rect(x, top, x + seg_w, top + bar_h), fill=col, color=(1, 1, 1), width=0.8)
            pct = 100.0 * val / total
            lab = format(pct, ".1f") + "%"
            if self.width(lab, 9, True) + 8 <= seg_w:
                self.cell(x, top, seg_w, bar_h, lab, 9, "c", (1, 1, 1), bold=True)
            x += seg_w
        self.y = top + bar_h + 8
        # legendă
        lx = MARGIN
        for (lbl, val, col) in segments:
            pct = 100.0 * val / total
            sw = 9.0
            self.page.draw_rect(fitz.Rect(lx, self.y + 1, lx + sw, self.y + 1 + sw), fill=col, color=col, width=0)
            txt = lbl + "  " + grp(val) + " (" + format(pct, ".1f") + "%)"
            self.text((lx + sw + 5, self.y + 9), txt, 8.5, color=INK)
            lx += sw + 9 + self.width(txt, 8.5) + 18
            if lx > PAGE_W - MARGIN - 120:
                lx = MARGIN
                self.y += 14
        self.y += 18

    def bytes(self):
        return self.doc.tobytes(deflate=True)


# ── T3-R1: fluxuri — aceeași mapare task → flux în tot raportul (PDF, CSV) ──────────────────────────
FLOW_ORDER = ["scoring", "call_category", "email_classification", "documente", "op_series",
              "satisfaction", "client_context", "altele"]
_EMAIL_CLASSIFICATION = ("email_category", "email_department", "email_priority", "email_assignee",
                         "intent_gate")


def task_flow(task) -> str:
    """Fluxul unui task din ai_call_log (cu sau fără prefixul `cargo360:` și sufixele de hash)."""
    t = (task or "").strip()
    if t.startswith("cargo360:"):
        t = t[len("cargo360:"):]
    fn = t.split(":", 1)[0]
    if fn.startswith("call_score_"):
        return "scoring"
    if fn == "call_category":
        return "call_category"
    if fn.startswith(_EMAIL_CLASSIFICATION):
        return "email_classification"
    if fn.startswith("doc_"):
        return "documente"
    if fn == "op_series":
        return "op_series"
    if fn.startswith("satisfaction"):
        return "satisfaction"
    if fn.startswith("client_context"):
        return "client_context"
    return "altele"


def aggregate_flows(by_task):
    """Rânduri pe flux din rândurile pe task (sumele pe flux = totalul raportului)."""
    acc = {f: {"flow": f, "calls": 0, "cost": 0.0, "errors": 0, "failed_cost": 0.0} for f in FLOW_ORDER}
    for t in by_task:
        a = acc[task_flow(t.get("task"))]
        a["calls"] += int(t.get("calls") or 0)
        a["cost"] += float(t.get("cost") or 0)
        a["errors"] += int(t.get("errors") or 0)
        a["failed_cost"] += float(t.get("failed_cost") or 0)
    out = []
    for f in FLOW_ORDER:
        a = acc[f]
        if not a["calls"]:
            continue
        a["cost_per_call"] = a["cost"] / a["calls"]
        out.append(a)
    return out


def _pct(part, whole):
    return ("%.1f%%" % (100.0 * part / whole)) if whole else "—"


def generate_cost_report_pdf(meta, totals, by_model, by_task, by_task_model, by_flow=None,
                             cache_savings=None):
    rep = Report(meta)

    rep.cards([
        ("Interogări total", grp(totals.get("calls", 0)), str(totals.get("errors", 0)) + " erori", ACCENT),
        ("Cost total", usd(totals.get("cost", 0), 2), "în perioada selectată", RED),
        ("Tokens in", grp(totals.get("tokens_in", 0)), None, GREEN),
        ("Tokens out", grp(totals.get("tokens_out", 0)), None, GREEN),
    ])

    rep.section("Per model AI", "IRIS (curated) și Gemma rulează local — cost $0. Costul real provine din modelele Claude (Haiku/Sonnet).")
    rows = [[model_label(m.get("model")), grp(m.get("calls")), usd(m.get("cost")),
             grp(m.get("tokens_in")), grp(m.get("tokens_out"))] for m in by_model]
    total_row = ["TOTAL", grp(totals.get("calls", 0)), usd(totals.get("cost", 0)),
                 grp(totals.get("tokens_in", 0)), grp(totals.get("tokens_out", 0))]
    rep.table(["Model", "Interogări", "Cost", "Tokens in", "Tokens out"],
              [150, 75, 90, 115, 115], rows, ["l", "r", "r", "r", "r"], total_row=total_row, money_cols=(2,))

    bar_items = [(model_label(m.get("model")), float(m.get("cost") or 0), BAR_PALETTE[i % len(BAR_PALETTE)])
                 for i, m in enumerate(sorted(by_model, key=lambda x: x.get("cost", 0), reverse=True))]
    if any(v > 0 for _, v, _ in bar_items):
        rep.hbar("Cost total pe model (USD)", bar_items, lambda v: usd(v, 2))

    # Proporție interogări: IRIS local/gratuit (curated + Gemma + necunoscut) vs Claude plătit (Haiku/Sonnet)
    LOCAL = {"curated", "gemma", "?", "", None}
    iris_calls = sum(int(m.get("calls") or 0) for m in by_model if (m.get("model") in LOCAL))
    paid_calls = sum(int(m.get("calls") or 0) for m in by_model if (m.get("model") not in LOCAL))
    if iris_calls + paid_calls > 0:
        rep.share_bar(
            "Proporție interogări: IRIS local vs Claude plătit",
            [("IRIS local (Gemma + curated + necunoscut) — $0", iris_calls, GREEN),
             ("Claude plătit (Haiku/Sonnet)", paid_calls, RED)],
            note="Din totalul de " + grp(iris_calls + paid_calls) + " interogări — câte au fost procesate gratuit local vs contra cost.")

    if by_flow is not None:
        rep.section("Pe flux", "Toate task-urile (inclusiv cele cu hash în nume), grupate pe flux. "
                               "Sumele sunt egale cu totalul raportului.")
        frows = [[f["flow"], grp(f["calls"]), usd(f["cost"]), grp(f["errors"]), usd(f["failed_cost"]),
                  usd(f["cost_per_call"], 5)] for f in by_flow]
        ftot = ["TOTAL", grp(totals.get("calls", 0)), usd(totals.get("cost", 0)),
                grp(totals.get("errors", 0)), usd(totals.get("failed_cost", 0)),
                usd((totals.get("cost", 0) / totals["calls"]) if totals.get("calls") else 0, 5)]
        rep.table(["Flux", "Apeluri", "Cost", "Apeluri eșuate", "Cost eșuate", "Cost / apel"],
                  [135, 70, 85, 80, 85, 80], frows, ["l", "r", "r", "r", "r", "r"],
                  total_row=ftot, money_cols=(2, 4, 5))

    rep.section("Per tip de task", "Interogări și cost cumulat pe toate modelele, plus modelul dominant (cele mai multe apeluri).")
    trows = []
    for t in by_task:
        share = t.get("top_share")
        dom = model_label(t.get("top_model")) + ((" " + str(int(round(share))) + "%") if share is not None else "")
        trows.append([t.get("task") or "—", grp(t.get("calls")), usd(t.get("cost")), dom, str(t.get("errors") or 0),
                      usd(t.get("failed_cost") or 0), _pct(t.get("failed_cost") or 0, t.get("cost") or 0)])
    ttot = ["TOTAL", grp(totals.get("calls", 0)), usd(totals.get("cost", 0)), "",
            grp(totals.get("errors", 0)), usd(totals.get("failed_cost", 0)),
            _pct(totals.get("failed_cost", 0), totals.get("cost", 0))]
    rep.table(["Task", "Interogări", "Cost", "Model dominant", "Apeluri eșuate", "Cost eșuate", "% cost eșuate"],
              [150, 55, 70, 90, 55, 65, 50], trows, ["l", "r", "r", "l", "r", "r", "r"],
              total_row=ttot, money_cols=(2, 5))

    top_tasks = sorted(by_task, key=lambda x: x.get("calls", 0), reverse=True)[:12]
    rep.hbar("Interogări pe task (top 12)",
             [(t.get("task") or "—", float(t.get("calls") or 0), BAR_PALETTE[i % len(BAR_PALETTE)]) for i, t in enumerate(top_tasks)],
             lambda v: grp(v))
    cost_tasks = sorted([t for t in by_task if (t.get("cost") or 0) > 0], key=lambda x: x.get("cost", 0), reverse=True)[:12]
    if cost_tasks:
        rep.hbar("Cost pe task (top, USD)",
                 [(t.get("task") or "—", float(t.get("cost") or 0), BAR_PALETTE[i % len(BAR_PALETTE)]) for i, t in enumerate(cost_tasks)],
                 lambda v: usd(v, 2))

    rep.section("Detaliu task × model", "Ce model a procesat fiecare tip de task și cu ce cost.")
    order = {t.get("task"): i for i, t in enumerate(by_task)}
    btm = sorted(by_task_model, key=lambda x: (order.get(x.get("task"), 999), -(x.get("calls") or 0)))
    drows = []
    last = None
    for r in btm:
        tk = r.get("task") or "—"
        drows.append(["" if tk == last else tk, model_label(r.get("model")), grp(r.get("calls")), usd(r.get("cost"))])
        last = tk
    rep.table(["Task", "Model", "Interogări", "Cost"], [205, 150, 75, 115], drows, ["l", "l", "r", "r"], money_cols=(3,))

    if cache_savings is not None:
        rep.section("Economie din cache", "Apeluri evitate de cache-ul de rezultat AI (ai_cache_hit_log), pe prefix.")
        if cache_savings:
            crow = [[c["task_prefix"], grp(c["hits"]), usd(c["saved_cost"])] for c in cache_savings]
            ctot = ["TOTAL", grp(sum(c["hits"] for c in cache_savings)),
                    usd(sum(c["saved_cost"] for c in cache_savings))]
            rep.table(["Prefix", "Hit-uri", "Cost evitat"], [255, 120, 160], crow, ["l", "r", "r"],
                      total_row=ctot, money_cols=(2,))
        else:
            rep.table(["Prefix", "Hit-uri", "Cost evitat"], [255, 120, 160],
                      [["— fără date (cache inactiv sau fără hit-uri în fereastră)", "", ""]], ["l", "r", "r"])

    return rep.bytes()
