"""Gardă de mediu pentru efectele externe (mailuri, apeluri spre terți).

Staging-ul rulează pe o clonă cu date reale: aceiași angajați, aceiași clienți, același cont SMTP
no-reply. Orice cod care trimite ceva în afară fără să întrebe întâi „sunt pe producție?" trimite
de pe staging către oameni reali. Așa a plecat raportul lunar de productivitate de pe staging pe
2026-08-03 și ar fi plecat din nou pe 2026-10-01.

Regula: pe producție, totul trece. În orice alt mediu, un canal trece DOAR dacă e trecut explicit
în `settings['outbound.allow_non_production']` (listă JSON de nume de canal, implicit goală).
Necunoscut = blocat: mediu nedeterminat, cheie ilizibilă, valoare de alt tip.

Mediul se decide în `vathub_send_guard.is_production()` — aceeași sursă ca redirectul VATHUB,
refolosită, nu copiată: două implementări ar putea ajunge să răspundă diferit pe același server.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("mailguard.env_guard")

ALLOW_KEY = "outbound.allow_non_production"

# Canalele cunoscute. Numele e cheia din `outbound.allow_non_production`.
CHANNEL_PRODUCTIVITY_REPORT = "productivity_report"


def is_production() -> bool:
    """True DOAR dacă mediul e sigur producție. Vezi `vathub_send_guard.is_production`."""
    from app.services.vathub_send_guard import is_production as _is_production
    return _is_production()


def env_name() -> str:
    """Numele mediului, pentru loguri și audit. Nu decide nimic."""
    explicit = os.environ.get("MAILGUARD_ENV", "").strip().lower()
    if explicit:
        return explicit
    try:
        from app.config import get_settings
        return (get_settings().app_env or "").strip().lower() or "necunoscut"
    except Exception:
        return "necunoscut"


def _allowed_channels(db: Session) -> set:
    """Canalele permise în afara producției. Orice eroare de citire → mulțime vidă."""
    try:
        row = db.execute(text("SELECT value FROM settings WHERE key = :k"),
                         {"k": ALLOW_KEY}).fetchone()
    except Exception:
        logger.warning("env_guard: nu pot citi %s — nimic permis", ALLOW_KEY, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return set()
    if not row:
        return set()
    val = row[0]
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except Exception:
            return set()
    if not isinstance(val, list):
        logger.warning("env_guard: %s nu e listă — ignor valoarea", ALLOW_KEY)
        return set()
    return {str(c).strip().lower() for c in val if isinstance(c, str) and c.strip()}


def outbound_allowed(channel: str, db: Session) -> bool:
    """True pe producție; altfel True doar dacă `channel` e în `outbound.allow_non_production`."""
    if is_production():
        return True
    return (channel or "").strip().lower() in _allowed_channels(db)


def _audit_block(db: Session, channel: str, env: str, actor: str, context: dict) -> None:
    """Un rând `outbound_blocked` în audit_log, cel mult unul pe zi per canal.

    Calea automată trece prin gardă la fiecare tick de 5 minute cât durează ziua de trimitere;
    fără limita asta, staging-ul ar scrie ~170 de rânduri identice pe lună. Logul aplicației
    rămâne complet — auditul consemnează doar că s-a blocat.
    Auditul e secundar: o eroare aici nu are voie să lase sesiunea abortată.
    """
    try:
        seen = db.execute(text(
            "SELECT 1 FROM audit_log WHERE action = 'outbound_blocked' "
            "   AND details->>'channel' = :c AND actor = :a "
            "   AND created_at >= date_trunc('day', now()) LIMIT 1"),
            {"c": channel, "a": actor}).fetchone()
        if seen:
            return
        db.execute(text(
            "INSERT INTO audit_log(action, actor, details, created_at) "
            "VALUES('outbound_blocked', :a, CAST(:d AS jsonb), now())"),
            {"a": actor, "d": json.dumps({"channel": channel, "env": env,
                                          "reason": "non_production", **(context or {})})})
        db.commit()
    except Exception:
        logger.warning("env_guard: audit_log insert esuat pentru %s", channel, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass


def block_reason(channel: str, db: Session, actor: str = "cron",
                 context: Optional[dict] = None) -> Optional[str]:
    """None dacă `channel` are voie să trimită; altfel motivul, după log + audit.

    Se apelează ÎNAINTE de orice lucru costisitor sau cu urme (AI, PDF, rezervări), ca un canal
    blocat să nu lase nimic în urmă în afară de rândul de audit.
    """
    if outbound_allowed(channel, db):
        return None
    env = env_name()
    reason = (f"blocat: mediu non-producție ({env}). Canalul '{channel}' nu e în "
              f"settings['{ALLOW_KEY}'].")
    logger.warning("OUTBOUND BLOCAT canal=%s mediu=%s actor=%s motiv=non_production",
                   channel, env, actor)
    _audit_block(db, channel, env, actor, context or {})
    return reason
