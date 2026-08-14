"""v0.9.1 — Sync clients from IRIS Gateway.
Reads from IRIS /clients/contact-list endpoint using IRIS_MAILGUARD_API_KEY.
Upserts into mailguard.clients keyed by iris_client_id.
OPS-2026-0124: populeaza si client_vehicles / client_contracts daca feed-ul le
contine (vehicles[]/contracts[]). INERT pana cand IRIS expune campurile — daca
lipsesc, nu se atinge nimic (backward-compatible).
"""
import logging
import httpx
import json
import psycopg2

from app.config import get_settings

logger = logging.getLogger("mailguard.iris_sync")
settings = get_settings()

import threading as _threading
_CLIENT_SYNC_LOCK = _threading.Lock()


def _write_client_sync_state(d):
    """Scrie starea sync-ului in settings['client_assets.last_result'] (best-effort)."""
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE settings SET value = %s::jsonb, updated_at = NOW()
                WHERE key = 'client_assets.last_result'
            """, (json.dumps(d, default=str),))
            conn.commit()
    except Exception:
        logger.exception("client sync state write failed")


_FREE_MAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.ro", "hotmail.com",
    "outlook.com", "icloud.com", "googlemail.com",
}


def discover_client_emails() -> int:
    """Backfill clients.emails + phones din interacțiunile confirmate CTS.

    Surse 100% confirmed (nu euristice):
    1. Mailuri PRIMITE cu iris_client_id în cts_ground_truth.raw.extra → from_address → emails
    2. Mailuri TRIMISE de agent cu iris_client_id → to_addresses jsonb → emails
    Adrese free-mail filtrate. Adaugă doar adrese NOI (nu suprascrie ce e deja setat).
    Returnează numărul de clienți actualizați.
    """
    updated = 0
    try:
        with _conn() as conn:
            cur = conn.cursor()

            # Adrese descoperite din received (from_address) + sent (to_addresses)
            cur.execute("""
                WITH addr_received AS (
                    SELECT
                        c.id AS client_id,
                        LOWER(e.from_address) AS addr
                    FROM clients c
                    JOIN cts_ground_truth gt
                        ON (gt.raw->'extra'->>'client_id') ~ '^[0-9]+$'
                        AND (gt.raw->'extra'->>'client_id')::bigint = c.iris_client_id
                    JOIN emails e ON e.id = gt.email_id
                    WHERE c.is_active = TRUE
                      AND c.iris_client_id IS NOT NULL
                      AND COALESCE(gt.cts_direction, 'received') = 'received'
                      AND e.from_address IS NOT NULL
                      AND e.from_address <> ''
                ),
                addr_sent AS (
                    -- to_addresses din emails table (mailuri sent cu row propriu)
                    SELECT
                        c.id AS client_id,
                        LOWER(addr_elem) AS addr
                    FROM clients c
                    JOIN cts_ground_truth gt
                        ON (gt.raw->'extra'->>'client_id') ~ '^[0-9]+$'
                        AND (gt.raw->'extra'->>'client_id')::bigint = c.iris_client_id
                    JOIN emails e ON e.id = gt.email_id,
                    jsonb_array_elements_text(COALESCE(e.to_addresses, '[]'::jsonb)) AS addr_elem
                    WHERE c.is_active = TRUE
                      AND c.iris_client_id IS NOT NULL
                      AND gt.cts_direction = 'sent'
                      AND addr_elem IS NOT NULL AND addr_elem <> ''
                    UNION
                    -- raw.extra.to_email (reply-uri CTS fara row separat in emails)
                    SELECT
                        c.id AS client_id,
                        LOWER(gt.raw->'extra'->>'to_email') AS addr
                    FROM clients c
                    JOIN cts_ground_truth gt
                        ON (gt.raw->'extra'->>'client_id') ~ '^[0-9]+$'
                        AND (gt.raw->'extra'->>'client_id')::bigint = c.iris_client_id
                    WHERE c.is_active = TRUE
                      AND c.iris_client_id IS NOT NULL
                      AND gt.cts_direction = 'sent'
                      AND gt.raw->'extra'->>'to_email' IS NOT NULL
                      AND gt.raw->'extra'->>'to_email' <> ''
                ),
                all_addrs AS (
                    SELECT client_id, addr FROM addr_received
                    UNION
                    SELECT client_id, addr FROM addr_sent
                ),
                filtered AS (
                    SELECT client_id, addr
                    FROM all_addrs
                    WHERE SPLIT_PART(addr, '@', 2) NOT IN (
                        'gmail.com','yahoo.com','yahoo.ro','hotmail.com',
                        'outlook.com','icloud.com','googlemail.com'
                    )
                      AND addr LIKE '%@%'
                      -- Adresele NOASTRE nu identifica un client: in CTS ele inseamna
                      -- „agentul care gestioneaza clientul". Fara filtrul asta, sync-ul
                      -- reintroducea office@cargotrack.ro & adresele colegilor in
                      -- clients.emails (71 clienti, 193 adrese, curatate de migratia
                      -- 20260729c), iar match_client() atribuia apoi un client arbitrar
                      -- oricarui email trimis de un coleg.
                      AND addr NOT LIKE '%cargotrack.ro'
                      AND addr NOT LIKE '%trakosoft.ro'
                ),
                new_per_client AS (
                    SELECT
                        client_id,
                        jsonb_agg(DISTINCT addr ORDER BY addr) AS discovered
                    FROM filtered
                    GROUP BY client_id
                )
                UPDATE clients c
                SET emails = (
                    SELECT jsonb_agg(DISTINCT val ORDER BY val)
                    FROM (
                        SELECT jsonb_array_elements_text(
                            COALESCE(NULLIF(c.emails, '[]'::jsonb), '[]'::jsonb)
                        ) AS val
                        UNION
                        SELECT jsonb_array_elements_text(npc.discovered) AS val
                    ) merged
                ),
                updated_at = NOW()
                FROM new_per_client npc
                WHERE c.id = npc.client_id
                  AND NOT (
                    COALESCE(c.emails, '[]'::jsonb) @> npc.discovered
                    AND npc.discovered @> COALESCE(c.emails, '[]'::jsonb)
                  )
            """)
            updated = cur.rowcount
            conn.commit()
            logger.info("discover_client_emails: %d clienti actualizati", updated)
    except Exception:
        logger.exception("discover_client_emails: eroare")
    return updated


# ── Sync periodic de clienti (vehicule + contracte) ──────────────────────────
# Pina la 2026-08-14 sync-ul rula DOAR la apasarea butonului din UI: nu exista cron,
# timer sau task care sa-l cheme, iar vehiculele/contractele au ramas inghetate din
# 29.07. Cheia `client_sync_interval_minutes` exista in settings, dar nu o citea nimeni.
CLIENT_SYNC_DEFAULT_MINUTES = 60
CLIENT_SYNC_MIN_MINUTES = 5          # un pull complet (~16k clienti) dureaza 60-90s
_NEXT_SYNC_KEY = "client_assets.next_sync_at"


def client_sync_interval_minutes() -> int:
    """Intervalul configurat in settings, cu podea de siguranta."""
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM settings WHERE key = 'client_sync_interval_minutes'")
            row = cur.fetchone()
        val = int(str(row[0]).strip('"')) if row and row[0] is not None else CLIENT_SYNC_DEFAULT_MINUTES
    except Exception:
        return CLIENT_SYNC_DEFAULT_MINUTES
    return max(CLIENT_SYNC_MIN_MINUTES, val)


def claim_client_sync() -> bool:
    """True daca ACEST proces a cistigat dreptul sa ruleze sync-ul acum.

    API-ul ruleaza cu 4 workeri gunicorn = 4 procese separate, deci `_CLIENT_SYNC_LOCK`
    (lock de threading, per-proces) NU ii poate coordona intre ei. Claim-ul se face
    atomic in DB: `ON CONFLICT ... DO UPDATE ... WHERE scadent` muta scadenta si
    intoarce rind exact unui singur worker. Un "citeste, compara, scrie" ar lasa toti
    patru sa porneasca simultan acelasi pull de 16k clienti.

    Scadenta persista in DB, deci un restart/deploy nu reporneste numaratoarea de la zero.
    """
    minutes = client_sync_interval_minutes()
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO settings (key, value, description, updated_at)
                VALUES (%s, to_jsonb((NOW() + make_interval(mins => %s))::text),
                        'Cind ruleaza urmatorul sync de clienti (claim intre workeri)', NOW())
                ON CONFLICT (key) DO UPDATE
                   SET value = to_jsonb((NOW() + make_interval(mins => %s))::text),
                       updated_at = NOW()
                 WHERE (settings.value #>> '{}')::timestamptz <= NOW()
                RETURNING 1
            """, (_NEXT_SYNC_KEY, minutes, minutes))
            won = cur.fetchone() is not None
            conn.commit()
        return won
    except Exception:
        logger.exception("claim_client_sync failed")
        return False


def sync_clients_guarded():
    """Wrapper cu lock anti-suprapunere pt rularea in fundal (daemon thread)."""
    if not _CLIENT_SYNC_LOCK.acquire(blocking=False):
        return {"status": "running", "message": "Sync deja in curs"}
    _write_client_sync_state({"status": "running"})
    try:
        res = sync_clients_from_iris()
        return res
    except Exception as e:
        logger.exception("client sync failed")
        _write_client_sync_state({"status": "error", "message": str(e)[:200]})
        return {"status": "error", "message": str(e)[:200]}
    finally:
        _CLIENT_SYNC_LOCK.release()



def _conn():
    return psycopg2.connect(
        host=settings.db_host, port=settings.db_port,
        dbname=settings.db_name, user=settings.db_user, password=settings.db_password,
    )


def _norm_email_priority(v):
    """Normalizeaza email_priority din IRIS la 1 (urgent) / 2 (normal).
    Orice valoare != 1 si non-null devine 2 (P4, etc. -> normal). None ramane None."""
    if v is None:
        return None
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return 2
    return 1 if n == 1 else 2


def _pick(d, *keys, default=None):
    """Primul camp non-null dintr-un dict, tolerant la denumiri alternative."""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


#: Domeniile noastre. O adresa din ele, pusa pe un client in CTS, e agentul care il
#: gestioneaza (sau un placeholder de tip `fara_email@`), nu adresa clientului.
_INTERNAL_CLIENT_DOMAINS = ("cargotrack.ro", "trakosoft.ro")


def _is_internal_client_address(addr) -> bool:
    a = str(addr or "").strip().lower()
    if "@" not in a:
        return False
    dom = a.rsplit("@", 1)[-1]
    return any(dom == d or dom.endswith("." + d) for d in _INTERNAL_CLIENT_DOMAINS)


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        # IRIS ar putea intoarce {"items": [...]} sau {"data": [...]}
        for k in ("items", "data", "records", "list"):
            if isinstance(v.get(k), list):
                return v[k]
    return []


def _norm_date(v):
    """Returneaza 'YYYY-MM-DD' sau None. Accepta date/datetime/ISO string."""
    if not v:
        return None
    s = str(v).strip()
    if not s:
        return None
    # pastram doar partea de data (primii 10 caractere daca e ISO datetime)
    return s[:10]


def _norm_docs(raw_docs):
    """Normalizeaza lista de documente la [{tip,status,data}]. Pastreaza raw extra."""
    out = []
    for d in _as_list(raw_docs):
        if isinstance(d, str):
            out.append({"tip": d, "status": None, "data": None})
            continue
        if not isinstance(d, dict):
            continue
        out.append({
            "tip": _pick(d, "tip", "type", "doc_type", "name", "denumire"),
            "status": _pick(d, "status", "stare", "valid"),
            "data": _norm_date(_pick(d, "data_expirare", "data_incarcare", "expira",
                                     "expiry", "uploaded_at", "data", "date")),
            "format": _pick(d, "format", "ext", "extension"),
            "filename": _pick(d, "filename", "nume_fisier", "file"),
        })
    return out


def _vehicle_plates(raw_vehicles):
    """Din lista de vehicule a unui contract extrage numerele de inmatriculare (string-uri)."""
    out = []
    for v in _as_list(raw_vehicles):
        if isinstance(v, str):
            p = v.strip()
            if p:
                out.append(p)
        elif isinstance(v, dict):
            p = _pick(v, "numar_inmatriculare", "plate", "nr_inmatriculare",
                      "registration", "numar")
            if p:
                out.append(str(p).strip())
    return out


def _sync_client_assets(cur, client_id, iris_id, c):
    """Populeaza client_vehicles / client_contracts pentru un client (delete + reinsert,
    idempotent). No-op daca feed-ul nu contine vehicles/contracts. Returneaza (nv, nc)."""
    has_vehicles = any(k in c for k in ("vehicles", "vehicule", "masini", "cars"))
    has_contracts = any(k in c for k in ("contracts", "contracte", "contract_list"))
    nv = nc = 0

    if has_vehicles:
        vehicles = _as_list(_pick(c, "vehicles", "vehicule", "masini", "cars", default=[]))
        cur.execute("DELETE FROM client_vehicles WHERE client_id = %s", (client_id,))
        seen = set()
        for v in vehicles:
            if not isinstance(v, dict):
                continue
            plate = _pick(v, "numar_inmatriculare", "plate", "nr_inmatriculare",
                          "registration", "numar")
            plate = str(plate).strip() if plate else None
            key = (plate or "").lower()
            if key in seen:
                continue
            seen.add(key)
            status = _pick(v, "status", "stare")
            vin = _pick(v, "vin", "vin_code", "serie_sasiu", "serie_sasie", "chassis", "chassis_no")
            vin = str(vin).strip().upper() if vin and str(vin).strip() else None
            docs = _norm_docs(_pick(v, "documents", "documente", "docs"))
            cur.execute("""
                INSERT INTO client_vehicles(client_id, iris_client_id, plate, vin, status, documents, raw, synced_at)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, NOW())
                ON CONFLICT (client_id, lower(COALESCE(plate, ''))) DO UPDATE SET
                    vin=EXCLUDED.vin, status=EXCLUDED.status, documents=EXCLUDED.documents,
                    raw=EXCLUDED.raw, synced_at=NOW()
            """, (client_id, iris_id, plate, vin, status,
                  json.dumps(docs), json.dumps(v)))
            nv += 1

    if has_contracts:
        contracts = _as_list(_pick(c, "contracts", "contracte", "contract_list", default=[]))
        cur.execute("DELETE FROM client_contracts WHERE client_id = %s", (client_id,))
        seen = set()
        for ct in contracts:
            if not isinstance(ct, dict):
                continue
            cid = _pick(ct, "iris_contract_id", "id", "contract_id")
            cid = str(cid).strip() if cid is not None else None
            ctype = _pick(ct, "tip_contract", "contract_type", "tip", "type")
            ccat = _pick(ct, "categorie", "category", "categorie_contract")
            sd = _norm_date(_pick(ct, "data_start", "start_date", "valabil_de", "start"))
            ed = _norm_date(_pick(ct, "data_end", "end_date", "valabil_pana", "expira", "end"))
            status = _pick(ct, "status", "stare")
            docs = _norm_docs(_pick(ct, "documents", "documente", "docs"))
            plates = _vehicle_plates(_pick(ct, "vehicles", "vehicule", "masini", default=[]))
            cno = _pick(ct, "numar_contract", "contract_no", "nr_contract", "serie_contract", "serie", "numar")
            cno = str(cno).strip() if cno is not None and str(cno).strip() else None
            ccui = _pick(ct, "cui_client", "cui", "cif", "cod_fiscal", "vat")
            ccui = str(ccui).strip() if ccui is not None and str(ccui).strip() else None
            dedup_key = (cid or "", (ctype or "").lower(), sd or "", ed or "")
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            cur.execute("""
                INSERT INTO client_contracts(client_id, iris_client_id, iris_contract_id,
                    contract_type, category, start_date, end_date, status, documents, vehicles, contract_no, cui, raw, synced_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s::jsonb, NOW())
                ON CONFLICT (client_id, lower(COALESCE(iris_contract_id, '')),
                             lower(COALESCE(contract_type, '')),
                             COALESCE(start_date, '0001-01-01'::date),
                             COALESCE(end_date, '0001-01-01'::date)) DO UPDATE SET
                    category=EXCLUDED.category,
                    status=EXCLUDED.status, documents=EXCLUDED.documents,
                    vehicles=EXCLUDED.vehicles, contract_no=EXCLUDED.contract_no,
                    cui=EXCLUDED.cui, raw=EXCLUDED.raw, synced_at=NOW()
            """, (client_id, iris_id, cid, ctype, ccat, sd, ed, status,
                  json.dumps(docs), json.dumps(plates), cno, ccui, json.dumps(ct)))
            nc += 1

    return nv, nc


def sync_clients_from_iris() -> dict:
    """Fetch clients from IRIS Gateway + upsert into mailguard.clients."""
    iris_url = settings.iris_api_url.rstrip('/')
    # Use dedicated CARGO360 key
    import os
    mg_key = os.getenv('IRIS_MAILGUARD_API_KEY', '')
    if not mg_key:
        return {"status": "error", "message": "IRIS_MAILGUARD_API_KEY missing"}

    try:
        with httpx.Client(timeout=90, verify=False) as cl:
            r = cl.get(f"{iris_url}/clients/contact-list",
                       params={"include": "vehicles,contracts"},
                       headers={"X-Mailguard-Key": mg_key})
            if r.status_code != 200:
                return {"status": "error", "code": r.status_code, "message": r.text[:200]}
            clients = r.json()
    except Exception as e:
        return {"status": "error", "message": str(e)[:200]}

    inserted, updated = 0, 0
    vehicles_synced, contracts_synced, clients_with_assets = 0, 0, 0
    seen_ids = set()
    with _conn() as conn:
        cur = conn.cursor()
        for c in clients:
            iris_id = c.get('iris_client_id')
            if iris_id is None:
                continue
            seen_ids.add(iris_id)
            # Adresele NOASTRE nu identifica un client: in CTS ele inseamna „agentul care
            # gestioneaza clientul" (`office@`, adrese de colegi, `fara_email@`). Le separam
            # ca `match_client()` sa nu atribuie un client arbitrar oricarui email trimis de
            # un coleg (vezi migratia 20260729c). Nu se pierd: merg in internal_contact_emails.
            _emails_all = c.get('emails') or []
            _emails_ext = [a for a in _emails_all if not _is_internal_client_address(a)]
            _emails_int = [str(a).strip().lower() for a in _emails_all
                           if _is_internal_client_address(a)]
            emails_json = json.dumps(_emails_ext)
            internal_emails_json = json.dumps(sorted(set(_emails_int)))
            phones_json = json.dumps(c.get('phones') or [])
            email_priority = _norm_email_priority(
                c.get('email_priority', c.get('priority', c.get('mail_priority'))))
            ccui = _pick(c, "cui", "cui_client", "cif", "cod_fiscal", "vat")
            ccui = str(ccui).strip() if ccui is not None and str(ccui).strip() else None
            # emails/phones: dacă IRIS trimite [] dar avem adrese descoperite local, le păstrăm.
            # EXCLUDED.emails = [] nu suprascrie adresele existente ne-goale (discovered local).
            cur.execute("""
                INSERT INTO clients(iris_client_id, name, cui, emails, internal_contact_emails,
                    phones, is_active, email_priority, last_synced_at, updated_at)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, NOW(), NOW())
                ON CONFLICT (iris_client_id) DO UPDATE SET
                    name=EXCLUDED.name, cui=COALESCE(EXCLUDED.cui, clients.cui),
                    emails=CASE
                        WHEN EXCLUDED.emails IS NOT NULL AND jsonb_array_length(EXCLUDED.emails) > 0
                        THEN EXCLUDED.emails
                        -- IRIS nu trimite nicio adresa EXTERNA: pastram ce avem local
                        -- (descoperit din interactiuni), dar DOAR partea externa. Altfel un
                        -- client care are in CTS exclusiv adrese interne rămânea cu ele in
                        -- `emails` si redevenea tinta de match arbitrar.
                        -- Atentie: procentul se dubleaza in acest query (psycopg2 il trateaza
                        -- ca placeholder de parametru cand execute() primeste argumente).
                        ELSE COALESCE(NULLIF((
                            SELECT COALESCE(jsonb_agg(v), '[]'::jsonb)
                            FROM jsonb_array_elements_text(COALESCE(clients.emails, '[]'::jsonb)) v
                            WHERE lower(v) NOT LIKE '%%cargotrack.ro'
                              AND lower(v) NOT LIKE '%%trakosoft.ro'
                        ), '[]'::jsonb), EXCLUDED.emails)
                    END,
                    internal_contact_emails=CASE
                        WHEN jsonb_array_length(EXCLUDED.internal_contact_emails) > 0
                        THEN EXCLUDED.internal_contact_emails
                        ELSE clients.internal_contact_emails
                    END,
                    phones=CASE
                        WHEN EXCLUDED.phones IS NOT NULL AND jsonb_array_length(EXCLUDED.phones) > 0
                        THEN EXCLUDED.phones
                        ELSE COALESCE(NULLIF(clients.phones, '[]'::jsonb), EXCLUDED.phones)
                    END,
                    is_active=EXCLUDED.is_active, email_priority=EXCLUDED.email_priority,
                    last_synced_at=NOW(), updated_at=NOW()
                RETURNING id, (xmax = 0) AS inserted
            """, (iris_id, c.get('name'), ccui, emails_json, internal_emails_json, phones_json,
                  c.get('is_active', True), email_priority))
            row = cur.fetchone()
            local_id = row[0] if row else None
            if row and row[1]:
                inserted += 1
            else:
                updated += 1
            # OPS-0124: vehicule + contracte (no-op daca feed-ul nu le contine)
            if local_id is not None:
                try:
                    nv, nc = _sync_client_assets(cur, local_id, iris_id, c)
                    if nv or nc:
                        clients_with_assets += 1
                    vehicles_synced += nv
                    contracts_synced += nc
                except Exception:
                    logger.exception("client assets sync failed for iris_id=%s", iris_id)
        # Mark missing clients as inactive
        if seen_ids:
            cur.execute("""
                UPDATE clients SET is_active=FALSE
                WHERE iris_client_id NOT IN %s AND is_active=TRUE
            """, (tuple(seen_ids),))
            deactivated = cur.rowcount
        else:
            deactivated = 0
        # Observabilitate (informativ)
        try:
            cur.execute("""
                UPDATE settings SET value = %s::jsonb, updated_at = NOW()
                WHERE key = 'client_assets.last_result'
            """, (json.dumps({
                "status": "ok",
                "fetched": len(clients), "inserted": inserted, "updated": updated,
                "deactivated": deactivated,
                "vehicles": vehicles_synced, "contracts": contracts_synced,
                "clients_with_assets": clients_with_assets,
            }),))
        except Exception:
            logger.exception("client_assets.last_result update failed")
        conn.commit()

    discovered = discover_client_emails()

    # Reconstruieste indexul de chei de telefon (client_phone_keys) dupa ce clients.phones
    # s-a schimbat. Fara asta, apelurile clientilor nou-sincronizati nu se mai leaga:
    # match_client_by_phone() citeste din index, nu din clients.phones.
    phone_keys = None
    unique_emails = None
    try:
        from app.services.phone_match import rebuild_phone_index, rebuild_client_unique_emails
        phone_keys = rebuild_phone_index()
        # Idem pentru adresele care identifica unic un client (legarea mailurilor orfane in
        # calculul de satisfactie) — depinde de clients.emails, deci se reface dupa sync.
        unique_emails = rebuild_client_unique_emails()
    except Exception:
        logger.exception("rebuild indexuri client failed after client sync")

    return {"status": "ok", "fetched": len(clients), "inserted": inserted,
            "updated": updated, "deactivated": deactivated,
            "vehicles": vehicles_synced, "contracts": contracts_synced,
            "clients_with_assets": clients_with_assets,
            "emails_discovered": discovered,
            "phone_keys_indexed": phone_keys,
            "unique_emails_indexed": unique_emails}
