"""GET read-only pe IRIS Gateway, cu reincercare pe erori TRANZITORII.

DE CE EXISTA (2026-09-14, dupa alerta "5 erori critice" pe productie, 2026-09-12 03:09):
gateway-ul IRIS (aplicatie separata) raspunde ocazional 500 pe /cts/* — verificat ulterior
manual: aceleasi cereri, aceiasi parametri, 12/12 raspunsuri 200. Deci e o pana de cateva
secunde pe partea IRIS, nu un bug de contract la noi.

Fiecare modul de sync avea `httpx.Client(...).get(...)` + `raise_for_status()` direct, adica
o singura incercare: un 500 de o secunda pierdea tot tick-ul de 5 minute si aparea in loguri
ca eroare. Datele nu se pierdeau (ferestrele sunt rolling si upsert-ul e idempotent), dar
zgomotul ajungea la watchdog ca "eroare critica".

Reincercam DOAR ce e tranzitoriu: 5xx + erori de transport/timeout. 4xx (401/403/404) sunt
probleme de contract sau de cheie — reincercarea nu le repara si doar ar tripla latenta
tick-ului, deci ies din prima.
"""
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("mailguard.iris_http")

RETRY_STATUSES = (500, 502, 503, 504)
# 3 incercari in total. Plafon ~7s peste prima cerere: tick-ul cronului e la 5 min si
# /process/run-now ruleaza sincron, deci nu putem sta mult aici.
BACKOFF_SECONDS = (2, 5)


def get_with_retry(url: str, *, params: Optional[Dict[str, Any]] = None,
                   headers: Optional[Dict[str, str]] = None,
                   timeout: float = 30, verify: bool = False, label: str = "",
                   allow_statuses: tuple = ()):
    """GET cu reincercare pe 5xx/transport. Ridica exact ca `raise_for_status` la ultima incercare.

    `allow_statuses` intoarce raspunsul fara `raise_for_status` pentru codurile date — folosit de
    modulele care trateaza 404 ca "endpoint neconstruit inca de IRIS", nu ca eroare.
    """
    import httpx
    what = label or url
    attempts = len(BACKOFF_SECONDS) + 1
    for i in range(attempts):
        last = (i == attempts - 1)
        try:
            with httpx.Client(timeout=timeout, verify=verify) as cl:
                r = cl.get(url, params=params, headers=headers)
            if r.status_code in RETRY_STATUSES and not last:
                logger.warning("IRIS %s: HTTP %s tranzitoriu, reincerc peste %ss (%s/%s)",
                               what, r.status_code, BACKOFF_SECONDS[i], i + 1, attempts)
                time.sleep(BACKOFF_SECONDS[i])
                continue
            if r.status_code in allow_statuses:
                return r
            r.raise_for_status()
            return r
        except httpx.TransportError as e:   # include TimeoutException, ConnectError, ReadError
            if last:
                raise
            logger.warning("IRIS %s: %s: %s — reincerc peste %ss (%s/%s)",
                           what, type(e).__name__, e, BACKOFF_SECONDS[i], i + 1, attempts)
            time.sleep(BACKOFF_SECONDS[i])
    raise RuntimeError("IRIS %s: reincercari epuizate" % what)  # defensiv, bucla iese prin return/raise
