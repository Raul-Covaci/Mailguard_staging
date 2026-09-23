"""Concatenarea paginilor unui grup de documente intr-un singur PDF.

Un contract fotografiat pagina cu pagina ajunge ca N atasamente imagine, grupate de
`_autogroup_holistic` (primar + membri cu `grouped_into`). Pana acum CTS primea DOAR fisierul
rindului primar, deci paginile 2..N nu urcau niciodata pe contractul clientului: `_extract_group`
combina doar TEXTUL, iar feed-ul CTS filtreaza `grouped_into IS NULL`.

Aici se face singura operatie care lipsea: fisierele membrilor, in ordine, intr-un PDF.
"""
import logging

logger = logging.getLogger(__name__)

# Peste atat nu mai adaugam pagini: gateway-ul CTS plafoneaza atasamentul (base64 umfla ~33%).
MAX_GROUP_PDF_BYTES = 14 * 1024 * 1024


def build_group_pdf(pieces):
    """`pieces` = [(bytes, mime), ...] in ordinea paginilor (primar intai).

    Returneaza (pdf_bytes, "application/pdf"), sau (None, None) daca nu se poate produce nimic.
    Paginile care nu se pot deschide se sar — un contract cu o poza corupta trebuie sa ajunga la
    CTS cu paginile bune, nu sa cada de tot.
    """
    usable = [(b, m) for b, m in (pieces or []) if b]
    if not usable:
        return None, None
    if len(usable) == 1:
        return usable[0][0], usable[0][1]

    try:
        import fitz
    except ImportError:
        logger.warning("build_group_pdf: PyMuPDF indisponibil — trimit doar prima pagina")
        return usable[0][0], usable[0][1]

    out = fitz.open()
    added = 0
    size_so_far = 0
    try:
        for idx, (raw, mime) in enumerate(usable, 1):
            # Plafonul se verifica pe octetii ADAUGATI, nu serializand `out` la fiecare pas:
            # `tobytes()` pe un document cu zero pagini arunca ValueError, iar serializarea
            # repetata a intregului PDF ar fi patratica in numarul de pagini.
            if added and size_so_far >= MAX_GROUP_PDF_BYTES:
                logger.warning("build_group_pdf: plafon %d bytes atins — opresc la pagina %d/%d",
                               MAX_GROUP_PDF_BYTES, idx - 1, len(usable))
                break
            src = None
            try:
                if "pdf" in (mime or "").lower():
                    src = fitz.open(stream=raw, filetype="pdf")
                else:
                    img = fitz.open(stream=raw, filetype="image")
                    try:
                        src = fitz.open(stream=img.convert_to_pdf(), filetype="pdf")
                    finally:
                        img.close()
                out.insert_pdf(src)
                added += src.page_count
                size_so_far += len(raw)
            except Exception:
                logger.warning("build_group_pdf: pagina %d/%d nefolosibila — sarita",
                               idx, len(usable), exc_info=True)
            finally:
                if src is not None:
                    src.close()

        if added == 0:
            logger.warning("build_group_pdf: nicio pagina utilizabila din %d", len(usable))
            return usable[0][0], usable[0][1]
        return out.tobytes(deflate=True, garbage=4, clean=True), "application/pdf"
    except Exception:
        logger.exception("build_group_pdf: concatenare esuata — trimit doar prima pagina")
        return usable[0][0], usable[0][1]
    finally:
        out.close()
