# Cache de rezultat AI (T3-L1, v3.31.0)

Evită re-trimiterea la model a aceleiași intrări (același document / atașament, același prompt).
Cod: `app/services/ai_cache.py`, interceptare în `app/services/iris_ai.py::run_prompt()`.
Tabele: `ai_result_cache`, `ai_cache_hit_log` (`migrations/20260929_ai_result_cache.sql`).

## Activare

| Cheie `settings` | Implicit | Rol |
|---|---|---|
| `ai_cache.enabled` | `false` | comutatorul general; `false` = comportament identic cu v3.30 |
| `ai_cache.prefixes` | 9 prefixe (vezi migrația) | funcțiile de task cache-uite, potrivire EXACTĂ (`doc_autogroup` ≠ `doc_autogroup_holistic`) |
| `ai_cache.epoch` | `1` | intră în cheie; crescut = tot cache-ul invalidat |
| `ai_cache.ttl_days` | `10` | valabilitatea unui rezultat |

Configul se recitește cel mult o dată la 30 s per proces — o schimbare prinde în ≤30 s, fără restart.

## Cheia

`sha256` pe payload-ul **efectiv** trimis la gateway: prefixul funcției din task (fără slug/hash) ·
`model_hint` (sau `default`) · `response_format` · `max_tokens` · `temperature` · sha256(system) ·
sha256(transcript după tăierea la 48.000) · lista ordonată (mime, sha256(octeți)) a atașamentelor ·
`ai_cache.epoch`. NU numele task-ului: în `documents.py` numele nu garantează intrare identică.

## Ce intră și ce nu

- Doar `ok: true` validat de apelant (`run_prompt(cache_ok=…)`). Apelurile vision cu parsare proprie
  (`doc_segment`, `doc_classify_vision`, `doc_extract_vision`, `doc_autogroup*`) cer `_salvage_json`
  reușit; `doc_classify` / `doc_extract` / `doc_rename` cer `parsed` dict; restul: `parsed` nenul (json)
  sau text nevid. `op_series` → „NONE" e răspuns valid.
- Niciodată: erori, `ok: false`, `temperature > 0` (`doc_prompt_gen`, `doc_detect_gen`).
- La hit: niciun apel la gateway, niciun rând în `ai_call_log`; se întoarce rezultatul stocat, cu
  modelul ORIGINAL (ajunge în `document_extractions.model`).

## Ocolire

Doar „Reidentifică" (`POST /documents/extractions/{id}/reidentify`) — ContextVar `ai_cache_bypass`:
nici citire, nici scriere. `reprocess-by-ids`, `ungroup`, `unsplit` și drain-ul folosesc cache-ul.

## TTL și curățare

Rândurile expirate sunt miss (și se suprascriu). Tick-ul `/process/run-now` le șterge cel mult o dată
pe oră (poartă atomică `settings['ai_cache.last_purge_at']`), doar cu cache-ul activ. Dacă cache-ul
se oprește definitiv, rândurile rămase (conțin date extrase din documente) se șterg manual:
`DELETE FROM ai_result_cache;`

## Eșec

Orice eroare a cache-ului (config, citire, scriere, validator) = apel normal la gateway + WARNING
`mailguard.ai_cache`. Un hit a cărui evidență (`ai_cache_hit_log`) nu se poate scrie devine miss.

## Măsurare

`scripts/metrics/ai_cache_savings.sql` — pe zi și pe prefix: hit-uri, `saved_cost_usd`, apeluri reale
(miss-uri din `ai_call_log`) și `hit_ratio`. Rapoartele existente nu se schimbă.

## Invalidare

Totală: `UPDATE settings SET value = to_jsonb((value)::int + 1) WHERE key = 'ai_cache.epoch';`
Punctuală: `DELETE FROM ai_result_cache WHERE task_prefix = '<prefix>';`
