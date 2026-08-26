
"""Sursa dedicata pentru mutarile intre departamente: `client_contact_email_department_log`.

DE CE INCA O SURSA. `client_contact_email_log` (vezi `cts_email_log.py`) tine un rand per
ALOCARE, deci lantul se reconstruieste din secventa de `department_id`. Log-ul de departamente e
scris exact la MUTARE: cine a mutat, in ce departament, cand — adica fix ce cer INDICE 1 (cate
mutari are un mail) si INDICE 3 (cele mai frecvente departamente intermediare), fara sa deducem
nimic din diferente.

⚠️ SCHEMA SE REZOLVA LA RUNTIME. Sync-ul DV creeaza tabela locala din coloanele primului rand
(toate TEXT), iar numele exacte ale coloanelor din view NU sunt fixate in cod: `_resolve()` le
cauta printre variantele plauzibile si intoarce maparea. Motivul e practic — daca CTS numeste
coloana `to_department_id` in loc de `department_id`, raportul nu trebuie sa cada, iar
`GET /cts-training/dept-report/log-schema` arata ce s-a gasit efectiv. Cand numele sunt
confirmate, listele de candidati raman ca atare: sunt ordonate, prima potrivire castiga.

FORMA IESIRII e identica cu a celorlalte doua surse (`mail` + `ch` cu message_id/dep/moved_at/
step/steps), ca endpoint-urile raportului sa fie comune.
"""
import logging
from typing import Dict, List, Optional

from sqlalchemy import text

from app.services import cts_email_log

logger = logging.getLogger("mailguard.cts_dept_log")

TABLE = "cts_dv_client_contact_email_department_log"
VIEW_NAME = "client_contact_email_department_log"

# Schema reala a view-ului (Laravel, CTS):
#   id, client_contact_email_log_id (FK -> client_contact_email_log.id), mid,
#   department_from_id, department_to_id, user_id (cine a mutat), updated_at (cand).
# Listele de mai jos sunt ordonate: prima coloana EXISTENTA castiga. Numele reale sunt primele;
# restul sunt variante tolerate, ca o redenumire in view sa nu darame raportul (diagnostic:
# GET /cts-training/dept-report/log-schema).
_CANDIDATES: Dict[str, List[str]] = {
    # Cheia mailului. ATENTIE: `mid` din ACEST view e intreg (alt lucru decat `mid` varchar din
    # client_contact_email_log), deci NU e candidat de message_id — gruparea se face prin FK.
    "message_id": ["message_id"],
    "email_fk":   ["client_contact_email_log_id", "client_contact_email_id", "email_log_id",
                   "email_id", "cce_id"],
    "to_dept":    ["department_to_id", "to_department_id", "new_department_id", "department_id"],
    "from_dept":  ["department_from_id", "from_department_id", "old_department_id",
                   "previous_department_id"],
    "actor":      ["user_id", "created_by", "moved_by", "admin_id", "responsible_id", "updated_by"],
    "at":         ["updated_at", "created_at", "moved_at", "assigned_at", "date"],
    "deleted_at": ["deleted_at"],
}


def columns(db) -> List[str]:
    """Coloanele tabelei locale (goala daca view-ul nu a fost sincronizat inca)."""
    try:
        rows = db.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t ORDER BY ordinal_position"
        ), {"t": TABLE}).fetchall()
        return [r[0] for r in rows]
    except Exception as e:
        logger.warning("cts_dept_log.columns: %s", e)
        return []


def _resolve(db) -> Dict[str, Optional[str]]:
    cols = {c.lower(): c for c in columns(db)}
    out: Dict[str, Optional[str]] = {}
    for field, cands in _CANDIDATES.items():
        out[field] = next((cols[c] for c in cands if c in cols), None)
    return out


def resolve(db) -> dict:
    """Maparea camp logic -> coloana reala + de ce e (ne)utilizabila sursa. Pentru diagnostic."""
    cols = columns(db)
    m = _resolve(db)
    missing = []
    if not (m.get("message_id") or m.get("email_fk")):
        missing.append("cheia mailului (message_id sau FK catre client_contact_email_log)")
    if not m.get("to_dept"):
        missing.append("departamentul destinatie")
    if not m.get("at"):
        missing.append("momentul mutarii")
    try:
        n = int(db.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar() or 0) if cols else 0
    except Exception:
        n = 0
    return {"table": TABLE, "view": VIEW_NAME, "synced": bool(cols), "rows": n,
            "columns": cols, "mapping": m, "missing": missing, "usable": bool(cols and not missing)}


def available(db) -> bool:
    """True daca tabela exista, are randuri si campurile obligatorii s-au putut rezolva."""
    try:
        info = resolve(db)
        return bool(info["usable"] and info["rows"])
    except Exception as e:
        logger.warning("cts_dept_log.available: %s", e)
        return False


def ensure_indexes(db) -> None:
    """Indexuri pe tabela creata la runtime de sync-ul DV (best-effort, idempotent)."""
    m = _resolve(db)
    wanted = [m.get("message_id"), m.get("email_fk"), m.get("to_dept"), m.get("at")]
    for col in [c for c in wanted if c]:
        try:
            db.execute(text(
                f'CREATE INDEX IF NOT EXISTS idx_ccedl_{col.lower()} ON {TABLE} ("{col}")'))
        except Exception as e:
            logger.info("cts_dept_log.ensure_indexes skip (%s)", e)
            db.rollback()
            return
    db.commit()


def _ts(col: str) -> str:
    return cts_email_log._ts(col)


def _num(col: str) -> str:
    return cts_email_log._num(col)


def chain_cte(db) -> str:
    """CTE-ul lantului, construit pe coloanele rezolvate. Expune `mail` si `ch` exact ca
    celelalte doua surse, deci endpoint-urile raportului raman comune.

    LEGATURA CU MAILURILE NOASTRE trece prin `cts_ground_truth`, NU prin oglinda DV a log-ului
    de alocari:
      `department_log.client_contact_email_log_id` = `cts_ground_truth.cts_ticket_id`
      (populat din `extra.cts_email_log_id`, vezi 20260805_cts_ticket_replicas.sql), iar
      `cts_ground_truth.email_id` e ID-ul din tabela noastra `emails`.
    De ce asa: `cts_dv_client_contact_email_log` e un snapshot DV care poate fi trunchiat sau in
    urma (pe productie avea 10.000 randuri, toate din 2020, in timp ce mutarile erau din 2026),
    iar un JOIN pe el facea raportul GOL. `cts_ground_truth` e tabela NOASTRA, sincronizata de
    cronul propriu, deci e sursa de adevar pentru corelarea cu pagina „Emailuri".

    Cheia de grupare: `message_id` cand il stim din ground truth (asa se colapseaza replicile pe
    care CTS le face per destinatar), altfel `ticket:<id>` — un mail fara ground truth nu se
    pierde din statistica, doar nu are subiect/ID local de afisat.

    Departamentul INITIAL (cel pe care a INTRAT mailul) nu e un rand aici — log-ul tine doar
    mutari. Se ia din `department_from_id` al primei mutari. Fara pasul asta INDICE 1 ar numara
    cu o mutare mai putin, iar INDICE 3 ar trata departamentul de intrare ca intermediar.

    `user_id` (cine a mutat) se pastreaza pe eveniment — de acolo vine „mutat de" din drill-down.
    """
    m = _resolve(db)
    at = _ts(f'l."{m["at"]}"')
    to_dept = f'NULLIF(l."{m["to_dept"]}", \'\')'
    actor = _num(f'l."{m["actor"]}"') if m.get("actor") else "NULL::bigint"
    from_dept = f'NULLIF(l."{m["from_dept"]}", \'\')' if m.get("from_dept") else "NULL::text"
    del_guard = (f'(l."{m["deleted_at"]}" IS NULL OR l."{m["deleted_at"]}" = \'\' '
                 f'OR left(l."{m["deleted_at"]}", 4) = \'0000\')') if m.get("deleted_at") else "TRUE"
    ticket = _num(f'l."{m["email_fk"]}"') if m.get("email_fk") else "NULL::bigint"

    return f"""
WITH {cts_email_log._DEPT_MAP_CTE},
mv0 AS (
    SELECT {ticket}        AS ticket_id,
           {to_dept}       AS to_dept_id,
           {from_dept}     AS from_dept_id,
           {actor}         AS actor_id,
           {at}            AS moved_at,
           {_num('l."id"')} AS mv_id
      FROM {TABLE} l
     WHERE {del_guard}
),
mv1 AS (
    SELECT * FROM mv0
     WHERE ticket_id IS NOT NULL AND to_dept_id IS NOT NULL AND moved_at IS NOT NULL
),
-- Ground truth per tichet CTS: de aici vin message_id, ID-ul local de mail si starea de inchidere.
gt AS (
    -- Doar tichetele care apar in log-ul de mutari — altfel se agrega toata `cts_ground_truth`
    -- (sute de mii de randuri) la fiecare interogare a raportului.
    SELECT cts_ticket_id,
           min(message_id)                            AS message_id,
           min(email_id)                              AS email_id,
           max(cts_solved_at)                         AS solved_at,
           bool_or(cts_solved_at IS NOT NULL
                   OR lower(COALESCE(cts_status, '')) IN ('solved', 'rezolvat', 'closed')) AS is_solved
      FROM cts_ground_truth
     WHERE cts_ticket_id IS NOT NULL
       AND cts_ticket_id IN (SELECT ticket_id FROM mv1)
     GROUP BY cts_ticket_id
),
mv AS (
    SELECT v.*,
           COALESCE(g.message_id, 'ticket:' || v.ticket_id::text) AS message_id,
           g.email_id, g.solved_at, g.is_solved
      FROM mv1 v
      LEFT JOIN gt g ON g.cts_ticket_id = v.ticket_id
),
-- Alocarea INITIALA a fiecarui mail (vezi docstring).
first_mv AS (
    SELECT DISTINCT ON (message_id) message_id, from_dept_id, moved_at
      FROM mv ORDER BY message_id, moved_at, mv_id
),
seed AS (
    SELECT message_id, from_dept_id AS dept_id, moved_at - interval '1 second' AS moved_at
      FROM first_mv
     WHERE from_dept_id IS NOT NULL
),
ev0 AS (
    SELECT message_id, to_dept_id AS dept_id, moved_at, mv_id AS ord, actor_id FROM mv
    UNION ALL
    SELECT message_id, dept_id, moved_at, 0 AS ord, NULL::bigint FROM seed
),
ev1 AS (
    SELECT e.message_id, e.moved_at, e.ord, e.actor_id,
           COALESCE(dm.slug, 'cts_' || e.dept_id) AS dep
      FROM ev0 e
      LEFT JOIN dept_map dm ON dm.cts_dept_id = e.dept_id
),
-- Starea mailului (inchis / ID local), agregata O SINGURA DATA per mail.
-- ⚠️ NU se face `ev1 LEFT JOIN mv ON message_id` inainte de GROUP BY: ambele au mai multe randuri
-- per mail, deci join-ul producea produs cartezian (pasi × mutari) doar ca sa recalculeze
-- aceleasi agregate.
mail_attr AS (
    SELECT message_id,
           max(solved_at)                      AS solved_at,
           COALESCE(bool_or(is_solved), false) AS is_solved,
           min(email_id)                       AS email_id
      FROM mv
     GROUP BY message_id
),
mail AS (
    SELECT e.message_id, e.started_at,
           a.solved_at,
           COALESCE(a.is_solved, false) AS is_solved,
           a.email_id
      FROM (SELECT message_id, min(moved_at) AS started_at FROM ev1 GROUP BY message_id) e
      LEFT JOIN mail_attr a ON a.message_id = e.message_id
     WHERE (CAST(:date_from AS date) IS NULL
            OR e.started_at >= CAST(:date_from AS date))
       AND (CAST(:date_to AS date) IS NULL
            OR e.started_at < CAST(:date_to AS date) + interval '1 day')
       AND (NOT CAST(:only_solved AS boolean) OR COALESCE(a.is_solved, false))
),
ev AS (
    SELECT v.message_id, v.dep, v.moved_at, v.ord AS id,
           lag(v.dep) OVER (PARTITION BY v.message_id ORDER BY v.moved_at, v.ord) AS prev_dep
      FROM ev1 v
      JOIN mail m ON m.message_id = v.message_id
),
seq AS (
    SELECT message_id, dep, moved_at,
           row_number() OVER (PARTITION BY message_id ORDER BY moved_at, id) AS step,
           count(*)     OVER (PARTITION BY message_id) AS steps
      FROM ev
     WHERE prev_dep IS NULL OR prev_dep IS DISTINCT FROM dep
),
kept AS (
    SELECT message_id FROM seq GROUP BY message_id
    HAVING CAST(:dept AS text) IS NULL OR bool_or(dep = CAST(:dept AS text))
),
ch AS (
    SELECT s.* FROM seq s JOIN kept k ON k.message_id = s.message_id
)
"""


def coverage(db) -> dict:
    """Cat acopera log-ul de mutari: randuri, mailuri distincte, fereastra, department_id-uri
    netraduse in slug local."""
    m = _resolve(db)
    if not m.get("to_dept") or not m.get("at"):
        return {"rows": 0, "mails": 0, "first_at": None, "last_at": None,
                "unmapped_departments": 0}
    key = (f'NULLIF(l."{m["message_id"]}", \'\')' if m.get("message_id")
           else f'NULLIF(l."{m["email_fk"]}", \'\')')
    try:
        row = db.execute(text(f"""
            WITH {cts_email_log._DEPT_MAP_CTE},
            l0 AS (
                SELECT {key} AS mail_key,
                       NULLIF(l."{m['to_dept']}", '') AS dept_id,
                       {_ts(f'l."{m["at"]}"')} AS moved_at
                  FROM {TABLE} l
            )
            SELECT count(*) AS rows_total,
                   count(DISTINCT mail_key) AS mails,
                   min(moved_at) AS first_at,
                   max(moved_at) AS last_at,
                   count(DISTINCT l0.dept_id) FILTER (WHERE dm.slug IS NULL
                                                      AND l0.dept_id IS NOT NULL) AS unmapped
              FROM l0 LEFT JOIN dept_map dm ON dm.cts_dept_id = l0.dept_id
        """)).fetchone()
        r = row._mapping
        return {"rows": int(r["rows_total"] or 0), "mails": int(r["mails"] or 0),
                "first_at": r["first_at"], "last_at": r["last_at"],
                "unmapped_departments": int(r["unmapped"] or 0)}
    except Exception as e:
        logger.warning("cts_dept_log.coverage: %s", e)
        return {"rows": 0, "mails": 0, "first_at": None, "last_at": None,
                "unmapped_departments": 0}


def steps_for_mail(db, message_id: str, limit: int = 200) -> list:
    """Mutarile BRUTE ale unui mail (departament sursa/destinatie, cine, cand), pentru
    drill-down-ul „de ce s-a mutat". Fiecare rand E o mutare — nu se deduce nicio tranzitie.

    Cheia primita e aceeasi ca in raport: `message_id` real (rezolvat prin
    `cts_ground_truth.cts_ticket_id`) sau `ticket:<id>` pentru mailurile fara ground truth."""
    m = _resolve(db)
    if not m.get("to_dept") or not m.get("at") or not m.get("email_fk"):
        return []
    at = _ts(f'l."{m["at"]}"')
    to_dept = f'NULLIF(l."{m["to_dept"]}", \'\')'
    from_dept = f'NULLIF(l."{m["from_dept"]}", \'\')' if m.get("from_dept") else "NULL::text"
    actor = _num(f'l."{m["actor"]}"') if m.get("actor") else "NULL::bigint"
    del_guard = (f'(l."{m["deleted_at"]}" IS NULL OR l."{m["deleted_at"]}" = \'\' '
                 f'OR left(l."{m["deleted_at"]}", 4) = \'0000\')') if m.get("deleted_at") else "TRUE"
    ticket = _num(f'l."{m["email_fk"]}"')

    rows = db.execute(text(f"""
        WITH {cts_email_log._DEPT_MAP_CTE},
        tickets AS (
            -- Tichetele CTS ale acestui mail: fie prin message_id din ground truth, fie direct
            -- din cheia `ticket:<id>` cand mailul nu are ground truth.
            SELECT DISTINCT cts_ticket_id AS ticket_id
              FROM cts_ground_truth
             WHERE cts_ticket_id IS NOT NULL AND message_id = :mid
            UNION
            SELECT CASE WHEN :mid ~ '^ticket:[0-9]+$'
                        THEN substring(:mid from 8)::bigint END
        )
        SELECT {_num('l."id"')} AS log_id,
               {from_dept} AS from_dept_id,
               {to_dept}   AS to_dept_id,
               COALESCE(dmf.slug, 'cts_' || {from_dept}) AS from_dep,
               COALESCE(dmt.slug, 'cts_' || {to_dept})   AS to_dep,
               {actor}     AS actor_id,
               {at}        AS moved_at,
               emp.name    AS actor_name
          FROM {TABLE} l
          JOIN tickets tk ON tk.ticket_id = {ticket}
          LEFT JOIN dept_map dmf ON dmf.cts_dept_id = {from_dept}
          LEFT JOIN dept_map dmt ON dmt.cts_dept_id = {to_dept}
          LEFT JOIN LATERAL (
              SELECT e.name
                FROM cts_dv_employee dv
                JOIN employee_department_mapping e ON lower(e.email) = lower(dv.email)
               WHERE dv.admin_id = CAST({actor} AS text)
               ORDER BY e.enabled DESC, e.id
               LIMIT 1
          ) emp ON true
         WHERE {del_guard}
         ORDER BY moved_at NULLS LAST, log_id
         LIMIT :lim
    """), {"mid": message_id, "lim": limit}).fetchall()
    return [dict(r._mapping) for r in rows]
