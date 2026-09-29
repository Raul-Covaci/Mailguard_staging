"""Blocajul nu are voie să lase sesiunea în „current transaction is aborted".

Pe Postgres, după o eroare într-o tranzacție, ORICE comandă următoare eșuează până la ROLLBACK.
Nu avem Postgres în teste, așa că folosim o sesiune SQLAlchemy REALĂ peste SQLite în memorie și
emulăm exact regula asta la nivel de engine: o eroare marchează conexiunea „abortată", orice
execuție ulterioară ridică eroarea Postgres, iar doar ROLLBACK (sau COMMIT, pe care Postgres îl
transformă în rollback) o curăță. Testul de control dovedește că emularea chiar prinde lipsa
rollback-ului — altfel testele de mai jos ar trece degeaba.
"""

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.services import env_guard
from app.services import productivity_notifier as pn


PG_ABORTED = ("current transaction is aborted, commands ignored until end of "
              "transaction block")


def _pg_like_engine(fail_on: str):
    """Engine SQLite cu semantica Postgres de tranzacție abortată.

    `fail_on`: 'insert' — INSERT-ul în audit_log eșuează (trigger);
               'select' — și SELECT-ul de deduplicare eșuează (tabela lipsește).
    """
    eng = create_engine("sqlite://", poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    state = {"aborted": False}

    @event.listens_for(eng, "connect")
    def _fns(dbapi_conn, _rec):
        # Funcțiile Postgres folosite de gardă, ca SQL-ul să ajungă până la punctul de eșec.
        dbapi_conn.create_function("now", 0, lambda: "2026-10-01 07:00:00")
        dbapi_conn.create_function("date_trunc", 2, lambda unit, ts: str(ts)[:10] + " 00:00:00")

    @event.listens_for(eng, "before_cursor_execute")
    def _refuse_when_aborted(conn, cursor, statement, params, context, executemany):
        if state["aborted"]:
            raise RuntimeError(PG_ABORTED)

    @event.listens_for(eng, "handle_error")
    def _abort(ctx):
        state["aborted"] = True

    @event.listens_for(eng, "rollback")
    def _clear_rb(conn):
        state["aborted"] = False

    @event.listens_for(eng, "commit")
    def _clear_commit(conn):
        state["aborted"] = False   # Postgres: COMMIT pe tranzacție abortată = ROLLBACK

    with eng.begin() as c:
        c.execute(text("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)"))
        if fail_on == "insert":
            c.execute(text("CREATE TABLE audit_log (action TEXT, actor TEXT, details TEXT, "
                           "created_at TEXT)"))
            c.execute(text("CREATE TRIGGER audit_fail BEFORE INSERT ON audit_log "
                           "BEGIN SELECT RAISE(ABORT, 'simulated audit_log failure'); END"))
    return eng, state


@pytest.fixture
def staging(monkeypatch):
    monkeypatch.setenv("MAILGUARD_ENV", "staging")


def test_control_emulation_catches_missing_rollback():
    """Fără rollback, sesiunea chiar rămâne blocată — emularea funcționează."""
    eng, state = _pg_like_engine("insert")
    s = Session(eng)
    with pytest.raises(Exception):
        s.execute(text("INSERT INTO audit_log(action) VALUES ('x')"))
    with pytest.raises(Exception, match="current transaction is aborted"):
        s.execute(text("SELECT 1"))
    s.rollback()
    assert s.execute(text("SELECT 1")).scalar() == 1


@pytest.mark.parametrize("fail_on", ["insert", "select"])
def test_audit_failure_leaves_session_usable(staging, fail_on):
    eng, state = _pg_like_engine(fail_on)
    s = Session(eng)
    s.execute(text("SELECT 1"))            # tranzacție deja deschisă, ca după porțile tick-ului

    reason = env_guard.block_reason(env_guard.CHANNEL_PRODUCTIVITY_REPORT, s, actor="cron")

    assert reason and reason.startswith("blocat: mediu non-producție")
    assert state["aborted"] is False, "sesiunea a rămas în tranzacție abortată"
    # Următorii pași pot scrie și citi normal pe aceeași sesiune.
    assert s.execute(text("SELECT 1")).scalar() == 1
    s.execute(text("INSERT INTO settings(key, value) VALUES ('k', '1')"))
    s.commit()
    assert s.execute(text("SELECT value FROM settings WHERE key='k'")).scalar() == "1"


def test_send_monthly_reports_blocked_with_failing_audit_keeps_session_clean(staging):
    """Calea reală a notificatorului (garda e prima instrucțiune), cu audit_log care pică."""
    eng, state = _pg_like_engine("insert")
    s = Session(eng)
    res = pn.send_monthly_reports(s)
    assert res["blocked"] is True and res["sent"] == 0
    assert state["aborted"] is False
    assert s.execute(text("SELECT 1")).scalar() == 1
    s.close()


def test_allow_list_read_failure_leaves_session_usable(staging):
    """Eșec deja la citirea `outbound.allow_non_production` (tabela settings lipsește)."""
    eng = create_engine("sqlite://", poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    aborted = {"v": False}
    event.listen(eng, "handle_error", lambda ctx: aborted.__setitem__("v", True))
    event.listen(eng, "rollback", lambda conn: aborted.__setitem__("v", False))

    def refuse(conn, cursor, statement, params, context, executemany):
        if aborted["v"]:
            raise RuntimeError(PG_ABORTED)
    event.listen(eng, "before_cursor_execute", refuse)

    s = Session(eng)
    assert env_guard.outbound_allowed("productivity_report", s) is False
    assert aborted["v"] is False
    assert s.execute(text("SELECT 1")).scalar() == 1
