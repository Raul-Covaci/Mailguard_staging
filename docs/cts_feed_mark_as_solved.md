# CTS feed — câmp nou `mark_as_solved` (mailuri automate → SOLVED)

**Status:** implementat în Cargo360 (staging). **Necesită implementare pe partea CTS** (vezi secțiunea finală).
**Versiune:** 2026-06-30.

## Ce s-a schimbat

Endpoint-ul de feed `GET /api/v1/cts/get_emails` returnează acum, pentru **fiecare mesaj**, un câmp boolean nou:

```jsonc
{
  "@odata.type": "#microsoft.graph.message",
  "id": 39333,
  "subject": "... / Daily summary for toll products for 2026-06-29",
  "from": { "emailAddress": { "address": "noreply@itsbulgaria.com" } },
  // ... restul câmpurilor Graph + clasificare (categorie/departament/prioritate) neschimbate ...
  "mark_as_solved": true          // <— NOU. default false.
}
```

- **`mark_as_solved: false`** (implicit) → comportament neschimbat: CTS ingestează mailul ca până acum (status `new`).
- **`mark_as_solved: true`** → mailul este o notificare automată recunoscută; **CTS trebuie să-l seteze direct pe `SOLVED`** la ingestie (fără a mai trece prin coada de operator).

Câmpul este **mereu prezent** în payload. Restul contractului (envelope `@odata.*`, `value[]`, atașamente
cu `contentBytes`, ack-ul `POST /cts/update_emails`) rămâne **identic**. CTS-ul actual care ignoră câmpuri
necunoscute nu e afectat până implementează partea lui.

## Cum decide Cargo360 `mark_as_solved`

Determinist, pe baza **expeditorului** + (opțional) a unui **substring în subiect**, case-insensitive.
Reguli (config în `settings['cts.auto_solved_rules']`, editabile fără deploy; built-in identic în cod ca fail-safe):

| # | Expeditor(i) | Subiect conține (oricare) | Exemple |
|---|---|---|---|
| 1 | `noreply@itsbulgaria.com` | `Daily summary for toll products for` | #39333, #38071 |
| 2 | `secretariat@urbansiasociatii.ro` | `nregistrare` (orice subiect cu „Inregistrare"/„Înregistrare") | #36481, #36486 |
| 3 | `noreply@hu-go.hu` | `Vélelmezett jogosulatlan úthasználat miatti riasztás` | #39325, #39082 |
| 4 | `support@expert-erp.net` | *(orice subiect — doar pe expeditor)* | #39191, #38714 |
| 5 | `notificari@euplatesc.ro`, `mis.batch@btrl.ro`, `notificari@europayment.services` | `Tranzactii zilnice` **sau** `Tranzactii ecomm` | #38242, #38250, #39284, #39341 |

Semantică regulă: `from_address ∈ senders` **ȘI** (`subject_contains` gol → orice subiect; altfel vreun
substring apare în subiect). Expeditor: match exact pe adresă **sau** pe domeniu (intrare `"@domeniu"`).

Format config (JSON în `settings`):
```json
[
  {"senders": ["noreply@itsbulgaria.com"], "subject_contains": ["Daily summary for toll products for"]},
  {"senders": ["support@expert-erp.net"], "subject_contains": []}
]
```
**Kill-switch:** `settings['cts.auto_solved_rules'] = []` → nimic nu se marchează (toate `false`).

## Trasabilitate în Cargo360

- Coloana `emails.cts_mark_solved` (bool) e setată `TRUE` la **confirmarea CTS** (`POST /cts/update_emails`),
  pentru mailurile chiar livrate care se potrivesc unei reguli → „a plecat ca solved".
- În lista de emailuri (UI) apare un badge **„Solved→CTS"**: contur punctat = se potrivește regulii (urmează
  să plece ca solved); plin verde cu ✓ = a plecat efectiv ca solved (confirmat de CTS).

## Pentru product owner CTS (ce trebuie făcut pe partea CTS)

> În feed-ul `GET /api/v1/cts/get_emails`, fiecare mesaj are acum un boolean `mark_as_solved` (default `false`).
> Vă rugăm ca, la ingestia unui mesaj cu **`mark_as_solved == true`**, CTS să creeze/actualizeze sesizarea
> direct cu status **`solved`** (în loc de `new`), restul fluxului rămânând neschimbat. Ack-ul
> (`POST /cts/update_emails` cu `{"saved":[...]}`) nu se modifică. Lista de reguli e gestionată în Cargo360
> și poate fi extinsă fără modificări la CTS.
