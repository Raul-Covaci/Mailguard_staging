"""Fereastra de raportare pentru statisticile din pagina „Procesare documente".

Decizie business (2026-08-26): statisticile de incadrare (auto-validare, corectat de operator,
reincadrat, trasabilitate CTS) se numara DOAR de la 24.08.2026 incolo. Documentele de dinainte au
fost procesate cu alt set de tipuri/prompturi si trag procentele in jos, fara sa spuna nimic despre
cum se comporta fluxul curent.

Se filtreaza pe data MAILULUI (`emails.received_at`), la fel ca `scope=today` din aceleasi
endpoint-uri — altfel „azi" nu ar mai fi un subset al totalului. Trasabilitatea CTS are propriul
camp de timp (`cts_document_tracking.extracted_at`), filtrat cu aceeasi data.

Constanta e SINGURA sursa de adevar: o folosesc si `/documents/extractions/stats`
(app/api/v1/documents.py) si `/cts/document-stats` (app/api/v1/cts.py), iar UI-ul o afiseaza din
raspuns, nu o rescrie.
"""

STATS_SINCE = "2026-08-24"


def clamp_from_date(from_date):
    """Ridica un `from_date` primit din afara la STATS_SINCE (comparatie lexicografica pe ISO).

    Returneaza STATS_SINCE daca `from_date` lipseste sau e mai vechi — nimic dinainte de fereastra
    nu se contorizeaza, indiferent ce cere apelantul.
    """
    fd = (from_date or "").strip()
    if not fd or fd[:10] < STATS_SINCE:
        return STATS_SINCE
    return fd
