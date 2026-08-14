"""v10.20.0 — Angajați (Utilizatori → Angajați): router separat de Settings.

Endpoint-urile /settings/employees* alimentează pagina Utilizatori, nu zona de
Setări. Cât timp stăteau în settings.py erau păzite de require_module("settings")
— modul rezervat developerilor — deci un admin primea 403 pe propria pagină și
UI-ul rămânea alb. Router propriu, montat cu require_module("utilizatori"):
admin + developer au acces, operatorul nu.

Căile rămân neschimbate (/api/v1/settings/employees...) ca să nu rupem UI-ul.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.api.v1.auth import get_current_admin
from app.database import get_db

router = APIRouter()


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
        "          AND s.kind!='planned_leave' AND s.entry_source<>'manual_extra') AS leave_count "
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
    row = db.execute(text(
        "INSERT INTO employee_schedule (employee_id, kind, start_date, end_date, status, days, raw, entry_source) "
        "VALUES (:eid, :k, :s::date, :e::date, :st, :d, '{}', 'manual') RETURNING id"
    ), {"eid": emp_id, "k": kind, "s": start, "e": end, "st": status, "d": days}).fetchone()
    db.commit()
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
    db.execute(text(
        "UPDATE employee_schedule SET kind=:k, start_date=:s::date, end_date=:e::date, "
        "status=:st, days=:d WHERE id=:sid"
    ), {"k": kind, "s": start, "e": end, "st": status, "d": days, "sid": sid})
    db.commit()
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
    """Șterge un angajat din lista de employee signature matching."""
    result = db.execute(text(
        "DELETE FROM employee_department_mapping WHERE id=:id"
    ), {"id": emp_id})
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(404, "Angajat negăsit")
    return {"ok": True}
