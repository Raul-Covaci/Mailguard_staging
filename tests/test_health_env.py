"""/api/v1/health expune mediul văzut de garda de trimitere (verificare după deploy fără SSH)."""
from unittest.mock import MagicMock

import pytest

from app.api.v1 import health as health_api


@pytest.fixture(autouse=True)
def no_redis(monkeypatch):
    monkeypatch.setattr("redis.Redis", MagicMock())


@pytest.mark.parametrize("mode", ["staging", "local"])
def test_health_reports_non_production(monkeypatch, mode):
    monkeypatch.setenv("MAILGUARD_ENV", mode)
    res = health_api.health(db=MagicMock())
    assert res["environment"] == mode
    assert res["is_production"] is False
    assert res["status"] == "healthy"


def test_health_reports_production(monkeypatch):
    monkeypatch.setenv("MAILGUARD_ENV", "production")
    res = health_api.health(db=MagicMock())
    assert res["environment"] == "production" and res["is_production"] is True


def test_health_keeps_existing_fields(monkeypatch):
    monkeypatch.setenv("MAILGUARD_ENV", "staging")
    res = health_api.health(db=MagicMock())
    assert {"status", "service", "version", "timestamp", "checks"} <= set(res)
    assert res["checks"] == {"database": "ok", "redis": "ok"}
