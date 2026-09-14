"""OPS-2026-0132 — Sincronizare zilnica a listei de angajati CargoTrack din IRIS.

Inlocuieste introducerea manuala din Setari -> Utilizatori (Employee Signature Matching).
Canal: IRIS Gateway (GET {iris_api_url}{employee_sync.endpoint_path}, header X-Mailguard-Key),
exact ca cts_groundtruth_sync / iris_sync. Read-only fata de IRIS. Endpoint LIVE: /cts/employees.

Payload real (un element):
  full_name, email, department, department_slug, status, work_hours, break_minutes,
  shift (mereu null -> setat MANUAL in Cargo360, sync-ul NU il atinge),
  planned_leave: [{start,end,days}]      -> CONCEDII
  leave_requests: [{start,end,status}]   -> INVOIRI ORARE (drept 3h/zi)

Reguli de import:
  * Departament: se prefera department_slug; se trece prin employee_sync.department_map
    (IRIS slug -> slug Cargo360) + normalizare cratima->underscore. Angajatii din departamente
    care NU exista in Cargo360 (HR, Marketing, Management, etc.) sunt IGNORATI (skip), NU fortati
    intr-un fallback — userul a cerut explicit doar departamentele existente in aplicatie.
  * shift: NICIODATA scris de sync (vine null din IRIS) -> ramane valoarea setata manual.
  * reconcile: randurile sync_source='iris' lipsa din feed -> enabled=false (nu se sterg);
    randurile manuale nu sunt atinse.

IMPORT PUNCTUAL (2026-07-02, modulul Task-uri): un angajat asignat pe un task CTS poate sa nu
existe local, pentru ca departamentul lui de azi nu e in whitelist. `import_employee_by_email`
il importa punctual (sync_source='task_import'), FARA sa filtreze pe departament — narrow, nu o
largire generala a whitelist-ului. Ca acest rand sa nu ramana "inghetat" (fara refresh) la
urmatorul sync normal zilnic, bucla principala din `sync_employees` a fost restructurata: cautarea
randului existent (dupa email, apoi nume) se face INAINTE de gate-ul de departament. Whitelist-ul
blocheaza DOAR crearea unui rand NOU; un rand deja existent local primeste update necondiționat
(indiferent cum a ajuns acolo), pastrand departamentul deja stocat daca cel din feed nu mai
mapeaza in whitelist.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Departamentele reale in care pot fi incadrate mail-urile in Cargo360.
VALID_DEPARTMENTS = {
    # Cele 8 departamente „de mesaje" (au prompt de clasificare AI in department_classifier)
    "suport_1", "suport_2", "suport_3",
    "taxe_drum", "contabilitate", "mobilitate",
    "recuperare_tva", "comercial",
    # Departamente care produc muncă urmărită (task-uri / operațiuni pe dispozitive) chiar dacă
    # nu primesc emailuri clasificate de AI. Lipseau din whitelist, deci angajații lor erau
    # RESPINȘI la import — ex. Adrian Jurca (`instalari`, activ în roster) nu se putea importa,
    # iar cele 132 de operațiuni ale lui nu se contorizau nicăieri (constatat 2026-07-29).
    # `employee_department_mapping` avea deja 7 oameni în `instalari`, importați pe altă cale:
    # whitelist-ul era pur și simplu în urma realității.
    "instalari", "hr", "marketing", "administrativ",
    "product_management", "management_general", "management_operational",
    "account_management", "it",
}
# Mapare implicita IRIS slug -> slug Cargo360 (peste cea din settings, daca exista).
_DEFAULT_DEPT_MAP = {
    "recuperare-tva": "recuperare_tva",
    "suport-1": "suport_1", "suport-2": "suport_2", "suport-3": "suport_3",
    "taxe-de-drum": "taxe_drum", "taxe-drum": "taxe_drum",
    "conta": "contabilitate", "contabilitate": "contabilitate",
    "mobilitate": "mobilitate", "comercial": "comercial",
    "instalari": "instalari", "instalări": "instalari",
    "resurse-umane": "hr", "hr": "hr",
    "product-management": "product_management",
    "management-general": "management_general",
    "management-operational": "management_operational", "operational": "management_operational",
    "account-management": "account_management",
    "it-team-1": "it", "it-team": "it",
}


# ── helpers settings ─────────────────────────────────────────────────────────
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
    return bool(_get_setting(db, "employee_sync.enabled", False))


def _gateway_config() -> tuple[str, str]:
    from app.config import get_settings
    base = (get_settings().iris_api_url or "").rstrip("/")
    key = os.getenv("IRIS_MAILGUARD_API_KEY", "")
    return base, key


# ── extragere defensiva campuri ──────────────────────────────────────────────
def _pick(d: Dict[str, Any], *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _to_int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _norm_dept(raw_slug, raw_name, dmap: Dict[str, str], require_whitelist: bool = True) -> Optional[str]:
    """IRIS (slug sau nume) -> slug Cargo360.

    Cu `require_whitelist=True` (implicit, folosit de sync-ul normal): intoarce None daca
    departamentul nu exista in `VALID_DEPARTMENTS` — politica existenta, neschimbata.
    Cu `require_whitelist=False` (folosit DOAR de importul punctual pe task-uri): intoarce
    slug-ul normalizat oricare-ar-fi-el, fara sa-l respinga daca nu e in whitelist — angajatul
    exista real la CargoTrack, chiar daca departamentul lui nu e unul "Cargo360 native".
    """
    s = (str(raw_slug).strip() if raw_slug else "") or (str(raw_name).strip() if raw_name else "")
    if not s:
        return None
    merged = dict(_DEFAULT_DEPT_MAP)
    merged.update(dmap or {})
    # 1) mapare directa (raw / lowercase)
    for cand in (s, s.lower()):
        if cand in merged:
            m = merged[cand]
            return m if (m in VALID_DEPARTMENTS or not require_whitelist) else None
    # 2) normalizare cratima/spatiu -> underscore, apoi mapare/validare
    norm = s.lower().replace(" ", "_").replace("-", "_")
    if norm in merged:
        norm = merged[norm]
    return norm if (norm in VALID_DEPARTMENTS or not require_whitelist) else None


def _iter_leaves(emp: Dict[str, Any]):
    """Yield (kind, leave_type, start, end, status, days, raw) — DEZACTIVAT (2026-07-31).

    Canalul `leave_requests[]` din payload-ul IRIS producea DUPLICATE: aceeasi cerere de concediu
    venea si prin DV (cts_dv_employee_vacation_request), scrisa ca 'vacation_approved', si prin
    payload, scrisa ca 'leave_request' -> afisata "ÎNVOIRE" in UI. 32 din 48 de intrari erau
    duplicat exact (acelasi angajat + aceleasi date) al unui concediu DV, iar 42 din 48 se
    intindeau pe mai multe zile, deci nu erau invoiri orare, ci concedii.

    SURSA UNICA de concedii = cts_dv_employee_vacation_request (vezi sync_vacation_from_dv),
    care aduce acum si status=1 (in asteptare), nu doar status=2 (aprobat).

    Daca IRIS expune vreodata invoiri orare REALE (sub-zi, cu ora de inceput/sfarsit), acest
    generator se poate reactiva — dar filtrat pe interval sub-zi, nu pe cereri de zile intregi.
    Intrarile manuale (entry_source='manual') NU sunt afectate.
    """
    return
    yield  # pragma: no cover -- pastreaza functia ca generator


# ── fetch ────────────────────────────────────────────────────────────────────
def fetch_employees(db: Session) -> List[Dict[str, Any]]:
    from app.services.iris_http import get_with_retry
    base, key = _gateway_config()
    if not base or not key:
        raise RuntimeError("Gateway IRIS neconfigurat (iris_api_url / IRIS_MAILGUARD_API_KEY).")
    path = _get_setting(db, "employee_sync.endpoint_path", "/cts/employees") or "/cts/employees"
    if not str(path).startswith("/"):
        path = "/" + str(path)
    r = get_with_retry(base + path, headers={"X-Mailguard-Key": key}, label=path)
    data = r.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("employees") or data.get("items") or data.get("records") or data.get("data") or []
    return []


# ── upsert unitar (reutilizat de sync-ul normal SI de importul punctual) ────
def _upsert_one_employee(db: Session, existing_id: Optional[int], name: str, dept: str, active: bool,
                         email: Optional[str], status, work_hours, break_minutes, iris_id,
                         sync_source: str) -> int:
    """Insert sau update pe employee_department_mapping. NU atinge `shift` (setare manuala)."""
    if existing_id is None:
        new_id = db.execute(text(
            "INSERT INTO employee_department_mapping "
            "(name, department, enabled, email, status, work_hours, break_minutes, "
            " sync_source, iris_id, last_synced_at, created_by) "
            "VALUES (:n,:d,:en,:e,:s,:wh,:bm,:ss,:iid, now(), 'iris-sync') RETURNING id"
        ), {"n": name, "d": dept, "en": active, "e": email, "s": status,
            "wh": work_hours, "bm": break_minutes, "ss": sync_source, "iid": iris_id}).fetchone()
        return new_id._mapping["id"]
    db.execute(text(
        "UPDATE employee_department_mapping SET "
        "name=:n, department=:d, enabled=:en, email=:e, status=:s, "
        "work_hours=:wh, break_minutes=:bm, "
        "sync_source=:ss, iris_id=:iid, last_synced_at=now(), updated_at=now() WHERE id=:id"
    ), {"n": name, "d": dept, "en": active, "e": email, "s": status,
        "wh": work_hours, "bm": break_minutes, "ss": sync_source, "iid": iris_id, "id": existing_id})
    return existing_id


def _write_employee_leaves(db: Session, emp_id: int, emp: Dict[str, Any]) -> int:
    """Sterge + re-insereaza concediile/invoirile CTS ale angajatului (idempotent).
    Intrarile cu entry_source='manual' sunt pastrate intacte."""
    db.execute(text("DELETE FROM employee_schedule WHERE employee_id=:id AND entry_source='cts' AND kind != 'vacation_approved'"), {"id": emp_id})
    n = 0
    for kind, ltype, start, end, st, days, raw in _iter_leaves(emp):
        try:
            db.execute(text(
                "INSERT INTO employee_schedule "
                "(employee_id, kind, leave_type, start_date, end_date, status, days, raw, entry_source) "
                "VALUES (:id,:k,:lt, NULLIF(:sd,'')::date, NULLIF(:ed,'')::date, :st, :days, CAST(:raw AS jsonb), 'cts') "
                "ON CONFLICT DO NOTHING"
            ), {"id": emp_id, "k": kind, "lt": ltype,
                "sd": str(start) if start else "", "ed": str(end) if end else "",
                "st": st, "days": days, "raw": json.dumps(raw)})
            n += 1
        except Exception as e:
            logger.warning("schedule insert skip (emp %s): %s", emp_id, e)
    return n


def _find_employee_row(db: Session, email: Optional[str], name: str) -> Optional[int]:
    row = None
    if email:
        row = db.execute(text(
            "SELECT id FROM employee_department_mapping WHERE lower(email)=lower(:e)"), {"e": email}).fetchone()
    if row is None:
        row = db.execute(text(
            "SELECT id FROM employee_department_mapping WHERE lower(name)=lower(:n)"), {"n": name}).fetchone()
    return row._mapping["id"] if row else None


# ── import punctual (modulul Task-uri) ───────────────────────────────────────
def import_employee_by_email(db: Session, email: str, roster: Optional[List[Dict[str, Any]]] = None) -> Optional[int]:
    """Importa punctual UN angajat dupa email, chiar daca departamentul lui nu e in
    VALID_DEPARTMENTS (bypass explicit al whitelist-ului, DOAR pentru acest apel).

    `roster`: daca apelantul a facut deja `fetch_employees(db)` in aceasta rulare (ex. sync de
    task-uri cu mai multi assignee necunoscuti), il paseaza ca sa nu re-facem fetch-ul complet
    de fiecare data. Returneaza id-ul local sau None daca angajatul nu exista nici in rosterul IRIS.
    """
    email = (email or "").strip()
    if not email or "@" not in email:
        return None
    if roster is None:
        roster = fetch_employees(db)
    emp = None
    for e in roster:
        if isinstance(e, dict) and str(_pick(e, "email", "mail") or "").strip().lower() == email.lower():
            emp = e
            break
    if emp is None:
        return None

    name = _pick(emp, "full_name", "name", "nume")
    if not name:
        return None
    name = str(name).strip()

    dmap = _get_setting(db, "employee_sync.department_map", {}) or {}
    if not isinstance(dmap, dict):
        dmap = {}
    dept = _norm_dept(
        _pick(emp, "department_slug", "dept_slug"),
        _pick(emp, "department", "dept", "departament"),
        dmap, require_whitelist=False,
    )
    if dept is None:
        dept = "necunoscut"  # angajat fara departament identificabil in payload -- pastram totusi randul

    status = _pick(emp, "status", "stare")
    work_hours = _to_int(_pick(emp, "work_hours", "ore", "work_hours_per_day"))
    break_minutes = _to_int(_pick(emp, "break_minutes", "pauza", "break"))
    iris_id = _pick(emp, "id", "iris_id", "employee_id")
    iris_id = str(iris_id) if iris_id is not None else None
    active = True
    if status and str(status).strip().lower() in ("inactiv", "inactive", "plecat", "suspendat", "disabled", "left"):
        active = False

    existing_id = _find_employee_row(db, email, name)
    emp_id = _upsert_one_employee(db, existing_id, name, dept, active, email, status,
                                  work_hours, break_minutes, iris_id, sync_source="task_import")
    _write_employee_leaves(db, emp_id, emp)
    db.commit()
    logger.info("cts_tasks: import punctual angajat %s (dept=%s, %s)", email, dept,
                "nou" if existing_id is None else "actualizat")
    return emp_id


# ── sync principal ───────────────────────────────────────────────────────────
def sync_employees(db: Session, *, dry_run: bool = False, payload: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Sincronizeaza lista de angajati. Returneaza rezumat (counts). Idempotent.

    payload: pentru test/manual — daca e dat, se foloseste in loc de fetch din IRIS.
    """
    if not is_enabled(db) and payload is None:
        return {"ok": False, "skipped": "employee_sync.enabled=false"}

    base, key = _gateway_config()
    if payload is None and (not base or not key):
        return {"ok": False, "skipped": "Gateway IRIS neconfigurat (iris_api_url / IRIS_MAILGUARD_API_KEY)."}

    try:
        employees = payload if payload is not None else fetch_employees(db)
    except Exception as e:
        logger.warning("employee sync fetch failed: %s", e)
        res = {"ok": False, "error": str(e)}
        if not dry_run:
            _set_setting(db, "employee_sync.last_result", res)
            db.commit()
        return res

    dmap = _get_setting(db, "employee_sync.department_map", {}) or {}
    if not isinstance(dmap, dict):
        dmap = {}

    seen_ids: List[int] = []
    n_insert = n_update = n_leaves = n_skip_dept = 0

    for emp in employees:
        if not isinstance(emp, dict):
            continue
        name = _pick(emp, "full_name", "name", "nume")
        if not name:
            continue
        name = str(name).strip()

        email = _pick(emp, "email", "mail")
        email = str(email).strip() if email else None

        dept = _norm_dept(
            _pick(emp, "department_slug", "dept_slug"),
            _pick(emp, "department", "dept", "departament"),
            dmap,
        )

        # Cauta randul existent INAINTE de gate-ul de departament: un angajat deja prezent local
        # (ex. importat punctual via import_employee_by_email pt modulul Task-uri) trebuie sa
        # primeasca refresh la fiecare sync normal, indiferent daca departamentul lui curent mai
        # mapeaza in whitelist. Whitelist-ul blocheaza DOAR crearea unui rand NOU.
        existing_id = None if dry_run else _find_employee_row(db, email, name)

        if dept is None and existing_id is None:
            n_skip_dept += 1
            continue  # angajat NOU, departament in afara Cargo360 -> nu importam (politica existenta)

        if dept is None:
            # angajat deja existent local, dar departamentul curent din feed nu mai mapeaza in
            # whitelist -> pastram departamentul deja stocat (coloana e NOT NULL).
            cur = db.execute(text(
                "SELECT department FROM employee_department_mapping WHERE id=:id"), {"id": existing_id}).fetchone()
            dept = cur._mapping["department"] if cur else None
            if dept is None:
                n_skip_dept += 1
                continue

        status = _pick(emp, "status", "stare")
        work_hours = _to_int(_pick(emp, "work_hours", "ore", "work_hours_per_day"))
        break_minutes = _to_int(_pick(emp, "break_minutes", "pauza", "break"))
        iris_id = _pick(emp, "id", "iris_id", "employee_id")
        iris_id = str(iris_id) if iris_id is not None else None
        active = True
        if status and str(status).strip().lower() in ("inactiv", "inactive", "plecat", "suspendat", "disabled", "left"):
            active = False

        if dry_run:
            seen_ids.append(-1)
            for _ in _iter_leaves(emp):
                n_leaves += 1
            continue

        # NB: 'shift' NU e scris de sync (IRIS trimite null) -> ramane setarea manuala.
        emp_id = _upsert_one_employee(db, existing_id, name, dept, active, email, status,
                                      work_hours, break_minutes, iris_id, sync_source="iris")
        if existing_id is None:
            n_insert += 1
        else:
            n_update += 1
        seen_ids.append(emp_id)
        n_leaves += _write_employee_leaves(db, emp_id, emp)

    # reconcile: randurile IRIS care nu mai vin in feed -> enabled=false (NU stergem; manualele neatinse)
    n_disabled = 0
    if not dry_run:
        if seen_ids:
            res = db.execute(text(
                "UPDATE employee_department_mapping SET enabled=false, updated_at=now() "
                "WHERE sync_source='iris' AND enabled=true AND id <> ALL(:ids)"
            ), {"ids": seen_ids})
            n_disabled = res.rowcount or 0
        summary = {"ok": True, "received": len(employees), "imported": n_insert + n_update,
                   "inserted": n_insert, "updated": n_update, "skipped_dept": n_skip_dept,
                   "disabled_missing": n_disabled, "schedule_rows": n_leaves}
        db.execute(text("UPDATE settings SET value=to_jsonb(now()) WHERE key='employee_sync.last_sync_at'"))
        _set_setting(db, "employee_sync.last_result", summary)
        db.commit()
        return summary

    return {"ok": True, "dry_run": True, "received": len(employees),
            "skipped_dept": n_skip_dept, "schedule_rows": n_leaves}


def run_daily_if_due() -> Dict[str, Any]:
    """Self-gated pe cron-ul de 5 min (via /process/run-now). Ruleaza sync-ul din IRIS cel mult
    o data pe zi. No-op ieftin daca: sync dezactivat, gateway neconfigurat, sau deja sincronizat
    in ultimele ~20h. Best-effort, read-only fata de IRIS.
    """
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        if not is_enabled(db):
            return {"skipped": "disabled"}
        base, key = _gateway_config()
        if not base or not key:
            return {"skipped": "gateway not configured"}
        last = _get_setting(db, "employee_sync.last_sync_at")
        if last:
            try:
                from datetime import datetime, timezone, timedelta
                lt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                if lt.tzinfo is None:
                    lt = lt.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - lt < timedelta(hours=20):
                    return {"skipped": "already synced recently", "last_sync_at": str(last)}
            except Exception:
                pass
        return sync_employees(db)
    except Exception as e:
        logger.warning("employee daily sync failed: %s", e)
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


def _populate_iris_ids(db: Session) -> int:
    """Populează iris_id în employee_department_mapping via JOIN pe email cu cts_dv_employee.
    No-op dacă tabela cts_dv_employee nu există. Idempotent.

    ATENȚIE — de ce e nevoie de DISTINCT ON (bug reparat 2026-07-31, raportat de pe producție):
    angajații reangajați au 2-3 fișe în `cts_dv_employee` cu ACELAȘI email și `id` diferit
    (contracte succesive). Varianta anterioară era un `UPDATE ... FROM cts_dv_employee` simplu:
      - NEDETERMINISTĂ — cu mai multe rânduri care se potrivesc pentru același `e.id`, Postgres
        scrie unul arbitrar, fără eroare;
      - NU CONVERGEA — condiția `e.iris_id != dv.id` rămâne mereu satisfăcută cât timp există
        fișe multiple (celelalte fișe „contrazic" orice valoare pusă), deci rescria la fiecare
        rulare, cu rezultat posibil diferit.
    Măsurat pe staging, trei rulări consecutive pe date identice: UPDATE 9, 7, 7 (o funcție
    corectă dă 0 la a doua). Efect real pe prod: angajați cu concedii 2026 ajungeau pe fișa fără
    concedii (Popa Andreea 9->0, Vlad Cosmin 5->0) — adică bug-ul reparat în v0.64.0, reintrodus
    zilnic prin altă cale.

    `DISTINCT ON (edm.id)` garantează un rând per angajat, iar `ORDER BY` alege CONTRACTUL ACTIV
    (`contract_termination_date` gol) înaintea celui mai recent angajat — coloană care exista în
    `cts_dv_employee` dar nu era folosită. Match pe email, fără ID-uri hardcodate, deci se comportă
    identic pe staging și pe prod, deși ID-urile fișelor diferă între medii.
    (`cts_dv_employee` nu are coloană `name`, doar `first_name`/`last_name` — match pe nume nu e
    o opțiune, și n-ar ajuta: ambiguitatea vine din contractele multiple, nu din cheia de match.)
    """
    try:
        tbl_exists = db.execute(text(
            "SELECT 1 FROM information_schema.tables WHERE table_name='cts_dv_employee' LIMIT 1"
        )).fetchone()
        if not tbl_exists:
            return 0
        result = db.execute(text("""
            UPDATE employee_department_mapping e
            SET iris_id = best.dv_id
            FROM (
                SELECT DISTINCT ON (edm.id) edm.id AS local_id, dv.id AS dv_id
                FROM employee_department_mapping edm
                JOIN cts_dv_employee dv ON lower(dv.email) = lower(edm.email)
                WHERE edm.enabled = true
                  AND edm.email IS NOT NULL AND dv.email IS NOT NULL AND dv.email <> ''
                  AND (dv.deleted_at IS NULL OR dv.deleted_at = '')
                ORDER BY edm.id,
                         (NULLIF(trim(dv.contract_termination_date), '') IS NULL) DESC,
                         dv.date_of_employment DESC NULLS LAST,
                         dv.id DESC
            ) best
            WHERE e.id = best.local_id
              AND (e.iris_id IS NULL OR e.iris_id != best.dv_id)
        """))
        db.commit()
        return result.rowcount
    except Exception as exc:
        logger.warning("_populate_iris_ids failed: %s", exc)
        return 0


def sync_vacation_from_dv(db: Session) -> int:
    """Scrie concedii reale (status=2, 2026+) din cts_dv_employee_vacation_request în
    employee_schedule (kind='vacation_approved', entry_source='cts'), pentru angajații
    cu iris_id populat. Șterge și re-inserează vacation_approved CTS (idempotent).
    Intrările manuale rămân intacte.
    """
    try:
        tbl_exists = db.execute(text(
            "SELECT 1 FROM information_schema.tables WHERE table_name='cts_dv_employee_vacation_request' LIMIT 1"
        )).fetchone()
        if not tbl_exists:
            return 0

        # Angajați cu iris_id setat — iris_id = CTS employee_id din cts_dv_employee_vacation_request
        mapped = db.execute(text(
            "SELECT id, iris_id FROM employee_department_mapping WHERE enabled = true AND iris_id IS NOT NULL"
        )).fetchall()
        if not mapped:
            return 0

        local_ids = [row._mapping["id"] for row in mapped]
        # Mapare iris_id (CTS id) -> edm local id
        cts_to_local = {str(row._mapping["iris_id"]): row._mapping["id"] for row in mapped}
        cts_ids = list(cts_to_local.keys())

        # Șterge vacation_approved CTS existente pentru toți angajații mapați
        db.execute(text(
            "DELETE FROM employee_schedule "
            "WHERE employee_id = ANY(:ids) AND kind='vacation_approved' AND entry_source='cts'"
        ), {"ids": local_ids})

        # Inserează din DV prin iris_id -> CTS employee_id; doar 2026+.
        # Status DV: 1 = in asteptare (zero aprobatori pe toate cele 141 din istoric),
        # 2 = aprobat, 3/4 = respins/anulat (au aprobator, deci procesate -> NU se importa).
        # Cele in asteptare TREBUIE importate: blocheaza zile de lucru la estimarea de
        # productivitate pe lunile viitoare, chiar daca nu sunt inca confirmate.
        vacations = db.execute(text("""
            SELECT v.employee_id::text AS cts_id, v.period_begin, v.period_end, v.days,
                   v.status::int AS dv_status
            FROM cts_dv_employee_vacation_request v
            WHERE v.employee_id::text = ANY(:cts_ids)
              AND v.status::int IN (1, 2)
              AND (v.deleted_at IS NULL OR v.deleted_at::text = '')
              AND v.period_begin::date >= '2026-01-01'
            ORDER BY v.employee_id, v.period_begin
        """), {"cts_ids": cts_ids}).fetchall()

        n = 0
        for v in vacations:
            local_id = cts_to_local.get(str(v._mapping["cts_id"]))
            if not local_id:
                continue
            try:
                db.execute(text(
                    "INSERT INTO employee_schedule "
                    "(employee_id, kind, leave_type, start_date, end_date, status, days, raw, entry_source) "
                    "VALUES (:eid, 'vacation_approved', 'concediu', "
                    "  CAST(:sd AS date), CAST(:ed AS date), :st, :days, '{}', 'cts') "
                    "ON CONFLICT DO NOTHING"
                ), {
                    "eid": local_id,
                    "sd": v._mapping["period_begin"],
                    "ed": v._mapping["period_end"],
                    "days": v._mapping["days"],
                    "st": "approved" if v._mapping["dv_status"] == 2 else "pending",
                })
                n += 1
            except Exception as exc:
                logger.warning("vacation insert skip (emp %s): %s", local_id, exc)

        db.commit()
        return n
    except Exception as exc:
        logger.warning("sync_vacation_from_dv failed: %s", exc)
        db.rollback()
        return 0


def run_vacation_dv_sync_if_due() -> Dict[str, Any]:
    """Sincronizeaza zilnic (self-gated):
    1. DV employee (iris_id mapping)
    2. DV employee_vacation_request (concedii reale aprobate)
    3. Scrie vacation_approved in employee_schedule
    """
    from app.database import SessionLocal
    from datetime import datetime, timezone, timedelta
    db = SessionLocal()
    try:
        from app.api.v1.iris_dv import _get_api_key, _sync_view_snapshot, _get_state
        api_key = _get_api_key(db)
        if not api_key:
            return {"skipped": "iris_dv key not configured"}

        # Gate pe 20h bazat pe vacation_request sync
        state = _get_state(db, "employee_vacation_request")
        last_sync = state.get("last_sync_at")
        if last_sync:
            try:
                lt = last_sync if hasattr(last_sync, "tzinfo") else datetime.fromisoformat(str(last_sync).replace("Z", "+00:00"))
                if lt.tzinfo is None:
                    lt = lt.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - lt < timedelta(hours=20):
                    return {"skipped": "already synced recently", "last_sync_at": str(last_sync)}
            except Exception:
                pass

        result = {}

        # 1. Sync DV employee (pentru iris_id mapping)
        try:
            r = _sync_view_snapshot("employee", api_key, db)
            result["employee_dv"] = r
        except Exception as e:
            logger.warning("employee dv sync failed: %s", e)
            result["employee_dv"] = {"error": str(e)}

        # 2. Populează iris_id
        mapped = _populate_iris_ids(db)
        result["iris_ids_updated"] = mapped

        # 3. Sync DV vacation requests
        r2 = _sync_view_snapshot("employee_vacation_request", api_key, db)
        result["vacation_dv"] = r2

        # 4. Scrie vacation_approved in employee_schedule
        n_written = sync_vacation_from_dv(db)
        result["vacation_rows_written"] = n_written

        return {"ok": True, **result}
    except Exception as e:
        logger.warning("vacation dv sync failed: %s", e)
        return {"ok": False, "error": str(e)}
    finally:
        db.close()
