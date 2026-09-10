"""Program de lucru per departament -- CRUD pt tabela department_schedule.

Folosit de app/services/productivity.py (business_minutes) ca sa calculeze SLA-ul de
solutionare doar in intervalul orar configurat aici, nu 24/7. Vezi
migrations/20260702h_department_schedule.sql.
"""
import calendar
import datetime as _dt
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.api.v1.auth import get_current_admin
from app.services.iris_employee_sync import VALID_DEPARTMENTS

logger = logging.getLogger("mailguard.department_schedule")
router = APIRouter()

_TIME_RE = __import__("re").compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# employee_attendance.begin_time/end_time sunt UTC naive. Afișarea și editarea
# din UI folosesc ora României — același +3h ca GET monthly-attendance.
_LOCAL_UTC_OFFSET = _dt.timedelta(hours=3)


def _utc_to_local_hm(ts) -> Optional[str]:
    if not ts:
        return None
    return (ts + _LOCAL_UTC_OFFSET).strftime("%H:%M")


def _local_hm_to_utc(work_date: _dt.date, hm: str) -> _dt.datetime:
    hh, mm = map(int, hm.split(":"))
    return _dt.datetime.combine(work_date, _dt.time(hh, mm)) - _LOCAL_UTC_OFFSET


def _empty_week(department: str) -> list:
    return [{"weekday": wd, "start_time": None, "end_time": None,
             "active": False, "requires_attendance": (wd == 6)}
            for wd in range(1, 8)]


@router.get("/department-schedule/pontaj-sync-status")
def pontaj_sync_status(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Starea sincronizarii pontaj CTS -> department_attendance (INERT pana la grant Razvan)."""
    def _val(k, d=None):
        r = db.execute(text("SELECT value FROM settings WHERE key=:k"), {"k": k}).fetchone()
        return r._mapping["value"] if r and r._mapping["value"] is not None else d
    n_days = db.execute(text(
        "SELECT count(DISTINCT work_date) AS n FROM department_attendance")).fetchone()
    return {
        "enabled": bool(_val("pontaj_sync.enabled", False)),
        "endpoint_path": _val("pontaj_sync.endpoint_path"),
        "last_sync_at": _val("pontaj_sync.last_sync_at"),
        "last_result": _val("pontaj_sync.last_result"),
        "days_covered": n_days._mapping["n"] if n_days else 0,
    }


@router.post("/department-schedule/pontaj-sync")
def pontaj_sync_now(body: dict = None, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Declanseaza manual sincronizarea pontaj din CTS. INERT (no-op) pana la grant Razvan --
    daca pontaj_sync.enabled=false sau gateway-ul nu e configurat, intoarce {ok:false, skipped:...}.
    dry_run=true -> doar numara, nu scrie.
    """
    from app.services import pontaj_sync
    dry = bool((body or {}).get("dry_run"))
    try:
        return pontaj_sync.sync_attendance(db, dry_run=dry)
    except Exception as e:
        raise HTTPException(500, f"Sync pontaj esuat: {e}")


@router.get("/department-schedule/monthly-attendance")
def monthly_attendance(
    month: Optional[str] = Query(None, description="Luna in format YYYY-MM, default luna curenta"),
    department: Optional[str] = Query(None, description="Slug departament; daca lipseste, returneaza toate"),
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Calendar lunar de prezenta reala per angajat, din employee_attendance.

    Completeaza cu angajatii enabled din employee_department_mapping care nu au inregistrari
    in employee_attendance (present=null = fara date pontaj pentru ziua respectiva).
    """
    today = _dt.date.today()
    if month:
        try:
            month_start = _dt.date.fromisoformat(month + "-01")
        except ValueError:
            raise HTTPException(400, "Format luna invalid, asteptat YYYY-MM")
    else:
        month_start = today.replace(day=1)

    month_end = month_start.replace(day=calendar.monthrange(month_start.year, month_start.month)[1])
    month_str = month_start.strftime("%Y-%m")

    depts = [department] if department else sorted(VALID_DEPARTMENTS)
    for d in depts:
        if d not in VALID_DEPARTMENTS:
            raise HTTPException(400, f"Departament necunoscut: {d}")

    # Zile lucratoare conform department_schedule (active=true)
    sched_rows = db.execute(text(
        "SELECT department, weekday FROM department_schedule WHERE active=true"
    )).fetchall()
    working_weekdays: dict = {}  # dept -> set of ISO weekdays (1=Luni..7=Duminica)
    for r in sched_rows:
        m = r._mapping
        working_weekdays.setdefault(m["department"], set()).add(int(m["weekday"]))

    # Componenta departamentelor IN LUNA AFISATA (employee_department_history): calendarul lui
    # august trebuie sa arate echipa din august, nu pe cea de azi. Pentru luna curenta rezultatul
    # e identic cu `enabled=true` + departamentul curent, fiindca trigger-ul tine intervalul
    # deschis in oglinda cu scalarul.
    emp_rows = db.execute(text(
        "SELECT e.id, e.name, h.department "
        "FROM employee_department_history h "
        "JOIN employee_department_mapping e ON e.id = h.employee_id "
        "WHERE h.valid_from <= :mend AND (h.valid_to IS NULL OR h.valid_to > :mstart) "
        "ORDER BY e.name"
    ), {"mstart": month_start, "mend": month_end}).fetchall()
    dept_employees: dict = {}  # dept -> list of {id, name}
    for r in emp_rows:
        m = r._mapping
        if m["department"] not in dept_employees:
            dept_employees[m["department"]] = []
        dept_employees[m["department"]].append({"id": m["id"], "name": str(m["name"])})

    # Date prezenta din employee_attendance (cu ore pontaj)
    att_rows = db.execute(text(
        "SELECT employee_id, full_name, department, work_date, present, "
        "  begin_time, end_time, minutes, intervals, "
        "  COALESCE(manual_override, false) AS manual_override, "
        "  manual_override_by, source "
        "FROM employee_attendance "
        "WHERE department = ANY(:depts) "
        "  AND work_date BETWEEN :start AND :end "
        "ORDER BY department, work_date"
    ), {"depts": depts, "start": month_start, "end": month_end}).fetchall()

    # Index: (dept, date_str) -> {employee_id: {present, begin, end, minutes, intervals}}
    att_index: dict = {}
    for r in att_rows:
        m = r._mapping
        key = (m["department"], str(m["work_date"]))
        if key not in att_index:
            att_index[key] = {}
        if m["employee_id"] is not None:
            att_index[key][int(m["employee_id"])] = {
                "present": bool(m["present"]),
                "begin": _utc_to_local_hm(m["begin_time"]),
                "end": _utc_to_local_hm(m["end_time"]),
                "minutes": int(m["minutes"]) if m["minutes"] else None,
                "intervals": m["intervals"] if m["intervals"] else None,
                "manual_override": bool(m["manual_override"]),
                "manual_override_by": m["manual_override_by"],
                "source": m["source"],
            }

    # Construim raspunsul per departament
    result = {}
    cur = month_start
    all_days = []
    while cur <= month_end:
        all_days.append(cur)
        cur += _dt.timedelta(days=1)

    for dept in depts:
        dept_wdays = working_weekdays.get(dept, set())
        employees = dept_employees.get(dept, [])

        days_info = []
        for d in all_days:
            iso_wd = d.isoweekday()
            days_info.append({
                "day": d.day,
                "date": d.isoformat(),
                "weekday": iso_wd,
                "is_working": iso_wd in dept_wdays,
            })

        emp_list = []
        for emp in employees:
            emp_id = emp["id"]
            days_presence = {}
            for d in all_days:
                date_str = d.isoformat()
                att = att_index.get((dept, date_str), {})
                days_presence[str(d.day)] = att.get(emp_id)  # True/False/None
            emp_list.append({"name": emp["name"], "employee_id": emp_id, "days": days_presence})

        # Totaluri zilnice + distributie schimburi (begin < 11:00 = Sch.1, altfel Sch.2)
        daily_totals = {}
        daily_shift_counts = {}
        for d in all_days:
            att = att_index.get((dept, d.isoformat()), {})
            daily_totals[str(d.day)] = sum(1 for v in att.values() if v and v.get("present"))
            s1 = s2 = 0
            for v in att.values():
                if not v or not v.get("present"):
                    continue
                if (v.get("minutes") or 0) < 330:  # sub 5h — partial, nu e schimb complet
                    continue
                b = v.get("begin")
                if b:
                    s1, s2 = (s1 + 1, s2) if b < "11:00" else (s1, s2 + 1)
            daily_shift_counts[str(d.day)] = {"sch1": s1, "sch2": s2}

        result[dept] = {
            "month": month_str,
            "department": dept,
            "days": days_info,
            "employees": emp_list,
            "daily_totals": daily_totals,
            "daily_shift_counts": daily_shift_counts,
            "total_employees": len(employees),
            "has_data": bool(att_index),
        }

    if department:
        return result.get(department, {})
    return result


@router.put("/department-schedule/attendance")
def put_employee_attendance(body: dict, db: Session = Depends(get_db),
                            admin=Depends(get_current_admin)):
    """Corectare manuală a unui pontaj (angajat + zi). Marchează rândul ca
    manual_override — pontaj_sync nu-l mai suprascrie.

    Body: employee_id, work_date (YYYY-MM-DD), present (bool), begin/end (HH:MM
    ora României). revert=true scoate protecția; următoarea sync ia din nou CTS.
    """
    if not isinstance(body, dict):
        raise HTTPException(400, "Corp invalid")

    try:
        emp_id = int(body.get("employee_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "employee_id invalid")

    raw_date = body.get("work_date")
    try:
        work_date = _dt.date.fromisoformat(str(raw_date)[:10])
    except (TypeError, ValueError):
        raise HTTPException(400, "work_date invalid, așteptat YYYY-MM-DD")

    emp = db.execute(text(
        "SELECT id, name, department FROM employee_department_mapping WHERE id=:id"
    ), {"id": emp_id}).fetchone()
    if not emp:
        raise HTTPException(404, "Angajat necunoscut")
    emp_m = emp._mapping
    actor = (admin or {}).get("email") or (admin or {}).get("username") or "admin"

    existing = db.execute(text(
        "SELECT id, cts_employee_id FROM employee_attendance "
        "WHERE employee_id=:eid AND work_date=:wd ORDER BY id"
    ), {"eid": emp_id, "wd": work_date}).fetchall()

    if body.get("revert"):
        if not existing:
            raise HTTPException(404, "Nu există pontaj de revenit pentru această zi")
        db.execute(text(
            "UPDATE employee_attendance SET manual_override=false, "
            "manual_override_at=NULL, manual_override_by=NULL "
            "WHERE employee_id=:eid AND work_date=:wd"
        ), {"eid": emp_id, "wd": work_date})
        db.execute(text(
            "DELETE FROM employee_attendance "
            "WHERE employee_id=:eid AND work_date=:wd "
            "  AND cts_employee_id LIKE 'manual_%'"
        ), {"eid": emp_id, "wd": work_date})
        db.commit()
        return {"ok": True, "reverted": True,
                "employee_id": emp_id, "work_date": work_date.isoformat()}

    present = bool(body.get("present", True))
    begin_hm = body.get("begin")
    end_hm = body.get("end")
    begin_ts = end_ts = minutes = None
    if present:
        if not (isinstance(begin_hm, str) and _TIME_RE.match(begin_hm)):
            raise HTTPException(400, "begin invalid (așteptat HH:MM)")
        if not (isinstance(end_hm, str) and _TIME_RE.match(end_hm)):
            raise HTTPException(400, "end invalid (așteptat HH:MM)")
        begin_ts = _local_hm_to_utc(work_date, begin_hm)
        end_ts = _local_hm_to_utc(work_date, end_hm)
        if end_ts <= begin_ts:
            end_ts += _dt.timedelta(days=1)
        minutes = int((end_ts - begin_ts).total_seconds() // 60)

    params = {
        "eid": emp_id, "fn": str(emp_m["name"]), "dept": str(emp_m["department"]),
        "wd": work_date, "p": present, "bt": begin_ts, "et": end_ts,
        "mins": minutes, "by": actor,
    }
    if existing:
        db.execute(text(
            "UPDATE employee_attendance SET "
            "present=:p, begin_time=:bt, end_time=:et, minutes=:mins, intervals=NULL, "
            "source='manual', manual_override=true, manual_override_at=now(), "
            "manual_override_by=:by, synced_at=now() "
            "WHERE employee_id=:eid AND work_date=:wd"
        ), params)
    else:
        db.execute(text(
            "INSERT INTO employee_attendance "
            "(cts_employee_id, employee_id, full_name, department, work_date, "
            " present, begin_time, end_time, minutes, intervals, source, synced_at, "
            " manual_override, manual_override_at, manual_override_by) "
            "VALUES (:cid, :eid, :fn, :dept, :wd, :p, :bt, :et, :mins, NULL, "
            "        'manual', now(), true, now(), :by)"
        ), {**params, "cid": "manual_" + str(emp_id)})
    db.commit()
    return {
        "ok": True,
        "employee_id": emp_id,
        "work_date": work_date.isoformat(),
        "present": present,
        "begin": begin_hm if present else None,
        "end": end_hm if present else None,
        "minutes": minutes,
        "manual_override": True,
    }


@router.get("/department-schedule")
def get_department_schedule(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    rows = db.execute(text(
        "SELECT department, weekday, start_time, end_time, active, requires_attendance "
        "FROM department_schedule ORDER BY department, weekday")).fetchall()
    out = {d: _empty_week(d) for d in sorted(VALID_DEPARTMENTS)}
    for r in rows:
        m = r._mapping
        dept = m["department"]
        if dept not in out:
            continue
        slot = out[dept][m["weekday"] - 1]
        slot["start_time"] = m["start_time"].strftime("%H:%M") if m["start_time"] else None
        slot["end_time"] = m["end_time"].strftime("%H:%M") if m["end_time"] else None
        slot["active"] = bool(m["active"])
        slot["requires_attendance"] = bool(m["requires_attendance"])
    return out


@router.put("/department-schedule/{department}")
def put_department_schedule(department: str, body: list, db: Session = Depends(get_db),
                             admin=Depends(get_current_admin)):
    if department not in VALID_DEPARTMENTS:
        raise HTTPException(400, "Departament necunoscut: " + str(department))
    if not isinstance(body, list) or not body:
        raise HTTPException(400, "Corp invalid -- se astepta o lista de zile")

    by_weekday = {}
    for entry in body:
        if not isinstance(entry, dict):
            raise HTTPException(400, "Fiecare intrare trebuie sa fie un obiect")
        wd = entry.get("weekday")
        if not isinstance(wd, int) or wd < 1 or wd > 7:
            raise HTTPException(400, "weekday invalid (1=Luni..7=Duminica): " + str(wd))
        if wd in by_weekday:
            raise HTTPException(400, "weekday duplicat: " + str(wd))
        active = bool(entry.get("active"))
        requires_attendance = bool(entry.get("requires_attendance"))
        start_time = entry.get("start_time")
        end_time = entry.get("end_time")
        if active:
            if not (isinstance(start_time, str) and _TIME_RE.match(start_time)):
                raise HTTPException(400, "start_time invalid pt ziua " + str(wd) + " (asteptat HH:MM)")
            if not (isinstance(end_time, str) and _TIME_RE.match(end_time)):
                raise HTTPException(400, "end_time invalid pt ziua " + str(wd) + " (asteptat HH:MM)")
            if start_time >= end_time:
                raise HTTPException(400, "start_time trebuie sa fie inainte de end_time (ziua " + str(wd) + ")")
        by_weekday[wd] = {
            "active": active, "requires_attendance": requires_attendance,
            "start_time": start_time if active else None, "end_time": end_time if active else None,
        }

    for wd in range(1, 8):
        if wd in by_weekday:
            e = by_weekday[wd]
            db.execute(text(
                "INSERT INTO department_schedule(department, weekday, start_time, end_time, "
                "active, requires_attendance, updated_by, updated_at) "
                "VALUES (:d, :wd, :st, :et, :active, :ra, :by, now()) "
                "ON CONFLICT (department, weekday) DO UPDATE SET "
                "start_time=EXCLUDED.start_time, end_time=EXCLUDED.end_time, "
                "active=EXCLUDED.active, requires_attendance=EXCLUDED.requires_attendance, "
                "updated_by=EXCLUDED.updated_by, updated_at=now()"),
                {"d": department, "wd": wd,
                 "st": e["start_time"] or "00:00", "et": e["end_time"] or "00:00",
                 "active": e["active"], "ra": e["requires_attendance"],
                 "by": (admin or {}).get("email") or (admin or {}).get("username")})
        else:
            db.execute(text("DELETE FROM department_schedule WHERE department=:d AND weekday=:wd"),
                       {"d": department, "wd": wd})
    db.commit()
    return get_department_schedule(db=db, admin=admin).get(department, _empty_week(department))
