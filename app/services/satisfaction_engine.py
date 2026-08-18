"""Motor de scor satisfacție client — V6, traiectorie IRIS pe transcript (singura versiune).

Un singur KPI: starea finală 0-100 (sau N/A), estimată de IRIS după promptul
`prompts/satisfaction_trajectory_v6.txt`, pe interacțiunile reale ale clientului.

  * 1 apel IRIS per săptămână ISO cu interacțiuni, cu `stare_initiala` transmisă explicit:
    prima săptămână pornește din ultima lună cu scor (`client_satisfaction_snapshots`),
    fiecare săptămână următoare din starea în care s-a încheiat cea precedentă.
  * Scorul lunii = starea finală a ultimei săptămâni scorate (recența decide).
  * Sursa datelor: `cts_ground_truth` (mailuri) + `cts_calls_ground_truth` (apeluri),
    pe luna calendaristică [month_start, month_end).

Motoarele vechi (v1/v2 pe piloni, v3 „AI holistic", agregarea V4 pe medie) au fost eliminate —
o singură cale de încadrare, configurabilă din settings key `satisfaction.v6`.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.services.category_classifier import _email_body

logger = logging.getLogger("mailguard.satisfaction")


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _first(row):
    if row is None:
        return None
    if hasattr(row, "values"):
        return list(row.values())[0]
    return row[0]


def _row_get(row, *keys):
    if hasattr(row, "keys"):
        return tuple(row.get(k) for k in keys)
    return tuple(row[i] for i in range(len(keys)))


# ── PILON A: Emoție ───────────────────────────────────────────────────────────


# ── PILON B: Efort client ─────────────────────────────────────────────────────


# ── PILON C: Operațional ──────────────────────────────────────────────────────


# ── PILON D: Relație ──────────────────────────────────────────────────────────


# ── Red flags ────────────────────────────────────────────────────────────────


def _segment(score: float) -> str:
    if score >= 70:
        return "sanatos"
    if score >= 45:
        return "neutru"
    if score >= 25:
        return "la_risc"
    return "critic"


# ══════════════════════════════════════════════════════════════════════════════
# MOTOR V6 — traiectorie IRIS (prompt V6, 2026-08) — SINGURUL motor de încadrare
#
# Un singur KPI: starea finală 0-100 (sau N/A) din promptul de traiectorie V6.
# Fără piloni Emoție/Context/Restituire. Fără boost/floor pe scorul IRIS.
# Motoarele vechi (v1/v2 pe 4-5 piloni, v3 „AI holistic") au fost eliminate — o singură
# versiune de încadrare, ca rezultatele să nu depindă de ce cale a apelat cine.
#
# Sursa datelor: cts_ground_truth (mailuri) + cts_calls_ground_truth (apeluri),
#   pe luna calendaristică [month_start, month_end).
# ══════════════════════════════════════════════════════════════════════════════

_TRAJECTORY_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "satisfaction_trajectory_v6.txt"
_TRAJECTORY_SYSTEM_CACHE: Optional[str] = None

# Punctul de start implicit când clientul nu are istoric cunoscut (promptul V6, principiul 17).
_NEUTRAL_START = 50.0

# Config motor (cheile numerice legacy sunt neutre — nu se mai ponderează piloni)
_V6_DEFAULTS = {
    "pen_sesizare": 10.0,
    "pen_reclamatie": 20.0,
    "pen_recontact": 5.0,
    "w_emotion": 0.0,
    "w_context": 1.0,
    "recovery_max": 0.0,
    "mode": "iris_trajectory_v6",
    "prompt_version": "V6",
    # V6 — traiectoria e continuă: fiecare săptămână pornește din starea în care s-a
    # încheiat cea precedentă, iar prima săptămână din starea finală a lunii anterioare.
    "carry_start_state": True,
    "start_lookback_months": 3.0,
    # "last_week_final" = starea finală a ultimei săptămâni scorate (recența decide, principiul 1);
    # "weighted_avg_weeks" = comportamentul V4 (medie ponderată pe interacțiuni), păstrat ca revert
    # fără redeploy din settings key `satisfaction.v6`.
    "month_aggregation": "last_week_final",
    # Modelul care rulează promptul de traiectorie. Până la V6 se folosea implicitul
    # gateway-ului (Claude Haiku 4.5) — prea grosier pentru distincțiile din V6 (cele trei
    # niveluri de mulțumire, plafon interzis). Se schimbă din settings fără redeploy.
    "model_hint": "claude-sonnet-4-6",
}


def _load_trajectory_system() -> str:
    global _TRAJECTORY_SYSTEM_CACHE
    if _TRAJECTORY_SYSTEM_CACHE is None:
        _TRAJECTORY_SYSTEM_CACHE = _TRAJECTORY_PROMPT_PATH.read_text(encoding="utf-8")
    return _TRAJECTORY_SYSTEM_CACHE

# Numere interne CargoTrack (nu sunt clienți) — se ignoră la maparea apel→client
_CARGOTRACK_PHONE_PREFIXES = ("037443006",)


def _load_v6_config(cur) -> dict:
    """Config motor din settings, cheia `satisfaction.v6` (singura citită).

    Permite, fără redeploy: agregarea lunii (`month_aggregation`), reportarea stării de start
    (`carry_start_state`, `start_lookback_months`) și modelul IRIS (`model_hint`).
    """
    cfg = dict(_V6_DEFAULTS)
    try:
        cur.execute("SELECT value FROM settings WHERE key = %s", ("satisfaction.v6",))
        row = cur.fetchone()
        if row:
            val = _first(row)
            if isinstance(val, str):
                val = json.loads(val)
            if isinstance(val, dict):
                for k in cfg:
                    if k in val and val[k] is not None:
                        if isinstance(cfg[k], bool):
                            v = val[k]
                            cfg[k] = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
                            continue
                        if isinstance(cfg[k], str):
                            cfg[k] = str(val[k])
                            continue
                        try:
                            cfg[k] = float(val[k])
                        except (TypeError, ValueError):
                            pass
                # păstrează și chei noi (mode/prompt_version) chiar dacă nu sunt în defaults numerice
                for k in ("mode", "prompt_version", "single_kpi", "month_aggregation", "model_hint"):
                    if k in val and val[k] is not None:
                        cfg[k] = val[k]
    except Exception:
        logger.warning("satisfaction v6: nu am putut citi settings satisfaction.v6, folosesc defaults", exc_info=True)
    return cfg


def _norm_category(cat) -> str:
    """Normalizează cts_category la {informatie, sesizare, reclamatie}. Gol/necunoscut → informatie."""
    c = (cat or "").strip().lower()
    if c in ("sesizare", "reclamatie"):
        return c
    return "informatie"  # informatie, necunoscut, gol, orice altceva = neutru


def _email_domain(addr: str) -> str:
    a = (addr or "").strip().lower()
    if "@" in a:
        return a.rsplit("@", 1)[-1]
    return ""


def _client_email_domains(cur, client_id: int) -> set:
    """Domeniile de email ale unui client (din clients.emails, jsonb murdar cu ';' multiple)."""
    domains = set()
    try:
        cur.execute("SELECT emails FROM clients WHERE id = %s", (client_id,))
        row = cur.fetchone()
        if not row:
            return domains
        emails = _first(row)
        if isinstance(emails, str):
            try:
                emails = json.loads(emails)
            except Exception:
                emails = [emails]
        if not isinstance(emails, list):
            return domains
        for entry in emails:
            # Fiecare element poate conține mai multe adrese lipite cu ';'
            for part in str(entry).split(";"):
                d = _email_domain(part)
                # Ignoră domenii free-mail generice și domenii interne (nu identifică unic un client)
                _GENERIC_DOMAINS = {
                    "gmail.com", "yahoo.com", "yahoo.ro", "yahoo.es", "yahoo.it", "yahoo.fr",
                    "hotmail.com", "hotmail.ro", "hotmail.it", "hotmail.fr",
                    "outlook.com", "outlook.ro",
                    "icloud.com", "me.com", "mac.com",
                    "mail.ru", "mail.com", "ymail.com",
                    "live.com", "live.ro", "msn.com",
                    "protonmail.com", "proton.me",
                    "cargotrack.ro", "trakosoft.ro",  # domenii interne — nu identifică clientul
                }
                if d and d not in _GENERIC_DOMAINS:
                    domains.add(d)
    except Exception:
        logger.warning("satisfaction v6: _client_email_domains eroare client_id=%s", client_id, exc_info=True)
    return domains


def _client_email_addresses(cur, client_id: int) -> set:
    """Adresele EXACTE ale clientului din clients.emails (jsonb, poate avea ';' multiple).

    Înlocuiește potrivirea pe DOMENIU la legarea mailurilor orfane. Motiv: un domeniu de firmă
    apare frecvent la mai mulți clienți CTS — pe staging, 171 de domenii sunt partajate între 646
    de clienți (ex. `ruptela.com` la 8 clienți, printre care unul cu 0 mailuri proprii). Cu
    potrivire pe domeniu, fiecare dintre ei primea mailurile tuturor celorlalți, de unde
    „am analizat 54 de interacțiuni" pentru un client care are 10.

    Adresa exactă e la fel de bună ca sursă de legare, fără contaminare între clienți.
    """
    addresses = set()
    try:
        # Doar adresele care identifica UNIC clientul, din tabela derivata `client_unique_emails`
        # (vezi migratia 20260729h). In CTS multe adrese sunt puse pe mai multi clienti: furnizori
        # (`support@ruptela.com` la 8), banci (`no-reply@unicredit.ro` la 6), sau text liber in loc
        # de adresa (`dispecer` la 37, `sotia` la 27) — o adresa partajata nu identifica pe nimeni.
        cur.execute(
            "SELECT email FROM client_unique_emails WHERE client_id = %s",
            (client_id,),
        )
        for row in cur.fetchall():
            a = _first(row)
            if a:
                addresses.add(str(a))
    except Exception:
        logger.warning("satisfaction v6: _client_email_addresses eroare client_id=%s",
                       client_id, exc_info=True)
    return addresses


def _fetch_month_interactions(client_id: int, cur, start: datetime, end: datetime) -> Tuple[List[dict], List[dict]]:
    """Interacțiunile clientului DIN LUNĂ (calendaristic), din CTS ground-truth.

    Returnează (received, sent):
      received = mailuri+apeluri PRIMITE de la client (folosite pentru penalizări + IRIS)
      sent     = mailuri trimise de agent (context pentru restituire — 'agentul a răspuns rezolvat')

    Legare mail↔client: emails.client_id SAU adresa expeditorului ∈ adresele EXACTE ale clientului
      (nu pe domeniu — domeniile partajate contaminau clienții între ei; vezi
      `_client_email_addresses`).
    Legare apel↔client: calls.client_id SAU phone_match pe numărul non-CargoTrack.
    """
    received: List[dict] = []
    sent: List[dict] = []
    addresses = _client_email_addresses(cur, client_id)

    # ── Mailuri primite (received) ────────────────────────────────────────────
    # Prinde mailurile cu client_id setat + orfanele de la o adresă declarată a clientului.
    try:
        addr_list = list(addresses)
        cur.execute(
            """
            SELECT * FROM (
                SELECT e.id, gt.cts_category, gt.cts_thread_key, e.subject,
                       e.body_text, e.body_html, e.received_at,
                       gt.cts_solved_at, gt.cts_reply_at, e.from_address
                FROM cts_ground_truth gt
                JOIN emails e ON e.id = gt.email_id
                WHERE COALESCE(gt.cts_direction, 'received') = 'received'
                  AND e.received_at >= %s AND e.received_at < %s
                  AND (
                        e.client_id = %s
                     OR (e.client_id IS NULL AND %s <> '{}'::text[]
                         AND LOWER(TRIM(e.from_address)) = ANY(%s))
                  )
                ORDER BY e.received_at DESC
                LIMIT 300
            ) sub
            ORDER BY received_at ASC
            """,
            (start, end, client_id, addr_list, addr_list),
        )
        for row in cur.fetchall():
            (eid, cat, thread, subject, body_text, body_html, received_at, solved_at, reply_at, from_addr) = _row_get(
                row, "id", "cts_category", "cts_thread_key", "subject", "body_text", "body_html",
                "received_at", "cts_solved_at", "cts_reply_at", "from_address"
            )
            # Doar mesajul NOU (ultimul reply), fara istoricul citat din thread — altfel un thread
            # lung (Re:Re:Re...) contamineaza analiza AI cu texte VECHI, iar un singur follow-up
            # scurt/neutru ("multumesc", "am inteles") e etichetat gresit drept "revenire pe
            # problema nerezolvata" doar pentru ca body-ul brut contine si plangerea veche citata.
            body = _email_body({"body_text": body_text, "body_html": body_html})[:600]
            received.append({
                "kind": "email",
                "ref": f"mail#{eid}",
                "category": _norm_category(cat),
                "thread_key": thread or "",
                "subject": subject or "",
                "text": (str(body or "")).strip(),
                "date": str(received_at)[:19] if received_at else "",
                "occurred_at": received_at,
                "solved_at": str(solved_at)[:19] if solved_at else None,
                "reply_at": str(reply_at)[:19] if reply_at else None,
            })
    except Exception:
        logger.warning("satisfaction v6: fetch mailuri received eroare client_id=%s", client_id, exc_info=True)

    # ── Mailuri trimise de agent (sent) — context restituire ────────────────────
    try:
        addr_list = list(addresses)
        cur.execute(
            """
            SELECT e.id, gt.cts_thread_key, e.subject,
                   LEFT(COALESCE(gt.cts_reply_text, ''), 500) AS reply_preview,
                   COALESCE(gt.cts_reply_at, e.received_at) AS sent_at
            FROM cts_ground_truth gt
            JOIN emails e ON e.id = gt.email_id
            WHERE gt.cts_direction = 'sent'
              AND COALESCE(gt.cts_reply_at, e.received_at) >= %s
              AND COALESCE(gt.cts_reply_at, e.received_at) < %s
              AND (
                    e.client_id = %s
                 OR (e.client_id IS NULL AND %s <> '{}'::text[]
                     AND EXISTS (
                         SELECT 1 FROM jsonb_array_elements_text(COALESCE(e.to_addresses, '[]'::jsonb)) AS addr
                         WHERE LOWER(TRIM(addr)) = ANY(%s)
                     ))
              )
            ORDER BY sent_at ASC
            LIMIT 150
            """,
            (start, end, client_id, addr_list, addr_list),
        )
        for row in cur.fetchall():
            (eid, thread, subject, reply, sent_at) = _row_get(
                row, "id", "cts_thread_key", "subject", "reply_preview", "sent_at"
            )
            txt = (str(reply or "")).strip()
            if not txt:
                continue
            sent.append({
                "kind": "email_agent",
                "ref": f"reply#{eid}",
                "thread_key": thread or "",
                "subject": subject or "",
                "text": txt,
                "date": str(sent_at)[:19] if sent_at else "",
            })
    except Exception:
        logger.warning("satisfaction v6: fetch mailuri sent eroare client_id=%s", client_id, exc_info=True)

    # ── Apeluri (received = orice apel al clientului; direcția e informativă) ────
    try:
        cur.execute(
            """
            -- DISTINCT ON (c.id): un apel poate avea MAI MULTE rânduri în
            -- cts_calls_ground_truth (6 cazuri pe staging), iar LEFT JOIN-ul îl returna o dată
            -- per rând → apelul se număra dublu în interacțiunile analizate.
            SELECT DISTINCT ON (c.id)
                   c.id, gt.cts_category, c.direction, c.started_at,
                   LEFT(c.transcript, 600) AS transcript_preview,
                   c.caller_number, c.callee_number, c.client_id
            FROM calls c
            LEFT JOIN cts_calls_ground_truth gt ON gt.call_local_id = c.id
            WHERE c.started_at >= %s AND c.started_at < %s
              AND c.client_id = %s
            ORDER BY c.id, gt.cts_category NULLS LAST
            LIMIT 150
            """,
            (start, end, client_id),
        )
        call_rows = cur.fetchall()
    except Exception:
        call_rows = []
        logger.warning("satisfaction v6: fetch apeluri eroare client_id=%s", client_id, exc_info=True)

    # Apeluri orfane (client_id NULL) — mapare prin telefon
    orphan_calls = _fetch_orphan_calls_for_client(client_id, cur, start, end)

    for row in list(call_rows):
        (cid, cat, direction, started_at, transcript, caller, callee, c_client) = _row_get(
            row, "id", "cts_category", "direction", "started_at", "transcript_preview",
            "caller_number", "callee_number", "client_id"
        )
        received.append({
            "kind": "call",
            "ref": f"apel#{cid}",
            "category": _norm_category(cat),
            "thread_key": "",  # apelurile nu au thread
            "subject": "",
            "text": (str(transcript or "")).strip(),
            "date": str(started_at)[:19] if started_at else "",
            "occurred_at": started_at,
            "direction": direction or "",
        })
    received.extend(orphan_calls)

    # Sortare cronologică (mai vechi → mai nou) — util pentru IRIS să vadă evoluția
    received.sort(key=lambda x: str(x.get("date") or ""))
    sent.sort(key=lambda x: str(x.get("date") or ""))
    return received, sent


def _fetch_orphan_calls_for_client(client_id: int, cur, start: datetime, end: datetime) -> List[dict]:
    """Apeluri cu client_id NULL a căror număr non-CargoTrack se mapează pe acest client (phone_match)."""
    out: List[dict] = []
    try:
        from app.services import phone_match
    except Exception:
        return out
    # Numerele de telefon ale clientului (clients.phones)
    client_phones = set()
    try:
        cur.execute("SELECT phones FROM clients WHERE id = %s", (client_id,))
        row = cur.fetchone()
        phones = _first(row) if row else None
        if isinstance(phones, str):
            try:
                phones = json.loads(phones)
            except Exception:
                phones = []
        if isinstance(phones, list):
            for p in phones:
                n = phone_match.normalize_phone(str(p))
                if n:
                    client_phones.add(n)
    except Exception:
        pass
    if not client_phones:
        return out
    try:
        cur.execute(
            """
            -- DISTINCT ON (c.id): un apel poate avea mai multe rânduri CTS legate (vezi
            -- comentariul din _fetch_month_interactions) — altfel se numără dublu.
            SELECT DISTINCT ON (c.id)
                   c.id, gt.cts_category, c.direction, c.started_at,
                   LEFT(c.transcript, 600) AS transcript_preview,
                   c.caller_number, c.callee_number
            FROM calls c
            LEFT JOIN cts_calls_ground_truth gt ON gt.call_local_id = c.id
            WHERE c.started_at >= %s AND c.started_at < %s
              AND c.client_id IS NULL
            ORDER BY c.id, gt.cts_category NULLS LAST
            LIMIT 300
            """,
            (start, end),
        )
        for row in cur.fetchall():
            (cid, cat, direction, started_at, transcript, caller, callee) = _row_get(
                row, "id", "cts_category", "direction", "started_at", "transcript_preview",
                "caller_number", "callee_number"
            )
            # Numărul clientului = celălalt capăt față de CargoTrack
            candidates = []
            for num in (caller, callee):
                n = phone_match.normalize_phone(str(num or ""))
                if not n:
                    continue
                if any(n.lstrip("+").startswith(pfx) for pfx in _CARGOTRACK_PHONE_PREFIXES):
                    continue  # număr intern CargoTrack
                candidates.append(n)
            if not any(n in client_phones for n in candidates):
                continue
            out.append({
                "kind": "call",
                "ref": f"apel#{cid}",
                "category": _norm_category(cat),
                "thread_key": "",
                "subject": "",
                "text": (str(transcript or "")).strip(),
                "date": str(started_at)[:19] if started_at else "",
                "occurred_at": started_at,
                "direction": direction or "",
            })
    except Exception:
        logger.warning("satisfaction v6: orphan calls eroare client_id=%s", client_id, exc_info=True)
    return out


# ── Prompturi IRIS v4 ──────────────────────────────────────────────────────────


def _salvage_satisfaction_json(text: str) -> Optional[dict]:
    """Recuperează câmpurile esențiale din JSON IRIS trunchiat (max_tokens)."""
    import re
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass

    # Închide array/obiecte trunchiate: taie la ultimul obiect complet din trajectory_events
    cut = t
    # încearcă să închidă JSON-ul după ultimul `},` complet din events
    idx = cut.rfind("},\n")
    if idx < 0:
        idx = cut.rfind("},")
    if idx > 0 and '"trajectory_events"' in cut[:idx]:
        candidate = cut[: idx + 1] + "], \"reputation_risks\": [], \"escalation_risks\": [], \"financial_risk\": null, \"reasoning\": null, \"suggestions\": []}"
        # dacă reasoning există deja mai sus, ok; altfel null
        try:
            # balanță acolade brute
            opens = candidate.count("{") - candidate.count("}")
            if opens > 0:
                candidate += "}" * opens
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    out: Dict[str, Any] = {}
    m = re.search(r'"satisfaction_pct"\s*:\s*(null|-?\d+(?:\.\d+)?)', t)
    if m:
        out["satisfaction_pct"] = None if m.group(1) == "null" else float(m.group(1))

    def _str_field(name: str) -> Optional[str]:
        mm = re.search(rf'"{name}"\s*:\s*"((?:\\.|[^"\\])*)"', t)
        if not mm:
            return None
        try:
            return json.loads('"' + mm.group(1) + '"')
        except Exception:
            return mm.group(1)

    for key in ("no_score_label", "no_score_note", "category", "trajectory_shape", "reasoning"):
        val = _str_field(key)
        if val is not None:
            out[key] = val
    # dacă reasoning lipsește, folosește no_score_note
    if not out.get("reasoning") and out.get("no_score_note"):
        out["reasoning"] = out["no_score_note"]
    out.setdefault("trajectory_events", [])
    out.setdefault("reputation_risks", [])
    out.setdefault("escalation_risks", [])
    out.setdefault("suggestions", [])
    if any(k in out for k in ("satisfaction_pct", "no_score_label", "reasoning", "category")):
        out["_salvaged"] = True
        return out
    return None


def _iris_call(system: str, payload: dict, max_tokens: int = 500,
               model_hint: Optional[str] = None) -> Optional[dict]:
    """Apel IRIS cu JSON. Returnează dict-ul parsat sau None dacă IRIS indisponibil/eșec.

    Dacă gateway-ul întoarce JSON_PARSE_ERROR pe răspuns trunchiat (max_tokens),
    încearcă recuperarea câmpurilor esențiale din raw_text.
    """
    try:
        from app.services import iris_ai
    except Exception:
        return None
    if not iris_ai or not iris_ai.is_configured():
        return None
    try:
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        resp = iris_ai.run_prompt(
            system=system,
            content=content,
            response_format="json",
            temperature=0.1,
            max_tokens=max_tokens,
            model_hint=model_hint or None,
            client="Cargo360-SatisfactionV6",
            no_cache=True,
            task="satisfaction_v6_trajectory",
        )
        if resp and resp.get("ok"):
            parsed = resp.get("parsed")
            if not isinstance(parsed, dict) and resp.get("text"):
                parsed = _salvage_satisfaction_json(resp.get("text") or "")
            return parsed if isinstance(parsed, dict) else None

        # eșec oficial — încearcă salvage din raw_text (caz tipic: output tăiat la max_tokens)
        err = (resp or {}).get("error") or {}
        raw = err.get("raw_text") or (resp or {}).get("text") or ""
        salvaged = _salvage_satisfaction_json(raw)
        if salvaged:
            logger.warning(
                "satisfaction v6: JSON IRIS invalid/trunchiat — recuperat câmpuri esențiale (code=%s keys=%s)",
                err.get("code"),
                sorted(k for k in salvaged.keys() if not k.startswith("_")),
            )
            return salvaged
        if err:
            logger.warning(
                "satisfaction v6: apel IRIS eșuat code=%s msg=%s",
                err.get("code"),
                str(err.get("message") or "")[:200],
            )
    except Exception:
        logger.warning("satisfaction v6: apel IRIS eșuat", exc_info=True)
    return None


def _previous_month_state(cur, client_id: int, month_start: datetime, lookback: int = 3) -> Tuple[Optional[float], Optional[str]]:
    """Starea finală cunoscută dinaintea lunii curente (prompt V6, principiul 17).

    Caută în `client_satisfaction_snapshots` ultima lună cu scor, mergând înapoi cel mult
    `lookback` luni. Fără istoric → (None, None), iar apelantul pornește de la neutru (50).
    Repornirea de la neutru la fiecare lună ștergea continuitatea: un client lăsat nemulțumit în
    iunie apărea în iulie ca și cum relația ar fi început de la zero.
    """
    try:
        y, m = month_start.year, month_start.month
        keys = []
        for _ in range(max(1, int(lookback))):
            m -= 1
            if m == 0:
                m, y = 12, y - 1
            keys.append(f"{y:04d}-{m:02d}")
        cur.execute(
            """
            SELECT month_key, satisfaction_pct
            FROM client_satisfaction_snapshots
            WHERE client_id = %s AND month_key = ANY(%s) AND satisfaction_pct IS NOT NULL
            ORDER BY month_key DESC
            LIMIT 1
            """,
            (client_id, keys),
        )
        row = cur.fetchone()
        if not row:
            return None, None
        mk, pct = _row_get(row, "month_key", "satisfaction_pct")
        if pct is None:
            return None, None
        return round(_clamp(float(pct)), 1), f"snapshot lunar {mk}"
    except Exception:
        logger.warning("satisfaction v6: _previous_month_state eroare client_id=%s", client_id, exc_info=True)
        return None, None


def _iris_payload_interactions(received: List[dict], sent: List[dict] = None, *, text_limit: int = 600) -> list:
    """Serializează interacțiunile pentru IRIS (câmpuri relevante, text trunchiat)."""
    out = []
    for i in received:
        out.append({
            "ref": i.get("ref"),
            "tip": i.get("kind"),
            "categorie": i.get("category"),
            "subiect": i.get("subject") or None,
            "thread": i.get("thread_key") or None,
            "data": i.get("date"),
            "text": (i.get("text") or "")[:text_limit],
        })
    if sent:
        sent_limit = max(500, text_limit - 100)
        for s in sent:
            out.append({
                "ref": s.get("ref"),
                "tip": "raspuns_agent",
                "subiect": s.get("subject") or None,
                "thread": s.get("thread_key") or None,
                "data": s.get("date"),
                "text": (s.get("text") or "")[:sent_limit],
            })
    return out


def compute_satisfaction_v6(
    client_id: int,
    iris_client_id: Optional[int],
    cur,
    month_start: datetime,
    month_end: datetime,
    *,
    use_ai: bool = True,
    skip_exclude_check: bool = False,
) -> dict:
    """Scor satisfacție v6 — 1 apel IRIS per săptămână cu interacțiuni; traiectorie continuă.

    Diferă de v4 prin două lucruri, ambele cerute de promptul V6:
      * fiecare săptămână primește un punct de start (`stare_initiala`) — prima săptămână
        din starea finală a ultimei luni cu scor, restul din starea săptămânii precedente;
      * scorul lunii = starea finală a ultimei săptămâni scorate (recența decide), nu media
        ponderată pe interacțiuni. Media rămâne calculată și expusă în breakdown pentru
        comparație, iar `month_aggregation="weighted_avg_weeks"` în settings readuce v4.
    """
    import time as _time
    import re

    now = month_end
    cfg = _load_v6_config(cur)
    config_used = {
        "version": "v6_trajectory",
        "prompt_version": str(cfg.get("prompt_version") or "V6"),
        "weights": cfg,
        "granularity": "week_iris_chained",
        "no_cache": True,
    }

    if not skip_exclude_check:
        try:
            cur.execute("SELECT satisfaction_exclude FROM clients WHERE id = %s", (client_id,))
            row = cur.fetchone()
            if row and _first(row):
                return {
                    "satisfaction_pct": None,
                    "is_unsatisfied": False,
                    "breakdown": {"scoring_mode": "excluded"},
                    "config_used": config_used,
                    "computed_at": now.isoformat(),
                    "error": "excluded",
                }
        except Exception:
            pass

    received, sent = _fetch_month_interactions(client_id, cur, month_start, month_end)
    n = len(received) + len(sent)

    def _na_result(label: str, note: str, *, interactions: int, extra: Optional[dict] = None) -> dict:
        breakdown = {
            "scoring_mode": "v6_trajectory_na",
            "store_null": True,
            "single_kpi": "iris_stare_finala",
            "total_interactions": interactions,
            "segment": "neutru",
            "red_flags_active": [],
            "no_score_label": label,
            "no_score_note": note,
            "iris_reasoning": note,
            "category": label,
            "trajectory_shape": None,
            "trajectory_events": [],
            "weekly_trajectories": [],
            "reputation_risks": [],
            "escalation_risks": [],
            "financial_risk": None,
            "suggestions": [],
            "iris_calls": 0,
            "month_aggregation": "weighted_avg_weeks",
        }
        if extra:
            breakdown.update(extra)
        return {
            "satisfaction_pct": None,
            "is_unsatisfied": False,
            "breakdown": breakdown,
            "config_used": config_used,
            "computed_at": now.isoformat(),
        }

    if n == 0:
        return _na_result(
            "Neutru — fără interacțiune (necesită contact proactiv)",
            "De ce N/A: nicio interacțiune (apel/email) în luna analizată. "
            "Ce se știe: fără semnal pe axa de serviciu sau financiară în fereastra curentă. "
            "Recomandare: contact proactiv sau extinderea ferestrei pentru semnal real.",
            interactions=0,
        )

    if not use_ai:
        return {
            "satisfaction_pct": 75.0,
            "is_unsatisfied": False,
            "breakdown": {
                "scoring_mode": "v6_trajectory_no_ai",
                "single_kpi": "iris_stare_finala",
                "total_interactions": n,
                "segment": "sanatos",
                "iris_reasoning": "AI dezactivat — scor neutru 75.",
                "weekly_trajectories": [],
                "trajectory_events": [],
                "iris_calls": 0,
                "month_aggregation": "weighted_avg_weeks",
            },
            "config_used": config_used,
            "computed_at": now.isoformat(),
        }

    def _item_dt(item: dict) -> Optional[datetime]:
        raw = item.get("occurred_at") or item.get("date")
        if raw is None:
            return None
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        s = str(raw).strip()
        if not s:
            return None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s[:19] if "T" in s[:19] or len(s) >= 19 else s[:10])
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            try:
                return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except Exception:
                return None

    def _week_key(dt: datetime) -> str:
        iso = dt.astimezone(timezone.utc).isocalendar()
        return f"{iso.year}-W{iso.week:02d}"

    def _week_bounds(dt: datetime) -> Tuple[datetime, datetime]:
        d = dt.astimezone(timezone.utc)
        monday = (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return monday, monday + timedelta(days=7)

    def _parse_pct(v) -> Optional[float]:
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return round(_clamp(float(v)), 1)
        s = str(v).strip().replace(",", ".")
        if s.lower() in ("", "null", "none", "n/a", "na"):
            return None
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        if not m:
            return None
        try:
            return round(_clamp(float(m.group(0))), 1)
        except ValueError:
            return None

    # Bucket pe săptămâni ISO (doar cele cu interacțiuni)
    buckets: Dict[str, dict] = {}
    order: List[str] = []
    ms = month_start if month_start.tzinfo else month_start.replace(tzinfo=timezone.utc)
    me = month_end if month_end.tzinfo else month_end.replace(tzinfo=timezone.utc)
    for kind, items in (("received", received), ("sent", sent)):
        for it in items:
            dt = _item_dt(it) or ms
            wk = _week_key(dt)
            if wk not in buckets:
                w0, w1 = _week_bounds(dt)
                buckets[wk] = {
                    "week_key": wk,
                    "start": max(w0, ms),
                    "end": min(w1, me),
                    "received": [],
                    "sent": [],
                }
                order.append(wk)
            buckets[wk][kind].append(it)

    # Ordine strict cronologică: `order` se construia din ordinea de parcurgere a listelor
    # (întâi toate mailurile primite, apoi cele trimise), deci o săptămână care apărea numai
    # în `sent` ajungea la coadă. Cu înlănțuirea stării (V6), ordinea greșită ar transporta
    # starea înapoi în timp.
    order = sorted(buckets, key=lambda k: buckets[k]["start"])

    # Punctul de start al lunii (prompt V6, principiul 17): starea finală a ultimei luni
    # cu scor, dacă există; altfel neutru. Fără asta, o lună calmă a unui client lăsat
    # nemulțumit luna trecută repornea de la 50 și „vindeca" clientul din calcul.
    start_state: Optional[float] = None
    start_source = "neutru implicit (fără istoric cunoscut)"
    if cfg.get("carry_start_state"):
        prev_pct, prev_src = _previous_month_state(
            cur, client_id, ms, lookback=int(cfg.get("start_lookback_months") or 3)
        )
        if prev_pct is not None:
            start_state = prev_pct
            start_source = f"stare reportată: {prev_src}"
    month_start_state = start_state if start_state is not None else _NEUTRAL_START
    chain_state = month_start_state
    chain_source = start_source

    weekly_rows: List[dict] = []
    merged_events: List[dict] = []
    iris_calls = 0
    all_rep: List = []
    all_esc: List = []
    all_sug: List = []
    last_fin = None

    for i, wk in enumerate(order):
        wb = buckets[wk]
        w_recv, w_sent = wb["received"], wb["sent"]
        n_w = len(w_recv) + len(w_sent)
        if n_w == 0:
            continue
        period = {
            "week_key": wk,
            "week_start": wb["start"].date().isoformat(),
            "week_end_exclusive": wb["end"].date().isoformat(),
        }
        payload = {
            "perioada": period,
            "nivel_analiza": "saptamana",
            "instructiune": (
                "Analizează traiectoria de satisfacție DOAR pentru această SĂPTĂMÂNĂ (prompt V6). "
                "Interacțiunile = [TRANSCRIEREA CONVERSAȚIEI]. Fără cache. "
                "Pornește de la `stare_initiala` (principiul 17), nu de la 50, și nu plafona o "
                "recuperare reală sau o mulțumire despre colaborare/recomandare (principiul 18). "
                "JSON COMPACT: trajectory_events maxim 10 (explanation ≤1 propoziție); "
                "satisfaction_pct = starea finală a săptămânii."
            ),
            "stare_initiala": {"valoare": round(chain_state, 1), "sursa": chain_source},
            "total_interactiuni": n_w,
            "interactiuni": _iris_payload_interactions(w_recv, w_sent, text_limit=1400),
        }
        parsed = _iris_call(_load_trajectory_system(), payload, max_tokens=5000,
                            model_hint=str(cfg.get("model_hint") or "") or None)
        iris_calls += 1
        if not parsed:
            row = {
                "week_key": wk,
                "week_start": period["week_start"],
                "week_end_exclusive": period["week_end_exclusive"],
                "n_interactions": n_w,
                "start_state": round(chain_state, 1),
                "start_state_source": chain_source,
                "satisfaction_pct": None,
                "category": "IRIS eșuat pe săptămână",
                "trajectory_shape": None,
                "iris_reasoning": "Apel IRIS eșuat pentru această săptămână.",
                "no_score_label": "IRIS eșuat",
                "trajectory_events": [],
                "iris_ok": False,
            }
        else:
            pct_w = _parse_pct(parsed.get("satisfaction_pct"))
            events = parsed.get("trajectory_events") if isinstance(parsed.get("trajectory_events"), list) else []
            for ev in events:
                if isinstance(ev, dict):
                    ev = dict(ev)
                    ev["week_key"] = wk
                    merged_events.append(ev)
            reasoning = parsed.get("reasoning") or parsed.get("no_score_note") or ""
            row = {
                "week_key": wk,
                "week_start": period["week_start"],
                "week_end_exclusive": period["week_end_exclusive"],
                "n_interactions": n_w,
                "start_state": round(chain_state, 1),
                "start_state_source": chain_source,
                "satisfaction_pct": pct_w,
                "category": parsed.get("category") or parsed.get("no_score_label"),
                "trajectory_shape": parsed.get("trajectory_shape"),
                "iris_reasoning": reasoning,
                "no_score_label": parsed.get("no_score_label"),
                "trajectory_events": events[:20],
                "iris_ok": True,
            }
            if isinstance(parsed.get("reputation_risks"), list):
                all_rep.extend(parsed["reputation_risks"][:5])
            if isinstance(parsed.get("escalation_risks"), list):
                all_esc.extend(parsed["escalation_risks"][:5])
            if isinstance(parsed.get("suggestions"), list):
                all_sug.extend(parsed["suggestions"][:3])
            if isinstance(parsed.get("financial_risk"), dict):
                last_fin = parsed["financial_risk"]
            # Traiectorie continuă: săptămâna următoare pornește din starea în care s-a
            # încheiat aceasta. O săptămână fără scor (N/A sau IRIS eșuat) nu rupe lanțul.
            if pct_w is not None:
                chain_state = pct_w
                chain_source = f"stare reportată: săptămâna {wk}"
        weekly_rows.append(row)
        if i < len(order) - 1:
            _time.sleep(0.25)

    # Luna = medie ponderată pe interacțiuni (fără apel IRIS lunar)
    scored = [(r["satisfaction_pct"], r["n_interactions"]) for r in weekly_rows if r.get("satisfaction_pct") is not None]
    if not scored:
        # toate săptămânile N/A / eșec
        notes = [r.get("iris_reasoning") or r.get("no_score_label") or "" for r in weekly_rows]
        note = " ".join(x for x in notes if x)[:800] or "Nicio săptămână nu a produs scor IRIS."
        return _na_result(
            "Semnal insuficient pentru un scor de satisfacție",
            note,
            interactions=n,
            extra={
                "weekly_trajectories": weekly_rows,
                "trajectory_events": merged_events[:80],
                "iris_calls": iris_calls,
                "reputation_risks": all_rep[:15],
                "escalation_risks": all_esc[:15],
                "financial_risk": last_fin,
                "suggestions": all_sug[:10],
            },
        )

    weight_sum = sum(w for _, w in scored) or 1
    weighted_avg_pct = round(_clamp(sum(p * w for p, w in scored) / weight_sum), 1)
    last_week_pct = next(
        r["satisfaction_pct"] for r in reversed(weekly_rows) if r.get("satisfaction_pct") is not None
    )
    aggregation = str(cfg.get("month_aggregation") or "last_week_final")
    if aggregation == "weighted_avg_weeks":
        month_pct = weighted_avg_pct
    else:
        aggregation = "last_week_final"
        # Recența decide (prompt V6, principiile 1 și 18): media ponderată dilua exact
        # recuperările pe care promptul cere să nu fie plafonate — o criză în săptămâna 1
        # trăgea în jos o lună încheiată cu reconciliere confirmată.
        month_pct = round(_clamp(float(last_week_pct)), 1)

    # raționament agregat (nu IRIS lunar)
    parts = []
    for r in weekly_rows:
        if r.get("satisfaction_pct") is None:
            continue
        parts.append(
            f"{r['week_key']}: {r['satisfaction_pct']}% ({r['n_interactions']} interacțiuni)"
            + (f" — {(r.get('iris_reasoning') or '')[:160]}" if r.get("iris_reasoning") else "")
        )
    if aggregation == "last_week_final":
        head = (
            f"Scor lunar = starea finală a ultimei săptămâni scorate ({month_pct}%; "
            f"{len(scored)} săptămâni scorate, {weight_sum} interacțiuni; "
            f"medie ponderată pentru comparație: {weighted_avg_pct}%). "
            f"Punct de start lună: {month_start_state}% ({start_source}). "
        )
    else:
        head = (
            f"Scor lunar = medie ponderată pe interacțiuni din scorurile săptămânale IRIS "
            f"({month_pct}%; {len(scored)} săptămâni scorate, {weight_sum} interacțiuni). "
            f"Punct de start lună: {month_start_state}% ({start_source}). "
        )
    reasoning = (head + " | ".join(parts))[:1200]

    # Sinteză lunară AI — apel suplimentar după scorarea săptămânilor.
    # Primește scorurile + reasoning-urile săptămânale și returnează un rezumat acționabil
    # max 3 propoziții pentru iris_reasoning afișat în UI.
    # Dacă apelul eșuează sau AI nu e configurat, rămâne reasoning-ul programatic (fallback).
    _summary_system = (
        "Ești un analist de satisfacție clienți B2B. Primești scorurile săptămânale ale unui client "
        "și raționamentele lor pentru o lună. Generează un rezumat lunar de maxim 3 propoziții, "
        "orientat pe acțiune:\n"
        "1. Starea generală a clientului în această lună și principalele probleme sau aspecte pozitive identificate.\n"
        "2. Trendul față de startul lunii sau față de luna anterioară dacă există.\n"
        "3. Cel mai important risc sau oportunitate de acțiune imediată pentru echipă.\n"
        "NU include calcule, ponderi, numere de apeluri sau explicații metodologice. "
        'Răspunde JSON: {"iris_reasoning": "<rezumat>"}'
    )
    _summary_payload = {
        "scor_final_luna": month_pct,
        "scor_start_luna": month_start_state,
        "sursa_start": start_source,
        "saptamani": [
            {
                "saptamana": r["week_key"],
                "scor": r.get("satisfaction_pct"),
                "n_interactiuni": r.get("n_interactions"),
                "reasoning": (r.get("iris_reasoning") or "")[:300],
            }
            for r in weekly_rows
        ],
        "riscuri_reputatie": all_rep[:5],
        "riscuri_escaladare": all_esc[:3],
    }
    _summary_resp = _iris_call(_summary_system, _summary_payload, max_tokens=400)
    if (
        _summary_resp
        and isinstance(_summary_resp.get("iris_reasoning"), str)
        and _summary_resp["iris_reasoning"].strip()
    ):
        reasoning = _summary_resp["iris_reasoning"].strip()
        iris_calls += 1

    # categorie din scorul mediu
    if month_pct >= 90:
        category = "Ambasador"
    elif month_pct >= 75:
        category = "Foarte satisfăcut"
    elif month_pct >= 60:
        category = "Satisfăcut"
    elif month_pct >= 45:
        category = "Neutru / satisfacție moderată — recomandat follow-up"
    elif month_pct >= 30:
        category = "Nemulțumit — necesită intervenție"
    else:
        category = "Critic / risc de pierdere a clientului"

    shapes = [r.get("trajectory_shape") for r in weekly_rows if r.get("trajectory_shape")]
    shape = shapes[-1] if shapes else "Agregat săptămânal"

    segment = _segment(month_pct)
    breakdown = {
        "scoring_mode": "v6_trajectory",
        "single_kpi": "iris_stare_finala",
        "total_interactions": n,
        "segment": segment,
        "red_flags_active": [],
        "iris_reasoning": reasoning,
        "category": category,
        "trajectory_shape": shape,
        "trajectory_events": merged_events[:80],
        "weekly_trajectories": weekly_rows,
        "no_score_label": None,
        "no_score_note": None,
        "reputation_risks": all_rep[:15],
        "escalation_risks": all_esc[:15],
        "financial_risk": last_fin,
        "suggestions": all_sug[:10],
        "iris_calls": iris_calls,
        "month_start_state": month_start_state,
        "month_start_source": start_source,
        "month_aggregation": aggregation,
        "month_avg_detail": {
            "weeks_scored": len(scored),
            "weight_interactions": weight_sum,
            "last_week_final_pct": last_week_pct,
            "weighted_avg_pct": weighted_avg_pct,
            "formula": ("starea finală a ultimei săptămâni scorate"
                        if aggregation == "last_week_final"
                        else "sum(week_pct * n_interactions) / sum(n_interactions)"),
        },
        "iris_holistic": {
            "reasoning": reasoning,
            "dominant_signal": category,
            "trend_assessment": shape or "",
        },
    }
    return {
        "satisfaction_pct": month_pct,
        "is_unsatisfied": month_pct < 70.0,
        "breakdown": breakdown,
        "config_used": config_used,
        "computed_at": now.isoformat(),
    }
