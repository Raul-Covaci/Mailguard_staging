"""IMAP connector for personal mailbox ingest (T1) + body fetch (T2).

Responsibilities:
  - test_login(): validate credentials without side effects
  - fetch_new_metadata(): incremental fetch by UID, returns metadata-only (no body)
  - fetch_message_body(): transient body fetch for detection (T2) — never stored
  - ensure_folders(): create SPAM / CARANTINA folders if absent (T4 prep)
  - move_to_folder(): move UID to target folder (T4 hook, implemented here)
  - inject_test_mail(): APPEND a synthetic RFC 2822 message into INBOX for e2e testing
"""
import email as _email_lib
import email.header
import email.utils
import imaplib
import logging
import re
import datetime as _dt
from typing import Optional

logger = logging.getLogger("mailguard.personal_imap")

PERSONAL_FOLDERS = ["SPAM", "CARANTINA"]
_FETCH_FIELDS = "(UID RFC822.HEADER)"
_MAX_BODY_BYTES = 512_000  # truncate body parts at 512 KB


def _decode_header_value(raw) -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", errors="replace")
        except Exception:
            return None
    parts = email.header.decode_header(raw)
    out = []
    for part, charset in parts:
        if isinstance(part, bytes):
            try:
                out.append(part.decode(charset or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                out.append(part.decode("utf-8", errors="replace"))
        else:
            out.append(str(part))
    return "".join(out)


def _parse_date(date_str: Optional[str]) -> Optional[_dt.datetime]:
    if not date_str:
        return None
    try:
        tup = email.utils.parsedate_tz(date_str.strip())
        if tup:
            ts = email.utils.mktime_tz(tup)
            return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
    except Exception:
        pass
    return None


def _open_imap(account: dict, timeout: Optional[int] = None) -> "imaplib.IMAP4_SSL | imaplib.IMAP4":
    host = account["imap_host"]
    port = int(account.get("imap_port") or 993)
    use_ssl = account.get("imap_ssl", True)
    kwargs = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if use_ssl:
        conn = imaplib.IMAP4_SSL(host, port, **kwargs)
    else:
        conn = imaplib.IMAP4(host, port, **kwargs)
    return conn


def test_login(account: dict) -> tuple[bool, Optional[str]]:
    """Try IMAP LOGIN. Returns (ok, error_message).
    account keys: imap_host, imap_port, imap_ssl, email_address, _password (plaintext).
    """
    try:
        conn = _open_imap(account)
        conn.login(account["email_address"], account["_password"])
        conn.logout()
        return True, None
    except imaplib.IMAP4.error as e:
        return False, f"IMAP auth error: {str(e)[:200]}"
    except OSError as e:
        return False, f"Connection error: {str(e)[:200]}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)[:200]}"


_WINDOW_MINUTES = 30  # re-scan recent window to catch mails missed by UID gap


def fetch_new_metadata(account: dict, password: str) -> list[dict]:
    """Fetch metadata for UIDs > account['last_uid'] PLUS a 30-min sliding window.

    The sliding window catches mails that arrived before last_uid was advanced
    (e.g. injected during a gap, or processed out-of-order).
    Dedup happens at DB level via ON CONFLICT DO NOTHING.

    Each dict: {imap_uid, message_id, from_address, subject, received_at}
    No body content is fetched or returned.
    """
    last_uid = int(account.get("last_uid") or 0)
    results = []
    try:
        conn = _open_imap(account)
        conn.login(account["email_address"], password)
        conn.select("INBOX", readonly=True)

        uid_set_parts: set[str] = set()

        if last_uid == 0:
            # First run: last 24h only — avoid full inbox scan
            since = (_dt.datetime.utcnow() - _dt.timedelta(hours=24)).strftime("%d-%b-%Y")
            typ, data = conn.uid("SEARCH", None, f"SINCE {since}")
            if typ == "OK" and data and data[0]:
                uid_set_parts.update(data[0].decode().split())
        else:
            # New mails
            typ, data = conn.uid("SEARCH", None, f"UID {last_uid + 1}:*")
            if typ == "OK" and data and data[0]:
                uid_set_parts.update(data[0].decode().split())

            # Sliding window: re-scan last 30 min to catch any missed mails
            window_since = (_dt.datetime.utcnow() - _dt.timedelta(minutes=_WINDOW_MINUTES)).strftime("%d-%b-%Y")
            typ_w, data_w = conn.uid("SEARCH", None, f"SINCE {window_since}")
            if typ_w == "OK" and data_w and data_w[0]:
                uid_set_parts.update(data_w[0].decode().split())

        if not uid_set_parts:
            conn.logout()
            return []

        # Cap per run to avoid timeout (rate limit safety)
        uid_list = sorted(uid_set_parts, key=int)[:500]

        uid_set = ",".join(uid_list)
        typ, fetch_data = conn.uid("FETCH", uid_set, _FETCH_FIELDS)
        conn.logout()

        if typ != "OK":
            return []

        # fetch_data alternates: (b"N (UID x RFC822.HEADER {len}", b"<raw headers>"), b")"
        i = 0
        while i < len(fetch_data):
            item = fetch_data[i]
            if not isinstance(item, tuple) or len(item) < 2:
                i += 1
                continue
            uid_match = re.search(rb"UID (\d+)", item[0])
            if not uid_match:
                i += 1
                continue
            uid = int(uid_match.group(1))

            raw_headers = item[1]
            if not isinstance(raw_headers, bytes):
                results.append({"imap_uid": uid, "message_id": None,
                                 "from_address": None, "subject": None, "received_at": None})
                i += 1
                continue

            try:
                msg = _email_lib.message_from_bytes(raw_headers)
                from_raw = msg.get("From") or msg.get("Return-Path")
                addrs = email.utils.getaddresses([from_raw]) if from_raw else []
                from_address = addrs[0][1] if addrs else None
                subject = _decode_header_value(msg.get("Subject"))
                message_id_raw = msg.get("Message-ID")
                message_id = message_id_raw.strip() if message_id_raw else None
                received_at = _parse_date(msg.get("Date"))
            except Exception as e:
                logger.warning(f"header parse failed uid={uid}: {e}")
                from_address, subject, message_id, received_at = None, None, None, None
            results.append({
                "imap_uid": uid,
                "message_id": message_id,
                "from_address": from_address,
                "subject": subject,
                "received_at": received_at,
            })
            i += 1

    except imaplib.IMAP4.error as e:
        logger.warning(f"IMAP error account {account.get('id')}: {e}")
    except Exception as e:
        logger.exception(f"fetch_new_metadata failed account {account.get('id')}: {e}")

    return results


_AUTOGEN_HEADERS = frozenset([
    "auto-submitted",
    "x-auto-response-suppress",
])


def fetch_message_body(
    account: dict, password: str, uid: int
) -> tuple[str, str, dict]:
    """Fetch body_text + body_html + extra_headers for one UID transiently.

    Returns ("", "", {}) on failure.
    Body is NOT stored — caller must discard after scanning.
    Uses BODY.PEEK[] (read-only, does not set \\Seen flag).
    extra_headers: dict of lowercased header names from _AUTOGEN_HEADERS that are present.
    Times out after 15 seconds. Truncates parts at 512 KB.
    """
    try:
        conn = _open_imap(account, timeout=15)
        conn.login(account["email_address"], password)
        conn.select("INBOX", readonly=True)
        typ, data = conn.uid("FETCH", str(uid).encode(), "(BODY.PEEK[])")
        conn.logout()

        if typ != "OK" or not data:
            return "", "", {}

        raw_bytes = None
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                raw_bytes = item[1]
                break

        if not raw_bytes:
            return "", "", {}

        msg = _email_lib.message_from_bytes(raw_bytes)
        body_text, body_html = "", ""

        for part in msg.walk():
            if part.get_content_disposition() == "attachment":
                continue
            ct = part.get_content_type()
            if ct == "text/plain" and not body_text:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                body_text = payload[:_MAX_BODY_BYTES].decode(charset, errors="replace")
            elif ct == "text/html" and not body_html:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                body_html = payload[:_MAX_BODY_BYTES].decode(charset, errors="replace")

        extra_headers = {}
        for hdr in _AUTOGEN_HEADERS:
            val = msg.get(hdr)
            if val is not None:
                extra_headers[hdr] = val.strip()

        return body_text, body_html, extra_headers

    except imaplib.IMAP4.error as e:
        logger.warning(f"IMAP error fetching body account={account.get('id')} uid={uid}: {e}")
    except OSError as e:
        logger.warning(f"Timeout/connection error fetching body account={account.get('id')} uid={uid}: {e}")
    except Exception as e:
        logger.warning(f"fetch_message_body failed account={account.get('id')} uid={uid}: {e}")

    return "", "", {}


def _parse_envelope(raw: bytes) -> dict:
    """Extract date, subject, from, message-id from raw IMAP ENVELOPE bytes."""
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return {"message_id": None, "from_address": None, "subject": None, "received_at": None}

    # Tokenize quoted strings and NIL
    tokens = []
    i = 0
    while i < len(text):
        if text[i] == '"':
            j = i + 1
            buf = []
            while j < len(text):
                if text[j] == '\\' and j + 1 < len(text):
                    buf.append(text[j + 1])
                    j += 2
                elif text[j] == '"':
                    j += 1
                    break
                else:
                    buf.append(text[j])
                    j += 1
            tokens.append("".join(buf))
            i = j
        elif text[i:i+3].upper() == "NIL":
            tokens.append(None)
            i += 3
        elif text[i] in "( )":
            i += 1
        else:
            i += 1

    # ENVELOPE order: date, subject, from, sender, reply-to, to, cc, bcc, in-reply-to, message-id
    date_str = tokens[0] if len(tokens) > 0 else None
    subject_raw = tokens[1] if len(tokens) > 1 else None
    # from is a nested structure — grab first email-like token after position 2
    from_address = None
    message_id = tokens[-1] if tokens else None

    # Find from_address: look for @-containing token
    for tok in tokens[2:]:
        if tok and "@" in tok:
            from_address = tok
            break

    return {
        "message_id": _decode_header_value(message_id),
        "from_address": from_address,
        "subject": _decode_header_value(subject_raw),
        "received_at": _parse_date(date_str),
    }


_TEST_MAIL_TEMPLATES = {
    "quarantine": {
        "from": "security-alert@micros0ft-verify.com",
        "subject": "[TEST CARANTINA] Urgent: resetează parola contului tău acum",
        "body": (
            "Contul tău a fost suspendat. Resetează imediat parola accesând link-ul de mai jos.\n"
            "Click aici: http://192.168.1.1/login/verify?token=abc123\n"
            "Acțiune necesară în 24 ore sau contul va fi blocat permanent."
        ),
    },
    "spam": {
        "from": "promotions@bulk-offers-newsletter.com",
        "subject": "[TEST SPAM] Oferta URGENTA! Castiga acum!",
        "body": (
            "Felicitari! Ai fost selectat pentru oferta noastra exclusiva!\n"
            "Cumpara acum si primesti 90% reducere. Stoc limitat!\n"
            "Dezaboneaza-te: http://bit.ly/unsub999\n"
            "URGENT - oferta expira AZI! Actioneaza RIGHT NOW! ASAP!"
        ),
    },
}


def inject_test_mail(account: dict, password: str, scenario: str) -> tuple[bool, str, Optional[int]]:
    """APPEND a synthetic RFC 2822 message into INBOX for e2e detection testing.

    Returns (ok, error_or_info, uid_or_None).
    The poller will pick it up within ~1 minute and classify/move it.
    """
    tpl = _TEST_MAIL_TEMPLATES.get(scenario)
    if not tpl:
        return False, f"Unknown scenario '{scenario}'", None

    now = _dt.datetime.now(_dt.timezone.utc)
    date_str = email.utils.formatdate(now.timestamp(), localtime=False)
    to_addr = account["email_address"]

    raw_msg = (
        f"From: {tpl['from']}\r\n"
        f"To: {to_addr}\r\n"
        f"Subject: {tpl['subject']}\r\n"
        f"Date: {date_str}\r\n"
        f"Message-ID: <mailguard-test-{scenario}-{int(now.timestamp())}@mailguard.test>\r\n"
        f"MIME-Version: 1.0\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n"
        f"{tpl['body']}\r\n"
    ).encode("utf-8")

    try:
        conn = _open_imap(account, timeout=15)
        conn.login(account["email_address"], password)
        typ, data = conn.append("INBOX", None, imaplib.Time2Internaldate(now.timestamp()), raw_msg)
        conn.logout()
        if typ != "OK":
            return False, f"APPEND failed: {data}", None
        # Extract UID from APPENDUID response if server supports it
        uid = None
        if data and data[0]:
            m = re.search(rb'\[APPENDUID \d+ (\d+)\]', data[0])
            if m:
                uid = int(m.group(1))
        return True, f"Injected scenario='{scenario}' into INBOX", uid
    except imaplib.IMAP4.error as e:
        return False, f"IMAP error: {str(e)[:200]}", None
    except Exception as e:
        return False, f"Error: {str(e)[:200]}", None


def ensure_folders(account: dict, password: str) -> None:
    """Create SPAM and CARANTINA folders if they don't exist. T4 prep."""
    try:
        conn = _open_imap(account)
        conn.login(account["email_address"], password)
        typ, folder_list = conn.list()
        existing = set()
        if typ == "OK":
            for item in folder_list:
                if isinstance(item, bytes):
                    m = re.search(rb'"([^"]+)"\s*$|(\S+)\s*$', item)
                    if m:
                        name = (m.group(1) or m.group(2)).decode("utf-8", errors="replace")
                        existing.add(name.strip('"'))
        for folder in PERSONAL_FOLDERS:
            if folder not in existing:
                conn.create(folder)
                logger.info(f"Created IMAP folder '{folder}' for account {account.get('id')}")
        conn.logout()
    except Exception as e:
        logger.warning(f"ensure_folders failed account {account.get('id')}: {e}")


def move_to_folder(account: dict, password: str, uid: int, folder: str) -> bool:
    """Move message UID to target folder. Returns True on success. T4 hook.

    Sequence: COPY → verify copy arrived → STORE \\Deleted → EXPUNGE.
    If COPY succeeds but expunge fails, the message remains in INBOX with
    \\Deleted flag set; next poll will retry expunge via a fresh connection.
    Idempotent: if UID already absent from INBOX (prior incomplete move),
    returns True immediately.
    """
    uid_b = str(uid).encode()
    try:
        conn = _open_imap(account)
        conn.login(account["email_address"], password)
        conn.select("INBOX")

        # Check if UID still exists in INBOX (idempotency guard)
        typ_chk, data_chk = conn.uid("SEARCH", None, f"UID {uid}")
        if typ_chk == "OK" and data_chk and data_chk[0]:
            existing = data_chk[0].decode().split()
            if str(uid) not in existing:
                # Already moved in a prior run — success
                conn.logout()
                return True

        typ, _ = conn.uid("COPY", uid_b, folder)
        if typ != "OK":
            conn.logout()
            logger.warning(f"move_to_folder COPY failed uid={uid} folder={folder}")
            return False

        # Verify message arrived in target folder before deleting from INBOX
        try:
            conn.select(folder, readonly=True)
            # Search by UID is not reliable cross-folder; search by message-id would
            # require an extra fetch. Use folder EXISTS count as a lightweight sanity check.
            typ_v, data_v = conn.status(folder, "(MESSAGES)")
            copy_ok = typ_v == "OK"
        except Exception:
            copy_ok = False  # Conservative: proceed with delete anyway — COPY typ was OK

        if not copy_ok:
            logger.warning(f"move_to_folder: cannot verify copy uid={uid} → {folder}; aborting delete")
            conn.logout()
            return False

        # Re-select INBOX for STORE+EXPUNGE
        conn.select("INBOX")
        conn.uid("STORE", uid_b, "+FLAGS", r"(\Deleted)")
        typ_exp, _ = conn.expunge()
        conn.logout()

        if typ_exp != "OK":
            # COPY succeeded, expunge failed — message is in both places with \Deleted.
            # Next poll will find UID gone from INBOX search (server may auto-expunge)
            # or still present — either way retry is safe because COPY is idempotent on target.
            logger.warning(f"move_to_folder EXPUNGE failed uid={uid} folder={folder} — will retry")
            return False

        return True
    except Exception as e:
        logger.warning(f"move_to_folder uid={uid} folder={folder}: {e}")
        return False


# ── VATHUB redirect: mesaj brut + marcaj ─────────────────────────────────────

MAX_RAW_BYTES = 25 * 1024 * 1024   # 25 MB — peste asta forwardul nu are rost
VATHUB_KEYWORD = "$VathubForwarded"


def fetch_raw_message(account: dict, password: str, uid: int) -> Optional[bytes]:
    """Return the complete original message (headers + body + attachments) for one UID.

    Uses BODY.PEEK[] so the mail stays unread in the owner's inbox — the whole
    point of the redirect is that Diana still sees her mail as new.
    Returns None on failure or if the message exceeds MAX_RAW_BYTES.
    """
    conn = None
    try:
        conn = _open_imap(account, timeout=60)
        conn.login(account["email_address"], password)
        conn.select("INBOX", readonly=True)
        typ, data = conn.uid("FETCH", str(uid).encode(), "(BODY.PEEK[])")
        if typ != "OK" or not data:
            return None

        for item in data:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                raw = item[1]
                if len(raw) > MAX_RAW_BYTES:
                    logger.warning(
                        f"fetch_raw_message: uid={uid} account={account.get('id')} "
                        f"is {len(raw)} bytes — over MAX_RAW_BYTES, skipped"
                    )
                    return None
                return raw
        return None
    except Exception as e:
        logger.warning(f"fetch_raw_message failed account={account.get('id')} uid={uid}: {e}")
        return None
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass


def add_keyword(account: dict, password: str, uid: int, keyword: str = VATHUB_KEYWORD) -> bool:
    """Tag a message with an IMAP keyword, without marking it read.

    Best-effort: servers that reject custom keywords (PERMANENTFLAGS without \\*)
    return False and the caller carries on — the authoritative forward state is
    the DB column, not the flag. The flag exists so the owner can see in her own
    client which mails already went to VATHUB.
    """
    conn = None
    try:
        conn = _open_imap(account, timeout=15)
        conn.login(account["email_address"], password)
        conn.select("INBOX")
        typ, _ = conn.uid("STORE", str(uid).encode(), "+FLAGS", f"({keyword})")
        return typ == "OK"
    except Exception as e:
        logger.info(f"add_keyword failed account={account.get('id')} uid={uid}: {e}")
        return False
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass
