"""Pe al cui program curge SLA-ul de preluare al unei reclamatii (NEW -> In progress).

Reclamatiile se scoreaza integral la Suport 3, dar preluarea se masoara pe programul
departamentului pe care e deschisa reclamatia in CTS (`cts_quality_evaluation.department_id`):
Suport 1 / Suport 2 lucreaza pina seara, Suport 3 doar 08:00-16:30. Decizie business 2026-09-30.

Departamentul CTS se traduce in slug local prin departamentul dominant al angajatilor din acel
`department_id` (aceeasi treapta ca `cts_email_log._DEPT_MAP_CTE`), NICIODATA pe egalitate de nume.
Fara departament, cu un ID netradus sau cu un slug fara program in aplicatie (comercial,
instalari, ...) ramine programul departamentului scorat.
"""
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.cts_email_log import _DEPT_MAP_CTE


def cts_dept_slug_map(db: Session) -> dict:
    """{department_id CTS (text): slug local}."""
    rows = db.execute(text(f"WITH {_DEPT_MAP_CTE} SELECT cts_dept_id, slug FROM dept_map")).fetchall()
    return {str(r[0]): r[1] for r in rows}


def contact_clock_dept(scored_dept: str, cts_dept_id, slug_map: dict, eligible) -> str:
    """Departamentul al carui program masoara preluarea; `eligible` = departamentele cu fereastra."""
    slug: Optional[str] = slug_map.get(str(cts_dept_id)) if cts_dept_id is not None else None
    return slug if slug in eligible else scored_dept
