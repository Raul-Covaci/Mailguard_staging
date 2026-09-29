"""T3-S1 — apelurile cu transcript sub prag nu se scorează și nu se reselectează.

Postgres local efemer (pgserver): tabelele calls / call_ai_scores / call_scoring_prompts au forma
folosită de cod, iar migrația 20260929d se aplică din fișier. Gateway-ul e înlocuit; se numără
apelurile AI. Mediile se verifică prin endpoint-ul real /calls/analytics/dashboard.
"""
import os
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

try:
    import pgserver
except ImportError:          # pragma: no cover
    pgserver = None

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.api.v1 import calls_analytics as CA
from app.services import call_scorer, feature_flags

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UP = os.path.join(ROOT, "migrations", "20260929d_call_score_min_transcript.sql")
DOWN = os.path.join(ROOT, "migrations", "down", "20260929d_call_score_min_transcript.down.sql")
METRICS = os.path.join(ROOT, "scripts", "metrics", "call_score_skipped.sql")

_TEXT = ("agent_speaker client_speaker agent_advice_empathy agent_advice_professionalism "
         "agent_advice_clarity agent_advice_next_steps agent_next_steps_observation issue_summary "
         "issue_main_problem issue_main_solution model").split()
_NUM = ("agent_explaining_solution agent_patient agent_understanding agent_politeness agent_empathy "
        "agent_transparency agent_score_total customer_explaining customer_patient customer_understanding "
        "customer_politeness customer_empathy customer_score_total customer_unacknowledged_count").split()
_BOOL = ("is_valid_call agent_next_steps_clear issue_resolved issue_within_company_scope "
         "agentul_sa_prezentat clientul_aminta_judecata clientul_aminta_renuntare clientul_contactat_anterior "
         "masini_care_nu_transmit").split()
_JSON = ("agent_actions agent_vulgar_words customer_vulgar_words issue_tags customer_additional_requests "
         "binary_evidence").split()
SCHEMA = (
    "CREATE TABLE settings (key varchar(100) PRIMARY KEY, value jsonb NOT NULL, description text, "
    "updated_by varchar(100), updated_at timestamptz DEFAULT now());"
    "CREATE TABLE calls (id bigserial PRIMARY KEY, transcript text, transcript_turns jsonb, "
    "started_at timestamptz DEFAULT now(), caller_number text, callee_number text, direction text, "
    "call_status text, duration_seconds integer, ai_category text, ai_tone text, agent_extension text);"
    "CREATE TABLE call_phone_blacklist (phone_number text);"
    "CREATE TABLE call_scoring_prompts (key text PRIMARY KEY, prompt_text text, output_type text, "
    "enabled boolean DEFAULT true);"
    "CREATE TABLE call_ai_scores (id bigserial PRIMARY KEY, call_id bigint UNIQUE, scored_at timestamptz, "
    + ", ".join([f"{c} text" for c in _TEXT] + [f"{c} numeric" for c in _NUM]
                + [f"{c} boolean" for c in _BOOL] + [f"{c} jsonb" for c in _JSON]) + ");"
)
PROMPTS = ["agentScore", "issueResolution", "checkForValidCall"]
AGENT = {"explainingTheSolution": 8, "patient": 8, "understanding": 8, "politeness": 8, "empathy": 8,
         "transparency": 8}
SHORT, LONG = "AGENT: Alo? " * 5, "AGENT: Buna ziua, CargoTrack. CLIENT: masina nu transmite. " * 20


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    if pgserver is None:
        pytest.fail("pgserver lipsește — instalare: venv/bin/pip install -r requirements-dev.txt")
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("pgs1")), cleanup_mode="stop")
    eng = sa.create_engine(srv.get_uri())
    with eng.begin() as c:
        c.exec_driver_sql(SCHEMA)
        c.exec_driver_sql(open(UP, encoding="utf-8").read())
    yield eng
    eng.dispose()


@pytest.fixture
def env(pg, monkeypatch):
    with pg.begin() as c:
        c.exec_driver_sql("TRUNCATE calls, call_ai_scores, call_scoring_prompts RESTART IDENTITY")
        for k in PROMPTS:
            c.execute(sa.text("INSERT INTO call_scoring_prompts (key, prompt_text, output_type) VALUES (:k, 'P', 'json')"),
                      {"k": k})
    Session = sessionmaker(bind=pg)
    monkeypatch.setattr(call_scorer, "SessionLocal", Session)
    cfg = {"calls.score_min_transcript_chars": 300}
    monkeypatch.setattr(feature_flags, "get_value", lambda key, default=None: cfg.get(key, default))
    monkeypatch.setattr(call_scorer, "seed_prompts_if_empty", lambda db=None: 0)
    monkeypatch.setattr(call_scorer, "sync_prompts_from_repo", lambda *a, **k: {})
    monkeypatch.setattr(call_scorer.iris_ai, "is_configured", lambda: True)

    def fake(system, content, **kw):
        key = kw["task"].replace("call_score_", "")
        parsed = {"agentScore": AGENT, "issueResolution": {"problemWasSolved": True},
                  "checkForValidCall": {"isValid": True}}[key]
        return {"ok": True, "parsed": parsed, "text": ""}
    rp = MagicMock(side_effect=fake)
    monkeypatch.setattr(call_scorer.iris_ai, "run_prompt", rp)
    s = Session()
    yield SimpleNamespace(cfg=cfg, rp=rp, pg=pg, s=s)
    s.close()


def _call(pg, transcript):
    with pg.begin() as c:
        return c.execute(sa.text("INSERT INTO calls (transcript, caller_number, callee_number, direction, "
                                 "call_status, duration_seconds) VALUES (:t, '0711', '0722', 'inbound', "
                                 "'ANSWERED', 60) RETURNING id"), {"t": transcript}).scalar()


def _score_row(pg, cid):
    with pg.connect() as c:
        r = c.execute(sa.text("SELECT * FROM call_ai_scores WHERE call_id=:i"), {"i": cid}).fetchone()
        return dict(r._mapping) if r else None


def test_below_threshold_no_ai_and_marked(env):
    cid = _call(env.pg, SHORT)
    out = call_scorer.score_call(cid)
    assert out == {"ok": False, "reason": "too_short", "skipped": True, "call_id": cid}
    assert env.rp.call_count == 0
    row = _score_row(env.pg, cid)
    assert row["skip_reason"] == "too_short" and row["agent_score_total"] is None


def test_above_threshold_scores_as_today(env):
    cid = _call(env.pg, LONG)
    assert call_scorer.score_call(cid)["ok"] is True
    assert env.rp.call_count == len(PROMPTS)
    row = _score_row(env.pg, cid)
    assert row["skip_reason"] is None and float(row["agent_score_total"]) == 8.0


def test_threshold_zero_is_todays_behaviour(env):
    env.cfg["calls.score_min_transcript_chars"] = 0
    cid = _call(env.pg, SHORT)
    assert call_scorer.score_call(cid)["ok"] is True and env.rp.call_count == len(PROMPTS)


def test_length_is_measured_on_raw_transcript(env):
    # Diarizarea poate fi mai lungă/scurtă; pragul se aplică pe `calls.transcript`.
    cid = _call(env.pg, SHORT)
    with env.pg.begin() as c:
        c.execute(sa.text("UPDATE calls SET transcript_turns = CAST(:t AS jsonb) WHERE id=:i"),
                  {"t": '[{"speaker": "AGENT", "text": "%s"}]' % ("x" * 900), "i": cid})
    assert call_scorer.score_call(cid)["skipped"] is True and env.rp.call_count == 0


def test_marked_call_is_not_reselected(env):
    short, long_ = _call(env.pg, SHORT), _call(env.pg, LONG)
    first = call_scorer.score_batch(limit=10)
    assert first["total"] == 2 and first["scored"] == 1 and first["skipped_too_short"] == 1
    n = env.rp.call_count
    again = call_scorer.score_batch(limit=10, rescore_null=True)
    assert again["total"] == 0 and env.rp.call_count == n
    assert _score_row(env.pg, short)["skip_reason"] == "too_short"


def test_rescore_missing_binary_ignores_marked(env, monkeypatch):
    cid = _call(env.pg, SHORT)
    call_scorer.score_call(cid)
    spy = MagicMock(return_value={"ok": True})
    monkeypatch.setattr(call_scorer, "score_call", spy)
    out = CA.analytics_rescore_missing_binary(db=env.s, admin={})
    assert out["rescored"] == 0 and not spy.called


def test_force_scores_anyway(env):
    cid = _call(env.pg, SHORT)
    call_scorer.score_call(cid)
    out = CA.analytics_score_now(cid, force=True, admin={}, db=env.s)
    assert out["ok"] is True and env.rp.call_count == len(PROMPTS)
    assert _score_row(env.pg, cid)["skip_reason"] is None


def test_dashboard_averages_exclude_skipped(env):
    long_, short = _call(env.pg, LONG), _call(env.pg, SHORT)
    call_scorer.score_call(long_)
    call_scorer.score_call(short)
    kpi = CA.analytics_dashboard(date_from=None, date_to=None, department=None, agent=None,
                                 exclude_blacklist=False, days=30, db=env.s, admin={})["kpi"]
    assert kpi["total"] == 2 and kpi["scored_calls"] == 1          # sărit = nescorat, nu scor 0
    assert float(kpi["avg_agent_score"]) == 8.0 and kpi["invalid_calls"] == 0


def test_every_analytics_join_excludes_skipped():
    src = open(CA.__file__, encoding="utf-8").read()
    joins = re.findall(r"JOIN call_ai_scores cas ON cas\.call_id = c\.id[^\n]*", src)
    assert joins and all("cas.skip_reason IS NULL" in j for j in joins)


def test_metrics_query(env):
    call_scorer.score_call(_call(env.pg, LONG))
    call_scorer.score_call(_call(env.pg, SHORT))
    call_scorer.score_call(_call(env.pg, SHORT))
    with env.pg.connect() as c:
        [row] = c.exec_driver_sql(open(METRICS, encoding="utf-8").read()).fetchall()
    assert (row.scored, row.skipped_too_short, float(row.skipped_pct)) == (1, 2, 66.67)


def test_migration_down_then_up(pg):
    with pg.begin() as c:
        c.exec_driver_sql("INSERT INTO call_ai_scores (call_id, scored_at, skip_reason) VALUES (999, now(), 'too_short')")
        c.exec_driver_sql(open(DOWN, encoding="utf-8").read())
        assert c.exec_driver_sql("SELECT count(*) FROM call_ai_scores WHERE call_id=999").scalar() == 0
        c.exec_driver_sql(open(UP, encoding="utf-8").read())
        c.exec_driver_sql(open(UP, encoding="utf-8").read())
        assert c.exec_driver_sql("SELECT value FROM settings WHERE key='calls.score_min_transcript_chars'").scalar() == 0
