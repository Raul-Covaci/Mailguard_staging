"""Reguli deterministe de incadrare pe PRIORITATE (P0 urgent).

Spre deosebire de department_rules (editabile din Setari), regulile de prioritate sunt
definite in COD: sunt semnale TARI, PUTINE si STABILE, care forteaza P0 indiferent de scorul
AI. Restul nuantelor (ton, disperare implicita, context) le decide AI-ul prin prompt.

Filozofie (dupa feedback: prea multe false-positive): regulile prind DOAR semnale care nu pot fi
confundate cu rutina. NU declansam P0 doar pentru ca exista un atasament sau cuvantul "factura":
- facturile, proformele, remindere de scadenta, mailurile automate CargoTrack = rutina (P1);
- un OP / dovada de plata trimisa de client, sau disperare clara = P0.

`match(email, att_names="")` -> dict {id, tier, note} daca un semnal determinist loveste
(plata -> tier P2; urgenta/furie -> tier P3), altfel None.
"""
import re
import unicodedata

# id-uri de reguli (pentru audit in ai_priority_result.rule_id)
RULE_PAYMENT = "pay_proof"      # dovada/confirmare de plata explicita
RULE_OP = "pay_op"             # ordin de plata (OP) trimis de client
RULE_ATTACHMENT = "pay_attachment"  # atasament cu nume clar de OP/dovada
RULE_URGENCY = "urgency"       # disperare / urgenta clara
RULE_SCOUT = "scout_report"     # raport intern "Email Scout Report" (office@cargotrack.ro)
RULE_PROCESSOR = "pay_processor"  # notificare de la un procesator de plati online


def _fold(s: str) -> str:
    """lower + elimina diacriticele (ca 'plătesc' sa prinda si 'platesc')."""
    s = (s or "")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


# --- Mailuri AUTOMATE / template CargoTrack -> NICIODATA P0 prin regula ---
# Reminder de scadenta, "factura a fost emisa", footer "| CargoTrack", extras bancar zilnic etc.
# Sunt rutina: daca un client raspunde furios pe un astfel de thread, decizia o ia AI-ul (prompt),
# nu regula. Asta elimina FP-urile de tip "Informare scadenta factura | CargoTrack".
_AUTOMATED_SUBJECT = [
    "informare scadenta",
    "scadenta factura",
    "| cargotrack",
    "|cargotrack",
    "solutii de monitorizare",
    "a fost emisa",            # "Factura ... a fost emisa"
    "factura de servicii",
    "extras zilnic",
]


# --- Dovada / confirmare de plata EXPLICITA trimisa de client -> P0 ---
# Doar formulari neechivoce ca "am platit / confirmare plata / dovada platii". NU "factura" simpla.
_PAYPROOF_PATTERNS = [
    r"confirmare(a)? (de )?plat",       # confirmare(a) (de) plata
    r"confirmarea platii",
    r"dovad[ai] (de )?plat",            # dovada (de) plata
    r"am (efectuat|facut) plata",
    r"am achitat",
    r"am platit",
    r"v-?am platit",
    r"plata (efectuat|trimis)",
    r"am trimis (op|confirmarea|dovada|ordinul)",
    r"atase?z (op|ordinul|dovada|confirmarea|extrasul)",
    r"plat[ai] neprocesat",             # plata neprocesata
    r"nu s-a procesat plata",
    r"plata (blocat|respins|refuzat|nu a (fost )?intrat)",
    r"(nu pot|nu reusesc|nu am reusit|nu se poate) (sa )?(incarc|alimentez|aliment) cont",
    r"(incarcare|alimentare) cont (esuat|nereusit|blocat|nu a)",
]

# --- Urgenta CLARA / disperare -> P0 ---
# Tinut strict: disperare/repetare/amenintare reala. "reziliere" administrativa NU intra aici.
_URGENCY_PATTERNS = [
    r"\burgent\b",
    r"de urgent",
    r"foarte urgent",
    r"a (treia|patra|cincea|sasea) oar[ai]",
    r"a (\d+)-?a oar[ai]",
    r"pentru a (\d+|cata|nu stiu cata) (-?a )?oar",
    r"v-?am (scris|trimis|sunat|solicitat) de (mai multe|cateva|atatea|nenumarate) ori",
    r"de (mai multe|cateva|nenumarate) ori (v-?am|am) (scris|trimis|solicitat|sunat)",
    r"solicitari repetate",
    r"nu mai pot",
    r"plat(esc|im|esti) degeaba",
    r"plata degeaba",
    r"bataie de joc",
    r"(va|imi|ne) bate(ti)? joc",
    r"inacceptabil",
    r"inadmisibil",
    r"depun (o )?(plangere|reclamatie)",
    r"actionez in (judecata|instanta)",
    r"chem in (judecata|instanta)",
]

_PAYPROOF_RE = [re.compile(p) for p in _PAYPROOF_PATTERNS]
_URGENCY_RE = [re.compile(p) for p in _URGENCY_PATTERNS]

# Nume de fisier care indica CLAR un OP / dovada de plata. Strict, multi-cuvant:
# evitam "plata"/"payment"/"tranzactie"/"completed" simple care apar pe facturi/taloane de rutina.
_ATTACHMENT_STRICT = [
    "ordin de plata", "ordin_de_plata", "ordin-de-plata", "ordinplata",
    "dovada plata", "dovada_plata", "dovada-plata", "dovada de plata",
    "confirmare plata", "confirmare_plata", "confirmare-plata",
    "op cargo", "op_cargo",
    "swift",
]

# Prefixe de reply de curatat din subiect inainte de detectia OP.
_REPLY_PREFIX = re.compile(r"^\s*(re|fwd|fw|rasp|raspuns|raspunde)\s*:\s*")


def _strip_reply(subj: str) -> str:
    for _ in range(6):
        m = _REPLY_PREFIX.match(subj)
        if not m:
            break
        subj = subj[m.end():]
    return subj.strip()


def _new_body(email: dict) -> str:
    """Corpul DOAR al mesajului nou (ultimul reply), nu tot thread-ul citat.

    Regulile trebuie sa se uite la ce a scris ACUM expeditorul: un 'dovada de plata' sau
    'platesc degeaba' aflat in ISTORICUL citat al thread-ului nu mai conteaza pentru mesajul nou
    (ex. o cerere despre date GPS cu o veche confirmare de plata dedesubt). Reutilizeaza
    quote-stripper-ul comun (phishing_detector._new_content, RO+EN). Daca taierea ar lasa corpul
    gol (reply/forward fara text nou), cade pe corpul integral ca sa nu orbeasca regulile.
    """
    nt = nh = None
    try:
        from app.services import phishing_detector as _pd
        nt, nh, _ = _pd._new_content(email)
    except Exception:
        nt, nh = email.get("body_text"), email.get("body_html")
    joined = ((nt or "") + " " + (nh or "")).strip()
    if not joined:
        joined = (email.get("body_text") or "") + " " + (email.get("body_html") or "")
    return _fold(joined)


def _attachment_strict_hit(att_names: str) -> bool:
    hay = _fold(att_names)
    if not hay:
        return False
    for kw in _ATTACHMENT_STRICT:
        if kw in hay:
            return True
    return False


# --- Raport intern "Email Scout Report" (office@cargotrack.ro) -> P4, FORTAT ---
# E un raport generat de aplicatie, nu o cerere de client. Contine citate din mailuri (inclusiv
# OP-uri / dovezi de plata), deci ajungea pe P2 prin regulile de plata sau prin seria OP extrasa
# de vision AI. `match_forced` se evalueaza INAINTEA oricarui alt semnal (vezi
# priority_classifier._classify_priority_core), deci nimic nu il mai poate urca.
_SCOUT_FROM = "office@cargotrack.ro"
_SCOUT_SUBJECT = "email scout report"


def match_forced(email: dict):
    """Reguli care bat ORICE alt semnal de prioritate (inclusiv ai_op_series). Altfel None."""
    hay_from = _fold((email.get("from_address") or "") + " " + (email.get("from_name") or ""))
    subj = _fold(email.get("subject") or "")
    if _SCOUT_FROM in hay_from and _SCOUT_SUBJECT in subj:
        return {"id": RULE_SCOUT, "tier": "P4",
                "note": "Email Scout Report de la office@cargotrack.ro -> P4 (raport intern)."}
    return None


# --- Procesatori de plati online -> P2, pe EXPEDITOR ---
# Notificarile lor sunt, prin definitie, despre bani intrati sau esuati: se lucreaza inaintea
# rutinei, chiar cand textul nu contine nicio formulare de tip "am platit". Cerere business
# 2026-09-12, aceiasi expeditori care merg pe Contabilitate.
#
# Lista sta AICI, in cod, nu in `settings`: regulile de prioritate sunt deliberat in cod (semnale
# tari, putine, stabile), spre deosebire de regulile de departament, care se editeaza din Setari.
# Cele doua liste se schimba deci in DOUA locuri — `department_rules.DEFAULT_RULES` + migratia
# care le duce in store, si aici.
#
# Potrivire pe SUBSTRING in expeditor (adresa + nume), ca la regulile de departament: euPlatesc
# trimite de pe mai multe cutii si domenii (euplatesc.ro / .com), iar pe europayment.services sunt
# patru cutii cunoscute (notificari@, contact@, noreply@, suport@) si oricand alta noua.
_PAYMENT_PROCESSORS = ("euplatesc", "@europayment.services")


def _is_payment_processor(email: dict) -> bool:
    hay = _fold((email.get("from_address") or "") + " " + (email.get("from_name") or ""))
    return any(p in hay for p in _PAYMENT_PROCESSORS)


def match(email: dict, att_names: str = ""):
    """Returneaza dict {id, tier, note} pentru primul semnal determinist care loveste, altfel None.

    Precedenta P2 > P3: intai PLATILE (P2), apoi urgenta/furie fara plata (P3).
      0.0) regula FORTATA (Email Scout Report) -> P4, inaintea oricarui alt semnal;
      0.1) procesator de plati online (euPlatesc / europayment.services) -> P2, pe expeditor;
      0) mail automat/template CargoTrack -> None (decide AI);
      1) dovada/confirmare de plata explicita -> P2 (plata);
      2) subiect care e un OP (ordin de plata) trimis de client -> P2 (plata);
      3) atasament cu nume clar de OP/dovada de plata -> P2 (plata);
      4) disperare / urgenta clara, FARA plata -> P3 (sesizare/reclamatie).
    """
    # 0.0) Reguli fortate (raport intern) — bat orice alt semnal.
    forced = match_forced(email)
    if forced:
        return forced

    # 0.1) Procesator de plati online -> P2 pe EXPEDITOR, INAINTEA filtrului de mailuri automate.
    # Notificarile lor sunt automate prin natura lor; daca subiectul prinde un tipar din
    # `_AUTOMATED_SUBJECT`, regula de mai jos ar returna None si decizia ar cadea pe AI, care le-ar
    # da tipic P5. Pusa aici, incadrarea P2 e garantata.
    if _is_payment_processor(email):
        return {"id": RULE_PROCESSOR, "tier": "P2",
                "note": "Notificare de la un procesator de plati online -> P2 (plata)."}

    subj = _fold(email.get("subject") or "")
    body = _new_body(email)

    # 0) Mail automat / template -> nu fortam nimic; lasam AI-ul sa decida.
    if any(kw in subj for kw in _AUTOMATED_SUBJECT):
        return None

    # 1) Dovada / confirmare de plata explicita (subiect sau corp) -> P2
    for rx in _PAYPROOF_RE:
        if rx.search(subj) or rx.search(body):
            return {"id": RULE_PAYMENT, "tier": "P2",
                    "note": "Dovada / confirmare de plata trimisa de client -> P2 (plata)."}

    # 2) Subiect = ordin de plata (OP) sau plata efectiva trimisa de client -> P2.
    # Cautam DOAR in subiect (corpul e prea zgomotos).
    # Include si "plata taxe drum / taxe de drum / taxe intracomp" — formulari tipice prin care
    # clientii confirma o plata de taxa de drum fara a scrie explicit "OP".
    subj_core = _strip_reply(subj)
    _OP_SUBJ_RE = re.compile(
        r"\bop\b"
        r"|ordin de plata"
        r"|plata (taxe|taxa) (drum|de drum|intracomp|rutier)"
        r"|plata drum"
        r"|taxa drum (platit|efectuat|trimis|atasat)"
        r"|plata (factur|abonament|servicii).{0,30}(atasat|trimis|efectuat)"
    )
    if _OP_SUBJ_RE.search(subj_core):
        return {"id": RULE_OP, "tier": "P2",
                "note": "Ordin de plata / plata taxa drum in subiect -> P2 (plata)."}

    # 3) Atasament cu nume clar de OP / dovada de plata -> P2
    if _attachment_strict_hit(att_names):
        return {"id": RULE_ATTACHMENT, "tier": "P2",
                "note": "Atasament cu nume clar de OP / dovada de plata -> P2 (plata)."}

    # 4) Urgenta clara / disperare FARA plata -> P3 (sesizare/reclamatie)
    for rx in _URGENCY_RE:
        if rx.search(subj) or rx.search(body):
            return {"id": RULE_URGENCY, "tier": "P3",
                    "note": "Limbaj de urgenta clara / disperare -> P3 (sesizare/reclamatie)."}

    return None
