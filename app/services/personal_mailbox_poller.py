"""Personal mailbox poller (T1+T2) — runs via cargo360-personal-poll systemd timer (1 min).

Reads active accounts, fetches new email metadata via IMAP (incremental by UID),
stores metadata-only in personal_mails. No mail body is stored in DB.

After metadata insert, T2 detection runs transiently (body fetched, scanned, discarded).

T4 extension point: after detection verdict, personal_imap.move_to_folder() will be
called to move spam/quarantined mails to SPAM/CARANTINA IMAP folders.
"""
import logging
import time
from typing import Optional

import psycopg2
import psycopg2.extras

from app.config import get_settings
from app.services.credential_crypto import decrypt_credentials
from app.services import personal_imap, vathub_forward

logger = logging.getLogger("mailguard.personal_poller")
settings = get_settings()

POLL_INTERVAL_S = 60          # target poll interval per account
MAX_UID_PER_RUN = 500         # safety cap — matches personal_imap.py


def _conn():
    return psycopg2.connect(
        host=settings.db_host, port=settings.db_port,
        dbname=settings.db_name, user=settings.db_user, password=settings.db_password,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def _smtp_password(account: dict) -> Optional[str]:
    """Parola SMTP separată, dacă a fost salvată.

    None înseamnă „aceeași ca la IMAP" — vezi personal_smtp.resolve_smtp().
    """
    enc = account.get("smtp_cred_enc")
    if not enc:
        return None
    try:
        return decrypt_credentials(enc)["pass"]
    except Exception as e:
        logger.warning(f"account {account.get('id')}: cannot decrypt SMTP creds: {e}")
        return None


def _get_due_accounts(cur) -> list[dict]:
    """Return active accounts that are due for polling (last_poll_at + 60s <= now)."""
    cur.execute("""
        SELECT id, user_id, label, imap_host, imap_port, imap_ssl,
               email_address, cred_enc, last_uid, last_poll_at,
               smtp_host, smtp_port, smtp_tls, smtp_user, smtp_cred_enc,
               vathub_enabled, filter_enabled
        FROM personal_mailbox_accounts
        WHERE status = 'active'
          AND (last_poll_at IS NULL
               OR last_poll_at <= now() - interval '60 seconds')
        ORDER BY last_poll_at ASC NULLS FIRST
    """)
    return [dict(r) for r in cur.fetchall()]


_FOLDER_MAP = {
    "move_spam": "SPAM",
    "move_quarantine": "CARANTINA",
}


def _move_pending_folder_actions(account: dict, password: str, cur, conn) -> int:
    """Move mails with folder_action != 'none' and folder_action_at IS NULL to IMAP folders.

    Idempotent: only processes rows where folder_action_at is NULL.
    On IMAP failure per-mail, leaves folder_action_at NULL → retried on next poll.
    Returns count of successfully moved mails.
    """
    account_id = account["id"]

    cur.execute("""
        SELECT id, imap_uid, folder_action
        FROM personal_mails
        WHERE account_id = %s
          AND folder_action != 'none'
          AND folder_action_at IS NULL
        ORDER BY imap_uid
    """, (account_id,))
    pending = [dict(r) for r in cur.fetchall()]

    if not pending:
        return 0

    # Ensure target folders exist (idempotent, one IMAP connection)
    try:
        personal_imap.ensure_folders(account, password)
    except Exception as e:
        logger.warning(f"ensure_folders failed account {account_id}: {e}")
        # Continue — move_to_folder may still succeed if folders already exist

    moved = 0
    for mail in pending:
        folder = _FOLDER_MAP.get(mail["folder_action"])
        if not folder:
            logger.warning(f"Unknown folder_action '{mail['folder_action']}' mail_id={mail['id']}")
            continue

        ok = personal_imap.move_to_folder(account, password, mail["imap_uid"], folder)
        if ok:
            cur.execute("""
                UPDATE personal_mails SET folder_action_at = now() WHERE id = %s
            """, (mail["id"],))
            moved += 1
            logger.info(
                f"account {account_id} uid={mail['imap_uid']} moved to {folder}"
            )
        else:
            logger.warning(
                f"account {account_id} uid={mail['imap_uid']} move to {folder} failed — will retry"
            )

    if moved:
        conn.commit()

    return moved


def _poll_one(account: dict, cur, conn) -> dict:
    """Poll one mailbox: ingest metadata (T1) + run detection (T2). Returns summary dict."""
    account_id = account["id"]
    email_addr = account["email_address"]
    try:
        creds = decrypt_credentials(account["cred_enc"])
        password = creds["pass"]
    except Exception as e:
        logger.warning(f"account {account_id} ({email_addr}): cannot decrypt creds: {e}")
        cur.execute("""
            UPDATE personal_mailbox_accounts
            SET status='error', last_error=%s, updated_at=now()
            WHERE id=%s
        """, (f"Credential decrypt error: {str(e)[:200]}", account_id))
        conn.commit()
        return {"account_id": account_id, "error": "decrypt_failed", "inserted": 0}

    mails = personal_imap.fetch_new_metadata(account, password)
    inserted = 0
    max_uid = int(account.get("last_uid") or 0)

    for m in mails:
        uid = m["imap_uid"]
        if uid > max_uid:
            max_uid = uid
        try:
            cur.execute("""
                INSERT INTO personal_mails
                    (account_id, user_id, imap_uid, message_id, from_address,
                     subject, received_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (account_id, imap_uid) DO NOTHING
            """, (
                account_id, account["user_id"], uid,
                m.get("message_id"), m.get("from_address"),
                m.get("subject"), m.get("received_at"),
            ))
            if cur.rowcount:
                inserted += 1
        except Exception as e:
            logger.warning(f"account {account_id} insert uid={uid}: {e}")

    cur.execute("""
        UPDATE personal_mailbox_accounts
        SET last_poll_at=now(), last_uid=%s, last_error=NULL, updated_at=now()
        WHERE id=%s
    """, (max_uid, account_id))
    conn.commit()

    if inserted:
        logger.info(f"account {account_id} ({email_addr}): +{inserted} mails (max_uid={max_uid})")

    # T2 + T4 rulează doar cu filtrarea pornită. Cu filtrul OFF mailurile se
    # ingerează în continuare (metadata + redirect VATHUB), dar nu se scanează
    # și nu se mută nimic în SPAM/CARANTINA. Rândurile rămase 'pending' se
    # marchează 'filter_off', altfel ar fi scanate retroactiv la repornirea
    # filtrului — exact ce nu vrea un utilizator care l-a oprit deliberat.
    detected = 0
    moved = 0
    if account.get("filter_enabled", True):
        try:
            from app.services import personal_mail_processor
            detected = personal_mail_processor.process_account_pending(account, password, cur, conn)
        except Exception as e:
            logger.exception(f"T2 detection failed account {account_id}: {e}")

        # T4: move spam/quarantined mails to IMAP folders
        moved = _move_pending_folder_actions(account, password, cur, conn)
    else:
        cur.execute("""
            UPDATE personal_mails
            SET verdict='filter_off', folder_action='none'
            WHERE account_id=%s AND verdict='pending'
        """, (account_id,))
        if cur.rowcount:
            logger.info(f"account {account_id}: filtrare OFF — {cur.rowcount} mailuri nescanate")
        conn.commit()

    # VATHUB: redirect mailuri de la autorități fiscale spre căsuța generală
    vathub = vathub_forward.process_account(
        account, password, _smtp_password(account), cur, conn
    )

    return {"account_id": account_id, "inserted": inserted, "detected": detected,
            "moved": moved, "max_uid": max_uid, "vathub": vathub}


def run() -> dict:
    """Main entry point — called by cargo360-personal-poll timer or /personal-mailboxes/poll."""
    started = time.time()
    results = []
    try:
        conn = _conn()
        cur = conn.cursor()
        accounts = _get_due_accounts(cur)

        if not accounts:
            return {"accounts_polled": 0, "total_inserted": 0, "elapsed_ms": 0}

        # Stagger: spread accounts evenly across 60s window to respect rate limits
        spread_ms = POLL_INTERVAL_S * 1000 // max(len(accounts), 1)

        for i, account in enumerate(accounts):
            if i > 0 and spread_ms > 0:
                time.sleep(spread_ms / 1000)
            try:
                r = _poll_one(account, cur, conn)
                results.append(r)
            except Exception as e:
                logger.exception(f"poll_one failed account {account['id']}: {e}")
                try:
                    cur.execute("""
                        UPDATE personal_mailbox_accounts
                        SET status='needs_reconnect', last_error=%s, updated_at=now()
                        WHERE id=%s
                    """, (str(e)[:200], account["id"]))
                    conn.commit()
                except Exception:
                    pass
                results.append({"account_id": account["id"], "error": str(e)[:100]})

        cur.close()
        conn.close()
    except Exception as e:
        logger.exception(f"personal_mailbox_poller.run() fatal: {e}")
        return {"error": str(e)[:200], "accounts_polled": 0}

    total_inserted = sum(r.get("inserted", 0) for r in results)
    total_detected = sum(r.get("detected", 0) for r in results)
    total_moved = sum(r.get("moved", 0) for r in results)
    total_vathub = sum((r.get("vathub") or {}).get("sent", 0) for r in results)
    elapsed_ms = int((time.time() - started) * 1000)
    return {
        "accounts_polled": len(results),
        "total_inserted": total_inserted,
        "total_detected": total_detected,
        "total_moved": total_moved,
        "total_vathub_forwarded": total_vathub,
        "elapsed_ms": elapsed_ms,
        "details": results,
    }
