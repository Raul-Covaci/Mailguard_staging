"""T3-G2 — în afara producției Mailguard nu face apeluri AI automate.

Plasa (`iris_ai.run_prompt` -> AI_DISABLED_ENV, fără HTTP și fără ai_call_log) plus porțile de pe
căile automate (tick, clasificare emailuri, op_series, pipeline apeluri, satisfacție, rezumatul de
productivitate). Permisiunea `ai.allow_non_production` și producția = comportamentul de azi.
"""
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.api.v1 import documents
from app.api.v1 import emails as emails_api
from app.services import (calls_pipeline, feature_flags, iris_ai, op_extractor, process_email,
                          productivity_notifier, satisfaction_snapshot)
from app.api.v1.reports import _RETRY_CODES


@pytest.fixture
def staging(monkeypatch):
    monkeypatch.setenv("MAILGUARD_ENV", "staging")
    monkeypatch.setattr(feature_flags, "is_enabled", lambda key: False)


@pytest.fixture
def staging_allowed(monkeypatch):
    monkeypatch.setenv("MAILGUARD_ENV", "staging")
    monkeypatch.setattr(feature_flags, "is_enabled", lambda key: key == "ai.allow_non_production")


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setenv("IRIS_AI_URL", "http://gateway.test/run-prompt")
    monkeypatch.setenv("IRIS_AI_KEY", "k")
    monkeypatch.delenv("AI_DISABLED", raising=False)
    resp = SimpleNamespace(status_code=200, text="{}",
                           json=lambda: {"ok": True, "raw_text": "x", "parsed": None, "model": "m",
                                         "usage": {"cost_usd": 0.001}})
    post = MagicMock(return_value=resp)
    monkeypatch.setattr(iris_ai.httpx, "post", post)
    log = MagicMock()
    monkeypatch.setattr(iris_ai, "_log_call", log)
    return SimpleNamespace(post=post, log=log)


# ── Plasa din run_prompt ─────────────────────────────────────────────────────

def test_run_prompt_blocked_outside_production(staging, gateway):
    res = iris_ai.run_prompt("SYS", "content", task="cargo360:email_category:abc")
    assert res["ok"] is False and res["error"]["code"] == "AI_DISABLED_ENV"
    assert not gateway.post.called, "HTTP trimis în afara producției"
    assert not gateway.log.called, "rând în ai_call_log pentru un apel care n-a existat"


def test_run_prompt_with_permission_is_unchanged(staging_allowed, gateway):
    res = iris_ai.run_prompt("SYS", "content", task="cargo360:x")
    assert res["ok"] is True and gateway.post.call_count == 1 and gateway.log.call_count == 1


def test_run_prompt_on_production_is_unchanged(gateway):
    res = iris_ai.run_prompt("SYS", "content", task="cargo360:x")
    assert res["ok"] is True and gateway.post.call_count == 1


def test_error_is_permanent_for_retrying_callers(staging, gateway):
    err = iris_ai.run_prompt("SYS", "c", task="cargo360:doc_classify:a")["error"]
    assert err["code"] not in _RETRY_CODES                  # nicio buclă de retry în documents
    assert documents._is_transient_ai_err(err["message"]) is False   # nici retry_transient


# ── Tick-ul ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tick(monkeypatch):
    """Toți pașii tick-ului înlocuiți; rămâne doar logica de orchestrare din process_now."""
    import importlib
    import app.database
    stubs = {
        "app.services.process_email": ["process_pending_batch", "advance_queue_batch",
                                       "advance_op_extract_batch"],
        "app.services.maintenance": ["fire_maintenance"],
        "app.services.ndr_report": ["run_daily_ndr_report_if_due"],
        "app.services.cts_groundtruth_sync": ["run_recent_if_due"],
        "app.services.cts_calls_sync": ["run_recent_if_due"],
        "app.services.cts_tasks_sync": ["run_recent_if_due"],
        "app.services.iris_employee_sync": ["run_daily_if_due", "run_vacation_dv_sync_if_due"],
        "app.services.productivity_notifier": ["send_monthly_reports_if_due"],
        "app.services.vathub_inbox": ["run_once"],
        "app.services.pontaj_sync": ["run_recent_if_due"],
        "app.services.device_ops_suport2_sync": ["run_recent_if_due"],
        "app.services.iris_dv_autosync": ["run_due_syncs"],
        "app.services.quality_eval_sync": ["run_recent_if_due"],
        "app.services.ai_cache": ["purge_expired_if_due"],
        "app.services.calls_pipeline": ["kick"],
    }
    for mod, names in stubs.items():
        m = importlib.import_module(mod)
        for n in names:
            monkeypatch.setattr(m, n, MagicMock(return_value={}))
    monkeypatch.setattr(app.database, "get_db", lambda: iter([MagicMock()]))
    drain = MagicMock(return_value=True)
    monkeypatch.setattr(documents, "_kick_drain", drain)
    return SimpleNamespace(drain=drain)


def test_tick_outside_production_skips_ai_steps(staging, gateway, tick, caplog):
    caplog.set_level(logging.INFO, logger="mailguard.emails")
    res = emails_api.process_now(limit=5)
    assert not tick.drain.called, "drain-ul de documente pornit în afara producției"
    assert not gateway.post.called and not gateway.log.called
    assert "drain documente" in res["ai_skipped_env"]
    msgs = [r.getMessage() for r in caplog.records if "pasi AI sariti" in r.getMessage()]
    assert len(msgs) == 1 and "mediu staging" in msgs[0]
    assert process_email.process_pending_batch.called            # ingestia rulează ca azi
    assert calls_pipeline.kick.called                            # audio + transcriere rulează


@pytest.mark.parametrize("fixture", ["staging_allowed", None])
def test_tick_with_permission_or_production_is_unchanged(request, fixture, gateway, tick, caplog):
    if fixture:
        request.getfixturevalue(fixture)
    caplog.set_level(logging.INFO, logger="mailguard.emails")
    res = emails_api.process_now(limit=5)
    assert tick.drain.called and "ai_skipped_env" not in res
    assert not [r for r in caplog.records if "pasi AI sariti" in r.getMessage()]


# ── Clasificarea emailurilor pe calea clean ──────────────────────────────────

class _Cur:
    def __init__(self, log):
        self.log, self._last = log, ""

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        self._last = sql

    def fetchone(self):
        if "information_schema.columns" in self._last:
            return ("queue_status",)
        if self._last.startswith("SELECT id, subject"):
            return {"id": 7, "subject": "s", "from_address": "a@b.ro", "from_name": "", "body_text": "b",
                    "body_html": "", "conversation_id": None, "received_at": None}
        if "SELECT ai_op_extract_attempts" in self._last:
            return (0,)
        return None


class _Conn:
    def __init__(self, log):
        self.log = log

    def cursor(self, **kw):
        return _Cur(self.log)

    def commit(self):
        self.log.append(("COMMIT", None))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_advance_one_clean_skips_ai_and_makes_email_eligible(staging, monkeypatch):
    log = []
    monkeypatch.setattr(process_email, "_conn", lambda: _Conn(log))
    monkeypatch.setattr(process_email, "_QUEUE_COLS", None)
    cls = MagicMock(side_effect=AssertionError("clasificare AI în afara producției"))
    monkeypatch.setattr("app.services.category_classifier.classify_category", cls)
    ctx = MagicMock(side_effect=AssertionError("context client AI în afara producției"))
    monkeypatch.setattr("app.services.client_context.get_context_summary", ctx)
    monkeypatch.setattr(process_email, "_ai_context_enabled", lambda cur: True)
    out = process_email.advance_one_clean(7)
    assert out == {"status": "ready_for_cts", "reason": "ai_classification_off"}
    sqls = [s for s, _ in log]
    assert any("ai_status='skipped'" in s for s in sqls)
    assert not any(p and "error_nova" in p for _, p in log if isinstance(p, (list, tuple)))


def test_intent_gate_and_context_helpers(staging, monkeypatch):
    assert process_email._ai_allowed_env() is False
    em = {"id": 1}
    process_email._inject_client_context(em, _Cur([]))
    assert em["_client_context"] == {}


# ── op_series: fără vision, finalizare imediată ──────────────────────────────

def test_op_series_finalizes_immediately_outside_production(staging, monkeypatch, tmp_path):
    img = tmp_path / "op.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 50)
    rows = [SimpleNamespace(_mapping={"storage_path": str(img), "content_type": "image/png", "name": "op.png"})]
    email_row = SimpleNamespace(_mapping={"subject": "", "body_text": ""})

    class _S:
        def __init__(self):
            self.n = 0

        def execute(self, *a, **k):
            self.n += 1
            return SimpleNamespace(fetchone=lambda: email_row, fetchall=lambda: rows)

        def close(self):
            pass
    monkeypatch.setattr(op_extractor, "SessionLocal", lambda: _S())
    monkeypatch.setattr(op_extractor, "_host_path", lambda p: p)
    monkeypatch.setattr(op_extractor, "_doc_text_local", lambda p, m: "")
    rp = MagicMock(side_effect=AssertionError("vision în afara producției"))
    monkeypatch.setattr(op_extractor.iris_ai, "run_prompt", rp)
    result = op_extractor.extract_op_series(42)
    assert result == {"series": None, "department": "suport_1", "ai_blocked": True}

    log = []
    monkeypatch.setattr("app.services.process_email._conn", lambda: _Conn(log))
    monkeypatch.setattr(process_email, "_QUEUE_COLS", None)
    stats = {"processed": 0, "series_found": 0, "fallback": 0, "error": 0}
    op_extractor._process_one_op(42, stats)
    assert stats["fallback"] == 1
    assert any("queue_status" in s and p and "ready_for_cts" in p for s, p in log if isinstance(p, list))


def test_op_series_on_production_unchanged(monkeypatch, tmp_path):
    img = tmp_path / "op.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 50)
    rp = MagicMock(return_value={"ok": True, "text": "NONE|NONE"})
    monkeypatch.setattr(op_extractor.iris_ai, "run_prompt", rp)
    monkeypatch.setattr(feature_flags, "is_enabled", lambda key: False)
    assert op_extractor._vision_extract_series(str(img), "image/png") == {"series": None, "currency": None}
    assert rp.called


# ── Pipeline apeluri, satisfacție, productivitate ────────────────────────────

class _LockDB:
    def execute(self, *a, **k):
        return SimpleNamespace(scalar=lambda: True)

    def close(self):
        pass


@pytest.mark.parametrize("fixture,ai_steps", [("staging", False), ("staging_allowed", True), (None, True)])
def test_calls_pipeline_ai_steps(request, monkeypatch, fixture, ai_steps):
    if fixture:
        request.getfixturevalue(fixture)
    monkeypatch.setattr(calls_pipeline, "SessionLocal", lambda: _LockDB())
    from app.services import call_audio, call_transcribe, call_classifier, call_scorer
    mocks = {}
    for mod, name in [(call_audio, "process_pending_batch"), (call_transcribe, "process_pending_batch"),
                      (call_classifier, "process_pending_batch"), (call_classifier, "process_diarize_batch"),
                      (call_scorer, "score_batch")]:
        mocks[(mod.__name__, name)] = MagicMock(return_value={})
        monkeypatch.setattr(mod, name, mocks[(mod.__name__, name)])
    calls_pipeline._run_pipeline(5)
    assert mocks[("app.services.call_audio", "process_pending_batch")].called
    assert mocks[("app.services.call_transcribe", "process_pending_batch")].called
    assert mocks[("app.services.call_classifier", "process_pending_batch")].called is ai_steps
    assert mocks[("app.services.call_classifier", "process_diarize_batch")].called is ai_steps


def test_satisfaction_snapshot_skipped_outside_production(staging, monkeypatch):
    monkeypatch.setattr(satisfaction_snapshot.psycopg2, "connect",
                        MagicMock(side_effect=AssertionError("snapshot pornit în afara producției")))
    out = satisfaction_snapshot.run_monthly_snapshot(month_key="2026-09")
    assert out["skipped_env"] is True and out["errors"] == 0 and out["ai_calls"] == 0


def test_productivity_summary_uses_template_outside_production(staging, monkeypatch):
    monkeypatch.setattr(iris_ai, "is_configured", lambda: True)
    monkeypatch.setattr(iris_ai, "run_prompt", MagicMock(side_effect=AssertionError("AI apelat")))
    txt = productivity_notifier._generate_ai_summary(
        "operational", "Operational", 2026, 8, 2026, 9,
        [{"department": "suport_1", "status": "atins", "obiectiv_atins": 1.0, "obiectiv_real": 0.9}], [])
    assert "Suport 1" in txt
