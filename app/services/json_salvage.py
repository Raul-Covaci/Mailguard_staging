"""Recuperarea unui obiect JSON dintr-un răspuns de model imperfect — utilitar comun.

Mutat din call_scorer.py (T3-L3, 2026-09-29) fără nicio schimbare de logică, ca să-l folosească și
clasificarea apelurilor (call_classifier.classify_call). call_scorer îl reimportă sub numele vechi.
Tratează: gard de cod markdown, text înainte/după obiect, ghilimele nescapate într-o valoare,
răspuns trunchiat, valori fără ghilimele. Întoarce dict sau None — niciodată altceva.
"""
import json
import re
from typing import Optional


def salvage_json(raw: str) -> Optional[dict]:
    """Recupereaza un obiect JSON dintr-un raspuns de model imperfect. None daca nu se poate.

    Doua defecte reale, ambele observate in productie (2026-09-23, promptul `agentulSaPrezentat`,
    ~3-4 WARNING/minut):
      1. GHILIMELE NESCAPATE intr-o valoare. Sase prompturi cer un CITAT exact ("evidence must
         quote the exact phrase"), iar modelul citeaza natural cu ": {"evidence": "a spus "Buna
         ziua" la inceput"} — a doua ghilimea inchide string-ul si parserul cere o virgula
         (`Expecting ',' delimiter`). NU diacriticele sau virgulele sunt problema: un string JSON
         valid le accepta.
      2. RASPUNS TRUNCHIAT la max_tokens — obiectul se termina brusc.
      3. VALOARE FARA GHILIMELE (2026-09-25): cerut sa citeze cu «...», modelul scoate uneori si
         ghilimelele JSON ale valorii — `"evidence": «Buna ziua» ...` (`Expecting value`).
    """
    if not raw or not raw.strip():
        return None
    s = raw.strip()
    if s.startswith("```"):                       # gard de cod markdown
        s = s.split("```")[1] if len(s.split("```")) > 1 else s
        s = s[4:] if s.lower().startswith("json") else s
    i, j = s.find("{"), s.rfind("}")
    if i < 0:
        return None
    candidate = s[i:j + 1] if j > i else s[i:]
    try:
        out = json.loads(candidate)
        return out if isinstance(out, dict) else None
    except Exception:
        pass

    # Defectul 1: rescrie valorile de string escapand ghilimelele interioare. Parcurgem caracter
    # cu caracter; o ghilimea inchide valoarea doar daca urmeaza (dupa spatii) `,` `}` sau `:`.
    out_chars, in_str, escaped, depth = [], False, False, 0
    for pos, ch in enumerate(candidate):
        if not in_str:
            if ch == '"':
                in_str = True
            elif ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
            out_chars.append(ch)
            continue
        if escaped:
            out_chars.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out_chars.append(ch)
            escaped = True
            continue
        if ch == '"':
            # O ghilimea inchide valoarea doar daca URMEAZA structura JSON: `:` (era o cheie),
            # `}`/`]` (sfarsit de obiect) sau `,` urmata de o CHEIE noua (`"nume":`). Un citat
            # terminat cu virgula — `a zis doar "Alo", fara nume` — nu inchide nimic, iar un
            # test naiv pe caracterul urmator l-ar rupe exact aici.
            rest = candidate[pos + 1:].lstrip()
            if rest[:1] in (":", "}", "]") or not rest:
                closes = True
            elif rest[:1] == ",":
                closes = re.match(r',\s*"[^"]{1,80}"\s*:', rest) is not None
            else:
                closes = False
            if closes:
                in_str = False
                out_chars.append(ch)
            else:
                out_chars.append('\\"')       # ghilimea din interiorul citatului
            continue
        out_chars.append(ch)
    repaired = "".join(out_chars)
    try:
        out = json.loads(repaired)
        if isinstance(out, dict):
            return out
    except Exception:
        pass

    # Defectul 2: obiect trunchiat — inchide string-ul/parantezele ramase deschise.
    if in_str:
        repaired += '"'
    repaired += "}" * max(0, depth)
    try:
        out = json.loads(repaired)
        if isinstance(out, dict):
            return out
    except Exception:
        pass

    # Defectul 3: valoare fara ghilimele — `"evidence": «Buna ziua» ...`. Ultima incercare, ca sa
    # nu atinga raspunsurile pe care reparatiile de mai sus le recupereaza deja.
    bare = _quote_bare_values(candidate)
    if bare == candidate:
        return None
    for attempt in (bare, bare + "}"):
        try:
            out = json.loads(attempt)
            if isinstance(out, dict):
                return out
        except Exception:
            pass
    return None


_JSON_LITERAL_RE = re.compile(r'(?:true|false|null|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(?:[,}\]]|$)')
_NEXT_KEY_RE = re.compile(r',\s*"[^"]{1,80}"\s*:')


def _quote_bare_values(s: str) -> str:
    """Pune intre ghilimele valorile care nu incep cu un token JSON valid. Valoarea se intinde pana
    la urmatoarea cheie (`, "nume":`) sau pana la ultima acolada."""
    out, i, in_str, escaped, n = [], 0, False, False, len(s)
    while i < n:
        ch = s[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
        if ch == '"':
            in_str = True
            continue
        if ch != ":":
            continue
        j = i
        while j < n and s[j] in " \t\r\n":
            j += 1
        if j >= n or s[j] in '"{[' or _JSON_LITERAL_RE.match(s, j):
            continue
        m = _NEXT_KEY_RE.search(s, j)
        last_brace = s.rfind("}")
        end = m.start() if m else (last_brace if last_brace > j else n)
        value = s[j:end].rstrip()
        out.append(s[i:j])
        out.append(json.dumps(value, ensure_ascii=False))
        out.append(s[j + len(value):end])
        i = end
    return "".join(out)
