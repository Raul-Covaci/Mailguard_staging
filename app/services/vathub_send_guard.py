"""Gardă de siguranță — redirect VATHUB din căsuțele personale.

Redirectul ia mailuri din căsuța personală a unui angajat și le retrimite prin
SMTP. Riscul e simetric cu cel de la campaniile de feedback: o listă greșită sau
un config greșit poate trimite corespondență reală către o adresă nedorită.

Regula, decisă de Raul Covaci (2026-08-20): destinația redirectului e o singură
adresă internă, aprobată explicit. Orice altă țintă e blocată — și pe staging, și
pe producție — indiferent ce scrie în `settings.vathub.redirect`.

⛔ A DOUA regulă (Raul Covaci, 2026-09-12, REVOCĂ aprobarea de trimitere reală pe
staging din 2026-08-20): redirectul trimite DOAR din producție. Staging și producție
citesc aceeași căsuță, deci ambele ar potrivi aceleași mailuri și VATHUB ar primi
câte două copii din fiecare. Pe staging potrivirea și jurnalul rămân active (se poate
verifica lista înainte de promovare), dar trimiterea e oprită în COD, nu din config.

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


# Cele două adrese de test. Pe staging se poate trimite DOAR către ele, și numai cu
# MAILGUARD_VATHUB_ALLOW_STAGING pornit explicit — niciodată către căsuța reală.
STAGING_TEST_TARGETS = {
    "raul.covaci@cargotrack.ro",
    "raul.covaci@trakosoft.ro",
}


def is_production() -> bool:
    """True DOAR dacă știm sigur că rulăm pe producție.

    Două frâne independente, ambele trebuie să spună „producție":
      - `MAILGUARD_ENV`, dacă e setat explicit — are ultimul cuvânt, deci un
        `MAILGUARD_ENV=staging` oprește redirectul chiar și pe o mașină de producție;
      - altfel `app_env` (`APP_ENV` din .env), care pe staging e explicit "staging".

    Dacă nu putem citi configul, răspunsul e NU: necunoscut înseamnă „nu trimite".
    """
    explicit = os.environ.get("MAILGUARD_ENV", "").strip().lower()
    if explicit:
        return explicit == "production"
    try:
        from app.config import get_settings
        return (get_settings().app_env or "").strip().lower() == "production"
    except Exception:
        logger.warning("Redirect VATHUB: nu pot determina mediul — presupun staging")
        return False


def is_staging() -> bool:
    """True dacă NU rulăm pe producție (fail-safe: presupune staging)."""
    return not is_production()


def staging_test_allowed() -> bool:
    """Supapa de testare pe staging. Implicit OPRITĂ, se pornește din .env."""
    return os.environ.get("MAILGUARD_VATHUB_ALLOW_STAGING", "off").strip().lower() \
        in ("on", "1", "true", "yes")


def forward_allowed(to_address: str):
    """(permis, motiv). Varianta fără excepție — pentru gate-uri și pentru UI."""
    normalized = (to_address or "").strip().lower()
    if normalized not in ALLOWED_FORWARD_TARGETS:
        return False, (f"Redirect blocat: '{to_address}' nu e o destinație aprobată "
                       f"({', '.join(sorted(ALLOWED_FORWARD_TARGETS))}).")
    if is_production():
        return True, None
    if normalized in STAGING_TEST_TARGETS and staging_test_allowed():
        return True, None
    return False, ("Redirect blocat: mediul nu e PRODUCȚIE. Pe staging mailurile se "
                   "potrivesc și se văd în jurnal, dar nu pleacă — altfel ar ajunge "
                   "duplicate în căsuța VATHUB, trimise și de producție. Pentru un "
                   "test către o adresă proprie: MAILGUARD_VATHUB_ALLOW_STAGING=on.")


def assert_forward_target_allowed(to_address: str) -> None:
    """Oprește redirectul spre orice adresă neaprobată SAU din afara producției.

    ⛔ Se apelează per mail, chiar înainte de conectarea SMTP — nu la salvarea
    configului, care se poate schimba din UI între două rulări.
    """
    ok, reason = forward_allowed(to_address)
    if not ok:
        logger.warning("Redirect VATHUB BLOCAT — %s", reason)
        raise VathubForwardBlocked(reason)
