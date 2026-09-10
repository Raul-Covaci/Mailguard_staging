"""Reconstruieste `employee_department_history` din pontaj (`employee_attendance`).

Migratia 20260911 seedeaza un singur interval per angajat, cu departamentul de AZI valabil de la
2000-01-01 — corect ca sa nu se goleasca rapoartele, dar nu stie de mutarile din trecut. Scriptul
asta deduce mutarile reale din pontaj: `employee_attendance.department` e scris de `pontaj_sync`
la momentul sincronizarii, deci pe zilele care nu au fost re-sincronizate poarta departamentul de
atunci.

⚠️ LIMITA: o zi RE-sincronizata dupa o mutare poarta departamentul NOU (pontaj_sync cauta omul in
bucketul departamentului curent, vezi pontaj_sync.py). Deci rezultatul e o aproximatie —
corectiile fine se fac din UI (Utilizatori -> angajat -> Istoric departament).

Reguli:
  * per (angajat, luna) se ia departamentul dominant dupa zile PREZENTE; la egalitate castiga cel
    cu ultima zi mai recenta (o mutare la mijloc de luna se termina in departamentul nou);
  * lunile consecutive cu acelasi departament se comprima intr-un singur interval;
  * lunile fara pontaj nu creeaza gol: primul interval se intinde inapoi pana la 2000-01-01, iar o
    pauza intre doua luni cu acelasi departament e absorbita;
  * ultimul interval primeste departamentul CURENT din `employee_department_mapping` (scalarul e
    adevarul pentru azi); pentru angajatii dezactivati se inchide dupa ultima luna cu pontaj;
  * angajatii care au deja un interval `manual` (corectat din UI) sau `trigger` (mutare observata
    in DB) sunt SARITI complet — o deductie din pontaj nu suprascrie o observatie.

Rulare:
  cd /opt/iris-mailguard && sudo venv/bin/python -m scripts.backfill_employee_department_history
  ... --apply            scrie efectiv (implicit: dry-run, nu comite nimic)
  ... --employee 15      doar un angajat
"""
import sys
import datetime as _dt
from collections import defaultdict

from sqlalchemy import text
from app.database import SessionLocal

EPOCH = _dt.date(2000, 1, 1)


def _month_start(d: _dt.date) -> _dt.date:
    return d.replace(day=1)


def _next_month(d: _dt.date) -> _dt.date:
    return (d.replace(day=1) + _dt.timedelta(days=32)).replace(day=1)


def _monthly_picks(db, emp_filter):
    """(emp_id) -> {luna: departament} — departamentul dominant al lunii, din pontaj."""
    q = ("SELECT employee_id, date_trunc('month', work_date)::date AS m, department, "
         "       count(*) FILTER (WHERE present) AS zile, max(work_date) AS ultima "
         "FROM employee_attendance "
         "WHERE employee_id IS NOT NULL AND department IS NOT NULL ")
    params = {}
    if emp_filter:
        q += "AND employee_id = :eid "
        params["eid"] = emp_filter
    q += "GROUP BY 1, 2, 3"
    agg = defaultdict(dict)   # emp -> month -> (zile, ultima, dept)
    for emp_id, m, dept, zile, ultima in db.execute(text(q), params).fetchall():
        cur = agg[int(emp_id)].get(m)
        cand = (int(zile or 0), ultima, dept)
        if cur is None or (cand[0], cand[1]) > (cur[0], cur[1]):
            agg[int(emp_id)][m] = cand
    return {emp: {m: v[2] for m, v in months.items()} for emp, months in agg.items()}


def _chain(picks_by_month: dict, current_dept: str, enabled: bool):
    """{luna: dept} -> [(dept, valid_from, valid_to)] fara goluri."""
    months = sorted(picks_by_month)
    if not months:
        return [(current_dept, EPOCH, None if enabled else _month_start(_dt.date.today()))]
    chain = []
    for m in months:
        d = picks_by_month[m]
        if chain and chain[-1][0] == d:
            continue
        chain.append((d, m, None))
    # inceputul se intinde pana la epoca, ca nicio luna veche sa nu ramana fara departament
    chain[0] = (chain[0][0], EPOCH, None)
    out = []
    for i, (d, start, _) in enumerate(chain):
        end = chain[i + 1][1] if i + 1 < len(chain) else None
        out.append((d, start, end))
    # ultimul interval: scalarul curent e adevarul pentru azi
    d, start, _ = out[-1]
    if enabled:
        if d != current_dept:
            out.append((current_dept, _month_start(_dt.date.today()), None))
            out[-2] = (d, start, _month_start(_dt.date.today()))
        else:
            out[-1] = (d, start, None)
    else:
        out[-1] = (d, start, _next_month(months[-1]))
    return [x for x in out if x[2] is None or x[2] > x[1]]


def main():
    apply_ = "--apply" in sys.argv
    emp_filter = None
    if "--employee" in sys.argv:
        emp_filter = int(sys.argv[sys.argv.index("--employee") + 1])

    db = SessionLocal()
    try:
        emps = db.execute(text(
            "SELECT id, name, department, enabled FROM employee_department_mapping "
            + ("WHERE id = :eid " if emp_filter else "") + "ORDER BY name"
        ), ({"eid": emp_filter} if emp_filter else {})).fetchall()
        # Se sar angajatii cu istoric OBSERVAT: 'manual' (corectat de admin) si 'trigger' (mutare
        # prinsa in DB, deci reala). Pontajul e o deductie — nu are voie sa suprascrie o observatie.
        manual = {int(r[0]) for r in db.execute(text(
            "SELECT DISTINCT employee_id FROM employee_department_history "
            "WHERE source IN ('manual','trigger')"
        )).fetchall()}
        picks = _monthly_picks(db, emp_filter)

        changed = unchanged = no_pontaj = skipped_manual = 0
        for emp_id, name, dept, enabled in emps:
            emp_id = int(emp_id)
            if emp_id in manual:
                skipped_manual += 1
                continue
            months = picks.get(emp_id, {})
            if not months:
                no_pontaj += 1
                continue
            new_chain = _chain(months, dept, bool(enabled))
            old = db.execute(text(
                "SELECT department, valid_from, valid_to FROM employee_department_history "
                "WHERE employee_id=:id ORDER BY valid_from"
            ), {"id": emp_id}).fetchall()
            old_chain = [(r[0], r[1], r[2]) for r in old]
            if old_chain == new_chain:
                unchanged += 1
                continue
            changed += 1
            print(f"{name} (#{emp_id}):")
            print("   vechi: " + (" | ".join(f"{d} {f}→{t or '…'}" for d, f, t in old_chain) or "—"))
            print("   nou:   " + " | ".join(f"{d} {f}→{t or '…'}" for d, f, t in new_chain))
            if apply_:
                # Trigger-ul ar reactiona la rescriere; il oprim explicit pentru tranzactia asta.
                db.execute(text("SET LOCAL mailguard.skip_dept_history = 'on'"))
                db.execute(text("DELETE FROM employee_department_history "
                                "WHERE employee_id=:id AND source IN ('seed','backfill')"),
                           {"id": emp_id})
                for d, vfrom, vto in new_chain:
                    db.execute(text(
                        "INSERT INTO employee_department_history "
                        "(employee_id, department, valid_from, valid_to, source, created_by) "
                        "VALUES (:id, :d, CAST(:f AS date), CAST(:t AS date), 'backfill', 'script')"
                    ), {"id": emp_id, "d": d, "f": vfrom, "t": vto})
                db.commit()

        print(f"\nmodificati: {changed}, neschimbati: {unchanged}, "
              f"fara pontaj: {no_pontaj}, sariti (manual): {skipped_manual}")
        if not apply_:
            print("DRY-RUN — nimic nu s-a scris. Adauga --apply.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
