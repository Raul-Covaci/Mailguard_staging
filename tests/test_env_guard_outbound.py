"""Garda de mediu pe raportul lunar de productivitate.

Fără BD și fără rețea: sesiunea e un fals care răspunde după textul SQL, iar SMTP-ul, rezumatul AI
și rapoartele sunt înlocuite. Ce se verifică: în afara producției NIMIC nu pleacă și nimic nu se
pregătește (AI, rezervare, `last_monthly_sent`); pe producție fluxul e cel de azi; pe staging cu
canalul permis explicit, trimite.
"""
import datetime as _dt
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.api.v1 import productivity as prod_api
from app.services import env_guard
from app.services import productivity_notifier as pn


class _Result:
    def __init__(self, one=None, many=None):
        self._one, self._many = one, many or []

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._many


class FakeDB:
    """Sesiune falsă: răspunde după fragmente de SQL și ține minte ce s-a executat."""

    def __init__(self, allow=None, audit_seen=False, fail_allow_read=False, fail_audit=False):
        self.allow = allow            # valoarea din settings['outbound.allow_non_production']
        self.audit_seen = audit_seen
        self.fail_allow_read = fail_allow_read
        self.fail_audit = fail_audit
        self.sql = []
        self.rollbacks = 0

    def execute(self, stmt, params=None):
        q = str(stmt)
        self.sql.append((q, params or {}))
        if "FROM settings WHERE key = :k" in q:
            if self.fail_allow_read:
                raise RuntimeError("db down")
            return _Result(None if self.allow is None else (self.allow,))
        if "SELECT 1 FROM audit_log" in q:
            if self.fail_audit:
                raise RuntimeError("audit down")
            return _Result((1,) if self.audit_seen else None)
        if "CURRENT_TIMESTAMP AT TIME ZONE" in q:
            return _Result((_dt.date(2026, 10, 1), 10))      # joi, 10:00 Europe/Bucharest
        if "productivity.ro_holidays" in q:
            return _Result(None)
        if "last_monthly_sent_at" in q and q.lstrip().upper().startswith("SELECT"):
            return _Result(None)
        if "productivity.last_monthly_sent'" in q and q.lstrip().upper().startswith("SELECT"):
            return _Result(("2026-08",))
        if "FROM productivity_notifications" in q:
            return _Result(many=[(1, "dest@example.test", "operational")])
        if "INSERT INTO productivity_notification_log" in q:
            return _Result((1,))
        return _Result()

    def commit(self):
        pass

    def rollback(self):
        self.rollbacks += 1

    def ran(self, fragment):
        return [p for q, p in self.sql if fragment in q]


class _FakeDate(_dt.date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 1)


@pytest.fixture
def env(monkeypatch):
    """Setează mediul prin MAILGUARD_ENV (are ultimul cuvânt în `is_production`)."""
    def _set(value):
        if value is None:
            monkeypatch.delenv("MAILGUARD_ENV", raising=False)
        else:
            monkeypatch.setenv("MAILGUARD_ENV", value)
    return _set


@pytest.fixture
def world(monkeypatch):
    """Înlocuiește tot ce iese din proces: SMTP, AI, PDF, rapoarte, config no-reply."""
    monkeypatch.setattr(pn._dt, "date", _FakeDate)
    smtp = MagicMock(name="SMTP")
    monkeypatch.setattr("smtplib.SMTP", smtp)
    ai = MagicMock(name="ai_summary", return_value="intro")
    monkeypatch.setattr(pn, "_generate_ai_summary", ai)
    pdf = MagicMock(name="pdf", return_value=(b"%PDF", "application/pdf", "r.pdf"))
    monkeypatch.setattr(pn, "_generate_pdf", pdf)
    monkeypatch.setattr(pn, "_expand_departments", lambda db, g: ["suport_1"])
    monkeypatch.setattr(pn, "_build_email_html", lambda *a, **k: "<p>raport</p>")
    monkeypatch.setattr("app.services.productivity.department_report",
                        lambda *a, **k: {"department": "suport_1"})
    monkeypatch.setattr("app.services.productivity.forecast_report",
                        lambda *a, **k: {"department": "suport_1"})
    monkeypatch.setattr("app.services.noreply_sender.get_noreply_config", lambda db: {
        "smtp_host": "smtp.test", "smtp_port": 587, "smtp_user": "u", "smtp_pass_enc": "x",
        "from_address": "noreply@example.test", "use_tls": True})
    monkeypatch.setattr("app.services.credential_crypto.decrypt_credentials",
                        lambda enc: {"password": "p"})
    return SimpleNamespace(smtp=smtp, ai=ai, pdf=pdf)


def _nothing_prepared(db, w):
    assert not w.smtp.called, "SMTP apelat în afara producției"
    assert not w.ai.called, "rezumat AI generat în afara producției"
    assert not w.pdf.called
    assert not db.ran("INSERT INTO productivity_notification_log"), "rezervare scrisă"
    assert not db.ran("INSERT INTO settings"), "last_monthly_sent scris"
    assert not db.ran("FROM productivity_notifications"), "destinatarii citiți"


# ── Non-producție: nimic nu pleacă ──────────────────────────────────────────

@pytest.mark.parametrize("mode", ["staging", "local", "dev"])
def test_auto_path_blocked_outside_production(env, world, mode):
    env(mode)
    db = FakeDB()
    res = pn.send_monthly_reports_if_due(db)
    assert res["blocked"] is True and res["sent"] == 0
    assert res["reason"].startswith("blocat: mediu non-producție")
    _nothing_prepared(db, world)


def test_send_now_blocked_on_staging_returns_explicit_answer(env, world):
    env("staging")
    db = FakeDB()
    res = prod_api.send_notifications_now(force=True, db=db, admin={"username": "t"})
    assert res["ok"] is False and res["blocked"] is True
    assert res["reason"].startswith("blocat: mediu non-producție")
    _nothing_prepared(db, world)
    assert not db.ran("DELETE FROM productivity_notification_log"), "force a șters rezervări"


def test_block_writes_audit_row(env, world):
    env("staging")
    db = FakeDB()
    pn.send_monthly_reports_if_due(db)
    rows = db.ran("INSERT INTO audit_log")
    assert len(rows) == 1
    assert rows[0]["a"] == "cron"
    details = json.loads(rows[0]["d"])
    assert details["channel"] == "productivity_report"
    assert details["env"] == "staging" and details["reason"] == "non_production"


def test_audit_written_once_per_day(env, world):
    env("staging")
    db = FakeDB(audit_seen=True)
    assert pn.send_monthly_reports_if_due(db)["blocked"] is True
    assert not db.ran("INSERT INTO audit_log")


def test_audit_failure_still_blocks(env, world):
    env("staging")
    db = FakeDB(fail_audit=True)
    res = pn.send_monthly_reports_if_due(db)
    assert res["blocked"] is True
    assert db.rollbacks >= 1
    _nothing_prepared(db, world)


def test_app_env_decides_when_mailguard_env_unset(env, world, monkeypatch):
    env(None)
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(app_env="staging"))
    db = FakeDB()
    assert pn.send_monthly_reports(db)["blocked"] is True
    assert env_guard.env_name() == "staging"
    _nothing_prepared(db, world)


def test_unreadable_config_means_not_production(env, monkeypatch):
    env(None)
    def boom():
        raise RuntimeError("no config")
    monkeypatch.setattr("app.config.get_settings", boom)
    assert env_guard.is_production() is False
    assert env_guard.env_name() == "necunoscut"


@pytest.mark.parametrize("allow", [
    None,                                  # cheie lipsă
    ["vathub"],                            # alt canal
    "productivity_report",                 # string, nu listă
    json.dumps({"productivity_report": 1}),  # obiect JSON
    "{not json",                           # text invalid
    [],                                    # listă goală (implicit)
])
def test_allow_list_other_values_stay_blocked(env, world, allow):
    env("staging")
    db = FakeDB(allow=allow)
    assert pn.send_monthly_reports(db)["blocked"] is True
    _nothing_prepared(db, world)


def test_allow_list_read_error_blocks(env, world):
    env("staging")
    db = FakeDB(fail_allow_read=True)
    assert pn.send_monthly_reports(db)["blocked"] is True
    _nothing_prepared(db, world)


# ── Producție: comportamentul de azi ────────────────────────────────────────

def test_production_auto_path_sends_as_today(env, world):
    env("production")
    db = FakeDB()
    res = pn.send_monthly_reports_if_due(db)
    assert res == {"sent": 1, "errors": 0, "skipped": 0, "month": "2026-09"}
    assert world.ai.called and world.pdf.called
    world.smtp.assert_called_once_with("smtp.test", 587, timeout=20)
    world.smtp.return_value.sendmail.assert_called_once()
    assert db.ran("INSERT INTO productivity_notification_log")
    assert db.ran("INSERT INTO settings"), "last_monthly_sent nescris pe producție"
    assert not db.ran("audit_log WHERE action = 'outbound_blocked'")
    assert not db.ran("FROM settings WHERE key = :k"), "allow-list citit pe producție"


def test_production_send_now_sends_as_today(env, world):
    env("production")
    db = FakeDB()
    res = prod_api.send_notifications_now(force=False, db=db, admin={"username": "t"})
    assert res["ok"] is True and res["sent"] == 1 and "blocked" not in res
    world.smtp.return_value.sendmail.assert_called_once()


def test_mailguard_env_staging_overrides_production_app_env(env, world, monkeypatch):
    env("staging")
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(app_env="production"))
    db = FakeDB()
    assert pn.send_monthly_reports(db)["blocked"] is True
    _nothing_prepared(db, world)


# ── Staging cu canalul permis explicit ──────────────────────────────────────

@pytest.mark.parametrize("allow", [["productivity_report"], [" Productivity_Report "],
                                   json.dumps(["productivity_report", "altceva"])])
def test_staging_with_channel_allowed_sends(env, world, allow):
    env("staging")
    db = FakeDB(allow=allow)
    res = pn.send_monthly_reports_if_due(db)
    assert res["sent"] == 1 and "blocked" not in res
    world.smtp.return_value.sendmail.assert_called_once()
    assert not db.ran("INSERT INTO audit_log(action, actor, details, created_at) "
                      "VALUES('outbound_blocked'")


def test_outbound_allowed_unit(env):
    env("staging")
    assert env_guard.outbound_allowed("productivity_report", FakeDB(allow=["productivity_report"]))
    assert not env_guard.outbound_allowed("productivity_report", FakeDB())
    assert not env_guard.outbound_allowed("", FakeDB(allow=["productivity_report"]))
    env("production")
    assert env_guard.outbound_allowed("orice", FakeDB())
