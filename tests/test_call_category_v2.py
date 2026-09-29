"""T3-L3 — call_category: extragerea JSON-ului din raw_text, în spatele unui flag.

Fără BD și fără rețea: `iris_ai.run_prompt` e înlocuit, flag-ul e controlat direct. Cu flag-ul OFF,
promptul și parsarea trebuie să fie identice cu versiunea anterioară.
"""
import json
import logging
from types import SimpleNamespace

import pytest

from app.services import call_classifier as C
from app.services import call_scorer, json_salvage

GOOD = {"categorie": "sesizare", "stil": "tensionat", "motivare_scurta": "Clientul raporteaza o problema."}
GOOD_TXT = json.dumps(GOOD, ensure_ascii=False)
TRANSCRIPT = "AGENT: Buna ziua.\nCLIENT: Masina nu transmite de ieri."


def _parse_error(text_):
    """Forma răspunsului iris_ai când gateway-ul nu parsează JSON-ul (raw_text în `text`)."""
    return {"ok": False, "text": text_, "parsed": None, "usage": None, "model": "claude-haiku-4-5",
            "error": {"code": "JSON_PARSE_ERROR", "message": "invalid json"}, "task": "x"}


def _ok_parsed(d):
    return {"ok": True, "text": json.dumps(d), "parsed": d, "usage": None,
            "model": "claude-haiku-4-5", "error": None, "task": "x"}


@pytest.fixture
def ai(monkeypatch):
    state = SimpleNamespace(res=None, systems=[])

    def fake_run_prompt(system, content, **kw):
        state.systems.append(system)
        assert kw["max_tokens"] == 250 and kw["response_format"] == "json"   # neschimbate
        return state.res
    monkeypatch.setattr(C.iris_ai, "run_prompt", fake_run_prompt)
    monkeypatch.setattr(C.iris_ai, "is_configured", lambda: True)
    monkeypatch.setattr(C, "load_call_prompts", lambda: dict(C.DEFAULT_CALL_PROMPTS))
    return state


@pytest.fixture
def v2(monkeypatch):
    def _set(on):
        monkeypatch.setattr(C, "call_category_v2_enabled", lambda: on)
    return _set


# ── Flag ON: extragere validată ─────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "```json\n" + GOOD_TXT + "\n```",
    "```\n" + GOOD_TXT + "\n```",
    "Iata clasificarea:\n" + GOOD_TXT + "\nSper ca ajuta.",
    "Raspuns:\n```json\n" + GOOD_TXT + "\n```\nGata.",
])
def test_v2_salvages_fences_and_surrounding_text(ai, v2, raw):
    v2(True)
    ai.res = _parse_error(raw)
    out = C.classify_call(TRANSCRIPT)
    assert out == {"category": "sesizare", "tone": "tensionat", "reason": GOOD["motivare_scurta"],
                   "model": "claude-haiku-4-5", "salvaged": True}


def test_v2_reads_raw_text_from_error_too(ai, v2):
    v2(True)
    res = _parse_error("")
    res["error"]["raw_text"] = "```json\n" + GOOD_TXT + "\n```"
    ai.res = res
    assert C.classify_call(TRANSCRIPT)["category"] == "sesizare"


def test_v2_correct_json_is_unchanged(ai, v2):
    ai.res = _ok_parsed(GOOD)
    v2(False)
    old = C.classify_call(TRANSCRIPT)
    v2(True)
    new = C.classify_call(TRANSCRIPT)
    assert new == old and "salvaged" not in new


def test_v2_salvaged_unknown_keeps_fallback_policy(ai, v2):
    v2(True)
    ai.res = _parse_error("```json\n" + json.dumps({**GOOD, "categorie": "necunoscut"}) + "\n```")
    out = C.classify_call(TRANSCRIPT)
    assert out["category"] == "informatie" and out["unknown_fallback"] is True


@pytest.mark.parametrize("raw,why", [
    ("```json\n" + json.dumps({**GOOD, "categorie": "urgent"}) + "\n```", "bad_category"),
    ("```json\n" + json.dumps({"categorie": "sesizare"}) + "\n```", "missing_keys"),
    ("nu pot clasifica acest apel", "no_json"),
    ("", "no_json"),
])
def test_v2_invalid_stays_failure_and_logs_only_structure(ai, v2, caplog, raw, why):
    v2(True)
    ai.res = _parse_error(raw)
    caplog.set_level(logging.WARNING, logger="mailguard.call_classifier")
    assert C.classify_call(TRANSCRIPT) is None
    [msg] = [r.getMessage() for r in caplog.records]
    assert f"motiv={why}" in msg and f"len={len(raw)}" in msg
    assert f"fences={'```' in raw}" in msg
    for leaked in ("urgent", "sesizare", "nu pot", "Masina", "motivare"):
        assert leaked not in msg, "conținut în log"


def test_v2_transport_error_is_failure(ai, v2):
    v2(True)
    ai.res = {"ok": False, "text": "", "parsed": None, "usage": None, "task": "x",
              "error": {"code": "TRANSPORT", "message": "timeout"}}
    assert C.classify_call(TRANSCRIPT) is None


def test_v2_prompt_has_json_only_instruction(ai, v2):
    v2(True)
    ai.res = _ok_parsed(GOOD)
    C.classify_call(TRANSCRIPT)
    assert "Răspunde DOAR cu obiectul JSON, fără alt text și fără ```." in ai.systems[-1]


# ── Flag OFF: comportamentul de azi ─────────────────────────────────────────

def test_off_prompt_is_the_legacy_one(ai, v2):
    v2(False)
    ai.res = _ok_parsed(GOOD)
    C.classify_call(TRANSCRIPT)
    p = dict(C.DEFAULT_CALL_PROMPTS)
    legacy = (C._BASE_HEAD
              + "   - informatie: " + p["informatie"] + "\n"
              + "   - sesizare: " + p["sesizare"] + "\n"
              + "   - reclamatie: " + p["reclamatie"] + "\n"
              + C._TONE_INSTRUCTIONS + C._BASE_TAIL)
    assert ai.systems[-1] == legacy and "DOAR cu obiectul JSON" not in legacy


@pytest.mark.parametrize("res", [
    _parse_error("```json\n" + GOOD_TXT + "\n```"),
    _parse_error("Iata: " + GOOD_TXT),
    {"ok": True, "text": GOOD_TXT, "parsed": None, "usage": None, "model": "m", "error": None},
])
def test_off_does_not_salvage_and_does_not_log(ai, v2, caplog, res):
    v2(False)
    ai.res = res
    caplog.set_level(logging.WARNING, logger="mailguard.call_classifier")
    assert C.classify_call(TRANSCRIPT) is None
    assert caplog.records == []


def test_off_parsed_dict_with_unlisted_category_keeps_legacy_fallback(ai, v2):
    v2(False)
    ai.res = _ok_parsed({**GOOD, "categorie": "urgent", "stil": "x"})
    out = C.classify_call(TRANSCRIPT)
    assert out["category"] == "informatie" and out["tone"] is None and out["unknown_fallback"]


# ── Citirea flag-ului ───────────────────────────────────────────────────────

class _Sess:
    def __init__(self, value=None, missing=False, boom=False):
        self.value, self.missing, self.boom = value, missing, boom

    def execute(self, *a, **k):
        if self.boom:
            raise RuntimeError("db down")
        return SimpleNamespace(fetchone=lambda: None if self.missing else (self.value,))

    def close(self):
        pass


@pytest.mark.parametrize("sess,expected", [
    (_Sess(missing=True), False),
    (_Sess(True), True),
    (_Sess({"enabled": True}), True),
    (_Sess(False), False),
    (_Sess({"enabled": "true"}), False),
    (_Sess("true"), False),
    (_Sess(boom=True), False),
])
def test_flag_reader(monkeypatch, sess, expected):
    monkeypatch.setattr(C, "SessionLocal", lambda: sess)
    assert C.call_category_v2_enabled() is expected


# ── Utilitarul comun: mutat, nu schimbat ────────────────────────────────────

def test_call_scorer_uses_the_shared_salvage():
    assert call_scorer._salvage_json is json_salvage.salvage_json


@pytest.mark.parametrize("raw,expected", [
    ('{"evidence": "a spus "Buna ziua" la inceput", "result": true}',
     {"evidence": 'a spus "Buna ziua" la inceput', "result": True}),
    ('{"a": "trunc', {"a": "trunc"}),
    ('{"evidence": «Buna ziua» si apoi, "result": true}', {"evidence": "«Buna ziua» si apoi", "result": True}),
    ("[1,2]", None),
    ("", None),
])
def test_salvage_behaviour_preserved(raw, expected):
    assert json_salvage.salvage_json(raw) == expected
