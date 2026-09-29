"""Email AI categorization — stats, per-email re-run, and on-demand backfill.

New emails are categorized automatically in the processing pipeline
(process_email.process_one). These endpoints let an admin re-run after editing the
prompts, or backfill historical emails.
"""
import json
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.api.v1.auth import get_current_admin
from app.services import category_classifier
from app.services import iris_ai

logger = logging.getLogger("mailguard.ai_category")
router = APIRouter()


def _classify_and_store(db: Session, email_id: int, force_fresh: bool = False) -> dict:
    row = db.execute(text(
        "SELECT id, subject, from_address, from_name, body_text, body_html, conversation_id, received_at "
        "FROM emails WHERE id=:id"), {"id": email_id}).fetchone()
    if not row:
        raise HTTPException(404, "Email not found")
    res = category_classifier.classify_category(dict(row._mapping), force_fresh=force_fresh)
    if not res:
        db.execute(text("UPDATE emails SET ai_status='error', ai_processed_at=NOW() WHERE id=:id"),
                   {"id": email_id})
        db.commit()
        raise HTTPException(502, "Clasificare indisponibilă (IRIS AI neconfigurat sau eroare)")
    db.execute(text(
        "UPDATE emails SET ai_category=:c, ai_result=CAST(:r AS jsonb), "
        "ai_status='done', ai_processed_at=NOW() WHERE id=:id"),
        {"c": res["category"], "r": json.dumps(res), "id": email_id})
    db.commit()
    return res


_CATS = ["informatie", "sesizare", "reclamatie", "necunoscut"]


@router.post("/ai/category/{email_id}/correct")
def category_correct(email_id: int, body: dict, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Corectează manual categoria unui email. Salvează vechea + noua categorie."""
    new_cat = (body.get("category") or "").strip().lower()
    if new_cat not in _CATS:
        raise HTTPException(400, "Categorie invalidă (informatie/sesizare/reclamatie/necunoscut)")
    row = db.execute(text("SELECT ai_category, ai_result FROM emails WHERE id=:id"), {"id": email_id}).fetchone()
    if not row:
        raise HTTPException(404, "Email not found")
    old_cat = row._mapping["ai_category"]
    old_reason = None
    try:
        old_reason = (row._mapping["ai_result"] or {}).get("reason")
    except Exception:
        pass
    reviewer = admin.get("username") or admin.get("email") or "admin"
    db.execute(text(
        "INSERT INTO ai_category_corrections(email_id, old_category, new_category, old_reason, corrected_by) "
        "VALUES(:e, :o, :n, :r, :by)"),
        {"e": email_id, "o": old_cat, "n": new_cat, "r": old_reason, "by": reviewer})
    new_result = {"category": new_cat, "confidence": 1.0,
                  "reason": "Corectat manual de " + reviewer, "manual": True}
    db.execute(text(
        "UPDATE emails SET ai_category=:c, ai_result=CAST(:r AS jsonb), ai_category_manual=TRUE, "
        "ai_status='done', ai_processed_at=NOW() WHERE id=:id"),
        {"c": new_cat, "r": json.dumps(new_result), "id": email_id})
    db.commit()
    return {"ok": True, "email_id": email_id, "old_category": old_cat,
            "new_category": new_cat, "ai_result": new_result}


@router.get("/ai/category/corrections")
def category_corrections(limit: int = Query(200, ge=1, le=2000),
                         db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Lista emailurilor încadrate greșit (corecții manuale): vechea vs noua categorie."""
    rows = db.execute(text(
        "SELECT c.id, c.email_id, c.old_category, c.new_category, c.old_reason, c.corrected_by, "
        "to_char(c.created_at,'YYYY-MM-DD HH24:MI') AS created_at, "
        "e.subject, e.from_address, e.ai_category AS current_category "
        "FROM ai_category_corrections c JOIN emails e ON e.id=c.email_id "
        "ORDER BY c.id DESC LIMIT :l"), {"l": limit}).fetchall()
    total = db.execute(text("SELECT count(*) FROM ai_category_corrections")).scalar()
    return {"total": total, "items": [dict(r._mapping) for r in rows]}


@router.delete("/ai/category/corrections")
def category_corrections_reset(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Reset: sterge TOATE corectiile de categorie (ai_category_corrections).

    Folosit cand operatorul a introdus corectii de test gresite care ar polua
    faza de learning a prompturilor. NU atinge prompturile in sine (raman
    versiunile salvate); doar feedback-ul brut. Ireversibil."""
    n = db.execute(text("SELECT count(*) FROM ai_category_corrections")).scalar() or 0
    db.execute(text("DELETE FROM ai_category_corrections"))
    db.commit()
    return {"ok": True, "deleted": int(n)}


_REGEN_SYSTEM = (
    "Ești expert în prompt engineering pentru un clasificator de emailuri de suport al unei firme de "
    "monitorizare GPS/telemetrie pentru transport (CargoTrack — dispozitive GPS, carduri carGObox pentru "
    "taxe de drum, tahografe, managementul documentelor vehicule). Clientii sunt firme de transport. "
    "Clasificatorul incadreaza emailurile in 4 categorii: informatie, sesizare, reclamatie, necunoscut. "
    "Primești regulile actuale și o listă de emailuri pe care clasificatorul le-a încadrat GREȘIT. "
    "GRANITE CRITICE (cele mai frecvente erori de confundat):\n"
    "  - Notificare externa (amenda, alerta sistem, suspendare automata) la care clientul cere lamuriri "
    "-> INFORMATIE, NU sesizare.\n"
    "  - Client care cere status despre comanda/activare/livrare -> INFORMATIE, NU sesizare.\n"
    "  - Client care confirma o plata/actiune proprie -> INFORMATIE chiar daca ton iritai.\n"
    "  - Dispozitiv NU functioneaza / card nu merge / nu transmite -> SESIZARE.\n"
    "  - Ton agresiv singur NU face reclamatie; trebuie referinta la esec anterior al companiei.\n\n"
    "Sarcina ta: RESCRIE COMPLET regulile pentru informatie/sesizare/reclamatie — produ un prompt nou, "
    "CURAT și CONSOLIDAT, care încorporează ce e încă valid din regulile actuale ȘI lecțiile din "
    "emailurile greșit încadrate. NU adăuga pur și simplu reguli noi peste cele vechi: ELIMINĂ "
    "redundanțele, regulile contradictorii, repetițiile și fragmentele învechite, comasează regulile "
    "care spun același lucru, și rescrie totul coerent de la zero. Rezultatul trebuie să fie mai clar și "
    "(de regulă) mai scurt decât intrarea, nu mai lung. Păstrează reguli GENERALE (NU hardcoda emailuri "
    "specifice, NU referi cazuri individuale). "
    "Returnează DOAR JSON valid, fără ```, cu forma exactă: "
    '{"informatie":"<reguli complete rescrise>","sesizare":"<reguli complete rescrise>",'
    '"reclamatie":"<reguli complete rescrise>","explicatie":"<ce ai consolidat/eliminat, scurt, in romana>"}'
)


def _salvage_cat_json(raw):
    """Recupereaza dict-ul de prompturi pe categorie dintr-un raspuns AI ne-curat: JSON cu ``` in jur,
    JSON cu text inainte/dupa, sau JSON TRUNCHIAT (model a depasit max_tokens). Oglinda salvajului din
    ai_department, dar pe mai multe chei. Returneaza dict (chiar partial) sau None."""
    if not raw:
        return None
    # 1) JSON intreg, eventual cu ``` sau proza in jur
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    # 2) extragere per-cheie (rezista la JSON trunchiat / nevalid)
    out = {}
    for key in ("informatie", "sesizare", "reclamatie", "explicatie"):
        mm = re.search(r'"' + key + r'"\s*:\s*"((?:[^"\\]|\\.)*)"', raw, re.DOTALL)
        if mm:
            frag = mm.group(1)
            try:
                out[key] = json.loads('"' + frag + '"')
            except Exception:
                out[key] = frag.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\').strip()
    return out or None


_CAT_LABELS = {"informatie": "Informatie", "sesizare": "Sesizare",
               "reclamatie": "Reclamatie", "necunoscut": "Necunoscut"}


def _regen_system_one_cat(cat: str) -> str:
    """System prompt pentru regenerarea promptului UNEI singure categorii (oglinda celui de departament).
    Per-categorie => raspuns scurt, fara truncare; rescriere/consolidare, nu append."""
    return (
        "Esti expert in prompt engineering pentru un clasificator de emailuri de suport al unei firme de "
        "monitorizare GPS/telemetrie pentru transport (CargoTrack). Firma ofera: dispozitive GPS, carduri "
        "carGObox pentru taxe de drum, tahografe, managementul documentelor vehicule (CEMT, ITP etc.). "
        "Clientii sunt firme de transport si soferi profesionisti.\n\n"
        "Lucrezi pe O SINGURA categorie: " + _CAT_LABELS.get(cat, cat) + " (" + cat + ").\n\n"
        "Primesti promptul ACTUAL al acestei categorii si exemple REALE de emailuri incadrate GRESIT:\n"
        "  (A) emailuri pe care AI le-a pus in aceasta categorie desi NU apartin (prea larg)\n"
        "  (B) emailuri care AR FI TREBUIT puse AICI dar au mers la alta categorie (prea ingust)\n\n"
        "DEFINITII FIXE ale celor 3 categorii — nu le schimba, doar aplica-le:\n"
        "  INFORMATIE: email fara problema activa si fara nemultumire — cerere de informatii, confirmare, "
        "cerere administrativa, notificare externa (amenda, suspendare automata) la care clientul "
        "REACTIONEAZA sau cere lamuriri FARA a raporta o problema proprie a dispozitivului/serviciului.\n"
        "  SESIZARE: clientul raporteaza O PROBLEMA ACTIVA a unui dispozitiv/serviciu propriu care NU "
        "functioneaza si asteapta interventie/remediere — raportat PENTRU PRIMA DATA, fara referinta "
        "la contactari esuate anterioare.\n"
        "  RECLAMATIE: clientul exprima nemultumire EXPLICITA ca o problema ANTERIOARA (deja semnalata) "
        "nu a fost rezolvata — necesita DOUA elemente: problema anterioara + esecul companiei de a o rezolva.\n\n"
        "GRANITE CRITICE de retinut (cele mai frecvente erori):\n"
        "  - Notificare externa (amenda din Ungaria, alerta sistem, suspendare automata cont) la care "
        "clientul cere lamuriri sau confirma primirea -> INFORMATIE, NU sesizare (nu e o problema "
        "a dispozitivului, e o notificare administrativa).\n"
        "  - Client care cere STATUS despre o comanda/activare/livrare ('cand vine dispozitivul?', "
        "'cand se activeaza?') -> INFORMATIE, NU sesizare.\n"
        "  - Client care confirma ca a facut o plata sau o actiune ('am alimentat', 'am platit') "
        "-> INFORMATIE chiar daca tonul e iritat, daca nu raporteaza o problema tehnica.\n"
        "  - Client care raporteaza CA dispozitivul NU functioneaza, nu transmite, nu vede ruta, "
        "card nu merge -> SESIZARE.\n"
        "  - Ton agresiv/iritare SINGUR nu face reclamatie: trebuie referinta explicita la un esec "
        "anterior al companiei.\n\n"
        "SARCINA: RESCRIE COMPLET promptul ACESTEI categorii ca exemplele gresite sa fie clasificate "
        "CORECT pe viitor. Reguli GENERALE si clare (NU hardcoda emailuri specifice, NU referi cazuri "
        "individuale). NU adauga reguli noi peste cele vechi: ELIMINA redundantele, repetitiile, "
        "regulile contradictorii; comaseaza ce spune acelasi lucru; rescrie coerent de la zero. "
        "Rezultatul trebuie sa fie mai clar si (de regula) mai scurt decat intrarea, NU mai lung.\n\n"
        "IMPORTANT format: raspunsul tau INCEPE direct cu { si SE TERMINA cu }. NU scrie text/analiza "
        "inainte de JSON, NU folosi blocuri markdown (```). Explicatia (MAX 2 propozitii) o pui in campul "
        "\"explicatie\". Returneaza EXCLUSIV JSON valid, forma exacta:\n"
        '{"prompt":"<reguli complete rescrise>","explicatie":"<ce ai consolidat/eliminat, scurt, romana>"}'
    )


def _load_cat_corr(db, use_cts):
    """Lista de emailuri incadrate gresit pe categorie (sursa CTS sau corectii manuale)."""
    if use_cts:
        rows = db.execute(text(
            "SELECT e.ai_category AS old_category, gt.cts_category AS new_category, e.subject, "
            "LEFT(COALESCE(e.body_text, e.body_html, ''), 500) AS snippet "
            "FROM cts_ground_truth gt JOIN emails e ON e.id=gt.email_id "
            "WHERE COALESCE(gt.cts_direction,'received')='received' "
            "  AND gt.cts_category IS NOT NULL AND e.ai_category IS NOT NULL "
            "  AND gt.cts_category <> e.ai_category "
            "  AND gt.cts_category IN ('informatie','sesizare','reclamatie','necunoscut') "
            "ORDER BY gt.changed_at DESC NULLS LAST, gt.id DESC LIMIT 60")).fetchall()
    else:
        rows = db.execute(text(
            "SELECT c.old_category, c.new_category, e.subject, "
            "LEFT(COALESCE(e.body_text, e.body_html, ''), 500) AS snippet "
            "FROM ai_category_corrections c JOIN emails e ON e.id=c.email_id "
            "ORDER BY c.id DESC LIMIT 60")).fetchall()
    return [r._mapping for r in rows]


@router.post("/ai/category/regenerate-prompts")
def regenerate_prompts(body: dict = None, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Regenereaza prompturile de clasificare. Daca body['category'] e dat -> regenereaza DOAR acea
    categorie (un singur apel AI, raspuns scurt, fara truncare; UI-ul itereaza categoriile, ca la
    departament). Fara 'category' -> calea legacy all-in-one (un raspuns cu toate 3, fragila la
    truncare; pastrata pentru compatibilitate). NU salveaza automat.

    Sursa semnalului de „incadrat gresit":
      - implicit: corectiile manuale (ai_category_corrections), din modalul emailului;
      - body {use_cts:true}: divergentele fata de adevarul de teren CTS (ce a setat manual
        suportul in CTS vs ce a pus AI), pe mailuri PRIMITE — vezi modulul Mail-uri CTS."""
    if not iris_ai.is_configured():
        raise HTTPException(400, "IRIS AI neconfigurat (lipsește IRIS_AI_KEY)")
    body = body or {}
    use_cts = bool(body.get("use_cts"))
    cat = (body.get("category") or "").strip().lower()
    # Model implicit: sonnet (calitate > cost la rescrierea prompturilor). Override: body['model'].
    # claude-sonnet-5 respins de gateway IRIS (temperature deprecated); claude-sonnet-4-5 e echivalent si stabil.
    _model = (body.get("model") or "claude-sonnet-4-5").strip()
    prompts = category_classifier.load_prompts()
    corr = _load_cat_corr(db, use_cts)
    if not corr:
        raise HTTPException(400, ("Nicio divergenta fata de CTS (categorie) — nimic de regenerat"
                                  if use_cts else "Nu exista emailuri marcate ca incadrate gresit"))

    # ---- CALEA PER-CATEGORIE (preferata) ----
    if cat:
        if cat not in category_classifier.EDITABLE:
            raise HTTPException(400, "Categorie invalida")
        fp = [m for m in corr if m["old_category"] == cat]   # AI a pus GRESIT aici (corect e altul)
        fn = [m for m in corr if m["new_category"] == cat]   # ar fi trebuit aici (AI a pus altundeva)
        if not fp and not fn:
            return {"ok": True, "category": cat, "skipped": True, "reason": "no_corrections",
                    "suggested": {}, "based_on": 0, "fp": 0, "fn": 0}
        lines = ["CATEGORIE TINTA: " + _CAT_LABELS.get(cat, cat) + " (" + cat + ")",
                 "\nPROMPT ACTUAL:\n" + (prompts.get(cat) or "")]
        if fp:
            lines.append("\n\n[A] Emailuri pe care AI le-a pus GRESIT in aceasta categorie (de fapt "
                         "apartin altei categorii) — promptul e PREA LARG, clarifica ce NU apartine:")
            for i, m in enumerate(fp, 1):
                lines.append("\n(" + str(i) + ") Corect era: " + str(m["new_category"]) +
                             " | Subiect: " + (m["subject"] or "(gol)") +
                             "\n    Continut: " + (m["snippet"] or "").strip())
        if fn:
            lines.append("\n\n[B] Emailuri care AR FI TREBUIT puse in aceasta categorie dar AI le-a pus "
                         "altundeva — promptul e PREA INGUST, adauga indiciile lipsa:")
            for i, m in enumerate(fn, 1):
                lines.append("\n(" + str(i) + ") AI a pus gresit in: " + str(m["old_category"]) +
                             " | Subiect: " + (m["subject"] or "(gol)") +
                             "\n    Continut: " + (m["snippet"] or "").strip())
        content = "\n".join(lines)
        res = iris_ai.run_prompt(_regen_system_one_cat(cat), content, response_format="json",
                                 model_hint=_model,
                                 max_tokens=4500, task="cargo360:cat_prompt_regen", no_cache=True)
        parsed = res.get("parsed") if isinstance(res.get("parsed"), dict) else None
        v = parsed.get("prompt") if parsed else None
        if not (isinstance(v, str) and v.strip()):
            # Salvage din raspuns brut (``` / proza / trunchiat)
            err = res.get("error") if isinstance(res.get("error"), dict) else {}
            raw = res.get("text") or err.get("raw_text") or ""
            sv = _salvage_cat_json(raw) or {}
            v = sv.get("prompt")
            if isinstance(v, str) and v.strip():
                parsed = sv
        if not (isinstance(v, str) and v.strip()):
            # Ultima plasa: cerem ca TEXT PUR (fara JSON)
            res2 = iris_ai.run_prompt(
                _regen_system_one_cat(cat) +
                " ALTERNATIV, daca nu poti produce JSON, returneaza DOAR textul noului prompt, fara JSON.",
                content, response_format="text", model_hint=_model,
                max_tokens=4000, task="cargo360:cat_prompt_regen", no_cache=True)
            t2 = (res2.get("text") or "").strip()
            if t2.startswith("```"):
                t2 = re.sub(r"^```[a-zA-Z]*\s*", "", t2)
                t2 = re.sub(r"\s*```\s*$", "", t2).strip()
            sv2 = _salvage_cat_json(t2) if t2 else None
            if sv2 and isinstance(sv2.get("prompt"), str) and sv2["prompt"].strip():
                parsed = {"prompt": sv2["prompt"].strip(), "explicatie": "(regenerat in mod text)"}
                v = parsed["prompt"]
            elif t2 and not t2.lstrip().startswith("{"):
                parsed = {"prompt": t2, "explicatie": "(regenerat in mod text)"}
                v = t2
        if not (isinstance(v, str) and v.strip()):
            if not res.get("ok"):
                raise HTTPException(502, "Regenerare eșuată: " + str(res.get("error")))
            raise HTTPException(502, "AI nu a returnat un prompt valid")
        return {"ok": True, "category": cat, "suggested": {cat: v.strip()},
                "explicatie": (parsed.get("explicatie") if isinstance(parsed, dict) else None),
                "based_on": len(corr), "source": ("cts" if use_cts else "manual"),
                "fp": len(fp), "fn": len(fn), "model": res.get("model"), "usage": res.get("usage")}

    # ---- CALEA LEGACY ALL-IN-ONE (fara 'category') ----
    lines = ["REGULILE ACTUALE PE CATEGORIE:"]
    for c in category_classifier.EDITABLE:
        lines.append("\n=== " + c.upper() + " ===\n" + prompts[c])
    lines.append("\n\nEMAILURI ÎNCADRATE GREȘIT:")
    for i, m in enumerate(corr, 1):
        lines.append("\n[" + str(i) + "] AI a zis: " + str(m["old_category"]) +
                     " | CORECT: " + str(m["new_category"]) +
                     "\nSubiect: " + (m["subject"] or "(gol)") +
                     "\nConținut: " + (m["snippet"] or "").strip())
    content = "\n".join(lines)
    res = iris_ai.run_prompt(_REGEN_SYSTEM, content, response_format="json",
                             model_hint=_model,
                             max_tokens=8000, task="cargo360:prompt_regen", no_cache=True)
    parsed = res.get("parsed")
    if not isinstance(parsed, dict) or not parsed:
        err = res.get("error") if isinstance(res.get("error"), dict) else {}
        raw = res.get("text") or err.get("raw_text") or ""
        parsed = _salvage_cat_json(raw) or {}
    suggested = {}
    for c in category_classifier.EDITABLE:
        v = parsed.get(c) if isinstance(parsed, dict) else None
        if isinstance(v, str) and v.strip():
            suggested[c] = v.strip()
    if not suggested:
        if not res.get("ok"):
            raise HTTPException(502, "Regenerare eșuată: " + str(res.get("error")))
        raise HTTPException(502, "AI nu a returnat prompturi valide")
    return {"ok": True, "suggested": suggested,
            "explicatie": (parsed.get("explicatie") if isinstance(parsed, dict) else None),
            "based_on": len(corr), "source": ("cts" if use_cts else "manual"),
            "model": res.get("model"), "usage": res.get("usage")}


@router.get("/ai/analytics")
def ai_analytics(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Cozile de analiză AI (extensibil) + utilizare/cost din ai_call_log."""
    # --- Queue: email categorization (din emails.ai_status) ---
    qm = dict(db.execute(text(
        "SELECT count(*) FILTER (WHERE ai_status='pending') AS pending, "
        "count(*) FILTER (WHERE ai_status='done') AS done, "
        "count(*) FILTER (WHERE ai_status='error') AS error, count(*) AS total FROM emails"
    )).fetchone()._mapping)
    by_cat = db.execute(text(
        "SELECT ai_category AS category, count(*) AS n FROM emails "
        "WHERE ai_category IS NOT NULL GROUP BY ai_category ORDER BY n DESC")).fetchall()
    queues = [{
        "key": "email_category", "label": "Categorizare emailuri", "task": "email_category",
        "pending": qm["pending"], "done": qm["done"], "error": qm["error"], "total": qm["total"],
        "by_category": [dict(r._mapping) for r in by_cat],
    }]

    # --- Usage / cost din ai_call_log ---
    def _agg(where=""):
        m = db.execute(text(
            "SELECT count(*) AS calls, COALESCE(sum(cost_usd),0) AS cost, "
            "COALESCE(sum(tokens_in),0) AS tin, COALESCE(sum(tokens_out),0) AS tout, "
            "count(*) FILTER (WHERE NOT ok) AS errors FROM ai_call_log " + where)).fetchone()._mapping
        return {"calls": m["calls"] or 0, "cost": float(m["cost"] or 0),
                "tokens_in": int(m["tin"] or 0), "tokens_out": int(m["tout"] or 0), "errors": m["errors"] or 0}
    total = _agg()
    today = _agg("WHERE created_at >= CURRENT_DATE")
    by_model = [{"model": r._mapping["model"] or "?", "calls": r._mapping["calls"],
                 "cost": float(r._mapping["cost"] or 0), "tokens_in": int(r._mapping["tin"] or 0),
                 "tokens_out": int(r._mapping["tout"] or 0)}
                for r in db.execute(text(
                    "SELECT model, count(*) AS calls, COALESCE(sum(cost_usd),0) AS cost, "
                    "COALESCE(sum(tokens_in),0) AS tin, COALESCE(sum(tokens_out),0) AS tout "
                    "FROM ai_call_log GROUP BY model ORDER BY calls DESC")).fetchall()]
    by_task = [{"task": r._mapping["task"], "calls": r._mapping["calls"],
                "cost": float(r._mapping["cost"] or 0), "errors": r._mapping["errors"]}
               for r in db.execute(text(
                   "SELECT task, count(*) AS calls, COALESCE(sum(cost_usd),0) AS cost, "
                   "count(*) FILTER (WHERE NOT ok) AS errors FROM ai_call_log GROUP BY task ORDER BY calls DESC")).fetchall()]
    # email_id leagă fiecare apel NOVA de mailul efectiv (migrația 20260611). Defensiv:
    # selectăm coloana doar dacă există, altfel întoarcem None ca să nu pice pre-migrare.
    _has_eid = db.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='ai_call_log' AND column_name='email_id'")).fetchone() is not None
    _eid_col = "email_id" if _has_eid else "NULL AS email_id"
    recent = [{"task": r._mapping["task"], "model": r._mapping["model"],
               "tokens_in": r._mapping["tokens_in"], "tokens_out": r._mapping["tokens_out"],
               "cost_usd": float(r._mapping["cost_usd"] or 0) if r._mapping["cost_usd"] is not None else None,
               "ok": r._mapping["ok"], "error_code": r._mapping["error_code"],
               "email_id": r._mapping["email_id"], "created_at": r._mapping["created_at"]}
              for r in db.execute(text(
                  "SELECT task, model, tokens_in, tokens_out, cost_usd, ok, error_code, "
                  f"{_eid_col}, to_char(created_at,'YYYY-MM-DD HH24:MI:SS') AS created_at "
                  "FROM ai_call_log ORDER BY id DESC LIMIT 20")).fetchall()]
    usage = {
        "total_calls": total["calls"], "total_cost_usd": total["cost"],
        "avg_cost_usd": (total["cost"] / total["calls"]) if total["calls"] else 0,
        "tokens_in": total["tokens_in"], "tokens_out": total["tokens_out"], "errors": total["errors"],
        "today_calls": today["calls"], "today_cost_usd": today["cost"],
        "by_model": by_model, "by_task": by_task, "recent": recent,
    }
    return {"queues": queues, "usage": usage}


@router.get("/ai/cost-report")
def ai_cost_report(date_from: str = Query(...), date_to: str = Query(...),
                   fmt: str = Query("pdf"),
                   db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Raport costuri AI pe interval [date_from, date_to] (dată locală Bucuresti),
    agregat din ai_call_log. fmt=pdf (implicit, cu grafice) sau fmt=csv.
    Funcționează identic pe staging și producție (date per-app)."""
    import re
    import io as _io
    import csv as _csv
    from datetime import datetime
    from app.config import get_settings

    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_from or "") or not re.match(r"^\d{4}-\d{2}-\d{2}$", date_to or ""):
        raise HTTPException(400, "Format dată invalid (asteptat YYYY-MM-DD).")
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    # T3-R1: rândurile `local_cache` (apeluri evitate) nu sunt apeluri reale — ies din toate calculele.
    where = ("WHERE (created_at AT TIME ZONE 'Europe/Bucharest')::date BETWEEN :df AND :dt "
             "AND model IS DISTINCT FROM 'local_cache'")
    params = {"df": date_from, "dt": date_to}

    tm = db.execute(text(
        "SELECT count(*) AS calls, COALESCE(sum(cost_usd),0) AS cost, "
        "COALESCE(sum(tokens_in),0) AS tin, COALESCE(sum(tokens_out),0) AS tout, "
        "count(*) FILTER (WHERE NOT ok) AS errors, "
        "COALESCE(sum(cost_usd) FILTER (WHERE NOT ok),0) AS failed_cost FROM ai_call_log " + where), params).fetchone()._mapping
    totals = {"calls": tm["calls"] or 0, "cost": float(tm["cost"] or 0),
              "tokens_in": int(tm["tin"] or 0), "tokens_out": int(tm["tout"] or 0),
              "errors": tm["errors"] or 0, "failed_cost": float(tm["failed_cost"] or 0)}

    by_model = [{"model": r._mapping["model"], "calls": r._mapping["calls"],
                 "cost": float(r._mapping["cost"] or 0), "tokens_in": int(r._mapping["tin"] or 0),
                 "tokens_out": int(r._mapping["tout"] or 0)}
                for r in db.execute(text(
                    "SELECT model, count(*) AS calls, COALESCE(sum(cost_usd),0) AS cost, "
                    "COALESCE(sum(tokens_in),0) AS tin, COALESCE(sum(tokens_out),0) AS tout "
                    "FROM ai_call_log " + where + " GROUP BY model ORDER BY cost DESC, calls DESC"), params).fetchall()]

    btm_rows = [{"task": r._mapping["task"], "model": r._mapping["model"],
                 "calls": r._mapping["calls"], "cost": float(r._mapping["cost"] or 0),
                 "tin": int(r._mapping["tin"] or 0), "tout": int(r._mapping["tout"] or 0)}
                for r in db.execute(text(
                    "SELECT regexp_replace(task, '(:[0-9a-f]{6,})+$', '') AS task, model, count(*) AS calls, "
                    "COALESCE(sum(cost_usd),0) AS cost, COALESCE(sum(tokens_in),0) AS tin, COALESCE(sum(tokens_out),0) AS tout "
                    "FROM ai_call_log " + where + " GROUP BY 1, model"), params).fetchall()]

    by_task_map = {}
    for r in btm_rows:
        t = by_task_map.setdefault(r["task"], {"task": r["task"], "calls": 0, "cost": 0.0,
                                               "tokens_in": 0, "tokens_out": 0, "errors": 0,
                                               "failed_cost": 0.0, "_models": []})
        t["calls"] += r["calls"]; t["cost"] += r["cost"]
        t["tokens_in"] += r["tin"]; t["tokens_out"] += r["tout"]
        t["_models"].append((r["model"], r["calls"], r["cost"]))
    for r in db.execute(text(
            "SELECT regexp_replace(task, '(:[0-9a-f]{6,})+$', '') AS task, count(*) FILTER (WHERE NOT ok) AS errors, "
            "COALESCE(sum(cost_usd) FILTER (WHERE NOT ok),0) AS failed_cost "
            "FROM ai_call_log " + where + " GROUP BY 1"), params).fetchall():
        if r._mapping["task"] in by_task_map:
            by_task_map[r._mapping["task"]]["errors"] = r._mapping["errors"] or 0
            by_task_map[r._mapping["task"]]["failed_cost"] = float(r._mapping["failed_cost"] or 0)

    by_task = []
    for t in by_task_map.values():
        models = sorted(t["_models"], key=lambda x: x[1], reverse=True)
        top = models[0] if models else (None, 0, 0)
        t["top_model"] = top[0]
        t["top_share"] = (top[1] / t["calls"] * 100.0) if t["calls"] else None
        t.pop("_models", None)
        by_task.append(t)
    by_task.sort(key=lambda x: x["calls"], reverse=True)
    by_task_model = [{"task": r["task"], "model": r["model"], "calls": r["calls"], "cost": r["cost"]} for r in btm_rows]

    from app.services import cost_report as _cr
    by_flow = _cr.aggregate_flows(by_task)          # T3-R1: aceeași mapare task → flux peste tot

    # T3-R1: economia din cache pe fereastră (goală dacă tabelul lipsește sau nu are rânduri).
    cache_savings = []
    try:
        if db.execute(text("SELECT to_regclass('ai_cache_hit_log')")).scalar():
            cache_savings = [{"task_prefix": r._mapping["task_prefix"], "hits": int(r._mapping["hits"] or 0),
                              "saved_cost": float(r._mapping["saved"] or 0)}
                             for r in db.execute(text(
                                 "SELECT task_prefix, count(*) AS hits, COALESCE(sum(saved_cost_usd),0) AS saved "
                                 "FROM ai_cache_hit_log "
                                 "WHERE (created_at AT TIME ZONE 'Europe/Bucharest')::date BETWEEN :df AND :dt "
                                 "GROUP BY 1 ORDER BY saved DESC, hits DESC"), params).fetchall()]
    except Exception:
        db.rollback()
        cache_savings = []

    def _pct(part, whole):
        return ("%.2f" % (100.0 * part / whole)) if whole else ""

    def _lbl(m):
        if not m or m == "?":
            return "necunoscut"
        if m == "curated":
            return "IRIS (curated)"
        if m == "gemma":
            return "Gemma (local)"
        return m

    if fmt == "csv":
        buf = _io.StringIO()
        w = _csv.writer(buf)
        w.writerow(["Raport costuri AI", date_from, "->", date_to])
        w.writerow([])
        w.writerow(["PER MODEL"])
        w.writerow(["model", "interogari", "cost_usd", "tokens_in", "tokens_out"])
        for m in by_model:
            w.writerow([_lbl(m["model"]), m["calls"], "%.6f" % m["cost"], m["tokens_in"], m["tokens_out"]])
        w.writerow(["TOTAL", totals["calls"], "%.6f" % totals["cost"], totals["tokens_in"], totals["tokens_out"]])
        w.writerow([])
        w.writerow(["PE FLUX"])
        w.writerow(["flux", "apeluri", "cost_usd", "apeluri_esuate", "cost_esuate_usd", "cost_per_apel_usd"])
        for f in by_flow:
            w.writerow([f["flow"], f["calls"], "%.6f" % f["cost"], f["errors"], "%.6f" % f["failed_cost"],
                        "%.6f" % f["cost_per_call"]])
        w.writerow(["TOTAL", totals["calls"], "%.6f" % totals["cost"], totals["errors"],
                    "%.6f" % totals["failed_cost"],
                    "%.6f" % (totals["cost"] / totals["calls"]) if totals["calls"] else ""])
        w.writerow([])
        w.writerow(["PER TASK"])
        w.writerow(["task", "interogari", "cost_usd", "model_dominant", "pct_dominant", "erori",
                    "cost_erori_usd", "pct_cost_erori"])
        for t in by_task:
            share = ("%.0f" % t["top_share"]) if t["top_share"] is not None else ""
            w.writerow([t["task"], t["calls"], "%.6f" % t["cost"], _lbl(t["top_model"]), share, t["errors"],
                        "%.6f" % t["failed_cost"], _pct(t["failed_cost"], t["cost"])])
        w.writerow(["TOTAL", totals["calls"], "%.6f" % totals["cost"], "", "", totals["errors"],
                    "%.6f" % totals["failed_cost"], _pct(totals["failed_cost"], totals["cost"])])
        w.writerow([])
        w.writerow(["TASK x MODEL"])
        w.writerow(["task", "model", "interogari", "cost_usd"])
        for r in sorted(by_task_model, key=lambda x: ((x["task"] or ""), -x["calls"])):
            w.writerow([r["task"], _lbl(r["model"]), r["calls"], "%.6f" % r["cost"]])
        w.writerow([])
        w.writerow(["ECONOMIE DIN CACHE"])
        w.writerow(["prefix", "hituri", "cost_evitat_usd"])
        for c in cache_savings:
            w.writerow([c["task_prefix"], c["hits"], "%.6f" % c["saved_cost"]])
        data = buf.getvalue().encode("utf-8-sig")
        fname = "raport-costuri-ai_%s_%s.csv" % (date_from, date_to)
        return Response(content=data, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="%s"' % fname})

    # PDF: limităm la cele mai relevante taskuri (CSV rămâne complet) ca raportul să fie compact.
    TASK_CAP, DETAIL_CAP = 40, 25
    pdf_tasks = list(by_task[:TASK_CAP])
    rest = by_task[TASK_CAP:]
    if rest:
        pdf_tasks.append({
            "task": "… alte %d taskuri" % len(rest),
            "calls": sum(t["calls"] for t in rest), "cost": sum(t["cost"] for t in rest),
            "tokens_in": sum(t["tokens_in"] for t in rest), "tokens_out": sum(t["tokens_out"] for t in rest),
            "errors": sum(t["errors"] for t in rest), "top_model": None, "top_share": None,
            "failed_cost": sum(t["failed_cost"] for t in rest),
        })
    top_names = set(t["task"] for t in by_task[:DETAIL_CAP])
    pdf_btm = [r for r in by_task_model if r["task"] in top_names]

    s_ = get_settings()
    meta = {"app_name": s_.app_name, "app_env": s_.app_env, "app_version": s_.app_version,
            "date_from": date_from, "date_to": date_to,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    pdf = _cr.generate_cost_report_pdf(meta, totals, by_model, pdf_tasks, pdf_btm,
                                       by_flow=by_flow, cache_savings=cache_savings)
    fname = "raport-costuri-ai_%s_%s.pdf" % (date_from, date_to)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="%s"' % fname})


@router.get("/ai/category/stats")
def category_stats(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    by_cat = db.execute(text(
        "SELECT COALESCE(ai_category,'(neclasificat)') AS category, count(*) AS n "
        "FROM emails GROUP BY ai_category ORDER BY n DESC")).fetchall()
    by_status = db.execute(text(
        "SELECT ai_status, count(*) AS n FROM emails GROUP BY ai_status ORDER BY n DESC")).fetchall()
    return {
        "configured": iris_ai.is_configured(),
        "by_category": [dict(r._mapping) for r in by_cat],
        "by_status": [dict(r._mapping) for r in by_status],
    }


@router.post("/ai/category/{email_id}/run")
def category_run_one(email_id: int, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """(Re)clasifică un singur email — folosit după editarea prompturilor. Ocolește curated-cache
    (force_fresh=True) ca să aplice prompturile curente, nu un răspuns cached posibil expirat."""
    res = _classify_and_store(db, email_id, force_fresh=True)
    return {"ok": True, "email_id": email_id, "ai_category": res["category"], "ai_result": res}


def _range_where(only_pending: bool, from_date, from_id):
    conds, params = [], {}
    if only_pending:
        conds.append("ai_status='pending'")
    if from_date:
        conds.append("received_at >= :fd"); params["fd"] = from_date
    if from_id is not None:
        conds.append("id >= :fid"); params["fid"] = from_id
    where = "WHERE " + (" AND ".join(conds) if conds else "1=1")
    return where, params


@router.post("/ai/category/reset")
def category_reset(from_date: str = Query(None), from_id: int = Query(None),
                   db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Marchează emailurile dintr-un interval ca 'pending' (pentru reclasificare).
    Specifică from_date (YYYY-MM-DD, received_at >=) SAU from_id (id >=)."""
    if not from_date and from_id is None:
        raise HTTPException(400, "Specifică from_date sau from_id")
    where, params = _range_where(False, from_date, from_id)
    res = db.execute(text(
        f"UPDATE emails SET ai_status='pending', ai_category=NULL, ai_result=NULL, "
        f"ai_processed_at=NULL {where}"), params)
    db.commit()
    return {"ok": True, "reset": res.rowcount}


@router.post("/ai/category/backfill")
def category_backfill(limit: int = Query(100, ge=1, le=1000),
                      only_pending: bool = Query(True),
                      from_date: str = Query(None),
                      from_id: int = Query(None),
                      db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Clasifică un lot de emailuri (cele mai noi întâi). only_pending=true → doar
    ai_status='pending'. Opțional limitat la un interval (from_date / from_id).
    Fiecare email = un apel AI (cost mic dar real)."""
    if not iris_ai.is_configured():
        raise HTTPException(400, "IRIS AI neconfigurat (lipsește IRIS_AI_KEY)")
    where, params = _range_where(only_pending, from_date, from_id)
    params["lim"] = limit
    rows = db.execute(text(
        f"SELECT id, subject, from_address, from_name, body_text, body_html, conversation_id, received_at "
        f"FROM emails {where} ORDER BY received_at DESC LIMIT :lim"), params).fetchall()
    processed, done, errors = 0, 0, 0
    for r in rows:
        em = dict(r._mapping)
        processed += 1
        res = category_classifier.classify_category(em)
        if res:
            db.execute(text(
                "UPDATE emails SET ai_category=:c, ai_result=CAST(:r AS jsonb), "
                "ai_status='done', ai_processed_at=NOW() WHERE id=:id"),
                {"c": res["category"], "r": json.dumps(res), "id": em["id"]})
            done += 1
        else:
            db.execute(text("UPDATE emails SET ai_status='error', ai_processed_at=NOW() WHERE id=:id"),
                       {"id": em["id"]})
            errors += 1
    db.commit()
    return {"ok": True, "processed": processed, "done": done, "errors": errors,
            "hint": "rerulează pentru următorul lot dacă processed == limit"}


# ---------------------------------------------------------------------------
# Reclasificare în FUNDAL (server-side, fire-and-forget).
# Pornește scripts/reclassify_all.py ca proces detașat (start_new_session) — supraviețuiește
# reciclării workerilor gunicorn și închiderii UI-ului. Progresul e scris de script în
# logs/reclassify_status.json și citit de GET /reclassify/status. Fără worker daemon, fără
# tabel nou: coada efectivă este chiar ai_status=pending.
# ---------------------------------------------------------------------------
import os
import subprocess
from datetime import datetime, timezone

_APP_DIR = "/opt/iris-mailguard"
_STATUS_FILE = f"{_APP_DIR}/logs/reclassify_status.json"
_PY = f"{_APP_DIR}/venv/bin/python"
_STALE_SECONDS = 180  # heartbeat mai vechi de atat => job mort, se poate reporni


def _read_recl_status():
    try:
        with open(_STATUS_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def _is_alive(st) -> bool:
    """Job activ = running + heartbeat proaspat. Apara impotriva jobului mort care altfel
    ar bloca pornirea la nesfarsit."""
    if not st or not st.get("running"):
        return False
    try:
        ts = datetime.fromisoformat(st["updated_at"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age < _STALE_SECONDS
    except Exception:
        return False


@router.post("/ai/category/reclassify/start")
def reclassify_start(from_date: str = Query(None), from_id: int = Query(None),
                     fresh: bool = Query(True),
                     db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Porneste reclasificarea in fundal pe server. Procesarea continua chiar daca inchizi
    modalul/UI-ul. Specifica from_date (YYYY-MM-DD), from_id (id >=), sau niciunul = toate
    emailurile pending. La dublu-click intoarce jobul existent (nu dubleaza costul AI).
    fresh=True (implicit) ocoleste curated-cache ca prompturile curente sa se aplice efectiv."""
    if not iris_ai.is_configured():
        raise HTTPException(400, "NOVA AI neconfigurat (lipseste NOVA_AI_KEY)")

    cur = _read_recl_status()
    if _is_alive(cur):
        return {"ok": True, "already_running": True, "status": cur}

    args = [_PY, "-m", "scripts.reclassify_all"]
    if fresh:
        args.append("--fresh")
    if from_date:
        where, params = _range_where(False, from_date, None)
        db.execute(text(f"UPDATE emails SET ai_status='pending', ai_category=NULL, ai_result=NULL, "
                        f"ai_processed_at=NULL {where} AND status <> 'ndr'"), params)
        db.commit()
        args += ["--from-date", from_date]
    elif from_id is not None:
        where, params = _range_where(False, None, from_id)
        db.execute(text(f"UPDATE emails SET ai_status='pending', ai_category=NULL, ai_result=NULL, "
                        f"ai_processed_at=NULL {where} AND status <> 'ndr'"), params)
        db.commit()
        args += ["--from-id", str(from_id)]

    total = db.execute(text(
        "SELECT count(*) AS n FROM emails WHERE status <> 'ndr' AND ai_status='pending'"
        + (" AND received_at >= :fd" if from_date else "")
        + (" AND id >= :fid" if from_id is not None else "")),
        {k: v for k, v in (("fd", from_date), ("fid", from_id)) if v is not None}).scalar()

    subprocess.Popen(args, cwd=_APP_DIR, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    logger.info("reclassify background started: %s (total=%s)", args, total)
    return {"ok": True, "started": True, "total": int(total or 0),
            "scope": from_date or (("id:" + str(from_id)) if from_id is not None else "all_pending")}


import signal as _signal


@router.post("/ai/category/reclassify/cancel")
def reclassify_cancel(admin=Depends(get_current_admin)):
    """Oprește jobul de reclasificare care rulează în fundal. Emailurile deja procesate rămân
    clasificate; cele neatinse rămân ai_status='pending' (le poți relua oricând cu Pornește)."""
    st = _read_recl_status()
    if not st or not st.get("running"):
        return {"ok": True, "was_running": False, "note": "Niciun job activ."}
    pid = st.get("pid")
    killed = False
    if pid:
        try:
            os.kill(int(pid), _signal.SIGTERM)
            killed = True
        except ProcessLookupError:
            pass  # deja terminat
        except Exception as e:
            logger.warning("cancel kill failed pid=%s: %s", pid, e)
    # marchează jobul oprit imediat (nu aștepta heartbeat-ul stale)
    st["running"] = False
    st["canceled"] = True
    try:
        tmp = _STATUS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, _STATUS_FILE)
    except Exception:
        pass
    logger.info("reclassify canceled pid=%s killed=%s", pid, killed)
    return {"ok": True, "was_running": True, "killed": killed,
            "processed": st.get("processed", 0)}


@router.get("/ai/category/reclassify/status")
def reclassify_status(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Progresul jobului de reclasificare. running=true cat timp mai sunt emailuri de procesat."""
    st = _read_recl_status()
    pending = db.execute(text(
        "SELECT count(*) AS n FROM emails WHERE status <> 'ndr' AND ai_status='pending'")).scalar()
    alive = _is_alive(st)
    out = {"alive": alive, "pending_now": int(pending or 0)}
    if st:
        out.update(st)
        if st.get("running") and not alive:
            out["running"] = False
            out["stale"] = True
    return out
