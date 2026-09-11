"""Reguli deterministe de incadrare pe DEPARTAMENT (expeditor / subiect / continut).

Departamentele sunt rule-first: majoritatea emailurilor se incadreaza prin potriviri
exacte pe expeditor/domeniu/subiect/continut, iar AI-ul decide doar restul fuzzy (vezi
department_classifier). Regulile sunt EDITABILE din Setari (ca sender_lists) — store
canonic in tabela `settings`, cheia 'department_rules'.

Store: settings['department_rules'] = { "rules": [ <rule>, ... ], "seeded": true }
  rule = { id, department, from, subject, body, enabled, note, by, at }
    - from    : substring (case+diacritice-insensitive) cautat in from_address + from_name
    - subject : substring cautat in subiect
    - body    : substring cautat in corpul INTEGRAL (body_text + body_html, inclusiv istoricul
                citat). Folosit pentru a prinde un interlocutor intern oriunde in fir, chiar cand
                expeditorul curent e clientul — ex. semnatura lui Cosmin (cosmin.bogdan@cargotrack.ro)
                intr-un reply al clientului -> Mobilitate.
    - o regula loveste daca TOATE conditiile NEVIDE se potrivesc (AND). Cel putin una
      dintre `from`/`subject`/`body` trebuie sa fie nevida.

`match(email)` evalueaza regulile activate, INTAI cele mai specifice (mai multe criterii), apoi
cele cu mai putine; intre regulile cu acelasi numar de criterii, cele pe `body`-only sunt
evaluate ULTIMELE (semnal incidental — o mentiune in fir e mai slaba decat expeditorul/subiectul
real). Prima potrivire castiga. Asta rezolva cazul in care acelasi expeditor (ex.
support@locatorbg.com) merge pe departamente diferite in functie de subiect (Taxe de drum vs
Contabilitate), si tine o regula de tip body-only (Cosmin in fir) sub regulile pe expeditor real
(ex. un fir de facturare de la mis.batch unde Cosmin a fost CC -> ramane Contabilitate).
"""
import json
import uuid
import unicodedata
from datetime import datetime, timezone

from sqlalchemy import text

_KEY = "department_rules"

# Set canonic de departamente valide (duplicat intentionat aici ca sa evitam importul circular
# cu department_classifier, care importa acest modul). Tine-l sincron cu DEPARTMENTS de acolo.
VALID_DEPARTMENTS = (
    "suport_1", "suport_2", "suport_3", "taxe_drum",
    "contabilitate", "mobilitate", "recuperare_tva", "comercial",
)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _fold(s: str) -> str:
    """lower + elimina diacriticele (ca 'Tranzactii zilnice' sa prinda si 'Tranzacții zilnice')."""
    s = (s or "")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


# Reguli implicite — seedate o singura data (la prima citire daca store-ul lipseste). Ordinea
# logica grupeaza pe departament; specificitatea (from+subject) e impusa la match, nu de ordine.
DEFAULT_RULES = [
    # Notificarile automate ale aplicatiilor noastre pleaca de pe noreply@cargotrack.ro si merg
    # MEREU la Suport 1 (cerere business 2026-09-11), indiferent de subiect sau continut. Regula
    # e prima in store ca sa castige si egalitatile de specificitate cu alte reguli pe un singur
    # criteriu (ex. subiect "Tranzactii zilnice" -> Contabilitate).
    {"id": "noreply-cargotrack-01", "department": "suport_1", "from": "noreply@cargotrack.ro",
     "subject": "", "note": "noreply@cargotrack.ro -> Suport 1"},
    # --- Taxe de drum ---
    {"department": "taxe_drum", "from": "support@locatorbg.com", "subject": "Request for refund -",
     "note": "locatorbg + refund -> taxe"},
    {"department": "taxe_drum", "from": "no-reply@idata.hu", "subject": "Presumed unauthorized road use",
     "note": "idata.hu utilizare neautorizata -> taxe"},
    {"department": "taxe_drum", "from": "alert@ct.its-pro.hu", "subject": "", "note": "HU-GO toll"},
    {"department": "taxe_drum", "from": "noreply@nemzetiutdij.hu", "subject": "", "note": "HU-GO toll"},
    {"department": "taxe_drum", "from": "noreply@hu-go.hu", "subject": "", "note": "HU-GO toll"},
    # --- Recuperare TVA (tipologie „dosar rambursare TVA extern") ---
    # 3 reguli OR (subiect SAU corp) — prinde tipologia chiar cand subiectul difera de standard.
    # Semnale date de business (fara diacritice, _fold le normalizeaza oricum).
    {"department": "recuperare_tva", "from": "", "subject": "rambursare tva extern",
     "note": "Subiect rambursare TVA extern -> recuperare_tva"},
    {"department": "recuperare_tva", "from": "", "subject": "", "body": "dosarul de recuperare tva",
     "note": "Corp: dosarul de recuperare TVA -> recuperare_tva"},
    {"department": "recuperare_tva", "from": "", "subject": "", "body": "situatia dosarului dumneavoastra pentru recuperare tva",
     "note": "Corp: situatia dosarului pentru recuperare TVA -> recuperare_tva"},
    # --- Contabilitate ---
    {"department": "contabilitate", "from": "support@locatorbg.com", "subject": "Your Purchase Receipt from DigiToll",
     "note": "locatorbg + DigiToll receipt -> contabilitate"},
    {"department": "contabilitate", "from": "urbansiasociatii.ro", "subject": "", "note": "URBAN & ASOCIATII"},
    {"department": "contabilitate", "from": "mis.batch@btrl.ro", "subject": "", "note": "extrase BTRL"},
    # Procesatori de plati online -> Contabilitate (cerere business 2026-09-11).
    # euPlatesc: potrivire pe NUMELE DE DOMENIU, nu pe adresa exacta — notificarile lor pleaca de
    # pe mai multe cutii (noreply@, suport@) si de pe euplatesc.ro / euplatesc.com.
    {"id": "euplatesc-01", "department": "contabilitate", "from": "euplatesc",
     "subject": "", "note": "euPlatesc (orice adresa) -> Contabilitate"},
    # europayment.services: regula pe domeniu acopera cele 4 cutii cerute (notificari@, contact@,
    # noreply@, suport@) si orice alta cutie a aceluiasi expeditor.
    {"id": "europayment-services-01", "department": "contabilitate", "from": "@europayment.services",
     "subject": "", "note": "europayment.services (orice adresa) -> Contabilitate"},
    {"id": "moovleasing-01", "department": "contabilitate", "from": "office@moovleasing.ro",
     "subject": "", "note": "office@moovleasing.ro -> Contabilitate"},
    {"department": "contabilitate", "from": "", "subject": "Tranzactii zilnice", "note": "tranzactii zilnice CARGOTRACK"},
    # Notificarile ONRC (registrul comertului) merg mereu la Contabilitate, indiferent de subiect.
    {"id": "onrc-notificari-01", "department": "contabilitate", "from": "onrc_notificari@onrc.ro",
     "subject": "", "note": "Notificari ONRC -> Contabilitate"},
    # --- Suport 1 ---
    # Raport intern generat de aplicatie, trimis de pe office@cargotrack.ro. NU e cerere de
    # client: nu are serie de factura, dar contine citate din mailuri (inclusiv OP-uri), deci
    # AI-ul il ducea pe Contabilitate. Regula pe from+subiect il tine FIX pe Suport 1.
    {"id": "scout-report-01", "department": "suport_1", "from": "office@cargotrack.ro",
     "subject": "Email Scout Report", "note": "Email Scout Report (office@) -> Suport 1"},
    # --- Suport 3 (Zoli Tyepak) ---
    {"department": "suport_3", "from": "zoli", "subject": "", "note": "Zoli Tyepak (nume)"},
    {"department": "suport_3", "from": "tyepak", "subject": "", "note": "Zoli Tyepak (nume)"},
    # --- Suport 2 ---
    {"id": "orange-otc-01", "department": "suport_2", "from": "noreply.otc@orange.com", "subject": "",
     "note": "Cod autentificare Orange (OTC) -> Suport 2"},
    # --- Mobilitate ---
    {"department": "mobilitate", "from": "cosmin", "subject": "", "note": "Cosmin pe email"},
    {"department": "mobilitate", "from": "guretruck", "subject": "", "note": "client guretruck"},
    {"department": "mobilitate", "from": "transportinnood", "subject": "", "note": "client transportinnood"},
    # Cosmin (Reprezentare Europeana) implicat in fir, chiar cand expeditorul curent e clientul:
    # semnatura/adresa lui apare in istoricul citat -> Mobilitate. Regula pe CONTINUT (body-only).
    {"department": "mobilitate", "from": "", "subject": "", "body": "cosmin.bogdan@cargotrack.ro",
     "note": "Cosmin Bogdan in fir -> Mobilitate"},
]


def _normalize_rule(r: dict, by=None) -> dict:
    """Curata + completeaza o regula venita din UI/seed."""
    dep = (r.get("department") or "").strip().lower()
    frm = (r.get("from") or "").strip()
    subj = (r.get("subject") or "").strip()
    bdy = (r.get("body") or "").strip()
    return {
        "id": r.get("id") or uuid.uuid4().hex[:8],
        "department": dep,
        "from": frm,
        "subject": subj,
        "body": bdy,
        "enabled": bool(r.get("enabled", True)),
        "note": (r.get("note") or "").strip() or None,
        "by": r.get("by") or by,
        "at": r.get("at") or _now_iso(),
    }


def _validate_rule(r: dict):
    if r["department"] not in VALID_DEPARTMENTS:
        return "Departament invalid: " + str(r["department"])
    if not r["from"] and not r["subject"] and not r.get("body"):
        return "Regula trebuie sa aiba cel putin un criteriu (from, subject sau body)"
    return None


def _load(db) -> dict:
    row = db.execute(text("SELECT value FROM settings WHERE key=:k"), {"k": _KEY}).fetchone()
    store = (row[0] if row and row[0] else None)
    if not store:
        # prima initializare: seed default-urile
        store = {"rules": [_normalize_rule(r, by="seed") for r in DEFAULT_RULES], "seeded": True}
        _save(db, store, "seed")
        db.commit()
        return store
    if not isinstance(store.get("rules"), list):
        store["rules"] = []
    return store


def _save(db, store, by):
    db.execute(text(
        "INSERT INTO settings(key, value, description, updated_by, updated_at) "
        "VALUES(:k, CAST(:v AS jsonb), 'Reguli deterministe de incadrare pe departament', :by, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_by=:by, updated_at=NOW()"),
        {"k": _KEY, "v": json.dumps(store), "by": by})


# ── API folosit de endpoint-urile din settings.py ──

def list_all(db) -> list:
    return _load(db).get("rules", [])


def add_rule(db, rule: dict, by, commit=True):
    r = _normalize_rule(rule, by=by)
    err = _validate_rule(r)
    if err:
        return {"error": err}
    store = _load(db)
    store["rules"].append(r)
    _save(db, store, by)
    if commit:
        db.commit()
    return {"ok": True, "rule": r}


def update_rule(db, rule_id: str, patch: dict, by, commit=True):
    store = _load(db)
    found = None
    for i, r in enumerate(store["rules"]):
        if r.get("id") == rule_id:
            merged = dict(r)
            for k in ("department", "from", "subject", "body", "note", "enabled"):
                if k in patch:
                    merged[k] = patch[k]
            merged = _normalize_rule({**merged, "id": rule_id, "by": r.get("by"), "at": r.get("at")}, by=by)
            err = _validate_rule(merged)
            if err:
                return {"error": err}
            store["rules"][i] = merged
            found = merged
            break
    if not found:
        return {"error": "Regula inexistenta"}
    _save(db, store, by)
    if commit:
        db.commit()
    return {"ok": True, "rule": found}


def remove_rule(db, rule_id: str, by, commit=True):
    store = _load(db)
    n0 = len(store["rules"])
    store["rules"] = [r for r in store["rules"] if r.get("id") != rule_id]
    if len(store["rules"]) == n0:
        return {"error": "Regula inexistenta"}
    _save(db, store, by)
    if commit:
        db.commit()
    return {"ok": True, "removed": rule_id}


# ── Motorul de potrivire (folosit de department_classifier) ──

def _crit_count(r: dict) -> int:
    """Cate criterii NEVIDE are regula (from/subject/body) — mai multe = mai specifica."""
    return sum(1 for k in ("from", "subject", "body") if (r.get(k) or "").strip())


def _is_body_only(r: dict) -> bool:
    """True daca regula are DOAR criteriu pe continut (semnal incidental, evaluat ultim)."""
    return bool((r.get("body") or "").strip()) and not (r.get("from") or "").strip() \
        and not (r.get("subject") or "").strip()


def _rule_matches(r: dict, hay_from: str, hay_subject: str, hay_body: str) -> bool:
    if not r.get("enabled", True):
        return False
    frm = _fold(r.get("from") or "")
    subj = _fold(r.get("subject") or "")
    bdy = _fold(r.get("body") or "")
    if not frm and not subj and not bdy:
        return False
    if frm and frm not in hay_from:
        return False
    if subj and subj not in hay_subject:
        return False
    if bdy and bdy not in hay_body:
        return False
    return True


def match(email: dict, db=None):
    """Returneaza (department, rule) pentru prima regula care loveste, altfel None.
    Ordine: mai multe criterii INTAI (mai specific); la egalitate, regulile body-only ULTIMELE
    (mentiune incidentala in fir < expeditor/subiect real)."""
    own = False
    if db is None:
        from app.database import SessionLocal
        db = SessionLocal()
        own = True
    try:
        rules = list_all(db)
    finally:
        if own:
            db.close()
    hay_from = _fold((email.get("from_address") or "") + " " + (email.get("from_name") or ""))
    hay_subject = _fold(email.get("subject") or "")
    # corpul INTEGRAL (text + html), inclusiv istoricul citat — ca o semnatura din fir (Cosmin) sa
    # fie vazuta chiar cand expeditorul curent e clientul. Fold o singura data (poate fi mare).
    hay_body = _fold((email.get("body_text") or "") + " " + (email.get("body_html") or ""))
    ordered = sorted(
        [r for r in rules if r.get("enabled", True)],
        key=lambda r: (-_crit_count(r), 1 if _is_body_only(r) else 0),
    )
    for r in ordered:
        if _rule_matches(r, hay_from, hay_subject, hay_body):
            return r["department"], r
    return None
