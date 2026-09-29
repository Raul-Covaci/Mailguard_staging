"""T3-O14 — „Haiku întâi" și shadow pe vision-ul de clasificare (doc_classify_vision, doc_segment).

Gateway-ul e înlocuit (răspunsul depinde de `model_hint`); doc_model_shadow e real, pe un Postgres
local efemer, cu migrația 20260929c aplicată din fișier.
"""
import io
import json
import os
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

try:
    import pgserver
except ImportError:          # pragma: no cover
    pgserver = None

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.api.v1 import documents as D
from app.services import ai_cache, feature_flags

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UP = os.path.join(ROOT, "migrations", "20260929c_doc_model_shadow.sql")
DOWN = os.path.join(ROOT, "migrations", "down", "20260929c_doc_model_shadow.down.sql")
METRICS = os.path.join(ROOT, "scripts", "metrics", "doc_model_shadow.sql")

SECRET = "Popescu Ion CNP 1800101123456"      # un `reason` cu date personale: NU are voie în shadow
SONNET = {"type_id": 3, "category": "vehicul", "confidence": 0.97, "is_document": True,
          "reason": SECRET, "documents": [{"type_id": 3, "bbox": [0, 0, 1, 1]}], "starts_new": True}


def _resp(parsed, model, text=None, cost=0.01):
    return {"ok": True, "text": text if text is not None else json.dumps(parsed), "parsed": parsed,
            "model": model, "usage": {"cost_usd": cost}, "error": None}


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    if pgserver is None:
        pytest.fail("pgserver lipsește — instalare: venv/bin/pip install -r requirements-dev.txt")
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("pgo14")), cleanup_mode="stop")
    eng = sa.create_engine(srv.get_uri())
    with eng.begin() as c:
        c.exec_driver_sql("CREATE TABLE IF NOT EXISTS settings (key varchar(100) PRIMARY KEY, "
                          "value jsonb NOT NULL, description text, updated_by varchar(100), "
                          "updated_at timestamptz DEFAULT now())")
        c.exec_driver_sql(open(UP, encoding="utf-8").read())
    yield eng
    eng.dispose()


@pytest.fixture
def env(pg, monkeypatch, tmp_path):
    with pg.begin() as c:
        c.exec_driver_sql("TRUNCATE doc_model_shadow RESTART IDENTITY")
    Session = sessionmaker(bind=pg)
    monkeypatch.setattr(D, "SessionLocal", Session)
    cfg = {"documents.haiku_first_tasks": [], "documents.haiku_shadow_tasks": [],
           "documents.haiku_shadow_sample": 0.3, "documents.haiku_min_confidence": 0.90}
    monkeypatch.setattr(feature_flags, "get_value", lambda key, default=None: cfg.get(key, default))
    monkeypatch.setattr(feature_flags, "is_enabled", lambda key: False)   # fără normalizare/limite
    calls = []
    answers = {"sonnet": _resp(SONNET, "claude-sonnet-4-6"),
               D.HAIKU_MODEL: _resp(dict(SONNET), "claude-haiku-4-5", cost=0.003)}

    def fake_run_prompt(system, content, **kw):
        calls.append(kw["model_hint"])
        a = answers[kw["model_hint"]]
        return a() if callable(a) else a
    monkeypatch.setattr(D.iris_ai, "run_prompt", fake_run_prompt)
    img = tmp_path / "doc.png"
    buf = io.BytesIO()
    Image.new("RGB", (50, 40), (10, 20, 30)).save(buf, "PNG")
    img.write_bytes(buf.getvalue())
    return SimpleNamespace(cfg=cfg, calls=calls, answers=answers, img=str(img), pg=pg)


def _classify(env):
    return D._classify_attachment_vision("SYS", env.img, "image/png", "", "doc.png")


def _rows(env):
    with env.pg.connect() as c:
        return [dict(r._mapping) for r in c.exec_driver_sql("SELECT * FROM doc_model_shadow ORDER BY id")]


# ── Liste goale = comportamentul de azi ──────────────────────────────────────

def test_empty_lists_keep_sonnet_only(env):
    parsed, model, err = _classify(env)
    assert parsed == SONNET and model == "claude-sonnet-4-6" and err is None
    assert env.calls == ["sonnet"]
    assert _rows(env) == []


# ── „Haiku întâi" ────────────────────────────────────────────────────────────

def test_valid_haiku_skips_sonnet(env):
    env.cfg["documents.haiku_first_tasks"] = ["doc_classify_vision"]
    parsed, model, err = _classify(env)
    assert env.calls == [D.HAIKU_MODEL] and model == D.HAIKU_MODEL and parsed["type_id"] == 3


@pytest.mark.parametrize("haiku_answer", [
    _resp(None, "h", text="Nu pot clasifica documentul."),        # respins de apelant
    _resp({**SONNET, "confidence": 0.6}, "h"),                     # încredere sub prag
    _resp({k: v for k, v in SONNET.items() if k != "confidence"}, "h"),   # fără confidence
    {"ok": False, "text": "", "error": {"code": "HTTP_502", "message": "bad gateway"}},
])
def test_invalid_or_unsure_haiku_falls_back_to_sonnet(env, haiku_answer):
    env.cfg["documents.haiku_first_tasks"] = ["doc_classify_vision"]
    env.answers[D.HAIKU_MODEL] = haiku_answer
    parsed, model, err = _classify(env)
    assert env.calls == [D.HAIKU_MODEL, "sonnet"] and model == "claude-sonnet-4-6" and parsed == SONNET


def test_min_confidence_is_configurable(env):
    env.cfg["documents.haiku_first_tasks"] = ["doc_classify_vision"]
    env.cfg["documents.haiku_min_confidence"] = 0.5
    env.answers[D.HAIKU_MODEL] = _resp({**SONNET, "confidence": 0.6}, "h")
    assert _classify(env)[1] == D.HAIKU_MODEL


@pytest.fixture
def segment(env, monkeypatch):
    buf = io.BytesIO()
    Image.new("RGB", (60, 80), (200, 200, 200)).save(buf, "JPEG")
    monkeypatch.setattr(D, "_render_page_image", lambda path, i, zoom=2.0: (buf.getvalue(), "image/jpeg"))
    seg = {"type_id": 3, "confidence": 0.95, "starts_new": True, "reason": SECRET}
    env.answers["sonnet"] = _resp(seg, "claude-sonnet-4-6")
    env.answers[D.HAIKU_MODEL] = _resp(dict(seg), "claude-haiku-4-5")
    catalog = [{"id": 3, "name": "Talon", "category": "vehicul", "titles": [], "detect": ""}]
    return lambda: D._segment_pages("x.pdf", "application/pdf", catalog, 2)


def test_segment_haiku_first(env, segment):
    env.cfg["documents.haiku_first_tasks"] = ["doc_segment"]
    out = segment()
    assert env.calls == [D.HAIKU_MODEL, D.HAIKU_MODEL] and [p["type_id"] for p in out] == [3, 3]


def test_segment_unsure_haiku_falls_back(env, segment):
    env.cfg["documents.haiku_first_tasks"] = ["doc_segment"]
    env.answers[D.HAIKU_MODEL] = _resp({"type_id": 3, "confidence": 0.4, "starts_new": True}, "h")
    segment()
    assert env.calls == [D.HAIKU_MODEL, "sonnet", D.HAIKU_MODEL, "sonnet"]


def test_segment_empty_lists_unchanged(env, segment):
    out = segment()
    assert env.calls == ["sonnet", "sonnet"] and len(out) == 2


# ── Shadow ───────────────────────────────────────────────────────────────────

def _wait_threads():
    for t in threading.enumerate():
        if t is not threading.current_thread() and t.daemon:
            t.join(timeout=5)


def test_shadow_does_not_change_result_nor_block(env):
    env.cfg["documents.haiku_shadow_tasks"] = ["doc_classify_vision"]
    env.cfg["documents.haiku_shadow_sample"] = 1.0
    gate = threading.Event()

    def slow_haiku():
        gate.wait(5)                                   # Haiku „lent": procesarea nu îl așteaptă
        return _resp({**SONNET, "type_id": 4}, "claude-haiku-4-5", cost=0.003)
    env.answers[D.HAIKU_MODEL] = slow_haiku
    parsed, model, err = _classify(env)
    assert parsed == SONNET and model == "claude-sonnet-4-6"    # rezultatul de producție neatins
    assert _rows(env) == []                                     # încă nescris: nu a blocat
    gate.set()
    _wait_threads()
    [row] = _rows(env)
    assert (row["sonnet_type_id"], row["haiku_type_id"], row["match"], row["haiku_valid"]) == (3, 4, False, True)
    assert float(row["sonnet_cost_usd"]) == 0.01 and float(row["haiku_cost_usd"]) == 0.003
    assert len(row["input_hash"]) == 64


def test_shadow_table_holds_labels_only(env):
    env.cfg["documents.haiku_shadow_tasks"] = ["doc_classify_vision"]
    env.cfg["documents.haiku_shadow_sample"] = 1.0
    _classify(env)
    _wait_threads()
    [row] = _rows(env)
    assert set(row["sonnet_labels"]) == {"type_id", "confidence", "category", "is_document", "documents"}
    blob = json.dumps({k: v for k, v in row.items() if k != "created_at"}, default=str)
    assert "Popescu" not in blob and "CNP" not in blob and "bbox" not in blob
    assert row["match"] is True


def test_shadow_sample_zero_and_unlisted_do_nothing(env):
    env.cfg["documents.haiku_shadow_sample"] = 0.0
    env.cfg["documents.haiku_shadow_tasks"] = ["doc_classify_vision"]
    _classify(env)
    env.cfg["documents.haiku_shadow_sample"] = 1.0
    env.cfg["documents.haiku_shadow_tasks"] = ["doc_segment"]
    _classify(env)
    _wait_threads()
    assert env.calls == ["sonnet", "sonnet"] and _rows(env) == []


def test_shadow_segment_labels(env, segment):
    env.cfg["documents.haiku_shadow_tasks"] = ["doc_segment"]
    env.cfg["documents.haiku_shadow_sample"] = 1.0
    segment()
    _wait_threads()
    rows = _rows(env)
    assert rows and all(set(r["sonnet_labels"]) == {"type_id", "confidence", "starts_new"} for r in rows)
    assert all(r["match"] for r in rows)


# ── Cache (T3-L1): Haiku și Sonnet au chei diferite ──────────────────────────

def test_cache_key_separates_models():
    base = {"prompt": "S", "transcript": "C", "response_format": "text", "max_tokens": 1200,
            "attachments": [{"mime_type": "image/png", "data_base64": "AAAA"}]}
    k_sonnet = ai_cache.cache_key("doc_classify_vision", {**base, "model_hint": "sonnet"}, 0.0, 1)
    k_haiku = ai_cache.cache_key("doc_classify_vision", {**base, "model_hint": D.HAIKU_MODEL}, 0.0, 1)
    assert k_sonnet != k_haiku


# ── Măsurare și migrație ─────────────────────────────────────────────────────

def test_metrics_queries(env):
    with env.pg.begin() as c:
        for st, ht, m in [(3, 3, True), (3, 4, False), (5, 5, True), (5, 5, True)]:
            c.execute(sa.text("INSERT INTO doc_model_shadow (task_prefix, input_hash, sonnet_type_id, "
                              "haiku_type_id, haiku_valid, match, sonnet_cost_usd, haiku_cost_usd) "
                              "VALUES ('doc_segment', repeat('a',64), :s, :h, true, :m, 0.01, 0.003)"),
                      {"s": st, "h": ht, "m": m})
    stmts = [q for q in open(METRICS, encoding="utf-8").read().split(";") if "SELECT" in q]
    with env.pg.connect() as c:
        conc, diff, cost = [c.exec_driver_sql(q).fetchall() for q in stmts]
    assert conc[0]._mapping["match_pct"] == 75 and conc[0]._mapping["type_id_match_pct"] == 75
    assert [(r.sonnet_type_id, r.haiku_type_id, r.n) for r in diff] == [(3, 4, 1)]
    assert float(cost[0]._mapping["haiku_cost_total"]) == 0.012


def test_migration_down_then_up(pg):
    with pg.begin() as c:
        c.exec_driver_sql(open(DOWN, encoding="utf-8").read())
        assert c.exec_driver_sql("SELECT to_regclass('doc_model_shadow')").scalar() is None
        c.exec_driver_sql(open(UP, encoding="utf-8").read())
        c.exec_driver_sql(open(UP, encoding="utf-8").read())
        assert c.exec_driver_sql("SELECT value FROM settings WHERE key='documents.haiku_first_tasks'").scalar() == []
