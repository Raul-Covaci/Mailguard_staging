import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _production_by_default(monkeypatch):
    """Testele descriu comportamentul de PRODUCȚIE, dacă nu cer altfel. Local `.env` are
    APP_ENV=staging, iar din T3-G2 `iris_ai.run_prompt` nu mai apelează gateway-ul în afara
    producției. Testele care verifică alt mediu își setează singure MAILGUARD_ENV (suprascrie asta)."""
    monkeypatch.setenv("MAILGUARD_ENV", "production")
    from app.services import feature_flags
    feature_flags.reset_cache()
    yield
    feature_flags.reset_cache()
