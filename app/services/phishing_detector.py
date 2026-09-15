"""v0.6.0 — Multi-layer phishing detection.

Layer 1 — Header analysis (SPF/DKIM/DMARC, From spoofing, look-alike domains)
Layer 2 — Content analysis (URLs, attachments, body patterns)
Layer 3 — LLM analysis (Gemma local @ NOVA, only when 20 <= score <= 80)
Layer 4 — Quarantine STRICT (password change, suspicious links — independent of score)

v0.6.1 (FIX 0): content & STRICT triggers run on NEW content only
  (quote/forward/signature history stripped). Body stored unchanged.

v0.6.0 changes:
- STRICT password_change_request narrowed: requires explicit "parol|password" token
  (previous regex over-matched "actualizarea contului/soldului" on legitimate invoices).
- Trusted-sender bypass for own domains (nordlogistics.eu, deltacargo.eu).
- All findings now include `match_text` = exact substring that triggered the rule.
- RULES_CATALOG exported for /api/v1/settings/rules.
"""
import os
import re
import logging
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("mailguard.phishing")

# Suspect attachment extensions
EXE_EXTENSIONS = {'.exe', '.scr', '.bat', '.cmd', '.com', '.pif', '.vbs', '.js', '.jar', '.iso', '.lnk'}
ARCHIVE_EXT = {'.zip', '.rar', '.7z'}
MACRO_EXT = {'.docm', '.xlsm', '.xltm', '.dotm', '.pptm', '.potm'}
HTML_EXT = {'.html', '.htm', '.shtml', '.xhtml'}

# Levenshtein-1 from common brands (typosquatting detection)
LOOK_ALIKE_DOMAINS = {
    'microsoft.com': ['microsft.com', 'microsoftt.com', 'micros0ft.com', 'mircosoft.com'],
    'office.com': ['0ffice.com', 'offce.com', 'offlce.com'],
    'nordlogistics.eu': ['cargotrac.ro', 'cargotrak.ro', 'cargoctrack.ro', 'cargo-track.ro', 'cargotracck.ro'],
    'google.com': ['g00gle.com', 'gogle.com'],
    'paypal.com': ['paypa1.com', 'paypall.com'],
}

# URL shorteners
URL_SHORTENERS = {'bit.ly', 'tinyurl.com', 't.co', 'goo.gl', 'short.link', 'rebrand.ly', 'ow.ly', 'is.gd', 'buff.ly'}

# Trusted internal sender domains — strict triggers are skipped for these.
TRUSTED_SENDER_DOMAINS = {'nordlogistics.eu', 'deltacargo.eu'}

# Known payment / billing platforms used in merchant invoice fraud.
# Fraudsters create real merchant accounts on these platforms, then send fake invoices —
# the email passes DMARC/DKIM (it IS from the platform) but the invoice is fraudulent.
PAYMENT_PLATFORM_DOMAINS = {
    "stripe.com", "paypal.com", "squareup.com", "intuit.com",
    "quickbooks.com", "invoicecloud.com", "xero.com", "freshbooks.com",
    "wave.com", "bill.com",
}

def _in_payment_domain(domain: str) -> bool:
    d = domain.lower()
    return any(d == pd or d.endswith("." + pd) for pd in PAYMENT_PLATFORM_DOMAINS)

# Subject keyword indicating an inbound invoice/billing message.
_PAYMENT_INVOICE_SUBJ = re.compile(
    r"\b(new invoice|invoice from|new bill|new payment|factur[a\u0103]|plat[a\u0103]|billing notice)\b",
    re.IGNORECASE,
)
# Account/customer ID embedded in a payment-platform sender local-part (e.g. acct_1RjVexG8ldHkikUZ).
_PAYMENT_ACCT_ID = re.compile(
    r"(?:acct|cust|merchant|sub)_[A-Za-z0-9]{8,}", re.IGNORECASE
)

# Strict quarantine patterns (Layer 4)
# password_change_request: now requires explicit "parol/password" token (not generic "cont/account").
STRICT_PATTERNS = [
    (re.compile(r'(schimb|reset|expir|actualiz|verific|confirm)\W{0,5}(\w+\W){0,5}?(parola|parole|parolei|password)', re.IGNORECASE), 'password_change_request'),
    (re.compile(r'(click|accesa|deschid|access)\W{0,5}(\w+\W){0,5}?(link|aici|here|button|buton)', re.IGNORECASE), 'click_request'),
    (re.compile(r'(suspended|suspendat|blocat|locked|inactive|inactiv)\W{0,5}(\w+\W){0,5}?(cont|account)', re.IGNORECASE), 'account_suspended'),
    (re.compile(r'https?://[^/\s]*(nordlogistics|office|microsoft|google|paypal|bank|banca)\.[^/\s]+/(login|sign-?in|account|verify)', re.IGNORECASE), 'sensitive_login_link'),
]

# Body urgency patterns
URGENCY_PATTERNS = [
    re.compile(r'\b(urgent|imediat|in 24 ore|expira (azi|today)|right now|asap|emergency)\b', re.IGNORECASE),
    re.compile(r'\b(action required|acțiune necesar|action needed|nevoie de acțiune)\b', re.IGNORECASE),
]


# Rule catalog: id → metadata for UI /settings/rules
RULES_CATALOG = [
    {"code": "display_name_impersonation", "layer": 1, "label": "Impersonare nume expeditor", "weight": 30,
     "description": "Numele afișat al expeditorului conține un brand cunoscut (ex: Microsoft, Office365, PayPal) dar domeniul real nu este al acelui brand.",
     "action": "score +30 (Layer 1)"},
    {"code": "typosquat_domain", "layer": 1, "label": "Domeniu look-alike (typosquat)", "weight": 35,
     "description": "Domeniul expeditorului este o variație vizuală a unui domeniu cunoscut (ex: 'micros0ft.com', 'cargotrac.ro').",
     "action": "score +35 (Layer 1)"},
    {"code": "first_time_sender", "layer": 1, "label": "Expeditor nou", "weight": 5,
     "description": "Prima dată când acest expeditor scrie organizației.",
     "action": "score +5 (Layer 1)"},
    {"code": "url_shortener", "layer": 2, "label": "URL scurtat", "weight": 15,
     "description": "Linkul folosește un serviciu de scurtare (bit.ly, tinyurl, t.co etc.) care ascunde destinația reală.",
     "action": "score +15 (Layer 2)"},
    {"code": "ip_url", "layer": 2, "label": "URL bazat pe IP", "weight": 25,
     "description": "Linkul folosește o adresă IP în loc de domeniu (semnal puternic de phishing).",
     "action": "score +25 (Layer 2)"},
    {"code": "subdomain_abuse", "layer": 2, "label": "Brand abuzat în subdomeniu", "weight": 30,
     "description": "Un brand cunoscut apare ca subdomeniu al altui domeniu (ex: microsoft.security.evil.com).",
     "action": "score +30 (Layer 2)"},
    {"code": "executable_attachment", "layer": 2, "label": "Atașament executabil", "weight": 40,
     "description": "Atașament cu extensie de risc (.exe, .scr, .bat, .vbs, .js etc.).",
     "action": "score +40 (Layer 2)"},
    {"code": "macro_attachment", "layer": 2, "label": "Atașament Office cu macro", "weight": 20,
     "description": "Document Office care permite macro-uri (.docm, .xlsm etc.).",
     "action": "score +20 (Layer 2)"},
    {"code": "double_extension", "layer": 2, "label": "Dublă extensie", "weight": 50,
     "description": "Fișier cu dublă extensie disimulată (ex: 'factura.pdf.exe').",
     "action": "score +50 (Layer 2)"},
    {"code": "urgency_pattern", "layer": 2, "label": "Limbaj de urgență", "weight": 15,
     "description": "Conținutul folosește cuvinte de presiune temporală: urgent, imediat, în 24 ore, ASAP, action required etc.",
     "action": "score +15 (Layer 2)"},
    {"code": "password_request_with_link", "layer": 2, "label": "Cerere parolă + link", "weight": 25,
     "description": "Conținutul conține cuvinte ca password/parola/reset/verifica combinate cu URL — pattern clasic de phishing.",
     "action": "score +25 (Layer 2)"},
    {"code": "password_change_request", "layer": 4, "label": "Cerere schimbare parolă",
     "description": "Verbe de acțiune (schimbă/resetează/expiră/actualizează/verifică/confirmă) urmate explicit de cuvântul 'parola' sau 'password'. NU se declanșează pe expresii generice cu 'cont' (ex: 'actualizare sold cont').",
     "action": "STATUS = quarantined_strict (Layer 4)"},
    {"code": "click_request", "layer": 4, "label": "Cerere click pe link",
     "description": "Conținutul îți cere explicit să dai click / accesezi un link / buton / 'aici'.",
     "action": "STATUS = quarantined_strict (Layer 4)"},
    {"code": "account_suspended", "layer": 4, "label": "Cont suspendat / blocat",
     "description": "Mesajul afirmă că un cont este suspendat, blocat, inactiv — tactică tipică de panică.",
     "action": "STATUS = quarantined_strict (Layer 4)"},
    {"code": "sensitive_login_link", "layer": 4, "label": "Link de login pe brand sensibil",
     "description": "Conține un URL către path-uri de login/sign-in/verify pe domenii care imită branduri sensibile (Microsoft, PayPal, bancă etc.).",
     "action": "STATUS = quarantined_strict (Layer 4)"},
    {"code": "suspicious_payment_subaddress", "layer": 1, "label": "Adresă subaddressed suspectă (platformă plăți)", "weight": 30,
     "description": "Emailul vine de pe un domeniu de platformă de plăți (Stripe, PayPal etc.) dar local-part-ul are o structură complexă: 2+ semne '+' sau un ID de cont (acct_XXXX). Pattern tipic pentru merchant invoice fraud.",
     "action": "score +30 (Layer 1, semnal coroborator pentru payment_invoice_abuse)"},
    {"code": "payment_invoice_abuse", "layer": 4, "label": "Fraudă factură via platformă de plăți (Stripe/PayPal)",
     "description": "Combinație: domeniu platformă plăți + local-part tip merchant (2+ '+' sau acct_XXXX) + subiect tip factură. Atacatorul creează un cont real pe Stripe/PayPal și trimite facturi false — emailul trece DMARC dar factura e frauduloasă.",
     "action": "STATUS = quarantined_strict (Layer 4, cu coroborare Layer 1)"},
    {"code": "manual_blacklist", "layer": 4, "label": "Expeditor blacklist (carantinare manuală)", "weight": 50,
     "description": "Expeditor pe care un operator l-a carantinat manual anterior (learning agresiv). Semnal decisiv: forțează singur carantină strictă. Pentru clienți cunoscuți blacklist-ul e scoped pe amprenta mesajului, nu pe adresă (evită blocarea unui client compromis).",
     "action": "STATUS = quarantined_strict (Layer 4), nesuprimabil de feedback"},
]

GLOBAL_POLICY = {
    "score_quarantine_threshold": 60,
    "score_review_threshold": 85,
    "trusted_sender_domains": sorted(TRUSTED_SENDER_DOMAINS),
    "strict_bypass_for_trusted": True,
}


def _extract_urls(text: str) -> List[str]:
    """Extract all URLs from text."""
    if not text:
        return []
    pattern = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)
    return pattern.findall(text)


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().strip('.')
    except Exception:
        return ''


def _sender_domain(email: Dict[str, Any]) -> str:
    addr = (email.get('from_address') or '').lower()
    return addr.split('@', 1)[-1] if '@' in addr else ''


# --- FIX 0 (quote stripping): triggers evaluate only NEW content, not quoted/forwarded history ---

# Text-derived rule codes — eligible for the "evaluat pe conținut nou" explainability note.
# Attachment codes (executable/macro/double_extension) are excluded: they come from attachment
# metadata, not body text, so quote stripping does not affect them.
TEXT_DERIVED_CODES = {
    'url_shortener', 'ip_url', 'subdomain_abuse', 'urgency_pattern',
    'password_request_with_link', 'password_change_request', 'click_request',
    'account_suspended', 'sensitive_login_link',
}

# Plaintext quote/forward boundary markers (RO + EN). From the first whole-line match
# downward the text is treated as quoted/forwarded/signature history.
_QUOTE_INTRO = re.compile(
    r'(?i)^\s*(?:'
    r'-{2,}\s*original message\s*-{2,}'
    r'|-{2,}\s*mesaj(?:ul)? original\s*-{2,}'
    r'|on .{0,200}?wrote:'
    r'|(?:[îi]n|la)\b.{0,200}?a scris:'
    r'|.{0,120}?\ba scris:'
    # Quote intros in alte locale (clienti Gmail/Outlook ne-RO) — altfel istoricul citat
    # (inclusiv oferta NOASTRA) scurge in textul nou si declanseaza fals semnale de spam.
    r'|el .{0,200}?escribi[o\u00f3]:'        # ES: "El <fecha>, <rem> escribio:"
    r'|em .{0,200}?escreveu:'                 # PT: "Em <data>, <rem> escreveu:"
    r'|le .{0,200}?a [e\u00e9]crit\s*:'      # FR: "Le <date>, <exp> a ecrit :"
    r'|il giorno .{0,200}?ha scritto:'        # IT: "Il giorno <data> <mitt> ha scritto:"
    r'|am .{0,200}?schrieb.{0,200}?:'         # DE: "Am <Datum> schrieb <Abs>:"
    r')\s*$'
)
# Atributii de citat INLINE: clientii Yahoo/mobil lipesc textul citat pe ACEEASI linie cu marcajul
# (ex. "Pe <data>, <expeditor> a scris:Buna ziua, ..."), deci marcajul nu e la finalul liniei si
# _QUOTE_INTRO (ancorat pe linie intreaga) il rateaza -> tot threadul citat se scurge in continutul
# nou (categorii gresite + false phishing din notificari citate). Garda anti-fals-pozitiv: linia
# TREBUIE sa inceapa cu un cuvant de atributie SI sa contina o data (hh:mm / zz.ll) sau un email
# inainte de marcaj -> proza care contine 'a scris:' (ex. 'Clientul mi-a scris: ...') NU se taie.
_INLINE_QUOTE = re.compile(
    r'(?i)^\s*'
    r'(?:on|[i\u00ee]n|la|pe|el|em|le|il giorno|am)\b'
    r'.{0,200}?'
    r'(?:@|\d{1,2}[:/.]\d)'
    r'.{0,200}?'
    r'(?:wrote:|a scris:|escribi[o\u00f3]:|escreveu:|a [e\u00e9]crit\s*:|ha scritto:|schrieb.{0,80}?:)'
)
_SEP_LINE = re.compile(r'^\s*[-_]{2,}\s*$')          # signature / "--" / "___" separators
_GT_LINE = re.compile(r'^\s*>')                        # quoted lines
_FWD_FROM = re.compile(r'(?i)^\s*>?\s*(?:de la|from)\s*:')
_FWD_FOLLOW = re.compile(r'(?i)^\s*>?\s*(?:trimis|sent|c[aă]tre|to|subiect|subject)\s*:')


def _strip_quoted_text(text: str) -> str:
    """Return only the NEW plaintext, dropping quoted/forwarded/signature history."""
    if not text:
        return ''
    lines = text.splitlines()
    cut = len(lines)
    for i, ln in enumerate(lines):
        if _QUOTE_INTRO.match(ln) or _INLINE_QUOTE.match(ln) or _SEP_LINE.match(ln):
            cut = i
            break
        if _FWD_FROM.match(ln) and any(_FWD_FOLLOW.match(w) for w in lines[i + 1:i + 5]):
            cut = i
            break
    kept = [ln for ln in lines[:cut] if not _GT_LINE.match(ln)]
    return '\n'.join(kept).strip()


# HTML quoted-content containers. Cut from the boundary container to end-of-document
# (quoted history is appended last); blockquotes removed in place. Other tags are kept
# so that href= URLs in the NEW portion remain extractable.
_HTML_OUTLOOK_BOUNDARY = re.compile(r'(?is)<div[^>]*id=["\'][^"\']*divRplyFwdMsg[^"\']*["\'].*$')
_HTML_GMAIL_BOUNDARY = re.compile(r'(?is)<div[^>]*class=["\'][^"\']*gmail_quote[^"\']*["\'].*$')
# Yahoo Mail wraps the quoted thread in <div class="yahoo_quoted" ...> (id="yahoo_quoted_...").
_HTML_YAHOO_BOUNDARY = re.compile(r'(?is)<div[^>]*(?:class|id)=["\'][^"\']*yahoo_quoted[^"\']*["\'].*$')
# Thunderbird prefixes the quote with <div class="moz-cite-prefix">…wrote:</div><blockquote>.
_HTML_MOZ_BOUNDARY = re.compile(r'(?is)<div[^>]*class=["\'][^"\']*moz-cite-prefix[^"\']*["\'].*$')
# Apple Mail / generic: an explicit "originalmessage" or "*_quote*" wrapper div.
_HTML_GENERIC_QUOTE_BOUNDARY = re.compile(r'(?is)<div[^>]*(?:class|id)=["\'][^"\']*(?:_?quote|originalmessage|reply-?message)[^"\']*["\'].*$')
_HTML_BLOCKQUOTE = re.compile(r'(?is)<blockquote\b.*?</blockquote>')
_HTML_TAG = re.compile(r'(?s)<[^>]+>')


def _strip_quoted_html(html: str) -> str:
    """Return NEW HTML with quoted containers removed; non-quoted tags preserved."""
    if not html:
        return ''
    s = _HTML_OUTLOOK_BOUNDARY.sub('', html)
    s = _HTML_GMAIL_BOUNDARY.sub('', s)
    s = _HTML_YAHOO_BOUNDARY.sub('', s)
    s = _HTML_MOZ_BOUNDARY.sub('', s)
    s = _HTML_GENERIC_QUOTE_BOUNDARY.sub('', s)
    s = _HTML_BLOCKQUOTE.sub(' ', s)
    return s


def _new_content(email: Dict[str, Any]):
    """(new_text, new_html, quoted_removed): body stripped of quoted history, for TRIGGER
    evaluation only — the stored body is never altered. Falls back to the full original
    body when stripping would leave essentially nothing (avoid blinding on a bare forward)."""
    raw_text = email.get('body_text') or ''
    raw_html = email.get('body_html') or ''

    new_text = _strip_quoted_text(raw_text)
    new_html = _strip_quoted_html(raw_html)

    had_body = bool(raw_text.strip() or raw_html.strip())
    visible = _HTML_TAG.sub(' ', new_html)
    meaningful = re.sub(r'\s+', '', new_text + ' ' + visible)
    if had_body and len(meaningful) < 3:
        logger.info("phishing FIX0: quote-strip left <15 chars; using full body (from=%s)",
                    email.get('from_address'))
        return raw_text, raw_html, False

    quoted_removed = (new_text != raw_text.strip()) or (new_html != raw_html)
    return new_text, new_html, quoted_removed


def _scan_content(email: Dict[str, Any]):
    """Conținutul pe care se evaluează spam/phishing — (text, html, quoted_removed).

    ANALYZE_FULL_THREAD (default OFF) → doar mesajul NOU / ultimul reply (_new_content,
    quote-stripped). Acesta e comportamentul implicit: analizăm DOAR ce a scris efectiv
    expeditorul în acest mesaj, NU istoricul citat (ex. un reply al clientului la propriul
    nostru email de „Servicii suspendate" nu mai moștenește triggerele din textul citat).
    ON (=1/true/yes/on) → tot thread-ul (body integral, INCLUSIV istoricul citat) — folosește
    doar pentru depanare/comparație. Comutare fără redeploy: ANALYZE_FULL_THREAD=1 + restart.
    Folosit de detect_phishing (aici) și de spam_detector.detect_spam (un singur loc comun)."""
    flag = (os.getenv('ANALYZE_FULL_THREAD', '0') or '').strip().lower()
    if flag in ('1', 'true', 'yes', 'on'):
        return (email.get('body_text') or ''), (email.get('body_html') or ''), False
    return _new_content(email)


def layer1_headers(email: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Layer 1 — Header analysis (no AI). Returns list of finding dicts."""
    findings = []
    from_addr = (email.get('from_address') or '').lower()
    from_name = email.get('from_name') or ''

    brand_keywords = ['microsoft', 'office 365', 'office365', 'paypal', 'google', 'apple',
                      'admin', 'security', 'support team', 'it team', 'helpdesk']
    if from_name:
        fn_lower = from_name.lower()
        from_domain = from_addr.split('@', 1)[-1] if '@' in from_addr else ''
        for brand in brand_keywords:
            if brand in fn_lower and brand.replace(' ', '') not in from_domain:
                findings.append({'layer': 1, 'code': 'display_name_impersonation', 'weight': 30,
                                 'match_text': from_name,
                                 'details': f"From name '{from_name}' references '{brand}' but domain is '{from_domain}'"})
                break

    from_domain = from_addr.split('@', 1)[-1] if '@' in from_addr else ''
    for canonical, look_alikes in LOOK_ALIKE_DOMAINS.items():
        if from_domain in look_alikes:
            findings.append({'layer': 1, 'code': 'typosquat_domain', 'weight': 35,
                             'match_text': from_domain,
                             'details': f"Domain '{from_domain}' look-alike for '{canonical}'"})
            break

    # Corroborating signal for payment_invoice_abuse (Layer 4 strict).
    # Fires when a payment platform sender has a complex subaddressed local part —
    # 2+ plus signs OR a Stripe/PayPal-style account ID (acct_XXXX) — characteristic
    # of merchant-invoice fraud where the attacker owns the payment platform account.
    if _in_payment_domain(from_domain):
        _local = from_addr.split('@')[0] if '@' in from_addr else ''
        _plus = _local.count('+')
        _has_id = bool(_PAYMENT_ACCT_ID.search(_local))
        if _plus >= 2 or _has_id:
            findings.append({
                'layer': 1,
                'code': 'suspicious_payment_subaddress',
                'weight': 30,
                'match_text': from_addr,
                'details': (
                    f"Payment platform '{from_domain}' — local part uses complex subaddressing "
                    f"(plus_count={_plus}, acct_id={_has_id}) typical of merchant invoice fraud"
                ),
            })

    return findings


def layer2_content(email: Dict[str, Any], attachments: Optional[List[Dict]] = None) -> List[Dict[str, Any]]:
    """Layer 2 — Content analysis (URLs, attachments, body patterns)."""
    findings = []
    body = (email.get('body_text') or '') + ' ' + (email.get('body_html') or '')
    urls = _extract_urls(body)

    for url in urls:
        domain = _domain_of(url)
        if domain in URL_SHORTENERS:
            findings.append({'layer': 2, 'code': 'url_shortener', 'weight': 15,
                             'match_text': url[:120],
                             'details': f"URL shortener detected: {domain}"})
        if re.match(r'^https?://\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', url):
            findings.append({'layer': 2, 'code': 'ip_url', 'weight': 25,
                             'match_text': url[:120],
                             'details': f"URL uses IP instead of domain: {url[:50]}"})
        parts = domain.split('.')
        if len(parts) >= 4:
            sus_brands = ['microsoft', 'office', 'google', 'paypal', 'apple', 'nordlogistics']
            if any(b in parts[:-2] for b in sus_brands) and not any(domain.endswith(b + '.com') or domain.endswith(b + '.ro') for b in sus_brands):
                findings.append({'layer': 2, 'code': 'subdomain_abuse', 'weight': 30,
                                 'match_text': domain,
                                 'details': f"Brand in subdomain: {domain}"})

    if attachments:
        for att in attachments:
            name = (att.get('name') or '').lower()
            for ext in EXE_EXTENSIONS:
                if name.endswith(ext):
                    findings.append({'layer': 2, 'code': 'executable_attachment', 'weight': 40,
                                     'match_text': name,
                                     'details': f"Suspect executable: {name}"})
                    break
            for ext in MACRO_EXT:
                if name.endswith(ext):
                    findings.append({'layer': 2, 'code': 'macro_attachment', 'weight': 20,
                                     'match_text': name,
                                     'details': f"Office macro-enabled: {name}"})
                    break
            if re.match(r'.+\.(pdf|doc|xls|jpg|png)\.(exe|scr|bat|cmd)$', name):
                findings.append({'layer': 2, 'code': 'double_extension', 'weight': 50,
                                 'match_text': name,
                                 'details': f"Double extension: {name}"})

    for pat in URGENCY_PATTERNS:
        m = pat.search(body)
        if m:
            findings.append({'layer': 2, 'code': 'urgency_pattern', 'weight': 15,
                             'match_text': m.group(0),
                             'details': "Urgency language detected"})
            break

    body_lower = body.lower()
    m = re.search(r'\b(password|parola|reset|verify|verifica|cont suspendat|account suspended)\b', body_lower)
    if m and urls:
        findings.append({'layer': 2, 'code': 'password_request_with_link', 'weight': 25,
                         'match_text': m.group(0),
                         'details': "Password/account language combined with URL"})

    return findings


def layer4_strict(email: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Layer 4 — STRICT quarantine triggers (independent of score).
    Skipped entirely for trusted internal sender domains.
    """
    findings = []
    sender_dom = _sender_domain(email)
    if sender_dom in TRUSTED_SENDER_DOMAINS:
        return findings  # trusted internal sender — bypass strict

    body = (email.get('body_text') or '') + ' ' + (email.get('body_html') or '')
    subject = email.get('subject') or ''
    # La un REPLY/FWD subiectul e MOȘTENIT din firul original (deseori chiar emailul NOSTRU,
    # ex. „Răsp.: Servicii suspendate pentru neplată | CargoTrack"). Nu lăsăm subiectul moștenit
    # să declanșeze singur triggere stricte (account_suspended etc.) pe răspunsul benign al
    # clientului — la reply evaluăm strict DOAR pe conținutul nou, nu pe subiect.
    _is_reply = bool(re.match(r'\s*(re|fwd|fw|raspuns|r[ăa]spuns)\s*:', subject, re.IGNORECASE))
    combined = body if _is_reply else (subject + ' ' + body)

    for pattern, code in STRICT_PATTERNS:
        m = pattern.search(combined)
        if m:
            findings.append({'layer': 4, 'code': code, 'weight': None,
                             'match_text': m.group(0)[:200],
                             'details': f"Strict trigger: {m.group(0)[:100]}"})

    # Layer 4 strict — payment platform merchant invoice fraud.
    # Fires when: payment platform domain + complex merchant local-part + invoice subject keyword.
    # Skipped for replies (re:/fwd:) to avoid false hits on forwarded payment confirmations.
    _from_full = (email.get('from_address') or '').lower()
    _from_dom4 = _from_full.split('@', 1)[-1] if '@' in _from_full else ''
    _local4 = _from_full.split('@')[0] if '@' in _from_full else ''
    if not _is_reply and _in_payment_domain(_from_dom4):
        _plus4 = _local4.count('+')
        _has_id4 = bool(_PAYMENT_ACCT_ID.search(_local4))
        if (_plus4 >= 2 or _has_id4) and _PAYMENT_INVOICE_SUBJ.search(subject):
            findings.append({
                'layer': 4,
                'code': 'payment_invoice_abuse',
                'weight': None,
                'match_text': _from_full,
                'details': (
                    f"Payment platform '{_from_dom4}' — merchant invoice fraud: "
                    f"local='{_local4}' (plus={_plus4}, acct_id={_has_id4}), "
                    f"subject contains invoice/billing keyword"
                ),
            })

    return findings


# Malware-class codes: NEVER suppressible by human feedback (Treapta 1 guardrail).
NEVER_SUPPRESS = {'executable_attachment', 'macro_attachment', 'double_extension'}


def detect_phishing(email: Dict[str, Any], attachments: Optional[List[Dict]] = None,
                    suppress_codes: Optional[set] = None,
                    blacklist: Optional[set] = None,
                    whitelist: Optional[set] = None) -> Tuple[float, str, List[Dict[str, Any]]]:
    """Top-level detection. Returns (score, status, reasons).
    status: 'clean' | 'quarantined' | 'quarantined_strict'

    suppress_codes: rule codes neutralized for this sender/domain via human
    feedback (auto-learning). Malware-class codes (NEVER_SUPPRESS) are never
    dropped even if listed. Score AND strict status are recomputed from the
    POST-suppression findings, so suppressing an L4 trigger truly releases it.

    whitelist: senderi/domenii marcați de operator ca de încredere (acțiunea
    „Legit"). Suprimare SOFT: elimină semnalele slabe Layer-1/2 (non-malware) și
    DOAR dacă emailul NU are niciun trigger strict Layer-4 — ca să nu slăbească
    niciodată detecția decisivă. Blacklist bate whitelist (un sender pe blacklist
    are deja L4, deci whitelist nu intervine).
    """
    # Sursa de conținut (ANALYZE_FULL_THREAD): default tot thread-ul; OFF → doar conținutul nou
    # (FIX 0 — STRICT/content triggers fără istoricul citat). Vezi _scan_content.
    # L1 (impersonare) + L2 (URL-uri, atașamente, urgency) rămân pe scan content (tot thread-ul când flag ON).
    new_text, new_html, _quoted_removed = _scan_content(email)
    email_new = dict(email)
    email_new['body_text'] = new_text
    email_new['body_html'] = new_html

    # STRICT (Layer 4): triggerele decisive de TEXT (cont suspendat, resetare parolă, click) se evaluează
    # DOAR pe conținutul NOU scris de expeditor (quote-stripped), indiferent de ANALYZE_FULL_THREAD — frazele
    # din istoricul CITAT nu mai forțează carantină strictă pe un simplu reply. Atașamentele/URL-urile
    # periculoase rămân scanate pe tot thread-ul (L2). Fallback la full body dacă quote-strip lasă <3 chars.
    _st_text, _st_html, _st_quoted = _new_content(email)
    email_strict = dict(email)
    email_strict['body_text'] = _st_text
    email_strict['body_html'] = _st_html

    findings = []
    findings.extend(layer1_headers(email))
    findings.extend(layer2_content(email_new, attachments))
    findings.extend(layer4_strict(email_strict))

    if _st_quoted:
        for _f in findings:
            if _f.get('layer') == 4:
                _f['details'] = (_f.get('details') or '') + ' (evaluat pe conținut nou, fără citat)'
    if _quoted_removed:
        for _f in findings:
            if _f.get('layer') in (1, 2) and _f.get('code') in TEXT_DERIVED_CODES:
                _f['details'] = (_f.get('details') or '') + ' (evaluat pe conținut nou, fără citat)'

    if suppress_codes:
        kept = []
        for f in findings:
            code = f.get('code')
            if code in suppress_codes and code not in NEVER_SUPPRESS:
                continue
            kept.append(f)
        findings = kept

    # Blacklist (learning din carantinarea manuala) — un expeditor pe care operatorul l-a
    # carantinat manual devine semnal Layer-4 DECISIV. Adaugat dupa filtrul de suppress, deci
    # nu poate fi suprimat de feedback. (Blacklist-ul hard se aplica DOAR expeditorilor
    # necunoscuti — vezi quarantine_email; clientii cunoscuti sunt scoped pe amprenta.)
    # Potrivire cu domenii-parinte (`sender_scopes`), ca in sender_block: o intrare pe
    # `akcenta.eu` trebuie sa prinda si `info@email.akcenta.eu`.
    if blacklist:
        from app.services.spam_detector import sender_scopes
        saddr, sdoms = sender_scopes(email.get('from_address'))
        bl_hit = saddr if (saddr and saddr in blacklist) else next(
            (d for d in sdoms if d in blacklist), None)
        if bl_hit:
            findings.append({'layer': 4, 'code': 'manual_blacklist', 'weight': 50,
                             'match_text': bl_hit,
                             'details': 'Expeditor pe blacklist (carantinat manual anterior de operator)'})

    # Whitelist (de încredere, marcat de operator) — suprimare SOFT a semnalelor slabe.
    # Aplicată DOAR dacă nu există niciun trigger strict Layer-4: nu poate slăbi detecția
    # decisivă/malware, doar reduce fals-pozitivele de scor. Semnalele L1/L2 din NEVER_SUPPRESS
    # (atașamente periculoase) NU se ating niciodată.
    if whitelist:
        wsaddr = (email.get('from_address') or '').lower().strip()
        wsdom = _sender_domain(email)
        if (wsaddr and wsaddr in whitelist) or (wsdom and wsdom in whitelist):
            if not any(f.get('layer') == 4 for f in findings):
                _kept, _dropped = [], 0
                for f in findings:
                    if f.get('layer') in (1, 2) and f.get('code') not in NEVER_SUPPRESS:
                        _dropped += 1
                        continue
                    _kept.append(f)
                if _dropped:
                    findings = _kept
                    findings.append({'layer': 0, 'code': 'sender_whitelist', 'weight': None,
                                     'match_text': wsaddr or wsdom,
                                     'details': ('Expeditor pe whitelist (de încredere) — %d semnal(e) slab(e) '
                                                 'suprimat(e) soft' % _dropped)})

    score = sum(f['weight'] for f in findings if f.get('weight') is not None)
    score = min(score, 100)

    # Poarta de COMBINATIE pentru STRICT (reducere false-pozitive): un singur trigger Layer-4
    # pe o fraza nu mai forteaza singur strict. Necesita: >=2 coduri stricte distincte, SAU
    # 1 strict + un finding coroborant Layer-1/2. manual_blacklist (semnal explicit al
    # operatorului) e decisiv pe cont propriu.
    strict_findings = [f for f in findings if f.get('layer') == 4]
    strict_codes = sorted({f.get('code') for f in strict_findings if f.get('code')})
    # Coroborare pentru escaladarea la STRICT: doar semnale L1/L2 cu adevărat suspecte.
    # `urgency_pattern` și `first_time_sender` sunt prea frecvente în mail legitim (notificări,
    # marketing benign) ca să justifice singure carantină strictă pe baza unui singur trigger L4
    # (ex. un „click here" / „accesează aici" banal). Le excludem din coroborare — un singur L4
    # + doar urgency NU mai forțează strict (rămâne pe scor; sub prag = clean).
    WEAK_CORROBORATION = {'urgency_pattern', 'first_time_sender'}
    corroborating = [f for f in findings if f.get('layer') in (1, 2)
                     and f.get('code') not in WEAK_CORROBORATION]
    decisive = 'manual_blacklist' in strict_codes
    is_strict = bool(strict_codes) and (
        decisive or len(strict_codes) >= 2 or (len(strict_codes) >= 1 and bool(corroborating))
    )

    if is_strict:
        status = 'quarantined_strict'
    elif score >= GLOBAL_POLICY['score_quarantine_threshold']:
        status = 'quarantined'
    else:
        status = 'clean'

    # Explicabilitate: daca un trigger strict s-a declansat dar NU a escaladat (semnal unic,
    # fara coroborare), noteaza pe finding de ce a fost redus.
    if strict_findings and not is_strict:
        for f in strict_findings:
            f['details'] = (f.get('details') or '') + ' (strict redus: semnal unic, fara coroborare)'

    return float(score), status, findings
