# Cache de rezultat AI (T3-L1, v3.31.0)

Evită re-trimiterea la model a aceleiași intrări (același document / atașament, același prompt).
Cod: `app/services/ai_cache.py`, interceptare în `app/services/iris_ai.py::run_prompt()`.
Tabele: `ai_result_cache`, `ai_cache_hit_log` (`migrations/20260929_ai_result_cache.sql`).

## Activare

| Cheie `settings` | Implicit | Rol |
|---|---|---|
| `ai_cache.enabled` | `false` | comutatorul general; `false` = apelurile AI identice cu v3.30 (curățarea rândurilor expirate rulează oricum, vezi „TTL și curățare") |
| `ai_cache.prefixes` | 9 prefixe (vezi migrația) | funcțiile de task cache-uite, potrivire EXACTĂ (`doc_autogroup` ≠ `doc_autogroup_holistic`) |
| `ai_cache.epoch` | `1` | intră în cheie; crescut = tot cache-ul invalidat |
| `ai_cache.ttl_days` | `10` | valabilitatea unui rezultat |

Configul se recitește cel mult o dată la 30 s per proces — o schimbare prinde în ≤30 s, fără restart.

## Cheia

`sha256` pe payload-ul **efectiv** trimis la gateway: prefixul funcției din task (fără slug/hash) ·
`model_hint` (sau `default`) · `response_format` · `max_tokens` · `temperature` · sha256(system) ·
sha256(transcript după tăierea la 48.000) · lista ordonată (mime, sha256(octeți)) a atașamentelor ·
`ai_cache.epoch`. NU numele task-ului: în `documents.py` numele nu garantează intrare identică.

**`no_cache` / `skip_cache` / `use_cache` / `learn` nu au legătură cu acest cache.** Se referă la
cache-ul „curated" al gateway-ului IRIS (răspunsuri învățate, cheiate pe `task`) și merg mai departe
în payload exact ca înainte; `ai_result_cache` nu le citește și nu le pune în cheie. Azi niciun
apelant de pe prefixele cache-uite (`documents.py`, `op_extractor.py`) nu le trimite — le folosesc
clasificarea mailurilor, scorarea apelurilor, satisfacția etc., care nu sunt în `ai_cache.prefixes`.
Dacă un flux cache-uit ar începe să trimită `no_cache=True`, asta NU ocolește `ai_result_cache`;
ocolirea lui e doar `ai_cache_bypass` (vezi mai jos).

## Ce intră și ce nu

- Doar `ok: true` validat de apelant (`run_prompt(cache_ok=…)`) — validatorul face EXACT verificarea
  apelantului, altfel un răspuns respins ar deveni eșec permanent 10 zile:
  - vision cu parsare proprie (`doc_segment`, `doc_classify_vision`, `doc_extract_vision`,
    `doc_autogroup*`): `_salvage_json` reușit (`documents._cache_ok_salvage`);
  - `doc_classify` / `doc_extract`: `parsed` dict (`_cache_ok_parsed_dict`);
  - `doc_rename`: `parsed` dict cu `nume_complet` nevid (`_cache_ok_rename`);
  - `op_series`: răspuns recunoscut de `op_extractor._parse_series_answer` — seria validă sau `NONE`,
    moneda validă, `NONE` sau absentă (aceleași regex-uri ca apelantul). `NONE|NONE` intră; un refuz
    sau proză nu (apelantul îl tratează ca „serie negăsită" și reîncearcă, `MAX_EXTRACT_ATTEMPTS`);
  - `doc_vision_ocr`: validatorul implicit (text nevid — exact ce acceptă `_vision_transcribe`).
- Fiecare apelant are test prin funcția lui reală (`tests/test_ai_cache.py`, „Legarea validatorului").
- Niciodată: erori, `ok: false`, `temperature > 0` (`doc_prompt_gen`, `doc_detect_gen`).
- La hit: niciun apel la gateway, niciun rând în `ai_call_log`; se întoarce rezultatul stocat, cu
  modelul ORIGINAL (ajunge în `document_extractions.model`).
- `result` e coloană `json`, NU `jsonb`: `jsonb` sortează cheile, iar `_normalize_keys` (documents.py)
  păstrează ULTIMA valoare când modelul întoarce două variante ale aceleiași chei (`Vin` și
  `Vin (E.)`) — un hit ar fi ales altă valoare decât apelul original.

## Ocolire

Doar „Reidentifică" (`POST /documents/extractions/{id}/reidentify`) — ContextVar `ai_cache_bypass`:
**sare citirea**, iar scrierea (dacă rezultatul trece validatorul) face upsert NECONDIȚIONAT, deci
înlocuiește intrarea existentă, chiar nevalidă. Așa, rezultatul corectat de operator e cel pe care îl
primesc apoi `reprocess-by-ids`, `ungroup`, `unsplit` și drain-ul (toate folosesc cache-ul). Un
rezultat respins de validator nu atinge intrarea veche. Comparația shadow pornește pe fir propriu
(`threading.Thread` nu moștenește ContextVar-ul), deci ea folosește cache-ul normal.

## TTL și curățare

Rândurile expirate sunt miss (și se suprascriu). Tick-ul `/process/run-now` le șterge cel mult o dată
pe oră (poartă atomică `settings['ai_cache.last_purge_at']`) — **și cu flag-ul OFF**, fiindcă
rândurile rămase dintr-o perioadă activă conțin date extrase din documente. Fără tabel (migrație
neaplicată) nu face nimic, nici măcar poarta. Pentru ștergere imediată: `DELETE FROM ai_result_cache;`

`scripts/purge_documents_before.py` șterge și rândurile din `ai_result_cache` create înainte de
`--before`. Cheia e un sha256 pe payload, deci un rând nu se poate lega de documentul lui; un rezultat
creat DUPĂ prag pentru un document vechi rămâne până la expirare (≤ `ai_cache.ttl_days`).
`ai_cache_hit_log` nu conține date din documente (task, prefix, cheie, cost) și nu se curăță.

## Eșec

Orice eroare a cache-ului (config, citire, scriere, validator) = apel normal la gateway + WARNING
`mailguard.ai_cache`. Un hit a cărui evidență (`ai_cache_hit_log`) nu se poate scrie devine miss.

## Măsurare

`scripts/metrics/ai_cache_savings.sql` — pe zi și pe prefix: `hits`, `saved_cost_usd`, `real_calls`,
`real_cost_usd`, `hits_vs_real_calls`. `real_calls` = TOATE apelurile reale din `ai_call_log` pe acele
prefixe (inclusiv eșecuri, reîncercări, ocoliri, apelurile dinainte de activare) — NU numai miss-uri,
deci `hits_vs_real_calls` e ponderea apelurilor evitate, nu rata de hit. Rapoartele existente nu se
schimbă.

## Teste

`tests/test_ai_cache.py` rulează pe Postgres local efemer (`pgserver`, din `requirements-dev.txt`).
Fără `pgserver`, testele cu BD **pică** cu un mesaj explicit (nu se sar tăcut).

## Invalidare

Totală: `UPDATE settings SET value = to_jsonb((value)::int + 1) WHERE key = 'ai_cache.epoch';`
Punctuală: `DELETE FROM ai_result_cache WHERE task_prefix = '<prefix>';`
