"""Gardă de siguranță — redirect VATHUB din căsuțele personale.

Redirectul ia mailuri din căsuța personală a unui angajat și le retrimite prin
SMTP. Riscul e simetric cu cel de la campaniile de feedback: o listă greșită sau
un config greșit poate trimite corespondență reală către o adresă nedorită.

Regula, decisă de Raul Covaci (2026-08-20): destinația redirectului e o singură
adresă internă, aprobată explicit. Orice altă țintă e blocată — și pe staging, și
pe producție — indiferent ce scrie în `settings.vathub.redirect`.

`assert_forward_target_allowed(to_address)` TREBUIE apelată chiar înainte de
conectarea la SMTP, nu la salvarea configului: configul se poate schimba din UI
între validare și trimitere.

Notă: garda din `feedback_send_guard` rămâne neatinsă — acolo whitelist-ul de
staging protejează clienții reali de mailuri de feedback, altă regulă de business.
"""
import logging
import os

logger = logging.getLogger("mailguard.vathub_send_guard")

# Singurele destinații permise pentru redirect. `vathub@cargotrack.ro` e căsuța
# generală citită de aplicația VATHUB; celelalte două există pentru testare.
ALLOWED_FORWARD_TARGETS = {
    "vathub@cargotrack.ro",
    "raul.covaci@cargotrack.ro",
    "raul.covaci@trakosoft.ro",
}


class VathubForwardBlocked(Exception):
    """Ridicată când redirectul ar trimite către o adresă neaprobată."""


def is_staging() -> bool:
    """True dacă nu rulăm explicit pe producție (fail-safe: presupune staging)."""
    return os.environ.get("MAILGUARD_ENV", "staging").strip().lower() != "production"


def assert_forward_target_allowed(to_address: str) -> None:
    """Oprește redirectul spre orice adresă în afara listei aprobate."""
    normalized = (to_address or "").strip().lower()
    if normalized not in ALLOWED_FORWARD_TARGETS:
        logger.warning("Redirect VATHUB BLOCAT — destinație neaprobată: %s", normalized)
        raise VathubForwardBlocked(
            f"Redirect blocat: '{to_address}' nu e o destinație aprobată "
            f"({', '.join(sorted(ALLOWED_FORWARD_TARGETS))})."
        )
