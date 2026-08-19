"""Modul „Mail-uri CTS" — comparatie ground-truth CTS vs clasificare Cargo360 (training).

Read-only fata de fluxul curent: NU re-trimite nimic spre CTS, NU modifica `emails`. Citeste
`cts_ground_truth` (categorie/departament setate MANUAL in CTS + reply-ul trimis de colegi) si
le compara cu ai_category / ai_department / ai_autoreply ale Cargo360. Scop: sa vedem unde MG
greseste (mai ales pe DEPARTAMENT, azi <80%) si sa ajustam prompturile.

Sursa de date e populata de app/services/cts_groundtruth_sync (inert pana la grant cross-app
de la Razvan via outbox) sau de un fixture sintetic pt verificare.
"""
import logging
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Body
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.api.v1.auth import get_current_admin
from app.api.v1.sorting import sort_dir
from app.services import cts_groundtruth_sync as SYNC
from app.services.department_classifier import DEPT_LABELS

logger = logging.getLogger("mailguard.cts_training")
router = APIRouter()

SYNC_ENABLED_KEY = "cts_gt.sync_enabled"


def _conf(result) -> float:
    """Extrage confidence dintr-un jsonb result (dict) -> 0..1 sau None."""
    try:
        if isinstance(result, dict) and result.get("confidence") is not None:
            return round(float(result["confidence"]), 4)
    except (TypeError, ValueError):
        pass
    return None


@router.get("/cts-training/list")
def cts_training_list(
    only_mismatch: int = Query(0),
    axis: str = Query("", description="'', 'category', 'department', 'reply'"),
    department: str = Query(""),
    dept_from: str = Query("", description="filtru departament MG (sursa)"),
    dept_to: str = Query("", description="filtru departament CTS (destinatie)"),
    date_from: str = Query("", description="data start YYYY-MM-DD"),
    date_to: str = Query("", description="data sfarsit YYYY-MM-DD"),
    direction: str = Query("", description="'', 'received', 'sent'"),
    assignee: str = Query("", description="filtru utilizator (email assignee CTS, gol = toti)"),
    status: str = Query("", description="filtru status CTS ('new'|'in progress'|'solved')"),
    search_id: str = Query("", description="cauta dupa ID email (gt.id sau e.id)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    dir: str = Query("desc", description="ordonare dupa data: 'asc' | 'desc'"),
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Lista comparativa MG vs CTS, cu filtre (doar divergente / departament / axa / data)."""
    where = ["gt.cts_deleted_at IS NULL"]  # ascunde emailurile sterse in CTS
    params = {}

    if department:
        where.append("(gt.cts_department = :dep OR e.ai_department = :dep)")
        params["dep"] = department

    if dept_from:
        where.append("e.ai_department = :dept_from")
        params["dept_from"] = dept_from

    if dept_to:
        where.append("gt.cts_department = :dept_to")
        params["dept_to"] = dept_to

    if date_from:
        try:
            from datetime import date
            date.fromisoformat(date_from)
            where.append("COALESCE(e.received_at, gt.cts_reply_at, gt.changed_at) >= CAST(:date_from AS date)")
            params["date_from"] = date_from
        except ValueError:
            pass

    if date_to:
        try:
            from datetime import date
            date.fromisoformat(date_to)
            where.append("COALESCE(e.received_at, gt.cts_reply_at, gt.changed_at) < (CAST(:date_to AS date) + interval '1 day')")
            params["date_to"] = date_to
        except ValueError:
            pass

    if direction in ("received", "sent"):
        where.append("COALESCE(gt.cts_direction,'received') = :direction")
        params["direction"] = direction

    # Utilizator = cine a preluat mailul in CTS (adevarul de teren), nu sugestia AI: filtrul
    # raspunde la "ce a lucrat X", nu la "ce i-am fi dat lui X".
    if assignee.strip():
        where.append("lower(gt.cts_assignee_email) = lower(:assignee)")
        params["assignee"] = assignee.strip()

    if status.strip():
        where.append("lower(gt.cts_status) = lower(:status)")
        params["status"] = status.strip()

    if search_id.strip().isdigit():
        where.append("(gt.email_id = :search_id OR gt.id = :search_id)")
        params["search_id"] = int(search_id.strip())

    # divergenta: comparabil (ambele non-null) si diferit
    cat_diff = "(gt.cts_category IS NOT NULL AND e.ai_category IS NOT NULL AND gt.cts_category <> e.ai_category)"
    dep_diff = "(gt.cts_department IS NOT NULL AND e.ai_department IS NOT NULL AND gt.cts_department <> e.ai_department)"
    asg_diff = "(gt.cts_assignee_email IS NOT NULL AND e.ai_assignee IS NOT NULL AND lower(gt.cts_assignee_email) <> lower(e.ai_assignee))"
    if only_mismatch:
        if axis == "category":
            where.append(cat_diff)
        elif axis == "department":
            where.append(dep_diff)
        elif axis == "assignee":
            where.append(asg_diff)
        else:
            where.append("(%s OR %s)" % (cat_diff, dep_diff))

    where_sql = " AND ".join(where)
    _dir = sort_dir(dir)
    total = db.execute(text(
        "SELECT count(*) FROM cts_ground_truth gt LEFT JOIN emails e ON e.id = gt.email_id "
        "WHERE " + where_sql), params).scalar()

    params["lim"] = page_size
    params["off"] = (page - 1) * page_size
    rows = db.execute(text(
        "SELECT gt.id, gt.email_id, gt.message_id, gt.cts_category, gt.cts_department, "
        "       gt.cts_direction, "
        "       gt.cts_reply_text, gt.cts_reply_html, gt.cts_attachments, gt.cts_solved_at, "
        "       gt.cts_thread_key, gt.cts_reply_at, gt.cts_status, gt.raw, gt.fetched_at, "
        "       gt.changed_at, gt.cts_category_prev, gt.cts_department_prev, "
        "       e.subject, e.from_name, e.from_address, e.received_at, "
        "       e.ai_category, e.ai_result, e.ai_department, e.ai_department_result, "
        "       e.ai_autoreply, e.ai_autoreply_confidence, "
        "       e.ai_assignee, gt.cts_assignee_email, gt.cts_assignee_name "
        "FROM cts_ground_truth gt LEFT JOIN emails e ON e.id = gt.email_id "
        # Ordine pe RECENTA REALA a emailului, NU pe fetched_at: sync-ul rescrie fetched_at=now() pe
        # toate randurile la fiecare ciclu, iar pass-ul backfill (vechi) ruleaza dupa cel window (nou)
        # -> pe fetched_at vechile pluteau deasupra si pagina "ingheta" (incident 2026-06-30).
        # received_at (mail primit) -> cts_reply_at (mail trimis) -> changed_at. Randurile fara niciun
        # timp real (GT nematchuit, inactiv) merg jos (NULLS LAST), nu in varf pe fetched_at=now.
        # Directia e din whitelist (_sort_dir), nu din input brut — interpolarea in SQL
        # e sigura doar asa; parametrii legati nu pot tine un ORDER BY.
        "WHERE " + where_sql + f" ORDER BY COALESCE(e.received_at, gt.cts_reply_at, gt.changed_at) {_dir} NULLS LAST, gt.id {_dir} "
        "LIMIT :lim OFFSET :off", ), params).fetchall()

    items = []
    for r in rows:
        m = r._mapping
        ai_cat = m["ai_category"]
        cts_cat = m["cts_category"]
        ai_dep = m["ai_department"]
        cts_dep = m["cts_department"]
        cat_match = (None if (ai_cat is None or cts_cat is None) else (ai_cat == cts_cat))
        dep_match = (None if (ai_dep is None or cts_dep is None) else (ai_dep == cts_dep))
        ai_asg = m["ai_assignee"]
        cts_asg = m["cts_assignee_email"]
        asg_match = (None if (not ai_asg or not cts_asg) else (ai_asg.lower() == cts_asg.lower()))
        unmapped = {}
        extra = {}
        is_spam = False
        try:
            if isinstance(m["raw"], dict):
                unmapped = m["raw"].get("_unmapped") or {}
                extra = m["raw"].get("extra") or {}
                is_spam = bool(m["raw"].get("_spam"))
        except Exception:
            unmapped, extra, is_spam = {}, {}, False
        direction = m["cts_direction"] or "received"
        items.append({
            "id": m["id"],
            "email_id": m["email_id"],
            "in_cargo360": m["email_id"] is not None,
            "message_id": m["message_id"],
            # directie: TRIMIS de noi (reply colegi) vs PRIMIT (mail client de incadrat)
            "direction": direction,
            "is_sent": (direction == "sent"),
            "is_spam": is_spam,
            "cts_from_email": extra.get("from_email"),
            "cts_to_email": extra.get("to_email"),
            # id-ul log-ului CTS (companion /cts/email-content) — corpul emailului trimis lazy
            "cts_email_log_id": extra.get("cts_email_log_id"),
            # subiect/data: la mailurile TRIMISE nu exista rand local -> fallback pe metadatele CTS
            "subject": m["subject"] or extra.get("title"),
            "cts_date": extra.get("email_date"),
            "from_name": m["from_name"],
            "from_address": m["from_address"],
            "received_at": m["received_at"].isoformat() if m["received_at"] else None,
            "cts_status": m["cts_status"],
            "cts_reply_at": m["cts_reply_at"].isoformat() if m["cts_reply_at"] else None,
            "changed_at": m["changed_at"].isoformat() if m["changed_at"] else None,
            "cts_department_prev": m["cts_department_prev"],
            "cts_category_prev": m["cts_category_prev"],
            # categorie
            "ai_category": ai_cat,
            "ai_category_confidence": _conf(m["ai_result"]),
            "cts_category": cts_cat,
            "cts_category_label": cts_cat,
            "cat_match": cat_match,
            "cat_unmapped": bool(unmapped.get("category")),
            # departament
            "ai_department": ai_dep,
            "ai_department_label": DEPT_LABELS.get(ai_dep, ai_dep),
            "ai_department_confidence": _conf(m["ai_department_result"]),
            "cts_department": cts_dep,
            "cts_department_label": DEPT_LABELS.get(cts_dep, cts_dep),
            "dep_match": dep_match,
            "dep_unmapped": bool(unmapped.get("department")),
            # asignare (utilizator)
            "ai_assignee": ai_asg,
            "cts_assignee": cts_asg,
            "cts_assignee_name": m["cts_assignee_name"],
            "asg_match": asg_match,
            # reply (colegi vs sugestie AI)
            "ai_autoreply": m["ai_autoreply"],
            "ai_autoreply_confidence": (round(float(m["ai_autoreply_confidence"]), 4)
                                        if m["ai_autoreply_confidence"] is not None else None),
            "cts_reply_text": m["cts_reply_text"],
            "has_cts_reply": bool(m["cts_reply_text"]),
            # emailul trimis complet (corp html, atasamente, timestamp real, thread)
            "cts_reply_html": m["cts_reply_html"],
            "cts_attachments": m["cts_attachments"],
            "cts_solved_at": m["cts_solved_at"].isoformat() if m["cts_solved_at"] else None,
            "cts_thread_key": m["cts_thread_key"],
        })

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/cts-training/sent-body")
def cts_training_sent_body(log_id: str = Query(..., description="cts_email_log_id din CTS"),
                           refresh: bool = Query(False, description="forțează re-aducerea din CTS"),
                           db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Corpul + atașamentele unui email TRIMIS (reply coleg), aduse LAZY din CTS prin
    IRIS Gateway (/cts/email-content) și CACHED în cts_ground_truth.cts_reply_text /
    cts_attachments. A doua deschidere = instant (cache). Read-only față de CTS; conținutul
    binar al atașamentelor NU e disponibil prin gateway (doar nume/tip/mărime).
    Include și `paired_received`: emailul PRIMIT căruia i s-a răspuns (legat pe msid =
    Message-ID-ul la care răspunde reply-ul) + sugestia AI de reply de pe el (ai_autoreply),
    pentru comparația „ce a zis colegul vs ce ar fi sugerat AI"."""
    row = db.execute(text(
        "SELECT id, cts_reply_text, cts_attachments, raw->'extra'->>'msid' AS msid, "
        "       raw->'extra'->>'to_email' AS to_email, "
        "       raw->'extra'->>'title' AS title, "
        "       raw->'extra'->>'client_id' AS cts_client_id, "
        "       COALESCE(cts_reply_at, cts_solved_at, fetched_at) AS ref_at "
        "FROM cts_ground_truth "
        "WHERE raw->'extra'->>'cts_email_log_id' = :lid "
        "  AND COALESCE(cts_direction,'received') = 'sent' "
        "ORDER BY id DESC LIMIT 1"), {"lid": str(log_id)}).mappings().first()
    if row is None:
        raise HTTPException(404, "Nu există rând CTS trimis pentru acest log_id")

    def _norm_subj(s):
        """Normalizează subiectul: scoate prefixele Re:/Fwd:/Fw:/R: (oricâte, orice limbă uzuală)."""
        s = (s or "").strip()
        prev = None
        while s and s != prev:
            prev = s
            s = re.sub(r'^\s*(re|r|fw|fwd|rspns|răspuns|raspuns)\s*[:\-]\s*', '', s, flags=re.I)
        return s.lower().strip()

    def _shape(e, how):
        if e is None:
            return None
        body = e.get("body_text") or e.get("body_html") or ""
        return {"email_id": e["id"], "subject": e["subject"],
                "from_address": e.get("from_address"), "from_name": e.get("from_name"),
                "received_at": e["received_at"].isoformat() if e.get("received_at") else None,
                "body_text": (body or "")[:20000],
                "body_is_html": bool(e.get("body_text") is None and e.get("body_html")),
                "match_by": how,
                "ai_autoreply": e["ai_autoreply"],
                "ai_autoreply_confidence": (round(float(e["ai_autoreply_confidence"]), 4)
                                            if e["ai_autoreply_confidence"] is not None else None),
                "ai_autoreply_status": e["ai_autoreply_status"]}

    _SEL = ("SELECT id, subject, from_address, from_name, received_at, body_text, body_html, "
            "ai_autoreply, ai_autoreply_confidence, ai_autoreply_status FROM emails ")

    def _paired():
        """Emailul PRIMIT căruia i s-a răspuns + sugestia AI de pe el.
        Strategie: (1) exact pe Message-ID (msid), apoi (2) fallback pe expeditor =
        destinatarul reply-ului + subiect normalizat + primit înainte de reply."""
        # (1) exact pe Message-ID
        if row["msid"]:
            e = db.execute(text(_SEL +
                "WHERE email_headers->>'message_id' = :m ORDER BY id DESC LIMIT 1"),
                {"m": row["msid"]}).mappings().first()
            if e:
                return _shape(e, "msid")
        # (2) fallback: from_address = destinatarul reply-ului, subiect normalizat egal,
        #     primit înaintea trimiterii (cel mai recent dinainte)
        to_email = (row["to_email"] or "").strip().lower()
        norm = _norm_subj(row["title"])
        if to_email and norm:
            cands = db.execute(text(_SEL +
                "WHERE lower(from_address) = :to AND received_at IS NOT NULL "
                "  AND (CAST(:ref AS timestamptz) IS NULL OR received_at <= CAST(:ref AS timestamptz)) "
                "ORDER BY received_at DESC LIMIT 25"),
                {"to": to_email, "ref": row["ref_at"]}).mappings().all()
            for e in cands:
                if _norm_subj(e["subject"]) == norm:
                    return _shape(e, "subject")
        return None

    paired = _paired()

    if row["cts_reply_text"] and not refresh:
        return {"ok": True, "cached": True, "available": True, "log_id": str(log_id),
                "reply_text": row["cts_reply_text"], "attachments": row["cts_attachments"] or [],
                "paired_received": paired}
    try:
        items = SYNC.fetch_email_content([str(log_id)])
    except Exception as e:
        raise HTTPException(502, "Eroare la aducerea din gateway-ul CTS: %s" % e)
    rec = (items or {}).get(str(log_id)) or {}
    reply_text = rec.get("reply_text")
    atts = rec.get("attachments") or []
    if reply_text:
        db.execute(text(
            "UPDATE cts_ground_truth SET cts_reply_text = :b, "
            "cts_attachments = COALESCE(cts_attachments, CAST(:a AS jsonb)) "
            "WHERE id = :id"),
            {"b": reply_text[:300000], "a": json.dumps(atts), "id": row["id"]})
        db.commit()
    return {"ok": True, "cached": False, "available": bool(reply_text), "log_id": str(log_id),
            "reply_text": reply_text, "attachments": atts, "paired_received": paired}


@router.get("/cts-training/stats")
def cts_training_stats(
    department: str = Query(""),
    dept_from: str = Query(""),
    dept_to: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
    assignee: str = Query(""),
    status: str = Query(""),
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Rate de potrivire MG vs CTS + matrice de confuzie pe departament (divergente).
    Accepta aceleasi filtre ca /list ca sa afiseze statistici pe subsetul filtrat."""
    from datetime import date as _date

    base = "FROM cts_ground_truth gt LEFT JOIN emails e ON e.id = gt.email_id"

    # Construieste WHERE dinamic identic cu /list
    where_parts = []
    params = {}
    if department:
        where_parts.append("(gt.cts_department = :dep OR e.ai_department = :dep)")
        params["dep"] = department
    if dept_from:
        where_parts.append("e.ai_department = :dept_from")
        params["dept_from"] = dept_from
    if dept_to:
        where_parts.append("gt.cts_department = :dept_to")
        params["dept_to"] = dept_to
    if date_from:
        try:
            _date.fromisoformat(date_from)
            where_parts.append("COALESCE(e.received_at, gt.cts_reply_at, gt.changed_at) >= CAST(:date_from AS date)")
            params["date_from"] = date_from
        except ValueError:
            pass
    if date_to:
        try:
            _date.fromisoformat(date_to)
            where_parts.append("COALESCE(e.received_at, gt.cts_reply_at, gt.changed_at) < (CAST(:date_to AS date) + interval '1 day')")
            params["date_to"] = date_to
        except ValueError:
            pass
    if assignee.strip():
        where_parts.append("lower(gt.cts_assignee_email) = lower(:assignee)")
        params["assignee"] = assignee.strip()
    if status.strip():
        where_parts.append("lower(gt.cts_status) = lower(:status)")
        params["status"] = status.strip()

    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

    total = db.execute(text("SELECT count(*) " + base + where_sql), params).scalar() or 0
    only_in_cts = db.execute(text("SELECT count(*) " + base + where_sql + (" AND " if where_parts else " WHERE ") + "gt.email_id IS NULL"), params).scalar() or 0

    cat = db.execute(text(
        "SELECT count(*) FILTER (WHERE gt.cts_category IS NOT NULL AND e.ai_category IS NOT NULL) AS comparable, "
        "       count(*) FILTER (WHERE gt.cts_category IS NOT NULL AND e.ai_category IS NOT NULL "
        "                        AND gt.cts_category = e.ai_category) AS matched, "
        "       count(*) FILTER (WHERE gt.cts_category IS NOT NULL AND e.ai_category IS NOT NULL "
        "                        AND e.ai_category='necunoscut') AS unknown " + base + where_sql), params).fetchone()
    dep = db.execute(text(
        "SELECT count(*) FILTER (WHERE gt.cts_department IS NOT NULL AND e.ai_department IS NOT NULL) AS comparable, "
        "       count(*) FILTER (WHERE gt.cts_department IS NOT NULL AND e.ai_department IS NOT NULL "
        "                        AND gt.cts_department = e.ai_department) AS matched, "
        "       count(*) FILTER (WHERE gt.cts_department IS NOT NULL AND e.ai_department IS NOT NULL "
        "                        AND (gt.cts_department = e.ai_department OR e.ai_department='suport_1')) AS matched_excl " + base + where_sql), params).fetchone()
    asg = db.execute(text(
        "SELECT count(*) FILTER (WHERE gt.cts_assignee_email IS NOT NULL AND e.ai_assignee IS NOT NULL) AS comparable, "
        "       count(*) FILTER (WHERE gt.cts_assignee_email IS NOT NULL AND e.ai_assignee IS NOT NULL "
        "                        AND lower(gt.cts_assignee_email) = lower(e.ai_assignee)) AS matched, "
        "       count(*) FILTER (WHERE gt.cts_assignee_email IS NOT NULL AND e.ai_assignee IS NULL) AS missed_unassigned " + base + where_sql), params).fetchone()
    reply_cov = db.execute(text(
        "SELECT count(*) FILTER (WHERE gt.cts_reply_text IS NOT NULL AND gt.cts_reply_text <> '') " + base + where_sql), params).scalar() or 0
    # cate au fost re-incadrate de CTS in ultimele 24h (catch al sync-ului rolling) — global, fara filtre
    changed_24h = db.execute(text(
        "SELECT count(*) FROM cts_ground_truth WHERE changed_at >= now() - interval '24 hours'")).scalar() or 0
    # cate sunt aduse din CTS dar inca NEINCADRATE (operatorul nu a setat categorie/dept) —
    # sync-ul automat le re-interogheaza pana sunt setate. DOAR mailurile PRIMITE: cele TRIMISE
    # de noi nu se incadreaza niciodata, deci nu sunt "in asteptare".
    pending_unclassified = db.execute(text(
        "SELECT count(*) FROM cts_ground_truth "
        "WHERE COALESCE(cts_direction,'received')='received' "
        "AND (cts_category IS NULL OR cts_department IS NULL)")).scalar() or 0
    # mailuri TRIMISE de noi (reply colegi) — afisate cu flag TRIMIS, EXCLUSE din comparatii/statistici
    sent_count = db.execute(text(
        "SELECT count(*) FROM cts_ground_truth WHERE cts_direction='sent'")).scalar() or 0

    def _rate(matched, comparable):
        return (round(100.0 * matched / comparable, 1) if comparable else None)

    cat_cmp, cat_ok = (cat._mapping["comparable"], cat._mapping["matched"])
    cat_unknown = cat._mapping["unknown"]
    dep_cmp, dep_ok = (dep._mapping["comparable"], dep._mapping["matched"])
    dep_ok_excl = dep._mapping["matched_excl"]
    asg_cmp, asg_ok = (asg._mapping["comparable"], asg._mapping["matched"])
    asg_missed = asg._mapping["missed_unassigned"]

    # Filtru suplimentar pentru matricele de confuzie: combina cu filtrul general
    conf_extra = (" AND " if where_parts else " WHERE ") + "COALESCE(gt.cts_direction,'received')='received' AND gt.cts_department IS NOT NULL AND e.ai_department IS NOT NULL AND gt.cts_department <> e.ai_department"
    conf_rows = db.execute(text(
        "SELECT e.ai_department AS mg, gt.cts_department AS cts, count(*) AS n " + base +
        where_sql + conf_extra +
        " GROUP BY e.ai_department, gt.cts_department ORDER BY n DESC"), params).fetchall()
    confusion = [{
        "mg": r._mapping["mg"], "mg_label": DEPT_LABELS.get(r._mapping["mg"], r._mapping["mg"]),
        "cts": r._mapping["cts"], "cts_label": DEPT_LABELS.get(r._mapping["cts"], r._mapping["cts"]),
        "n": r._mapping["n"],
    } for r in conf_rows]

    cat_conf_extra = (" AND " if where_parts else " WHERE ") + "COALESCE(gt.cts_direction,'received')='received' AND gt.cts_category IS NOT NULL AND e.ai_category IS NOT NULL AND gt.cts_category <> e.ai_category AND gt.cts_category IN ('informatie','sesizare','reclamatie','necunoscut')"
    cat_conf_rows = db.execute(text(
        "SELECT e.ai_category AS mg, gt.cts_category AS cts, count(*) AS n " + base +
        where_sql + cat_conf_extra +
        " GROUP BY e.ai_category, gt.cts_category ORDER BY n DESC"), params).fetchall()
    category_confusion = [{
        "mg": r._mapping["mg"], "cts": r._mapping["cts"], "n": r._mapping["n"],
    } for r in cat_conf_rows]

    return {
        "total": total,
        "only_in_cts": only_in_cts,
        "category": {"comparable": cat_cmp, "matched": cat_ok, "match_rate": _rate(cat_ok, cat_cmp),
                     "unknown": cat_unknown, "unknown_rate": _rate(cat_unknown, cat_cmp)},
        "department": {"comparable": dep_cmp, "matched": dep_ok, "match_rate": _rate(dep_ok, dep_cmp),
                       "matched_excl_suport1": dep_ok_excl, "match_rate_excl_suport1": _rate(dep_ok_excl, dep_cmp)},
        "assignee": {"comparable": asg_cmp, "matched": asg_ok, "match_rate": _rate(asg_ok, asg_cmp),
                     "missed_unassigned": asg_missed},
        "reply_coverage": reply_cov,
        "reply_coverage_rate": (round(100.0 * reply_cov / total, 1) if total else None),
        "changed_24h": changed_24h,
        "pending_unclassified": pending_unclassified,
        "sent_count": sent_count,
        "confusion": confusion,
        "category_confusion": category_confusion,
    }


@router.get("/cts-training/accuracy-daily")
def cts_training_accuracy_daily(days: int = Query(7, ge=1, le=730),
                                date_from: str = Query(""),
                                date_to: str = Query(""),
                                db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Acuratetea zilnica MG vs ground-truth CTS. Accepta fie days (fereastra relativa din azi),
    fie date_from/date_to (interval fix). Raspunsul include prompt_changes."""
    from datetime import date as _date
    # Determina intervalul: date_from/date_to au prioritate peste days
    if date_from:
        try:
            d_from = _date.fromisoformat(date_from)
        except ValueError:
            d_from = None
    else:
        d_from = None
    if date_to:
        try:
            d_to = _date.fromisoformat(date_to)
        except ValueError:
            d_to = _date.today()
    else:
        d_to = _date.today()

    if d_from:
        sql_params = {"d_from": d_from.isoformat(), "d_to": d_to.isoformat()}
        sql = """
            WITH day_series AS (
                SELECT generate_series(CAST(:d_from AS date), CAST(:d_to AS date), INTERVAL '1 day')::date AS d
            ),
            agg AS (
                SELECT e.received_at::date AS d,
                    count(*) FILTER (WHERE gt.cts_category IS NOT NULL AND e.ai_category IS NOT NULL) AS cat_cmp,
                    count(*) FILTER (WHERE gt.cts_category IS NOT NULL AND e.ai_category IS NOT NULL
                                     AND gt.cts_category = e.ai_category) AS cat_ok,
                    count(*) FILTER (WHERE gt.cts_department IS NOT NULL AND e.ai_department IS NOT NULL) AS dep_cmp,
                    count(*) FILTER (WHERE gt.cts_department IS NOT NULL AND e.ai_department IS NOT NULL
                                     AND gt.cts_department = e.ai_department) AS dep_ok,
                    count(*) FILTER (WHERE gt.cts_department IS NOT NULL AND e.ai_department IS NOT NULL
                                     AND (gt.cts_department = e.ai_department OR e.ai_department='suport_1')) AS dep_ok_excl,
                    count(*) FILTER (WHERE gt.cts_assignee_email IS NOT NULL AND e.ai_assignee IS NOT NULL) AS asg_cmp,
                    count(*) FILTER (WHERE gt.cts_assignee_email IS NOT NULL AND e.ai_assignee IS NOT NULL
                                     AND lower(gt.cts_assignee_email) = lower(e.ai_assignee)) AS asg_ok
                FROM cts_ground_truth gt JOIN emails e ON e.id = gt.email_id
                WHERE COALESCE(gt.cts_direction,'received')='received'
                  AND e.received_at::date >= CAST(:d_from AS date)
                  AND e.received_at::date <= CAST(:d_to AS date)
                GROUP BY e.received_at::date
            )
            SELECT to_char(day_series.d,'YYYY-MM-DD') AS day,
                   COALESCE(agg.cat_cmp,0) AS cat_cmp, COALESCE(agg.cat_ok,0) AS cat_ok,
                   COALESCE(agg.dep_cmp,0) AS dep_cmp, COALESCE(agg.dep_ok,0) AS dep_ok, COALESCE(agg.dep_ok_excl,0) AS dep_ok_excl,
                   COALESCE(agg.asg_cmp,0) AS asg_cmp, COALESCE(agg.asg_ok,0) AS asg_ok
            FROM day_series LEFT JOIN agg ON agg.d = day_series.d ORDER BY day_series.d
        """
        pc_where = "created_at::date >= CAST(:d_from AS date) AND created_at::date <= CAST(:d_to AS date)"
    else:
        sql_params = {"days": days}
        sql = """
            WITH day_series AS (
                SELECT generate_series((CURRENT_DATE - (:days - 1)), CURRENT_DATE, INTERVAL '1 day')::date AS d
            ),
            agg AS (
                SELECT e.received_at::date AS d,
                    count(*) FILTER (WHERE gt.cts_category IS NOT NULL AND e.ai_category IS NOT NULL) AS cat_cmp,
                    count(*) FILTER (WHERE gt.cts_category IS NOT NULL AND e.ai_category IS NOT NULL
                                     AND gt.cts_category = e.ai_category) AS cat_ok,
                    count(*) FILTER (WHERE gt.cts_department IS NOT NULL AND e.ai_department IS NOT NULL) AS dep_cmp,
                    count(*) FILTER (WHERE gt.cts_department IS NOT NULL AND e.ai_department IS NOT NULL
                                     AND gt.cts_department = e.ai_department) AS dep_ok,
                    count(*) FILTER (WHERE gt.cts_department IS NOT NULL AND e.ai_department IS NOT NULL
                                     AND (gt.cts_department = e.ai_department OR e.ai_department='suport_1')) AS dep_ok_excl,
                    count(*) FILTER (WHERE gt.cts_assignee_email IS NOT NULL AND e.ai_assignee IS NOT NULL) AS asg_cmp,
                    count(*) FILTER (WHERE gt.cts_assignee_email IS NOT NULL AND e.ai_assignee IS NOT NULL
                                     AND lower(gt.cts_assignee_email) = lower(e.ai_assignee)) AS asg_ok
                FROM cts_ground_truth gt JOIN emails e ON e.id = gt.email_id
                WHERE COALESCE(gt.cts_direction,'received')='received'
                  AND e.received_at >= CURRENT_DATE - (:days - 1)
                GROUP BY e.received_at::date
            )
            SELECT to_char(day_series.d,'YYYY-MM-DD') AS day,
                   COALESCE(agg.cat_cmp,0) AS cat_cmp, COALESCE(agg.cat_ok,0) AS cat_ok,
                   COALESCE(agg.dep_cmp,0) AS dep_cmp, COALESCE(agg.dep_ok,0) AS dep_ok, COALESCE(agg.dep_ok_excl,0) AS dep_ok_excl,
                   COALESCE(agg.asg_cmp,0) AS asg_cmp, COALESCE(agg.asg_ok,0) AS asg_ok
            FROM day_series LEFT JOIN agg ON agg.d = day_series.d ORDER BY day_series.d
        """
        pc_where = "created_at >= CURRENT_DATE - (:days - 1)"
    rows = db.execute(text(sql), sql_params).fetchall()
    series = []
    for r in rows:
        m = dict(r._mapping)
        m["cat_pct"] = round(m["cat_ok"] * 100.0 / m["cat_cmp"], 1) if m["cat_cmp"] else None
        m["dep_pct"] = round(m["dep_ok"] * 100.0 / m["dep_cmp"], 1) if m["dep_cmp"] else None
        m["dep_pct_excl"] = round(m["dep_ok_excl"] * 100.0 / m["dep_cmp"], 1) if m["dep_cmp"] else None
        m["asg_pct"] = round(m["asg_ok"] * 100.0 / m["asg_cmp"], 1) if m["asg_cmp"] else None
        series.append(m)

    # Schimbari de prompturi de categorie si departament, grupate pe zi
    prompt_sql = """
        SELECT to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS day,
               'category' AS prompt_type,
               source,
               COALESCE(created_by, 'sistem') AS changed_by,
               COUNT(*) AS n
        FROM ai_category_prompt_versions
        WHERE """ + pc_where + """
        GROUP BY 1, 2, 3, 4
        UNION ALL
        SELECT to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS day,
               'department' AS prompt_type,
               source,
               COALESCE(created_by, 'sistem') AS changed_by,
               COUNT(*) AS n
        FROM ai_department_prompt_versions
        WHERE """ + pc_where + """
        GROUP BY 1, 2, 3, 4
        ORDER BY day
    """
    pc_rows = db.execute(text(prompt_sql), sql_params).fetchall()
    # Agregate pe zi: o intrare per zi per tip de prompt (cat/dep)
    pc_by_day = {}
    for r in pc_rows:
        m = r._mapping
        day = m["day"]
        if day not in pc_by_day:
            pc_by_day[day] = {"day": day, "types": [], "source": m["source"], "changed_by": m["changed_by"]}
        if m["prompt_type"] not in pc_by_day[day]["types"]:
            pc_by_day[day]["types"].append(m["prompt_type"])
    prompt_changes = sorted(pc_by_day.values(), key=lambda x: x["day"])

    return {"days": days, "series": series, "prompt_changes": prompt_changes}


@router.get("/cts-training/divergences")
def cts_training_divergences(axis: str = Query("department", description="'category' | 'department'"),
                             limit: int = Query(200, ge=1, le=2000),
                             db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Lista CONCRETA a emailurilor unde AI != CTS (adevar de teren), in acelasi format ca tabelul
    de corectii manuale: email_id, subiect, ce a zis AI (old), ce e corect dupa CTS (new), de cine
    (operatorul din CTS). Doar mailuri PRIMITE. Sursa pentru regenerarea prompturilor din CTS."""
    ax = (axis or "department").strip().lower()
    by_who = "COALESCE(NULLIF(gt.raw->>'assigned_to',''), NULLIF(gt.raw->'extra'->>'responsible_id',''), 'CTS (suport)')"
    if ax == "category":
        sql = ("SELECT gt.email_id, e.subject, e.from_address, e.ai_category AS old_val, "
               "gt.cts_category AS new_val, " + by_who + " AS by_who, "
               "to_char(gt.changed_at,'YYYY-MM-DD HH24:MI') AS changed_at "
               "FROM cts_ground_truth gt JOIN emails e ON e.id=gt.email_id "
               "WHERE COALESCE(gt.cts_direction,'received')='received' "
               "  AND gt.cts_category IS NOT NULL AND e.ai_category IS NOT NULL "
               "  AND gt.cts_category <> e.ai_category "
               "  AND gt.cts_category IN ('informatie','sesizare','reclamatie','necunoscut') "
               "ORDER BY gt.changed_at DESC NULLS LAST, gt.email_id DESC LIMIT :l")
    elif ax == "assignee":
        sql = ("SELECT gt.email_id, e.subject, e.from_address, e.ai_assignee AS old_val, "
               "gt.cts_assignee_email AS new_val, " + by_who + " AS by_who, "
               "to_char(gt.changed_at,'YYYY-MM-DD HH24:MI') AS changed_at "
               "FROM cts_ground_truth gt JOIN emails e ON e.id=gt.email_id "
               "WHERE COALESCE(gt.cts_direction,'received')='received' "
               "  AND gt.cts_assignee_email IS NOT NULL AND e.ai_assignee IS NOT NULL "
               "  AND lower(gt.cts_assignee_email) <> lower(e.ai_assignee) "
               "ORDER BY gt.changed_at DESC NULLS LAST, gt.email_id DESC LIMIT :l")
    else:
        ax = "department"
        sql = ("SELECT gt.email_id, e.subject, e.from_address, e.ai_department AS old_val, "
               "gt.cts_department AS new_val, " + by_who + " AS by_who, "
               "to_char(gt.changed_at,'YYYY-MM-DD HH24:MI') AS changed_at "
               "FROM cts_ground_truth gt JOIN emails e ON e.id=gt.email_id "
               "WHERE COALESCE(gt.cts_direction,'received')='received' "
               "  AND gt.cts_department IS NOT NULL AND e.ai_department IS NOT NULL "
               "  AND gt.cts_department <> e.ai_department "
               "  AND e.ai_department <> 'suport_1' AND gt.cts_department <> 'suport_1' "
               "ORDER BY gt.changed_at DESC NULLS LAST, gt.email_id DESC LIMIT :l")
    rows = db.execute(text(sql), {"l": limit}).fetchall()
    items = [{
        "email_id": m["email_id"], "subject": m["subject"], "from_address": m["from_address"],
        "old_val": m["old_val"], "new_val": m["new_val"], "by": m["by_who"],
        "changed_at": m["changed_at"],
    } for m in (r._mapping for r in rows)]
    return {"axis": ax, "total": len(items), "items": items}


@router.post("/cts-training/sync")
def cts_training_sync(limit: int = Query(500, ge=1, le=5000),
                      admin=Depends(get_current_admin)):
    """Declanseaza sync-ul COMPLET din CTS. Inert (ok:False + reason) pana la grant cross-app."""
    return SYNC.sync_ground_truth(limit=limit)


@router.post("/cts-training/sync-recent")
def cts_training_sync_recent(hours: int = Query(24, ge=1, le=168),
                             wait: bool = Query(False),
                             admin=Depends(get_current_admin)):
    """Re-sincronizeaza fereastra rolling (default 24h): prinde emailurile pe care CTS le-a
    (re)incadrat dupa ce au intrat (categoria/departamentul se pot schimba la ~1h dupa receptie).
    Ruleaza in FUNDAL (daemon thread) si intoarce IMEDIAT, ca sa nu pice pe timeout-ul gateway-ului
    pe ferestre mari (sync-ul poate dura minute). wait=true -> sincron, intoarce rezultatul complet
    (debug/cron). Acelasi mecanism ruleaza automat din cron (POST /process/run-now). Inert pana la grant."""
    # Inert (sursa neconfigurata / dezactivat) -> raspuns instant cu motivul, fara thread.
    if not SYNC.is_enabled():
        return SYNC.sync_recent(hours=hours)
    if wait:
        return SYNC.sync_recent(hours=hours)
    import threading as _th
    _th.Thread(target=SYNC.sync_recent_guarded, kwargs={"hours": hours}, daemon=True).start()
    return {"ok": True, "started": True, "async": True, "window_hours": hours,
            "message": "Sync pornit in fundal. Lista se actualizeaza in cateva momente."}


@router.get("/cts-training/assignees")
def cts_training_assignees(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Utilizatorii care APAR ca assignee CTS pe mailuri — pentru selectul de filtru.

    Numele vine din `cts_assignee_name` (cum il scrie CTS); daca lipseste, cade pe
    `employee_department_mapping`. Selectul e limitat la cine chiar are mailuri, nu la toti
    angajatii — altfel ar avea 300 de intrari inutile.
    """
    rows = db.execute(text("""
        SELECT lower(gt.cts_assignee_email) AS email,
               COALESCE(max(gt.cts_assignee_name), max(edm.name)) AS name,
               max(edm.department) AS department,
               count(*) AS n
        FROM cts_ground_truth gt
        LEFT JOIN employee_department_mapping edm ON lower(edm.email) = lower(gt.cts_assignee_email)
        WHERE gt.cts_assignee_email IS NOT NULL AND gt.cts_assignee_email <> ''
          AND gt.cts_deleted_at IS NULL
        GROUP BY 1
        ORDER BY 2
    """)).fetchall()
    return {"assignees": [{"email": r[0], "name": r[1] or r[0], "department": r[2],
                           "count": int(r[3])} for r in rows]}


@router.get("/cts-training/sync-config")
def cts_training_sync_config(admin=Depends(get_current_admin)):
    """Stare sync (fara secrete) pt UI."""
    return SYNC.status()


@router.put("/cts-training/sync-config")
def cts_training_set_sync_config(payload: dict = Body(...),
                                 db: Session = Depends(get_db),
                                 admin=Depends(get_current_admin)):
    """Activeaza/dezactiveaza flag-ul de sync. (Sync-ul ramane inert daca sursa nu e configurata.)"""
    enabled = bool(payload.get("enabled"))
    by = admin.get("username") or admin.get("email") or "admin"
    db.execute(text(
        "INSERT INTO settings(key, value, updated_by, updated_at) "
        "VALUES (:k, CAST(:v AS jsonb), :by, now()) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_by=EXCLUDED.updated_by, "
        "updated_at=now()"),
        {"k": SYNC_ENABLED_KEY, "v": ("true" if enabled else "false"), "by": by})
    db.commit()
    return SYNC.status()


# ─────────────────────────────────────────────────────────────────────────────
# Raport departamente (tab „Raport departamente" din Mail-uri CTS) — 2026-08-19
#
# Trei unghiuri peste acelasi lant de departamente prin care trece un mail:
#   1) de cate ori a fost mutat un mail (distributie 0 / 1 / 2 / 3+),
#   2) topul departamentelor care INITIAZA mutari (de pe cine pleaca mailul),
#   3) topul departamentelor INTERMEDIARE (nici primul alocat, nici cel care inchide).
#
# Sursa: `cts_department_moves` (vezi migrations/20260819_cts_department_moves.sql) — un rand per
# eveniment, scris de trigger la fiecare schimbare de departament pe cts_ground_truth. Lantul se
# reconstruieste per MAIL (message_id), agregand tichetele lui (CTS face un tichet per destinatar)
# si colapsand pasii consecutivi identici.
#
# LIMITARE (de spus si in UI): sync-ul CTS ruleaza la ~5 min, deci doua mutari intre doua rulari
# se vad ca una singura, iar pentru mailurile de dinaintea migratiei avem doar pasul salvat in
# cts_department_prev. Cifrele sunt un PLANSEU (sub-numarare), nu o valoare exacta.
# ─────────────────────────────────────────────────────────────────────────────

_DEPT_REPORT_CTE = """
WITH mail AS (
    SELECT g.message_id,
           min(COALESCE(g.cts_assigned_at, g.fetched_at)) AS started_at,
           max(g.cts_solved_at) AS solved_at,
           bool_or(lower(COALESCE(g.cts_status,'')) IN ('solved','rezolvat')) AS is_solved,
           min(g.email_id) AS email_id
      FROM cts_ground_truth g
     WHERE COALESCE(g.cts_direction,'received') = 'received'
       AND g.cts_deleted_at IS NULL
       AND g.message_id IS NOT NULL
     GROUP BY g.message_id
    HAVING (CAST(:date_from AS date) IS NULL
            OR min(COALESCE(g.cts_assigned_at, g.fetched_at)) >= CAST(:date_from AS date))
       AND (CAST(:date_to AS date) IS NULL
            OR min(COALESCE(g.cts_assigned_at, g.fetched_at)) < CAST(:date_to AS date) + interval '1 day')
       AND (NOT CAST(:only_solved AS boolean)
            OR bool_or(lower(COALESCE(g.cts_status,'')) IN ('solved','rezolvat')))
),
ev0 AS (
    -- CTS face un tichet PER DESTINATAR, deci acelasi mail poate avea mai multe alocari
    -- INITIALE (una per tichet). Le pastram doar pe cea mai veche — altfel replicile ar
    -- aparea ca „mutari" care nu s-au intamplat. Mutarile reale (from_department NOT NULL)
    -- se pastreaza toate, indiferent de tichet.
    SELECT mv.message_id, mv.from_department, mv.to_department, mv.moved_at, mv.id,
           row_number() OVER (PARTITION BY mv.message_id, (mv.from_department IS NULL)
                              ORDER BY mv.moved_at, mv.id) AS rn_kind
      FROM cts_department_moves mv
      JOIN mail m ON m.message_id = mv.message_id
     WHERE mv.to_department IS NOT NULL
),
ev AS (
    SELECT message_id, to_department AS dep, moved_at, id,
           lag(to_department) OVER (PARTITION BY message_id ORDER BY moved_at, id) AS prev_dep
      FROM ev0
     WHERE from_department IS NOT NULL OR rn_kind = 1
),
seq AS (
    SELECT message_id, dep, moved_at,
           row_number() OVER (PARTITION BY message_id ORDER BY moved_at, id) AS step,
           count(*)     OVER (PARTITION BY message_id) AS steps
      FROM ev
     WHERE prev_dep IS NULL OR prev_dep IS DISTINCT FROM dep
),
kept AS (
    SELECT message_id
      FROM seq
     GROUP BY message_id
    HAVING CAST(:dept AS text) IS NULL OR bool_or(dep = CAST(:dept AS text))
),
ch AS (
    SELECT s.* FROM seq s JOIN kept k ON k.message_id = s.message_id
)
"""


def _dept_report_params(date_from: str, date_to: str, department: str, only_solved: int) -> dict:
    """Normalizeaza filtrele comune ale raportului (date invalide -> ignorate, nu 400)."""
    from datetime import date as _date

    def _d(v):
        try:
            _date.fromisoformat((v or "").strip())
            return v.strip()
        except ValueError:
            return None

    dep = (department or "").strip() or None
    if dep is not None and dep not in DEPT_LABELS:
        dep = None
    return {"date_from": _d(date_from), "date_to": _d(date_to), "dept": dep,
            "only_solved": bool(only_solved)}


def _lbl(slug):
    return DEPT_LABELS.get(slug, slug or "—")


def _pct(n, total):
    return (round(100.0 * n / total, 1) if total else 0.0)


@router.get("/cts-training/dept-report")
def cts_training_dept_report(
    date_from: str = Query("", description="data start YYYY-MM-DD (pe intrarea mailului)"),
    date_to: str = Query("", description="data sfarsit YYYY-MM-DD"),
    department: str = Query("", description="doar mailurile care au trecut prin acest departament"),
    only_solved: int = Query(0, description="1 = doar mailurile inchise (solved)"),
    top: int = Query(10, ge=3, le=30),
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Cele 3 statistici de mutari intre departamente + perechile din-in (pentru drill-down)."""
    params = _dept_report_params(date_from, date_to, department, only_solved)

    # 1) Distributia mailurilor dupa numarul de mutari (0 / 1 / 2 / 3+)
    dist_rows = db.execute(text(_DEPT_REPORT_CTE + """
        SELECT LEAST(steps - 1, 3) AS bucket, count(*) AS emails, sum(steps - 1) AS moves
          FROM (SELECT message_id, max(steps) AS steps FROM ch GROUP BY message_id) t
         GROUP BY 1 ORDER BY 1
    """), params).fetchall()
    by_bucket = {int(r._mapping["bucket"]): r._mapping for r in dist_rows}
    total_emails = sum(int(m["emails"]) for m in by_bucket.values())
    total_moves = sum(int(m["moves"] or 0) for m in by_bucket.values())
    labels = {0: "0 (rezolvat de departamentul inițial)", 1: "1 mutare", 2: "2 mutări", 3: "3 sau mai multe mutări"}
    distribution = [{
        "bucket": b, "label": labels[b],
        "emails": int(by_bucket[b]["emails"]) if b in by_bucket else 0,
        "pct": _pct(int(by_bucket[b]["emails"]) if b in by_bucket else 0, total_emails),
    } for b in (0, 1, 2, 3)]

    # 2) Top departamente care fac mutari: la fiecare pas care NU e ultimul, departamentul
    #    de pe care pleaca mailul a initiat o mutare.
    init_rows = db.execute(text(_DEPT_REPORT_CTE + """
        SELECT dep, count(*) AS n FROM ch WHERE step < steps GROUP BY dep ORDER BY n DESC
    """), params).fetchall()
    init_total = sum(int(r._mapping["n"]) for r in init_rows)
    initiators = [{"department": r._mapping["dep"], "label": _lbl(r._mapping["dep"]),
                   "moves": int(r._mapping["n"]), "pct": _pct(int(r._mapping["n"]), init_total)}
                  for r in init_rows][:top]

    # 3) Top departamente intermediare: pasi care nu sunt nici primul (alocarea initiala),
    #    nici ultimul (unde s-a oprit / s-a inchis mailul).
    mid_rows = db.execute(text(_DEPT_REPORT_CTE + """
        SELECT dep, count(*) AS n FROM ch WHERE step > 1 AND step < steps GROUP BY dep ORDER BY n DESC
    """), params).fetchall()
    mid_total = sum(int(r._mapping["n"]) for r in mid_rows)
    intermediaries = [{"department": r._mapping["dep"], "label": _lbl(r._mapping["dep"]),
                       "n": int(r._mapping["n"]), "pct": _pct(int(r._mapping["n"]), mid_total)}
                      for r in mid_rows][:top]

    # Perechile din -> in (traseele concrete), pentru tabelul de sub grafice.
    pair_rows = db.execute(text(_DEPT_REPORT_CTE + """
        SELECT f, t, count(*) AS n FROM (
            SELECT message_id, dep AS t,
                   lag(dep) OVER (PARTITION BY message_id ORDER BY step) AS f
              FROM ch
        ) x WHERE f IS NOT NULL GROUP BY f, t ORDER BY n DESC LIMIT :lim
    """), dict(params, lim=top * 2)).fetchall()
    pairs = [{"from": r._mapping["f"], "from_label": _lbl(r._mapping["f"]),
              "to": r._mapping["t"], "to_label": _lbl(r._mapping["t"]),
              "n": int(r._mapping["n"]), "pct": _pct(int(r._mapping["n"]), total_moves)}
             for r in pair_rows]

    # Acoperire: de cand avem captura completa (trigger) vs. ce s-a putut reconstitui la migrare.
    cov = db.execute(text(
        "SELECT count(*) FILTER (WHERE detected_by='trigger') AS live, "
        "       count(*) FILTER (WHERE detected_by='backfill') AS backfilled, "
        "       min(moved_at) FILTER (WHERE detected_by='trigger') AS live_since "
        "  FROM cts_department_moves")).fetchone()

    moved = total_emails - (distribution[0]["emails"] if distribution else 0)
    return {
        "totals": {
            "emails": total_emails,
            "moved_emails": moved,
            "moved_pct": _pct(moved, total_emails),
            "moves": total_moves,
            "avg_moves": (round(total_moves / total_emails, 2) if total_emails else 0),
        },
        "distribution": distribution,
        "initiators": initiators,
        "intermediaries": intermediaries,
        "pairs": pairs,
        "coverage": {
            "live_events": int(cov._mapping["live"] or 0),
            "backfilled_events": int(cov._mapping["backfilled"] or 0),
            "live_since": cov._mapping["live_since"],
        },
        "filters": {"date_from": params["date_from"], "date_to": params["date_to"],
                    "department": params["dept"], "only_solved": params["only_solved"]},
    }


@router.get("/cts-training/dept-report/cases")
def cts_training_dept_report_cases(
    date_from: str = Query(""),
    date_to: str = Query(""),
    department: str = Query("", description="doar mailurile care au trecut prin acest departament"),
    only_solved: int = Query(0),
    min_moves: int = Query(1, ge=0, le=10),
    dept_from: str = Query("", description="departamentul care a initiat o mutare"),
    dept_mid: str = Query("", description="departament aparut ca INTERMEDIAR pe lant"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Cazurile concrete din spatele statisticilor: un rand per mail, cu lantul de departamente.

    Filtrele `dept_from` / `dept_mid` corespund click-ului pe o linie din statistica 2 / 3."""
    params = _dept_report_params(date_from, date_to, department, only_solved)
    d_from = (dept_from or "").strip() or None
    d_mid = (dept_mid or "").strip() or None
    params.update({"min_moves": min_moves,
                   "d_from": d_from if d_from in DEPT_LABELS else None,
                   "d_mid": d_mid if d_mid in DEPT_LABELS else None})

    per_mail = _DEPT_REPORT_CTE + """
    , agg AS (
        SELECT c.message_id,
               max(c.steps) - 1 AS moves,
               min(c.moved_at)  AS first_at,
               max(c.moved_at)  AS last_move_at,
               string_agg(c.dep, ' → ' ORDER BY c.step) AS chain,
               (array_agg(c.dep ORDER BY c.step))[1] AS first_dep,
               (array_agg(c.dep ORDER BY c.step DESC))[1] AS last_dep
          FROM ch c
         GROUP BY c.message_id
        HAVING max(c.steps) - 1 >= :min_moves
           AND (CAST(:d_from AS text) IS NULL
                OR bool_or(c.dep = CAST(:d_from AS text) AND c.step < c.steps))
           AND (CAST(:d_mid AS text) IS NULL
                OR bool_or(c.dep = CAST(:d_mid AS text) AND c.step > 1 AND c.step < c.steps))
    )
    """

    total = db.execute(text(per_mail + " SELECT count(*) FROM agg"), params).scalar() or 0
    rows = db.execute(text(per_mail + """
        SELECT a.message_id, a.moves, a.chain, a.first_dep, a.last_dep,
               a.first_at, a.last_move_at, m.email_id, m.solved_at, m.is_solved,
               e.subject, e.from_address, e.received_at
          FROM agg a
          JOIN mail m ON m.message_id = a.message_id
          LEFT JOIN emails e ON e.id = m.email_id
         ORDER BY a.moves DESC, a.last_move_at DESC
         LIMIT :page_size OFFSET :offset
    """), dict(params, page_size=page_size, offset=(page - 1) * page_size)).fetchall()

    items = [{
        "message_id": m["message_id"], "email_id": m["email_id"],
        "moves": int(m["moves"]), "chain": m["chain"],
        "chain_labels": " → ".join(_lbl(s) for s in (m["chain"] or "").split(" → ") if s),
        "first_department": m["first_dep"], "first_department_label": _lbl(m["first_dep"]),
        "last_department": m["last_dep"], "last_department_label": _lbl(m["last_dep"]),
        "first_at": m["first_at"], "last_move_at": m["last_move_at"],
        "solved_at": m["solved_at"], "is_solved": bool(m["is_solved"]),
        "subject": m["subject"], "from_address": m["from_address"], "received_at": m["received_at"],
    } for m in (r._mapping for r in rows)]
    return {"total": int(total), "page": page, "page_size": page_size, "items": items}
