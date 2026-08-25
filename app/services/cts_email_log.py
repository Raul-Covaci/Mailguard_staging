"""Sursa noua pentru „Raport departamente": `client_contact_email_log` din CTS.

DE CE. Pana acum lantul de departamente al unui mail se reconstruia din `cts_department_moves`
(trigger pe `cts_ground_truth`), care vede doar tranzitiile prinse INTRE doua sincronizari (~5 min)
si, pentru mailurile de dinaintea migratiei, doar un singur pas de istorie. Departamentele
INTERMEDIARE se pierdeau: Suport 1 -> Contabilitate -> Taxe drum aparea ca Suport 1 -> Taxe drum.

`client_contact_email_log` e log-ul CTS: un rand per alocare (mail x folder/departament), deci
lantul complet e in date, nu reconstruit din diferente. Se sincronizeaza ca orice view IRIS Data
Views (pagina „Surse date") in tabela locala `cts_dv_client_contact_email_log`, toate coloanele TEXT.

FORMA IESIRII e identica cu a CTE-ului vechi (`mail` + `ch` cu message_id/dep/moved_at/step/steps),
ca raportul sa poata comuta intre surse fara alt cod: vezi `_DEPT_REPORT_CTE` din
`app/api/v1/cts_training.py`.

Note despre date:
  * toate coloanele sunt TEXT (oglinda bruta a DV-ului) -> cast-urile sunt DEFENSIVE: datele
    MySQL '0000-00-00' si textul liber devin NULL, nu 500.
  * `department_id` e ID-ul CTS, nu slug-ul nostru. Se traduce prin departamentul dominant al
    angajatilor din acel department_id (`cts_dv_employee` -> `employee_department_mapping`),
    aceeasi treapta folosita in quality_eval/productivity. Un ID netradus NU se arunca: apare
    ca pseudo-slug `cts_<id>`, altfel lantul ar pierde exact pasii care ne intereseaza.
  * gruparea per MAIL se face pe `message_id`; CTS scrie un rand per destinatar/folder, iar pasii
    consecutivi pe acelasi departament se colapseaza (ca in raportul vechi).
"""
import logging
from typing import Optional

from sqlalchemy import text

logger = logging.getLogger("mailguard.cts_email_log")

TABLE = "cts_dv_client_contact_email_log"
VIEW_NAME = "client_contact_email_log"


def _ts(col: str) -> str:
    """Cast defensiv TEXT -> timestamp. MySQL scrie '0000-00-00 00:00:00' pe datele nesetate,
    iar un cast direct ar da 500 pe tot raportul."""
    # `%` e evitat intentionat (LIKE '0000%'): SQL-ul ajunge la psycopg2, unde procentul e
    # placeholder de parametru. left(...) face acelasi lucru fara ambiguitate.
    return (f"CASE WHEN {col} ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}' AND left({col}, 4) <> '0000' "
            f"THEN substring({col} from 1 for 19)::timestamp ELSE NULL END")


def _num(col: str) -> str:
    """Cast defensiv TEXT -> bigint (NULL daca nu e numar intreg)."""
    return f"CASE WHEN {col} ~ '^[0-9]+$' THEN {col}::bigint ELSE NULL END"


def available(db) -> bool:
    """True daca view-ul a fost sincronizat macar o data (tabela locala exista si are randuri)."""
    try:
        ok = db.execute(text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name=:t LIMIT 1"), {"t": TABLE}).fetchone()
        if not ok:
            return False
        return bool(db.execute(text(f"SELECT 1 FROM {TABLE} LIMIT 1")).fetchone())
    except Exception as e:
        logger.warning("cts_email_log.available: %s", e)
        return False


def ensure_indexes(db) -> None:
    """Indexuri pe tabela creata la runtime de sync-ul DV (best-effort, idempotent).
    Sync-ul face DELETE + INSERT, nu DROP TABLE, deci indexurile supravietuiesc rularilor."""
    stmts = [
        f'CREATE INDEX IF NOT EXISTS idx_ccel_message_id ON {TABLE} ("message_id")',
        f'CREATE INDEX IF NOT EXISTS idx_ccel_mid        ON {TABLE} ("mid")',
        f'CREATE INDEX IF NOT EXISTS idx_ccel_date       ON {TABLE} ("date")',
        f'CREATE INDEX IF NOT EXISTS idx_ccel_dept       ON {TABLE} ("department_id")',
    ]
    for s in stmts:
        try:
            db.execute(text(s))
        except Exception as e:      # tabela lipsa / coloana lipsa -> raportul merge si fara index
            logger.info("cts_email_log.ensure_indexes skip (%s)", e)
            db.rollback()
            return
    db.commit()


# Traducere department_id (CTS) -> slug local, prin departamentul dominant al angajatilor din
# acel department_id. Aceeasi treapta ca in quality_eval.py / productivity.py — NU pe egalitate
# de nume. `cts_dv_employee.department_id` e TEXT (oglinda bruta a DV-ului).
_DEPT_MAP_CTE = """
dept_map AS (
    SELECT cts_dept_id, slug FROM (
        SELECT dv."department_id" AS cts_dept_id, e.department AS slug,
               row_number() OVER (PARTITION BY dv."department_id"
                                  ORDER BY count(*) DESC, e.department) AS rn
          FROM cts_dv_employee dv
          JOIN employee_department_mapping e ON lower(e.email) = lower(dv.email)
         WHERE e.enabled = true AND COALESCE(dv."department_id", '') <> ''
         GROUP BY dv."department_id", e.department
    ) t WHERE rn = 1
)
"""


def chain_cte() -> str:
    """CTE-ul lantului de departamente din log. Expune `mail` si `ch` cu ACELEASI coloane ca
    varianta pe `cts_department_moves`, ca endpoint-urile sa fie comune.

    Parametri asteptati: :date_from, :date_to, :dept, :only_solved (vezi _dept_report_params)."""
    return f"""
WITH {_DEPT_MAP_CTE},
log0 AS (
    SELECT {_num('l."id"')}                                   AS log_id,
           NULLIF(l."message_id", '')                          AS message_id,
           NULLIF(l."mid", '')                                 AS mid,
           NULLIF(l."department_id", '')                       AS dept_id,
           {_num('l."responsible_id"')}                        AS responsible_id,
           NULLIF(l."status", '')                              AS status,
           NULLIF(l."title", '')                               AS title,
           NULLIF(l."from_email", '')                          AS from_email,
           {_num('l."client_id"')}                             AS client_id,
           {_num('l."admin_email_folder_id"')}                 AS folder_id,
           COALESCE({_ts('l."assigned_at"')}, {_ts('l."created_at"')}, {_ts('l."date"')})
                                                               AS event_at,
           {_ts('l."solved_at"')}                              AS solved_at
      FROM {TABLE} l
     WHERE l."deleted_at" IS NULL OR l."deleted_at" = '' OR left(l."deleted_at", 4) = '0000'
),
log1 AS (
    -- Cheia de grupare: message_id (RFC). Randurile fara message_id nu se pot lipi de restul
    -- lantului, deci raman mailuri separate pe cheia lor proprie — mai bine decat sa fie unite
    -- gresit sub o cheie comuna.
    SELECT COALESCE(l.message_id, 'mid:' || l.mid, 'log:' || l.log_id::text) AS message_id,
           l.log_id, l.event_at, l.solved_at, l.status, l.title, l.from_email,
           l.client_id, l.folder_id, l.responsible_id, l.dept_id,
           COALESCE(dm.slug, 'cts_' || l.dept_id) AS dep
      FROM log0 l
      LEFT JOIN dept_map dm ON dm.cts_dept_id = l.dept_id
     WHERE l.dept_id IS NOT NULL AND l.event_at IS NOT NULL
),
mail AS (
    SELECT l.message_id,
           min(l.event_at) AS started_at,
           max(l.solved_at) AS solved_at,
           bool_or(l.solved_at IS NOT NULL
                   OR lower(COALESCE(l.status, '')) IN ('solved', 'rezolvat', 'closed')) AS is_solved,
           (SELECT min(g.email_id) FROM cts_ground_truth g
             WHERE g.message_id = l.message_id)                AS email_id
      FROM log1 l
     GROUP BY l.message_id
    HAVING (CAST(:date_from AS date) IS NULL
            OR min(l.event_at) >= CAST(:date_from AS date))
       AND (CAST(:date_to AS date) IS NULL
            OR min(l.event_at) < CAST(:date_to AS date) + interval '1 day')
       AND (NOT CAST(:only_solved AS boolean)
            OR bool_or(l.solved_at IS NOT NULL
                       OR lower(COALESCE(l.status, '')) IN ('solved', 'rezolvat', 'closed')))
),
ev AS (
    SELECT l.message_id, l.dep, l.event_at AS moved_at, l.log_id AS id,
           lag(l.dep) OVER (PARTITION BY l.message_id ORDER BY l.event_at, l.log_id) AS prev_dep
      FROM log1 l
      JOIN mail m ON m.message_id = l.message_id
),
seq AS (
    -- Pasii consecutivi pe acelasi departament (replicile per destinatar) se colapseaza.
    SELECT message_id, dep, moved_at,
           row_number() OVER (PARTITION BY message_id ORDER BY moved_at, id) AS step,
           count(*)     OVER (PARTITION BY message_id) AS steps
      FROM ev
     WHERE prev_dep IS NULL OR prev_dep IS DISTINCT FROM dep
),
kept AS (
    SELECT message_id
      FROM seq
     GROUP BY message_id
    HAVING CAST(:dept AS text) IS NULL OR bool_or(dep = CAST(:dept AS text))
),
ch AS (
    SELECT s.* FROM seq s JOIN kept k ON k.message_id = s.message_id
)
"""


def coverage(db) -> dict:
    """Cat acopera log-ul: randuri, mailuri distincte, fereastra de timp si cate department_id-uri
    NU s-au putut traduce in slug local (acelea apar ca `cts_<id>` in lanturi)."""
    try:
        row = db.execute(text(f"""
            WITH {_DEPT_MAP_CTE},
            l AS (
                SELECT NULLIF("message_id", '') AS message_id,
                       NULLIF("department_id", '') AS dept_id,
                       COALESCE({_ts('"assigned_at"')}, {_ts('"created_at"')}, {_ts('"date"')}) AS event_at
                  FROM {TABLE}
                 WHERE "deleted_at" IS NULL OR "deleted_at" = '' OR left("deleted_at", 4) = '0000'
            )
            SELECT count(*) AS rows_total,
                   count(DISTINCT message_id) AS mails,
                   min(event_at) AS first_at,
                   max(event_at) AS last_at,
                   count(DISTINCT l.dept_id) FILTER (WHERE dm.slug IS NULL
                                                     AND l.dept_id IS NOT NULL) AS unmapped_departments
              FROM l LEFT JOIN dept_map dm ON dm.cts_dept_id = l.dept_id
        """)).fetchone()
        m = row._mapping
        return {"rows": int(m["rows_total"] or 0), "mails": int(m["mails"] or 0),
                "first_at": m["first_at"], "last_at": m["last_at"],
                "unmapped_departments": int(m["unmapped_departments"] or 0)}
    except Exception as e:
        logger.warning("cts_email_log.coverage: %s", e)
        return {"rows": 0, "mails": 0, "first_at": None, "last_at": None,
                "unmapped_departments": 0}


def steps_for_mail(db, message_id: str, limit: int = 200) -> list:
    """Toti pasii bruti ai unui mail (fara colapsare), pentru drill-down-ul „de ce s-a mutat".
    Include si randurile duplicate per destinatar — acolo se vede cine a preluat si cand."""
    rows = db.execute(text(f"""
        WITH {_DEPT_MAP_CTE}
        SELECT {_num('l."id"')} AS log_id,
               NULLIF(l."department_id", '') AS dept_id,
               COALESCE(dm.slug, 'cts_' || NULLIF(l."department_id", '')) AS dep,
               {_num('l."responsible_id"')} AS responsible_id,
               {_num('l."admin_email_folder_id"')} AS folder_id,
               NULLIF(l."status", '') AS status,
               NULLIF(l."title", '') AS title,
               COALESCE({_ts('l."assigned_at"')}, {_ts('l."created_at"')}, {_ts('l."date"')}) AS event_at,
               {_ts('l."assigned_at"')} AS assigned_at,
               {_ts('l."solved_at"')}   AS solved_at,
               {_ts('l."updated_at"')}  AS updated_at,
               emp.name AS responsible_name
          FROM {TABLE} l
          LEFT JOIN dept_map dm ON dm.cts_dept_id = NULLIF(l."department_id", '')
          LEFT JOIN LATERAL (
              SELECT e.name
                FROM cts_dv_employee dv
                JOIN employee_department_mapping e ON lower(e.email) = lower(dv.email)
               WHERE dv.admin_id = NULLIF(l."responsible_id", '')
               ORDER BY e.enabled DESC, e.id
               LIMIT 1
          ) emp ON true
         WHERE (l."deleted_at" IS NULL OR l."deleted_at" = '' OR left(l."deleted_at", 4) = '0000')
           AND (l."message_id" = :mid OR ('mid:' || NULLIF(l."mid", '')) = :mid)
         ORDER BY event_at NULLS LAST, log_id
         LIMIT :lim
    """), {"mid": message_id, "lim": limit}).fetchall()
    return [dict(r._mapping) for r in rows]


# ── Responsabili (pentru filtrul si coloana din „Cazuri concrete") ────────────
# Log-ul tine `responsible_id` = admin CTS. Traducerea in angajat trece prin
# `cts_dv_employee.admin_id` -> email -> `employee_department_mapping`, NICIODATA pe egalitate
# de nume (numele din CTS e scris altfel decat in mapping — vezi CLAUDE.md).
_RESP_LATERAL = """
    LEFT JOIN LATERAL (
        SELECT e.name
          FROM cts_dv_employee dv
          JOIN employee_department_mapping e ON lower(e.email) = lower(dv.email)
         WHERE dv.admin_id = NULLIF(l2."responsible_id", '')
         ORDER BY e.enabled DESC, e.id
         LIMIT 1
    ) emp ON true
"""

_MID_MATCH = """(l2."message_id" = {mid} OR ('mid:' || NULLIF(l2."mid", '')) = {mid})"""


def responsible_exists_sql(mid_expr: str) -> str:
    """EXISTS pentru filtrul pe responsabil: dupa nume (potrivire partiala, case-insensitive)
    sau dupa id-ul CTS, ca sa mearga si cand angajatul nu e inca mapat local.
    `position(... in ...)` in loc de ILIKE: `%` e placeholder de parametru la psycopg2, deci
    orice `%` literal in SQL-ul asta ar trebui dublat — vezi nota de la `_ts`."""
    return f"""EXISTS (
        SELECT 1 FROM {TABLE} l2 {_RESP_LATERAL}
         WHERE {_MID_MATCH.format(mid=mid_expr)}
           AND (position(lower(CAST(:resp AS text)) in lower(emp.name)) > 0
                OR NULLIF(l2."responsible_id", '') = CAST(:resp AS text))
    )"""


def responsibles_select_sql(mid_expr: str, limit: int = 4) -> str:
    """Numele responsabililor unui mail, pentru coloana din lista de cazuri (max `limit`)."""
    return f"""(
        SELECT string_agg(DISTINCT n, ', ')
          FROM (
            SELECT emp.name AS n
              FROM {TABLE} l2 {_RESP_LATERAL}
             WHERE {_MID_MATCH.format(mid=mid_expr)}
               AND emp.name IS NOT NULL
             LIMIT {int(limit)}
          ) r
    )"""


def responsible_options(db, limit: int = 200) -> list:
    """Lista responsabililor care apar in log — pentru dropdown-ul de filtru."""
    try:
        rows = db.execute(text(f"""
            SELECT emp.name AS name, count(*) AS n
              FROM {TABLE} l2 {_RESP_LATERAL}
             WHERE emp.name IS NOT NULL
             GROUP BY emp.name
             ORDER BY n DESC, emp.name
             LIMIT :lim
        """), {"lim": limit}).fetchall()
        return [{"name": r._mapping["name"], "n": int(r._mapping["n"])} for r in rows]
    except Exception as e:
        logger.warning("cts_email_log.responsible_options: %s", e)
        return []
