"""T3-R1 — raportul de costuri AI: pe flux, costul eșecurilor, local_cache exclus, economia din cache.

Date de test într-un ai_call_log real (Postgres local efemer); raportul se cere exact ca din UI, prin
endpoint, în CSV (parsat) și PDF (generat).
"""
import csv
import io

import pytest

try:
    import pgserver
except ImportError:          # pragma: no cover
    pgserver = None

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.api.v1 import ai_category
from app.services import cost_report

DAY = "2026-09-15"
ROWS = [  # task, model, cost, ok
    ("call_score_agentScore", "claude-haiku-4-5", 0.02, True),
    ("call_score_agentScore", "claude-haiku-4-5", 0.02, True),
    ("call_score_agentScore", "claude-haiku-4-5", 0.01, False),
    ("cargo360:call_category", "claude-haiku-4-5", 0.005, True),
    ("cargo360:call_category", "claude-haiku-4-5", 0.003, False),
    ("cargo360:email_category:abc123def0", "claude-haiku-4-5", 0.006, True),
    ("cargo360:email_department:0a1b2c3d4e", "claude-haiku-4-5", 0.002, True),
    ("cargo360:doc_segment:0123456789ab:_", "claude-sonnet-4-6", 0.01, True),       # hash rămâne în nume
    ("cargo360:doc_segment:ba9876543210:Talon", "claude-sonnet-4-6", 0.012, False),
    ("cargo360:op_series:abcdef123456", "claude-sonnet-4-6", 0.009, True),
    ("satisfaction_v6_trajectory", "claude-sonnet-4-6", 0.02, True),
    ("cargo360:client_context_summary", "claude-haiku-4-5", 0.001, True),
    ("image_orient_detect", "claude-haiku-4-5", 0.002, True),
    ("cargo360:email_category:ffff0000aa", None, None, False),                     # eșec fără cost
]
LOCAL_CACHE = ("cargo360:doc_segment:0123456789ab:_", "local_cache", 0.5, True)
EXPECTED_COST = round(sum(r[2] or 0 for r in ROWS), 6)
EXPECTED_FAILED = round(sum(r[2] or 0 for r in ROWS if not r[3]), 6)


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    if pgserver is None:
        pytest.fail("pgserver lipsește — instalare: venv/bin/pip install -r requirements-dev.txt")
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("pgr1")), cleanup_mode="stop")
    eng = sa.create_engine(srv.get_uri())
    with eng.begin() as c:
        c.exec_driver_sql(
            "CREATE TABLE ai_call_log (id bigserial PRIMARY KEY, task varchar(120), model varchar(80), "
            "tokens_in integer, tokens_out integer, cost_usd numeric(12,6), ok boolean, "
            "error_code varchar(40), created_at timestamptz NOT NULL DEFAULT now(), email_id bigint)")
    yield eng
    eng.dispose()


@pytest.fixture
def db(pg):
    with pg.begin() as c:
        c.exec_driver_sql("TRUNCATE ai_call_log")
        c.exec_driver_sql("DROP TABLE IF EXISTS ai_cache_hit_log")
        for task, model, cost, ok in ROWS + [LOCAL_CACHE]:
            c.execute(sa.text("INSERT INTO ai_call_log (task, model, cost_usd, ok, tokens_in, tokens_out, created_at) "
                              "VALUES (:t, :m, :c, :ok, 100, 10, CAST(:d AS timestamptz))"),
                      {"t": task, "m": model, "c": cost, "ok": ok, "d": DAY + " 10:00+03"})
        c.execute(sa.text("INSERT INTO ai_call_log (task, model, cost_usd, ok, created_at) "
                          "VALUES ('call_score_x', 'claude-haiku-4-5', 9.99, true, '2026-08-01 10:00+03')"))
    s = sessionmaker(bind=pg)()
    yield s
    s.close()


def _csv(db):
    resp = ai_category.ai_cost_report(date_from=DAY, date_to=DAY, fmt="csv", db=db, admin={})
    rows = list(csv.reader(io.StringIO(resp.body.decode("utf-8-sig"))))
    names = {"PER MODEL", "PE FLUX", "PER TASK", "TASK x MODEL", "ECONOMIE DIN CACHE"}
    sections, cur = {}, None
    for r in rows:
        if len(r) == 1 and r[0] in names:
            cur = sections.setdefault(r[0], [])
        elif cur is not None and r:
            cur.append(r)
    return sections


def test_flow_sums_equal_total(db):
    sec = _csv(db)
    flows = {r[0]: r for r in sec["PE FLUX"][1:]}
    total = flows.pop("TOTAL")
    assert round(sum(float(r[2]) for r in flows.values()), 6) == float(total[2]) == EXPECTED_COST
    assert sum(int(r[1]) for r in flows.values()) == int(total[1]) == len(ROWS)
    assert set(flows) == {"scoring", "call_category", "email_classification", "documente", "op_series",
                          "satisfaction", "client_context", "altele"}
    assert int(flows["documente"][1]) == 2           # task-urile cu hash nu mai cad în „alte N"
    assert float(flows["scoring"][4]) == 0.01 and int(flows["scoring"][3]) == 1


def test_failed_cost_per_task_and_total(db):
    sec = _csv(db)
    tasks = {r[0]: r for r in sec["PER TASK"][1:]}
    assert float(tasks["call_score_agentScore"][6]) == 0.01
    assert tasks["call_score_agentScore"][7] == "20.00"          # 0.01 din 0.05
    assert float(tasks["cargo360:call_category"][6]) == 0.003
    tot = tasks["TOTAL"]
    assert float(tot[6]) == EXPECTED_FAILED and int(tot[5]) == sum(1 for r in ROWS if not r[3])


def test_local_cache_excluded_everywhere(db):
    sec = _csv(db)
    model_rows = {r[0]: r for r in sec["PER MODEL"][1:]}
    assert "local_cache" not in model_rows
    assert float(model_rows["TOTAL"][2]) == EXPECTED_COST        # fără cei 0,5 USD „evitați"
    assert all(r[1] != "local_cache" for r in sec["TASK x MODEL"][1:])


def test_cache_section_empty_without_table(db):
    assert _csv(db)["ECONOMIE DIN CACHE"] == [["prefix", "hituri", "cost_evitat_usd"]]


def test_cache_section_with_hits(db, pg):
    with pg.begin() as c:
        c.exec_driver_sql("CREATE TABLE ai_cache_hit_log (id bigserial PRIMARY KEY, created_at timestamptz "
                          "NOT NULL DEFAULT now(), task varchar(120), task_prefix varchar(80) NOT NULL, "
                          "cache_key char(64) NOT NULL, saved_cost_usd numeric(12,6))")
        for pref, cost, day in [("doc_segment", 0.01, DAY), ("doc_segment", 0.02, DAY),
                                ("op_series", 0.009, DAY), ("op_series", 5.0, "2026-08-01")]:
            c.execute(sa.text("INSERT INTO ai_cache_hit_log (task_prefix, cache_key, saved_cost_usd, created_at) "
                              "VALUES (:p, repeat('a',64), :c, CAST(:d AS timestamptz))"),
                      {"p": pref, "c": cost, "d": day + " 12:00+03"})
    rows = _csv(db)["ECONOMIE DIN CACHE"][1:]
    assert [(r[0], int(r[1]), float(r[2])) for r in rows] == [("doc_segment", 2, 0.03), ("op_series", 1, 0.009)]


def test_pdf_renders_with_new_sections(db):
    resp = ai_category.ai_cost_report(date_from=DAY, date_to=DAY, fmt="pdf", db=db, admin={})
    assert resp.body[:4] == b"%PDF"
    import fitz
    text = "".join(p.get_text() for p in fitz.open(stream=resp.body, filetype="pdf"))
    for needle in ("Pe flux", "Cost eșuate", "Economie din cache", "email_classification", "Per model AI"):
        assert needle in text
    # totalul pe task, cu % cost pe eșecuri (0,033 din 0,12 = 27,5%)
    assert "%.1f%%" % (100 * EXPECTED_FAILED / EXPECTED_COST) in text


@pytest.mark.parametrize("task,flow", [
    ("call_score_agentAdviceClarity", "scoring"), ("cargo360:call_category", "call_category"),
    ("cargo360:call_cat_prompt_regen", "altele"), ("cargo360:email_priority_v3", "email_classification"),
    ("cargo360:email_assignee", "email_classification"), ("cargo360:intent_gate", "email_classification"),
    ("cargo360:doc_extract_vision:Talon:ab12cd34ef56", "documente"), ("cargo360:op_series:ab", "op_series"),
    ("satisfaction_v6_trajectory", "satisfaction"), ("cargo360:client_context_summary", "client_context"),
    ("productivity_summary", "altele"), ("", "altele"), (None, "altele"),
])
def test_task_flow_mapping(task, flow):
    assert cost_report.task_flow(task) == flow
