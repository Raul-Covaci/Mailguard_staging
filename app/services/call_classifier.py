"""Clasificare internă apeluri: categorie + stil (T3) + Agent (fuzzy match pe transcript,
reutilizând lista de angajați deja folosită de emailuri) + Client (match pe telefon).

Categoria + stilul folosesc un prompt DEDICAT apelurilor (ai_call_category_prompts), separat
de cel de la emailuri — taxonomia de categorie e aceeași (informatie/sesizare/reclamatie/
necunoscut), dar sursa (transcript AGENT/CLIENT, nu corp email) și atributul suplimentar
"stil" (tonul conversației) sunt specifice apelurilor. Un singur apel IRIS întoarce
{categorie, stil, motivare_scurta} (vezi classify_call).

Agentul se identifică ÎN PRIMUL RÂND din `agent_extension` (câmpul `user_fullname` din CDR-ul
While1, ex. "Robert Iova") — descoperit pe 2026-07-02 (apel #342) că e mult mai fiabil decât
ghicitul din transcript (acolo un simplu "Robert" rostit în deschidere s-a potrivit ambiguu cu
DOI angajați: Iova Oliviu-Robert și Kovacs Robert; CDR-ul confirmă direct "Robert Iova").
Fallback pe transcript (agenții se prezintă la începutul apelului, "Bună ziua, sunt Andrei de
la CargoTrack...") DOAR când `agent_extension` lipsește sau nu se potrivește sigur pe niciun
angajat. Ambele căi refolosesc aceeași sursă ca la asignarea emailurilor,
`employee_department_mapping`, prin assignee_classifier._load_employees/_name_tokens — reused,
not copied.
"""
import json
import logging
from typing import Optional, Dict, Any

from sqlalchemy import text
from app.database import SessionLocal
from app.services import iris_ai
from app.services import assignee_classifier
from app.services import phone_match
from app.services.json_salvage import salvage_json

logger = logging.getLogger("mailguard.call_classifier")

AGENT_OPENING_CHARS = 300  # primele ~300 caractere din transcript, unde agentul se prezinta

CATEGORIES = ["informatie", "sesizare", "reclamatie", "necunoscut"]
EDITABLE = ["informatie", "sesizare", "reclamatie"]
TONES = ["neutru", "tensionat", "prietenos"]

DEFAULT_CALL_PROMPTS: Dict[str, str] = {
    "informatie": (
        "Se incadreaza la categoria Informatii orice apel in care clientul NU semnaleaza o problema/"
        "disfunctionalitate si NU exprima nemultumire, ci are scop informativ, de coordonare, confirmare "
        "sau administrativ.\n"
        "Tipuri: (1) Cereri de informatii/lamuriri, inclusiv vagi. (2) Solicitari administrative/"
        "operationale (implementare user/sofer, activare serviciu, mutare vehicul, procesare documente). "
        "(3) Confirmari si notificari (confirma plata/programare/discutie anterioara). (4) Actualizari de "
        "status operational (locatie vehicule, intrare/iesire tara, finalizare cursa/descarcare). "
        "(5) Redirectionare/transfer (cere transfer la alt coleg/departament, revenire ulterioara, apel "
        "gresit). (6) Apeluri fara obiect clar (numar gresit, apel pierdut/scapat, verificare, persoane "
        "care intreaba doar ce face compania). (7) Clarificari tehnice fara disfunctionalitate (tonaj, "
        "masa, functionalitati platforma, incarcare documente/poze). (8) Clarificari financiare/facturare "
        "fara contestare (confirmare plata, suspendare temporara, stabilire facturare, info taxe drum). "
        "(9) Comunicari procedurale (modificari procedura, instructiuni, suspendari temporare). "
        "(10) Confirmari de rezolvare (problema anterioara s-a rezolvat, fara sesizare noua). "
        "(11) Indisponibilitate si reprogramare (nu poate continua, cere amanare, fara a semnala o "
        "problema).\n"
        "ATENTIE: daca exista nemultumire sau o problema activa -> NU este Informatii. Clientul care "
        "comunica o actiune (plata, alimentare, trimitere document) fara a cere interventie si fara a "
        "semnala o problema -> Informatii. Apelurile foarte scurte, fara solicitare clara si fara "
        "problema (inclusiv apeluri gresite/inchise rapid) -> Informatii (NU necunoscut)."
    ),
    "sesizare": (
        "Se incadreaza la Sesizare orice apel in care clientul semnaleaza o problema, disfunctionalitate, "
        "eroare sau neconcordanta (tehnica, functionala, administrativa sau financiara) si asteapta "
        "interventie/remediere. Nu e neaparat agresiv.\n"
        "Tipuri: (1) Probleme financiare/administrative (debitari incorecte, facturi gresite, plati duble, "
        "tranzactii necunoscute). (2) Erori de aplicatie/software (nu merge descarcarea, da eroare, nu se "
        "actualizeaza, versiune veche). (3) Probleme cu dispozitive/carduri (card nu functioneaza, "
        "tahograf nu citeste, dispozitiv defect). (4) Probleme de transmisie/vizualizare date (nu "
        "transmite, nu vede masina, locatie gresita, date care nu coincid, transmisie intermitenta). "
        "(5) Probleme multiple/la scara (la toate masinile la fel). (6) Probleme de acces/conectivitate "
        "(nu poate accesa platforma, nu se poate loga, cont blocat).\n"
        "Cuvinte-cheie (rostite de client): nu functioneaza, defect, eroare, nu se vede, nu apare, am o "
        "problema cu..., nu pot accesa, nu merge, nu transmite, nu se actualizeaza.\n"
        "ATENTIE: daca clientul doar cere informatii/confirma o actiune/face o cerere administrativa FARA "
        "a semnala o problema -> NU este Sesizare (este Informatii). Sesizarea poate fi exprimata si "
        "indirect ('ceva nu e in regula'). Daca problema este semnalata PENTRU PRIMA DATA in acest apel "
        "-> Sesizare. Daca exprima nemultumire ca o problema ANTERIOARA nu a fost rezolvata, cu referire "
        "la contactari anterioare esuate -> verificati Reclamatie."
    ),
    "reclamatie": (
        "Se incadreaza la Reclamatie orice apel in care clientul exprima nemultumire explicita fata de "
        "modul in care compania a gestionat (sau nu) o problema ANTERIOARA. Presupune DOUA elemente "
        "simultan: (1) o problema/solicitare anterioara si (2) nemultumirea ca nu a fost rezolvata/"
        "tratata corespunzator.\n"
        "DIFERENTA fata de Sesizare: Sesizare = problema activa, semnalata prima data in acest apel; "
        "Reclamatie = clientul a INCERCAT DEJA sa obtina rezolvare (apeluri/mailuri anterioare, "
        "promisiuni) si compania nu a reactionat adecvat.\n"
        "Tipuri: (1) Lipsa de reactie la contactari anterioare ('v-am mai sunat/scris si nu mi-a raspuns "
        "nimeni'). (2) Promisiuni nerespectate ('mi s-a promis ca se rezolva si nu s-a intamplat'). "
        "(3) Probleme repetitive/nerezolvate ('e a treia oara cand sun pentru aceeasi problema'). "
        "(4) Nemultumire privind calitatea serviciului ('sunt foarte nemultumit', 'ce fel de suport e "
        "asta'). (5) Amenintari de reziliere/plecare ('daca nu se rezolva, renunt la contract'). "
        "(6) Nemultumire financiara cu referinta la esec anterior ('v-am zis de factura gresita si tot nu "
        "s-a corectat').\n"
        "Cuvinte-cheie (rostite de client): v-am mai sunat, v-am mai scris, nu mi-a raspuns nimeni, mi "
        "s-a promis, e a doua/a treia oara, nu este normal, sunt foarte nemultumit, de cate ori, "
        "niciodata, degeaba, renunt, reziliez.\n"
        "ATENTIE: o problema semnalata PRIMA DATA in acest apel, fara referinta la contactari anterioare "
        "esuate si fara nemultumire explicita -> Sesizare, NU Reclamatie. Tonul agresiv/tensionat singur "
        "NU e suficient: trebuie referire la un esec anterior al companiei. Daca exprima nemultumire DAR "
        "semnaleaza si o problema noua: daca predomina nemultumirea fata de lipsa de reactie -> "
        "Reclamatie; daca predomina problema noua -> Sesizare."
    ),
}

_TONE_INSTRUCTIONS = (
    '3. "stil" — tonul general al conversatiei, una dintre: neutru, tensionat, prietenos\n'
    "   - prietenos: client colaborativ, calm SAU multumit la final; problema clarificata/rezolvata\n"
    "     fara frictiune; multumiri, ton amabil, colaborare normala. ACESTA e cazul IMPLICIT pentru\n"
    "     un apel de rutina care decurge OK — nu incadra la 'neutru' din inertie.\n"
    "   - neutru: schimb strict tranzactional, fara NICIUN semnal pozitiv sau negativ (rar).\n"
    "   - tensionat: client nervos/nemultumit, ton ridicat, reprosuri, plangeri repetate.\n"
)

_BASE_HEAD = (
    "Esti un asistent care clasifica apeluri telefonice CargoTrack pe baza transcriptului.\n"
    "Primesti un transcript cu replicile etichetate AGENT: si CLIENT:.\n\n"
    'Determina:\n1. "categorie" — EXACT una dintre: informatie, sesizare, reclamatie\n'
    "REGULA IMPLICITA OBLIGATORIE: FIECARE apel primeste o categorie — NU exista 'necunoscut' si NU "
    "lasa categoria goala. Daca transcriptul e neclar/scurt/ambiguu, fara obiect clar (inclusiv apeluri "
    "gresite/inchise rapid) sau nu se incadreaza clar la 'sesizare'/'reclamatie', incadreaza-l implicit "
    "la 'informatie' — este alegerea SIGURA cand nimic altceva nu se potriveste cu incredere.\n\n"
)
_BASE_TAIL = (
    '2. "motivare_scurta" — 1-2 propozitii, de ce ai ales categoria/stilul\n\n'
    "Raspunde STRICT in format JSON, fara text in plus:\n"
    '{"categorie": "...", "stil": "...", "motivare_scurta": "..."}'
)

# ── T3-L3: parsare tolerantă (flag settings['processing.call_category_v2_enabled'], implicit OFF) ──
# 92% din apelurile plătite ale `call_category` se terminau cu JSON_PARSE_ERROR (doar ~1% ating
# max_tokens, deci nu e trunchiere): modelul învelește JSON-ul în ``` sau adaugă text. Rezultatul era
# aruncat, `ai_category` rămânea NULL și `process_pending_batch` plătea aceeași conversație la fiecare
# tick. Cu flag-ul ON: instrucțiune explicită în prompt + extragere din raw_text (`json_salvage`,
# același utilitar ca scorarea apelurilor), validată pe câmpurile obligatorii și pe CATEGORIES.
# Cu flag-ul OFF: prompt și parsare identice cu versiunea anterioară.
_V2_KEY = "processing.call_category_v2_enabled"
_JSON_ONLY = "Răspunde DOAR cu obiectul JSON, fără alt text și fără ```.\n"
_V2_REQUIRED = ("categorie", "stil", "motivare_scurta")
_BASE_TAIL_V2 = _BASE_TAIL.replace(
    "Raspunde STRICT in format JSON, fara text in plus:\n",
    "Raspunde STRICT in format JSON, fara text in plus:\n" + _JSON_ONLY, 1)


def load_call_prompts() -> Dict[str, str]:
    """Prompturile editabile pe categorie (DB peste default-urile din cod)."""
    out = dict(DEFAULT_CALL_PROMPTS)
    try:
        db = SessionLocal()
        rows = db.execute(text("SELECT category, prompt_text FROM ai_call_category_prompts")).fetchall()
        db.close()
        for r in rows:
            cat = r._mapping["category"]
            txt = r._mapping["prompt_text"]
            if cat in EDITABLE and txt and txt.strip():
                out[cat] = txt
    except Exception as e:
        logger.warning("load_call_prompts DB failed, using defaults: %s", e)
    return out


def build_call_system_prompt(prompts: Optional[Dict[str, str]] = None, json_only: bool = False) -> str:
    """`json_only=True` (T3-L3, doar cu flag-ul v2) adaugă instrucțiunea explicită „DOAR JSON"."""
    p = prompts or load_call_prompts()
    defs = (
        "   - informatie: " + p["informatie"] + "\n"
        "   - sesizare: " + p["sesizare"] + "\n"
        "   - reclamatie: " + p["reclamatie"] + "\n"
    )
    return _BASE_HEAD + defs + _TONE_INSTRUCTIONS + (_BASE_TAIL_V2 if json_only else _BASE_TAIL)


def call_category_v2_enabled() -> bool:
    """settings['processing.call_category_v2_enabled']: `true` sau {"enabled": true}.
    Absent / altă valoare / eroare de citire => OFF (comportamentul vechi)."""
    try:
        db = SessionLocal()
        try:
            row = db.execute(text("SELECT value FROM settings WHERE key=:k"), {"k": _V2_KEY}).fetchone()
        finally:
            db.close()
    except Exception:
        logger.warning("citire %s esuata — call_category v2 OFF", _V2_KEY)
        return False
    if not row:
        return False
    v = row[0]
    if isinstance(v, dict):
        v = v.get("enabled")
    return v is True


def _parse_v2(res: Dict[str, Any]):
    """(parsed, salvaged) sau (None, motiv). `parsed` dict de la gateway = tratamentul de azi;
    altfel JSON-ul se extrage din raw_text și trebuie să aibă câmpurile cerute + o categorie din
    CATEGORIES — un fragment oarecare nu devine clasificare."""
    parsed = res.get("parsed") if res.get("ok") else None
    if isinstance(parsed, dict):
        return parsed, False
    raw = (res.get("error") or {}).get("raw_text") or res.get("text") or ""
    salvaged = salvage_json(raw)
    if not isinstance(salvaged, dict):
        return None, "no_json"
    if any(k not in salvaged for k in _V2_REQUIRED):
        return None, "missing_keys"
    if str(salvaged.get("categorie") or "").strip().lower() not in CATEGORIES:
        return None, "bad_category"
    return salvaged, True


def _log_unusable(res: Dict[str, Any], why: str) -> None:
    """Doar STRUCTURA răspunsului (fără conținut): cauzele rămase după activarea v2."""
    raw = (res.get("error") or {}).get("raw_text") or res.get("text") or ""
    t = raw.strip()
    logger.warning("call_category v2: raspuns neutilizabil motiv=%s ok=%s code=%s len=%d "
                   "first=%r last=%r fences=%s", why, bool(res.get("ok")),
                   (res.get("error") or {}).get("code"), len(raw), t[:1], t[-1:], "```" in raw)


def classify_call(transcript: str, no_cache: bool = False) -> Optional[Dict[str, Any]]:
    """Categorie + stil, intr-un singur apel IRIS, cu promptul dedicat apelurilor.
    Returneaza {category, tone, reason, model} sau None daca IRIS nu raspunde/nu e configurat.

    Politica (2026-07-02, mirror category_classifier._fallback_unknown_to_info): 'necunoscut'
    NU mai e o categorie finala livrabila la apeluri — orice apel trebuie sa fie DOAR
    informatie/sesizare/reclamatie. Daca AI-ul intoarce 'necunoscut' (sau un raspuns
    nerecunoscut), facem fallback sigur la 'informatie'; operatorii pot corecta manual din
    dropdown-ul de categorie daca e cazul.

    no_cache=True (2026-07-02, fix auto-learning apeluri): gateway-ul IRIS are un cache
    "curated" implicit care poate intoarce un raspuns invatat anterior pentru un transcript
    ~similar, IGNORAND promptul de sistem trimis acum — descoperit cand reclasificarea
    dupa regenerare de prompturi intorcea EXACT aceleasi categorii ("model":"curated" in
    raspuns, motivare care nu se potrivea cu transcriptul real). Pentru reclasificarea de
    verificare (ai_call_category.reclassify_divergent_calls) trebuie sa fortam un raspuns
    proaspat de la model, nu din cache — pentru clasificarea normala in masa lasam cache-ul
    activ (comportament neschimbat, default no_cache=False)."""
    if not iris_ai.is_configured() or not (transcript or "").strip():
        return None
    v2 = call_category_v2_enabled()
    system = build_call_system_prompt(json_only=v2)
    res = iris_ai.run_prompt(
        system, transcript, response_format="json", temperature=0.0, max_tokens=250,
        task="cargo360:call_category", no_cache=no_cache)
    salvaged = False
    if not v2:
        if not res.get("ok"):
            return None
        parsed = res.get("parsed")
        if not isinstance(parsed, dict):
            return None
    else:
        parsed, salvaged = _parse_v2(res)
        if parsed is None:
            _log_unusable(res, salvaged)
            return None
    cat = str(parsed.get("categorie") or "").strip().lower()
    if cat not in CATEGORIES:
        cat = "necunoscut"
    tone = str(parsed.get("stil") or "").strip().lower()
    if tone not in TONES:
        tone = None
    reason = parsed.get("motivare_scurta")
    unknown_fallback = (cat == "necunoscut")
    if unknown_fallback:
        cat = "informatie"
        base = "Fallback: încadrare neclară/necunoscută → tratat ca Informație."
        reason = (base + (" Motiv inițial: " + str(reason) if reason else ""))[:400]
    out = {"category": cat, "tone": tone, "reason": reason, "model": res.get("model")}
    if unknown_fallback:
        out["unknown_fallback"] = True
    if salvaged is True:
        out["salvaged"] = True          # JSON extras din raw_text (T3-L3), vizibil în ai_result
    return out


def _match_by_cdr_name(agent_extension: str, employees) -> Optional[Dict[str, Any]]:
    """Fuzzy-match pe tokeni ai numelui din CDR (user_fullname While1, ex. "Robert Iova") contra
    tokenilor fiecărui angajat (nume + local-part email). Overlap de tokeni (nu simpla prezenta
    intr-un text) — de-a lungul mai multor tokeni, nu doar unul, ca sa evite ambiguitatea vazuta
    la "Robert" (potrivit deopotriva cu Iova Oliviu-Robert si Kovacs Robert). Returneaza None daca
    nu exista un castigator clar."""
    name_toks = {t for t in assignee_classifier._WORD_SPLIT.split(agent_extension.lower())
                 if len(t) >= 3 and t not in assignee_classifier._STOPNAME}
    if not name_toks:
        return None
    scored = []
    for emp in employees:
        toks = assignee_classifier._name_tokens(emp)
        overlap = len(name_toks & toks)
        if overlap:
            scored.append((overlap, emp))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    top_overlap, top_emp = scored[0]
    ambiguous = len(scored) > 1 and scored[1][0] == top_overlap
    if ambiguous:
        return None
    return {"assignee_email": top_emp.get("email"), "assignee_name": top_emp.get("name"),
            "confidence": 0.95 if top_overlap >= 2 else 0.8,
            "reason": "Agent identificat din CDR While1 (user_fullname=\"%s\")." % agent_extension,
            "model": "cdr_user_fullname"}


def identify_agent(transcript: str, agent_extension: Optional[str] = None) -> Dict[str, Any]:
    """Identifică agentul CargoTrack. Sursă PRINCIPALĂ: `agent_extension` (user_fullname din
    CDR-ul While1) — mult mai fiabil decât ghicitul din transcript (vezi docstring modul).
    Fallback: prezentarea de la începutul apelului, prin fuzzy-match pe lista de angajați
    (aceeași sursă ca la assignee_classifier). Returnează
    {assignee_email, assignee_name, confidence, reason, model}."""
    employees = assignee_classifier._load_employees()
    if not employees:
        return {"assignee_email": None, "assignee_name": None, "confidence": None,
                "reason": "Lista de angajați indisponibilă.", "model": "none"}

    if agent_extension and agent_extension.strip():
        cdr_match = _match_by_cdr_name(agent_extension.strip(), employees)
        if cdr_match:
            return cdr_match

    opening = (transcript or "")[:AGENT_OPENING_CHARS].lower()
    if not opening.strip():
        return {"assignee_email": None, "assignee_name": None, "confidence": None,
                "reason": "Transcript gol si CDR fara nume de agent potrivit.", "model": "none"}

    scored = []
    for emp in employees:
        toks = assignee_classifier._name_tokens(emp)
        hits = sum(1 for t in toks if t in opening)
        if hits:
            scored.append((hits, emp))
    if not scored:
        return {"assignee_email": None, "assignee_name": None, "confidence": None,
                "reason": "Niciun nume de angajat identificat în deschiderea apelului.", "model": "none"}

    scored.sort(key=lambda x: -x[0])
    top_hits, top_emp = scored[0]
    ambiguous = len(scored) > 1 and scored[1][0] == top_hits
    if ambiguous:
        return {"assignee_email": None, "assignee_name": None, "confidence": None,
                "reason": "Mai mulți angajați potriviți egal în deschiderea apelului — necesită revizuire.",
                "model": "transcript_name", "candidates": [e["name"] for _, e in scored[:5]]}
    return {"assignee_email": top_emp.get("email"), "assignee_name": top_emp.get("name"),
            "confidence": min(0.6 + 0.1 * top_hits, 0.95),
            "reason": "Nume identificat în deschiderea apelului: %s." % top_emp.get("name"),
            "model": "transcript_name"}


_AI_CLASSIFY_KEY = "processing.call_ai_classification"
_DIARIZE_KEY = "processing.call_diarize_enabled"


def call_ai_classification_status() -> bool:
    """Starea switch-ului de clasificare AI (categorie) pentru apeluri. Absent/eroare => ON
    (fail-open), mirror process_email.ai_classification_status()."""
    try:
        db = SessionLocal()
        row = db.execute(text("SELECT value FROM settings WHERE key=:k"),
                         {"k": _AI_CLASSIFY_KEY}).fetchone()
        db.close()
        if not row or row[0] is None:
            return True
        v = row[0]
        return bool(v.get("enabled", True)) if isinstance(v, dict) else True
    except Exception:
        logger.warning("call_ai_classification_status failed — fail-open ON")
        return True


def set_call_ai_classification(enabled: bool, by: Optional[str] = None) -> bool:
    """START/STOP clasificare AI categorie pentru apeluri (settings, runtime, fara restart)."""
    db = SessionLocal()
    try:
        db.execute(text("""
            INSERT INTO settings(key, value, updated_by, updated_at)
            VALUES (:k, CAST(:v AS jsonb), :by, now())
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_by=EXCLUDED.updated_by,
              updated_at=now()
        """), {"k": _AI_CLASSIFY_KEY, "v": json.dumps({"enabled": bool(enabled)}), "by": by})
        db.commit()
    finally:
        db.close()
    return bool(enabled)


def call_diarize_status() -> bool:
    """Starea switch-ului de diarizare automată (segmentare AGENT/CLIENT). Absent/eroare => ON
    (fail-open, comportament actual păstrat)."""
    try:
        db = SessionLocal()
        row = db.execute(text("SELECT value FROM settings WHERE key=:k"),
                         {"k": _DIARIZE_KEY}).fetchone()
        db.close()
        if not row or row[0] is None:
            return True
        v = row[0]
        return bool(v.get("enabled", True)) if isinstance(v, dict) else True
    except Exception:
        logger.warning("call_diarize_status failed — fail-open ON")
        return True


def set_call_diarize(enabled: bool, by: Optional[str] = None) -> bool:
    """START/STOP diarizare automată AGENT/CLIENT pentru apeluri (runtime, fara restart)."""
    db = SessionLocal()
    try:
        db.execute(text("""
            INSERT INTO settings(key, value, updated_by, updated_at)
            VALUES (:k, CAST(:v AS jsonb), :by, now())
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_by=EXCLUDED.updated_by,
              updated_at=now()
        """), {"k": _DIARIZE_KEY, "v": json.dumps({"enabled": bool(enabled)}), "by": by})
        db.commit()
    finally:
        db.close()
    return bool(enabled)


def process_pending_batch(limit: int = 50) -> dict:
    """Rulează clasificarea (categorie+stil + agent + client) pentru apeluri transcrise dar
    neclasificate încă (transcript_status='success' AND ai_category IS NULL)."""
    if not call_ai_classification_status():
        return {"ok": True, "classified": 0, "errors": 0, "skipped": "ai_classification_disabled"}
    db = SessionLocal()
    done, errors = 0, 0
    try:
        rows = db.execute(text(
            "SELECT id, transcript, caller_number, callee_number, direction, agent_extension "
            "FROM calls WHERE transcript_status='success' AND ai_category IS NULL "
            "ORDER BY started_at DESC LIMIT :lim"), {"lim": limit}).fetchall()
        for row in rows:
            call_pk, transcript, caller_number, callee_number, direction, agent_extension = (
                row[0], row[1], row[2], row[3], row[4], row[5])
            try:
                cat_res = classify_call(transcript) or {}
                agent_res = identify_agent(transcript, agent_extension)
                # Numarul CLIENTULUI depinde de directie: la outbound NOI l-am sunat, deci
                # caller_number e numarul PROPRIU (nu al clientului) — clientul e la callee_number.
                # La inbound e invers. Bug gasit 2026-07-02 (apel #327, TOGIAL SRL nematchuit).
                client_number = callee_number if direction == "outbound" else caller_number
                client_id = phone_match.match_client_by_phone(client_number)
                db.execute(text("""
                    UPDATE calls SET
                        ai_category=:cat, ai_tone=:tone, ai_result=CAST(:catres AS jsonb),
                        ai_assignee=:aemail, ai_assignee_result=CAST(:ares AS jsonb),
                        client_id=COALESCE(:cid, client_id),
                        queue_status='categorized', updated_at=now()
                    WHERE id=:id
                """), {
                    "cat": cat_res.get("category"),
                    "tone": cat_res.get("tone"),
                    "catres": _to_json(cat_res),
                    "aemail": agent_res.get("assignee_email"),
                    "ares": _to_json(agent_res),
                    "cid": client_id,
                    "id": call_pk,
                })
                db.commit()
                done += 1
            except Exception as e:
                db.rollback()
                logger.warning("call classify fail id=%s: %s", call_pk, str(e)[:200])
                errors += 1
    finally:
        db.close()
    return {"ok": True, "classified": done, "errors": errors}


_DIARIZE_HEAD = (
    "Esti un asistent care imparte transcriptul unui apel telefonic CargoTrack pe ture de "
    "conversatie. Transcriptul e text continuu, FARA etichete de vorbitor (provine dintr-o "
    "transcriere Whisper simpla, nu diarizata) — trebuie sa deduci TU cine vorbeste in fiecare "
    "fragment, din context (agentul reprezinta compania — se prezinta, explica motivul apelului, "
    "pune intrebari tehnice/administrative; clientul raspunde despre situatia lui, descrie "
    "probleme, da numere de inmatriculare/date personale).\n"
    "ATENTIE la cine deschide apelul (contează pentru a nu inversa rolurile):\n"
    "- Apel IESIRE (agentul suna clientul): CLIENTUL raspunde primul, de regula cu \"Alo?\". "
    "Apoi AGENTUL se prezinta si explica de ce suna (numele agentului poate aparea aici, "
    "eventual insotit de numele clientului, pe care agentul il rosteste ca formula de adresare).\n"
    "- Apel INTRARE (clientul suna la CargoTrack): AGENTUL raspunde primul (preluare apel), "
    "clientul explica de ce a sunat.\n"
    "O singura propozitie din transcript poate contine de fapt DOUA replici diferite lipite "
    "fara semn de punctuatie clar (ex. \"Alo? Buna ziua X, sunt de la Y.\" = raspunsul cuiva "
    "urmat imediat de deschiderea celuilalt) — desparte-le pe ture separate cand sesizezi asta, "
    "nu le pune pe amandoua in aceeasi tura.\n"
)


def diarize_transcript(transcript: str, agent_name: Optional[str] = None,
                       direction: Optional[str] = None) -> Optional[list]:
    """Imparte transcriptul brut (fara etichete) in ture {speaker: 'agent'|'client', text}, pt
    afisare tip conversatie (OPERATOR stanga / CLIENT dreapta) in UI. Best-effort — la esec sau
    IRIS neconfigurat, returneaza None (UI cade pe afisarea plata, vezi CallDetail)."""
    if not iris_ai.is_configured() or not (transcript or "").strip():
        return None
    agent_hint = ('Numele agentului (din CDR While1): "%s".\n' % agent_name) if agent_name else ""
    dir_hint = ""
    if direction == "outbound":
        dir_hint = "Acest apel e IESIRE: agentul l-a sunat pe client.\n"
    elif direction == "inbound":
        dir_hint = "Acest apel e INTRARE: clientul l-a sunat pe agent.\n"
    system = (
        _DIARIZE_HEAD + agent_hint + dir_hint +
        'Raspunde STRICT in format JSON, fara text in plus, exact in forma:\n'
        '{"turns": [{"speaker": "agent", "text": "..."}, {"speaker": "client", "text": "..."}, ...]}\n'
        "Pastreaza textul ORIGINAL (nu parafraza, nu traduce, nu rezuma) — doar il imparti si "
        "etichetezi pe ture, in ordinea in care apare in transcript."
    )
    res = iris_ai.run_prompt(
        system, transcript, response_format="json", temperature=0.0, max_tokens=2000,
        task="cargo360:call_diarize")
    if not res.get("ok"):
        return None
    parsed = res.get("parsed")
    if not isinstance(parsed, dict):
        return None
    turns = parsed.get("turns")
    if not isinstance(turns, list) or not turns:
        return None
    out = []
    for t in turns:
        if not isinstance(t, dict):
            continue
        speaker = str(t.get("speaker") or "").strip().lower()
        if speaker not in ("agent", "client"):
            continue
        txt = str(t.get("text") or "").strip()
        if not txt:
            continue
        out.append({"speaker": speaker, "text": txt})
    return out or None


def process_diarize_batch(limit: int = 50) -> dict:
    """Segmentare pe ture AGENT/CLIENT pentru apeluri transcrise dar nediarizate încă
    (transcript_status='success' AND transcript_turns IS NULL). Independent de clasificarea pe
    categorie — rulează și pe apeluri deja clasificate, ca să prindă backfill-ul din urmă."""
    if not call_diarize_status():
        return {"ok": True, "diarized": 0, "errors": 0, "skipped": "diarize_disabled"}
    db = SessionLocal()
    done, errors = 0, 0
    try:
        rows = db.execute(text(
            "SELECT id, transcript, agent_extension, ai_assignee_result, direction FROM calls "
            "WHERE transcript_status='success' AND transcript_turns IS NULL "
            "ORDER BY started_at DESC LIMIT :lim"), {"lim": limit}).fetchall()
        for row in rows:
            call_pk, transcript, agent_extension, assignee_result, direction = (
                row[0], row[1], row[2], row[3], row[4])
            try:
                agent_name = agent_extension
                if not agent_name and isinstance(assignee_result, dict):
                    agent_name = assignee_result.get("assignee_name")
                turns = diarize_transcript(transcript, agent_name=agent_name, direction=direction)
                db.execute(text(
                    "UPDATE calls SET transcript_turns=CAST(:t AS jsonb), updated_at=now() "
                    "WHERE id=:id"),
                    {"t": json.dumps(turns) if turns else None, "id": call_pk})
                db.commit()
                done += 1
            except Exception as e:
                db.rollback()
                logger.warning("call diarize fail id=%s: %s", call_pk, str(e)[:200])
                errors += 1
    finally:
        db.close()
    return {"ok": True, "diarized": done, "errors": errors}


def _to_json(d: dict) -> str:
    return json.dumps(d or {})
