"""SMTP connector pentru redirectul VATHUB din căsuțele personale.

Redirectul pleacă de pe adresa proprietarului căsuței (contul lui SMTP), nu de
pe un cont de serviciu — mailul ajunge la VATHUB ca un forward normal făcut de
el, iar SPF/DKIM rămân valide pentru domeniul expeditorului.

Mesajul original se atașează INTACT ca `message/rfc822`: atașamentele (PDF-urile
deciziilor 318), semnăturile și headerele originale ajung neatinse la VATHUB.
Nu se rescrie `From` cu adresa autorității — asta ar produce un mail care pică
la SPF și ar putea fi respins sau marcat spam.
"""
import logging
import smtplib
import email.policy
from email import message_from_bytes
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Optional

logger = logging.getLogger("mailguard.personal_smtp")

SMTP_TIMEOUT = 30


def resolve_smtp(account: dict, imap_password: str,
                 smtp_password: Optional[str] = None) -> dict:
    """Config SMTP efectiv pentru un cont.

    `smtp_user` gol înseamnă „același user ca la IMAP", iar parola SMTP lipsă
    înseamnă „aceeași parolă ca la IMAP" — cazul obișnuit la Gmail/O365/cPanel,
    unde ambele protocoale folosesc aceleași credențiale.
    """
    return {
        "host": account.get("smtp_host"),
        "port": int(account.get("smtp_port") or 587),
        "tls": bool(account.get("smtp_tls", True)),
        "user": account.get("smtp_user") or account["email_address"],
        "password": smtp_password or imap_password,
        "from_address": account["email_address"],
    }


def _connect(cfg: dict) -> smtplib.SMTP:
    """Deschide conexiunea. Portul 465 = SMTPS implicit; 587/25 = STARTTLS."""
    if cfg["port"] == 465:
        server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=SMTP_TIMEOUT)
    else:
        server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=SMTP_TIMEOUT)
        if cfg["tls"]:
            server.starttls()
    server.login(cfg["user"], cfg["password"])
    return server


def test_login(cfg: dict) -> tuple[bool, Optional[str]]:
    """Verifică host/port/credențiale SMTP. Returnează (ok, mesaj_eroare)."""
    if not cfg.get("host"):
        return False, "Server SMTP necompletat"
    try:
        server = _connect(cfg)
        server.quit()
        return True, None
    except smtplib.SMTPAuthenticationError as e:
        return False, f"SMTP auth error: {str(e)[:200]}"
    except (OSError, smtplib.SMTPException) as e:
        return False, f"SMTP connection error: {str(e)[:200]}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)[:200]}"


def build_forward(raw_original: bytes, from_address: str, to_address: str,
                  matched_rule: str, source_mailbox: str) -> EmailMessage:
    """Împachetează mesajul original ca atașament `message/rfc822`.

    Subiectul rămâne NESCHIMBAT — VATHUB potrivește dosarele după numărul de
    referință din subiect (`RO2026…`), deci un prefix „Fwd:" l-ar strica.
    Expeditorul real ajunge în `Reply-To` și în headerele `X-Vathub-*`.
    """
    # policy=default: `add_attachment` are nevoie de un obiect EmailMessage ca să
    # producă o parte `message/rfc822` corectă (vezi nota de la add_attachment).
    original = message_from_bytes(raw_original, policy=email.policy.default)
    orig_from = original.get("From", "") or ""
    orig_date = original.get("Date", "") or ""
    orig_to = original.get("To", "") or ""
    orig_subject = original.get("Subject", "") or "(fără subiect)"
    orig_msgid = original.get("Message-ID", "") or ""

    msg = EmailMessage()
    msg["From"] = from_address
    msg["To"] = to_address
    msg["Subject"] = orig_subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_address.split("@")[-1])

    reply_to = parseaddr(orig_from)[1]
    if reply_to:
        msg["Reply-To"] = reply_to
    if orig_msgid:
        msg["References"] = orig_msgid
        msg["In-Reply-To"] = orig_msgid

    # Headere de urmărire — VATHUB le poate citi ca să știe de unde a venit
    # mailul și ce regulă din listă l-a adus.
    msg["X-Vathub-Forward"] = "1"
    msg["X-Vathub-Matched"] = matched_rule
    msg["X-Vathub-Source-Mailbox"] = source_mailbox
    if reply_to:
        msg["X-Vathub-Original-From"] = reply_to
    if orig_date:
        msg["X-Vathub-Original-Date"] = orig_date

    msg.set_content(
        "Mail redirecționat automat către VATHUB de Cargo360.\n\n"
        f"Expeditor original : {orig_from}\n"
        f"Către              : {orig_to}\n"
        f"Data               : {orig_date}\n"
        f"Subiect            : {orig_subject}\n"
        f"Regulă potrivită   : {matched_rule}\n"
        f"Căsuță sursă       : {source_mailbox}\n\n"
        "Mesajul original, cu atașamente și headere intacte, e atașat mai jos."
    )
    # Atașamentul se adaugă ca MESAJ deja parsat, nu ca bytes. Cu bytes, biblioteca
    # standard pune `Content-Transfer-Encoding: base64` pe partea `message/rfc822` —
    # interzis de RFC 2046 §5.2.1, care admite doar 7bit/8bit/binary. Efectul măsurat:
    # cititorul nu mai vede o parte de tip mesaj, ci un bloc de base64, deci nu poate
    # scoate din el expeditorul, data sau atașamentele originale. Cu obiectul `Message`,
    # partea iese pe `8bit` și se re-parsează corect la destinație.
    msg.add_attachment(original, filename="original.eml")
    return msg


def send_forward(cfg: dict, msg: EmailMessage, to_address: str) -> None:
    """Trimite mesajul construit. Ridică excepția SMTP originală la eșec."""
    server = _connect(cfg)
    try:
        server.send_message(msg, from_addr=cfg["from_address"], to_addrs=[to_address])
    finally:
        try:
            server.quit()
        except Exception:
            pass
    logger.info("Forward VATHUB trimis: %s → %s", cfg["from_address"], to_address)
