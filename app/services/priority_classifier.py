"""Incadrare email pe PRIORITATE — consumer al canalului IRIS AI.

Schema unificata pe 4 niveluri (precedenta P2 > P3 > P4 > P5; cel mai mic numar castiga):
  P2 = PLATI (OP, dovada/confirmare plata, incarcare cont, plata blocata) — cel mai prioritar.
  P3 = SESIZARE / RECLAMATIE (inclusiv urgenta/furie fara legatura cu plata).
  P4 = DOCUMENTE de procesat (sofer/vehicul/contract: buletin, permis, talon, CIV, contract...).
  P5 = GENERAL / restul (pot astepta mai mult).

Stocat in emails.ai_priority = "2"|"3"|"4"|"5". Decizia:
  0) Reguli FORTATE (priority_rules.match_forced) — bat orice alt semnal, inclusiv seria OP
     extrasa de vision AI. Azi: "Email Scout Report" de la office@cargotrack.ro -> P4.
  1) Reguli deterministe (priority_rules.match): plata -> P2; urgenta/furie -> P3 (confidence 1.0).
  2) Altfel AI: clasificare directa pe tier (NU scor de urgenta), folosind subiect + corp + numele
     atasamentelor + categoria deja stabilita (sesizare/reclamatie -> semnal puternic P3).
  3) Orice esec / continut insuficient / AI neconfigurat -> P5 (fallback general). Platile raman
     prinse de regulile deterministe, independent de AI.

Output (in emails.ai_priority_result jsonb; emails.ai_priority = '2'|'3'|'4'|'5'):
  { "priority": "2|3|4|5", "reason": "<o propozitie RO>",
    "model": "rule|gemma|claude-*|fallback", "rule_id"?: "..." }
"""
import os
import logging
from typing import Dict, Any, Optional

from sqlalchemy import text
from app.database import SessionLocal
from app.services import iris_ai
from app.services import priority_rules
# Reutilizam (NU copiem) helperele de corp/atasamente din clasificatorul de categorie.
from app.services import category_classifier as C

logger = logging.getLogger("mailguard.priority")

# Etichete CANONICE (externe, stocate in DB + emise pe API): "2".."5".
PRIORITY_LABELS = {
    "2": "P2 (Plăți)",
    "3": "P3 (Sesizare/Reclamație)",
    "4": "P4 (Documente)",
    "5": "P5 (General)",
}
PRIORITIES = ["2", "3", "4", "5"]
FALLBACK = "5"

# --- intern: modelul AI rationeaza in P2..P5; maparea la "2".."5" se face la iesire via _NUM ---
_AI_SET = {"P2", "P3", "P4", "P5"}
_AI_FALLBACK = "P5"
_NUM = {"P2": "2", "P3": "3", "P4": "4", "P5": "5"}

# Cheia in tabela KV `settings` pentru promptul editabil (mirror 'documents.classify_prompt').
PROMPT_KEY = "priority.classify_prompt"

DEFAULT_PROMPT = (
    "Esti un clasificator de PRIORITATE pentru emailurile unei firme de transport (Cargo Track). "
    "Incadrezi fiecare email intr-UNUL din patru niveluri, P2..P5, dupa CONTINUT si dupa tipul a "
    "ceea ce trimite clientul. Precedenta este P2 > P3 > P4 > P5: daca un email s-ar potrivi la mai "
    "multe niveluri, alegi pe cel mai prioritar (numarul cel mai mic).\n\n"
    "P2 — PLATI (cel mai prioritar; prevenim suspendarea conturilor):\n"
    "  Orice tine de bani / plati: ordin de plata (OP), dovada sau confirmare de plata, 'am platit / "
    "am achitat', incarcare / alimentare cont, o plata care nu se reflecta / e blocata / respinsa, "
    "SWIFT, un extras care dovedeste o plata. Daca clientul ATASEAZA un ordin de plata sau o dovada "
    "de plata -> P2.\n\n"
    "P3 — SESIZARE / RECLAMATIE:\n"
    "  Clientul semnaleaza o problema (tehnica / functionala / administrativa) sau este nemultumit de "
    "modul in care a fost tratat. Include urgenta, furie, repetare ('a treia oara', 'v-am scris de "
    "mai multe ori'), amenintari cu plangere / instanta — CHIAR DACA nu e despre o plata. Daca "
    "emailul a fost deja incadrat ca 'sesizare' sau 'reclamatie' (vezi campul 'Categorie' din input), "
    "este P3 — cu exceptia cazului in care e de fapt P2 prin plata.\n\n"
    "P4 — DOCUMENTE de procesat (sofer / vehicul / contract):\n"
    "  Clientul trimite documente de identificare sau contractuale care trebuie procesate: buletin / "
    "carte de identitate, permis de conducere, talon / certificat de inmatriculare, carte de "
    "identitate a vehiculului (CIV), CEMT, COC, contracte, anexe. Judeci dupa NUMELE atasamentelor si "
    "dupa text. ATENTIE: un document de PLATA (OP / dovada de plata) este P2, NU P4; aici intra doar "
    "documentele de sofer / vehicul / contract.\n\n"
    "P5 — GENERAL / restul:\n"
    "  Orice nu se incadreaza mai sus: intrebari generale, informari, cereri administrative de rutina, "
    "confirmari, mesaje fara obiect clar, forward fara context. Pot astepta mai mult.\n\n"
    "Reguli de decizie:\n"
    "- Mergi pe precedenta: intai verifici P2 (plata), apoi P3 (sesizare / reclamatie), apoi P4 "
    "(documente sofer / vehicul / contract), altfel P5.\n"
    "- NU presupune o plata doar pentru ca exista un atasament: judeci dupa TEXT si dupa NUMELE "
    "fisierului. O factura / proforma / reminder de scadenta NU este o dovada de plata.\n"
    "- O simpla informare sau intrebare, fara nemultumire si fara documente -> P5.\n\n"
    "Exemple:\n"
    "- 'Am atasat OP-ul, va rog alimentati contul.' -> P2\n"
    "- 'Plata facuta acum 3 zile tot nu se vede in cont.' -> P2\n"
    "- 'Aplicatia nu functioneaza de 3 zile, e a treia oara cand scriu.' -> P3\n"
    "- 'Atasez buletinul si permisul soferului nou.' -> P4\n"
    "- 'Va trimit talonul si cartea de identitate a camionului.' -> P4\n"
    "- 'Buna ziua, imi puteti spune care e programul de lucru?' -> P5"
)


def _tail() -> str:
    return (
        "\n\nReturneaza DOAR un JSON valid, fara text in plus, fara ```, exact in forma:\n"
        '{"priority":"P2|P3|P4|P5","reason":"<o singura propozitie scurta in romana>"}\n'
        "priority = nivelul ales conform regulilor de mai sus (P2 cel mai prioritar, P5 cel mai putin)."
    )


def load_prompt() -> str:
    """Promptul editabil (settings['priority.classify_prompt']) peste default-ul din cod."""
    try:
        db = SessionLocal()
        row = db.execute(text("SELECT value FROM settings WHERE key=:k"), {"k": PROMPT_KEY}).fetchone()
        db.close()
        if row and row[0]:
            val = row[0]
            if isinstance(val, str) and val.strip():
                return val
    except Exception as e:
        logger.warning("load_prompt (priority) DB failed, using default: %s", e)
    return DEFAULT_PROMPT


def build_system_prompt(prompt: Optional[str] = None) -> str:
    p = prompt if (prompt and prompt.strip()) else load_prompt()
    return p + _tail()


def _attachment_names(email: Dict[str, Any], attachments=None) -> str:
    """Numele atasamentelor (semnal pentru P2 plata si P4 documente). Best-effort."""
    names = []
    try:
        if attachments is not None:
            for a in attachments:
                n = (a.get("name") if isinstance(a, dict) else None)
                if n:
                    names.append(str(n))
        elif email.get("id"):
            db = SessionLocal()
            rows = db.execute(text("SELECT name FROM attachments WHERE email_id=:id"),
                              {"id": email.get("id")}).fetchall()
            db.close()
            names = [r._mapping["name"] for r in rows if r._mapping["name"]]
    except Exception as e:
        logger.warning("attachment_names (priority) failed: %s", e)
    return ", ".join(names[:10])


def _content(email: Dict[str, Any], att_count: int, att_names: str,
             category: Optional[str] = None) -> str:
    """Construieste continutul pentru incadrarea pe PRIORITATE.

    Pentru prioritate, SUBIECTUL e semnal important (spre deosebire de departament). Includem
    subiectul, expeditorul, mesajul nou (ultimul reply), atasamentele si categoria deja stabilita
    (sesizare/reclamatie = semnal puternic pentru P3).
    """
    subject = email.get("subject") or ""
    frm = ((email.get("from_name") or "") + " <" + (email.get("from_address") or "") + ">").strip()
    body = C._email_body(email)
    lines = ["Subiect: " + subject,
             "De la: " + frm,
             "Mesaj:\n" + body]
    if att_count:
        lines.append("Atasamente: " + str(att_count))
    if att_names:
        lines.append("Nume atasamente: " + att_names)
    if category:
        lines.append("Categorie (deja stabilita de sistem): " + str(category))
    return "\n".join(lines).strip()


def _normalize(parsed: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(parsed, dict):
        return None
    pri = str(parsed.get("priority") or "").strip().upper()
    if pri in ("2", "3", "4", "5"):
        pri = "P" + pri
    if pri not in _AI_SET:
        pri = None
    reason = parsed.get("reason")
    reason = str(reason).strip()[:400] if reason else None
    if pri is None:
        pri = _AI_FALLBACK
    return {"priority": pri, "reason": reason}


def _fallback(reason: str) -> Dict[str, Any]:
    # intern P5; maparea la "5" se face la iesirea din classify_priority
    return {"priority": _AI_FALLBACK, "reason": reason, "model": "fallback"}


def _classify_priority_core(email: Dict[str, Any],
                            prompts: Optional[str] = None,
                            attachments=None,
                            category: Optional[str] = None) -> Dict[str, Any]:
    """Rezultat brut (intern P2..P5).

    0) Reguli fortate (Email Scout Report -> P4), inaintea oricarui alt semnal.
    1) Reguli deterministe (plata -> P2; urgenta/furie -> P3) — confidence 1.0.
    2) Altfel AI -> tier {P2|P3|P4|P5} (clasificare, nu scor).
    3) Orice esec / continut insuficient / AI neconfigurat -> P5 (fallback general).
    """
    att_names = _attachment_names(email, attachments)

    # 0.0) Reguli FORTATE — inaintea oricarui alt semnal, inclusiv seria OP de la vision AI.
    # Un raport intern ("Email Scout Report") citeaza mailuri de client, deci poate contine o
    # serie OP care l-ar urca pe P2 desi nu e o plata a nimanui.
    try:
        forced = priority_rules.match_forced(email)
    except Exception as e:
        logger.warning("priority forced rules failed: %s", e)
        forced = None
    if forced:
        return {"priority": forced.get("tier", "P4"),
                "reason": forced.get("note") or "Regula determinista fortata de prioritate.",
                "model": "rule", "rule_id": forced.get("id")}

    # 0.5) Daca vision AI a extras deja o serie OP (ai_op_series), e clar un ordin de plata -> P2.
    # ai_op_series e populat de op_extractor.py doar cand recunoaste o serie valida pe document;
    # nu e un simplu flag de atasament, deci false-positive-urile sunt neglijabile.
    if email.get("ai_op_series"):
        return {"priority": "P2",
                "reason": f"Serie OP detectata de vision AI ({email['ai_op_series']}) -> P2 (plata).",
                "model": "rule", "rule_id": "pay_op_series"}

    # 1) Reguli deterministe (plata = P2, urgenta = P3; vezi priority_rules)
    try:
        hit = priority_rules.match(email, att_names)
    except Exception as e:
        logger.warning("priority rules match failed: %s", e)
        hit = None
    if hit:
        tier = hit.get("tier") if hit.get("tier") in _AI_SET else "P2"
        return {"priority": tier,
                "reason": hit.get("note") or "Regula determinista de prioritate.",
                "model": "rule", "rule_id": hit.get("id")}

    # 2) AI
    if not iris_ai.is_configured():
        return _fallback("AI indisponibil — prioritate generala (P5).")

    att_n = C._attachment_count(email, attachments)
    content = _content(email, att_n, att_names, category=category)
    if not content.strip() or len(content.strip()) < 3:
        return _fallback("Continut insuficient — prioritate generala (P5).")
    system = build_system_prompt(prompts)

    # learn_scope 'v3' = cache nou pentru schema P2..P5 (vechiul 'v2' ecoa raspunsuri P0/P1).
    res = iris_ai.run_prompt(
        system, content,
        response_format="json", temperature=0.0, max_tokens=200,
        task="cargo360:email_priority_v3", email_id=email.get("id"),
        use_cache=True, learn=True, learn_scope="cargo360:email_priority_v3",
    )
    if not res.get("ok"):
        logger.warning("priority classify failed: %s", res.get("error"))
        return _fallback("Eroare AI — prioritate generala (P5).")
    norm = _normalize(res.get("parsed"))
    if not norm:
        return _fallback("Raspuns AI invalid — prioritate generala (P5).")
    norm["model"] = res.get("model")
    return norm


def classify_priority(email: Dict[str, Any],
                      prompts: Optional[str] = None,
                      attachments=None,
                      category: Optional[str] = None) -> Dict[str, Any]:
    """Incadrare prioritate pe 4 niveluri (P2..P5). Reguli deterministe (plata->P2, urgenta->P3)
    au prioritate; altfel AI clasifica direct pe tier. Iesirea e mapata canonic la "2".."5".
    `category` = categoria deja stabilita (ai_category), folosita ca semnal pentru P3.
    """
    res = _classify_priority_core(email, prompts=prompts, attachments=attachments, category=category)
    # mapare canonica la iesire: P2->"2", P3->"3", P4->"4", P5->"5"
    res["priority"] = _NUM.get(res.get("priority"), res.get("priority"))
    return res
