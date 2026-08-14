"""v10.19.0 — Access control: roluri interne (operator/admin/developer).

Independent de CTS/Cargo360 (seniority). Rolul se seteaza manual din
pagina Utilizatori -> Roluri acces, de catre admin sau developer.

Matrice:
  operator  -> Emailuri + Productivitate (DOAR sub-tab Rapoarte)
  admin     -> tot, mai putin zona de Setari (settings/prompturi-ai/surse-date)
  developer -> tot
"""
from typing import Optional

from fastapi import Depends, HTTPException, Header
from sqlalchemy.orm import Session

from app.database import get_db
from app.api.v1.auth import get_current_admin

ROLE_OPERATOR = "operator"
ROLE_ADMIN = "admin"
ROLE_DEVELOPER = "developer"
ALL_ROLES = (ROLE_OPERATOR, ROLE_ADMIN, ROLE_DEVELOPER)
DEFAULT_ROLE = ROLE_OPERATOR

# Zona de Setari — doar developer.
SETTINGS_MODULES = ("settings", "prompturi-ai", "surse-date")

# Toate modulele (tab-urile) din UI. Sursa: TABS in mg-app.js.
ALL_MODULES = (
    "dashboard", "clients", "utilizatori", "ai-analiza", "reports-stats",
    "satisfactie", "productivity", "emails", "documents", "cts-training",
    "reports", "taskuri", "device-ops", "apeluri", "cts-calls-training",
    "calls-analitice", "quality-eval", "personal-mailboxes", "feedback-config",
    "feedback-campaigns", "feedback-dashboard",
) + SETTINGS_MODULES

# Operatorul vede strict lista de emailuri + productivitate (rapoarte).
OPERATOR_MODULES = ("emails", "productivity")

# Module "de sprijin": nu sint tab-uri in meniu, dar alimenteaza paginile permise.
# Ex: pagina Emailuri deschide fisa unui client (nume, contracte, satisfactie),
# deci un operator are nevoie de /clients chiar daca tab-ul Clienti ii e ascuns.
# Fara asta, blocarea la nivel de router ar rupe exact paginile pe care le poate vedea.
SUPPORT_MODULES_BY_ROLE = {
    ROLE_OPERATOR: ("clients", "spam", "ai"),
}

# Sub-tab-uri permise per modul. Absenta unei chei = toate sub-tab-urile permise.
OPERATOR_SUBTABS = {"productivity": ("rapoarte",)}

# Prima pagina cu acces, per rol — tinta pentru redirect cand accesul e refuzat.
LANDING = {
    ROLE_OPERATOR: "emails",
    ROLE_ADMIN: "dashboard",
    ROLE_DEVELOPER: "dashboard",
}


def normalize_role(value) -> str:
    """Rol valid, altfel fallback pe cel mai restrictiv (deny-by-default)."""
    role = (value or "").strip().lower()
    return role if role in ALL_ROLES else DEFAULT_ROLE


def allowed_modules(role: str) -> list:
    """Modulele vizibile in meniul lateral."""
    role = normalize_role(role)
    if role == ROLE_DEVELOPER:
        return list(ALL_MODULES)
    if role == ROLE_ADMIN:
        return [m for m in ALL_MODULES if m not in SETTINGS_MODULES]
    return list(OPERATOR_MODULES)


def api_allowed_modules(role: str) -> list:
    """Modulele pe care rolul le poate CERE de la server.

    Superset al celor din meniu: include modulele de sprijin (date pe care
    paginile permise le folosesc fara sa fie tab-uri separate).
    """
    role = normalize_role(role)
    return list(allowed_modules(role)) + list(SUPPORT_MODULES_BY_ROLE.get(role, ()))


def allowed_subtabs(role: str) -> dict:
    """Restrictii de sub-tab per modul (doar unde exista)."""
    return dict(OPERATOR_SUBTABS) if normalize_role(role) == ROLE_OPERATOR else {}


def landing_module(role: str) -> str:
    return LANDING.get(normalize_role(role), "emails")


def can_access(role: str, module: str) -> bool:
    """Vizibilitate in meniu (strict)."""
    return module in allowed_modules(role)


def can_call_api(role: str, module: str) -> bool:
    """Acces la endpoint-uri (include modulele de sprijin)."""
    return module in api_allowed_modules(role)


def can_assign_role(actor_role: str, target_role: str) -> bool:
    """Un admin NU poate crea developeri — altfel s-ar putea auto-promova
    in zona de Setari, iar restrictia lui ar deveni decorativa."""
    actor = normalize_role(actor_role)
    target = normalize_role(target_role)
    if actor == ROLE_DEVELOPER:
        return target in ALL_ROLES
    if actor == ROLE_ADMIN:
        return target in (ROLE_OPERATOR, ROLE_ADMIN)
    return False


def require_module(module: str):
    """Dependency FastAPI: blocheaza accesul la endpoint-urile unui modul.

    Ascunderea tab-ului in UI e doar cosmetica; asta e gate-ul real.
    """
    def _guard(user=Depends(get_current_admin)):
        role = normalize_role(user.get("access_role"))
        if not can_call_api(role, module):
            raise HTTPException(
                403,
                {
                    "error": "forbidden_module",
                    "module": module,
                    "role": role,
                    "redirect_to": landing_module(role),
                },
            )
        return user
    return _guard


def require_module_for_paths(module: str, prefixes: tuple, public_paths: tuple = ()):
    """Ca require_module(), dar activ DOAR pe anumite prefixe de cale.

    Nevoie reala: routerul `health` ține și /health (public, folosit de
    monitorizare si de load-balancer) si /stats/* (datele Dashboard-ului).
    Paza pe tot routerul ar rupe monitorizarea; fara paza, statisticile sint
    expuse oricui. Asa filtram pe cale.
    """
    from fastapi import Request

    def _guard(request: Request, authorization: Optional[str] = Header(None),
               db: Session = Depends(get_db)):
        path = request.url.path
        if path in public_paths:
            return None
        if not any(seg in path for seg in prefixes):
            return None
        user = get_current_admin(authorization=authorization, db=db)
        role = normalize_role(user.get("access_role"))
        if not can_call_api(role, module):
            raise HTTPException(
                403,
                {
                    "error": "forbidden_module",
                    "module": module,
                    "role": role,
                    "redirect_to": landing_module(role),
                },
            )
        return user
    return _guard


def require_role(*roles):
    """Dependency FastAPI: restrictioneaza la roluri explicite."""
    allowed = {normalize_role(r) for r in roles}

    def _guard(user=Depends(get_current_admin)):
        role = normalize_role(user.get("access_role"))
        if role not in allowed:
            raise HTTPException(
                403,
                {
                    "error": "forbidden_role",
                    "role": role,
                    "redirect_to": landing_module(role),
                },
            )
        return user
    return _guard
