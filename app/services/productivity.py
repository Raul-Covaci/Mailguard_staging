"""Modul Productivitate (mailuri) — calcul per operator -> agregat pe departament.

Faza 1: Suport 1/2, Taxe de drum; pondere email = 100%. Bottom-up: mailurile solved
sunt atribuite operatorului (cts_assignee_email -> employee_department_mapping.email)
si grupate pe departamentul OPERATORULUI. Se scoreaza doar departamentele cu obiective.

Timp de solutionare = minute in PROGRAMUL DE LUCRU al departamentului (nu timp brut 24/7):
start = created_at (raw->'extra', UTC; fallback emails.received_at), end = solved_at (fallback
cts_reply_at, egal cu solved_at in feed-ul actual). Durata efectiva e calculata de functia SQL
business_minutes() (migrations/20260702h_department_schedule.sql), care insumeaza doar minutele
care cad in intervalul orar configurat per departament (department_schedule), exclude sarbatorile
legale si (pana la integrarea pontajului CTS) sambetele -- un mail/task intrat vineri seara sau
in weekend nu mai acumuleaza ore "moarte" catre SLA, timpul repornind la urmatoarea reluare a
programului. Acoperire ~100%, non-degenerat. assigned->solved NU se foloseste: in CTS preluarea
si rezolvarea sunt acelasi moment (assigned~=solved => durata 0). Coloanele oficiale cts_solved_at
/ cts_created_at (populate de sync core) sunt preferate automat via COALESCE.
"""
from __future__ import annotations

import calendar
import datetime as _dt
import json
import re as _re
import statistics
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

_MINIM_DELTA = 5.0          # obiectiv_minim = obiectiv_real - 5
_DEFAULT_WORK_HOURS = 8
_NOTE_INSUFFICIENT = (
    "Nicio solutionare masurabila in aceasta luna (lipsa timp created->solved)."
)


# ---------------------------------------------------------------- snapshot lunar imutabil
def _get_snapshot(db: Session, department: str, year: int, month: int) -> Optional[dict]:
    """Returnează snapshot-ul fixat pentru departament/lună dacă există."""
    row = db.execute(text("""
        SELECT baza_procent, zile_lucratoare, ore_planificate, ore_disponibile,
               coeficient, obiectiv_real, obiectiv_minim
        FROM productivity_monthly_snapshot
        WHERE department = :dept AND year = :y AND month = :m
    """), {"dept": department, "y": year, "m": month}).fetchone()
    if row is None:
        return None
    return {
        "baza_procent":    float(row[0]),
        "zile_lucratoare": int(row[1]),
        "ore_planificate": float(row[2]),
        "ore_disponibile": float(row[3]),
        "coeficient":      float(row[4]) if row[4] is not None else None,
        "obiectiv_real":   float(row[5]) if row[5] is not None else None,
        "obiectiv_minim":  float(row[6]) if row[6] is not None else None,
    }


def _save_snapshot(db: Session, department: str, year: int, month: int,
                   baza_procent: float, zile_lucratoare: int,
                   ore_planificate: float, ore_disponibile: float,
                   coeficient, obiectiv_real, obiectiv_minim) -> None:
    """Persistă snapshot la prima generare. ON CONFLICT DO NOTHING — imutabil după creare."""
    try:
        db.execute(text("""
            INSERT INTO productivity_monthly_snapshot
                (department, year, month, baza_procent, zile_lucratoare,
                 ore_planificate, ore_disponibile, coeficient, obiectiv_real, obiectiv_minim)
            VALUES (:dept, :y, :m, :bp, :zl, :op, :od, :coef, :or_, :om)
            ON CONFLICT (department, year, month) DO NOTHING
        """), {
            "dept": department, "y": year, "m": month,
            "bp":   baza_procent, "zl": zile_lucratoare,
            "op":   ore_planificate, "od": ore_disponibile,
            "coef": coeficient, "or_": obiectiv_real, "om": obiectiv_minim,
        })
        db.commit()
    except Exception:
        db.rollback()


def _is_future_month(year: int, month: int, today: Optional[_dt.date] = None) -> bool:
    """True dacă luna (year, month) NU a început încă.

    Estimarea unei luni viitoare trebuie să fie LIVE: la fiecare accesare se recalculează din
    intrările actuale (concedii, zile de lucru pe proiecte, angajați cu productivity_start_date în
    viitor, program de lucru). Un snapshot fixat prematur ar îngheța cifrele și adăugarea unui
    concediu nu s-ar mai reflecta. Snapshot-ul devine imutabil doar din luna în care intră în curs.
    """
    d = today or _dt.date.today()
    return (year, month) > (d.year, d.month)


# Zile lucrătoare din luna în curs care trebuie să fie deja trecute înainte de a fixa snapshot-ul.
# Sub acest prag estimarea se recalculează LIVE la fiecare accesare.
_SNAPSHOT_MIN_ELAPSED_WD = 5


def _snapshot_too_early(year: int, month: int, holidays: set,
                        today: Optional[_dt.date] = None) -> bool:
    """True dacă luna e cea în curs și au trecut prea puține zile lucrătoare pentru a fixa targetul.

    Snapshot-ul e imutabil prin design. Dacă îl fixăm în ziua 1-2 a lunii, îl fixăm pe date de
    intrare incomplete: pontajul are doar câteva zile, iar orice angajat aflat în concediu chiar
    atunci apare cu zero activitate. Incidentul din 2026-08-03 a înghețat astfel suport_1 la
    840h (5 oameni) în loc de 1008h (6 oameni) și suport_2 la 672h în loc de 840h.

    Sub prag returnăm True -> estimarea se recalculează LIVE, exact ca o lună viitoare, deci
    reflectă imediat concediile și pontajul care intră pe parcurs. Peste prag snapshot-ul se
    fixează normal și rămâne imutabil pentru restul lunii.
    """
    d = today or _dt.date.today()
    if (year, month) != (d.year, d.month):
        return False  # lună trecută (fixată deja) sau viitoare (tratată de _is_future_month)
    elapsed_wd = sum(
        1 for day in range(1, d.day + 1)
        if _is_working_day(_dt.date(year, month, day), holidays)
    )
    return elapsed_wd < _SNAPSHOT_MIN_ELAPSED_WD


def reset_snapshot(db: Session, department: str, year: int, month: int) -> bool:
    """Sterge snapshot-ul fixat pentru departament/luna, ca urmatoarea generare sa-l recalculeze.

    Snapshot-ul e imutabil prin design (_save_snapshot foloseste ON CONFLICT DO NOTHING): odata
    emis un target pentru o luna, nu se schimba retroactiv sub picioarele oamenilor. Dar cand se
    modifica INTRARILE estimarii -- concedii noi/anulate, zile de lucru pe proiect/refurbished,
    data de start a productivitatii unui angajat, program de lucru -- estimarea trebuie refacuta,
    altfel rezerva cifrele vechi.

    Returneaza True daca exista un snapshot si a fost sters.
    """
    res = db.execute(text(
        "DELETE FROM productivity_monthly_snapshot "
        "WHERE department = :dept AND year = :y AND month = :m"
    ), {"dept": department, "y": year, "m": month})
    db.commit()
    return (res.rowcount or 0) > 0


# ---------------------------------------------------------------- sarbatori / zile lucratoare
def get_holidays(db: Session) -> set:
    """Set de 'YYYY-MM-DD' — sarbatori legale RO din settings KV 'productivity.ro_holidays'."""
    row = db.execute(text("SELECT value FROM settings WHERE key='productivity.ro_holidays'")).fetchone()
    if not row or row[0] is None:
        return set()
    raw = row[0]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return set()
    try:
        return {str(x) for x in (raw or [])}
    except Exception:
        return set()


def _is_working_day(d: _dt.date, holidays: set) -> bool:
    return d.isoweekday() < 6 and d.isoformat() not in holidays


def _holidays_date_list(holidays: set) -> list:
    """Converteste setul de 'YYYY-MM-DD' (get_holidays) intr-o lista de date, pt parametrul
    :holidays trimis catre business_minutes() (SQL, tip date[])."""
    out = []
    for s in holidays or ():
        try:
            out.append(_dt.date.fromisoformat(s))
        except Exception:
            continue
    return out


_EXTRA_WORK_KINDS = ("project_work", "refurbished")


def _extra_days_per_emp(db: Session, emp_ids: list, year: int, month: int) -> dict:
    """Zile libere extra (lucru pe proiecte / refurbished) per angajat pentru luna data.

    Sunt zile de lucru care NU sunt suport efectiv, deci se scad din ore_disponibile exact
    ca un concediu. Se exprima ca NUMAR de zile pe (an, luna) — fara date concrete — asa ca
    NU participa la union-ul de zile calendaristice al concediilor: se adauga aritmetic peste
    zilele de concediu, iar suma e plafonata la zile_lucratoare de catre apelant.

    Timing: intrarile se pot crea doar inainte de inceputul lunii vizate (validat in API),
    iar snapshot-ul lunar imutabil le fixeaza la prima zi lucratoare. Adaugarile ulterioare
    sunt refuzate, deci nu pot ajusta un target deja emis.
    """
    if not emp_ids:
        return {}
    rows = db.execute(text("""
        SELECT employee_id, COALESCE(SUM(days_count), 0)
        FROM employee_schedule
        WHERE employee_id = ANY(:ids)
          AND kind = ANY(:kinds)
          AND period_year = :y
          AND period_month = :m
        GROUP BY employee_id
    """), {"ids": emp_ids, "kinds": list(_EXTRA_WORK_KINDS), "y": year, "m": month}).fetchall()
    return {int(r[0]): int(r[1] or 0) for r in rows}


def working_days(year: int, month: int, holidays: set) -> int:
    """Nr. zile lucratoare (Luni-Vineri) din luna, minus sarbatorile legale."""
    ndays = calendar.monthrange(year, month)[1]
    return sum(
        1 for day in range(1, ndays + 1)
        if _is_working_day(_dt.date(year, month, day), holidays)
    )


def _working_days_in_range(start: _dt.date, end: _dt.date, holidays: set) -> int:
    if end < start:
        return 0
    cnt, d, one = 0, start, _dt.timedelta(days=1)
    while d <= end:
        if _is_working_day(d, holidays):
            cnt += 1
        d += one
    return cnt


# ---------------------------------------------------------------- durata solutionare (created->solved)
def resolution_minutes(start_at, solved_at) -> Optional[float]:
    """Minute de solutionare = interval created->solved (end - start).

    start = created_at (fallback received_at), end = solved_at (fallback reply_at).
    None daca lipseste un capat sau intervalul e <= 0 (created==solved => degenerat, ignorat).
    department_report calculeaza aceeasi expresie direct in SQL (tz/NULL-safe); aceasta ramane
    ca helper pur pentru teste si claritate."""
    if start_at is None or solved_at is None:
        return None
    mins = (solved_at - start_at).total_seconds() / 60.0
    return mins if mins > 0 else None


def _measurable(mins: Optional[float]) -> bool:
    """Masurabil = avem o durata (inclusiv 0).

    0 minute de program inseamna ca tot intervalul creare->solutionare a picat in afara
    orelor de lucru (mail intrat sambata noaptea si inchis inainte de luni dimineata, sau
    rezolvat de un angajat care iesise din tura). Decizie de business (Raul Covaci,
    2026-08-03): astfel de cazuri se considera rezolvate IN TIMP -- omul a raspuns cand nu
    era obligat sa fie la lucru, deci nu are sens sa fie penalizat sau exclus. `0 <= limita`
    e mereu adevarat in _accumulate, deci ies On time automat.

    `None` (interval invalid: capat lipsa, sau solved <= created) NU e masurabil -- acolo nu
    stim nimic despre durata. Vezi `business_minutes`, care separa explicit cele doua cazuri:
    `None` pentru interval degenerat, `0.0` pentru „complet in afara programului"."""
    return mins is not None


# ---------------------------------------------------------------- config / obiective
def get_department_config(db: Session, department: str) -> Optional[dict]:
    row = db.execute(
        text("SELECT department, baza_procent, updated_at, updated_by "
             "FROM productivity_department_config WHERE department=:d"),
        {"d": department},
    ).fetchone()
    if not row:
        return None
    return {
        "department": row[0],
        "baza_procent": float(row[1]),
        "updated_at": row[2].isoformat() if row[2] else None,
        "updated_by": row[3],
    }


def get_objectives(db: Session, department: str, tip: Optional[str] = "email") -> list:
    """Obiectivele departamentului. `tip=None` -> toate tipurile (Tab 2); altfel filtrat pe un tip."""
    if tip is None:
        rows = db.execute(
            text("SELECT id, department, tip, categorie, limita_minute, pondere, unitate "
                 "FROM productivity_objective WHERE department=:d "
                 "ORDER BY tip, pondere DESC, id"),
            {"d": department},
        ).fetchall()
    else:
        rows = db.execute(
            text("SELECT id, department, tip, categorie, limita_minute, pondere, unitate "
                 "FROM productivity_objective WHERE department=:d AND tip=:t "
                 "ORDER BY pondere DESC, id"),
            {"d": department, "t": tip},
        ).fetchall()
    return [{
        "id": r[0], "department": r[1], "tip": r[2],
        "categorie": r[3], "limita_minute": r[4], "pondere": float(r[5]), "unitate": r[6],
    } for r in rows]


def list_configs(db: Session) -> list:
    """Toate departamentele cu config + obiectivele lor, toate tipurile (pt Tab 2)."""
    rows = db.execute(text("SELECT department, baza_procent FROM productivity_department_config ORDER BY department")).fetchall()
    out = []
    for r in rows:
        out.append({
            "department": r[0],
            "baza_procent": float(r[1]),
            "obiective": get_objectives(db, r[0], tip=None),
        })
    return out


def upsert_department(db: Session, department: str, baza_procent: float,
                      obiective: list, updated_by: Optional[str] = None) -> dict:
    """Upsert config + inlocuieste setul de obiective al departamentului (toate tipurile).

    Fiecare obiectiv = un tip (email/task/apel) + o categorie optionala (NULL = general, ex.
    'cargobox' pt task-uri) -- un singur obiectiv per (tip, categorie), cu limita si pondere
    proprii. `unitate` ('minute'/'secunde') doar pt afisare -- comparatia foloseste valoarea bruta.
    """
    db.execute(
        text("INSERT INTO productivity_department_config(department, baza_procent, updated_at, updated_by) "
             "VALUES (:d, :b, now(), :u) "
             "ON CONFLICT (department) DO UPDATE SET baza_procent=:b, updated_at=now(), updated_by=:u"),
        {"d": department, "b": baza_procent, "u": updated_by},
    )
    db.execute(text("DELETE FROM productivity_objective WHERE department=:d"), {"d": department})
    for o in (obiective or []):
        if not o or o.get("limita_minute") in (None, ""):
            continue
        tip = (o.get("tip") or "email").strip().lower()
        categorie = (o.get("categorie") or "").strip().lower() or None
        pondere = o.get("pondere")
        pondere = 100 if pondere in (None, "") else float(pondere)
        unitate = (o.get("unitate") or "minute").strip().lower()
        if unitate not in ("minute", "secunde"):
            unitate = "minute"
        db.execute(
            text("INSERT INTO productivity_objective(department, tip, categorie, limita_minute, pondere, unitate) "
                 "VALUES (:d, :t, :c, :l, :p, :u)"),
            {"d": department, "t": tip, "c": categorie, "l": int(o["limita_minute"]), "p": pondere, "u": unitate},
        )
    db.commit()
    return {"department": department, "baza_procent": baza_procent, "obiective": get_objectives(db, department, tip=None)}


# ---------------------------------------------------------------- raport lunar per departament
def _month_bounds(year: int, month: int) -> tuple:
    first = _dt.date(year, month, 1)
    last = _dt.date(year, month, calendar.monthrange(year, month)[1])
    return first, last


# ---------------------------------------------------------------- business minutes — calcul Python (fara round-trip SQL per rand)

from zoneinfo import ZoneInfo as _ZoneInfo
_TZ_RO = _ZoneInfo("Europe/Bucharest")
_TZ_UTC = _ZoneInfo("UTC")


_MIN_STAFF_FOR_WORKING_DAY = 1  # zi nelucrătoare doar dacă 0 angajați prezenți; ≥1 = SLA curge

# Departamentele care masoara durata pe ACOPERIREA DEPARTAMENTULUI, nu pe tura operatorului care a
# rezolvat (vezi _dept_window). Decizie de business, 2026-08-04: clientul asteapta departamentul, nu
# persoana. Un mail intrat la 09:56 cand operatorul care il va rezolva intra in tura la 12:30 statea
# 2h34m necontorizate -- se raporta "46 min, on time" pentru o asteptare reala de 3h20m.
# Restul departamentelor rămân pe tura individuală (comportamentul anterior).
_DEPT_WINDOW_DEPARTMENTS = frozenset({
    "suport_1", "suport_2", "suport_3", "taxe_drum", "contabilitate", "recuperare_tva",
})


def _att_ts_to_local(t, day: _dt.date) -> _dt.datetime:
    """Ora de pontaj -> datetime local.

    `employee_attendance.begin_time/end_time` sunt `timestamp WITHOUT time zone` in UTC (verificat:
    tura dominanta 05:00-13:30 = 08:00-16:30 local, iar activitatea reala incepe ~08:00; tura de
    dupa-amiaza 09:30 = 12:30 local, cu 0 mailuri rezolvate inainte de 12:30 pe 408 de cazuri).
    """
    if isinstance(t, _dt.datetime):
        if t.tzinfo is None:
            return t.replace(tzinfo=_TZ_UTC).astimezone(_TZ_RO)
        return t.astimezone(_TZ_RO)
    return _dt.datetime.combine(day, t, tzinfo=_TZ_RO)


class _BizCache:
    """Cache preîncarcat pentru un interval date_from–date_to + lista departamente.

    Preîncarcă o singură dată:
      - department_schedule: {(dept, weekday): (start_time, end_time, requires_attendance)}
      - employee_attendance: {(emp_id, work_date): (present, begin_ts, end_ts)}
      - dept_day_count: {(dept, date): nr_prezenti} — prag <1 (=0) = zi nelucrătoare
    Apoi calculeaza business_minutes în Python fara niciun SELECT suplimentar.
    """

    def __init__(self, db: Session, departments: list, date_from: _dt.date, date_to: _dt.date,
                 holidays: Optional[list] = None):
        self.holidays: set = set(holidays or [])

        sched_rows = db.execute(
            text("SELECT department, weekday, start_time, end_time, requires_attendance "
                 "FROM department_schedule WHERE active=true AND department = ANY(:depts)"),
            {"depts": list(departments)},
        ).fetchall()
        self._sched: dict = {}
        for dept, wd, st, et, req_att in sched_rows:
            self._sched[(dept, int(wd))] = (st, et, bool(req_att))

        att_rows = db.execute(
            text("SELECT employee_id, department, work_date, present, begin_time, end_time "
                 "FROM employee_attendance "
                 "WHERE department = ANY(:depts) AND work_date BETWEEN :df AND :dt "
                 "AND employee_id IS NOT NULL"),
            {"depts": list(departments), "df": date_from, "dt": date_to},
        ).fetchall()
        self._att: dict = {}
        # dept_day_count: (dept, date) -> nr angajati prezenti
        self._dept_day_count: dict = {}
        # dept_day_union: (dept, date) -> (cel mai devreme inceput, cel mai tarziu final) LOCAL,
        # dintre operatorii efectiv prezenti. Folosit ca fereastra pentru departamentele fara
        # `department_schedule` (contabilitate, recuperare_tva) -- vezi _dept_window.
        self._dept_day_union: dict = {}
        for emp_id, dept, work_date, present, begin_time, end_time in att_rows:
            wd_date = work_date if isinstance(work_date, _dt.date) else _dt.date.fromisoformat(str(work_date))
            self._att[(int(emp_id), wd_date)] = (bool(present), begin_time, end_time, dept)
            if present:
                key = (dept, wd_date)
                self._dept_day_count[key] = self._dept_day_count.get(key, 0) + 1
                if begin_time is not None and end_time is not None:
                    b = _att_ts_to_local(begin_time, wd_date)
                    e = _att_ts_to_local(end_time, wd_date)
                    prev = self._dept_day_union.get(key)
                    self._dept_day_union[key] = (
                        b if prev is None or b < prev[0] else prev[0],
                        e if prev is None or e > prev[1] else prev[1],
                    )

    def is_working_day_for_dept(self, dept: str, day: _dt.date) -> bool:
        """True dacă departamentul are ≥1 angajat prezent în ziua respectivă (inclusiv sâmbăta).

        Dacă nu există nicio înregistrare de pontaj pentru (dept, day) — ex. ziua e în viitor
        sau pontajul nu a fost importat — nu penalizăm: returnăm True (zi potențial lucrătoare).
        Logica de schedule (weekday fără program = skip) rămâne separată în business_minutes().
        """
        key = (dept, day)
        if key not in self._dept_day_count:
            # Fara inregistrari de pontaj deloc pentru (dept, day): nu stim → zi potentiala
            return True
        return self._dept_day_count[key] >= _MIN_STAFF_FOR_WORKING_DAY

    def _dept_window(self, dept: str, day: _dt.date):
        """Fereastra de contorizare a departamentului pentru o zi, sau None dacă nu curge.

        Regula (2026-08-04): daca departamentul are >=1 om prezent, SLA curge pe PROGRAMUL
        DEPARTAMENTULUI, indiferent cine rezolva efectiv si in ce tura e. Clientul asteapta
        departamentul, nu persoana.
          - are `department_schedule` (suport_1/2/3, taxe_drum) -> fereastra = programul zilei
          - nu are program (contabilitate, recuperare_tva)      -> fereastra = uniunea turelor
            celor prezenti in acea zi (de la primul inceput la ultimul final)
        Zi cu 0 prezenti = nu curge deloc (se sare la ziua urmatoare).
        """
        if not self.is_working_day_for_dept(dept, day):
            return None

        sched = self._sched.get((dept, day.isoweekday()))
        if sched is not None:
            st, et, req_att = sched
            # requires_attendance: fara pontaj efectiv, ziua nu conteaza
            if req_att and (dept, day) not in self._dept_day_count:
                return None
            return (_dt.datetime.combine(day, st, tzinfo=_TZ_RO),
                    _dt.datetime.combine(day, et, tzinfo=_TZ_RO))

        # Fara program configurat: uniunea turelor reale ale celor prezenti.
        return self._dept_day_union.get((dept, day))

    def working_days_in_range(self, dept: str, date_from: _dt.date, date_to: _dt.date,
                               holidays: set) -> int:
        """Zile lucrătoare reale pentru departament: Luni–Vineri + ≥1 prezent + nu sărbătoare. Sâmbăta exclusă din count chiar dacă angajați prezenți."""
        cnt = 0
        d = date_from
        while d <= date_to:
            if _is_working_day(d, holidays) and self.is_working_day_for_dept(dept, d):
                cnt += 1
            d += _dt.timedelta(days=1)
        return cnt

    def business_minutes(self, dept: str, emp_id: int,
                         p_start, p_end,
                         holidays: Optional[list] = None) -> Optional[float]:
        """Replică logica business_minutes_emp() în Python.

        p_start / p_end: datetime-aware sau naive (tratat ca UTC dacă fără tz).
        Returnează None dacă lipsesc capetele sau intervalul e negativ/zero.
        """
        if p_start is None or p_end is None:
            return None

        def _to_ro(ts):
            if ts is None:
                return None
            if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
                return ts.astimezone(_TZ_RO)
            return ts.replace(tzinfo=_TZ_UTC).astimezone(_TZ_RO)

        def _combine_local(d, t):
            return _dt.datetime.combine(d, t, tzinfo=_TZ_RO)

        def _ts_to_local(t, day):
            if isinstance(t, _dt.datetime):
                if t.tzinfo is None:
                    return t.replace(tzinfo=_TZ_UTC).astimezone(_TZ_RO)
                return t.astimezone(_TZ_RO)
            return _dt.datetime.combine(day, t, tzinfo=_TZ_RO)

        start_local = _to_ro(p_start)
        end_local = _to_ro(p_end)
        if start_local is None or end_local is None or end_local <= start_local:
            return None

        holidays_set = set(holidays or []) | self.holidays

        total = 0.0
        day = start_local.date()
        last_day = end_local.date()
        itr = 0

        while day <= last_day and itr < 731:
            itr += 1

            if day in holidays_set:
                day += _dt.timedelta(days=1)
                continue

            # Zi cu 0 angajati prezenti in departament = zi nelucrătoare — SLA nu curge
            if not self.is_working_day_for_dept(dept, day):
                day += _dt.timedelta(days=1)
                continue

            # Departamentele din _DEPT_WINDOW_DEPARTMENTS: fereastra e a DEPARTAMENTULUI, deci nu
            # mai depinde de tura (sau absenta) operatorului care a rezolvat.
            if dept in _DEPT_WINDOW_DEPARTMENTS:
                win = self._dept_window(dept, day)
                if win is None:
                    day += _dt.timedelta(days=1)
                    continue
                work_start, work_end = win
                ov_start = max(start_local, work_start)
                ov_end = min(end_local, work_end)
                if ov_end > ov_start:
                    total += (ov_end - ov_start).total_seconds() / 60.0
                day += _dt.timedelta(days=1)
                continue

            att = self._att.get((emp_id, day))
            if att is not None:
                present, begin_time, end_time, _dept = att
                if not present:
                    day += _dt.timedelta(days=1)
                    continue
                if begin_time is not None and end_time is not None:
                    work_start = _ts_to_local(begin_time, day)
                    work_end = _ts_to_local(end_time, day)
                else:
                    sched = self._sched.get((dept, day.isoweekday()))
                    if sched is None:
                        day += _dt.timedelta(days=1)
                        continue
                    work_start = _combine_local(day, sched[0])
                    work_end = _combine_local(day, sched[1])
            else:
                sched = self._sched.get((dept, day.isoweekday()))
                if sched is None:
                    day += _dt.timedelta(days=1)
                    continue
                st, et, req_att = sched
                if req_att:
                    day += _dt.timedelta(days=1)
                    continue
                work_start = _combine_local(day, st)
                work_end = _combine_local(day, et)

            ov_start = max(start_local, work_start)
            ov_end = min(end_local, work_end)
            if ov_end > ov_start:
                total += (ov_end - ov_start).total_seconds() / 60.0

            day += _dt.timedelta(days=1)

        return total if total > 0 else 0.0


def _make_biz_cache(db: Session, departments, date_from: _dt.date, date_to: _dt.date,
                    holidays: Optional[list] = None) -> _BizCache:
    depts = list(departments) if not isinstance(departments, list) else departments
    return _BizCache(db, depts, date_from, date_to, holidays)


# ------------------------------------------------------- start & excluderi pentru mailuri (comune)
#
# START. `raw->'extra'->>'created_at'` pare momentul sosirii, dar NU e: e momentul crearii
# TICHETULUI in CTS, adica momentul in care cineva atinge mailul. Se deplaseaza inainte odata cu
# neglijarea mailului, deci intarzierea se ascunde singura -- un mail lasat 4 zile aparea rezolvat
# in 20 de minute. Caz real (email_id 54196): primit 24.07 14:42, trimis in CTS 14:45, tichet creat
# 28.07 08:36, rezolvat 29.07 06:55 => se raportau ~22h in loc de ~4.5 zile.
#   `extra.email_date` == `emails.received_at` EXACT, pe toate randurile => sursa corecta.
#   `emails.sent_to_cts_at` ar fi fost semantic mai potrivit, dar e NULL pe 33% din randuri.
# Textul din JSON e NAIV in UTC, deci trebuie marcat explicit `AT TIME ZONE 'UTC'`.
#
# `{g}` = aliasul tabelei cts_ground_truth in query-ul gazda; presupune un JOIN pe `emails e`.
_EMAIL_START_SQL = r"""COALESCE(
                       CASE WHEN {g}.raw->'extra'->>'email_date' ~ '^\d{{4}}-\d\d-\d\d'
                            THEN ({g}.raw->'extra'->>'email_date')::timestamp AT TIME ZONE 'UTC'
                       END,
                       e.received_at)"""

# EXCLUDERI. Entitatile care nu sunt clienti reali (sisteme de taxare, furnizori, pseudo-clienti
# interni) nu reprezinta munca de suport masurabila: 179 din 972 de randuri pe august 2026 (18%).
# Flag propriu `clients.productivity_exclude`, separat INTENTIONAT de `satisfaction_exclude` --
# vezi migrations/20260804_productivity_exclude_and_email_start.sql.
# Presupune `LEFT JOIN clients pex ON pex.id = e.client_id` in query-ul gazda.
_EMAIL_EXCLUDE_SQL = "COALESCE(pex.productivity_exclude, false) = false"

# CLIENT. Sursa principala e atribuirea din CTS (`extra.client_id`, ID IRIS): acolo un operator a
# decis pe cine e tichetul. `emails.client_id` e doar o DEDUCTIE din adresa expeditorului, care
# greseste cand acelasi om scrie pentru mai multe firme.
#   Caz real (email 58219/58222, august 2026): alin.vallgarden@yahoo.com e listat in
#   `clients.emails` la GETA-ALIN ROUTIER, dar CTS atribuise tichetul pe KOSMIN CARGO (IRIS 16033).
#   Se afisa GETA-ALIN. Pe august: 27 de randuri cu ambii clienti diferiti + 22 cu local necunoscut.
# `UNKNOWN CLIENT` (id local 3081) NU e o identificare -- e santinela CTS pentru "neatribuit", deci
# se trateaza ca lipsa si se cade pe deductia locala (22 de randuri pe august unde local e mai bun).
# Al treilea nivel acopera cazul in care clientul din CTS nu e sincronizat local (20 de randuri).
# Presupune `LEFT JOIN clients cx ON cx.iris_client_id = ...extra.client_id` si
# `LEFT JOIN clients c ON c.id = e.client_id` in query-ul gazda.
_EMAIL_CLIENT_SQL = (
    "COALESCE(NULLIF(CASE WHEN cx.name ILIKE '%UNKNOWN CLIENT%' THEN NULL ELSE cx.name END, ''), "
    "c.name)"
)


# ---------------------------------------------------------------- surse bottom-up per tip obiectiv
def _fetch_email_rows(db: Session, department: str, first: _dt.date, holidays: Optional[list] = None,
                      biz: Optional["_BizCache"] = None):
    """Mailuri solved (received) atribuite operatorilor dept-ului -- (op_id, categorie, mins).

    Durata (mins) = sosire->solved, calculata in Python via _BizCache (fara round-trip SQL per rand).
    Pentru startul perioadei vezi _EMAIL_START_SQL; pentru excluderi _EMAIL_EXCLUDE_SQL.
    """
    raw = db.execute(
        text(rf"""
            SELECT edm.id AS op_id,
                   lower(COALESCE(NULLIF(cgt.cts_category,''), e.ai_category)) AS categorie,
                   {_EMAIL_START_SQL.format(g='cgt')} AS p_start,
                   COALESCE(cgt.cts_solved_at, cgt.cts_reply_at) AS p_end
            FROM cts_ground_truth cgt
            JOIN employee_department_mapping edm ON lower(cgt.cts_assignee_email) = lower(edm.email)
            LEFT JOIN emails e ON e.id = cgt.email_id
            LEFT JOIN clients pex ON pex.id = e.client_id
            WHERE cgt.cts_status='solved' AND cgt.cts_direction='received'
              AND edm.department=:d AND edm.enabled=true
              AND {_EMAIL_EXCLUDE_SQL}
              AND date_trunc('month', (COALESCE(cgt.cts_solved_at, cgt.cts_solved_seen_at, cgt.cts_reply_at)
                                       AT TIME ZONE 'Europe/Bucharest'))::date = :first
        """),
        {"d": department, "first": first},
    ).fetchall()
    if biz is None:
        return [(op_id, cat, None) for op_id, cat, _, _ in raw]
    return [(op_id, cat, biz.business_minutes(department, op_id, p_start, p_end, holidays))
            for op_id, cat, p_start, p_end in raw]


# ---------------------------------------------------------------- identificarea device-ului (task-uri)
#
# Feed-ul CTS `/cts/tasks` NU trimite niciun camp de device -- payload-ul complet e:
# task_name / description / category_name / client_id / assignee_* (verificat pe raw_payload).
# Numarul de inmatriculare apare doar in TEXT (titlu sau descriere), iar pentru unele tipuri de
# task nici acolo (ex. "HU-GO: suspended device went into HU" -> descrierea e doar pozitia GPS).
# Extragem ce se poate din text; restul raman fara device pana cand IRIS expune campul in feed.
#
# Formate reale observate: B39GIN, SM11AGM, SV88ZKX, BH99CTS (numar RO), DGD022 / AKD318 / CQL177
# (cod device 3 litere + 3 cifre), 000070000398754 (IMEI, 15 cifre).
_DEVICE_PATTERNS = [
    _re.compile(r"\b[A-Z]{1,2}[- ]?\d{2,3}[- ]?[A-Z]{3}\b"),   # numar RO
    _re.compile(r"\b[A-Z]{3}\d{3}\b"),                          # cod device
    _re.compile(r"\b\d{15}\b"),                                 # IMEI
]


def _extract_device(*texts) -> Optional[str]:
    """Primul identificator de device gasit in textele date (in ordinea patternurilor).

    Ordinea conteaza: cautam intai numarul de inmatriculare in TOATE textele, abia apoi codul
    de device, apoi IMEI-ul -- altfel un titlu fara numar ar face sa castige IMEI-ul din descriere
    desi numarul exista mai jos in acelasi text.
    """
    for rx in _DEVICE_PATTERNS:
        for t in texts:
            if not t:
                continue
            m = rx.search(str(t))
            if m:
                return m.group(0).strip()
    return None


_TASK_FAMILY_KEYWORDS = ("cargobox", "bgtoll", "etoll", "hugo")

# task_type exact care intra in fiecare familie -- restul (BGToll: sub balance etc.) merg la general.
# Se aplica DOAR pentru departamentele din _TASK_FAMILY_EXACT_DEPTS (acum: taxe_drum).
# Alte departamente (ex. suport_1 cu cargobox) pastreaza comportamentul ILIKE original.
_TASK_FAMILY_EXACT = {
    "bgtoll":   "BGToll: New device installed",
    "etoll":    "EToll: New device installed",
    "hugo":     "HU-GO: New device installated",  # typo CTS intentionat, asa e in DB
    "cargobox": "carGObox new device installed",
}
_TASK_FAMILY_EXACT_DEPTS = {"taxe_drum"}


def _fetch_task_rows(db: Session, department: str, first: _dt.date, categorie: Optional[str],
                      holidays: Optional[list] = None, biz: Optional["_BizCache"] = None,
                      has_family_split: bool = True):
    """Task-uri solved/closed atribuite operatorilor dept-ului -- (op_id, mins).

    Durata (mins) calculata in Python via _BizCache (fara round-trip SQL per rand).

    has_family_split: daca departamentul are obiective task separate per familie
    (cargobox/bgtoll/etoll/hugo, ex. taxe_drum, suport_1), obiectivul general exclude
    familiile ca sa nu se numere de doua ori. Daca departamentul are un SINGUR obiectiv
    task general (fara nicio categorie family, ex. contabilitate/recuperare_tva/suport_2),
    nu exista risc de dublare -- obiectivul general trebuie sa ia TOATE task-urile.

    Familiile (bgtoll/etoll/hugo/cargobox) numara DOAR task_type-ul exact din _TASK_FAMILY_EXACT
    (ex. "BGToll: New device installed"), DAR NUMAI pentru departamentele din _TASK_FAMILY_EXACT_DEPTS
    (acum: taxe_drum). Celelalte departamente (ex. suport_1/cargobox) pastreaza ILIKE original.
    """
    use_exact = department in _TASK_FAMILY_EXACT_DEPTS
    norm = "regexp_replace(lower(ctgt.task_type), '[^a-z0-9]', '', 'g')"
    fam = categorie if categorie in _TASK_FAMILY_KEYWORDS else None
    params: dict = {"d": department, "first": first}
    if fam is not None:
        if use_exact:
            fam_filter = "ctgt.task_type = :fam_exact_type"
            params["fam_exact_type"] = _TASK_FAMILY_EXACT[fam]
        else:
            fam_filter = f"{norm} LIKE '%{fam}%'"
    elif has_family_split:
        if use_exact:
            # exclude exact task_type-urile de familie; restul (BGToll: sub balance etc.) raman in general
            exact_vals = list(_TASK_FAMILY_EXACT.values())
            placeholders = ", ".join(f":ex{i}" for i in range(len(exact_vals)))
            for i, v in enumerate(exact_vals):
                params[f"ex{i}"] = v
            fam_filter = f"ctgt.task_type NOT IN ({placeholders})"
        else:
            fam_filter = " AND ".join(f"{norm} NOT LIKE '%{kw}%'" for kw in _TASK_FAMILY_KEYWORDS)
    else:
        fam_filter = "TRUE"
    raw = db.execute(
        text(f"""
            SELECT ctgt.assignee_employee_id AS op_id,
                   ctgt.cts_created_at AS p_start,
                   ctgt.cts_updated_at AS p_end
            FROM cts_task_ground_truth ctgt
            JOIN employee_department_mapping edm ON edm.id = ctgt.assignee_employee_id
            WHERE edm.department=:d AND edm.enabled=true
              AND lower(ctgt.status) = 'solved'
              AND ctgt.cts_created_at IS NOT NULL AND ctgt.cts_updated_at IS NOT NULL
              AND {fam_filter}
              AND date_trunc('month', ctgt.cts_updated_at AT TIME ZONE 'Europe/Bucharest')::date = :first
        """),
        params,
    ).fetchall()
    if biz is None:
        return [(op_id, None) for op_id, _, _ in raw]
    return [(op_id, biz.business_minutes(department, op_id, p_start, p_end, holidays))
            for op_id, p_start, p_end in raw]


def _fetch_device_ops_rows(db: Session, department: str, first: _dt.date, categorie: Optional[str],
                            holidays: Optional[list] = None, biz: Optional["_BizCache"] = None):
    """Operatiuni Suport 2 inchise (closed_by whitelist) -- (op_id, mins).

    p_start=finished_at (montator a terminat), p_end=closed_at (Suport 2 a inchis).
    Whitelist-ul (nu department) e sursa de adevar -- Zoltan Tyepak are department oficial
    suport_3 dar e inclus explicit in whitelist-ul de sincronizare.
    """
    if department != "suport_2":
        return []
    raw = db.execute(
        text("""
            SELECT edo.closed_by_employee_id AS op_id,
                   edo.finished_at AS p_start,
                   edo.closed_at AS p_end
            FROM device_operations edo
            WHERE edo.closed_by_employee_id IS NOT NULL
              AND edo.action_type = :cat
              AND edo.finished_at IS NOT NULL AND edo.closed_at IS NOT NULL
              AND date_trunc('month', edo.closed_at AT TIME ZONE 'Europe/Bucharest')::date = :first
        """),
        {"cat": categorie, "first": first},
    ).fetchall()
    if biz is None:
        return [(op_id, None) for op_id, _, _ in raw]
    return [(op_id, biz.business_minutes(department, op_id, p_start, p_end, holidays))
            for op_id, p_start, p_end in raw]


# ---------------------------------------------------------------- canalul APEL (sursa While1)
#
# SURSA. Apelurile se iau din ingestul While1 (`calls`), NU din `cts_calls_ground_truth`.
# Motivul (decizie business owner, 2026-08-13): CTS logheaza doar apelurile devenite tichet,
# adica circa 15% din cele primite -- pe 12.08, While1 are 470 de apeluri primite la nivel de
# firma fata de 272 in total in CTS; pentru Oana Lasca, 16 primite pe 10.08 in While1 fata de 2
# in CTS. Raportul de productivitate arata astfel volumul real, acelasi cu pagina Apeluri.
#
# DIRECTIE. Se numara DOAR apelurile PRIMITE ('inbound'). Un apel dat de operator nu are timp de
# asteptare al clientului, deci SLA-ul nu masoara nimic acolo. Pe sursa veche distorsiunea era
# mare: contabilitate/august avea 78 de apeluri iesite in scor fata de 18 primite.
#
# ATRIBUIRE. `calls.agent_extension` contine NUMELE agentului (`user_fullname` din While1,
# ex. "Oana Lasca"). Nu exista email in CDR, deci legatura cu angajatul se face in doua trepte:
#
#   1. INVATATA din suprapunerea cu CTS (`agent_map` mai jos): pentru apelurile care au si rand
#      in `cts_calls_ground_truth` stim `cts_assignee_email`, deci si angajatul. Pastram, per
#      nume de agent, angajatul dominant. E singura metoda care rezista schimbarilor de nume:
#      angajatul 25 are `name` = "Buse Angelica-Adriana" dar `email` = adriana.brasovean@..., iar
#      While1 il raporteaza ca "Adriana Brasovean" -- fara treapta asta, tot volumul de apeluri
#      al departamentului taxe_drum (35 primite pe august) ramanea neatribuit.
#   2. FALLBACK pe potrivirea de nume, pentru agentii care nu au inca niciun apel in CTS:
#      numele din While1 trebuie sa fie inclus, ca set de tokenuri, in numele nostru
#      ("Oana Lasca" ⊆ "Lasca Oana-Maria"). Verificat pe cei 32 de agenti distincti: 30 potriviti
#      unic, 0 coliziuni.
#
# Maparea se recalculeaza la fiecare interogare, deci un agent nou intra automat imediat ce unul
# din apelurile lui ajunge si in CTS -- nu exista tabel de intretinut manual.
_APEL_AGENT_CTE = """
    WITH agent_map AS (
        SELECT agent_extension, employee_id FROM (
            SELECT c2.agent_extension, e2.id AS employee_id,
                   ROW_NUMBER() OVER (PARTITION BY c2.agent_extension
                                      ORDER BY COUNT(*) DESC, e2.id) AS rn
            FROM calls c2
            JOIN cts_calls_ground_truth g2 ON g2.call_local_id = c2.id
            JOIN employee_department_mapping e2
              ON lower(e2.email) = lower(g2.cts_assignee_email)
            WHERE c2.agent_extension IS NOT NULL AND e2.enabled = true
            GROUP BY 1, 2
        ) t WHERE rn = 1
    )
"""
#
# MASURABIL. SLA-ul de apel = timpul pana la raspuns. `calls.ring_seconds` exista din migrarea
# 20260813c, dar e NULL pe randurile ingerate inainte; pentru acelea se cade pe
# `cts_response_seconds` din suprapunerea cu CTS. Un apel PRIMIT fara niciuna din valori (tipic
# 'NO ANSWER' -- pe 10.08 Oana avea 10 din 16 asa) intra in VOLUM, dar nu in procentul "in timp":
# un apel la care nu s-a raspuns nu are timp de raspuns. Exact regula generala a motorului
# (`total` vs `measurable`).
# _APEL_AGENT_JOIN aduce si randul CTS al apelului (`g`), folosit atat pentru timpul de raspuns
# (vezi _APEL_MINS_SQL) cat si ca ultima treapta de atribuire.
_APEL_AGENT_JOIN = r"""
    -- LATERAL + LIMIT 1: 10 apeluri au DOUA randuri in cts_calls_ground_truth, iar un LEFT JOIN
    -- simplu le-ar numara de doua ori. Se prefera randul care are timp de raspuns.
    LEFT JOIN LATERAL (
        SELECT g2.cts_assignee_email, g2.cts_response_seconds, g2.cts_category, g2.raw
        FROM cts_calls_ground_truth g2
        WHERE g2.call_local_id = c.id
        ORDER BY (g2.cts_response_seconds IS NULL), g2.id
        LIMIT 1
    ) g ON true
    LEFT JOIN agent_map am ON am.agent_extension = c.agent_extension
    -- Potrivire pe nume, tolerantă la ordine, la prenume compuse si la o litera gresita:
    -- fiecare cuvant din numele While1 trebuie sa aiba, in numele nostru, un cuvant cu ACELASI
    -- PREFIX de 4 litere. "Oana Lasca" -> "Lasca Oana-Maria"; "Bulmai Anamaria" ->
    -- "Bulmau Anamaria-Iuliana" (While1 scrie gresit ultima litera a numelui de familie).
    -- Verificat pe toti cei 32 de agenti distincti: 32 potriviri unice, 0 coliziuni.
    --
    -- Agregatul cu CASE returneaza EXACT un rand: id-ul potrivirii cand e singura, altfel NULL.
    -- Fara el, doi angajati cu acelasi prefix (ex. "Popa Andrei" si "Popa Andreea") ar produce
    -- doua randuri pentru acelasi apel, deci volum dublat. Ambiguitatea se rezolva mai departe
    -- prin assignee-ul CTS, nu ghicind.
    LEFT JOIN LATERAL (
        SELECT CASE WHEN count(*) = 1 THEN min(e3.id) END AS id
        FROM employee_department_mapping e3
        WHERE e3.enabled = true
          AND c.agent_extension IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM unnest(regexp_split_to_array(lower(trim(c.agent_extension)), '\s+')) tok
              WHERE NOT EXISTS (
                  SELECT 1 FROM unnest(
                      regexp_split_to_array(lower(regexp_replace(e3.name, '-', ' ', 'g')), '\s+')
                  ) etok
                  WHERE left(etok, 4) = left(tok, 4)
              )
          )
    ) edm_n ON true
    -- Ultima treapta: assignee-ul din CTS. Se foloseste DOAR cand While1 nu a inregistrat numele
    -- agentului (`agent_extension` NULL) -- 788 de apeluri primite pe august. Ordinea conteaza:
    -- pentru "cine a raspuns", centrala telefonica e sursa de adevar, nu cine a preluat tichetul.
    LEFT JOIN employee_department_mapping edm_c
      ON edm_c.enabled = true
     AND lower(edm_c.email) = lower(g.cts_assignee_email)
    JOIN employee_department_mapping edm
      ON edm.id = COALESCE(am.employee_id, edm_n.id, edm_c.id)
     AND edm.enabled = true
"""
_APEL_MINS_SQL = "COALESCE(c.ring_seconds, g.cts_response_seconds)"

# Un rind din `calls` NU e un apel: centrala scrie cite un rind per LEG (ring paralel pe mai multe
# aparate, transfer, reapelare imediata). Pe august 2026, din 4354 randuri 'inbound': 2199
# 'NO ANSWER' de 0-1s, 16 'BUSY' si 12 'ANSWERED' de 0-1s => 2227 (51%) nu sint conversatii.
# Caz real (10.08, +37369841796, acelasi agent): 12:02:38 ANSWERED 1s, 12:03:36 ANSWERED 213s,
# 12:03:54 NO ANSWER 0s -- un singur apel vorbit, trei randuri in MG, doua aparind "neprocesate".
#
# De ce conteaza pentru SCOR, nu doar pentru volum: un leg fara conversatie nu are timp de raspuns
# real, dar `backfill_ring_seconds` completeaza `ring_seconds` pe TOT intervalul (nu doar pe apelurile
# raspunse), deci legs-urile moarte devin "masurabile" cu ~0s -- adica "on time" gratuit care umfla
# procentul departamentului.
#
# Verificat pe august 2026 dupa filtrul de mai jos: 0 legs suprapuse din 2127 randuri si doar 14
# perechi la sub 120s intr-o luna intreaga (reapelari reale, nu duplicate) => nu e nevoie de
# deduplicare pe `linkedid`; filtrul singur elimina dublurile semnalate.
_APEL_REAL_CALL_SQL = "c.call_status = 'ANSWERED' AND COALESCE(c.duration_seconds, 0) > 1"

# `calls.started_at` e `timestamp WITHOUT time zone` si contine ora LOCALA RO exact cum o scrie
# centrala (While1 `time`; verificat: call_id 1346015 e 12:02:38 in centrala si 12:02:38 in DB).
# Deci NU se converteste: un `AT TIME ZONE 'Europe/Bucharest'` peste el il muta cu 3h si, pe un
# Postgres cu sesiunea pe UTC, schimba si ziua apelurilor din primele ore ale dimineții. Pagina
# Apeluri filtreaza deja direct pe `c.started_at`, deci asa cele doua pagini spun acelasi lucru.
_APEL_DAY_SQL = "c.started_at::date"

# „Azi" pentru apeluri, independent de `TimeZone`-ul sesiunii Postgres: `started_at` e ora locala
# RO, deci ziua curenta trebuie citita si ea in RO. Cu `CURRENT_DATE` pe un server cu sesiunea pe
# UTC, intre 00:00 si 03:00 RO monitorul ar arata inca ziua precedenta.
_APEL_TODAY_RO_SQL = "(now() AT TIME ZONE 'Europe/Bucharest')::date"

# APEL PIERDUT. NU e "orice rind NO ANSWER": centrala suna in paralel pe mai multe aparate, deci
# leg-urile nepreluate apar 'NO ANSWER' chiar si cind apelul a fost de fapt raspuns pe alt aparat.
# Pe august 2026: 2215 randuri NO ANSWER/BUSY, dar doar 659 apeluri efectiv pierdute.
# Definitie: un leg neraspuns pentru care acelasi numar NU are nici un apel raspuns in +/-15 min.
# Fereastra acopera si reapelarea imediata a clientului (cazul obisnuit: suna, nu prinde, suna iar).
_APEL_LOST_CALL_SQL = """NOT EXISTS (
        SELECT 1 FROM calls a
        WHERE a.direction = 'inbound'
          AND a.caller_number = c.caller_number
          AND a.call_status = 'ANSWERED' AND COALESCE(a.duration_seconds, 0) > 1
          AND a.started_at BETWEEN c.started_at - INTERVAL '15 minutes'
                               AND c.started_at + INTERVAL '15 minutes'
    )"""

# Leg neraspuns (inclusiv BUSY) — complementul lui `_APEL_REAL_CALL_SQL` pe acelasi rind.
_APEL_UNANSWERED_SQL = "NOT (c.call_status = 'ANSWERED' AND COALESCE(c.duration_seconds, 0) > 1)"

# Varianta cu LEFT JOIN pe angajat, pentru LISTE (pagina Apeluri), nu pentru calcul: acolo un apel
# neatribuibil trebuie sa ramina vizibil, cu operator gol, nu sa dispara din tabel. Derivata din
# aceeasi constanta ca sa nu existe doua reguli de atribuire care pot diverge in timp.
_APEL_AGENT_JOIN_LEFT = _APEL_AGENT_JOIN.replace(
    "    JOIN employee_department_mapping edm\n      ON edm.id = COALESCE",
    "    LEFT JOIN employee_department_mapping edm\n      ON edm.id = COALESCE")
# Daca forma JOIN-ului se schimba, replace-ul devine no-op si lista ar taceti apelurile
# neatribuite. Preferam sa crape la import decit sa minta in UI.
assert _APEL_AGENT_JOIN_LEFT != _APEL_AGENT_JOIN, \
    "_APEL_AGENT_JOIN si-a schimbat forma — actualizeaza _APEL_AGENT_JOIN_LEFT"


def _fetch_apel_rows(db: Session, department: str, first: _dt.date):
    """Apeluri PRIMITE ale operatorilor dept-ului -- (op_id, mins), unde `mins` e de fapt SECUNDE
    (timpul pana la raspuns).

    Sursa e `calls` (While1) — aceeasi pe care o arata pagina Apeluri; vezi nota de mai sus pentru
    directie, atribuire si masurabilitate. Se numara doar conversatiile reale
    (`_APEL_REAL_CALL_SQL`): legs-urile de ring/transfer de 0-1s nu sint apeluri.
    Ziua trebuie sa fie una in care departamentul era activ -- filtru hibrid:
    pontaj (department_attendance) > program (department_schedule) > backward compat.
    Daca departamentul nu are niciun program configurat (department_schedule gol): includem tot."""
    return db.execute(
        text(f"""
            {_APEL_AGENT_CTE}
            SELECT edm.id AS op_id, {_APEL_MINS_SQL} AS mins
            FROM calls c
            {_APEL_AGENT_JOIN}
            LEFT JOIN department_attendance da
                ON da.department = edm.department
                AND da.work_date = {_APEL_DAY_SQL}
            LEFT JOIN department_schedule ds
                ON ds.department = edm.department
                AND ds.weekday = EXTRACT(ISODOW FROM c.started_at)::int
                AND ds.active = true
            LEFT JOIN (
                SELECT DISTINCT department, true AS configured
                FROM department_schedule WHERE active = true
            ) dsc ON dsc.department = edm.department
            WHERE edm.department=:d
              AND c.direction = 'inbound'
              AND {_APEL_REAL_CALL_SQL}
              AND date_trunc('month', c.started_at)::date = :first
              AND (
                CASE
                  WHEN da.work_date IS NOT NULL THEN da.present = true
                  WHEN ds.weekday IS NOT NULL THEN true
                  WHEN dsc.configured IS NULL THEN true
                  ELSE false
                END
              )
        """),
        {"d": department, "first": first},
    ).fetchall()


# ---------------------------------------------------------------- reclamatii (Quality Evaluation)
#
# SURSA: `cts_quality_evaluation`, oglinda view-ului IRIS DV (vezi quality_eval_sync.py).
#
# ATRIBUIRE (decizie business owner, 2026-08-13): TOATE reclamatiile intra la Suport 3, indiferent
# de departamentul persoanei evaluate. `department_id` din CTS ramane folosit doar pentru AFISARE
# (Monitorul Operational le arata per departament); productivitatea le pune integral pe Suport 3,
# care e echipa ce le proceseaza.
#
# CELE DOUA SLA-URI, ambele pornind din momentul inregistrarii reclamatiei:
#   contact      created_at -> in_progress_at   (preluarea)
#   solutionare  created_at -> solved_at        (inchiderea)
# O reclamatie neajunsa inca in starea respectiva nu e masurabila: intra in volum, nu in procent.
# Nu exista `in_progress_at` daca CTS a trecut-o direct in 'solved' (17 din 123 pe 01.07-13.08).
#
# Durata se masoara in minute de PROGRAM, ca la mailuri si task-uri (business_minutes), ca sa nu
# curga noaptea si in weekend.
_RECLAMATIE_CATEGORII = {
    "contact":     "in_progress_at",
    "solutionare": "solved_at",
}


def _fetch_reclamatie_rows(db: Session, department: str, first: _dt.date, categorie: Optional[str],
                            holidays: Optional[list] = None, biz: Optional["_BizCache"] = None):
    """Reclamatii CTS -- (op_id, mins). Vezi nota de mai sus pentru atribuire si SLA.

    `op_id` = operatorul Suport 3 caruia i se atribuie. Cand departamentul are un singur angajat
    activ (cazul de azi: Tyepak Zoltan), toate randurile merg la el. Daca sint mai multi, se
    incearca intai persoana care a schimbat efectiv statusul (`updated_by`, tradus prin
    cts_dv_employee.admin_id -> email), si abia apoi se cade pe primul operator al echipei.
    """
    end_col = _RECLAMATIE_CATEGORII.get((categorie or "").strip().lower())
    if end_col is None:
        return []

    rows = db.execute(
        text(f"""
            SELECT COALESCE(edm_u.id, edm_any.id) AS op_id,
                   qe.created_at AS p_start,
                   qe.{end_col}  AS p_end
            FROM cts_quality_evaluation qe
            -- Persoana care a miscat reclamatia, daca e din departamentul scorat.
            -- LATERAL + LIMIT 1 e obligatoriu: `cts_dv_employee` e oglinda bruta a DV-ului si are
            -- acelasi `admin_id` pe mai multe randuri (contracte succesive ale aceleiasi persoane
            -- -- ex. admin_id 176, 209, 14, 88), deci un JOIN simplu ar numara reclamatia de mai
            -- multe ori.
            LEFT JOIN LATERAL (
                SELECT e1.id FROM cts_dv_employee dv1
                JOIN employee_department_mapping e1 ON lower(e1.email) = lower(dv1.email)
                WHERE dv1.admin_id = qe.updated_by::text
                  AND e1.enabled = true AND e1.department = :d
                ORDER BY e1.id LIMIT 1
            ) edm_u ON true
            -- altfel: echipa care proceseaza reclamatiile (un singur rand cand e un singur om)
            LEFT JOIN LATERAL (
                SELECT e2.id FROM employee_department_mapping e2
                WHERE e2.department = :d AND e2.enabled = true
                ORDER BY e2.id LIMIT 1
            ) edm_any ON true
            WHERE qe.deleted_at IS NULL
              AND qe.{end_col} IS NOT NULL
              AND date_trunc('month', qe.{end_col} AT TIME ZONE 'Europe/Bucharest')::date = :first
        """),
        {"d": department, "first": first},
    ).fetchall()
    if biz is None:
        return [(r[0], None) for r in rows]
    return [(op_id, biz.business_minutes(department, op_id, p_start, p_end, holidays))
            for op_id, p_start, p_end in rows]


def _bd_status(mins: Optional[float], limit: Optional[float], allow_zero: bool = False) -> str:
    """Statusul unui rand din breakdown, cu EXACT regula folosita in _accumulate.

    0 minute de program => on_time (rezolvat in afara orelor de lucru, vezi _measurable).
    Randul e marcat separat cu `in_afara_programului` ca sa se vada in lista de ce apare
    „On time" cu 0 minute afisate."""
    meas = (mins is not None) if allow_zero else _measurable(mins)
    if not meas:
        return "nemasurat"
    if limit is None:
        return "nemasurat"
    return "on_time" if float(mins) <= float(limit) else "overdue"


def breakdown_rows(db: Session, department: str, tip: str, first: _dt.date,
                   categorie: Optional[str], limita: Optional[float]) -> list:
    """Randurile brute care intra in calculul unui obiectiv, cu durata si status per rand.

    Oglindeste exact sursele si filtrele din `_fetch_*_rows` (aceleasi WHERE, aceeasi
    conventie de luna pe fusul Europe/Bucharest, aceeasi durata via `_BizCache`), ca
    numarul de randuri de aici sa coincida cu volumul raportat pe pagina de Productivitate.
    """
    holidays = get_holidays(db)
    holidays_arr = _holidays_date_list(holidays)
    last = _month_bounds(first.year, first.month)[1]
    biz = _make_biz_cache(db, [department], first, last, holidays_arr)

    def _iso(ts):
        if ts is None:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_TZ_UTC)
        return ts.astimezone(_TZ_RO).isoformat()

    def _iso_ro_naive(ts):
        """Pentru `calls.started_at`: naiv, DEJA in ora locala RO (vezi _APEL_DAY_SQL).

        `_iso` l-ar marca UTC si l-ar muta cu 3h, deci acelasi apel apărea la 12:02 pe pagina
        Apeluri si la 15:02 in lista din Productivitate.
        """
        if ts is None:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_TZ_RO)
        return ts.astimezone(_TZ_RO).isoformat()

    out = []

    if tip == "email":
        raw = db.execute(
            text(rf"""
                SELECT edm.id AS op_id, edm.name AS op_name, g.id AS row_id, g.email_id,
                       lower(COALESCE(NULLIF(g.cts_category,''), e.ai_category)) AS categorie,
                       COALESCE(NULLIF(e.subject,''), g.raw->'extra'->>'title') AS subiect,
                       COALESCE({_EMAIL_CLIENT_SQL}, e.from_name, e.from_address) AS client,
                       {_EMAIL_START_SQL.format(g='g')} AS p_start,
                       COALESCE(g.cts_solved_at, g.cts_reply_at) AS p_end
                FROM cts_ground_truth g
                JOIN employee_department_mapping edm ON lower(g.cts_assignee_email) = lower(edm.email)
                LEFT JOIN emails e ON e.id = g.email_id
                LEFT JOIN clients c ON c.id = e.client_id
                -- `extra.client_id` e ID din IRIS => join pe iris_client_id, nu pe cheia locala
                -- (vezi nota de la ramura `apel`).
                LEFT JOIN clients cx ON cx.iris_client_id = NULLIF(g.raw->'extra'->>'client_id','')::bigint
                LEFT JOIN clients pex ON pex.id = e.client_id
                WHERE g.cts_status='solved' AND g.cts_direction='received'
                  AND edm.department=:d AND edm.enabled=true
                  AND {_EMAIL_EXCLUDE_SQL}
                  AND date_trunc('month', (COALESCE(g.cts_solved_at, g.cts_solved_seen_at, g.cts_reply_at)
                                           AT TIME ZONE 'Europe/Bucharest'))::date = :first
            """),
            {"d": department, "first": first},
        ).fetchall()
        # Obiectivul general ia tot; unul per-categorie ia doar categoria lui (ca _in_scope/_limit_for)
        for r in raw:
            if categorie is not None and (r.categorie or None) != categorie:
                continue
            mins = biz.business_minutes(department, r.op_id, r.p_start, r.p_end, holidays_arr)
            out.append({
                "id": r.row_id, "email_id": r.email_id,
                "op_id": r.op_id, "solved_by": r.op_name,
                "client": r.client, "subiect": r.subiect, "categorie": r.categorie,
                "created_at": _iso(r.p_start), "solved_at": _iso(r.p_end),
                "durata": round(mins, 1) if mins is not None else None,
                "status": _bd_status(mins, limita),
                "in_afara_programului": (mins == 0),
            })

    elif tip == "task":
        task_objs = get_objectives(db, department, tip="task")
        has_family_split = any(o.get("categorie") for o in task_objs)
        use_exact = department in _TASK_FAMILY_EXACT_DEPTS
        norm = "regexp_replace(lower(t.task_type), '[^a-z0-9]', '', 'g')"
        fam = categorie if categorie in _TASK_FAMILY_KEYWORDS else None
        task_params: dict = {"d": department, "first": first}
        if fam is not None:
            if use_exact:
                fam_filter = "t.task_type = :fam_exact_type"
                task_params["fam_exact_type"] = _TASK_FAMILY_EXACT[fam]
            else:
                fam_filter = f"{norm} LIKE '%{fam}%'"
        elif has_family_split:
            if use_exact:
                exact_vals = list(_TASK_FAMILY_EXACT.values())
                placeholders = ", ".join(f":ex{i}" for i in range(len(exact_vals)))
                for i, v in enumerate(exact_vals):
                    task_params[f"ex{i}"] = v
                fam_filter = f"t.task_type NOT IN ({placeholders})"
            else:
                fam_filter = " AND ".join(f"{norm} NOT LIKE '%{kw}%'" for kw in _TASK_FAMILY_KEYWORDS)
        else:
            fam_filter = "TRUE"
        raw = db.execute(
            text(f"""
                SELECT t.assignee_employee_id AS op_id, edm.name AS op_name, t.id AS row_id,
                       t.task_type, t.title AS subiect, t.description AS descriere,
                       cl.name AS client,
                       t.cts_created_at AS p_start, t.cts_updated_at AS p_end
                FROM cts_task_ground_truth t
                JOIN employee_department_mapping edm ON edm.id = t.assignee_employee_id
                -- `cts_task_ground_truth.client_id` e ID din IRIS (ca la apeluri/mailuri), deci
                -- join pe `iris_client_id`, NU pe cheia primara locala. Bug pana la v0.76.0:
                -- 1956 din 2210 de task-uri pe august 2026 (88%) afisau alt client, tacut --
                -- numerele se suprapun, deci join-ul gresit nu dadea NULL, ci alt rand.
                -- `cts_tasks_training.py` folosea deja corect `iris_client_id`.
                LEFT JOIN clients cl ON cl.iris_client_id = t.client_id
                WHERE edm.department=:d AND edm.enabled=true
                  AND lower(t.status) = 'solved'
                  AND t.cts_created_at IS NOT NULL AND t.cts_updated_at IS NOT NULL
                  AND {fam_filter}
                  AND date_trunc('month', t.cts_updated_at AT TIME ZONE 'Europe/Bucharest')::date = :first
            """),
            task_params,
        ).fetchall()
        for r in raw:
            mins = biz.business_minutes(department, r.op_id, r.p_start, r.p_end, holidays_arr)
            out.append({
                "id": r.row_id, "op_id": r.op_id, "solved_by": r.op_name,
                "client": r.client, "subiect": r.subiect, "categorie": r.task_type,
                # Numarul de device nu vine ca atare din CTS -- se extrage din titlu/descriere
                # (vezi _extract_device). `descriere` ramane expusa integral ca sa se poata
                # identifica task-ul si acolo unde textul nu contine un numar recunoscut.
                "device": _extract_device(r.subiect, r.descriere),
                "descriere": r.descriere,
                "created_at": _iso(r.p_start), "solved_at": _iso(r.p_end),
                "durata": round(mins, 1) if mins is not None else None,
                "status": _bd_status(mins, limita),
                "in_afara_programului": (mins == 0),
            })

    elif tip == "device_ops":
        if department != "suport_2":
            return []
        raw = db.execute(
            text("""
                SELECT d.closed_by_employee_id AS op_id, edm.name AS op_name, d.id AS row_id,
                       d.action_type, d.description AS subiect,
                       d.device_serial AS device, d.device_imei AS device_imei,
                       -- `device_operations.client_id` e NULL pe toate randurile (view-ul DV nu-l
                       -- trimite), deci join-ul pe clients nu returna niciodata nimic si coloana
                       -- Client aparea goala. `client_name` vine din DV si e populat -- il folosim
                       -- ca sursa, pastrand join-ul ca prioritate pentru cand campul se va popula.
                       COALESCE(cl.name, cx.name, d.client_name) AS client,
                       d.finished_at AS p_start, d.closed_at AS p_end
                FROM device_operations d
                LEFT JOIN employee_department_mapping edm ON edm.id = d.closed_by_employee_id
                LEFT JOIN clients cl ON cl.id = d.client_id
                LEFT JOIN clients cx ON cx.iris_client_id = d.client_id
                WHERE d.closed_by_employee_id IS NOT NULL
                  AND d.action_type = :cat
                  AND d.finished_at IS NOT NULL AND d.closed_at IS NOT NULL
                  AND date_trunc('month', d.closed_at AT TIME ZONE 'Europe/Bucharest')::date = :first
            """),
            {"cat": categorie, "first": first},
        ).fetchall()
        for r in raw:
            mins = biz.business_minutes(department, r.op_id, r.p_start, r.p_end, holidays_arr)
            out.append({
                "id": r.row_id, "op_id": r.op_id, "solved_by": r.op_name,
                "client": r.client, "subiect": r.subiect, "categorie": r.action_type,
                "device": r.device, "device_imei": r.device_imei,
                "created_at": _iso(r.p_start), "solved_at": _iso(r.p_end),
                "durata": round(mins, 1) if mins is not None else None,
                "status": _bd_status(mins, limita),
                "in_afara_programului": (mins == 0),
            })

    elif tip == "reclamatie":
        end_col = _RECLAMATIE_CATEGORII.get((categorie or "").strip().lower())
        if end_col is None:
            return []
        # Aceleasi surse si filtre ca `_fetch_reclamatie_rows`, plus cimpurile de identificare:
        # cine a fost evaluat, pe ce lucrare, si daca reclamatia a iesit fondata.
        raw = db.execute(
            text(f"""
                SELECT qe.id AS row_id,
                       COALESCE(edm_u.id, edm_any.id) AS op_id,
                       COALESCE(edm_any.name, edm_u.name) AS op_name,
                       cl.name AS client,
                       qe.entity AS entity, qe.entity_id AS entity_id,
                       qe.observations AS observations,
                       qe.is_according_to_the_procedure AS conform,
                       edm_r.name AS evaluat,
                       edm_r.department AS dept_evaluat,
                       qe.created_at AS p_start,
                       qe.{end_col}  AS p_end,
                       -- Ambele praguri, indiferent de categoria scorata: in lista trebuie sa se
                       -- vada tot lantul (inregistrata -> preluata -> solutionata) si cite minute
                       -- de PROGRAM s-au scurs pina la fiecare, nu doar cel care da statusul.
                       qe.in_progress_at AS contact_at,
                       qe.solved_at      AS solutionat_at
                FROM cts_quality_evaluation qe
                -- Toate rezolvarile de admin_id trec prin LATERAL + LIMIT 1: `cts_dv_employee`
                -- are acelasi admin_id (si acelasi email) pe mai multe randuri, deci un JOIN
                -- simplu ar dubla reclamatia in lista fata de calcul.
                LEFT JOIN LATERAL (
                    SELECT e1.id, e1.name FROM cts_dv_employee dv1
                    JOIN employee_department_mapping e1 ON lower(e1.email) = lower(dv1.email)
                    WHERE dv1.admin_id = qe.updated_by::text
                      AND e1.enabled = true AND e1.department = :d
                    ORDER BY e1.id LIMIT 1
                ) edm_u ON true
                LEFT JOIN LATERAL (
                    SELECT e2.id, e2.name FROM employee_department_mapping e2
                    WHERE e2.department = :d AND e2.enabled = true
                    ORDER BY e2.id LIMIT 1
                ) edm_any ON true
                -- persoana EVALUATA (responsible_id = admin din CTS), pentru context in lista
                LEFT JOIN LATERAL (
                    SELECT e3.name, e3.department FROM cts_dv_employee dv3
                    JOIN employee_department_mapping e3 ON lower(e3.email) = lower(dv3.email)
                    WHERE dv3.admin_id = qe.responsible_id::text
                    ORDER BY e3.id LIMIT 1
                ) edm_r ON true
                LEFT JOIN clients cl ON cl.iris_client_id = qe.client_id
                WHERE qe.deleted_at IS NULL
                  AND qe.{end_col} IS NOT NULL
                  AND date_trunc('month', qe.{end_col} AT TIME ZONE 'Europe/Bucharest')::date = :first
            """),
            {"d": department, "first": first},
        ).fetchall()
        _ENTITY_LABEL = {"client_contact_email_log": "email", "task": "task",
                         "client_call_log": "apel"}
        for r in raw:
            mins = biz.business_minutes(department, r.op_id, r.p_start, r.p_end, holidays_arr)
            # Ambele durate se masoara DIN MOMENTUL INREGISTRARII (created_at), nu una din alta:
            # contact = inregistrare -> preluare, solutionare = inregistrare -> inchidere. Asa e
            # definit SLA-ul, deci timpul de solutionare il include pe cel de contact.
            mins_contact = (biz.business_minutes(department, r.op_id, r.p_start, r.contact_at, holidays_arr)
                            if r.contact_at else None)
            mins_solutionare = (biz.business_minutes(department, r.op_id, r.p_start, r.solutionat_at, holidays_arr)
                                if r.solutionat_at else None)
            # is_according_to_the_procedure: 1 = s-a respectat procedura => NEFONDATA.
            fondata = (r.conform == 0)
            out.append({
                "id": r.row_id, "op_id": r.op_id, "solved_by": r.op_name,
                "client": r.client,
                "subiect": (r.observations or "").strip() or None,
                "categorie": _ENTITY_LABEL.get(r.entity, r.entity),
                "evaluat": r.evaluat, "dept_evaluat": r.dept_evaluat,
                "fondata": fondata,
                "created_at": _iso(r.p_start), "solved_at": _iso(r.p_end),
                # Lantul complet, pentru verificare vizuala in modal. `solved_at` ramine capatul
                # categoriei scorate (el da statusul on time / overdue).
                "contact_at": _iso(r.contact_at),
                "solutionat_at": _iso(r.solutionat_at),
                "durata_contact": round(mins_contact, 1) if mins_contact is not None else None,
                "durata_solutionare": round(mins_solutionare, 1) if mins_solutionare is not None else None,
                "durata": round(mins, 1) if mins is not None else None,
                "status": _bd_status(mins, limita),
                "in_afara_programului": (mins == 0),
            })

    elif tip == "apel":
        # Aceeasi sursa si aceleasi filtre ca `_fetch_apel_rows` (While1, doar 'inbound',
        # atribuire pe numele agentului), ca numarul de randuri de aici sa coincida cu volumul
        # din raport. `durata` = SECUNDE pana la raspuns, nu minute business.
        # allow_zero=True: un apel raspuns instant e o masuratoare valida (_accumulate).
        #
        # CLIENT: `calls.client_id` e cheia LOCALA (o pune `_match_client_phone` la ingest, dupa
        # numarul de telefon), deci join pe `clients.id` -- spre deosebire de feed-urile CTS, unde
        # `client_id` e ID-ul din IRIS si se joina pe `iris_client_id`.
        #
        # `solved_at` nu exista pe un apel While1; incheierea e start + durata convorbirii.
        raw = db.execute(
            text(f"""
                {_APEL_AGENT_CTE}
                SELECT edm.id AS op_id, edm.name AS op_name, c.id AS row_id,
                       {_APEL_MINS_SQL} AS secunde,
                       COALESCE(g.cts_category, c.ai_category) AS categorie,
                       COALESCE(g.raw->>'issue_name', c.ai_summary) AS subiect,
                       cl.name AS client,
                       c.started_at AS p_start,
                       c.started_at + make_interval(secs => COALESCE(c.duration_seconds, 0)) AS p_solved,
                       COALESCE(c.caller_number, '') AS telefon,
                       c.call_status AS call_status
                FROM calls c
                {_APEL_AGENT_JOIN}
                LEFT JOIN clients cl ON cl.id = c.client_id
                WHERE edm.department=:d
                  AND c.direction = 'inbound'
                  AND {_APEL_REAL_CALL_SQL}
                  AND date_trunc('month', c.started_at)::date = :first
            """),
            {"d": department, "first": first},
        ).fetchall()
        for r in raw:
            secs = float(r.secunde) if r.secunde is not None else None
            out.append({
                "id": r.row_id, "op_id": r.op_id, "solved_by": r.op_name,
                "client": r.client, "subiect": r.subiect, "categorie": r.categorie,
                "telefon": r.telefon or None,
                # Starea apelului din centrala. Lista arata acum doar conversatii reale
                # (`_APEL_REAL_CALL_SQL`), deci e mereu 'ANSWERED'; coloana rimine pentru ca un
                # apel raspuns fara timp de raspuns cunoscut (ring_seconds NULL, fara rand CTS)
                # iese tot 'nemasurat' si altfel ar parea o eroare de calcul.
                "call_status": r.call_status,
                "created_at": _iso_ro_naive(r.p_start),
                "solved_at": _iso_ro_naive(r.p_solved or r.p_start),
                "durata": round(secs, 1) if secs is not None else None,
                "status": _bd_status(secs, limita, allow_zero=True),
            })

    out.sort(key=lambda x: (x.get("solved_at") or ""), reverse=True)
    return out


def department_report(db: Session, department: str, year: int, month: int) -> dict:
    holidays = get_holidays(db)
    holidays_arr = _holidays_date_list(holidays)
    cfg = get_department_config(db, department)
    objectives = get_objectives(db, department, tip=None)
    first, last = _month_bounds(year, month)
    # Cache unic pentru calcul business_minutes in Python (elimina round-trip-uri SQL per rand)
    # _BizCache trebuie creat INAINTE de working_days_in_range (foloseste dept_day_count)
    biz = _make_biz_cache(db, [department], first, last, holidays_arr)
    # Zile lucrătoare reale = L-V + ≥2 angajati prezenti in dept + nu sărbătoare legală
    wd = biz.working_days_in_range(department, first, last, holidays)

    # 1) operatori din departament (enabled=true) — toti, inclusiv cei fara activitate luna asta
    #    Excludem angajatii cu productivity_start_date > ultima zi a lunii (inca in perioada de proba).
    ops = db.execute(
        text("SELECT id, name, COALESCE(work_hours, :wh) AS work_hours, iris_id "
             "FROM employee_department_mapping WHERE department=:d AND enabled=true "
             "AND (productivity_start_date IS NULL OR productivity_start_date <= :last_day) "
             "ORDER BY name"),
        {"d": department, "wh": _DEFAULT_WORK_HOURS, "last_day": last},
    ).fetchall()
    op_ids_all = [r[0] for r in ops]
    op_iris_ids = [int(r[3]) for r in ops if r[3] is not None]
    iris_to_edm = {int(r[3]): r[0] for r in ops if r[3] is not None}
    op_meta = {r[0]: {"name": r[1], "work_hours": int(r[2] or _DEFAULT_WORK_HOURS)} for r in ops}

    # 2) Pontaj real per operator — sursa de adevar pentru prezenta/absenta.
    #    Operator "activ in luna" = are pontaj prezent SAU absenta/concediu inregistrat in luna.
    #    Un om aflat in concediu la inceputul lunii NU e inactiv: concediul se scade din
    #    ore_disponibile, nu il sterge din ore_planificate si nici din lista de operatori.
    #    (Bug 2026-08-03: pe 03.08 pontajul avea 2 zile, iar cine era in concediu 01-03.08 avea
    #     0 zile present=true -> dispărea complet din dept, iar snapshot-ul ingheta cifra greșită.)
    #    Rămân exclusi doar cei fara NICIO urma in luna (nemapat in pontaj, angajat viitor).
    pontaj_prezent: dict = {}   # emp_id -> set[date] zile lucrate efectiv
    pontaj_absent_per_emp: dict = {}  # emp_id -> set[date] zile absente inregistrate in pontaj

    if op_ids_all:
        pa = db.execute(
            text("SELECT employee_id, work_date, present FROM employee_attendance "
                 "WHERE department=:dept AND work_date BETWEEN :first AND :last "
                 "AND employee_id IS NOT NULL AND employee_id = ANY(:ids)"),
            {"dept": department, "first": first, "last": last, "ids": op_ids_all},
        ).fetchall()
        for emp_id, work_date, present in pa:
            wd_date = work_date if isinstance(work_date, _dt.date) else _dt.date.fromisoformat(str(work_date))
            if present:
                pontaj_prezent.setdefault(emp_id, set()).add(wd_date)
            else:
                pontaj_absent_per_emp.setdefault(emp_id, set()).add(wd_date)

    # Concediu inregistrat in luna (employee_schedule + fallback DV) — un om in concediu la
    # inceputul lunii nu are inca pontaj prezent, dar e angajat activ al departamentului.
    _leave_in_month: set = set()
    if op_ids_all:
        _lm = db.execute(text("""
            SELECT DISTINCT es.employee_id
            FROM employee_schedule es
            WHERE es.employee_id = ANY(:ids)
              AND es.start_date <= :last AND es.end_date >= :first
        """), {"ids": op_ids_all, "first": first, "last": last}).fetchall()
        _leave_in_month = {int(r[0]) for r in _lm}
        _dv_exists_act = db.execute(text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='cts_dv_employee_vacation_request' LIMIT 1"
        )).fetchone()
        if _dv_exists_act and op_iris_ids:
            _dv_act = db.execute(text("""
                SELECT DISTINCT v.employee_id::int
                FROM cts_dv_employee_vacation_request v
                WHERE v.employee_id::int = ANY(:iris_ids)
                  AND v.status::int IN (1, 2)
                  AND v.deleted_at IS NULL
                  AND v.period_begin::date <= :last
                  AND v.period_end::date   >= :first
            """), {"iris_ids": op_iris_ids, "first": first, "last": last}).fetchall()
            _leave_in_month |= {iris_to_edm[r[0]] for r in _dv_act if r[0] in iris_to_edm}

    # Zile libere extra (proiecte / refurbished) declarate pe luna — tot o urma de activitate.
    _extra_act = _extra_days_per_emp(db, op_ids_all, year, month) if op_ids_all else {}

    # Operatori activi = pontaj prezent SAU absenta in pontaj SAU concediu/zile extra in luna
    op_ids = [
        oid for oid in op_ids_all
        if pontaj_prezent.get(oid)
        or pontaj_absent_per_emp.get(oid)
        or oid in _leave_in_month
        or _extra_act.get(oid)
    ]

    # ore_planificate = (zile_prezent + zile_absent) × work_hours per operator activ
    # Zile absente se numara doar din zilele lucrătoare REALE ale departamentului
    # (L-V + ≥2 prezenti + nu sărbătoare). Evita sa numaram absente de sambata/zile
    # nelucrătoare inregistrate gresit in CTS sau zile de sarbatoare neinregistrate in settings.
    working_dates_luna: set = set()
    d = first
    while d <= last:
        if _is_working_day(d, holidays) and biz.is_working_day_for_dept(department, d):
            working_dates_luna.add(d)
        d += _dt.timedelta(days=1)
    ore_planificate = 0.0
    absence_hours = 0.0
    for emp_id in op_ids:
        wh = op_meta[emp_id]["work_hours"]
        zile_prezent = pontaj_prezent.get(emp_id, set())
        # Zile absente valide = absente inregistrate in pontaj care sunt zile L-V standard SAU
        # sambete in care ACEST angajat a lucrat efectiv in alta luna (pontaj_prezent propriu).
        # Nu includem sambetele altor angajati — fiecare e planificat pe propriul program.
        zile_lucru_emp = zile_prezent | working_dates_luna  # prezentele proprii + L-V calendar
        zile_absent_pontaj = pontaj_absent_per_emp.get(emp_id, set()) & zile_lucru_emp
        ore_planificate += (len(zile_prezent) + len(zile_absent_pontaj)) * wh
        absence_hours += len(zile_absent_pontaj) * wh

    # Pastreaza compatibilitate cu codul de raportare care afiseaza leave_hours
    leave_hours = absence_hours

    ore_disponibile = max(0.0, ore_planificate - absence_hours)

    # 3) ore_planificate = calendar ideal (zile_lucratoare_cal × work_hours per operator activ).
    #    ore_disponibile = ore_plan_ideale - ore_concediu_aprobat (vacation_approved + manual, union L-V).
    #    Aceeasi logica ca forecast_report — valorile sunt comparabile intre Rapoarte si Forecast.
    #    Pontajul (ore_planificate_pontaj, absence_hours) ramane folosit DOAR pt SLA/obiectiv_atins.
    ndays_real = calendar.monthrange(year, month)[1]
    zile_lucratoare_cal = sum(
        1 for d in range(1, ndays_real + 1)
        if _is_working_day(_dt.date(year, month, d), holidays)
    )
    ore_plan_ideale = sum(op_meta[oid]["work_hours"] * zile_lucratoare_cal for oid in op_ids)

    # Concedii aprobate (+ in asteptare, tratate ca acceptate) per operator activ, union de zile L-V.
    # Sursa 1: employee_schedule cu kind='vacation_approved' sau entry_source='manual' (sync DV->ES cand iris_id e populat)
    # Sursa 2: cts_dv_employee_vacation_request status IN (1,2) (fallback cand iris_id nu e setat si sync-ul nu a rulat)
    #   status=1: in asteptare (fara approved_by) -> tratat ca acceptat la calculul ore_disponibile, cerinta 2026-07-30
    #   status=2: aprobat. status=3/4: au approved_by populat (decizie finala, non-aprobat) -> excluse.
    # Ambele surse combinate, deduplicate per zi.
    _ore_concediu_report = 0.0
    if op_ids:
        _leave_rows = db.execute(text("""
            SELECT es.employee_id, es.start_date, es.end_date
            FROM employee_schedule es
            WHERE es.employee_id = ANY(:ids)
              AND (es.kind = 'vacation_approved' OR es.entry_source = 'manual')
              AND es.start_date <= :last
              AND es.end_date   >= :first
        """), {"ids": op_ids, "first": first, "last": last}).fetchall()
        _dv_tbl_exists = db.execute(text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='cts_dv_employee_vacation_request' LIMIT 1"
        )).fetchone()
        if _dv_tbl_exists and op_iris_ids:
            _dv_rows_raw = db.execute(text("""
                SELECT v.employee_id::int, v.period_begin::date, v.period_end::date
                FROM cts_dv_employee_vacation_request v
                WHERE v.employee_id::int = ANY(:iris_ids)
                  AND v.status::int IN (1, 2)
                  AND v.deleted_at IS NULL
                  AND v.period_begin::date <= :last
                  AND v.period_end::date   >= :first
            """), {"iris_ids": op_iris_ids, "first": first, "last": last}).fetchall()
            _dv_rows = [(iris_to_edm[r[0]], r[1], r[2]) for r in _dv_rows_raw if r[0] in iris_to_edm]
            _leave_rows = list(_leave_rows) + _dv_rows
        from collections import defaultdict as _dd
        _emp_dates: dict = _dd(set)
        for _eid, _sd, _ed in _leave_rows:
            _ov_start = max(_sd, first)
            _ov_end = min(_ed, last)
            _d = _ov_start
            while _d <= _ov_end:
                if _d.isoweekday() < 6 and _d.isoformat() not in holidays:
                    _emp_dates[_eid].add(_d)
                _d += _dt.timedelta(days=1)
        # Zile libere extra (proiecte / refurbished) — se adauga peste zilele de concediu,
        # suma fiind plafonata la zilele lucratoare ale lunii, ca la concedii.
        _extra_days = _extra_days_per_emp(db, op_ids, year, month)
        for _eid in op_ids:
            _nd = len(_emp_dates.get(_eid, set())) + _extra_days.get(_eid, 0)
            _wh = op_meta[_eid]["work_hours"]
            _ore_concediu_report += min(_nd, zile_lucratoare_cal) * _wh

    leave_hours = _ore_concediu_report

    baza_procent = float(cfg["baza_procent"]) if cfg else 95.0

    # Snapshot lunar imutabil: dacă există, targetele rămân fixate indiferent de
    # concedii ulterioare, modificări de obiective sau alte schimbări de date.
    # EXCEPȚIE: lunile care nu au început încă se recalculează LIVE la fiecare accesare (vezi
    # _is_future_month) — altfel un concediu adăugat azi pentru luna viitoare nu s-ar reflecta.
    # Al doilea caz LIVE: luna în curs în primele zile lucrătoare — pontajul e încă incomplet,
    # deci un target fixat acum ar îngheța cifre greșite (vezi _snapshot_too_early).
    _future = _is_future_month(year, month) or _snapshot_too_early(year, month, holidays)
    _snap = None if _future else _get_snapshot(db, department, year, month)
    if _snap is not None:
        ore_planificate = _snap["ore_planificate"]
        ore_disponibile = _snap["ore_disponibile"]
        coeficient      = _snap["coeficient"]
        obiectiv_real   = _snap["obiectiv_real"]
        obiectiv_minim  = _snap["obiectiv_minim"]
    else:
        ore_planificate = ore_plan_ideale
        ore_disponibile = max(0.0, ore_plan_ideale - _ore_concediu_report)
        coeficient = round(baza_procent / ore_plan_ideale, 4) if ore_plan_ideale > 0 else None
        obiectiv_real = round(ore_disponibile * coeficient, 2) if coeficient is not None else baza_procent
        obiectiv_minim = round(obiectiv_real - _MINIM_DELTA, 2)
        # Persistă snapshot doar din luna în curs încolo — o lună viitoare rămâne recalculabilă.
        if not _future:
            _save_snapshot(db, department, year, month, baza_procent, zile_lucratoare_cal,
                           ore_planificate, ore_disponibile, coeficient, obiectiv_real, obiectiv_minim)

    # 4) acumulatori generici, per (tip, categorie) si per operator -- populati mai jos, cate un
    #    bloc per tip de obiectiv. Cheia __general__ marcheaza obiectivul fara categorie.
    per_obj = {}
    per_op_obj = {oid: {} for oid in op_ids}
    for o in objectives:
        key = (o["tip"], o["categorie"] or "__general__")
        per_obj[key] = {"tip": o["tip"], "categorie": o["categorie"], "limita_minute": o["limita_minute"],
                        "unitate": o.get("unitate") or "minute", "pondere": o["pondere"],
                        "total": 0, "measurable": 0, "in_timp": 0}

    def _accumulate(key, op_id, mins, limit, allow_zero=False):
        # 0 e o masuratoare valida pe toate canalele: la apel = raspuns instant, la email/task =
        # rezolvat integral in afara orelor de program (vezi _measurable, decizie 2026-08-03).
        # `allow_zero` a ramas doar pentru compatibilitatea apelurilor existente -- acum ambele
        # ramuri sunt identice, pentru ca `_measurable` accepta 0.
        meas = (mins is not None) if allow_zero else _measurable(mins)
        mv = float(mins) if mins is not None else None
        in_timp = bool(meas and limit is not None and mv is not None and mv <= limit)
        if key in per_obj:
            per_obj[key]["total"] += 1
            if meas:
                per_obj[key]["measurable"] += 1
                if in_timp:
                    per_obj[key]["in_timp"] += 1
        if op_id in per_op_obj:
            slot = per_op_obj[op_id].setdefault(key, {"total": 0, "measurable": 0, "in_timp": 0})
            slot["total"] += 1
            if meas:
                slot["measurable"] += 1
                if in_timp:
                    slot["in_timp"] += 1
        return meas

    # 4a) mailuri solved (received), contorizate dupa LUNA rezolvarii -- ramane sursa numerelor
    #     "de departament" volum/excluse/measurable (backward-compat cu departamentele cu un
    #     singur obiectiv email). Suporta in continuare modelul general/per-categorie existent
    #     (informatie/sesizare/reclamatie), acum restrans doar la obiectivele de tip 'email'.
    volum = 0
    excluse = 0
    measurable_total = 0
    per_op = {oid: {"mails": 0, "measurable": 0, "in_timp": 0} for oid in op_ids}

    email_objectives = [o for o in objectives if o["tip"] == "email"]
    if email_objectives:
        cat_objectives = [o for o in email_objectives if o["categorie"]]
        general_obj = next((o for o in email_objectives if not o["categorie"]), None)
        scored_cats = {o["categorie"] for o in cat_objectives}

        def _in_scope(cat: Optional[str]) -> bool:
            if general_obj is not None:
                return True
            return cat in scored_cats

        def _limit_for(cat: Optional[str]) -> Optional[int]:
            if general_obj is not None:
                return general_obj["limita_minute"]
            for o in cat_objectives:
                if o["categorie"] == cat:
                    return o["limita_minute"]
            return None

        for op_id, cat, mins in _fetch_email_rows(db, department, first, holidays_arr, biz=biz):
            volum += 1
            if op_id in per_op:
                per_op[op_id]["mails"] += 1
            if not _in_scope(cat):
                excluse += 1
                continue
            limit = _limit_for(cat)
            bkey = ("email", "__general__") if general_obj is not None else ("email", cat)
            meas = _accumulate(bkey, op_id, mins, limit)
            if meas:
                measurable_total += 1
                if op_id in per_op:
                    per_op[op_id]["measurable"] += 1

        # in_timp per_op (email) -- citit direct din per_op_obj (populat de _accumulate mai sus)
        for oid in op_ids:
            slot_sum = sum(s["in_timp"] for k, s in per_op_obj.get(oid, {}).items() if k[0] == "email")
            per_op[oid]["in_timp"] = slot_sum

    # 4b) task-uri solved/closed -- un fetch per obiectiv (fiecare deja filtrat pe familia CargoBox
    #     la nivel de SQL, vezi _fetch_task_rows), atribuire directa prin assignee_employee_id.
    task_objs = [x for x in objectives if x["tip"] == "task"]
    has_family_split = any(o["categorie"] for o in task_objs)
    for o in task_objs:
        key = ("task", o["categorie"] or "__general__")
        for op_id, mins in _fetch_task_rows(db, department, first, o["categorie"], holidays_arr, biz=biz,
                                             has_family_split=has_family_split):
            _accumulate(key, op_id, mins, o["limita_minute"])

    # 4b-bis) interventii pe echipamente (Suport 2) finalizate -- un fetch per obiectiv, filtrat
    #     exact pe action_type (vezi _fetch_device_ops_rows), atribuire directa prin
    #     assignee_employee_id.
    for o in [x for x in objectives if x["tip"] == "device_ops"]:
        key = ("device_ops", o["categorie"] or "__general__")
        for op_id, mins in _fetch_device_ops_rows(db, department, first, o["categorie"], holidays_arr, biz=biz):
            _accumulate(key, op_id, mins, o["limita_minute"])

    # 4b-ter) reclamatii (Quality Evaluation) -- un fetch per categorie (contact / solutionare),
    #     fiecare cu propriul SLA. Toate reclamatiile firmei intra la departamentul care le
    #     proceseaza (Suport 3), nu la departamentul persoanei evaluate.
    for o in [x for x in objectives if x["tip"] == "reclamatie"]:
        key = ("reclamatie", o["categorie"] or "__general__")
        for op_id, mins in _fetch_reclamatie_rows(db, department, first, o["categorie"],
                                                   holidays_arr, biz=biz):
            _accumulate(key, op_id, mins, o["limita_minute"])

    # 4c) apeluri raspunse -- un singur fetch (azi exista un singur obiectiv 'apel', general; daca
    #     apar obiective 'apel' per-categorie in viitor, _fetch_apel_rows va avea nevoie de acelasi
    #     parametru de filtrare ca _fetch_task_rows).
    apel_objectives = [o for o in objectives if o["tip"] == "apel"]
    if apel_objectives:
        apel_rows = _fetch_apel_rows(db, department, first)
        for o in apel_objectives:
            key = ("apel", o["categorie"] or "__general__")
            for op_id, mins in apel_rows:
                _accumulate(key, op_id, mins, o["limita_minute"], allow_zero=True)

    # 5) achieved% per obiectiv + obiectiv_atins (medie ponderata normalizata pe cele masurabile)
    obiective_out = []
    weighted_sum = 0.0
    weight_active = 0.0
    for o in objectives:
        key = (o["tip"], o["categorie"] or "__general__")
        b = per_obj[key]
        if b["measurable"] > 0:
            achieved = 100.0 * b["in_timp"] / b["measurable"]
        elif b["total"] == 0:
            # Obiectiv fara NIMIC de rezolvat luna asta (ex. Task-uri CargoBox, 0 intrari):
            # nu poti rata ce nu a existat, deci se considera indeplinit 100%. Inainte ieseam pe
            # None si ponderea se redistribuia pe restul obiectivelor (suport_1/august: /95 in loc
            # de /100 => 91.85% in loc de 92.26%), ceea ce lasa scorul sa depinda de un obiectiv gol.
            achieved = 100.0
        else:
            # Au existat intrari, dar NICIUNA masurabila (toate cu durata 0/None) -- aici nu putem
            # afirma nici 100%: raman None si obiectivul se exclude din media ponderata.
            achieved = None
        obiective_out.append({
            "tip": o["tip"],
            "categorie": o["categorie"],
            "limita_minute": o["limita_minute"],
            "unitate": o.get("unitate") or "minute",
            "pondere": o["pondere"],
            "total": b["total"],
            "measurable": b["measurable"],
            "in_timp": b["in_timp"],
            "achieved": round(achieved, 2) if achieved is not None else None,
        })
        if achieved is not None:
            weighted_sum += achieved * o["pondere"]
            weight_active += o["pondere"]

    obiectiv_atins = round(weighted_sum / weight_active, 2) if weight_active > 0 else None

    # 6) status
    if not objectives:
        status = "fara_obiective"
    elif obiectiv_atins is None or obiectiv_real is None:
        status = "insufficient"
    elif obiectiv_atins >= obiectiv_real:
        status = "atins"
    elif obiectiv_minim is not None and obiectiv_atins >= obiectiv_minim:
        status = "partial"
    else:
        status = "sub_minim"

    # 7) drill-down operatori -- scor combinat + volume per tip pentru cotizare granulara
    # totaluri la nivel departament per tip (pentru calculul cotizare%)
    dept_vol_by_tip: dict = {}
    for o in objectives:
        tip = o["tip"]
        key = (tip, o["categorie"] or "__general__")
        dept_vol_by_tip[tip] = dept_vol_by_tip.get(tip, 0) + per_obj[key]["total"]

    operatori = []
    for oid in op_ids:
        po = per_op[oid]
        obj_slots = per_op_obj.get(oid, {})
        w_sum = 0.0
        w_active = 0.0
        volum_total = 0
        measurable_total_op = 0
        op_vol_by_tip: dict = {}
        for o in objectives:
            tip = o["tip"]
            key = (tip, o["categorie"] or "__general__")
            slot = obj_slots.get(key)
            if not slot:
                continue
            volum_total += slot["total"]
            op_vol_by_tip[tip] = op_vol_by_tip.get(tip, 0) + slot["total"]
            if slot["measurable"] > 0:
                measurable_total_op += slot["measurable"]
                ach = 100.0 * slot["in_timp"] / slot["measurable"]
                w_sum += ach * o["pondere"]
                w_active += o["pondere"]
        achieved_op = round(w_sum / w_active, 2) if w_active > 0 else None

        def _cotiz(tip):
            d = dept_vol_by_tip.get(tip, 0)
            o = op_vol_by_tip.get(tip, 0)
            return round(100.0 * o / d, 2) if d > 0 else None

        # CONTRIBUTIE PONDERATA (cerinta 2026-08-13): cat la suta din productivitatea echipei
        # tine de acest om, dupa REGULILE departamentului. `cotizare` simpla numara bucati si
        # trateaza un mail la fel ca un task CargoBox; aici fiecare obiectiv intra cu ponderea
        # lui din configurare (ex. suport_2: email 20, task 20, apel 8, device_ops 52 cumulat).
        #
        #   contributie = Σ_obiectiv ( pondere_o × volum_op_o / volum_dept_o ) / Σ_obiectiv pondere_o
        #
        # Se insumeaza doar obiectivele cu volum de departament > 0 (un obiectiv fara nicio
        # intrare in luna nu poate imparti nimic si i-ar dilua pe ceilalti).
        # Suma pe echipa da 100% mai putin partea rezolvata de oameni care nu mai sunt operatori
        # activi ai departamentului in luna respectiva (plecati, mutati) -- randurile lor exista
        # in volumul de departament, dar nu au un rand in tabel.
        contrib_sum = 0.0
        contrib_weight = 0.0
        for o in objectives:
            key = (o["tip"], o["categorie"] or "__general__")
            dept_total = per_obj[key]["total"]
            if dept_total <= 0:
                continue
            op_total = (obj_slots.get(key) or {}).get("total", 0)
            contrib_sum += o["pondere"] * (op_total / dept_total)
            contrib_weight += o["pondere"]
        contributie = round(100.0 * contrib_sum / contrib_weight, 2) if contrib_weight > 0 else None

        operatori.append({
            "id": oid,
            "name": op_meta[oid]["name"],
            "mails": po["mails"],
            "volum_total": volum_total,
            "cotizare": round(100.0 * po["mails"] / volum, 2) if volum > 0 else 0.0,
            "measurable": po["measurable"],
            "measurable_total": measurable_total_op,
            "achieved": achieved_op,
            "contributie": contributie,
            "vol_email": op_vol_by_tip.get("email", 0),
            "vol_task": op_vol_by_tip.get("task", 0),
            "vol_apel": op_vol_by_tip.get("apel", 0),
            "vol_device_ops": op_vol_by_tip.get("device_ops", 0),
            # ATENTIE la reclamatii: aceeasi reclamatie apare in DOUA obiective (contact si
            # solutionare), deci `vol_reclamatie` e suma celor doua praguri, nu numarul de
            # reclamatii distincte. Pentru tabelul per operator se foloseste `obiective` de mai
            # jos, care le tine separate — la fel cum sint si in configurarea departamentului.
            "vol_reclamatie": op_vol_by_tip.get("reclamatie", 0),
            "cotiz_email": _cotiz("email"),
            "cotiz_task": _cotiz("task"),
            "cotiz_apel": _cotiz("apel"),
            "cotiz_device_ops": _cotiz("device_ops"),
            "cotiz_reclamatie": _cotiz("reclamatie"),
            # Volum + cotizatie PER OBIECTIV (tip + categorie), sursa exacta pentru coloanele din
            # tabelul echipei. Fara asta, un tip cu mai multe categorii (reclamatii: contact +
            # solutionare) nu se putea afisa corect pe coloane separate.
            "obiective": [
                {
                    "tip": o["tip"],
                    "categorie": o["categorie"],
                    "total": (obj_slots.get((o["tip"], o["categorie"] or "__general__")) or {}).get("total", 0),
                    "measurable": (obj_slots.get((o["tip"], o["categorie"] or "__general__")) or {}).get("measurable", 0),
                    "in_timp": (obj_slots.get((o["tip"], o["categorie"] or "__general__")) or {}).get("in_timp", 0),
                    "cotiz": (round(100.0 * (obj_slots.get((o["tip"], o["categorie"] or "__general__")) or {}).get("total", 0)
                                    / per_obj[(o["tip"], o["categorie"] or "__general__")]["total"], 2)
                              if per_obj[(o["tip"], o["categorie"] or "__general__")]["total"] > 0 else None),
                }
                for o in objectives
            ],
            "device_ops_breakdown": [
                {
                    "categorie": cat,
                    "total": obj_slots.get(("device_ops", cat), {}).get("total", 0),
                    "pct": round(100.0 * obj_slots.get(("device_ops", cat), {}).get("total", 0) /
                                 op_vol_by_tip.get("device_ops", 0), 2)
                          if op_vol_by_tip.get("device_ops", 0) > 0 else 0.0,
                }
                for cat in sorted({o["categorie"] for o in objectives if o["tip"] == "device_ops" and o["categorie"]})
            ] if op_vol_by_tip.get("device_ops", 0) > 0 else [],
        })
    operatori.sort(key=lambda x: x["volum_total"], reverse=True)

    return {
        "department": department,
        "month": f"{year:04d}-{month:02d}",
        "baza_procent": cfg["baza_procent"] if cfg else None,
        "zile_lucratoare": wd,
        "ore_planificate": round(ore_planificate, 2),
        "ore_disponibile": round(ore_disponibile, 2),
        "ore_plan_ideale": round(ore_plan_ideale, 2),
        "coeficient": round(coeficient, 4) if coeficient is not None else None,
        "obiectiv_real": round(obiectiv_real, 2) if obiectiv_real is not None else None,
        "obiectiv_minim": round(obiectiv_minim, 2) if obiectiv_minim is not None else None,
        "obiectiv_atins": obiectiv_atins,
        "status": status,
        "volum": volum,
        "excluse": excluse,
        "measurable": measurable_total,
        "obiective": obiective_out,
        "operatori": operatori,
        "note_masurare": None if measurable_total > 0 else _NOTE_INSUFFICIENT,
    }


# ---------------------------------------------------------------- productivitate pe ZILE (tab Rapoarte)
#
# DE CE: raportul lunar spune CIT s-a atins, nu CIND s-a pierdut. O luna la 88% poate ascunde trei
# zile la 40% -- ca sa le poti inspecta ("de ce a fost 10 august slab?") ai nevoie de scorul pe zi,
# defalcat pe obiective.
#
# SURSA: exact aceleasi randuri ca raportul lunar, prin `breakdown_rows` (care oglindeste sursele si
# filtrele din `_fetch_*_rows`). Nu se scrie SQL nou: altfel tabelul zilnic si scorul lunar ar putea
# diverge in timp.
#
# ZIUA unui rind = ziua REZOLVARII (`solved_at`), la fel ca luna in raportul lunar: un mail sosit pe
# 8 si rezolvat pe 10 conteaza pe 10. La apeluri `solved_at` e sfirsitul convorbirii, deci practic
# ziua apelului.
#
# DIFERENTA de regula fata de scorul lunar, deliberata: un obiectiv fara NICIUN rind intr-o zi nu
# intra in media zilei (lunar, un obiectiv gol conteaza 100% -- "nu poti rata ce n-a existat").
# Pe o zi, regula lunara ar da 100% pe canalele fara activitate si ar ascunde exact ziua slaba pe
# care o cauti. De aceea totalul zilei e media ponderata NUMAI pe obiectivele care au avut ceva
# masurabil, iar `pondere_activa` spune cita pondere a fost in joc. Consecinta care trebuie spusa in
# UI: media zilelor NU e egala cu scorul lunar (numitori diferiti).
def daily_report(db: Session, department: str, year: int, month: int) -> dict:
    """Scor de productivitate pe fiecare zi a lunii, defalcat pe obiective."""
    objectives = get_objectives(db, department, tip=None)
    first, last = _month_bounds(year, month)

    # Cheia unui obiectiv, stabila intre backend si UI.
    def _key(o) -> str:
        return o["tip"] if not o.get("categorie") else f'{o["tip"]}:{o["categorie"]}'

    obiective_meta = [{
        "key": _key(o), "tip": o["tip"], "categorie": o["categorie"],
        "limita_minute": o["limita_minute"], "unitate": o.get("unitate") or "minute",
        "pondere": o["pondere"],
    } for o in objectives]

    # zi -> cheie obiectiv -> contoare
    per_day: dict = {}

    def _slot(day: str, key: str) -> dict:
        return per_day.setdefault(day, {}).setdefault(key, {"total": 0, "measurable": 0, "in_timp": 0})

    for o in objectives:
        key = _key(o)
        try:
            rows = breakdown_rows(db, department, o["tip"], first, o["categorie"], o["limita_minute"])
        except Exception:
            # Un canal cu sursa stricata nu are voie sa doboare tot tabelul: se raporteaza gol.
            logger.exception("daily_report: breakdown %s/%s dept=%s", o["tip"], o["categorie"], department)
            continue
        for r in rows:
            # `solved_at` e momentul care da luna in raportul lunar; `created_at` e plasa de
            # siguranta pentru randurile fara capat de final.
            ts = r.get("solved_at") or r.get("created_at")
            if not ts:
                continue
            day = str(ts)[:10]          # ISO cu offset RO -> data locala
            sl = _slot(day, key)
            sl["total"] += 1
            if r.get("durata") is not None:
                sl["measurable"] += 1
                if r.get("status") == "on_time":
                    sl["in_timp"] += 1

    zile = []
    d = first
    while d <= last:
        dkey = d.isoformat()
        buckets = per_day.get(dkey, {})
        obiective_zi = []
        weighted_sum = 0.0
        weight_active = 0.0
        vol_zi = 0
        meas_zi = 0
        for om in obiective_meta:
            b = buckets.get(om["key"]) or {"total": 0, "measurable": 0, "in_timp": 0}
            achieved = round(100.0 * b["in_timp"] / b["measurable"], 2) if b["measurable"] > 0 else None
            obiective_zi.append({
                "key": om["key"], "total": b["total"], "measurable": b["measurable"],
                "in_timp": b["in_timp"], "achieved": achieved,
            })
            vol_zi += b["total"]
            meas_zi += b["measurable"]
            if achieved is not None:
                weighted_sum += achieved * om["pondere"]
                weight_active += om["pondere"]
        zile.append({
            "day": dkey,
            "weekday": d.isoweekday(),
            "volum": vol_zi,
            "measurable": meas_zi,
            "pondere_activa": round(weight_active, 2),
            "total_pct": round(weighted_sum / weight_active, 2) if weight_active > 0 else None,
            "obiective": obiective_zi,
        })
        d += _dt.timedelta(days=1)

    return {
        "department": department,
        "month": f"{year:04d}-{month:02d}",
        "obiective": obiective_meta,
        "zile": zile,
    }


def trend(db: Session, department: str, months: int = 6, year: Optional[int] = None,
          month: Optional[int] = None) -> list:
    """Serie lunara (ultimele `months` luni pana la year/month inclusiv): obiectiv_atins vs real."""
    if year is None or month is None:
        r = db.execute(text("SELECT extract(year from CURRENT_DATE)::int, extract(month from CURRENT_DATE)::int")).fetchone()
        year, month = int(r[0]), int(r[1])
    seq = []
    y, m = year, month
    for _ in range(max(1, months)):
        seq.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    out = []
    for (yy, mm) in reversed(seq):
        rep = department_report(db, department, yy, mm)
        out.append({
            "month": rep["month"],
            "obiectiv_atins": rep["obiectiv_atins"],
            "obiectiv_real": rep["obiectiv_real"],
            "volum": rep["volum"],
        })
    return out


# ---------------------------------------------------------------- analytics pe interval (tab Analiza)
_ANA_BUCKETS = [
    (30.0, "<30 min"),
    (60.0, "30-60 min"),
    (120.0, "1-2 h"),
    (240.0, "2-4 h"),
    (480.0, "4-8 h"),
    (None, ">8 h"),
]


def _bucket_index(mins: float) -> int:
    for i, (hi, _lbl) in enumerate(_ANA_BUCKETS):
        if hi is None or mins < hi:
            return i
    return len(_ANA_BUCKETS) - 1


def aggregate_reports(reports: list) -> dict:
    """Agregheaza mai multe rapoarte lunare intr-un singur raport multi-luna.

    Volum/ore/zile = suma; obiective achieved = in_timp_cumulat/measurable_cumulat;
    operatori = suma volume + recalcul cotizatii; obiectiv_atins = medie ponderata recalculata.
    """
    if not reports:
        return {}
    if len(reports) == 1:
        return reports[0]

    base = reports[0]
    department = base["department"]
    baza_procent = base["baza_procent"]
    obiectiv_real = base["obiectiv_real"]
    obiectiv_minim = base["obiectiv_minim"]

    # Sume simple
    zile = sum(r.get("zile_lucratoare") or 0 for r in reports)
    ore_plan = sum(r.get("ore_planificate") or 0 for r in reports)
    ore_disp = sum(r.get("ore_disponibile") or 0 for r in reports)
    ore_plan_ideale_total = sum(r.get("ore_plan_ideale") or r.get("ore_planificate") or 0 for r in reports)
    volum = sum(r.get("volum") or 0 for r in reports)
    excluse = sum(r.get("excluse") or 0 for r in reports)
    measurable_total = sum(r.get("measurable") or 0 for r in reports)

    coeficient = round(baza_procent / ore_plan_ideale_total, 4) if ore_plan_ideale_total > 0 else None
    obiectiv_real = round(ore_disp * coeficient, 2) if coeficient is not None else baza_procent
    obiectiv_minim = round(obiectiv_real - _MINIM_DELTA, 2)

    # Obiective — agregate pe (tip, categorie)
    obj_acc: dict = {}  # key -> {meta, total, measurable, in_timp}
    for r in reports:
        for o in (r.get("obiective") or []):
            k = (o["tip"], o.get("categorie"))
            if k not in obj_acc:
                obj_acc[k] = {
                    "tip": o["tip"], "categorie": o.get("categorie"),
                    "limita_minute": o["limita_minute"], "unitate": o.get("unitate", "minute"),
                    "pondere": o["pondere"],
                    "total": 0, "measurable": 0, "in_timp": 0,
                }
            obj_acc[k]["total"] += o.get("total") or 0
            obj_acc[k]["measurable"] += o.get("measurable") or 0
            obj_acc[k]["in_timp"] += o.get("in_timp") or 0

    obiective_out = []
    weighted_sum = 0.0
    weight_active = 0.0
    for acc in obj_acc.values():
        # Aceeasi regula ca in department_report: fara nicio intrare pe interval => 100%.
        if acc["measurable"] > 0:
            achieved = 100.0 * acc["in_timp"] / acc["measurable"]
        elif acc["total"] == 0:
            achieved = 100.0
        else:
            achieved = None
        obiective_out.append({
            "tip": acc["tip"], "categorie": acc["categorie"],
            "limita_minute": acc["limita_minute"], "unitate": acc["unitate"],
            "pondere": acc["pondere"],
            "total": acc["total"], "measurable": acc["measurable"], "in_timp": acc["in_timp"],
            "achieved": round(achieved, 2) if achieved is not None else None,
        })
        if achieved is not None:
            weighted_sum += achieved * acc["pondere"]
            weight_active += acc["pondere"]

    obiectiv_atins = round(weighted_sum / weight_active, 2) if weight_active > 0 else None

    if obiectiv_atins is None or obiectiv_real is None:
        status = "insufficient"
    elif obiectiv_atins >= obiectiv_real:
        status = "atins"
    elif obiectiv_minim is not None and obiectiv_atins >= obiectiv_minim:
        status = "partial"
    else:
        status = "sub_minim"

    # Operatori — agregate per id
    op_acc: dict = {}  # id -> {meta + acumulatori}
    for r in reports:
        for op in (r.get("operatori") or []):
            oid = op["id"]
            if oid not in op_acc:
                op_acc[oid] = {
                    "id": oid, "name": op["name"],
                    "department_label": op.get("department_label", ""),
                    "mails": 0, "volum_total": 0, "measurable": 0, "in_timp_w": 0.0,
                    "weight_active": 0.0,
                    "vol_email": 0, "vol_task": 0, "vol_apel": 0, "vol_device_ops": 0,
                    "vol_reclamatie": 0,
                    "_obj_slots": {},  # (tip, cat) -> {measurable, in_timp, pondere}
                }
            a = op_acc[oid]
            a["mails"] += op.get("mails") or 0
            a["volum_total"] += op.get("volum_total") or 0
            a["measurable"] += op.get("measurable") or 0
            a["vol_email"] += op.get("vol_email") or 0
            a["vol_task"] += op.get("vol_task") or 0
            a["vol_apel"] += op.get("vol_apel") or 0
            a["vol_device_ops"] += op.get("vol_device_ops") or 0
            a["vol_reclamatie"] += op.get("vol_reclamatie") or 0

    # Recalcul achieved per operator din obiectivele agregate per operator
    for r in reports:
        for op in (r.get("operatori") or []):
            oid = op["id"]
            a = op_acc[oid]
            # Nu avem per_op_obj in raspuns, reconstruim din cotiz% si volum dept
            # Folosim vol_* si cotiz_* pentru a estima in_timp per slot per luna
            # Simplu: accumulated achieved = weighted sum of o.achieved * pondere per obiectiv
            # dar NU avem achieved per operator per obiectiv in raspuns.
            # Folosim achieved la nivel operator direct din 'achieved' field
            ach = op.get("achieved")
            if ach is not None:
                # Ponderea = suma ponderilor active din luna respectiva
                # Estimare: o luna cu measurable > 0 contribuie cu pondere 1 (simplu)
                if op.get("measurable") and op["measurable"] > 0:
                    a["in_timp_w"] += ach * op["measurable"]
                    a["weight_active"] += op["measurable"]

    dept_vol_by_tip: dict = {}
    pondere_by_tip: dict = {}
    for acc in obj_acc.values():
        tip = acc["tip"]
        dept_vol_by_tip[tip] = dept_vol_by_tip.get(tip, 0) + acc["total"]
        pondere_by_tip[tip] = pondere_by_tip.get(tip, 0.0) + float(acc["pondere"])

    operatori_out = []
    for a in op_acc.values():
        achieved_op = round(a["in_timp_w"] / a["weight_active"], 2) if a["weight_active"] > 0 else None

        # Contributia ponderata pe interval. Pe multi-luna raspunsul lunar nu pastreaza volumele
        # per (tip, categorie) ale operatorului, doar per TIP, deci ponderile se cumuleaza la
        # nivel de tip (ex. suport_1: task general 20 + CargoBox 5 = 25). Diferenta apare doar
        # daca omul are un mix pe categorii diferit de al echipei in interiorul aceluiasi tip;
        # raportul pe 1 luna (department_report) ramane calculul exact, per obiectiv.
        contrib_sum = 0.0
        contrib_weight = 0.0
        for tip_key, pond in pondere_by_tip.items():
            dept_v = dept_vol_by_tip.get(tip_key, 0)
            if dept_v <= 0:
                continue
            contrib_sum += pond * (a.get("vol_" + tip_key, 0) / dept_v)
            contrib_weight += pond
        contributie = round(100.0 * contrib_sum / contrib_weight, 2) if contrib_weight > 0 else None

        def _cotiz(tip_key):
            d = dept_vol_by_tip.get(tip_key, 0)
            o = a.get("vol_" + tip_key, 0)
            return round(100.0 * o / d, 2) if d > 0 else None

        # `cotiz_task` imparte la volumul de TASK-uri, nu la task + device_ops cum facea pana
        # acum: operatiunile pe echipamente au propria coloana (`cotiz_device_ops`), iar
        # denominatorul comun facea ca aceeasi persoana sa aiba alt procent pe 1 luna
        # (department_report, doar task) fata de 3/6/12 luni (agregat, task + operatiuni).
        cotiz_task = _cotiz("task")

        operatori_out.append({
            "id": a["id"], "name": a["name"],
            "mails": a["mails"],
            "volum_total": a["volum_total"],
            "cotizare": round(100.0 * a["mails"] / volum, 2) if volum > 0 else 0.0,
            "measurable": a["measurable"],
            "achieved": achieved_op,
            "contributie": contributie,
            "vol_email": a["vol_email"], "vol_task": a["vol_task"], "vol_apel": a["vol_apel"],
            "vol_device_ops": a["vol_device_ops"],
            # Pe interval nu avem defalcarea per categorie (raspunsul lunar nu o pastreaza), deci
            # reclamatiile apar cumulat: contact + solutionare. Pe o singura luna, tabelul
            # foloseste `obiective`, care le tine separate.
            "vol_reclamatie": a["vol_reclamatie"],
            "cotiz_email": _cotiz("email"),
            "cotiz_task": cotiz_task,
            "cotiz_apel": _cotiz("apel"),
            "cotiz_device_ops": _cotiz("device_ops"),
            "cotiz_reclamatie": _cotiz("reclamatie"),
        })
    operatori_out.sort(key=lambda x: x["volum_total"], reverse=True)

    months_range = [r["month"] for r in reports if r.get("month")]
    label = months_range[0] + " – " + months_range[-1] if len(months_range) >= 2 else (months_range[0] if months_range else "")

    return {
        "department": department,
        "month": label,
        "baza_procent": baza_procent,
        "zile_lucratoare": zile,
        "ore_planificate": round(ore_plan, 2),
        "ore_disponibile": round(ore_disp, 2),
        "coeficient": coeficient,
        "obiectiv_real": obiectiv_real,
        "obiectiv_minim": obiectiv_minim,
        "obiectiv_atins": obiectiv_atins,
        "status": status,
        "volum": volum,
        "excluse": excluse,
        "measurable": measurable_total,
        "obiective": obiective_out,
        "operatori": operatori_out,
        "note_masurare": None if measurable_total > 0 else _NOTE_INSUFFICIENT,
    }


def forecast_report(db: Session, department: str, year: int, month: int) -> dict:
    """Estimare productivitate pentru o luna viitoare (sau luna curenta fara date complete).

    Logica:
    - Volume = media zilnica din ultimele 2 luni complete disponibile x zile_lucratoare_luna_tinta
    - Ore planificate = angajati activi x zile_lucratoare x work_hours (toti prezenti, caz ideal)
    - Ore disponibile = ajustate cu zilele de concediu planificate din employee_schedule
    - Achieved% estimat = aplicam proportia in_timp din ultimele 2 luni (daca exista date)
    - Returneaza aceeasi structura ca department_report() + campul is_forecast=True
    """
    holidays = get_holidays(db)
    cfg = get_department_config(db, department)
    objectives = get_objectives(db, department, tip=None)
    if not cfg or not objectives:
        return {
            "department": department,
            "month": f"{year:04d}-{month:02d}",
            "is_forecast": True,
            "status": "fara_obiective",
            "baza_procent": None,
            "zile_lucratoare": 0,
            "ore_planificate": 0.0,
            "ore_disponibile": 0.0,
            "coeficient": None,
            "obiectiv_real": None,
            "obiectiv_minim": None,
            "obiectiv_atins": None,
            "volum": 0,
            "excluse": 0,
            "measurable": 0,
            "obiective": [],
            "operatori": [],
            "note_masurare": None,
        }

    # Zile lucratoare luna tinta (calendaristic L-V, minus sarbatori)
    ndays_tgt = calendar.monthrange(year, month)[1]
    first_tgt = _dt.date(year, month, 1)
    last_tgt = _dt.date(year, month, ndays_tgt)
    zile_lucratoare = sum(
        1 for d in range(1, ndays_tgt + 1)
        if _is_working_day(_dt.date(year, month, d), holidays)
    )

    # Operatori activi — excludem cei cu productivity_start_date > ultima zi a lunii tinta
    ops = db.execute(
        text("SELECT id, name, COALESCE(work_hours, :wh) AS work_hours, iris_id "
             "FROM employee_department_mapping WHERE department=:d AND enabled=true "
             "AND (productivity_start_date IS NULL OR productivity_start_date <= :last_day) "
             "ORDER BY name"),
        {"d": department, "wh": _DEFAULT_WORK_HOURS, "last_day": last_tgt},
    ).fetchall()
    op_ids = [r[0] for r in ops]
    op_iris_ids_fc = [int(r[3]) for r in ops if r[3] is not None]
    iris_to_edm_fc = {int(r[3]): r[0] for r in ops if r[3] is not None}
    op_meta = {r[0]: {"name": r[1], "work_hours": int(r[2] or _DEFAULT_WORK_HOURS)} for r in ops}

    # Zile de concediu in luna tinta per angajat:
    leave_days_per_emp: dict = {}
    if op_ids:
        leave_rows = db.execute(
            text("""
                SELECT es.employee_id, es.start_date, es.end_date
                FROM employee_schedule es
                WHERE es.employee_id = ANY(:ids)
                  AND (es.kind = 'vacation_approved' OR es.entry_source = 'manual')
                  AND es.start_date <= :last
                  AND es.end_date   >= :first
            """),
            {"ids": op_ids, "first": first_tgt, "last": last_tgt},
        ).fetchall()
        _dv_tbl_exists2 = db.execute(text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='cts_dv_employee_vacation_request' LIMIT 1"
        )).fetchone()
        if _dv_tbl_exists2 and op_iris_ids_fc:
            _dv_rows2_raw = db.execute(text("""
                SELECT v.employee_id::int, v.period_begin::date, v.period_end::date
                FROM cts_dv_employee_vacation_request v
                WHERE v.employee_id::int = ANY(:iris_ids)
                  AND v.status::int IN (1, 2)
                  AND v.deleted_at IS NULL
                  AND v.period_begin::date <= :last
                  AND v.period_end::date   >= :first
            """), {"iris_ids": op_iris_ids_fc, "first": first_tgt, "last": last_tgt}).fetchall()
            _dv_rows2 = [(iris_to_edm_fc[r[0]], r[1], r[2]) for r in _dv_rows2_raw if r[0] in iris_to_edm_fc]
            leave_rows = list(leave_rows) + _dv_rows2
        # Calcul L-V zile per angajat cu deduplicare prin union de intervale
        from collections import defaultdict
        emp_dates: dict = defaultdict(set)
        for emp_id, sd, ed in leave_rows:
            overlap_start = max(sd, first_tgt)
            overlap_end = min(ed, last_tgt)
            d = overlap_start
            while d <= overlap_end:
                if d.isoweekday() < 6 and d.isoformat() not in holidays:
                    emp_dates[emp_id].add(d)
                d += _dt.timedelta(days=1)
        # Zile libere extra (proiecte / refurbished) — numar de zile pe luna, fara date
        # concrete, deci adunate peste zilele de concediu. Iterez pe op_ids (nu pe emp_dates)
        # ca sa prind si angajatii care au DOAR zile extra, fara concediu in luna.
        extra_days_per_emp = _extra_days_per_emp(db, op_ids, year, month)
        for emp_id in op_ids:
            total = len(emp_dates.get(emp_id, set())) + extra_days_per_emp.get(emp_id, 0)
            if total:
                leave_days_per_emp[emp_id] = min(total, zile_lucratoare)

    # Ore planificate (toti prezenti, caz ideal) si ore disponibile (minus concedii aprobate)
    baza_procent = float(cfg["baza_procent"])

    # Snapshot lunar imutabil: dacă există, targetele rămân fixate indiferent de
    # concedii ulterioare, modificări de obiective sau alte schimbări de date.
    # EXCEPȚIE: lunile care nu au început încă se recalculează LIVE la fiecare accesare — estimarea
    # trebuie să reflecte imediat un concediu nou, zile de lucru pe proiecte sau un angajat care
    # intră în calcul din luna respectivă.
    # Idem department_report: primele zile lucrătoare ale lunii în curs rămân LIVE.
    _future_fc = _is_future_month(year, month) or _snapshot_too_early(year, month, holidays)
    _snap_fc = None if _future_fc else _get_snapshot(db, department, year, month)
    if _snap_fc is not None:
        ore_planificate = _snap_fc["ore_planificate"]
        ore_disponibile = _snap_fc["ore_disponibile"]
        coeficient      = _snap_fc["coeficient"]
        obiectiv_real   = _snap_fc["obiectiv_real"]
        obiectiv_minim  = _snap_fc["obiectiv_minim"]
    else:
        ore_planificate = sum(op_meta[oid]["work_hours"] * zile_lucratoare for oid in op_ids)
        ore_concediu = sum(
            op_meta[oid]["work_hours"] * leave_days_per_emp.get(oid, 0) for oid in op_ids
        )
        ore_disponibile = max(0.0, ore_planificate - ore_concediu)
        # coeficient = obiectiv per ora disponibila (baza_procent / ore_ideale_totale)
        # obiectiv_real = ore_disponibile * coeficient — ajustat cu concediile reale
        coeficient = round(baza_procent / ore_planificate, 4) if ore_planificate > 0 else None
        obiectiv_real = round(ore_disponibile * coeficient, 2) if coeficient is not None else baza_procent
        obiectiv_minim = round(obiectiv_real - _MINIM_DELTA, 2)
        # Persistă snapshot doar din luna în curs încolo — o lună viitoare rămâne recalculabilă.
        if not _future_fc:
            _save_snapshot(db, department, year, month, baza_procent, zile_lucratoare,
                           ore_planificate, ore_disponibile, coeficient, obiectiv_real, obiectiv_minim)

    # Volume istorice: ultimele 2 luni complete disponibile
    hist_months = []
    yy, mm = year, month
    for _ in range(4):  # cauta pana la 4 luni inapoi sa gasim 2 luni cu date
        mm -= 1
        if mm == 0:
            mm = 12
            yy -= 1
        hist_months.append((yy, mm))
        if len(hist_months) >= 2:
            break

    # Media zilnica per tip din istoricul disponibil
    hist_vol: dict = {}   # tip -> [vol_lunar1, vol_lunar2, ...]
    hist_intmp: dict = {} # tip -> [in_timp_lunar1, ...]
    hist_meas: dict = {}  # tip -> [measurable_lunar1, ...]

    for (hy, hm) in hist_months:
        hfirst, hlast = _month_bounds(hy, hm)
        hdays = sum(
            1 for d in range(1, calendar.monthrange(hy, hm)[1] + 1)
            if _is_working_day(_dt.date(hy, hm, d), holidays)
        )
        if hdays == 0:
            continue

        for o in objectives:
            tip = o["tip"]
            hdays_f = float(hdays)
            vol_luna = 0
            intmp_luna = 0
            meas_luna = 0

            if tip == "email":
                rows = db.execute(
                    text(rf"""
                        SELECT COUNT(*) as vol,
                               COUNT(CASE WHEN g.cts_reply_at IS NOT NULL
                                         AND EXTRACT(EPOCH FROM (g.cts_reply_at -
                                             {_EMAIL_START_SQL.format(g='g')}
                                             ))/60 <= :lim THEN 1 END) as intmp,
                               COUNT(CASE WHEN g.cts_reply_at IS NOT NULL
                                         AND {_EMAIL_START_SQL.format(g='g')}
                                             IS NOT NULL THEN 1 END) as meas
                        FROM cts_ground_truth g
                        JOIN emails e ON e.id = g.email_id
                        JOIN employee_department_mapping edm ON lower(edm.email) = lower(g.cts_assignee_email)
                        LEFT JOIN clients pex ON pex.id = e.client_id
                        WHERE edm.department = :dept
                          AND {_EMAIL_EXCLUDE_SQL}
                          AND date_trunc('month', g.cts_reply_at AT TIME ZONE 'Europe/Bucharest') =
                              date_trunc('month', CAST(:hfirst AS timestamp))
                    """),
                    {"dept": department, "lim": o["limita_minute"], "hfirst": hfirst},
                ).fetchone()
                if rows:
                    vol_luna = int(rows[0] or 0)
                    intmp_luna = int(rows[1] or 0)
                    meas_luna = int(rows[2] or 0)

            elif tip == "apel":
                rows = db.execute(
                    text("""
                        SELECT COUNT(*) as vol,
                               COUNT(CASE WHEN cgt.cts_response_seconds <= :lim THEN 1 END) as intmp,
                               COUNT(*) as meas
                        FROM cts_calls_ground_truth cgt
                        JOIN employee_department_mapping edm ON lower(edm.email) = lower(cgt.cts_assignee_email)
                        WHERE edm.department = :dept
                          AND date_trunc('month', cgt.cts_started_at AT TIME ZONE 'Europe/Bucharest') =
                              date_trunc('month', CAST(:hfirst AS timestamp))
                    """),
                    {"dept": department, "lim": int(o["limita_minute"] * 60), "hfirst": hfirst},
                ).fetchone()
                if rows:
                    vol_luna = int(rows[0] or 0)
                    intmp_luna = int(rows[1] or 0)
                    meas_luna = int(rows[2] or 0)

            elif tip in ("task", "device_ops"):
                rows = db.execute(
                    text("""
                        SELECT COUNT(*) as vol,
                               COUNT(CASE WHEN tgt.cts_created_at IS NOT NULL
                                         AND tgt.cts_updated_at IS NOT NULL
                                         AND EXTRACT(EPOCH FROM (tgt.cts_updated_at - tgt.cts_created_at))/60
                                             <= :lim THEN 1 END) as intmp,
                               COUNT(CASE WHEN tgt.cts_created_at IS NOT NULL
                                         AND tgt.cts_updated_at IS NOT NULL THEN 1 END) as meas
                        FROM cts_task_ground_truth tgt
                        JOIN employee_department_mapping edm ON edm.id = tgt.assignee_employee_id
                        WHERE edm.department = :dept AND edm.enabled = true
                          AND lower(tgt.status) = 'solved'
                          AND date_trunc('month', tgt.cts_updated_at AT TIME ZONE 'Europe/Bucharest') =
                              date_trunc('month', CAST(:hfirst AS timestamp))
                    """),
                    {"dept": department, "lim": o["limita_minute"], "hfirst": hfirst},
                ).fetchone()
                if rows:
                    vol_luna = int(rows[0] or 0)
                    intmp_luna = int(rows[1] or 0)
                    meas_luna = int(rows[2] or 0)

            hist_vol.setdefault(tip, []).append(vol_luna / hdays_f)
            hist_intmp.setdefault(tip, []).append(intmp_luna)
            hist_meas.setdefault(tip, []).append(meas_luna)

    # Calcul obiective estimate
    obiective_out = []
    weighted_sum = 0.0
    weight_active = 0.0

    for o in objectives:
        tip = o["tip"]
        avg_daily = (sum(hist_vol.get(tip, [])) / len(hist_vol[tip])) if hist_vol.get(tip) else 0.0
        vol_est = int(round(avg_daily * zile_lucratoare))

        # Rata in_timp din istoric
        total_meas_hist = sum(hist_meas.get(tip, []))
        total_intmp_hist = sum(hist_intmp.get(tip, []))
        rate_intmp = (total_intmp_hist / total_meas_hist) if total_meas_hist > 0 else None

        intmp_est = int(round(vol_est * rate_intmp)) if rate_intmp is not None and vol_est > 0 else None
        achieved = round(rate_intmp * 100, 2) if rate_intmp is not None else None

        obiective_out.append({
            "tip": tip,
            "categorie": o["categorie"],
            "limita_minute": o["limita_minute"],
            "unitate": o.get("unitate") or "minute",
            "pondere": o["pondere"],
            "total": vol_est,
            "measurable": vol_est,
            "in_timp": intmp_est or 0,
            "achieved": achieved,
        })

        if achieved is not None:
            weighted_sum += achieved * o["pondere"]
            weight_active += o["pondere"]

    obiectiv_atins = round(weighted_sum / weight_active, 2) if weight_active > 0 else None

    if obiectiv_atins is None or obiectiv_real is None:
        status = "insufficient"
    elif obiectiv_atins >= obiectiv_real:
        status = "atins"
    elif obiectiv_atins >= obiectiv_minim:
        status = "partial"
    else:
        status = "sub_minim"

    # Operatori: volume estimate proportional cu contributia din istoricul lunii curente
    # Folosim ultima luna disponibila pentru repartizare per operator
    operatori = []
    if op_ids:
        # Cotizatie estimata = contributia relativa din ultima luna istorica
        last_hist = hist_months[0] if hist_months else None
        op_vol_est: dict = {oid: {} for oid in op_ids}
        dept_vol_est: dict = {}
        lhdays = 0

        if last_hist:
            lhy, lhm = last_hist
            lhfirst = _dt.date(lhy, lhm, 1)
            lhdays = sum(
                1 for d in range(1, calendar.monthrange(lhy, lhm)[1] + 1)
                if _is_working_day(_dt.date(lhy, lhm, d), holidays)
            )
            if lhdays > 0:
                # Email per operator din ultima luna
                email_obj = next((x for x in objectives if x["tip"] == "email"), None)
                if email_obj:
                    erows = db.execute(
                        text("""
                            SELECT edm.id as emp_id, COUNT(*) as vol
                            FROM cts_ground_truth g
                            JOIN emails e ON e.id = g.email_id
                            JOIN employee_department_mapping edm ON lower(edm.email) = lower(g.cts_assignee_email)
                            WHERE edm.department = :dept
                              AND date_trunc('month', g.cts_reply_at AT TIME ZONE 'Europe/Bucharest') =
                                  date_trunc('month', CAST(:hfirst AS timestamp))
                            GROUP BY edm.id
                        """),
                        {"dept": department, "hfirst": lhfirst},
                    ).fetchall()
                    dept_email_total = sum(r[1] for r in erows)
                    for emp_id, vol in erows:
                        if emp_id in op_vol_est:
                            op_vol_est[emp_id]["email"] = int(vol)
                    dept_vol_est["email"] = dept_email_total

                # Apeluri per operator
                apel_obj = next((x for x in objectives if x["tip"] == "apel"), None)
                if apel_obj:
                    arows = db.execute(
                        text("""
                            SELECT edm.id as emp_id, COUNT(*) as vol
                            FROM cts_calls_ground_truth cgt
                            JOIN employee_department_mapping edm ON lower(edm.email) = lower(cgt.cts_assignee_email)
                            WHERE edm.department = :dept
                              AND date_trunc('month', cgt.cts_started_at AT TIME ZONE 'Europe/Bucharest') =
                                  date_trunc('month', CAST(:hfirst AS timestamp))
                            GROUP BY edm.id
                        """),
                        {"dept": department, "hfirst": lhfirst},
                    ).fetchall()
                    dept_apel_total = sum(r[1] for r in arows)
                    for emp_id, vol in arows:
                        if emp_id in op_vol_est:
                            op_vol_est[emp_id]["apel"] = int(vol)
                    dept_vol_est["apel"] = dept_apel_total

                # Task-uri per operator
                task_obj = next((x for x in objectives if x["tip"] in ("task", "device_ops")), None)
                if task_obj:
                    trows = db.execute(
                        text("""
                            SELECT tgt.assignee_employee_id as emp_id, COUNT(*) as vol
                            FROM cts_task_ground_truth tgt
                            JOIN employee_department_mapping edm ON edm.id = tgt.assignee_employee_id
                            WHERE edm.department = :dept AND edm.enabled = true
                              AND lower(tgt.status) = 'solved'
                              AND date_trunc('month', tgt.cts_updated_at AT TIME ZONE 'Europe/Bucharest') =
                                  date_trunc('month', CAST(:hfirst AS timestamp))
                            GROUP BY tgt.assignee_employee_id
                        """),
                        {"dept": department, "hfirst": lhfirst},
                    ).fetchall()
                    dept_task_total = sum(r[1] for r in trows)
                    for emp_id, vol in trows:
                        if emp_id in op_vol_est:
                            op_vol_est[emp_id]["task"] = int(vol)
                    dept_vol_est["task"] = dept_task_total

        # Scala volumele estimate per operator proportional cu zile_lucratoare luna tinta
        lhdays_safe = max(lhdays, 1) if last_hist and lhdays > 0 else 1
        scale = zile_lucratoare / lhdays_safe

        for oid in op_ids:
            ov = op_vol_est.get(oid, {})
            vol_email = int(round(ov.get("email", 0) * scale))
            vol_apel = int(round(ov.get("apel", 0) * scale))
            vol_task = int(round(ov.get("task", 0) * scale))
            volum_total = vol_email + vol_apel + vol_task

            # Cotizatie ca proportie din volumul estimat al dept
            def _cotiz_est(tip_key, vol_op):
                dept_v = int(round(dept_vol_est.get(tip_key, 0) * scale))
                return round(100.0 * vol_op / dept_v, 2) if dept_v > 0 else None

            leave_zile = leave_days_per_emp.get(oid, 0)
            operatori.append({
                "id": oid,
                "name": op_meta[oid]["name"],
                "mails": vol_email,
                "volum_total": volum_total,
                "cotizare": _cotiz_est("email", vol_email),
                "measurable": vol_email,
                "measurable_total": volum_total,
                "achieved": obiectiv_atins,
                "vol_email": vol_email,
                "vol_task": vol_task,
                "vol_apel": vol_apel,
                "cotiz_email": _cotiz_est("email", vol_email),
                "cotiz_task": _cotiz_est("task", vol_task),
                "cotiz_apel": _cotiz_est("apel", vol_apel),
                "leave_days_planned": leave_zile,
            })
        operatori.sort(key=lambda x: x["volum_total"], reverse=True)

    volum_total_dept = sum(o["total"] for o in obiective_out)
    measurable_total = sum(o["measurable"] for o in obiective_out)

    return {
        "department": department,
        "month": f"{year:04d}-{month:02d}",
        "is_forecast": True,
        "baza_procent": cfg["baza_procent"],
        "zile_lucratoare": zile_lucratoare,
        "ore_planificate": round(float(ore_planificate), 2),
        "ore_disponibile": round(float(ore_disponibile), 2),
        # Derivat din ore, ca sa fie definit pe AMBELE ramuri: cand exista snapshot lunar,
        # `ore_concediu` nu se calculeaza (vin doar orele din snapshot), iar referinta directa
        # arunca UnboundLocalError -> forecast_report crapa pe TOATE departamentele, deci
        # dashboard-ul afisa lista de obiective goala (constatat 2026-07-29).
        "ore_concediu": round(max(0.0, float(ore_planificate) - float(ore_disponibile)), 2),
        "coeficient": coeficient,
        "obiectiv_real": round(obiectiv_real, 2),
        "obiectiv_minim": round(obiectiv_minim, 2),
        "obiectiv_atins": obiectiv_atins,
        "status": status,
        "volum": volum_total_dept,
        "excluse": 0,
        "measurable": measurable_total,
        "obiective": obiective_out,
        "operatori": operatori,
        "note_masurare": "Estimare bazata pe media ultimelor 2 luni disponibile.",
    }


def analytics_report(db: Session, departments: list, date_from: _dt.date, date_to: _dt.date, user_id: int = None) -> dict:
    """Analytics descriptiv peste [date_from, date_to] inclusiv, pt lista de departamente.

    Metrici bine-definite pe ORICE interval (spre deosebire de scorul lunar obiectiv_real):
    volum, serie zilnica (volum + % in-timp), distributie pe categorii, timp de solutionare
    (mediu/median + histograma), comparatie per departament & top operatori. Durata =
    acelasi interval created->solved ca in raportul lunar. In-timp% se calculeaza doar pe
    emailurile in-scope (categoria are obiectiv la dept-ul operatorului) si masurabile.
    """
    departments = list(departments or [])
    empty = {
        "from": date_from.isoformat(), "to": date_to.isoformat(),
        "departments_scope": departments, "volum": 0, "measurable": 0,
        "avg_min": None, "median_min": None, "in_timp_pct": None,
        "daily": [], "categorii": [],
        "buckets": [{"label": b[1], "count": 0} for b in _ANA_BUCKETS],
        "departamente": [], "operatori": [],
    }
    if not departments or date_to < date_from:
        return empty

    holidays_arr = _holidays_date_list(get_holidays(db))
    # Cache unic pentru calcul business_minutes in Python (elimina round-trip-uri SQL per rand)
    biz = _make_biz_cache(db, departments, date_from, date_to, holidays_arr)

    # obiective per dept -> limite pt in-timp
    obj_by_dept = {}
    for d in departments:
        cat_lim, general = {}, None
        for o in get_objectives(db, d):
            if o["categorie"]:
                cat_lim[o["categorie"]] = o["limita_minute"]
            else:
                general = o["limita_minute"]
        obj_by_dept[d] = (cat_lim, general)

    def _limit_for(dept, cat):
        cfg = obj_by_dept.get(dept)
        if not cfg:
            return None
        cat_lim, general = cfg
        if general is not None:
            return general
        return cat_lim.get(cat)

    uid_filter = "AND edm.id = :uid" if user_id is not None else ""
    uid_params = {"uid": user_id} if user_id is not None else {}
    raw_rows = db.execute(
        text(rf"""
            SELECT edm.id AS op_id, edm.name AS op_name, edm.department AS dept,
                   lower(COALESCE(NULLIF(cgt.cts_category,''), e.ai_category)) AS categorie,
                   (COALESCE(cgt.cts_solved_at, cgt.cts_solved_seen_at, cgt.cts_reply_at)
                        AT TIME ZONE 'Europe/Bucharest')::date AS solved_day,
                   -- raw timestamps pt calcul Python
                   {_EMAIL_START_SQL.format(g='cgt')} AS created_ts,
                   cgt.cts_in_progress_at AS in_progress_ts,
                   COALESCE(cgt.cts_solved_at, cgt.cts_reply_at) AS solved_ts
            FROM cts_ground_truth cgt
            JOIN employee_department_mapping edm ON lower(cgt.cts_assignee_email) = lower(edm.email)
            LEFT JOIN emails e ON e.id = cgt.email_id
            LEFT JOIN clients pex ON pex.id = e.client_id
            WHERE cgt.cts_status='solved' AND cgt.cts_direction='received'
              AND edm.enabled=true AND edm.department = ANY(:depts)
              AND {_EMAIL_EXCLUDE_SQL}
              """ + uid_filter + """
              AND (COALESCE(cgt.cts_solved_at, cgt.cts_solved_seen_at, cgt.cts_reply_at)
                       AT TIME ZONE 'Europe/Bucharest')::date BETWEEN :df AND :dt
        """),
        {"depts": departments, "df": date_from, "dt": date_to, **uid_params},
    ).fetchall()

    # calcul TTC/TTS in Python via biz cache (elimina business_minutes_emp() per rand)
    rows = []
    for op_id, op_name, dept, cat, solved_day, created_ts, in_progress_ts, solved_ts in raw_rows:
        has_ip = (in_progress_ts is not None and solved_ts is not None
                  and in_progress_ts < solved_ts)
        ttc_mins = biz.business_minutes(dept, op_id, created_ts, in_progress_ts) if has_ip else None
        if has_ip:
            tts_mins = biz.business_minutes(dept, op_id, in_progress_ts, solved_ts)
        else:
            tts_mins = biz.business_minutes(dept, op_id, created_ts, solved_ts)
        rows.append((op_id, op_name, dept, cat, solved_day, ttc_mins, tts_mins))

    # acumulatori (serie zilnica pre-populata pt continuitate)
    daily = {}
    dd = date_from
    one = _dt.timedelta(days=1)
    while dd <= date_to:
        daily[dd.isoformat()] = {"volum": 0, "scope_meas": 0, "in_timp": 0}
        dd += one
    cats = {}
    buckets = [0] * len(_ANA_BUCKETS)
    dep_agg = {d: {"volum": 0, "meas": 0, "sum_mins": 0.0, "scope_meas": 0, "in_timp": 0,
                   "sum_ttc": 0.0, "ttc_meas": 0, "sum_tts": 0.0, "tts_meas": 0}
               for d in departments}
    op_agg = {}
    all_mins = []
    sum_mins = 0.0
    meas_count = 0
    scope_meas = 0
    in_timp = 0
    volum = 0
    sum_ttc = 0.0; ttc_meas = 0
    sum_tts = 0.0; tts_meas = 0

    for op_id, op_name, dept, cat, solved_day, ttc_mins, mins in rows:
        volum += 1
        dkey = solved_day.isoformat() if solved_day else None
        catkey = cat or "necunoscut"
        cats[catkey] = cats.get(catkey, 0) + 1
        if dkey in daily:
            daily[dkey]["volum"] += 1
        if dept in dep_agg:
            dep_agg[dept]["volum"] += 1
        op = op_agg.get(op_id)
        if op is None:
            op = op_agg[op_id] = {"name": op_name, "department": dept, "mails": 0,
                                  "meas": 0, "sum_mins": 0.0, "scope_meas": 0, "in_timp": 0,
                                  "sum_ttc": 0.0, "ttc_meas": 0, "sum_tts": 0.0, "tts_meas": 0}
        op["mails"] += 1
        # TTC acumulare
        tc = float(ttc_mins) if ttc_mins is not None else None
        if tc is not None and tc >= 0:
            sum_ttc += tc; ttc_meas += 1
            dep_agg[dept]["sum_ttc"] += tc; dep_agg[dept]["ttc_meas"] += 1
            op["sum_ttc"] += tc; op["ttc_meas"] += 1
        # TTS = mins (InProgress→Solved sau fallback Created→Solved); 0 e durata valida
        m = float(mins) if mins is not None else None
        if m is not None and m >= 0:
            all_mins.append(m)
            sum_mins += m
            meas_count += 1
            buckets[_bucket_index(m)] += 1
            if dept in dep_agg:
                dep_agg[dept]["meas"] += 1
                dep_agg[dept]["sum_mins"] += m
            op["meas"] += 1
            op["sum_mins"] += m
            # TTS acumulare (doar când cts_in_progress_at exista = ttc_mins IS NOT NULL)
            if tc is not None:
                sum_tts += m; tts_meas += 1
                dep_agg[dept]["sum_tts"] += m; dep_agg[dept]["tts_meas"] += 1
                op["sum_tts"] += m; op["tts_meas"] += 1
            lim = _limit_for(dept, cat)
            if lim is not None:
                scope_meas += 1
                if dkey in daily:
                    daily[dkey]["scope_meas"] += 1
                if dept in dep_agg:
                    dep_agg[dept]["scope_meas"] += 1
                op["scope_meas"] += 1
                if m <= lim:
                    in_timp += 1
                    if dkey in daily:
                        daily[dkey]["in_timp"] += 1
                    if dept in dep_agg:
                        dep_agg[dept]["in_timp"] += 1
                    op["in_timp"] += 1

    def _pct(n, d):
        return round(100.0 * n / d, 2) if d > 0 else None

    def _avg(s, c):
        return round(s / c, 1) if c > 0 else None

    daily_out = [{"day": k, "volum": daily[k]["volum"],
                  "in_timp": _pct(daily[k]["in_timp"], daily[k]["scope_meas"])}
                 for k in sorted(daily.keys())]

    cats_out = sorted(
        [{"categorie": c, "volum": n, "pct": _pct(n, volum)} for c, n in cats.items()],
        key=lambda x: x["volum"], reverse=True,
    )

    buckets_out = [{"label": _ANA_BUCKETS[i][1], "count": buckets[i]} for i in range(len(_ANA_BUCKETS))]

    dep_out = [{"department": d, "volum": dep_agg[d]["volum"],
                "avg_min": _avg(dep_agg[d]["sum_mins"], dep_agg[d]["meas"]),
                "avg_ttc_min": _avg(dep_agg[d]["sum_ttc"], dep_agg[d]["ttc_meas"]),
                "avg_tts_min": _avg(dep_agg[d]["sum_tts"], dep_agg[d]["tts_meas"]),
                "in_timp_pct": _pct(dep_agg[d]["in_timp"], dep_agg[d]["scope_meas"])}
               for d in departments]
    dep_out.sort(key=lambda x: x["volum"], reverse=True)

    median_min = round(statistics.median(all_mins), 1) if all_mins else None

    # ---- task-uri ----
    task_uid_filter = "AND edm.id = :uid" if user_id is not None else ""
    raw_task_rows = db.execute(
        text(r"""
            SELECT edm.id AS op_id,
                   edm.department AS dept,
                   (ctgt.cts_updated_at AT TIME ZONE 'Europe/Bucharest')::date AS solved_day,
                   ctgt.task_type,
                   ctgt.cts_created_at AS created_ts,
                   ctgt.cts_in_progress_at AS in_progress_ts,
                   ctgt.cts_updated_at AS updated_ts
            FROM cts_task_ground_truth ctgt
            JOIN employee_department_mapping edm ON edm.id = ctgt.assignee_employee_id
            WHERE lower(ctgt.status) = 'solved'
              AND ctgt.cts_created_at IS NOT NULL AND ctgt.cts_updated_at IS NOT NULL
              AND edm.enabled = true AND edm.department = ANY(:depts)
              """ + task_uid_filter + """
              AND (ctgt.cts_updated_at AT TIME ZONE 'Europe/Bucharest')::date BETWEEN :df AND :dt
        """),
        {"depts": departments, "df": date_from, "dt": date_to, **({"uid": user_id} if user_id is not None else {})},
    ).fetchall()

    # calcul TTC/TTS task in Python via biz cache
    task_rows = []
    for op_id, dept, solved_day, task_type, created_ts, in_progress_ts, updated_ts in raw_task_rows:
        has_ip = (in_progress_ts is not None and updated_ts is not None
                  and in_progress_ts < updated_ts and created_ts is not None)
        ttc_mins = biz.business_minutes(dept, op_id, created_ts, in_progress_ts) if has_ip else None
        if has_ip:
            tts_mins = biz.business_minutes(dept, op_id, in_progress_ts, updated_ts)
        else:
            tts_mins = biz.business_minutes(dept, op_id, created_ts, updated_ts)
        task_rows.append((op_id, solved_day, task_type, ttc_mins, tts_mins))

    # SLA task per familie: {None: 120, 'cargobox': 8400, ...} -- agregat din toate dept-urile scope
    # None = limita generala (non-cargobox); familii specifice suprascriu
    task_lim_by_family: dict = {}
    for d in departments:
        for o in get_objectives(db, d, tip=None):
            if o["tip"] != "task" or not o.get("limita_minute"):
                continue
            fam_key = (o["categorie"] or "").strip().lower() or None
            if fam_key not in task_lim_by_family:
                task_lim_by_family[fam_key] = float(o["limita_minute"])
    task_lim_general = task_lim_by_family.get(None)  # backward compat

    def _task_lim_for(task_type: Optional[str]) -> Optional[float]:
        """Returneaza SLA-ul corect pt un task_type: CargoBox=8400, general=120, altfel None."""
        fam = None
        if task_type:
            norm = task_type.lower().replace(" ", "").replace("-", "").replace(":", "")
            for kw in _TASK_FAMILY_KEYWORDS:
                if kw in norm:
                    fam = kw
                    break
        return task_lim_by_family.get(fam, task_lim_general)

    t_daily = {k: {"volum": 0, "scope_meas": 0, "in_timp": 0} for k in daily.keys()}
    t_buckets = [0] * len(_ANA_BUCKETS)
    t_all_mins, t_sum_mins, t_meas, t_scope_meas, t_in_timp, t_volum = [], 0.0, 0, 0, 0, 0
    t_sum_ttc, t_ttc_meas, t_sum_tts, t_tts_meas = 0.0, 0, 0.0, 0
    # per-family accumulators: {fam_key: {volum, scope_meas, in_timp, sum_mins, meas, lim}}
    t_fam: dict = {}
    for op_id, solved_day, task_type, ttc_mins, mins in task_rows:
        t_volum += 1
        dkey = solved_day.isoformat() if solved_day else None
        if dkey in t_daily:
            t_daily[dkey]["volum"] += 1
        m = float(mins) if mins is not None else None
        tc = float(ttc_mins) if ttc_mins is not None else None
        t_lim = _task_lim_for(task_type)
        if op_id in op_agg:
            op_agg[op_id].setdefault("tasks", 0)
            op_agg[op_id]["tasks"] += 1
        # TTC task acumulare
        if tc is not None and tc >= 0:
            t_sum_ttc += tc; t_ttc_meas += 1
            if op_id in op_agg:
                op_agg[op_id].setdefault("task_sum_ttc", 0.0)
                op_agg[op_id].setdefault("task_ttc_meas", 0)
                op_agg[op_id]["task_sum_ttc"] += tc
                op_agg[op_id]["task_ttc_meas"] += 1
        if m is not None and m >= 0:
            t_all_mins.append(m)
            t_sum_mins += m
            t_meas += 1
            t_buckets[_bucket_index(m)] += 1
            if op_id in op_agg:
                op_agg[op_id].setdefault("task_meas", 0)
                op_agg[op_id].setdefault("task_sum_mins", 0.0)
                op_agg[op_id]["task_meas"] += 1
                op_agg[op_id]["task_sum_mins"] += m
            # TTS task acumulare (doar când cts_in_progress_at există)
            if tc is not None:
                t_sum_tts += m; t_tts_meas += 1
                if op_id in op_agg:
                    op_agg[op_id].setdefault("task_sum_tts", 0.0)
                    op_agg[op_id].setdefault("task_tts_meas", 0)
                    op_agg[op_id]["task_sum_tts"] += m
                    op_agg[op_id]["task_tts_meas"] += 1
            # per-family acumulare
            fam_key = None
            if task_type:
                norm = task_type.lower().replace(" ", "").replace("-", "").replace(":", "")
                for kw in _TASK_FAMILY_KEYWORDS:
                    if kw in norm:
                        fam_key = kw
                        break
            fam_label = fam_key.title() if fam_key else "General"
            if fam_label not in t_fam:
                t_fam[fam_label] = {"volum": 0, "scope_meas": 0, "in_timp": 0, "lim": t_lim}
            t_fam[fam_label]["volum"] += 1
            if t_lim is not None:
                t_scope_meas += 1
                t_fam[fam_label]["scope_meas"] += 1
                if dkey in t_daily:
                    t_daily[dkey]["scope_meas"] += 1
                if op_id in op_agg:
                    op_agg[op_id].setdefault("task_scope_meas", 0)
                    op_agg[op_id]["task_scope_meas"] += 1
                if m <= t_lim:
                    t_in_timp += 1
                    t_fam[fam_label]["in_timp"] += 1
                    if dkey in t_daily:
                        t_daily[dkey]["in_timp"] += 1
                    if op_id in op_agg:
                        op_agg[op_id].setdefault("task_in_timp", 0)
                        op_agg[op_id]["task_in_timp"] += 1

    tasks_out = {
        "volum": t_volum, "measurable": t_meas,
        "avg_min": _avg(t_sum_mins, t_meas),
        "avg_ttc_min": _avg(t_sum_ttc, t_ttc_meas),
        "avg_tts_min": _avg(t_sum_tts, t_tts_meas),
        "median_min": round(statistics.median(t_all_mins), 1) if t_all_mins else None,
        "in_timp_pct": _pct(t_in_timp, t_scope_meas),
        "daily": [{"day": k, "volum": t_daily[k]["volum"],
                   "in_timp": _pct(t_daily[k]["in_timp"], t_daily[k]["scope_meas"])}
                  for k in sorted(t_daily.keys())],
        "buckets": [{"label": _ANA_BUCKETS[i][1], "count": t_buckets[i]} for i in range(len(_ANA_BUCKETS))],
        "by_family": [{"label": lbl, "volum": v["volum"], "in_timp_pct": _pct(v["in_timp"], v["scope_meas"]), "lim_min": v["lim"]} for lbl, v in sorted(t_fam.items())],
    }

    # ---- apeluri ----
    apel_uid_filter = "AND edm.id = :uid" if user_id is not None else ""
    # Aceeasi sursa ca raportul lunar: While1, doar apeluri primite (vezi _fetch_apel_rows).
    apel_rows = db.execute(
        text(f"""
            {_APEL_AGENT_CTE}
            SELECT edm.id AS op_id,
                   {_APEL_DAY_SQL} AS call_day,
                   {_APEL_MINS_SQL} AS secs,
                   c.duration_seconds AS dur_secs
            FROM calls c
            {_APEL_AGENT_JOIN}
            LEFT JOIN department_attendance da
                ON da.department = edm.department
                AND da.work_date = {_APEL_DAY_SQL}
            LEFT JOIN department_schedule ds
                ON ds.department = edm.department
                AND ds.weekday = EXTRACT(ISODOW FROM c.started_at)::int
                AND ds.active = true
            LEFT JOIN (
                SELECT DISTINCT department, true AS configured
                FROM department_schedule WHERE active = true
            ) dsc ON dsc.department = edm.department
            WHERE edm.department = ANY(:depts)
              AND c.direction = 'inbound'
              AND {_APEL_REAL_CALL_SQL}
              """ + apel_uid_filter + f"""
              AND {_APEL_DAY_SQL} BETWEEN :df AND :dt
              AND (
                CASE
                  WHEN da.work_date IS NOT NULL THEN da.present = true
                  WHEN ds.weekday IS NOT NULL THEN true
                  WHEN dsc.configured IS NULL THEN true
                  ELSE false
                END
              )
        """),
        {"depts": departments, "df": date_from, "dt": date_to, **({"uid": user_id} if user_id is not None else {})},
    ).fetchall()

    # SLA apel: limita in secunde (tip='apel', unitate='secunde')
    apel_lim = None
    for d in departments:
        for o in get_objectives(db, d, tip=None):
            if o["tip"] == "apel" and o.get("limita_minute"):
                apel_lim = float(o["limita_minute"])
                break
        if apel_lim is not None:
            break

    a_daily = {k: {"volum": 0, "scope_meas": 0, "in_timp": 0} for k in daily.keys()}
    a_all_secs, a_sum_secs, a_meas, a_scope_meas, a_in_timp, a_volum = [], 0.0, 0, 0, 0, 0
    a_all_dur, a_sum_dur, a_dur_meas = [], 0.0, 0
    for op_id, call_day, secs, dur_secs in apel_rows:
        a_volum += 1
        dkey = call_day.isoformat() if call_day else None
        if dkey in a_daily:
            a_daily[dkey]["volum"] += 1
        s = float(secs) if secs is not None else None
        d = float(dur_secs) if dur_secs is not None else None
        if op_id in op_agg:
            op_agg[op_id].setdefault("apel_volum", 0)
            op_agg[op_id]["apel_volum"] += 1
        if s is not None and s >= 0:
            a_all_secs.append(s)
            a_sum_secs += s
            a_meas += 1
            if op_id in op_agg:
                op_agg[op_id].setdefault("apel_sum_secs", 0.0)
                op_agg[op_id].setdefault("apel_meas", 0)
                op_agg[op_id]["apel_sum_secs"] += s
                # Numaram separat apelurile CU timp de raspuns: de cand sursa e While1, un apel
                # primit poate exista fara el (NO ANSWER, sau ring_seconds inca necompletat pe
                # randurile ingerate inainte de migrarea 20260813c). Media trebuie impartita la
                # cate masuratori avem, nu la volumul total -- altfel iese artificial mica.
                op_agg[op_id]["apel_meas"] += 1
            if apel_lim is not None:
                a_scope_meas += 1
                if dkey in a_daily:
                    a_daily[dkey]["scope_meas"] += 1
                if op_id in op_agg:
                    op_agg[op_id].setdefault("apel_scope_meas", 0)
                    op_agg[op_id]["apel_scope_meas"] += 1
                if s <= apel_lim:
                    a_in_timp += 1
                    if dkey in a_daily:
                        a_daily[dkey]["in_timp"] += 1
                    if op_id in op_agg:
                        op_agg[op_id].setdefault("apel_in_timp", 0)
                        op_agg[op_id]["apel_in_timp"] += 1
        if d is not None and d >= 10:
            a_all_dur.append(d)
            a_sum_dur += d
            a_dur_meas += 1

    _APEL_BUCKETS = [(60, "<1 min"), (180, "1-3 min"), (300, "3-5 min"), (600, "5-10 min"), (None, ">10 min")]
    a_buckets = [0] * len(_APEL_BUCKETS)
    for d in a_all_dur:
        for i, (hi, _) in enumerate(_APEL_BUCKETS):
            if hi is None or d < hi:
                a_buckets[i] += 1
                break
    apeluri_out = {
        "volum": a_volum, "measurable": a_meas,
        "avg_response_sec": round(a_sum_secs / a_meas, 1) if a_meas > 0 else None,
        "avg_duration_sec": round(a_sum_dur / a_dur_meas, 1) if a_dur_meas > 0 else None,
        "median_duration_sec": round(statistics.median(a_all_dur), 1) if a_all_dur else None,
        "in_timp_pct": _pct(a_in_timp, a_scope_meas),
        "daily": [{"day": k, "volum": a_daily[k]["volum"],
                   "in_timp": _pct(a_daily[k]["in_timp"], a_daily[k]["scope_meas"])}
                  for k in sorted(a_daily.keys())],
        "buckets": [{"label": _APEL_BUCKETS[i][1], "count": a_buckets[i]} for i in range(len(_APEL_BUCKETS))],
    }

    # reconstruim op_out dupa ce am adaugat tasks/apeluri in op_agg
    op_out = [{"id": oid, "name": a["name"], "department": a["department"], "mails": a["mails"],
               "avg_min": _avg(a["sum_mins"], a["meas"]),
               "avg_ttc_min": _avg(a.get("sum_ttc", 0.0), a.get("ttc_meas", 0)),
               "avg_tts_min": _avg(a.get("sum_tts", 0.0), a.get("tts_meas", 0)),
               "in_timp_pct": _pct(a["in_timp"], a["scope_meas"]),
               "tasks": a.get("tasks", 0),
               "task_avg_min": _avg(a.get("task_sum_mins", 0.0), a.get("task_meas", 0)),
               "task_avg_ttc_min": _avg(a.get("task_sum_ttc", 0.0), a.get("task_ttc_meas", 0)),
               "task_avg_tts_min": _avg(a.get("task_sum_tts", 0.0), a.get("task_tts_meas", 0)),
               "task_in_timp_pct": _pct(a.get("task_in_timp", 0), a.get("task_scope_meas", 0)),
               "apel_volum": a.get("apel_volum", 0),
               "apel_avg_sec": (round(a["apel_sum_secs"] / a["apel_meas"], 1)
                                if a.get("apel_meas", 0) > 0 else None),
               "apel_in_timp_pct": _pct(a.get("apel_in_timp", 0), a.get("apel_scope_meas", 0))}
              for oid, a in op_agg.items()]
    op_out.sort(key=lambda x: x["mails"], reverse=True)
    op_out = op_out[:20]

    return {
        "from": date_from.isoformat(), "to": date_to.isoformat(),
        "departments_scope": departments,
        "volum": volum, "measurable": meas_count,
        "avg_min": _avg(sum_mins, meas_count), "median_min": median_min,
        "avg_ttc_min": _avg(sum_ttc, ttc_meas),
        "avg_tts_min": _avg(sum_tts, tts_meas),
        "in_timp_pct": _pct(in_timp, scope_meas),
        "daily": daily_out, "categorii": cats_out, "buckets": buckets_out,
        "departamente": dep_out, "operatori": op_out,
        "tasks": tasks_out,
        "apeluri": apeluri_out,
    }
