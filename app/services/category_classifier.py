"""Email category classification — consumer of the IRIS AI channel.

Classifies an email into exactly one of: informatie | sesizare | reclamatie | necunoscut,
using editable per-category rule prompts stored in `ai_category_prompts` (fallback to the
DEFAULT_PROMPTS below). Best-effort: returns None on any failure.

Output (stored in emails.ai_result jsonb; emails.ai_category holds the label):
  { "category": "...", "confidence": 0..1, "reason": "<o propoziție RO>" }
"""
import os
import re
import json
import hashlib
import logging
import unicodedata
from typing import Dict, Any, Optional

from sqlalchemy import text
from app.database import SessionLocal
from app.services import iris_ai

logger = logging.getLogger("mailguard.category")

CATEGORIES = ["informatie", "sesizare", "reclamatie", "necunoscut"]
EDITABLE = ["informatie", "sesizare", "reclamatie"]

# Override DETERMINIST de categorie pe expeditor (mailuri automate cu intentie fixa).
# Ex: noreply@hu-go.hu = alerte de utilizare neautorizata -> mereu 'sesizare'. Garanteaza
# consistenta INDIFERENT de cache-ul curat (care altfel poate servi un raspuns vechi gresit).
# Extensibil din DB FARA deploy: settings['category.forced_senders'] = {"adresa@x.ro":"sesizare",
# "@domeniu.ro":"informatie"} (cheie cu '@' in fata = domeniu intreg, match pe sufix).
_FORCED_CAT_KEY = "category.forced_senders"
_FORCED_CAT_DEFAULT = {"noreply@hu-go.hu": "sesizare"}


def _forced_category(from_addr: Optional[str]) -> Optional[str]:
    """Categorie fortata pentru un expeditor, sau None. Built-in ∪ settings (fail-safe la built-in)."""
    fa = (from_addr or "").strip().lower()
    if not fa:
        return None
    mapping = dict(_FORCED_CAT_DEFAULT)
    try:
        db = SessionLocal()
        row = db.execute(text("SELECT value FROM settings WHERE key=:k"),
                         {"k": _FORCED_CAT_KEY}).fetchone()
        db.close()
        if row and isinstance(row[0], dict):
            for k, v in row[0].items():
                if isinstance(k, str) and isinstance(v, str):
                    mapping[k.strip().lower()] = v.strip().lower()
    except Exception as e:
        logger.warning("_forced_category settings read failed, using defaults: %s", e)
    cat = mapping.get(fa)
    if cat is None and "@" in fa:
        cat = mapping.get("@" + fa.split("@", 1)[1])
    return cat if cat in CATEGORIES else None

DEFAULT_PROMPTS: Dict[str, str] = {
    "informatie": (
        "Se incadreaza la categoria Informatii orice email care NU semnaleaza o problema/disfunctionalitate "
        "si NU exprima nemultumire, dar are scop informativ, de coordonare, confirmare sau administrativ.\n"
        "Tipuri: (1) Cereri de informatii/lamuriri, inclusiv vagi. (2) Solicitari administrative/operationale "
        "(implementare user/sofer, activare serviciu, mutare vehicul, procesare documente). (3) Confirmari si "
        "notificari (confirma plata/programare/discutie anterioara). (4) Actualizari de status operational "
        "(locatie vehicule, intrare/iesire tara, finalizare cursa/descarcare). (5) Redirectionare/transfer "
        "(stabilire limba, transfer la coleg, revenire ulterioara). (6) Apeluri fara obiect (greseala, pierdut, "
        "verificare, non-clienti care intreaba ce face compania). (7) Clarificari tehnice fara disfunctionalitate "
        "(tonaj, masa, functionalitati platforma, incarcare documente/poze). (8) Clarificari financiare/facturare "
        "fara contestare (confirmare plata, suspendare temporara, stabilire facturare, info taxe drum). "
        "(9) Comunicari procedurale (modificari procedura, instructiuni, suspendari temporare). (10) Confirmari de "
        "rezolvare (problema anterioara s-a rezolvat, fara sesizare noua). (11) Indisponibilitate si reprogramare "
        "(nu poate continua, cere amanare, fara a semnala o problema).\n"
        "ATENTIE: daca exista nemultumire sau o problema activa -> NU este Informatii. Mesajele in care clientul "
        "comunica o actiune (plata, alimentare, trimitere document) fara a cere interventie si fara a semnala o "
        "problema -> Informatii. Apelurile foarte scurte fara solicitare clara si fara problema -> Informatii. "
        "EMAIL FARA CONTINUT: emailurile fara subiect si fara mesaj substantiv — care contin DOAR o semnatura "
        "automata (ex. semnatura aplicatiei Yahoo Mail, 'Trimis de pe iPhone', 'Sent from my Android'), un salut "
        "scurt, sau corp gol — se incadreaza la Informatii (NU necunoscut). "
        "CAPCANA frecventa: un reply la o factura in care clientul confirma ca a platit cash/fizic la echipa de "
        "montaj sau instalare (ex. 'aceasta factura a fost achitata cash la echipa de montaj') NU este contestare "
        "factura si NU este reclamatie — este confirmare de plata prin canal alternativ = Informatii. "
        "Absenta nemultumirii explicite + ton neutru/pozitiv (ex. 'Multumesc') = semnal clar Informatii."
    ),
    "sesizare": (
        "Se incadreaza la Sesizare orice email in care clientul semnaleaza o problema, disfunctionalitate, eroare "
        "sau neconcordanta (tehnica, functionala, administrativa sau financiara) si asteapta interventie/remediere. "
        "Nu e neaparat agresiv. Tipuri: (1) Probleme financiare/administrative (debitari incorecte, facturi gresite, "
        "plati duble, tranzactii necunoscute). (2) Erori de aplicatie/software (nu merge descarcarea, da eroare, nu "
        "se actualizeaza, versiune veche). (3) Probleme cu dispozitive/carduri (card nu functioneaza, tahograf nu "
        "citeste, dispozitiv defect). (4) Probleme de transmisie/vizualizare date (nu transmite, nu vad masina, "
        "locatie gresita, date care nu coincid, transmisie intermitenta). (5) Probleme multiple/la scara (la toate "
        "masinile la fel). (6) Probleme de acces/conectivitate (nu pot accesa platforma, nu ma pot loga, cont blocat).\n"
        "Cuvinte-cheie: nu functioneaza, defect, eroare, nu se vede, nu apare, problema cu..., nu pot accesa, nu merge, "
        "nu transmite, nu se actualizeaza.\n"
        "ATENTIE: daca clientul doar cere informatii/confirma o actiune/face o cerere administrativa FARA a semnala o "
        "problema -> NU este Sesizare (este Informatii). Sesizarea poate fi exprimata si indirect ('ceva nu e in "
        "regula'). Daca problema este semnalata PENTRU PRIMA DATA -> Sesizare. Daca exprima nemultumire ca o problema "
        "ANTERIOARA nu a fost rezolvata, cu referire la contactari anterioare esuate -> verificati Reclamatie."
    ),
    "reclamatie": (
        "Se incadreaza la Reclamatie orice email in care clientul exprima nemultumire explicita fata de modul in care "
        "compania a gestionat (sau nu) o problema ANTERIOARA. Presupune DOUA elemente simultan: (1) o problema/solicitare "
        "anterioara si (2) nemultumirea ca nu a fost rezolvata/tratata corespunzator.\n"
        "DIFERENTA fata de Sesizare: Sesizare = problema activa, prima data; Reclamatie = clientul a INCERCAT DEJA sa "
        "obtina rezolvare (apeluri, mailuri, promisiuni) si compania nu a reactionat adecvat.\n"
        "Tipuri: (1) Lipsa de reactie la contactari anterioare ('v-am scris si nu a raspuns nimeni'). (2) Promisiuni "
        "nerespectate ('mi s-a promis ca se rezolva si nu s-a intamplat'). (3) Probleme repetitive/nerezolvate ('a treia "
        "oara cand va contactez pentru aceeasi problema'). (4) Nemultumire privind calitatea serviciului ('sunt foarte "
        "nemultumit', 'ce fel de suport e asta'). (5) Amenintari de reziliere/plecare ('daca nu se rezolva, renunt la "
        "contract'). (6) Nemultumire financiara cu referinta la esec anterior ('v-am zis de factura gresita si tot nu "
        "s-a corectat').\n"
        "Cuvinte-cheie: v-am mai scris, v-am mai sunat, nu ati raspuns, nu m-a contactat nimeni, mi s-a promis, este a "
        "doua/a treia oara, nu este normal, sunt foarte nemultumit, de cate ori, niciodata, degeaba, renunt, reziliez.\n"
        "ATENTIE: o problema semnalata PRIMA DATA, fara referinta la contactari anterioare esuate si fara nemultumire "
        "explicita -> Sesizare, NU Reclamatie. Tonul agresiv singur NU e suficient: trebuie referire la un esec anterior "
        "al companiei. Daca exprima nemultumire DAR semnaleaza si o problema noua: daca predomina nemultumirea fata de "
        "lipsa de reactie -> Reclamatie; daca predomina problema noua -> Sesizare."
    ),
}

_BASE_HEAD = (
    "Esti un clasificator de emailuri de suport pentru clientii unei firme de monitorizare/telemetrie pentru "
    "transport. Citeste subiectul si corpul emailului si incadreaza-l in EXACT una dintre categoriile: "
    "informatie, sesizare, reclamatie.\n"
    "REGULA IMPLICITA OBLIGATORIE: FIECARE email primeste o categorie — NU exista 'necunoscut' si NU "
    "lasa categoria goala. Daca NU se incadreaza clar la 'sesizare' sau 'reclamatie' (nicio problema "
    "activa si nicio nemultumire fata de o problema anterioara), sau daca mesajul e vag/scurt/ambiguu/"
    "fara obiect clar, incadreaza-l implicit la 'informatie'. 'informatie' este alegerea SIGURA cand "
    "nimic altceva nu se potriveste cu incredere.\n\n"
    "LIMBA: emailurile pot fi in orice limba (frecvent ENGLEZA, dar si alte limbi). Clasifica DUPA SENS, "
    "indiferent de limba. Daca mesajul NU este in romana, tradu-l MENTAL in romana inainte de a-l incadra. "
    "NU scrie traducerea si NU explica limba — foloseste-o doar intern pentru a decide categoria. "
    "Un email scris corect intr-o limba straina NU este 'necunoscut' din cauza limbii; incadreaza-l in "
    "categoria potrivita pe baza continutului tradus mental.\n\n"
    "ATASAMENTE: daca emailul are CEL PUTIN UN atasament dar corpul mesajului este gol sau nesemnificativ "
    "(doar un salut, o semnatura, sau cateva cuvinte fara solicitare/problema), incadreaza-l la 'informatie' "
    "(este o transmitere de documente/informare), NU 'necunoscut'. Vei vedea in continut o linie 'Atasamente: N'.\n\n"
    "Definitiile categoriilor:\n\n"
)
_BASE_TAIL = (
    "\n\nRegula de departajare rapida: Informatie = fara problema si fara nemultumire; "
    "Sesizare = problema activa raportata (de regula prima data); "
    "Reclamatie = nemultumire ca o problema semnalata ANTERIOR nu a fost rezolvata (referinta la contactari esuate).\n\n"
    "CONFIDENCE — fii ONEST cu incertitudinea, NU pune 1.0 din reflex:\n"
    "- 0.90-1.0 = incadrare clara, un singur sens evident, fara ambiguitate.\n"
    "- 0.70-0.85 = plauzibil, dar ar putea fi rezonabil si alta categorie.\n"
    "- sub 0.60 = ambiguu / informatii insuficiente / s-ar potrivi 2+ categorii.\n"
    "Daca ezitati intre doua categorii sau mesajul e neclar/scurt, scadeti confidence sub 0.85, DAR tot "
    "alegeti o categorie (cand nu e clar sesizare/reclamatie -> 'informatie'). NU folositi 'necunoscut'.\n\n"
    "Returneaza DOAR un JSON valid, fara text in plus, fara traducerea mesajului, fara ```, exact in forma:\n"
    '{"category":"informatie|sesizare|reclamatie","confidence":<numar 0..1>,'
    '"reason":"<o singura propozitie scurta in romana care justifica incadrarea>"}'
)


_CONTEXT_HINT = (
    "\n\nDaca primesti un CONTEXT ISTORIC inainte de emailul curent:\n"
    # OPS-2026-0141 (14.08.2026): clientul office@evologistik.ro trimite ZILNIC aceeasi cerere de
    # rutina ("alimentati contul cu suma din OP atasat"). Contextul istoric o transforma in
    # "5 contactari fara raspuns" -> sesizare, apoi reclamatie, desi textul curent nu are nicio
    # nemultumire. Verificat pe mailurile 60334/60953/64994/66023/66042: FARA istoric ies toate
    # 'informatie' cu 0.92-0.95; CU istoric ies sesizare/reclamatie. De-aia regulile de mai jos.
    "- REPETITIA UNEI CERERI DE RUTINA NU E PROBLEMA. Daca acelasi expeditor trimite periodic "
    "(zilnic/saptamanal) aceeasi solicitare administrativa normala — alimentare/reincarcare cont, "
    "transmitere OP sau alte documente, cerere de factura/extras — FIECARE mail e o tranzactie "
    "NOUA, nu dovada ca precedentele au fost ignorate. Numarul de mailuri similare NU e, singur, "
    "motiv de sesizare sau reclamatie.\n"
    "- ESCALADAREA CERE DOVADA IN TEXTUL CURENT. Poti trece la sesizare/reclamatie pe baza "
    "contextului DOAR daca emailul CURENT contine el insusi o problema sau o nemultumire explicita "
    "(ex. 'v-am mai scris', 'nu mi-a raspuns nimeni', 'a treia oara', 'inca nu s-a rezolvat', "
    "'nu functioneaza'). Daca textul curent e o cerere/transmitere neutra, incadrarea se face DUPA "
    "el, nu dupa istoric — chiar daca in istoric apar sesizari sau reclamatii.\n"
    "- ISTORICUL NU CONTINE RASPUNSURILE NOASTRE. In blocul de context apar DOAR mailurile "
    "PRIMITE de la client; mailurile trimise de firma, apelurile si rezolvarile din CTS NU sunt "
    "acolo. Deci NU poti sti daca s-a raspuns sau nu, si NU ai voie sa folosesti drept motiv "
    "'fara raspuns documentat', 'nu s-a rezolvat' sau 'compania nu a reactionat' — asta e o "
    "presupunere, nu o observatie. Doar clientul poate afirma asta, in textul curent.\n"
    "- CATEGORIILE DIN ISTORIC POT FI GRESITE. Sunt incadrari anterioare ale aceluiasi sistem, nu "
    "adevar verificat. NU le prelua in lant (o eroare veche s-ar propaga si s-ar agrava).\n"
    "- Problema recurenta (2+ sesizari similare recente) + email curent ambiguu "
    "-> confirma sesizare/reclamatie.\n"
    "- Sesizare anterioara ignorata/nerezolvata + nemultumire explicita acum "
    "-> reclamatie.\n"
    "- Email curent clar informativ (cerere informatii, confirmare, etc.) "
    "-> informatie, chiar daca in context exista sesizari.\n"
    "- Ponderea email-ului CURENT este DOMINANTA; contextul e tie-breaker "
    "pentru cazuri ambigue.\n"
    "Daca primesti si un bloc '=== CONTEXT CLIENT ===' dupa emailul curent: "
    "aplica aceeasi regula — reply dominant, context suport."
)

def load_prompts() -> Dict[str, str]:
    """Editable prompts from DB merged over code defaults."""
    out = dict(DEFAULT_PROMPTS)
    try:
        db = SessionLocal()
        rows = db.execute(text("SELECT category, prompt_text FROM ai_category_prompts")).fetchall()
        db.close()
        for r in rows:
            cat = r._mapping["category"]
            txt = r._mapping["prompt_text"]
            if cat in EDITABLE and txt and txt.strip():
                out[cat] = txt
    except Exception as e:
        logger.warning("load_prompts DB failed, using defaults: %s", e)
    return out


def build_system_prompt(prompts: Optional[Dict[str, str]] = None) -> str:
    p = prompts or load_prompts()
    body = (
        "=== INFORMATIE ===\n" + p["informatie"] + "\n\n"
        "=== SESIZARE ===\n" + p["sesizare"] + "\n\n"
        "=== RECLAMATIE ===\n" + p["reclamatie"]
    )
    return _BASE_HEAD + body + _BASE_TAIL + _CONTEXT_HINT


def _strip_html(html: str) -> str:
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", s).strip()


_URL_RE = re.compile(r"https?://\S+|www\.\S+", flags=re.IGNORECASE)

# Semnaturi automate de client de mail. Link-ul de tracking e deja scos de _URL_RE, dar fraza
# reziduala ramane si umfla numaratoarea de cuvinte 'reale', mascand un corp efectiv gol (ex.
# Yahoo Mail care insereaza 'Yahoo Mail: Cautare, organizare, reusita' pe o transmitere de
# atasament fara mesaj). Le eliminam DOAR pentru numaratoare (nu din corpul trimis la AI), ca
# regula determinista 'atasament + corp gol -> informatie' sa se declanseze pe astfel de emailuri.
# Sunt fraze FIXE de marketing/semnatura, deci le eliminam indiferent de pozitie (corpul
# HTML-stripped vine pe o singura linie, deci ancorarea pe linie nu ar prinde-o). Branch-ul
# Yahoo cere cuvantul-tagline (Cautare/Search) imediat dupa 'Yahoo Mail:', ca sa NU taie o
# propozitie reala de tip 'am o problema cu Yahoo Mail: nu pot trimite'.
_SIG_RE = re.compile(
    r"(?i)(?:"
    r"yahoo\s*mail\s*:\s*(?:c[aă]utare|search)[^\n.]*"          # Yahoo Mail: Cautare,... / Search, Organize, Conquer
    r"|sent\s+from\s+yahoo\s+mail"
    r"|(?:trimis|sent)\s+(?:de\s+pe|from)\s+(?:my\s+)?"
    r"(?:iphone|ipad|ipod|android|samsung|huawei)"
    r"|sent\s+from\s+my\s+(?:mobile|phone|mail)\b"
    r"|(?:get|download|desc[aă]rca[țt]i)\s+outlook\s+(?:for|pentru)\s+(?:ios|android)"
    r"|sent\s+from\s+mail\s+for\s+windows"
    r"|trimis\s+din\s+aplica[țt]ia\s+\w+"
    r")"
)


def _strip_signatures(text_in: str) -> str:
    """Elimina semnaturile automate de client de mail (Yahoo Mail, 'Trimis de pe iPhone', etc.)."""
    return _SIG_RE.sub(" ", text_in or "")


def _real_word_count(text_in: str) -> int:
    """Numarul de cuvinte 'reale' (>=2 litere) DUPA eliminarea semnaturilor auto si a URL-urilor.
    URL-urile si semnaturile contin multe token-uri alfabetice (https, intercom, Yahoo, Mail...)
    care ar masca un corp gol de continut."""
    if not text_in:
        return 0
    no_sig = _strip_signatures(text_in)
    no_urls = _URL_RE.sub(" ", no_sig)
    return len(re.findall(r"[^\W\d_]{2,}", no_urls, flags=re.UNICODE))


def _email_body(email: Dict[str, Any]) -> str:
    """Corpul textual al emailului — DOAR ultimul reply (continut nou), nu tot thread-ul citat.
    Categoria se decide pe ce a scris ACUM expeditorul: un thread vechi pe 'sesizare' nu mai
    contamineaza reply-urile noi (care sunt 'informatie'). Reutilizeaza quote-stripper-ul din
    phishing_detector._new_content (RO+EN), care cade pe body-ul INTEGRAL daca taierea ar lasa
    <3 caractere (bare forward / reply gol) — deci nu orbim niciodata clasificatorul.
    Implicit body_text (de regula partea curata). DAR daca body_text e sarac in cuvinte reale
    dupa eliminarea URL-urilor (ex. mailuri Intercom/Ruptela in care partea text contine DOAR
    linkuri de tracking, iar mesajul real e in HTML), cade pe HTML-ul curatat."""
    try:
        from app.services import phishing_detector as _pd
        new_text, new_html, _ = _pd._new_content(email)
    except Exception:
        new_text, new_html = email.get("body_text"), email.get("body_html")
    bt = (new_text or "").strip()
    if _real_word_count(bt) >= 5:
        return bt
    html_stripped = _strip_html(new_html or "").strip()
    if _real_word_count(html_stripped) > _real_word_count(bt):
        return html_stripped
    return bt or html_stripped


def _is_body_insignificant(body: str) -> bool:
    """True daca mesajul e gol sau nesemnificativ (doar salut/semnatura/cateva cuvinte, sau doar
    linkuri). URL-urile sunt eliminate inainte de numarare. Folosit pentru regula: corp gol +
    atasament -> informatie."""
    return _real_word_count(body) < 5


# ── Cerere de rutina: cand NU are voie sa intervina contextul istoric ────────────────────────
# OPS-2026-0141 (14.08.2026). office@evologistik.ro trimite ZILNIC acelasi mail: "Va rog sa
# alimentati contul cu suma din OP atasat". Blocul de CONTEXT ISTORIC (ultimele 5 mailuri ale
# expeditorului) transforma repetitia in "5 contactari fara raspuns" -> sesizare, iar la mailul
# urmator categoria veche din istoric devine "escaladare" -> reclamatie. Masurat pe 60334/60953/
# 64994/66023/66042: CU istoric ies sesizare/reclamatie, FARA istoric ies toate 'informatie' cu
# 0.92-0.95.
#
# Instructiunile in prompt n-au fost suficiente (Haiku tot invoca "fara raspuns documentat" pe
# 2 din 5), iar scoaterea categoriilor din istoric a rezolvat cele 5 dar a rupt detectia
# reclamatiilor reale (set de control cu adevar CTS: 90% -> 73%). De-aia filtrul e AICI,
# determinist si ingust: contextul se taie DOAR pentru mailurile care sunt clar cereri de rutina.
_ROUTINE_HINTS = (
    "alimentati contul", "alimentare cont", "alimentati", "incarcati contul", "incarcarea contului",
    "incarcare cont", "reincarcare cont", "reincarcati", "sa incarcati", "suma din op",
    "op atasat", "op-ul atasat", "ordinul de plata", "atasat op", "extras de cont",
    "va rog sa alimentati", "va rugam sa alimentati",
)
# Orice semnal de PROBLEMA sau de NEMULTUMIRE anuleaza scutirea: acolo istoricul chiar ajuta.
_TROUBLE_HINTS = (
    "nu functioneaza", "nu merge", "nu mai merge", "eroare", "defect", "problema", "probleme",
    "nu apare", "nu se vede", "nu vad", "nu transmite", "nu pot", "nu am primit", "nu s-a",
    "inca nu", "tot nu", "nu a fost", "nu ati", "nu mi-a", "nu ma", "nu am reusit",
    "v-am mai", "v-am scris", "v-am sunat", "am mai scris", "am mai sunat", "a doua oara",
    "a treia oara", "a patra oara", "de cate ori", "nimeni", "degeaba", "nemultumit",
    "nemultumire", "reziliez", "reziliere", "renunt", "reclamatie", "amenda", "penalitat",
    "gresit", "gresita", "incorect", "blocat", "urgent", "intarziere", "de ce nu",
)


def _deaccent(s: str) -> str:
    return (unicodedata.normalize("NFKD", s or "")
            .encode("ascii", "ignore").decode("ascii").lower())


def _is_routine_request(body: str, subject: str) -> bool:
    """True daca mailul e o cerere administrativa periodica, fara niciun semnal de problema.

    Conditii CUMULATIVE (deliberat inguste — in dubiu, pastram contextul):
      1. text scurt (sub 60 de cuvinte reale) — cererile de rutina sunt de 1-2 randuri;
      2. contine o formulare de rutina (alimentare cont / OP / extras);
      3. NU contine niciun marker de problema sau nemultumire.
    """
    txt = _deaccent(_strip_signatures(body or "") + " " + (subject or ""))
    if _real_word_count(body) > 60:
        return False
    if not any(h in txt for h in _ROUTINE_HINTS):
        return False
    return not any(h in txt for h in _TROUBLE_HINTS)


def _attachment_count(email: Dict[str, Any], attachments=None) -> int:
    """Numarul de atasamente. Daca lista nu e data, o numara din DB dupa email_id."""
    if attachments is not None:
        try:
            return len(attachments)
        except TypeError:
            return 0
    eid = email.get("id")
    if not eid:
        return 0
    try:
        db = SessionLocal()
        n = db.execute(text("SELECT count(*) FROM attachments WHERE email_id=:id"),
                       {"id": eid}).scalar()
        db.close()
        return int(n or 0)
    except Exception as e:
        logger.warning("attachment_count DB failed email_id=%s: %s", eid, e)
        return 0



def _get_email_history(email_id, from_address, conversation_id, received_at, limit=5):
    """Ultimele `limit` email-uri clasificate ale aceluiasi expeditor/conversatie.
    Returneaza lista DESC (cel mai recent primul), sau [] la eroare/lipsa istoric."""
    if not from_address and not conversation_id:
        return []
    try:
        db = SessionLocal()
        if conversation_id:
            rows = db.execute(text("""
                SELECT id, subject, from_address, from_name, received_at,
                       body_text, ai_category
                FROM emails
                WHERE id != :eid
                  AND ai_status = 'done'
                  AND (conversation_id = :conv OR lower(from_address) = lower(:frm))
                  AND received_at < :recv
                ORDER BY received_at DESC
                LIMIT :lim
            """), {"eid": email_id, "conv": conversation_id,
                   "frm": from_address or "", "recv": received_at, "lim": limit}).fetchall()
        else:
            rows = db.execute(text("""
                SELECT id, subject, from_address, from_name, received_at,
                       body_text, ai_category
                FROM emails
                WHERE id != :eid
                  AND ai_status = 'done'
                  AND lower(from_address) = lower(:frm)
                  AND received_at < :recv
                ORDER BY received_at DESC
                LIMIT :lim
            """), {"eid": email_id, "frm": from_address or "",
                   "recv": received_at, "lim": limit}).fetchall()
        db.close()
        return [dict(r._mapping) for r in rows]
    except Exception as e:
        logger.warning("_get_email_history failed email_id=%s: %s", email_id, e)
        return []


def _format_history_block(history):
    """Construieste blocul de context istoric pentru prompt. `history` vine DESC (cel mai recent
    primul) — inverseaza pentru afisare cronologica. Returneaza '' daca lista e goala."""
    if not history:
        return ""
    items = list(reversed(history))
    lines = [
        "=== CONTEXT ISTORIC (ultimele mailuri din aceeasi conversatie/expeditor, "
        "de la cel mai vechi la cel mai recent) ===",
        "REGULA: email-ul curent (marcat mai jos) este DECISIV. Contextul arata "
        "tipare recurente si escaladari — nu inlocuieste intentia email-ului curent.",
        # Vezi nota de la _CONTEXT_HINT: fara avertismentele astea, un client care trimite zilnic
        # aceeasi cerere de rutina ajunge clasificat 'reclamatie' din simpla repetitie.
        "ATENTIE: mailuri repetate cu ACEEASI cerere de rutina (alimentare cont, trimitere OP/"
        "documente, cerere de factura) inseamna doar ca activitatea e periodica — NU ca cererile "
        "anterioare au fost ignorate. Lista de mai jos contine DOAR mailuri PRIMITE de la client; "
        "raspunsurile noastre nu apar aici, deci 'fara raspuns' NU se poate deduce din ea. "
        "Categoriile afisate sunt incadrari anterioare ale aceluiasi sistem, nu adevar verificat: "
        "nu le prelua in lant, o eroare veche s-ar agrava la fiecare mail.\n",
    ]
    for i, em in enumerate(items, 1):
        cat = em.get("ai_category") or "?"
        frm = ((em.get("from_name") or "") + " <" + (em.get("from_address") or "") + ">").strip()
        subj = (em.get("subject") or "")[:80]
        recv = ""
        if em.get("received_at"):
            try:
                recv = str(em["received_at"])[:10]
            except Exception:
                pass
        body_raw = (em.get("body_text") or "").strip()
        body_snippet = body_raw[:250].replace("\n", " ").strip()
        if len(body_raw) > 250:
            body_snippet += "..."
        lines.append(f"[{i}/{len(items)}] {recv} | {cat} (mail primit de la client)")
        lines.append(f"De la: {frm}")
        if subj:
            lines.append(f"Subiect: {subj}")
        if body_snippet:
            lines.append(f'"{body_snippet}"')
        lines.append("")
    return "\n".join(lines)


def _email_to_content(email: Dict[str, Any], att_count: int = 0) -> str:
    subject = email.get("subject") or ""
    frm = ((email.get("from_name") or "") + " <" + (email.get("from_address") or "") + ">").strip()
    body = _email_body(email)
    att_line = ("Atasamente: " + str(att_count) + "\n") if att_count else ""
    # La REPLY-uri (Re:/Fwd:/Răspuns:) subiectul e tema MOȘTENITĂ din thread și poate să NU reflecte
    # mesajul nou (ex. „Re: Sesizare…" + reply „VDO la ambele vehicule" = informatie). Îl etichetăm ca
    # atare și cerem intenția din mesajul NOU; pe emailuri fresh (fără prefix) păstrăm „Subiect:" normal,
    # ca să nu slăbim cazul cu intenția DOAR în subiect (body gol).
    if re.match(r"\s*(re|fwd|fw|răspuns|raspuns)\s*:", subject, re.IGNORECASE):
        content = (f"Subiect thread (temă moștenită din mesaje anterioare — poate să NU reflecte mesajul nou): {subject}\n"
                   f"De la: {frm}\n{att_line}\n"
                   f"Mesajul NOU al expeditorului (clasifică intenția DUPĂ acest text; folosește subiectul doar dacă mesajul e gol):\n{body}").strip()
    else:
        content = f"Subiect: {subject}\nDe la: {frm}\n{att_line}\n{body}".strip()

    summary = email.get("_context_summary") or {}
    atitudine = summary.get("atitudine", "necunoscuta")
    satisfactie = summary.get("satisfactie", "necunoscuta")
    nemultumire = summary.get("nemultumire_principala")
    numar_contacte = summary.get("numar_contacte", 0)

    if atitudine != "necunoscuta" or satisfactie != "necunoscuta" or nemultumire:
        context_lines = [
            "\n=== CONTEXT CLIENT (factor secundar — nu suprascrie reply-ul) ===",
            f"Atitudine generala a clientului (ultimele 5 zile): {atitudine}",
            f"Satisfactie generala: {satisfactie}",
        ]
        if nemultumire:
            context_lines.append(f"Principala nemultumire anterioara: {nemultumire}")
        if numar_contacte:
            context_lines.append(f"Numar contacte recente: {numar_contacte}")
        context_lines.append(
            "REGULA: Clasifica pe baza REPLY-ULUI de mai sus. Contextul e informatie de fond "
            "— ajuta decizia cand reply-ul e ambiguu, dar NU suprascrie un reply clar "
            "(ex: dovada de plata = informatie, chiar daca contextul indica nemultumire)."
        )
        content += "\n" + "\n".join(context_lines)

    return content


def _normalize(parsed: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(parsed, dict):
        return None
    cat = str(parsed.get("category") or "").strip().lower()
    if cat not in CATEGORIES:
        cat = "necunoscut"
    try:
        conf = float(parsed.get("confidence"))
        conf = max(0.0, min(1.0, conf))
    except (TypeError, ValueError):
        conf = None
    reason = parsed.get("reason")
    reason = str(reason).strip()[:400] if reason else None
    return {"category": cat, "confidence": conf, "reason": reason}


def _fallback_unknown_to_info(norm: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Politică (2026-06-22): 'necunoscut' NU mai e o categorie finală livrabilă. Dacă încadrarea
    nu e clară, o tratăm ca 'informatie' (fallback sigur), ca să NU plece niciodată 'necunoscut'
    spre CTS. `None` (clasificator indisponibil/eroare) rămâne `None` — e o stare de re-procesare,
    nu o categorie. Confidența și motivul inițial se păstrează pentru audit."""
    if not norm or not isinstance(norm, dict):
        return norm
    if (norm.get("category") or "").strip().lower() == "necunoscut":
        norm = dict(norm)
        orig = norm.get("reason")
        norm["category"] = "informatie"
        norm["unknown_fallback"] = True
        base = ("Fallback: încadrare neclară/necunoscută → tratat ca Informație "
                "(necunoscut nu se trimite la CTS).")
        norm["reason"] = (base + (" Motiv inițial: " + orig if orig else ""))[:400]
    return norm


def classify_category(email: Dict[str, Any],
                      prompts: Optional[Dict[str, str]] = None,
                      attachments=None,
                      force_fresh: bool = False) -> Optional[Dict[str, Any]]:
    """Wrapper public: clasifică și aplică fallback-ul 'necunoscut' → 'informatie'
    (vezi _fallback_unknown_to_info). Căile interne pot încă produce 'necunoscut' (folosit de
    cascadă pentru escaladare la Claude); doar rezultatul FINAL livrat e normalizat aici.
    `force_fresh=True` ocolește curated-cache (reclasificare cu prompturile curente)."""
    return _fallback_unknown_to_info(
        _classify_category_impl(email, prompts, attachments, force_fresh=force_fresh))


def _classify_category_impl(email: Dict[str, Any],
                            prompts: Optional[Dict[str, str]] = None,
                            attachments=None,
                            force_fresh: bool = False) -> Optional[Dict[str, Any]]:
    """Return {category, confidence, reason} or None if unavailable/failed.

    `attachments`: lista atasamentelor daca apelantul o are deja (process_one); daca e None,
    se numara din DB dupa email_id (advance_one_clean). Folosit pentru:
      - regula determinista: corp gol/nesemnificativ + atasament -> 'informatie' (fara apel AI);
      - semnalul 'Atasamente: N' din continut, care ajuta AI sa incadreze.
    Limba straina (ex. engleza) e tratata in promptul de sistem: AI traduce mental si incadreaza."""
    # Override DETERMINIST pe expeditor (mailuri automate), inaintea oricarui apel AI/cache.
    forced = _forced_category(email.get("from_address"))
    if forced:
        return {"category": forced, "confidence": 1.0,
                "reason": "Regulă expeditor automat: %s → %s." % (
                    (email.get("from_address") or "").strip().lower(), forced),
                "model": "rule", "forced": True}

    body = _email_body(email)
    att_n = _attachment_count(email, attachments)

    # Regula determinista (independenta de AI si de limba): corp gol + atasament -> informatie.
    if att_n > 0 and _is_body_insignificant(body):
        return {"category": "informatie", "confidence": 0.9,
                "reason": "Email cu atasament si corp gol/nesemnificativ — transmitere de documente (informare)."}

    if not iris_ai.is_configured():
        return None
    content = _email_to_content(email, att_count=att_n)

    # Context istoric (OPS-2026-0129): ultimele 5 mailuri clasificate ale aceluiasi expeditor.
    # Daca nu exista istoric (client nou, eroare DB) -> hist_block="" -> content neschimbat.
    #
    # EXCEPTIE (OPS-2026-0141): cererile de rutina NU primesc context. Vezi _is_routine_request —
    # pentru ele istoricul nu adauga informatie, doar transforma repetitia in "escaladare".
    if _is_routine_request(body, email.get("subject")):
        logger.info("category: context istoric sarit (cerere de rutina) email_id=%s",
                    email.get("id"))
        history = []
    else:
        history = _get_email_history(
            email_id=email.get("id"),
            from_address=email.get("from_address"),
            conversation_id=email.get("conversation_id"),
            received_at=email.get("received_at"),
            limit=5,
        )
    hist_block = _format_history_block(history)
    if hist_block:
        content = (hist_block
                   + "=== EMAIL CURENT (clasificati ACESTA; contextul e doar informativ) ===\n"
                   + content)
    if not content.strip() or len(content.strip()) < 3:
        # nimic de clasificat -> categoria implicita 'informatie' (NU lasam email fara categorie)
        return {"category": "informatie", "confidence": None,
                "reason": "Conținut insuficient — încadrat implicit la Informație."}
    system = build_system_prompt(prompts)

    # Cale FRESH (reclasificare): ocoleste curated-cache ca prompturile CURENTE sa se aplice.
    # Lever dovedit (vezi branch-ul "curated incert" de mai jos): task SARAT cu sha1(system+content)
    # + no_cache=True (skip_cache la gateway). FARA learn -> nu repoluam cache-ul.
    if force_fresh:
        _salt = hashlib.sha1((system + "\x1e" + content).encode("utf-8")).hexdigest()[:10]
        res = iris_ai.run_prompt(
            system, content, response_format="json", temperature=0.0, max_tokens=200,
            task="cargo360:email_category:" + _salt, email_id=email.get("id"), no_cache=True)
        if not res.get("ok"):
            logger.warning("category classify (fresh) failed: %s", res.get("error"))
            return None
        norm = _normalize(res.get("parsed"))
        if norm:
            norm["model"] = res.get("model")
            norm["fresh"] = True
        return norm

    # CATEGORIE: model FIX Haiku, FĂRĂ cache și FĂRĂ curated/learn (decizie 2026-07-24, simetric
    # cu departamentul). Vechea cascadă gemma->curated cu use_cache=True servea răspunsuri vechi
    # memorate care încadrau greșit (ex. mailuri Informație rămâneau pe Sesizare din cache curat —
    # #53302, #53449). Acum: fiecare mail e reevaluat PROASPĂT cu Haiku, task sărat cu
    # sha1(system+content) + no_cache=True => zero cache. Fără learn/learn_scope => nu se mai
    # populează ai_curated_ext pe categorie.
    _salt = hashlib.sha1((system + "\x1e" + content).encode("utf-8")).hexdigest()[:10]
    res = iris_ai.run_prompt(
        system, content, response_format="json",
        model_hint="claude-haiku-4-5-20251001",  # ID complet — „haiku" scurt cade pe sonnet la gateway
        temperature=0.0, max_tokens=200,
        task="cargo360:email_category:" + _salt,
        email_id=email.get("id"), no_cache=True,
    )
    if not res.get("ok"):
        logger.warning("category classify failed: %s", res.get("error"))
        return None
    norm = _normalize(res.get("parsed"))
    if norm:
        norm["model"] = res.get("model")
        norm["escalated"] = False
        norm["from_cache"] = False
    return norm
