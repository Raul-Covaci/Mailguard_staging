"""v10.20.0 — Angajați (Utilizatori → Angajați): router separat de Settings.

Endpoint-urile /settings/employees* alimentează pagina Utilizatori, nu zona de
Setări. Cât timp stăteau în settings.py erau păzite de require_module("settings")
— modul rezervat developerilor — deci un admin primea 403 pe propria pagină și
UI-ul rămânea alb. Router propriu, montat cu require_module("utilizatori"):
admin + developer au acces, operatorul nu.

Căile rămân neschimbate (/api/v1/settings/employees...) ca să nu rupem UI-ul.
"""
import datetime as _dt
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.api.v1.auth import get_current_admin
from app.database import get_db

logger = logging.getLogger("mailguard.employees")

router = APIRouter()


def _audit_dept_history(db: Session, actor: str, emp_id: int, action: str, details: dict) -> None:
    """Audit pentru editările de istoric departament — nu are voie să doboare operația."""
    try:
        db.execute(text(
            "INSERT INTO audit_log (actor, action, entity_type, entity_id, details, created_at) "
            "VALUES (:a, :ac, 'employee', :eid, CAST(:d AS jsonb), NOW())"
        ), {"a": actor, "ac": f"employee_dept_history_{action}", "eid": emp_id,
            "d": json.dumps(details, default=str)})
    except Exception as exc:  # pragma: no cover
        logger.warning("audit dept history esuat (emp %s): %s", emp_id, exc)


# ── Angajați CargoTrack → mapping departament pentru employee signature matching ──
_VALID_DEPARTMENTS = {
    "suport_1", "suport_2", "suport_3",
    "taxe_drum", "contabilitate", "mobilitate",
    "recuperare_tva", "comercial",
}


@router.get("/settings/employees")
def get_employees(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Lista angajaților CargoTrack folosiți pentru employee signature matching.

    Include câmpurile de sincronizare IRIS (OPS-2026-0132): email/status/shift/sync_source/
    last_synced_at + numărul de intrări de program (concedii/leave) per angajat.
    """
    rows = db.execute(text(
        "SELECT e.id, e.name, e.department, e.enabled, e.email, e.status, e.shift, "
        "       e.work_hours, e.break_minutes, e.sync_source, e.iris_id, e.last_synced_at, "
        "       e.productivity_start_date, "
        "       (SELECT count(*) FROM employee_schedule s WHERE s.employee_id=e.id "
        "          AND s.kind!='planned_leave' AND s.entry_source<>'manual_extra') AS schedule_count, "
        "       (SELECT count(*) FROM employee_schedule s WHERE s.employee_id=e.id AND s.kind='vacation_approved') AS planned_count, "
        "       (SELECT count(*) FROM employee_schedule s WHERE s.employee_id=e.id "
        "          AND s.kind!='planned_leave' AND s.entry_source<>'manual_extra') AS leave_count, "
        "       (SELECT count(*) FROM employee_department_history h WHERE h.employee_id=e.id) AS dept_history_count "
        "FROM employee_department_mapping e ORDER BY e.name"
    )).fetchall()
    return [dict(r._mapping) for r in rows]


@router.get("/settings/employees/sync-status")
def employees_sync_status(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Starea sincronizării IRIS: pornit/oprit, ultima rulare, ultimul rezultat, surse."""
    def _val(k, d=None):
        r = db.execute(text("SELECT value FROM settings WHERE key=:k"), {"k": k}).fetchone()
        return r._mapping["value"] if r and r._mapping["value"] is not None else d
    counts = db.execute(text(
        "SELECT sync_source, count(*) AS n, count(*) FILTER (WHERE enabled) AS n_enabled "
        "FROM employee_department_mapping GROUP BY sync_source"
    )).fetchall()
    return {
        "enabled": bool(_val("employee_sync.enabled", False)),
        "endpoint_path": _val("employee_sync.endpoint_path"),
        "last_sync_at": _val("employee_sync.last_sync_at"),
        "last_result": _val("employee_sync.last_result"),
        "by_source": [dict(c._mapping) for c in counts],
    }


@router.post("/settings/employees/sync")
def employees_sync_now(body: dict = None, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Declanșează manual sincronizarea din IRIS (OPS-2026-0132).

    INERT până la grant (outbox #11): dacă employee_sync.enabled=false sau gateway-ul IRIS
    nu e configurat, întoarce {ok:false, skipped:...} fără să modifice datele.
    dry_run=true → doar numără, nu scrie.
    """
    from app.services import iris_employee_sync
    dry = bool((body or {}).get("dry_run"))
    try:
        return iris_employee_sync.sync_employees(db, dry_run=dry)
    except Exception as e:
        raise HTTPException(500, f"Sync eșuat: {e}")


@router.get("/settings/employees/{emp_id}/schedule")
def employee_schedule(emp_id: int, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Concedii reale (vacation_approved din DV + intrari manuale) pentru un angajat.

    Exclus:
      - planned_leave — era planificare anuala estimata, nu cereri reale
      - entry_source='manual_extra' (project_work / refurbished) — zilele de lucru pe proiecte au
        propria secțiune in UI, cu luna+numar de zile, si NU au start_date/end_date; aparute aici
        se afisau ca "— – —" intr-un tabel de concedii.
    """
    rows = db.execute(text(
        "SELECT id, kind, leave_type, start_date, end_date, status, days, entry_source "
        "FROM employee_schedule "
        "WHERE employee_id=:id "
        "  AND kind != 'planned_leave' "
        "  AND entry_source <> 'manual_extra' "
        "ORDER BY start_date NULLS LAST, id"
    ), {"id": emp_id}).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/settings/employees/{emp_id}/schedule")
def add_employee_leave(emp_id: int, body: dict, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Adaugă un concediu manual pentru un angajat."""
    emp = db.execute(text("SELECT id FROM employee_department_mapping WHERE id=:id"), {"id": emp_id}).fetchone()
    if not emp:
        raise HTTPException(404, "Angajat negăsit")
    start = body.get("start_date")
    end = body.get("end_date")
    kind = body.get("kind", "leave_request")
    status = body.get("status", "approved")
    days = body.get("days")
    if not start or not end:
        raise HTTPException(400, "start_date și end_date sunt obligatorii")
    if kind not in ("leave_request", "planned_leave"):
        raise HTTPException(400, "kind invalid")
    if status not in ("approved", "pending", "other", None):
        raise HTTPException(400, "status invalid")
    if start > end:
        raise HTTPException(400, "start_date trebuie să fie <= end_date")
    existing = db.execute(text(
        "SELECT id FROM employee_schedule WHERE employee_id=:eid AND entry_source='manual' "
        "AND kind=:k AND start_date=:s AND end_date=:e"
    ), {"eid": emp_id, "k": kind, "s": start, "e": end}).fetchone()
    if existing:
        raise HTTPException(409, "Există deja un concediu manual pentru același interval")
    # employee_schedule_uidx e pe (employee_id, kind, leave_type, start_date, end_date) — FARA
    # entry_source. Deci un rand CTS cu acelasi kind+interval coliziona aici cu 500; verificarea
    # de mai sus vede doar intrarile manuale.
    try:
        row = db.execute(text(
            "INSERT INTO employee_schedule (employee_id, kind, start_date, end_date, status, days, raw, entry_source) "
            "VALUES (:eid, :k, CAST(:s AS date), CAST(:e AS date), :st, :d, '{}', 'manual') RETURNING id"
        ), {"eid": emp_id, "k": kind, "s": start, "e": end, "st": status, "d": days}).fetchone()
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Există deja o intrare pentru același interval (posibil importată din CTS)")
    return {"id": row[0], "employee_id": emp_id, "kind": kind, "start_date": start, "end_date": end,
            "status": status, "days": days, "entry_source": "manual"}


@router.put("/settings/employees/{emp_id}/schedule/{sid}")
def update_employee_leave(emp_id: int, sid: int, body: dict, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Editează un concediu manual (entry_source='manual' — intrările CTS nu pot fi editate)."""
    row = db.execute(text(
        "SELECT id FROM employee_schedule WHERE id=:sid AND employee_id=:eid AND entry_source='manual'"
    ), {"sid": sid, "eid": emp_id}).fetchone()
    if not row:
        raise HTTPException(404, "Concediu negăsit sau este o intrare CTS (read-only)")
    start = body.get("start_date")
    end = body.get("end_date")
    kind = body.get("kind", "leave_request")
    status = body.get("status", "approved")
    days = body.get("days")
    if not start or not end:
        raise HTTPException(400, "start_date și end_date sunt obligatorii")
    if start > end:
        raise HTTPException(400, "start_date trebuie să fie <= end_date")
    try:
        db.execute(text(
            "UPDATE employee_schedule SET kind=:k, start_date=CAST(:s AS date), end_date=CAST(:e AS date), "
            "status=:st, days=:d WHERE id=:sid"
        ), {"k": kind, "s": start, "e": end, "st": status, "d": days, "sid": sid})
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Există deja o intrare pentru același interval (posibil importată din CTS)")
    return {"id": sid, "employee_id": emp_id, "kind": kind, "start_date": start, "end_date": end,
            "status": status, "days": days, "entry_source": "manual"}


@router.delete("/settings/employees/{emp_id}/schedule/{sid}")
def delete_employee_leave(emp_id: int, sid: int, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Șterge un concediu manual (entry_source='manual' — intrările CTS nu pot fi șterse)."""
    row = db.execute(text(
        "SELECT id FROM employee_schedule WHERE id=:sid AND employee_id=:eid AND entry_source='manual'"
    ), {"sid": sid, "eid": emp_id}).fetchone()
    if not row:
        raise HTTPException(404, "Concediu negăsit sau este o intrare CTS (read-only)")
    db.execute(text("DELETE FROM employee_schedule WHERE id=:sid"), {"sid": sid})
    db.commit()
    return {"ok": True}


_EXTRA_KINDS = ("project_work", "refurbished")


def _extra_working_days(db: Session, year: int, month: int) -> int:
    """Zile lucratoare L-V (minus sarbatori legale) in luna — cap pentru days_count.
    Refoloseste implementarea din serviciul de productivitate, ca sa nu divergem."""
    from app.services.productivity import get_holidays, working_days
    return working_days(year, month, get_holidays(db))


def _extra_month_is_locked(year: int, month: int) -> bool:
    """True daca luna vizata a inceput deja (sau e trecuta).

    Zilele extra intra in calculul de productivitate doar daca sunt inregistrate INAINTE
    de prima zi a lunii vizate — identic cu regula concediilor. Dupa ce luna a inceput,
    snapshot-ul lunar e (sau va fi) fixat la prima zi lucratoare si targetele nu se mai
    ajusteaza; permiterea adaugarii ar crea intrari fara efect, deci le refuzam explicit.
    """
    today = datetime.now(timezone.utc).date()
    return (year, month) <= (today.year, today.month)


@router.get("/settings/employees/{emp_id}/extra-days")
def employee_extra_days(emp_id: int, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Zile libere extra (lucru pe proiecte / refurbished) pentru un angajat."""
    rows = db.execute(text(
        "SELECT id, kind, days_count, period_year, period_month, created_at, entry_source "
        "FROM employee_schedule "
        "WHERE employee_id=:id AND kind = ANY(:kinds) "
        "ORDER BY period_year DESC, period_month DESC, kind"
    ), {"id": emp_id, "kinds": list(_EXTRA_KINDS)}).fetchall()
    out = []
    for r in rows:
        d = dict(r._mapping)
        d["locked"] = _extra_month_is_locked(d["period_year"], d["period_month"])
        out.append(d)
    return out


@router.post("/settings/employees/{emp_id}/extra-days")
def add_employee_extra_days(emp_id: int, body: dict, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Adaugă zile libere extra (lucru pe proiecte / refurbished) pentru o lună viitoare.

    Imutabil dupa creare — nu exista endpoint de UPDATE. Corectie = DELETE + POST nou,
    posibil doar cat timp luna nu a inceput.
    """
    emp = db.execute(text("SELECT id FROM employee_department_mapping WHERE id=:id"), {"id": emp_id}).fetchone()
    if not emp:
        raise HTTPException(404, "Angajat negăsit")
    kind = body.get("kind")
    if kind not in _EXTRA_KINDS:
        raise HTTPException(400, "kind invalid — acceptat: project_work, refurbished")
    try:
        days = int(body.get("days_count"))
        year = int(body.get("period_year"))
        month = int(body.get("period_month"))
    except (TypeError, ValueError):
        raise HTTPException(400, "days_count, period_year și period_month sunt obligatorii (numere)")
    if month < 1 or month > 12:
        raise HTTPException(400, "period_month invalid")
    if _extra_month_is_locked(year, month):
        raise HTTPException(
            400,
            "Luna a început deja — zilele extra se pot adăuga doar înainte de începutul lunii "
            "vizate (altfel nu ar intra în calculul de productivitate)."
        )
    # Cap: nu poate depasi zilele lucratoare ale lunii (aceeasi regula ca la concedii)
    zile_lucratoare = _extra_working_days(db, year, month)
    if days < 1 or days > zile_lucratoare:
        raise HTTPException(400, f"days_count trebuie între 1 și {zile_lucratoare} (zile lucrătoare în lună)")
    existing = db.execute(text(
        "SELECT id FROM employee_schedule WHERE employee_id=:eid AND kind=:k "
        "AND period_year=:y AND period_month=:m"
    ), {"eid": emp_id, "k": kind, "y": year, "m": month}).fetchone()
    if existing:
        raise HTTPException(409, "Există deja o intrare de acest tip pentru luna respectivă — șterge-o întâi")
    row = db.execute(text(
        "INSERT INTO employee_schedule "
        "(employee_id, kind, leave_type, days_count, period_year, period_month, status, raw, entry_source) "
        "VALUES (:eid, :k, :lt, :d, :y, :m, 'approved', '{}', 'manual_extra') RETURNING id, created_at"
    ), {"eid": emp_id, "k": kind, "lt": f"{year:04d}-{month:02d}", "d": days, "y": year, "m": month}).fetchone()
    db.commit()
    return {"id": row[0], "employee_id": emp_id, "kind": kind, "days_count": days,
            "period_year": year, "period_month": month, "created_at": row[1],
            "entry_source": "manual_extra", "locked": False}


@router.delete("/settings/employees/{emp_id}/extra-days/{sid}")
def delete_employee_extra_days(emp_id: int, sid: int, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Șterge o intrare de zile extra. Blocat dupa ce luna a inceput — la acel moment
    valoarea e deja fixata in snapshot si trebuie sa rezulte trasabil ce a intrat in target."""
    row = db.execute(text(
        "SELECT period_year, period_month FROM employee_schedule "
        "WHERE id=:sid AND employee_id=:eid AND kind = ANY(:kinds)"
    ), {"sid": sid, "eid": emp_id, "kinds": list(_EXTRA_KINDS)}).fetchone()
    if not row:
        raise HTTPException(404, "Intrare negăsită")
    if _extra_month_is_locked(row[0], row[1]):
        raise HTTPException(400, "Luna a început deja — intrarea e fixată în snapshot și nu poate fi ștearsă")
    db.execute(text("DELETE FROM employee_schedule WHERE id=:sid"), {"sid": sid})
    db.commit()
    return {"ok": True}


@router.get("/settings/employees/schedule")
def all_employees_schedule(kind: str = None, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Vedere globala: toate concediile (planned_leave) si invoirile orare (leave_request)
    ale tuturor angajatilor, cu nume + departament. Filtru optional pe kind."""
    q = ("SELECT s.id, s.employee_id, e.name, e.department, s.kind, "
         "       s.start_date, s.end_date, s.status, s.days "
         "FROM employee_schedule s "
         "JOIN employee_department_mapping e ON e.id = s.employee_id ")
    params = {}
    if kind in ("planned_leave", "leave_request"):
        q += "WHERE s.kind = :k "
        params["k"] = kind
    q += "ORDER BY s.start_date DESC NULLS LAST, e.name"
    rows = db.execute(text(q), params).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/settings/employees")
def add_employee(body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Adaugă un angajat în lista de employee signature matching."""
    name = (body.get("name") or "").strip()
    dept = (body.get("department") or "").strip()
    if not name:
        raise HTTPException(400, "name este obligatoriu")
    if dept not in _VALID_DEPARTMENTS:
        raise HTTPException(400, f"department invalid. Valori acceptate: {sorted(_VALID_DEPARTMENTS)}")
    created_by = admin.get("username") or admin.get("email") or "admin"
    row = db.execute(text(
        "INSERT INTO employee_department_mapping (name, department, created_by) "
        "VALUES (:name, :dept, :by) RETURNING id"
    ), {"name": name, "dept": dept, "by": created_by}).fetchone()
    db.commit()
    return {"id": row._mapping["id"], "name": name, "department": dept, "enabled": True}


@router.put("/settings/employees/{emp_id}")
def update_employee(emp_id: int, body: dict, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Editează un angajat (name, department, enabled)."""
    existing = db.execute(text(
        "SELECT id FROM employee_department_mapping WHERE id=:id"
    ), {"id": emp_id}).fetchone()
    if not existing:
        raise HTTPException(404, "Angajat negăsit")
    set_parts, params = [], {"id": emp_id}
    if "name" in body and (body["name"] or "").strip():
        set_parts.append("name=:name")
        params["name"] = body["name"].strip()
    if "department" in body:
        if body["department"] not in _VALID_DEPARTMENTS:
            raise HTTPException(400, f"department invalid. Valori acceptate: {sorted(_VALID_DEPARTMENTS)}")
        set_parts.append("department=:dept")
        params["dept"] = body["department"]
    if "enabled" in body:
        set_parts.append("enabled=:enabled")
        params["enabled"] = bool(body["enabled"])
    if "shift" in body:
        # 'shift' e mereu manual (IRIS trimite null); editabil inclusiv pe randurile IRIS.
        sh = (body.get("shift") or "").strip()
        set_parts.append("shift=:shift")
        params["shift"] = sh or None
    if "productivity_start_date" in body:
        import datetime as _dt2
        val = body.get("productivity_start_date")
        if val:
            try:
                parsed = _dt2.date.fromisoformat(str(val).strip())
            except Exception:
                raise HTTPException(400, "productivity_start_date invalid (format YYYY-MM-DD).")
            set_parts.append("productivity_start_date=:psd")
            params["psd"] = parsed
        else:
            set_parts.append("productivity_start_date=NULL")
    if not set_parts:
        raise HTTPException(400, "Nimic de actualizat")
    set_parts.append("updated_at=NOW()")
    db.execute(text("UPDATE employee_department_mapping SET " + ", ".join(set_parts) + " WHERE id=:id"), params)
    db.commit()
    return {"ok": True}


@router.delete("/settings/employees/{emp_id}")
def delete_employee(emp_id: int, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Șterge un angajat din lista de employee signature matching.

    Refuzat dacă are istoric de departament cu mai mult de un interval: ștergerea l-ar duce cu ea
    (ON DELETE CASCADE) și lunile lui vechi ar dispărea din rapoarte. Pentru cineva plecat din
    firmă se folosește `enabled=false` — intervalul se închide, istoricul rămâne.
    """
    hist = db.execute(text(
        "SELECT count(*) FROM employee_department_history WHERE employee_id=:id"
    ), {"id": emp_id}).fetchone()
    if hist and int(hist[0] or 0) > 1:
        raise HTTPException(409, "Angajatul are istoric de departament — dezactivează-l "
                                 "(enabled=false) în loc să-l ștergi, altfel se pierd lunile vechi.")
    result = db.execute(text(
        "DELETE FROM employee_department_mapping WHERE id=:id"
    ), {"id": emp_id})
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(404, "Angajat negăsit")
    return {"ok": True}


# ── Istoric departament (employee_department_history) ────────────────────────────────────────
#
# Apartenența la departament e efectiv-datată, cu granularitate LUNĂ: intervalul `[valid_from,
# valid_to)` începe pe ziua 1 și se termină pe ziua 1 a primei luni în afară. Rapoartele de
# productivitate citesc DE AICI, nu din `employee_department_mapping.department` — vezi
# `productivity._DEPT_AT_SQL`. Scalarul rămâne adevărul pentru „acum"; trigger-ul îl oglindește.
#
# API-ul face editări de LANȚ, nu inserări libere: un gol între două intervale ar face angajatul
# să dispară din toate departamentele în lunile respective, fără nicio eroare.


def _month_start(val, field: str):
    """'YYYY-MM' sau 'YYYY-MM-DD' -> prima zi a lunii. None/'' -> None."""
    if val is None or str(val).strip() == "":
        return None
    s = str(val).strip()
    try:
        if len(s) == 7:
            return _dt.date(int(s[:4]), int(s[5:7]), 1)
        return _dt.date.fromisoformat(s).replace(day=1)
    except Exception:
        raise HTTPException(400, f"{field} invalid (format YYYY-MM).")


def _refresh_open_snapshots(db: Session, departments, since: _dt.date) -> list:
    """Reseteaza snapshot-urile de ORE/OBIECTIVE ale lunilor inca deschise, afectate de editare.

    Componenta departamentului schimba orele planificate/disponibile, deci si tinta. Lunile
    ÎNCHISE raman fixate — asta e chiar rostul lui `productivity_monthly_snapshot`: o tinta deja
    comunicata oamenilor nu se rescrie retroactiv. Se intorc ca avertismente, sa le vada adminul.
    """
    from app.services import productivity as P
    today = _dt.date.today()
    warnings = []
    y, m = since.year, since.month
    while (y, m) <= (today.year, today.month):
        for dep in departments:
            if (y, m) >= (today.year, today.month):
                P.reset_snapshot(db, dep, y, m)
            else:
                row = db.execute(text(
                    "SELECT snapshot_at FROM productivity_monthly_snapshot "
                    "WHERE department=:d AND year=:y AND month=:m"
                ), {"d": dep, "y": y, "m": m}).fetchone()
                if row:
                    warnings.append({"department": dep, "month": f"{y:04d}-{m:02d}",
                                     "snapshot_at": str(row[0]),
                                     "reason": "obiectiv fixat — se recalculeaza doar volumul"})
        m += 1
        if m == 13:
            m, y = 1, y + 1
    return warnings


def _sync_current_department(db: Session, emp_id: int) -> None:
    """Ține `employee_department_mapping.department` în oglindă cu intervalul deschis.

    Trigger-ul nu se declanșează la asta: vede aceeași valoare pe intervalul deschis și nu scrie.
    """
    row = db.execute(text(
        "SELECT department FROM employee_department_history "
        "WHERE employee_id=:id AND valid_to IS NULL ORDER BY valid_from DESC LIMIT 1"
    ), {"id": emp_id}).fetchone()
    if row:
        db.execute(text("UPDATE employee_department_mapping SET department=:d, updated_at=NOW() "
                        "WHERE id=:id AND department <> :d"), {"d": row[0], "id": emp_id})


@router.get("/settings/employees/{emp_id}/department-history")
def employee_department_history(emp_id: int, db: Session = Depends(get_db),
                                _admin=Depends(get_current_admin)):
    """Intervalele de apartenență la departament, cronologic."""
    rows = db.execute(text(
        "SELECT id, department, valid_from, valid_to, source, note, created_at, created_by "
        "FROM employee_department_history WHERE employee_id=:id ORDER BY valid_from"
    ), {"id": emp_id}).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/settings/employees/{emp_id}/department-history")
def add_employee_department(emp_id: int, body: dict, db: Session = Depends(get_db),
                            admin=Depends(get_current_admin)):
    """Mută angajatul în alt departament începând cu o lună.

    Editare de LANȚ: intervalul care acoperă luna se taie la `valid_from`, tot ce urmează după e
    înlocuit, iar noul interval rămâne deschis. Așa nu pot apărea nici goluri, nici suprapuneri.
    """
    emp = db.execute(text("SELECT id, enabled FROM employee_department_mapping WHERE id=:id"),
                     {"id": emp_id}).fetchone()
    if not emp:
        raise HTTPException(404, "Angajat negăsit")
    dept = (body.get("department") or "").strip()
    if dept not in _VALID_DEPARTMENTS:
        raise HTTPException(400, f"department invalid. Valori acceptate: {sorted(_VALID_DEPARTMENTS)}")
    start = _month_start(body.get("valid_from"), "valid_from")
    if start is None:
        raise HTTPException(400, "valid_from este obligatoriu (YYYY-MM).")
    note = (body.get("note") or "").strip() or None
    created_by = admin.get("username") or admin.get("email") or "admin"

    prev = db.execute(text(
        "SELECT id, department, valid_from, valid_to FROM employee_department_history "
        "WHERE employee_id=:id AND valid_from < CAST(:s AS date) ORDER BY valid_from DESC LIMIT 1"
    ), {"id": emp_id, "s": start}).fetchone()
    if prev is not None and prev[1] == dept and (prev[3] is None or prev[3] >= start):
        raise HTTPException(409, f"Angajatul era deja în {dept} în luna respectivă.")

    # Tot ce începe la sau după luna țintă e înlocuit de intrarea nouă.
    db.execute(text("DELETE FROM employee_department_history "
                    "WHERE employee_id=:id AND valid_from >= CAST(:s AS date)"),
               {"id": emp_id, "s": start})
    if prev is not None:
        db.execute(text("UPDATE employee_department_history SET valid_to = CAST(:s AS date) "
                        "WHERE id=:hid"), {"s": start, "hid": prev[0]})
    row = db.execute(text(
        "INSERT INTO employee_department_history "
        "(employee_id, department, valid_from, valid_to, source, note, created_by) "
        "VALUES (:id, :d, CAST(:s AS date), NULL, 'manual', :n, :by) RETURNING id"
    ), {"id": emp_id, "d": dept, "s": start, "n": note, "by": created_by}).fetchone()
    _sync_current_department(db, emp_id)
    _audit_dept_history(db, created_by, emp_id, "add",
                        {"department": dept, "valid_from": str(start), "note": note})
    db.commit()
    depts = {dept} | ({prev[1]} if prev is not None else set())
    warnings = _refresh_open_snapshots(db, depts, start)
    return {"id": row[0], "employee_id": emp_id, "department": dept,
            "valid_from": str(start), "valid_to": None, "source": "manual",
            "warnings": warnings}


@router.put("/settings/employees/{emp_id}/department-history/{hid}")
def update_employee_department(emp_id: int, hid: int, body: dict, db: Session = Depends(get_db),
                               admin=Depends(get_current_admin)):
    """Corectează un interval: departamentul și/sau luna de început.

    Luna de început mișcă și capătul intervalului precedent, ca lanțul să rămână continuu.
    """
    cur = db.execute(text(
        "SELECT id, department, valid_from, valid_to FROM employee_department_history "
        "WHERE id=:hid AND employee_id=:id"
    ), {"hid": hid, "id": emp_id}).fetchone()
    if not cur:
        raise HTTPException(404, "Interval negăsit")
    dept = (body.get("department") or cur[1]).strip()
    if dept not in _VALID_DEPARTMENTS:
        raise HTTPException(400, f"department invalid. Valori acceptate: {sorted(_VALID_DEPARTMENTS)}")
    start = _month_start(body.get("valid_from"), "valid_from") or cur[2]
    if cur[3] is not None and start >= cur[3]:
        raise HTTPException(400, "valid_from trebuie să fie înainte de sfârșitul intervalului.")

    prev = db.execute(text(
        "SELECT id, valid_from FROM employee_department_history "
        "WHERE employee_id=:id AND id <> :hid AND valid_from < CAST(:s AS date) "
        "ORDER BY valid_from DESC LIMIT 1"
    ), {"id": emp_id, "hid": hid, "s": start}).fetchone()
    if prev is not None and prev[1] >= start:
        raise HTTPException(409, "Intervalul s-ar suprapune peste cel precedent.")
    # Ordinea contează: întâi scurtăm precedentul, abia apoi mutăm începutul (constrângerea de
    # non-suprapunere din DB respinge starea intermediară inversă).
    if prev is not None:
        db.execute(text("UPDATE employee_department_history SET valid_to = CAST(:s AS date) "
                        "WHERE id=:pid"), {"s": start, "pid": prev[0]})
    db.execute(text(
        "UPDATE employee_department_history SET department=:d, valid_from=CAST(:s AS date), "
        "source='manual' WHERE id=:hid"
    ), {"d": dept, "s": start, "hid": hid})
    _sync_current_department(db, emp_id)
    actor = admin.get("username") or admin.get("email") or "admin"
    _audit_dept_history(db, actor, emp_id, "update",
                        {"id": hid, "department": dept, "valid_from": str(start)})
    db.commit()
    warnings = _refresh_open_snapshots(db, {dept, cur[1]}, min(start, cur[2]))
    return {"id": hid, "employee_id": emp_id, "department": dept,
            "valid_from": str(start), "valid_to": str(cur[3]) if cur[3] else None,
            "warnings": warnings}


@router.delete("/settings/employees/{emp_id}/department-history/{hid}")
def delete_employee_department(emp_id: int, hid: int, db: Session = Depends(get_db),
                               admin=Depends(get_current_admin)):
    """Șterge un interval, extinzând precedentul peste el (fără goluri).

    Primul interval al unui angajat nu se poate șterge: ar rămâne fără apartenență pe lunile
    dinaintea celui următor și ar dispărea din rapoartele acelor luni.
    """
    cur = db.execute(text(
        "SELECT id, valid_from, valid_to FROM employee_department_history "
        "WHERE id=:hid AND employee_id=:id"
    ), {"hid": hid, "id": emp_id}).fetchone()
    if not cur:
        raise HTTPException(404, "Interval negăsit")
    prev = db.execute(text(
        "SELECT id FROM employee_department_history "
        "WHERE employee_id=:id AND valid_from < CAST(:s AS date) ORDER BY valid_from DESC LIMIT 1"
    ), {"id": emp_id, "s": cur[1]}).fetchone()
    if prev is None:
        raise HTTPException(409, "Primul interval nu poate fi șters — editează-l în schimb.")
    dept_row = db.execute(text("SELECT department FROM employee_department_history WHERE id=:hid"),
                          {"hid": hid}).fetchone()
    db.execute(text("DELETE FROM employee_department_history WHERE id=:hid"), {"hid": hid})
    db.execute(text("UPDATE employee_department_history SET valid_to = :vt WHERE id=:pid"),
               {"vt": cur[2], "pid": prev[0]})
    _sync_current_department(db, emp_id)
    actor = admin.get("username") or admin.get("email") or "admin"
    _audit_dept_history(db, actor, emp_id, "delete", {"id": hid})
    db.commit()
    warnings = _refresh_open_snapshots(db, {dept_row[0]} if dept_row else set(), cur[1])
    return {"ok": True, "warnings": warnings}
