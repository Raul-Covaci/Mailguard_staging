"""Preluarea unei reclamatii (NEW -> In progress) curge pe programul departamentului din CTS.

Cache-ul de program e construit fara DB (doar `department_schedule`, fara pontaj), ca sa se vada
exact efectul programului: Suport 1 07:00-21:00, Suport 3 08:00-16:30.
"""
import datetime as dt
from zoneinfo import ZoneInfo

from app.services import productivity as P
from app.services.reclamatie_dept import contact_clock_dept

RO = ZoneInfo("Europe/Bucharest")
SLUGS = {"1": "suport_1", "15": "suport_2", "34": "suport_3", "2": "comercial"}


def _clock():
    c = P._BizCache.__new__(P._BizCache)
    c.holidays = set()
    c._sched = {}
    for wd in range(1, 6):
        c._sched[("suport_1", wd)] = (dt.time(7, 0), dt.time(21, 0), False)
        c._sched[("suport_3", wd)] = (dt.time(8, 0), dt.time(16, 30), False)
    c._att, c._dept_day_count, c._dept_day_union = {}, {}, {}
    c._dept_day_any, c._dept_emp, c._leave, c._all_leave_cache = set(), {}, {}, {}
    return c


def test_dept_choice():
    elig = P._DEPT_WINDOW_DEPARTMENTS
    assert contact_clock_dept("suport_3", 1, SLUGS, elig) == "suport_1"
    assert contact_clock_dept("suport_3", "15", SLUGS, elig) == "suport_2"
    assert contact_clock_dept("suport_3", 34, SLUGS, elig) == "suport_3"
    # fara program in aplicatie / netradus / fara departament -> programul Suport 3
    assert contact_clock_dept("suport_3", 2, SLUGS, elig) == "suport_3"
    assert contact_clock_dept("suport_3", 17, SLUGS, elig) == "suport_3"
    assert contact_clock_dept("suport_3", None, SLUGS, elig) == "suport_3"


def test_evening_complaint_on_suport_1_counts_suport_1_hours():
    c = _clock()
    start = dt.datetime(2026, 9, 14, 17, 0, tzinfo=RO)       # luni 17:00
    taken = dt.datetime(2026, 9, 15, 9, 0, tzinfo=RO)        # marti 09:00
    s1 = c.business_minutes(contact_clock_dept("suport_3", 1, SLUGS, P._DEPT_WINDOW_DEPARTMENTS),
                            None, start, taken)
    s3 = c.business_minutes("suport_3", None, start, taken)
    assert s1 == 4 * 60 + 2 * 60        # 17-21 + 07-09 = 360 min -> overdue la limita de 240
    assert s3 == 60                     # 08-09 = 60 min -> on time pe programul vechi
