"""Normalizarea imaginilor trimise la vision (T3-D1) — un singur loc pentru toate apelurile vision
din documents.py și op_extractor.py.

De ce: imaginile TIFF/BMP (nesuportate de model) și pozele de până la 14 MB plecau neschimbate, iar
gateway-ul le respingea — `doc_vision_ocr` avea ~37% erori, reluate apoi de drain. Cu flag-ul
settings['processing.vision_image_normalize_enabled'] (implicit OFF):
  - TIFF/BMP -> PNG;
  - peste 4,5 MB sau latura lungă peste 2.000 px -> micșorare proporțională (JPEG dacă PNG-ul tot
    nu încape);
  - PDF și orice non-imagine -> neatinse;
  - dacă tot nu încape sau nu se poate decoda -> None: apelantul NU cheamă AI-ul.
O imagine care respectă deja limitele trece cu octeții IDENTICI (cheia cache-ului T3-L1 rămâne
aceeași). Cheia se calculează oricum pe octeții trimiși efectiv, deci pe imaginea normalizată.
"""
import io
import logging
from typing import Optional, Tuple

from app.services import feature_flags

logger = logging.getLogger("mailguard.vision_image")

FLAG_KEY = "processing.vision_image_normalize_enabled"
MAX_BYTES = int(4.5 * 1024 * 1024)
MAX_SIDE = 2000
_CONVERT = {"image/tiff", "image/tif", "image/bmp", "image/x-ms-bmp"}
_JPEG_QUALITY = 85


def _encode(img, fmt: str) -> bytes:
    buf = io.BytesIO()
    if fmt == "JPEG":
        if img.mode not in ("RGB", "L"):
            from PIL import Image
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        img.save(buf, "JPEG", quality=_JPEG_QUALITY, optimize=True)
    else:
        img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def normalize(raw: bytes, mime: str) -> Optional[Tuple[bytes, str]]:
    """(octeți, mime) gata de trimis, sau None dacă imaginea nu poate fi adusă în limite."""
    m = (mime or "").lower()
    if not m.startswith("image/"):
        return raw, mime                      # PDF & co.: neatinse
    try:
        from PIL import Image, ImageOps
        img = Image.open(io.BytesIO(raw))
        w, h = img.size
    except Exception:
        # Nedecodabil: dacă oricum ar fi plecat (format acceptat, sub prag), nu blocăm ce merge azi.
        if m not in _CONVERT and len(raw) <= MAX_BYTES:
            return raw, mime
        logger.warning("vision_image: imagine nedecodabila (%s, %d octeti) — fara apel AI", m, len(raw))
        return None
    if m not in _CONVERT and len(raw) <= MAX_BYTES and max(w, h) <= MAX_SIDE:
        return raw, mime                      # deja în limite: octeți identici
    try:
        img.seek(0)                           # TIFF multi-pagină: prima pagină
        img = ImageOps.exif_transpose(img)    # re-encodarea pierde EXIF: aplicăm orientarea
        img.load()
        if max(img.size) > MAX_SIDE:
            img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
        lossless = m in _CONVERT or m == "image/png"
        data, out_mime = (_encode(img, "PNG"), "image/png") if lossless else (_encode(img, "JPEG"), "image/jpeg")
        for _ in range(6):
            if len(data) <= MAX_BYTES:
                return data, out_mime
            if out_mime == "image/png":       # PNG prea mare: JPEG la aceeași rezoluție
                data, out_mime = _encode(img, "JPEG"), "image/jpeg"
                continue
            img = img.resize((max(1, int(img.width * 0.8)), max(1, int(img.height * 0.8))), Image.LANCZOS)
            data = _encode(img, "JPEG")
    except Exception:
        logger.warning("vision_image: normalizare esuata (%s, %d octeti) — fara apel AI", m, len(raw))
        return None
    logger.warning("vision_image: imagine tot peste %d octeti dupa micsorare — fara apel AI", MAX_BYTES)
    return None


def prepare(raw: bytes, mime: str) -> Optional[Tuple[bytes, str]]:
    """Punctul de intrare pentru apelanți: cu flag-ul OFF, (raw, mime) neschimbate."""
    if not feature_flags.is_enabled(FLAG_KEY):
        return raw, mime
    return normalize(raw, mime)
