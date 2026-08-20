"""Procesare documente — modul nou (Phase 1: fundatie / tab „Tipuri de documente").

Operatorul defineste TIPURI de documente pe 3 categorii (vehicul / sofer / contract): nume,
un SABLON exemplu (poza/pdf), campurile de extras si un prompt de extragere (generat cu AI).
Modelat dupa modulul Rapoarte (tab „Automate"): editor campuri + „Genereaza prompt cu AI" +
„Testeaza extragerea pe exemplu".

Vision: gateway-ul AI IRIS e strict TEXT (Regula 9). Aici extragem text LOCAL din document
(PDF -> text nativ pdfplumber/PyMuPDF; poza -> OCR pytesseract + auto-rotate) si il trimitem la
`iris_ai`. Un canal vision dedicat (acuratete mare pe poze) e cerut separat prin outbox.

Phase 2 (NU in acest fisier inca): legarea in process_email (detectare tip + extragere automata
la emailuri cu atasament) -> document_extractions -> CTS.
"""
import os
import json
import base64
import uuid
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse, Response
from sqlalchemy.orm import Session
from sqlalchemy import text, bindparam

from app.database import get_db, SessionLocal
from app.api.v1.auth import get_current_admin
from app.services import iris_ai
from app.services import iris_docsvc
# Reutilizam helperele PURE din Rapoarte (sursa unica, fara drift).
from app.api.v1.reports import _fmt_fields, _fields_keys, _RETRY_CODES, AI_WORKERS
# Traducerea caii atasamentului (container -> host) e single-source in emails.py.
from app.api.v1.emails import _host_path

logger = logging.getLogger("mailguard.documents")
router = APIRouter()


# ── Prompt redenumire standardizata documente ────────────────────────────────
RENAME_SYSTEM_PROMPT = """Ești un modul de redenumire a documentelor dintr-un sistem de procesare a emailurilor. Tipul fiecărui document este DEJA identificat de sistem și ți se transmite. Sarcina ta NU este să reclasifici, ci doar să produci numele standardizat al fișierului conform regulilor de mai jos.

## Ce primești (input)
- `tip_document` — tipul deja identificat (ex.: `talon`, `carte` / `CIV`, `CEMT`, `COC`, `contract`, `anexa 1`, `anexa 2`, `anexa 3`, `anexa 4`, `formular de înregistrare`, `CUI firmă`, `buletin administrator`).
- `continut` — textul / OCR al documentului (necesar la documentele de vehicul pentru a extrage țara și numărul de înmatriculare; la contracte pentru tipul serviciului).
- `extensie` — extensia fișierului original (ex.: `.pdf`, `.jpg`).

Dacă `tip_document` lipsește, deduci tipul din conținut; altfel folosești tipul primit ca atare.

## Reguli de redenumire

### A. Documente de vehicul (mașină)
Format: `TARA_NRINMATRICULARE_TIP_NRDOC`
- **TARA** — codul de țară unde este înmatriculată mașina (`RO` = România, `MD` = Moldova etc.). Se determină din document; prefixul plăcuței poate ajuta.
- **NRINMATRICULARE** — numărul de înmatriculare, cu MAJUSCULE, fără spații, cratime sau puncte (ex.: „BH 01 CTS" → `BH01CTS`).
- **TIP** și **NRDOC** — conform tabelului:

| Document | TIP | NRDOC |
|---|---|---|
| Carte de identitate a vehiculului (CIV / „carte") | `VP` | `01` |
| Talon (certificat de înmatriculare) | `VP` | `02` |
| CEMT | `EC` | `01` |
| COC | `EC` | `02` |

Exemple (țară `RO`, plăcuță `BH01CTS`):
- Carte / CIV → `RO_BH01CTS_VP_01`
- Talon → `RO_BH01CTS_VP_02`
- CEMT → `RO_BH01CTS_EC_01`
- COC → `RO_BH01CTS_EC_02`

Excepție — țări non-RO: Doar România are carte ȘI talon ca documente separate. Pentru celelalte țări, unde există un singur document de înmatriculare (echivalent `VP`), NU se adaugă numărul de document: format `TARA_NRINMATRICULARE_VP`.

CEMT și COC (familia `EC`) rămân `EC_01` / `EC_02` și pentru țările non-RO.

### B. Contracte
Format: `contract_<serviciu>` — cuvântul „contract" + tipul serviciului.
Exemple: `contract_cargobox`, `contract_taxe_drum`, `contract_carbon`.

Pachetul contractului Cargobox conține și:
- Anexa 1 (oferta comercială) → `anexa 1`
- Anexa 2 (documentul de instalare/predare echipament, provenit din documentele de șofer) → `anexa 2`
- Anexa 3 (toll4europe, 6 pagini) → `anexa 3`
- Anexa 4 (protecția datelor, 4 pagini) → `anexa 4`

ATENȚIE — Anexa 2 a fost numită în trecut „Proces verbal" / „Proces verbal CargoBox", iar tipul
primit în `tip_document` poate încă purta denumirea veche. Documentul se numește ACUM **Anexa 2**
și nu mai are nicio legătură cu un proces verbal. Orice tip care conține „proces verbal" (în orice
formă: „Proces verbal", „proces-verbal", „Anexa 2 - Proces verbal CargoBox", „PV") se redenumește
`anexa 2`. **Nu produce NICIODATĂ un nume care conține „proces verbal".**

Documente obligatorii la Cargobox și taxe de drum Polonia:
- CUI firmă → `CUI firma`
- Buletin administrator → `buletin administrator`

### C. Alte documente
- Formular de înregistrare (taxe drum) → `formular de înregistrare`

## Reguli generale
- Păstrează extensia originală a fișierului; nu o modifica.
- La plăcuțe: MAJUSCULE, fără spații, cratime sau puncte.
- Nu inventa date. Dacă nu poți extrage cu certitudine țara sau numărul de înmatriculare, marchează incertitudinea și lasă câmpul gol.
- Nu schimba tipul primit de la sistem; doar redenumești.
- Dacă tipul nu se încadrează în niciuna din reguli, returnează o variantă normalizată a denumirii tipului și semnalează în `incertitudini`. **Excepție**: denumirile istorice au prioritate față de această normalizare — un tip care conține „proces verbal" devine `anexa 2`, nu o normalizare a denumirii primite.

## Format de ieșire
Răspunde DOAR cu un obiect JSON, fără alt text:
{"tip_document":"...","tara":"...","nr_inmatriculare":"...","nume_nou":"...","extensie":"...","nume_complet":"...","incertitudini":[]}
- `incertitudini` — lista goală dacă totul e clar.
- La documentele care nu sunt de vehicul, `tara` și `nr_inmatriculare` pot fi `null`."""

TEMPLATE_DIR = "/opt/iris-mailguard/data/doc_templates"
ALLOWED_CATS = {"vehicul", "sofer", "contract"}
MAX_DOC_TEXT = 14000        # text trimis la AI per document
MAX_PAGES = 12              # pagini PDF citite

# ── Control OCR (tesseract e CPU-bound; fara plafon -> 8 core la load 36) ────
# OCR_TESS_TIMEOUT: limita WALL-CLOCK per apel tesseract. CRITIC: o cerere abortata (client
# 75s) NU opreste OCR-ul sync din threadpool -> tesseract ramane ORFAN (ppid=1) macinand CPU
# minute intregi -> spirala de saturare. pytesseract cu timeout= ucide subprocesul -> fara orfani.
# _OCR_SEM: plafon de concurenta OCR per-proces (drain + interactiv se insumeaza; orfanul ocolea
# single-flight-ul drain-ului). PSM: doar PSM 6 (bloc uniform — castigator pe taloane/CIV/CEMT;
# PSM 3 default intorcea aproape nimic pe scanuri si DUBLA costul).
OCR_TESS_TIMEOUT = 30
_OCR_SEM = threading.Semaphore(3)

# ── Buget AI: interactiv (click-and-wait) vs batch ──────────────────────────
# Actiunile pornite manual din UI (test-extract / test-detect / reidentify) trebuie sa
# se termine cu mult sub bugetul proxy-ului (nginx ~ minut) ca sa NU intoarca 504 (HTML)
# si sa NU monopolizeze workerii (4) + cota gateway (AI_WORKERS=3), infometand
# categorisirea emailurilor (/process/run-now). De aceea, in mod interactiv, apelurile la
# gateway fac fail-fast: 1 incercare, timeout scurt. Batch-ul (cron) ramane rezilient
# (3 retry / timeout default 120s) — cron-ul re-preia oricum error_nova la rularea urmatoare.
import contextvars
_INTERACTIVE = contextvars.ContextVar("doc_interactive", default=False)
INTERACTIVE_TIMEOUT = 22.0          # s, per apel gateway text in mod interactiv
INTERACTIVE_VISION_TIMEOUT = 30.0   # s, vision (sonnet) e mai greu — buget putin mai mare
INTERACTIVE_ATTEMPTS = 1            # fara retry lung in mod interactiv


def _ai_budget(vision=False):
    """(max_attempts, call_timeout) dupa modul curent. Interactiv => fail-fast."""
    if _INTERACTIVE.get():
        return INTERACTIVE_ATTEMPTS, (INTERACTIVE_VISION_TIMEOUT if vision else INTERACTIVE_TIMEOUT)
    return 3, None           # batch: comportament istoric (3 retry, timeout default gateway)


# ── Preprocesare imagine pentru OCR (Pillow-only, fara dependente noi) ──────
def _prep_image(img):
    """Curata imaginea ca OCR-ul sa fie cat mai concret: EXIF-rotate, grayscale,
    upscale (poze mici), autocontrast, contrast + sharpen. Fail-safe: orice pas
    care pica e sarit, nu opreste pipeline-ul."""
    from PIL import Image, ImageOps, ImageFilter, ImageEnhance
    try:
        img = ImageOps.exif_transpose(img)            # orientare corecta dupa EXIF
    except Exception:
        pass
    try:
        img = img.convert("L")                        # grayscale
    except Exception:
        pass
    try:
        w, h = img.size                               # upscale daca poza e mica (OCR vrea ~300dpi)
        m = max(w, h)
        if m and m < 1800:
            f = min(3.0, 1800.0 / m)
            img = img.resize((max(1, int(w * f)), max(1, int(h * f))), Image.LANCZOS)
    except Exception:
        pass
    try:
        img = ImageOps.autocontrast(img, cutoff=1)    # normalizeaza luminozitatea
    except Exception:
        pass
    try:
        img = ImageEnhance.Contrast(img).enhance(1.4)
    except Exception:
        pass
    try:
        img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
    except Exception:
        pass
    return img


# ── OCR robust pe o imagine PIL ─────────────────────────────────────────────
def _ocr_image(img) -> str:
    """OCR pe o imagine PIL, incercand mai multe moduri de segmentare (PSM) si pastrand
    rezultatul cel mai lung. Scanurile de documente complexe (taloane, formulare) ies prost
    cu PSM auto (3) dar bine cu PSM 6 (bloc uniform). Daca preprocesarea strica scanul, cade
    pe imaginea bruta. Fail-safe: niciodata nu arunca."""
    import pytesseract
    from PIL import ImageOps
    try:
        prepped = _prep_image(img)
    except Exception:
        prepped = img

    def _tess(image, cfg):
        # timeout WALL-CLOCK: ucide tesseract daca CPU-ul e sufocat (fara orfani de minute).
        return (pytesseract.image_to_string(image, lang="ron+eng", config=cfg,
                                            timeout=OCR_TESS_TIMEOUT) or "").strip()

    # Plafon de concurenta: drain + interactiv NU trebuie sa pârlească toate core-urile.
    with _OCR_SEM:
        best = ""
        try:                                   # PSM 6 = bloc uniform (castigator pe scanuri)
            best = _tess(prepped, "--psm 6")
        except Exception:
            pass
        if len(best) < 40:                     # preprocesarea agresiva poate albi scanul -> brut
            try:
                raw = ImageOps.exif_transpose(img)
            except Exception:
                raw = img
            try:
                t = _tess(raw, "--psm 6")
                if len(t) > len(best):
                    best = t
            except Exception:
                pass
    return best


# ── OCR pe PDF scanat (poza in PDF, fara strat text) ────────────────────────
def _ocr_pdf(path: str) -> str:
    """Randeaza fiecare pagina PDF la ~300 DPI -> OCR robust (_ocr_image).
    Pentru taloane/CIV/formulare trimise ca scan/poza salvata in PDF. Fail-safe per pagina."""
    try:
        import io
        import fitz  # PyMuPDF
        from PIL import Image
        out = []
        d = fitz.open(path)
        n = min(MAX_PAGES, d.page_count)
        mat = fitz.Matrix(300 / 72.0, 300 / 72.0)   # ~300 DPI
        for i in range(n):
            try:
                pix = d[i].get_pixmap(matrix=mat)
                out.append(_ocr_image(Image.open(io.BytesIO(pix.tobytes("png")))))
            except Exception as e:
                logger.warning("pdf page OCR failed %s p%d: %s", path, i, e)
        d.close()
        return "\n".join(t for t in out if t).strip()
    except Exception as e:
        logger.warning("OCR PDF failed %s: %s", path, e)
        return ""


# ── Extragere text local din document (PDF nativ / OCR poza) ────────────────
def _doc_text(path: str, mime: str):
    """Returneaza (text, method). Fail-safe: niciodata nu arunca.
    PDF -> text nativ (pdfplumber, fallback PyMuPDF); PDF scanat fara strat text -> OCR pe
    pagini randate (_ocr_pdf). Imagine -> OCR (tesseract ron+eng, auto-rotate dupa EXIF)."""
    mime = (mime or "").lower()
    ext = (os.path.splitext(path or "")[1] or "").lower()
    is_pdf = ("pdf" in mime) or (ext == ".pdf")
    if is_pdf:
        txt = ""
        try:
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                txt = "\n".join((pg.extract_text() or "") for pg in pdf.pages[:MAX_PAGES])
        except Exception as e:
            logger.warning("pdfplumber failed %s: %s", path, e)
            txt = ""
        if len((txt or "").strip()) < 20:
            try:
                import fitz  # PyMuPDF
                d = fitz.open(path)
                txt = "\n".join(d[i].get_text() for i in range(min(MAX_PAGES, d.page_count)))
                d.close()
            except Exception as e:
                logger.warning("PyMuPDF failed %s: %s", path, e)
        # PDF scanat (fara strat text) -> randam paginile la imagine si facem OCR
        if len((txt or "").strip()) < 20:
            ocr_txt = _ocr_pdf(path)
            if len(ocr_txt) > len((txt or "").strip()):
                return ocr_txt, "pdf_ocr"
        return (txt or "").strip(), "pdf_text"
    # imagine -> OCR robust (preprocesare + PSM multiple, fallback brut)
    try:
        from PIL import Image
        return _ocr_image(Image.open(path)), "ocr"
    except Exception as e:
        logger.warning("OCR failed %s: %s", path, e)
        return "", "ocr_failed"


# ── Canal vision (citire scanate: poze de telefon / PDF scanat fara strat text) ──
VISION_MAX_BYTES = 14 * 1024 * 1024   # sub limita gateway-ului (20MB base64/atasament)


def _attachment_mime(path: str, mime: str) -> str:
    """Mime-ul pentru atasamentul multimodal (PDF/imagine) trimis gateway-ului vision.
    Detecteaza tipul REAL din magic bytes — previne erori 400 cand fisierul e PNG dar
    MIME-ul declarat e image/gif (expeditorul a gresit tipul sau redenumit fisierul)."""
    ext = (os.path.splitext(path or "")[1] or "").lower()
    m = (mime or "").lower()
    if "pdf" in m or ext == ".pdf":
        return "application/pdf"
    # Detectie din magic bytes (primii 12 octeti) — autoritara fata de extensie/MIME declarat.
    try:
        with open(path, "rb") as _fh:
            _head = _fh.read(12)
        if _head[:4] == b'\x89PNG':
            return "image/png"
        if _head[:3] == b'\xff\xd8\xff':
            return "image/jpeg"
        if _head[:6] in (b'GIF87a', b'GIF89a'):
            return "image/gif"
        if _head[:4] == b'RIFF' and _head[8:12] == b'WEBP':
            return "image/webp"
        if _head[:4] in (b'MM\x00*', b'II*\x00'):
            return "image/tiff"
        if _head[:2] == b'BM':
            return "image/bmp"
    except Exception:
        pass
    if m.startswith("image/"):
        return m
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
            ".tif": "image/tiff", ".tiff": "image/tiff"}.get(ext, "image/jpeg")


_VISION_OCR_SYSTEM = (
    "Esti un OCR expert. Transcrie FIDEL tot textul vizibil din acest document, exact cum apare "
    "in pagina: include codurile/etichetele de camp (ex. A, B, D.1, E, P.2), numerele, datele, "
    "placutele de inmatriculare, numele si tot ce e tiparit sau scris. Pastreaza, pe cat posibil, "
    "ordinea si gruparea (o eticheta urmata de valoarea ei). NU interpreta, NU rezuma, NU traduce, "
    "NU adauga nimic de la tine. Returneaza DOAR textul brut transcris."
)


def _vision_transcribe(path: str, mime: str):
    """Trimite documentul (imagine sau PDF) prin canalul vision extern (model multimodal) ca
    sa transcrie textul vizibil cand OCR-ul local iese gol (scan / poza de telefon).
    Returneaza (text, err): err non-None DOAR la esec de apel (folosit pentru retry tranzitoriu).
    Nu arunca niciodata."""
    import base64
    import hashlib
    import time
    amime = _attachment_mime(path, mime)
    try:
        sz = os.path.getsize(path)
        if sz > VISION_MAX_BYTES:
            return "", "fisier prea mare pentru vision (%d MB)" % (sz // (1024 * 1024))
        with open(path, "rb") as fh:
            raw = fh.read()
    except Exception as e:
        return "", "citire fisier esuata: " + str(e)[:120]
    digest = hashlib.sha1(raw).hexdigest()[:12]   # acelasi document -> acelasi task -> cache gateway
    b64 = base64.b64encode(raw).decode("ascii")
    task = "cargo360:doc_vision_ocr:" + digest
    res = None
    _attempts, _ctimeout = _ai_budget(vision=True)
    for attempt in range(_attempts):
        res = iris_ai.run_prompt(
            _VISION_OCR_SYSTEM, "", response_format="text", model_hint="sonnet",
            temperature=0.0, max_tokens=3000, task=task, timeout=_ctimeout,
            attachments=[{"mime_type": amime, "data_base64": b64}])
        if res.get("ok") or (res.get("error") or {}).get("code") not in _RETRY_CODES:
            break
        time.sleep(1.2 * (attempt + 1))
    if res and res.get("ok"):
        return (res.get("text") or "").strip(), None
    err = ((res.get("error") or {}).get("message") if res else "vision fail") or "vision fail"
    return "", err


def _doc_text_vision(path: str, mime: str):
    """OCR local; daca iese gol, fallback pe canalul vision extern. Returneaza
    (text, method, err). err non-None DOAR cand vision a fost incercat si a esuat
    (pentru a distinge un esec tranzitoriu de un document chiar fara text)."""
    txt, method = _doc_text(path, mime)
    if (txt or "").strip():
        return txt, method, None
    vtxt, verr = _vision_transcribe(path, mime)
    if (vtxt or "").strip():
        return vtxt, "vision", None
    return "", method, verr


# ── Prompturi (document-specific) ───────────────────────────────────────────
_DOC_GEN_SYSTEM = (
    "Esti un asistent care scrie PROMPTURI de extragere de date din DOCUMENTE (talon, CIV, CEMT, "
    "buletin, pasaport, contracte etc.). Primesti: (1) tipul si categoria documentului, (2) lista "
    "campurilor pe care utilizatorul vrea sa le extraga (cu nume, tip si descriere) si, optional, "
    "(3) textul extras dintr-un document EXEMPLU. Scrie un prompt CLAR, in romana, care va fi folosit "
    "ca instructiune de sistem pentru un model AI ca sa extraga EXACT acele campuri din orice document "
    "de acest tip. Promptul trebuie: sa explice ce tip de document este, sa listeze campurile de extras "
    "cu unde/cum se gasesc in document, sa ceara returnarea unui JSON cu cheile = numele campurilor, sa "
    "respecte tipul fiecarui camp si sa puna null cand un camp lipseste. Fa-l GENERAL (nu copia valorile "
    "concrete din exemplu). Returneaza DOAR textul promptului, fara explicatii, fara ``` ."
)


def _build_doc_extract_system(extract_prompt: str, fields: list) -> str:
    keys = _fields_keys(fields)
    base = (extract_prompt or "").strip()
    tail = ("\n\nReturneaza DOAR un JSON valid, fara text in plus, fara ``` , cu EXACT cheile: "
            + ", ".join(keys) + ". Pentru orice valoare care lipseste in document foloseste null. "
            "Nu inventa valori.")
    return base + tail


# Addendum pentru EXTRAGEREA VIZUALA (documentul trimis ca atasament multimodal). Citirea directa
# din imagine repara: (a) campuri pierdute de OCR cand layout-ul conteaza (taloane: cod langa valoare),
# (b) 'Este semnat' — semnatura olografa / stampila sunt elemente VIZUALE, invizibile in stratul de text.
_VISION_EXTRACT_ADDENDUM = (
    "\n\nSURSA VIZUALA (autoritara): documentul iti este oferit ca ATASAMENT (imagine sau PDF). "
    "Citeste valorile DIRECT din pagina, respectand pozitia si eticheta fiecarui camp (codurile de "
    "langa valoare: A, B, C.x, D.1, D.3, E, F.1, G, J/J1, P.1, P.3 etc.). Daca un cod din schema apare "
    "pe document cu o varianta locala (ex. categoria pe talonul moldovenesc e la 'J1', nu 'J'), "
    "potriveste dupa SENSUL campului. Transcrie sirurile lungi (VIN / serie sasiu, numar de inmatriculare) "
    "EXACT, caracter cu caracter. Daca primesti si un text OCR brut, foloseste-l DOAR ca indiciu pentru "
    "caractere greu de citit — imaginea ramane sursa de adevar. "
    "Pentru un camp boolean de tip 'Este semnat': raspunde true DOAR daca vezi efectiv in imagine o "
    "semnatura olografa (traseu de pix) si/sau o stampila aplicata in dreptul "
    "clientului/beneficiarului/cumparatorului; o eticheta 'Semnatura si stampila' fara nimic desenat "
    "dedesubt inseamna false. Nu confunda antetul/logo-ul tiparit cu o semnatura."
)


def _build_doc_extract_system_vision(extract_prompt: str, fields: list) -> str:
    keys = _fields_keys(fields)
    base = (extract_prompt or "").strip() + _VISION_EXTRACT_ADDENDUM
    tail = ("\n\nFORMAT RASPUNS: returneaza UN SINGUR obiect JSON valid si NIMIC ALTCEVA — fara "
            "explicatii, fara comentarii, fara markdown/``` inainte sau dupa. Primul caracter al "
            "raspunsului trebuie sa fie '{'. Cheile EXACTE: " + ", ".join(keys)
            + ". Pentru orice valoare care chiar lipseste in document foloseste null. Nu inventa valori.")
    return base + tail


def _cache_salt(*parts) -> str:
    """Hash scurt din (system+continut) folosit ca sufix de `task`. Gateway-ul IRIS
    cheieste cache-ul curated pe `task` (NU pe prompt si NU respecta use_cache), deci un
    `task` distinct per (prompt, continut) garanteaza: prompt schimbat -> re-ruleaza;
    document diferit -> re-ruleaza (fara contaminare intre documente); document identic ->
    cache idempotent (cost redus)."""
    import hashlib
    h = hashlib.sha1(("\n\x1e".join(p or "" for p in parts)).encode("utf-8", "ignore"))
    return h.hexdigest()[:10]


def _slug(name: str, fallback: str = "doc") -> str:
    """Eticheta ASCII din numele tipului, pentru `task` lizibil in telemetrie/cache
    (ex: 'Autorizatia CEMT' -> 'Autorizatia_CEMT'). Translitereaza diacriticele."""
    import re
    import unicodedata
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")
    return s[:60] or fallback


def _clip_doc_text(text: str, limit: int = MAX_DOC_TEXT) -> str:
    """Trunchiere care pastreaza CAPUL si COADA documentului. Capul contine titlul
    (nr./data) si partile; COADA contine blocul de semnaturi (mereu la final). O taiere
    simpla [:limit] ar elimina semnaturile la contractele lungi (ex. cargofuel ~45k chars
    pe 6 pagini) -> 'Este semnat' ar fi structural mereu fals. Pentru documente sub limita
    se intoarce textul intreg."""
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    head = int(limit * 0.62)
    tail = limit - head - 24
    return t[:head].rstrip() + "\n\n[...continut intermediar omis...]\n\n" + t[-tail:].lstrip()


import re as _re
_PAREN_TAIL = _re.compile(r"\s*\([^)]*\)\s*$")


def _canon_key(s) -> str:
    """Numele de baza al unui camp, FARA codul din paranteza finala, normalizat
    (ex. 'Vin (E.)' -> 'vin', 'Power (P.2)' -> 'power', 'Country ()' -> 'country')."""
    s = _PAREN_TAIL.sub("", str(s or "")).strip().lower()
    return _re.sub(r"\s+", " ", s)


def _normalize_keys(data, fields):
    """Remapeaza cheile returnate de model la cheile CANONICE din schema tipului.
    Modelul scapa uneori sufixul codului (intoarce 'Vin' in loc de 'Vin (E.)') sau
    paranteza goala ('Country' vs 'Country ()') -> ar aparea ca un camp duplicat in UI
    (datele sub cheia veche + campul schema gol). Potrivire pe NUMELE DE BAZA exact
    (fara codul din paranteza), NU pe prefix (CIV are 'Vehicle Type'/'Vehicle Model Year'
    care s-ar confunda la prefix). No-op cand cheile sunt deja canonice (ex. CEMT)."""
    if not isinstance(data, dict) or not fields:
        return data
    canon2key, valid = {}, set()
    for f in fields:
        k = f.get("name") if isinstance(f, dict) else None
        if k:
            valid.add(k)
            canon2key.setdefault(_canon_key(k), k)
    out = {}
    for mk, mv in data.items():
        if mk in valid:            # deja canonica
            out[mk] = mv
            continue
        tgt = canon2key.get(_canon_key(mk))
        out[tgt or mk] = mv        # remapeaza daca recunoastem baza; altfel pastreaza
    return out


# Coduri de judet RO (+ B Bucuresti) — inferenta tarii din placa cand lipseste campul Country.
_RO_COUNTY = {
    "B", "AB", "AG", "AR", "BC", "BH", "BN", "BR", "BT", "BV", "BZ", "CJ", "CL", "CS",
    "CT", "CV", "DB", "DJ", "GJ", "GL", "GR", "HD", "HR", "IF", "IL", "IS", "MH", "MM",
    "MS", "NT", "OT", "PH", "SB", "SJ", "SM", "SV", "TL", "TM", "TR", "VL", "VN", "VS",
}


def _vehicle_doc_code(detected_type):
    """(TIP, NRDOC) standard pt un document de inmatriculare vehicul, altfel None.
    CIV/Carte -> (VP,01); Talon/cert. inmatriculare -> (VP,02); CEMT -> (EC,01); COC -> (EC,02)."""
    n = (detected_type or "").lower()
    if "cemt" in n:
        return ("EC", "01")
    if ("coc" in n) or ("certificate of conformity" in n) or ("conformitate" in n):
        return ("EC", "02")
    if ("identitate vehicul" in n) or ("civ" in n) or (("carte" in n) and ("vehicul" in n)):
        return ("VP", "01")
    if ("talon" in n) or ("inmatricul" in n):
        return ("VP", "02")
    return None


def _vehicle_std_name(db, email_id, att_id, part_no, detected_type, data):
    """Numele standardizat al unui document de VEHICUL construit DETERMINIST din datele DEJA
    extrase (placa/tara/VIN) — fara apel AI. Format: TARA_PLACA_TIP_NRDOC.
    - CIV fara placa proprie -> mosteneste placa+tara de la fratele cu acelasi VIN din email.
    - non-RO familia VP (un singur doc de inmatriculare) -> fara NRDOC: TARA_PLACA_VP.
    - familia EC (CEMT/COC) pastreaza NRDOC si la tarile non-RO.
    Returneaza str sau None (=> nu e doc de vehicul / lipsesc datele -> fallback la AI)."""
    import re as _re
    code = _vehicle_doc_code(detected_type)
    if not code:
        return None
    tip, nrdoc = code
    data = data if isinstance(data, dict) else {}
    plate = _field_val(data, r"licence plate", r"inmatricul", r"\bplate\b", r"\(a\.\)")
    tara = _field_val(data, r"\bcountry\b", r"\btara\b", r"\bstat\b")
    vin = _field_val(data, r"\bvin\b", r"sasiu", r"chassis", r"\(e\.\)")
    # CIV (si orice doc de vehicul fara placa proprie): mosteneste de la fratele cu acelasi VIN.
    if (not plate) and vin and email_id is not None:
        try:
            sib = db.execute(text(
                "SELECT data FROM document_extractions "
                "WHERE email_id=:e AND attachment_id<>:a "
                "AND data->>'Vin (E.)' = :vin "
                "AND coalesce(data->>'Licence Plate (A.)','') <> '' LIMIT 1"
            ), {"e": email_id, "a": att_id, "vin": vin}).fetchone()
            if sib and sib[0]:
                sd = sib[0]
                if isinstance(sd, str):
                    sd = json.loads(sd)
                if isinstance(sd, dict):
                    plate = _field_val(sd, r"licence plate", r"inmatricul", r"\(a\.\)") or plate
                    tara = tara or _field_val(sd, r"\bcountry\b", r"\btara\b")
        except Exception:
            logger.exception("vehicle sibling-plate att=%s", att_id)
    if not plate:
        return None
    placa = _re.sub(r"[^A-Z0-9]", "", str(plate).upper())  # BH-08-TBM -> BH08TBM
    if not placa:
        return None
    # Tara: din date; altfel inferata din prefixul de judet RO; altfel necunoscut -> fallback.
    if tara:
        tara = _re.sub(r"[^A-Z]", "", str(tara).upper())[:2] or None
    if not tara:
        mm = _re.match(r"^([A-Z]{1,2})\d", placa)
        if mm and mm.group(1) in _RO_COUNTY:
            tara = "RO"
    if not tara:
        return None
    if tara == "RO" or tip == "EC":
        return "%s_%s_%s_%s" % (tara, placa, tip, nrdoc)
    return "%s_%s_%s" % (tara, placa, tip)  # non-RO familia VP -> fara NRDOC


# Denumirea istorica „Proces verbal (CargoBox)" a fost inlocuita de „Anexa 2" (cerere business
# 2026-08-20): documentul vine din documentele de sofer si nu mai are nicio legatura cu un proces
# verbal. `tip_document` primeste NUMELE tipului din `document_types` (vezi apelantii de la
# `_save_extraction` si `reidentify`), iar acel nume poate purta inca varianta veche — fara backfill
# in DB, decizie explicita. Promptul de redenumire are interzis sa produca „proces verbal", dar e un
# prompt AI: garda de mai jos face rezultatul DETERMINIST, in acelasi spirit ca `_vehicle_std_name`
# (introdus tot ca sa scoata AI-ul din calea numelor previzibile).
_PV_LEGACY_RE = _re.compile(r"proces[\s_\-]*verbal(?:[\s_\-]*cargobox)?", _re.IGNORECASE)


def _normalize_legacy_doc_name(name: str) -> str:
    """Nume care conține denumirea istorică „proces verbal" -> exact „anexa 2" (+ extensia).

    Se înlocuiește TOT numele, nu doar tokenul: numele standardizat al acestui document ESTE
    `anexa 2`, iar o substituție locală ar produce dubluri când modelul repetă denumirea din DB
    („Anexa 2 - Proces verbal CargoBox" -> „Anexa 2 - anexa 2"). Documentele de vehicul nu trec
    pe aici (au calea determinista `_vehicle_std_name`), deci nu există nume legitim care să
    conțină tokenul și să aibă nevoie de restul păstrat.
    """
    if not name:
        return name
    if not _PV_LEGACY_RE.search(name):
        return name
    import os as _os
    _, ext = _os.path.splitext(name)
    return "anexa 2" + (ext or "")


def _rename_doc(db, att_id: int, part_no: int, tip_document: str, raw_text: str, att_name: str,
                data=None, email_id=None):
    """Numele 'gata procesat' al documentului, scris in renamed_file SI part_label (campurile pe
    care le citeste exportul CTS). Documentele de VEHICUL -> nume DETERMINIST din datele extrase
    (placa/tara), fara AI (elimina ratarile cand OCR-ul nu prinde placa + timeout-urile de rename).
    Restul (contract / sofer / vehicul fara placa) -> prompt AI pe textul OCR (comportament vechi)."""
    ext = ""
    if att_name:
        import os as _os
        _, ext = _os.path.splitext(att_name)

    # 1) Cale DETERMINISTA pentru documentele de vehicul (placa e deja in `data`).
    renamed = None
    try:
        renamed = _vehicle_std_name(db, email_id, att_id, part_no, tip_document, data)
    except Exception:
        logger.exception("vehicle_std_name att=%s part_no=%s", att_id, part_no)

    # 2) Fallback AI pe textul OCR (contracte/sofer/anexe + vehicul fara placa identificabila).
    if not renamed:
        content = json.dumps({
            "tip_document": tip_document or "",
            "continut": (raw_text or "")[:6000],
            "extensie": ext or ".pdf",
        }, ensure_ascii=False)
        task = "cargo360:doc_rename:%s:%s" % (att_id, part_no)
        res = iris_ai.run_prompt(
            RENAME_SYSTEM_PROMPT, content,
            response_format="json", temperature=0.0, max_tokens=300,
            task=task, timeout=25)
        if res and res.get("ok") and isinstance(res.get("parsed"), dict):
            renamed = (res["parsed"].get("nume_complet") or "").strip() or None
        else:
            logger.warning("rename_doc att=%s part_no=%s AI failed: %s", att_id, part_no, res)

    if not renamed:
        return
    renamed = _normalize_legacy_doc_name(renamed)[:500]
    # Numele final ajunge in AMBELE campuri citite de CTS: renamed_file (canonic) si part_label.
    db.execute(text(
        "UPDATE document_extractions SET renamed_file=:rf, part_label=:rf, updated_at=now() "
        "WHERE attachment_id=:a AND part_no=:pno"
    ), {"rf": renamed, "a": att_id, "pno": int(part_no or 0)})
    db.commit()
    logger.info("renamed att=%s part_no=%s -> %s", att_id, part_no, renamed)


def _extract_doc(system: str, doc_text: str, type_id: int, name: str = None, fields=None):
    """Ruleaza promptul de extragere pe textul documentului. Returneaza (data|None, model|None, err|None).
    `fields` (extract_fields ale tipului) -> normalizeaza cheile la cele canonice din schema."""
    import time
    content = _clip_doc_text(doc_text)
    if not content:
        return None, None, "document fara text extractibil"
    task = "cargo360:doc_extract:%s:%s" % (_slug(name, str(type_id)), _cache_salt(system, content))
    res = None
    _attempts, _ctimeout = _ai_budget()
    for attempt in range(_attempts):
        res = iris_ai.run_prompt(
            system, content, response_format="json", temperature=0.0, max_tokens=700,
            task=task, timeout=_ctimeout)
        if res.get("ok") or (res.get("error") or {}).get("code") not in _RETRY_CODES:
            break
        time.sleep(1.2 * (attempt + 1))
    if res and res.get("ok") and isinstance(res.get("parsed"), dict):
        return _normalize_keys(res["parsed"], fields), res.get("model"), None
    err = ((res.get("error") or {}).get("message") if res else "fail") or "raspuns invalid"
    return None, (res.get("model") if res else None), err


def _salvage_json(s):
    """Extrage primul obiect JSON {...} dintr-un raspuns care poate avea proza/markdown in jur.
    Modelele vision (sonnet) raman vorbarete chiar si cu instructiuni stricte -> extragem noi
    JSON-ul, ca sa nu depindem de parserul strict al gateway-ului (uneori esueaza intermitent)."""
    if not s:
        return None
    import json as _j
    t = str(s).strip()
    m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, _re.S)
    if m:
        try:
            return _j.loads(m.group(1))
        except Exception:
            pass
    i, jx = t.find("{"), t.rfind("}")
    if 0 <= i < jx:
        try:
            return _j.loads(t[i:jx + 1])
        except Exception:
            pass
    return None


def _extract_doc_vision(system: str, files, type_id: int,
                        name: str = None, fields=None, ocr_hint: str = None):
    """Extragere VIZUALA: trimite documentul (una sau mai multe pagini) ca ATASAMENT multimodal
    (sonnet) si extrage campurile direct din imagine. Repara campurile pierdute de OCR (layout talon)
    si 'Este semnat' (semnatura/stampila vizibile). `files` = (path, mime) sau lista de (path, mime)
    (grup: talon fata+spate). `ocr_hint` = text OCR brut deja extras (indiciu optional, neautoritar).
    Returneaza (data|None, model|None, err|None). Nu arunca."""
    import base64
    import hashlib
    import time
    if isinstance(files, tuple):
        files = [files]
    atts, hasher, total = [], hashlib.sha1(), 0
    for p, m in (files or []):
        # `p` poate fi o cale pe disc SAU bytes in memorie (ex. o banda decupata via _crop_to_files).
        if isinstance(p, (bytes, bytearray)):
            raw = bytes(p)
            sz = len(raw)
            amime = (m or "image/jpeg")
        else:
            try:
                sz = os.path.getsize(p)
                with open(p, "rb") as fh:
                    raw = fh.read()
            except Exception:
                continue
            amime = _attachment_mime(p, m)
        if sz > VISION_MAX_BYTES or (total + sz) > VISION_MAX_BYTES:
            continue   # peste limita gateway-ului -> sari pagina (mai bine partial decat esec total)
        total += sz
        hasher.update(raw)
        atts.append({"mime_type": amime,
                     "data_base64": base64.b64encode(raw).decode("ascii")})
    if not atts:
        return None, None, "niciun fisier citibil pentru vision (lipsa/prea mare)"
    if (ocr_hint or "").strip():
        content = ("Text OCR brut din document (POATE fi gresit/incomplet — foloseste DOAR ca indiciu "
                   "pentru caractere greu de citit; imaginea atasata e sursa autoritara):\n"
                   + _clip_doc_text(ocr_hint, 6000))
    else:
        content = "Extrage campurile cerute din documentul atasat."
    # cache: (documente + system) -> acelasi doc+prompt = cache; prompt schimbat = re-ruleaza.
    hasher.update(system.encode("utf-8", "ignore"))
    task = "cargo360:doc_extract_vision:%s:%s" % (_slug(name, str(type_id)), hasher.hexdigest()[:12])
    res = None
    _attempts, _ctimeout = _ai_budget(vision=True)
    for attempt in range(_attempts):
        # response_format='text' + parsare proprie: modelele vision adauga proza chiar si cu
        # response_format='json' (parserul strict al gateway-ului esueaza intermitent — ex. CEMT).
        res = iris_ai.run_prompt(
            system, content, response_format="text", model_hint="sonnet",
            temperature=0.0, max_tokens=900, task=task, timeout=_ctimeout,
            attachments=atts)
        if res.get("ok") or (res.get("error") or {}).get("code") not in _RETRY_CODES:
            break
        time.sleep(1.2 * (attempt + 1))
    if res and (res.get("ok") or res.get("text")):
        parsed = res.get("parsed") if isinstance(res.get("parsed"), dict) else _salvage_json(res.get("text"))
        if isinstance(parsed, dict):
            return _normalize_keys(parsed, fields), res.get("model"), None
    err = ((res.get("error") or {}).get("message") if res else "fail") or "raspuns invalid"
    return None, (res.get("model") if res else None), err


def _norm_bbox(b):
    """Normalizeaza un bbox primit (lista [x,y,w,h] sau dict {x,y,w,h,page}) la dict cu fractii
    0..1 si page intreg, sau None daca invalid/acopera tot. Pur, fara IO."""
    try:
        if isinstance(b, (list, tuple)) and len(b) >= 4:
            x, y, w, h = float(b[0]), float(b[1]), float(b[2]), float(b[3])
            page = int(b[4]) if len(b) >= 5 else 0
        elif isinstance(b, dict):
            x, y = float(b.get("x", 0.0)), float(b.get("y", 0.0))
            w, h = float(b.get("w", 1.0)), float(b.get("h", 1.0))
            page = int(b.get("page", 0) or 0)
        else:
            return None
    except Exception:
        return None
    x = max(0.0, min(1.0, x)); y = max(0.0, min(1.0, y))
    w = max(0.0, min(1.0 - x, w)); h = max(0.0, min(1.0 - y, h))
    if w <= 0.01 or h <= 0.01:
        return None
    # banda care acopera (aproape) tot, pe pagina 0 -> ca si cum n-ar avea bbox
    if page == 0 and x <= 0.01 and y <= 0.01 and w >= 0.98 and h >= 0.98:
        return None
    return {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4), "page": max(0, page)}


def _crop_to_files(path: str, mime: str, bbox):
    """Decupeaza atasamentul la o banda/regiune (bbox = {page,x,y,w,h} fractii 0..1) si intoarce
    o LISTA [(bytes, mime)] pentru _extract_doc_vision / preview. bbox None/invalid -> [(path, mime)]
    (tot atasamentul). Imagine: PIL crop (EXIF aplicat). PDF: randeaza pagina (PyMuPDF) si decupeaza.
    Fail-safe: la orice eroare cade pe atasamentul intreg."""
    nb = _norm_bbox(bbox)
    if not nb:
        # Fara regiune: pentru un PDF prea mare ca atasament brut (peste limita gateway-ului vision),
        # randeaza paginile la imagini (mărginite) — altfel _extract_doc_vision sare fisierul intreg
        # si extragerea esueaza („niciun fisier citibil"). PDF mic -> ramane brut (text nativ, ieftin).
        _m = (mime or "").lower()
        _ext = (os.path.splitext(path or "")[1] or "").lower()
        if "pdf" in _m or _ext == ".pdf":
            try:
                _sz = os.path.getsize(path)
            except Exception:
                _sz = 0
            if _sz > VISION_MAX_BYTES:
                _pc = _pdf_page_count(path) or 1
                return _render_page_range(path, mime, 0, min(MAX_PAGES, _pc) - 1)
        return [(path, mime)]
    m = (mime or "").lower()
    ext = (os.path.splitext(path or "")[1] or "").lower()
    is_pdf = ("pdf" in m) or ext == ".pdf"
    try:
        import io
        if is_pdf:
            import fitz  # PyMuPDF
            from PIL import Image
            d = fitz.open(path)
            page = max(0, min(nb["page"], d.page_count - 1))
            pix = d[page].get_pixmap(matrix=fitz.Matrix(2.5, 2.5))
            img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
            d.close()
        else:
            from PIL import Image, ImageOps
            img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        W, H = img.size
        box = (int(nb["x"] * W), int(nb["y"] * H),
               int((nb["x"] + nb["w"]) * W), int((nb["y"] + nb["h"]) * H))
        buf = io.BytesIO()
        img.crop(box).save(buf, format="JPEG", quality=90)
        return [(buf.getvalue(), "image/jpeg")]
    except Exception as e:
        logger.warning("crop bbox esuat (%s): %s", path, e)
        return [(path, mime)]


# ── Helpers DB ──────────────────────────────────────────────────────────────
def _get_type(db: Session, type_id: int):
    r = db.execute(text("SELECT * FROM document_types WHERE id=:id AND status='active'"),
                   {"id": type_id}).fetchone()
    return dict(r._mapping) if r else None


# ════════════════════════════════════════════════════════════════════════════
# CRUD tipuri de documente
# ════════════════════════════════════════════════════════════════════════════
@router.get("/documents/types")
def list_types(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    rows = db.execute(text(
        "SELECT id, category, name, description, sample_name, sample_mime, "
        "       extract_fields, extract_prompt, detect_prompt, match_titles, enabled, "
        "       extract_via_vision, identify_only, iris_template_id, iris_synced_at, "
        "       (sample_path IS NOT NULL) AS has_sample, "
        "       jsonb_array_length(COALESCE(extract_fields,'[]'::jsonb)) AS field_count, "
        "       created_at, updated_at "
        "FROM document_types WHERE status='active' ORDER BY category, lower(name)")).fetchall()
    return {"ok": True, "types": [dict(r._mapping) for r in rows]}


@router.post("/documents/types")
def create_type(body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    cat = (body.get("category") or "").strip().lower()
    name = (body.get("name") or "").strip()
    if cat not in ALLOWED_CATS:
        raise HTTPException(400, "Categorie invalida (vehicul|sofer|contract)")
    if not name:
        raise HTTPException(400, "Numele tipului e obligatoriu")
    try:
        r = db.execute(text(
            "INSERT INTO document_types (category, name, description, created_by) "
            "VALUES (:c, :n, :d, :u) RETURNING id"),
            {"c": cat, "n": name, "d": (body.get("description") or "").strip() or None,
             "u": (admin.get("username") or admin.get("email"))}).fetchone()
        db.commit()
    except Exception as e:
        db.rollback()
        if "uq_document_types_cat_name" in str(e):
            raise HTTPException(409, "Exista deja un tip cu acest nume in categoria asta")
        raise HTTPException(500, "Eroare la creare: " + str(e)[:200])
    iris_docsvc.bg_sync_types()  # OPS-0122: IRIS la zi (Cargo360 = sursa)
    return {"ok": True, "id": r[0]}


@router.put("/documents/types/{type_id}")
def update_type(type_id: int, body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    if not _get_type(db, type_id):
        raise HTTPException(404, "Tip inexistent")
    sets, params = [], {"id": type_id}
    for col, key in (("name", "name"), ("description", "description"),
                     ("extract_prompt", "extract_prompt"), ("detect_prompt", "detect_prompt")):
        if key in body:
            sets.append(col + " = :" + col)
            params[col] = body.get(key)
    for col in ("extract_fields", "match_titles"):
        if col in body:
            sets.append(col + " = CAST(:" + col + " AS jsonb)")
            params[col] = json.dumps(body.get(col) or [])
    if "enabled" in body:
        sets.append("enabled = :enabled")
        params["enabled"] = bool(body.get("enabled"))
    if "extract_via_vision" in body:
        sets.append("extract_via_vision = :eviv")
        params["eviv"] = bool(body.get("extract_via_vision"))
    if "identify_only" in body:
        sets.append("identify_only = :ido")
        params["ido"] = bool(body.get("identify_only"))
    if not sets:
        return {"ok": True, "updated": 0}
    sets.append("updated_at = now()")
    db.execute(text("UPDATE document_types SET " + ", ".join(sets) + " WHERE id=:id"), params)
    db.commit()
    iris_docsvc.bg_sync_types()  # OPS-0122
    return {"ok": True, "updated": 1}


@router.delete("/documents/types/{type_id}")
def delete_type(type_id: int, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    n = db.execute(text("UPDATE document_types SET status='deleted', updated_at=now() "
                        "WHERE id=:id AND status='active'"), {"id": type_id}).rowcount
    db.commit()
    if not n:
        raise HTTPException(404, "Tip inexistent")
    iris_docsvc.bg_sync_types()  # OPS-0122
    return {"ok": True, "deleted": type_id}


# ════════════════════════════════════════════════════════════════════════════
# Sablon: upload + servire pentru preview
# ════════════════════════════════════════════════════════════════════════════
@router.post("/documents/types/{type_id}/sample")
def upload_sample(type_id: int, file: UploadFile = File(...),
                  db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    t = _get_type(db, type_id)
    if not t:
        raise HTTPException(404, "Tip inexistent")
    ext = (os.path.splitext(file.filename or "")[1] or "").lower()[:10]
    os.makedirs(TEMPLATE_DIR, exist_ok=True)
    fname = str(type_id) + "_" + uuid.uuid4().hex + ext
    dest = os.path.join(TEMPLATE_DIR, fname)
    try:
        data = file.file.read()
        if len(data) > 25 * 1024 * 1024:
            raise HTTPException(413, "Fisier prea mare (max 25MB)")
        with open(dest, "wb") as f:
            f.write(data)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, "Salvare esuata: " + str(e)[:200])
    # sterge sablonul vechi daca exista
    old = t.get("sample_path")
    db.execute(text("UPDATE document_types SET sample_path=:p, sample_name=:n, sample_mime=:m, "
                    "updated_at=now() WHERE id=:id"),
               {"p": dest, "n": (file.filename or "")[:500],
                "m": (file.content_type or "")[:200], "id": type_id})
    db.commit()
    if old and old != dest and os.path.exists(old):
        try:
            os.remove(old)
        except Exception:
            pass
    return {"ok": True, "name": file.filename, "mime": file.content_type}


@router.get("/documents/types/{type_id}/sample")
def get_sample(type_id: int, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    t = _get_type(db, type_id)
    if not t or not t.get("sample_path") or not os.path.exists(t["sample_path"]):
        raise HTTPException(404, "Fara sablon")
    return FileResponse(t["sample_path"], media_type=(t.get("sample_mime") or "application/octet-stream"),
                        filename=(t.get("sample_name") or os.path.basename(t["sample_path"])))


# ════════════════════════════════════════════════════════════════════════════
# Export tipuri documente (pentru import in IRIS)
# ════════════════════════════════════════════════════════════════════════════

@router.get("/documents/types/export")
def export_types(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Exporta toate tipurile active intr-un format importabil in IRIS."""
    rows = db.execute(text(
        "SELECT id AS source_id, category, name, description, "
        "       extract_fields, extract_prompt, detect_prompt, match_titles, "
        "       extract_via_vision, identify_only, sample_name, sample_mime "
        "FROM document_types WHERE status='active' ORDER BY category, id"
    )).fetchall()
    types = [dict(r._mapping) for r in rows]
    for t in types:
        t["source_app"] = "mailguard"
        t["apps"] = ["mailguard"]
    return {"ok": True, "count": len(types), "types": types}


@router.post("/documents/types/sync-iris")
def sync_types_iris(wait: bool = False, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """OPS-2026-0122: push catalogul de tipuri in IRIS + backfill iris_template_id.
    Async by default (daemon thread); ?wait=true ruleaza sincron (debug)."""
    if not iris_docsvc.is_configured():
        raise HTTPException(503, "IRIS docsvc neconfigurat (lipsa IRIS_AI_KEY / iris_api_url)")
    if wait:
        return iris_docsvc.sync_types_guarded()
    iris_docsvc.bg_sync_types()
    return {"ok": True, "started": True, "async": True,
            "message": "Sync tipuri pornit in fundal. Vezi /documents/types/sync-status."}


@router.get("/documents/types/sync-status")
def sync_types_status(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    r = db.execute(text("SELECT value, updated_at FROM settings WHERE key='doc_sync.last_result'")).fetchone()
    agg = db.execute(text(
        "SELECT count(*) FILTER (WHERE iris_template_id IS NOT NULL) AS mapped, "
        "       count(*) AS total FROM document_types WHERE status='active'")).fetchone()
    return {"ok": True,
            "last_result": (r._mapping["value"] if r else None),
            "updated_at": (r._mapping["updated_at"] if r else None),
            "mapped": (agg._mapping["mapped"] if agg else 0),
            "total": (agg._mapping["total"] if agg else 0)}


# ════════════════════════════════════════════════════════════════════════════
# AI: genereaza prompt + testeaza extragerea pe sablon
# ════════════════════════════════════════════════════════════════════════════
# ════════════════════════════════════════════════════════════════════════════
# OPS-2026-0122: comparatie extractie locala vs IRIS (/documents/extract central)
# ════════════════════════════════════════════════════════════════════════════
_COMPARE_LOCK = threading.Lock()


def _cmp_norm_val(v):
    if v is None or v == "" or v == [] or v == {}:
        return ""
    return _re.sub(r"\s+", " ", str(v).strip().lower())


def _cmp_vals_equal(a, b):
    return _cmp_norm_val(a) == _cmp_norm_val(b)


def _set_setting_json(db, key, obj):
    db.execute(text(
        "INSERT INTO settings(key, value, updated_at) VALUES(:k, CAST(:v AS jsonb), now()) "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()"),
        {"k": key, "v": json.dumps(obj, default=str)})
    db.commit()


def _load_extraction_for_compare(db, ex_id):
    r = db.execute(text(
        "SELECT d.id AS ex_id, d.attachment_id, d.part_no, d.document_type_id, d.category, "
        "       d.confidence AS local_conf, d.method AS local_method, d.raw_text AS raw_text, "
        "       d.data AS local_data, d.part_bbox AS part_bbox, "
        "       a.name AS att_name, a.content_type, a.storage_path "
        "FROM document_extractions d JOIN attachments a ON a.id=d.attachment_id "
        "WHERE d.id=:id"), {"id": ex_id}).fetchone()
    return dict(r._mapping) if r else None


def _run_extract_compare(db, ex, created_by=None):
    """Compara datele LOCALE stocate (incumbent) vs extractia IRIS proaspata, pe acelasi fisier.
    Salveaza un rand in doc_extract_comparisons si intoarce side-by-side + diff per camp."""
    import time as _t
    t = _get_type(db, ex.get("document_type_id")) if ex.get("document_type_id") else None
    if not t:
        return {"ok": False, "error": "extragere fara tip valid (nu pot compara)"}
    fields = t.get("extract_fields") or []
    local_data = ex.get("local_data") or {}
    path = _host_path(ex.get("storage_path"))
    mime = _attachment_mime(path, ex.get("content_type"))
    fname = ex.get("att_name") or "document"
    fbytes, fmime = None, mime
    try:
        files = _crop_to_files(path, mime, ex.get("part_bbox"))
        first = files[0] if files else (path, mime)
        if isinstance(first[0], (bytes, bytearray)):
            fbytes, fmime = bytes(first[0]), first[1]
        else:
            with open(first[0], "rb") as fh:
                fbytes = fh.read()
            fmime = mime
    except Exception as e:
        return {"ok": False, "error": "nu pot citi fisierul: " + str(e)[:160]}
    fbytes, fmime = _to_pdf_compressed(fbytes, fmime)
    t0 = _t.time()
    res = iris_docsvc.extract_document(t.get("iris_template_id"), fbytes, fname, fmime)
    iris_ms = int((_t.time() - t0) * 1000)
    iris_ok = bool(res.get("ok")) and isinstance(res.get("data"), dict)
    iris_err = None if iris_ok else (res.get("error") or "extractie IRIS esuata")
    iris_data = _normalize_keys(res.get("data"), fields) if iris_ok else None
    iris_conf = res.get("confidence") if isinstance(res.get("confidence"), (int, float)) else None
    iris_method = res.get("method")
    total, match, diff = 0, 0, []
    for f in fields:
        k = f.get("name")
        if not k:
            continue
        total += 1
        lv = local_data.get(k)
        iv = (iris_data or {}).get(k)
        if _cmp_vals_equal(lv, iv):
            match += 1
        else:
            diff.append({"field": k, "local": lv, "iris": iv})
    db.execute(text(
        "INSERT INTO doc_extract_comparisons "
        "(extraction_attachment_id, part_no, type_id, category, type_name, "
        " local_data, iris_data, fields_total, fields_match, fields_diff, "
        " local_conf, iris_conf, local_ms, iris_ms, local_method, iris_method, iris_error, created_by) "
        "VALUES (:aid,:pno,:tid,:cat,:tname, CAST(:ld AS jsonb),CAST(:idd AS jsonb),:ft,:fm,CAST(:fd AS jsonb),"
        " :lc,:ic,:lms,:ims,:lm,:im,:ierr,:cby)"),
        {"aid": ex.get("attachment_id"), "pno": int(ex.get("part_no") or 0),
         "tid": ex.get("document_type_id"), "cat": (t.get("category") or ex.get("category")),
         "tname": t.get("name"),
         "ld": json.dumps(local_data, default=str),
         "idd": (json.dumps(iris_data, default=str) if iris_data is not None else None),
         "ft": total, "fm": match, "fd": json.dumps(diff, default=str),
         "lc": ex.get("local_conf"), "ic": iris_conf, "lms": None, "ims": iris_ms,
         "lm": ex.get("local_method"), "im": iris_method, "ierr": iris_err,
         "cby": created_by})
    db.commit()
    return {"ok": True, "ex_id": ex.get("ex_id"), "type_name": t.get("name"),
            "category": (t.get("category") or ex.get("category")),
            "fields_total": total, "fields_match": match, "fields_diff": diff,
            "local_data": local_data, "iris_data": iris_data,
            "local_conf": ex.get("local_conf"), "iris_conf": iris_conf,
            "local_method": ex.get("local_method"), "iris_method": iris_method,
            "iris_ms": iris_ms, "iris_error": iris_err}


@router.post("/documents/extract-compare")
def extract_compare(body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    ex_id = body.get("ex_id") or body.get("extraction_id")
    if not ex_id:
        raise HTTPException(400, "ex_id obligatoriu")
    ex = _load_extraction_for_compare(db, int(ex_id))
    if not ex:
        raise HTTPException(404, "Extragere inexistenta")
    if not ex.get("document_type_id"):
        raise HTTPException(400, "Extragerea nu are tip — nu pot compara")
    if not iris_docsvc.is_configured():
        raise HTTPException(503, "IRIS docsvc neconfigurat")
    res = _run_extract_compare(db, ex, created_by=(admin.get("username") or admin.get("email")))
    if not res.get("ok"):
        raise HTTPException(502, res.get("error") or "comparatie esuata")
    return res


def _compare_batch_worker(ex_ids, created_by):
    from app.database import SessionLocal
    done, failed = 0, 0
    db = SessionLocal()
    try:
        for xid in ex_ids:
            try:
                ex = _load_extraction_for_compare(db, xid)
                if not ex or not ex.get("document_type_id"):
                    failed += 1
                    continue
                r = _run_extract_compare(db, ex, created_by=created_by)
                done += 1 if r.get("ok") else 0
                failed += 0 if r.get("ok") else 1
            except Exception:
                logger.exception("compare batch item %s failed", xid)
                failed += 1
                try:
                    db.rollback()
                except Exception:
                    pass
        try:
            _set_setting_json(db, "doc_compare.last_batch",
                              {"status": "ok", "done": done, "failed": failed, "total": len(ex_ids)})
        except Exception:
            logger.exception("compare batch state write failed")
    finally:
        db.close()
        try:
            _COMPARE_LOCK.release()
        except Exception:
            pass


@router.post("/documents/extract-compare/batch")
def extract_compare_batch(body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    if not iris_docsvc.is_configured():
        raise HTTPException(503, "IRIS docsvc neconfigurat")
    cat = body.get("category")
    tid = body.get("type_id")
    limit = max(1, min(int(body.get("limit") or 15), 60))
    q = ("SELECT d.id FROM document_extractions d JOIN document_types t ON t.id=d.document_type_id "
         "WHERE d.status='extracted' AND d.document_type_id IS NOT NULL "
         "AND t.iris_template_id IS NOT NULL AND COALESCE(d.part_no,0)=0 AND d.data IS NOT NULL")
    params = {}
    if cat in ("vehicul", "sofer", "contract"):
        q += " AND d.category=:cat"
        params["cat"] = cat
    if tid:
        q += " AND d.document_type_id=:tid"
        params["tid"] = int(tid)
    q += " ORDER BY d.updated_at DESC NULLS LAST LIMIT :lim"
    params["lim"] = limit
    ids = [r[0] for r in db.execute(text(q), params).fetchall()]
    if not ids:
        return {"ok": True, "started": False,
                "message": "Niciun document eligibil (extras, cu tip mapat in IRIS)."}
    if not _COMPARE_LOCK.acquire(blocking=False):
        raise HTTPException(409, "O comparatie pe lot ruleaza deja")
    _set_setting_json(db, "doc_compare.last_batch", {"status": "running", "total": len(ids)})
    created_by = (admin.get("username") or admin.get("email"))
    threading.Thread(target=_compare_batch_worker, args=(ids, created_by), daemon=True).start()
    return {"ok": True, "started": True, "count": len(ids),
            "message": "Comparatie pe " + str(len(ids)) + " documente pornita in fundal."}


@router.get("/documents/extract-compare/results")
def extract_compare_results(category: str = None, limit: int = 50,
                            db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    params = {}
    where = ""
    if category in ("vehicul", "sofer", "contract"):
        where = " WHERE category=:cat"
        params["cat"] = category
    summ = db.execute(text(
        "SELECT category, count(*) AS n, COALESCE(sum(fields_match),0) AS m, "
        "       COALESCE(sum(fields_total),0) AS tot, "
        "       count(*) FILTER (WHERE COALESCE(jsonb_array_length(fields_diff),0)=0 AND iris_error IS NULL) AS identice, "
        "       count(*) FILTER (WHERE iris_error IS NOT NULL) AS erori, "
        "       round(avg(iris_conf)::numeric,3) AS avg_iris_conf, round(avg(iris_ms)) AS avg_iris_ms "
        "FROM doc_extract_comparisons" + where + " GROUP BY category ORDER BY category"), params).fetchall()
    topf = db.execute(text(
        "SELECT e->>'field' AS field, count(*) AS divergente "
        "FROM doc_extract_comparisons c, jsonb_array_elements(COALESCE(c.fields_diff,'[]'::jsonb)) e" +
        (" WHERE c.category=:cat" if category in ("vehicul", "sofer", "contract") else "") +
        " GROUP BY 1 ORDER BY 2 DESC LIMIT 15"), params).fetchall()
    rows = db.execute(text(
        "SELECT id, extraction_attachment_id, part_no, type_id, category, type_name, "
        "       fields_total, fields_match, fields_diff, local_data, iris_data, "
        "       local_conf, iris_conf, iris_ms, iris_method, iris_error, created_at "
        "FROM doc_extract_comparisons" + where + " ORDER BY created_at DESC LIMIT :lim"),
        dict(params, lim=max(1, min(int(limit or 50), 200)))).fetchall()
    state = db.execute(text("SELECT value FROM settings WHERE key='doc_compare.last_batch'")).fetchone()
    return {"ok": True,
            "summary": [dict(r._mapping) for r in summ],
            "top_fields": [dict(r._mapping) for r in topf],
            "rows": [dict(r._mapping) for r in rows],
            "batch_state": (state._mapping["value"] if state else None)}


# ── OPS-2026-0122: motor extractie comutabil (local | shadow | iris) ──────────
def _doc_engine(db):
    try:
        r = db.execute(text("SELECT value #>> '{}' AS v FROM settings WHERE key='doc_extract.engine'")).fetchone()
        v = (r._mapping["v"] if r else None) or "local"
        return v if v in ("local", "shadow", "iris") else "local"
    except Exception:
        return "local"


def _iris_multifile_enabled(db) -> bool:
    """True daca trimitem si segmentele MULTI-PAGINA la IRIS (necesita endpoint IRIS multi-fisier).
    Ramane False pana IRIS suporta pages[] (outbox #16); pana atunci multi-pagina ramane local."""
    try:
        r = db.execute(text("SELECT value #>> '{}' AS v FROM settings WHERE key='doc_extract.iris_multifile'")).fetchone()
        return bool(r and str(r._mapping["v"]).strip().lower() == "true")
    except Exception:
        return False


def _type_extracts(t) -> bool:
    """True daca tipul cere EXTRAGERE de date (vs. DOAR identificare/clasificare).
    Sursa autoritara = coloana identify_only; fallback pe prezenta extract_prompt + extract_fields.
    Tipurile fara extragere (ex. 'Formular de Inregistrare a Vehiculelor (Anexa)') NU se trimit la IRIS."""
    if not t or t.get("identify_only"):
        return False
    return bool((t.get("extract_prompt") or "").strip() and (t.get("extract_fields") or []))



_MAX_BYTES = 1_600_000  # 1.6 MB
# Plafon pentru re-randarea paginilor (treapta cea mai scumpa din _to_pdf_compressed).
# Masurat pe staging: ~1.2 s/pagina => 10 pag = 12 s, 30 pag = 35 s, 60 pag = 72 s. Gunicorn are
# --timeout 60, deci peste ~50 pagini workerul e omorat inainte sa termine. 40 lasa marja.
_RENDER_MAX_PAGES = 40
# Peste atatea caractere de text pe primele pagini consideram PDF-ul "nativ" (nu scanat) si NU-l
# rasterizam — am pierde textul cautabil, ireversibil.
_NATIVE_TEXT_MIN = 200

def _to_pdf_compressed(file_bytes: bytes, mime: str):
    """Converteste imagine -> PDF si/sau comprima PDF > 1.6MB.
    Returneaza (bytes, mime_nou). La orice eroare returneaza originalul neschimbat."""
    import io
    try:
        import fitz
    except ImportError:
        return file_bytes, mime

    try:
        m = (mime or "").lower()
        is_image = m.startswith("image/") or m in ("image/jpeg", "image/png", "image/gif", "image/webp", "image/tiff")
        is_pdf = "pdf" in m

        if is_image:
            # Imagine -> PDF nou cu compresie.
            # `show_pdf_page` cere un PDF ca SURSA; un document deschis cu filetype="image" nu e PDF
            # (is_pdf=False) si arunca "ValueError: is no PDF" pe PyMuPDF >= 1.24 — conversia esua
            # tacut si se trimitea originalul. Deci convertim intai imaginea la PDF.
            img_doc = fitz.open(stream=file_bytes, filetype="image")
            try:
                _pdf_bytes = img_doc.convert_to_pdf()
            finally:
                img_doc.close()
            doc = fitz.open(stream=_pdf_bytes, filetype="pdf")
            out = doc.tobytes(deflate=True, garbage=4, clean=True)
            doc.close()
            # Daca tot > 1.6MB, recomprima cu DPI redus via PIL
            if len(out) > _MAX_BYTES:
                try:
                    from PIL import Image
                    img = Image.open(io.BytesIO(file_bytes))
                    if img.mode not in ("RGB", "L"):
                        img = img.convert("RGB")
                    # Scade intai calitatea JPEG; daca tot nu incape (scan la rezolutie mare),
                    # reduce si dimensiunea in pixeli — altfel un scan A4 la 600dpi ramane peste prag
                    # oricat de mult am cobori calitatea.
                    for scale, quality in ((1.0, 85), (1.0, 70), (1.0, 55),
                                           (0.75, 70), (0.6, 65), (0.45, 60), (0.35, 55)):
                        im = img
                        if scale < 1.0:
                            im = img.resize((max(1, int(img.width * scale)),
                                             max(1, int(img.height * scale))), Image.LANCZOS)
                        buf = io.BytesIO()
                        im.save(buf, format="JPEG", quality=quality, optimize=True)
                        buf.seek(0)
                        img2 = fitz.open(stream=buf.read(), filetype="jpg")
                        try:
                            _p2 = img2.convert_to_pdf()   # vezi nota de mai sus: show_pdf_page cere PDF sursa
                        finally:
                            img2.close()
                        doc2 = fitz.open(stream=_p2, filetype="pdf")
                        out2 = doc2.tobytes(deflate=True, garbage=4, clean=True)
                        doc2.close()
                        if len(out2) <= _MAX_BYTES:
                            out = out2
                            break
                        out = out2   # pastreaza cea mai mica varianta obtinuta pana acum
                except Exception:
                    logger.warning("_to_pdf_compressed: recompresie imagine esuata", exc_info=True)
            return out, "application/pdf"

        elif is_pdf and len(file_bytes) > _MAX_BYTES:
            # PDF existent prea mare -> recomprima
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            out = doc.tobytes(deflate=True, garbage=4, clean=True)
            doc.close()
            # Daca inca > 1.6MB, sterge metadate si optimizeaza mai agresiv
            if len(out) > _MAX_BYTES:
                doc = fitz.open(stream=out, filetype="pdf")
                out = doc.tobytes(deflate=True, garbage=4, clean=True, no_new_id=True)
                doc.close()
            # Ultima treapta: un PDF cu pagini SCANATE nu scade din `deflate` (imaginile sunt deja
            # comprimate). Re-randam paginile ca JPEG la rezolutie redusa.
            #
            # DOUA gardieni, ambii masurati pe serverul de staging:
            #  1) COST: re-randarea e ~1.2 s/pagina. Rulam SINCRON intr-un endpoint de polling, iar
            #     gunicorn are --timeout 60 cu 4 workeri: un PDF de 60 pagini (72 s masurat) omoara
            #     workerul, CTS primeste eroare, reincearca, si asa mai departe pana cade tot API-ul.
            #     Peste plafon preferam sa livram fisierul mare si sa lasam CTS sa-l refuze vizibil.
            #  2) TEXT NATIV: rasterizarea distruge ireversibil stratul de text. Pe un contract PDF
            #     nativ asta inseamna ca CTS nu mai poate cauta/copia text. Il rasterizam doar daca
            #     documentul e efectiv scanat (fara strat de text).
            if len(out) > _MAX_BYTES:
                _skip_reason = None
                try:
                    _probe = fitz.open(stream=out, filetype="pdf")
                    _pages = _probe.page_count
                    _txt = sum(len(_probe[i].get_text() or "")
                               for i in range(min(_pages, 5)))
                    _probe.close()
                    if _pages > _RENDER_MAX_PAGES:
                        _skip_reason = "prea multe pagini (%d > %d) — risc de timeout" % (
                            _pages, _RENDER_MAX_PAGES)
                    elif _txt > _NATIVE_TEXT_MIN:
                        _skip_reason = "PDF cu text nativ (%d caractere) — rasterizarea ar distruge textul" % _txt
                except Exception:
                    _skip_reason = "nu pot inspecta PDF-ul"
                if _skip_reason:
                    logger.warning("_to_pdf_compressed: sar peste re-randare — %s (%d bytes)",
                                   _skip_reason, len(out))
                    return out, "application/pdf"

                best = out
                for zoom, quality in ((1.4, 70), (1.0, 65), (0.8, 60), (0.6, 55)):
                    try:
                        src = fitz.open(stream=out, filetype="pdf")
                        dst = fitz.open()
                        for pg in src:
                            pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
                            jpg = pix.tobytes("jpg", jpg_quality=quality)
                            ip = fitz.open(stream=jpg, filetype="jpg")
                            try:
                                pdf_page = fitz.open(stream=ip.convert_to_pdf(), filetype="pdf")
                            finally:
                                ip.close()
                            dst.insert_pdf(pdf_page)
                            pdf_page.close()
                        cand = dst.tobytes(deflate=True, garbage=4, clean=True)
                        dst.close()
                        src.close()
                        # Pastram doar daca CHIAR e mai mic: re-randarea unui PDF deja comprimat
                        # agresiv poate produce un fisier mai MARE decat originalul.
                        if len(cand) < len(best):
                            best = cand
                        if len(best) <= _MAX_BYTES:
                            break
                    except Exception:
                        logger.warning("_to_pdf_compressed: re-randare PDF esuata (zoom=%s)", zoom,
                                       exc_info=True)
                        break
                out = best
            return out, "application/pdf"

        return file_bytes, mime
    except Exception as e:
        logger.warning("_to_pdf_compressed failed (%s), sending original", e)
        return file_bytes, mime


def _extract_via_iris(t, path, mime, doc_text, *, bbox=None, files=None):
    """Extragere prin IRIS /documents/extract.
    Returneaza (data|None, model|None, err|None, conf|None, method|None).
    - bbox: decupaj (banda taiata) -> trimite imaginea decupata;
    - files: lista explicita (path_or_bytes, mime) pt segment MULTI-PAGINA (gated de _iris_multifile_enabled);
      prima pagina merge ca file_content, restul ca 'pages' (suport IRIS in lucru — outbox #16)."""
    iris_tpl = t.get("iris_template_id") if t else None
    if not iris_tpl:
        return None, None, "tip nesincronizat in IRIS (fara iris_template_id)", None, None
    try:
        flist = files if files is not None else _crop_to_files(path, mime, bbox)
        if not flist:
            flist = [(path, mime)]

        def _read(item):
            if isinstance(item[0], (bytes, bytearray)):
                return bytes(item[0]), item[1]
            with open(item[0], "rb") as fh:
                return fh.read(), (item[1] or mime)
        fbytes, fmime = _read(flist[0])
        fbytes, fmime = _to_pdf_compressed(fbytes, fmime)
        extra = [base64.b64encode(_read(it)[0]).decode("ascii") for it in flist[1:]] or None
    except Exception as e:
        return None, None, "citire fisier esuata: " + str(e)[:120], None, None
    res = iris_docsvc.extract_document(iris_tpl, fbytes, os.path.basename(path or "document"), fmime,
                                       extra_files=extra)
    if res.get("ok") and isinstance(res.get("data"), dict):
        data = _normalize_keys(res.get("data"), (t.get("extract_fields") or []))
        c = res.get("confidence")
        conf = max(0.0, min(1.0, float(c))) if isinstance(c, (int, float)) else None
        return data, (res.get("model") or "iris"), None, conf, ((res.get("method") or "iris"))[:20]
    return None, (res.get("model") or "iris"), (res.get("error") or "extractie IRIS esuata"), None, None


def _local_extract_dispatch(t, path, mime, doc_text, type_id, *, bbox=None, files=None, force_vision=False):
    """Extragere LOCALA (engine propriu) pt UN document/parte/segment.
    Returneaza (data|None, model|None, err|None).
    - files: lista explicita (path_or_bytes, mime) -> VIZUAL pe acele fisiere (grup/segment multi-pagina);
    - bbox / force_vision / extract_via_vision -> VIZUAL pe decupaj (_crop_to_files);
    - altfel text OCR -> TEXT; fara text si fara vision -> (None, None, motiv)."""
    fields = t.get("extract_fields") or []
    if files is not None:
        sys_v = _build_doc_extract_system_vision(t["extract_prompt"], fields)
        return _extract_doc_vision(sys_v, files, type_id, t.get("name"), fields=fields, ocr_hint=doc_text)
    if force_vision or bbox is not None or t.get("extract_via_vision"):
        sys_v = _build_doc_extract_system_vision(t["extract_prompt"], fields)
        return _extract_doc_vision(sys_v, _crop_to_files(path, mime, bbox), type_id, t.get("name"),
                                   fields=fields, ocr_hint=doc_text)
    if (doc_text or "").strip():
        sys_t = _build_doc_extract_system(t["extract_prompt"], fields)
        return _extract_doc(sys_t, doc_text, type_id, t.get("name"), fields=fields)
    return None, None, "fara text OCR si fara vision"


def _extract_fields(db, t, path, mime, doc_text, type_id, *, bbox=None, files=None, force_vision=False):
    """Punct UNIC de extragere campuri, constient de motor (local|shadow|iris).
    Returneaza (data|None, model|None, err|None, conf|None, method|None) — conf/method populate doar de IRIS.
    Apelantul detine guard-ul _type_extracts (tipurile doar-identificare nu ajung aici).
    Pe 'iris': IRIS primar + FALLBACK local la esec NON-tranzitoriu; eroarea tranzitorie e propagata
    (apelantul pastreaza 'retry_transient'). Segmentele multi-pagina (files=...) raman LOCALE pana cand
    _iris_multifile_enabled (endpoint IRIS multi-fisier)."""
    eng = _doc_engine(db)
    multifile = files is not None
    use_iris = (eng == "iris") and (not multifile or _iris_multifile_enabled(db))
    if use_iris:
        data, model, err, conf, method = _extract_via_iris(t, path, mime, doc_text, bbox=bbox, files=files)
        if data is None and not _is_transient_ai_err(err):
            logger.info("iris->local fallback type=%s: %s", type_id, (err or "")[:120])
            ldata, lmodel, lerr = _local_extract_dispatch(
                t, path, mime, doc_text, type_id, bbox=bbox, files=files, force_vision=force_vision)
            return ldata, lmodel, lerr, None, "local-fallback"
        return data, model, err, conf, method
    data, model, err = _local_extract_dispatch(
        t, path, mime, doc_text, type_id, bbox=bbox, files=files, force_vision=force_vision)
    return data, model, err, None, None


def _shadow_enqueue(attachment_id, part_no):
    """Mod shadow: ruleaza IRIS in fundal pe documentul deja procesat local + logheaza comparatia."""
    def _w():
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            r = db.execute(text("SELECT id FROM document_extractions WHERE attachment_id=:a AND part_no=:p"),
                           {"a": attachment_id, "p": int(part_no or 0)}).fetchone()
            if not r:
                return
            ex = _load_extraction_for_compare(db, r._mapping["id"])
            if ex and ex.get("document_type_id"):
                _run_extract_compare(db, ex, created_by="shadow")
        except Exception:
            logger.exception("shadow compare failed")
        finally:
            db.close()
    threading.Thread(target=_w, daemon=True).start()


@router.get("/documents/extract-engine")
def get_extract_engine(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    r = db.execute(text("SELECT value #>> '{}' AS v FROM settings WHERE key='doc_extract.engine'")).fetchone()
    return {"ok": True, "engine": (r._mapping["v"] if r and r._mapping["v"] else "local")}


@router.post("/documents/extract-engine")
def set_extract_engine(body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    eng = (body.get("engine") or "").strip().lower()
    if eng not in ("local", "shadow", "iris"):
        raise HTTPException(400, "engine invalid (local|shadow|iris)")
    _set_setting_json(db, "doc_extract.engine", eng)
    return {"ok": True, "engine": eng}


@router.post("/documents/types/{type_id}/generate-extract-prompt")
def generate_extract_prompt(type_id: int, body: dict,
                            db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    t = _get_type(db, type_id)
    if not t:
        raise HTTPException(404, "Tip inexistent")
    fields = body.get("fields") or []
    if not _fields_keys(fields):
        raise HTTPException(400, "Adauga cel putin un camp cu nume")
    sample_txt = ""
    if t.get("sample_path") and os.path.exists(t["sample_path"]):
        txt, _ = _doc_text(t["sample_path"], t.get("sample_mime"))
        sample_txt = (txt or "")[:3000]
    extra = (body.get("instructions") or "").strip()
    content = ("TIP DOCUMENT: " + (t.get("name") or "") + " (categorie: " + (t.get("category") or "") + ")"
               + "\n\nCAMPURI DE EXTRAS:\n" + _fmt_fields(fields)
               + (("\n\nINSTRUCTIUNI SUPLIMENTARE:\n" + extra) if extra else "")
               + "\n\nTEXT DOCUMENT EXEMPLU:\n" + (sample_txt or "(indisponibil)"))
    # System-prompt SPECIFIC per tip — altfel gateway-ul (curated/cache) poate servi promptul
    # altui tip de document care a folosit acelasi system generic. Numele+categoria fac cheia distincta.
    sysp = (_DOC_GEN_SYSTEM + "\n\nSCRII PROMPTUL PENTRU UN SINGUR TIP DE DOCUMENT: \""
            + (t.get("name") or "") + "\" (categorie: " + (t.get("category") or "")
            + "). Promptul trebuie sa fie SPECIFIC acestui tip si acestor campuri — NU pentru alt tip de document.")
    res = iris_ai.run_prompt(sysp, content, response_format="text",
                             temperature=0.2, max_tokens=900,
                             task="cargo360:doc_prompt_gen:%s:%s" % (type_id, _cache_salt(sysp, content)),
                             use_cache=False)
    if not res.get("ok"):
        raise HTTPException(502, detail=res.get("error") or {"code": "FAIL"})
    return {"ok": True, "prompt": (res.get("text") or "").strip()}


@router.post("/documents/types/{type_id}/test-extract")
def test_extract(type_id: int, body: dict,
                 db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    _INTERACTIVE.set(True)   # click-and-wait: fail-fast pe gateway (vezi _ai_budget)
    t = _get_type(db, type_id)
    if not t:
        raise HTTPException(404, "Tip inexistent")
    prompt = body.get("prompt") if body.get("prompt") is not None else t.get("extract_prompt")
    fields = body.get("fields") if body.get("fields") is not None else (t.get("extract_fields") or [])
    if not (prompt or "").strip():
        raise HTTPException(400, "Lipseste promptul de extragere")
    if not t.get("sample_path") or not os.path.exists(t["sample_path"]):
        raise HTTPException(400, "Incarca intai un document exemplu (sablon)")
    # Vision: implicit dupa flag-ul tipului; body poate forta on/off pentru A/B in UI.
    use_vision = bool(body.get("vision")) if ("vision" in body) else bool(t.get("extract_via_vision"))
    doc_text, method = _doc_text(t["sample_path"], t.get("sample_mime"))   # si ca indiciu OCR la vision
    if use_vision:
        system = _build_doc_extract_system_vision(prompt, fields)
        data, model, err = _extract_doc_vision(
            system, (t["sample_path"], t.get("sample_mime")), type_id,
            t.get("name"), fields=fields, ocr_hint=doc_text)
        method = "vision"
    else:
        if not (doc_text or "").strip():
            raise HTTPException(422, detail={"code": "NO_TEXT", "message":
                "Nu am putut extrage text din document (PDF scanat fara strat text sau poza needitabila). "
                "Activeaza 'Extragere vizuala' pentru acest tip ca sa citesti direct din imagine."})
        system = _build_doc_extract_system(prompt, fields)
        data, model, err = _extract_doc(system, doc_text, type_id, t.get("name"), fields=fields)
    if data is None:
        raise HTTPException(502, detail={"code": "EXTRACT_FAIL", "message": err})
    return {"ok": True, "data": data, "model": model, "method": method,
            "type_id": type_id, "vision": use_vision}


# ════════════════════════════════════════════════════════════════════════════
# AI: recunoastere / validare tip document (fara extragere de date)
# Pentru tipurile din care NU extragem date (detect-only) — verifica daca un
# document exemplu ar fi corect identificat ca acest tip daca ar veni pe mail.
# ════════════════════════════════════════════════════════════════════════════
_DOC_DETECT_SYSTEM = (
    "Esti un clasificator de documente. Primesti DEFINITIA unui tip de document (nume, categorie, "
    "criterii de recunoastere si titluri/cuvinte-cheie de potrivire) si TEXTUL unui document. "
    "Decizi DOAR daca documentul apartine acelui tip — NU extragi date. Fii strict: pune match=true "
    "doar daca documentul corespunde clar criteriilor. Returneaza DOAR un JSON valid cu cheile: "
    "match (boolean), confidence (numar intre 0 si 1), reason (text scurt in romana), "
    "detected_title (headerul/fraza care a determinat decizia, sau null)."
)

_DOC_DETECT_GEN_SYSTEM = (
    "Esti un asistent care scrie PROMPTURI de ANALIZA / RECUNOASTERE a unui tip de document "
    "(NU de extragere de date). Primesti tipul si categoria documentului, eventuale titluri / fraze "
    "de identificare si, optional, textul unui document EXEMPLU. Scrie un prompt CLAR, in romana, care "
    "descrie cum se recunoaste acest tip de document: titlu / header caracteristic, structura, "
    "termeni si campuri-cheie prezente, ce il deosebeste de documente similare. Promptul va fi folosit "
    "ca instructiune de sistem pentru un model AI ca sa decida daca un document primit ESTE sau NU de "
    "acest tip. NU cere extragerea vreunei valori. Fa-l GENERAL (nu copia valori concrete din exemplu). "
    "Returneaza DOAR textul promptului, fara explicatii, fara ``` ."
)


def _build_doc_detect_system(t: dict) -> str:
    """Construieste system-promptul de clasificare din definitia tipului."""
    name = (t.get("name") or "").strip()
    crit = (t.get("detect_prompt") or "").strip()
    titles = t.get("match_titles") or []
    parts = [_DOC_DETECT_SYSTEM,
             "\n\nTIP TINTA: \"" + name + "\" (categorie: " + (t.get("category") or "") + ")."]
    if crit:
        parts.append("\n\nCRITERII DE RECUNOASTERE:\n" + crit)
    if titles:
        parts.append("\n\nTITLURI / CUVINTE-CHEIE DE POTRIVIRE (oricare poate confirma tipul):\n- "
                     + "\n- ".join(str(x) for x in titles))
    parts.append("\n\nDecizia: daca textul documentului corespunde acestui tip -> match=true; "
                 "altfel match=false.")
    return "".join(parts)


def _detect_doc(system: str, doc_text: str, type_id: int, name: str = None):
    """Ruleaza clasificarea pe textul documentului. Returneaza (rezultat|None, model|None, err|None)."""
    import time
    content = (doc_text or "")[:MAX_DOC_TEXT].strip()
    if not content:
        return None, None, "document fara text extractibil"
    task = "cargo360:doc_detect:%s:%s" % (_slug(name, str(type_id)), _cache_salt(system, content))
    res = None
    _attempts, _ctimeout = _ai_budget()
    for attempt in range(_attempts):
        res = iris_ai.run_prompt(
            system, content, response_format="json", temperature=0.0, max_tokens=300,
            task=task, timeout=_ctimeout)
        if res.get("ok") or (res.get("error") or {}).get("code") not in _RETRY_CODES:
            break
        time.sleep(1.2 * (attempt + 1))
    if res and res.get("ok") and isinstance(res.get("parsed"), dict):
        return res["parsed"], res.get("model"), None
    err = ((res.get("error") or {}).get("message") if res else "fail") or "raspuns invalid"
    return None, (res.get("model") if res else None), err


@router.post("/documents/types/{type_id}/generate-detect-prompt")
def generate_detect_prompt(type_id: int, body: dict,
                           db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    t = _get_type(db, type_id)
    if not t:
        raise HTTPException(404, "Tip inexistent")
    titles = body.get("match_titles") if body.get("match_titles") is not None else (t.get("match_titles") or [])
    sample_txt = ""
    if t.get("sample_path") and os.path.exists(t["sample_path"]):
        txt, _ = _doc_text(t["sample_path"], t.get("sample_mime"))
        sample_txt = (txt or "")[:3000]
    content = ("TIP DOCUMENT: " + (t.get("name") or "") + " (categorie: " + (t.get("category") or "") + ")"
               + "\n\nTITLURI / FRAZE DE IDENTIFICARE:\n"
               + ("\n".join("- " + str(x) for x in titles) if titles else "(niciuna)")
               + "\n\nTEXT DOCUMENT EXEMPLU:\n" + (sample_txt or "(indisponibil)"))
    sysp = (_DOC_DETECT_GEN_SYSTEM + "\n\nSCRII PROMPTUL DE RECUNOASTERE PENTRU UN SINGUR TIP: \""
            + (t.get("name") or "") + "\" (categorie: " + (t.get("category") or "")
            + "). Promptul trebuie sa fie SPECIFIC acestui tip — NU pentru alt document.")
    res = iris_ai.run_prompt(sysp, content, response_format="text",
                             temperature=0.2, max_tokens=700,
                             task="cargo360:doc_detect_gen:%s:%s" % (type_id, _cache_salt(sysp, content)),
                             use_cache=False)
    if not res.get("ok"):
        raise HTTPException(502, detail=res.get("error") or {"code": "FAIL"})
    return {"ok": True, "prompt": (res.get("text") or "").strip()}


@router.post("/documents/types/{type_id}/test-detect")
def test_detect(type_id: int, body: dict,
                db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    _INTERACTIVE.set(True)   # click-and-wait: fail-fast pe gateway (vezi _ai_budget)
    t = _get_type(db, type_id)
    if not t:
        raise HTTPException(404, "Tip inexistent")
    if not t.get("sample_path") or not os.path.exists(t["sample_path"]):
        raise HTTPException(400, "Incarca intai un document exemplu (sablon)")
    # Permite validarea cu valori editate, inainte de salvare.
    if body.get("detect_prompt") is not None:
        t["detect_prompt"] = body.get("detect_prompt")
    if body.get("match_titles") is not None:
        t["match_titles"] = body.get("match_titles")
    doc_text, method = _doc_text(t["sample_path"], t.get("sample_mime"))
    if not (doc_text or "").strip():
        raise HTTPException(422, detail={"code": "NO_TEXT", "message":
            "Nu am putut extrage text din document (PDF scanat fara strat text sau poza needitabila). "
            "Pentru poze va fi nevoie de canalul vision (in curs de aprobare)."})
    system = _build_doc_detect_system(t)
    res, model, err = _detect_doc(system, doc_text, type_id, t.get("name"))
    if res is None:
        raise HTTPException(502, detail={"code": "DETECT_FAIL", "message": err})
    return {"ok": True, "result": res, "model": model, "method": method, "type_id": type_id}


# ════════════════════════════════════════════════════════════════════════════
# STEP 2 — clasificare + extragere a ATASAMENTELOR reale din emailuri
#   Un atasament = un rand in `document_extractions` (unique pe attachment_id).
#   Pipeline per atasament: pre-filtru -> _doc_text -> clasificare (categorie+tip,
#   1 apel AI cu catalogul) -> extragere conform tipului -> upsert.
#   Statusuri: extracted | classified | needs_review | needs_vision | neidentificat | failed
# ════════════════════════════════════════════════════════════════════════════
CLASSIFY_CONF_MIN = 0.55      # >= atat + tip valid -> extragere; sub -> needs_review
DOC_DISCARD_CONF_MIN = 0.50   # sub atat (sau fara categorie/tip) -> junk -> discard (sters, ascuns)
AUTO_CONF_MIN = 0.85          # sub atat (conf efectiv: extract_confidence sau confidence clasificare)
                              # -> pilot automat: nu propagam documentul, il tratam ca necunoscut -> discard
AUTO_VALID_CONF_MIN         = 0.82   # confidence >= pt auto-validare extracted
AUTO_VALID_COMPLETENESS_MIN = 0.95   # % campuri completate >= pt auto-validare
AUTO_VALID_CLASSIFIED_CONF  = 0.90   # classified (fara extract_fields) -> auto-validare
AUTO_VALID_CLASS_OVERRIDE   = 0.92   # clasificare >= => auto-validare chiar cu campuri lipsa (decizie user; flag in observatii_ai)
RECLASSIFY_THRESHOLD        = 0.65   # completeness 40-65% -> retry reclasificare
NEIDENTIFICAT_CS_MAX        = 0.40   # completeness < 40% -> tip gresit -> neidentificat automat
MIN_IMG_BYTES = 8000          # imagini sub atat -> logo/iconita (fara OCR)


def _is_transient_ai_err(msg) -> bool:
    """Eroare TEMPORARA de infrastructura AI (gateway 502/503/504, timeout, transport).
    In acest caz NU persistam un rand 'failed' (altfel logo-uri/documente reale raman
    blocate vizibil pana la reprocesare manuala) — lasam atasamentul fara rand in coada
    ca sa se reia automat la urmatorul drain, cand gateway-ul revine."""
    m = (msg or "").lower()
    return any(k in m for k in ("502", "503", "504", "bad gateway", "gateway tim",
                                "timeout", "timed out", "transport", "temporar"))

_DEFAULT_CLASSIFY_PROMPT = (
    "Esti un clasificator de atasamente de email pentru o firma de transport (Cargo Track). "
    "Pentru fiecare atasament primesti textul extras din el (PDF sau OCR) si decizi daca este un "
    "DOCUMENT real procesabil dintr-una din categoriile cunoscute (vehicul / sofer / contract) "
    "SI corespunde unui tip din catalog. "
    "REGULA STRICTA: daca NU poti incadra atasamentul intr-una din aceste trei categorii, "
    "pune is_document=false (NU ghici o categorie). Daca pui is_document=true, esti OBLIGAT sa "
    "completezi si category cu una din valorile vehicul/sofer/contract.\n\n"
    "PUNE is_document=false (si category=null) pentru:\n"
    "- elemente vizuale fara valoare de document: logo, semnatura de email, iconita, banner, "
    "screenshot, imagine decorativa, poze cu aparate/dispozitive/echipamente, poze ciudate sau "
    "off-context, fotografii generice, materiale de marketing, imagini fara text relevant;\n"
    "- documente de business care NU sunt din categoriile noastre: ordin de plata / OP, factura / "
    "proforma, chitanta / bon, aviz, asigurare / polita (RCA, CASCO, CMR, carte verde), extras de "
    "cont / extras bancar, oferta comerciala, balanta, declaratie fiscala, alte acte contabile;\n"
    "- orice document care NU corespunde clar unui tip din catalogul de mai jos.\n\n"
    "Daca e document valid si corespunde unui tip din catalog, incadreaza-l in categorie (vehicul/"
    "sofer/contract) si in tipul EXACT din catalog, mai ales dupa TITLU.\n\n"
    "REGULA DE POTRIVIRE A TIPULUI: atribuie un type_id DOAR daca documentul corespunde EFECTIV "
    "titlului si criteriilor acelui tip din catalog. NU forta cel mai apropiat tip doar pentru ca "
    "exista cuvinte comune. Un CONTRACT real care NU corespunde niciunui tip definit in catalog "
    "(alt fel de contract, alt titlu) -> type_id=null si confidence mica (va fi marcat NECUNOSCUT, "
    "NU procesat). O IMPUTERNICIRE / PROCURA / POWER OF ATTORNEY / VOLLMACHT / POOBLASTILO / "
    "PLNA MOC NU este un contract de prestari servicii — daca nu exista un tip dedicat in catalog, "
    "type_id=null.\n\n"
    "IN DUBIU sau pentru un atasament ciudat / nesigur / greu de citit: pune is_document=false. "
    "Este preferabil sa NU procesam un atasament incert decat sa procesam gunoi — extragerea "
    "ulterioara este costisitoare, iar documentele relevante au titluri si structura clare."
)


def _get_classify_prompt(db) -> str:
    r = db.execute(text("SELECT value FROM settings WHERE key='documents.classify_prompt'")).fetchone()
    if r and isinstance(r[0], str) and r[0].strip():
        return r[0]
    return _DEFAULT_CLASSIFY_PROMPT


def _types_catalog(db) -> list:
    """Catalog compact al tipurilor active pentru clasificator. Titlurile de potrivire
    sunt ESENTIALE: contractele se disting aproape exclusiv dupa titlu."""
    rows = db.execute(text(
        "SELECT id, category, name, match_titles, detect_prompt, identify_only, "
        "       jsonb_array_length(COALESCE(extract_fields,'[]'::jsonb)) AS nf "
        "FROM document_types WHERE status='active' AND enabled=true "
        "ORDER BY category, lower(name)")).fetchall()
    out = []
    for r in rows:
        m = dict(r._mapping)
        out.append({"id": m["id"], "category": m["category"], "name": m["name"],
                    "titles": (m.get("match_titles") or [])[:8],
                    "detect": (m.get("detect_prompt") or "").strip()[:300],
                    "identify_only": bool(m.get("identify_only")),
                    "has_extract": (m.get("nf") or 0) > 0 and not m.get("identify_only")})
    return out


def _build_classify_system(classify_prompt: str, catalog: list) -> str:
    lines = []
    for t in catalog:
        seg = "- id=%s | categorie=%s | tip=\"%s\"" % (t["id"], t["category"], t["name"])
        if t.get("titles"):
            seg += " | titluri: " + "; ".join(str(x) for x in t["titles"])
        if t.get("detect"):
            seg += " | criterii: " + t["detect"]
        lines.append(seg)
    catalog_txt = "\n".join(lines) if lines else "(niciun tip definit)"
    schema = ("\n\nRaspunde DOAR cu un JSON valid (fara text in plus, fara ```), cu cheile EXACT: "
              "is_document (boolean), category (\"vehicul\"|\"sofer\"|\"contract\" sau null), "
              "type_id (numarul id din catalog sau null), type_name (text sau null), "
              "confidence (numar intre 0 si 1), reason (text scurt in romana). "
              "Nu inventa un id care nu e in catalog.\n\n"
              "MAI MULTE DOCUMENTE PE ACELASI ATASAMENT: daca pe ACELASI fisier/pagina vezi MAI MULTE "
              "documente DISTINCTE, de tipuri DIFERITE (ex. buletin + permis de conducere + atestat "
              "intr-o singura poza scanata, sau un PDF care amesteca tipuri), adauga si cheia OPTIONALA "
              "documents = [{category, type_id, type_name, confidence, reason, bbox}, ...] cu cate un "
              "element pentru FIECARE document identificat. Pune in cheile de la nivelul de sus documentul "
              "DOMINANT. Daca atasamentul are UN SINGUR document, OMITE complet cheia documents (sau "
              "pune null) — nu o folosi pentru pagini multiple ale aceluiasi document.\n"
              "REGULI pentru documents[]: (1) fiecare element primeste tipul LUI; (2) daca un document "
              "NU corespunde NICIUNUI tip din catalog, pune type_id=null (NU forta cel mai apropiat tip); "
              "(3) NU refolosi acelasi type_id pentru documente DIFERITE; (4) optional, bbox=[x,y,w,h] ca "
              "FRACTII (0..1) din latimea/inaltimea imaginii, ce incadreaza acel document (util cand sunt "
              "stivuite vertical intr-o poza).")
    return (classify_prompt or "").strip() + "\n\nCATALOG TIPURI DISPONIBILE:\n" + catalog_txt + schema


def _title_prematch(doc_text, catalog):
    """Potrivire DETERMINISTA pe titlu (match_titles) in primii ~2000 caractere.
    Contractele se disting aproape exclusiv dupa titlu; un singur tip cu titlul prezent (substring
    normalizat, >=6 ch) devine INDICIU pentru clasificator. Intoarce (type_id, type_name)|None.
    Conservator: doar potrivire UNICA (un singur type_id) — nu fortam cand e ambiguu. Fara dependinte."""
    import re as _re
    norm = _re.sub(r"\s+", " ", (doc_text or "")[:2000]).strip().lower()
    if len(norm) < 20:
        return None
    hit_ids, hit = set(), None
    for t in catalog:
        for title in (t.get("titles") or []):
            ttl = _re.sub(r"\s+", " ", str(title or "")).strip().lower()
            if len(ttl) >= 6 and ttl in norm:
                hit_ids.add(t.get("id"))
                if hit is None:
                    hit = (t.get("id"), t.get("name"))
                break
    return hit if len(hit_ids) == 1 else None


def _classify_attachment(classify_system: str, doc_text: str, att_name: str = None):
    """1 apel AI: intoarce (parsed|None, model|None, err|None)."""
    import time
    content = ("NUME FISIER: " + (att_name or "?") + "\n\nTEXT DOCUMENT:\n"
               + _clip_doc_text(doc_text))
    task = "cargo360:doc_classify:%s" % _cache_salt(classify_system, content)
    res = None
    _attempts, _ctimeout = _ai_budget()
    for attempt in range(_attempts):
        res = iris_ai.run_prompt(classify_system, content, response_format="json",
                                 temperature=0.0, max_tokens=900, task=task, timeout=_ctimeout)
        if res.get("ok") or (res.get("error") or {}).get("code") not in _RETRY_CODES:
            break
        time.sleep(1.2 * (attempt + 1))
    if res and res.get("ok") and isinstance(res.get("parsed"), dict):
        return res["parsed"], res.get("model"), None
    err = ((res.get("error") or {}).get("message") if res else "fail") or "raspuns invalid"
    return None, (res.get("model") if res else None), err


def _classify_attachment_vision(classify_system: str, path: str, mime: str,
                                doc_text: str = None, att_name: str = None):
    """Clasificare VIZUALA: trimite poza/PDF-ul scanat ca atasament multimodal (sonnet) ca sa vada
    DIRECT toate documentele din imagine (OCR-ul pe o poza cu mai multe acte iese amestecat si rateaza
    multi-doc). Intoarce (parsed|None, model|None, err|None). Acelasi schema/JSON ca _classify_attachment,
    plus documents[] fiabil. Nu arunca."""
    import base64
    import hashlib
    import time
    try:
        sz = os.path.getsize(path)
        if sz > VISION_MAX_BYTES:
            return None, None, "fisier prea mare pentru vision-classify"
        with open(path, "rb") as fh:
            raw = fh.read()
    except Exception as e:
        return None, None, "citire fisier esuata: " + str(e)[:120]
    atts = [{"mime_type": _attachment_mime(path, mime),
             "data_base64": base64.b64encode(raw).decode("ascii")}]
    content = ("NUME FISIER: " + (att_name or "?") + "\n\nImaginea/PDF-ul ATASAT e sursa AUTORITARA "
               "(poate contine MAI MULTE documente fizice distincte). Clasifica si raspunde DOAR JSON.")
    if (doc_text or "").strip():
        content += "\n\nText OCR brut (indiciu, poate fi gresit):\n" + _clip_doc_text(doc_text, 4000)
    digest = hashlib.sha1(raw).hexdigest()[:12]
    task = "cargo360:doc_classify_vision:%s:%s" % (digest, _cache_salt(classify_system, content))
    res = None
    _attempts, _ctimeout = _ai_budget(vision=True)
    for attempt in range(_attempts):
        res = iris_ai.run_prompt(classify_system, content, response_format="text", model_hint="sonnet",
                                 temperature=0.0, max_tokens=1200, task=task, timeout=_ctimeout,
                                 attachments=atts)
        if res.get("ok") or (res.get("error") or {}).get("code") not in _RETRY_CODES:
            break
        time.sleep(1.2 * (attempt + 1))
    if res and (res.get("ok") or res.get("text")):
        parsed = res.get("parsed") if isinstance(res.get("parsed"), dict) else _salvage_json(res.get("text"))
        if isinstance(parsed, dict):
            return parsed, res.get("model"), None
    err = ((res.get("error") or {}).get("message") if res else "fail") or "raspuns invalid"
    return None, (res.get("model") if res else None), err


def _dedupe_multidoc(docs, catalog):
    """Pregateste lista `documents[]` de la clasificator pentru multidoc: normalizeaza bbox-ul si
    rezolva DUPLICATELE de type_id (modelul forteaza uneori acelasi tip pe doua acte diferite — ex.
    atestatul mapat la 'Act de identitate'). Pe duplicat, pastreaza ocurenta cu confidenta maxima ca
    tipul respectiv; restul -> type_id=None (parte needs_review, operatorul alege). Intoarce lista de
    dict-uri normalizate (cu cheia 'bbox' = dict sau None)."""
    tmap = {t["id"]: t for t in catalog}
    out = []
    for d in (docs or []):
        if not isinstance(d, dict):
            continue
        try:
            tid = int(d.get("type_id"))
        except Exception:
            tid = None
        if tid not in tmap:
            tid = None
        try:
            conf = float(d.get("confidence"))
        except Exception:
            conf = None
        out.append({"type_id": tid, "type_name": d.get("type_name"), "category": d.get("category"),
                    "confidence": conf, "reason": d.get("reason"), "bbox": _norm_bbox(d.get("bbox"))})
    # rezolva duplicatele de type_id: tine indexul cu cea mai mare confidenta per tip
    best = {}
    for i, d in enumerate(out):
        tid = d["type_id"]
        if tid is None:
            continue
        c = d["confidence"] if d["confidence"] is not None else 0.0
        if tid not in best or c > (out[best[tid]]["confidence"] or 0.0):
            best[tid] = i
    for i, d in enumerate(out):
        tid = d["type_id"]
        if tid is not None and best.get(tid) != i:
            d["type_id"] = None   # duplicat ne-castigator -> needs_review fara tip
            d["reason"] = (d.get("reason") or "") + " (tip duplicat — verifica manual)"
    return out


def _track_extracted_document(db, att, part_no, category, document_type_id, detected_type):
    """Inregistreaza documentul in cts_document_tracking cu starea 'extracted'.

    Numitorul statisticii („din cate documente extrase, cate ajung salvate in CTS") NU poate fi
    citit din document_extractions: scripts/storage_cleanup.sh o goleste zilnic, deci procentele
    s-ar reseta in fiecare noapte. Aici pastram doar identitatea si categoria, nu documentul.

    Nu suprascrie niciodata o stare avansata (sent/saved/failed/deleted) — re-extragerea aceluiasi
    atasament actualizeaza doar metadatele. Best-effort: nicio eroare de aici nu opreste extragerea.
    """
    try:
        db.execute(text(
            "INSERT INTO cts_document_tracking "
            "  (email_id, attachment_id, part_no, extraction_id, attachment_name, "
            "   document_type_id, category, extracted_at, cts_status) "
            "SELECT :eid, :aid, :pno, d.id, :name, :dtid, :cat, now(), 'extracted' "
            "  FROM document_extractions d "
            " WHERE d.attachment_id = :aid AND d.part_no = :pno "
            "ON CONFLICT (attachment_id, part_no) DO UPDATE SET "
            "  extraction_id = EXCLUDED.extraction_id, "
            "  document_type_id = EXCLUDED.document_type_id, "
            "  category = EXCLUDED.category, "
            "  updated_at = now() "
            "WHERE cts_document_tracking.cts_status = 'extracted'"),
            {"eid": att.get("email_id"), "aid": att["id"], "pno": int(part_no or 0),
             "name": ((att.get("name") or detected_type or "") or None) and
                     str(att.get("name") or detected_type)[:500],
             "dtid": document_type_id,
             "cat": ((category or "") or None) and str(category)[:20]})
        db.commit()
    except Exception:
        logger.exception("cts_document_tracking (extracted) att=%s part_no=%s — non-fatal",
                         att.get("id"), part_no)
        db.rollback()


def _save_extraction(db, att, status, category=None, detected_type=None, document_type_id=None,
                     confidence=None, data=None, raw_text=None, method=None, model=None,
                     error=None, confidence_reason=None, part_no=0, part_label=None, part_bbox=None,
                     page_from=None, page_to=None,
                     completeness_score=None, retry_reclassify=0,
                     extract_confidence=None, extract_method=None) -> str:
    """Upsert pe (attachment_id, part_no). part_no=0 = documentul intreg (single-doc, default).
    part_no>=1 = parti dintr-un atasament cu MAI MULTE documente (vezi _process_multidoc/split).
    part_bbox = banda/regiune decupata (dict fractii sau None = tot atasamentul).
    Corecturile umane (reviewed=true) sunt protejate de drain-ul automat prin selectia
    'd.id IS NULL' (rand inexistent) + check-ul timpuriu 'force=False -> reviewed_skip' din
    _process_attachment; reidentificarea explicita (force=True) suprascrie intentionat."""
    db.execute(text(
        "INSERT INTO document_extractions "
        "(email_id, attachment_id, part_no, part_label, part_bbox, document_type_id, category, "
        " detected_type, confidence, data, raw_text, method, model, status, error, confidence_reason, "
        " page_from, page_to, completeness_score, retry_reclassify, "
        " extract_confidence, extract_method, created_at, extracted_at, updated_at) "
        "VALUES (:eid,:aid,:pno,:plabel,CAST(:pbbox AS jsonb),:tid,:cat,:dt,:conf,CAST(:data AS jsonb),"
        " :rt,:method,:model,:status,:err,:creason, :pfrom, :pto, :cs, :rr, "
        " :econf, :emeth, now(), now(), now()) "
        "ON CONFLICT (attachment_id, part_no) DO UPDATE SET "
        "  part_label=EXCLUDED.part_label, part_bbox=EXCLUDED.part_bbox, "
        "  page_from=EXCLUDED.page_from, page_to=EXCLUDED.page_to, "
        "  document_type_id=EXCLUDED.document_type_id, "
        "  category=EXCLUDED.category, detected_type=EXCLUDED.detected_type, "
        "  confidence=EXCLUDED.confidence, data=EXCLUDED.data, raw_text=EXCLUDED.raw_text, "
        "  method=EXCLUDED.method, model=EXCLUDED.model, status=EXCLUDED.status, "
        "  error=EXCLUDED.error, confidence_reason=EXCLUDED.confidence_reason, "
        "  completeness_score=EXCLUDED.completeness_score, "
        "  retry_reclassify=EXCLUDED.retry_reclassify, "
        "  extract_confidence=EXCLUDED.extract_confidence, extract_method=EXCLUDED.extract_method, "
        "  extracted_at=now(), updated_at=now()"),
        {"eid": att["email_id"], "aid": att["id"], "pno": int(part_no or 0),
         "plabel": (part_label or "")[:160] or None,
         "pbbox": (json.dumps(part_bbox) if part_bbox else None), "tid": document_type_id,
         "cat": category, "dt": (detected_type or "")[:160] or None, "conf": confidence,
         "data": json.dumps(data or {}), "rt": (raw_text or "")[:20000] or None,
         "method": (method or "")[:30] or None, "model": (model or "")[:80] or None,
         "status": status, "err": (error or "")[:1000] or None,
         "creason": (confidence_reason or "")[:1000] or None,
         "pfrom": (int(page_from) if page_from is not None else None),
         "pto": (int(page_to) if page_to is not None else None),
         "cs": (float(completeness_score) if completeness_score is not None else None),
         "rr": int(retry_reclassify or 0),
         "econf": (float(extract_confidence) if extract_confidence is not None else None),
         "emeth": (extract_method or "")[:20] or None})
    db.commit()
    _track_extracted_document(db, att, part_no, category, document_type_id, detected_type)
    if status in ("extracted", "classified") and detected_type:
        try:
            _rename_doc(db, att["id"], part_no, detected_type, raw_text, att.get("name"),
                        data=data, email_id=att.get("email_id"))
        except Exception:
            logger.exception("rename_doc att=%s part_no=%s", att["id"], part_no)
    if status in ("extracted", "classified") and data and (category or "").lower() in ("vehicul", "contract"):
        try:
            _validate_client_assets(db, att, category, detected_type, data, part_no)
        except Exception:
            logger.exception("client validation att=%s", att.get("id"))
    # Nota informativa: campurile tipului ramase necompletate -> observatii_ai (NU blocheaza fluxul).
    if status in ("extracted", "needs_review") and document_type_id:
        try:
            _t_mf = _get_type(db, document_type_id)
            _set_fields_observatie(db, att["id"], part_no, data,
                                   (_t_mf.get("extract_fields") or []) if _t_mf else [])
            db.commit()
        except Exception:
            logger.exception("fields observatie att=%s part_no=%s", att["id"], part_no)
    return status


def _discard_attachment(db, att, reason: str = None) -> str:
    """Junk (logo/iconita, OP/factura, fara categorie, confidenta <50%, tip necunoscut):
    NU pastram rand in document_extractions (dispare automat din TOATE listele, care nu
    filtreaza pe status). Ca sa NU se reproceseze (drain = 'd.id IS NULL'), marcam atasamentul
    cu doc_discarded=true; drain-ul il exclude. Recuperabil: restore-discarded reseteaza flag-ul."""
    aid = att["id"]
    db.execute(text("DELETE FROM document_extractions WHERE attachment_id=:a"), {"a": aid})
    db.execute(text("UPDATE attachments SET doc_discarded=true, doc_discard_reason=:r, "
                    "doc_discarded_at=now() WHERE id=:a"),
               {"a": aid, "r": (reason or "")[:500] or None})
    db.commit()
    return "discarded"


# ── OPS-0124: validare date extrase vs. activele clientului (vehicule/contracte) ──
# Verdict informativ (NU blocheaza auto-validarea, per decizia user): scrie observatii_ai
# (consumat de /cts/get_email_documents) + un verdict structurat in client_match.
_WARN = "\u26a0"
_AUTO_OBS_PREFIX = _WARN + " Verificare automată:"   # prefix nota auto — recunoscut la regenerare

_PLATE_FIELD_HINTS = ("licence plate", "license plate", "numar de inmatriculare",
                      "nr. inmatriculare", "nr inmatriculare", "inmatricul", "placa",
                      "plate", "licence", "license")


def _norm_plate(v):
    import re as _re
    if v is None:
        return ""
    return _re.sub(r"[^A-Z0-9]", "", str(v).upper())


def _extract_plate_value(data):
    if not isinstance(data, dict):
        return None
    for k, v in data.items():
        kl = str(k).lower()
        if any(hint in kl for hint in _PLATE_FIELD_HINTS):
            if v is not None and str(v).strip() and _norm_plate(v):
                return str(v).strip()
    return None


def _extract_vin_value(data):
    """Serie de sasiu / VIN (pt CIV/COC fara numar de inmatriculare). Garda lungime
    >=8 ca sa nu prinda fragmente scurte (VIN real = 17 caractere)."""
    import re as _re
    if not isinstance(data, dict):
        return None
    for k, v in data.items():
        kl = str(k).lower()
        hit = ("sasiu" in kl or "chassis" in kl or "identificare" in kl
               or "(e.)" in kl or _re.search(r"\bvin\b", kl))
        if hit and v is not None and str(v).strip() and len(_norm_plate(v)) >= 8:
            return str(v).strip()
    return None


def _store_client_match(db, att_id, part_no, verdict, detail):
    db.execute(text(
        "UPDATE document_extractions SET client_match=:m, client_match_detail=:d, "
        "client_match_at=now(), updated_at=now() WHERE attachment_id=:a AND part_no=:p"),
        {"m": (verdict or "")[:16] or None, "d": (detail or "")[:1000] or None,
         "a": att_id, "p": int(part_no or 0)})


def _set_auto_observatie(db, att_id, part_no, auto_msg):
    """Pune nota AUTO in observatii_ai pastrand textul manual existent. Liniile marcate
    cu _OBS_AUTO_MARK sunt regenerate la fiecare reprocesare; restul (note umane) raman."""
    cur = db.execute(text("SELECT observatii_ai FROM document_extractions "
                          "WHERE attachment_id=:a AND part_no=:p"),
                     {"a": att_id, "p": int(part_no or 0)}).fetchone()
    existing = (cur[0] if cur else None) or ""
    kept = [ln for ln in existing.splitlines()
            if ln.strip() and not ln.lstrip().startswith(_AUTO_OBS_PREFIX)
            and "\u27e6auto\u27e7" not in ln]
    if auto_msg:
        kept.append(auto_msg)
    new_val = "\n".join(kept).strip() or None
    db.execute(text("UPDATE document_extractions SET observatii_ai=:o, updated_at=now() "
                    "WHERE attachment_id=:a AND part_no=:p"),
               {"o": new_val, "a": att_id, "p": int(part_no or 0)})


_OBS_FIELDS_PREFIX = _WARN + " Câmpuri necompletate:"   # nota auto, separata de verificarea client


def _missing_extract_fields(data, fields):
    """Numele campurilor din extract_fields ramase goale/neextrase (in ordinea definita in tip)."""
    if not fields:
        return []
    d = data if isinstance(data, dict) else {}
    out = []
    for f in fields:
        nm = f.get("name") if isinstance(f, dict) else None
        if nm and d.get(nm) in (None, "", [], {}):
            out.append(nm)
    return out


def _set_fields_observatie(db, att_id, part_no, data, fields):
    """Semnaleaza in observatii_ai campurile din tip ramase NECOMPLETATE (informativ — NU blocheaza,
    documentul trece mai departe). Linie auto cu prefix propriu => coexista cu nota de verificare
    client (_set_auto_observatie) si cu notele umane. Reidempotenta la reprocesare."""
    miss = _missing_extract_fields(data, fields)
    msg = None
    if miss:
        msg = "%s %s (neextrase — verifică manual)" % (_OBS_FIELDS_PREFIX, ", ".join(miss))
    cur = db.execute(text("SELECT observatii_ai FROM document_extractions "
                          "WHERE attachment_id=:a AND part_no=:p"),
                     {"a": att_id, "p": int(part_no or 0)}).fetchone()
    existing = (cur[0] if cur else None) or ""
    kept = [ln for ln in existing.splitlines()
            if ln.strip() and not ln.lstrip().startswith(_OBS_FIELDS_PREFIX)]
    if msg:
        kept.append(msg)
    new_val = "\n".join(kept).strip() or None
    db.execute(text("UPDATE document_extractions SET observatii_ai=:o, updated_at=now() "
                    "WHERE attachment_id=:a AND part_no=:p"),
               {"o": new_val, "a": att_id, "p": int(part_no or 0)})
    return miss


def _validate_client_assets(db, att, category, detected_type, data, part_no=0):
    """Compara datele extrase cu vehiculele/contractele clientului (sincronizate din CTS).
    Best-effort, informativ. Vehicul = match pe numarul de inmatriculare. Contract = match
    pe serie/CUI (inert pana CTS livreaza aceste campuri). Nu arunca niciodata."""
    import re as _re
    try:
        cat = (category or "").lower()
        if cat not in ("vehicul", "contract") or not isinstance(data, dict) or not data:
            return
        aid = att["id"]
        pno = int(part_no or 0)
        row = db.execute(text("SELECT client_id FROM emails WHERE id=:e"),
                         {"e": att.get("email_id")}).fetchone()
        client_id = row[0] if row else None
        if not client_id:
            _store_client_match(db, aid, pno, "no_client", "email fara client identificat")
            db.commit()
            return
        crow = db.execute(text("SELECT name, cui FROM clients WHERE id=:c"),
                          {"c": client_id}).fetchone()
        cname = ((crow[0] if crow else None) or "").strip()
        client_cui = (crow[1] if crow else None)
        cref = (" " + cname) if cname else ""

        if cat == "vehicul":
            plate = _extract_plate_value(data)
            if not plate:
                # Fara numar de inmatriculare (ex. CIV/COC) -> incearca validarea pe VIN.
                vin = _extract_vin_value(data)
                if vin:
                    vins = set()
                    for (vv,) in db.execute(text(
                            "SELECT vin FROM client_vehicles WHERE client_id=:c AND vin IS NOT NULL"),
                            {"c": client_id}):
                        nvv = _norm_plate(vv)
                        if len(nvv) >= 8:
                            vins.add(nvv)
                    if not vins:
                        # CTS nu are VIN de referinta (~70% lipsa) -> nu pot verifica, NU semnalez.
                        _store_client_match(db, aid, pno, "no_ref",
                                            "validare VIN indisponibila (clientul nu are serie de sasiu in CTS)")
                        _set_auto_observatie(db, aid, pno, None)
                        db.commit()
                        return
                    if _norm_plate(vin) in vins:
                        _store_client_match(db, aid, pno, "match",
                                            "seria de sasiu (VIN) " + vin + " corespunde unui vehicul al clientului")
                        _set_auto_observatie(db, aid, pno, None)
                    else:
                        msg = (_WARN + " Verificare automată: seria de șasiu (VIN) extrasă (" + vin +
                               ") nu corespunde niciunui vehicul al clientului" + cref + " în CTS.")
                        _store_client_match(db, aid, pno, "mismatch", msg)
                        _set_auto_observatie(db, aid, pno, msg)
                    db.commit()
                    return
                _store_client_match(db, aid, pno, "no_key",
                                    "document fara numar de inmatriculare extras")
                _set_auto_observatie(db, aid, pno, None)
                db.commit()
                return
            plates = set()
            for (pv,) in db.execute(text(
                    "SELECT plate FROM client_vehicles WHERE client_id=:c AND plate IS NOT NULL"),
                    {"c": client_id}):
                npl = _norm_plate(pv)
                if npl:
                    plates.add(npl)
            if not plates:
                _store_client_match(db, aid, pno, "no_ref",
                                    "clientul nu are vehicule sincronizate in CTS")
                _set_auto_observatie(db, aid, pno, None)
                db.commit()
                return
            if _norm_plate(plate) in plates:
                _store_client_match(db, aid, pno, "match",
                                    "numarul de inmatriculare " + plate + " corespunde unui vehicul al clientului")
                _set_auto_observatie(db, aid, pno, None)
            else:
                msg = (_WARN + " Verificare automată: numărul de înmatriculare extras (" + plate +
                       ") nu corespunde niciunui vehicul al clientului" + cref + " în CTS.")
                _store_client_match(db, aid, pno, "mismatch", msg)
                _set_auto_observatie(db, aid, pno, msg)
            db.commit()
            return

        # cat == "contract": CUI-ul clientului (O SINGURA DATA, din clients.cui) + numerele
        # de contract (client_contracts.contract_no), aduse din CTS. Inert pana atunci.
        nos = set()
        for (cn,) in db.execute(text(
                "SELECT contract_no FROM client_contracts WHERE client_id=:c AND contract_no IS NOT NULL"),
                {"c": client_id}):
            nn = _norm_plate(cn)
            if nn:
                nos.add(nn)
        cui_digits = _re.sub(r"[^0-9]", "", str(client_cui)) if client_cui else ""
        if not cui_digits and not nos:
            _store_client_match(db, aid, pno, "pending",
                                "validare contract indisponibila (CTS nu livreaza inca CUI client / numar contract)")
            db.commit()
            return
        dlow = {str(k).lower(): v for k, v in data.items()}

        def _find(*subs):
            for k, v in dlow.items():
                if any(sb in k for sb in subs) and v is not None and str(v).strip():
                    return str(v).strip()
            return None

        ext_no = _find("numar contract", "nr contract", "serie")
        ext_cui = _find("cui", "cif")
        cui_bad = bool(cui_digits) and bool(ext_cui) and _re.sub(r"[^0-9]", "", str(ext_cui)) != cui_digits
        # Numar contract: match normalizat exact SAU numarul clientului apare ca secventa in cel
        # extras (acopera anexe/sufixe gen "1833231/2024"), cu garda de lungime >=5 anti fals-pozitiv.
        ext_no_n = _norm_plate(ext_no) if ext_no else ""
        no_hit = bool(ext_no_n) and ((ext_no_n in nos) or any(len(r) >= 5 and r in ext_no_n for r in nos))
        no_bad = bool(nos) and bool(ext_no) and not no_hit
        if cui_bad:
            msg = (_WARN + " Verificare automată: CUI-ul extras (" + str(ext_cui) +
                   ") nu coincide cu al clientului" + cref + " — contractul pare să aparțină altui client.")
            _store_client_match(db, aid, pno, "mismatch", msg)
            _set_auto_observatie(db, aid, pno, msg)
        elif no_bad:
            msg = (_WARN + " Verificare automată: contractul trimis (nr. " + str(ext_no) +
                   ") nu coincide cu niciun contract al clientului" + cref + " în CTS.")
            _store_client_match(db, aid, pno, "mismatch", msg)
            _set_auto_observatie(db, aid, pno, msg)
        else:
            _store_client_match(db, aid, pno, "match", "contractul corespunde clientului")
            _set_auto_observatie(db, aid, pno, None)
        db.commit()
    except Exception:
        logger.exception("validate_client_assets att=%s", att.get("id"))
        try:
            db.rollback()
        except Exception:
            pass


def _maybe_auto_validate(db, att_id, part_no, data, fields, confidence, status, ext_conf=None):
    """Seteaza reviewed=true + auto_validated=true daca confidence + completeness sunt
    suficient de mari. Apelata dupa _save_extraction cand extraction a reusit.
    ext_conf = confidenta de EXTRAGERE intoarsa de IRIS (cand exista) — bate confidenta de
    clasificare la decizia de auto-validare pt documentele extrase ('toate campurile + raspuns pozitiv')."""
    if status not in ("extracted", "classified"):
        return
    try:
        conf = float(confidence or 0)
    except Exception:
        return
    if status == "classified":
        cs = 1.0
        valid = conf >= AUTO_VALID_CLASSIFIED_CONF
    else:
        cs = _completeness_score(data or {}, fields or [])
        eff = float(ext_conf) if ext_conf is not None else conf
        # Decizie user: clasificare mare (>=0.92) => auto-validare chiar cu campuri lipsa / extragere
        # IRIS sub prag. Campurile necompletate raman semnalate in observatii_ai (informativ, nu blocant).
        valid = ((conf >= AUTO_VALID_CLASS_OVERRIDE)
                 or (eff >= AUTO_VALID_CONF_MIN and cs >= AUTO_VALID_COMPLETENESS_MIN))
    if valid:
        db.execute(text(
            "UPDATE document_extractions SET reviewed=true, auto_validated=true, "
            "auto_validated_at=now(), completeness_score=:cs, updated_at=now() "
            "WHERE attachment_id=:a AND part_no=:pno"
        ), {"a": att_id, "pno": int(part_no or 0), "cs": cs})
        db.commit()
        logger.info("auto_validated att=%s part_no=%s conf=%.2f cs=%.2f", att_id, part_no, conf, cs)
    else:
        db.execute(text(
            "UPDATE document_extractions SET completeness_score=:cs, updated_at=now() "
            "WHERE attachment_id=:a AND part_no=:pno"
        ), {"a": att_id, "pno": int(part_no or 0), "cs": cs})
        db.commit()


def _maybe_trigger_reclassify(db, att_id, part_no, data, fields, status, prev_retry, ext_conf=None):
    """Daca extracted dar completeness mica si nu s-a facut inca retry,
    marcheaza randul cu retry_reclassify=1 + status=needs_review.
    Drain-ul va relua reclasificarea segmentului.
    ext_conf = confidenta de EXTRAGERE IRIS: cand IRIS e increzator pe tip, completeness mic inseamna
    campuri optionale lipsa (NU tip gresit) -> verificare umana, fara auto-'neidentificat' (distructiv)."""
    if status != "extracted":
        return
    if (prev_retry or 0) >= 1:
        return
    # Nu anula o auto-validare deja decisa (ex. clasificare >=0.92 cu campuri lipsa) -> nu o trimite la retry.
    if db.execute(text("SELECT reviewed FROM document_extractions WHERE attachment_id=:a AND part_no=:p"),
                  {"a": att_id, "p": int(part_no or 0)}).scalar():
        return
    cs = _completeness_score(data or {}, fields or [])
    if cs >= RECLASSIFY_THRESHOLD:
        return
    pct = int(round(cs * 100))
    if ext_conf is not None and float(ext_conf) >= CLASSIFY_CONF_MIN:
        # IRIS sigur pe tip dar campuri putine -> verificare umana, FARA reclasificare/auto-neidentificat.
        db.execute(text(
            "UPDATE document_extractions SET status='needs_review', retry_reclassify=2, "
            "completeness_score=:cs, "
            "error='completeness ' || :pct || '%% — campuri putine (IRIS sigur pe tip); verificare manuala', "
            "updated_at=now() WHERE attachment_id=:a AND part_no=:pno"
        ), {"a": att_id, "pno": int(part_no or 0), "cs": cs, "pct": pct})
        db.commit()
        logger.info("iris low-completeness -> needs_review att=%s part_no=%s completeness=%d%%", att_id, part_no, pct)
        return
    if cs < NEIDENTIFICAT_CS_MAX:
        db.execute(text(
            "UPDATE document_extractions SET status='needs_review', retry_reclassify=2, "
            "document_type_id=NULL, category=NULL, detected_type='neidentificat', "
            "completeness_score=:cs, reviewed=true, auto_validated=true, auto_validated_at=now(), "
            "error='completeness ' || :pct || '%% — tip incorect, neidentificat automat', "
            "updated_at=now() "
            "WHERE attachment_id=:a AND part_no=:pno"
        ), {"a": att_id, "pno": int(part_no or 0), "cs": cs, "pct": pct})
        db.commit()
        logger.info("neidentificat auto att=%s part_no=%s completeness=%d%%", att_id, part_no, pct)
    else:
        db.execute(text(
            "UPDATE document_extractions SET status='needs_review', retry_reclassify=1, "
            "completeness_score=:cs, "
            "error='completeness ' || :pct || '%% — reclasificare programata (retry 1)', "
            "updated_at=now() "
            "WHERE attachment_id=:a AND part_no=:pno"
        ), {"a": att_id, "pno": int(part_no or 0), "cs": cs, "pct": pct})
        db.commit()
        logger.info("reclassify scheduled att=%s part_no=%s completeness=%d%%", att_id, part_no, pct)


def _auto_dismiss_neidentificat(db, att_id, part_no):
    """Neidentificat => auto-confirmat imediat; operatorul nu poate face nimic cu el."""
    db.execute(text(
        "UPDATE document_extractions SET reviewed=true, auto_validated=true, "
        "auto_validated_at=now(), status='discarded', updated_at=now() "
        "WHERE attachment_id=:a AND part_no=:pno AND detected_type='neidentificat'"
    ), {"a": att_id, "pno": int(part_no or 0)})
    db.commit()
    logger.info("auto_dismiss neidentificat att=%s part_no=%s", att_id, part_no)


def _cls_unidentified(cls) -> bool:
    """True daca rezultatul clasificarii inseamna 'nu s-a putut identifica un document'
    (nu e document, sau categorie necunoscuta) -> candidat pentru rescue prin vision."""
    if not cls or not cls.get("is_document"):
        return True
    return (cls.get("category") or "").strip() not in ("vehicul", "sofer", "contract")


MULTIDOC_MAX_PARTS = 6   # plafon de siguranta: nu spargem un atasament in mai mult de atat
# Segmentare per-pagina (PDF scanat multi-document): modelul folosit la clasificarea FIECAREI pagini.
# Haiku (ieftin) — id complet, fiindca _model_hint forwardeaza doar id-uri 'claude-*'. La esec
# (gateway fara vision pe haiku) se cade automat pe 'sonnet' in _segment_pages.
SEGMENT_MODEL = "sonnet"
# Prag confidenta per pagina la segmentare: o pagina identificata SUB acest prag e tratata ca
# neidentificata (set aside) — evita sa fortam un tip pe pagini incerte.
DOC_SEGMENT_CONF_MIN = 0.90  # sub atat un segment devine 'necunoscut' (de verificat manual)


def _save_part_extraction(db, att, path, mime, part_no, doc, catalog, doc_text, cmodel, part_bbox=None):
    """Salveaza O parte (part_no>=1) a unui atasament cu mai multe documente. `doc` =
    {category, type_id, type_name, confidence, reason}. Daca tipul e valid -> extragere VIZUALA
    pe TOT atasamentul (vision se concentreaza pe documentul cerut); altfel parte 'needs_review'
    fara tip (operatorul alege manual). Nu arunca; eroare AI tranzitorie -> needs_review."""
    tmap = {t["id"]: t for t in catalog}
    tid = doc.get("type_id")
    try:
        tid = int(tid)
    except Exception:
        tid = None
    if tid not in tmap:
        tid = None
    cat = (doc.get("category") or "").strip() or None
    if cat not in ("vehicul", "sofer", "contract"):
        cat = (tmap[tid]["category"] if tid else None)
    tname = doc.get("type_name") or (tmap[tid]["name"] if tid else None)
    try:
        conf = float(doc.get("confidence"))
    except Exception:
        conf = None
    creason = doc.get("reason")
    bbox = part_bbox if part_bbox is not None else doc.get("bbox")
    bbox = _norm_bbox(bbox)
    if not tid:
        _save_extraction(db, att, status="needs_review", method="vision", model=cmodel,
                         category=cat, detected_type=tname, document_type_id=None,
                         confidence=conf, raw_text=doc_text, confidence_reason=creason,
                         part_no=part_no, part_label=tname, part_bbox=bbox)
        _auto_dismiss_neidentificat(db, att["id"], part_no)
        return "needs_review"
    t = _get_type(db, tid)
    data, emodel, eerr, status = None, cmodel, None, "classified"
    econf, emeth = None, None
    if _type_extracts(t):
        # Parte cu bbox -> extragere din DECUPAJ (vision); prin punctul UNIC constient de motor.
        data, emodel, eerr, econf, emeth = _extract_fields(
            db, t, path, mime, doc_text, tid, bbox=bbox, force_vision=True)
        # eroare tranzitorie pe o parte -> lasam partea in needs_review (nu pica tot atasamentul)
        status = "extracted" if data is not None else "needs_review"
    _fields = t.get("extract_fields") or [] if t else []
    _save_extraction(
        db, att, status=status, method=(emeth or "vision"), model=(emodel or cmodel),
        category=(t.get("category") if t else cat), detected_type=(t.get("name") if t else tname),
        document_type_id=tid, confidence=conf, data=data, raw_text=doc_text,
        confidence_reason=creason, error=eerr, part_no=part_no,
        part_label=(t.get("name") if t else tname), part_bbox=bbox,
        extract_confidence=econf, extract_method=emeth)
    _maybe_auto_validate(db, att["id"], part_no, data, _fields, conf, status, ext_conf=econf)
    _maybe_trigger_reclassify(db, att["id"], part_no, data, _fields, status, 0, ext_conf=econf)
    return status


def _process_multidoc(db, att, docs, path, mime, doc_text, catalog, cmodel) -> str:
    """Un atasament cu MAI MULTE documente (auto-detectie din clasificator): creeaza cate o parte
    (part_no=1..N) pentru fiecare, extragand VIZUAL fiecare tip de pe tot atasamentul. Sterge intai
    orice rand existent al atasamentului (idempotent la reprocesare)."""
    aid = att["id"]
    docs = _dedupe_multidoc(docs, catalog)   # rezolva duplicatele de type_id + normalizeaza bbox
    db.execute(text("DELETE FROM document_extractions WHERE attachment_id=:a"), {"a": aid})
    db.commit()
    n = 0
    for doc in docs[:MULTIDOC_MAX_PARTS]:
        n += 1
        try:
            _save_part_extraction(db, att, path, mime, n, doc, catalog, doc_text, cmodel)
        except Exception:
            logger.exception("multidoc part %d att %s", n, aid)
    logger.info("multidoc att %s -> %d parti", aid, n)
    return "multidoc:%d" % n


# ── Segmentare per-pagina: PDF scanat cu MAI MULTE documente pe intervale de pagini ──
def _pdf_page_count(path):
    try:
        import fitz  # PyMuPDF
        d = fitz.open(path)
        n = d.page_count
        d.close()
        return n
    except Exception:
        return None


def _render_page_image(path, page_index, zoom=2.0):
    """Randeaza O pagina PDF -> (jpeg_bytes, 'image/jpeg'). None la esec."""
    import io
    try:
        import fitz  # PyMuPDF
        from PIL import Image
        d = fitz.open(path)
        page = max(0, min(int(page_index), d.page_count - 1))
        pix = d[page].get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        d.close()
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return (buf.getvalue(), "image/jpeg")
    except Exception as e:
        logger.warning("render page %s p%s esuat: %s", path, page_index, e)
        return None


def _render_page_range(path, mime, pf, pt, zoom=2.0):
    """Lista [(bytes,mime)] cu paginile pf..pt (0-based, inclusiv) pentru vision-extract.
    Non-PDF (imagine) -> [(path, mime)]. Respecta plafonul de octeti al gateway-ului."""
    m = (mime or "").lower()
    ext = (os.path.splitext(path or "")[1] or "").lower()
    is_pdf = ("pdf" in m) or ext == ".pdf"
    if not is_pdf:
        return [(path, mime)]
    out, total = [], 0
    for i in range(int(pf), int(pt) + 1):
        r = _render_page_image(path, i, zoom=zoom)
        if not r:
            continue
        if total + len(r[0]) > VISION_MAX_BYTES:
            break  # mai bine partial decat esec total
        total += len(r[0])
        out.append(r)
    return out or [(path, mime)]


def _pdf_page_subset(path, mime, pf, pt):
    """Intoarce (bytes, mime) cu DOAR paginile pf..pt (0-based, inclusiv) ca document nou:
    PDF -> sub-PDF (pagini intregi, calitate originala, multi-pagina). Non-PDF (imagine 1 pagina)
    -> fisierul intreg. None la eroare. Folosit pentru preview-ul/decupajul 'bucata taiata'."""
    m = (mime or "").lower()
    ext = (os.path.splitext(path or "")[1] or "").lower()
    is_pdf = ("pdf" in m) or ext == ".pdf"
    if not is_pdf:
        try:
            with open(path, "rb") as fh:
                return (fh.read(), mime or "application/octet-stream")
        except Exception as e:
            logger.warning("page subset (non-pdf) esuat (%s): %s", path, e)
            return None
    try:
        import fitz  # PyMuPDF
        d = fitz.open(path)
        a = max(0, int(pf))
        b = min(d.page_count - 1, int(pt))
        if b < a:
            b = a
        out = fitz.open()
        out.insert_pdf(d, from_page=a, to_page=b)
        data = out.tobytes()
        out.close()
        d.close()
        return (data, "application/pdf")
    except Exception as e:
        logger.warning("pdf subset esuat (%s): %s", path, e)
        return None


def _build_segment_system(catalog):
    """System-prompt pentru clasificarea UNEI SINGURE pagini dintr-un PDF cu mai multe documente.
    Poate primi si pagina anterioara ca referinta de context (fereastra glisanta)."""
    lines = []
    for t in catalog:
        seg = "- id=%s | categorie=%s | tip=\"%s\"" % (t["id"], t["category"], t["name"])
        if t.get("titles"):
            seg += " | titluri: " + "; ".join(str(x) for x in t["titles"])
        if t.get("detect"):
            seg += " | criterii: " + t["detect"]
        lines.append(seg)
    catalog_txt = "\n".join(lines) if lines else "(niciun tip definit)"
    return (
        "Esti un clasificator de PAGINI dintr-un PDF scanat care contine MAI MULTE documente fizice "
        "puse cap la cap; fiecare document ocupa una sau mai multe pagini CONSECUTIVE. "
        "Poti primi pana la DOUA imagini: optional pagina ANTERIOARA (DOAR ca referinta de context) "
        "si apoi pagina CURENTA. Clasifica DOAR pagina CURENTA: ce tip de document e si daca pagina "
        "INCEPE un act fizic NOU (titlu/antet/format nou, alt titular/alta serie) sau CONTINUA actul "
        "de pe pagina anterioara (verso / pagina 2 a aceluiasi act).\n\n"
        "REGULI DE DOMENIU (foloseste-le pentru type_id si starts_new):\n"
        "- Talon (Certificat de inmatriculare): DE REGULA O SINGURA PAGINA. Daca pagina curenta\n"
        "  e de tip Talon si pagina anterioara era TOT Talon, pune starts_new=False DOAR DACA\n"
        "  numarul de inmatriculare e ACELASI sau nu exista un numar DIFERIT clar vizibil.\n"
        "  Doua Taloane DISTINCTE (numere de inmatriculare DIFERITE) = starts_new=True.\n"
        "- CIV (Carte de Identitate Vehicul) = ADESEA 2 PAGINI (fata + verso obligatoriu).\n"
        "  Versoul (pagina 2) contine date tehnice/proprietar si CONTINUA acelasi vehicul.\n"
        "  Daca pagina anterioara e CIV, pagina curenta are date tehnice ale ACELUIASI vehicul\n"
        "  -> starts_new=False (verso CIV, NU act nou). starts_new=True DOAR daca VIN/proprietar\n"
        "  este CLAR DIFERIT fata de pagina anterioara.\n"
        "- Permis de conducere / act de identitate: fiecare titular = act distinct. Mai multe permise "
        "consecutive (titulari diferiti) = fiecare cu starts_new=true.\n"
        "- Documente in ALTA LIMBA (permis/buletin strain, ex. spaniol): mapeaza la cel mai apropiat "
        "tip din catalog (permis strain -> Permis de conducere; act identitate strain -> Act de "
        "identitate) ca sa fie extrase — NU le lasa necunoscute doar fiindca nu sunt in romana.\n"
        "- REGULA GENERALA: starts_new=True DOAR cand esti SIGUR ca e un document fizic NOU\n"
        "  (alt titular/alta masina, alt antet complet diferit). Daca exista ORICE dubiu ca\n"
        "  e continuarea actului anterior -> starts_new=False (mai bine unesti decat spargi).\n\n"
        "CATALOG TIPURI:\n" + catalog_txt + "\n\n"
        "Raspunde DOAR cu JSON valid (fara ```), cu cheile EXACT: "
        "type_id (id din catalog sau null daca pagina nu corespunde niciunui tip cunoscut), "
        "type_name (text sau null), category (\"vehicul\"|\"sofer\"|\"contract\" sau null), "
        "confidence (numar 0..1), starts_new (boolean), reason (text scurt in romana). "
        "Daca pagina e goala/ilizibila/necunoscuta: type_id=null, starts_new=true."
    )


def _segment_pages(path, mime, catalog, page_count):
    """Un apel vision per pagina (sonnet) -> lista per-pagina cu tip + starts_new. Fereastra
    glisanta: pagina anterioara e trimisa ca referinta de context ca decizia 'continua vs act nou'
    sa fie precisa. Intoarce None DOAR cand TOATE paginile esueaza tranzitoriu (=> retry global)."""
    import base64
    import hashlib
    import time
    system = _build_segment_system(catalog)
    tmap = {t["id"]: t for t in catalog}
    n = min(MAX_PAGES, int(page_count or 0))
    out = []
    transient = 0
    seg_model = SEGMENT_MODEL
    prev_raw = None      # imaginea (jpeg bytes) a paginii anterioare procesate, ca referinta
    prev_label = None    # tipul stabilit pe pagina anterioara (text)
    for i in range(n):
        rendered = _render_page_image(path, i)
        if not rendered or len(rendered[0]) > VISION_MAX_BYTES:
            out.append({"page": i, "type_id": None, "type_name": None, "category": None,
                        "confidence": None, "starts_new": True, "reason": "pagina nerandata/prea mare"})
            prev_raw, prev_label = None, None
            continue
        raw, amime = rendered
        # context glisant: prima imagine = pagina ANTERIOARA (referinta), a doua = pagina CURENTA
        atts, ref_txt = [], ""
        if prev_raw is not None and (len(prev_raw) + len(raw)) <= VISION_MAX_BYTES:
            atts.append({"mime_type": "image/jpeg", "data_base64": base64.b64encode(prev_raw).decode("ascii")})
            ref_txt = (" Prima imagine e pagina ANTERIOARA (referinta, tip: %s). A doua imagine e "
                       "pagina CURENTA de clasificat." % (prev_label or "necunoscut"))
        atts.append({"mime_type": amime, "data_base64": base64.b64encode(raw).decode("ascii")})
        content = ("Clasifica pagina CURENTA (pagina %d din document)." % (i + 1)) + ref_txt + " Raspunde DOAR JSON."
        # cache pe hash-ul paginii curente + contextul anterior (contexte diferite -> rezultate diferite)
        task = "cargo360:doc_segment:%s:%s" % (hashlib.sha1(raw).hexdigest()[:12], (prev_label or "_")[:10])
        _attempts, _ctimeout = _ai_budget(vision=True)
        parsed, last_err = None, None
        # Incearca pe seg_model; daca esueaza ne-tranzitoriu pe haiku, cade o data pe sonnet (si ramane).
        for mdl in ([seg_model] if seg_model == "sonnet" else [seg_model, "sonnet"]):
            res = None
            for attempt in range(_attempts):
                res = iris_ai.run_prompt(system, content, response_format="text", model_hint=mdl,
                                         temperature=0.0, max_tokens=400, task=task,
                                         timeout=_ctimeout, attachments=atts)
                if res.get("ok") or (res.get("error") or {}).get("code") not in _RETRY_CODES:
                    break
                time.sleep(1.0 * (attempt + 1))
            if res and (res.get("ok") or res.get("text")):
                parsed = res.get("parsed") if isinstance(res.get("parsed"), dict) else _salvage_json(res.get("text"))
            last_err = (res.get("error") or {}).get("message") if res else "fail"
            if isinstance(parsed, dict):
                if mdl == "sonnet" and seg_model != "sonnet":
                    logger.info("doc_segment: fallback haiku->sonnet (att pagina %d)", i + 1)
                    seg_model = "sonnet"
                break
            if _is_transient_ai_err(last_err):
                break  # tranzitoriu: nu mai schimba modelul, semnaleaza retry
        if not isinstance(parsed, dict):
            if _is_transient_ai_err(last_err):
                transient += 1
            out.append({"page": i, "type_id": None, "type_name": None, "category": None,
                        "confidence": None, "starts_new": True, "reason": "clasificare pagina esuata"})
            prev_raw, prev_label = raw, "necunoscut"
            continue
        try:
            tid = int(parsed.get("type_id"))
        except Exception:
            tid = None
        if tid not in tmap:
            tid = None
        try:
            conf = float(parsed.get("confidence"))
        except Exception:
            conf = None
        _reason = parsed.get("reason")
        # pragul de confidenta se aplica la nivel de SEGMENT (dupa grupare), nu per-pagina, ca sa nu
        # spargem un document de acelasi tip doar pentru ca o pagina e putin sub prag.
        out.append({
            "page": i, "type_id": tid,
            "type_name": (tmap[tid]["name"] if tid else None),
            "category": (tmap[tid]["category"] if tid else None),
            "confidence": conf, "starts_new": bool(parsed.get("starts_new", True)),
            "reason": _reason,
        })
        prev_raw = raw
        prev_label = (tmap[tid]["name"] if tid else "necunoscut")
    if n and transient >= n:
        return None  # totul tranzitoriu -> retry global
    try:
        logger.info("doc_segment summary (model=%s): %s", seg_model,
                    [(x["page"], x["type_id"], (x["type_name"] or "")[:18], x["starts_new"],
                      x["confidence"]) for x in out])
    except Exception:
        pass
    return out


def _group_page_segments(per_page):
    """Uneste paginile consecutive in segmente. Granita = schimbare de type_id SAU starts_new=true
    (acte de acelasi tip lipite raman distincte). Paginile neidentificate consecutive -> un singur
    segment 'set aside'. Intoarce [{type_id,type_name,category,page_from,page_to,confidence,reason}]."""
    segments, cur = [], None

    def _close(seg):
        confs = [c for c in seg["_confs"] if c is not None]
        # MEDIA per-pagina (nu min): o pagina mai slaba nu trebuie sa traga tot segmentul sub
        # DOC_SEGMENT_CONF_MIN si sa-l marcheze fals 'necunoscut'. Media reflecta increderea pe segment.
        seg["confidence"] = round(sum(confs) / len(confs), 4) if confs else None
        seg.pop("_confs", None)
        return seg

    for pp in per_page:
        tid = pp.get("type_id")
        # extinde segmentul neidentificat curent (necunoscutele se grupeaza, ignora starts_new)
        if cur is not None and tid is None and cur["type_id"] is None:
            cur["page_to"] = pp["page"]
            cur["_confs"].append(pp.get("confidence"))
            continue
        # Granita = schimbare de tip. Paginile consecutive de ACELASI tip = un singur document
        # (ex. CIV fata+spate pe 2 pagini). Compromis: doua documente ADIACENTE de acelasi tip se
        # unesc (rar — de obicei sunt separate de alt tip); operatorul poate sparge manual.
        boundary = (cur is None) or (tid != cur["type_id"]) or (tid is not None and bool(pp.get("starts_new")))
        if cur is not None and not boundary:
            cur["page_to"] = pp["page"]
            cur["_confs"].append(pp.get("confidence"))
            continue
        if cur is not None:
            segments.append(_close(cur))
        cur = {"type_id": tid, "type_name": pp.get("type_name"), "category": pp.get("category"),
               "page_from": pp["page"], "page_to": pp["page"], "reason": pp.get("reason"),
               "_confs": [pp.get("confidence")]}
    if cur is not None:
        segments.append(_close(cur))
    # Post-merge: doua segmente CONSECUTIVE de 1 pagina fiecare, acelasi type_id, categorie
    # vehicul -> aproape sigur acelasi document fizic (fata/verso). Merge-uit intr-un singur
    # segment de 2 pagini. Rationale: e extrem de rar ca doua vehicule diferite sa aiba acte
    # de acelasi tip intr-un singur PDF de 2 pagini; operatorul poate sparge manual daca nu e.
    merged, i = [], 0
    while i < len(segments):
        seg = segments[i]
        nxt = segments[i + 1] if i + 1 < len(segments) else None
        if (nxt is not None
                and seg.get("type_id") is not None
                and seg["type_id"] == nxt.get("type_id")
                and seg.get("category") == "vehicul"
                and (seg["page_to"] - seg["page_from"]) == 0
                and (nxt["page_to"] - nxt["page_from"]) == 0):
            ms = dict(seg)
            ms["page_to"] = nxt["page_to"]
            if seg.get("confidence") and nxt.get("confidence"):
                # fata+verso: media celor doua pagini (nu min) — acelasi document fizic.
                ms["confidence"] = round((seg["confidence"] + nxt["confidence"]) / 2, 4)
            ms["reason"] = (seg.get("reason") or "") + " [+ verso: acelasi act, 2 pag]"
            merged.append(ms)
            i += 2
        else:
            merged.append(seg)
            i += 1
    return merged


def _completeness_score(data: dict, fields: list) -> float:
    """% campuri non-nule/non-goale din extract_fields. 1.0 daca nu sunt campuri (classified)."""
    if not fields:
        return 1.0
    filled = sum(1 for f in fields if data.get(f.get("name")) not in (None, "", [], {}))
    return round(filled / len(fields), 3)


def _normalize_ro_plate(plate):
    """Normalizeaza numarul de inmatriculare roman la format standard cu liniute:
    BH74UKA -> BH-74-UKA, B123ABC -> B-123-ABC, BH 74 UKA -> BH-74-UKA.
    Daca nu e format roman recognoscibil -> returneaza original (fara transformare)."""
    import re
    if not plate:
        return plate
    p = str(plate).strip().upper()
    p_clean = re.sub(r'[\s\-_./]', '', p)
    m = re.match(r'^([A-Z]{1,2})(\d{2,3})([A-Z]{2,3})$', p_clean)
    if m:
        return "%s-%s-%s" % (m.group(1), m.group(2), m.group(3))
    return plate


def _clean_token(v, sep="-"):
    """Normalizeaza o valoare de camp pentru numele documentului: MAJUSCULE, spatii->sep,
    pastreaza doar litere/cifre/_/-."""
    import re
    v = re.sub(r"\s+", sep, str(v or "").strip().upper())
    v = re.sub(r"[^A-Z0-9_\-]", "", v)
    return v.strip("-_")


def _field_val(data, *patterns):
    """Prima valoare ne-goala dintr-un dict a carei CHEIE se potriveste cu unul din pattern-uri."""
    import re
    if not isinstance(data, dict):
        return None
    for k, v in data.items():
        if v in (None, "", [], {}):
            continue
        kl = str(k).lower()
        for p in patterns:
            if re.search(p, kl):
                return str(v).strip()
    return None


def _derive_part_name(t, data):
    """Nume derivat pentru un document REZULTAT din spargere: 'TIP_<identificator>'.
    Ex: TALON_VL-48-SIL, CIV_W1V5KD3..., PERMIS_NUME_PRENUME, Buletin_NUME_PRENUME.
    Contract -> numele contractului (+ _CLIENT daca exista). Fallback -> numele tipului.
    Folosit DOAR pe calea de spargere (parti); un singur document nu trece pe aici."""
    import re
    if not t:
        return None
    cat = (t.get("category") or "").lower()
    nm = (t.get("name") or "").strip()
    nml = nm.lower()
    plate = _normalize_ro_plate(_field_val(data, r"licence plate", r"inmatricul", r"\bplate\b", r"\(a\.\)"))
    vin = _field_val(data, r"\bvin\b", r"sasiu", r"chassis", r"\(e\.\)")
    nume = _field_val(data, r"\bnume\b", r"surname", r"last ?name", r"family ?name")
    prenume = _field_val(data, r"prenume", r"first ?name", r"given ?name")
    if cat == "vehicul":
        if ("talon" in nml) or ("inmatricul" in nml):
            if plate:
                return "TALON_" + _clean_token(plate, "-")
        if ("civ" in nml) or ("identitate vehicul" in nml):
            if vin:
                return "CIV_" + _clean_token(vin, "")
        key = plate or vin
        if key:
            pref = "TALON" if (("talon" in nml) or ("inmatricul" in nml)) else ("CIV" if "civ" in nml else "VEHICUL")
            return pref + "_" + _clean_token(key, "-")
        return nm
    if cat == "sofer":
        full = "_".join([x for x in [_clean_token(nume, "-"), _clean_token(prenume, "-")] if x])
        if "permis" in nml:
            return ("PERMIS_" + full) if full else nm
        if ("identitate" in nml) or ("buletin" in nml):
            return ("Buletin_" + full) if full else nm
        return ((nm.split()[0].upper() + "_" + full) if full else nm)
    if cat == "contract":
        client = _field_val(data, r"client", r"beneficiar", r"denumire", r"firma", r"company", r"cumparator")
        base = re.sub(r"\s+", "_", nm)
        return base + ("_" + _clean_token(client, "-") if client else "")
    return nm


def _process_multidoc_pages(db, att, segments, path, mime, catalog, cmodel) -> str:
    """Un atasament PDF scanat cu mai multe documente pe intervale de pagini: cate o parte
    (part_no=1..N) per segment. Tip valid -> extragere VIZUALA pe DOAR paginile acelui document;
    segment neidentificat -> needs_review 'set aside' (fara extragere). Idempotent."""
    aid = att["id"]
    tmap = {t["id"]: t for t in catalog}
    # Retine retry_reclassify per part INAINTE de DELETE (pentru retry awareness dupa stergere)
    _prev_retries = {}
    try:
        _rr_rows = db.execute(text(
            "SELECT part_no, COALESCE(retry_reclassify,0) FROM document_extractions WHERE attachment_id=:a"
        ), {"a": aid}).fetchall()
        _prev_retries = {int(r[0]): int(r[1]) for r in _rr_rows}
    except Exception:
        pass
    db.execute(text("DELETE FROM document_extractions WHERE attachment_id=:a"), {"a": aid})
    db.commit()
    n = 0
    for seg in segments[:MAX_PAGES]:
        n += 1
        pf, pt = int(seg["page_from"]), int(seg["page_to"])
        prange = ("p%d" % (pf + 1)) if pf == pt else ("p%d-%d" % (pf + 1, pt + 1))
        tid = seg.get("type_id")
        if tid not in tmap:
            tid = None
        conf = seg.get("confidence")
        # Prag de confidenta la nivel de segment: sub DOC_SEGMENT_CONF_MIN -> necunoscut (verifica
        # manual). Asa un 'doc' la ~85% ramane necunoscut in loc sa fie incadrat gresit pe un tip.
        low_conf = (tid is not None and conf is not None and conf < DOC_SEGMENT_CONF_MIN)
        if (not tid) or low_conf:
            if low_conf:
                _reason = ("incredere %d%% sub pragul de %d%% — necunoscut (verifica manual)"
                           % (round(conf * 100), round(DOC_SEGMENT_CONF_MIN * 100)))
            else:
                _reason = (seg.get("reason") or "pagini neidentificate — lasate deoparte")
            _save_extraction(db, att, status="needs_review", method="vision-pages", model=cmodel,
                             category=None, detected_type="neidentificat",
                             document_type_id=None, confidence=conf, confidence_reason=_reason,
                             part_no=n, part_label=("Necunoscut (%s)" % prange),
                             page_from=pf, page_to=pt)
            _auto_dismiss_neidentificat(db, att["id"], n)
            continue
        t = _get_type(db, tid)
        cat = tmap[tid]["category"]
        tname = tmap[tid]["name"]
        data, emodel, eerr, status = None, cmodel, None, "classified"
        econf, emeth = None, None
        if _type_extracts(t):
            # Segment MULTI-PAGINA (interval pf..pt): ramane local pana IRIS suporta multi-fisier.
            vfiles = _render_page_range(path, mime, pf, pt)
            data, emodel, eerr, econf, emeth = _extract_fields(
                db, t, path, mime, None, tid, files=vfiles)
            status = "extracted" if data is not None else "needs_review"
        # Denumire derivata DOAR la spargere: TIP_<identificator-cheie> (fallback = tip + interval).
        pname = _derive_part_name(tmap[tid], data) or ("%s (%s)" % (tname, prange))
        _prev_rr = _prev_retries.get(n, 0)
        _rr = 2 if _prev_rr >= 1 else 0
        _fields = t.get("extract_fields") or [] if t else []
        _save_extraction(db, att, status=status, method=(emeth or "vision-pages"), model=(emodel or cmodel),
                         category=cat, detected_type=tname, document_type_id=tid, confidence=conf,
                         data=data, error=eerr, confidence_reason=seg.get("reason"),
                         part_no=n, part_label=pname,
                         page_from=pf, page_to=pt, retry_reclassify=_rr,
                         extract_confidence=econf, extract_method=emeth)
        _maybe_auto_validate(db, aid, n, data, _fields, conf, status, ext_conf=econf)
        _maybe_trigger_reclassify(db, aid, n, data, _fields, status, _prev_rr, ext_conf=econf)
    logger.info("multidoc-pages att %s -> %d parti", aid, n)
    return "multidoc_pages:%d" % n



_GARBLE_VOWELS = set("aeiouăâîAEIOUĂÂÎаеёиоуыэюяіАЕЁИОУЫЭЮЯІ")
_GARBLE_PUNCT_OK = set(" .,:;/\\-_()[]{}%#°'\"+&@*=<>|!?\n\t\r€$£0123456789")


def _looks_garbled(t: str) -> bool:
    """True cand OCR-ul local a produs 'gunoi' (scan ilizibil: caractere aleatorii / chirilice
    distorsionate, fragmente fara cuvinte reale) desi textul e NE-gol. Semnaleaza ca trebuie
    sa trimitem documentul pe canalul VISION in loc sa-l clasificam pe gunoi (ar fi discardat
    gresit ca 'nu e document'). Calibrat pe CIV-uri scanate (word_ratio ~0.03-0.04) vs.
    documente reale, chiar prost-OCR-uite (word_ratio >=0.27). Pur local, fara cost."""
    t = (t or "").strip()
    if not t:
        return False
    nz = sum(1 for c in t if not c.isspace())
    if nz < 40:                       # prea putin text -> tratat ca 'gol' pe cealalta cale
        return False
    weird = sum(1 for c in t if (not c.isspace()) and (not c.isalnum()) and (c not in _GARBLE_PUNCT_OK))
    if weird / nz >= 0.12:            # densitate mare de simboluri exotice
        return True
    toks = t.split()
    if len(toks) < 10:
        return False

    def _wordlike(tok):
        al = sum(1 for c in tok if c.isalpha())
        return len(tok) >= 3 and (al / len(tok)) >= 0.7 and any(c in _GARBLE_VOWELS for c in tok)

    wl = sum(1 for tk in toks if _wordlike(tk))
    return (wl / len(toks)) < 0.18    # aproape niciun cuvant real -> gunoi


_SKIP_SENDERS_KEY = "documents.skip_senders"
# Expeditori INTERNI (scannerul nostru) ale caror atasamente NU se analizeaza: sunt scanate
# de noi, nu primite de la client. Configurabil via settings (lista jsonb de adrese, lowercase).
# Fail-safe la {print@cargotrack.ro} daca setarea lipseste/pica. Filtrul principal e in drain
# (atasamentul nu primeste rand); garda de aici acopera caile non-automate (force=False).


def _skip_senders(db) -> set:
    try:
        r = db.execute(text("SELECT value FROM settings WHERE key=:k"),
                       {"k": _SKIP_SENDERS_KEY}).fetchone()
        v = r._mapping["value"] if r else None
        if isinstance(v, list):
            return {str(x).strip().lower() for x in v if str(x).strip()}
    except Exception:
        logger.exception("citire %s esuata — fallback print@", _SKIP_SENDERS_KEY)
    return {"print@cargotrack.ro"}


def _process_attachment(db, att, force=False) -> str:
    """Proceseaza un atasament -> upsert in document_extractions. Idempotent.
    force=True (Reidentifica) trece peste protectia reviewed."""
    aid = att["id"]
    if not force:
        # bool_or: un atasament poate avea MAI MULTE parti (part_no>0); daca ORICARE e reviewed,
        # protejam tot atasamentul de reprocesarea automata.
        ex = db.execute(text("SELECT bool_or(reviewed) FROM document_extractions WHERE attachment_id=:a"),
                        {"a": aid}).fetchone()
        if ex and ex[0]:
            return "reviewed_skip"
        # Expeditor intern (scanner) -> nu analizam (doar atasamentele de la clienti).
        skip = _skip_senders(db)
        if skip:
            fr = db.execute(text("SELECT lower(from_address) FROM emails WHERE id=:e"),
                            {"e": att.get("email_id")}).fetchone()
            if fr and (fr[0] or "") in skip:
                return _discard_attachment(db, att, "expeditor intern (scanner) — neanalizat")
    name = att.get("name") or ""
    mime = (att.get("content_type") or "").lower()
    ext = (os.path.splitext(name)[1] or "").lower()
    # Retine retry_reclassify existent (part_no=0) inainte de procesare — pentru a stii daca e un retry
    _prev_retry_0 = db.execute(text(
        "SELECT COALESCE(retry_reclassify,0) FROM document_extractions "
        "WHERE attachment_id=:a AND part_no=0 LIMIT 1"
    ), {"a": aid}).scalar() or 0
    # --- pre-filtru ieftin, INAINTE de OCR (nu sarim peste jpeg/png mari = posibile poze de talon) ---
    # Arhive (zip/rar/7z/...) -> excluse DIN START, neprocesate (nu deschidem, nu facem OCR/AI).
    if ext in (".zip", ".rar", ".7z", ".gz", ".tgz", ".tar", ".bz2", ".xz") or \
       any(k in mime for k in ("zip", "compressed", "x-rar", "x-7z", "x-tar", "gzip")):
        return _discard_attachment(db, att, "arhiva (" + (ext or mime or "?") + ") — neprocesat")
    if "svg" in mime or ext == ".svg":
        return _discard_attachment(db, att, "imagine vectoriala (svg)")
    path = _host_path(att.get("storage_path"))
    if not path or not os.path.exists(path):
        return _save_extraction(db, att, status="failed", error="fisier indisponibil pe disc")
    is_pdf = ("pdf" in mime) or ext == ".pdf"
    is_image = mime.startswith("image/") or ext in (
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff")
    # Tip neprocesabil (zip/xml/docx/eml/etc.) -> NEIDENTIFICAT, NU needs_vision.
    # Fallback magic-bytes: octet-stream care e de fapt PDF (%PDF-) e tratat ca PDF.
    if not (is_pdf or is_image):
        try:
            with open(path, "rb") as fh:
                head = fh.read(5)
        except Exception:
            head = b""
        if head[:4] == b"%PDF":
            is_pdf = True
        else:
            return _discard_attachment(db, att, "tip de fisier neprocesabil (" + (ext or mime or "?") + ")")
    try:
        sz = os.path.getsize(path)
    except Exception:
        sz = None
    if is_image and sz is not None and sz < MIN_IMG_BYTES:
        return _discard_attachment(db, att, "imagine foarte mica (logo/iconita)")
    # --- text (OCR local; fallback pe canalul vision extern pentru scanate/poze) ---
    doc_text, method, verr = _doc_text_vision(path, mime)
    if not (doc_text or "").strip():
        if _is_transient_ai_err(verr):
            logger.warning("doc vision transient (att %s) — lasat in coada pt. retry: %s",
                           att.get("id"), (verr or "")[:80])
            return "retry_transient"
        _save_extraction(db, att, status="needs_review", method=method,
                         detected_type="neidentificat", document_type_id=None,
                         confidence=None,
                         confidence_reason="text/OCR/vision gol — document necitibil")
        _auto_dismiss_neidentificat(db, att["id"], 0)
        return "needs_review"
    # --- clasificare (categorie + tip) ---
    catalog = _types_catalog(db)
    # ── PDF SCANAT multi-pagina: segmentare per-pagina (documente pe intervale de pagini) ──
    # Gardat la SCANATE (method pdf_ocr/vision sau OCR inutilizabil) ca sa NU adaugam apeluri
    # vision pe PDF-urile DIGITALE multi-pagina (contracte) — alea raman pe clasificarea ieftina
    # pe text. Rutam pe calea multi-pagina doar la semnal tare (>=2 documente identificate, sau
    # >=1 identificat + pagini neidentificate). Esec tranzitoriu -> retry; restul -> calea normala.
    _seg_unusable = len((doc_text or "").strip()) < 40 or _looks_garbled(doc_text)
    if is_pdf and (method in ("pdf_ocr", "vision") or _seg_unusable):
        _pc = _pdf_page_count(path)
        if _pc and _pc >= 2:
            per_page = _segment_pages(path, mime, catalog, _pc)
            if per_page is None:
                logger.warning("doc segment transient (att %s) — lasat in coada pt. retry", att.get("id"))
                return "retry_transient"
            segs = _group_page_segments(per_page) if per_page else []
            ident = [g for g in segs if g.get("type_id")]
            # Roteaza spre page-rendering daca: >=2 segmente identificate, SAU
            # >=1 identificat + pagini neidentificate, SAU 1 segment identificat
            # pe MULTIPLE pagini (ex. Talon/CIV fata+verso mergeuite in un singur segment
            # de 2 pag). Asta evita incarcarea PDF-ului intreg (>14MB) in vision-classify.
            _multi_page_seg = any(
                g.get("page_to", 0) > g.get("page_from", 0) for g in ident)
            if (len(ident) >= 2) or (len(ident) >= 1 and len(segs) > len(ident)) \
                    or (len(ident) >= 1 and _multi_page_seg):
                return _process_multidoc_pages(db, att, segs, path, mime, catalog, None)
    csys = _build_classify_system(_get_classify_prompt(db), catalog)
    _pm = _title_prematch(doc_text, catalog)
    if _pm:
        csys += ("\n\nINDICIU TITLU (determinist): textul contine titlul tipului id=%s (\"%s\"). "
                 "Daca documentul corespunde acelui tip, prefera-l; altfel ignora indiciul." % (_pm[0], _pm[1]))
    # Pe SCANATE (poze + PDF scanat) clasificam VIZUAL: modelul vede DIRECT imaginea si detecteaza
    # fiabil mai multe documente pe aceeasi pagina (OCR-ul pe o poza cu 3 acte iese amestecat si rateaza
    # multi-doc-ul). PDF-urile DIGITALE (strat text curat) raman pe clasificarea pe text (ieftin).
    ocr_unusable = len((doc_text or "").strip()) < 40 or _looks_garbled(doc_text)
    use_vision_cls = is_image or (is_pdf and (method in ("pdf_ocr", "vision") or ocr_unusable))
    if use_vision_cls:
        cls, cmodel, cerr = _classify_attachment_vision(csys, path, mime, doc_text, name)
        if cls is not None:
            method = "vision"
    else:
        cls, cmodel, cerr = _classify_attachment(csys, doc_text, name)
    if cls is None and _is_transient_ai_err(cerr):
        logger.warning("doc classify transient (att %s) — lasat in coada pt. retry: %s",
                       att.get("id"), (cerr or "")[:80])
        return "retry_transient"
    # Rescue prin vision: o imagine/PDF scanat care nu se poate clasifica e re-transcris o data.
    # COST: rulam vision DOAR cand OCR-ul local e INUTILIZABIL — fie ~GOL, fie GUNOI (scan ilizibil
    # cu caractere aleatorii/chirilice distorsionate; ex. CIV-uri trimise ca PDF scanat care ies
    # cu word_ratio ~0.03). Daca OCR a produs text LIZIBIL si tot NU se incadreaza intr-un tip
    # cunoscut (ordin de plata, factura, extras bancar, contract nedefinit), avem increderea ca NU
    # e un document de-al nostru -> NU cheltuim un apel vision aiurea (gardat de _looks_garbled).
    # Gardă: method != 'vision' previne dubla-rulare; clasificare neidentificata limiteaza la cazuri reale.
    # (Cand am clasificat deja VIZUAL — use_vision_cls — nu mai facem rescue: imaginea a fost autoritara.)
    scanned_like = is_image or is_pdf
    if (not use_vision_cls) and method != "vision" and scanned_like and ocr_unusable \
       and (cls is None or _cls_unidentified(cls)):
        vtxt, verr2 = _vision_transcribe(path, mime)
        if (vtxt or "").strip():
            logger.info("doc vision rescue (att %s): OCR local neclasificabil -> vision (%d ch)",
                        att.get("id"), len(vtxt))
            doc_text, method = vtxt, "vision"
            cls, cmodel, cerr = _classify_attachment(csys, doc_text, name)
            if cls is None and _is_transient_ai_err(cerr):
                return "retry_transient"
        elif _is_transient_ai_err(verr2) and cls is None:
            logger.warning("doc vision rescue transient (att %s) — lasat in coada: %s",
                           att.get("id"), (verr2 or "")[:80])
            return "retry_transient"
    if cls is None:
        # Clasificare esuata NON-tranzitorie (eroare permanenta, ex. fisier prea mare pentru
        # vision-classify): pilot automat -> necunoscut -> discard, nu ramane agatat ca "Eroare".
        return _discard_attachment(db, att, "clasificare esuata (necunoscut, auto-skip): " + (cerr or ""))
    # Politica junk (user): doar documentele cu >=50% confidenta + categorie + tip cunoscut trec
    # mai departe. Restul (nu e document, fara categorie, confidenta <50%, tip necunoscut) = junk ->
    # DISCARD (sters, ascuns din liste, ne-reprocesat). Recuperabil prin restore-discarded.
    if not cls.get("is_document"):
        return _discard_attachment(db, att, (cls.get("reason") or "nu este document procesabil"))
    # MAI MULTE documente pe acelasi atasament (auto): clasificatorul a returnat un array `documents`.
    # Intram pe calea multidoc DOAR daca exista >=2 tipuri VALIDE si DISTINCTE din catalog (semnal
    # tare, evita falsele pozitive pe pagini ale aceluiasi document). Altfel cade pe single-doc.
    mdocs = cls.get("documents")
    if isinstance(mdocs, list) and len(mdocs) >= 2:
        tmap = {t["id"]: t for t in catalog}
        valid_tids = set()
        for d in mdocs:
            try:
                vt = int(d.get("type_id"))
            except Exception:
                continue
            if vt in tmap:
                valid_tids.add(vt)
        if len(valid_tids) >= 2:
            return _process_multidoc(db, att, mdocs, path, mime, doc_text, catalog, cmodel)
    category = (cls.get("category") or "").strip() or None
    if category not in ("vehicul", "sofer", "contract"):
        return _discard_attachment(
            db, att, (cls.get("reason") or "nu s-a putut incadra intr-o categorie cunoscuta"))
    try:
        conf = float(cls.get("confidence"))
    except Exception:
        conf = None
    tid = cls.get("type_id")
    tmap = {t["id"]: t for t in catalog}
    if tid not in tmap:
        tid = None
    creason = cls.get("reason")
    # Confidenta prea mica (<50%) = nu seamana clar cu niciun document -> junk -> discard.
    if conf is not None and conf < DOC_DISCARD_CONF_MIN:
        return _discard_attachment(
            db, att, "confidenta " + str(round(conf * 100)) + "% < 50% — " + (creason or "incert"))
    if not tid:
        # Categorie clara, dar NU corespunde niciunui tip din catalog -> junk (decizie user:
        # "sterge si necunoscut"). Recuperabil daca se defineste un tip nou + restore.
        return _discard_attachment(
            db, att, (creason or "categorie clara, dar tip necunoscut (nedefinit in catalog)"))
    if conf is not None and conf < AUTO_CONF_MIN:
        # Pilot automat (Task user): sub incredere-efectiva "sigura" (85%) -> necunoscut -> discard,
        # nu se mai propaga spre verificare manuala (fara CLASSIFY_CONF_MIN separat — un singur prag).
        return _discard_attachment(
            db, att, "incredere clasificare " + str(round(conf * 100)) + "% < "
            + str(round(AUTO_CONF_MIN * 100)) + "% — necunoscut (auto-skip)")
    # --- extragere conform tipului ---
    t = _get_type(db, tid)
    _eng = _doc_engine(db)
    data, emodel, eerr, status = None, cmodel, None, "classified"
    econf, emeth = None, None
    if _type_extracts(t):
        # OPS-0122: extragere prin punctul UNIC constient de motor (local|shadow|iris) + fallback local.
        data, emodel, eerr, econf, emeth = _extract_fields(db, t, path, mime, doc_text, tid)
        if data is None and _is_transient_ai_err(eerr):
            logger.warning("doc extract transient (att %s) — lasat in coada pt. retry: %s",
                           att.get("id"), (eerr or "")[:80])
            return "retry_transient"
        status = "extracted" if data is not None else "failed"
        if data is None:
            # Extragere esuata NON-tranzitorie (ex. fisier prea mare, format nesuportat):
            # pilot automat -> nu ramane agatat ca "Eroare", tratat ca necunoscut -> discard.
            return _discard_attachment(
                db, att, "extragere esuata (necunoscut, auto-skip): " + (eerr or ""))
    # Prag pilot-automat (Task user: sub incredere-efectiva "sigura" -> necunoscut, nu se propaga).
    eff_conf = float(econf) if econf is not None else conf
    if eff_conf is not None and eff_conf < AUTO_CONF_MIN:
        return _discard_attachment(
            db, att, "incredere " + str(round(eff_conf * 100)) + "% < "
            + str(round(AUTO_CONF_MIN * 100)) + "% — necunoscut (auto-skip)")
    _rr = 2 if _prev_retry_0 >= 1 else 0
    _fields = t.get("extract_fields") or [] if t else []
    _save_extraction(
        db, att, status=status, method=method, model=(emodel or cmodel),
        category=(t.get("category") if t else cls.get("category")),
        detected_type=(t.get("name") if t else cls.get("type_name")),
        document_type_id=tid, confidence=conf, data=data, raw_text=doc_text,
        confidence_reason=creason, error=eerr, retry_reclassify=_rr,
        extract_confidence=econf, extract_method=emeth)
    _maybe_auto_validate(db, aid, 0, data, _fields, conf, status, ext_conf=econf)
    _maybe_trigger_reclassify(db, aid, 0, data, _fields, status, _prev_retry_0, ext_conf=econf)
    if _eng == "shadow" and status == "extracted":
        try:
            _shadow_enqueue(aid, 0)
        except Exception:
            pass
    return status


# ── Drain background (fire-and-forget; oglindeste reports._drain_queue) ──────
_drain_lock = threading.Lock()
_drain_active = {}
# Single-flight CLUSTER-WIDE: cu 4 workeri gunicorn, garda _drain_active (per-proces) NU
# impiedica 4 drain-uri simultane (1/worker) × ThreadPoolExecutor(AI_WORKERS) = ~12 OCR
# (tesseract) concurente -> CPU thrash (load 38 pe 8 core). pg_advisory_lock pe acest key
# face ca un SINGUR drain sa ruleze pe tot clusterul; restul sar (backlog-ul ramane, e
# preluat la urmatorul tick). Un drain la viteza plina > 4 care se sufoca reciproc.
_DRAIN_LOCK_KEY = 778231


def _reclassify_part(db, att, part_row, catalog):
    """Reclasifica un segment (part_no>=1 sau part_no=0) cu retry_reclassify=1.
    Actualizeaza DOAR randul respectiv (nu sterge alte parti). Seteaza retry_reclassify=2
    indiferent de rezultat — maxim 1 retry automat per segment."""
    path = _host_path(att.get("storage_path", ""))
    if not path:
        db.execute(text("UPDATE document_extractions SET retry_reclassify=2, updated_at=now() WHERE id=:id"),
                   {"id": part_row["id"]})
        db.commit()
        return
    mime = _attachment_mime(path, att.get("content_type", ""))
    pf = int(part_row.get("page_from") or 0)
    pt = int(part_row.get("page_to") or pf)
    tmap = {t["id"]: t for t in catalog}
    csys = _build_classify_system(_get_classify_prompt(db), catalog)
    vfiles = _render_page_range(path, mime, pf, pt)
    if not vfiles:
        db.execute(text("UPDATE document_extractions SET retry_reclassify=2, updated_at=now() WHERE id=:id"),
                   {"id": part_row["id"]})
        db.commit()
        return
    try:
        cls, cmodel, _ = _classify_attachment_vision(csys, path, mime, "", att.get("name", ""))
    except Exception as e:
        logger.exception("reclassify_part vision failed att=%s part=%s", att.get("id"), part_row["id"])
        db.execute(text("UPDATE document_extractions SET retry_reclassify=2, updated_at=now() WHERE id=:id"),
                   {"id": part_row["id"]})
        db.commit()
        return
    tid = cls.get("type_id") if cls else None
    if tid not in tmap:
        tid = None
    if not cls or not cls.get("is_document") or not tid:
        db.execute(text(
            "UPDATE document_extractions SET document_type_id=null, category=null, "
            "detected_type='neidentificat', status='discarded', retry_reclassify=2, "
            "reviewed=true, auto_validated=true, auto_validated_at=now(), "
            "confidence=:conf, error='reclasificat: tip necunoscut sau nu este un document', "
            "updated_at=now() WHERE id=:id"
        ), {"id": part_row["id"], "conf": (cls.get("confidence") if cls else None)})
        db.commit()
        logger.info("reclassify_part -> necunoscut att=%s part=%s", att.get("id"), part_row["id"])
        return
    t = _get_type(db, tid)
    data, emodel, eerr = None, cmodel, None
    econf, emeth = None, None
    if _type_extracts(t):
        # Segment MULTI-PAGINA: ramane local pana IRIS suporta multi-fisier (prin punctul UNIC).
        vf2 = _render_page_range(path, mime, pf, pt)
        data, emodel, eerr, econf, emeth = _extract_fields(db, t, path, mime, None, tid, files=vf2)
        status = "extracted" if data is not None else "needs_review"
    else:
        status = "classified"
    pname = _derive_part_name(tmap[tid], data) or tmap[tid]["name"]
    conf = cls.get("confidence")
    fields = t.get("extract_fields") or [] if t else []
    cs = _completeness_score(data or {}, fields) if data else 0.0
    # Confidenta de EXTRAGERE IRIS (cand exista) bate confidenta de clasificare la auto-validare.
    _eff = econf if econf is not None else (conf or 0)
    av = (status in ("extracted", "classified") and (_eff or 0) >= AUTO_VALID_CONF_MIN
          and cs >= AUTO_VALID_COMPLETENESS_MIN)
    db.execute(text(
        "UPDATE document_extractions SET document_type_id=:tid, category=:cat, "
        "detected_type=:dt, status=:st, data=CAST(:data AS jsonb), confidence=:conf, "
        "model=:model, error=:err, part_label=:plabel, retry_reclassify=2, "
        "extract_confidence=:econf, extract_method=:emeth, "
        "completeness_score=:cs, reviewed=:rv, auto_validated=:av, "
        "auto_validated_at=CASE WHEN :av THEN now() ELSE null END, "
        "updated_at=now() WHERE id=:id"
    ), {"id": part_row["id"], "tid": tid, "cat": tmap[tid]["category"],
        "dt": tmap[tid]["name"], "st": status, "data": json.dumps(data or {}),
        "conf": conf, "model": emodel, "err": eerr, "plabel": pname,
        "econf": econf, "emeth": emeth,
        "cs": cs, "rv": av, "av": av})
    db.commit()
    # Nume 'gata procesat' si pe calea de reclasificare (altfel partile reclasificate ar pastra
    # eticheta interna TIP_<id> in loc de numele standard). Vehicul -> determinist din `data`.
    try:
        _rename_doc(db, att["id"], part_row.get("part_no", 0), tmap[tid]["name"], "",
                    att.get("name"), data=data, email_id=att.get("email_id"))
    except Exception:
        logger.exception("rename_doc(reclassify) att=%s part=%s", att.get("id"), part_row.get("id"))
    logger.info("reclassify_part -> %s (av=%s cs=%.2f) att=%s part=%s", status, av, cs, att.get("id"), part_row["id"])


def _drain_doc_extractions(scope="recent", limit=500, force=False):
    """scope: 'today' (azi), 'recent' (ultimele 2 zile), 'all' (tot, manual explicit),
    'auto' (DOAR atasamente din emailuri primite DUPA activarea automatizarii — `enabled_at`).
    Toate cer d.id IS NULL. `force=True` (actiune manuala) ignora comutatorul si NU se opreste
    la STOP; `force=False` (cron auto) verifica STOP-ul per atasament si abandoneaza restul."""
    # Conexiune DEDICATA pentru lock-ul de sesiune: o tinem deschisa (fara commit/rollback)
    # pe toata durata drain-ului, ca lock-ul advisory sa NU se intoarca in pool pe alta sesiune.
    lock_db = SessionLocal()
    locked = False
    try:
        locked = bool(lock_db.execute(text("SELECT pg_try_advisory_lock(:k)"),
                                      {"k": _DRAIN_LOCK_KEY}).scalar())
        if not locked:
            logger.info("doc drain: deja in curs pe alt worker (advisory lock) — skip scope=%s", scope)
            return
        db = SessionLocal()
        try:
            # Selecteaza atasamente NEINCEPUTE sau cu status='failed' (retry automat).
            # Retry: numai daca nu exista niciun rand cu status != failed (adica totul a esuat),
            # niciun rand reviewed, si ultima incercare a fost cu >10 min in urma (debounce).
            base = ("SELECT a.id, a.email_id, a.name, a.content_type, a.storage_path "
                    "FROM attachments a JOIN emails e ON e.id=a.email_id "
                    "WHERE NOT COALESCE(a.doc_discarded, false) "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM document_extractions d WHERE d.attachment_id=a.id "
                    "  AND (d.status != 'failed' OR COALESCE(d.reviewed, false))"
                    ") "
                    "AND ("
                    "  NOT EXISTS (SELECT 1 FROM document_extractions d WHERE d.attachment_id=a.id) "
                    "  OR EXISTS ("
                    "    SELECT 1 FROM document_extractions d WHERE d.attachment_id=a.id "
                    "    AND d.status = 'failed' AND COALESCE(d.reviewed, false) = false "
                    "    AND d.updated_at < NOW() - INTERVAL '10 minutes'"
                    "  )"
                    ")")
            # Expeditori interni (scanner) -> atasamentele lor NU primesc rand (nu se proceseaza).
            skip = sorted(_skip_senders(db))
            if skip:
                base += " AND lower(e.from_address) NOT IN :skip"
            since = None
            if scope == "today":
                base += " AND e.received_at::date = CURRENT_DATE"
            elif scope == "all":
                pass  # fara limita de data — doar la cererea explicita a operatorului
            elif scope == "auto":
                # DOAR ce a intrat DUPA activarea automatizarii (START). Backlog-ul anterior NU
                # se proceseaza automat. Fara `enabled_at` salvat -> doar de-acum incolo (now()).
                since = _auto_since()
                base += " AND e.received_at >= :since" if since else " AND e.received_at >= now()"
            else:  # 'recent' (cron vechi): fereastra de 2 zile, evita gap-ul de la miezul noptii
                base += " AND e.received_at >= CURRENT_DATE - INTERVAL '2 days'"
            q = base + " ORDER BY a.id DESC LIMIT :lim"
            stmt = text(q)
            params = {"lim": limit}
            if since:
                params["since"] = since
            if skip:
                stmt = stmt.bindparams(bindparam("skip", expanding=True))
                params["skip"] = skip
            rows = [dict(r._mapping) for r in db.execute(stmt, params).fetchall()]
            if not rows:
                logger.info("doc drain: nimic de procesat (scope=%s)", scope)
                return

            def _work(att):
                # STOP efectiv: pe drain-ul AUTOMAT (force=False), daca operatorul a apasat STOP
                # intre timp, abandonam atasamentele ramase (cele in curs se termina). Manualul
                # (force=True) ruleaza pana la capat indiferent de comutator.
                if not force and not _auto_enabled():
                    return "stopped"
                wdb = SessionLocal()
                try:
                    return _process_attachment(wdb, att)
                except Exception as e:
                    logger.exception("process attachment %s failed", att.get("id"))
                    try:
                        _save_extraction(wdb, att, status="failed", error=str(e)[:500])
                    except Exception:
                        pass
                    return "failed"
                finally:
                    wdb.close()

            with ThreadPoolExecutor(max_workers=AI_WORKERS) as ex:
                list(ex.map(_work, rows))
            logger.info("doc drain done scope=%s n=%s", scope, len(rows))
            # auto-grupare: imaginile care sunt acelasi document fizic (fata/verso) per email
            for _eid in {r.get("email_id") for r in rows if r.get("email_id")}:
                try:
                    _gdb = SessionLocal()
                    try:
                        _autogroup_email_images(_gdb, _eid)
                    finally:
                        _gdb.close()
                except Exception:
                    logger.exception("autogroup email %s failed", _eid)
            # auto-grupare FATA/VERSO PDF (acelasi numar de inmatriculare)
            for _eid_pdf in {r.get("email_id") for r in rows if r.get("email_id")}:
                try:
                    _gpdb = SessionLocal()
                    try:
                        _autogroup_fata_verso_pdf(_gpdb, _eid_pdf)
                    finally:
                        _gpdb.close()
                except Exception:
                    logger.exception("autogroup fata/verso pdf email %s failed", _eid_pdf)
            # auto-dismiss pagini neidentificate din multi-doc (nu pot fi revizuite de operator)
            try:
                db.execute(text(
                    "UPDATE document_extractions SET reviewed=true, auto_validated=true, "
                    "auto_validated_at=now(), updated_at=now() "
                    "WHERE part_no>=1 AND detected_type='neidentificat' "
                    "AND COALESCE(reviewed,false)=false "
                    "AND status IN ('needs_review','classified')"
                ))
                db.commit()
            except Exception:
                logger.exception("auto-dismiss neidentificat failed")
            # ── Retry reclassify ──────────────────────────────────────────────────
            # Single-doc (part_no=0): reprocess complet (extrage prev_retry intern)
            try:
                _sr = [dict(r._mapping) for r in db.execute(text(
                    "SELECT DISTINCT a.id, a.email_id, a.name, a.content_type, a.storage_path "
                    "FROM attachments a JOIN document_extractions d ON d.attachment_id=a.id "
                    "WHERE d.part_no=0 AND d.retry_reclassify=1 "
                    "AND d.updated_at < NOW() - INTERVAL '10 minutes' "
                    "AND NOT COALESCE(a.doc_discarded, false)"
                )).fetchall()]
                for _r in _sr:
                    try:
                        _wdb = SessionLocal()
                        try:
                            _process_attachment(_wdb, _r, force=False)
                        finally:
                            _wdb.close()
                    except Exception:
                        logger.exception("retry reclassify single-doc att=%s", _r.get("id"))
                if _sr:
                    logger.info("doc drain retry single-doc: %d", len(_sr))
            except Exception:
                logger.exception("retry reclassify single-doc query failed")
            # Multi-doc parts (part_no>=1): reclasifica doar segmentul respectiv
            try:
                _catalog_rr = _types_catalog(db)
                _mr = [dict(r._mapping) for r in db.execute(text(
                    "SELECT d.id as ex_id, d.attachment_id, d.part_no, d.page_from, d.page_to, "
                    "a.email_id, a.name, a.content_type, a.storage_path "
                    "FROM document_extractions d JOIN attachments a ON a.id=d.attachment_id "
                    "WHERE d.part_no>=1 AND d.retry_reclassify=1 "
                    "AND d.updated_at < NOW() - INTERVAL '10 minutes' "
                    "AND NOT COALESCE(a.doc_discarded, false)"
                )).fetchall()]
                for _r in _mr:
                    _att = {"id": _r["attachment_id"], "email_id": _r["email_id"],
                            "name": _r["name"], "content_type": _r["content_type"],
                            "storage_path": _r["storage_path"]}
                    _part = {"id": _r["ex_id"], "part_no": _r["part_no"],
                             "page_from": _r["page_from"], "page_to": _r["page_to"]}
                    try:
                        _rcldb = SessionLocal()
                        try:
                            _reclassify_part(_rcldb, _att, _part, _catalog_rr)
                        finally:
                            _rcldb.close()
                    except Exception:
                        logger.exception("retry reclassify part=%s att=%s", _r["ex_id"], _r["attachment_id"])
                        try:
                            db.execute(text("UPDATE document_extractions SET retry_reclassify=2 WHERE id=:id"),
                                       {"id": _r["ex_id"]})
                            db.commit()
                        except Exception:
                            pass
                if _mr:
                    logger.info("doc drain retry multi-parts: %d", len(_mr))
            except Exception:
                logger.exception("retry reclassify multi-parts query failed")
        except Exception:
            logger.exception("doc drain failed scope=%s", scope)
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()
    except Exception:
        logger.exception("doc drain lock/setup failed scope=%s", scope)
    finally:
        if locked:
            try:
                lock_db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _DRAIN_LOCK_KEY})
            except Exception:
                pass
        lock_db.close()


# ── Comutator automatizare (START/STOP procesare documente) ──
# Setting persistent (settings.value jsonb {"enabled": bool}), partajat intre cei 4 workeri.
# OFF => drain-ul AUTOMAT (declansat de cron-ul de emailuri) sare; categorisirea emailurilor
# (informatii/sesizare/reclamatie) NU e afectata. Actiunea manuala explicita ("Proceseaza acum")
# foloseste force=True si ruleaza oricum. Lipsa randului = ON (compatibilitate cu comportamentul
# anterior). Fail-open la ON daca citirea pica (un blip DB nu inghet tot pipeline-ul).
_AUTO_KEY = "documents.auto_processing"


def _auto_enabled() -> bool:
    db = SessionLocal()
    try:
        r = db.execute(text("SELECT value FROM settings WHERE key=:k"), {"k": _AUTO_KEY}).fetchone()
        if not r or r._mapping["value"] is None:
            return True  # absent = ON (default istoric)
        v = r._mapping["value"]
        return bool(v.get("enabled", True)) if isinstance(v, dict) else True
    except Exception:
        logger.exception("citire %s esuata — fail-open ON", _AUTO_KEY)
        return True
    finally:
        db.close()


def _set_auto(enabled: bool, by: str = None) -> bool:
    db = SessionLocal()
    desc = "Procesare automata documente (extragere tip+date din atasamente). OFF = skip."
    try:
        if enabled:
            # La START stampilam `enabled_at` = acum -> drain-ul auto proceseaza DOAR ce intra
            # dupa acest moment (nu backlog-ul istoric). Re-pornirea reseteaza momentul.
            db.execute(text(
                "INSERT INTO settings(key, value, description, updated_by, updated_at) "
                "VALUES (:k, jsonb_build_object('enabled', true, 'enabled_at', to_jsonb(now())), "
                "        :d, :by, now()) "
                "ON CONFLICT (key) DO UPDATE SET "
                "  value = jsonb_build_object('enabled', true, 'enabled_at', to_jsonb(now())), "
                "  updated_by=EXCLUDED.updated_by, updated_at=now()"),
                {"k": _AUTO_KEY, "d": desc, "by": by})
        else:
            # La STOP doar setam enabled=false (pastram enabled_at, irelevant cat e oprit).
            db.execute(text(
                "INSERT INTO settings(key, value, description, updated_by, updated_at) "
                "VALUES (:k, jsonb_build_object('enabled', false), :d, :by, now()) "
                "ON CONFLICT (key) DO UPDATE SET "
                "  value = jsonb_set(COALESCE(settings.value, '{}'::jsonb), '{enabled}', 'false'::jsonb), "
                "  updated_by=EXCLUDED.updated_by, updated_at=now()"),
                {"k": _AUTO_KEY, "d": desc, "by": by})
        db.commit()
        return bool(enabled)
    except Exception:
        db.rollback()
        logger.exception("scriere %s esuata", _AUTO_KEY)
        raise
    finally:
        db.close()


def _auto_since():
    """Momentul (ISO/timestamptz) de la care automatizarea proceseaza — `enabled_at`. None daca lipseste."""
    db = SessionLocal()
    try:
        return db.execute(text("SELECT value->>'enabled_at' FROM settings WHERE key=:k"),
                          {"k": _AUTO_KEY}).scalar()
    except Exception:
        logger.exception("citire enabled_at esuata")
        return None
    finally:
        db.close()


def _kick_drain(scope="new", force=False) -> bool:
    """Porneste drain-ul intr-un thread daemon, fara suprapunere pe acelasi scope.
    Best-effort: NICIODATA nu arunca catre apelant (ex. process_email).
    force=True = actiune manuala explicita (ignora comutatorul de automatizare)."""
    try:
        if not force and not _auto_enabled():
            logger.info("doc drain: automatizare OPRITA — skip scope=%s", scope)
            return False
        with _drain_lock:
            if _drain_active.get(scope):
                return False
            _drain_active[scope] = True

        def _run():
            try:
                _drain_doc_extractions(scope, force=force)
            finally:
                with _drain_lock:
                    _drain_active[scope] = False

        threading.Thread(target=_run, daemon=True).start()
        return True
    except Exception:
        logger.exception("kick_drain failed scope=%s", scope)
        return False


# ── Grupare manuala atasamente (acelasi email = un document, ex. talon fata+spate) ──
def _member_text(db, row) -> tuple:
    """Textul unui rand de grup: raw_text salvat daca exista, altfel re-OCR din disc.
    row: dict cu raw_text, method, content_type, storage_path."""
    rt = (row.get("raw_text") or "").strip()
    if rt:
        return rt, (row.get("method") or "stored")
    path = _host_path(row.get("storage_path"))
    if not path or not os.path.exists(path):
        return "", None
    try:
        txt, method, _verr = _doc_text_vision(path, row.get("content_type") or "")
        return txt, method
    except Exception:
        logger.exception("member OCR/vision failed att %s", row.get("attachment_id"))
        return "", None


def _group_member_rows(db, primary_id) -> list:
    """Primarul + membrii (grouped_into=primary_id), ordonati pentru extragerea combinata."""
    return [dict(r._mapping) for r in db.execute(text(
        "SELECT d.id AS ex_id, d.attachment_id, d.raw_text, d.method, "
        "       a.name AS att_name, a.content_type, a.storage_path, "
        "       (d.id = :pid) AS is_primary "
        "FROM document_extractions d JOIN attachments a ON a.id=d.attachment_id "
        "WHERE d.id = :pid OR d.grouped_into = :pid "
        "ORDER BY (d.id = :pid) DESC, a.id ASC"), {"pid": primary_id}).fetchall()]


def _extract_group(db, primary_id) -> str:
    """Re-extrage datele pe textul COMBINAT al grupului (primar + membri) si scrie pe primar.
    NU suprascrie raw_text-ul propriu al primarului (textul combinat e efemer => ungroup curat).
    Eroare tranzitorie (502) -> 'retry_transient', primarul ramane neschimbat."""
    rows = _group_member_rows(db, primary_id)
    prim = next((r for r in rows if r.get("is_primary")), None)
    if not prim:
        return "missing"
    tid = db.execute(text("SELECT document_type_id FROM document_extractions WHERE id=:id"),
                     {"id": primary_id}).scalar()
    t = _get_type(db, tid) if tid else None
    n = len(rows) or 1
    per = max(1000, MAX_DOC_TEXT // n)   # buget per-membru: evita trunchierea spatelui
    parts, method_used = [], None
    for i, r in enumerate(rows):
        txt, m = _member_text(db, r)
        method_used = method_used or m
        if not (txt or "").strip():
            continue
        label = "primar" if r.get("is_primary") else ("pagina %d" % (i + 1))
        parts.append("----- [%s: %s] -----\n%s" % (label, r.get("att_name") or "", _clip_doc_text(txt, per)))
    combined = "\n\n".join(parts)
    if not combined.strip():
        db.execute(text(
            "UPDATE document_extractions SET status='discarded', "
            "detected_type='neidentificat', document_type_id=NULL, "
            "reviewed=true, auto_validated=true, auto_validated_at=now(), "
            "confidence_reason='grup fara text — document necitibil', updated_at=now() "
            "WHERE id=:id"
        ), {"id": primary_id})
        db.commit()
        logger.info("auto_dismiss neidentificat (grup fara text) primary_id=%s", primary_id)
        return "needs_review"
    data, emodel, eerr, status = None, None, None, "classified"
    # Grup fata/verso = inerent MULTI-PAGINA -> ramane pe extragere LOCALA pana cand endpoint-ul IRIS
    # accepta multi-fisier (outbox #16; flag doc_extract.iris_multifile). Pastram split-ul text/vision local.
    if _type_extracts(t):
        if t.get("extract_via_vision"):
            # Vision pe grup: trimite TOATE paginile (fata+spate) ca atasamente; combined = indiciu OCR.
            sys_v = _build_doc_extract_system_vision(t["extract_prompt"], t["extract_fields"])
            mfiles = [(_host_path(r.get("storage_path")), r.get("content_type")) for r in rows]
            mfiles = [(p, m) for p, m in mfiles if p and os.path.exists(p)]
            data, emodel, eerr = _extract_doc_vision(
                sys_v, mfiles, tid, t.get("name"), fields=t["extract_fields"], ocr_hint=combined)
        else:
            system = _build_doc_extract_system(t["extract_prompt"], t["extract_fields"])
            data, emodel, eerr = _extract_doc(system, combined, tid, t.get("name"), fields=t["extract_fields"])
        if data is None and _is_transient_ai_err(eerr):
            logger.warning("group extract transient (primar %s) — neschimbat: %s", primary_id, (eerr or "")[:80])
            return "retry_transient"
        status = "extracted" if data is not None else "failed"
    cs = _completeness_score(data or {}, (t.get("extract_fields") or []) if t else []) if data else None
    db.execute(text(
        "UPDATE document_extractions SET data=CAST(:data AS jsonb), status=:st, method=:m, "
        "model=:mo, error=:err, "
        "completeness_score=COALESCE(:cs, completeness_score), "
        "extracted_at=now(), updated_at=now() WHERE id=:id"),
        {"id": primary_id, "data": json.dumps(data or {}), "st": status,
         "m": (method_used or "group")[:30], "mo": (emodel or "")[:80] or None,
         "err": (eerr or "")[:1000] or None, "cs": cs})
    db.commit()
    return status


def _reclassify_group_primary(db, primary_id) -> str:
    """Reclasifica un grup de imagini cand primarul e neidentificat (type_id=NULL).
    Trimite prima imagine + hint OCR la vision-classify, actualizeaza tipul primarului,
    apoi apeleaza _extract_group. Daca primarul are deja tip valid -> _extract_group direct."""
    rows = _group_member_rows(db, primary_id)
    prim = next((r for r in rows if r.get("is_primary")), None)
    if not prim:
        return "missing"
    tid = prim.get("document_type_id")
    if tid:
        return _extract_group(db, primary_id)
    classify_system = _get_classify_prompt(db)
    main_path = _host_path(prim.get("storage_path"))
    if not main_path or not os.path.exists(main_path):
        return "missing"
    ocr_parts = []
    for r in rows:
        txt, _ = _member_text(db, r)
        if (txt or "").strip():
            ocr_parts.append(txt.strip()[:1000])
    ocr_hint = "\n".join(ocr_parts)[:3000]
    result, _m, _e = _classify_attachment_vision(
        classify_system, main_path, prim.get("content_type") or "image/jpeg",
        doc_text=ocr_hint, att_name=prim.get("att_name"))
    if not isinstance(result, dict) or not result.get("type_id"):
        logger.info("reclassify_group primary=%s: neidentificat dupa vision", primary_id)
        return "needs_review"
    db.execute(text(
        "UPDATE document_extractions SET document_type_id=:tid, category=:cat, "
        "detected_type=:dt, confidence=:conf, status='classified', updated_at=now() "
        "WHERE id=:id"),
        {"tid": result["type_id"], "cat": result.get("category"),
         "dt": result.get("type_name"), "conf": float(result.get("confidence") or 0.7),
         "id": primary_id})
    db.commit()
    logger.info("reclassify_group primary=%s -> type_id=%s conf=%.2f",
                primary_id, result["type_id"], float(result.get("confidence") or 0.7))
    return _extract_group(db, primary_id)



def _autogroup_holistic(db, email_id):
    """Grupare AI 'din ansamblu': priveste TOATE imaginile negrupate dintr-un email odata (ancore
    identificate + pagini orfane), cu INDICIU DE TEXT per imagine, si leaga paginile care formeaza
    ACELASI document fizic — mai ales contracte multi-pagina, unde paginile de mijloc nu au
    identificatori (placa/nr contract), doar continut (articole/clauze). Decide grupurile o SINGURA
    data (inainte de PASS1/PASS2) => fara interferenta de ordine. Idempotent."""
    import base64
    import hashlib
    import time as _time
    rows = [dict(r._mapping) for r in db.execute(text(
        "SELECT d.id AS ex_id, d.attachment_id, d.document_type_id, d.detected_type, d.confidence, "
        "       d.raw_text, a.name AS att_name, a.content_type, a.storage_path "
        "FROM document_extractions d JOIN attachments a ON a.id=d.attachment_id "
        # d.reviewed_by IS NULL => include ancore auto-validate de masina; exclude doar validarile UMANE
        "WHERE d.email_id=:eid AND COALESCE(d.part_no,0)=0 AND d.grouped_into IS NULL "
        "  AND d.reviewed_by IS NULL "
        "  AND d.status IN ('extracted','classified','needs_review') "
        "  AND lower(COALESCE(a.content_type,'')) LIKE 'image/%' "
        "ORDER BY a.name ASC, d.id ASC"), {"eid": email_id}).fetchall()]
    if len(rows) < 2:
        return 0
    # Anti-supragrupare: ruleaza DOAR daca exista cel putin o imagine INCERTA (tip necunoscut sau
    # confidence < 0.85). Un set curat de documente distincte (toate identificate sigur) ramane neatins.
    if not any((r.get("document_type_id") is None) or ((r.get("confidence") or 0) < 0.85) for r in rows):
        return 0
    atts, listing, id_map, total = [], [], {}, 0
    for idx, m in enumerate(rows, 1):
        id_map[int(m["attachment_id"])] = m
        ident = m.get("detected_type") or "necunoscut"
        try:
            cpct = int(round((m.get("confidence") or 0) * 100))
        except Exception:
            cpct = 0
        snippet = " ".join((m.get("raw_text") or "").split())[:360] or "(fara text)"
        listing.append("Imaginea %d: fisier=\"%s\" att_id=%s tip=\"%s\" (%d%%)\n  text: %s"
                       % (idx, m.get("att_name") or "?", m["attachment_id"], ident, cpct, snippet))
        path = _host_path(m.get("storage_path"))
        if path and os.path.exists(path):
            try:
                sz = os.path.getsize(path)
                if sz <= VISION_MAX_BYTES and total + sz <= VISION_MAX_BYTES:
                    with open(path, "rb") as fh:
                        atts.append({"mime_type": _attachment_mime(path, m.get("content_type")),
                                     "data_base64": base64.b64encode(fh.read()).decode("ascii")})
                        total += sz
            except Exception:
                pass
    system = (
        "Primesti TOATE imaginile (in ordinea numelui de fisier) dintr-un email, fiecare cu un indiciu "
        "de text. Unele sunt pagini ale ACELUIASI document fizic scanat in mai multe poze — mai ales "
        "CONTRACTE multi-pagina. Priveste ANSAMBLUL si leaga paginile care formeaza un singur document.\n\n"
        "Recunoasterea unui document multi-pagina:\n"
        "- pagina ANTET identificata (titlu, parti, numar contract) + pagini de CONTINUARE cu doar "
        "continut (ART. 1, ART. 6, ART. 8..., clauze, '7.5.', '13.2.') fara identificatori + pagina "
        "FINALA cu semnaturi => TOATE acelasi document;\n"
        "- numerotare secventiala de fisier (001,002,003,004 / WA0001.. / IMG_1,2,3) in acelasi email;\n"
        "- articole/sectiuni care CONTINUA de la o pagina la alta (ART.6 -> ART.8 -> ART.13);\n"
        "- acelasi numar de contract / aceleasi parti pe paginile care le contin;\n"
        "- o pagina cu MULT text si tip incert / confidenta mica, asezata intre sau langa paginile unui "
        "contract, apartine cel mai probabil ACELUI contract (NU e document separat).\n\n"
        "NU grupa documente DISTINCTE: taloane/CIV ale unor vehicule DIFERITE, contracte cu numere sau "
        "parti diferite, acte ale unor persoane diferite. La dubiu intre 'pagina de continuare' si "
        "'document nou', daca pagina nu are titlu/identificatori proprii, inclin-o spre CONTINUARE.\n\n"
        "Raspunde DOAR JSON (fara ```): {\"groups\": [[att_id, att_id, ...], ...]} — fiecare sub-lista = "
        "att_id-urile paginilor ACELUIASI document fizic (minim 2 membri). Daca niciun grup: {\"groups\": []}."
    )
    content = "Imaginile:\n" + "\n".join(listing) + "\n\nRaspunde DOAR JSON."
    digest = hashlib.sha1(("|".join(str(m["attachment_id"]) for m in rows)).encode()).hexdigest()[:12]
    task = "cargo360:doc_autogroup_holistic:%s" % digest
    _attempts, _ctimeout = _ai_budget(vision=True)
    parsed, res = None, None
    for attempt in range(_attempts):
        res = iris_ai.run_prompt(system, content, response_format="text", model_hint="sonnet",
                                 temperature=0.0, max_tokens=500, task=task, timeout=_ctimeout,
                                 attachments=(atts or None))
        if res.get("ok") or (res.get("error") or {}).get("code") not in _RETRY_CODES:
            break
        _time.sleep(1.2 * (attempt + 1))
    if res and (res.get("ok") or res.get("text")):
        parsed = res.get("parsed") if isinstance(res.get("parsed"), dict) else _salvage_json(res.get("text"))
    if not isinstance(parsed, dict):
        logger.info("autogroup holistic: raspuns invalid email=%s", email_id)
        return 0
    created, used = 0, set()
    for g in (parsed.get("groups") or []):
        if not isinstance(g, (list, tuple)):
            continue
        members = []
        for a in g:
            try:
                aid = int(a)
            except Exception:
                continue
            if aid in id_map and id_map[aid]["ex_id"] not in used:
                members.append(id_map[aid])
                used.add(id_map[aid]["ex_id"])
        if len(members) < 2:
            continue
        # Primar = pagina cel mai bine identificata (are tip + confidenta maxima) => grupul mosteneste
        # tipul CORECT (ex. contractul Polonia, nu pagina de mijloc clasificata gresit cargobox).
        primary_row = max(members, key=lambda r: (r.get("document_type_id") is not None, r.get("confidence") or 0))
        primary = primary_row["ex_id"]
        member_ids = [r["ex_id"] for r in members if r["ex_id"] != primary]
        db.execute(text("UPDATE document_extractions SET grouped_into=:pid, status='grouped', "
                        "updated_at=now() WHERE id = ANY(:ids)"),
                   {"pid": primary, "ids": member_ids})
        db.commit()
        st = _extract_group(db, primary)
        created += 1
        logger.info("autogroup holistic: email=%s primar=%s membri=%s tip=%s -> %s",
                    email_id, primary, member_ids, primary_row.get("detected_type"), st)
    return created


def _autogroup_email_images(db, email_id):
    """Dupa procesarea unui email: detecteaza atasamentele IMAGINE care sunt ACELASI document fizic
    (fata/verso sau mai multe poze ale aceluiasi act) si le grupeaza automat. Grupeaza DOAR cand un
    apel vision confirma 'acelasi document' (aceeasi placa/VIN/serie/nume) — doua acte DISTINCTE de
    acelasi tip NU se unesc. Intoarce nr. de grupuri create. Idempotent: ignora randuri reviewed/grupate."""
    import base64
    import hashlib
    import time
    # PASS 0: grupare AI 'din ansamblu' peste TOATE imaginile (ancore + orfani), cu indiciu de text.
    _holistic_n = 0
    try:
        _holistic_n = _autogroup_holistic(db, email_id) or 0
    except Exception:
        logger.exception("autogroup_holistic email=%s", email_id)
    rows = [dict(r._mapping) for r in db.execute(text(
        "SELECT d.id AS ex_id, d.attachment_id, d.document_type_id, d.category, d.detected_type, "
        "       a.name AS att_name, a.content_type, a.storage_path "
        "FROM document_extractions d JOIN attachments a ON a.id=d.attachment_id "
        "WHERE d.email_id=:eid AND d.document_type_id IS NOT NULL AND COALESCE(d.part_no,0)=0 "
        "  AND d.grouped_into IS NULL AND d.status IN ('extracted','classified','needs_review') "
        "  AND lower(COALESCE(a.content_type,'')) LIKE 'image/%' "
        "ORDER BY d.document_type_id, d.id ASC"), {"eid": email_id}).fetchall()]
    if len(rows) < 2:
        return 0
    clusters = {}
    for r in rows:
        clusters.setdefault((r.get("category"), r.get("document_type_id")), []).append(r)
    created = 0
    for (cat, tid), members in clusters.items():
        if len(members) < 2:
            continue
        atts, listing, id_map, total = [], [], {}, 0
        for idx, m in enumerate(members, 1):
            path = _host_path(m.get("storage_path"))
            if not path or not os.path.exists(path):
                continue
            try:
                sz = os.path.getsize(path)
                if sz > VISION_MAX_BYTES or total + sz > VISION_MAX_BYTES:
                    continue
                with open(path, "rb") as fh:
                    raw = fh.read()
            except Exception:
                continue
            total += len(raw)
            atts.append({"mime_type": _attachment_mime(path, m.get("content_type")),
                         "data_base64": base64.b64encode(raw).decode("ascii")})
            listing.append("Imaginea %d: fisier=\"%s\", att_id=%s"
                           % (idx, m.get("att_name") or "?", m.get("attachment_id")))
            id_map[int(m.get("attachment_id"))] = m.get("ex_id")
        if len(atts) < 2:
            continue
        tname = members[0].get("detected_type") or "document"
        system = (
            "Primesti mai multe imagini de ACELASI TIP (\"%s\") din acelasi email. "
            "Identifica imaginile care sunt SIGUR sau PROBABIL pagini ale aceluiasi document fizic.\n\n"
            "Grupeaza daca:\n"
            "- Date identice pe mai multe imagini (aceeasi placa/VIN/serie/CUI/nr. contract)\n"
            "- Fata/verso: o imagine pare spatele actului (stampile/semnatura fara identificatori), "
            "tipul vizual e acelasi cu fata — grupeaza chiar fara confirmare prin date\n"
            "- Pagini consecutive ale aceluiasi dosar/formular (acelasi antet/sigla/format)\n"
            "- Denumiri secventiale: 'Image.jpg'+'Image (2).jpg'... sau 'WA0002'+'WA0003'... "
            "in acelasi email = probabil acelasi act scanat in etape\n\n"
            "Prag: grupeaza daca probabilitatea > 65%%. NU lasa separate fata/verso doar pentru ca "
            "spatele nu are date identificatoare. Documente DISTINCTE (ex. taloane de vehicule "
            "DIFERITE) NU se grupeaza.\n\n"
            "Raspunde DOAR JSON (fara ```): {\"groups\": [[att_id, att_id, ...], ...]} "
            "unde fiecare sub-lista contine att_id-urile imaginilor care sunt ACELASI document fizic. "
            "Include DOAR grupuri cu 2+ membri. Daca niciunul: {\"groups\": []}."
        ) % tname
        content = "Imaginile, in ordine:\n" + "\n".join(listing) + "\n\nRaspunde DOAR JSON."
        digest = hashlib.sha1("|".join(str(m.get("attachment_id")) for m in members).encode()).hexdigest()[:12]
        task = "cargo360:doc_autogroup:%s" % digest
        _attempts, _ctimeout = _ai_budget(vision=True)
        parsed, res = None, None
        for attempt in range(_attempts):
            res = iris_ai.run_prompt(system, content, response_format="text", model_hint="sonnet",
                                     temperature=0.0, max_tokens=400, task=task, timeout=_ctimeout,
                                     attachments=atts)
            if res.get("ok") or (res.get("error") or {}).get("code") not in _RETRY_CODES:
                break
            time.sleep(1.2 * (attempt + 1))
        if res and (res.get("ok") or res.get("text")):
            parsed = res.get("parsed") if isinstance(res.get("parsed"), dict) else _salvage_json(res.get("text"))
        if not isinstance(parsed, dict):
            logger.info("autogroup: raspuns invalid email=%s tip=%s", email_id, tid)
            continue
        used = set()
        for g in (parsed.get("groups") or []):
            if not isinstance(g, (list, tuple)):
                continue
            ex_ids = []
            for a in g:
                try:
                    aid = int(a)
                except Exception:
                    continue
                if aid in id_map and id_map[aid] not in used:
                    ex_ids.append(id_map[aid])
                    used.add(id_map[aid])
            if len(ex_ids) < 2:
                continue
            ex_ids.sort()
            primary, member_ids = ex_ids[0], ex_ids[1:]
            db.execute(text("UPDATE document_extractions SET grouped_into=:pid, status='grouped', "
                            "updated_at=now() WHERE id = ANY(:ids)"),
                       {"pid": primary, "ids": member_ids})
            db.commit()
            st = _extract_group(db, primary)
            created += 1
            logger.info("autogroup: email=%s tip=%s primar=%s membri=%s -> %s",
                        email_id, tid, primary, member_ids, st)

    # ── PASS 2: grupare imagini neidentificate / low-confidence ──────────────────
    # Include imagini cu document_type_id IS NULL (neidentificat) SAU confidence < 0.85
    # care nu au fost grupate in Pass 1. Prompt mai permisiv, cross-type.
    unclass_rows = [dict(r._mapping) for r in db.execute(text(
        "SELECT d.id AS ex_id, d.attachment_id, d.document_type_id, d.category, d.detected_type, "
        "       d.confidence, a.name AS att_name, a.content_type, a.storage_path "
        "FROM document_extractions d JOIN attachments a ON a.id=d.attachment_id "
        "WHERE d.email_id=:eid AND COALESCE(d.part_no,0)=0 "
        "  AND d.grouped_into IS NULL "
        "  AND d.status IN ('extracted','classified','needs_review') "
        "  AND (d.document_type_id IS NULL OR d.confidence < 0.85) "
        "  AND lower(COALESCE(a.content_type,'')) LIKE 'image/%' "
        "ORDER BY d.id ASC"), {"eid": email_id}).fetchall()]
    if len(unclass_rows) >= 2:
        atts2, listing2, id_map2, total2 = [], [], {}, 0
        for idx2, m2 in enumerate(unclass_rows, 1):
            path2 = _host_path(m2.get("storage_path"))
            if not path2 or not os.path.exists(path2):
                continue
            try:
                sz2 = os.path.getsize(path2)
                if sz2 > VISION_MAX_BYTES or total2 + sz2 > VISION_MAX_BYTES:
                    continue
                with open(path2, "rb") as fh2:
                    raw2 = fh2.read()
            except Exception:
                continue
            total2 += len(raw2)
            atts2.append({"mime_type": _attachment_mime(path2, m2.get("content_type")),
                          "data_base64": base64.b64encode(raw2).decode("ascii")})
            listing2.append("Imaginea %d: fisier=\"%s\", att_id=%s"
                            % (idx2, m2.get("att_name") or "?", m2.get("attachment_id")))
            id_map2[int(m2.get("attachment_id"))] = m2.get("ex_id")
        if len(atts2) >= 2:
            system2 = (
                "Primesti mai multe imagini din acelasi email. Unele nu au putut fi identificate "
                "individual (tip necunoscut sau confidence scazut). Identifica grupuri de imagini "
                "care sunt PAGINI ALE ACELUIASI document fizic (dosar cu mai multe file, act scanat "
                "in bucati, fata+verso). NU te baza pe tipul documentului.\n\n"
                "Criterii de grupare:\n"
                "- Acelasi format/antet/sigla pe mai multe pagini\n"
                "- Denumiri secventiale: Image.jpg, Image (2).jpg, Image (3).jpg... "
                "sau WA0001/WA0002... trimise in acelasi email\n"
                "- Aspect de pagini consecutive ale unui formular sau dosar\n"
                "- Fata+verso ale aceluiasi act (spatele poate fi aproape gol)\n\n"
                "Prag permisiv: grupeaza daca probabilitatea > 55%%. "
                "Raspunde DOAR JSON (fara ```): {\"groups\": [[att_id, ...], ...]}. "
                "Include DOAR grupuri cu 2+ membri. Daca niciunul: {\"groups\": []}."
            )
            content2 = "Imaginile, in ordine:\n" + "\n".join(listing2) + "\n\nRaspunde DOAR JSON."
            digest2 = hashlib.sha1("|".join(str(m2.get("attachment_id")) for m2 in unclass_rows).encode()).hexdigest()[:12]
            task2 = "cargo360:doc_autogroup_p2:%s" % digest2
            _attempts2, _ctimeout2 = _ai_budget(vision=True)
            parsed2, res2 = None, None
            for attempt2 in range(_attempts2):
                res2 = iris_ai.run_prompt(system2, content2, response_format="text", model_hint="sonnet",
                                          temperature=0.0, max_tokens=400, task=task2, timeout=_ctimeout2,
                                          attachments=atts2)
                if res2.get("ok") or (res2.get("error") or {}).get("code") not in _RETRY_CODES:
                    break
                time.sleep(1.2 * (attempt2 + 1))
            if res2 and (res2.get("ok") or res2.get("text")):
                parsed2 = res2.get("parsed") if isinstance(res2.get("parsed"), dict) else _salvage_json(res2.get("text"))
            if isinstance(parsed2, dict):
                used2 = set()
                for g2 in (parsed2.get("groups") or []):
                    if not isinstance(g2, (list, tuple)):
                        continue
                    ex_ids2 = []
                    for a2 in g2:
                        try:
                            aid2 = int(a2)
                        except Exception:
                            continue
                        if aid2 in id_map2 and id_map2[aid2] not in used2:
                            ex_ids2.append(id_map2[aid2])
                            used2.add(id_map2[aid2])
                    if len(ex_ids2) < 2:
                        continue
                    ex_ids2.sort()
                    primary2, member_ids2 = ex_ids2[0], ex_ids2[1:]
                    db.execute(text("UPDATE document_extractions SET grouped_into=:pid, status='grouped', "
                                    "updated_at=now() WHERE id = ANY(:ids)"),
                               {"pid": primary2, "ids": member_ids2})
                    db.commit()
                    st2 = _reclassify_group_primary(db, primary2)
                    created += 1
                    logger.info("autogroup p2: email=%s primar=%s membri=%s -> %s",
                                email_id, primary2, member_ids2, st2)
            else:
                logger.info("autogroup p2: raspuns invalid email=%s", email_id)
    return created + _holistic_n



def _autogroup_fata_verso_pdf(db, email_id):
    """Detecteaza perechi PDF FATA/VERSO ale aceluiasi vehicul (acelasi numar de inmatriculare)
    si le grupeaza automat. Documentul cu completeness mai mare (de obicei VERSO) devine primar.
    Dupa grupare, ruleaza auto-validare pe primar. Idempotent."""
    import re as _re
    rows = [dict(r._mapping) for r in db.execute(text(
        "SELECT d.id AS ex_id, d.attachment_id, d.document_type_id, d.category, d.detected_type, "
        "       d.confidence, d.completeness_score, d.data, d.status, d.reviewed, "
        "       a.name AS att_name, a.content_type "
        "FROM document_extractions d JOIN attachments a ON a.id=d.attachment_id "
        "WHERE d.email_id=:eid AND d.document_type_id IS NOT NULL AND COALESCE(d.part_no,0)=0 "
        "  AND d.grouped_into IS NULL "
        "  AND d.status IN ('extracted','classified','needs_review') "
        "  AND lower(COALESCE(a.content_type,'')) NOT LIKE 'image/%%' "
        "ORDER BY d.document_type_id, d.id ASC"), {"eid": email_id}).fetchall()]
    if len(rows) < 2:
        return 0

    def _extract_plate(row):
        data = row.get("data") or {}
        plate = data.get("Licence Plate (A.)") or ""
        if plate:
            return _re.sub(r"[-\s]", "", plate.upper())
        name = (row.get("att_name") or "").upper()
        m = _re.search(r"\b([A-Z]{1,2})\s*(\d{2,3})\s*([A-Z]{2,4})\b", name)
        if m:
            return m.group(1) + m.group(2) + m.group(3)
        return ""

    by_key = {}
    for r in rows:
        plate = _extract_plate(r)
        if not plate:
            continue
        key = (r.get("document_type_id"), plate)
        by_key.setdefault(key, []).append(r)

    created = 0
    for (tid, plate), members in by_key.items():
        if len(members) < 2:
            continue

        def _score(d):
            name = (d.get("att_name") or "").upper()
            is_verso = "VERSO" in name
            cs = float(d.get("completeness_score") or 0)
            conf = float(d.get("confidence") or 0)
            return (is_verso, cs, conf)

        members_sorted = sorted(members, key=_score, reverse=True)
        primary = members_sorted[0]
        secondaries = members_sorted[1:]
        secondary_ids = [s["ex_id"] for s in secondaries]

        db.execute(text(
            "UPDATE document_extractions SET grouped_into=:pid, status='grouped', updated_at=now() "
            "WHERE id = ANY(:ids) AND grouped_into IS NULL AND COALESCE(reviewed,false)=false"
        ), {"pid": primary["ex_id"], "ids": secondary_ids})
        db.commit()

        # Auto-validare primary (recalculeaza completeness din data actuala)
        p_data = primary.get("data") or {}
        if isinstance(p_data, str):
            try:
                import json as _j; p_data = _j.loads(p_data)
            except Exception:
                p_data = {}
        p_fields = []
        if tid:
            t = _get_type(db, tid)
            if t:
                p_fields = t.get("extract_fields") or []
        _maybe_auto_validate(db, primary["attachment_id"], 0, p_data, p_fields,
                             primary.get("confidence"), primary.get("status"))
        logger.info("autogroup fata/verso pdf: email=%s plate=%s primary_ex=%s grouped=%s",
                    email_id, plate, primary["ex_id"], secondary_ids)
        created += 1
    return created


# ── Endpointuri STEP 2 ───────────────────────────────────────────────────────
def _drain_in_progress(db) -> bool:
    """Citeste (read-only) daca lock-ul advisory de drain e detinut undeva in cluster.
    Forma cu un singur bigint: classid=high32, objid=low32, objsubid=2."""
    try:
        hi = (_DRAIN_LOCK_KEY >> 32) & 0xFFFFFFFF
        lo = _DRAIN_LOCK_KEY & 0xFFFFFFFF
        n = db.execute(text(
            "SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
            "AND classid=:hi AND objid=:lo AND objsubid=2"),
            {"hi": hi, "lo": lo}).scalar()
        return bool(n and int(n) > 0)
    except Exception:
        return False


@router.post("/documents/emails/reprocess-by-ids")
def documents_reprocess_by_ids(body: dict, db: Session = Depends(get_db),
                                admin=Depends(get_current_admin)):
    """Reseteaza starea de procesare pentru o lista de email ID-uri, fara a sterge emailurile.
    Sterge extractiile existente (non-reviewed) + reseteaza doc_discarded pe atasamente,
    apoi kickeaza drain-ul cu scope='all' (force=True) ca sa le preia imediat.
    Emailurile raman cu acelasi ID — nu se pierd date."""
    raw_ids = body.get("email_ids") or []
    if not raw_ids:
        raise HTTPException(400, "email_ids lista goala")
    try:
        email_ids = [int(x) for x in raw_ids]
    except (ValueError, TypeError):
        raise HTTPException(400, "email_ids trebuie sa fie numere intregi")
    if len(email_ids) > 50:
        raise HTTPException(400, "Maxim 50 de email ID-uri per apel")
    if not email_ids:
        raise HTTPException(400, "email_ids lista goala")

    existing = [r[0] for r in db.execute(
        text("SELECT id FROM emails WHERE id = ANY(:ids)"), {"ids": email_ids}
    ).fetchall()]
    missing = sorted(set(email_ids) - set(existing))
    if not existing:
        raise HTTPException(404, "Niciun email gasit pentru ID-urile furnizate")

    # Sterge extractiile non-reviewed (reviewed raman intacte)
    deleted = db.execute(text("""
        DELETE FROM document_extractions
        WHERE attachment_id IN (
            SELECT id FROM attachments WHERE email_id = ANY(:ids)
        )
        AND NOT COALESCE(reviewed, false)
        RETURNING id
    """), {"ids": existing}).fetchall()
    n_deleted = len(deleted)

    # Reseteaza doc_discarded + retry_reclassify (daca exista) ca sa intre in coada
    db.execute(text("""
        UPDATE attachments SET doc_discarded = false, doc_discard_reason = null,
            doc_discarded_at = null
        WHERE email_id = ANY(:ids)
    """), {"ids": existing})
    db.commit()

    n_atts = db.execute(text(
        "SELECT COUNT(*) FROM attachments WHERE email_id = ANY(:ids)"
    ), {"ids": existing}).scalar() or 0

    by = (getattr(admin, "email", None) or getattr(admin, "username", None) if admin else "unknown")
    logger.info("reprocess-by-ids reset: %s emailuri, %s atasamente, deleted=%s, by=%s",
                len(existing), n_atts, n_deleted, by)

    # Kickeaza drain-ul cu scope='all' + force=True — preia imediat atasamentele resetate
    _kick_drain("all", force=True)

    return {
        "ok": True,
        "email_ids": existing,
        "missing_ids": missing,
        "attachments_found": n_atts,
        "extractions_deleted": n_deleted,
        "message": "Resetat %d email(uri), %d atasamente. Procesarea a pornit." % (len(existing), n_atts),
    }


@router.post("/documents/process/run-now")
def documents_process_now(scope: str = "today", db: Session = Depends(get_db),
                          admin=Depends(get_current_admin)):
    # 'today'/'all' sunt actiuni manuale; 'all' matura tot arhivul (explicit).
    sc = scope if scope in ("today", "recent", "all") else "today"
    busy = _drain_in_progress(db)
    # Actiune manuala explicita a operatorului: ruleaza chiar daca automatizarea e OPRITA.
    started = _kick_drain(sc, force=True)
    # Daca un drain ruleaza deja (cron sau alt worker), clickul manual nu porneste un al
    # doilea (single-flight) — dar backlog-ul ESTE in curs de procesare; raporteaza onest.
    return {"ok": True, "started": started, "scope": sc, "already_running": busy,
            "message": ("Procesare deja în curs — documentele se procesează." if (busy or not started)
                        else "Procesare pornită.")}


@router.get("/documents/automation")
def documents_automation_status(db: Session = Depends(get_db),
                                admin=Depends(get_current_admin)):
    """Starea comutatorului de procesare automata a documentelor."""
    return {"ok": True, "enabled": _auto_enabled(), "running": _drain_in_progress(db)}


@router.post("/documents/automation/toggle")
def documents_automation_toggle(body: dict = None,
                                admin=Depends(get_current_admin)):
    """Porneste/opreste procesarea AUTOMATA a documentelor (extragere tip+date din atasamente).
    OFF => atasamentele emailurilor noi NU se mai proceseaza automat (categorisirea emailurilor
    ramane neafectata); actiunea manuala „Procesează acum" ruleaza oricum."""
    body = body or {}
    enabled = bool(body.get("enabled"))
    by = (getattr(admin, "email", None) or getattr(admin, "username", None)
          if admin else None)
    _set_auto(enabled, by=by)
    return {"ok": True, "enabled": enabled,
            "message": ("Automatizare pornita: se proceseaza DOAR documentele care intra de acum incolo. "
                        "Backlog-ul anterior nu se atinge; pentru el foloseste butonul Proceseaza acum."
                        if enabled else
                        "Automatizare oprita: procesarea in curs se opreste, iar atasamentele noi nu se "
                        "mai proceseaza. Categorisirea emailurilor ramane neafectata.")}


@router.get("/documents/extractions")
def list_extractions(scope: str = "today", include_skipped: bool = False,
                     db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    where = ["1=1"]
    if scope == "today":
        where.append("e.received_at::date = CURRENT_DATE")
    list_where = list(where)
    # Junk-ul (neidentificat/necunoscut) nu mai exista ca rand (e discard-uit). Ascundem doar
    # membrii grupati (status='grouped') — primarul ii reprezinta. include_skipped pastrat compat.
    if not include_skipped:
        list_where.append("d.status NOT IN ('grouped', 'discarded')")
    rows = db.execute(text(
        "SELECT d.id, d.email_id, d.attachment_id, d.document_type_id, d.category, "
        "  d.detected_type, d.confidence, d.status, d.reviewed, d.auto_validated, d.manual_type, d.method, "
        "  d.extract_confidence, d.extract_method, "
        "  d.model, d.confidence_reason, d.observatii_ai, d.renamed_file, d.client_match, d.client_match_detail, d.data, d.updated_at, d.part_no, d.part_label, d.part_bbox, "
        "  (SELECT count(*) FROM document_extractions m WHERE m.grouped_into=d.id) AS group_count, "
        "  (SELECT count(*) FROM document_extractions p WHERE p.attachment_id=d.attachment_id "
        "     AND p.part_no>0) AS part_count, "
        "  a.name AS att_name, a.content_type AS att_mime, "
        "  e.subject, e.from_address, e.from_name, e.received_at "
        "FROM document_extractions d "
        "JOIN attachments a ON a.id=d.attachment_id "
        "JOIN emails e ON e.id=d.email_id "
        "WHERE " + " AND ".join(list_where) +
        " ORDER BY e.received_at DESC, d.attachment_id DESC, d.part_no ASC, d.id DESC LIMIT 1000")).fetchall()
    crows = db.execute(text(
        "SELECT d.status, count(*) FROM document_extractions d JOIN emails e ON e.id=d.email_id "
        "WHERE " + " AND ".join(where) + " GROUP BY d.status")).fetchall()
    return {"ok": True, "items": [dict(r._mapping) for r in rows],
            "counts": {r[0]: int(r[1]) for r in crows}}



def _stats_by_category(db, where_clauses):
    """Statistici auto_validated per categorie (sofer/vehicul/contract)."""
    r2 = db.execute(text(
        "SELECT dt.category, "
        "  count(*) AS total, "
        "  count(*) FILTER (WHERE d.auto_validated) AS auto_validated "
        "FROM document_extractions d "
        "  JOIN emails e ON e.id = d.email_id "
        "  JOIN document_types dt ON dt.id = d.document_type_id "
        "WHERE " + " AND ".join(where_clauses) +
        " AND dt.category IN ('sofer','vehicul','contract') "
        "GROUP BY dt.category"
    )).fetchall()
    result = {}
    for row in r2:
        cat = row[0]
        total = int(row[1] or 0)
        av = int(row[2] or 0)
        result[cat] = {
            "total": total,
            "auto_validated": av,
            "auto_validated_pct": round(100.0 * av / total, 1) if total else 0.0,
        }
    return result


@router.get("/documents/extractions/stats")
def extractions_stats(scope: str = "all", engine: str = "all",
                      db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Statistici pt pagina Procesare documente (migration readiness): % auto-validat, extras corect
    (fara interventie operator), corectat de operator, reincadrat (tip schimbat manual).
    scope: today|all. engine: all|iris (doar documentele extrase prin IRIS, extract_confidence not null)."""
    where = ["d.status NOT IN ('grouped','discarded')"]
    if scope == "today":
        where.append("e.received_at::date = CURRENT_DATE")
    if engine == "iris":
        where.append("d.extract_confidence IS NOT NULL")
    r = db.execute(text(
        "SELECT count(*) AS total, "
        "  count(*) FILTER (WHERE d.auto_validated) AS auto_validated, "
        "  count(*) FILTER (WHERE d.status='needs_review') AS needs_review, "
        "  count(*) FILTER (WHERE d.corrected) AS corrected, "
        "  count(*) FILTER (WHERE d.manual_type) AS reincadrat, "
        "  count(*) FILTER (WHERE d.extract_confidence IS NOT NULL) AS iris_extracted, "
        "  round(avg(d.extract_confidence) FILTER (WHERE d.extract_confidence IS NOT NULL), 2) AS avg_iris_conf "
        "FROM document_extractions d JOIN emails e ON e.id=d.email_id "
        "WHERE " + " AND ".join(where))).fetchone()
    m = dict(r._mapping) if r else {}
    total = int(m.get("total") or 0)
    corrected = int(m.get("corrected") or 0)
    reincadrat = int(m.get("reincadrat") or 0)

    def _pct(n):
        return round(100.0 * int(n or 0) / total, 1) if total else 0.0
    return {
        "ok": True, "scope": scope, "engine": engine, "total": total,
        "auto_validated": int(m.get("auto_validated") or 0), "auto_validated_pct": _pct(m.get("auto_validated")),
        "needs_review": int(m.get("needs_review") or 0), "needs_review_pct": _pct(m.get("needs_review")),
        "corrected": corrected, "corrected_pct": _pct(corrected),
        "reincadrat": reincadrat, "reincadrat_pct": _pct(reincadrat),
        # extras corect (proxy) = nici corectat de operator, nici reincadrat
        "correct_pct": (round(100.0 * (total - corrected - reincadrat) / total, 1) if total else 0.0),
        "iris_extracted": int(m.get("iris_extracted") or 0),
        "avg_iris_conf": (float(m["avg_iris_conf"]) if m.get("avg_iris_conf") is not None else None),
        "by_category": _stats_by_category(db, where),
    }


@router.get("/documents/extractions/{ex_id}")
def get_extraction(ex_id: int, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    r = db.execute(text(
        "SELECT d.*, a.name AS att_name, a.content_type AS att_mime, "
        "  e.subject, e.from_address, e.from_name, e.received_at "
        "FROM document_extractions d JOIN attachments a ON a.id=d.attachment_id "
        "JOIN emails e ON e.id=d.email_id WHERE d.id=:id"), {"id": ex_id}).fetchone()
    if not r:
        raise HTTPException(404, "Inexistent")
    item = dict(r._mapping)
    fields = []
    if item.get("document_type_id"):
        t = _get_type(db, item["document_type_id"])
        if t:
            fields = t.get("extract_fields") or []
    item["type_fields"] = fields
    if item.get("raw_text"):
        item["raw_text"] = item["raw_text"][:4000]
    # companions = celelalte atasamente ale aceluiasi email (pentru grupare manuala).
    # suggested = acelasi tip ca primarul si inca negrupat (pre-bifat in UI).
    comps = db.execute(text(
        "SELECT d.id AS ex_id, d.attachment_id, d.status, d.detected_type, "
        "       d.document_type_id, d.grouped_into, a.name AS att_name, a.content_type "
        "FROM document_extractions d JOIN attachments a ON a.id=d.attachment_id "
        "WHERE d.email_id=:eid AND d.id <> :id ORDER BY a.id ASC"),
        {"eid": item["email_id"], "id": ex_id}).fetchall()
    companions = []
    for c in comps:
        c = dict(c._mapping)
        c["suggested"] = bool(c.get("document_type_id") is not None
                              and item.get("document_type_id") is not None
                              and c["document_type_id"] == item["document_type_id"]
                              and c.get("grouped_into") is None
                              and c["status"] != 'grouped')
        companions.append(c)
    item["companions"] = companions
    gc = db.execute(text("SELECT count(*) FROM document_extractions WHERE grouped_into=:id"),
                    {"id": ex_id}).scalar()
    item["group_count"] = int(gc or 0)
    if item["group_count"] > 0:
        mem = db.execute(text(
            "SELECT a.id AS attachment_id, a.name AS att_name, a.content_type "
            "FROM document_extractions d JOIN attachments a ON a.id=d.attachment_id "
            "WHERE d.grouped_into=:id ORDER BY a.id ASC"), {"id": ex_id}).fetchall()
        item["members"] = ([{"attachment_id": item.get("attachment_id"),
                             "att_name": item.get("att_name"),
                             "content_type": item.get("att_mime"), "primary": True}]
                           + [dict(m._mapping) for m in mem])
    return {"ok": True, "item": item}


def _data_differs(old, new):
    """True daca valorile campurilor difera (normalizat string/trim), ignorand cheile goale pe ambele parti.
    => adaugarea unei valori intr-un camp gol conteaza ca o corectura; reordonarea cheilor NU."""
    def norm(d):
        out = {}
        for k, v in (d or {}).items():
            s = "" if v is None else str(v).strip()
            if s != "":
                out[str(k)] = s
        return out
    a, b = norm(old), norm(new)
    if set(a.keys()) != set(b.keys()):
        return True
    return any(a[k] != b[k] for k in a)


@router.put("/documents/extractions/{ex_id}")
def update_extraction(ex_id: int, body: dict, db: Session = Depends(get_db),
                      admin=Depends(get_current_admin)):
    r = db.execute(text("SELECT data, document_type_id FROM document_extractions WHERE id=:id"),
                   {"id": ex_id}).fetchone()
    if not r:
        raise HTTPException(404, "Inexistent")
    cur_data = r[0] if r[0] is not None else {}
    cur_type = r[1]
    # col -> (sql_expr, bind_name sau None, bind_value). Dict => fara coloane duplicate in SET.
    who = (admin.get("username") or admin.get("email"))
    assigns = {"reviewed": ("true", None, None), "reviewed_by": (":who", "who", who),
               "updated_at": ("now()", None, None)}
    corrected = False   # operatorul a MODIFICAT date sau tip (vs. simpla confirmare)
    if "data" in body:
        if _data_differs(cur_data, body.get("data") or {}):
            corrected = True
        assigns["data"] = ("CAST(:data AS jsonb)", "data", json.dumps(body.get("data") or {}))
    if "document_type_id" in body:
        nt = body.get("document_type_id")
        t = _get_type(db, nt) if nt else None
        new_tid = (t["id"] if t else None)
        if (new_tid or None) != (cur_type or None):
            corrected = True
        assigns["document_type_id"] = (":tid", "tid", new_tid)
        assigns["manual_type"] = ("true", None, None)
        if t:
            assigns["category"] = (":cat", "cat", t["category"])
            assigns["detected_type"] = (":dt", "dt", t["name"])
            assigns["status"] = ("'extracted'", None, None)
    # categorie explicita cand NU se alege un tip anume (type_id null/absent) — ex. confirmi
    # 'contract' fara tip, sau o pui pe null la 'Necunoscut'. Valori in afara ALLOWED_CATS -> null.
    if "category" in body and not body.get("document_type_id"):
        cv = body.get("category")
        assigns["category"] = (":cat", "cat", (cv if cv in ALLOWED_CATS else None))
    if body.get("status") in ("extracted", "needs_review", "classified", "neidentificat", "necunoscut"):
        assigns["status"] = (":st", "st", body["status"])
    if "observatii_ai" in body:
        _obs = body["observatii_ai"]
        assigns["observatii_ai"] = (":obs_ai", "obs_ai", str(_obs).strip() if _obs else None)
    if corrected:
        assigns["corrected"] = ("true", None, None)
        assigns["corrected_at"] = ("now()", None, None)
    sets, params = [], {"id": ex_id}
    for col, (expr, bname, val) in assigns.items():
        sets.append(col + " = " + expr)
        if bname is not None:
            params[bname] = val
    db.execute(text("UPDATE document_extractions SET " + ", ".join(sets) + " WHERE id=:id"), params)
    db.commit()
    return {"ok": True}


@router.post("/documents/extractions/{ex_id}/reidentify")
def reidentify_extraction(ex_id: int, type_id: int = None, db: Session = Depends(get_db),
                          admin=Depends(get_current_admin)):
    """Fara type_id: reclasifica (categorie->tip) + extrage. Cu type_id: forteaza tipul
    ales manual si re-extrage cu campurile acelui tip (cheile difera de extragerea veche)."""
    _INTERACTIVE.set(True)   # click-and-wait: fail-fast pe gateway (vezi _ai_budget)
    r = db.execute(text(
        "SELECT a.id, a.email_id, a.name, a.content_type, a.storage_path, d.raw_text AS _raw, "
        "       d.part_no AS _pno, d.part_bbox AS _pbbox "
        "FROM document_extractions d JOIN attachments a ON a.id=d.attachment_id WHERE d.id=:id"),
        {"id": ex_id}).fetchone()
    if not r:
        raise HTTPException(404, "Inexistent")
    att = dict(r._mapping)
    _part_bbox = _norm_bbox(att.get("_pbbox"))   # banda taiata -> extragerea respecta decupajul
    is_group_primary = bool(db.execute(text(
        "SELECT 1 FROM document_extractions WHERE grouped_into=:id LIMIT 1"), {"id": ex_id}).fetchone())
    if type_id and is_group_primary:
        # Primar de grup: seteaza tipul, apoi re-extrage pe textul COMBINAT al grupului.
        t = _get_type(db, type_id)
        if not t:
            raise HTTPException(404, "Tip inexistent")
        db.execute(text("UPDATE document_extractions SET document_type_id=:tid, category=:cat, "
                        "detected_type=:dt, manual_type=true, reviewed_by=:w, updated_at=now() WHERE id=:id"),
                   {"id": ex_id, "tid": type_id, "cat": t["category"], "dt": t["name"],
                    "w": (admin.get("username") or admin.get("email"))})
        db.commit()
        status = _extract_group(db, ex_id)
    elif type_id:
        t = _get_type(db, type_id)
        if not t:
            raise HTTPException(404, "Tip inexistent")
        path = _host_path(att.get("storage_path"))
        if not path or not os.path.exists(path):
            raise HTTPException(400, "Fisier indisponibil pe disc")
        # Prefera textul deja stocat (poate fi transcrierea buna prin vision din pasul auto)
        # ca sa nu regresam la garbage-ul OCR re-citind discul; altfel OCR local + fallback vision.
        stored = (att.get("_raw") or "").strip()
        if stored:
            doc_text, method = stored, "stored"
        else:
            doc_text, method, _verr = _doc_text_vision(path, att.get("content_type"))
        data, emodel, eerr, status = None, None, None, "classified"
        econf, emeth = None, None
        ct = att.get("content_type")
        # Re-extragere manuala prin punctul UNIC (onoreaza motorul: IRIS cand e activ + fallback local).
        # Banda taiata (part_bbox) -> MEREU vizual pe DECUPAJ; altfel vision/text dupa tip.
        if _type_extracts(t):
            if _part_bbox or t.get("extract_via_vision") or (doc_text or "").strip():
                data, emodel, eerr, econf, emeth = _extract_fields(
                    db, t, path, ct, doc_text, type_id, bbox=_part_bbox, force_vision=bool(_part_bbox))
                method = emeth or ("vision" if (_part_bbox or t.get("extract_via_vision")) else method)
                status = "extracted" if data is not None else "failed"
            else:
                status = "needs_vision"
        # UPDATE by id (NU upsert pe attachment_id): un atasament poate avea MAI MULTE parti
        # (part_no>=1); upsert-ul pe (attachment_id, part_no=0 default) ar insera un rand fantoma
        # si ar lasa partea reala (ex_id) cu date vechi. Tintim exact randul ex_id.
        db.execute(text(
            "UPDATE document_extractions SET status=:st, method=:m, model=:mo, category=:cat, "
            "detected_type=:dt, document_type_id=:tid, data=CAST(:data AS jsonb), raw_text=:rt, "
            "confidence_reason='tip ales manual', error=:err, manual_type=true, reviewed_by=:w, "
            "extract_confidence=:econf, extract_method=:emeth, "
            "extracted_at=now(), updated_at=now() WHERE id=:id"),
            {"id": ex_id, "st": status, "m": (method or "")[:30] or None,
             "mo": (emodel or "")[:80] or None, "cat": t["category"], "dt": (t["name"] or "")[:160],
             "tid": type_id, "data": json.dumps(data or {}), "rt": (doc_text or "")[:20000] or None,
             "err": (eerr or "")[:1000] or None,
             "econf": econf, "emeth": emeth,
             "w": (admin.get("username") or admin.get("email"))})
        db.commit()
    else:
        status = _process_attachment(db, att, force=True)
    out = db.execute(text(
        "SELECT category, detected_type, confidence, status, data, method, model, confidence_reason "
        "FROM document_extractions WHERE id=:id"), {"id": ex_id}).fetchone()
    return {"ok": True, "status": status, "item": dict(out._mapping) if out else None}


@router.post("/documents/extractions/{ex_id}/group")
def group_extraction(ex_id: int, body: dict, db: Session = Depends(get_db),
                     admin=Depends(get_current_admin)):
    """Grupeaza atasamentele bifate (acelasi email) sub primarul ex_id si re-extrage combinat.
    Primar = documentul tipat deschis de operator; membrii devin status='grouped' (ascunsi)."""
    prim = db.execute(text("SELECT id, email_id FROM document_extractions WHERE id=:id"),
                      {"id": ex_id}).fetchone()
    if not prim:
        raise HTTPException(404, "Inexistent")
    prim = dict(prim._mapping)
    raw_ids = body.get("attachment_ids") or []
    att_ids = []
    for a in raw_ids:
        try:
            att_ids.append(int(a))
        except (TypeError, ValueError):
            pass
    if not att_ids:
        raise HTTPException(400, "Niciun atasament selectat")
    members = [dict(m._mapping) for m in db.execute(text(
        "SELECT d.id, d.reviewed FROM document_extractions d "
        "WHERE d.email_id=:eid AND d.attachment_id = ANY(:aids) AND d.id <> :pid"),
        {"eid": prim["email_id"], "aids": att_ids, "pid": ex_id}).fetchall()]
    if not members:
        raise HTTPException(400, "Atasamentele selectate nu apartin aceluiasi email")
    if any(m["reviewed"] for m in members):
        raise HTTPException(400, "Un atasament selectat e deja verificat manual — nu poate fi grupat")
    # Mutual exclusion grup × parts: nu grupam un atasament care e deja spart in mai multe documente
    # (part_no>0). Gruparea = N atasamente -> 1 doc; spargerea = 1 atasament -> N docs; sunt inverse.
    split_hit = db.execute(text(
        "SELECT 1 FROM document_extractions WHERE attachment_id = ANY(:aids) AND part_no > 0 LIMIT 1"),
        {"aids": att_ids}).fetchone()
    if split_hit:
        raise HTTPException(400, "Un atasament selectat e impartit in mai multe documente — "
                                 "foloseste «Unifica» pe el inainte de a-l grupa")
    member_ids = [m["id"] for m in members]
    db.execute(text("UPDATE document_extractions SET grouped_into=:pid, status='grouped', "
                    "updated_at=now() WHERE id = ANY(:ids)"), {"pid": ex_id, "ids": member_ids})
    # type_id optional: seteaza tipul primarului INAINTE de extragerea combinata (atomic
    # "grupeaza selectia ca tipul X"). Identic cu ramura group-primary din reidentify.
    type_id = body.get("type_id")
    if type_id:
        t = _get_type(db, type_id)
        if not t:
            raise HTTPException(404, "Tip inexistent")
        db.execute(text("UPDATE document_extractions SET document_type_id=:tid, category=:cat, "
                        "detected_type=:dt, manual_type=true, reviewed_by=:w, updated_at=now() WHERE id=:id"),
                   {"id": ex_id, "tid": t["id"], "cat": t["category"], "dt": t["name"],
                    "w": (admin.get("username") or admin.get("email"))})
    db.commit()
    status = _extract_group(db, ex_id)
    out = db.execute(text("SELECT category, detected_type, confidence, status, data "
                          "FROM document_extractions WHERE id=:id"), {"id": ex_id}).fetchone()
    return {"ok": True, "status": status, "grouped": len(member_ids),
            "item": dict(out._mapping) if out else None}


@router.post("/documents/extractions/{ex_id}/ungroup")
def ungroup_extraction(ex_id: int, db: Session = Depends(get_db),
                       admin=Depends(get_current_admin)):
    """Desface grupul: membrii redevin randuri individuale (re-OCR + reclasificare), iar
    primarul se re-proceseaza pe textul propriu."""
    prim = db.execute(text(
        "SELECT a.id, a.email_id, a.name, a.content_type, a.storage_path "
        "FROM document_extractions d JOIN attachments a ON a.id=d.attachment_id WHERE d.id=:id"),
        {"id": ex_id}).fetchone()
    if not prim:
        raise HTTPException(404, "Inexistent")
    members = [dict(m._mapping) for m in db.execute(text(
        "SELECT a.id, a.email_id, a.name, a.content_type, a.storage_path "
        "FROM document_extractions d JOIN attachments a ON a.id=d.attachment_id "
        "WHERE d.grouped_into=:pid"), {"pid": ex_id}).fetchall()]
    # STERGEM randurile membrilor (nu doar grouped_into=NULL): altfel, daca re-extragerea
    # de mai jos pica tranzitoriu (gateway 502), randul ar ramane status='grouped' = ascuns
    # SI ne-reintrodus in coada (are rand). Stergerea -> 'd.id IS NULL' -> drain-ul le reia.
    db.execute(text("DELETE FROM document_extractions WHERE grouped_into=:pid"), {"pid": ex_id})
    db.commit()
    for m in members:
        try:
            _process_attachment(db, m, force=True)  # reinsereaza individual; transient -> ramane in coada
        except Exception:
            logger.exception("ungroup reprocess member %s", m.get("id"))
    try:
        _process_attachment(db, dict(prim._mapping), force=True)
    except Exception:
        logger.exception("ungroup reprocess primary %s", ex_id)
    return {"ok": True, "ungrouped": len(members)}


@router.post("/documents/extractions/{ex_id}/split")
def split_extraction(ex_id: int, body: dict, db: Session = Depends(get_db),
                     admin=Depends(get_current_admin)):
    """Operatorul declara ca un atasament contine MAI MULTE documente. Creeaza part_no=1..N.
    body: {parts:[{type_id|null, label?}]} (preferat) SAU {count:N} (parti goale needs_review).
    Partile cu tip ales se extrag VIZUAL imediat (pe tot atasamentul); restul raman needs_review.
    Fara decupare la pixel — fiecare parte vede tot atasamentul, vision se concentreaza pe tipul ei."""
    row = db.execute(text(
        "SELECT d.attachment_id, d.email_id, d.part_no, d.grouped_into, d.raw_text, "
        "       a.name, a.content_type, a.storage_path "
        "FROM document_extractions d JOIN attachments a ON a.id=d.attachment_id WHERE d.id=:id"),
        {"id": ex_id}).fetchone()
    if not row:
        raise HTTPException(404, "Inexistent")
    row = dict(row._mapping)
    if row.get("part_no"):
        raise HTTPException(400, "Atasamentul e deja impartit — foloseste «Unifica» intai")
    if row.get("grouped_into"):
        raise HTTPException(400, "Document membru intr-un grup — desfa grupul intai")
    is_group_primary = bool(db.execute(text(
        "SELECT 1 FROM document_extractions WHERE grouped_into=:id LIMIT 1"), {"id": ex_id}).fetchone())
    if is_group_primary:
        raise HTTPException(400, "Document primar de grup — desfa grupul intai")
    # Construim lista de parti
    parts = body.get("parts")
    if not isinstance(parts, list) or not parts:
        try:
            count = int(body.get("count") or 0)
        except Exception:
            count = 0
        if count < 2:
            raise HTTPException(400, "Specifica cel putin 2 documente (parts[] sau count>=2)")
        parts = [{} for _ in range(count)]
    if len(parts) > MULTIDOC_MAX_PARTS:
        raise HTTPException(400, "Maxim %d documente per atasament" % MULTIDOC_MAX_PARTS)
    att = {"id": row["attachment_id"], "email_id": row["email_id"], "name": row.get("name"),
           "content_type": row.get("content_type"), "storage_path": row.get("storage_path")}
    path = _host_path(row.get("storage_path"))
    if not path or not os.path.exists(path):
        raise HTTPException(400, "Fisier indisponibil pe disc")
    doc_text = row.get("raw_text") or ""
    catalog = _types_catalog(db)
    tmap = {t["id"]: t for t in catalog}
    # 1) Inseram placeholderele (part_no=1..N) SI stergem part_no=0 in ACEEASI tranzactie:
    #    atasamentul nu ramane niciodata fara rand (drain-ul nu-l reia in fereastra).
    for i, p in enumerate(parts, 1):
        lbl = (p.get("label") or "")[:160] or None
        bbox = _norm_bbox(p.get("bbox"))   # banda taiata (optional)
        db.execute(text(
            "INSERT INTO document_extractions (email_id, attachment_id, part_no, part_label, part_bbox, "
            " status, confidence_reason, data, created_at, updated_at) "
            "VALUES (:eid,:aid,:pno,:plabel,CAST(:pbbox AS jsonb),'needs_review',"
            " 'impartit manual — alege tipul','{}'::jsonb, now(), now()) "
            "ON CONFLICT (attachment_id, part_no) DO UPDATE SET part_label=EXCLUDED.part_label, "
            " part_bbox=EXCLUDED.part_bbox, status='needs_review', updated_at=now()"),
            {"eid": row["email_id"], "aid": row["attachment_id"], "pno": i, "plabel": lbl,
             "pbbox": (json.dumps(bbox) if bbox else None)})
    db.execute(text("DELETE FROM document_extractions WHERE attachment_id=:a AND part_no=0"),
               {"a": row["attachment_id"]})
    db.commit()
    # 2) Extragem partile carora li s-a ales un tip valid (vision pe banda taiata sau pe tot atasamentul).
    extracted = 0
    for i, p in enumerate(parts, 1):
        try:
            tid = int(p.get("type_id"))
        except Exception:
            tid = None
        if tid not in tmap:
            continue
        t = tmap[tid]
        doc = {"type_id": tid, "type_name": t["name"], "category": t["category"]}
        try:
            _save_part_extraction(db, att, path, row.get("content_type"), i, doc, catalog, doc_text,
                                  None, part_bbox=_norm_bbox(p.get("bbox")))
            extracted += 1
        except Exception:
            logger.exception("split extract part %d att %s", i, row["attachment_id"])
    new_ids = [m[0] for m in db.execute(text(
        "SELECT id FROM document_extractions WHERE attachment_id=:a AND part_no>0 ORDER BY part_no"),
        {"a": row["attachment_id"]}).fetchall()]
    return {"ok": True, "parts": len(parts), "extracted": extracted, "ex_ids": new_ids}


@router.post("/documents/extractions/{ex_id}/unsplit")
def unsplit_extraction(ex_id: int, body: dict = None, db: Session = Depends(get_db),
                       admin=Depends(get_current_admin)):
    """Unifica un atasament impartit: sterge toate partile (part_no>0) si re-proceseaza
    atasamentul de la zero (single-doc / auto). Mirror al «ungroup».
    Robust la UI invechit: daca randul `ex_id` a fost deja sters (ex. o parte discardata,
    lasand un singur part_no orfan), cade pe `attachment_id` din body ca sa unifice tot
    atasamentul in loc de 404."""
    row = db.execute(text(
        "SELECT a.id, a.email_id, a.name, a.content_type, a.storage_path "
        "FROM document_extractions d JOIN attachments a ON a.id=d.attachment_id WHERE d.id=:id"),
        {"id": ex_id}).fetchone()
    if not row:
        aid = (body or {}).get("attachment_id")
        if aid is not None:
            row = db.execute(text(
                "SELECT id, email_id, name, content_type, storage_path "
                "FROM attachments WHERE id=:a"), {"a": aid}).fetchone()
    if not row:
        raise HTTPException(404, "Inexistent")
    att = dict(row._mapping)
    db.execute(text("DELETE FROM document_extractions WHERE attachment_id=:a"), {"a": att["id"]})
    db.commit()
    try:
        status = _process_attachment(db, att, force=True)
    except Exception:
        logger.exception("unsplit reprocess att %s", att["id"])
        status = "retry_transient"
    return {"ok": True, "status": status}


@router.get("/documents/extractions/{ex_id}/crop")
def crop_extraction(ex_id: int, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Preview-ul DECUPAT al unei parti (banda taiata): daca rândul are part_bbox, intoarce imaginea
    decupata (JPEG); altfel 404 (UI-ul cade pe download-ul atasamentului intreg)."""
    r = db.execute(text(
        "SELECT d.part_bbox, d.page_from, d.page_to, d.part_label, a.content_type, a.storage_path "
        "FROM document_extractions d JOIN attachments a ON a.id=d.attachment_id WHERE d.id=:id"),
        {"id": ex_id}).fetchone()
    if not r:
        raise HTTPException(404, "Inexistent")
    row = dict(r._mapping)
    path = _host_path(row.get("storage_path"))
    if not path or not os.path.exists(path):
        raise HTTPException(404, "Fisier indisponibil")
    bbox = _norm_bbox(row.get("part_bbox"))
    # (a) banda decupata pe O pagina (acte stivuite) -> imaginea benzii
    if bbox:
        files = _crop_to_files(path, row.get("content_type"), bbox)
        data, fmime = files[0]
        if not isinstance(data, (bytes, bytearray)):
            raise HTTPException(404, "Decupaj indisponibil")
        return Response(content=bytes(data), media_type=(fmime or "image/jpeg"),
                        headers={"Cache-Control": "no-store"})
    # (b) bucata pe interval de pagini (PDF multi-doc spart) -> sub-PDF cu DOAR paginile partii
    pf, pt = row.get("page_from"), row.get("page_to")
    if pf is not None and pt is not None:
        sub = _pdf_page_subset(path, row.get("content_type"), int(pf), int(pt))
        if sub and isinstance(sub[0], (bytes, bytearray)):
            import re as _re
            _fn = _re.sub(r"[^A-Za-z0-9_.\-]", "_", (row.get("part_label") or ("doc_p%d-%d" % (int(pf) + 1, int(pt) + 1))))
            _ext = "pdf" if "pdf" in (sub[1] or "") else "bin"
            return Response(content=bytes(sub[0]), media_type=(sub[1] or "application/pdf"),
                            headers={"Cache-Control": "no-store",
                                     "Content-Disposition": "inline; filename=\"%s.%s\"" % (_fn[:80], _ext)})
    raise HTTPException(404, "Fara decupaj (foloseste atasamentul intreg)")


@router.get("/documents/emails/{email_id}/docs")
def list_email_docs(email_id: int, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Toate atasamentele unui email cu starea lor de extragere — pentru modalul dedicat de grupare
    per email (re-catalogare + grupare arbitrara). Include si membrii grupati (grouped_into not null)."""
    em = db.execute(text("SELECT id, subject, from_address, from_name, received_at "
                         "FROM emails WHERE id=:id"), {"id": email_id}).fetchone()
    if not em:
        raise HTTPException(404, "Email inexistent")
    rows = db.execute(text(
        "SELECT d.id AS ex_id, d.attachment_id AS att_id, d.status, d.reviewed, d.detected_type, "
        "       d.document_type_id, d.category, d.grouped_into, d.data, d.part_no, d.part_label, d.part_bbox, "
        "       d.renamed_file, a.name AS att_name, a.content_type, "
        "       (SELECT count(*) FROM document_extractions m WHERE m.grouped_into=d.id) AS group_count, "
        "       (SELECT count(*) FROM document_extractions p WHERE p.attachment_id=d.attachment_id "
        "          AND p.part_no>0) AS part_count "
        "FROM document_extractions d JOIN attachments a ON a.id=d.attachment_id "
        "WHERE d.email_id=:eid ORDER BY a.id ASC, d.part_no ASC"), {"eid": email_id}).fetchall()
    docs = []
    for r in rows:
        d = dict(r._mapping)
        d["group_count"] = int(d.get("group_count") or 0)
        d["is_primary"] = d["group_count"] > 0
        d["part_count"] = int(d.get("part_count") or 0)
        docs.append(d)
    # Atasamentele ascunse (junk discard-uit): nu mai au rand in document_extractions, dar
    # le aratam separat in modal cu optiune de restaurare (plasa de siguranta).
    drows = db.execute(text(
        "SELECT a.id AS att_id, a.name AS att_name, a.content_type, a.doc_discard_reason "
        "FROM attachments a LEFT JOIN document_extractions d ON d.attachment_id=a.id "
        "WHERE a.email_id=:eid AND COALESCE(a.doc_discarded, false) AND d.id IS NULL "
        "ORDER BY a.id ASC"), {"eid": email_id}).fetchall()
    discarded = [dict(r._mapping) for r in drows]
    return {"ok": True, "email": dict(em._mapping), "docs": docs, "discarded": discarded}


@router.post("/documents/extractions/{ex_id}/discard")
def discard_extraction(ex_id: int, db: Session = Depends(get_db),
                       admin=Depends(get_current_admin)):
    """Operatorul marcheaza manual un document drept junk: sterge randul + marcheaza
    atasamentul (doc_discarded) ca sa nu fie reprocesat. Recuperabil via restore-discarded."""
    r = db.execute(text(
        "SELECT d.attachment_id, d.email_id, d.part_no FROM document_extractions d WHERE d.id=:id"),
        {"id": ex_id}).fetchone()
    if not r:
        raise HTTPException(404, "Inexistent")
    r = dict(r._mapping)
    # daca e primar de grup, eliberam intai membrii (redevin individuali) ca sa nu ramana orfani
    db.execute(text("UPDATE document_extractions SET grouped_into=NULL, status='needs_review', "
                    "updated_at=now() WHERE grouped_into=:id"), {"id": ex_id})
    # Cate alte parti mai are atasamentul? Daca ASTA e ultima -> ascundem tot atasamentul (flag
    # doc_discarded, ca la junk). Daca mai sunt parti (atasament cu mai multe documente), stergem
    # DOAR aceasta parte si NU marcam atasamentul (altfel l-am scoate din coada + ascunde restul).
    others = db.execute(text("SELECT count(*) FROM document_extractions "
                             "WHERE attachment_id=:a AND id<>:id"),
                        {"a": r["attachment_id"], "id": ex_id}).scalar() or 0
    if others > 0:
        db.execute(text("DELETE FROM document_extractions WHERE id=:id"), {"id": ex_id})
        db.commit()
        return {"ok": True, "discarded": 1, "attachment_hidden": False}
    _discard_attachment(db, {"id": r["attachment_id"], "email_id": r["email_id"]},
                        "marcat junk de operator")
    return {"ok": True, "discarded": 1, "attachment_hidden": True}


@router.post("/documents/emails/{email_id}/restore-discarded")
def restore_discarded(email_id: int, db: Session = Depends(get_db),
                      admin=Depends(get_current_admin)):
    """Reseteaza flag-ul doc_discarded pe atasamentele ascunse ale unui email. NU reruleaza
    clasificarea (ar re-discarda acelasi junk = plasa inutila daca AI-ul a gresit); in schimb
    creeaza un rand 'needs_review' fara tip, ca operatorul sa-l identifice manual (reidentify)."""
    atts = [dict(m._mapping) for m in db.execute(text(
        "SELECT a.id, a.email_id, a.name, a.content_type "
        "FROM attachments a LEFT JOIN document_extractions d ON d.attachment_id=a.id "
        "WHERE a.email_id=:eid AND COALESCE(a.doc_discarded, false) AND d.id IS NULL"),
        {"eid": email_id}).fetchall()]
    db.execute(text("UPDATE attachments SET doc_discarded=false, doc_discard_reason=NULL, "
                    "doc_discarded_at=NULL WHERE email_id=:eid AND COALESCE(doc_discarded, false)"),
               {"eid": email_id})
    db.commit()
    for a in atts:
        try:
            _save_extraction(db, {"id": a["id"], "email_id": a["email_id"]},
                             status="needs_review",
                             confidence_reason="restaurat manual — alege tipul documentului")
        except Exception:
            logger.exception("restore placeholder att %s", a.get("id"))
    return {"ok": True, "restored": len(atts)}


@router.post("/documents/emails/{email_id}/autogroup")
def autogroup_email(email_id: int, db: Session = Depends(get_db),
                    admin=Depends(get_current_admin)):
    """Ruleaza auto-gruparea imaginilor (fata/verso = acelasi document fizic) pe un email —
    manual / re-rulare pe emailuri existente. Grupeaza doar la confirmare vision."""
    em = db.execute(text("SELECT id FROM emails WHERE id=:id"), {"id": email_id}).fetchone()
    if not em:
        raise HTTPException(404, "Email inexistent")
    grouped_images = _autogroup_email_images(db, email_id)
    grouped_pdf = _autogroup_fata_verso_pdf(db, email_id)
    return {"ok": True, "grouped": grouped_images + grouped_pdf, "grouped_images": grouped_images, "grouped_pdf": grouped_pdf}


@router.get("/documents/classify-prompt")
def get_classify_prompt_ep(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    return {"ok": True, "prompt": _get_classify_prompt(db)}


@router.put("/documents/classify-prompt")
def put_classify_prompt_ep(body: dict, db: Session = Depends(get_db),
                           admin=Depends(get_current_admin)):
    p = (body.get("prompt") or "").strip()
    if len(p) < 30:
        raise HTTPException(400, "Prompt prea scurt (min 30 caractere)")
    db.execute(text(
        "INSERT INTO settings(key, value, description, updated_by, updated_at) "
        "VALUES('documents.classify_prompt', to_jsonb(CAST(:p AS text)), "
        "       'Prompt identificare/clasificare atasamente', :who, now()) "
        "ON CONFLICT (key) DO UPDATE SET value=to_jsonb(CAST(:p AS text)), "
        "  updated_by=:who, updated_at=now()"),
        {"p": p, "who": (admin.get("username") or admin.get("email"))})
    db.commit()
    return {"ok": True}
