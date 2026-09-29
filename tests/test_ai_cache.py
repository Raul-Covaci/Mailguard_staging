"""T3-L1 — cache de rezultat AI în `iris_ai.run_prompt()`.

Postgres REAL (pgserver, local, efemer) cu migrația aplicată din fișier, gateway-ul mock-uit la
nivel de `httpx.post`. Așa se verifică și SQL-ul cache-ului (jsonb, ON CONFLICT … WHERE, intervale)
și migrația up/down, nu doar logica Python.
"""
import base64
import json
import logging
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

try:
    import pgserver
except ImportError:          # NU skip tăcut: fără Postgres local, cache-ul nu e testat deloc
    pgserver = None

import sqlalchemy as sa  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.api.v1 import documents  # noqa: E402
from app.services import ai_cache, iris_ai, op_extractor  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UP = os.path.join(ROOT, "migrations", "20260929_ai_result_cache.sql")
DOWN = os.path.join(ROOT, "migrations", "down", "20260929_ai_result_cache.down.sql")
METRICS = os.path.join(ROOT, "scripts", "metrics", "ai_cache_savings.sql")

# Doar tabelele preexistente pe care le atinge migrația / cache-ul (forma din schema_baseline).
BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key varchar(100) PRIMARY KEY, value jsonb NOT NULL, description text,
    updated_by varchar(100), updated_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS ai_call_log (
    id bigserial PRIMARY KEY, task varchar(120), model varchar(80), tokens_in integer,
    tokens_out integer, cost_usd numeric(12,6), ok boolean, error_code varchar(40),
    created_at timestamptz NOT NULL DEFAULT now(), email_id bigint);
"""

PNG = b"\x89PNG\r\n\x1a\n" + b"pagina-1" * 50
PNG2 = b"\x89PNG\r\n\x1a\n" + b"pagina-2" * 50
USAGE = {"cost_usd": 0.0123, "input_tokens": 1500, "output_tokens": 40}
MODEL = "claude-sonnet-4-6"
PREFIXES = ["doc_segment", "doc_classify_vision", "doc_classify", "doc_extract", "doc_extract_vision",
            "doc_vision_ocr", "doc_rename", "doc_autogroup", "op_series"]


def _run_file(eng, path):
    with eng.begin() as c:
        c.exec_driver_sql(open(path, encoding="utf-8").read())


@pytest.fixture(scope="session")
def pg(tmp_path_factory):
    if pgserver is None:
        pytest.fail("pgserver lipsește — testele cache-ului AI (T3-L1) rulează pe un Postgres local "
                    "efemer. Instalează dependințele de test: "
                    "venv/bin/pip install -r requirements.txt -r requirements-dev.txt", pytrace=False)
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("pg")), cleanup_mode="stop")
    eng = sa.create_engine(srv.get_uri())
    with eng.begin() as c:
        c.exec_driver_sql(BASE_SCHEMA)
    _run_file(eng, UP)
    yield eng
    eng.dispose()


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def _gw_ok(text='{"type_id": 3, "starts_new": true}', parsed=None):
    return _Resp(200, {"ok": True, "raw_text": text, "parsed": parsed, "usage": dict(USAGE),
                       "model": MODEL})


@pytest.fixture
def env(pg, monkeypatch):
    with pg.begin() as c:
        c.exec_driver_sql("TRUNCATE ai_result_cache, ai_cache_hit_log, ai_call_log")
        c.exec_driver_sql("DELETE FROM settings WHERE key = 'ai_cache.last_purge_at'")
        c.exec_driver_sql("UPDATE settings SET value = 'true'::jsonb WHERE key = 'ai_cache.enabled'")
        c.exec_driver_sql("UPDATE settings SET value = '1'::jsonb WHERE key = 'ai_cache.epoch'")
        c.exec_driver_sql("UPDATE settings SET value = '10'::jsonb WHERE key = 'ai_cache.ttl_days'")
        c.execute(sa.text("UPDATE settings SET value = CAST(:v AS jsonb) WHERE key = 'ai_cache.prefixes'"),
                  {"v": json.dumps(PREFIXES)})
    Session = sessionmaker(bind=pg)
    monkeypatch.setattr(ai_cache, "_session", lambda: Session())
    ai_cache.reset_config_cache()
    monkeypatch.setenv("IRIS_AI_URL", "http://gateway.test/run-prompt")
    monkeypatch.setenv("IRIS_AI_KEY", "k")
    monkeypatch.delenv("AI_DISABLED", raising=False)
    gw = MagicMock(name="httpx.post", return_value=_gw_ok())
    monkeypatch.setattr(iris_ai.httpx, "post", gw)
    log_call = MagicMock(name="_log_call")
    monkeypatch.setattr(iris_ai, "_log_call", log_call)
    stmts = []

    def _capture(conn, cursor, statement, params, context, executemany):
        stmts.append(statement)
    event.listen(pg, "before_cursor_execute", _capture)

    def setting(key, value):
        with pg.begin() as c:
            c.execute(sa.text("UPDATE settings SET value = CAST(:v AS jsonb) WHERE key = :k"),
                      {"k": key, "v": json.dumps(value)})
        ai_cache.reset_config_cache()

    def q(sql, **p):
        with pg.connect() as c:
            return c.execute(sa.text(sql), p).fetchall()

    yield SimpleNamespace(gw=gw, log_call=log_call, stmts=stmts, setting=setting, q=q, pg=pg)
    event.remove(pg, "before_cursor_execute", _capture)
    ai_cache.reset_config_cache()


def _att(raw, mime="image/png"):
    return {"mime_type": mime, "data_base64": base64.b64encode(raw).decode("ascii")}


def call(task="cargo360:doc_segment:aaaa:_", system="SYS", content="Clasifica pagina 1",
         attachments=None, **kw):
    kw.setdefault("response_format", "text")
    kw.setdefault("model_hint", "sonnet")
    kw.setdefault("max_tokens", 400)
    kw.setdefault("temperature", 0.0)
    return iris_ai.run_prompt(system, content, task=task,
                              attachments=[_att(PNG)] if attachments is None else attachments, **kw)


def _cache_stmts(env):
    return [s for s in env.stmts if "ai_result_cache" in s or "ai_cache_hit_log" in s]


def _rows(env):
    return env.q("SELECT task_prefix, original_cost_usd, original_tokens_in, original_tokens_out, "
                 "hit_count, result FROM ai_result_cache")


# ── Flag OFF / prefix nelistat: comportament identic ──────────────────────────

def test_flag_off_is_identical_and_never_touches_cache(env):
    env.setting("ai_cache.enabled", False)
    r1, r2 = call(), call()
    assert env.gw.call_count == 2
    assert env.log_call.call_count == 2
    assert r1 == r2 == {"ok": True, "text": '{"type_id": 3, "starts_new": true}', "parsed": None,
                        "usage": USAGE, "model": MODEL, "error": None,
                        "task": "cargo360:doc_segment:aaaa:_", "truncated": False}
    assert _cache_stmts(env) == []


@pytest.mark.parametrize("task", ["cargo360:doc_detect:Talon:abc",      # nelistat
                                  "cargo360:doc_autogroup_holistic:x",  # prefix exact, nu startswith
                                  "email_category", None])
def test_unlisted_prefix_is_identical(env, task):
    call(task=task), call(task=task)
    assert env.gw.call_count == 2
    assert _cache_stmts(env) == []


# ── Miss → scriere; identic → hit ─────────────────────────────────────────────

def test_miss_writes_then_identical_call_hits_without_gateway(env):
    first = call(task="cargo360:doc_segment:aaaa:_")
    assert env.gw.call_count == 1 and env.log_call.call_count == 1
    [(prefix, cost, tin, tout, hits, stored)] = _rows(env)
    assert (prefix, float(cost), tin, tout, hits) == ("doc_segment", 0.0123, 1500, 40, 0)
    assert stored == first

    second = call(task="cargo360:doc_segment:bbbb:Talon")      # alt nume, aceeași intrare
    assert env.gw.call_count == 1, "gateway apelat la hit"
    assert env.log_call.call_count == 1, "rând în ai_call_log la hit"
    assert second == {**first, "task": "cargo360:doc_segment:bbbb:Talon"}
    assert second["model"] == MODEL
    [(task, prefix, saved)] = env.q("SELECT task, task_prefix, saved_cost_usd FROM ai_cache_hit_log")
    assert (task, prefix, float(saved)) == ("cargo360:doc_segment:bbbb:Talon", "doc_segment", 0.0123)
    assert _rows(env)[0][4] == 1


def test_key_uses_transcript_after_truncation(env):
    base = "x" * iris_ai.TRANSCRIPT_CAP
    call(content=base + "A"), call(content=base + "B")        # diferă doar după tăiere
    assert env.gw.call_count == 1


@pytest.mark.parametrize("change", [
    {"system": "SYS v2"},
    {"content": "Clasifica pagina 2"},
    {"attachments": [_att(PNG2)]},
    {"attachments": [_att(PNG, "image/jpeg")]},
    {"attachments": [_att(PNG), _att(PNG2)]},
    {"max_tokens": 401},
    {"model_hint": "claude-haiku-4-5-20251001"},
    {"response_format": "json"},
])
def test_any_input_change_is_a_miss(env, change):
    call()
    call(**change)
    assert env.gw.call_count == 2


def test_attachment_order_is_part_of_key(env):
    call(attachments=[_att(PNG), _att(PNG2)])
    call(attachments=[_att(PNG2), _att(PNG)])
    assert env.gw.call_count == 2


def test_epoch_bump_invalidates(env):
    call()
    env.setting("ai_cache.epoch", 2)
    call()
    assert env.gw.call_count == 2
    call()
    assert env.gw.call_count == 2          # epoca 2 are acum propriul rând


# ── Ce NU intră în cache ──────────────────────────────────────────────────────

@pytest.mark.parametrize("task", ["cargo360:doc_segment:a:_", "cargo360:doc_prompt_gen:3:abc"])
def test_temperature_above_zero_never_cached(env, task):
    env.setting("ai_cache.prefixes", ["doc_segment", "doc_prompt_gen"])
    call(task=task, temperature=0.2), call(task=task, temperature=0.2)
    assert env.gw.call_count == 2
    assert _cache_stmts(env) == []


def test_gateway_ok_false_not_stored(env):
    env.gw.return_value = _Resp(200, {"ok": False, "raw_text": "partial",
                                      "error": {"code": "PARSE", "message": "x"}})
    assert call()["ok"] is False
    assert _rows(env) == []


def test_http_error_not_stored(env, monkeypatch):
    monkeypatch.setattr(iris_ai, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))
    env.gw.return_value = _Resp(503, {"detail": "down"})
    assert call()["ok"] is False
    assert _rows(env) == []


def test_validator_false_not_stored_and_next_call_goes_to_gateway(env):
    call(cache_ok=lambda r: False)
    call(cache_ok=lambda r: False)
    assert _rows(env) == [] and env.gw.call_count == 2


def test_validator_exception_not_stored(env):
    def boom(r):
        raise ValueError("x")
    assert call(cache_ok=boom)["ok"] is True
    assert _rows(env) == []


@pytest.mark.parametrize("fmt,body,stored", [
    ("json", {"raw_text": "{}", "parsed": None}, False),
    ("json", {"raw_text": '{"a":1}', "parsed": {"a": 1}}, True),
    ("text", {"raw_text": "   ", "parsed": None}, False),
])
def test_default_validator(env, fmt, body, stored):
    env.gw.return_value = _Resp(200, {"ok": True, "usage": USAGE, "model": MODEL, **body})
    call(response_format=fmt)
    assert bool(_rows(env)) is stored


def test_doc_salvage_validator_rejects_what_the_caller_rejects(env):
    env.gw.return_value = _gw_ok(text="nu pot clasifica pagina")
    call(cache_ok=documents._cache_ok_salvage)
    assert _rows(env) == []
    env.gw.return_value = _gw_ok(text='Iata: {"type_id": 3} gata')
    call(cache_ok=documents._cache_ok_salvage)
    assert len(_rows(env)) == 1


def test_document_validators_unit():
    assert documents._cache_ok_salvage({"parsed": {"a": 1}, "text": ""})
    assert documents._cache_ok_salvage({"parsed": None, "text": 'x {"a": 1} y'})
    assert not documents._cache_ok_salvage({"parsed": None, "text": "fara json"})
    assert documents._cache_ok_parsed_dict({"parsed": {"a": 1}})
    assert not documents._cache_ok_parsed_dict({"parsed": [1]})


# ── Ocolire (reidentify) ──────────────────────────────────────────────────────

def test_bypass_skips_read_and_overwrites_entry(env):
    call()
    tok = ai_cache.ai_cache_bypass.set(True)
    env.gw.return_value = _gw_ok(text='{"type_id": 7}')
    try:
        assert call()["text"] == '{"type_id": 7}'          # nu citește: gateway-ul e apelat
    finally:
        ai_cache.ai_cache_bypass.reset(tok)
    assert env.gw.call_count == 2
    assert env.q("SELECT count(*) FROM ai_cache_hit_log")[0][0] == 0
    [row] = _rows(env)
    assert row[5]["text"] == '{"type_id": 7}' and row[4] == 0   # intrarea validă a fost înlocuită


def test_bypass_result_rejected_by_validator_keeps_old_entry(env):
    call()
    tok = ai_cache.ai_cache_bypass.set(True)
    env.gw.return_value = _gw_ok(text="fara json")
    try:
        call(cache_ok=documents._cache_ok_salvage)
    finally:
        ai_cache.ai_cache_bypass.reset(tok)
    assert [r[5]["text"] for r in _rows(env)] == ['{"type_id": 3, "starts_new": true}']


def test_reidentify_corrects_cache_for_later_reprocess(env, tmp_path, monkeypatch):
    """drain pune type_id 1 în cache → „Reidentifică" obține type_id 3 → un reprocess ulterior
    primește type_id 3 (din cache, fără gateway)."""
    img = tmp_path / "act.png"
    img.write_bytes(PNG)

    def classify():
        return documents._classify_attachment_vision("SYS", str(img), "image/png", "", "act.png")

    env.gw.return_value = _gw_ok(text='{"type_id": 1}')
    assert classify()[0] == {"type_id": 1}                 # drain: răspuns greșit, cache-uit

    env.gw.return_value = _gw_ok(text='{"type_id": 3}')
    monkeypatch.setattr(documents, "_reidentify_extraction",
                        lambda ex_id, type_id, db, admin: classify())
    assert documents.reidentify_extraction(1, None, db=None, admin={})[0] == {"type_id": 3}
    assert env.gw.call_count == 2

    env.gw.return_value = _gw_ok(text='{"type_id": 99}')   # n-ar trebui să mai fie cerut
    assert classify()[0] == {"type_id": 3}
    assert env.gw.call_count == 2


def test_reidentify_endpoint_sets_bypass_only_for_its_duration(monkeypatch):
    seen = []

    def fake(ex_id, type_id, db, admin):
        seen.append(ai_cache.ai_cache_bypass.get())
        if ex_id == 2:
            raise RuntimeError("x")
        return {"ok": True}
    monkeypatch.setattr(documents, "_reidentify_extraction", fake)
    assert documents.reidentify_extraction(1, None, db=None, admin={}) == {"ok": True}
    with pytest.raises(RuntimeError):
        documents.reidentify_extraction(2, None, db=None, admin={})
    assert seen == [True, True]
    assert ai_cache.ai_cache_bypass.get() is False


# ── TTL și curățare ───────────────────────────────────────────────────────────

def test_expired_entry_is_a_miss_and_gets_replaced(env):
    call()
    with env.pg.begin() as c:
        c.exec_driver_sql("UPDATE ai_result_cache SET expires_at = now() - interval '1 minute', "
                          "hit_count = 7")
    env.gw.return_value = _gw_ok(text='{"type_id": 9}')
    second = call()
    assert env.gw.call_count == 2 and second["text"] == '{"type_id": 9}'
    [(fresh, hits, text_)] = env.q("SELECT expires_at > now(), hit_count, result->>'text' "
                                   "FROM ai_result_cache")
    assert fresh and hits == 0 and text_ == '{"type_id": 9}'


def test_ttl_days_setting(env):
    env.setting("ai_cache.ttl_days", 3)
    call()
    [(days,)] = env.q("SELECT round(extract(epoch FROM expires_at - created_at) / 86400) "
                      "FROM ai_result_cache")
    assert int(days) == 3


def test_purge_deletes_expired_at_most_once_per_hour(env):
    call(), call(system="al doilea")
    with env.pg.begin() as c:
        c.exec_driver_sql("UPDATE ai_result_cache SET expires_at = now() - interval '1 second' "
                          "WHERE cache_key = (SELECT min(cache_key) FROM ai_result_cache)")
    assert ai_cache.purge_expired_if_due() == 1
    assert len(_rows(env)) == 1
    assert ai_cache.purge_expired_if_due() is None          # aceeași oră: poarta e închisă
    with env.pg.begin() as c:
        c.exec_driver_sql("UPDATE settings SET updated_at = now() - interval '61 minutes' "
                          "WHERE key = 'ai_cache.last_purge_at'")
    assert ai_cache.purge_expired_if_due() == 0


def test_purge_runs_when_disabled(env):
    """Rândurile rămase dintr-o perioadă cu cache activ conțin date din documente: expiră și cu
    flag-ul OFF."""
    call()
    with env.pg.begin() as c:
        c.exec_driver_sql("UPDATE ai_result_cache SET expires_at = now() - interval '1 second'")
    env.setting("ai_cache.enabled", False)
    assert ai_cache.purge_expired_if_due() == 1
    assert _rows(env) == []


def test_purge_without_table_does_nothing(env):
    with env.pg.begin() as c:
        c.exec_driver_sql("ALTER TABLE ai_result_cache RENAME TO ai_result_cache_x")
    try:
        assert ai_cache.purge_expired_if_due() is None
        assert env.q("SELECT count(*) FROM settings WHERE key = 'ai_cache.last_purge_at'")[0][0] == 0
    finally:
        with env.pg.begin() as c:
            c.exec_driver_sql("ALTER TABLE ai_result_cache_x RENAME TO ai_result_cache")


# ── Eșecul cache-ului = comportamentul de azi ─────────────────────────────────

def test_db_down_means_normal_call(env, monkeypatch, caplog):
    ai_cache.load_config()                                   # config deja în memorie
    def down():
        raise RuntimeError("db down")
    monkeypatch.setattr(ai_cache, "_session", down)
    caplog.set_level(logging.WARNING, logger="mailguard.ai_cache")
    r = call()
    assert r["ok"] is True and env.gw.call_count == 1
    assert any("citire eșuată" in m for m in caplog.messages)
    assert any("scriere eșuată" in m for m in caplog.messages)


def test_config_unreadable_means_disabled(env, monkeypatch, caplog):
    def down():
        raise RuntimeError("db down")
    monkeypatch.setattr(ai_cache, "_session", down)
    ai_cache.reset_config_cache()
    caplog.set_level(logging.WARNING, logger="mailguard.ai_cache")
    call(), call()
    assert env.gw.call_count == 2
    assert any("nu pot citi configul" in m for m in caplog.messages)
    assert ai_cache.purge_expired_if_due() is None


def test_hit_log_failure_turns_hit_into_miss_atomically(env):
    call()
    with env.pg.begin() as c:
        c.exec_driver_sql("ALTER TABLE ai_cache_hit_log RENAME TO ai_cache_hit_log_x")
    try:
        call()
        assert env.gw.call_count == 2                      # miss, nu hit nemăsurat
        assert _rows(env)[0][4] == 0                       # contorul a făcut rollback
    finally:
        with env.pg.begin() as c:
            c.exec_driver_sql("ALTER TABLE ai_cache_hit_log_x RENAME TO ai_cache_hit_log")


# ── op_series: „NONE" e răspuns valid ─────────────────────────────────────────

def test_op_series_none_is_cached_second_attempt_skips_gateway(env, tmp_path):
    img = tmp_path / "op.png"
    img.write_bytes(PNG)
    env.gw.return_value = _gw_ok(text="NONE|NONE")
    a = op_extractor._vision_extract_series(str(img), "image/png")
    b = op_extractor._vision_extract_series(str(img), "image/png")
    assert a == b == {"series": None, "currency": None}
    assert env.gw.call_count == 1
    assert env.q("SELECT task_prefix FROM ai_cache_hit_log")[0][0] == "op_series"


# ── Configurare, cheie, măsurare, migrație ────────────────────────────────────

def test_task_prefix():
    assert ai_cache.task_prefix("cargo360:doc_extract_vision:Talon:ab12") == "doc_extract_vision"
    assert ai_cache.task_prefix("op_series:ab") == "op_series"
    assert ai_cache.task_prefix("extract") == "extract"
    assert ai_cache.task_prefix("") is None and ai_cache.task_prefix(None) is None
    assert ai_cache.task_prefix("cargo360:") is None


@pytest.mark.parametrize("key,value,field,expected", [
    ("ai_cache.enabled", "true", "enabled", True),
    ("ai_cache.enabled", "nu", "enabled", False),
    ("ai_cache.enabled", 1, "enabled", False),
    ("ai_cache.prefixes", "doc_segment", "prefixes", frozenset()),
    ("ai_cache.epoch", "abc", "epoch", 1),
    ("ai_cache.epoch", True, "epoch", 1),
    ("ai_cache.ttl_days", -3, "ttl_days", 10),
])
def test_config_parsing(env, key, value, field, expected):
    env.setting(key, value)
    assert ai_cache.load_config()[field] == expected


def test_cache_key_is_deterministic_and_tolerates_bad_base64():
    p = {"prompt": "s", "transcript": "c", "response_format": "text", "max_tokens": 5,
         "attachments": [{"mime_type": "image/png", "data_base64": "@@nu-e-base64@@"}]}
    assert ai_cache.cache_key("x", p, 0.0, 1) == ai_cache.cache_key("x", dict(p), 0.0, 1)
    assert ai_cache.cache_key("x", p, 0.0, 1) != ai_cache.cache_key("y", p, 0.0, 1)


def test_metrics_query(env):
    with env.pg.begin() as c:
        c.exec_driver_sql(
            "INSERT INTO ai_cache_hit_log (task, task_prefix, cache_key, saved_cost_usd) VALUES "
            "('cargo360:doc_segment:a:_', 'doc_segment', repeat('a', 64), 0.02), "
            "('cargo360:doc_segment:b:_', 'doc_segment', repeat('b', 64), 0.03)")
        c.exec_driver_sql(
            "INSERT INTO ai_call_log (task, model, cost_usd, ok) VALUES "
            "('cargo360:doc_segment:c:_', 'm', 0.05, true), "
            "('cargo360:doc_segment:d:_', 'm', 0.00, false), "
            "('cargo360:email_category', 'm', 0.01, true)")
        rows = c.exec_driver_sql(open(METRICS, encoding="utf-8").read()).fetchall()
    assert len(rows) == 1                                   # email_category nu e în prefixe
    r = rows[0]._mapping
    assert (r["task_prefix"], r["hits"], r["real_calls"]) == ("doc_segment", 2, 2)
    assert float(r["saved_cost_usd"]) == 0.05 and float(r["real_cost_usd"]) == 0.05
    assert float(r["hits_vs_real_calls"]) == 0.5


def test_migration_down_then_up_is_clean_and_idempotent(pg):
    _run_file(pg, DOWN)
    with pg.connect() as c:
        assert c.exec_driver_sql("SELECT to_regclass('ai_result_cache')").scalar() is None
        assert c.execute(sa.text("SELECT count(*) FROM settings WHERE key LIKE :p"),
                         {"p": "ai_cache.%"}).scalar() == 0
    _run_file(pg, UP)
    _run_file(pg, UP)                                       # a doua oară: fără erori, fără dubluri
    with pg.connect() as c:
        assert c.execute(sa.text("SELECT count(*) FROM settings WHERE key LIKE :p"),
                         {"p": "ai_cache.%"}).scalar() == 4
        assert c.exec_driver_sql("SELECT value FROM settings WHERE key='ai_cache.enabled'").scalar() is False
        assert c.exec_driver_sql("SELECT value FROM settings WHERE key='ai_cache.prefixes'").scalar() == PREFIXES


# ── Legarea validatorului în apelanții REALI (T3-L1, review: mutațiile M4/M5) ─────
# Fiecare apelant de pe un prefix cache-uit, cu un răspuns pe care EL îl respinge azi, dar pe
# care validatorul implicit (`default_ok`) l-ar accepta: dacă apelantul nu-și trimite validatorul,
# răspunsul intră în cache și devine eșec permanent. Aici: nimic scris, a doua rulare cheamă
# gateway-ul din nou.

PROSE = "Nu pot clasifica documentul, imaginea e neclara."
REJECTED_JSON = [{"type_id": 3}]            # `parsed` nenul (trece de default_ok), dar nu e dict


def _gw_parsed(parsed, text="[]"):
    return _Resp(200, {"ok": True, "raw_text": text, "parsed": parsed, "usage": dict(USAGE),
                       "model": MODEL})


def _twice_rejected(env, fn):
    first, second = fn(), fn()
    assert env.gw.call_count == 2, "al doilea apel n-a ajuns la gateway: răspunsul respins e în cache"
    assert _rows(env) == []
    return first, second


@pytest.fixture
def img(tmp_path):
    p = tmp_path / "pagina.png"
    p.write_bytes(PNG)
    return str(p)


def test_caller_segment_pages_rejects_prose(env, img, monkeypatch):
    monkeypatch.setattr(documents, "_render_page_image", lambda path, i: (PNG, "image/png"))
    catalog = [{"id": 3, "name": "Talon", "category": "vehicul"}]
    env.gw.return_value = _gw_ok(text=PROSE)
    first, _ = _twice_rejected(env, lambda: documents._segment_pages(img, "application/pdf", catalog, 1))
    assert first[0]["reason"] == "clasificare pagina esuata"


def test_caller_extract_doc_rejects_non_dict(env):
    env.gw.return_value = _gw_parsed(REJECTED_JSON)
    first, _ = _twice_rejected(env, lambda: documents._extract_doc("SYS", "text document", 7, "Talon"))
    assert first[0] is None


def test_caller_classify_rejects_non_dict(env):
    env.gw.return_value = _gw_parsed(REJECTED_JSON)
    first, _ = _twice_rejected(env, lambda: documents._classify_attachment("SYS", "text document", "a.pdf"))
    assert first[0] is None


def test_caller_classify_vision_rejects_prose(env, img):
    env.gw.return_value = _gw_ok(text=PROSE)
    first, _ = _twice_rejected(
        env, lambda: documents._classify_attachment_vision("SYS", img, "image/png", "", "a.png"))
    assert first[0] is None


def test_caller_extract_vision_rejects_prose(env, img):
    env.gw.return_value = _gw_ok(text=PROSE)
    first, _ = _twice_rejected(
        env, lambda: documents._extract_doc_vision("SYS", (img, "image/png"), 7, "Talon"))
    assert first[0] is None


def test_caller_rename_rejects_empty_name(env, monkeypatch):
    monkeypatch.setattr(documents, "_vehicle_std_name", lambda *a, **k: None)
    db = MagicMock(name="db")
    env.gw.return_value = _gw_parsed({"nume_complet": "  "}, text='{"nume_complet": "  "}')
    _twice_rejected(env, lambda: documents._rename_doc(db, 11, 0, "Contract", "text", "c.pdf"))
    db.execute.assert_not_called()                          # apelantul n-a redenumit nimic


def test_caller_vision_ocr_rejects_blank(env, img):
    env.gw.return_value = _gw_ok(text="   ")
    first, _ = _twice_rejected(env, lambda: documents._vision_transcribe(img, "image/png"))
    assert first == ("", None)                              # apelantul: text gol = fara transcriere


def test_caller_op_series_rejects_unrecognized_format(env, img):
    """Refuz / proză: apelantul îl tratează ca „serie negăsită" și reîncearcă (MAX_EXTRACT_ATTEMPTS)
    — deci nu are voie în cache; un NONE|NONE real are (vezi testul de mai sus)."""
    env.gw.return_value = _gw_ok(text="Nu pot citi imaginea")
    assert op_extractor._vision_extract_series(img, "image/png") == {"series": None, "currency": None}
    assert _rows(env) == []
    env.gw.return_value = _gw_ok(text="PPCB|RON")
    assert op_extractor._vision_extract_series(img, "image/png") == {"series": "PPCB", "currency": "RON"}
    assert env.gw.call_count == 2
    assert len(_rows(env)) == 1


class _FakeDB:
    """document_extractions pentru un email cu 2 imagini incerte de același tip: declanșează toate
    cele trei treceri de autogrupare (holistic, pass 1, pass 2)."""

    def __init__(self, rows):
        self.rows, self.writes = rows, []

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if not sql.lstrip().upper().startswith("SELECT"):
            self.writes.append(sql)
        return SimpleNamespace(fetchall=lambda: [SimpleNamespace(_mapping=dict(r)) for r in self.rows])

    def commit(self):
        pass


def test_caller_autogroup_all_passes_reject_prose(env, tmp_path, monkeypatch):
    env.setting("ai_cache.prefixes", PREFIXES + ["doc_autogroup_holistic", "doc_autogroup_p2"])
    monkeypatch.setattr(documents, "_host_path", lambda p: p)
    rows = []
    for i, raw in enumerate((PNG, PNG2), 1):
        f = tmp_path / ("IMG_%d.png" % i)
        f.write_bytes(raw)
        rows.append({"ex_id": 100 + i, "attachment_id": 200 + i, "document_type_id": 5,
                     "category": "vehicul", "detected_type": "Talon", "confidence": 0.5,
                     "raw_text": "text", "att_name": f.name, "content_type": "image/png",
                     "storage_path": str(f)})
    db = _FakeDB(rows)
    env.gw.return_value = _gw_ok(text=PROSE)
    assert documents._autogroup_email_images(db, 1) == 0
    assert env.gw.call_count == 3                           # holistic + pass 1 + pass 2
    assert documents._autogroup_email_images(db, 1) == 0
    assert env.gw.call_count == 6, "o trecere a servit din cache un răspuns respins"
    assert _rows(env) == [] and db.writes == []
    tasks = {c.kwargs["json"]["task"].split(":")[1] for c in env.gw.call_args_list}
    assert tasks == {"doc_autogroup_holistic", "doc_autogroup", "doc_autogroup_p2"}


def test_rename_validator_unit():
    assert documents._cache_ok_rename({"parsed": {"nume_complet": "RO_B123ABC_VP.pdf"}})
    assert not documents._cache_ok_rename({"parsed": {"nume_complet": " "}})
    assert not documents._cache_ok_rename({"parsed": {"nume_complet": None}})
    assert not documents._cache_ok_rename({"parsed": ["x"]})


@pytest.mark.parametrize("answer,series,currency,recognized", [
    ("PPCB|RON", "PPCB", "RON", True),
    ("NONE|NONE", None, None, True),
    ("NONE|MDL", None, "MDL", True),
    ("ppcb", "PPCB", None, True),
    ("RON|RON", None, "RON", False),                        # seria = moneda: apelantul o respinge
    ("PPCB|LEI ROMANESTI", "PPCB", None, False),           # moneda nerecunoscută
    ("Nu pot citi imaginea", None, None, False),
    ("", None, None, False),
])
def test_op_series_parse_matches_caller_rule(answer, series, currency, recognized):
    assert op_extractor._parse_series_answer(answer) == {
        "series": series, "currency": currency, "recognized": recognized}


# ── Ordinea cheilor: `json`, nu `jsonb` ───────────────────────────────────────

def test_hit_keeps_key_order_for_duplicate_normalized_keys(env):
    """Modelul întoarce și 'Vin (E.)', și 'Vin'; `_normalize_keys` le mapează pe aceeași cheie
    canonică și câștigă ULTIMA. Cu `jsonb` (chei sortate) hit-ul ar alege cealaltă valoare."""
    fields = [{"name": "Vin (E.)"}]
    env.gw.return_value = _gw_parsed({"Vin (E.)": "WVWZZZ1", "Vin": "WVWZZZ2"})
    miss = documents._extract_doc("SYS", "text document", 7, "Talon", fields=fields)
    hit = documents._extract_doc("SYS", "text document", 7, "Talon", fields=fields)
    assert env.gw.call_count == 1
    assert miss[0] == hit[0] == {"Vin (E.)": "WVWZZZ2"}
    assert list(_rows(env)[0][5]["parsed"]) == ["Vin (E.)", "Vin"]


# ── purge_documents_before.py: și cache-ul AI ─────────────────────────────────

def _load_purge_script(monkeypatch):
    import importlib.util
    real_isfile = os.path.isfile
    # scriptul încarcă `.env` la import; în teste nu vrem variabilele reale în mediu
    monkeypatch.setattr(os.path, "isfile", lambda p: False if str(p).endswith(".env") else real_isfile(p))
    spec = importlib.util.spec_from_file_location(
        "purge_documents_before", os.path.join(ROOT, "scripts", "purge_documents_before.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(os.path, "isfile", real_isfile)
    return mod


def test_purge_documents_script_clears_ai_cache_before_cutoff(env, monkeypatch):
    mod = _load_purge_script(monkeypatch)
    call(), call(system="al doilea")
    with env.pg.begin() as c:
        c.exec_driver_sql("UPDATE ai_result_cache SET created_at = '2026-08-01' "
                          "WHERE cache_key = (SELECT min(cache_key) FROM ai_result_cache)")
    Session = sessionmaker(bind=env.pg)
    db = Session()
    try:
        assert mod.purge_ai_cache(db, "2026-08-24", apply=False) == 1
        assert len(_rows(env)) == 2                         # dry-run: nimic șters
        assert mod.purge_ai_cache(db, "2026-08-24", apply=True) == 1
        db.commit()
    finally:
        db.close()
    assert len(_rows(env)) == 1


def test_purge_documents_script_without_cache_table(env, monkeypatch):
    mod = _load_purge_script(monkeypatch)
    with env.pg.begin() as c:
        c.exec_driver_sql("ALTER TABLE ai_result_cache RENAME TO ai_result_cache_x")
    db = sessionmaker(bind=env.pg)()
    try:
        assert mod.purge_ai_cache(db, "2026-08-24", apply=True) == 0
    finally:
        db.close()
        with env.pg.begin() as c:
            c.exec_driver_sql("ALTER TABLE ai_result_cache_x RENAME TO ai_result_cache")


def test_purge_documents_script_main_apply_clears_ai_cache(env, monkeypatch, capsys):
    """`main()` cu --apply: pasul de cache chiar e legat în flux, nu doar funcția."""
    import sys
    mod = _load_purge_script(monkeypatch)
    with env.pg.begin() as c:                                # doar coloanele atinse de script
        c.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS emails (id bigint PRIMARY KEY, received_at timestamptz);"
            "CREATE TABLE IF NOT EXISTS document_extractions (id bigint PRIMARY KEY, email_id bigint, "
            "  grouped_into bigint);")
    call()
    with env.pg.begin() as c:
        c.exec_driver_sql("UPDATE ai_result_cache SET created_at = '2026-08-01'")
    monkeypatch.setattr(mod, "SessionLocal", sessionmaker(bind=env.pg))
    monkeypatch.setattr(sys, "argv", ["purge", "--before", "2026-08-24", "--apply", "--no-files"])
    assert mod.main() == 0
    assert _rows(env) == []
    assert "sterse: 1 rezultate AI din cache" in capsys.readouterr().out
