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
# Excluderea are DOUA laturi: flagul pe clientul dedus local (`emails.client_id`, prin `pex`) si
# clientul ATRIBUIT IN CTS (`extra.client_id` = ID IRIS) -- vezi P._EMAIL_EXCLUDE_CTS_SQL. Toate
# query-urile de mail de aici folosesc alias-ul `g` pentru `cts_ground_truth`.
_EMAIL_EXCLUDE_SQL = (P._EMAIL_EXCLUDE_SQL + "\n          AND "
                      + P._EMAIL_EXCLUDE_CTS_SQL.format(g='g'))
# Sursele de task-uri / apeluri / operatiuni, filtrate de clientii exclusi din productivitate
# (`clients.productivity_exclude`). Se filtreaza in SUBQUERY, nu in WHERE-ul fiecarui query:
# monitorul are ~15 interogari pe aceste tabele, iar un filtru uitat intr-una din ele ar face ca
# doua panouri sa arate volume diferite pentru aceeasi zi.
_SRC_TASK = ("(SELECT * FROM cts_task_ground_truth _t WHERE "
             + P._TASK_EXCLUDE_SQL.format(t='_t') + ")")
_SRC_CALLS = ("(SELECT * FROM calls _c WHERE "
              + P._APEL_EXCLUDE_SQL.format(c='_c') + ")")
_SRC_DEVOPS = ("(SELECT * FROM device_operations _d WHERE NOT EXISTS ("
               "SELECT 1 FROM clients cex WHERE cex.productivity_exclude "
               "AND (cex.id = _d.client_id OR lower(cex.name) = lower(_d.client_name))))")
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


@router.get("/productivity/daily")
def get_daily(month: Optional[str] = Query(None),
              department: Optional[str] = Query(None),
              db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Productivitate pe ZILE, defalcata pe obiective (tab Rapoarte -> „Productivitate zilnica").

    Aceleasi randuri ca raportul lunar (`breakdown_rows`), grupate pe ziua rezolvarii. Fara
    `department` -> toate departamentele configurate, ca la /report.
    """
    y, m = _parse_month(db, month)
    depts = [department] if department else _configured_departments(db)
    return {
        "month": f"{y:04d}-{m:02d}",
        "departments": [P.daily_report(db, d, y, m) for d in depts],
    }


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
                         month: Optional[str] = Query(None, description="YYYY-MM; implicit luna curenta"),
                         db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Operatorii unui departament pentru selectorul din tab Analiză.

    Cu `month` -> componenta departamentului IN LUNA ACEEA (employee_department_history), ca
    selectorul sa arate oamenii care erau atunci acolo, nu pe cei de azi. Fara `month` -> luna
    curenta, adica exact comportamentul dinainte.
    """
    dep = (department or "").strip().lower()
    if not dep or dep in ("operational", "general", "all", "toate", "__all__"):
        return []
    y, m = _parse_month(db, month)
    rows = db.execute(
        text("SELECT id, name FROM employee_department_mapping "
             "WHERE id IN (SELECT employee_id FROM employee_dept_members(:d, CAST(:first AS date))) "
             "ORDER BY name"),
        {"d": dep, "first": f"{y:04d}-{m:02d}-01"},
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
def send_notifications_now(force: bool = Query(False, description="retrimite chiar dacă a plecat deja"),
                           db: Session = Depends(get_db), admin=Depends(require_prod_full)):
    """Declanșează trimiterea manuală a rapoartelor (ignoră gating zi/oră).

    NU ignoră evidența per destinatar: cine a primit deja raportul lunii respective e sărit, deci
    un click repetat nu mai trimite nimic. `force=true` e singura cale de retrimitere — decizie
    explicită, cerută în URL.
    """
    from app.services import productivity_notifier as _pn
    try:
        result = _pn.send_monthly_reports(db, force=bool(force), claimed_by="manual")
        return {"ok": True, **result}
    except Exception as exc:
        logger.exception("send_notifications_now failed")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/productivity/notifications/log")
def notifications_log(month: str = Query("", description="YYYY-MM; implicit ultimele 6 luni"),
                      limit: int = Query(200, ge=1, le=1000),
                      db: Session = Depends(get_db), admin=Depends(require_prod_full)):
    """Evidența trimiterilor lunare — cine a primit raportul, când, și ce a eșuat.

    Sursa de adevăr pentru „s-a trimis sau nu": rândul se scrie ÎNAINTE de trimitere, deci un
    `claimed` rămas fără `sent_at` înseamnă că procesul a murit între rezervare și SMTP.
    """
    rows = db.execute(text(
        "SELECT month_key, department_group, recipient_email, status, error, "
        "       claimed_at, sent_at, claimed_by "
        "  FROM productivity_notification_log "
        " WHERE (:m = '' OR month_key = :m) "
        " ORDER BY month_key DESC, department_group, recipient_email "
        " LIMIT :lim"), {"m": (month or "").strip(), "lim": limit}).mappings().all()
    return {"items": [{
        "month": r["month_key"], "group": r["department_group"], "email": r["recipient_email"],
        "status": r["status"], "error": r["error"], "claimed_by": r["claimed_by"],
        "claimed_at": r["claimed_at"].isoformat() if r["claimed_at"] else None,
        "sent_at": r["sent_at"].isoformat() if r["sent_at"] else None,
    } for r in rows]}


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

# ── FRAGMENTELE MONITORULUI — la nivel de MODUL, nu locale in `get_monitor_live` ─────────────
# Au fost variabile locale pana pe 2026-09-22. De aceea `_by_dept` a putut rescrie expresia de
# departament INLINE, iar cele doua copii au divergit tacut de restul aplicatiei. Orice interogare
# noua care raspunde la „ce e deschis acum" (contor de grup, carduri, grafic pe ore, endpoint de
# audit) foloseste DE AICI, nu isi scrie propria varianta.

# „DESCHIS" = NEterminal, adica NOT IN ('solved','closed') — NU o lista alba de stari.
# `status` e text liber in ambele tabele („enum CTS TBD" in migratia 20260702), iar setul confirmat
# de vendor (Razvan, 2026-07-02) e: unallocated, new, in_progress, postponed, closed, solved. O
# lista alba ar rata `unallocated` (task alocat nimanui — exact munca pe care nimeni n-a preluat-o)
# si ar depinde de ortografia lui in_progress, scrisa in feed in ambele feluri („in progress" cu
# spatiu la task-uri, „in_progress" in `cts_groundtruth_sync`). `lower` + `btrim` + `COALESCE` fac
# expresia imuna la majuscule, spatii si NULL.
# `monitor_closed_at` = inchiderea NOASTRA, decisa in aplicatie (junk vechi, vezi
# migrations/20260922_monitor_junk_close.sql si POST /productivity/monitor/close-backlog). NU se
# scrie in `cts_status`: aceea e oglinda CTS, pe care upsert-ul de sync o suprascrie la fiecare
# rulare. Face parte din definitia de „deschis" ca sa se aplice IDENTIC in toate barele.
# Stergerile: mailurile prin `cts_deleted_at IS NULL` (in WHERE), task-urile n-au coloana de stergere.
# NB: PARANTEZATE. Predicatul e compus (doua conditii), iar apelantii il combina cu `AND`/`NOT`
# — fara paranteze, un `NOT {_EMAIL_OPEN_STATES}` ar nega doar primul termen, tacut.
_EMAIL_OPEN_STATES = ("(lower(btrim(COALESCE(g.cts_status,''))) NOT IN ('solved','closed') "
                      "AND g.monitor_closed_at IS NULL)")
_TASK_OPEN_STATES = ("(lower(btrim(COALESCE(t.status,''))) NOT IN ('solved','closed') "
                     "AND t.monitor_closed_at IS NULL)")
# „IN LUCRU" = preluat de cineva. Ambele ortografii, fiindca feed-ul le scrie pe amandoua:
# task-urile ca „in progress", iar `cts_groundtruth_sync` verifica mailurile pe „in_progress".
# Restul starilor deschise (new, unallocated, postponed, NULL) sunt NEPRELUATE, deci merg la „Noi".
_EMAIL_WIP = "lower(btrim(COALESCE(g.cts_status,''))) IN ('in progress','in_progress')"
_TASK_WIP = "lower(btrim(COALESCE(t.status,''))) IN ('in progress','in_progress')"
# „NU E DIN ZIUA CURENTA" ca NEGARE EXACTA a ferestrei folosite de barele zilei, nu ca
# `< CURRENT_DATE`. Motivul: `CURRENT_DATE` e ziua serverului DB, iar data comparata e convertita
# in Europe/Bucharest. Daca Postgres ruleaza pe UTC, intre 00:00 si 03:00 local cele doua nu
# coincid, si un rand sosit atunci n-ar fi nici „de azi" nici „< azi" — ar disparea din toate
# barele, tacut. `IS DISTINCT FROM` acopera si sosirea NULL (join LEFT pe `emails`,
# `cts_created_at` nullable), deci cele trei bare partitioneaza exact randurile deschise.
_EMAIL_BEFORE_TODAY = f"(DATE({_EMAIL_ARRIVED_LOCAL}) IS DISTINCT FROM CURRENT_DATE)"
_TASK_BEFORE_TODAY = (f"(DATE(t.cts_created_at AT TIME ZONE '{_TZ}') "
                      f"IS DISTINCT FROM CURRENT_DATE)")

# ATRIBUIREA PE DEPARTAMENT — o SINGURA sursa, in serviciu. Vezi comentariul complet de la
# `productivity._LIVE_DEPT_EMAIL_SQL`: omul asignat intai, coada CTS ca rezerva.
_DEP_EMAIL_JOIN = P._LIVE_DEPT_EMAIL_JOIN.format(e='edm', g='g')
_EFF_DEPT_EMAIL = P._LIVE_DEPT_EMAIL_SQL.format(e='edm', g='g')
_DEP_EMAIL_W = f"AND {_EFF_DEPT_EMAIL} = ANY(:depts)"
_DEP_TASK_JOIN = P._LIVE_DEPT_TASK_JOIN.format(e='edm', t='t')
_EFF_DEPT_TASK = P._LIVE_DEPT_TASK_SQL.format(e='edm', t='t')


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
    # ── ATRIBUIREA PE DEPARTAMENT ───────────────────────────────────────────────────────────
    # OMUL ASIGNAT INTAI, COADA CA REZERVA — `_EFF_DEPT_EMAIL` / `_EFF_DEPT_TASK`, definite la
    # nivel de modul din `productivity._LIVE_DEPT_*_SQL` (sursa unica, vezi comentariul de acolo).
    #
    # Pana pe 2026-09-22 era invers (coada intai). Doua consecinte, amandoua reclamate de
    # utilizatori: tichetele parcate pe coada suport_1 dar lucrate de oameni din alte departamente
    # umflau Suport 1 (26 de restante raportate, ~4 reale), iar munca oamenilor din taxe_drum
    # parcata pe alte cozi nu se vedea la ei. Monitorul era SINGURUL loc din aplicatie care
    # atribuia pe coada: raportul lunar, analiticele si breakdown-ul o faceau deja pe assignee,
    # deci gauge-ul si barele de pe ACELASI card raspundeau dupa reguli diferite.
    #
    # Join-ul ramane LEFT, si rezerva pe coada ramane: fixul din 2026-08-06 (69 din 171 de mailuri
    # 'new' n-au assignee — in CTS stau in coada departamentului) nu se pierde. S-a inversat doar
    # PRECEDENTA, nu tratarea randurilor neasignate.
    #
    # ── RESTANTA: deschis ACUM, dar sosit INAINTE de azi ────────────────────────────────────
    # Un mail/task din 02.09 ramas 'new'/'in progress' trebuie sa se vada si pe 03.09 (cerere
    # business owner, 2026-09-10). NU se amesteca insa in barele „Noi"/„In lucru": acelea raman pe
    # ziua curenta, fiindca exact amestecul lor cu restanta istorica a dus la limitarea din
    # 2026-08-13 (CTS lasa tichete deschise la nesfarsit — Financiar avea 769 'new', unele din
    # martie). Restanta e o BARA SEPARATA, deci nimic nu se pierde si nimic nu se contamineaza.
    # Predicatele (`_EMAIL_OPEN_STATES`, `_EMAIL_WIP`, `_EMAIL_BEFORE_TODAY` + omoloagele de task)
    # sunt la nivel de MODUL — nu le rescrie inline, vezi nota de acolo.
    _dep_email = _DEP_EMAIL_JOIN
    _dep_email_w = _DEP_EMAIL_W
    # task-uri și device ops au cheie străină numerică spre edm.id (NU iris_id, care
    # e text și nu se potrivește)
    _dep_task = _DEP_TASK_JOIN
    _dep_dev = """
        JOIN employee_department_mapping edm
          ON d.closed_by_employee_id = edm.id AND edm.department = ANY(:depts)
    """
    # Apelurile nu mai vin din CTS (v2.12.0): atribuirea pe departament se face cu
    # `P._APEL_AGENT_JOIN` (agentul din centrală), deci join-ul pe assignee-ul CTS a dispărut.

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
            COUNT(*) FILTER (WHERE {_EMAIL_OPEN_STATES} AND {_EMAIL_WIP}
                             AND DATE({_EMAIL_ARRIVED_LOCAL}) = CURRENT_DATE)                AS in_lucru,
            COUNT(*) FILTER (WHERE {_EMAIL_OPEN_STATES} AND NOT {_EMAIL_WIP}
                             AND DATE({_EMAIL_ARRIVED_LOCAL}) = CURRENT_DATE)                AS noi,
            COUNT(*) FILTER (WHERE {_EMAIL_OPEN_STATES} AND {_EMAIL_BEFORE_TODAY})           AS restanta
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
            COUNT(*) FILTER (WHERE {_TASK_OPEN_STATES} AND {_TASK_WIP}
                             AND DATE(t.cts_created_at AT TIME ZONE '{_TZ}') = CURRENT_DATE)  AS in_progress,
            COUNT(*) FILTER (WHERE {_TASK_OPEN_STATES} AND NOT {_TASK_WIP}
                             AND DATE(t.cts_created_at AT TIME ZONE '{_TZ}') = CURRENT_DATE)  AS pending,
            COUNT(*) FILTER (WHERE {_TASK_OPEN_STATES} AND {_TASK_BEFORE_TODAY})              AS restanta
        FROM {_SRC_TASK} t
        {_dep_task}
        WHERE {_EFF_DEPT_TASK} = ANY(:depts)
    """), _p).fetchone()

    # APELURI AZI — sursa e `calls` (While1), aceeași pe care o arată pagina Apeluri și canalul
    # „Apeluri" din Productivitate (v2.12.0). Până aici se citea din `cts_calls_ground_truth`
    # (Apeluri CTS): alt set de date — doar apelurile care au ajuns tichet în CTS — și alt ciclu de
    # viață (new → in progress → solved), deci monitorul și raportul lunar spuneau cifre diferite
    # pentru aceeași zi.
    #
    # Ce se poate spune și ce NU, din centrală:
    #   - RĂSPUNS / PIERDUT: da. Un rând CDR apare abia după ce apelul s-a încheiat.
    #   - `in_curs`: NU există. Un apel în desfășurare nu e încă în CDR, deci cheia rămâne în
    #     răspuns (compatibilitate) dar e mereu 0 — nu mai e o restanță „neînchisă" ca în CTS.
    # Filtrele de leg (apel real / pierdut) sunt definițiile unice din productivity.py.
    call_row = db.execute(text(f"""
        {P._APEL_AGENT_CTE}
        SELECT
            COUNT(*) FILTER (WHERE {P._APEL_REAL_CALL_SQL})                              AS azi,
            COUNT(*) FILTER (WHERE {P._APEL_UNANSWERED_SQL} AND {P._APEL_LOST_CALL_SQL}) AS pierdute_azi
        FROM {_SRC_CALLS} c
        {P._APEL_AGENT_JOIN}
        WHERE c.direction = 'inbound'
          AND {P._APEL_DAY_SQL} = {P._APEL_TODAY_RO_SQL}
          AND edm.department = ANY(:depts)
    """), _p).fetchone()

    # APELURI PIERDUTE, LA NIVEL DE FIRMĂ. Nu se pot împărți pe departamente: un apel pierdut n-a
    # fost preluat de nimeni, deci centrala nu scrie agent pe el — pe august 2026, din 666 apeluri
    # efectiv pierdute doar 72 (11%) au `agent_extension`. Restul au doar linia apelată
    # (`callee_number`: 0374430060 linia principală, 022022292 linia MD), iar linia e a firmei, nu
    # a unui departament. De aceea cifra asta se afișează o singură dată, în capul monitorului, și
    # NU pe cardurile per departament — acolo ar arăta 11% din realitate.
    lost_row = db.execute(text(f"""
        SELECT COUNT(*)
        FROM {_SRC_CALLS} c
        WHERE c.direction = 'inbound'
          AND {P._APEL_UNANSWERED_SQL}
          AND {P._APEL_LOST_CALL_SQL}
          AND {P._APEL_DAY_SQL} = {P._APEL_TODAY_RO_SQL}
    """)).fetchone()

    # SESIZĂRI — categorie de email, nu există tabelă dedicată. Același COALESCE ca în
    # productivity._fetch_email_rows (ground truth primează, ai_category e fallback pentru ce
    # n-a fost încă clasificat în CTS).
    #
    # RECLAMAȚIILE au ieșit din interogarea asta (2026-08-14): sursa lor e acum modulul Quality
    # Evaluation (`recl_row` mai jos), nu categoria emailului. Filtrele de mai jos numără DOAR
    # 'sesizare'; cifrele mixte se compun în răspuns, din cele două surse.
    sesiz_row = db.execute(text(f"""
        SELECT
            COUNT(*) FILTER (WHERE cat = 'sesizare' AND NOT rezolvat)                              AS deschise,
            COUNT(*) FILTER (WHERE cat = 'sesizare' AND NOT rezolvat)                              AS sesizari_deschise,
            COUNT(*) FILTER (WHERE cat = 'reclamatie' AND NOT rezolvat)                            AS reclamatii_email_deschise,
            COUNT(*) FILTER (WHERE cat = 'sesizare' AND rezolvat AND solved_azi)                   AS rezolvate_azi,
            COUNT(*) FILTER (WHERE cat = 'sesizare' AND NOT rezolvat
                             AND DATE(changed_loc) < CURRENT_DATE)                                 AS restante,
            COUNT(*) FILTER (WHERE cat = 'sesizare' AND NOT rezolvat
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

    # Sesizări/reclamații venite pe telefon — aceleași categorii, dar pe sursa Apeluri (`calls`),
    # ca restul contoarelor de apel. Categoria e cea pusă de AI pe transcript (`ai_category`);
    # varianta anterioară citea `cts_calls_ground_truth.cts_category` (încadrarea omului în CTS),
    # care e mai bună ca adevăr dar apare abia după ce apelul devine tichet — deci pe monitorul de
    # AZI arăta sistematic mai puțin decât lista de apeluri.
    sesiz_call = db.execute(text(f"""
        {P._APEL_AGENT_CTE}
        SELECT
            COUNT(*) FILTER (WHERE lower(c.ai_category) = 'sesizare')   AS sesizari_azi,
            COUNT(*) FILTER (WHERE lower(c.ai_category) = 'reclamatie') AS reclamatii_azi
        FROM {_SRC_CALLS} c
        {P._APEL_AGENT_JOIN}
        WHERE c.direction = 'inbound'
          AND {P._APEL_REAL_CALL_SQL}
          AND {P._APEL_DAY_SQL} = {P._APEL_TODAY_RO_SQL}
          AND edm.department = ANY(:depts)
    """), _p).fetchone()

    # RECLAMAȚII (agregat pe grup) — sursa e modulul Quality Evaluation din CTS
    # (`cts_quality_evaluation`), NU categoria emailului. Categoria marca alt lucru: un mail pe
    # care operatorul l-a încadrat „reclamatie", care nu se potrivea cu ce se vede în CTS
    # (constatat 2026-08-13: 8 „reclamații deschise" pe Operațional din emailuri, față de 16
    # reclamații reale deschise în CTS). Aici un rând = o reclamație reală, cu ciclul ei
    # new → in progress → solved.
    #
    # Departamentul e al persoanei EVALUATE, exact ca la cardurile per departament de mai jos,
    # deci suma cardurilor = cifra de grup de aici. (În productivitate aceleași reclamații merg
    # integral la Suport 3 — echipa care le procesează — vezi _fetch_reclamatie_rows.)
    _recl_sql = text(f"""
        SELECT
            COUNT(*) FILTER (WHERE primit_azi)                                  AS primite_azi,
            COUNT(*) FILTER (WHERE rezolvat_azi)                                AS rezolvate_azi,
            COUNT(*) FILTER (WHERE deschisa)                                    AS deschise,
            COUNT(*) FILTER (WHERE deschisa AND NOT primit_azi)                 AS restante,
            COUNT(*) FILTER (WHERE deschisa AND creat < NOW() - INTERVAL '7 days') AS peste_7z,
            COUNT(*) FILTER (WHERE primit_azi AND entity = 'client_call_log')   AS apel_azi,
            -- Cele două stări pe care le arată monitorul (decizie business owner, 2026-08-18):
            -- reclamație înregistrată dar încă nepreluată, și una în lucru. `deschise` de mai sus
            -- rămâne suma lor (folosit de blocul `sesizari`), nu se schimbă sensul cheii vechi.
            COUNT(*) FILTER (WHERE noua)     AS noi,
            COUNT(*) FILTER (WHERE in_lucru) AS in_lucru,
            -- Monitorul arata DOUA cifre (decizie business owner, 2026-08-18): cite reclamatii
            -- s-au inregistrat in luna curenta si cite sint acum in lucru. "Deschise" (status 1)
            -- iese din card: in CTS reclamatiile nu stau in 'new' -- pe eșantionul curent sint 0
            -- randuri cu status 1, deci bara ar fi fost permanent goala.
            COUNT(*) FILTER (WHERE luna_curenta) AS total_luna
        FROM (
            SELECT qe.entity AS entity,
                   qe.created_at AS creat,
                   (DATE(qe.created_at AT TIME ZONE '{_TZ}') = CURRENT_DATE) AS primit_azi,
                   (qe.status = 3
                    AND DATE(qe.solved_at AT TIME ZONE '{_TZ}') = CURRENT_DATE) AS rezolvat_azi,
                   (qe.status IS DISTINCT FROM 3)                               AS deschisa,
                   -- status CTS: 1 = new, 2 = in progress, 3 = solved (vezi migrația 20260813d)
                   -- `noua` / `in_lucru` se raporteaza la LUNA CURENTA, ca `total_luna` de langa
                   -- ele pe card. Pana la v3.3.1 numarau pe tot istoricul, deci cardul punea
                   -- alaturi doua cifre cu numitori diferiti: „11 in luna" langa „9 in lucru",
                   -- din care 7 erau reclamatii vechi, nu din luna afisata (taxe_drum, 19.08).
                   -- Aceeasi conventie ca la mail/task pe monitor: fereastra afisata, nu restanta
                   -- istorica.
                   (qe.status = 1 AND date_trunc('month', qe.created_at AT TIME ZONE '{_TZ}')
                                     = date_trunc('month', (NOW() AT TIME ZONE '{_TZ}')))  AS noua,
                   (qe.status = 2 AND date_trunc('month', qe.created_at AT TIME ZONE '{_TZ}')
                                     = date_trunc('month', (NOW() AT TIME ZONE '{_TZ}')))  AS in_lucru,
                   (date_trunc('month', qe.created_at AT TIME ZONE '{_TZ}')
                      = date_trunc('month', (NOW() AT TIME ZONE '{_TZ}')))       AS luna_curenta
            FROM cts_quality_evaluation qe
            -- ATRIBUIRE = departamentul persoanei EVALUATE (`responsible_id`), cu
            -- `department_id`-ul din CTS doar ca fallback. Pina la v2.13.0 monitorul folosea NUMAI
            -- fallback-ul, deci punea reclamatia pe alt departament decit pagina Reclamatii, care
            -- foloseste `COALESCE(ev.department, dep.department)`: o reclamatie inregistrata in CTS
            -- pe "Suport 1", dar cu responsabil din Comercial, aparea pe cardul Suport 1 si in
            -- lista la Comercial. Aceeasi expresie in ambele locuri = aceeasi cifra.
            LEFT JOIN LATERAL (
                SELECT e.department
                FROM cts_dv_employee dv
                JOIN employee_department_mapping e ON lower(e.email) = lower(dv.email)
                WHERE dv.admin_id = qe.responsible_id::text
                ORDER BY e.enabled DESC, e.id
                LIMIT 1
            ) ev ON true
            -- Fallback: departamentul dominant al `department_id`-ului din CTS. LATERAL + LIMIT 1:
            -- `cts_dv_employee` are rânduri multiple per persoană, iar `department_id` e TEXT acolo
            -- și INT aici.
            LEFT JOIN LATERAL (
                SELECT e.department
                FROM cts_dv_employee dv
                JOIN employee_department_mapping e ON lower(e.email) = lower(dv.email)
                WHERE dv.department_id = qe.department_id::text AND e.enabled = true
                GROUP BY e.department
                ORDER BY count(*) DESC, e.department
                LIMIT 1
            ) dep ON true
            WHERE qe.deleted_at IS NULL
              AND COALESCE(ev.department, dep.department) = ANY(:depts)
        ) r
    """)
    try:
        recl_row = db.execute(_recl_sql, _p).fetchone()
    except Exception:
        # Monitorul e un ecran de perete: o problemă pe sursa de reclamații nu are voie să
        # doboare tot payload-ul (mail/task/apel rămân valabile). Se raportează 0 și se loghează.
        logger.exception("monitor_live reclamatii (quality evaluation)")
        recl_row = (0, 0, 0, 0, 0, 0, 0, 0, 0)

    # Ce s-a rezolvat azi, pe CATEGORIA EMAILULUI (informație / sesizare / reclamație).
    # Atenție la cheia 'reclamatie' de aici: e categoria pusă pe mail de operator, NU o
    # reclamație din Quality Evaluation. Cifra reală de reclamații e în blocul `reclamatii`.
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
        FROM {_SRC_DEVOPS} d
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
        FROM {_SRC_TASK} t
        {_dep_task}
        WHERE t.status IN ('solved','closed')
          AND DATE(t.cts_updated_at AT TIME ZONE '{_TZ}') = CURRENT_DATE
          AND {_EFF_DEPT_TASK} = ANY(:depts)
        GROUP BY 1
    """)
    # Apeluri pe oră — tot din `calls` (While1), ca și contoarele de sus.
    h_call = _hourly(f"""
        {P._APEL_AGENT_CTE}
        SELECT EXTRACT(hour FROM c.started_at)::int AS h, COUNT(*)
        FROM {_SRC_CALLS} c
        {P._APEL_AGENT_JOIN}
        WHERE c.direction = 'inbound'
          AND {P._APEL_REAL_CALL_SQL}
          AND {P._APEL_DAY_SQL} = {P._APEL_TODAY_RO_SQL}
          AND edm.department = ANY(:depts)
        GROUP BY 1
    """)
    h_dev = _hourly(f"""
        SELECT EXTRACT(hour FROM d.closed_at AT TIME ZONE '{_TZ}')::int AS h, COUNT(*)
        FROM {_SRC_DEVOPS} d
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
        FROM {_SRC_TASK} t
        {_dep_task}
        WHERE DATE(t.cts_created_at AT TIME ZONE '{_TZ}') = CURRENT_DATE
          AND {_EFF_DEPT_TASK} = ANY(:depts)
        GROUP BY 1
    """)
    # Apelurile: "intrate" = când a început apelul; "rezolvate" = când s-a încheiat conversația
    # (start + durată). În centrală un apel răspuns E rezolvat — nu există stare de „ticket
    # deschis", deci cele două serii diferă doar prin ora de încadrare, nu prin mulțime.
    h_call_new = h_call
    h_call_done = _hourly(f"""
        {P._APEL_AGENT_CTE}
        SELECT EXTRACT(hour FROM (c.started_at
                 + make_interval(secs => COALESCE(c.duration_seconds, 0))))::int AS h,
               COUNT(*)
        FROM {_SRC_CALLS} c
        {P._APEL_AGENT_JOIN}
        WHERE c.direction = 'inbound'
          AND {P._APEL_REAL_CALL_SQL}
          AND (c.started_at + make_interval(secs => COALESCE(c.duration_seconds, 0)))::date
              = {P._APEL_TODAY_RO_SQL}
          AND edm.department = ANY(:depts)
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
          AND {_EMAIL_OPEN_STATES}
          AND {_EMAIL_ARRIVED_LOCAL} IS NOT NULL
          AND {_EMAIL_EXCLUDE_SQL}
          AND DATE({_EMAIL_ARRIVED_LOCAL}) = CURRENT_DATE
          {_dep_email_w}
        GROUP BY 1
    """)
    h_task_open = _hourly(f"""
        SELECT EXTRACT(hour FROM t.cts_created_at AT TIME ZONE '{_TZ}')::int AS h, COUNT(*)
        FROM {_SRC_TASK} t
        {_dep_task}
        WHERE {_TASK_OPEN_STATES}
          AND DATE(t.cts_created_at AT TIME ZONE '{_TZ}') = CURRENT_DATE
          AND {_EFF_DEPT_TASK} = ANY(:depts)
        GROUP BY 1
    """)
    # „Încă deschise" pe canalul apeluri = APELURI PIERDUTE la ora respectivă. Un apel nu rămâne
    # deschis (vezi h_call_done), dar unul pierdut e exact echivalentul: a intrat și n-a fost
    # tratat. Fără asta, bara portocalie a apelurilor ar fi mereu 0 pe monitor.
    h_call_open = _hourly(f"""
        {P._APEL_AGENT_CTE}
        SELECT EXTRACT(hour FROM c.started_at)::int AS h, COUNT(*)
        FROM {_SRC_CALLS} c
        {P._APEL_AGENT_JOIN}
        WHERE c.direction = 'inbound'
          AND {P._APEL_UNANSWERED_SQL}
          AND {P._APEL_LOST_CALL_SQL}
          AND {P._APEL_DAY_SQL} = {P._APEL_TODAY_RO_SQL}
          AND edm.department = ANY(:depts)
        GROUP BY 1
    """)
    h_dev_open = _hourly(f"""
        SELECT EXTRACT(hour FROM d.finished_at AT TIME ZONE '{_TZ}')::int AS h, COUNT(*)
        FROM {_SRC_DEVOPS} d
        {_dep_dev}
        WHERE d.closed_at IS NULL AND d.finished_at IS NOT NULL
          AND DATE(d.finished_at AT TIME ZONE '{_TZ}') = CURRENT_DATE
        GROUP BY 1
    """)

    h_dev_new = _hourly(f"""
        SELECT EXTRACT(hour FROM d.finished_at AT TIME ZONE '{_TZ}')::int AS h, COUNT(*)
        FROM {_SRC_DEVOPS} d
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
    warnings = []
    if depts:
        def _by_dept(sql: str, ncols: int, label: str) -> dict:
            """Rulează o interogare grupată pe departament; la eroare întoarce dict gol.

            ⚠️ Dict-ul gol face TOATE cardurile să afișeze 0, iar un 0 e indistinguibil de un
            departament fără muncă. Monitorul e un ecran de perete — nimeni nu se uită în loguri
            când o cifră scade. De aceea eroarea se și RAPORTEAZĂ, în `warnings`, iar UI-ul o
            afișează ca bandă; `logger.exception` singur nu ajunge la nimeni.
            """
            try:
                rows = db.execute(text(sql), {"depts": depts}).fetchall()
                return {r[0]: tuple(int(r[i] or 0) for i in range(1, ncols + 1)) for r in rows}
            except Exception as e:
                logger.exception("monitor_live per_dept [%s]", label)
                warnings.append({"scope": label, "error": str(e)[:200]})
                return {}

        # Aceleași fragmente ca la contoarele de grup de mai sus — `_EFF_DEPT_EMAIL`,
        # `_EMAIL_OPEN_STATES` & co. Blocurile astea aveau, până pe 2026-09-22, expresia de
        # departament scrisă INLINE: așa a putut monitorul să atribuie pe coadă în timp ce tot
        # restul aplicației atribuia pe omul asignat. Nu le rescrie — invariantul „suma
        # cardurilor = totalul de grup" se ține doar atâta timp cât ambele citesc din același loc.
        d_mail = _by_dept(f"""
            SELECT {_EFF_DEPT_EMAIL} AS dept,
                   COUNT(*) FILTER (WHERE g.cts_status IN ('solved','closed')
                                    AND DATE(g.cts_solved_at AT TIME ZONE '{_TZ}') = CURRENT_DATE) AS rezolvate_azi,
                   COUNT(*) FILTER (WHERE {_EMAIL_OPEN_STATES} AND {_EMAIL_WIP}
                                    AND DATE({_EMAIL_ARRIVED_LOCAL}) = CURRENT_DATE)                AS in_lucru,
                   COUNT(*) FILTER (WHERE {_EMAIL_OPEN_STATES} AND NOT {_EMAIL_WIP}
                                    AND DATE({_EMAIL_ARRIVED_LOCAL}) = CURRENT_DATE)                AS noi,
                   COUNT(*) FILTER (WHERE {_EMAIL_ARRIVED_LOCAL} IS NOT NULL
                                    AND DATE({_EMAIL_ARRIVED_LOCAL}) = CURRENT_DATE) AS intrate_azi,
                   COUNT(*) FILTER (WHERE {_EMAIL_OPEN_STATES}
                                    AND {_EMAIL_BEFORE_TODAY})                       AS restanta
            FROM cts_ground_truth g
            {_DEP_EMAIL_JOIN}
            {_J_EMAIL_EXCL}
            WHERE g.cts_deleted_at IS NULL
              AND COALESCE(g.cts_direction,'received') = 'received'
              AND {_EFF_DEPT_EMAIL} = ANY(:depts)
              AND {_EMAIL_EXCLUDE_SQL}
            GROUP BY 1
        """, 5, "mailuri per departament")

        d_task = _by_dept(f"""
            SELECT {_EFF_DEPT_TASK} AS dept,
                   COUNT(*) FILTER (WHERE t.status IN ('solved','closed')
                                    AND DATE(t.cts_updated_at AT TIME ZONE '{_TZ}') = CURRENT_DATE) AS rezolvate_azi,
                   COUNT(*) FILTER (WHERE {_TASK_OPEN_STATES} AND {_TASK_WIP}
                                    AND DATE(t.cts_created_at AT TIME ZONE '{_TZ}') = CURRENT_DATE)   AS in_progress,
                   COUNT(*) FILTER (WHERE {_TASK_OPEN_STATES} AND NOT {_TASK_WIP}
                                    AND DATE(t.cts_created_at AT TIME ZONE '{_TZ}') = CURRENT_DATE)   AS noi,
                   COUNT(*) FILTER (WHERE DATE(t.cts_created_at AT TIME ZONE '{_TZ}') = CURRENT_DATE) AS intrate_azi,
                   COUNT(*) FILTER (WHERE {_TASK_OPEN_STATES} AND {_TASK_BEFORE_TODAY})     AS restanta
            FROM {_SRC_TASK} t
            {_DEP_TASK_JOIN}
            WHERE {_EFF_DEPT_TASK} = ANY(:depts)
            GROUP BY 1
        """, 5, "task-uri per departament")

        # Apeluri per departament — aceeași sursă (`calls`) și exact aceleași filtre ca `call_row`,
        # deci suma cardurilor = contorul de grup. Atribuirea e a agentului din centrală
        # (`_APEL_AGENT_JOIN`): un leg fără agent înregistrat nu se poate pune pe niciun
        # departament, deci nu apare nici în total, nici în carduri.
        d_call = _by_dept(f"""
            {P._APEL_AGENT_CTE}
            SELECT edm.department AS dept,
                   COUNT(*) FILTER (WHERE {P._APEL_REAL_CALL_SQL})                              AS azi,
                   COUNT(*) FILTER (WHERE {P._APEL_UNANSWERED_SQL} AND {P._APEL_LOST_CALL_SQL}) AS pierdute_azi
            FROM {_SRC_CALLS} c
            {P._APEL_AGENT_JOIN}
            WHERE c.direction = 'inbound'
              AND {P._APEL_DAY_SQL} = {P._APEL_TODAY_RO_SQL}
              AND edm.department = ANY(:depts)
            GROUP BY 1
        """, 2, "apeluri per departament")

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
                   COUNT(*) FILTER (WHERE deschisa)     AS deschise,
                   COUNT(*) FILTER (WHERE noua)         AS noi,
                   COUNT(*) FILTER (WHERE in_lucru)     AS in_lucru,
                   COUNT(*) FILTER (WHERE luna_curenta) AS total_luna
            FROM (
                SELECT COALESCE(ev.department, dep.department) AS dept,
                       (DATE(qe.created_at AT TIME ZONE '{_TZ}') = CURRENT_DATE) AS primit_azi,
                       (qe.status = 3
                        AND DATE(qe.solved_at AT TIME ZONE '{_TZ}') = CURRENT_DATE) AS rezolvat_azi,
                       (qe.status IS DISTINCT FROM 3)                               AS deschisa,
                       -- vezi nota de la interogarea de grup: fereastra e LUNA CURENTA,
                       -- aceeasi ca `total_luna`, altfel cardul compara mere cu pere.
                       (qe.status = 1 AND date_trunc('month', qe.created_at AT TIME ZONE '{_TZ}')
                                         = date_trunc('month', (NOW() AT TIME ZONE '{_TZ}')))  AS noua,
                       (qe.status = 2 AND date_trunc('month', qe.created_at AT TIME ZONE '{_TZ}')
                                         = date_trunc('month', (NOW() AT TIME ZONE '{_TZ}')))  AS in_lucru,
                       (date_trunc('month', qe.created_at AT TIME ZONE '{_TZ}')
                          = date_trunc('month', (NOW() AT TIME ZONE '{_TZ}')))       AS luna_curenta
                FROM cts_quality_evaluation qe
                -- Departamentul persoanei EVALUATE (`responsible_id`) — aceeasi regula ca pagina
                -- Reclamatii; vezi nota de la interogarea de grup.
                LEFT JOIN LATERAL (
                    SELECT e.department
                    FROM cts_dv_employee dv
                    JOIN employee_department_mapping e ON lower(e.email) = lower(dv.email)
                    WHERE dv.admin_id = qe.responsible_id::text
                    ORDER BY e.enabled DESC, e.id
                    LIMIT 1
                ) ev ON true
                -- Fallback: department_id (CTS) -> slug-ul nostru, dedus din angajatii mapati.
                -- LATERAL + LIMIT 1: `cts_dv_employee` are randuri multiple per persoana.
                LEFT JOIN LATERAL (
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
                  AND COALESCE(ev.department, dep.department) = ANY(:depts)
            ) s
            GROUP BY 1
        """, 6, "reclamații per departament")

        # `_z5` = zero-ul pentru mail/task (5 coloane, ultima e `restanta`).
        _z5, _z3, _z2 = (0, 0, 0, 0, 0), (0, 0, 0, 0, 0, 0), (0, 0)
        per_dept = [
            {
                "department": d,
                # chei păstrate pentru compatibilitate cu orice consumator existent
                "rezolvate_azi": d_mail.get(d, _z5)[0],
                "in_lucru":      d_mail.get(d, _z5)[1],
                "emailuri": {
                    "rezolvate_azi": d_mail.get(d, _z5)[0],
                    "in_lucru":      d_mail.get(d, _z5)[1],
                    "noi":           d_mail.get(d, _z5)[2],
                    "intrate_azi":   d_mail.get(d, _z5)[3],
                    "restanta":      d_mail.get(d, _z5)[4],
                },
                "taskuri": {
                    "rezolvate_azi": d_task.get(d, _z5)[0],
                    "in_progress":   d_task.get(d, _z5)[1],
                    "noi":           d_task.get(d, _z5)[2],
                    "intrate_azi":   d_task.get(d, _z5)[3],
                    "restanta":      d_task.get(d, _z5)[4],
                },
                "apeluri": {
                    "azi":           d_call.get(d, _z2)[0],
                    # In centrala un apel raspuns E incheiat: `rezolvate_azi` == `azi`, pastrat
                    # pentru consumatorii vechi. Noutatea utila e `pierdute_azi`.
                    "rezolvate_azi": d_call.get(d, _z2)[0],
                    "pierdute_azi":  d_call.get(d, _z2)[1],
                },
                "reclamatii": {
                    "primite_azi":   d_recl.get(d, _z3)[0],
                    "rezolvate_azi": d_recl.get(d, _z3)[1],
                    "deschise":      d_recl.get(d, _z3)[2],
                    "noi":           d_recl.get(d, _z3)[3],
                    "in_lucru":      d_recl.get(d, _z3)[4],
                    "total_luna":    d_recl.get(d, _z3)[5],
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
        # `restanta` = deschis acum, sosit ÎNAINTE de azi — bară separată, fără limită de vechime.
        "emailuri": {
            "rezolvate_azi": int(email_row[0] or 0),
            "in_lucru":      int(email_row[1] or 0),
            "noi":           int(email_row[2] or 0),
            "intrate_azi":   int(mail_in_azi or 0),
            "restanta":      int(email_row[3] or 0),
        },
        "taskuri": {
            "rezolvate_azi": int(task_row[0] or 0),
            "in_progress":   int(task_row[1] or 0),
            "pending":       int(task_row[2] or 0),
            "intrate_azi":   int(task_in_azi or 0),
            "restanta":      int(task_row[3] or 0),
        },
        "apeluri": {
            "azi":           int(call_row[0] or 0),
            # Un apel raspuns e incheiat in momentul in care apare in CDR, deci "rezolvate" e
            # acelasi set ca "azi" -- cheia rimine pentru compatibilitate.
            "rezolvate_azi": int(call_row[0] or 0),
            # Subsetul atribuibil pe departamentele grupului (agentul pe care a sunat centrala).
            "pierdute_azi":  int(call_row[1] or 0),
            # Toate apelurile pierdute ale firmei azi — cifra reala, neatribuibila pe departament
            # (vezi nota de la `lost_row`). Monitorul o arata o singura data, in cap.
            "pierdute_azi_total": int(lost_row[0] or 0),
            # `in_curs` nu exista in While1: un apel in desfasurare nu e inca in CDR. Ramine 0.
            "in_curs":       0,
        },
        "device_ops": {
            "rezolvate_azi": int(dev_row[0] or 0),
            "in_asteptare":  int(dev_row[1] or 0),
        },
        # RECLAMAȚII — bloc propriu, alimentat exclusiv din Quality Evaluation. Cifrele de aici
        # sunt suma cardurilor per departament (aceeași atribuire, pe persoana evaluată).
        "reclamatii": {
            "primite_azi":   int(recl_row[0] or 0),
            "rezolvate_azi": int(recl_row[1] or 0),
            "deschise":      int(recl_row[2] or 0),
            "restante":      int(recl_row[3] or 0),
            "peste_7z":      int(recl_row[4] or 0),
            "apel_azi":      int(recl_row[5] or 0),
            # Cele doua stari afisate pe monitor: nepreluate + in lucru (suma = `deschise`).
            "noi":           int(recl_row[6] or 0),
            "in_lucru":      int(recl_row[7] or 0),
            "total_luna":    int(recl_row[8] or 0),
        },
        # SESIZĂRI + RECLAMAȚII, cheile istorice. Partea de SESIZARE rămâne pe categoria
        # emailului (nu există altă sursă), partea de RECLAMAȚIE vine acum din Quality
        # Evaluation — de-aia câmpurile mixte se compun din două surse, nu dintr-o interogare.
        "sesizari": {
            "deschise":            int(sesiz_row[1] or 0) + int(recl_row[2] or 0),
            "sesizari_deschise":   int(sesiz_row[1] or 0),
            "reclamatii_deschise": int(recl_row[2] or 0),
            # emailurile de tip sesizare închise azi + reclamațiile CTS închise azi
            "rezolvate_azi":       int(sesiz_row[3] or 0) + int(recl_row[1] or 0),
            # deschise dinainte de azi (restanțe) și cele care depășesc 7 zile
            "restante":            int(sesiz_row[4] or 0) + int(recl_row[3] or 0),
            "peste_7z":            int(sesiz_row[5] or 0) + int(recl_row[4] or 0),
            # intrate azi pe telefon: sesizările din categoria apelului, reclamațiile din CTS
            # (Quality Evaluation înregistrează pe ce lucrare s-a reclamat — aici, un apel).
            "apel_sesizari_azi":   int(sesiz_call[0] or 0),
            "apel_reclamatii_azi": int(recl_row[5] or 0),
        },
        "rezolvate_categorii": rezolvate_categorii,
        "hourly": hourly,
        "per_dept": per_dept,
        # Interogările per departament care au eșuat. Gol în regim normal. Nevid = cifrele din
        # cardurile respective sunt 0 pentru că query-ul a crăpat, nu pentru că nu e muncă;
        # UI-ul afișează o bandă, ca zeroul să nu treacă drept realitate.
        "warnings": warnings,
    }


# ── INCHIDEREA RESTANTEI JUNK ────────────────────────────────────────────────────────────────
# Predicatele de vechime, o singura data, ca endpoint-ul si migratia
# (20260922_monitor_junk_close.sql) sa taie EXACT aceleasi randuri. `{op}` primeste `<` la
# inchidere. Data de sosire = aceeasi expresie ca in monitor (P._EMAIL_START_SQL), cu rezerva pe
# `fetched_at` / `first_synced_at` pentru randurile fara nicio data: predicatul de restanta e
# `IS DISTINCT FROM CURRENT_DATE`, deci un rand nedatat cade MEREU la restanta — exact profilul
# de junk, care altfel n-ar putea fi taiat niciodata.
_JUNK_MAIL_AGE_SQL = r"""COALESCE(
         CASE WHEN g.raw->'extra'->>'email_date' ~ '^\d{4}-\d\d-\d\d'
              THEN (g.raw->'extra'->>'email_date')::timestamp AT TIME ZONE 'UTC' END,
         (SELECT e2.received_at FROM emails e2 WHERE e2.id = g.email_id),
         g.fetched_at
       ) < CAST(:before AS timestamptz)"""
_JUNK_TASK_AGE_SQL = ("COALESCE(t.cts_created_at, t.first_synced_at) "
                      "< CAST(:before AS timestamptz)")


@router.post("/productivity/monitor/close-backlog")
def close_monitor_backlog(before: str = Query(..., description="YYYY-MM-DD — se inchide ce a intrat STRICT inainte"),
                          kind: str = Query("all", description="all | mail | task"),
                          dry_run: bool = Query(True),
                          reason: Optional[str] = Query(None, max_length=120),
                          db: Session = Depends(get_db), admin=Depends(require_prod_full)):
    """Marcheaza ca INCHISE PENTRU MONITOR randurile deschise intrate inainte de `before`.

    Junk-ul se reacumuleaza (CTS lasa tichete deschise la nesfarsit), deci taierea trebuie sa fie
    repetabila fara migratie noua. Refoloseste exact predicatele migratiei
    20260922_monitor_junk_close.sql.

    ⚠️ `dry_run=true` e IMPLICIT: intoarce cate randuri s-ar inchide, fara sa scrie. O taiere prea
    larga se anuleaza cu DELETE pe acelasi endpoint, dupa `reason` — de aceea `reason` se
    persista pe fiecare rand si e obligatoriu sa fie distinctiv.

    ⛔ NU scrie in `cts_status` / `status`: acelea sunt oglinda CTS si upsert-ul de sync le
    suprascrie la fiecare rulare. Vezi nota din capul migratiei.
    """
    kind = (kind or "all").strip().lower()
    if kind not in ("all", "mail", "task"):
        raise HTTPException(status_code=400, detail="kind trebuie sa fie 'all', 'mail' sau 'task'.")
    try:
        d = _dt.date.fromisoformat((before or "").strip())
    except Exception:
        raise HTTPException(status_code=400, detail="Parametrul 'before' invalid (format YYYY-MM-DD).")
    if d > _dt.date.today():
        raise HTTPException(status_code=400, detail="'before' nu poate fi in viitor.")
    tag = (reason or f"junk_cutoff_{d.isoformat()}").strip()
    # Miezul noptii LOCAL, nu UTC: „inainte de 1 septembrie" inseamna ora Bucurestiului, altfel
    # ultimele 3 ore ale zilei de 31 august ar scapa de taiere (sau ar fi taiate in plus).
    p = {"before": f"{d.isoformat()} 00:00:00+03", "tag": tag}

    _MAIL_WHERE = f"""
        WHERE g.monitor_closed_at IS NULL
          AND g.cts_deleted_at IS NULL
          AND lower(btrim(COALESCE(g.cts_status,''))) NOT IN ('solved','closed')
          AND {_JUNK_MAIL_AGE_SQL}
    """
    _TASK_WHERE = f"""
        WHERE t.monitor_closed_at IS NULL
          AND lower(btrim(COALESCE(t.status,''))) NOT IN ('solved','closed')
          AND {_JUNK_TASK_AGE_SQL}
    """
    out = {"before": d.isoformat(), "kind": kind, "dry_run": bool(dry_run), "reason": tag}
    try:
        if kind in ("all", "mail"):
            if dry_run:
                n = db.execute(text(f"SELECT count(*) FROM cts_ground_truth g {_MAIL_WHERE}"), p).scalar()
                out["mail"] = {"ar_inchide": int(n or 0)}
            else:
                r = db.execute(text(
                    f"UPDATE cts_ground_truth g SET monitor_closed_at = now(), "
                    f"monitor_closed_reason = :tag {_MAIL_WHERE}"), p)
                out["mail"] = {"inchise": int(r.rowcount or 0)}
        if kind in ("all", "task"):
            if dry_run:
                n = db.execute(text(f"SELECT count(*) FROM cts_task_ground_truth t {_TASK_WHERE}"), p).scalar()
                out["task"] = {"ar_inchide": int(n or 0)}
            else:
                r = db.execute(text(
                    f"UPDATE cts_task_ground_truth t SET monitor_closed_at = now(), "
                    f"monitor_closed_reason = :tag {_TASK_WHERE}"), p)
                out["task"] = {"inchise": int(r.rowcount or 0)}
        if not dry_run:
            db.commit()
    except Exception as e:
        db.rollback()
        logger.exception("close_monitor_backlog")
        raise HTTPException(status_code=500, detail="Eroare la inchiderea restantei: %s" % e)
    return out


@router.delete("/productivity/monitor/close-backlog")
def reopen_monitor_backlog(reason: str = Query(..., max_length=120),
                           db: Session = Depends(get_db), admin=Depends(require_prod_full)):
    """Anuleaza o taiere, dupa eticheta ei. Plasa de siguranta daca `before` a fost prea larg.

    Redeschide DOAR randurile marcate cu acel `reason` — o taiere ulterioara, cu alta eticheta,
    ramane intacta. Randurile redevin vizibile in restanta imediat, la urmatorul poll de 15s.
    """
    tag = (reason or "").strip()
    if not tag:
        raise HTTPException(status_code=400, detail="Parametrul 'reason' e obligatoriu.")
    try:
        m = db.execute(text("UPDATE cts_ground_truth SET monitor_closed_at = NULL, "
                            "monitor_closed_reason = NULL WHERE monitor_closed_reason = :tag"),
                       {"tag": tag})
        k = db.execute(text("UPDATE cts_task_ground_truth SET monitor_closed_at = NULL, "
                            "monitor_closed_reason = NULL WHERE monitor_closed_reason = :tag"),
                       {"tag": tag})
        db.commit()
    except Exception as e:
        db.rollback()
        logger.exception("reopen_monitor_backlog")
        raise HTTPException(status_code=500, detail="Eroare la redeschidere: %s" % e)
    return {"reason": tag, "mail": {"redeschise": int(m.rowcount or 0)},
            "task": {"redeschise": int(k.rowcount or 0)}}


@router.get("/productivity/monitor/attribution-audit")
def get_attribution_audit(group: str = Query("operational"),
                          db: Session = Depends(get_db), admin=Depends(require_prod_full)):
    """Restanta deschisa, numarata pe AMBELE reguli de atribuire, plus ce cade in afara grupului.

    Exista ca sa nu se mai poata intampla ce s-a intamplat pe 2026-09-22: o regula de atribuire
    divergenta, timp de saptamani, fara ca nimeni sa poata vedea cifra cu cifra ce se muta unde.
    Nu e un instrument temporar de migrare — e contra-proba permanenta a regulii din
    `productivity._LIVE_DEPT_EMAIL_SQL`.

    Matricea `coada x efectiv`: diagonala = randurile pe care ambele reguli le pun la fel;
    restul = exact randurile pe care vechea regula le atribuia gresit. `in_afara_grupului` sunt
    randurile al caror departament efectiv nu e in `productivity_department_config` (mobilitate,
    comercial, it...): corecte semantic, dar invizibile pe ORICE monitor — de urmarit, ca sa nu
    devina o a doua gaura tacuta.
    """
    depts = _dashboard_depts(db, group)
    out = {"group": group, "departamente_configurate": depts}

    def _matrix(sql: str, label: str) -> list:
        try:
            return [{"dept_coada": r[0], "dept_efectiv": r[1], "n": int(r[2] or 0)}
                    for r in db.execute(text(sql)).fetchall()]
        except Exception as e:
            logger.exception("attribution_audit [%s]", label)
            out.setdefault("warnings", []).append({"scope": label, "error": str(e)[:200]})
            return []

    out["mail"] = _matrix(f"""
        SELECT COALESCE(g.cts_department, '(fara coada)')  AS dept_coada,
               COALESCE({_EFF_DEPT_EMAIL}, '(neatribuit)') AS dept_efectiv,
               COUNT(*) AS n
        FROM cts_ground_truth g
        {_DEP_EMAIL_JOIN}
        {_J_EMAIL_EXCL}
        WHERE g.cts_deleted_at IS NULL
          AND COALESCE(g.cts_direction,'received') = 'received'
          AND {_EMAIL_EXCLUDE_SQL}
          AND {_EMAIL_OPEN_STATES} AND {_EMAIL_BEFORE_TODAY}
        GROUP BY 1, 2
        ORDER BY 3 DESC
    """, "mail")

    out["task"] = _matrix(f"""
        SELECT COALESCE(t.department, '(fara coada)')     AS dept_coada,
               COALESCE({_EFF_DEPT_TASK}, '(neatribuit)') AS dept_efectiv,
               COUNT(*) AS n
        FROM {_SRC_TASK} t
        {_DEP_TASK_JOIN}
        WHERE {_TASK_OPEN_STATES} AND {_TASK_BEFORE_TODAY}
        GROUP BY 1, 2
        ORDER BY 3 DESC
    """, "task")

    # Sumarele care raspund direct la intrebarea „cat s-a mutat si unde".
    _known = set(depts)
    for kind in ("mail", "task"):
        rows = out.get(kind) or []
        out[kind + "_sumar"] = {
            "total": sum(r["n"] for r in rows),
            "pe_coada": _sum_by(rows, "dept_coada"),
            "pe_efectiv": _sum_by(rows, "dept_efectiv"),
            # Randurile pe care cele doua reguli le pun in departamente DIFERITE.
            "mutate": sum(r["n"] for r in rows if r["dept_coada"] != r["dept_efectiv"]),
            "in_afara_grupului": _sum_by(
                [r for r in rows if r["dept_efectiv"] not in _known], "dept_efectiv"),
        }
    return out


def _sum_by(rows: list, key: str) -> dict:
    """Agregă rândurile matricei pe una dintre axe. Sortat descrescător, ca să se citească."""
    acc = {}
    for r in rows:
        acc[r[key]] = acc.get(r[key], 0) + r["n"]
    return dict(sorted(acc.items(), key=lambda kv: -kv[1]))


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
