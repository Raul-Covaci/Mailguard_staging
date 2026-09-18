"""HEIC/HEIF (pozele de pe iPhone) -> JPEG, la ingest.

Restul aplicației nu știe HEIC: Pillow nu-l deschide fără `pillow-heif`, gateway-ul vision
(OCR/clasificare documente) îl respinge, browserul nu-l afișează, iar conversia la PDF pentru
CTS (`_to_pdf_compressed`) pică pe el. De aceea convertim O SINGURĂ DATĂ, la salvarea
atașamentului, și tot ce urmează vede un JPEG obișnuit.

Detecția merge pe content-type, extensie ȘI magic bytes: Graph raportează uneori HEIC ca
`application/octet-stream`, iar un fișier redenumit poate avea extensia greșită.
Fail-safe: orice eroare -> None, apelantul păstrează originalul (un atașament neconvertit
e vizibil și recuperabil; unul aruncat dispare tăcut).
"""
import io
import logging

logger = logging.getLogger("mailguard.heic_convert")

HEIF_EXT = (".heic", ".heif", ".hif")
HEIF_CT = {"image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence",
           "image/x-heic", "image/x-heif"}
# brand-urile ISO-BMFF din caseta `ftyp` (octeții 8-12) folosite de HEIC/HEIF
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs",
                b"mif1", b"msf1"}

_registered = None


def _register():
    """Înregistrează opener-ul HEIF în Pillow. True dacă `pillow-heif` e instalat."""
    global _registered
    if _registered is None:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
            _registered = True
        except Exception as e:
            logger.warning("pillow-heif indisponibil — HEIC rămâne neconvertit: %s", e)
            _registered = False
    return _registered


def is_heif(name=None, content_type=None, data=None):
    ct = (content_type or "").lower().split(";")[0].strip()
    if ct in HEIF_CT:
        return True
    if (name or "").lower().endswith(HEIF_EXT):
        return True
    if data and len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS:
        return True
    return False


def to_jpeg(data, quality=90):
    """bytes HEIC/HEIF -> bytes JPEG (orientarea EXIF aplicată). None la eșec."""
    if not data or not _register():
        return None
    try:
        from PIL import Image, ImageOps
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception as e:
        logger.warning("conversie HEIC->JPEG eșuată: %s", str(e)[:200])
        return None


def jpeg_name(name):
    """`IMG_1234.HEIC` -> `IMG_1234.jpg`; fără extensie HEIF -> se adaugă `.jpg`."""
    n = name or "image"
    low = n.lower()
    for ext in HEIF_EXT:
        if low.endswith(ext):
            return n[: -len(ext)] + ".jpg"
    return n + ".jpg"
