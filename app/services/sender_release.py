# -*- coding: utf-8 -*-
"""Eliberare RETROACTIVA a mailurilor blocate ale unui expeditor pus pe whitelist.

De ce exista: adaugarea unei adrese in whitelist (Setari -> Liste expeditori) schimba DOAR
clasificarea viitoare. Mailurile deja oprite — ca spam (`stopped_spam`) sau in carantina
(`quarantined` / `quarantined_strict`) — raman exact acolo unde erau, fiindca nimic nu le
reevalueaza: pipeline-ul ruleaza o singura data per email, la ingestie. Butonul „Legit" din
pagina Spam facea o eliberare retroactiva, dar numai pentru spam si numai pentru expeditorul
mailului pe care s-a dat click (`status NOT IN ('quarantined', ...)` il excludea explicit).
Rezultatul vazut de operator: „am pus adresa in whitelist, dar nu s-au reprocesat toate".

Ce face `release_sender()`:
  - spam        -> `email_spam.override = FALSE` (NU mai e spam, la orice scor);
  - carantina   -> `status = 'clean'`, marcheaza `quarantine_strict` ca eliberat;
  - ambele      -> repune emailul pe calea clean (`queued_general` + `manual_clean`), de unde
                   tick-ul de 5 minute il duce prin categorie/departament spre CTS.

⛔ Mailurile blocate HARD nu se elibereaza NICIODATA in masa: atasament cu malware, macro /
executabil / dubla extensie (`emails.NEVER_SUPPRESS`) si impersonarea unui domeniu intern
(`auth_spoof_internal_domain`). Un cont legitim poate fi compromis — whitelist-ul e o decizie
despre expeditor, nu despre continutul unui atasament infectat. Astea raman in carantina si se
elibereaza doar individual, de om, din pagina de carantina.

⚠️ Mailurile DEJA trimise la CTS (`sent_to_cts_at IS NOT NULL`) nu se ating: repunerea lor pe
coada ar produce un tichet duplicat in CTS.
"""
import json
import logging

from sqlalchemy import text

from app.services.sender_lists import normalize as _normalize

logger = logging.getLogger("mailguard.sender_release")

# Coduri de detectie care tin emailul in carantina chiar si pentru un expeditor whitelist-at.
# Oglindeste `emails.NEVER_SUPPRESS` (malware/executabil/macro) + blocajele hard din pipeline.
HARD_BLOCK_CODES = (
    'executable_attachment', 'macro_attachment', 'double_extension',
    'attachment_malware', 'auth_spoof_internal_domain',
)

# Un email al expeditorului, in orice stare oprita, fara blocaje hard.
_SENDER_MATCH_SQL = """
    (CASE WHEN :scope = 'email'
          THEN lower(COALESCE(e.from_address, '')) = :key
          ELSE split_part(lower(COALESCE(e.from_address, '')), '@', 2) = :key
            OR right(split_part(lower(COALESCE(e.from_address, '')), '@', 2),
                     length(:dot_key)) = :dot_key
     END)
"""

_NO_HARD_BLOCK_SQL = """
    NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements(COALESCE(e.phishing_reasons, '[]'::jsonb)) r
         WHERE r ->> 'code' = ANY(CAST(:hard AS text[])))
"""


def _params(key, scope):
    return {"key": key, "scope": scope, "dot_key": "." + key,
            "hard": list(HARD_BLOCK_CODES)}


def find_blocked(db, value):
    """Mailurile oprite ale expeditorului, grupate pe motiv. Nu schimba nimic.

    Returneaza {"spam": [...], "quarantine": [...], "hard_blocked": [...]} cu id-uri.
    """
    key, scope = _normalize(value)
    if not key:
        return {"spam": [], "quarantine": [], "hard_blocked": []}
    p = _params(key, scope)

    rows = db.execute(text("""
        SELECT e.id,
               e.status,
               COALESCE(e.queue_status, '') AS queue_status,
               EXISTS (SELECT 1 FROM email_spam s
                        WHERE s.email_id = e.id
                          AND (s.override = TRUE
                               OR (s.override IS DISTINCT FROM FALSE AND s.spam_score >= 50)))
                   AS is_spam,
               NOT (""" + _NO_HARD_BLOCK_SQL + """) AS hard_blocked
          FROM emails e
         WHERE """ + _SENDER_MATCH_SQL + """
           AND e.status NOT IN ('ndr', 'deleted')
           AND e.sent_to_cts_at IS NULL
    """), p).fetchall()

    out = {"spam": [], "quarantine": [], "hard_blocked": []}
    for r in rows:
        m = r._mapping
        if m["hard_blocked"]:
            if m["status"] in ('quarantined', 'quarantined_strict'):
                out["hard_blocked"].append(m["id"])
            continue
        if m["status"] in ('quarantined', 'quarantined_strict'):
            out["quarantine"].append(m["id"])
        elif m["is_spam"] or m["queue_status"] == 'stopped_spam':
            out["spam"].append(m["id"])
    return out


def release_sender(db, value, by, include_quarantine=True, commit=True):
    """Elibereaza retroactiv mailurile oprite ale expeditorului `value` (adresa sau domeniu).

    `include_quarantine=False` elibereaza doar spam-ul (comportamentul butonului „Legit").
    Mailurile blocate hard (malware / impersonare domeniu intern) sunt raportate separat in
    `hard_blocked`, NU eliberate. Nu arunca — o eroare lasa datele neatinse si se propaga in log.
    """
    key, scope = _normalize(value)
    if not key:
        return {"error": "Valoare goala"}
    found = find_blocked(db, value)
    ids_spam = found["spam"]
    ids_quar = found["quarantine"] if include_quarantine else []
    ids = ids_spam + ids_quar
    if not ids:
        return {"ok": True, "released": 0, "spam": 0, "quarantine": 0,
                "hard_blocked": len(found["hard_blocked"]), "ids": []}

    if ids_quar:
        db.execute(text("""
            UPDATE emails
               SET status = 'clean', review_decision = 'whitelist_release',
                   reviewed_by = :by, reviewed_at = NOW(), needs_human_review = FALSE
             WHERE id = ANY(CAST(:ids AS bigint[]))
        """), {"by": by, "ids": ids_quar})
        db.execute(text("""
            UPDATE quarantine_strict
               SET review_status = 'released', decision = 'whitelist_release',
                   reviewed_by = :by, reviewed_at = NOW()
             WHERE email_id = ANY(CAST(:ids AS bigint[])) AND review_status = 'pending'
        """), {"by": by, "ids": ids_quar})

    # Spam: override=FALSE explicit (nu stergem randul — pastreaza scorul si istoricul).
    db.execute(text("""
        INSERT INTO email_spam (email_id, spam_score, override, reviewed_by, reviewed_at)
        SELECT x, 0, FALSE, :by, NOW() FROM unnest(CAST(:ids AS bigint[])) AS x
        ON CONFLICT (email_id) DO UPDATE
           SET override = FALSE, reviewed_by = :by, reviewed_at = NOW()
    """), {"by": by, "ids": ids})

    # Inapoi pe calea clean; tick-ul de 5 min le duce prin categorie/departament spre CTS.
    db.execute(text("""
        UPDATE emails
           SET queue_status = 'queued_general', manual_clean = TRUE,
               sent_to_cts_at = NULL, cts_send_error = NULL
         WHERE id = ANY(CAST(:ids AS bigint[]))
    """), {"ids": ids})

    db.execute(text("""
        INSERT INTO audit_log(actor, action, entity_type, entity_id, details)
        VALUES (:a, 'whitelist_release', 'sender', NULL, CAST(:d AS jsonb))
    """), {"a": by, "d": json.dumps({
        "value": key, "scope": scope, "released": len(ids),
        "spam": len(ids_spam), "quarantine": len(ids_quar),
        "hard_blocked": found["hard_blocked"], "email_ids": ids[:200]})})

    if commit:
        db.commit()
    logger.info("whitelist_release %s: %d mailuri (spam=%d, carantina=%d, blocate hard=%d)",
                key, len(ids), len(ids_spam), len(ids_quar), len(found["hard_blocked"]))
    return {"ok": True, "released": len(ids), "spam": len(ids_spam),
            "quarantine": len(ids_quar), "hard_blocked": len(found["hard_blocked"]),
            "ids": ids}
