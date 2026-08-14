"""Modul Productivitate (mailuri) — API.

GET  /api/v1/productivity/objectives              -> config + obiective per departament (Tab 2)
PUT  /api/v1/productivity/objectives/{department}  -> upsert config + set obiective
GET  /api/v1/productivity/report?month=YYYY-MM     -> raport lunar (toate dept configurate sau unul)
GET  /api/v1/productivity/trend?months=6           -> serie lunara obiectiv_atins vs real
GET  /api/v1/productivity/department-users         -> lista operatori activi dintr-un departament
"""
import datetime as _dt
import logging
from typing import Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.api.v1.auth import get_current_admin
from app.services import productivity as P
from app.services import access_control as _ac

router = APIRouter()

# v10.19.0 — operatorul are acces DOAR la sub-tab-ul "Rapoarte". Restul
# sub-tab-urilor (Analiza, Obiective, Notificari, Monitor op/fin) cer admin
# sau developer. Ascunderea in UI e cosmetica; asta e gate-ul real.
require_prod_full = _ac.require_role(_ac.ROLE_ADMIN, _ac.ROLE_DEVELOPER)

# Startul perioadei si excluderile pentru mailuri se definesc O SINGURA DATA in serviciu, ca
# pagina, modalul si monitorul sa nu poata ajunge sa raspunda diferit la aceeasi intrebare.
# Vezi app/services/productivity.py (_EMAIL_START_SQL / _EMAIL_EXCLUDE_SQL) pentru motivatie:
# `extra.created_at` e momentul crearii tichetului, nu al sosirii mailului.
_EMAIL_START_SQL = P._EMAIL_START_SQL
_EMAIL_EXCLUDE_SQL = P._EMAIL_EXCLUDE_SQL
# JOIN necesar pentru _EMAIL_EXCLUDE_SQL cand query-ul nu are deja `emails e` / `clients pex`.
_J_EMAIL_EXCL = """
        LEFT JOIN emails e ON e.id = g.email_id
        LEFT JOIN clients pex ON pex.id = e.client_id
"""
# Acelasi moment de sosire, dar convertit in fusul LOCAL — pentru histogramele pe ora si
# comparatiile cu CURRENT_DATE, unde `timestamptz` brut ar cadea pe ziua/ora greasita.
# Fusul e literal aici (nu `_TZ`, definit mai jos in fisier) ca sa nu depinda de ordinea liniilor.
_EMAIL_ARRIVED_LOCAL = (
    f"({_EMAIL_START_SQL.format(g='g')} AT TIME ZONE 'Europe/Bucharest')"
)


def _default_ym(db: Session) -> tuple:
    r = db.execute(text("SELECT extract(year from CURRENT_DATE)::int, extract(month from CURRENT_DATE)::int")).fetchone()
    return int(r[0]), int(r[1])


def _parse_month(db: Session, month: Optional[str]) -> tuple:
    if not month:
        return _default_ym(db)
    try:
        y, m = month.split("-")
        y, m = int(y), int(m)
        if not (1 <= m <= 12) or y < 2000 or y > 2100:
            raise ValueError()
        return y, m
    except Exception:
        raise HTTPException(status_code=400, detail="Parametrul 'month' invalid (format YYYY-MM).")


def _configured_departments(db: Session) -> list:
    rows = db.execute(text("SELECT department FROM productivity_department_config ORDER BY department")).fetchall()
    return [r[0] for r in rows]


def _admin_email(admin) -> Optional[str]:
    if isinstance(admin, dict):
        return admin.get("email")
    return getattr(admin, "email", None)


@router.get("/productivity/objectives")
def get_objectives(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Config + obiective pentru toate departamentele (pt Tab 2 - Obiective & Ponderi)."""
    return {"departments": P.list_configs(db)}


@router.put("/productivity/objectives/{department}")
def put_objectives(department: str, body: dict,
                   db: Session = Depends(get_db), admin=Depends(require_prod_full)):
    """Upsert baza_procent + inlocuieste setul de obiective (toate tipurile) al departamentului.

    Fiecare obiectiv tine de un `tip` (email/task/apel) si optional o `categorie` (NULL = general,
    ex. 'cargobox' pt task-uri) - un singur obiectiv per pereche (tip, categorie), cu limita,
    unitate ('minute'/'secunde') si pondere proprii.
    """
    department = (department or "").strip()
    if not department:
        raise HTTPException(status_code=400, detail="Departament lipsa.")
    try:
        baza = float(body.get("baza_procent", 95))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="baza_procent invalid.")
    if not (0 < baza <= 100):
        raise HTTPException(status_code=400, detail="baza_procent trebuie in intervalul (0, 100].")

    obiective = body.get("obiective") or []
    if not isinstance(obiective, list):
        raise HTTPException(status_code=400, detail="obiective trebuie sa fie o lista.")

    norm = []
    keys = []
    for o in obiective:
        if not isinstance(o, dict):
            raise HTTPException(status_code=400, detail="Obiectiv invalid.")
        tip = (o.get("tip") or "email")
        if not isinstance(tip, str) or not tip.strip():
            raise HTTPException(status_code=400, detail="tip invalid.")
        tip = tip.strip().lower()
        categorie = o.get("categorie")
        categorie = categorie.strip().lower() if isinstance(categorie, str) and categorie.strip() else None
        unitate = (o.get("unitate") or "minute")
        unitate = unitate.strip().lower() if isinstance(unitate, str) else "minute"
        if unitate not in ("minute", "secunde"):
            raise HTTPException(status_code=400, detail="unitate trebuie sa fie 'minute' sau 'secunde'.")
        try:
            limita = int(o["limita_minute"])
            pondere = float(o["pondere"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail="limita_minute / pondere invalide.")
        if limita <= 0:
            raise HTTPException(status_code=400, detail="limita_minute trebuie > 0.")
        if pondere < 0:
            raise HTTPException(status_code=400, detail="pondere trebuie >= 0.")
        keys.append((tip, categorie))
        norm.append({"tip": tip, "categorie": categorie, "limita_minute": limita, "pondere": pondere, "unitate": unitate})

    if len(keys) != len(set(keys)):
        raise HTTPException(status_code=400, detail="Combinatie tip+categorie duplicata intre obiective.")

    return P.upsert_department(db, department, baza, norm, updated_by=_admin_email(admin))


@router.get("/productivity/report")
def get_report(month: Optional[str] = Query(None),
               months: int = Query(1, ge=1, le=12),
               department: Optional[str] = Query(None),
               db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Raport lunar sau multi-luna (months=1..12). Fara `department` -> toate departamentele configurate."""
    y, m = _parse_month(db, month)
    depts = [department] if department else _configured_departments(db)

    if months == 1:
        reports = [P.department_report(db, d, y, m) for d in depts]
        return {"month": f"{y:04d}-{m:02d}", "months": 1, "departments": reports}

    # Multi-luna: calculeaza N luni si agrheaza
    seq = []
    yy, mm = y, m
    for _ in range(months):
        seq.append((yy, mm))
        mm -= 1
        if mm == 0:
            mm = 12
            yy -= 1
    seq.reverse()  # cronologic: cel mai vechi primul

    dept_monthly: dict = {d: [] for d in depts}
    for (ry, rm) in seq:
        for d in depts:
            dept_monthly[d].append(P.department_report(db, d, ry, rm))

    aggregated = [P.aggregate_reports(dept_monthly[d]) for d in depts]
    label_from = f"{seq[0][0]:04d}-{seq[0][1]:02d}"
    label_to = f"{seq[-1][0]:04d}-{seq[-1][1]:02d}"
    return {"month": f"{label_from} – {label_to}", "months": months, "departments": aggregated}


@router.get("/productivity/forecast")
def get_forecast(month: Optional[str] = Query(None),
                 months: int = Query(1, ge=1, le=12),
                 department: Optional[str] = Query(None),
                 db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Estimare productivitate luna viitoare (sau orice luna fara date complete).
    Aceeasi structura ca /productivity/report + is_forecast=True."""
    y, m = _parse_month(db, month)
    depts = [department] if department else _configured_departments(db)

    if months == 1:
        reports = [P.forecast_report(db, d, y, m) for d in depts]
        return {"month": f"{y:04d}-{m:02d}", "months": 1, "is_forecast": True, "departments": reports}

    seq = []
    yy, mm = y, m
    for _ in range(months):
        seq.append((yy, mm))
        mm -= 1
        if mm == 0:
            mm = 12
            yy -= 1
    seq.reverse()

    dept_monthly: dict = {d: [] for d in depts}
    for (ry, rm) in seq:
        for d in depts:
            dept_monthly[d].append(P.forecast_report(db, d, ry, rm))

    aggregated = [P.aggregate_reports(dept_monthly[d]) for d in depts]
    for a in aggregated:
        a["is_forecast"] = True
    label_from = f"{seq[0][0]:04d}-{seq[0][1]:02d}"
    label_to = f"{seq[-1][0]:04d}-{seq[-1][1]:02d}"
    return {"month": f"{label_from} – {label_to}", "months": months, "is_forecast": True, "departments": aggregated}


@router.post("/productivity/recalculate")
def recalculate_estimate(month: Optional[str] = Query(None),
                         department: Optional[str] = Query(None),
                         db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Recalculeaza estimarea unei luni: sterge snapshot-ul fixat si il regenereaza.

    Snapshot-ul lunar (zile lucratoare, ore planificate/disponibile, coeficient, obiective) e
    imutabil prin design, ca un target emis sa nu se schimbe retroactiv. Cand se modifica INTRARILE
    -- concedii noi/anulate, zile de lucru pe proiect/refurbished, data de start a productivitatii
    unui angajat, program de lucru -- estimarea trebuie refacuta explicit.

    Permis doar pe luna curenta si pe cele VIITOARE. Lunile incheiate nu se rescriu: acolo cifrele
    sunt deja raportate si o recalculare ar schimba istoria.
    """
    y, m = _parse_month(db, month)
    cy, cm = _default_ym(db)
    if (y, m) < (cy, cm):
        raise HTTPException(
            status_code=400,
            detail=f"Nu se poate recalcula o lună încheiată ({y:04d}-{m:02d}). "
                   f"Sunt permise doar luna curentă ({cy:04d}-{cm:02d}) și cele viitoare.",
        )

    depts = [department] if department else _configured_departments(db)
    if not depts:
        raise HTTPException(status_code=400, detail="Niciun departament configurat.")

    reset, regenerated = [], []
    for d in depts:
        if P.reset_snapshot(db, d, y, m):
            reset.append(d)
        # Regenerare imediata: forecast_report reconstruieste si re-fixeaza snapshot-ul.
        try:
            P.forecast_report(db, d, y, m)
            regenerated.append(d)
        except Exception as e:
            logger.warning("productivity recalculate failed for %s %04d-%02d: %s", d, y, m, e)

    logger.info("productivity recalculate: month=%04d-%02d reset=%s by=%s",
                y, m, reset, _admin_email(admin))
    return {
        "ok": True,
        "month": f"{y:04d}-{m:02d}",
        "snapshots_reset": reset,
        "regenerated": regenerated,
        "departments_processed": len(depts),
    }


@router.get("/productivity/trend")
def get_trend(months: int = Query(6, ge=1, le=24),
              month: Optional[str] = Query(None),
              department: Optional[str] = Query(None),
              db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Serie lunara (ultimele `months` luni). Fara `department` -> toate departamentele configurate."""
    y, m = _parse_month(db, month)
    depts = [department] if department else _configured_departments(db)
    out = [{"department": d, "series": P.trend(db, d, months, y, m)} for d in depts]
    return {"months": months, "departments": out}


def _parse_date(s: Optional[str]) -> Optional[_dt.date]:
    if not s:
        return None
    try:
        return _dt.date.fromisoformat(s.strip())
    except Exception:
        raise HTTPException(status_code=400, detail="Data invalida (format YYYY-MM-DD).")


@router.get("/productivity/analytics")
def get_analytics(from_: Optional[str] = Query(None, alias="from"),
                  to: Optional[str] = Query(None),
                  department: Optional[str] = Query(None),
                  user_id: Optional[int] = Query(None),
                  db: Session = Depends(get_db), admin=Depends(require_prod_full)):
    """Analytics pe interval [from,to] (YYYY-MM-DD). department gol/'operational' -> toate configurate.
    user_id optional -> filtrare la angajatul respectiv din employee_department_mapping."""
    df = _parse_date(from_)
    dt = _parse_date(to)
    if dt is None:
        r = db.execute(text("SELECT (CURRENT_TIMESTAMP AT TIME ZONE 'Europe/Bucharest')::date")).fetchone()
        dt = r[0]
    if df is None:
        df = dt - _dt.timedelta(days=29)
    if df > dt:
        df, dt = dt, df
    if (dt - df).days > 731:
        raise HTTPException(status_code=400, detail="Interval prea mare (max ~2 ani).")
    dep = (department or "").strip().lower()
    if dep in ("", "operational", "general", "all", "toate", "__all__"):
        depts = _configured_departments(db)
    elif dep == "financiar":
        depts = ["contabilitate", "recuperare_tva"]
    else:
        depts = [dep]
    return P.analytics_report(db, depts, df, dt, user_id=user_id)


@router.get("/productivity/breakdown")
def get_breakdown(tip: str = Query(..., description="'email' | 'task' | 'apel' | 'device_ops' | 'reclamatie'"),
                  department: str = Query(...),
                  month: Optional[str] = Query(None, description="YYYY-MM; implicit luna curenta"),
                  categorie: Optional[str] = Query(None, description="categoria obiectivului; '' = general"),
                  status: Optional[str] = Query(None, description="'on_time' | 'overdue' | '' = toate"),
                  user_id: Optional[int] = Query(None, description="filtreaza pe un angajat"),
                  search: Optional[str] = Query(None, description="caută în client / subiect"),
                  date_from: Optional[str] = Query(None, description="data soluționării >= (YYYY-MM-DD)"),
                  date_to: Optional[str] = Query(None, description="data soluționării <= (YYYY-MM-DD)"),
                  sort_by: Optional[str] = Query(None, description="client | created_at | solved_at"),
                  sort_dir: Optional[str] = Query(None, description="asc | desc"),
                  page: int = Query(1, ge=1),
                  page_size: int = Query(50, ge=1, le=500),
                  db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Lista brută din spatele unui obiectiv de productivitate — fiecare rând care intră în calcul.

    Refoloseste EXACT aceleasi surse, filtre si conventii de timp ca `department_report`
    (`P._fetch_*_rows` + `P._BizCache.business_minutes`), ca totalurile de aici sa fie identice
    cu procentul afisat pe pagina de Productivitate. Statusul unui rand:
      - `on_time`  = durata masurabila (> 0) si <= limita obiectivului
      - `overdue`  = durata masurabila si > limita
      - `nemasurat`= durata 0/None (interval integral in afara programului, sau capat lipsa)
    """
    tip = (tip or "").strip().lower()
    if tip not in ("email", "task", "apel", "device_ops", "reclamatie"):
        raise HTTPException(status_code=400,
                            detail="tip invalid (email|task|apel|device_ops|reclamatie).")
    dept = (department or "").strip().lower()
    if not dept:
        raise HTTPException(status_code=400, detail="department obligatoriu.")

    year, mon = _parse_month(db, month)
    first = _dt.date(year, mon, 1)
    df, dtt = _parse_date(date_from), _parse_date(date_to)

    cat = (categorie or "").strip().lower() or None
    objectives = P.get_objectives(db, dept, tip=tip)
    obj = next((o for o in objectives if (o.get("categorie") or None) == cat), None)
    if obj is None and cat is None:
        obj = next((o for o in objectives if not o.get("categorie")), None)
    limita = obj.get("limita_minute") if obj else None
    unitate = (obj.get("unitate") if obj else None) or ("secunde" if tip == "apel" else "minute")

    rows = P.breakdown_rows(db, dept, tip, first, cat, limita)

    st = (status or "").strip().lower() or None
    if st in ("on_time", "overdue", "nemasurat"):
        rows = [r for r in rows if r["status"] == st]
    if user_id is not None:
        rows = [r for r in rows if r.get("op_id") == user_id]
    if df is not None:
        rows = [r for r in rows if r.get("solved_at") and r["solved_at"][:10] >= df.isoformat()]
    if dtt is not None:
        rows = [r for r in rows if r.get("solved_at") and r["solved_at"][:10] <= dtt.isoformat()]
    q = (search or "").strip().lower()
    if q:
        # `device` intra in cautare ca sa se poata gasi task-ul/operatiunea dupa numarul de
        # inmatriculare, nu doar dupa client sau subiect (cerinta 2026-08-13).
        rows = [r for r in rows
                if q in (r.get("client") or "").lower()
                or q in (r.get("subiect") or "").lower()
                or q in (r.get("device") or "").lower()]

    _sort_fields = {"client", "created_at", "solved_at"}
    sb = (sort_by or "").strip().lower()
    sd = (sort_dir or "desc").strip().lower()
    if sb in _sort_fields:
        reverse = sd != "asc"
        rows.sort(key=lambda x: (x.get(sb) or ""), reverse=reverse)

    total = len(rows)
    on_time = sum(1 for r in rows if r["status"] == "on_time")
    overdue = sum(1 for r in rows if r["status"] == "overdue")
    nemasurat = sum(1 for r in rows if r["status"] == "nemasurat")
    measurable = on_time + overdue
    durate = [r["durata"] for r in rows if r.get("durata") is not None and r["durata"] > 0]

    start = (page - 1) * page_size
    return {
        "tip": tip, "department": dept, "month": f"{year:04d}-{mon:02d}",
        "categorie": cat, "limita": limita, "unitate": unitate,
        "totals": {
            "total": total, "on_time": on_time, "overdue": overdue,
            "nemasurat": nemasurat, "measurable": measurable,
            "in_timp_pct": round(100.0 * on_time / measurable, 2) if measurable else None,
            "durata_medie": round(sum(durate) / len(durate), 1) if durate else None,
        },
        "page": page, "page_size": page_size,
        "sort_by": sb or None, "sort_dir": sd,
        "items": rows[start:start + page_size],
    }


@router.get("/productivity/department-users")
def get_department_users(department: Optional[str] = Query(None),
                         db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Lista operatorilor activi dintr-un departament, pentru selectorul din tab Analiză."""
    dep = (department or "").strip().lower()
    if not dep or dep in ("operational", "general", "all", "toate", "__all__"):
        return []
    rows = db.execute(
        text("SELECT id, name FROM employee_department_mapping WHERE department=:d AND enabled=true ORDER BY name"),
        {"d": dep},
    ).fetchall()
    return [{"id": r[0], "name": r[1]} for r in rows]


# ── Notificări productivitate ─────────────────────────────────────────────────

_VALID_GROUPS = {"operational", "financiar", "toate", "suport_1", "suport_2",
                 "suport_3", "taxe_drum", "contabilitate", "recuperare_tva"}


@router.get("/productivity/notifications")
def get_notifications(db: Session = Depends(get_db), admin=Depends(require_prod_full)):
    """Lista tuturor destinatarilor de email configurați."""
    rows = db.execute(
        text("SELECT id, email, department_group, enabled, created_at "
             "FROM productivity_notifications ORDER BY department_group, email")
    ).fetchall()
    return [{"id": r[0], "email": r[1], "department_group": r[2],
             "enabled": r[3], "created_at": str(r[4])} for r in rows]


@router.post("/productivity/notifications")
def add_notification(body: dict, db: Session = Depends(get_db), admin=Depends(require_prod_full)):
    """Adaugă destinatar email. Body: {email, department_group}."""
    import re as _re
    email = (body.get("email") or "").strip().lower()
    group = (body.get("department_group") or "").strip().lower()
    if not email or not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(status_code=400, detail="Email invalid.")
    if group not in _VALID_GROUPS:
        raise HTTPException(status_code=400,
                            detail=f"department_group invalid. Valori acceptate: {sorted(_VALID_GROUPS)}")
    try:
        row = db.execute(
            text("INSERT INTO productivity_notifications(email, department_group) "
                 "VALUES (:e, :g) ON CONFLICT (email, department_group) DO UPDATE "
                 "SET enabled=true, updated_at=now() RETURNING id, email, department_group, enabled, created_at"),
            {"e": email, "g": group}
        ).fetchone()
        db.commit()
        return {"id": row[0], "email": row[1], "department_group": row[2],
                "enabled": row[3], "created_at": str(row[4])}
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete("/productivity/notifications/{notif_id}")
def delete_notification(notif_id: int, db: Session = Depends(get_db), admin=Depends(require_prod_full)):
    """Șterge destinatar după id."""
    result = db.execute(
        text("DELETE FROM productivity_notifications WHERE id=:id RETURNING id"),
        {"id": notif_id}
    ).fetchone()
    db.commit()
    if not result:
        raise HTTPException(status_code=404, detail="Notificarea nu a fost găsită.")
    return {"ok": True, "id": notif_id}


@router.post("/productivity/notifications/send-now")
def send_notifications_now(db: Session = Depends(get_db), admin=Depends(require_prod_full)):
    """Declanșează trimiterea manuală a rapoartelor (ignoră gating zi/oră)."""
    from app.services import productivity_notifier as _pn
    try:
        result = _pn.send_monthly_reports(db)
        return {"ok": True, **result}
    except Exception as exc:
        logger.exception("send_notifications_now failed")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/productivity/notifications/send-test")
def send_test_notification(body: dict, db: Session = Depends(get_db), admin=Depends(require_prod_full)):
    """Trimite un email de test la o adresă specificată, pentru o lună și un grup anume.

    Body: { email, department_group, month }  — month format YYYY-MM (luna pentru care se generează
    summary-ul; forecast = luna următoare față de cea specificată).
    """
    import re as _re
    from app.services import productivity_notifier as _pn

    email = (body.get("email") or "").strip().lower()
    group = (body.get("department_group") or "operational").strip().lower()
    month_str = (body.get("month") or "").strip()

    if not email or not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(status_code=400, detail="Email invalid.")
    if group not in _VALID_GROUPS:
        raise HTTPException(status_code=400,
                            detail=f"department_group invalid. Valori acceptate: {sorted(_VALID_GROUPS)}")
    if month_str:
        try:
            parts = month_str.split("-")
            prev_year, prev_month = int(parts[0]), int(parts[1])
            if not (1 <= prev_month <= 12) or prev_year < 2000:
                raise ValueError()
        except Exception:
            raise HTTPException(status_code=400, detail="month invalid (format YYYY-MM).")
    else:
        import datetime as _dt2
        today = _dt2.date.today()
        prev_year, prev_month = _pn._prev_month(today.year, today.month)

    # Luna curentă (pentru forecast) = luna imediat după luna de test
    curr_month = prev_month + 1 if prev_month < 12 else 1
    curr_year = prev_year if prev_month < 12 else prev_year + 1

    try:
        depts = _pn._expand_departments(db, group)
        if not depts:
            raise HTTPException(status_code=400, detail=f"Grupul '{group}' nu are departamente configurate.")

        from app.services.productivity import department_report, forecast_report

        summary_reports = []
        for dept in depts:
            try:
                summary_reports.append(department_report(db, dept, prev_year, prev_month))
            except Exception as e:
                logger.warning("test: department_report %s %d-%02d: %s", dept, prev_year, prev_month, e)

        forecast_reports = []
        for dept in depts:
            try:
                forecast_reports.append(forecast_report(db, dept, curr_year, curr_month))
            except Exception as e:
                logger.warning("test: forecast_report %s %d-%02d: %s", dept, curr_year, curr_month, e)

        group_lbl = _pn._group_label(group)
        prev_lbl = f"{_pn._luna_label(prev_month).title()} {prev_year}"
        curr_lbl = f"{_pn._luna_label(curr_month).title()} {curr_year}"

        intro = _pn._generate_ai_summary(group, group_lbl, prev_year, prev_month,
                                         curr_year, curr_month, summary_reports, forecast_reports)
        att_data, att_mime, att_name = _pn._generate_pdf(
            db, group_lbl, prev_year, prev_month, depts, summary_reports, forecast_reports
        )
        html_body = _pn._build_email_html(group_lbl, prev_lbl, curr_lbl,
                                          intro, summary_reports, forecast_reports)
        subject = f"[TEST] Rezumat productivitate {prev_lbl} — {group_lbl}"

        ok = _pn._send_email(db, email, subject, html_body, att_data, att_mime, att_name)
        if not ok:
            raise HTTPException(status_code=500, detail="Trimiterea a eșuat. Verifică configurația SMTP.")
        return {"ok": True, "sent_to": email, "month": f"{prev_year}-{prev_month:02d}",
                "department_group": group, "depts": depts,
                "has_pdf": att_data is not None}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("send_test_notification failed")
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Dashboard monitor (public — fără auth, pentru monitoare birou intern)
# ---------------------------------------------------------------------------

_FINANCIAR_DEPTS = {"recuperare_tva", "contabilitate"}


def _dashboard_depts(db: Session, group: str) -> list:
    all_depts = _configured_departments(db)
    if group == "financiar":
        return [d for d in all_depts if d in _FINANCIAR_DEPTS]
    return [d for d in all_depts if d not in _FINANCIAR_DEPTS]


@router.get("/productivity/dashboard/data")
def get_dashboard_data(group: str = Query("operational"), db: Session = Depends(get_db)):
    """Date dashboard monitor productivitate — PUBLIC (fără auth), pentru monitoare birou intern.

    group: 'operational' (suport_1/2/3, taxe_drum) | 'financiar' (contabilitate, recuperare_tva)
    """
    import datetime as _dt2
    today = _dt2.date.today()
    year, month = today.year, today.month
    first_of_month = _dt2.date(year, month, 1)

    if group not in ("operational", "financiar"):
        raise HTTPException(status_code=400, detail="group trebuie să fie 'operational' sau 'financiar'.")

    depts = _dashboard_depts(db, group)

    # Ținte + capacitate pentru luna curentă din `forecast_report` (obiectiv_real/minim, ore,
    # coeficient — acolo sunt calculate pe luna întreagă), DAR procentul efectiv atins și statusul
    # se iau din `department_report`, adică din datele REALE ale lunii.
    #
    # `forecast_report.obiectiv_atins` e o ESTIMARE (proiectează media ultimelor 2 luni complete),
    # nu realizarea de până acum, deci ieșea sistematic sub cifra din pagina Rapoarte — pe
    # suport_1/august 2026: 88.12% pe monitor vs 92.67% în Rapoarte. Monitorul de perete trebuie să
    # arate exact ce arată Rapoartele; altfel două ecrane din aceeași firmă se contrazic.
    forecast = []
    for dept in depts:
        try:
            r = P.forecast_report(db, dept, year, month)
            atins, status = r.get("obiectiv_atins"), r.get("status")
            try:
                real_rep = P.department_report(db, dept, year, month)
                # Doar dacă luna are date măsurabile; altfel rămâne estimarea (ex. 1 a lunii).
                if real_rep.get("obiectiv_atins") is not None:
                    atins = real_rep.get("obiectiv_atins")
                    status = real_rep.get("status")
            except Exception:
                logger.exception("dashboard department_report %s %d-%02d", dept, year, month)
            forecast.append({
                "department": dept,
                "obiectiv_real": r.get("obiectiv_real"),
                "obiectiv_minim": r.get("obiectiv_minim"),
                "obiectiv_atins": atins,
                "zile_lucratoare": r.get("zile_lucratoare"),
                "ore_planificate": r.get("ore_planificate"),
                "ore_disponibile": r.get("ore_disponibile"),
                "coeficient": r.get("coeficient"),
                "status": status,
            })
        except Exception:
            logger.exception("dashboard forecast_report %s %d-%02d", dept, year, month)

    # Analytics de la prima zi a lunii până azi — volum și % zilnic per dept
    per_day_map: dict = {}  # day_str -> {dept -> {pct, volum_email, volum_apel}}
    analytics_today: list = []

    for dept in depts:
        try:
            ana = P.analytics_report(db, [dept], first_of_month, today)
            analytics_today.append({
                "department": dept,
                "volum": ana.get("volum", 0),
                "in_timp_pct": ana.get("in_timp_pct"),
                "apeluri_volum": (ana.get("apeluri") or {}).get("volum", 0),
            })
            # email daily — câmpul e "in_timp" (nu "in_timp_pct")
            for entry in (ana.get("daily") or []):
                day_str = str(entry.get("day", ""))[:10]
                if not day_str:
                    continue
                if day_str not in per_day_map:
                    per_day_map[day_str] = {}
                if dept not in per_day_map[day_str]:
                    per_day_map[day_str][dept] = {"pct": None, "volum_email": 0, "volum_apel": 0}
                per_day_map[day_str][dept]["pct"] = entry.get("in_timp")
                per_day_map[day_str][dept]["volum_email"] = entry.get("volum", 0)
            # apeluri daily
            for entry in ((ana.get("apeluri") or {}).get("daily") or []):
                day_str = str(entry.get("day", ""))[:10]
                if not day_str:
                    continue
                if day_str not in per_day_map:
                    per_day_map[day_str] = {}
                if dept not in per_day_map[day_str]:
                    per_day_map[day_str][dept] = {"pct": None, "volum_email": 0, "volum_apel": 0}
                per_day_map[day_str][dept]["volum_apel"] = entry.get("volum", 0)
        except Exception:
            logger.exception("dashboard analytics_report %s", dept)

    per_day = [
        {"day": day, "depts": depts_data}
        for day, depts_data in sorted(per_day_map.items())
    ]

    return {
        "group": group,
        "month": f"{year:04d}-{month:02d}",
        "generated_at": today.isoformat(),
        "departments": depts,
        "forecast": forecast,
        "analytics_today": analytics_today,
        "per_day": per_day,
    }


# Fusul orar de business. `cts_solved_at` & co. sunt timestamptz stocate în UTC;
# fără conversie, bucketing-ul pe oră/zi apare decalat (vârful de la 10:00 local
# ar cădea pe ora 07 UTC), iar "azi" s-ar rupe la miezul nopții UTC, nu local.
_TZ = "Europe/Bucharest"


@router.get("/productivity/monitor/live")
def get_monitor_live(group: str = Query("operational"), db: Session = Depends(get_db)):
    """Date live pentru monitorul heartbeat — PUBLIC (monitoare de birou intern).

    Cheile `emailuri` / `taskuri` / `apeluri` sunt păstrate pentru compatibilitate;
    `sesizari`, `device_ops`, `hourly` și `per_dept` sunt adăugate aditiv.
    """
    import datetime as _dt2

    depts = _dashboard_depts(db, group)

    # Filtrare pe grup: monitorul „Operațional" trebuie să arate DOAR suport_1/2/3 +
    # taxe_drum, iar „Financiar" doar contabilitate + recuperare_tva. Fără join-ul de
    # mai jos cifrele erau globale pe toată firma (ex. 717 emailuri „operațional",
    # din care doar 164 chiar erau ale grupului).
    # Atribuirea se face prin asignat -> employee_department_mapping, exact ca în
    # productivity._fetch_email_rows.
    _p = {"depts": depts}
    # DEPARTAMENTUL UNUI MAIL = cel al TICHETULUI din CTS (`cts_department`), nu al persoanei
    # asignate. JOIN-ul vechi (INNER pe `cts_assignee_email` = edm.email) arunca orice mail
    # NEASIGNAT: 69 din 171 de mailuri 'new' (40%) nu au assignee — in CTS stau in coada
    # departamentului, la noi dispareau complet. Suport 1 arata 0 'new' in loc de 47 (constatat
    # 2026-08-06). In plus, mailuri asignate cuiva din alt departament decat cel al tichetului
    # se numarau la departamentul greșit (contabilitate arata 20 in loc de 8).
    # Fallback pe departamentul assignee-ului doar cand `cts_department` lipseste (4066 rânduri,
    # toate 'solved' istorice — nici un mail deschis nu e afectat).
    _dep_email = """
        LEFT JOIN employee_department_mapping edm
          ON lower(g.cts_assignee_email) = lower(edm.email)
    """
    # Expresia de departament efectiv, refolosita in GROUP BY / WHERE.
    _EFF_DEPT_EMAIL = "COALESCE(g.cts_department, edm.department)"
    _dep_email_w = f"AND {_EFF_DEPT_EMAIL} = ANY(:depts)"
    # task-uri și device ops au cheie străină numerică spre edm.id (NU iris_id, care
    # e text și nu se potrivește)
    # Idem pentru task-uri: `cts_task_ground_truth.department` e departamentul tichetului din CTS,
    # prezent si pe task-urile neasignate (10 'new' pe mobilitate, 2026-08-06).
    _dep_task = """
        LEFT JOIN employee_department_mapping edm
          ON t.assignee_employee_id = edm.id
    """
    _EFF_DEPT_TASK = "COALESCE(t.department, edm.department)"
    _dep_dev = """
        JOIN employee_department_mapping edm
          ON d.closed_by_employee_id = edm.id AND edm.department = ANY(:depts)
    """
    _dep_call = """
        JOIN employee_department_mapping edm
          ON lower(c.cts_assignee_email) = lower(edm.email) AND edm.department = ANY(:depts)
    """

    # emailuri (cts_ground_truth) — "azi" în fus local
    # FEREASTRA: toate contoarele de stare deschisă („în lucru", „noi") se raportează la ce a
    # SOSIT AZI, nu la tot ce n-a fost vreodată marcat solved în CTS (decizie business owner,
    # 2026-08-13). Motiv: CTS lasă deschise pe termen nelimitat mailuri care nu se mai închid
    # niciodată (notificări automate, tichete abandonate) — pe Suport 1, 26 de mailuri „noi",
    # din care doar 3 sosite în ultimele 7 zile. Monitorul de perete arăta astfel o restanță
    # istorică pe care nimeni n-o mai lucrează, nu starea zilei.
    email_row = db.execute(text(f"""
        SELECT
            COUNT(*) FILTER (WHERE g.cts_status IN ('solved','closed')
                             AND DATE(g.cts_solved_at AT TIME ZONE '{_TZ}') = CURRENT_DATE) AS rezolvate_azi,
            COUNT(*) FILTER (WHERE g.cts_status = 'in progress'
                             AND DATE({_EMAIL_ARRIVED_LOCAL}) = CURRENT_DATE)                AS in_lucru,
            COUNT(*) FILTER (WHERE g.cts_status = 'new'
                             AND DATE({_EMAIL_ARRIVED_LOCAL}) = CURRENT_DATE)                AS noi
        FROM cts_ground_truth g
        {_dep_email}
        {_J_EMAIL_EXCL}
        WHERE g.cts_deleted_at IS NULL
          AND COALESCE(g.cts_direction,'received') = 'received'
          AND {_EMAIL_EXCLUDE_SQL}
          {_dep_email_w}
    """), _p).fetchone()

    # taskuri (cts_task_ground_truth)
    # NB: statusul în DB e literal 'in progress' (cu spațiu), nu 'in_progress'.
    # Aceeași fereastră ca la mailuri: stările deschise se raportează la task-urile CREATE AZI.
    # Varianta anterioară (fără limită de vechime) aduna restanța istorică — la Financiar 769
    # 'new' + 351 'postponed', unele din martie — un număr mare care nu spune nimic despre ziua
    # curentă. `pending_vechi` a fost eliminat: în interiorul unei singure zile e mereu 0.
    task_row = db.execute(text(f"""
        SELECT
            COUNT(*) FILTER (WHERE t.status IN ('solved','closed')
                             AND DATE(t.cts_updated_at AT TIME ZONE '{_TZ}') = CURRENT_DATE) AS rezolvate_azi,
            COUNT(*) FILTER (WHERE t.status = 'in progress'
                             AND DATE(t.cts_created_at AT TIME ZONE '{_TZ}') = CURRENT_DATE)  AS in_progress,
            COUNT(*) FILTER (WHERE t.status IN ('new','postponed')
                             AND DATE(t.cts_created_at AT TIME ZONE '{_TZ}') = CURRENT_DATE)  AS pending
        FROM cts_task_ground_truth t
        {_dep_task}
        WHERE {_EFF_DEPT_TASK} = ANY(:depts)
    """), _p).fetchone()

    # apeluri azi + cele încă neînchise (status 'new' / 'in progress')
    call_row = db.execute(text(f"""
        SELECT
            COUNT(*) FILTER (WHERE DATE(c.cts_started_at AT TIME ZONE '{_TZ}') = CURRENT_DATE) AS azi,
            COUNT(*) FILTER (WHERE DATE(c.cts_started_at AT TIME ZONE '{_TZ}') = CURRENT_DATE
                             AND lower(c.cts_status) IN ('solved','closed'))                    AS rezolvate_azi,
            -- doar apelurile de AZI încă neînchise; fără filtrul pe zi ar aduna
            -- restanțe istorice (358 apeluri vechi niciodată închise), nu apeluri în curs
            COUNT(*) FILTER (WHERE DATE(c.cts_started_at AT TIME ZONE '{_TZ}') = CURRENT_DATE
                             AND lower(c.cts_status) IN ('new','in progress'))                   AS in_curs
        FROM cts_calls_ground_truth c
        {_dep_call}
    """), _p).fetchone()

    # sesizări / reclamații deschise — nu există tabelă dedicată: sunt valori de
    # categorie. Același COALESCE ca în productivity._fetch_email_rows (ground
    # truth primează, ai_category e fallback pentru ce n-a fost încă clasificat în CTS).
    sesiz_row = db.execute(text(f"""
        SELECT
            COUNT(*) FILTER (WHERE cat IN ('sesizare','reclamatie') AND NOT rezolvat)              AS deschise,
            COUNT(*) FILTER (WHERE cat = 'sesizare'   AND NOT rezolvat)                            AS sesizari_deschise,
            COUNT(*) FILTER (WHERE cat = 'reclamatie' AND NOT rezolvat)                            AS reclamatii_deschise,
            COUNT(*) FILTER (WHERE cat IN ('sesizare','reclamatie') AND rezolvat AND solved_azi)   AS rezolvate_azi,
            COUNT(*) FILTER (WHERE cat IN ('sesizare','reclamatie') AND NOT rezolvat
                             AND DATE(changed_loc) < CURRENT_DATE)                                 AS restante,
            COUNT(*) FILTER (WHERE cat IN ('sesizare','reclamatie') AND NOT rezolvat
                             AND changed_loc < NOW() - INTERVAL '7 days')                          AS peste_7z
        FROM (
            SELECT lower(coalesce(g.cts_category, e.ai_category))              AS cat,
                   (g.cts_status IN ('solved','closed'))                       AS rezolvat,
                   (DATE(g.cts_solved_at AT TIME ZONE '{_TZ}') = CURRENT_DATE) AS solved_azi,
                   -- vechimea sesizării: momentul real de SOSIRE. `extra.created_at` (folosit
                   -- până la v0.73.0) e momentul creării tichetului, deci se deplasa înainte cu
                   -- cât sesizarea era neglijată — exact invers de ce trebuie la restanțe.
                   -- `changed_at` rămâne fallback: e NULL pe majoritatea rândurilor deschise.
                   COALESCE({_EMAIL_START_SQL.format(g='g')},
                            g.changed_at)                                      AS changed_loc
            FROM cts_ground_truth g
            {_dep_email}
            LEFT JOIN emails e ON e.id = g.email_id
            LEFT JOIN clients pex ON pex.id = e.client_id
            WHERE g.cts_deleted_at IS NULL
              AND {_EMAIL_EXCLUDE_SQL}
              {_dep_email_w}
        ) s
    """), _p).fetchone()

    # sesizări/reclamații venite pe telefon — aceleași categorii pe apeluri
    sesiz_call = db.execute(text(f"""
        SELECT
            COUNT(*) FILTER (WHERE lower(cts_category) = 'sesizare')   AS sesizari_azi,
            COUNT(*) FILTER (WHERE lower(cts_category) = 'reclamatie') AS reclamatii_azi
        FROM cts_calls_ground_truth c
        {_dep_call}
        WHERE DATE(c.cts_started_at AT TIME ZONE '{_TZ}') = CURRENT_DATE
    """), _p).fetchone()

    # ce s-a rezolvat azi, pe categorie de email (informație vs sesizare vs reclamație)
    cat_rows = db.execute(text(f"""
        SELECT coalesce(nullif(lower(coalesce(g.cts_category, e.ai_category)), ''), 'neclasificat') AS cat,
               COUNT(*) AS n
        FROM cts_ground_truth g
        {_dep_email}
        LEFT JOIN emails e ON e.id = g.email_id
        WHERE g.cts_deleted_at IS NULL AND g.cts_status IN ('solved','closed')
          AND DATE(g.cts_solved_at AT TIME ZONE '{_TZ}') = CURRENT_DATE
          {_dep_email_w}
        GROUP BY 1
    """), _p).fetchall()
    rezolvate_categorii = {str(r[0]): int(r[1] or 0) for r in cat_rows}

    # device ops (Suport 2) — al 4-lea canal
    dev_row = db.execute(text(f"""
        SELECT
            COUNT(*) FILTER (WHERE DATE(d.closed_at AT TIME ZONE '{_TZ}') = CURRENT_DATE) AS rezolvate_azi,
            COUNT(*) FILTER (WHERE d.closed_at IS NULL AND d.finished_at IS NOT NULL)       AS in_asteptare
        FROM device_operations d
        {_dep_dev}
    """), _p).fetchone()

    # rezolvate pe oră azi (email + task + apel) — alimentează sparkline-urile
    def _hourly(sql: str) -> dict:
        try:
            return {int(r[0]): int(r[1] or 0) for r in db.execute(text(sql), _p).fetchall()}
        except Exception:
            logger.exception("monitor_live hourly")
            return {}

    h_mail = _hourly(f"""
        SELECT EXTRACT(hour FROM g.cts_solved_at AT TIME ZONE '{_TZ}')::int AS h, COUNT(*)
        FROM cts_ground_truth g
        {_dep_email}
        WHERE g.cts_deleted_at IS NULL AND g.cts_status IN ('solved','closed')
          AND DATE(g.cts_solved_at AT TIME ZONE '{_TZ}') = CURRENT_DATE
          {_dep_email_w}
        GROUP BY 1
    """)
    h_task = _hourly(f"""
        SELECT EXTRACT(hour FROM t.cts_updated_at AT TIME ZONE '{_TZ}')::int AS h, COUNT(*)
        FROM cts_task_ground_truth t
        {_dep_task}
        WHERE t.status IN ('solved','closed')
          AND DATE(t.cts_updated_at AT TIME ZONE '{_TZ}') = CURRENT_DATE
          AND {_EFF_DEPT_TASK} = ANY(:depts)
        GROUP BY 1
    """)
    h_call = _hourly(f"""
        SELECT EXTRACT(hour FROM c.cts_started_at AT TIME ZONE '{_TZ}')::int AS h, COUNT(*)
        FROM cts_calls_ground_truth c
        {_dep_call}
        WHERE DATE(c.cts_started_at AT TIME ZONE '{_TZ}') = CURRENT_DATE
        GROUP BY 1
    """)
    h_dev = _hourly(f"""
        SELECT EXTRACT(hour FROM d.closed_at AT TIME ZONE '{_TZ}')::int AS h, COUNT(*)
        FROM device_operations d
        {_dep_dev}
        WHERE d.closed_at IS NOT NULL
          AND DATE(d.closed_at AT TIME ZONE '{_TZ}') = CURRENT_DATE
        GROUP BY 1
    """)

    # ── INTRATE pe oră (volumul care sosește), ca pereche pentru "rezolvate" ──
    # Ora SOSIRII vine din `extra.email_date` (== emails.received_at), NU din `extra.created_at`
    # (momentul creării tichetului — vezi _EMAIL_START_SQL). Textul din JSON e NAIV în UTC, deci
    # se marchează explicit ca UTC și se convertește în fusul local, altfel barele „noi" ar
    # apărea decalate cu 3 ore față de barele „rezolvate".
    h_mail_new = _hourly(f"""
        SELECT EXTRACT(hour FROM ({_EMAIL_ARRIVED_LOCAL}))::int AS h,
               COUNT(*)
        FROM cts_ground_truth g
        {_dep_email}
        {_J_EMAIL_EXCL}
        WHERE g.cts_deleted_at IS NULL
          AND {_EMAIL_ARRIVED_LOCAL} IS NOT NULL
          AND {_EMAIL_EXCLUDE_SQL}
          AND DATE({_EMAIL_ARRIVED_LOCAL}) = CURRENT_DATE
          {_dep_email_w}
        GROUP BY 1
    """)
    h_task_new = _hourly(f"""
        SELECT EXTRACT(hour FROM t.cts_created_at AT TIME ZONE '{_TZ}')::int AS h, COUNT(*)
        FROM cts_task_ground_truth t
        {_dep_task}
        WHERE DATE(t.cts_created_at AT TIME ZONE '{_TZ}') = CURRENT_DATE
          AND {_EFF_DEPT_TASK} = ANY(:depts)
        GROUP BY 1
    """)
    # Apelurile: "intrate" = când a început apelul. Pentru "rezolvate" folosim
    # momentul încheierii = start + durată; `changed_at` este NULL pe toate
    # rândurile de apel, deci nu poate servi ca timp de închidere.
    h_call_new = h_call
    h_call_done = _hourly(f"""
        SELECT EXTRACT(hour FROM ((c.cts_started_at
                 + make_interval(secs => COALESCE(c.cts_duration_seconds, 0)))
                 AT TIME ZONE '{_TZ}'))::int AS h,
               COUNT(*)
        FROM cts_calls_ground_truth c
        {_dep_call}
        WHERE lower(c.cts_status) IN ('solved','closed')
          AND DATE((c.cts_started_at
                 + make_interval(secs => COALESCE(c.cts_duration_seconds, 0)))
                 AT TIME ZONE '{_TZ}') = CURRENT_DATE
        GROUP BY 1
    """)
    # ── ÎNCĂ DESCHISE, pe ora SOSIRII ──────────────────────────────────────
    # Din ce a intrat la ora X azi, cât e încă nerezolvat acum. Atenție: se referă
    # strict la ce a SOSIT azi — restanțele din zilele trecute nu au oră de azi și
    # nu apar pe grafic (la Financiar: 94 din 108 deschise sunt din zile trecute).
    h_mail_open = _hourly(f"""
        SELECT EXTRACT(hour FROM ({_EMAIL_ARRIVED_LOCAL}))::int AS h,
               COUNT(*)
        FROM cts_ground_truth g
        {_dep_email}
        {_J_EMAIL_EXCL}
        WHERE g.cts_deleted_at IS NULL
          AND g.cts_status NOT IN ('solved','closed')
          AND {_EMAIL_ARRIVED_LOCAL} IS NOT NULL
          AND {_EMAIL_EXCLUDE_SQL}
          AND DATE({_EMAIL_ARRIVED_LOCAL}) = CURRENT_DATE
          {_dep_email_w}
        GROUP BY 1
    """)
    h_task_open = _hourly(f"""
        SELECT EXTRACT(hour FROM t.cts_created_at AT TIME ZONE '{_TZ}')::int AS h, COUNT(*)
        FROM cts_task_ground_truth t
        {_dep_task}
        WHERE t.status NOT IN ('solved','closed')
          AND DATE(t.cts_created_at AT TIME ZONE '{_TZ}') = CURRENT_DATE
          AND {_EFF_DEPT_TASK} = ANY(:depts)
        GROUP BY 1
    """)
    h_call_open = _hourly(f"""
        SELECT EXTRACT(hour FROM c.cts_started_at AT TIME ZONE '{_TZ}')::int AS h, COUNT(*)
        FROM cts_calls_ground_truth c
        {_dep_call}
        WHERE lower(c.cts_status) NOT IN ('solved','closed')
          AND DATE(c.cts_started_at AT TIME ZONE '{_TZ}') = CURRENT_DATE
        GROUP BY 1
    """)
    h_dev_open = _hourly(f"""
        SELECT EXTRACT(hour FROM d.finished_at AT TIME ZONE '{_TZ}')::int AS h, COUNT(*)
        FROM device_operations d
        {_dep_dev}
        WHERE d.closed_at IS NULL AND d.finished_at IS NOT NULL
          AND DATE(d.finished_at AT TIME ZONE '{_TZ}') = CURRENT_DATE
        GROUP BY 1
    """)

    h_dev_new = _hourly(f"""
        SELECT EXTRACT(hour FROM d.finished_at AT TIME ZONE '{_TZ}')::int AS h, COUNT(*)
        FROM device_operations d
        {_dep_dev}
        WHERE d.finished_at IS NOT NULL
          AND DATE(d.finished_at AT TIME ZONE '{_TZ}') = CURRENT_DATE
        GROUP BY 1
    """)
    hourly = [
        {
            "h": f"{hh:02d}",
            # rezolvate pe oră (păstrate sub aceleași chei ca înainte)
            "mail": h_mail.get(hh, 0),
            "task": h_task.get(hh, 0),
            "apel": h_call_done.get(hh, 0),
            "device": h_dev.get(hh, 0),
            # intrate pe oră
            "mail_new": h_mail_new.get(hh, 0),
            "task_new": h_task_new.get(hh, 0),
            "apel_new": h_call_new.get(hh, 0),
            "device_new": h_dev_new.get(hh, 0),
            # încă deschise, raportate la ora sosirii (doar ce a intrat azi)
            "mail_open": h_mail_open.get(hh, 0),
            "task_open": h_task_open.get(hh, 0),
            "apel_open": h_call_open.get(hh, 0),
            "device_open": h_dev_open.get(hh, 0),
            "total": h_mail.get(hh, 0) + h_task.get(hh, 0) + h_call_done.get(hh, 0),
        }
        for hh in range(24)
    ]

    # defalcare pe departament (doar departamentele grupului curent)
    #
    # Fiecare canal are propria interogare grupată pe departament, cu EXACT aceleași join-uri,
    # filtre și convenții de timp ca agregatele de grup de mai sus — altfel suma cardurilor per
    # departament n-ar da totalul afișat în contoarele de sus. Convenții refolosite:
    #   - email rezolvat azi  = cts_status solved/closed + DATE(cts_solved_at) = azi
    #   - email nou azi       = raw->extra->created_at cade azi (momentul real de sosire)
    #   - task rezolvat azi   = status solved/closed + DATE(cts_updated_at) = azi
    #   - task în lucru       = status 'in progress' (literal, cu spațiu în DB)
    #   - task nou azi        = DATE(cts_created_at) = azi
    #   - apel azi            = DATE(cts_started_at) = azi
    #   - reclamații          = categorie 'reclamatie' din coalesce(cts_category, ai_category)
    per_dept = []
    if depts:
        def _by_dept(sql: str, ncols: int) -> dict:
            """Rulează o interogare grupată pe departament; la eroare întoarce dict gol."""
            try:
                rows = db.execute(text(sql), {"depts": depts}).fetchall()
                return {r[0]: tuple(int(r[i] or 0) for i in range(1, ncols + 1)) for r in rows}
            except Exception:
                logger.exception("monitor_live per_dept")
                return {}

        # Departamentul unui mail = cel al TICHETULUI din CTS (`cts_department`), cu fallback pe
        # departamentul assignee-ului. Vezi comentariul de la `_dep_email` in get_dashboard_data:
        # JOIN-ul INNER pe assignee arunca mailurile neasignate (40% din cele 'new'), motiv pentru
        # care Suport 1 arata 0 'new' desi CTS avea 47.
        # Stările deschise sunt filtrate pe ce a SOSIT AZI — aceeași regulă ca la contoarele de
        # grup de mai sus, ca suma cardurilor să rămână egală cu totalul.
        d_mail = _by_dept(f"""
            SELECT COALESCE(g.cts_department, edm.department) AS dept,
                   COUNT(*) FILTER (WHERE g.cts_status IN ('solved','closed')
                                    AND DATE(g.cts_solved_at AT TIME ZONE '{_TZ}') = CURRENT_DATE) AS rezolvate_azi,
                   COUNT(*) FILTER (WHERE g.cts_status = 'in progress'
                                    AND DATE({_EMAIL_ARRIVED_LOCAL}) = CURRENT_DATE)                AS in_lucru,
                   COUNT(*) FILTER (WHERE g.cts_status = 'new'
                                    AND DATE({_EMAIL_ARRIVED_LOCAL}) = CURRENT_DATE)                AS noi,
                   COUNT(*) FILTER (WHERE {_EMAIL_ARRIVED_LOCAL} IS NOT NULL
                                    AND DATE({_EMAIL_ARRIVED_LOCAL}) = CURRENT_DATE) AS intrate_azi
            FROM cts_ground_truth g
            LEFT JOIN employee_department_mapping edm
              ON lower(g.cts_assignee_email) = lower(edm.email)
            {_J_EMAIL_EXCL}
            WHERE g.cts_deleted_at IS NULL
              AND COALESCE(g.cts_direction,'received') = 'received'
              AND COALESCE(g.cts_department, edm.department) = ANY(:depts)
              AND {_EMAIL_EXCLUDE_SQL}
            GROUP BY 1
        """, 4)

        d_task = _by_dept(f"""
            SELECT COALESCE(t.department, edm.department) AS dept,
                   COUNT(*) FILTER (WHERE t.status IN ('solved','closed')
                                    AND DATE(t.cts_updated_at AT TIME ZONE '{_TZ}') = CURRENT_DATE) AS rezolvate_azi,
                   COUNT(*) FILTER (WHERE t.status = 'in progress'
                                    AND DATE(t.cts_created_at AT TIME ZONE '{_TZ}') = CURRENT_DATE)   AS in_progress,
                   COUNT(*) FILTER (WHERE t.status IN ('new','postponed')
                                    AND DATE(t.cts_created_at AT TIME ZONE '{_TZ}') = CURRENT_DATE)   AS noi,
                   COUNT(*) FILTER (WHERE DATE(t.cts_created_at AT TIME ZONE '{_TZ}') = CURRENT_DATE) AS intrate_azi
            FROM cts_task_ground_truth t
            LEFT JOIN employee_department_mapping edm
              ON t.assignee_employee_id = edm.id
            WHERE COALESCE(t.department, edm.department) = ANY(:depts)
            GROUP BY 1
        """, 4)

        d_call = _by_dept(f"""
            SELECT edm.department AS dept,
                   COUNT(*) FILTER (WHERE DATE(c.cts_started_at AT TIME ZONE '{_TZ}') = CURRENT_DATE) AS azi,
                   COUNT(*) FILTER (WHERE DATE(c.cts_started_at AT TIME ZONE '{_TZ}') = CURRENT_DATE
                                    AND lower(c.cts_status) IN ('solved','closed'))                    AS rezolvate_azi
            FROM cts_calls_ground_truth c
            JOIN employee_department_mapping edm
              ON lower(c.cts_assignee_email) = lower(edm.email)
            WHERE edm.department = ANY(:depts)
            GROUP BY 1
        """, 2)

        # Reclamații: primite azi + rezolvate azi + deschise acum. Sursa e categoria emailului
        # (nu există tabelă dedicată). "Primite" = momentul real de sosire (`extra.email_date`,
        # cu fallback pe emails.received_at); "rezolvate" = cts_solved_at azi.
        #
        # ATENȚIE: primite_azi și rezolvate_azi sunt seturi DIFERITE, nu un flux. O reclamație
        # primită pe 01.08 și rezolvată pe 03.08 apare doar la "rezolvate" — de aceea combinația
        # "0 primite / 1 rezolvată" e corectă, deși pare imposibilă (caz real Suport 2, 03.08:
        # email 66619023, primit 01.08 21:33, rezolvat 03.08 06:30). `deschise` e ancora care dă
        # sens celor două: câte sunt deschise ACUM, indiferent de ziua sosirii.
        # RECLAMATII: sursa e modulul Quality Evaluation din CTS (`cts_quality_evaluation`,
        # sincronizat din IRIS DV), NU categoria emailului ca pana la v2.10.0. Categoria de email
        # marca alt lucru -- un mail incadrat 'reclamatie' de operator -- si nu se potrivea cu ce
        # se vede in CTS (constatat 2026-08-13: Suport 1 arata 1 rezolvata azi desi in CTS nu era
        # niciuna). Aici un rand = o reclamatie reala, cu propriul ciclu new -> in progress -> solved.
        #
        # Departamentul afisat e al persoanei EVALUATE (`department_id` din CTS, tradus prin
        # departamentul angajatilor nostri). Pentru PRODUCTIVITATE aceleasi reclamatii merg
        # integral la Suport 3, echipa care le proceseaza -- vezi _fetch_reclamatie_rows.
        d_recl = _by_dept(f"""
            SELECT dept,
                   COUNT(*) FILTER (WHERE primit_azi)   AS primite_azi,
                   COUNT(*) FILTER (WHERE rezolvat_azi) AS rezolvate_azi,
                   COUNT(*) FILTER (WHERE deschisa)     AS deschise
            FROM (
                SELECT dep.department AS dept,
                       (DATE(qe.created_at AT TIME ZONE '{_TZ}') = CURRENT_DATE) AS primit_azi,
                       (qe.status = 3
                        AND DATE(qe.solved_at AT TIME ZONE '{_TZ}') = CURRENT_DATE) AS rezolvat_azi,
                       (qe.status IS DISTINCT FROM 3)                               AS deschisa
                FROM cts_quality_evaluation qe
                -- department_id (CTS) -> slug-ul nostru, dedus din angajatii mapati. LATERAL +
                -- LIMIT 1: `cts_dv_employee` are randuri multiple per persoana.
                JOIN LATERAL (
                    SELECT e.department
                    FROM cts_dv_employee dv
                    JOIN employee_department_mapping e ON lower(e.email) = lower(dv.email)
                    -- `cts_dv_employee.department_id` e TEXT (oglinda bruta a DV-ului), iar in
                    -- reclamatie e INT: fara cast, join-ul crapa si contoarele ies 0.
                    WHERE dv.department_id = qe.department_id::text AND e.enabled = true
                    GROUP BY e.department
                    ORDER BY count(*) DESC, e.department
                    LIMIT 1
                ) dep ON true
                WHERE qe.deleted_at IS NULL
                  AND dep.department = ANY(:depts)
            ) s
            GROUP BY 1
        """, 3)

        _z4, _z3, _z2 = (0, 0, 0, 0), (0, 0, 0), (0, 0)
        per_dept = [
            {
                "department": d,
                # chei păstrate pentru compatibilitate cu orice consumator existent
                "rezolvate_azi": d_mail.get(d, _z4)[0],
                "in_lucru":      d_mail.get(d, _z4)[1],
                "emailuri": {
                    "rezolvate_azi": d_mail.get(d, _z4)[0],
                    "in_lucru":      d_mail.get(d, _z4)[1],
                    "noi":           d_mail.get(d, _z4)[2],
                    "intrate_azi":   d_mail.get(d, _z4)[3],
                },
                "taskuri": {
                    "rezolvate_azi": d_task.get(d, _z4)[0],
                    "in_progress":   d_task.get(d, _z4)[1],
                    "noi":           d_task.get(d, _z4)[2],
                    "intrate_azi":   d_task.get(d, _z4)[3],
                },
                "apeluri": {
                    "azi":           d_call.get(d, _z2)[0],
                    "rezolvate_azi": d_call.get(d, _z2)[1],
                },
                "reclamatii": {
                    "primite_azi":   d_recl.get(d, _z3)[0],
                    "rezolvate_azi": d_recl.get(d, _z3)[1],
                    "deschise":      d_recl.get(d, _z3)[2],
                },
            }
            for d in depts
        ]

    # Volumul INTRAT azi (indiferent de starea actuală) — alimentează indicatorul "Ritm"
    # (rezolvat azi / intrat azi). E o metrică distinctă de "Noi": un mail intrat azi și deja
    # rezolvat intră aici, dar NU la "Noi". Se derivă din bucket-urile orare deja calculate,
    # fără interogări suplimentare.
    mail_in_azi = sum(h_mail_new.values())
    task_in_azi = sum(h_task_new.values())

    return {
        "ts": _dt2.datetime.now().isoformat(timespec="seconds"),
        "group": group,
        # `in_lucru` / `noi` / `in_progress` / `pending` = doar din ce a sosit AZI (vezi nota de
        # la email_row). `rezolvate_azi` a fost mereu pe ziua curentă.
        "emailuri": {
            "rezolvate_azi": int(email_row[0] or 0),
            "in_lucru":      int(email_row[1] or 0),
            "noi":           int(email_row[2] or 0),
            "intrate_azi":   int(mail_in_azi or 0),
        },
        "taskuri": {
            "rezolvate_azi": int(task_row[0] or 0),
            "in_progress":   int(task_row[1] or 0),
            "pending":       int(task_row[2] or 0),
            "intrate_azi":   int(task_in_azi or 0),
        },
        "apeluri": {
            "azi":           int(call_row[0] or 0),
            "rezolvate_azi": int(call_row[1] or 0),
            "in_curs":       int(call_row[2] or 0),
        },
        "device_ops": {
            "rezolvate_azi": int(dev_row[0] or 0),
            "in_asteptare":  int(dev_row[1] or 0),
        },
        "sesizari": {
            "deschise":            int(sesiz_row[0] or 0),
            "sesizari_deschise":   int(sesiz_row[1] or 0),
            "reclamatii_deschise": int(sesiz_row[2] or 0),
            "rezolvate_azi":       int(sesiz_row[3] or 0),
            # deschise dinainte de azi (restanțe) și cele care depășesc 7 zile
            "restante":            int(sesiz_row[4] or 0),
            "peste_7z":            int(sesiz_row[5] or 0),
            # intrate azi pe telefon (aceleași categorii, sursă apeluri)
            "apel_sesizari_azi":   int(sesiz_call[0] or 0),
            "apel_reclamatii_azi": int(sesiz_call[1] or 0),
        },
        "rezolvate_categorii": rezolvate_categorii,
        "hourly": hourly,
        "per_dept": per_dept,
    }


@router.get("/productivity/dashboard/{group}")
def get_dashboard_page(group: str, refresh: int = Query(10, ge=1, le=60)):
    """Pagina HTML standalone pentru monitorul de productivitate (public, fără sidebar/auth)."""
    from fastapi.responses import HTMLResponse

    if group not in ("operational", "financiar"):
        raise HTTPException(status_code=404, detail="grup necunoscut")

    group_label = "Operațional" if group == "operational" else "Financiar"
    # cache-busting: citit din VERSION, altfel browserul servește mg-app.js din cache
    try:
        from pathlib import Path as _Path
        version = (_Path(__file__).resolve().parents[3] / "VERSION").read_text().strip()
    except Exception:
        version = "0"

    html = f"""<!DOCTYPE html>
<html lang="ro" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Monitor Productivitate — {group_label}</title>
<script src="/vendor/mg-theme-init.js"></script>
<link href="/vendor/fonts.css" rel="stylesheet">
<link href="/vendor/mg-dash.css" rel="stylesheet">
<script src="/vendor/react.production.min.js"></script>
<script src="/vendor/react-dom.production.min.js"></script>
<script src="/vendor/chart.umd.min.js"></script>
<script src="/vendor/gauge.min.js"></script>
</head>
<body>
<div id="root"></div>
<script src="/vendor/mg-app.js?v={version}"></script>
</body>
</html>"""
    return HTMLResponse(content=html)
