"""Ordonare ASC/DESC pentru listele paginate (Emailuri, Mail-uri CTS, Task-uri,
Device Operations, Apeluri, Apeluri CTS, Reclamații).

De ce pe server și nu în UI: tabelele sunt paginate cu LIMIT/OFFSET, deci o sortare
locală ar reordona doar cele 50 de rânduri afișate, nu tot setul filtrat — inversarea
ar arăta "cele mai vechi din pagina 1", nu cele mai vechi în general.

Direcția nu poate fi legată ca parametru (ORDER BY nu acceptă bind params), deci se
interpolează în SQL — de aceea trece OBLIGATORIU prin whitelist-ul de aici.
"""

_ALLOWED = {"asc": "ASC", "desc": "DESC"}


def sort_dir(value: str, default: str = "DESC") -> str:
    """'asc'/'desc' (case-insensitive) → 'ASC'/'DESC'. Orice altceva → default."""
    return _ALLOWED.get((value or "").strip().lower(), default)


def sort_expr(value: str, columns: dict, default_key: str) -> str:
    """Cheia de coloană trimisă de UI → expresia SQL de ordonare, din whitelist.

    `columns` mapează cheia publică (ex. 'client') la expresia SQL ('cl.name').
    Cheile necunoscute cad pe `default_key`, deci un query param stricat nu poate
    nici să injecteze SQL, nici să rupă lista.
    """
    key = (value or "").strip().lower()
    return columns.get(key, columns[default_key])
