"""Scoring AI detaliat pentru apeluri — agent score, customer score, sfaturi, rezumat.

Prompturile sunt stocate în tabelul call_scoring_prompts (extensibil — se pot adăuga
oricând noi tipuri de întrebări AI). La prima rulare, seeding automat din SEED_PROMPTS.
Scoring-ul rulează batch nocturn via score_batch().
"""
import json
import logging
import os as _os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Dict, Any

from sqlalchemy import text
from app.database import SessionLocal
from app.services import iris_ai

logger = logging.getLogger("mailguard.call_scorer")

_MAX_TRANSCRIPT_CHARS = 12000
_BATCH_DEFAULT_LIMIT = 200

# Prompturi seed (conținut din fișierele diag Bia — încărcat din fișiere la seeding)
# Cheile trebuie să corespundă cu coloanele din call_ai_scores unde se mapează rezultatele
SEED_PROMPTS: Dict[str, Dict[str, Any]] = {
    "checkForValidCall": {
        "label": "Verificare validitate apel",
        "output_schema": {"isValid": "bool", "reason": "str"},
    },
    "speakers": {
        "label": "Identificare vorbitori (agent/client)",
        "output_schema": {"agentSpeaker": "str", "clientSpeaker": "str"},
    },
    "issueSummarization": {
        "label": "Rezumat problemă",
        "output_schema": {"summary": "str"},
    },
    "issueResolution": {
        "label": "Rezolvare problemă",
        "output_type": "binary",
        "output_schema": {
            "mainProblem": "str", "requestWithinCompanyScope": "bool",
            "problemWasSolved": "bool", "mainSolution": "str",
        },
    },
    "issueTags": {
        "label": "Etichete problemă",
        "output_schema": {"category": "str", "subcategory": "str", "issueTopic": "str"},
    },
    "customerVulgarWords": {
        "label": "Cuvinte vulgare client",
        "output_schema": {"found": "bool", "words": "list"},
    },
    "customerScore": {
        "label": "Scor client",
        "output_schema": {
            "explainingTheProblem": "int", "patient": "int",
            "understanding": "int", "politeness": "int", "empathy": "int",
        },
    },
    "agentVulgarWords": {
        "label": "Cuvinte vulgare agent",
        "output_schema": {"found": "bool", "words": "list"},
    },
    "agentScore": {
        "label": "Scor agent",
        "output_type": "score",
        "output_schema": {
            "explainingTheSolution": "int", "patient": "int",
            "understanding": "int", "politeness": "int", "empathy": "int",
            "transparency": "int",
        },
    },
    "agentAdviceEmpathy": {
        "label": "Sfat agent – empatie",
        "output_schema": {"score": "int", "observations": "str", "improvements": "list"},
    },
    "agentAdviceProfessionalism": {
        "label": "Sfat agent – profesionalism",
        "output_schema": {"score": "int", "observations": "str", "improvements": "list"},
    },
    "agentAdviceClarity": {
        "label": "Sfat agent – claritate",
        "output_schema": {"score": "int", "observations": "str", "improvements": "list"},
    },
    "agentActions": {
        "label": "Acțiuni agent",
        "output_schema": {
            "keyPhrases": "list", "actionItems": "list",
            "nextStepsClearlyStatedToCustomer": "bool", "nextStepsObservation": "str",
        },
    },
    "agentAdviceNextSteps": {
        "label": "Sfat agent – pași următori",
        "output_type": "advice",
        "output_schema": {"observation": "str", "advice": "str"},
    },
    "customerAdditionalRequests": {
        "label": "Cereri suplimentare client",
        "output_schema": {"additionalRequests": "list", "unacknowledgedCount": "int"},
    },
}


# Prompturile versionate în repo: app/services/prompts/calls/<key>.txt
PROMPTS_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "prompts", "calls")


def load_prompt_texts_from_repo() -> Dict[str, str]:
    """Textele prompturilor versionate în repo (app/services/prompts/calls/<key>.txt)."""
    import glob
    texts: Dict[str, str] = {}
    for fpath in glob.glob(_os.path.join(PROMPTS_DIR, "*.txt")):
        key = _os.path.basename(fpath)[:-4]
        with open(fpath, encoding="utf-8") as f:
            content = f.read().strip()
        if content:
            texts[key] = content
    return texts


def _load_prompt_texts_from_files() -> Dict[str, str]:
    """Textele de seeding: repo (prioritar) + upload-uri diag din /tmp (fallback istoric)."""
    import glob
    texts = {}
    pattern = "/tmp/iris_diag_uploads/**/*.txt"
    for fpath in glob.glob(pattern, recursive=True):
        fname = _os.path.basename(fpath)
        # Ex: 20260722_151526_717_agentScore.txt -> key = agentScore
        parts = fname.replace(".txt", "").split("_", 4)
        if len(parts) >= 5:
            key = parts[4]
        elif len(parts) == 4:
            key = parts[3]
        else:
            continue
        if key in SEED_PROMPTS:
            with open(fpath) as f:
                texts[key] = f.read().strip()
    # Repo bate upload-urile vechi din /tmp
    texts.update(load_prompt_texts_from_repo())
    return texts


def seed_prompts_if_empty(db=None) -> int:
    """Inserează prompturile seed dacă tabelul e gol. Returnează nr rânduri inserate."""
    close_db = db is None
    if db is None:
        db = SessionLocal()
    try:
        count = db.execute(text("SELECT COUNT(*) FROM call_scoring_prompts")).scalar()
        if count > 0:
            return 0
        file_texts = _load_prompt_texts_from_files()
        inserted = 0
        for key, meta in SEED_PROMPTS.items():
            prompt_text = file_texts.get(key, f"[Prompt {key} — adaugă textul manual]")
            db.execute(text(
                "INSERT INTO call_scoring_prompts (key, label, prompt_text, enabled, output_type, output_schema) "
                "VALUES (:key, :label, :pt, true, :otype, :schema) ON CONFLICT (key) DO NOTHING"
            ), {
                "key": key,
                "label": meta["label"],
                "pt": prompt_text,
                "otype": meta.get("output_type", "json"),
                "schema": json.dumps(meta["output_schema"]),
            })
            inserted += 1
        db.commit()
        logger.info("call_scoring_prompts seeded with %d prompts", inserted)
        return inserted
    except Exception:
        db.rollback()
        logger.exception("seed_prompts_if_empty failed")
        return 0
    finally:
        if close_db:
            db.close()


def sync_prompts_from_repo(db=None, keys=None, insert_only: bool = False,
                           dry_run: bool = False) -> Dict[str, Any]:
    """Sincronizează call_scoring_prompts cu fișierele din PROMPTS_DIR.

    insert_only=True → doar cheile lipsă (nu suprascrie textele existente/editate din UI).
    Returnează {"inserted": [...], "updated": [...], "unchanged": [...]}.
    """
    close_db = db is None
    if db is None:
        db = SessionLocal()
    try:
        texts = load_prompt_texts_from_repo()
        if keys:
            texts = {k: v for k, v in texts.items() if k in keys}
        inserted, updated, unchanged = [], [], []
        for key in sorted(texts):
            body = texts[key]
            meta = SEED_PROMPTS.get(key, {})
            row = db.execute(text(
                "SELECT prompt_text FROM call_scoring_prompts WHERE key = :k"
            ), {"k": key}).fetchone()

            if row is None:
                if not dry_run:
                    db.execute(text(
                        "INSERT INTO call_scoring_prompts "
                        "(key, label, prompt_text, enabled, output_type, output_schema) "
                        "VALUES (:k, :l, :pt, true, :ot, CAST(:sc AS jsonb)) "
                        "ON CONFLICT (key) DO NOTHING"
                    ), {
                        "k": key,
                        "l": meta.get("label", key),
                        "pt": body,
                        "ot": meta.get("output_type", "json"),
                        "sc": json.dumps(meta.get("output_schema", {})),
                    })
                inserted.append(key)
            elif insert_only or (row[0] or "").strip() == body:
                unchanged.append(key)
            else:
                if not dry_run:
                    params = {"k": key, "pt": body}
                    sets = ["prompt_text = :pt", "updated_at = NOW()"]
                    if meta.get("output_schema"):
                        sets.append("output_schema = CAST(:sc AS jsonb)")
                        params["sc"] = json.dumps(meta["output_schema"])
                    if meta.get("output_type"):
                        sets.append("output_type = :ot")
                        params["ot"] = meta["output_type"]
                    db.execute(text(
                        f"UPDATE call_scoring_prompts SET {', '.join(sets)} WHERE key = :k"
                    ), params)
                updated.append(key)

        if dry_run:
            db.rollback()
        else:
            db.commit()
        return {"inserted": inserted, "updated": updated, "unchanged": unchanged}
    except Exception:
        db.rollback()
        logger.exception("sync_prompts_from_repo failed")
        return {"inserted": [], "updated": [], "unchanged": [], "error": True}
    finally:
        if close_db:
            db.close()


def _load_active_prompts(db) -> Dict[str, Dict[str, str]]:
    rows = db.execute(text(
        "SELECT key, prompt_text, output_type FROM call_scoring_prompts WHERE enabled = true"
    )).fetchall()
    return {row[0]: {"prompt_text": row[1], "output_type": row[2] or "json"} for row in rows}


def _build_transcript(call_row) -> str:
    """Construiește transcript text din transcript_turns (diarizat) sau transcript raw."""
    turns = call_row.transcript_turns
    if turns:
        if isinstance(turns, str):
            turns = json.loads(turns)
        lines = []
        for t in turns:
            speaker = t.get("speaker", "?")
            txt = t.get("text", "").strip()
            if txt:
                lines.append(f"{speaker}: {txt}")
        text_out = "\n".join(lines)
    else:
        text_out = call_row.transcript or ""
    return text_out[:_MAX_TRANSCRIPT_CHARS]


def _run_one_prompt(key: str, prompt_text: str, transcript: str, output_type: str = "json") -> tuple[str, Any]:
    """Rulează un singur prompt pe transcript. Returnează (key, parsed_result sau text)."""
    is_text = output_type == "text"
    res = iris_ai.run_prompt(
        system=prompt_text,
        content=transcript,
        response_format="text" if is_text else "json",
        model_hint="claude-haiku-4-5-20251001",
        temperature=0.0,
        max_tokens=1500,
        task=f"call_score_{key}",
        no_cache=True,
    )
    if not res.get("ok"):
        logger.warning("call_scorer prompt %s failed: %s", key, res.get("error"))
        return key, None
    if is_text:
        return key, res.get("text") or res.get("raw_text") or res.get("content")
    if not res.get("parsed"):
        logger.warning("call_scorer prompt %s failed: %s", key, res.get("error"))
        return key, None
    return key, res["parsed"]


def _avg(*vals) -> Optional[float]:
    """Medie simplă a valorilor non-None."""
    nums = [v for v in vals if v is not None]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 2)


def _safe_bool(v) -> Optional[bool]:
    """Bool tolerant: modelul poate întoarce true/false ca string ('true'/'adevarat')."""
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    sv = str(v).strip().lower()
    if sv in ("true", "1", "da", "adevarat", "adevărat", "yes"):
        return True
    if sv in ("false", "0", "nu", "fals", "no"):
        return False
    return None


def _fmt_advice(d: Dict[str, Any]) -> Optional[str]:
    """Sfat agent → text pentru coloană: observația + sfatul concret."""
    if not d:
        return None
    obs = (d.get("observation") or d.get("observations") or "").strip()
    adv = (d.get("advice") or "").strip()
    parts = [x for x in (obs, adv) if x]
    return "\n\n".join(parts) or None


def _safe_int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def score_call(call_id: int, db=None, force: bool = False) -> Dict[str, Any]:
    """Scorează un singur apel. Persistă în call_ai_scores. Returnează dict cu rezultatele."""
    close_db = db is None
    if db is None:
        db = SessionLocal()
    try:
        # Verifică să nu fie deja scorat (dacă nu e force)
        existing = db.execute(text(
            "SELECT id FROM call_ai_scores WHERE call_id = :id"
        ), {"id": call_id}).fetchone()
        if existing and not force:
            return {"ok": False, "reason": "already_scored", "call_id": call_id}
        if existing and force:
            db.execute(text("DELETE FROM call_ai_scores WHERE call_id = :id"), {"id": call_id})
            db.commit()

        # Fetch apel
        call_row = db.execute(text(
            "SELECT id, transcript, transcript_turns FROM calls WHERE id = :id"
        ), {"id": call_id}).fetchone()
        if not call_row:
            return {"ok": False, "reason": "not_found", "call_id": call_id}

        transcript = _build_transcript(call_row)
        if not transcript.strip():
            return {"ok": False, "reason": "no_transcript", "call_id": call_id}

        prompts = _load_active_prompts(db)
        if not prompts:
            return {"ok": False, "reason": "no_prompts", "call_id": call_id}

        # Rulează toate prompturile în paralel (max 5 concurent — cost control)
        results: Dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {
                pool.submit(_run_one_prompt, key, pmeta["prompt_text"], transcript, pmeta.get("output_type", "json")): key
                for key, pmeta in prompts.items()
            }
            for future in as_completed(futures):
                k, v = future.result()
                results[k] = v

        # Mapare rezultate → coloane call_ai_scores
        r = results

        valid_call_data = r.get("checkForValidCall") or {}
        speakers_data = r.get("speakers") or {}
        agent_score_data = r.get("agentScore") or {}
        customer_score_data = r.get("customerScore") or {}
        adv_empathy = r.get("agentAdviceEmpathy") or {}
        adv_prof = r.get("agentAdviceProfessionalism") or {}
        adv_clarity = r.get("agentAdviceClarity") or {}
        actions_data = r.get("agentActions") or {}
        adv_next = r.get("agentAdviceNextSteps") or {}
        addl_req = r.get("customerAdditionalRequests") or {}
        issue_summ_raw = r.get("issueSummarization")
        issue_summ = issue_summ_raw if isinstance(issue_summ_raw, dict) else {}
        issue_tags = r.get("issueTags")
        issue_res = r.get("issueResolution") or {}
        agent_vulgar = r.get("agentVulgarWords")
        customer_vulgar = r.get("customerVulgarWords")
        agentul_sa_prezentat = (r.get("agentulSaPrezentat") or {}).get("result")
        clientul_aminta_judecata = (r.get("clientulAmintaJudecata") or {}).get("result")
        clientul_aminta_renuntare = (r.get("clientulAmintaRenuntare") or {}).get("result")
        clientul_contactat_anterior = (r.get("clientulContactatAnterior") or {}).get("result")

        # Scoruri agent
        a_exp = _safe_int(agent_score_data.get("explainingTheSolution"))
        a_pat = _safe_int(agent_score_data.get("patient"))
        a_und = _safe_int(agent_score_data.get("understanding"))
        a_pol = _safe_int(agent_score_data.get("politeness"))
        a_emp = _safe_int(agent_score_data.get("empathy"))
        a_tra = _safe_int(agent_score_data.get("transparency"))
        a_total = _avg(a_exp, a_pat, a_und, a_pol, a_emp, a_tra)

        # Scoruri client
        c_exp = _safe_int(customer_score_data.get("explainingTheProblem"))
        c_pat = _safe_int(customer_score_data.get("patient"))
        c_und = _safe_int(customer_score_data.get("understanding"))
        c_pol = _safe_int(customer_score_data.get("politeness"))
        c_emp = _safe_int(customer_score_data.get("empathy"))
        c_total = _avg(c_exp, c_pat, c_und, c_pol, c_emp)

        db.execute(text("""
            INSERT INTO call_ai_scores (
                call_id, scored_at,
                is_valid_call,
                agent_speaker, client_speaker,
                agent_explaining_solution, agent_patient, agent_understanding,
                agent_politeness, agent_empathy, agent_transparency, agent_score_total,
                agent_advice_empathy, agent_advice_professionalism, agent_advice_clarity,
                agent_advice_next_steps,
                agent_actions, agent_next_steps_clear, agent_next_steps_observation,
                customer_explaining, customer_patient, customer_understanding,
                customer_politeness, customer_empathy, customer_score_total,
                agent_vulgar_words, customer_vulgar_words,
                issue_summary, issue_tags, issue_resolved,
                issue_main_problem, issue_main_solution, issue_within_company_scope,
                customer_additional_requests, customer_unacknowledged_count,
                agentul_sa_prezentat, clientul_aminta_judecata,
                clientul_aminta_renuntare, clientul_contactat_anterior,
                model
            ) VALUES (
                :call_id, NOW(),
                :is_valid,
                :agent_spk, :client_spk,
                :a_exp, :a_pat, :a_und, :a_pol, :a_emp, :a_tra, :a_total,
                :adv_emp, :adv_prof, :adv_clar,
                :adv_next,
                CAST(:actions AS jsonb), :next_clear, :next_obs,
                :c_exp, :c_pat, :c_und, :c_pol, :c_emp, :c_total,
                CAST(:ag_vul AS jsonb), CAST(:cu_vul AS jsonb),
                :issue_sum, CAST(:issue_tags AS jsonb), :issue_res,
                :issue_main_problem, :issue_main_solution, :issue_scope,
                CAST(:addl_req AS jsonb), :unack_count,
                :agentul_sa_prezentat, :clientul_aminta_judecata,
                :clientul_aminta_renuntare, :clientul_contactat_anterior,
                'claude-haiku-4-5-20251001'
            )
            ON CONFLICT (call_id) DO NOTHING
        """), {
            "call_id": call_id,
            "is_valid": valid_call_data.get("isValid"),
            "agent_spk": speakers_data.get("agentSpeaker"),
            "client_spk": speakers_data.get("clientSpeaker"),
            "a_exp": a_exp, "a_pat": a_pat, "a_und": a_und, "a_pol": a_pol, "a_emp": a_emp,
            "a_tra": a_tra, "a_total": a_total,
            "adv_emp": str(adv_empathy.get("observations") or adv_empathy.get("improvements") or ""),
            "adv_prof": str(adv_prof.get("observations") or adv_prof.get("improvements") or ""),
            "adv_clar": str(adv_clarity.get("observations") or adv_clarity.get("improvements") or ""),
            "adv_next": _fmt_advice(adv_next),
            "actions": json.dumps(actions_data) if actions_data else None,
            "next_clear": _safe_bool(actions_data.get("nextStepsClearlyStatedToCustomer")),
            "next_obs": actions_data.get("nextStepsObservation") or None,
            "c_exp": c_exp, "c_pat": c_pat, "c_und": c_und, "c_pol": c_pol, "c_emp": c_emp,
            "c_total": c_total,
            "ag_vul": json.dumps(agent_vulgar) if agent_vulgar else None,
            "cu_vul": json.dumps(customer_vulgar) if customer_vulgar else None,
            "issue_sum": issue_summ_raw if isinstance(issue_summ_raw, str) else issue_summ.get("summary"),
            "issue_tags": json.dumps(issue_tags) if issue_tags else None,
            "issue_res": _safe_bool(issue_res.get("problemWasSolved")),
            "issue_main_problem": issue_res.get("mainProblem") or None,
            "issue_main_solution": issue_res.get("mainSolution") or None,
            "issue_scope": _safe_bool(issue_res.get("requestWithinCompanyScope")),
            "addl_req": json.dumps(addl_req) if addl_req else None,
            "unack_count": _safe_int(addl_req.get("unacknowledgedCount")),
            "agentul_sa_prezentat": agentul_sa_prezentat,
            "clientul_aminta_judecata": clientul_aminta_judecata,
            "clientul_aminta_renuntare": clientul_aminta_renuntare,
            "clientul_contactat_anterior": clientul_contactat_anterior,
            "model": "claude-haiku-4-5-20251001",
        })
        db.commit()
        logger.info("call_scorer: scored call_id=%s agent_total=%s", call_id, a_total)
        return {"ok": True, "call_id": call_id, "agent_score_total": a_total, "customer_score_total": c_total}

    except Exception:
        db.rollback()
        logger.exception("call_scorer: score_call failed for call_id=%s", call_id)
        return {"ok": False, "reason": "exception", "call_id": call_id}
    finally:
        if close_db:
            db.close()


def score_batch(limit: int = _BATCH_DEFAULT_LIMIT, days_back: int = 1, progress_cb=None, rescore_null: bool = False) -> Dict[str, Any]:
    """Scorează apelurile nescorate din ultimele `days_back` zile (exclude blacklist).
    progress_cb(scored, failed, total) apelat după fiecare apel procesat.
    rescore_null=True: șterge scorurile cu agent_score_total NULL înainte de re-scorare.
    """
    if not iris_ai.is_configured():
        return {"ok": False, "reason": "ai_not_configured", "scored": 0}

    days_back = max(1, min(days_back, 365))

    db = SessionLocal()
    try:
        seed_prompts_if_empty(db)
        # Prompturile noi din repo apar automat în rulare (fără a suprascrie textele existente).
        sync_prompts_from_repo(db, insert_only=True)

        if rescore_null:
            deleted = db.execute(text("""
                DELETE FROM call_ai_scores
                WHERE agent_score_total IS NULL
                  AND call_id IN (
                      SELECT c.id FROM calls c
                      WHERE c.started_at >= NOW() - INTERVAL '1 day' * :days
                  )
            """), {"days": days_back}).rowcount
            db.commit()
            logger.info("call_scorer batch: deleted %d empty score rows for rescore", deleted)

        rows = db.execute(text("""
            SELECT c.id
            FROM calls c
            LEFT JOIN call_ai_scores cas ON cas.call_id = c.id
            WHERE c.transcript IS NOT NULL
              AND c.started_at >= NOW() - INTERVAL '1 day' * :days
              AND cas.id IS NULL
              AND c.caller_number NOT IN (
                  SELECT phone_number FROM call_phone_blacklist
              )
              AND c.callee_number NOT IN (
                  SELECT phone_number FROM call_phone_blacklist
              )
            ORDER BY c.started_at DESC
            LIMIT :lim
        """), {"lim": limit, "days": days_back}).fetchall()

        call_ids = [r[0] for r in rows]
        total = len(call_ids)
        logger.info("call_scorer batch: %d calls to score (days_back=%d)", total, days_back)

        ok_count = 0
        fail_count = 0
        for call_id in call_ids:
            result = score_call(call_id)
            if result.get("ok"):
                ok_count += 1
            else:
                fail_count += 1
            if progress_cb:
                try:
                    progress_cb(ok_count, fail_count, total)
                except Exception:
                    pass

        return {
            "ok": True,
            "total": total,
            "scored": ok_count,
            "failed": fail_count,
        }
    except Exception:
        logger.exception("call_scorer: score_batch failed")
        return {"ok": False, "reason": "exception", "scored": 0}
    finally:
        db.close()
