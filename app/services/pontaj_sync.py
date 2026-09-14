"""Sincronizare pontaj CTS -> department_attendance (mirror iris_employee_sync.py).

STARE (2026-07-02): endpoint-ul `/cts/timesheets` e live pe Gateway (verificat direct cu cheia
reală). Contract CONFIRMAT (diferă de presupunerea inițială din OUTBOX_pontaj_endpoint.md):
  GET {IRIS_API_URL}{pontaj_sync.endpoint_path} (implicit /cts/timesheets), header X-Mailguard-Key,
  params `start` / `end` (YYYY-MM-DD) -- NU `date_from`/`date_to` (Gateway le ignoră silențios și
  cade pe ziua curentă dacă primește nume greșite). Interval maxim acceptat de Gateway: 31 zile
  (400 peste); `fetch_timesheets` sparge ferestre mai mari în bucăți ≤31 zile.
  Răspuns: `{"items": [{department, department_slug, date, present, employees: [...]}]}`.
  Parsarea de mai jos rămâne defensivă (mai multe denumiri posibile de cheie) ca la restul
  sync-urilor CTS, dar shape-ul real e deja confirmat compatibil.

Departament: reutilizează normalizarea + whitelist-ul din iris_employee_sync (_norm_dept,
VALID_DEPARTMENTS) ca să nu apară drift între cele două sincronizări (un slug acceptat la
angajați dar respins la pontaj, sau invers) -- confirmat compatibil: `_dept_slug` pe Gateway
produce cratimă (ex. "suport-1"), iar `_norm_dept` normalizează cratimă->underscore.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import datetime as _dt
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.iris_employee_sync import _norm_dept, VALID_DEPARTMENTS

logger = logging.getLogger("mailguard.pontaj_sync")

RECENT_WINDOW_DAYS = 35   # acopera retroactiv ultimele ~5 sambete daca CTS corecteaza pontaje
RECENT_MIN_INTERVAL_S = 240
_recent_lock = threading.Lock()


# ── helpers settings (mirror iris_employee_sync) ─────────────────────────────
def _get_setting(db: Session, key: str, default=None):
    row = db.execute(text("SELECT value FROM settings WHERE key=:k"), {"k": key}).fetchone()
    if not row or row._mapping["value"] is None:
        return default
    return row._mapping["value"]


def _set_setting(db: Session, key: str, value, description: Optional[str] = None):
    db.execute(text(
        "INSERT INTO settings(key, value, description) VALUES (:k, CAST(:v AS jsonb), :d) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()"
    ), {"k": key, "v": json.dumps(value), "d": description})


def is_enabled(db: Session) -> bool:
    return bool(_get_setting(db, "pontaj_sync.enabled", False))


def _gateway_config() -> tuple[str, str]:
    from app.config import get_settings
    base = (get_settings().iris_api_url or "").rstrip("/")
    key = os.getenv("IRIS_MAILGUARD_API_KEY", "")
    return base, key


def _pick(d: Dict[str, Any], *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _parse_ts(v) -> Optional[_dt.datetime]:
    if v is None:
        return None
    if isinstance(v, _dt.datetime):
        return v.replace(tzinfo=None) if v.tzinfo else v
    try:
        dt = _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except Exception:
        return None


GATEWAY_MAX_WINDOW_DAYS = 31   # Gateway respinge (400) orice interval start..end > 31 zile


def _chunk_date_range(date_from: str, date_to: str, max_days: int = GATEWAY_MAX_WINDOW_DAYS):
    """Sparge [date_from, date_to] in bucati inclusive de cel mult max_days zile."""
    d_from = _dt.date.fromisoformat(date_from)
    d_to = _dt.date.fromisoformat(date_to)
    step = _dt.timedelta(days=max_days)
    cur = d_from
    while cur <= d_to:
        chunk_end = min(cur + step, d_to)
        yield cur.isoformat(), chunk_end.isoformat()
        cur = chunk_end + _dt.timedelta(days=1)


def _fetch_timesheets_window(db: Session, base: str, key: str, path: str,
                              start: str, end: str) -> List[Dict[str, Any]]:
    from app.services.iris_http import get_with_retry
    r = get_with_retry(base + path, params={"start": start, "end": end},
                       headers={"X-Mailguard-Key": key}, label=path)
    data = r.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("timesheets") or data.get("items") or data.get("records") or data.get("data") or []
    return []


# ── fetch ────────────────────────────────────────────────────────────────────
def fetch_timesheets(db: Session, date_from: str, date_to: str) -> List[Dict[str, Any]]:
    base, key = _gateway_config()
    if not base or not key:
        raise RuntimeError("Gateway IRIS neconfigurat (iris_api_url / IRIS_MAILGUARD_API_KEY).")
    path = _get_setting(db, "pontaj_sync.endpoint_path", "/cts/timesheets") or "/cts/timesheets"
    if not str(path).startswith("/"):
        path = "/" + str(path)
    records: List[Dict[str, Any]] = []
    for start, end in _chunk_date_range(date_from, date_to):
        records.extend(_fetch_timesheets_window(db, base, key, path, start, end))
    return records


# ── sync principal ───────────────────────────────────────────────────────────
def sync_attendance(db: Session, *, dry_run: bool = False, payload: Optional[List[Dict[str, Any]]] = None,
                     date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict[str, Any]:
    """Sincronizează prezența pe zile în department_attendance. Idempotent (upsert pe PK).

    payload: pentru test/manual -- daca e dat, se foloseste in loc de fetch din IRIS.
    """
    if not is_enabled(db) and payload is None:
        return {"ok": False, "skipped": "pontaj_sync.enabled=false"}

    base, key = _gateway_config()
    if payload is None and (not base or not key):
        return {"ok": False, "skipped": "Gateway IRIS neconfigurat (iris_api_url / IRIS_MAILGUARD_API_KEY)."}

    if payload is None:
        if not date_from or not date_to:
            today = _dt.date.today()
            date_from = date_from or (today - _dt.timedelta(days=RECENT_WINDOW_DAYS)).isoformat()
            date_to = date_to or today.isoformat()
        try:
            records = fetch_timesheets(db, date_from, date_to)
        except Exception as e:
            logger.warning("pontaj sync fetch failed: %s", e)
            res = {"ok": False, "error": str(e)}
            if not dry_run:
                _set_setting(db, "pontaj_sync.last_result", res)
                db.commit()
            return res
    else:
        records = payload

    dmap = _get_setting(db, "employee_sync.department_map", {}) or {}
    if not isinstance(dmap, dict):
        dmap = {}

    n_upsert = n_skip_dept = n_skip_date = 0
    n_emp_skipped_manual = 0
    seen_emp: Dict[Any, Any] = {}  # (dept, work_date) -> (employees_list, dept_present)

    # Pontaje corectate manual: nu se suprascriu (nici prezență CTS, nici absență implicită).
    prot_rows = db.execute(text(
        "SELECT employee_id, work_date FROM employee_attendance "
        "WHERE manual_override = true AND employee_id IS NOT NULL"
    )).fetchall()
    protected = {(int(r._mapping["employee_id"]), str(r._mapping["work_date"])[:10]) for r in prot_rows}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        dept = _norm_dept(
            _pick(rec, "department_slug", "dept_slug"),
            _pick(rec, "department", "dept", "departament"),
            dmap,
        )
        if dept is None:
            n_skip_dept += 1
            continue

        raw_date = _pick(rec, "date", "work_date", "data", "ziua")
        if not raw_date:
            n_skip_date += 1
            continue
        work_date = str(raw_date)[:10]

        present = _pick(rec, "present", "prezent", "worked", default=None)
        present = bool(present) if present is not None else True

        emps = _pick(rec, "employees", "angajati", default=None)
        if isinstance(emps, list):
            seen_emp[(dept, work_date)] = (emps, present)

        if dry_run:
            n_upsert += 1
            continue

        db.execute(text(
            "INSERT INTO department_attendance(department, work_date, present, source, synced_at) "
            "VALUES (:d, :wd, :p, 'cts_pontaj', now()) "
            "ON CONFLICT (department, work_date) DO UPDATE SET "
            "present=EXCLUDED.present, source='cts_pontaj', synced_at=now()"
        ), {"d": dept, "wd": work_date, "p": present})
        n_upsert += 1

    if dry_run:
        return {"ok": True, "dry_run": True, "received": len(records),
                "would_upsert": n_upsert, "skipped_dept": n_skip_dept, "skipped_date": n_skip_date,
                "employee_days_with_data": len(seen_emp)}

    # ── employee_attendance sync ──────────────────────────────────────────────
    n_emp_upsert = n_emp_absence = 0
    if seen_emp:
        emp_rows = db.execute(text(
            "SELECT id, name, department FROM employee_department_mapping WHERE enabled=true"
        )).fetchall()
        emp_cache: Dict[str, Any] = {}  # dept -> {name_norm: (id, full_name)}
        for row in emp_rows:
            m = row._mapping
            d = m["department"]
            if d not in emp_cache:
                emp_cache[d] = {}
            emp_cache[d][str(m["name"]).lower().strip()] = (m["id"], str(m["name"]))

        for (dept, work_date), (emp_list, _dept_present) in seen_emp.items():
            dept_cache = emp_cache.get(dept, {})
            seen_names: set = set()

            # Acumulare intervale per (cts_id, work_date) — CTS poate trimite mai multe
            # perioade per angajat per zi (ex. LR cu recuperare = 3 intervale distincte).
            emp_day_acc: Dict[str, Any] = {}

            for item in emp_list:
                if isinstance(item, str):
                    fn, item = item.strip(), {}
                elif isinstance(item, dict):
                    fn = str(_pick(item, "full_name", "name", "nume") or "").strip()
                else:
                    continue
                if not fn:
                    continue
                fn_norm = fn.lower().strip()
                seen_names.add(fn_norm)

                cts_id = _pick(item, "id", "cts_id", "iris_id", "employee_id")
                cts_id = str(cts_id) if cts_id is not None else fn_norm
                emp_id = dept_cache.get(fn_norm, (None,))[0]

                begin_ts = _parse_ts(_pick(item, "begin", "begin_time", "check_in", "start_time", "ora_intrare"))
                end_ts = _parse_ts(_pick(item, "end", "end_time", "check_out", "ora_iesire"))
                mins = _pick(item, "minutes", "worked_minutes", "minute")
                mins = int(mins) if mins is not None else None

                acc_key = cts_id
                if acc_key not in emp_day_acc:
                    emp_day_acc[acc_key] = {
                        "eid": emp_id, "fn": fn, "mins": 0,
                        "begin": None, "end": None, "intervals": [],
                    }
                acc = emp_day_acc[acc_key]
                if mins:
                    acc["mins"] += mins
                if begin_ts:
                    acc["begin"] = min(acc["begin"], begin_ts) if acc["begin"] else begin_ts
                if end_ts:
                    acc["end"] = max(acc["end"], end_ts) if acc["end"] else end_ts
                if begin_ts and end_ts:
                    acc["intervals"].append({
                        "begin": begin_ts.isoformat(),
                        "end": end_ts.isoformat(),
                        "minutes": mins,
                    })

            for cts_id, acc in emp_day_acc.items():
                if acc["eid"] and (acc["eid"], work_date) in protected:
                    n_emp_skipped_manual += 1
                    continue
                ivs = json.dumps(acc["intervals"]) if len(acc["intervals"]) > 1 else None
                db.execute(text(
                    "INSERT INTO employee_attendance"
                    "(cts_employee_id, employee_id, full_name, department, work_date,"
                    " present, begin_time, end_time, minutes, intervals, source, synced_at)"
                    " VALUES (:cid,:eid,:fn,:dept,:wd,true,:bt,:et,:mins,:ivs,'cts_pontaj',now())"
                    " ON CONFLICT (cts_employee_id, work_date) DO UPDATE SET"
                    " employee_id=EXCLUDED.employee_id, full_name=EXCLUDED.full_name,"
                    " department=EXCLUDED.department, present=EXCLUDED.present,"
                    " begin_time=EXCLUDED.begin_time, end_time=EXCLUDED.end_time,"
                    " minutes=EXCLUDED.minutes, intervals=EXCLUDED.intervals, synced_at=now()"
                    " WHERE employee_attendance.manual_override IS NOT TRUE"
                ), {"cid": cts_id, "eid": acc["eid"], "fn": acc["fn"], "dept": dept,
                    "wd": work_date, "bt": acc["begin"], "et": acc["end"],
                    "mins": acc["mins"] or None, "ivs": ivs})
                n_emp_upsert += 1

            # Absent implicite: angajati enabled din dept care nu au aparut in employees[]
            for name_norm, (emp_id, full_name) in dept_cache.items():
                if name_norm not in seen_names:
                    if emp_id and (emp_id, work_date) in protected:
                        n_emp_skipped_manual += 1
                        continue
                    cts_id = f"absent_{emp_id}_{work_date}"
                    db.execute(text(
                        "INSERT INTO employee_attendance"
                        "(cts_employee_id, employee_id, full_name, department, work_date,"
                        " present, source, synced_at)"
                        " VALUES (:cid,:eid,:fn,:dept,:wd,false,'cts_pontaj',now())"
                        " ON CONFLICT (cts_employee_id, work_date) DO UPDATE SET"
                        " present=EXCLUDED.present, synced_at=now()"
                        " WHERE employee_attendance.manual_override IS NOT TRUE"
                    ), {"cid": cts_id, "eid": emp_id, "fn": full_name,
                        "dept": dept, "wd": work_date})
                    n_emp_absence += 1

    summary = {"ok": True, "received": len(records), "upserted": n_upsert,
               "skipped_dept": n_skip_dept, "skipped_date": n_skip_date,
               "emp_upserted": n_emp_upsert, "emp_absences": n_emp_absence,
               "emp_skipped_manual": n_emp_skipped_manual,
               "date_from": date_from, "date_to": date_to}
    db.execute(text("UPDATE settings SET value=to_jsonb(now()) WHERE key='pontaj_sync.last_sync_at'"))
    _set_setting(db, "pontaj_sync.last_result", summary)
    db.commit()
    return summary


def run_recent_if_due() -> Dict[str, Any]:
    """Self-gated pe cron-ul de 5 min (via /process/run-now), mirror cts_calls_sync.

    No-op ieftin daca: sync dezactivat, gateway neconfigurat, sau a rulat recent (throttle
    intern ~4 min via lock, ca sa nu bata IRIS Gateway la fiecare tick).
    """
    if not _recent_lock.acquire(blocking=False):
        return {"skipped": "already running"}
    try:
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            if not is_enabled(db):
                return {"skipped": "disabled"}
            base, key = _gateway_config()
            if not base or not key:
                return {"skipped": "gateway not configured"}
            last = _get_setting(db, "pontaj_sync.last_sync_at")
            if last:
                try:
                    lt = _dt.datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                    if lt.tzinfo is None:
                        lt = lt.replace(tzinfo=_dt.timezone.utc)
                    if (_dt.datetime.now(_dt.timezone.utc) - lt).total_seconds() < RECENT_MIN_INTERVAL_S:
                        return {"skipped": "throttled"}
                except Exception:
                    pass
            return sync_attendance(db)
        except Exception as e:
            logger.warning("pontaj recent sync failed: %s", e)
            return {"ok": False, "error": str(e)}
        finally:
            db.close()
    finally:
        _recent_lock.release()
