# CHANGELOG IRIS Cargo360

<!-- CONVENȚIE VERSIUNI (din 2026-08-06):
     Schema: MAJOR.MINOR.PATCH
     - MAJOR crește la fiecare Release spre producție (v1.0, v2.0, ...)
     - MINOR crește între release-uri pentru feature-uri (v1.1, v1.2, ...)
     - PATCH crește pentru fix-uri între release-uri (v1.0.1, v1.1.2, ...)
     Istoricul pre-release (v0.x) păstrat mai jos pentru referință.
-->

## v3.7.0 - 2026-08-20

### MINOR — Redirect VATHUB: mailurile oficiale de recuperare TVA pleacă singure spre căsuța comună

Autoritățile fiscale scriu pe adresa personală a persoanei care a depus declarația 318, nu pe o
adresă de firmă. Până acum Diana forwarda manual fiecare decizie către Evelina, Nicoleta, Ana-Maria
și Cristina. Mecanismul nou face redirectul automat spre `vathub@cargotrack.ro`, de unde le citește
aplicația VATHUB — deci toate patru le văd în timp real, fără forward manual.

**Lista de expeditori vine din trafic, nu din presupuneri.** Analiza a luat 392 de forwarduri între
cele 5 adrese și 204 mailuri primite direct din exterior (15 iul – 13 aug 2026), a extras headerele
`From:` din corpul mesajelor forwardate (inclusiv lanțurile `Fwd: Fw:`) și a scos 13 domenii de
autoritate: `mfinante.ro` (369 mailuri), `financnasprava.sk` (126), `nav.gov.hu` (94), `nra.bg` (47),
`fs.gov.cz` (20), `anaf.ro` (6), plus `aade.gr`, `correo.aeat.es`, `bmf.gv.at`, `mf.gov.pl`,
`vmi.lt`, `at.gov.pt`, `porezna-uprava.hr`.

Potrivirea se face **pe domeniu, cu subdomenii** — la SK, CZ, GR, AT și PL scriu inspectori
nominali, de pe adrese care se schimbă de la dosar la dosar (`nav.gov.hu` prinde
`elekafa@elekafa.nav.gov.hu`, `nra.bg` prinde `b.stoilova@ro22.nra.bg`). Boundary-ul e strict:
`evilnav.gov.hu` NU potrivește `nav.gov.hu`.

**Cele 13 domenii intră în migrație `muted`, adică inactive.** Sunt propuneri extrase din trafic,
nu reguli — devin active abia după ce sunt validate din UI. Nimic nu se redirectează până când
cineva nu bifează explicit, iar `enabled` din config e `false` la instalare.

**Ce se trimite:** mesajul original BRUT, luat prin `BODY.PEEK[]` (deci rămâne necitit în căsuța
proprietarei) și atașat intact ca `message/rfc822` — PDF-urile deciziilor, semnăturile și headerele
originale ajung neatinse la VATHUB. Subiectul rămâne NESCHIMBAT, fără prefix „Fwd:", fiindcă VATHUB
potrivește dosarele după numărul de referință din subiect. Expeditorul real ajunge în `Reply-To` și
în headerele `X-Vathub-*`. Nu se rescrie `From` cu adresa autorității — ar pica la SPF.

Forwardul pleacă prin SMTP-ul căsuței personale (câmpuri noi pe `personal_mailbox_accounts`, parolă
criptată cu `credential_crypto`; parola SMTP lipsă = aceeași ca la IMAP, cazul obișnuit).

**Gardă de destinație:** `vathub_send_guard.assert_forward_target_allowed()` se verifică per mail,
chiar înainte de conectarea SMTP — nu la salvarea configului, fiindcă acesta se poate schimba din UI
între două rulări. Singurele destinații acceptate sunt `vathub@cargotrack.ro` și cele două adrese de
test. Garda din `feedback_send_guard` rămâne neatinsă: acolo whitelist-ul protejează clienții reali
de mailuri de feedback, altă regulă de business.

**Două siguranțe împotriva inundării VATHUB:** la prima activare se iau doar mailurile din ultimele
`max_age_hours` (implicit 24), iar un mail examinat nu se reevaluează — o regulă adăugată azi se
aplică de la mailurile următoare, nu retroactiv.

UI, în „Căsuțe personale": buton **„Email-uri redirect VATHUB"** per căsuță, cu adresa țintă,
comutatorul de activare, lista de domenii/adrese (Validează / Suspendă / Șterge), „Rulează acum"
și ultimele mailuri potrivite, cu starea fiecăruia. Formularele de adăugare/editare a căsuței au
acum și câmpurile SMTP.

Rulează pe timer-ul existent de căsuțe personale (1 min), în continuarea pasului de detecție.

### MINOR — Comutator de filtrare spam/carantină, per căsuță

`personal_mailbox_accounts.filter_enabled` (implicit ON, deci comportamentul căsuțelor existente nu
se schimbă). OFF = mailurile se ingerează în continuare și redirectul VATHUB merge, dar nu se
scanează pentru spam/phishing și nu se mută nimic în SPAM/CARANTINA — cazul unei căsuțe care
primește doar corespondență oficială și pe care filtrarea nu are ce să ajute.

Oprirea închide și coada de scanare: rândurile rămase `pending` trec pe `verdict='filter_off'`.
Altfel ar fi scanate retroactiv la repornirea filtrului — exact ce nu vrea cineva care l-a oprit
deliberat. Filtrarea și redirectul VATHUB sunt comutatoare INDEPENDENTE.

UI: coloană „Filtrare" cu buton ON/OFF în tabelul de căsuțe (cu confirmare la oprire), plus bifă în
formularele de adăugare/editare. În modalul VATHUB există acum două comutatoare distincte —
„Redirect activ (global)" și „Activ pe această căsuță". API: `POST /personal-mailboxes/{id}/toggles`.

### PATCH — Corecții găsite la auditul propriu al redirectului

Verificat cu teste care simulează SMTP-ul și IMAP-ul, pe un mail ostil (mai mulți destinatari, `Cc`,
`Bcc`, `Reply-To` extern și tentativă de injectare de header prin `Subject`): forwardul pleacă doar
spre `vathub@cargotrack.ro`, wrapper-ul nu are `Cc`/`Bcc`, subiectul injectat e tăiat la newline de
parserul de mail, iar cu o destinație neaprobată în config se trimit ZERO mailuri (2 blocate).

1. **`received_at` e nullable** (mail cu `Date` nevalid) — filtrul de vechime excludea aceste rânduri,
   deci un asemenea mail n-ar fi fost examinat NICIODATĂ. Acum se folosește
   `coalesce(received_at, created_at)`.
2. **`smtp_port` era `SMALLINT`** (max 32767), deși API-ul acceptă porturi până la 65535 → eroare de
   overflow la un port mare. Trecut pe `INTEGER`.
3. **Numărătoarea încercărilor se făcea după trimitere.** Un proces oprit între SMTP și `COMMIT` ar
   fi reluat mailul la infinit. Acum contorul crește ÎNAINTE de orice I/O, deci un eventual duplicat
   e mărginit la `MAX_ATTEMPTS`; compromisul e deliberat — mai bine o copie în plus decât o decizie
   318 pierdută. Ramurile de eroare nu mai incrementează a doua oară.
4. **Câmp SMTP golit din UI** ajungea `''` în DB în loc de `NULL` (`_clean_host`).
5. `BATCH_PER_POLL` coborât de la 25 la 12: fiecare forward costă 2 conexiuni IMAP + 1 SMTP, iar
   poller-ul rulează la fiecare minut.

## v3.6.2 - 2026-08-21

### PATCH — Analitice apeluri: filtrul pe departament lipsea din „Top 10 clienți"

Completare la v3.4.0. Filtrul de departament/operator (atribuirea în trei trepte) fusese aplicat la
patru din cele cinci endpointuri ale paginii Analitice. **`/calls/analytics/top-clients` declara
parametrii `department` și `agent`, iar UI-ul îi trimitea, dar SQL-ul nu îi folosea deloc.**

Efectul pe dashboard: cu un departament selectat, KPI-urile, scorurile și donuturile se filtrau
corect, dar cardul „Top 10 clienți" continua să arate toată firma — două seturi de cifre
necomparabile, unul lângă altul, fără nimic care să indice diferența. Nu era o eroare vizibilă:
tabelul se încărca, doar că răspundea la altă întrebare decât restul ecranului.

Verificat apoi sistematic, prin parcurgerea AST a ambelor module: toate endpointurile din
`calls_analytics.py` și `calls.py` care declară `department`/`agent` îl folosesc acum efectiv.
`calls.py` (lista Apeluri) era deja corect — folosea de la început fragmentele de atribuire din
`productivity`, deci nu avea bug-ul de potrivire exactă pe nume.

**Drill-down: trunchiere tăcută.** `/calls/analytics/agent-calls` returnează maximum 200 de apeluri
(`LIMIT`), dar rândul agentului din tabelul de deasupra arată `call_count` complet — pentru un agent
cu peste 200 de apeluri scorate, lista părea că nu se potrivește cu totalul. Endpointul întoarce
acum și `total` (numărat înainte de LIMIT) și `truncated`, iar antetul panoului scrie explicit
„primele 200 din N apeluri scorate (cele mai slabe)".

**Documentat, nu rezolvat:** regula de atribuire agent→angajat există acum în două forme —
fragmentele SQL din `productivity` (folosite de raportul de productivitate și de lista Apeluri) și
`_AGENT_MAP_SQL` din `calls_analytics` (aceleași trepte, rezolvate o dată per interogare într-o
mapare cu cache de 5 minute, din care se construiește un `IN`). Formele diferă fiindcă una
îmbogățește rânduri și cealaltă filtrează. Comentariul din cod marca greșit relația ca „refolosire";
acum spune corect că e o oglindă și că orice schimbare de regulă (ex. prefixul de 4 litere) se face
în ambele locuri.

## v3.6.1 - 2026-08-21

### PATCH — Satisfacție: aliniere config DB + două incoerențe găsite la auditul pre-live

Audit înainte de punerea pe producție, după trei schimbări de direcție pe același modul.

**1. Configul din DB bătea codul (cel mai important).** `20260818_satisfaction_v6.sql` inserase
`settings.satisfaction.v6` cu `ON CONFLICT (key) DO NOTHING`, iar `_load_v6_config` dă prioritate
valorilor din DB față de `_V6_DEFAULTS`. Pe orice bază unde rândul exista deja — sau unde cineva
editase configul între v3.5.0 și v3.6.0 — codul nou ar fi fost livrat, dar comportamentul ar fi
rămas cel intermediar (start fix la 50 + media simplă). Migrația `20260821_satisfaction_v6_carry_config.sql`
forțează `carry_start_state=true`, `month_aggregation='last_week_final'` și adaugă `max_workers=6`,
păstrând restul cheilor. Șterge și `start_lookback_months` (rămăsese cu valoarea 3, deși lookback-ul
e nelimitat din v3.6.0 — codul nu o mai citește, dar mințea pe oricine inspecta configul).

**2. Conflict în tabelul de amplitudine.** Tabelul nou pusese `multumire_generala` (+2) în banda
mică (5-15 puncte), dar nota din taxonomie spune explicit că e un semnal **mai puternic** decât
`confirmare_rezolvare_multumire` (+3, banda medie). Mutat în banda medie, cu regula explicită că
atunci când tabelul de intensități și nota din taxonomie diferă, nota decide.

**3. Comentariu înșelător la `max_workers`.** Justifica paralelizarea prin „nu se mai reportează
stare între săptămâni" — fals din v3.6.0. Corectat: clienții sunt independenți între ei, dar
săptămânile unui client se înlănțuie și NU trebuie paralelizate.

Verificat la audit și găsit corect: pragul de 60% e consistent în tot backend-ul
(`_UNSATISFIED_BELOW`, fallback-ul din `clients.py`, bucket-urile de distribuție 40/60/75/90) și în
UI; graficul de trend din dashboard e deja pe 12 luni, iar istoricul per client pe 24; o lună cu
`satisfaction_pct` NULL nu rupe lanțul (`_previous_month_state` filtrează `IS NOT NULL` și merge
mai în urmă); rândurile de carry-forward propagă corect scorul pentru clienții fără activitate.

## v3.6.0 - 2026-08-21

### MINOR — Satisfacție clienți: traiectorie continuă între luni (+ regula de amplitudine care lipsea)

Cerință: dacă un client încheie luna la 80%, luna următoare pornește de la 80% și urcă/scade de
acolo — ca graficul pe 12 luni să arate evoluția graduală, nu o resetare la 50% în fiecare lună.

Asta reactivează reportarea eliminată în v3.5.0, **dar nu în forma în care nu funcționa**. Prima
versiune (v3.3.x) eșua pentru un motiv diagnosticabil: promptul dă intensitățile evenimentelor pe
scala `-5 … +5` și starea pe `0 … 100`, fără nicio regulă de conversie. Modelul avea două ieșiri,
ambele rupeau continuitatea — fie mișca starea cu câteva puncte dintr-o sută (dintr-un start de 80,
o reclamație gravă dădea 75, deci o linie aproape plată), fie re-ancora în banda care descria luna
și arunca startul complet. De aceea reportarea se reintroduce **împreună cu regula de amplitudine**,
care era piesa lipsă.

**Regula de calcul:**
- luna pornește din **ultimul scor cunoscut** al clientului, oricât de vechi (lookback nelimitat);
  neutru (50) doar dacă nu există niciun scor anterior;
- fiecare **săptămână pornește din starea în care s-a încheiat cea precedentă**; o săptămână fără
  scor (N/A / IRIS eșuat) nu rupe lanțul;
- **scorul lunii = starea la finalul ultimei săptămâni scorate**, iar aceea se reportează mai
  departe. Revenire de la media simplă introdusă în v3.5.0: o medie nu descrie „unde a ajuns
  clientul", deci nu e o valoare care se poate reporta coerent. Mediile rămân calculate și expuse
  în `month_avg_detail` pentru comparație;
- prag nesatisfăcut nemodificat (sub 60%), benzile de segment nemodificate.

**Regula de amplitudine, nouă în prompt** (secțiunea „Cum se traduce intensitatea în mișcare pe
scala 0-100"). Taxonomia și intensitățile nu se schimbă — se adaugă doar conversia care lipsea:
intensitate mică (±1..±2) = 5-15 puncte, medie (±3) = 15-25, mare (±4..±5) = 25-40; valoarea din
interiorul benzii se alege după modificatorii de severitate; recuperarea confirmată rămâne
neplafonată și poate depăși banda; starea saturează la 0 și 100; evenimentele se aplică în ordine
cronologică, fiecare peste starea rezultată din cel anterior.

**Auditul de start rămâne și devine mai util:** `start_state_drift` se calculează acum față de
startul TRIMIS pe fiecare săptămână (nu față de 50), iar `breakdown.start_audit` are și
`sent_per_week`. Un `drift_max` nenul înseamnă că modelul nu a pornit din starea reportată — adică
exact simptomul care a dus la eliminarea primei versiuni. UI-ul îl semnalează explicit în fișa
clientului.

⚠️ **Ordinea lunilor contează la rescorare.** Luna N citește scorul lunii N-1 din snapshot, deci o
rescorare pe mai multe luni se face cronologic crescător. În interiorul unei luni clienții rămân
independenți, deci paralelizarea din v3.5.0 e nemodificată și sigură; săptămânile unui client se
scorează secvențial, cum cere înlănțuirea.

Config (`settings.satisfaction.v6`, fără redeploy): `carry_start_state` (implicit `true`),
`month_aggregation` (implicit `last_week_final`; `avg_weeks` / `weighted_avg_weeks` rămân ca revert,
dar reportează o medie).

Verificat local, cu IRIS și DB stubuite — iulie încheiat la 80, august cu trei săptămâni:
```
W32  start 80.0  <- snapshot lunar 2026-07     -> 45.0
W33  start 45.0  <- saptamana 2026-W32         -> 70.0
W35  start 70.0  <- saptamana 2026-W33         -> 70.0
scor august = 70.0  (media simpla ar fi dat 61.7)   -> septembrie porneste de la 70.0
```
Verificate și: fără istoric → start 50 („neutru implicit"); model care raportează start 50 când i
s-a trimis 80 → `drift_max = 30.0`.

## v3.5.1 - 2026-08-20

### PATCH — Procesare documente: „Anexa 2" nu se mai redenumește „proces verbal"

Documentul de instalare/predare echipament, provenit din documentele de șofer, se numește acum
**Anexa 2** și nu mai are nicio legătură cu un proces verbal. Fișierul încărcat era corect, dar
numele standardizat și explicația ieșeau ca „proces verbal".

Cauza: promptul de redenumire `RENAME_SYSTEM_PROMPT` e **hardcodat în cod**
(`app/api/v1/documents.py`), nu în baza de date — deci editarea prompturilor din interfață nu îl
atingea. Avea trei probleme:
- lista de tipuri din input includea `proces verbal`, tip care nu mai există;
- secțiunea „C. Alte documente" conținea regula `Proces verbal → proces verbal`, adică exact numele
  greșit;
- Anexa 2 era descrisă drept „contract carbon" și stătea la „Alte documente", deși aparține
  pachetului contractului carGObox, alături de Anexele 1, 3 și 4.

Ștergerea regulii nu era suficientă: `tip_document` primește **numele tipului din
`document_types`** (`detected_type` la salvarea extragerii, `tmap[tid]["name"]` la reidentificare),
iar acel nume poate purta încă denumirea veche. Regula de fallback din prompt („returnează o
variantă normalizată a denumirii tipului") ar fi reintrodus „proces verbal" pe altă cale.

Corecția are două straturi:
1. **Prompt** — Anexa 2 intră în pachetul contractului Cargobox, cu o regulă de alias explicită:
   orice tip care conține „proces verbal" (în orice formă, inclusiv „Anexa 2 - Proces verbal
   CargoBox" sau „PV") se redenumește `anexa 2`, iar aliasul are prioritate față de normalizarea
   denumirii primite.
2. **Gardă deterministă** — `_normalize_legacy_doc_name()`, aplicată pe numele întors de AI înainte
   de scriere: un nume care conține denumirea istorică devine exact `anexa 2` + extensia. Promptul
   e AI și poate aluneca; garda face rezultatul previzibil, în același spirit ca `_vehicle_std_name`
   (introdus tot ca să scoată AI-ul din calea numelor deterministe). Se înlocuiește tot numele, nu
   doar tokenul, altfel un model care repetă denumirea din DB producea dubluri („Anexa 2 - Proces
   verbal CargoBox" → „Anexa 2 - anexa 2").

Numele final ajunge în `document_extractions.renamed_file` **și** `part_label` — ambele citite de
exportul CTS.

Verificat local pe 16 cazuri de nume: toate variantele denumirii istorice (spațiu, underscore,
cratimă, lipite, cu/fără „CargoBox", majuscule, cu/fără extensie) devin `anexa 2`, iar numele
legitime (`anexa 2`, `anexa 3`, `contract_cargobox`, `RO_BH01CTS_VP_02`, `formular de înregistrare`)
rămân neatinse.

**Fără backfill** — decizie explicită: se aplică doar documentelor procesate de acum înainte.
Extragerile existente păstrează numele vechi.

⚠️ Rămas de decis separat: în `document_types`, rândul se numește încă „Anexa 2 - Proces verbal
CargoBox", iar numele acela e afișat în UI, injectat în catalogul promptului de clasificare
(`_types_catalog` → `_build_classify_system`, de unde vine textul explicației) și exportat la CTS
prin `iris_docsvc`. Redenumirea lui nu s-a făcut niciodată printr-un fișier de migrație, deci pe
producție tipul e probabil încă „Proces verbal CargoBox" în categoria `vehicul`.

## v3.5.0 - 2026-08-20

### MINOR — Satisfacție clienți: start neutru pe fiecare săptămână, scor lunar = media săptămânilor

Adminii au raportat că reportarea stării între luni nu se comporta cum a fost gândită („dacă în
iulie clientul a ieșit cu 30%, august ar trebui să pornească de la 30%"). Investigația a găsit trei
probleme, dintre care una face imposibilă chiar și diagnoza:

1. **Promptul mixează două scale fără regulă de conversie.** Taxonomia evenimentelor e pe `-5 … +5`,
   starea pe `0 … 100`, iar promptul nu spune niciodată cât mișcă un `-3` pe axa 0-100. Modelul
   inventa conversia: fie citea intensitatea ca puncte pe 0-100 (`reclamatie_generala` = -5 peste
   50 → 45, adică „Neutru", absurd), fie re-ancora în banda care descria săptămâna și arunca
   startul reportat.
2. **Răspunsul modelului despre startul folosit era aruncat.** Promptul cere obligatoriu
   `start_state` în JSON tocmai ca startul să fie auditabil, dar codul nu îl citea niciodată —
   scria înapoi valoarea trimisă de noi. Breakdown-ul arăta *mereu* ca dacă reportarea ar fi
   funcționat, orice ar fi făcut modelul.
3. **`stare_initiala` din payload nu avea unitate** — un `30.0` gol, fără „scala 0-100", lângă o
   taxonomie de intensități `-5..+5`.

**Regula de calcul nouă (decizie business):**
- fiecare **săptămână ISO** cu interacțiuni pornește de la **50 (neutru)** și se evaluează
  independent — nu se mai reportează stare, nici între săptămâni, nici din luna precedentă;
- săptămânile fără interacțiuni se sar complet (nu intră în medie, nu contează ca 50);
- **scorul lunii = media simplă a săptămânilor scorate** — o săptămână cu 1 interacțiune cântărește
  cât una cu 20; o singură săptămână scorată ⇒ scorul ei e scorul lunii;
- **scorul vine exclusiv din răspunsul promptului V6** — codul nu ajustează, nu plafonează, nu
  injectează stare;
- **prag nesatisfăcut: sub 60%**, granița dintre „Satisfăcut" (60-74) și „Neutru" (45-59) din
  tabelul de benzi al promptului. Până acum coexistau trei valori: 70 în cod, 90 în documentație
  (rămas din motorul vechi cu 5 factori) și benzile din prompt. `_segment()` folosește acum
  aceleași benzi (`sanatos` ≥60, `neutru` 45-59, `la_risc` 30-44, `critic` <30).

**Auditul startului**, adăugat ca să nu se mai repete cazul 2: se stochează atât intenția
(`weekly_trajectories[].start_state`, mereu 50) cât și ce a raportat modelul
(`start_state_model`, `start_state_drift`, agregat în `breakdown.start_audit`). Un `drift_max`
diferit de 0 înseamnă că promptul nu a pornit din neutru — fișa clientului afișează un avertisment
explicit, nu se mai ascunde în JSON.

**Scala de intensitate din prompt nu a fost atinsă** (decizie explicită: satisfacția e determinată
exclusiv de promptul V6 și de ce conține el). Dacă un incident grav aterizează la ~45 dintr-un
start de 50, confuzia de scală de la punctul 1 e confirmată și se rezolvă separat, pe cifre reale.

Cod eliminat: `_previous_month_state()`, `carry_start_state`, `start_lookback_months`, înlănțuirea
`chain_state` între săptămâni.

### PERFORMANȚĂ — snapshot-ul lunar: de la ~1h la ~10 min pentru 300 de clienți

Descompunerea orei: ~94% era **latență IRIS serializată** (300 clienți × ~3 apeluri × ~4s), din
care apelurile săptămânale ~79% și sinteza lunară ~15%; sleep-urile fixe erau 5% și interogările
DB 2% — adică optimizarea lor separată n-ar fi schimbat nimic.

Clienții sunt complet independenți (după eliminarea reportării de stare), deci se procesează acum
în paralel cu `ThreadPoolExecutor`. Numărul de fire vine din `settings.satisfaction.v6` →
`max_workers` (implicit **6**, plafon 16), ca să se poată ajusta fără redeploy: gateway-ul IRIS e
partajat cu clasificarea mailurilor și scorarea apelurilor.

Detalii de implementare:
- fiecare fir primește **conexiunea lui** psycopg2 (`threading.local`), pe autocommit — o conexiune
  partajată nu e thread-safe, iar tranzacțiile lungi ar sta deschise cât durează apelurile IRIS;
- **scrierea rămâne pe un singur fir**: workerul întoarce rândul, nu îl inserează. `BATCH_SIZE=1`
  se păstrează, deci UI-ul vede în continuare progresul client cu client;
- pauzele fixe (`AI_CALL_SPACING_SECONDS` de 0.3s după fiecare client, 0.25s între săptămâni) au
  fost scoase — numărul de fire e acum rate-limit-ul, iar `iris_ai` are deja retry cu backoff pe
  429/5xx;
- un client care eșuează incrementează `errors` și rularea continuă (workerul nu propagă excepții).

Verificat local pe 60 de clienți cu IRIS și DB stubuite: **6.0x accelerare** la 6 fire, concurență
maximă observată exact 6 (plafonul respectat), toate rândurile scrise, iar o excepție injectată pe
un client a fost izolată corect (59 procesați, 1 eroare).

Sinteza lunară a rămas neschimbată (produce rezumatul afișat în UI).

Migrație: `20260820_satisfaction_unsat_threshold_60.sql` — re-derivă **doar** flagul boolean
`is_unsatisfied` din `satisfaction_pct`-ul deja stocat (fără apel IRIS, fără schimbare de scor),
altfel graficul de nesatisfăcuți ar amesteca luni evaluate cu prag 70 și luni cu prag 60.
**Istoricul nu se rescorează automat** — snapshot-urile de dinainte de 2026-08-20 rămân cu
valorile produse de logica înlănțuită; rescorarea rămâne disponibilă client-cu-client din
interfață.

## v3.4.0 - 2026-08-19

### MINOR — Analiza apelurilor: întrebări binare reale, filtru pe departament funcțional, scor per apel

**1. Întrebările binare erau altele decât cele alese, și nu aveau prompturi.**
Motorul citea deja patru chei binare (`agentulSaPrezentat`, `clientulAmintaJudecata`,
`clientulAmintaRenuntare`, `clientulContactatAnterior`), dar ele nu existau nici în seed, nici ca
fișier de prompt — deci nu rulau niciodată, iar coloanele din `call_ai_scores` rămâneau NULL.
Cardurile afișau în schimb `issueResolution` (marcat greșit ca binar, deși are patru câmpuri) plus
trei indicatori derivați hardcodați în cod.

Setul afișat e acum exact cel cerut de business, fiecare cu prompt propriu versionat în
`app/services/prompts/calls/`:
- Mașini care nu transmit *(nouă)*
- Clientul ne amenință că ne dă în judecată?
- Agentul s-a prezentat la începutul apelului?
- Clientul a amenințat că renunță la colaborarea cu noi?
- Clientul a menționat că ne-a contactat anterior, dar nu a primit răspuns?

Fiecare răspuns vine acum cu citatul pe care s-a bazat modelul (`call_ai_scores.binary_evidence`),
vizibil în tab-ul „Analiza AI" al apelului. O întrebare binară nouă nu mai cere migrație: fără
coloană dedicată, rezultatul se citește din `binary_evidence`.

**2. Selectarea unui departament nu încărca nimic.**
Filtrul compara `calls.agent_extension` (numele scris de centrala While1) cu
`employee_department_mapping.name` prin egalitate exactă — dar cele două nu coincid niciodată
("Oana Lasca" vs "Lasca Oana-Maria", "Adriana Brasovean" vs "Buse Angelica-Adriana"). Zero
potriviri ⇒ zero rânduri pentru orice departament; mergea doar „Operational (toate)", care nu
aplică filtrul.

Analiticele folosesc acum aceeași atribuire în trei trepte ca raportul de productivitate: mapare
învățată din suprapunerea cu CTS → potrivire de nume tolerantă la ordine și la prefixe de 4 litere
→ assignee-ul CTS pentru apelurile fără nume de agent în CDR. Se aplică la fel pe filtrul de
operator. Selectorul de departamente listează doar departamentele care au efectiv agenți în
centrală.

**3. Scor per apel, nu doar medii per agent.**
Tabelul „Scoruri Agenți" arăta doar medii. Un click pe rândul agentului deschide acum lista
apelurilor lui, cu scorul agent/client al fiecăruia, rezolvarea, indicatorii binari declanșați și
problema principală — sortate crescător după scor, deci apelurile slabe primele. Click pe un apel
deschide modalul de apel direct pe tab-ul „Analiza AI". Endpoint nou:
`GET /calls/analytics/agent-calls`.

Migrație: `20260819f_call_binary_questions.sql`.
După deploy: `venv/bin/python3 scripts/sync_call_prompts.py`, apoi „Rescoreaza apeluri incomplete"
din tab-ul Scoruri Agenți (apelurile deja scorate nu au întrebările binare completate).

## v3.3.1 - 2026-08-19

### PATCH — Monitor: „Reclamații / În lucru" număra pe tot istoricul, nu pe luna afișată

Cardul de departament punea alături două cifre cu numitori diferiți: **„Total lună"** (reclamațiile
înregistrate în luna curentă) și **„În lucru"** (status 2 pe **tot istoricul**). De aceea Taxe de
drum arăta „11 total / 9 în lucru" deși în luna curentă erau 3 în lucru — restul erau reclamații
vechi, rămase deschise din lunile anterioare. Idem Suport 2: 6 total / 6 în lucru, în loc de 2.

`noi` și `in_lucru` se raportează acum la **aceeași fereastră ca `total_luna`**, în ambele
interogări ale monitorului (agregat + per departament). E aceeași convenție ca la mail/task, unde
stările deschise se numără din ce a sosit în fereastra afișată, tocmai ca să nu apară restanța
istorică din CTS.

Verificat local (august): taxe_drum 10 total / **9 → 2** în lucru; suport_2 6 total / **6 → 4** în
lucru; suport_1 și suport_3 neschimbate. `deschise` / `restante` / `peste_7z` rămân pe tot
istoricul — sunt folosite de blocul `sesizari`, unde restanța chiar e subiectul.

## v3.3.0 - 2026-08-19

### MINOR — Zi fără nimeni pontat = zi inactivă (concediile nu mai consumă SLA)

O zi în care nu e pontat niciun angajat al departamentului e **inactivă, ca duminica** — iar
sâmbăta cu un singur om pontat e, invers, zi lucrătoare. Până acum, o zi **fără niciun rând** de
pontaj era tratată ca „poate lucrătoare" și cădea pe `department_schedule`, deci SLA-ul curgea și
în concedii.

Caz real: `suport_3` are un singur angajat (Tyepak Zoltan), în concediu aprobat **10–21.08.2026**.
Cele 10 zile lucrătoare din concediu se măsurau pe programul manual 08:00–17:30, deși nu era
nimeni — iar 13 din 36 de reclamații ale lunii au `solved_at` după 07.08, deci chiar prindeau
ferestre inexistente.

Regula distinge **de ce** lipsește pontajul:

- există rânduri de pontaj, dar 0 prezenți → zi inactivă (ca înainte);
- nu există rânduri, dar **toți** angajații activi ai departamentului sunt în concediu aprobat
  (`employee_schedule` sau `cts_dv_employee_vacation_request`, status 1/2) → zi inactivă;
- nu există rânduri și nu e concediu general → zi potențial lucrătoare, fallback pe
  `department_schedule`. Fără această ramură, o pană de sync ar opri tot SLA-ul și toate scorurile
  ar sări la 100%.

Verificat pe august 2026: singurul departament cu zile „toți în concediu" e `suport_3` (10 zile);
restul au 0, deci sunt neatinse. `zile_lucratoare` suport_3: 21 → **11**; reclamații contact
83,33% → **90,0%**. Celelalte departamente: cifre identice.

Ca bonus, dispare o divergență Python/SQL: o zi cu rânduri de pontaj dar 0 prezenți era tratată ca
lucrătoare în Python și ca inactivă în SQL. Acum ambele o consideră inactivă.

Schimbarea e în oglindă: `_BizCache.is_working_day_for_dept` (mailuri, apeluri, operațiuni) și
funcția SQL `business_minutes_emp` prin
`migrations/20260819e_business_minutes_concediu_zi_inactiva.sql`. Verificat că dau identic pe
același interval (07.08 15:00 → 24.08 10:00 = 210 min în ambele).

## v3.2.1 - 2026-08-19

### PATCH — Dedupare apeluri: nu mai ascunde apelurile pierdute reale

Regula din v3.2.0 („leg nerăspuns cu sibling răspuns în ±15 min") era prea largă: ascundea și
cazul în care clientul a sunat, n-a prins, și a sunat DIN NOU peste câteva minute — prima încercare
e un apel pierdut real, nu un leg de centrală.

Semnătura leg-ului fantomă, măsurată pe iulie+august: `callee_number IS NULL` + sibling răspuns la
câteva secunde (din 5352 de legături nerăspunse cu sibling, **4493 aveau `callee_number` NULL**, iar
4900 erau la sub 150s de sibling). Regula devine:

- `callee_number IS NULL` **și** sibling răspuns în ±2 min (ring paralel), **sau**
- sibling răspuns în ±30 s, indiferent de `callee_number` (același eveniment fizic).

Efect pe august: 1365 leg-uri ascunse (față de 1561) și **862 apeluri pierdute vizibile (față de
666)** — 196 de apeluri pierdute reale nu mai dispar din raport. Cazul de referință
(`0747586201`, leg 0s + răspuns la 26s) rămâne colapsat corect.

**Ieșirile NU se deduplică** (verificat pe cerere): „suspectele" de pe outbound au număr valid și
status `BUSY`/`NO ANSWER`, urmate de o reușită — sunt reapelări reale ale operatorului, nu leg-uri
fantomă. Ascunderea lor ar șterge muncă făcută.

Calculul de productivitate rămâne neatins (folosea deja `_APEL_REAL_CALL_SQL`).

## v3.2.0 - 2026-08-19

### MINOR — Apelurile nu mai apar dublate (leg-uri de centrală)

Centrala sună în PARALEL pe mai multe aparate și scrie un CDR **per canal**, deci același apel
fizic apărea de două ori în listă: un leg `NO ANSWER` de `0:00` („fără conversație / fără
înregistrare", fără `callee_number`) și leg-ul răspuns, cu durata reală. Exemplu din producție:
`0744525434` → 18.08 17:07 „0:00" + 18.08 17:06 „2:32", același agent, același client.

Regulă nouă (`productivity.apel_no_dup_leg_sql`, o singură definiție): un leg de intrare nerăspuns
se **ascunde** dacă există un leg răspuns pentru același număr în ±15 min — adică exact când nu e
apel pierdut. **Apelurile pierdute reale rămân vizibile.** Pe august 2026: 1561 de leg-uri
duplicate ascunse, 666 apeluri pierdute păstrate (filtrul „fără răspuns" arată acum 659 în loc de
2215 rânduri brute).

Aplicat în: pagina Apeluri (`GET /calls`, cu `include_legs=1` pentru inspecție), tab-ul Apeluri al
clientului, contoarele de apeluri din lista și fișa clientului, `activity_score`, pagina Analitice
(toate KPI-urile și seriile) și statisticile de Dashboard. Volume după colapsare, august: intrări
4354 → 2793 rânduri.

Calculul de productivitate **nu se schimbă** — folosea deja doar conversațiile reale
(`_APEL_REAL_CALL_SQL`): scoruri identice înainte/după (suport_1 94,36 / suport_2 88,66 /
taxe_drum 92,18).

### MINOR — „Nemăsurat" pe apeluri reale: backfill `ring_seconds`

Cauza nu e leg-ul de `0:00` (acela e deja exclus din calcul), ci lipsa timpului de răspuns:
`calls.ring_seconds` a apărut în migrația `20260813c`, după ce ingestul While1 rula de luni, iar
cursorul incremental nu mai atinge rândurile vechi. Pe august 2026: **922 din 2127** de apeluri
primite și răspunse n-au nici `ring_seconds`, nici rând în CTS → ies „nemăsurate".

Endpoint nou: `POST /api/v1/calls/backfill-ring?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD` (admin) —
re-interoghează While1 pe interval și completează `ring_seconds` doar unde e NULL (idempotent, nu
rescrie CDR-ul, nu mișcă cursorul incremental). Funcția exista în serviciu, dar nu era apelabilă.

## v3.1.0 - 2026-08-19

### MINOR — Timpul de lucru se măsoară pe PONTAJ, nu pe programul manual

Fereastra în care curge SLA-ul era luată din `department_schedule` (tabelă populată manual), iar
pontajul servea doar la verificarea „≥1 prezent". Tabela divergase de realitate: `suport_2` era
configurat **07:00–22:00**, deși turele reale sunt 08:00–16:30 și 12:30–21:00.

Caz real, Suport 2 / august: mail intrat sâmbătă 01.08 19:04 (după program), rezolvat luni 03.08
09:36 → se raportau **156,7 min (2h37, „Overdue")**, pentru că numărătoarea pornea luni la 07:00.
Al doilea: 02.08 00:31 (duminică) → 03.08 09:30 = **150,8 min (2h31)**.

Precedență nouă (decizie business): sursa de adevăr e **„Utilizatori → Pontaj pe departamente"**
(`employee_attendance` — preluat din CTS sau ajustat manual):

1. există pontaj cu ore în ziua respectivă → fereastra = **uniunea turelor celor prezenți**
   (primul început → ultimul final);
2. nu există pontaj utilizabil (zi neimportată / viitoare / prezenți fără ore) → fallback pe
   `department_schedule`;
3. nici program configurat → ziua nu curge. Zi cu 0 prezenți → nu curge (neschimbat).

Cele două cazuri de mai sus dau acum **96,7 min (1h36)** și **90,8 min (1h30)** — identic în Python
și în SQL. Schimbarea e oglindită în `_BizCache._dept_window` (mailuri, apeluri, operațiuni) și în
funcția `business_minutes_emp` (task-uri), prin
`migrations/20260819d_business_minutes_pontaj_first.sql`.

Impact pe august 2026 (scoruri recalculate, ferestrele fiind mai mici): suport_2 82,57 → **88,66**,
taxe_drum 87,47 → **92,18**, suport_3 91,67 → **93,00**, suport_1 94,25 → **94,36**, contabilitate
neschimbată (n-avea program configurat, deci folosea deja pontajul).

### MINOR — Clienții excluși din productivitate se aplică pe TOATE canalele

Lista de excluderi (`clients.productivity_exclude`: HU-GO, LOCATOR BG, RUPTELA, TOLL4EUROPE,
00-FIRMA NECUNOSCUTA LA MONTAJ, ORANGE ROMANIA, HELP DESK CTS, CTS INTERNAL CLIENT) era consultată
doar pe mailuri, și acolo doar prin `emails.client_id`. Acum se aplică pe fiecare canal, cu
legătura corectă pentru fiecare sursă:

- **mail** — flagul pe clientul dedus local **plus** clientul atribuit în CTS (`extra.client_id`,
  ID IRIS): 70 de mailuri pe august scăpau doar prin al doilea;
- **task** — `cts_task_ground_truth.client_id` e ID IRIS (595 rânduri se potrivesc pe
  `iris_client_id` față de 29 pe cheia locală), cu fallback pe nume: **91 de rânduri** pe august
  (comercial 43, suport_2 42, taxe_drum 4, contabilitate 1, suport_1 1);
- **apel** — `calls.client_id` (cheie locală);
- **operațiuni** — doar pe nume, `device_operations.client_id` fiind NULL pe toate rândurile;
- **reclamații** — `cts_quality_evaluation` nu are câmp de client, deci nu se poate filtra (rămâne
  de cerut în feed).

Aplicat în scor, breakdown, prognoză, analitice **și** în cele 16 interogări ale monitoarelor
(filtrare în subquery, ca un filtru uitat într-un panou să nu mai poată arăta alt volum).
`migrations/20260819c_productivity_exclude_reassert.sql` re-asertează lista (util pe producție,
unde s-a constatat drift de migrații). Volume după filtrare pe august: task suport_2 1390 → 1348,
taxe_drum bgtoll 42 → 40 / etoll 58 → 57 / hugo 90 → 89, email contabilitate 689 → 686.

## v3.0.2 - 2026-08-19

### PATCH — Reparație drift schemă pe producție (cauza reală a sync-ului eșuat)

Diagnosticul pe producție a confirmat: `_release_migrations` marchează ca aplicate migrațiile
`20260629_client_contract_category.sql` și baseline-ul pe `clients`, dar coloanele **nu există** în
schemă — marcare fără execuție. Lipseau `client_contracts.category`, `clients.created_at`,
`clients.updated_at` și indexul `client_contracts_category_idx`. Sync-ul cădea la primul INSERT
(`column "category" does not exist`), tranzacția se aborta, restul lotului raporta doar mesajul
generic → `client_vehicles` și `client_contracts` cu **0 rânduri** pe producție.

`migrations/20260819b_repair_client_schema_drift.sql` — fișier nou (nume nou ⇒ `migrate.sh` îl
rulează chiar dacă vechile migrații sunt marcate aplicate), strict aditiv și idempotent: reasertează
toate coloanele folosite de sync pe `clients` / `client_contracts` / `client_vehicles`, plus
indexurile dependente și cele două indexuri UNICE pe care se sprijină `ON CONFLICT`. Pe mediile
sănătoase (staging, local) e no-op — verificat.

### PATCH — Productivitate: operațiunile pseudo-clienților ies din calcul

„00-FIRMA NECUNOSCUTA LA MONTAJ" (placeholder de montaj, nu o firmă) intra în obiectivul
*Operațiuni* al Suport 2 cu **38 de rânduri măsurabile pe august 2026** (37 pe „Instalare nouă", 1 pe
„Mutare"). Clientul era deja marcat `productivity_exclude = TRUE`, dar excluderea se aplica doar pe
mailuri: `_fetch_device_ops_rows` și breakdown-ul de operațiuni nu o consultau deloc.

- Ambele query-uri filtrează acum pe `clients.productivity_exclude`. Potrivirea se face pe **nume**,
  nu pe id, fiindcă `device_operations.client_id` e NULL pe toate rândurile (view-ul DV nu-l trimite).
- `migrations/20260819c_productivity_exclude_reassert.sql` re-asertează lista de excluderi (util și
  pe producție, unde s-a constatat drift de migrații).
- Verificat local pe august: instalare_noua 357 → 320, mutare 129 → 128; restul canalelor neatinse.
- Sursa de adevăr rămâne flagul din `clients`, nu o listă hardcodată — o excludere nouă se face o
  singură dată, pentru toate canalele.

### PATCH — Productivitate (Rapoarte): un card de departament pe rând

Grila trece de la 2 carduri pe rând la **1** (`repeat(1, minmax(0,1fr))`). Cardul fiind acum pe toată
lățimea, gauge-ul nu mai stă pe o coloană de 50% — gauge.js calculează raza din înălțime (200px),
deci arcul rămânea mic, centrat între două zone goale. Acum: coloană proprie de `minmax(280px,340px)`
pentru gauge, restul lățimii pentru chip-urile de metrici, care se așază pe un singur rând
(`repeat(auto-fit, minmax(150px,1fr))`). Tab-ul „Obiective & Ponderi" rămâne pe 2 coloane.

## v3.0.1 - 2026-08-19

### PATCH — Sync clienți: o eroare pe un client nu mai omoară tot lotul

Simptom (producție): „Sync eșuat: `current transaction is aborted, commands ignored until end of
transaction block`", cu vehicule/contracte goale. Cauza nu e clientul pe care pică, ci faptul că tot
pull-ul (~16k clienți) rula într-o **singură tranzacție**: prima instrucțiune căzută abortează
tranzacția, iar `except`-ul din jurul sync-ului de assets înghițea eroarea reală și continua — toate
instrucțiunile de după ieșeau cu mesajul generic, singurul care ajungea în UI.

- Fiecare client rulează acum într-un **SAVEPOINT** propriu: la eroare se face `ROLLBACK TO
  SAVEPOINT` doar pentru el, restul lotului continuă.
- Prima eroare reală se întoarce în rezultat (`first_error`) și se numără (`errors`); status nou
  **`partial`** când lotul a mers dar unii clienți au picat.
- UI (Clienți → „Sync acum") afișează `partial` cu numărul de clienți căzuți + prima eroare reală,
  în loc să aștepte până la timeout.
- Dacă se pierde conexiunea (nu doar instrucțiunea), sync-ul se oprește explicit, nu tăcut.

Verificat local pe feed-ul real: 16.492 clienți, 43.741 vehicule, 32.309 contracte, `errors: 0`.

## v3.0.0 - 2026-08-19 — RELEASE PE PRODUCȚIE

Consolidează toate livrările v2.1.0 → v2.18.0 (intrările individuale rămân mai jos, neatinse).

**Mail-uri CTS & analiză trafic**
- Tab nou **„Raport departamente"**: 3 indici (mutări per mail, cine mută, departamente intermediare),
  fiecare cu tabel top 10 + grafic și drill-down pe mailurile concrete (v2.18.0).
- Tabelă nouă `cts_department_moves` + trigger pe `cts_ground_truth` — istoricul mutărilor între
  departamente, cu backfill din `cts_department_prev` (v2.18.0).

**Satisfacție clienți**
- Motor V6 pe traiectorie IRIS: `iris_reasoning` condensat la rezumat acționabil 2-3 rânduri
  (v2.11.0, v2.13.1) + sinteză lunară AI (v2.13.2), cu fix pe `model_hint` lipsă (v2.16.1).
- Scos cardul „Clienți la risc real" din dashboard și din API (v2.18.0).

**Analiză apeluri**
- Prompturi de scoring versionate în repo (`app/services/prompts/calls/`), sync fără redeploy;
  `issueResolution` V2, `agentScore` V3 (dimensiunea `transparency`), `agentActions` V2 + 2 prompturi
  noi (`agentAdviceNextSteps`, `customerAdditionalRequests`) și KPI-uri noi în dashboard (v2.17.0).
- Apelurile din Productivitate vin din centrală, nu din CTS; leg-urile de centrală nu mai contează ca
  apeluri; apeluri pierdute numărate corect; fix decalaj de 3h la ora apelului (v2.12.0, v2.13.0).

**Productivitate**
- Tabel **Productivitate zilnică** per departament (colapsabil, o linie per zi, scor TOTAL),
  endpoint `GET /productivity/daily` (v2.15.0), mutat în cardul fiecărui departament — 0.29s în loc
  de 1.03s la deschidere (v2.15.1).
- Monitor operațional: contoare pe ziua curentă, identificarea rândurilor și contribuția ponderată
  (v2.9.0), reclamații „Deschise"/„În lucru" + total lunar (v2.13.0, v2.14.0), pictograma scrie
  obiectivul real, nu minimul (v2.14.0).
- Editare manuală a pontajului (v2.9.0).

**Acces & securitate**
- Roluri interne operator / admin / developer (v2.5.0), pre-atribuire pe email + atribuire în masă
  pe departament (v2.6.0), module interzise blocate pe server, nu doar ascunse în meniu (v2.6.1).
- Audit adversarial pre-producție: 11 defecte reparate (v2.4.0) + încă 2 găuri închise la
  re-verificare (v2.4.1).

**Documente & integrare CTS**
- Trasabilitate documente MailGuard în CTS — CCTS-5308 / CCTS-5071 (v2.2.0), corecții din audit +
  statistici pe pagina Procesare documente (v2.2.1), documentație `update_documents` (v2.2.2) și
  apariția lui în panoul Conexiune API (v2.2.3).
- Documente spre CTS: max 1,6 MB, PDF obligatoriu la vehicul și contract (v2.3.0).

**Operațional / infrastructură**
- Deploy prin `git pull` pe server (`deploy-pull.sh`) + integrare Git (v2.7.0).
- Export bază de date pentru dezvoltare locală, din Setări (v2.8.0).
- Cleanup automat storage zilnic la 00:00 (v2.1.0) + fix la raportarea `storage_cleanup.sh` (v2.3.1).

**UI general & fix-uri**
- Sortare pe coloane în tabelele mari, coloana Client/Device la Task-uri, apeluri in/out în lista de
  Clienți (v2.10.0).
- Fix: vehiculele și contractele clienților erau înghețate din 29.07 — cheia IRIS citită doar din
  environment (v2.10.0, v2.14.0).
- Fix: pagina Utilizatori rămânea albă pentru admini; operatorul vede cele două monitoare (v2.9.1).
- Fix: emailurile cu Ordin de Plată primeau P4/P5 în loc de P2 (v2.1.1).

## v2.18.0 - 2026-08-19

### MINOR — Tab „Raport departamente" în Mail-uri CTS

Raport nou peste traseul unui mail prin departamente, cu 3 unghiuri (conform modelului
`Model_tabele_grafice_CargoTrack.docx`), fiecare cu grafic + tabel și drill-down pe mailurile concrete:

1. **Distribuția mailurilor după numărul de mutări** (0 / 1 / 2 / 3+).
2. **Top departamente care fac mutările** — cine trimite mailul mai departe.
3. **Top departamente intermediare** — nici primele alocate, nici cele care închid mailul.
   Plus tabel „trasee frecvente" (din → în) și KPI: mailuri analizate, % mutate, total mutări, medie/mail.

Layout: **un indice pe rând**, fiecare card pe toată lățimea, împărțit în tabel compact (30%, top 10
departamente cu mutări ≠ 0) + grafic (70%): donut pentru distribuția mutărilor, bare orizontale pentru
topuri. Click pe orice linie din tabel → lista de mailuri din spatele cifrei.

**Istoric mutări (nou):** `cts_department_moves` — un rând per eveniment (alocare inițială + fiecare
schimbare de departament), scris de un trigger pe `cts_ground_truth`
(`migrations/20260819_cts_department_moves.sql`). Migrația face și backfill din
`cts_department_prev` / `changed_at` (singurul pas de istorie păstrat până acum).

**Limitare cunoscută (afișată în UI):** sync-ul CTS rulează la ~5 min, deci două mutări între două
sincronizări apar ca una singură, iar pe datele reconstituite lanțul are maxim un pas → statistica 3
(intermediari) rămâne aproape goală până se adună date live. Preluarea istoricului complet de alocări
din CTS rămâne task separat.

### Eliminat — cardul „Clienți la risc real" din Satisfacție clienți

Scos definitiv din UI (`SatisfactieDashboard`) și din backend: query-ul `at_risk` și câmpul omonim
din `GET /api/v1/clients/satisfaction-stats` nu se mai calculează (o interogare mai puțin per
încărcare de dashboard). Restul secțiunilor (top satisfăcuți, nesatisfăcuți <70%, segmente, semnale)
rămân neschimbate.

**Endpoints:** `GET /api/v1/cts-training/dept-report`, `GET /api/v1/cts-training/dept-report/cases`
(filtre: interval, departament, doar mailuri închise; drill: `min_moves`, `dept_from`, `dept_mid`).

## v2.17.0 - 2026-08-18

### MINOR — Scripturi analiză apeluri V2 (3 actualizate + 2 noi)

Prompturile de scoring apeluri devin **versionate în repo** (`app/services/prompts/calls/<key>.txt`),
sursă de adevăr pentru tabela `call_scoring_prompts`. Sincronizare fără redeploy de cod:
`python3 scripts/sync_call_prompts.py` (are și `--dry-run`).

**Actualizate:**
- `issueResolution` V2 — separă „cerere în afara competenței companiei" de „agent a eșuat".
  Câmpuri noi în output: `mainProblem`, `requestWithinCompanyScope`, `mainSolution`.
- `agentScore` V3 — a 6-a dimensiune `transparency`; reguli noi (handoff lingvistic și întreruperi
  tehnice nu se penalizează; lipsa prezentării la început = aceeași severitate ca lipsa salutului final).
  Scorul total agent = media pe 6 dimensiuni (era 5).
- `agentActions` V2 — câmpuri noi `nextStepsClearlyStatedToCustomer` + `nextStepsObservation`.

**Noi:**
- `agentAdviceNextSteps` — observație + sfat concret pe claritatea pașilor următori.
- `customerAdditionalRequests` — prinde cererile secundare ale clientului; marchează cele
  nerecepționate de agent (`unacknowledgedCount`).

**DB** (`migrations/20260818_call_scoring_v2_columns.sql`) — coloane noi pe `call_ai_scores`:
`agent_transparency`, `issue_main_problem`, `issue_main_solution`, `issue_within_company_scope`,
`agent_next_steps_clear`, `agent_next_steps_observation`, `agent_advice_next_steps`,
`customer_additional_requests`, `customer_unacknowledged_count`; + seed pentru cele 2 prompturi noi.

**UI Analiza apeluri:**
- **Întrebări AI**: cele 2 prompturi noi apar automat în listă (sync insert-only la deschiderea
  tab-ului — nu suprascrie textele editate din UI). Buton nou **„Sincronizează din repo"**
  (`POST /calls/analytics/scoring-prompts/sync-repo`) care rescrie textele din fișiere.
- **Dashboard**: KPI-uri noi *% Pași clari*, *Cereri ignorate*, *În afara competenței*; bară
  *Transparenta* în cardul „KPI medie agenti"; trei donut-uri derivate — *Cerere în competența
  companiei*, *Pași următori comunicați clar*, *Toate cererile clientului recepționate*.
- **Scoruri agenți**: coloane noi *Transparenta*, *% Pași clari*, *Cereri ignorate*.
- **Modal apel → tab „Analiza AI"** (nou): rezultatul complet al întrebărilor AI per apel —
  problema principală, soluția, verdicte (rezolvat / în competență / pași clari), barele de scor
  agent (6) și client (5), observația și sfatul pe pași următori, cererile suplimentare ale
  clientului cu status, frazele cheie și acțiunile promise. Buton „Analizeaza acum / Reanalizeaza".
  `GET /calls/{id}` întoarce acum și `ai_scores`.
- Analiza rulează automat pentru fiecare apel după transcriere când `calls.auto_score` e pornit
  (Setări → Prompturi AI); altfel manual, din „Scoruri Agenti" sau din modalul apelului.
- „Rescoreaza apeluri incomplete" prinde acum și rândurile fără câmpurile V2.

## v2.16.1 - 2026-08-18

### PATCH — Fix model_hint lipsă la sinteza lunară satisfacție

- **satisfaction_engine.py**: apelul AI de sinteză lunară ( pentru )
  nu primea , deci folosea modelul implicit al gateway-ului (Haiku) în loc de Sonnet.
  Fix: pasează același  din  ca la apelurile săptămânale (linia 907).

## v2.16.0 - 2026-08-18

Merge al celor două linii de lucru paralele de pe 18.08: productivitatea (monitor pe `calls`,
apeluri pierdute, reclamații total lună / în lucru, productivitate zilnică — v2.12.0…v2.15.1) și
satisfacția (`iris_reasoning` condensat + sinteza lunară AI — v2.13.1, v2.13.2). Ambele seturi de
modificări sunt în arbore; nu s-a rescris nimic din niciuna. Numerotarea entry-urilor de mai jos
rămâne cea din momentul scrierii, deci nu e strict crescătoare în timp.

## v2.15.1 - 2026-08-18

Tabelul „Productivitate zilnică" trece din card separat (unul mare, cu toate departamentele) în
**cardul fiecărui departament**, ca secțiune colapsabilă sub tabelul de operatori. Cifrele și
convențiile rămân identice; se schimbă doar locul și granularitatea cererii: fiecare card întreabă
numai de departamentul lui, deci deschiderea unui card costă 0.29s în loc de 1.03s.

## v2.15.0 - 2026-08-18

### Productivitate zilnică — tab Rapoarte

Secțiune nouă, **colapsată implicit, în cardul fiecărui departament**: un tabel cu o linie per zi, o
coloană per obiectiv și scorul TOTAL al zilei. Scorul lunar spune cât s-a atins, nu când s-a pierdut
— o lună la 94% poate ascunde o zi la 78%, iar aici se vede și obiectivul care a tras-o în jos.

Exemplu real, Suport 1 / august (date locale): luna iese 94.25%, dar 03.08 e la **78.6%**, cu
Emailuri la **61.06% pe 208 măsurabile** — apelurile și task-urile erau la 99-96% în aceeași zi.
Fără tabel, ziua asta era invizibilă.

Endpoint: `GET /productivity/daily?month=YYYY-MM[&department=]`. Fiecare card cere DOAR
departamentul lui, la PRIMA deschidere a secțiunii — nu la încărcarea paginii. Cost măsurat: 0.29s
per departament (1.03s dacă s-ar cere toate șase odată).

**Sursa e aceeași ca raportul lunar**, prin `breakdown_rows` — nu s-a scris SQL nou, altfel tabelul
zilnic și scorul lunar ar fi putut diverge în timp.

Convenții, scrise și în tooltip-ul „cum se calculează?":
- **ziua unui element = ziua REZOLVĂRII**, exact ca luna în raportul lunar (un mail sosit pe 8 și
  rezolvat pe 10 contează pe 10; la apeluri, sfârșitul convorbirii);
- **un obiectiv fără nimic măsurabil în ziua respectivă NU intră în media zilei.** În scorul lunar,
  un obiectiv gol contează 100% („nu poți rata ce n-a existat"); pe o singură zi regula asta ar da
  100% pe toate canalele fără activitate și ar ascunde exact ziua slabă pe care o cauți. Coloana
  arată `—`, iar `pondere_activa` spune câtă pondere a fost în joc;
- consecința, spusă explicit: **media zilelor nu e egală cu scorul lunar** (numitori diferiți).

Culorile urmează convenția gauge-ului (verde ≥ obiectiv real, galben ≥ minim, roșu sub), atât pe
celulele de obiectiv cât și pe TOTAL, deci ziua slabă și canalul vinovat sar în ochi. Zilele fără
activitate nu se afișează; weekendurile cu activitate rămân, cu fundal diferit. Panoul apare doar pe
o lună reală, nu pe interval agregat (nu există zile comune) și nu pe prognoză.

## v2.14.0 - 2026-08-18

### Fix: vehiculele și contractele nu se mai importau — cheia IRIS citită doar din environment

`sync_clients_from_iris()` lua cheia exclusiv cu `os.getenv('IRIS_MAILGUARD_API_KEY')`. Sub systemd
merge (`EnvironmentFile=/opt/iris-mailguard/.env`), dar orice rulare în afara lui — dev local, script
manual, cron fără env — vedea variabila goală și se oprea cu `IRIS_MAILGUARD_API_KEY missing`, fără
nicio eroare vizibilă în UI. Simptomul raportat: „numărul de vehicule nu îl importă, contractele nu
le importă". Cheia e acum și câmp de settings (`iris_mailguard_api_key`), deci se citește și din
`.env`; environment-ul rămâne prioritar.

Verificat că feed-ul IRIS livrează datele (probe read-only pe `/clients/contact-list?include=vehicles,contracts`):
16478 clienți, **10149 cu listă de vehicule** și **11387 cu contracte**. După fix, sync-ul rulat local
a scris **43699 vehicule / 32267 contracte** pe 12063 clienți — deci codul de import era corect, doar
nu ajungea să pornească.

Al doilea efect al aceleiași căi: pe return de eroare, `client_assets.last_result` rămânea
`{"status":"running"}` pentru totdeauna (starea se scria doar pe succes și pe excepție), deci UI-ul
arăta un sync în curs care se terminase de mult. `sync_clients_guarded` scrie acum rezultatul
oricare ar fi el.

### Monitor — reclamații: total pe lună + total în lucru

Barele devin **„Total lună"** (reclamații înregistrate în luna curentă) și **„În lucru"** (status CTS
2, indiferent de lună — e o stare, nu un flux). „Primite azi" / „Rezolvate azi" ies: pe o singură zi
cifrele sunt aproape mereu 0. „Deschise" (status 1) iese și el — în CTS reclamațiile nu stau în
starea `new`: pe eșantionul curent sunt **0 rânduri cu status 1** din 134, deci bara ar fi fost
permanent goală.

### Fix: monitorul punea reclamația pe alt departament decât pagina Reclamații

Monitorul atribuia reclamația **doar** prin `department_id`-ul din CTS (departamentul dominant al
angajaților mapați pe acel id), în timp ce pagina Reclamații folosește
`COALESCE(ev.department, dep.department)` — departamentul persoanei EVALUATE, cu `department_id` doar
ca fallback. De aceea o reclamație înregistrată în CTS pe „Suport 1", dar cu responsabil din
Comercial, apărea pe cardul Suport 1 și în listă la Comercial. Monitorul folosește acum exact
aceeași expresie, atât pe grup cât și per departament — comentariul din cod promitea deja asta, SQL-ul
nu o făcea.

Verificat pe august: totalurile per departament coincid acum cu interogarea de control
(taxe_drum 10, suport_1 8, suport_2 6, contabilitate 2, recuperare_tva 2).

### Pictograma de productivitate: se scrie obiectivul REAL, nu minimul

Pe arcul gauge-ului era scris pragul MINIM (`staticLabels.labels = [safeMin]`), deși cel urmărit e
obiectivul real — cel care trebuie atins. Se scrie acum realul. Ambele markere colorate rămân pe arc
(galben = minim, verde = real), la fel și valorile numerice de sub grafic; doar eticheta de pe arc nu
se poate dubla, fiindcă la o mărime lizibilă cele două se suprapun (Suport 1: 77.9 vs 82.9).
## v2.13.2 - 2026-08-18

### PATCH — Sinteză lunară AI pentru iris_reasoning

- **satisfaction_engine.py**: adăugat apel AI secundar după scorarea săptămânilor pentru .
  Generează un rezumat lunar de max 3 propoziții (stare + trend + risc principal) folosind reasoning-urile săptămânale.
  Fallback la textul programatic dacă apelul IRIS eșuează sau AI nu e configurat.

## v2.13.1 - 2026-08-18

### Prompt V6: iris_reasoning condensat la rezumat acționabil 2-3 rânduri

Câmpul `iris_reasoning` afișat în UI sub "Raționament complet AI" conținea anterior
justificarea tehnică a scorului (calcule, ponderi, săptămâni). Înlocuit cu un rezumat
orientat pe acțiune: starea curentă a clientului + trendul față de luna anterioară +
cel mai important risc sau oportunitate imediată.

## v2.13.0 - 2026-08-18

### Monitorul de productivitate: apelurile vin din centrală, nu din CTS

Cardurile per departament citeau apelurile din `cts_calls_ground_truth` (Apeluri CTS) — alt set de
date (doar apelurile ajunse tichet în CTS) și alt ciclu de viață (new → in progress → solved) decât
canalul „Apeluri" din raportul lunar. Monitorul și raportul spuneau cifre diferite pentru aceeași
zi. Toate interogările de apel din `/productivity/monitor/live` trec pe `calls` (While1), cu
aceleași definiții de leg ca raportul: contorul de grup, cardurile per departament, cele trei serii
orare și sesizările/reclamațiile venite pe telefon.

Ce nu se poate lua din centrală: **`in_curs`**. Un apel în desfășurare nu e încă în CDR — rândul
apare abia după ce s-a încheiat. Cheia rămâne în răspuns pentru compatibilitate, dar e mereu 0;
înainte număra tichete CTS neînchise, ceea ce pe un monitor „AZI" era restanță, nu apeluri în curs.

Categoria pentru „sesizări venite pe telefon" devine `calls.ai_category` (încadrarea AI a
transcriptului) în loc de `cts_calls_ground_truth.cts_category` (încadrarea omului în CTS): a doua
e mai bună ca adevăr, dar apare abia după ce apelul devine tichet, deci pe monitorul zilei curente
arăta sistematic mai puțin decât lista de apeluri.

### Apeluri pierdute — numărate corect, nu ca „rânduri NO ANSWER"

Cardul de apeluri arată **Răspunse** (conversații reale), iar apelurile **pierdute** apar o singură
dată, ca cifră de firmă, în capul monitorului. Nu se pot împărți pe departamente: centrala nu scrie
agent pe un apel nepreluat — din 666 apeluri pierdute în august, doar 72 (11%) au `agent_extension`;
restul au doar linia apelată (`callee_number`), iar linia e a firmei. Pe 10.08: 84 pierdute real,
din care doar 25 atribuibile — o bară per departament ar fi arătat 30% din realitate. API-ul întoarce
ambele: `pierdute_azi` (subsetul atribuit grupului) și `pierdute_azi_total` (firma).

Un apel pierdut NU e orice rând `NO ANSWER`: centrala sună în paralel pe mai multe aparate, deci legs-urile nepreluate apar
`NO ANSWER` chiar și când apelul a fost preluat pe alt aparat. Pe august 2026 sunt **2215 rânduri
NO ANSWER/BUSY, dar doar 659 apeluri efectiv pierdute** — definiția folosită
(`_APEL_LOST_CALL_SQL`): un leg nerăspuns de la un număr care nu are nici un apel răspuns în
±15 minute (fereastra acoperă și reapelarea imediată a clientului).

Verificat pe 10.08: 277 apeluri răspunse și 22 pierdute pe grupul Operațional, distribuite pe orele
8–20 locale. Seria orară „încă deschise" a canalului apel devine numărul de apeluri pierdute la ora
respectivă — un apel răspuns e încheiat, deci altfel bara ar fi fost mereu 0. „Intrate azi" (numitorul
indicatorului Ritm) include acum și pierdutele: un apel pierdut a sosit, chiar dacă n-a fost tratat.

Ziua se citește cu `(now() AT TIME ZONE 'Europe/Bucharest')::date`, nu `CURRENT_DATE`: pe un
Postgres cu sesiunea pe UTC, între 00:00 și 03:00 RO monitorul ar arăta încă ziua precedentă.

### Reclamații pe monitor: doar „Deschise" și „În lucru"

Cerere business owner. Barele „Primite azi" / „Rezolvate azi" / „Deschise" devin **„Deschise"**
(status CTS 1 = înregistrată, nepreluată) și **„În lucru"** (status 2). Pe un monitor de perete
contează ce e nerezolvat acum; „primite azi" și „rezolvate azi" sunt seturi diferite, nu un flux —
o reclamație primită ieri și rezolvată azi apărea doar în a doua bară.

Cheia veche `deschise` din API rămâne cu sensul ei (toate cele nerezolvate, adică suma celor două),
fiindcă o folosește blocul `sesizari`; s-au adăugat aditiv `noi` și `in_lucru`, atât pe grup cât și
per departament.

## v2.12.0 - 2026-08-18

### Apeluri în Productivitate: legs-urile de centrală nu mai sînt apeluri

Canalul „Apeluri" din Productivitate ia datele din `calls` (While1) — aceeași sursă ca pagina
Apeluri — dar număra fiecare **leg** de centrală ca apel separat. Centrala scrie cîte un rînd per
leg: ring paralel pe mai multe aparate, transfer, reapelare imediată. Pe august 2026, din 4354
rînduri `inbound`: **2199 `NO ANSWER` de 0-1s, 16 `BUSY`, 12 `ANSWERED` de 0-1s — 2227 (51%) care
nu sînt conversații**.

Caz real semnalat (10.08, +37369841796, același agent):
```
12:02:38  ANSWERED   1s   ← leg, apărea ca apel „procesat"
12:03:36  ANSWERED 213s   ← apelul real
12:03:54  NO ANSWER  0s   ← leg, apărea „neprocesat"
```

Se numără acum doar conversațiile reale (`_APEL_REAL_CALL_SQL`: `ANSWERED` și durată > 1s), în
toate cele trei locuri care citeau `calls`: raportul lunar (`_fetch_apel_rows`), lista din modal
(`breakdown_rows`) și analytics/monitorul de departament. Efect pe august: volumul Suport 1 scade
3159 → 1777, Suport 2 325 → 177.

Nu s-a adăugat deduplicare pe `linkedid`: verificat pe august, după filtru rămîn **0 legs
suprapuse din 2127** și doar 14 perechi la sub 120s într-o lună întreagă — reapelări reale.

**De ce atingea și scorul, nu doar volumul:** `backfill_ring_seconds` completează `ring_seconds` pe
tot intervalul, nu doar pe apelurile răspunse, deci un leg mort devine „măsurabil" cu ~0s — un
„on time" gratuit. Local (unde `ring_seconds` e încă NULL) procentul nu se mișcă, dar măsurabilele
Suport 1 scad 1184 → 1024, deci pe staging, unde backfill-ul a rulat, procentul era umflat.

### Fix: ora apelurilor era cu 3h în față în Productivitate

`calls.started_at` e `timestamp WITHOUT time zone` și conține ora **locală RO**, exact cum o scrie
centrala (verificat: `call_id` 1346015 e 12:02:38 în centrală și 12:02:38 în DB). Productivitatea îl
trata ca UTC: `AT TIME ZONE 'Europe/Bucharest'` în SQL și `_iso()` la afișare, deci același apel
apărea la 12:02 pe pagina Apeluri și la 15:02 în lista din Productivitate. În plus, pe un Postgres
cu sesiunea pe UTC, `(started_at AT TIME ZONE 'Europe/Bucharest')::date` mută ziua apelurilor din
primele ore ale dimineții. Filtrele folosesc acum direct `c.started_at`, ca pagina Apeluri.

### Apeluri: „fără conversație" în loc de „neprocesat"

Un leg fără înregistrare (`audio_status='no_recording'`) nu poate fi clasificat niciodată, deci
coloana Categorie afișa „neprocesat" la 2222 de rînduri pe august — părea o coadă blocată. Nu e:
niciun worker nu selectează `queue_status='queued_ingest'` pe apeluri (audio-ul merge după
`audio_status='pending'`), deci rîndurile nu erau în așteptare, ci terminale. Eticheta devine
„fără conversație", cu detaliul stării din centrală în tooltip.

## v2.11.0 - 2026-08-18

### Prompt V6: iris_reasoning condensat la rezumat acționabil 2-3 rânduri

Câmpul `iris_reasoning` afișat în UI sub "Raționament complet AI" conținea anterior
justificarea tehnică a scorului (calcule, ponderi, săptămâni). Înlocuit cu un rezumat
orientat pe acțiune: starea curentă a clientului + trendul față de luna anterioară +
cel mai important risc sau oportunitate imediată.

## v2.10.0 - 2026-08-14

### Sortare pe coloane în tabelele mari

Toate cele șapte liste paginate — Emailuri, Mail-uri CTS, Task-uri, Device Operations,
Apeluri, Apeluri CTS, Reclamații — au acum antet de dată sortabil ASC/DESC. Task-uri și
Device Operations sortează pe **orice** coloană (ID, client, tip, departament, asignat,
status, durată, echipament), nu doar pe dată.

Sortarea se face **pe server**, nu în UI: tabelele sunt paginate cu LIMIT/OFFSET, deci o
inversare locală ar reordona doar cele 50 de rânduri afișate — „cel mai vechi" ar însemna
de fapt „cel mai vechi din pagina 1". Direcția și coloana nu pot fi legate ca parametri
(`ORDER BY` nu acceptă bind params), deci trec prin whitelist-ul din `api/v1/sorting.py`;
o valoare necunoscută cade pe ordinea implicită, nu pe eroare și nu pe SQL brut. Fără
parametru, fiecare listă păstrează ordinea de dinainte, byte-for-byte.

### Task-uri: coloana Client/Device

Task-urile fără client sunt legate de un echipament. Coloana „Client" devine
„Client/Device" și afișează numărul de device când clientul lipsește — extras din
titlu/descriere cu același `_extract_device` folosit de Productivitate, ca cele două
pagini să nu arate identificatori diferiți pentru același task (CTS nu trimite câmp de
device în `/cts/tasks`). Acoperire pe eșantionul curent: 82 din 110 task-uri fără client.
În tabelul din Productivitate antetul devine la fel „Client/Device", iar rândurile fără
client spun „pe echipament" în loc de „—", cu numărul în coloana Device alăturată.

### Clienți: apeluri in/out în listă

Lista de clienți arăta doar mailurile primite/trimise. S-a adăugat o coloană „Apeluri
(↓ primite · ↑ date)" — un client care sună mult și scrie puțin apărea inactiv.

### Fix: vehiculele și contractele clienților erau înghețate din 29.07.2026

Datele erau în DB (43k vehicule, 32k contracte) și endpoint-urile răspundeau corect —
doar că **nimic nu rula sync-ul periodic**. `POST /api/v1/clients/sync-now` se declanșa
exclusiv la apăsarea butonului din UI: nu există cron, timer sau task în aplicație care
să-l cheme. Cheia `client_sync_interval_minutes = 5` din `settings` nu e citită de nimeni.
Sync-ul rulează acum periodic **din aplicație** (`_client_sync_loop`, pornit de `lifespan`),
la 60 min — nu are nevoie de cron sau timer systemd, deci intră în funcțiune la primul
restart al serviciului.

API-ul rulează cu 4 workeri gunicorn, deci fiecare proces are propria buclă. `_CLIENT_SYNC_LOCK`
e un lock de threading și nu poate coordona procese separate, așa că scadența se ia atomic din
DB (`claim_client_sync()`, cheia `client_assets.next_sync_at`): un `INSERT ... ON CONFLICT DO
UPDATE ... WHERE scadent` întoarce rând exact unui singur worker. Un „citește, compară, scrie"
ar fi lăsat toți patru să pornească simultan același pull de 16k clienți. Scadența persistă în
DB, deci un deploy nu repornește numărătoarea de la zero.

Intervalul se schimbă din `settings.client_sync_interval_minutes` fără redeploy (podea 5 min,
fiindcă un pull durează 60-90s). O eroare de rețea într-un ciclu nu oprește bucla.

## v2.9.1 - 2026-08-14

### Operatorul vede și cele două monitoare de productivitate

`OPERATOR_SUBTABS` pentru `productivity` trece de la `("rapoarte",)` la
`("rapoarte", "monitor-op", "monitor-fin")`. Cele două sub-taburi de monitor sunt doar
lansatoare pentru paginile de perete `/productivity/dashboard/{group}`, care sunt oricum
publice (fără auth, pentru monitoarele de birou) — deci nu expun nimic peste ce vede
oricine deschide URL-ul. Rămân închise pentru operator: Analiză, Obiective & Ponderi,
Notificări (păzite pe backend de `require_prod_full` = admin/developer).

### Fix: pagina Utilizatori rămânea albă pentru admini

**Cauza 1 — rutele de angajați erau păzite de modulul greșit.** Endpoint-urile
`/settings/employees*` alimentează pagina Utilizatori, dar stăteau în `settings.py`,
router montat cu `require_module("settings")` — modul rezervat developerilor. Un admin
primea 403 pe propria pagină. Mutate în `app/api/v1/employees.py`, router propriu montat
cu `require_module("utilizatori")`: admin și developer au acces, operatorul nu. Căile au
rămas neschimbate, deci UI-ul nu s-a modificat.

**Cauza 2 — `_parseResp` returna erorile JSON ca date valide.** Orice răspuns cu
`content-type: application/json` era întors direct din `api()`, indiferent de status. La
403, apelantul făcea `setEmployees({detail: {...}})`, iar la următorul render
`employees.filter(...)` arunca `TypeError` → React demonta tot arborele → ecran alb. Acum
răspunsurile non-OK aruncă o eroare cu mesajul din `detail` (pentru `forbidden_module`,
„Nu ai acces la acest modul"), deci componentele își afișează starea de eroare în loc să
cadă. Fix global: orice pagină care primea un 4xx/5xx cu body JSON se putea rupe la fel.

## v2.9.0 - 2026-08-13

### Productivitate: identificarea rândurilor și contribuția ponderată

**Număr de device pe task-uri și pe operațiuni.** În lista din spatele unui obiectiv
(clic pe obiectiv → modal) există o coloană „Device". La operațiuni pe echipamente vine
direct din CTS (`device_serial`, ID-ul CTS al aparatului pe hover). La task-uri **CTS nu
trimite niciun câmp de device** — feed-ul `/cts/tasks` conține doar `task_name`,
`description`, `category_name`, `client_id`, `assignee_*` (verificat pe endpoint-ul live) —
așa că numărul se extrage din titlu și descriere: număr de înmatriculare RO (`B39GIN`,
`SM11AGM`), cod device (`DGD022`) sau IMEI. Acoperire pe august: 4471 din 6460 task-uri
Taxe de drum. Unde textul nu conține niciun identificator (ex. „HU-GO: suspended device
went into HU", a cărui descriere e doar poziția GPS) rămâne „—", iar descrierea completă
apare la hover, ca task-ul să poată fi totuși identificat. Căutarea din modal caută și
după device.

**Client reparat la operațiuni pe echipamente.** Coloana era goală pe toate rândurile:
`device_operations.client_id` e NULL peste tot (view-ul IRIS DV nu-l trimite), deci join-ul
pe `clients` nu returna niciodată nimic. Se folosește `client_name`, care vine populat din
DV, cu join-ul păstrat ca prioritate pentru când câmpul se va popula.

**Coloană nouă „Contribuție%" în tabelul de operatori.** Cât din productivitatea echipei
ține de fiecare om, ponderat cu obiectivele departamentului:

    contributie = Σ_obiectiv ( pondere × volum_op / volum_dept ) / Σ_obiectiv pondere

Spre deosebire de coloanele „Cotiz.%", care numără bucăți și tratează un mail la fel ca o
operațiune pe echipament. Pe Suport 2 / august răstoarnă clasamentul: Miclau Adrian-David
are 6.15% din volumul de mailuri, dar 35.12% din productivitatea ponderată a echipei
(face 217 din 357 de instalări noi). Suma pe echipă dă 100% minus partea rezolvată de
oameni care nu mai sunt operatori activi ai departamentului în perioada afișată. Apare și
în exportul PDF.

**Fix consistență multi-lună:** pe 3/6/12 luni `cotiz_task` împărțea la task-uri +
operațiuni, iar pe o lună doar la task-uri — aceeași persoană avea două procente diferite
pentru aceeași coloană. Acum ambele împart la volumul de task-uri; operațiunile au propria
coloană.

### Monitor operațional: contoare pe ziua curentă

**„În lucru" și „Noi" numără doar ce a sosit azi.** Înainte se număra tot ce CTS nu a
marcat vreodată `solved`, fără limită de vechime — pe Suport 1, 26 de mailuri „noi", din
care doar 3 sosite în ultimele 7 zile; restul e restanță istorică pe care nu o mai
lucrează nimeni (notificări automate, tichete abandonate). Un monitor de perete trebuie să
arate starea zilei, nu arhiva. Aceeași regulă la task-uri (`in progress` / `new` /
`postponed` create azi). Fiecare secțiune afectată e marcată „AZI" pe card; rezumatul de
jos devine „Deschis din azi". `noi_vechi` / `pending_vechi` au fost eliminate — în
interiorul unei singure zile sunt mereu 0.

Secțiunea „Reclamații" **nu** primește marcajul „AZI" și rămâne neschimbată: „Deschise"
acolo e în continuare pe tot istoricul. Sursa ei e categoria emailului (CTS
`category_id=1`), nu modulul Quality evaluation din CTS — tabela `quality_evaluation`
există în replica CTS, dar IRIS Gateway nu o expune (fără endpoint și fără view DV), deci
alinierea cu CTS rămâne blocată până la un endpoint nou.

**Rândul de contoare de sus a fost eliminat** (Rezolvate azi / În lucru acum / Sesizări și
reclamații deschise / Sesizări rezolvate azi). Erau agregate pe tot grupul, dublau cifrele
din cardurile per departament și ocupau prima bandă a ecranului.

### Editare manuală a pontajului (documentare retroactivă a 53dda86)

Clic pe orice celulă din Utilizatori → Pontaj pe departamente deschide un modal de
corectare: prezent/absent, preset Schimb 1 (08:00–16:30) / Schimb 2 (12:00–20:30) sau ore
libere. Rândul corectat primește `manual_override=true` și **sync-ul CTS nu îl mai
suprascrie** (skip la upsert + `WHERE manual_override IS NOT TRUE` ca plasă de siguranță în
SQL). Buton „Revino la CTS" pentru anulare. Motivul: `/cts/timesheets` trimite uneori
schimbul greșit (10–14 august 2026, Breahna Andrei și Cuc Mihai apar pe schimb 1 deși sunt
pe 2). Corecția nu e cosmetică — `employee_attendance` e sursa ferestrei orare pentru SLA,
deci mută și calculul de productivitate al zilei. Migrare:
`migrations/20260813_employee_attendance_manual.sql`.

## v2.8.0 - 2026-08-13

### Export bază de date pentru dezvoltare locală (zona Setări)

Buton nou în Setări → „Export bază", care generează o copie completă a bazei
(`pg_dump -Fc`, ~196 MB, 108 tabele) descărcabilă pentru lucru local. Se
importă cu `pg_restore -U mailguard -d mailguard -c fisier.dump`.

**Acces: doar rolul `developer`.** Dublă pază — la nivel de router (`main.py`)
și pe fiecare endpoint (`require_role(ROLE_DEVELOPER)`). Un admin (Bianca,
Robert, Vlad, Calin) primește 403. Motiv: dump-ul conține date reale de client
(emailuri, clienți, angajați, apeluri) — nu e o funcție de comoditate.

**Asincron:** exportul rulează în fundal (un thread), interfața interoghează
starea din 3 în 3 secunde. La ~196 MB, un request sincron ar depăși timeout-ul
gunicorn (60s) și ar bloca un worker.

**Audit:** fiecare pornire (`db_export_start`) și fiecare descărcare
(`db_export_download`) se scriu în `audit_log` cu actor, IP și mărime.
Descărcarea se auditează separat de pornire — un export poate fi descărcat de
mai multe ori.

**Endpoints:** `POST /db-export/start`, `GET /db-export/status`,
`GET /db-export/list`, `GET /db-export/download/{filename}`. Retenție: ultimele
3 arhive (fiecare ~196 MB). Scriere în `.part` + rename la final — o descărcare
concurentă nu poate prinde un fișier incomplet.

**Fișierul se salvează în** `storage/db-exports/`, exclus din Git (`.gitignore`
acoperă `*.dump` și `storage/`).

**Respins deliberat: buton de download `.env`.** Un secret descărcabil printr-un
clic încetează să fie secret — orice sesiune uitată deschisă sau bug de
autorizare ar expune `IRIS_SSO_SECRET` (cu care se emit token-uri pentru orice
utilizator), `MS_CLIENT_SECRET` și `PERSONAL_MAILBOX_KEY`, ocolind exact
restricția de roluri construită în v2.5.0–v2.6.1. Alternativă: `.env.example`
în repo + valorile reale prin `scp`/Passbolt.

## v2.7.0 - 2026-08-12

### Integrare Git + deploy prin pull

Codul aplicației trăiește acum în `git@github.com:Raul-Covaci/Mailguard_staging.git`
(privat). Deploy-ul nu se mai face prin rsync din workspace-ul IRIS, ci prin
`git pull` pe server — se lucrează local, se împinge, serverul trage.

**Repo:** 315 fișiere, ~7 MB (din 1,2 GB pe disc).

**Excluse deliberat** (`.gitignore`, cu motivul lângă fiecare regulă):
`.env` + `app/.env` + deploy key (secrete) · `data/doc_templates/` 114 MB
(41 documente reale de client) · `logs/` 684 MB · `venv/` 238 MB ·
`backups/` + `storage/` (dump-uri, symlink spre `/home/mail-data`) ·
~90 fișiere `*.bak*` 65 MB · fișierele specifice mediului IRIS
(`deploy.sh`, `CLAUDE.md`, `OUTBOX_*`, `.iris_*`).

`app/ui/vendor/mg-app.js.gz` **rămâne** în repo — nu e artefact de build,
aplicația îl servește direct din `_GZIP_FILES` (`app/main.py`).

**Autentificare:** deploy key ed25519 în `/opt/iris-mailguard/deploy/`, cu
`core.sshCommand` și `IdentitiesOnly=yes`. Partea privată nu părăsește serverul.

**`deploy-pull.sh`** (nou): oprire dacă arborele are modificări necommitate
(`--force` trece peste) → backup DB `pg_dump -Fc` din container, retenție 10 →
`pull --ff-only`, fără `reset --hard` automat la istoric divergent → `pip` doar
dacă `requirements.txt` s-a schimbat între revizii → regenerare `mg-app.js.gz`
dacă sursa e mai nouă → restart (migrările rulează fail-fast prin
`ExecStartPre`) → health check pe `/healthz`, cu comanda de rollback și calea
backup-ului afișate la eșec.

**`DEPLOY.md`** (nou): fluxul zilnic, tabel cu ce se propagă prin push și ce nu,
bootstrap pe server nou (inclusiv drop-in-ul `10-migrate.conf`, care nu e în
repo și fără care migrările nu se aplică), depanare.

**`.env.example`** (nou): 52 de variabile documentate, zero valori.

**Auditul de secrete:** curat. Toate cheile se citesc din environment
(`os.getenv` / pydantic `BaseSettings`); zero literale în cod.

**Două capcane prinse la verificare:**
- `git add` a eșuat cu `dubious ownership`, iar căutarea de fișiere sensibile a
  raportat „curat" — indexul era gol, nu curat. De aceea verificarea cere și
  numărul de fișiere.
- `mg-app.js.bak20260722_v3` a trecut prin filtre: `.bak` lipit direct de dată,
  fără separator. Regula acoperea `.bak-`, `.bak_`, `.bak.`, dar nu varianta
  lipită. Corectat cu `*.bak*`.

## v2.6.1 - 2026-08-12

### Închiderea modulelor interzise la nivel de server (nu doar în meniu)

**Problema găsită la testare:** meniul lateral era filtrat corect pe rol, dar
routerele care nu fuseseră cerute explicit în v2.5.0 rămăseseră nepăzite. Un
operator care cerea direct adresa primea date din module ascunse: Apeluri,
Clienți, Device Ops, Căsuțe personale, Utilizatori, Emailuri automate,
Satisfacție. Ascunderea în interfață era cosmetică pentru acestea.

**Paze adăugate** (`main.py`): `reports`, `documents`, `cts_training`,
`personal_mailboxes`, `calls`, `cts_calls_training`, `calls_analytics`,
`calls_analyze`, `cts_tasks_training`, `device_ops`, `department_schedule`,
`feedback_config`, `feedback_campaigns`, `feedback_frequency`,
`feedback_dashboard`. `admin_reset` → doar developer.

**Filtrare pe cale** — funcție nouă `require_module_for_paths()`, pentru routere
mixte unde paza pe tot routerul ar rupe funcționalitate legitimă:
- `health`: `/health` rămâne public (monitorizare, load-balancer), `/stats/*`
  cere acces la Dashboard.
- Prompturi AI: administrarea (regenerare, corecții globale, statistici, rapoarte
  de cost, dispatch) cere acces; acțiunile per-email (`/ai/<x>/{id}/run|correct`,
  `/ai/assignee/employees`) rămân la îndemâna operatorului — se folosesc din fișa
  unui email, pe care operatorul are dreptul să o deschidă.

**Module de sprijin** (`SUPPORT_MODULES_BY_ROLE`): separare între ce se vede în
meniu (`allowed_modules`) și ce se poate cere serverului (`api_allowed_modules`).
Operatorul primește `clients`, `spam`, `ai` ca sprijin — fișa de client deschisă
din Emailuri are nevoie de ele, deși tabul Clienți îi este ascuns.

**Neatins intenționat:** `cts.router` (token propriu X-CTS-Token, nu JWT — paza
JWT ar rupe integrarea CTS), `feedback_public` (formular public pentru clienți),
`client_satisfaction_feed` (cheie API proprie), `productivity/dashboard/{group}`
(monitor pe TV, public dinainte).

**Migrație** `20260812e_access_roles_contabilitate.sql`: cei 6 din contabilitate
atribuiți din interfață după migrația `d`. Fără acest fișier ar fi existat pe
staging dar nu pe producție — `scripts/migrate.sh` sare peste fișierele deja
înregistrate în `_release_migrations`.

**Verificat pe staging** cu un cont real de operator: 12 module blocate (403),
7 permise (200); admin neafectat (200 peste tot, 403 doar pe zona Setări).

## v2.6.0 - 2026-08-12

### Pre-atribuire roluri pe email + atribuire în masă pe departament

Problema rezolvată: în v2.5.0 rolul se putea seta doar pe conturi existente, dar
conturile apar abia la prima logare prin IRIS SSO. Din cei 56 de angajați cu email,
doar 4 aveau cont — restul nu puteau primi un rol în avans.

**DB:** tabel nou `access_role_assignments` (cheie = email, rol, cine a atribuit, notă).
Rolul efectiv = rolul contului dacă omul s-a logat, altfel pre-atribuirea.

**Seed inițial (decis de Raul Covaci):**
- `suport_1` → operator, exceptând Bianca Judea (admin)
- `suport_2` → operator, exceptând Robert Kovacs (admin)
- `taxe_drum` → operator, exceptând Vlad Pusta (admin)
- Calin Lucaciu (management_operational) → admin
- Razvan Perticas, Raul Covaci → developer

**Backend:**
- `GET /access/users` — acum listează TOȚI angajații din `employee_department_mapping`
  (nu doar conturile), cu departament, rol efectiv și dacă are cont. Întoarce și
  lista de departamente.
- `PUT /access/by-email` — setează rolul pe email; funcționează și fără cont.
- `POST /access/bulk` — atribuie un rol întregului departament, cu listă de excepții.
  Un eșec pe o persoană (ex. developer protejat) nu anulează tot lotul.
- SSO provisioning citește din `access_role_assignments`; fără intrare → `operator`.
- Eliminat `SSO_ROLE_SEED` hardcodat — o singură sursă de adevăr (tabelul).

**Frontend (Utilizatori → Roluri acces):**
- Listă cu toți angajații, filtru pe departament, căutare după nume/email.
- Coloană „Cont": *Activ* sau *La prima logare*.
- Atribuire în masă pe departament, cu confirmare care arată explicit cine rămâne
  neschimbat (ex. „Rămân neschimbate: Judea Bianca-Denisa (Admin)").
- Contoare per rol în legendă.

**Neschimbat:** un admin tot nu poate atribui rolul `developer`, nu poate modifica
un cont de developer și nu își poate schimba propriul rol.

## v2.5.0 - 2026-08-12

### Roluri interne de acces (operator / admin / developer)

Control de acces pe module, independent de CTS/Cargo360. Rolul se setează manual
din **Utilizatori → Roluri acces**, nu se deduce din `seniority`.

**Matrice:**
- `operator` — Emailuri + Productivitate (DOAR sub-tab "Rapoarte")
- `admin` — tot, mai puțin zona de Setări (Setări / Prompturi AI / Surse date)
- `developer` — acces complet

**DB:**
- `admin_users.access_role varchar(20) NOT NULL DEFAULT 'operator'` + CHECK constraint
- Backfill: Razvan + Raul → `developer`; Bianca → `admin`; restul → `operator`

**Backend:**
- Nou: `app/services/access_control.py` — matricea rol→module, `require_module()`,
  `require_role()`, `can_assign_role()`
- `/auth/me` returnează `access_role`, `allowed_modules`, `allowed_subtabs`,
  `landing_module`, `can_manage_roles`. Rolul e citit din DB la fiecare request,
  deci schimbarea are efect imediat, fără re-login.
- Nou: `GET /access/users`, `PUT /access/users/{id}/role` (audit în `audit_log`)
- Gate pe routere: `settings`, `ai_category/department/priority/assignee/autoreply`
  (prompturi-ai), `cts_sync_control` (surse-date)
- Gate per-endpoint în `productivity.py`: `objectives PUT`, `analytics`,
  `notifications*` cer admin/developer. `report`, `forecast`, `trend`,
  `breakdown`, `recalculate` rămân accesibile operatorului (sub-tab Rapoarte).
- SSO provisioning: utilizatorii noi intră ca `operator` (era `admin`) —
  deny-by-default. Seed explicit pentru Razvan/Raul/Bianca/Robert Kovacs/Calin.

**Securitate:**
- Un `admin` NU poate atribui rolul `developer` și nu poate modifica un cont
  de developer — altfel s-ar putea auto-promova în zona de Setări.
- Nimeni nu își poate schimba propriul rol.
- Ascunderea tab-urilor în UI e cosmetică; gate-ul real e pe endpoint-uri.

**Frontend:**
- Filtrare `TABS` după `allowed_modules`; etichetele de secțiune se mută pe
  primul tab vizibil din grup.
- Redirect automat spre prima pagină cu acces la tab interzis (hash manual
  sau rol schimbat între timp).
- Sub-tab-urile Productivitate filtrate pe rol; fallback pe "Rapoarte".
- Panou nou `RoluriAccesPanel` în Utilizatori, vizibil doar pentru admin/developer.

**Neatins intenționat:** `GET /productivity/dashboard/{group}` rămâne public
(monitor pe TV, fără login) — era public înainte, nu a fost în scopul cererii.

## v2.4.1 - 2026-08-12

### Verificare suplimentară după audit: două găuri rămase

Reparațiile din v2.4.0 nu trecuseră prin niciun revizor, așa că le-am măsurat separat.

**Lot fără limită → pierdere totală la volume mari.** `update_documents` accepta oricâte documente.
Măsurat pe staging: 300 = 0,4 s (bine), 20.000 = 21 s, **50.000 = 52 s** — la limita de 60 s a
serverului. Peste prag workerul e omorât și CTS pierde tot lotul, inclusiv confirmările valide,
exact tiparul reparat la conversia PDF. Plafon nou `CTS_MAX_BATCH = 5000`, cu HTTP 413 și mesaj care
spune ce trebuie făcut. Verificat: 6.000 de intrări → refuz în 0,07 s în loc de blocaj.

**Documentele fără categorie scăpau de regula PDF.** Formatul obligatoriu se decidea după categorie
(`vehicul`/`contract`), dar clasificarea poate eșua: măsurat pe staging, 3 din 39 de documente
validate aveau categoria goală — printre care **două taloane**, clar documente de vehicul. Toate
pleacă totuși spre CTS, deci exact cele neclasificate ar fi ajuns ca imagine acolo unde CTS așteaptă
PDF. Regula e acum inversă: PDF obligatoriu implicit, `sofer` singura excepție. Verificat pe toate
variantele de categorie (inclusiv `None`, `""`, `necunoscut`) și pe 10 documente reale livrate.

## v2.4.0 - 2026-08-12

### Audit adversarial pre-producție: 11 defecte reparate

Trei revizori independenți pe backend, conversie/curățenie și interfață/documentație. Toate
problemele de mai jos au fost reproduse pe staging înainte de reparare și retestate după.

**Pierdere tăcută a trasabilității unui email întreg.** `_track_sent_document` prindea excepțiile ca
„non-fatale" și continua bucla — dar în Postgres prima eroare ABORTEAZĂ tranzacția: toate scrierile
următoare eșuau, iar `db.commit()` de la final raporta SUCCES fără să scrie nimic. Reprodus:
`commit: OK` urmat de zero rânduri persistate. Un email cu 6 documente pleca spre CTS fără nicio
urmă în statistică, iar confirmările ulterioare cădeau pe `unknown`. Fix: `db.begin_nested()`
(SAVEPOINT) per document — verificat că 3 din 4 documente se salvează când al doilea eșuează intenționat.

**Documente marcate salvate fără să fi fost trimise — și blocate definitiv.** Confirmarea CTS se
aplica pe `attachment_id`, care e comun tuturor actelor dintr-un fișier. Un act încă în validare
(`extracted`, `sent_to_cts_at IS NULL`) devenea `saved`, iar garda din `_track_sent_document` îl
bloca acolo pentru totdeauna — nu mai era livrat niciodată. Pierdere reală de documente, nu doar de
statistică. Fix: `AND sent_to_cts_at IS NOT NULL AND cts_status IN ('sent','failed','saved')`.

**Confirmare per document.** `update_documents` acceptă acum `part_no` sau `extraction_id` (ambele
livrate în feed) pentru a ținti UN act dintr-un fișier cu mai multe. Măsurat pe date reale: din 226
de atașamente, 47 conțineau mai multe documente, unele cu categorii diferite (șofer + vehicul în
același PDF) — acolo o confirmare globală înregistra asocieri care nu s-au întâmplat.

**Contoare care minteau.** `marked_*` numărau RÂNDURI, deci o confirmare pe un fișier cu 3 acte
raporta `3`. În `cts_api_log` ajungea `success=3, total=1` — orice raport construit peste ar fi dat
peste 100%. Separat acum: `marked_*` = confirmări, `rows_*` = rânduri atinse. Plus `already_final`
(document cunoscut, deja șters) separat de `unknown` (inexistent), și `partially_deleted`.

**Aplicația putea deveni indisponibilă.** Re-randarea PDF-urilor scanate rulează sincron în
endpoint-ul de polling, iar gunicorn are `--timeout 60` cu 4 workeri. Măsurat pe staging: 10 pagini
= 12 s, 30 = 35 s, **60 = 72 s**. Un contract scanat de 60 de pagini omora workerul; CTS reîncerca,
declanșând aceleași 72 s, până la blocarea tuturor celor 4. Plafon nou: `_RENDER_MAX_PAGES = 40`.

**Rasterizarea distrugea textul PDF-urilor native.** Se aplica fără să verifice dacă documentul e
scanat: un contract PDF nativ ajungea la CTS ca teanc de poze, ireversibil (`get_text()` → `''`).
Fix: prag `_NATIVE_TEXT_MIN` — PDF cu text nativ nu se rasterizează, se livrează peste limită.

**Fișiere goale livrate ca succes.** La conversie eșuată se trimitea originalul; cu bytes goi
rezulta `contentBytes: ""` marcat `sent`. Fix: `_normalize_for_cts` întoarce `None` (document
raportat lipsă, recuperabil) în loc de un fals succes permanent în statistică.

**`storage_cleanup.sh` putea muri la primul pas.** Același tipar `"0\n0"` mai exista la `du -sb`:
`du` poate tipări o valoare ȘI ieși cu cod != 0, iar cu `set -e` scriptul abandona la secțiunea 1 —
pașii 2 și 3 nu mai rulau niciodată, logul se oprea tăcut, discul se umplea. Reprodus mecanismul.
Fix: helper `_num`. Reparat și: eșec DB indistinct de „zero rânduri" (acum log explicit + exit 1),
foldere deja goale renumărate în fiecare noapte (14k × `du` recursiv degeaba), `call_id`
neparametrizat în UPDATE (fișier șters de pe disc cu `audio_path` rămas în DB).

**Data extragerii se pierdea.** Un document extras acum 3 zile și trimis azi primea
`extracted_at = now()`, deci apărea în raportul zilei greșite. Fix: `LEAST()` pe conflict + citirea
`created_at` real din `document_extractions`.

**Interfață:** statisticile CTS nu se reîncărcau niciodată (nici la `↻`, nici după procesare — doar
F5); documentele fără categorie intrau în totalul din titlu dar lipseau din defalcare, deci cifrele
se contraziceau pe ecran; raportul din jurnalul de apeluri afișa greșit pentru `update_documents`,
ascunzând exact intrările invalide.

**Documentație falsă corectată** (CTS ar fi implementat-o literal): „`marked_saved` scade spre 0 la
reapel" — fals, rămâne constant (verificat); „`unknown` = id necunoscut" — fals, apare și pentru
documente cunoscute deja șterse (verificat); `entity_type`/`entity_id` documentate obligatorii la
`deleted` deși codul le ignoră; lipsea HTTP 422 pentru body non-obiect. Adăugată secțiunea despre
`part_no`/`extraction_id` cu motivația măsurată.

Fără migrație DB.

## v2.3.1 - 2026-08-12

### Fix: `storage_cleanup.sh` crăpa la raportare (ștergerea se făcea, scriptul ieșea cu eroare)

Descoperit rulând curățenia reală, ca să verific că statistica de trasabilitate îi supraviețuiește.

`DEL1=$(... | grep -c '^[0-9]' || echo 0)`: `grep -c` scrie deja `0` când nu găsește nimic, iar
`|| echo 0` mai adăuga un `0` pe linie nouă. Variabila devenea `"0\n0"`, iar `$(( DEL1 + DEL2 ))`
arunca `syntax error in expression`, urmat de `DOC_DELETED: unbound variable` (`set -u`).

Ștergerea rândurilor se executa corect — cedau doar numărătoarea și raportul final, iar scriptul
ieșea cu cod de eroare. Efect: raportul zilnic din log era trunchiat („document_extractions: N
rânduri șterse" lipsea) și orice supraveghere care se uită la codul de ieșire vedea eșec la fiecare
rulare. Normalizat printr-un helper care întoarce mereu o singură valoare numerică.

Verificat pe staging: scriptul rulează acum până la capăt, cod de ieșire 0, raport complet.

### Verificat: statistica supraviețuiește curățeniei

Probă pe date reale, nu doar pe intenția din proiectare — rulat `storage_cleanup.sh` efectiv
(23 foldere de atașamente șterse, 7 MB eliberați, extracții șterse din DB) și confirmat că:

- rândurile din `cts_document_tracking` rămân neatinse, cu nume, categorie și stare;
- un document a cărui extracție a dispărut din `document_extractions` își păstrează integral
  poziția în statistică (`extraction_id` devine referință moartă, dar cifrele rămân corecte);
- **CTS poate raporta ștergerea unui document care nu mai există local** — cazul care a motivat
  întregul design — și confirmarea se înregistrează normal (`marked_deleted: 1`, cu `admin_id` și
  `deleted_at`), fără `orphan_deleted`.

## v2.3.0 - 2026-08-12

### Documentele spre CTS: max 1,6 MB, PDF obligatoriu la vehicul și contract

Constrângeri impuse de CTS. Nu erau respectate — și nu doar din lipsa unei limite:

- **Decupajul dintr-un PDF se producea ca JPEG** (`_crop_to_files`), deci exact pentru vehicul și
  contract, unde CTS cere PDF, trimiteam imagini. Orice document extras dintr-un PDF cu mai multe
  acte pleca în format greșit.
- **Nicio limită de mărime** pe calea spre CTS: un scan de 13 MB pleca întreg.
- **`_to_pdf_compressed` era defectă și eșua tăcut.** `show_pdf_page` cere un PDF ca sursă, dar
  primea un document deschis cu `filetype="image"` (`is_pdf=False`) → `ValueError: is no PDF`,
  prins de `except` și tratat ca „trimite originalul". Deci conversia imagine→PDF **nu a funcționat
  niciodată** pe PyMuPDF 1.27 — inclusiv pe calea către IRIS, unde funcția era deja folosită
  (`documents.py:1052`). Reparat cu `convert_to_pdf()`; se rezolvă ambele căi.
- Blocul de recompresie agresivă avea același defect, ascuns sub `except: pass`.

Normalizarea se aplică acum ca strat final peste toate ieșirile din `_doc_piece_bytes`, deci nu
există cale ocolită:

| Categorie | Format garantat | Mărime |
|---|---|---|
| vehicul, contract | PDF întotdeauna | ≤ 1,6 MB |
| sofer | PDF sau imagine | ≤ 1,6 MB |

Pentru șofer imaginea se păstrează dacă încape; se convertește doar când e prea mare (acolo
conversia e și mecanismul de compresie). Reducerea are trepte: compresie fără pierderi → calitate
JPEG descrescătoare → reducerea rezoluției. Pentru PDF-uri cu pagini scanate (care nu scad prin
`deflate`, imaginile fiind deja comprimate) paginile se re-randează la rezoluție redusă.
Măsurat pe staging: PNG 1,9 MB → PDF 1,08 MB; PDF scanat 13,6 MB → 564 KB.

Corectat și numele fișierului: extensia urmează conținutul real. Un `.jpg` convertit la PDF ajungea
altfel la CTS ca „talon.jpg" cu octeți de PDF înăuntru.

Dacă nici după toate treptele fișierul nu coboară sub prag, se livrează oricum + avertisment în
jurnal: un refuz CTS e vizibil în `update_documents` și intră în statistică, pe când un document
nelivrat ar dispărea tăcut.

`original_attachment` rămâne intenționat în afara regulii — e fișierul-sursă de referință, nu
documentul care se atașează pe entitate.

## v2.2.3 - 2026-08-12

### `update_documents` apare acum în panoul Conexiune API

Endpoint-ul era documentat în fereastra de integrare, dar lipsea din lista scurtă de pe pagina
Setări → Conexiune API — cea pe care o citește toată lumea prima dată. Adăugat ca
„Confirmare documente (POST)", imediat după „Documente (GET)", cu o notă despre ce alimentează.

Jurnalul de apeluri CTS eticheta apelul ca `GET update_documents`, colorat albastru ca o citire.
Corectat: apare `POST update_documents`, verde, simetric cu `update_emails`.

Documentul `docs/cts_update_documents.md` are acum o secțiune de deschidere pentru PO / Team Leader:
ce se schimbă pentru utilizator, ce întrebare de business răspunde fiecare cifră, ce trebuie făcut
pe partea CTS ca datele să apară, și de ce procentul de ștergeri are un decalaj de până la 24h.

## v2.2.2 - 2026-08-12

### Documentație integrare pentru CTS — `update_documents`

Noul endpoint apare acum în Setări → Conexiune API, alături de celelalte: în lista de apeluri, în
modalul de integrare (secțiunea „4) POST /cts/update_documents") și în documentul HTML descărcabil
ca PDF, pe care colegii de la CTS îl primesc. Include exemple de apel pentru ambele fluxuri
(confirmare salvare/respingere și lotul zilnic de ștergeri), răspunsul așteptat cu explicația
fiecărui contor, câmpurile acceptate și comportamentul la cazuri limită.

Fișier de referință nou: `docs/cts_update_documents.md` — contractul complet, ciclul de viață al
unui document, formulele statisticii cu numitorii lor, note de implementare și lista concretă de
pași rămași pe partea CTS.

## v2.2.1 - 2026-08-12

### Trasabilitate documente: corecții din auditul pre-producție

Audit adversarial pe codul v2.2.0 înainte de promovarea spre producție. Șase probleme reale,
două dintre ele capabile să piardă date în producție:

- **Batch pierdut la o dată invalidă.** `CAST(:deleted_at AS timestamptz)` cu un string neparsabil
  („ieri", „13/08/2026") arunca eroarea direct din Postgres și aborta întreaga tranzacție: într-un
  batch zilnic cu sute de confirmări valide, TOATE se pierdeau, iar CTS nu le retrimite. Datele se
  parsează acum în Python (`_parse_iso_ts`), strict ISO 8601; ce nu e valid cade pe `now()` și e
  raportat în `bad_timestamp`. Legat: `DateStyle=MDY` interpreta „12/08/2026" ca 8 decembrie, nu
  12 august — o dată greșită tăcut e mai rea decât una respinsă.
- **Documente pierdute din statistică la atașamente cu mai multe documente.** Cheia era
  `UNIQUE(attachment_id)`, dar un PDF poate conține mai multe documente (contract + talon + act),
  rânduri distincte în `document_extractions` cu același `attachment_id`. Măsurat pe date reale
  (`document_extractions_bak_sim20_20260630`): 304 documente din 226 atașamente — 78 s-ar fi
  contopit. Cheia e acum `(attachment_id, part_no)`, simetric cu `document_extractions`.
- `entity_id` / `admin_id` non-numerice sau peste limita coloanei abortau tranzacția — coerciție
  defensivă prin `_as_int` (peste range → NULL, nu eroare).
- Același id în `saved` și `failed` producea un rând incoerent (status eșuat, dar cu entitate CTS
  atașată) și îl număra de două ori. Precedență explicită `deleted > failed > saved`, deduplicare
  per listă, iar ramura `failed` golește entitatea.
- Documentele respinse de CTS rămâneau blocate pe `failed` pentru totdeauna: după corectare și
  retrimitere, garda le bloca. Garda acceptă acum `extracted`/`sent`/`failed`, dar nu `saved`/`deleted`.
- Commit per document într-un endpoint de polling → un singur commit după buclă.

### Statistici trasabilitate pe pagina Procesare documente

Blocul de statistici arată acum pâlnia completă: **extras → trimis spre CTS → salvat pe entitate →
șters de operator**, cu defalcare pe șofer / vehicul / contract. Fiecare procent își scrie explicit
numitorul în subtitlu, fiindcă bazele diferă (trimise din extrase, salvate din trimise, șterse din
salvate) — un procent fără numitor știut se citește greșit.

Numitorul „câte s-au extras" nu putea veni din `document_extractions`: e golită zilnic de
`storage_cleanup.sh` (0 rânduri pe staging), deci procentele s-ar fi resetat în fiecare noapte.
Documentele se înregistrează acum în trasabilitate încă de la extragere (stare `extracted`), prin
`_save_extraction` — punctul unic prin care trec toate.

Migrație `20260812b_cts_tracking_part_no.sql` (aditivă, idempotentă).

## v2.2.0 - 2026-08-12

### Trasabilitate documente MailGuard în CTS (CCTS-5308 / CCTS-5071)

Până acum CTS confirma doar emailurile (`update_emails`), nu și soarta fiecărui document trimis.
Nu se putea măsura ce procent din documentele asociate automat chiar ajung pe entitatea corectă,
nici câte sunt șterse ulterior de operatori fiindcă asocierea a fost greșită.

Tabelă nouă `cts_document_tracking` (migrație `20260812_cts_document_tracking.sql`), cheie
`attachment_id` — același id livrat către CTS ca `id_mailguard`. Deliberat FĂRĂ chei străine către
`attachments` / `document_extractions` / `emails`: acele rânduri sunt curățate zilnic de
`storage_cleanup.sh`, iar CTS raportează ștergerile în batch a doua zi. Cu FK, statistica ar
dispărea odată cu documentul; aici păstrăm doar nume, categorie și stare, nu fișierul.

- `get_email_documents`: fiecare document care pleacă efectiv spre CTS (validat, cu fișier produs)
  se înregistrează cu starea `sent`. Polling-ul repetat până la `ready` nu duplică rândul — doar
  incrementează `cts_retry_count`, și nu suprascrie un rând deja confirmat de CTS.
- `POST /api/v1/cts/update_documents` (nou, header `X-CTS-Token`): CTS raportează în același apel
  `saved` (cu `entity_type` + `entity_id`), `failed` (cu `reason`) și `deleted` (batch zilnic, cu
  `admin_id` + `deleted_at`). Idempotent. O ștergere se aplică doar peste un document confirmat
  salvat — dacă ack-ul de salvare s-a pierdut, e numărată separat (`not_saved_yet`), nu dedusă ca
  salvare, ca să nu contamineze rata de succes.
- `GET /api/v1/cts/document-stats` (admin): rata de succes pe categorie (contract/șofer/vehicul),
  cu `from_date` opțional. Un document șters a fost salvat înainte, deci baza ratei de succes e
  `saved + deleted_after` — ștergerile se raportează separat, nu scad succesul asocierii.

UI-ul de statistică pe pagina Procesare documente urmează separat, după validarea datelor reale.

## v2.1.1 - 2026-08-11

### Fix: emailurile cu Ordin de Plată primeau P4/P5 în loc de P2

Seria OP din atașament (`ai_op_series`) se extrage asincron, la 3-14 secunde după ce pipeline-ul
a calculat deja prioritatea. Regula `pay_op_series` din `priority_classifier.py` se uita la un câmp
încă gol, deci nu a lovit niciodată — 0 apariții în `ai_priority_result` pe toată baza, deși
1433 din 1434 emailuri cu OP aveau `ai_priority_at < ai_op_extract_at`. Rezultat: OP-urile primeau
prioritatea ghicită de AI din numele fișierului (ex. `COMPLETED888201889_2026-08-11.pdf` → „raport
de completare" → P4), iar cele rutate inițial spre alt departament rămâneau fără prioritate.

- `op_extractor.py`: după persistarea seriei, prioritatea se recalculează (`_recalc_priority_after_op`).
  Cost AI zero — regula determinstă întoarce înainte de orice apel la model.
- `process_email.py`: `ai_op_series` se recitește proaspăt din DB înainte de clasificare, deci
  ordinea celor două etape nu mai contează în niciun sens.
- `process_email.py`: corecțiile manuale de prioritate (`ai_priority_manual=TRUE`) nu mai pot fi
  suprascrise automat — aliniere cu comportamentul deja existent la asignare.
- `ai_priority.py`: re-încadrarea manuală din UI citea emailul fără `ai_op_series`, deci rata și ea
  regula P2. Coloana e acum inclusă în SELECT.

Fără migrație DB. Emailurile istorice nu se corectează retroactiv (sunt deja trimise la CTS).

## v2.1.0 - 2026-08-08

### Cleanup automat storage zilnic (00:00)

Script `scripts/storage_cleanup.sh` + crontab instalat pe staging și producție:

- **Atașamente native >10 zile**: șterge fișierele fizice din `/home/mail-data/attachments/native/`,
  păstrează folderul și înregistrarea din DB (`storage_path` intact ca referință).
- **Audio apeluri >3 zile cu transcript success**: șterge MP3/WAV din `/home/mail-data/call_audio/`,
  nullează `audio_path` în DB — transcriptul rămâne intact.
- **document_extractions procesate**: DELETE din DB pentru rânduri cu `status != pending` create
  înainte de azi. Două pass-uri (grouped_into IS NOT NULL primul, pentru FK constraint).

Rulează zilnic la 00:00 via crontab. Log: `/opt/iris-mailguard/storage/logs/storage_cleanup.log`
(rotat la 10MB). Eliberează estimat 5-15GB/lună constant.

Migrație `20260808_storage_cleanup_index.sql`: două indexuri parțiale pentru performanță
(`idx_calls_audio_cleanup`, `idx_doc_ext_cleanup`).

## v2.0.0 - 2026-08-08 — RELEASE PE PRODUCȚIE

Consolidează toate livrările v1.1.0–v1.5.0 (export conversație, fix monitor productivitate,
fix cts_department NULL pe slug-uri cu cratimă, fix replici CTS per destinatar,
reducere latență sync CTS la ~2 minute). Rezoluție bug cronic UniqueViolation
în cts_gt_sync (constraint înlocuit cu (source, message_id, cts_ticket_id)).

## v1.5.0 - 2026-08-07

### Export conversatie client — îmbunătățiri

- Imagini inline base64 (jpg/png incluse în HTML email) înlocuite cu `[imagine]` în textul exportat
- Mesaje clare pentru apeluri fără transcript: „Apelul a existat, însă transcrierea audio nu a fost efectuată."
- Apeluri cu eroare de transcriere: mesaj explicit despre fișierul audio indisponibil

### KPI-uri procesare documente — restructurate

Cardurile „Extrase corect / Corectate / Reîncadrate / În verificare / Extrase IRIS" înlocuite cu:
- **Doc. șofer** — % din total global + N auto-validate din total
- **Doc. vehicul** — % din total global + N auto-validate din total
- **Contracte** — % din total global + N auto-validate din total

Procentele reflectă ponderea fiecărei categorii din totalul global (nu % intern per categorie).
Backend: câmp `by_category` adăugat la `/api/v1/documents/extractions/stats`.

### Conversie automată documente → PDF cu compresie

Toate documentele trimise la IRIS pentru extragere sunt acum normalizate:
- Imagini (jpg/png/gif/webp/tiff) convertite automat la PDF via PyMuPDF
- PDF-uri > 1.6 MB comprimate cu `deflate + garbage collect` (elimină resurse redundante)
- Fallback safe: la orice eroare de conversie, fișierul original e trimis neschimbat

## v1.4.0 - 2026-08-07

### Export conversatie client

Buton Exporta conversatia pe pagina fiecarui client. Permite selectarea unui interval de date
(de la / pana la) si genereaza un document HTML cu toate mailurile primite, mailurile trimise si
apelurile telefonice in ordine cronologica, cu continut complet (body text email + transcript apel).

Documentul se deschide intr-o fereastra noua; din dialogul de printare al browserului se salveaza
ca PDF (Destinatie Salveaza ca PDF).

Format document: badge tip (MAIL PRIMIT / MAIL TRIMIS / APEL INTRAT / APEL IESIT), data, subiect
sau numar de telefon, durata apel, adresa expeditor, categorie AI - urmate de textul complet.

Backend: endpoint GET /api/v1/clients/{id}/export-conversation?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD.

## v1.3.0 - 2026-08-06

### Sync CTS — `cts_department` rămânea NULL pe slug-urile cu cratimă

Bug găsit imediat după fixul de atribuire din v1.2.0, care depinde de `cts_department`.
CTS trimite departamentul în două forme: label (`"Suport 1"`) pe tichetele mai vechi, slug cu
cratimă (`"suport-2"`, `"recuperare-tva"`, `"taxe-de-drum"`) pe cele noi. Două defecte cumulate:

1. `_map_department()` nu normaliza cratima — forma cu cratimă cădea pe fallback.
2. `dep_raw` citea doar `department` top-level, care lipsește pe tichetele noi; singura sursă
   reală, `assignment.department_slug`, nu era consultată (e prezentă chiar și fără assignee).

Rezultat: **23 din 243 de tichete deschise** aveau `cts_department` NULL și, neavând nici assignee
pe care să cadă fallback-ul, dispăreau complet din monitor — exact clasa de tichete pe care v1.2.0
trebuia să o facă vizibilă.

Fix în `cts_groundtruth_sync.py`: normalizare cratimă/spațiu + aliasare, și `assignment.department_slug`
/ `department_label` ca surse de rezervă pentru `dep_raw`. Verificat pe 14 forme de intrare.

### Migrație `20260806_cts_department_backfill.sql`

Repară rândurile deja scrise, derivând `cts_department` din `raw->assignment->department_slug`.
Aditivă și idempotentă (atinge doar `cts_department IS NULL`), fără operații structurale.
**19489 rânduri** completate; NULL pe tichetele deschise: 23 → **0**.

Verificat post-fix — API-ul monitorului coincide exact cu DB-ul pe toate departamentele
operaționale (suport_1 26 noi / 2 în lucru, suport_2 3/1, suport_3 0/1, taxe_drum 0/12).

## v1.2.0 - 2026-08-06

### Monitor Productivitate — volume corectate (atribuire pe departamentul tichetului)

Volumele „Noi" / „În lucru" din Monitor nu corespundeau cu dashboardul CTS pe niciun departament.
Cauza: monitorul contoriza pe **departamentul persoanei asignate** (`employee_department_mapping`),
nu pe **departamentul tichetului din CTS** (`cts_ground_truth.cts_department`). Consecințe:

- Un mail `new` **neasignat** nu are persoană, deci JOIN-ul INNER îl arunca complet.
  **69 din 171 de mailuri `new` (40%)** dispăreau. Suport 1 afișa **0 noi în loc de 47**.
- Invers, mailurile asignate cuiva dintr-un alt departament decât cel al tichetului se numărau la
  departamentul greșit: contabilitate afișa **20 în loc de 8**.

Fix: departamentul efectiv = `COALESCE(cts_department, departament_assignee)`, aplicat în toate
interogările de email și task ale monitorului (totaluri, per-departament, reclamații, histograme
orare). JOIN-ul pe angajat a devenit `LEFT JOIN` — nu mai filtrează, doar completează fallback-ul.
Fallback-ul atinge doar rânduri `solved` istorice (4066); **niciun tichet deschis nu depinde de el**.

Același tratament pe task-uri (`cts_task_ground_truth.department`) — acolo divergența era mică
(12 rânduri, ex. 10 `new` pe mobilitate fără assignee), dar consistența e necesară.

### Sincronizare CTS — latență redusă la ~2 minute

`RECENT_MIN_INTERVAL_S` coborât de la **240s la 50s** pe mailuri, task-uri și apeluri. Cron-ul rulează
la 2 minute, iar throttle-ul de 240s arunca un tick din două; la 50s niciun tick nu se pierde, deci
statusurile CTS ajung în monitor cu maxim ~2 minute întârziere (înainte: până la 5-6 minute).

## v1.1.0 - 2026-08-06

### Monitor Operațional — KPI eliminat

Eliminat KPI-ul „Rezolvat / intrat" din cardurile per departament (valori explodau nejustificat,
ex. 1400% la Suport 3). Rămas: „Soluționat azi" + „Deschis acum".

### Device Operations — înlocuiri (inlocuire)

Sync-ul `view_device_operations` preia acum și operațiunile de tip **înlocuire** (`Device Replacement`)
— adăugate în view la sursă de Razvan. 233 înlocuiri importate la primul sync.
Timer automat (30 min) configurabil prin endpoint intern `/device-ops/suport2/sync-internal`.

### Prioritate email — detecție OP îmbunătățită

- Subiect `Plata taxe drum / plata taxa drum / plata taxa rutier / plata taxa intracomp` → **P2** automat (regulă deterministă, fără AI).
- Dacă vision AI a extras o serie OP (`ai_op_series`), emailul e promovat direct la **P2** înainte de orice analiză AI.
- Elimină false-negative-urile pe mailuri cu atașament OP și subiect/corp gol.

### Timestamps Device Operations

Timestamps din `view_device_operations` (UTC cu offset RO aplicat greșit) corectate în sync și în API:
ora afișată în UI este acum ora reală din România (ex. 09:18 în loc de 06:18).

### Breakdown modal — sortare + telefon apeluri

- Coloane sortabile: Client (alfabetic), Creat (ASC/DESC), Soluționat (ASC/DESC).
- La tipul „Apeluri": coloana Subiect înlocuită cu numărul de telefon al apelantului.

---

## v1.0.0 - 2026-08-06 — PRIMUL RELEASE PE PRODUCȚIE

### Productivitate — Taxe de drum: filtrare exactă pe tip task

Categoriile BGToll / EToll / HU-GO / CargoBox numără acum EXCLUSIV task-urile cu
`task_type` exact (ex. `BGToll: New device installed`). Restul (sub balance, device moved etc.)
intră la obiectivul general. Se aplică doar la `taxe_drum`, alte departamente nemodificate.
Fix aplicat atât pe calculul de productivitate cât și pe modalul de detaliu (breakdown).

### Procesare documente — tipuri noi și ajustări

- **Anexa 2 - Proces verbal CargoBox** (mutat din `vehicul` → `contract`): extrage `Licence Plates List` + `Companie`
- **Anexa 3 - contract carGObox** (`contract`): identify only — Termeni și condiții Toll4Europe
- **Anexa 4 - contract carGObox** (`contract`): identify only — Informații GDPR Toll4Europe
- **CUI / Extras pe contract carGObox sau ETOLL** (`contract`): extrage `Nume firma` + `CUI firma` (după „Cod Unic de Înregistrare")
- **TMS - Diurne si salarii minime** (`contract`): extrage `Numar contract`, `Data contract`, `Prestator`, `Client`, `CUI client`, `Este semnat`
- **Dezactivat:** `Anexa 2 - contract carGObox` (înlocuit de Procesul verbal de mai sus)

---
<!-- Istoric pre-release (versiuni interne de dezvoltare) -->

## v0.77.1 - 2026-08-06

### Taxe de drum: categoriile BGToll/EToll/HU-GO/CargoBox numără doar "New device installed"

Înainte, ORICE task de tip `BGToll` (ex. `BGToll: sub balance unknown increase`) era numărat la
obiectivul **BGToll** din productivitate. La fel pentru EToll, HU-GO, CargoBox.

Acum, pentru **taxe_drum** (și doar pentru acest departament), fiecare categorie numără exclusiv
task-urile cu `task_type` exact:
- BGToll → `BGToll: New device installed`
- EToll → `EToll: New device installed`
- HU-GO → `HU-GO: New device installated` *(typo existent în CTS, asta e valoarea reală din DB)*
- CargoBox → `carGObox new device installed`

Restul (ex. `BGToll: sub balance unknown increase`, `HU-GO: Device moved` etc.) intră acum
la obiectivul general de task-uri al departamentului.

Alte departamente (ex. suport_1/cargobox) — comportament nemodificat.

## v0.77.0 - 2026-08-05

### Un rând per TICHET CTS: replicile pe destinatari nu se mai suprascriu reciproc

CTS creează un tichet **per destinatar**. Mailul 58176 (`mmm@novatrade.ro`, trimis către `office@`
+ `maria.tomuta@` + `madalina.apetrei@`) a generat 3 tichete, toate cu același `message_id`:

| log_id | status | assignee | departament | assigned_at |
|---|---|---|---|---|
| 1088948 | solved | vanessa.boros | suport-1 | 01.08 07:59:52 |
| 1088946 | solved | madalina.apetrei | contabilitate | 03.08 05:46:07 |
| 1088947 | new | maria.tomuta | contabilitate | 03.08 05:46:15 |

`UNIQUE (source, message_id)` + `ON CONFLICT DO UPDATE` le făcea să se suprascrie una pe alta, deci
rămânea **ultima procesată** — arbitrar, după ordinea din răspunsul CTS. Aici a rămas cea a Mariei,
`new`, deși Vanessa rezolvase mailul în 36 de minute. Consecință: munca Vanessei și a Mădălinei nu se
contoriza, iar mailul apărea blocat pe o persoană aflată în concediu.

**Schimbare de model:** cheia de unicitate include acum tichetul
(`migrations/20260805_cts_ticket_replicas.sql`):

- `cts_ticket_id` — id-ul tichetului CTS (`extra.cts_email_log_id`), unic per destinatar
- `cts_is_replica` — `false` pe original, `true` pe replici
- `UNIQUE (source, message_id, cts_ticket_id)` înlocuiește `UNIQUE (source, message_id)`

Originalul nu e marcat de CTS (toate replicile au același `to_email` și `created_at`), deci se
deduce: cel cu cel mai vechi `cts_assigned_at`, la egalitate cel mai mic `cts_ticket_id`. Marcarea
rulează o dată pe lot în `_mark_replicas()` — depinde de toate tichetele aceluiași mail, deci nu
poate fi calculată în upsert-ul per rând.

Rezultat pe 58176: fiecare tichet intră în productivitatea persoanei lui —
Vanessa 35.9 min on time (suport 1), Mădălina 46.1 min on time (contabilitate). Tichetul Mariei
rămâne `new`, deci nu intră în productivitate.

Pe august: 2737 rânduri / 2712 mailuri distincte, 25 replici. Un mail intern
(`lavinia.stefanou@cargotrack.ro`) are 45 de tichete pe 10 departamente — cazul extrem; mailurile de
la clienți au 2–4 tichete, pe 1–2 departamente.

`_coerce_client_id` → `_coerce_pos_int` (folosit acum și pentru id de tichet, nu doar client).

## v0.76.1 - 2026-08-05

### Mailuri: clientul afișat se deducea din adresa expeditorului, nu din atribuirea CTS

Alt tip de problemă decât la apeluri/task-uri (v0.75.0/v0.76.0) — acolo join-ul era pe cheia
greșită. Aici join-ul era corect, dar **sursa** era greșită: se prefera `emails.client_id`, care e o
deducție a MailGuard din adresa expeditorului, peste `extra.client_id` din CTS, unde un operator
uman a decis efectiv pe cine e tichetul.

Deducția pe adresă greșește când același om scrie pentru mai multe firme. Caz raportat:

| Email | Expeditor | Afișa (greșit) | Corect (CTS) |
|---|---|---|---|
| 58219, 58222 | alin.vallgarden@yahoo.com | GETA-ALIN ROUTIER S.R.L. | KOSMIN CARGO SPOLKA (IRIS 16033) |
| 58205 | soringavris78@yahoo.com | MIRESOR TRANS SRL | COPFOREST CONSTRUCT SRL |
| 58263 | cernis68@gmail.com | VERHOVETCHI AUREL | VOLOSCIUC ANATOLIE |

`alin.vallgarden@yahoo.com` e listat în `clients.emails` la GETA-ALIN ROUTIER, deci potrivirea pe
adresă „reușea" — doar că pe firma nepotrivită.

**Noua prioritate** (`_EMAIL_CLIENT_SQL`): CTS întâi, deducția locală doar ca plasă de siguranță.
`UNKNOWN CLIENT` (id local 3081) NU se tratează ca identificare — e santinela CTS pentru
„neatribuit", deci cade pe deducția locală. Fără asta, 22 de mailuri identificate corect local ar fi
devenit „necunoscut".

Rezultat pe august 2026 (1291 rânduri): 48 corectate, 22 recuperate din `UNKNOWN CLIENT`,
**0 pierdute** în necunoscut, 1243 neschimbate. Rămân 18 fără client — clientul atribuit în CTS nu
e sincronizat în `clients` local (nu e decidabil aici).

Impact zero pe cifre: din cele 48 corectate, niciunul nu intră sau iese din `productivity_exclude`,
deci numărul de mailuri măsurate și statusurile on time/overdue rămân identice. Se schimbă doar
eticheta clientului.

Pagina „Mail-uri CTS" (`cts_training.py`) folosea deja `extra.client_id` — neatinsă.

## v0.76.0 - 2026-08-05

### Task-uri: clientul afișat era greșit pe 88% din rânduri (aceeași cauză ca la apeluri)

`cts_task_ground_truth.client_id` e ID din **IRIS**, dar join-ul se făcea pe cheia primară locală
`clients.id`. Ca la apeluri (v0.75.0): numerele se suprapun, deci join-ul greșit nu dădea NULL —
returna tăcut alt client.

Amploare pe august 2026: din 2210 task-uri cu `client_id`, **1956 afișau alt client** (88%).
Exemple verificate, task-uri `solved`:

| Task | client_id CTS | Afișa (greșit) | Corect |
|---|---|---|---|
| 66090312 | 15184 | VIATRANS EMDO SRL | LUCACROSS INOVATIV SRL |
| 5224891 | 11388 | NAC SPEED LOG SRL | SABRIGIS LOGISTICS S.R.L. |
| 63991277 | 2952 | CRIS TRANS LOGISTIC SRL | TAC LOGISTICS SRL |
| 63991319 | 542 | VTR TRANS EUROPEAN S.R.L. | LI.COM S.R.L. |

După fix, pe task-urile `solved` din august care au `client_id`: **1299 din 1343 (96.7%)** se
rezolvă corect. Restul (44) au un ID care nu există în `clients`.

Notă asupra acoperirii: din 2901 task-uri `solved` în august, **1558 nu au deloc `client_id`** —
sunt task-uri generate de sistem („Device in roadtax country will be suspended", „ETOLL: device
position jump…"), nelegate de un client. Acolo nu e nimic de mapat, coloana rămâne goală corect.

`cts_tasks_training.py` folosea deja corect `iris_client_id` — divergența era doar în
`app/services/productivity.py`, care acum e aliniat pe toate cele trei canale (mailuri, apeluri,
task-uri).

### `device_operations` — verificat, NU a fost modificat

Are același tipar de join (`cl.id = d.client_id`), dar `client_id` e **NULL pe toate cele 1988 de
rânduri**, deci nu se poate determina empiric ce convenție folosește și nicio schimbare nu ar avea
efect observabil. Lăsat neatins intenționat, ca să nu se ghicească; de reverificat când tabela
primește date reale.

## v0.75.0 - 2026-08-05

### Apeluri: clientul afișat era greșit pe 90% din rânduri (join pe cheia greșită)

`cts_calls_ground_truth.raw->>'client_id'` e ID-ul din **IRIS**, dar join-ul se făcea pe cheia
primară locală `clients.id`. Numerele se suprapun, deci nu dădea NULL — nimerea peste un client
complet diferit, tăcut.

Caz verificat cu sursa BI (apel 720757): CTS trimite `client_id=11442`, care e
`TRANSEMC TRAVEL SRL` (`clients.id=11528`, `iris_client_id=11442`). Se afișa **EURO RIN SRL**,
pentru că acela are întâmplător `clients.id=11442`.

Amploare pe august 2026: din 743 de apeluri, **671 afișau alt client** (90%). Join-ul pe
`iris_client_id` prinde 741/743; cel vechi prindea 672, aproape toate greșit. După fix: **595 din
597 de rânduri (99.7%)** au clientul corect.

Restul codebase-ului folosea deja corect `iris_client_id` (`cts_tasks_training.py`, `clients.py`) —
doar `productivity.py` avea join-ul pe cheia locală. Corectat în ambele locuri: ramura `apel` și
fallback-ul de la mailuri (acolo nu producea efect vizibil — `emails.client_id` acoperă 1219 din
1240 de rânduri și fallback-ul nu se activa — dar rămânea aceeași capcană).

### Apeluri: coloana „Soluționat" arăta ora de START

`created_at` și `solved_at` primeau amândouă `p_start`, deci cele două coloane erau identice și
niciuna nu arăta închiderea reală. Payload-ul CTS conține `solved_at` corect, doar că nu era citit.

Confirmat cu BI pe apelurile din poză: `05:18:44` și `05:44:52` — exact `closed_at` din sursa de
adevăr. Textul din JSON e naiv în UTC, deci se marchează explicit ca UTC (ca la mailuri). Fallback
pe `cts_started_at` dacă `solved_at` lipsește.

### Ce NU s-a modificat (intenționat)

**Timpul măsurat rămâne `ring_seconds`** (cât sună până răspunde agentul), cu obiectivul configurat
la 4 secunde. Payload-ul mai conține două mărimi diferite — `duration_seconds` (durata convorbirii)
și `time_to_solved_seconds` (start → soluționare: 809s și 2529s pe cele două apeluri din BI). Care
dintre ele reprezintă „productivitatea pe apeluri" e o decizie de business, nedecisă încă; până
atunci calculul rămâne neschimbat. Pe cele două apeluri din poză statusul nostru (`5s > 4s` =>
overdue) coincide oricum cu BI.

**Mailurile rămân neatinse.** Rândurile rezolvate integral în afara programului (0 minute) rămân
`on_time`, conform deciziei din 03.08 — confirmat explicit 05.08 după analiza pe august: 14 rânduri
din 727 (1.9%), niciunul ascunzând o întârziere imputabilă cuiva (weekend/noapte fără personal
pontat, sau rezolvate voluntar în câteva ore).

## v0.74.0 - 2026-08-04

### Durata se măsoară pe acoperirea DEPARTAMENTULUI, nu pe tura operatorului care a rezolvat

Până acum durata se calcula pe pontajul **individual** al operatorului care a rezolvat. Dacă mailul
intra înainte ca acel operator să intre în tură, așteptarea nu se contoriza nicăieri.

Caz care a declanșat schimbarea (email_id 58516, Suport 1, NOVA TRADE GLOBAL MMM SRL): mail intrat
**03.08 09:56** local, rezolvat **13:16** de un operator cu tura 12:30–21:00. Se numărau doar
**46 min** („on time"), pentru o așteptare reală de 3h20m. Din perspectiva clientului răspunsul a
venit în peste 3 ore; raportul spunea 46 de minute.

Regula nouă: clientul așteaptă **departamentul, nu persoana**. Cât timp departamentul are cel puțin
un om prezent, timpul curge pe programul departamentului, indiferent cine rezolvă efectiv și în ce
tură e. Același mail dă acum **199.7 min → overdue**.

- **Cu program configurat** (`suport_1/2/3`, `taxe_drum`) → fereastra = programul zilei
  (ex. Suport 1: 07:00–21:00)
- **Fără program** (`contabilitate`, `recuperare_tva`) → fereastra = uniunea turelor celor prezenți
  în acea zi (de la primul început la ultimul final, din pontaj)
- **Zi cu 0 prezenți** → nu curge deloc, se trece la ziua următoare

Intervalele care depășesc finalul programului **continuă a doua zi la deschidere**: mail intrat la
20:00 și rezolvat la 09:00 dimineața = 60 min (azi, până la 21:00) + 120 min (mâine, de la 07:00)
= **180 min**. Verificat: 180.0 exact, atât în Python cât și în SQL.

Efect secundar important: durata nu mai depinde de **cine** a rezolvat. Aceleași capete de interval
dau același rezultat pentru orice operator — inclusiv pentru unul fără pontaj în ziua respectivă
(înainte, un pontaj lipsă putea da 0 minute).

**Scope: doar cele 6 departamente cerute** — `suport_1`, `suport_2`, `suport_3`, `taxe_drum`,
`contabilitate`, `recuperare_tva`. Restul (comercial, mobilitate, HR, account_management etc.)
rămân pe tura individuală, comportament neschimbat. Verificat: `comercial` dă în continuare 46.15
pe același interval de test.

Modificat în **ambele** implementări, ca mailurile și task-urile să nu răspundă diferit la aceeași
întrebare:
- `_BizCache.business_minutes` / `_dept_window` (Python) — productivitatea pe mailuri
- `business_minutes_emp()` (SQL, `migrations/20260804b_business_minutes_dept_window.sql`) —
  task-uri, device-ops, pagina de sănătate

Confirmat că cele două dau rezultate identice pe toate cazurile de test (199.73 / 180.0 / union /
departament exclus).

### Recalculare august 2026 (toate cele 6 departamente)

| Departament | Rânduri | On time | Overdue | Medie |
|---|---|---|---|---|
| suport_1 | 396 | 304 | 92 | 79.4 min |
| suport_2 | 47 | 29 | 18 | 113.3 min |
| suport_3 | 0 | 0 | 0 | — |
| taxe_drum | 30 | 23 | 7 | 271.3 min |
| contabilitate | 171 | 146 | 25 | 78.1 min |
| recuperare_tva | 83 | 14 | 69 | 422.4 min |
| **TOTAL** | **727** | **516** | **211** | **71.0% on time** |

Duratele mailurilor se calculează live din `cts_ground_truth`, deci schimbarea se aplică retroactiv
la tot istoricul, nu doar la august. Snapshot-urile lunare (target-uri: zile lucrătoare, ore
planificate, coeficienți) au fost re-fixate pentru august.

### Notă despre pontaj și fusul orar (verificat, nu presupus)

`employee_attendance.begin_time/end_time` sunt `timestamp WITHOUT time zone` stocate în **UTC**.
Suspiciunea inițială a fost că tura `09:30–18:00` e ora locală și conversia o mută greșit cu 3h —
datele au infirmat-o: pe zilele cu pontaj `09:30`, activitatea reală a acelor operatori începe la
**12:30 local**, cu **0 mailuri rezolvate înainte de 12:30 pe 408 cazuri** (Buda Alina-Mioara).
Iar tura dominantă `05:00–13:30` (1729 înregistrări) = 08:00–16:30 local, ceea ce se potrivește cu
activitatea observată (~08:00). Deci conversia UTC era corectă; problema era *care* fereastră se
folosește, nu cum se convertește.

## v0.73.0 - 2026-08-04

### Productivitate mailuri: „Creat" arăta momentul greșit, iar mailurile overdue apăreau „on time"

Data „Creat" din modalul de breakdown (și din tot calculul de productivitate) venea din
`cts_ground_truth.raw->'extra'->>'created_at'`. Acela **nu** e momentul în care mailul intră în
CTS: e momentul creării **tichetului**, adică momentul în care cineva atinge mailul. Se deplasa
înainte odată cu neglijarea mailului, deci întârzierea se ascundea singură — cu cât un mail
stătea mai mult neatins, cu atât startul se împingea și durata raportată ieșea mai mică.

Caz care a dovedit-o (email_id 54196): primit **24.07 14:42**, trimis în CTS 14:45 (3 minute
mai târziu), tichet creat **28.07 08:36** (4 zile mai târziu), rezolvat 29.07 06:55. Se raportau
~22h în loc de ~4.5 zile, iar rândul apărea **on time**. Acum: 1135 minute de program, **overdue**.

Sursa corectă e `extra.email_date`, care e egal cu `emails.received_at` **exact**, pe toate
rândurile. Alternativa `emails.sent_to_cts_at` ar fi fost semantic mai potrivită (momentul real
al trimiterii spre CTS), dar e NULL pe 2876 din 8648 de rânduri (33%) — inclusiv pe majoritatea
cazurilor problematice — deci ar fi lăsat o treime din calcul pe fallback.

Expresia de start e acum definită **o singură dată** (`_EMAIL_START_SQL` în
`app/services/productivity.py`) și refolosită în toate cele 9 locuri care o consumau prin
copy-paste: totalurile paginii, modalul rând-cu-rând, trendul lunar, statisticile per operator,
monitorul live, restanțele și histogramele pe oră. Înainte, un fix într-un singur loc ar fi făcut
modalul să contrazică pagina.

Efect colateral important: la **restanțe** (`noi_vechi`, `restante`, `peste_7z`) bug-ul acționa
exact invers de cum trebuie — vechimea unei sesizări neglijate se resetează la fiecare atingere,
deci restanțele erau **sub-raportate**. Comentariul din cod afirma greșit că `extra.created_at`
e „momentul real de sosire"; a fost corectat.

Pagina **Mail-uri CTS** nu a necesitat modificări: folosea deja `email_date`. Redundanța
observată (aceeași dată afișată diferit în două locuri) era simptomul — acum cele două se aliniază.

**Task-urile au fost verificate și NU au această problemă.** `cts_task_ground_truth.cts_created_at`
e `timestamptz` real de la CTS, cu 0 cazuri de `created_at > updated_at`, istoric coerent din 2024
și tail lung onest (583 task-uri peste 240h). Sunt evenimente create de sistem la momentul
producerii, nu tichete deschise când cineva le atinge — deci nu suferă de deplasare retroactivă.

### Excluderea non-clienților din productivitate (coloană nouă `clients.productivity_exclude`)

Entitățile care nu sunt clienți reali (sisteme de taxare rutieră, furnizori, pseudo-clienți
interni) intrau în calculul de productivitate cu status „on time"/„overdue", deși nu reprezintă
muncă de suport măsurabilă: **142 din 972 de rânduri pe august 2026**. Regula exista, dar doar
pentru **satisfacție** (`clients.satisfaction_exclude`, migrația `20260729i`) — niciun query de
productivitate nu o consulta. În plus, 6 din cele 10 entități nu erau nici măcar marcate.

Flag nou `clients.productivity_exclude`, **separat intenționat** de `satisfaction_exclude`: cele
două rapoarte răspund la întrebări diferite („e clientul mulțumit?" vs „a răspuns operatorul în
timp?"), iar cuplarea lor ar face ca orice excludere viitoare dintr-unul să dispară silențios și
din celălalt.

Excluse (match pe nume, ILIKE, ca la `20260729i` — prinde variantele viitoare fără migrație nouă):
`HU-GO%` (HU-GO ELECTRONIC TOLL SYSTEM, HU-GO TEMP), `RUPTELA%` (RUPTELA, RUPTELA UAB),
`TOLL4EUROPE%`, `LOCATOR BG%`, `ORANGE ROMANIA%`, `00-FIRMA NECUNOSCUTA%`, `HELP DESK CTS%`,
`CTS INTERNAL%` — 10 clienți azi.

### Impact numeric (august 2026, toate departamentele, limită 2h)

| | Înainte | După |
|---|---|---|
| Rânduri în calcul | 1009 | 830 |
| On time | 795 | 582 |
| % în timp | 78.8% | **70.1%** |

Cele mai afectate: Suport 1 (418→381 rânduri, 367→306 on time), Taxe drum, Contabilitate.
Procentul scade pentru că raporta prea optimist, nu pentru că performanța s-ar fi schimbat.

Migrație: `migrations/20260804_productivity_exclude_and_email_start.sql` (idempotentă, aditivă;
rollback prin `UPDATE clients SET productivity_exclude = FALSE`).

## v0.72.3 - 2026-08-04

### Bannerul de decalaj CTS afirma o cauză neverificată („fișa apelului")

Textul spunea: *„decalajul vine din CTS, de la momentul în care operatorul deschide/închide fișa
apelului"*. Două probleme:

1. **„Fișă" nu există în CTS.** Sursa `/cts/calls` returnează 24 de câmpuri (`status`,
   `category_id`, `assignee_*`, `client_id`, `ring_seconds`, …) — nicio noțiune de fișă, nimic
   despre „deschis/închis de operator". Cuvântul era inventat în interfața noastră.
2. **Cauza afirmată nu e susținută de date.** Măsurat 2026-08-04 la 10:40 local: `calltrack_id`
   (id secvențial generat de CTS) era `1329233` în `/cts/calls`, dar `1329629` în While1 — ~400 de
   apeluri cărora CTS le alocase deja un id nu erau returnate de endpoint. Cele mai noi 6 apeluri
   din centrală, căutate individual după `ctk_uniqueid` ȘI `calltrack_id`, lipseau complet.
   Deci nu e „operatorul n-a completat încă", ci apeluri care nu ajung în răspunsul sursei.

Text nou, limitat la ce e verificabil: „Cel mai nou apel primit din CTS" / „nu au ajuns încă în CTS"
/ „Apelurile care lipsesc nu sunt returnate de CTS la momentul interogării — cauza se află în amonte,
nu în Cargo360."

Cauza reală (ingestie oprită în `cts_replica.client_call_log` sau filtru în endpoint) nu e
decidabilă din Cargo360 — infra IRIS. Escaladată în outbox #58, cu interogările de verificare.
Comentariul din `_freshness()` documentează măsurătoarea, ca interpretarea greșită să nu revină.

## v0.72.2 - 2026-08-04

### Apeluri CTS fără client („—") — clientul exista, doar nu-l citeam

Raportat: rânduri în „Apeluri CTS" cu client necompletat, deși toate apelurile au client.

Cauza: `client_id` și `client_name` se citeau EXCLUSIV din `calls`, prin
`LEFT JOIN calls ON c.id = gt.call_local_id`. Pentru un apel CTS fără corespondent While1
(`call_local_id IS NULL`) join-ul dădea NULL, deci clientul apărea „—". Sursa `/cts/calls` trimite
însă `client_id` pentru FIECARE apel — îl păstram doar în `raw`, folosit exclusiv ca forward-fix pe
`calls.client_id`. Verificat: 5.514 apeluri cu `call_local_id NULL`, **toate** cu `client_id` în sursă.

Fix:
- coloană nouă `cts_calls_ground_truth.cts_client_id` (migrație `20260804_cts_calls_client_id.sql`,
  aditivă + idempotentă, cu index parțial și backfill din `raw` — 13.283 rânduri completate)
- upsert-ul persistă `cts_client_id` la fiecare sincronizare
- lista rezolvă clientul cu fallback: While1 întâi, apoi fișa CTS (`clients.iris_client_id`).
  Câmp nou în răspuns: `client_source` = `while1` | `cts` | `null`

Rezultat: din 13.283 apeluri, 13.268 au acum client afișabil. Cele 15 rămase au `client_id` în CTS,
dar clientul nu e încă în tabela `clients` (ex. 16803–16805, clienți noi) — se rezolvă la următoarea
sincronizare de clienți, nu necesită cod.

Notă despre banner-ul „CTS a rămas în urmă cu N minute": e calculat corect (`_freshness()`) și
reflectă întârzierea reală din CTS — momentul în care operatorul deschide/închide fișa. Nu e afectat
de acest fix și nu indică o problemă de sincronizare.

## v0.72.1 - 2026-08-04

### Apeluri CTS întârziate cu câteva ore — fix pe fereastra de sincronizare

Pagina „Apeluri CTS" arăta un decalaj de câteva ore (raportat: 9h), cu impresia că sincronizarea
se blochează și pornește doar din când în când. Sincronizarea rula corect, la fiecare 5 minute;
problema era fereastra de timp cerută sursei.

`updated_at` din sursa CTS e inconsecvent între două fusuri (verificat empiric 2026-08-04,
ceas UTC 06:11 / local RO 09:11):

- apel abia intrat, neatins de operator (`status='new'`) — `updated_at == started_at`, ambele **UTC**
  (ex. `cts_call_id=721493`, `started=06:10:14`, `upd=06:10:14`)
- apel atins/modificat de operator — `updated_at` rescris în ora **locală** România
  (ex. `cts_call_id=721447`, `started=04:51:23`, `upd=07:54:25`)

Adică `updated_at` e UTC la inserare și devine local la modificare. Filtrul `?since` era aliniat la
ora locală, deci pentru un apel nou cădea cu 3h *după* `updated_at`-ul lui în UTC: apelul devenea
vizibil abia când operatorul îl atingea. De aici decalajul și aparența de sincronizare agățată.

Fix: `_source_now()` ancorează în cadranul cel mai devreme (UTC), iar fereastra rolling crește de
la 24h la 72h — acoperă ambele forme de `updated_at` plus fișele atinse peste noapte sau în weekend.
Câștig de acoperire: ~51h. Cost zero — upsert idempotent pe `UNIQUE(source, cts_call_id)`, volum
~350 apeluri/zi.

### Sincronizarea „completă" aducea apeluri din 2020, nu pe cele recente

`sync_ground_truth(since=None)` nu însemna „tot": sursa livrează în ordine `updated_at ASC` și taie
la `limit`, deci fără ancoră se întorceau cele mai VECHI apeluri. Verificat: `limit=5000` fără
`since` returna până la `cts_call_id=5148`, `started 2020-04-10` — niciodată prezentul.

Fix: `since=None` se ancorează acum la `FULL_SYNC_MAX_DAYS = 400` zile în urmă.

Fără schimbări de schemă sau de interfață. Sincronizarea la 5 minute nu era afectată de acest
al doilea punct (ea trimite mereu `since`).

## v0.72.0 - 2026-08-03

### Obiectiv fără nimic de rezolvat = 100% (nu exclus din scor)

Un obiectiv cu 0 intrări pe luna respectivă (ex. „Task-uri — CargoBox", 0 task-uri) ieșea cu
`achieved = None` și era scos din media ponderată — ponderea lui de 5% se redistribuia pe restul,
deci se împărțea la 95 în loc de 100. Scorul general depindea astfel de un obiectiv gol.

Acum: 0 intrări => 100% (nu poți rata ce nu a existat). Suport 1 / august 2026: 91.85% → 92.25%,
suma ponderilor active 95 → 100.

Distincție păstrată: dacă au existat intrări dar NICIUNA măsurabilă, obiectivul rămâne `None` și
în continuare exclus — acolo nu se poate afirma nici 100%.

### Timpul 0 minute (rezolvat în afara programului) se numără ca „On time"

Decizie de business (Raul Covaci, 2026-08-03), care inversează parțial fix-ul din v0.71.0.

Când tot intervalul creare→soluționare cade în afara orelor de lucru, `business_minutes` întoarce
`0`. În v0.71.0 aceste cazuri au fost scoase din calcul ca „Nemăsurate". Acum se consideră rezolvate
**în timp**: omul a răspuns când nu era obligat să fie la lucru (weekend, noaptea, sau după ce
ieșise din tură), deci nu are sens să fie penalizat sau ignorat.

`_measurable()` acceptă din nou `0`; `None` (interval invalid — capăt lipsă sau soluționat înainte
de creare) rămâne nemăsurabil, pentru că acolo nu se știe nimic despre durată. `business_minutes`
separa deja explicit cele două cazuri.

Rândurile afectate sunt marcate în pagina de breakdown cu „în afara programului" sub eticheta de
status (`in_afara_programului` în API), altfel un „On time / 0 min" ar părea eroare de calcul.

Suport 1 / august 2026 după ambele schimbări: email 86.61% → 87.55% (20 cazuri), task 95.24% →
95.61% (9 cazuri), scor general 91.85% → **92.88%**. Nu mai există rânduri „Nemăsurate";
`total == measurable` pe toate canalele.

Exemple reale găsite la verificare: 17 mailuri închise în weekend, plus 3 închise luni 03.08 între
16:37 și 16:52 de un operator cu tura pontată 05:00–13:30 (muncă peste program — confirmat ca
normal, nu eroare de pontaj).

### Verificare task-uri și apeluri (fără modificări de cod)

**Task-uri — corect.** Pe suport_1 / august: 0 timestamp-uri inversate, 0 cazuri cu timp de lucru
mai mare decât timpul de ceas (deci programul se aplică), medie 30.0 min lucru vs 38.7 min ceas.
Limitare de notat: CTS nu trimite un `solved_at` pentru task-uri, doar `cts_updated_at` (ultima
modificare) — o editare după rezolvare ar umfla durata. Pe august: 0 astfel de cazuri.

**Apeluri — problemă de definiție, nu de calcul.** Obiectivul „4 secunde" măsoară
`cts_response_seconds` = `ring_seconds` = cât sună telefonul până răspunde agentul, NU timpul de
soluționare. Toate valorile pe suport_1 / august sunt între 0 și 5 secunde (medie 1.9s, 122 din 260
exact 0), deci indicatorul nu poate scădea sub limită — de aici 99.56%. CTS trimite și
`time_to_solved_seconds` (medie 200 minute, maxim ~24h), care e ignorat. Apelurile sunt și singurul
canal care nu trece prin `business_minutes`. Comportamentul e documentat ca intenționat în
`cts_calls_sync.py`; schimbarea lui e o decizie de business, în așteptare.

## v0.71.1 - 2026-08-03

### Fix: Monitor arăta procentul ESTIMAT, nu cel realizat (diferit de pagina Rapoarte)

`/productivity/dashboard/data` lua `obiectiv_atins` și `status` din `forecast_report()`, care e o
**estimare** — proiectează media ultimelor 2 luni complete pe luna în curs — nu realizarea efectivă.
Pagina Rapoarte folosește `department_report()` (datele reale ale lunii), deci cele două ecrane
arătau cifre diferite pentru aceeași lună și același departament:

| Departament | Monitor (înainte) | Rapoarte | Monitor (acum) |
|---|---|---|---|
| suport_1 | 88.12% | 92.67% | 92.67% |
| suport_2 | 88.71% | 96.77% | 96.77% |
| taxe_drum | 84.19% | 89.86% | 89.86% |
| contabilitate | 58.43% | 72.20% | 72.20% |
| recuperare_tva | 51.14% | 69.23% | 69.23% |

Acum `obiectiv_atins` / `status` vin din `department_report()`. Țintele și capacitatea
(`obiectiv_real`, `obiectiv_minim`, `ore_planificate`, `ore_disponibile`, `coeficient`) rămân din
`forecast_report()`, unde sunt calculate corect pe luna întreagă.

Fallback păstrat: dacă luna nu are încă date măsurabile (`obiectiv_atins is None`, ex. suport_3 sau
prima zi a lunii), cardul afișează estimarea — altfel ar rămâne gol. `/productivity/forecast`
(pagina de estimări) nu e afectat.

## v0.71.0 - 2026-08-03

### Breakdown per obiectiv de productivitate (buton „ochi")

Fiecare rând de obiectiv din pagina Productivitate are acum un buton de detalii care deschide
lista brută din spatele procentului: client, subiect, data creării, data soluționării, cine a
rezolvat, timpul de soluționare (minute de program de lucru) și statusul `On time` / `Overdue` /
`Nemăsurat`. Filtre: status, angajat, interval de dată a soluționării, căutare pe client/subiect;
paginare 100 rânduri.

`GET /api/v1/productivity/breakdown?tip=&department=&month=&categorie=&status=&user_id=&search=&date_from=&date_to=`
(admin). Suportă `tip` ∈ `email|task|apel|device_ops`.

Implementarea (`productivity.breakdown_rows()`) refolosește exact aceleași interogări, filtre și
convenții de timp ca `department_report` / `_fetch_*_rows`, inclusiv `_BizCache.business_minutes`.
Verificat pe suport_1 / august 2026: breakdown-ul dă identic cu raportul pe toate obiectivele —
email 227/210/187, task 106/97/94, apel 229/229/228.

### Fix: durata 0 minute era contorizată drept „în timp" (procent de productivitate umflat)

`business_minutes_emp` întoarce `0` când tot intervalul creare→soluționare cade în afara
programului de lucru (mail intrat noaptea sau în weekend și închis înainte de următoarea
fereastră). `_measurable()` accepta `mins >= 0`, deci 0 era considerat măsurabil și `0 <= limită`
îl marca „în timp" — deși nu s-a măsurat nimic. Contrazicea `resolution_minutes()`, care respinge
explicit intervalele `<= 0` ca degenerate, și chiar comentariul din `_accumulate`.

Fix: `_measurable()` cere `mins > 0`. Apelurile rămân neatinse (folosesc `allow_zero=True`, unde
0 secunde = răspuns instant, o măsurătoare validă). Efect pe suport_1 / august 2026: 17 din 224
mailuri ieșite din numărătoare, email 90.18% → 89.05%.

### Fix: contoarele „Noi" și „În lucru" din Monitor operațional / financiar

Cardurile numărau stări suprapuse, nu disjuncte:
- **„Noi"** număra tot ce a INTRAT azi, indiferent de starea actuală — un mail intrat și rezolvat
  în aceeași zi apărea și la „Soluționate" și la „Noi". De aici valorile umflate: 170 „noi" la
  mailuri, deși doar 1 era efectiv în starea `new` (166 erau deja `solved`); la task-uri 786 vs 2.
- **„În lucru"** la mailuri era `status NOT IN ('solved','closed')`, deci amesteca `new` cu
  `in progress`.
- **„În lucru"** la task-uri pierdea orice task deschis mai vechi de 30 de zile (fereastra
  `cts_created_at >= CURRENT_DATE - 30`), exact restanțele care contează cel mai mult.
- Mailurile nu erau filtrate pe direcție — se puteau număra și cele trimise.

Acum stările sunt disjuncte și se numără doar mailurile primite (`cts_direction = 'received'`):
`Soluționate` = închise azi · `În lucru` = `in progress` · `Noi` = `new` (task: `new` + `postponed`),
ultimele două fără limită de vechime. Volumul intrat azi rămâne expus separat ca `intrate_azi` și
alimentează indicatorul „Ritm" (rezolvat azi / intrat azi) — altfel ritmul ieșea absurd
(166 rezolvate / 1 nou).

Valori după fix (grup operațional): mailuri `noi` 7 (era 130), `în lucru` 20, `intrate_azi` 181;
task-uri `noi` 23 (era 100), `în lucru` 20, `intrate_azi` 823.

Scoaterea ferestrei de 30 de zile a scos la iveală o restanță istorică reală la Financiar: 1120
task-uri deschise (769 `new` + 351 `postponed`), din care 309 mai vechi de 30 de zile, unele din
martie. Nu e artefact de numărare — filtrul vechi pur și simplu le ascundea. Ca să rămână lizibil pe
un monitor de perete, cardul afișează acum sub cifră „din care N vechi", iar API-ul expune
`noi_vechi` (mail) și `pending_vechi` (task).

## v0.70.2 - 2026-08-03

### Securitate: suprimare false positive gitleaks pe `.env`

Scan #29 (gitleaks, 26 findings high) — toate pe `.env` live + 3 backup-uri `.env.bak-*`.
Nu e leak în cod/git history — sunt fișiere config pe disk, deja excluse din orice
versionare. Fix: `.gitleaksignore` la root, exclude `.env` și `.env.bak-*` (backup-urile
oricum eliminate progresiv, vezi v0.70.1). Nu s-a rotit niciun secret — nu era nevoie
(fișierele nu erau expuse public, doar semnalate de scanner ca zgomot).

## v0.70.1 - 2026-08-03

### Securitate: validare identificatori SQL în sincronizarea IRIS Data Views

`view_name` venea din path param (`POST /api/v1/iris-dv/views/{view_name}/sync`) și ajungea
neverificat în `DELETE FROM {tbl}` și `CREATE TABLE {tbl}` — `_local_table_name()` înlocuia doar
`-` și `.`, deci `;`, spații și ghilimele treceau. Un admin autentificat putea rula DDL/DML arbitrar
pe baza de date. Endpointul cerea `get_current_admin`, deci nu era exploatabil anonim, dar escalada
„admin de aplicație" la „control total pe DB", ceea ce nu era intenționat.

Fix: `_IDENT_RE = ^[A-Za-z0-9_.-]{1,60}$` aplicat în `_validate_view_name()`, apelat în
`_local_table_name()` și fail-fast în `trigger_sync()` (400 înainte de a porni background task-ul).
Cele trei view-uri reale (`client_contact_email_log`, `employee`, `employee_vacation_request`) trec
validarea neschimbate.

Numele de coloane din răspunsul remote IRIS DV se filtrează acum o singură dată, la citirea din
payload, ca `CREATE TABLE` și `INSERT` să rămână consistente; gardă defensivă păstrată în
`_create_local_table_if_needed()`. Coloanele respinse se logează.

### Securitate: token de feedback nu mai ajunge în loguri

`feedback_public.py` — pixelul de tracking logă tokenul întreg la eroare
(`token=%s`), deci cine citea journald putea deschide formularul în numele clientului.
Acum se logează doar prefixul de 8 caractere, suficient pentru corelare la debug.

### Notă scan semgrep #28

Din 141 findings `high`, 129 sunt false positive: regula `avoid-sqlalchemy-text` e `audit`-tier și
marchează orice `text()`, fără analiză de taint. Codul folosește consecvent fragmente SQL literale
cu valori prin bind params. Cele 14 findings SHA1 sunt tot false positive — SHA1 e folosit exclusiv
ca cheie de cache/dedup, niciodată ca semnătură; migrarea ar invalida cache-urile fără câștig.

## v0.70.0 - 2026-08-03

### Documentație de integrare pentru feed-ul de satisfacție clienți (fereastră + export PDF)

Setări → Conexiune API: buton „Documentație" lângă URL-ul feed-ului de satisfacție, care deschide
`SatisfactionApiModal` — același tipar ca „Informații integrare" al feed-ului CTS (explicații în
fereastră + export imprimabil prin dialogul de tipărire al browserului, „Salvează ca PDF").

Motivul: endpointul avea contract complet, dar nicio explicație pentru cine îl consumă. Întrebările
„cum trec la pagina următoare" și „cum filtrez după CUI" erau răspunse doar în codul sursă, unde
consumatorul extern nu se uită.

**Acoperă:** autentificarea (`X-CTS-Token`, cu Arată/Copiază pe cheia reală), URL-ul, exemplul de
răspuns adnotat câmp cu câmp, tabelul celor 8 câmpuri per client, căutarea unui client anume
(tabel „ce aveți → ce parametru folosiți"), navigarea între pagini cu pseudocod pentru parcurgerea
tuturor celor ~15.7k clienți, cei 11 parametri, codurile de răspuns (200/400/401/429) și pașii
concreți în Postman.

**Două capcane documentate explicit**, fiindcă ambele produc date greșite în silence:
- clienții fără scor apar cu `client_satisfactie: 100` — se deosebesc prin `are_scor_calculat:false`,
  altfel sunt citiți ca „evaluați și perfecți";
- `q` cu valoare numerică poate întoarce MAI MULȚI clienți (e încercată ca CUI, ID intern și ID
  IRIS); pentru un singur client se folosește parametrul dedicat.

**Design** — butonul folosește `Icon id="doc"` (line-style, `stroke="currentColor"`), nu emoji, iar
blocurile de cod au `color: var(--t2)` în loc de hex brut, ca să rămână lizibile pe tema deschisă.
Lint universal: 219 hex / 73 emoji = exact cifrele de dinaintea acestei livrări, zero regresie
introdusă. (Baseline-ul fișierului rămâne nescris — `.claude/` e read-only pentru agent.)

Doar interfață; endpointul și contractul răspunsului sunt neatinse.


## v0.69.3 - 2026-08-03

### Setări → Conexiune API: scos rândul redundant de autentificare pentru satisfacție

Cardul de conexiune afișa două rânduri de autentificare („Autentificare (header): X-CTS-Token" și
„Autentificare satisfacție (header): ..."), deși de la v0.69.2 ambele feed-uri folosesc aceeași
cheie și același header. Al doilea rând nu adăuga informație — sugera că există două mecanisme
de configurat, exact confuzia care a produs 401-ul din v0.69.2.

Rămâne un singur rând de autentificare, plus URL-ul feed-ului de satisfacție. Nota de sub cheie
preia informația explicit („aceeași cheie și același header ca feed-ul de emailuri"), ca ștergerea
rândului să nu lase golul în care header-ul necesar nu mai apare nicăieri în interfață.

Doar interfață. Endpointul acceptă în continuare ambele headere (`X-CTS-Token` și `X-API-Key`) —
contractul nu s-a schimbat, doar afișarea.


## v0.69.2 - 2026-08-03

### Feed satisfacție: acceptă și cheia CTS (`X-CTS-Token`), nu doar `X-API-Key`

`GET /api/v1/ext/clients/satisfaction` întorcea `401 X-API-Key lipsă` pentru cine folosea cheia
afișată în Setări → Conexiune API. Cauză: acolo e afișată cheia **CTS**, iar endpointul cerea o
cheie din tabelul `api_keys` — două mecanisme separate, o singură cheie vizibilă în interfață.
Nicio cheie utilizabilă nu exista în `api_keys` (doar `healthcheck-monitor`, a cărei valoare brută
nu e recuperabilă — se stochează doar hash-ul).

**Fix** (`_verify_any_key`): endpointul acceptă ORICARE dintre cele două headere. `X-API-Key` are
prioritate dacă e prezent; altfel `X-CTS-Token` e comparat cu `settings.cts_feed_api_key` prin
`hmac.compare_digest`. Cheia CTS intră în ACEEAȘI fereastră de rate limit (60 req/min), sub o
identitate proprie derivată prin SHA-256 — altfel ar fi ocolit complet limita.
Mesajul de 401 numește acum ambele variante acceptate.

**Compromis asumat** (decizie Raul Covaci): cheia CTS nu mai poate fi revocată independent de
accesul la datele de satisfacție — rotirea ei rupe simultan feed-ul de emailuri ȘI acest endpoint.
Documentat în docstring, cu instrucțiunea de a emite o cheie dedicată în `api_keys` și de a scoate
ramura CTS dacă e nevoie de revocare separată. Restul endpointului nu depinde de această alegere.

**UI** — cauza reală a confuziei era aranjarea: cele două rânduri de satisfacție stăteau imediat
deasupra câmpului „Cheie (X-CTS-Token)", sugerând că acea cheie le acoperă pe ambele. Acum eticheta
spune explicit `X-CTS-Token (cheia de mai jos) sau X-API-Key`, plus o notă sub cheie că aceeași
valoare deschide și feed-ul de satisfacție.

Nicio schimbare de schemă DB. Contractul răspunsului neatins.


## v0.69.1 - 2026-08-03

### Fix: atașamentele din feed-ul CTS trimit din nou `id_mailguard`

`app/api/v1/cts.py` — `_build_attachment` trimitea ID-ul intern al atașamentului DOAR ca
`id_cargo360`. Numele istoric, pe care CTS îl citește, e `id_mailguard`; redenumirea din
31.07.2026 (rebranding Cargo360) a rupt corelarea atașamentelor în producție.

**De ce a trecut neobservat:** câmpul nu a dispărut, doar și-a schimbat numele. CTS citea o cheie
inexistentă, primea `None` și continua fără eroare — nici 500, nici linie în log. O redenumire de
câmp într-un contract extern nu produce niciun semnal; se vede doar în consumator.

**Fix:** ambele nume, aceeași valoare. `id_mailguard` e contractul principal, `id_cargo360` rămâne
alias. O simplă redenumire inversă ar fi rupt simetric orice consumator adaptat între timp la
`id_cargo360` — de aceea alias, nu înlocuire. Comentariu în cod care cere verificarea consumatorilor
înainte de a scoate vreunul.

Restul obiectului de atașament e neatins (`id` = Graph attachment id, `name`, `contentType`, `size`,
`isInline`, `contentId`, `contentBytes`). Nicio schimbare de schemă DB.

**UI** — Setări → Conexiune API: exemplul JSON și textul de integrare arată acum `id_mailguard` ca
câmp de citit, cu `id_cargo360` marcat drept alias.


## v0.69.0 - 2026-08-03

### Feed extern: satisfacție clienți grupată pe client (medie + istoric lunar)

`GET /api/v1/ext/clients/satisfaction` — endpoint nou, pentru aplicații externe care au nevoie de
gradul de satisfacție per client, cu istoric. Auth `X-API-Key` (tabelul `api_keys`), rate limit
60 req/min per cheie — reutilizate din `satisfaction_api.py`, fără duplicare.

Rămâne SEPARAT de `/ext/v1/satisfaction`, care nu se modifică: acela întoarce rânduri plate
(client × lună), fără nume/CUI și fără medie. Consumatorii lui nu sunt afectați.

**Formă răspuns** — un obiect per client:
`id_client`, `iris_id_client`, `client_nume`, `client_cui`, `client_satisfactie` (media generală),
`are_scor_calculat`, `luni_calculate`, `istoric_satisfactie` (`{"2026-07": 60.7, ...}`).

**Reguli de business** (decise cu Raul Covaci, 2026-08-03):
- Se întorc TOȚI clienții activi (~15.7k), nu doar cei cu snapshot — aplicația externă pornește de
  la lista noastră de clienți, deci un client lipsă din răspuns ar fi indistinct de „client șters".
- Client fără NICIUN snapshot → `client_satisfactie: 100.0`, istoric gol, `are_scor_calculat: false`
  („fără semnal negativ = client mulțumit").
- Media se calculează DOAR peste lunile cu scor real; lunile fără snapshot NU sunt completate cu
  100. Altfel o lună slabă ar fi diluată de luni inexistente, iar media ar crește pe măsură ce
  trece timpul fără să se schimbe nimic în realitate.
- `satisfaction_exclude` (parteneri/furnizori) excluși implicit; `include_exclusi=true` îi readuce.

**Căutare / paginare** — `limit` 1–1000 (implicit 100), `offset`, `total`, `has_more`.
`q` = căutare liberă care acceptă nume parțial SAU identificator numeric: un termen numeric e
încercat simultan ca `cui`, `id` intern și `iris_client_id`, deci consumatorul nu e obligat să știe
ce tip de identificator are în mână. CUI-ul se normalizează la cifre (`RO 12345678` = `12345678`).
Filtre exacte separate: `client_id`, `iris_client_id`, `cui`. Interval istoric: `from_month`/`to_month`.
`doar_cu_scor=true` restrânge la clienții cu cel puțin o lună calculată. `format_luni=nume` schimbă
cheile istoricului în `iulie 2026` (implicit `2026-07`, sortabil).

**Optimizare** — agregarea lunară se face într-un CTE (`page` → `hist`) limitat la clienții din
pagina curentă, nu pe tot tabelul: costul nu crește cu numărul total de clienți. Fără acest CTE,
`jsonb_object_agg` ar rula peste toate snapshot-urile înainte de `LIMIT`.

`migrations/20260803_client_satisfaction_feed_indexes.sql` — indexuri noi, aditive și idempotente:
`pg_trgm` + GIN pe `clients.name` (ILIKE `%text%` nu poate folosi btree), `(name, id)` pentru
ordonare stabilă la paginare, expresie pe CUI normalizat, și index acoperitor
`(client_id, month_key) INCLUDE (satisfaction_pct, ...)` pentru agregare. Nicio schemă modificată.

**UI** — Setări → Conexiune API afișează noul URL și header-ul de autentificare, lângă feed-ul CTS.


## v0.68.8 - 2026-08-03

### Monitor: badge cu iconiță per categorie + spațiere distribuită

Titlurile categoriilor din `MonitorDeptCard` (Mail-uri / Task-uri / Apeluri / Reclamații) devin
badge-uri: iconiță + nume, pe fundal `color-mix` derivat din accentul categoriei, cu bară de accent
în stânga. Iconițele vin din `MonitorIcon` existent (`mail`, `task`, `call`, `alert`) — line-style,
`stroke="currentColor"`, deci fără emoji și adaptate la temă, conform regulilor de design.

Accentul colorează DOAR badge-ul (mail = ambră, task = albastru, apel = verde, reclamații = roșu).
Barele rămân colorate pe **stare** (soluționat verde / în lucru galben / nou albastru), ca lectura
să nu devină ambiguă: culoarea unei bare înseamnă mereu același lucru, indiferent de categorie.

**Spațiere:** containerul barelor primește `justifyContent: 'space-between'`, deci spațiul liber se
distribuie ÎNTRE cele 4 categorii în loc să se adune la finalul cardului. `gap: 10` rămâne ca prag
minim pentru cardurile scunde, unde nu există spațiu de distribuit. Eliminat `overflow: 'hidden'` de
pe acest container — cu `space-between` ar fi tăiat conținutul în loc să lase părintele (care are
deja `overflow: 'auto'`) să deruleze.

## v0.68.7 - 2026-08-03

### Migrație: corecțiile de stare din v0.67.1 / v0.68.6 se propagă acum pe producție

`migrations/20260803_productivity_dup_guard_and_snapshot_reset.sql`

Fixurile de cod nu erau suficiente: două dintre ele depind de STAREA din DB, iar release-ul duce pe
prod doar ce se află în `migrations/`. Comenzile rulate manual pe staging n-ar fi ajuns acolo.

**1) Gard anti-duplicat pentru raportul lunar.** Seed pe `settings`:
`productivity.last_monthly_sent` (luna curentă) și `productivity.last_monthly_sent_at` (acum).
Fără ele, prima rulare de cron după release ar retrimite raportul o dată — cheia lipsește pe prod
exact ca pe staging, fiindcă bug-ul `audit_log(user_id)` împiedicase scrierea ei.
`ON CONFLICT DO NOTHING`: o stare mai nouă decât migrația nu se suprascrie.

**2) Reset snapshot luna în curs.** `DELETE FROM productivity_monthly_snapshot` pentru luna
curentă, ca targetele să se regenereze din logica reparată în v0.67.1 (angajații în concediu la
începutul lunii ieșeau complet din calcul). Snapshot-ul e imutabil prin design, deci codul nou nu
l-ar rescrie singur. Lunile încheiate NU sunt atinse — acolo cifrele sunt deja raportate.

**Idempotență** — `migrate.sh` e `ExecStartPre`, deci rulează la fiecare restart:
- `_release_migrations` sare peste fișierele deja aplicate;
- în plus, gardă proprie `productivity.snapshot_reset_20260803` în `settings`: dacă fișierul ar fi
  reaplicat într-o lună ulterioară, un DELETE necondiționat ar șterge targetele valide ale acelei
  luni. A doua rulare raportează „deja aplicat, sar peste".

Testat pe staging: rulare directă → exit 0; a doua rulare → sare peste, cheile originale intacte;
restart → `migrate.sh` raportează „aplicate: 1, deja prezente: 114", serviciul pornește curat.

Nicio schimbare de schemă (fără tabele/coloane/indexuri noi) — doar date în `settings` și curățare
în `productivity_monthly_snapshot`.

## v0.68.6 - 2026-08-03

### Fix critic: raportul lunar de productivitate se trimitea repetat (5 emailuri duplicate)

**Lanțul cauzal.** `INSERT INTO audit_log(action, user_id, ...)` — coloana se numește **`actor`**,
nu `user_id` (`\d audit_log`). Inserția arunca `psycopg2.errors.SyntaxError`, care lăsa tranzacția
**abortată**. `_mark_sent(db)` rula imediat după, pe aceeași sesiune, și eșua în cascadă — deci
KV-ul `productivity.last_monthly_sent` nu se scria **niciodată**. Gating-ul din
`send_monthly_reports_if_due` nu avea ce citi, iar cron-ul (`process_now`, la 5 min) retrimitea
raportul la fiecare rulare. Confirmat pe staging: emailurile plecau („productivity report sent"),
dar `audit_log` era gol (0 rânduri) și cheia lipsea din `settings`.

**Fix:**
- `user_id` → `actor` în inserția de audit.
- `db.rollback()` explicit în `except` (audit ȘI grup) — o eroare secundară nu mai poate lăsa
  sesiunea abortată și bloca singura protecție anti-duplicat.
- `_mark_sent` apelat doar dacă `sent > 0`, dar necondiționat de eșecul unui grup: altfel cron-ul
  reia la 5 min și inundă destinatarii grupurilor care AU reușit.
- **Nou `productivity.last_monthly_sent_at`** (timestamp) + `_recently_sent(min_days=25)` ca a
  treia poartă în `send_monthly_reports_if_due`. Plasă de siguranță independentă de eticheta de
  lună: chiar dacă aceasta lipsește sau e coruptă, momentul ultimei trimiteri oprește retrimiterea.
- `logger.warning(..., exc_info=True)` pe audit — eroarea era înghițită fără urmă în log.

**Acțiune pe staging:** `productivity.last_monthly_sent = "2026-08"` și `last_monthly_sent_at = now()`
scrise manual, ca să opreasca pe loc al 6-lea email înainte de următorul ciclu de cron. Verificat
după restart: 0 erori, 0 trimiteri.

Restul inserțiilor în `audit_log` din codebase (auth, spam, emails, settings) foloseau deja `actor`
— bug-ul era izolat în `productivity_notifier`.

### Monitor: gauge de la 82% la 90%

`radiusScale` 0.82 → 0.90 în `MonitorDeptCard` — la 0.82 eticheta se vedea întreagă, dar gauge-ul
era prea mic.

## v0.68.5 - 2026-08-03

### Fix real: eticheta de prag tăiată sus pe gauge-urile din cardurile de monitor

Încercările din v0.68.3/v0.68.4 (padding + `minHeight` pe containerul exterior) nu au rezolvat
nimic, fiindcă atacau locul greșit. Cauza reală, din `gauge.min.js`:

1. Raza arcului se calculează din ÎNĂLȚIMEA canvas-ului: `radius = availableHeight - lineWidth/2`,
   unde `availableHeight = canvas.height * (1 - paddingTop - paddingBottom)` și `paddingTop` e
   implicit **0.1** (10% rezervă).
2. `renderStaticLabels` desenează eticheta **rotită**, la raza arcului:
   `ctx.rotate(angle); ctx.fillText(txt, 0, -s - lineWidth/2)` — adică ÎN AFARA arcului.

Când un prag cade aproape de vârful arcului (~40–60% din scală; Suport 3 are minim 44.8%), acel
punct depășește rezerva de 10% și textul e tăiat de marginea **canvas-ului**. Padding sau height pe
containerul exterior nu pot ajuta: tăierea se produce în interiorul canvas-ului.

**Fix.** `ProdGauge` primește două prop-uri OPȚIONALE, `radiusScale` și `height`. Micșorarea arcului
(`radiusScale`) e singura pârghie care eliberează spațiu pentru etichete în interiorul canvas-ului.
`MonitorDeptCard` trimite `radiusScale: 0.82, height: 210` (înălțimea compensează arcul mai mic).

Pagina Rapoarte nu trimite niciun prop nou → primește implicit `1.0` / `200px`, deci rămâne
pixel-identică. Ambele prop-uri adăugate în lista de dependențe a `useEffect`, altfel gauge-ul nu
s-ar recrea la schimbarea lor.

## v0.68.4 - 2026-08-03

### Monitor: reclamații cu „Deschise", header card mai mare, gauge netăiat

**Reclamații — „0 primite / 1 rezolvată" NU e un bug.** Semnalat pe Suport 2, 03.08. Cauza: cele
două contoare sunt seturi diferite, nu un flux. Reclamația (email `66619023`) a fost primită
01.08 21:33 și rezolvată 03.08 06:30 — deci `primite_azi=0` (n-a sosit azi) și `rezolvate_azi=1`
(s-a închis azi), ambele corecte. Regula „primite ≥ rezolvate" ar ține doar dacă totul s-ar
rezolva în ziua sosirii, ce nu se întâmplă la reclamații.

- Adăugat `reclamatii.deschise` în `per_dept` + a treia bară **„Deschise"** (galben) — ancora care
  dă sens celorlalte două. A scos la iveală 10 reclamații deschise pe Taxe de drum, informație
  care nu se vedea nicăieni.
- `primite_azi` are acum fallback pe `emails.received_at` când `raw->extra->created_at` lipsește
  (nu schimbă cifrele curente — toate rândurile aveau valoarea — dar previne subraportarea).

**UI:**
- Header card (Suport 1/2/3, Taxe de drum): 11 → 15px, `fontWeight` 800, padding mărit.
- Eliminat rândul text „minim X% · țintă Y%" de sub gauge (pragurile se citesc de pe arc).
- Gauge: wrapper 240 → 250px, `paddingTop` 10 → 22, `minHeight: 232` — la un prag jos (Suport 3:
  44.8%) eticheta cădea sus pe arc și intra sub marginea cardului, fiindcă containerul intern al
  lui `ProdGauge` e fix la 200px cu `overflow:hidden`.

## v0.68.3 - 2026-08-03

### Monitor: gauge netăiat, tipografie, rezumat per departament

**Gauge tăiat sus — cauză.** `ProdGauge` are wrapper propriu cu `height:200; overflow:hidden`.
Canvas-ul se scalează după lățime, dar containerul rămâne fix la 200px: într-un card îngust
eticheta de prag desenată în partea de sus a arcului (un prag de ~40–60% cade exact acolo) intra
sub marginea cardului. Rezolvat în `MonitorDeptCard`, nu în `ProdGauge` — acesta e folosit și de
pagina Rapoarte: lățime 190 → 240px + `paddingTop: 10`.

**Tipografie.** Etichete de stare 11.5 → 13.5px, `fontWeight` 600, lățime coloană 76 → 92px.
Capitalizare: „soluționate/în lucru/noi" → „Soluționate/În lucru/Noi" (idem „Total azi",
„Primite azi", „Rezolvate azi"). Titlurile de categorie (Mail-uri / Task-uri / Apeluri /
Reclamații) primesc `paddingTop: 8`.

**Rezumat nou, sub bare** — umple spațiul gol rămas, fără cereri suplimentare (totul derivat din
datele deja aduse): `Soluționat azi` (mail + task + apel), `Deschis acum` (mail în lucru + task în
progres), `Rezolvat / intrat` (%).

Ultimul e plafonat la 999% și verde la orice ≥100%: raportul brut explodează când se lichidează
restanțe din zilele trecute (Suport 3 a ieșit 1400% pe 03.08, Taxe de drum 463%), iar mesajul util
pe un monitor de perete e „ține pasul / nu ține pasul", nu cifra exactă.

## v0.68.2 - 2026-08-03

### Monitor Productivitate: volumul per departament devine conținutul principal

**Eliminate** (redundante față de cardurile per departament introduse în v0.68.0):
- cardul „Obiectiv lunar — realizat vs țintă · ziua N/21" (`MonitorGaugeKm` × departament) — același
  gauge există acum în fiecare card de departament;
- rândul de canale `MonitorChannelCard` (Mail / Apel / Task / Device ops) — arăta totaluri de GRUP,
  nu volum per departament.

Layout-ul rămâne: contoare KPI → carduri per departament. Componentele `MonitorGaugeKm` și
`MonitorChannelCard` rămân definite, dar nefolosite.

**Bare per card — culoare pe STARE, nu pe categorie.** Soluționate = verde (`--gn`), în lucru =
galben (`--yw`), noi = albastru (`--bl`). Aceeași stare are aceeași culoare peste toate categoriile.
La Reclamații: primite = albastru (intrare), rezolvate = verde.

**Lizibilitate:** cifre 11 → 19px (colorate în culoarea stării), etichete 9.5 → 11.5px, titluri de
categorie 9.5 → 12px, înălțime bară 9 → 14px, spațiere între categorii 5 → 12px. Gauge redus la
190px ca barele să domine cardul.

**Bara la valoare 0** rămâne golă (fără ciot colorat), dar cifra `0` e afișată mereu în gri — un
departament fără apeluri azi trebuie să arate `0`, nu spațiu gol. (Suport 2 și Suport 3 au real 0
apeluri azi; verificat în DB — nu e defect de calcul.)

## v0.68.0 - 2026-08-03

### Monitor Productivitate (Operațional / Financiar): carduri per departament + rearanjare

**Date — `per_dept` extins în `/api/v1/productivity/monitor/live`.** Conținea doar emailuri
(`rezolvate_azi`, `in_lucru`), deci un card per departament era imposibil de construit fără să
repete totalul grupului. Adăugat aditiv, per departament:

- `emailuri`: rezolvate_azi / in_lucru / noi_azi
- `taskuri`: rezolvate_azi / in_progress / noi_azi
- `apeluri`: azi / rezolvate_azi
- `reclamatii`: primite_azi / rezolvate_azi (categorie `reclamatie` din `coalesce(cts_category, ai_category)`)

Fiecare interogare refolosește EXACT join-urile, filtrele și convențiile de timp ale agregatelor de
grup, deci suma cardurilor dă totalul din contoarele de sus. Verificat pe staging: operațional
mail 98 = 98, task 418 = 418, apel 22 = 22. Cheile vechi (`rezolvate_azi`, `in_lucru`) păstrate la
rădăcina fiecărui element pentru compatibilitate.

**UI — `MonitorDeptCard`.** Un card per departament (4 pe rând pe Operațional, 3 pe Financiar):

- Gauge-ul din pagina Rapoarte (`ProdGauge` / gauge.js) — același stil, aceleași markere de prag
  pe arc — plus `minim %` și `țintă %` scrise numeric dedesubt.
- Bare ORIZONTALE cu volumul de azi: Mail 3 bare (soluționate / în lucru / noi), Task 3 bare
  (idem), Apeluri 1 bară (total azi), Reclamații 2 bare (primite / rezolvate azi). Scala e comună
  pe card (maximul local), altfel o categorie cu volum mare ar strivi restul.

**Layout.** „Obiectiv lunar — realizat vs țintă" mutat imediat sub cardurile KPI și micșorat
(`flex` 1.55 → 0.85) — era cea mai mare felie din pagină. Ordinea: KPI → obiectiv lunar → carduri
per departament → canale live.

## v0.67.3 - 2026-08-03

### Gauge Rapoarte: doar pragul minim scris pe arc, mărime 20px

Continuare la v0.67.2. La o mărime lizibilă cele două etichete de prag se suprapuneau și ieșeau din
grafic — minimul și realul sunt adesea foarte apropiate (ex. Suport 1: 77.9 / 82.9).

- `staticLabels.font`: `34px` → `20px` (tot ~2× cât se vedea înainte de v0.67.2, când fontul invalid
  era ignorat și se desena implicitul ~10px).
- `staticLabels.labels`: `[safeMin, safeMax]` → `[safeMin]` — pe arc rămâne scris doar pragul minim.

Neatinse: markerele colorate de pe arc (galben = minim, verde = real), culoarea gauge-ului, acul,
gradațiile, badge-ul cu valoarea și textul de status de sub grafic. Regenerat `mg-app.js.gz`.

## v0.67.2 - 2026-08-03

### Fix: etichetele de prag (minim/real) de pe gauge-urile din Productivitate → Rapoarte erau ~10px

Pragurile scrise direct pe grafic (ex. Suport 1: 77.9 / 82.9) apăreau mult mai mici decât cei 28px
configurați în `ProdGauge` (`app/ui/vendor/mg-app.js`).

**Cauză.** `gauge.js` parsează mărimea fontului din `staticLabels.font` cu `/\d+\.?\d?/`, apoi
reconstruiește șirul ca `parseFloat(r)*displayScale + n.slice(r.length)`. `slice` taie de la
**începutul** șirului, nu de după cifre — cu `'bold 28px Inter,...'` (`r.length === 2`) rezulta
fontul invalid `'28ld 28px Inter,...'`, pe care canvas-ul ignoră, păstrând implicitul ~10px.
Creșterea numărului nu avea deci niciun efect: mărimea trebuie să fie primul token din șir.

**Fix.** `staticLabels.font` devine `'34px Inter,system-ui,sans-serif'` — fără prefix, deci mărimea
se aplică efectiv. `bold` a fost eliminat intenționat (biblioteca nu îl suportă în acest câmp);
îngroșarea ar necesita desenarea etichetelor separat peste canvas.

Doar acest câmp a fost modificat — restul gauge-ului (zone, pointer, ticks) și etichetele de SUB
grafic sunt neatinse. Regenerat `mg-app.js.gz` după modificare.

## v0.67.1 - 2026-08-03

### Fix: angajații în concediu la începutul lunii dispăreau din Productivitate → Rapoarte

**Simptom.** Pe 03.08.2026, la Suport 1 lipseau Negrescu Elena și la Suport 2 Kovacs Robert — atât
din tabelul Operatori, cât și din Ore planificate / Ore disponibile. Suport 1 arăta 840h (5 oameni)
în loc de 1008h (6 × 21 × 8), Suport 2 arăta 672h (4 oameni) în loc de 840h (5 × 21 × 8).

**Cauză 1 — filtru de activitate greșit.** `department_report` considera „operator activ în lună"
doar pe cine avea cel puțin o zi `present=true` în pontaj. Pe 3 august pontajul avea doar 2 zile
(01–03.08), iar cei doi erau exact atunci în concediu (Negrescu 31.07–03.08, Kovacs 03.08–14.08),
deci cu 0 zile prezente. Un om în concediu nu e inactiv: concediul trebuie scăzut din orele
disponibile, nu să-l elimine din orele planificate și din listă.

**Cauză 2 — snapshot fixat prea devreme.** Snapshot-ul lunar e imutabil prin design. Fiind salvat
în dimineața zilei 3, a înghețat cifrele calculate pe pontaj incomplet, așa că eroarea persista
tot restul lunii chiar după ce pontajul se completa.

**Fix.**
- Operator activ = pontaj prezent **SAU** absență în pontaj **SAU** concediu înregistrat în lună
  (`employee_schedule` + fallback `cts_dv_employee_vacation_request` status 1/2) **SAU** zile de
  lucru pe proiecte/refurbished declarate pe lună. Rămân excluși doar cei fără nicio urmă în lună
  (nemapați în pontaj, angajați cu `productivity_start_date` în viitor).
- `_snapshot_too_early()`: snapshot-ul lunii în curs nu se mai fixează până nu au trecut cel puțin
  5 zile lucrătoare (`_SNAPSHOT_MIN_ELAPSED_WD`). Sub prag estimarea se recalculează LIVE la fiecare
  accesare, exact ca o lună viitoare, deci reflectă imediat concediile și pontajul care intră pe
  parcurs. Aplicat în ambele ramuri: `department_report` și `forecast_report`.
- Snapshot-urile august 2026 pentru suport_1/2/3 au fost șterse și recalculate.

## v0.67.0 - 2026-07-31

### Rebranding: MailGuard → Cargo360
Numele produsului se schimbă în **Cargo360**. Înlocuit în tot ce e vizibil pentru utilizator și în
documentație: 348 de înlocuiri în 84 de fișiere (UI, titlu pagină, cod, `CHANGELOG.md`, `CLAUDE.md`,
`docs/`, migrații, scripturi).

- **UI**: badge-ul din meniul lateral `MG` → **`C360`**, titlul `MailGuard` → `Cargo360`, iniţialele
  din favicon `MG` → `C360` (font redus la 10px ca 4 caractere să încapă în 36px). Versiunea afișată
  în meniu era hardcodată la `v0.13.1` — corectată la versiunea reală.
- **Nume serviciu**: `NordLogistics MailGuard` → `NordLogistics Cargo360`, în `app/config.py` **și** în
  `APP_NAME` din `.env` (variabila de mediu suprascrie valoarea din cod — altfel `/api/v1/health` ar fi
  continuat să raporteze numele vechi). `/healthz` întoarce acum `cargo360-v0.67.0`.

### Identificatori de infrastructură PĂSTRAȚI intenționat
Nu sunt „nume de produs", sunt identificatori reali de care depinde funcționarea. Redenumirea lor ar
opri aplicația, nu ar rebranda-o:

| Păstrat | De ce |
|---|---|
| `IRIS_MAILGUARD_API_KEY`, `X-Mailguard-Key` | Cheia + header-ul validate de IRIS Gateway. Redenumite = acces pierdut la CTS (ground-truth, apeluri, task-uri, angajați) |
| `db_name`/`db_user` = `mailguard` | Baza de date și utilizatorul există fizic cu acest nume |
| `mailguard-api`, `mailguard-db`, `mailguard-cron` | Servicii systemd / containere Docker existente |
| `/opt/iris-mailguard`, alias SSH `mailguard-staging` | Căi și acces pe server |
| `MAILGUARD_ENV`, `MAILGUARD_NATIVE_INGEST`, `MAILGUARD_SIDE_EFFECTS` | Variabile de mediu citite din `.env` |
| `getLogger("mailguard.*")` (79 de locuri) | Nume de loggere — schimbate ar rupe filtrele existente pe jurnale |
| `mg-app.js`, `mg-badge`, clasele CSS `mg-*` | Nume de fișier și de clase; redenumirea cere modificări corelate în HTML/CSS, fără câștig vizibil |

Redenumirea acestora e o migrație de infrastructură separată (bază de date, systemd, căi, cheie IRIS),
nu parte din rebranding — de făcut deliberat, cu fereastră de indisponibilitate.

### Verificat după rebranding
- Sintaxă: `compileall` pe tot `app/` + `scripts/`, `node --check` pe UI — curate.
- Toți identificatorii de infrastructură prezenți și după (`IRIS_MAILGUARD_API_KEY` 37×,
  `X-Mailguard-Key` 30×, `/opt/iris-mailguard` 93×, loggere 79×); cheile din `.env` identice
  înainte/după (`sed` a atins doar valorile de text, nu numele variabilelor).
- **Autentificarea la IRIS funcționează**: sincronizări reale după restart — mailuri 7.174 actualizate,
  apeluri 264 (262 potrivite local). Dovada că header-ul și cheia n-au fost atinse.
- Design: zero regresii (289 hex, 165 emoji — identic înainte/după).
- Backup: `/tmp/pre_rebrand_20260731_163755.tar.gz` (19 MB) + `.env.bak-rebrand-*` pe server.

## v0.66.1 - 2026-07-31

### `_populate_iris_ids` — mapare nedeterministă care rescria zilnic (raportat de pe producție)
- **Problemă**: angajații reangajați au 2-3 fișe în `cts_dv_employee` cu ACELAȘI email și `id`
  diferit (contracte succesive). `UPDATE employee_department_mapping ... FROM cts_dv_employee` cu
  mai multe rânduri potrivite pentru același `e.id` scrie unul **arbitrar**, fără eroare. În plus
  condiția `e.iris_id != dv.id` rămâne mereu satisfăcută cât timp există fișe multiple (celelalte
  fișe „contrazic" orice valoare pusă) → **nu converge**, rescrie la fiecare rulare.
- **Reprodus pe staging** (3 rulări consecutive pe date identice, în `ROLLBACK`): `UPDATE 9`,
  `UPDATE 7`, `UPDATE 7`. O funcție corectă dă `0` la a doua. 9 angajați au contracte multiple pe
  staging, deci bug-ul era activ aici, nu doar pe prod.
- **Efect**: angajați cu concedii 2026 ajungeau pe fișa fără concedii (pe prod: Popa Andreea 9→0,
  Vlad Cosmin 5→0) — adică fix-ul din v0.64.0, reintrodus zilnic prin altă cale.
- **Fix**: `DISTINCT ON (edm.id)` garantează un rând per angajat, iar `ORDER BY` alege **contractul
  activ** (`contract_termination_date` gol) înaintea celui mai recent angajat — coloană care exista
  în `cts_dv_employee` dar nu era folosită. Match pe email, fără ID-uri hardcodate → comportament
  identic pe staging și prod, deși ID-urile fișelor diferă între medii.
- **Verificat pe staging, prin funcția reală din cod** (nu doar SQL): 3 rulări → `2, 0, 0`
  (convergență). Concedii 2026 vizibile prin mapare: **264 → 270** (+6, nimeni nu pierde). Cele 2
  mapări schimbate câștigă amândouă: Groza Tudor Nicolae 118→37 (0→1 concedii), Vid Alexandru
  32→31 (0→5). Sincronizarea reală `run_vacation_dv_sync_if_due` rulată de două ori consecutiv:
  `iris_ids_updated: 0` la ambele.
- Backup înainte de aplicare: `edm_iris_id_backup_20260731` (56 rânduri).
- Notă: filtrul nou `edm.enabled = true` nu afectează cei deja mapați — pe staging există 1 angajat
  dezactivat cu `iris_id` setat, valoarea îi rămâne, iar `sync_vacation_from_dv` filtrează oricum
  pe `enabled = true`.

## v0.66.0 - 2026-07-31

### Operațiuni dispozitive — sincronizarea rula DOAR manual (pagina rămânea înghețată)
- **Problemă raportată**: pagina „Device Operations" stă la „Ultima sincronizare 10:43, 31.07.2026",
  pe staging și pe producție.
- **Cauză**: niciunul din cele două module `device_ops` nu era în blocul de cron din
  `emails.py` (`POST /process/run-now`), deși mailurile, apelurile, task-urile CTS și pontajul erau.
  `device_ops_sync.run_recent_if_due()` exista dar nu era apelat de nimeni, iar sursa DV nu avea
  deloc o funcție de cron. Cele două date vizibile erau ultimele apăsări de buton:
  `device_ops.last_recent_sync_at` = 30 iulie 10:00, `device_ops_dv.last_recent_sync_at` =
  31 iulie 07:43 UTC (= **10:43 local**, exact ora din interfață).
- **Fix**: `device_ops_suport2_sync.run_recent_if_due()` + înregistrat în cron. Throttle 1h (nu 5 min):
  sync-ul e `TRUNCATE` + repopulare completă, cu 70.069 de rânduri citite din view — nu incremental.

### Operațiuni dispozitive — 1.805 operațiuni lipseau din cauza ordinii numelor
- **Descoperit** în timpul verificării fix-ului de mai sus: `Closed by` din view e „Robert Kovacs",
  iar `employee_department_mapping` are „Kovacs Robert". Egalitatea exactă potrivea **1 din 7**
  angajați Suport 2/3 (doar Baican Emanuel-Crinel, singurul cu aceeași ordine în ambele surse).
- Numele compuse erau scrise și parțial: „David Miclau" vs „Miclau Adrian-David", „Ovidiu Ticus" vs
  „Ticus Ovidiu Alexandru", „Robert Iova" vs „Iova Oliviu-Robert".
- **Fix**: potrivire pe mulțimea de cuvinte, cu cratima ca separator și test de incluziune (toate
  cuvintele din `Closed by` trebuie să existe în numele angajatului) în loc de egalitate. Ambiguitatea
  întoarce `None` — o operațiune pusă greșit în contul cuiva ar intra în calculul de productivitate.
- Rezultat: **1.808 operațiuni** în baza de date, față de 3. Toate cele 6 tipuri expuse de view revenite
  (instalare_noua 1061, mutare 353, interventie 162, demontare 108, calibrare 76, periferice 48).

### Gardă anti-golire pe sincronizarea de operațiuni
- Sync-ul face `TRUNCATE` înainte de repopulare, deci o regresie în filtrare nu doar aduce mai puțin —
  **șterge ce era bun**. S-a întâmplat în timpul acestei sesiuni: filtrarea a picat la 3 rânduri și
  `TRUNCATE`-ul a golit tabela (restaurată din sursă după fix).
- Dacă setul nou e sub 50% din cel existent (și existentul e ≥20 rânduri), sync-ul se abandonează cu
  `rollback`, datele rămân neatinse și mesajul spune ce să verifici. Testat cu regresie simulată:
  1808 rânduri înainte → 1808 după.

### Tipuri de operațiune nemapate — nu se mai pierd silențios
- Un `Operation Type` necunoscut era ignorat fără nicio urmă (`continue`). Acum rezultatul sync-ului
  întoarce `unmapped_types` (tipuri eligibile pe care maparea nu le cunoaște) și `missing_types`
  (tipuri cunoscute care au ajuns cu 0 rânduri), ambele și în log ca WARNING.

### „Înlocuire" NU există în sursa nouă — necesită extinderea view-ului la sursă
- **Verificat exhaustiv** (`view_device_operations`): `Operation Type` are exact **6** valori distincte
  pe toate cele 70.069 de rânduri, niciuna o înlocuire. Nu există coloană care să le distingă
  (21 `output_columns`) și nici view alternativ (`view_device_replacements` / `_replacement` /
  `_replacements` / `_operations_all` / `_ops` → toate 404). „replacement"/„înlocuire" apare doar ca
  **text liber** în `Notes`/`Description`.
- Cele **202** înlocuiri finalizate după cutoff există DOAR în sursa veche `/cts/device-operations`,
  ca `action_type='inlocuire'` cu `operation_id` prefixat `RP-` (218 din 1 iulie). `_row_id` din view
  nu are prefixe → nu se pot corela nici prin id. Corelarea pe dispozitiv a găsit 26 din 202, iar
  acelea apar în view ca Device Move / New Installation / Calibration → nici reclasificarea nu e o
  cale corectă. Confirmare independentă: `device_operations_backup_20260730` (din sursa veche) are
  122 de înlocuiri; au dispărut exact la trecerea pe view-ul nou.
- **Deci nu e reparabil din Cargo360**: view-ul `cts_views.view_device_operations` trebuie extins la
  sursă. Maparea acceptă deja în avans „Device Replacement" / „Device Replace" / „Device Exchange" /
  „Device Swap" → `inlocuire`, ca preluarea să funcționeze fără redeploy când apar. Până atunci,
  `missing_types: ['inlocuire']` apare la fiecare rulare.

## v0.65.0 - 2026-07-31

### Filtru după dată pe pagina Apeluri CTS
- `GET /cts-calls-training/list` acceptă `date_from` / `date_to` (`YYYY-MM-DD`, inclusiv la ambele
  capete); format greșit → 400, nu 500.
- Filtrarea se face pe **data apelului**, nu pe data actualizării fișei în CTS — utilizatorul caută
  „apelurile de ieri", nu „fișele modificate ieri".
- Ziua se compară pe ora `Europe/Bucharest`, cu `COALESCE(calls.started_at::date,
  (cts_started_at AT TIME ZONE 'Europe/Bucharest')::date)`. `calls.started_at` (While1, ora locală)
  e sursa preferată pentru ora apelului; `cts_started_at` (timestamptz UTC) e doar fallback pentru
  apelurile care există în CTS fără corespondent în centrală. Fără conversie, apelurile de după
  21:00 ar cădea în ziua calendaristică următoare (decalaj 3h vara).
- UI: două câmpuri de dată + scurtături „Azi" / „7 zile" / „Șterge datele" în bara de filtre.

### Indicator de prospețime — separă „Cargo360 nu a sincronizat" de „CTS nu a mai scris nimic"
- **Problemă raportată**: „ultimul apel adus din CTS e la 13:08, ar trebui la 5-10 minute";
  utilizatorii sesizau apeluri lipsă sau nesincronizate.
- **Diagnostic (verificat pe sursă, nu presupus)**: sincronizarea rula corect la fiecare 5 minute și
  aducea **100% din ce exista**. Sursa CTS se oprise: zero înregistrări cu `updated_at` după
  13:09:29, în orice fereastră interogată (24h, 48h, limită 20.000 — deci nu plafonare, nu `since`).
  Între timp centrala înregistra apeluri reale: 358 din 432 de apeluri din ultimele 2h fără fișă CTS.
  Blocajul e **în amonte**, la momentul în care operatorul deschide/închide fișa în CTS.
- `GET /cts-calls-training/stats` întoarce `freshness`: `last_sync_at`, `last_cts_call_at`,
  `last_while1_call_at`, `lag_minutes`, `while1_last_2h`, `while1_last_2h_without_cts`.
- Banner în pagină peste 45 min de decalaj (roșu peste 90), care spune explicit că Cargo360
  verifică la 5 min și aduce tot ce există. Fără el, un blocaj în amonte arată identic cu un bug
  de sincronizare — exact confuzia care a generat raportarea.

### Aliniere fus orar la interogarea sursei CTS
- `/cts/calls` compară `since` **literal** cu `updated_at`, iar `updated_at` e scris în ora locală
  România — în timp ce `started_at` din **același** payload e UTC (verificat pe apelul 720260: CTS
  `started_at`=10:08 UTC vs While1 `started_at`=13:08 local, `updated_at`=13:09).
- `sync_recent()` calcula `since` cu `utcnow()`, deci cerea o fereastră deplasată cu 3h în trecut.
  Inofensiv pentru pierderi (fereastra ieșea mai *largă*: 27h efectiv în loc de 24h), dar
  `window_hours` mințea și ar fi devenit periculos la o fereastră mai mică decât decalajul.
- Helper `_source_now()` (`Europe/Bucharest`) — fereastra de 24h e acum exact 24h. Păstrată la 24h.

### Verificat și lăsat neatins, cu motiv
- **Nepotrivirile CTS ↔ centrală nu sunt o problemă de cod**: 98,6–100% potrivire pe ultimele 12 zile
  (ultima rulare: 245/247). Cele 5.499 nepotrivite sunt istorice — 4.991 din iunie și 300 din 2020,
  perioade în care nu există **niciun** apel While1 în bază (primul e 1 iulie). Zero din ele s-ar
  potrivi acum, pe niciuna din cele două chei.
- Cele 197 din iulie: 66 fără `ctk_uniqueid`, iar din cele 131 cu cheie validă doar 3 au vreun apel
  While1 în ±90s — și acelea ambiguu (mai mulți candidați, `uniqueid` diferit). Legarea pe proximitate
  temporală ar produce atribuiri greșite într-un calcul de satisfacție/productivitate, deci **nu** a
  fost implementată: mai bine nelegat decât legat greșit (aceeași regulă ca la `match_client_by_phone`).

## v0.64.0 - 2026-07-31

### Estimarea lunilor viitoare se recalculează în timp real
- **Problemă**: snapshot-ul lunar se fixa la PRIMA accesare a unei luni, inclusiv pentru luni care
  nu începuseră. Un concediu adăugat ulterior, zile de lucru pe proiecte sau un angajat cu
  `productivity_start_date` în viitor nu se mai reflectau în ore planificate/disponibile/coeficient.
- **Fix**: helper `_is_future_month()` în `app/services/productivity.py`. Pentru lunile care nu au
  început, snapshot-ul NU se citește și NU se scrie — estimarea se recalculează din intrările
  actuale la fiecare accesare. Snapshot-ul devine imutabil din luna în curs încolo, ca un target
  deja emis să nu se schimbe retroactiv. Aplicat în ambele căi de calcul (raport și estimare).
- Șterse cele 18 snapshot-uri deja fixate pe luni viitoare (august–octombrie 2026), salvate în
  `productivity_snapshot_backup_20260731`.

### Buton „Recalculează estimarea"
- `POST /productivity/recalculate?month=YYYY-MM[&department=]` — șterge snapshot-ul lunii și îl
  regenerează. Respins cu 400 pe luni încheiate: acolo cifrele sunt deja raportate.
- Buton în bara de sus a paginii Productivitate, lângă „Exportă raport", cu confirmare și
  reîmprospătare automată a raportului. Ascuns pe lunile trecute.
- Utile mai ales pe luna ÎN CURS, unde snapshot-ul e fixat; lunile viitoare se recalculează oricum.

### Concedii — sursă unică DV, fără duplicate „ÎNVOIRE"
- **Problemă**: aceeași cerere de concediu venea pe două canale — din DV
  (`cts_dv_employee_vacation_request`, scrisă `vacation_approved`) și din payload-ul IRIS
  (`leave_requests[]`, scrisă `leave_request` → afișată „ÎNVOIRE"). 43 din 48 de intrări erau
  duplicat exact al unui concediu DV; restul 5 corespundeau unor cereri cu status DV 4
  (anulate/respinse), deci nu trebuiau să blocheze zile. Cererile neaprobate apăreau DOAR ca
  „învoire în așteptare", niciodată ca „concediu în așteptare".
- **Cauză de fond**: `iris_id` era NULL pentru TOȚI cei 55 de angajați activi, deci
  `sync_vacation_from_dv()` ieșea devreme și nu scria niciodată nimic — cele 209 concedii din bază
  erau istorice. Fără maparea asta, DV nu putea fi sursă unică.
- **Fix**: `iris_id` populat pentru toți cei 55, pe adresa de email, alegând contractul CTS cel mai
  recent la cei 9 angajați cu mai multe fișe (verificat: acolo sunt concediile curente).
  `sync_vacation_from_dv()` importă acum și `status=1` (în așteptare) pe lângă `status=2` (aprobat),
  cu `status='pending'`/`'approved'` în `employee_schedule`; statusurile 3/4 rămân excluse; limita
  `period_begin >= 2026-01-01` păstrată. `_iter_leaves()` dezactivat — concediile vin exclusiv din
  DV. 42 de concedii importate (7 în așteptare), cele 48 de `leave_request` din CTS șterse.
  Intrările manuale neatinse. Backup complet în `employee_schedule_full_backup_20260731`.
- Concediile în așteptare intră automat în calculul de productivitate: filtrul existent e pe
  `kind='vacation_approved'`, fără condiție de status.

### Extensia PostgreSQL `unaccent` instalată
- Lipsea complet din baza de staging (doar `plpgsql` era instalat). Fix-ul din v0.62.3
  (`device_ops_suport2_sync._resolve_employee_by_name`) o folosește și ar fi eșuat la prima rulare
  a sincronizării — inclusiv pe prod după release. `CREATE EXTENSION IF NOT EXISTS unaccent`.
- **De verificat pe prod înainte de release**: `SELECT extname FROM pg_extension`.

### Zilele de lucru pe proiecte nu mai apar în tabelul de concedii
- `project_work` / `refurbished` (`entry_source='manual_extra'`) apăreau în lista de concedii a
  angajatului ca „— – —" (nu au `start_date`/`end_date`, doar lună + număr de zile) și umflau
  contorul de concedii din lista de utilizatori. Excluse din ambele.
- Se scad în continuare din orele disponibile, la fel ca un concediu — comportament neschimbat.

## v0.63.1 - 2026-07-31

### Productivitate — task-urile numără doar `solved` (fără `closed`)
- Cele 4 interogări de task-uri din `app/services/productivity.py` treceau `status IN
  ('solved','closed')`. Acum doar `= 'solved'`: `closed` în CTS înseamnă „închis FĂRĂ rezolvare" și
  nu e muncă finalizată. Regula per sursă, confirmată: **task-uri → doar `solved`**,
  **device operations → doar `closed`** (acolo `closed` ESTE starea de finalizare pentru Suport 2).
- Baza de lună rămâne **data soluționării**, pe toate sursele — Productivitate măsoară munca
  finalizată în lună, nu pe cea intrată:
  - task-uri: `cts_updated_at`
  - mailuri: `cts_solved_at` (respectiv `cts_solved_seen_at`/`cts_reply_at`)
  - apeluri: `cts_started_at` — apelul se preia și se rezolvă în aceeași conversație, nu există o
    dată de soluționare separată
  - device operations: `closed_at`
- **Notă de interpretare**: pagina Task-uri filtrează pe data creării, Productivitate pe data
  soluționării — deci cifrele diferă intenționat. Pentru Pop Adelina, iulie: 171 create-și-rezolvate
  vs 195 rezolvate în lună (24 dintre ele primite în lunile anterioare). Nu e o eroare, sunt două
  întrebări diferite.

## v0.63.0 - 2026-07-31

### Task-uri — ingestie completă pentru angajații din roster (fix date lipsă)
- **Problemă**: filtrul de ingestie accepta doar 6 categorii CTS din 398 (`cts_tasks.category_allowlist`).
  Rezultat: ~56% din task-uri aruncate (`filtered_noise: 5325` din `fetched: 9519` pe iulie).
  Task-urile contabilității pe categorii nelistate nu ajungeau niciodată în bază — de-aia raportul
  Adelina Pop (≈200 solved în CTS pe iulie) nu se potrivea cu 85 afișate în Cargo360.
- **Fix**: SINGURUL criteriu de ingestie e acum „asignat unui angajat din
  `employee_department_mapping`". Filtrul pe categorii (`CATEGORY_ALLOWLIST`) eliminat complet —
  categoria CTS e irelevantă pentru productivitate. Zgomotul automat e oprit oricum de criteriul de
  assignee (alertele nu au assignee real).
- Helper nou `_get_roster_emails(db)` — citește emailurile din roster o dată per rulare.
- Eliminate: `_DEFAULT_CATEGORY_ALLOWLIST`, `CATEGORY_ALLOWLIST_KEY`, `_get_category_allowlist()`.
  `_device_family()` păstrat — folosit la clasificarea tipurilor în UI, nu la filtrare.
- `RECENT_MAX_BACKFILL_HOURS` 168 → 1440 (7 → 60 zile): fereastra de 7 zile nu putea readuce
  task-urile mai vechi respinse de vechiul filtru.
- **Efect măsurat**: `filtered_noise` 5325/9519 (56%) → 1925/72653 (2.6%). Tabelă 30.187 → 68.833
  rânduri. Verificare pe Pop Adelina, iulie: 85 → **171 solved, identic cu raportul CTS**.
  Creșteri similare la majoritatea utilizatorilor (Kovacs Robert 307→822, Tomuta Maria 821→3440).

### Migrație 20260731 — indexuri pentru filtrele Task-uri
- `ix_ctgt_cts_created_at` și `ix_ctgt_assignee_raw` pe `cts_task_ground_truth`.
- Necesare după dublarea tabelei + filtrul nou de utilizator: listă filtrată pe utilizator + lună
  a scăzut de la 62ms la 5ms. Aditive, `IF NOT EXISTS`.

### Endpoint nou `POST /cts-tasks-training/backfill`
- Re-ingestie paginată de la o dată explicită (`since=YYYY-MM-DD`), fără plafonul de 168h al
  sync-ului rolling. Necesar după schimbarea regulilor de filtrare: task-urile respinse anterior
  nu există în bază, iar fereastra de 7 zile nu le readuce pe cele mai vechi.

## v0.62.3 - 2026-07-31

### device_ops_suport2_sync — lookup DB în loc de whitelist hardcodat
- `SUPORT2_CLOSED_BY_WHITELIST` (dict cu ID-uri specifice staging) eliminat complet.
- Înlocuit cu `_resolve_employee_by_name(db_session, name)`: lookup `lower(unaccent(name))` în `employee_department_mapping` filtrat pe `department IN (suport_2, suport_3)`.
- Fix pentru prod: ID-urile angajaților diferă între staging și prod — acum se rezolvă dinamic.

### Filtre utilizator + optimizare performanță Task-uri / Device Ops / Apeluri CTS
- Dropdown „Utilizator" adăugat pe paginile Task-uri, Device Operations, Apeluri CTS.
- Task-uri: eliminat LATERAL JOIN cu regex pe `description` (penalitate 180ms, coloana IMEI nefolosită).
- `assignees` și `agents` endpoints noi pentru popularea dropdownurilor, cu rezolvare nume din `employee_department_mapping`.
- Gzip pre-comprimare `mg-app.js` (1.2MB → 270KB): `_GzipStaticFiles` servește `.gz` automat.

### call_scorer — output_type + rescore_null + fix issue_summ
- Prompturi cu `output_type='text'` returnează răspuns brut (string), nu JSON — util pentru prompturi narative.
- `score_batch(rescore_null=True)` șterge rândurile cu scor NULL înainte de re-scorare (evită duplicate blocate).
- `issue_summ`: fix pentru cazul când `issueSummarization` e string direct (nu dict) — nu mai crasha.
- API `POST /calls-analytics/rescore`: parametrul `rescore_null` expus și în request body.

### CTS Mail-uri — carduri stats răspund la filtre active
- Cardurile de statistici din pagina Mail-uri CTS (total, potrivire categorie/departament) se actualizează
  la schimbarea filtrului de perioadă sau categorie, nu mai afișează valori globale.

### Rapoarte & Statistici — grafice acuratețe AI în timp
- 2 grafice noi în secțiunea „Acuratețe AI vs CTS în timp": potrivire categorie (linie albastră) și
  potrivire departament fără Suport 1 (linie verde), cu bare galbene la schimbări de prompt.
- Graficele urmează filtrul activ al paginii: interval de zile sau perioadă personalizată (date_from/date_to).
- Backend: `/cts-training/accuracy-daily` acceptă `date_from`/`date_to` pentru interval fix; `prompt_changes` inclus în răspuns.
- Backend: `/cts-training/stats` acceptă `department`, `dept_from`, `dept_to`, `date_from`, `date_to`.

### Migrație 20260730 — fix pipeline release
- `20260730_extra_work_days.sql`: `DROP CONSTRAINT` înlocuit cu bloc `DO $$ BEGIN IF NOT EXISTS ... END $$`
  (idempotent, fără DROP — compatibil cu pipeline release care blochează operații distructive).

## v0.62.1 - 2026-07-31

### UI
- Eliminare butoane nefuncționale 7/30/60/90 zile din Rapoarte & Statistici.
- Scroll restore + highlight la întoarcere din ClientDetail pe pagina Satisfacție clienți.
- Eliminare coloana „Nr. device" din Task-uri (LEFT JOIN LATERAL scos din query backend).

### Backend
- Sync periodic Device Operations adăugat în cron (`device_ops_suport2_sync.py` + `emails.py`).
- Endpoint nou `POST /device-ops/sync-run-now` pentru declanșare manuală sync.

## v0.62.0 - 2026-07-30 (Filtre perioadă personalizată, barchart-uri, raport PDF, tipuri documente)

Pachet de modificări UI/UX cerute de Raul Covaci. Toate filtrele de perioadă folosesc două date
explicite (**de la — până la**), fără preseturi de 7/21/30 zile.

### Filtru perioadă personalizată (component nou, reutilizat)
`DateRangeFilter` + helperul `rangeQS()` în `app/ui/vendor/mg-app.js`, montat în:
- **Dashboard** — actualizează TOATE categoriile (emailuri, apeluri, task-uri, documente).
- **Rapoarte & Statistici** — pe fiecare categorie (Email-uri / Apeluri / Task-uri); perioada e
  ridicată în shell, deci se păstrează la schimbarea tabului.
- **Emailuri** — pe data de recepție; pentru o singură zi se pune aceeași dată în ambele câmpuri.
- **Task-uri**, **Device Operations** — pe data creării în CTS.
- **Apeluri → Analitice** — perioada personalizată are prioritate peste selectorul de lună.

Backend: `date_from`/`date_to` (ziua de final inclusă) pe `/stats/dashboard`, `/stats/overview`,
`/stats/daily`, `/stats/daily-category`, `/stats/calls-dashboard`, `/stats/calls-daily`,
`/stats/calls-daily-category`, `/stats/calls-overview`, `/stats/tasks-daily`,
`/stats/tasks-overview`, `/stats/document-processing`, `/emails`, `/cts-tasks-training/{list,stats}`,
`/device-ops/{list,stats}`. Fără parametri, comportamentul rămâne identic (aditiv).

### Rapoarte & Statistici
- **Grafice de evoluție: bare în loc de linii.** `MultiLineChart` randează `type: 'bar'` (bare
  grupate); numele componentei a rămas ca să nu atingem cele ~20 de locuri care o folosesc.
- **Acuratețea scoasă din UI** (Email-uri + Apeluri + per tip document). Endpoint-urile
  `/cts-training/accuracy-daily` și `/cts-calls-training/stats` rămân în backend, nefolosite aici.
  Graficul „Acuratețe per tip document" e înlocuit cu „Documente pe tip (volum)".
- **Buton „Raport PDF"** — `printReportPdf()` deschide o fereastră de print cu exact conținutul
  paginii (KPI + grafice + tabele), cu perioada și data generării în antet. Canvas-urile Chart.js
  sunt convertite în imagini (altfel ies goale la print) și se forțează varianta light.

### Utilizatori
- **Coloana „Schimb" ștearsă** din tabel (nu era folosită). Coloana DB `shift` și endpoint-ul
  rămân neatinse — s-a scos doar din interfață.
- **Filtru de utilizator**: căutare pe nume + email, insensibilă la diacritice
  („Brasovean" găsește „Brașovean"), plus filtru pe departament și „Reset filtre".

### Satisfacție clienți
- **Prompt AI corectat — motive economice/externe.** Insolvența, lipsa de bani, vânzarea firmei
  sau a camioanelor, accidentele/dauna totală, încheierea unui leasing, restructurarea NU mai scad
  scorul și nu mai marchează clientul ca nemulțumit/la risc. Regula e pusă în `satisfaction_engine.py`
  ȘI în `interaction_analyzer.py` (ambele prompturi, email + apel) — al doilea e esențial: el
  generează `mentiune_reziliere`, iar acel flag forța automat segmentul „critic" peste decizia AI.
  Excepție: dacă pe lângă motivul economic clientul reproșează explicit calitatea serviciului.
- **Buton „Înapoi la Satisfacție clienți"** în detaliul clientului, când s-a intrat cu „View" din
  pagina Satisfacție (înainte butonul ducea în lista de clienți, pierzând contextul).

### Task-uri
- **Nr. device** — coloană nouă. CTS nu expune numărul devicelui ca câmp și nu există cheie de
  legătură cu `device_operations` (verificat pe date reale: 0 potriviri pe `operation_id`), dar la
  task-urile ETOLL/carGObox apare în descriere ca IMEI de 14–17 cifre — de acolo se extrage.
  Când se regăsește în `device_operations` se afișează și numărul de înmatriculare; altfel numărul
  e marcat cu `*` (neconfirmat). Join-ul e `LEFT JOIN LATERAL … LIMIT 1`, ca IMEI-urile duplicate
  să nu multiplice rândurile (verificat: 30017 = 30017).
- **Status „closed" distinct de „solved".** În CTS `closed` = închis FĂRĂ rezolvare; badge-ul
  afișează „închis (nerezolvat)" cu tooltip explicativ, iar KPI-urile arată separat „Rezolvate
  (solved)" și „Închise nerezolvate". Înainte `closed` intra la rezolvate și umfla rata.
- **KPI-urile de sus respectă filtrele.** `/cts-tasks-training/stats` primește aceleași filtre ca
  `/list` (status/departament/tip/perioadă) prin helperul comun `_task_filters()`, deci cifrele de
  sus nu mai rămân pe cumulat când se filtrează.

### Device Operations
- Filtru de perioadă + statisticile de sus recalculate la orice filtru (helper `_ops_filters()`,
  aceeași sursă pentru listă și statistici).

### Apeluri → Analitice
- KPI-uri complete: **Nr. apeluri, IN, OUT, Total ore, Durata medie**, Răspuns (IN/OUT cu procent
  din total; Total ore = suma duratelor). `total_duration_seconds` adăugat în
  `/calls/analytics/dashboard` și `/stats/calls-dashboard`.
- Filtrarea pe departament/persoană exista deja; s-a adăugat perioada cu dată.
- KPI agenți/clienți și analiza AI pe întrebări binare (7 prompturi binare active) sunt
  funcționale pe staging și respectă filtrele — promovarea pe producție se face prin Release.

### Procesare documente — tipuri noi
Migrație `migrations/20260730_doc_types_cargobox_etoll.sql` (idempotentă, verificată prin rulare
dublă → `INSERT 0 0`), 4 tipuri fără șablon (se încarcă manual din UI):
`CUI / Extras pe contract carGObox sau ETOLL`, `Anexa 2/3/4 - contract carGObox`.
„Act de identitate" (buletin/pașaport) exista deja — neatins.
`ON CONFLICT` țintește indexul unic **parțial** real `(category, lower(name)) WHERE status='active'`
— un `ON CONFLICT (category, name)` ar fi eșuat.

### Corecții colaterale
- `VBarChart` și `MultiLineChart`: culorile de axă/grilă/tooltip vin din tokenii CSS
  (`prodCssVar`) în loc de hex hardcodat + grilă `rgba(255,255,255,.05)` invizibilă pe light.
- `VBarChart`: unitate configurabilă în tooltip — înainte scria „emailuri" și pe graficele de
  apeluri și task-uri.
- Iconițe noi line-style `currentColor`: `calendar`, `download`, `back`. Emoji scoase de pe
  butoanele atinse (Concedii & învoiri).
- Lint de design: baseline separat `.design_lint_baseline_mgapp.json` pentru `mg-app.js` (cel
  existent era pentru `index.html`, ceea ce raporta 227 de regresii false). Zero regresii noi;
  hex brut 227 → 219, emoji 74 → 73.

### Bug-uri găsite și reparate la double-check (înainte de release)
1. **Dată invalidă în query string → HTTP 500.** O valoare ca `?date_from=abc` ajungea direct în
   `CAST(... AS date)`, Postgres ridica `DataError` și endpoint-ul întorcea 500. Afecta toate cele
   12 endpoint-uri cu filtru de perioadă (plus 5 din `calls_analytics.py`, unde `date_from` exista
   de dinainte — defect preexistent). Reparat cu validare ISO (`_valid_date()` în `health.py`,
   `cts_tasks_training.py`, `device_ops.py`, `emails.py`) și `pattern=r"^\d{4}-\d{2}-\d{2}$"` pe
   parametrii din `calls_analytics.py`. Acum: 400/422 cu mesaj clar. UI-ul nu era afectat
   (`<input type="date">` trimite mereu format valid), dar un URL editat manual spărgea pagina.
2. **Rapoarte → Email-uri se putea bloca pe „Se încarcă".** Garda de randare era
   `if (!cts) return …`, iar `cts` venea din `/cts-training/stats` — endpoint folosit DOAR pentru
   KPI-urile de acuratețe, care au fost scoase. Cu `.catch` gol, un apel eșuat lăsa `cts=null`
   permanent și pagina rămânea blocată, deși toate datele necesare erau deja încărcate. Apelul a
   fost eliminat, garda mutată pe `/stats/dashboard`, cu buton „Reîncearcă" la eroare.
3. **Graficele de evoluție puteau afișa date vechi.** `MultiLineChart` avea semnătura de
   re-randare `lungime + prima zi + ultima zi`, fără valori. La schimbarea unui filtru care
   păstrează aceleași zile dar schimbă cifrele (ex. departamentul în Apeluri → Analitice),
   `useEffect` nu se re-executa și graficul rămânea pe datele anterioare. Semnătura include acum
   și valorile seriei. (Defect preexistent, devenit mai probabil cu noile filtre. `VBarChart`,
   `HBarChart` și `ProdBarChart` erau deja corecte.)

### Verificări rulate la double-check
- Sintaxă: 7 fișiere Python + bundle JS (local și cel servit de nginx).
- Sincronizare local↔remote pe toate cele 11 fișiere atinse (md5 identic).
- Cifre API vs interogare directă în DB: emailuri 5466=5466, apeluri 5804=5804, serie zilnică
  10 zile pentru interval de 10 zile, zi unică 752=752.
- Paritate listă vs statistici (helperii comuni de filtre): Task-uri 3854=3854,
  Device Ops 46=46 — KPI-urile de sus reflectă exact filtrele din tabel.
- `LEFT JOIN LATERAL` pe device: 22392=22392 cu filtre active, 30017=30017 fără — zero duplicare.
- Analiză statică: 0 referințe orfane la codul șters (`shiftSel`, `SHIFTS`, `saveShift`,
  `ctsDaily`, `ctsAsg`), 0 variabile folosite nedeclarate în 195 de funcții, 0 încălcări ale
  regulilor hook-urilor React în 16 componente.
- Coloane tabel: Utilizatori 6 header = 6 `colSpan`; Task-uri 11 header = 11 celule = 11 `colSpan`.
- Test de runtime într-un context izolat: 16 cazuri limită pe componentele noi (null, listă goală,
  status necunoscut, popup blocat) + 13 scenarii de randare pe paginile mari (loading / cu date /
  filtrat / eroare) — toate fără excepții.
- Migrație: rulată de 3 ori → `INSERT 0 0` (idempotentă), validată și în tranzacție anulată.
- Schema DB neatinsă: `document_types` tot 20 coloane, coloana `shift` intactă (0 valori setate).
- Lint design: 0 regresii (R1 0, R2 219, R5 73 — toate la baseline).
- Endpoint-uri preexistente fără parametri: toate 200. Loguri: 0 erori grave.

### Rămas de făcut / de știut
- Schimbarea prompturilor din `interaction_analyzer.py` modifică hash-ul de versiune al
  promptului → interacțiunile se re-analizează automat. Scorurile de satisfacție se recalculează
  progresiv; pentru efect imediat pe un client anume se poate apăsa butonul de estimare.
- Numărul de device apare doar la task-urile care îl conțin în descriere (~255 din 21.251 de
  task-uri de device). Pentru acoperire completă, CTS ar trebui să expună devicele ca **câmp**
  în feed-ul de task-uri — nu se poate rezolva din Cargo360.

## v0.61.0 - 2026-07-30 (Export PDF raport productivitate)

### Ce s-a adăugat
Buton **„Exportă raport"** în tabul Rapoarte al modulului Productivitate. Generează un PDF cu
exact selecția curentă: luna din navigatorul de lună, grupul de departamente (Operațional /
Financiar) și intervalul. Financiar exportă doar Contabilitate + Recuperare TVA, fără nimic din
Operațional — aceeași filtrare `FINANCIAR_DEPTS` folosită la afișare.

### Conținut PDF (per departament)
- Statistica lunii: obiectiv minim / real / atins, coeficient, zile lucrătoare,
  ore planificate, ore disponibile, badge de status, nota de măsurare.
- Tabelul de obiective: tip, limită, pondere, total, scor obținut.
- Tabelul de ponderi per operator: volum + cotizație per canal (email / task-uri / apeluri /
  operațiuni — doar canalele configurate pe departamentul respectiv) și performanța finală.
  Detaliul pe categorii al Operațiunilor apare ca rând secundar sub operator.

### Tehnic
- `app/ui/vendor/mg-app.js` — `prodExportPdf()` construiește un HTML autonom și îl deschide cu
  `window.open` + `print()`, același mecanism ca exportul de documentație API (`downloadDoc`).
  Nu apelează backendul: primește datele deja încărcate în tab, deci PDF-ul nu poate divergea
  de ecran. CSS-ul de print folosește culori absolute (documentul ajunge la imprimantă, nu în
  tema light/dark a aplicației) și `page-break-inside:avoid` per departament.
- Lună viitoare (forecast): banner „Productivitate estimată" + badge „Estimat", fără tabelele
  de operatori și fără „obiectiv atins" — consecvent cu ce afișează tabul.
- `RANGE_OPTS_LABELS` extras la nivel de modul pentru eticheta de interval din antet.

## v0.60.0 - 2026-07-30 (Zile libere extra: lucru pe proiecte / refurbished)

### Ce s-a adăugat
Secțiune nouă în modalul „Concedii" din pagina Utilizatori, sub „Adaugă concediu manual":
**zile libere extra pentru lucru pe proiecte / refurbished**. Se exprimă ca număr de zile pe
(lună, an) — ex. „3 zile în August 2026" — fără date calendaristice concrete, pentru că nu
contează *când*, doar *câte*.

Sunt zile de lucru care NU sunt suport efectiv, deci se scad din `ore_disponibile` exact
ca un concediu. Într-o lună cu 25 zile de concediu + 2 useri × 2 zile pe proiecte,
calculul de productivitate pleacă de la 29 zile libere.

### Regula de timing (identică cu concediile)
Intrările contează doar dacă sunt adăugate **înainte de începutul lunii vizate**. Selectorul de
lună oferă doar luni viitoare, iar API-ul refuză (HTTP 400) orice lună deja începută sau trecută.
Snapshot-ul lunar imutabil (`productivity_monthly_snapshot`) se fixează la prima zi lucrătoare a
lunii, când `productivity_notifier` trimite raportul lunar și apelează `forecast_report` — de acolo
încolo targetul nu se mai ajustează. O adăugare pe 16 august pentru august e respinsă.

### Imutabilitate
Nu există endpoint de UPDATE — o intrare salvată se poate doar șterge, și doar cât timp luna nu a
început. După ce luna începe, ștergerea e blocată (HTTP 400) ca să rămână trasabil ce a intrat în
snapshot; UI afișează „Fixat în snapshot" în loc de butonul Șterge.

### Plafonare
Suma (zile concediu + zile extra) per angajat e plafonată la zilele lucrătoare ale lunii, aceeași
regulă ca la concedii. `days_count` validat între 1 și numărul de zile lucrătoare ale lunii țintă.

### Tehnic
- `migrations/20260730_extra_work_days.sql` — aditiv/idempotent. Reutilizează `employee_schedule`
  cu `kind IN ('project_work','refurbished')`, `entry_source='manual_extra'`, `start_date/end_date`
  NULL. Coloane noi: `days_count`, `period_year`, `period_month`, `created_at`. Index unic parțial
  pe `(employee_id, kind, period_year, period_month)` + CHECK de integritate.
  Notă: `employee_schedule_uidx` preexistent colapsează datele NULL la `0001-01-01`, deci intrările
  extra scriu și `leave_type='YYYY-MM'` ca discriminant în cheia existentă.
- `app/api/v1/settings.py` — `GET/POST /settings/employees/{id}/extra-days`,
  `DELETE /settings/employees/{id}/extra-days/{sid}`. Admin-only, ca la concedii.
- `app/services/productivity.py` — `_extra_days_per_emp()`, folosit în `department_report` și
  `forecast_report`. Zilele extra se adună aritmetic peste union-ul de zile de concediu (nu au
  date concrete, deci nu participă la deduplicare), suma plafonată la `zile_lucratoare`.
- `app/ui/vendor/mg-app.js` — secțiune nouă în modalul Concedii: listă intrări + formular
  (tip / zile / lună), fără editare.

## v0.59.0 - 2026-07-30 (Monitor Productivitate: bare stivuite cu „încă în lucru", canale pe un rând)

### Bare stivuite: rezolvate + încă nerezolvate, pe ora sosirii
Fiecare bară arată acum două segmente: **rezolvate** (culoarea canalului, jos) și **încă în lucru**
(galben, sus), cu eticheta `22+7`. Se vede pe ce oră a rămas volum neprocesat.

Metrici noi în `/monitor/live`: `hourly[].mail_open` / `task_open` / `apel_open` / `device_open`.

**Definiție importantă — segmentul galben e raportat la ora SOSIRII, nu a rezolvării.** „La ora 10
au intrat 18, din care 1 e încă deschis." Nu este același lucru cu totalul „în lucru" din antetul
cardului, care include și restanțele din zilele anterioare: la Financiar, din 108 emailuri deschise,
doar **14 au sosit azi** — restul de 94 sunt din zile trecute și nu au oră în ziua curentă, deci nu
pot apărea pe graficul de azi. Legenda apare doar când chiar există volum nerezolvat.

### Canalele pe un singur rând
Grid-ul 2×2 devine un rând unic: 4 coloane pe Operațional, 3 pe Financiar (unde device ops lipsește).
Rândul primește `flex` 1,7→1,15, cardurile fiind acum mai late și mai joase.

Device ops trece de la galben la albastru — galbenul e rezervat acum segmentului „încă în lucru",
ca să nu existe două sensuri pentru aceeași culoare.

## v0.58.0 - 2026-07-30 (Monitor Productivitate: sesizările urcă în contoare, bare redimensionate)

### Sesizări & reclamații — din card separat în contoare sus
Cardul „Reclamații & sesizări" din partea de jos e desfăcut în trei contoare, pe rândul de sus,
lângă celelalte (5 contoare în total):
- **Sesizări deschise** — cu câte sunt restanțe din zilele trecute;
- **Reclamații deschise** — cu câte depășesc 7 zile;
- **Sesizări rezolvate azi** — cu câte au intrat pe telefon.

Componenta `MonitorComplaints` a fost eliminată (nu mai avea consumatori). Defalcarea pe categorie
a emailurilor rezolvate azi s-a mutat în antetul cardului de obiective.
Canalele ocupă acum toată lățimea ecranului, nu 3/4.

### Fix: barele apăreau disproporționat de mari
SVG-ul barelor folosea `preserveAspectRatio: 'none'`, ceea ce îl întindea pe toată înălțimea
disponibilă a cardului — bare și cifre deformate pe verticală, cu atât mai vizibil cu cât cardul
creștea. Trecut pe `xMidYMid meet` (scalare uniformă, raport păstrat).

Redimensionări în același pas: înălțime viewBox 130→96, lățime bară max 40→20px, cifra de pe bară
12→9px, ora 11,5→8,5px, iar rândul canalelor `flex` 2,4→1,7. Antetul cardului de canal: titlu
17→14,5px, cifra „rezolvate azi" 30→24px. Contoarele de sus: 27→24px (5 pe rând în loc de 3).

## v0.57.0 - 2026-07-30 (Monitor Productivitate: canale în grid 2×2, grafice mari)

- **Eliminat** graficul agregat „Solicitări pe oră — azi · intrate vs rezolvate" — dubla informația
  deja prezentă în cardurile de canal.
- **Canalele trec în grid 2×2** și ocupă zona principală: fiecare card are acum ~2,5× suprafața
  anterioară, iar barele sunt semnificativ mai mari (lățime 15→22px, înălțime utilă 68→90px,
  valorile 8,5→12px, orele 9→11,5px). Adăugată linie de bază sub bare.
- **Înapoi la o singură serie** (rezolvate pe oră). Suprapunerea intrate/rezolvate încărca inutil
  un card mic; datele despre volumul intrat rămân disponibile în API (`*_new`).
- **Device ops se afișează doar unde există**: cardul apare dacă grupul chiar are astfel de
  operațiuni (Operațional), și dispare pe Financiar, unde contabilitate/recuperare TVA nu au
  device ops — un card permanent gol nu spune nimic. Cu 3 canale, grid-ul devine 3 coloane, ca să
  nu rămână un gol.
- Antetul cardului: numele + ora de vârf în stânga, „rezolvate azi" mare (30px) în dreapta, cu
  „în lucru" dedesubt.
- Defalcarea pe categorie a emailurilor rezolvate azi s-a mutat în cardul „Reclamații & sesizări".

## v0.56.0 - 2026-07-30 (Monitor Productivitate: filtrare pe grup + bare suprapuse)

### FIX MAJOR DE CORECTITUDINE: cifrele nu erau filtrate pe grup
Monitorul „Operațional" afișa numărători **globale pe toată firma**, nu doar pe departamentele
grupului. Concret, la emailuri rezolvate azi arăta **717**, din care doar **164** aparțineau
Operațional (suport_1/2/3 + taxe_drum) — restul erau contabilitate, comercial, mobilitate și 389
fără departament atribuit. Aceeași problemă pe Financiar.

Toate interogările din `/monitor/live` primesc acum un JOIN pe `employee_department_mapping`
filtrat pe departamentele grupului: contoarele de canal, seriile orare (intrate și rezolvate),
sesizările/reclamațiile, categoriile și device ops.

Chei de legătură (verificate în bază — diferă de la o tabelă la alta):
- emailuri / apeluri → `lower(cts_assignee_email) = lower(edm.email)`
- task-uri → `assignee_employee_id = edm.id` (cheie străină numerică; `iris_id` este text și
  nu se potrivește — un `JOIN` pe el întorcea zero rânduri)
- device ops → `closed_by_employee_id = edm.id`

Valori după filtrare: Operațional 167 mail / 455 task / 122 apel / 14 device;
Financiar 126 / 62 / 31 / 0. Totalurile din carduri coincid cu suma barelor orare pe ambele grupuri.

### Fix: „restanțe" la sesizări subraporta
Vechimea se calcula din `changed_at`, prezent doar pe 68 din 320 de emailuri deschise. Înlocuit cu
momentul real de sosire (`raw->'extra'->>'created_at'`, marcat UTC), cu `changed_at` ca rezervă.

### Bare suprapuse în loc de alăturate
Perechea de bare de la v0.55.0 înjumătățea lățimea fiecărei bare — ilizibil pe TV. Acum, per oră,
o **singură bară lată translucidă** (intrate) cu o **bară mai îngustă plină în față** (rezolvate,
verde). Aceeași logică pe graficul mare, prin `barPercentage` diferit pe aceeași categorie
(`stacked: false` — seriile se suprapun, nu se însumează).

## v0.55.0 - 2026-07-30 (Monitor Productivitate: bare pereche intrate vs rezolvate)

Fiecare canal arată acum, pe fiecare oră, **două bare alăturate**: intrate (culoarea canalului) și
rezolvate (verde) — se vede dacă echipa ține pasul cu volumul primit. Același principiu și pe
graficul mare de sus.

### Metrici noi în `/monitor/live` — volumul INTRAT pe oră
- `hourly[].mail_new` — din `raw->'extra'->>'created_at'` (singurul timp real de sosire al
  emailului). **Este text naiv în UTC**, deci se marchează explicit `AT TIME ZONE 'UTC'` și se
  convertește în fusul local; altfel barele „intrate" ar fi apărut decalate cu 3 ore față de cele
  „rezolvate", pe același grafic.
- `hourly[].task_new` — `cts_created_at`; `hourly[].apel_new` — `cts_started_at`;
  `hourly[].device_new` — `finished_at` (momentul predării de către montator).

### Fix: apelurile rezolvate pe oră erau toate zero
`hourly[].apel` folosea `changed_at`, care este **NULL pe toate rândurile** din
`cts_calls_ground_truth` — coloana ieșea goală. Înlocuit cu momentul încheierii apelului
(`cts_started_at + cts_duration_seconds`). Rezultat: 182 apeluri rezolvate, distribuite corect
pe ore, egal cu totalul din card.

### Fix: fereastra graficului mare pornea de la index, nu de la oră
`all.findIndex(...)` returna poziția în listă, folosită apoi ca oră de start — corect doar din
întâmplare când lista începe la 00. Înlocuit cu ora reală a primului interval cu volum
semnificativ (≥10% din vârf), calculat pe max(intrate, rezolvate).

### Verificare de consistență (2026-07-30)
Totalurile din carduri vs suma barelor orare vs interogare directă în bază:

| Canal | Card | Σ bare | Bază |
|---|---|---|---|
| Mail rezolvate | 717 | 717 | 717 |
| Apel rezolvate | 182 | 182 | 182 |
| Task rezolvate | 551 | 551 | 551 |
| Device rezolvate | 14 | 14 | 14 |
| Mail în lucru | 320 | — | 320 |
| Mail intrate | 768 | 768 | 768 |

**Limitare cunoscută:** „Task rezolvate" folosește `cts_updated_at` (ultima modificare), fiindcă
tabela nu are un timp de rezolvare propriu. În practică ultima modificare este rezolvarea, dar
editarea unui task deja închis îl mută la ora editării.

## v0.54.0 - 2026-07-30 (Monitor Productivitate: canalele trec pe bar chart)

Graficul de linie din cardurile de canal era greu de citit (fără axă, fără valori) — înlocuit cu
bare pe oră.

### Bar chart pe oră, per canal
- Fiecare bară = o oră din ziua curentă, cu **valoarea scrisă deasupra** și **ora dedesubt**.
- Ora curentă e evidențiată (bară la opacitate plină + oră îngroșată).
- **Fereastra de start** nu mai pornește de la prima înregistrare, ci de la prima oră cu volum
  semnificativ (≥10% din vârf). Altfel 1–2 emailuri primite noaptea (ora 02, 04, 06) întindeau
  graficul pe toată ziua și striveau orele reale de lucru: acum mail-ul începe de la 07, iar
  Device ops își păstrează orele de dimineață pentru că acolo chiar are volum.
- Barele cu zero rămân vizibile ca linie subțire (se vede că ora a existat, dar fără activitate).

### Rezumat numeric în antetul cardului
Cifrele „rezolvate azi" și „în lucru" s-au mutat sus, lângă titlu, în formatul `650 / 319`
(rezolvate / în lucru), cu delta verde `+N` la schimbare. Eliberează spațiu pentru grafic.

### Apeluri: „în curs" în loc de un câmp gol
Cardul Apel afișa `0` la „în lucru" pentru că metrica nu exista. Adăugat în `/monitor/live`:
- `apeluri.rezolvate_azi` — apeluri de azi cu status `solved`/`closed`;
- `apeluri.in_curs` — apeluri de azi încă neînchise (status `new` / `in progress`).

**Atenție la definiție:** `in_curs` numără doar apelurile **de azi**. Fără filtrul pe zi ieșeau 372,
din care 358 erau restanțe istorice niciodată închise — un număr care ar fi arătat ca „372 apeluri
în desfășurare acum", complet fals. Cu filtrul pe azi: 14.

## v0.53.1 - 2026-07-30 (Monitor Productivitate: reechilibrare proporții)

Ajustare de proporții — contoarele de sus dominau ecranul, gauge-urile erau prea mici.

- **KPI-uri micșorate**: cifra 40→27px, iconița 44→34px, padding redus. Rămân lizibile de la
  distanță fără să ocupe un sfert din ecran.
- **Gauge-uri mărite**: procentul 32→42px, ținta 12→14px, numele departamentului 14→16px.
  Eliminat plafonul de înălțime (`maxHeight: 190`) care le ținea mici degeaba.
- **Redistribuit spațiul pe verticală**: rândul obiectivelor primește `flex 1.55` (era 1),
  rândul canalelor 0.95, graficul orar 1 — gauge-urile au acum cea mai mare suprafață.

*Notă privind datele:* seriile pe oră NU sunt simulate. Mail = `cts_ground_truth.cts_solved_at`,
Apel = `cts_calls_ground_truth.cts_started_at`, Task = `cts_task_ground_truth.cts_updated_at`,
Device = `device_operations.closed_at` — toate grupate pe oră în `Europe/Bucharest`, filtrate pe
ziua curentă. Verificat prin comparație directă cu interogarea în bază: valori identice.

## v0.53.0 - 2026-07-30 (Monitor Productivitate: layout pentru TV — 4 rânduri, grafice mari)

Dimensionare pentru ecran mare (wall-monitor), după mockup-ul `varC1`.

### Fix: Device ops nu avea serie orară — sparkline-ul chiar era gol
Canalul „Device ops" primea un array gol ca serie, deci graficul lui era plat indiferent de
activitate. Adăugat `hourly[].device` în `/monitor/live` (din `device_operations.closed_at`,
în fus local). Acum are date reale (azi: vârf 6 operațiuni la ora 09).

### Layout — 4 rânduri, fără chart-ul lunar
- **Eliminat** „Volum zilnic luna curentă — emailuri + apeluri" (cerut explicit).
- Structura devine: KPI-uri → grafic orar + reclamații → 4 canale live → obiective pe toată lățimea.
- Obiectivele ocupă acum tot rândul de jos, nu o treime — gauge-urile au spațiu real.
- Toate cifrele mărite pentru citit de la distanță: KPI 26→40px, cifrele canalelor 16→27px,
  cutiile de reclamații 23→34px, ticks/legendă grafic 9.5→12px.

### Gauge-uri — înapoi la arc
Inelul complet din 0.52.2 e înlocuit cu **gauge clasic (arc deschis)**, ca în mockup:
- procentul realizat mare în centru, ținta scrisă sub el;
- **ac pe arc** pe poziția obiectivului real — se vede instant dacă a fost depășit;
- verde = obiectiv atins, galben = sub obiectiv;
- antetul cardului arată ziua lucrătoare curentă („ziua 22/23").

### Canalele live
- Titlu grafic: „**Solicitări rezolvate pe oră** — azi" (era „Rezolvate pe oră").
- Fiecare canal arată acum **ora de vârf** („vârf 09–10 · 97") citită din datele reale.
- Cifra „rezolvate azi" are count-up + delta verde (`+N`) când se schimbă între două citiri,
  ca să se vadă mișcarea pe monitor.
- Sparkline îngroșat (2.5px) și înălțime flexibilă, cu punct pe ora curentă.

## v0.52.2 - 2026-07-30 (Monitor Productivitate: gauge înlocuit cu inel complet)

Gauge-ul în formă de arc (240°) rămânea îngust pe coloană și împingea textele sub el, unde se
strângeau până deveneau ilizibile („obiectiv atins" / „target la zi 71.9% · ziua 22/23").

- Înlocuit cu un **inel complet (360°)** care se umple cu procentul **realizat din obiectiv**
  (`obiectiv_atins / obiectiv_real`), nu cu o valoare absolută pe o scală 0–100.
- **Toate cifrele stau acum în interiorul inelului**: procentul mare în centru, eticheta
  „DIN OBIECTIV" sub el, iar dedesubt realizat vs țintă („90.7% / 75.2%").
- Statusul devine badge pe fundal propriu (contrast real, nu text mic pe fundalul cardului).
- „ținta zilei" și „ziua N din M" pe două rânduri separate, la 10px — lizibile de la distanță.
- Reper subțire pe inel = unde ar trebui să fim azi conform ritmului lunii.

## v0.52.1 - 2026-07-30 (Monitor Productivitate: lizibilitate — valori pe gauge, zonă reclamații, reordonare)

Ajustări vizuale după prima rulare pe monitor + un bug de calcul găsit pe parcurs.

### Fix de calcul: „target la zi" era umflat cu ~36%
Gauge-ul compara **zile calendaristice** scurse (30, din `per_day`) cu **zile lucrătoare** din lună
(23) — de unde și eticheta absurdă „30/23 zl". Targetul zilei ieșea `obiectiv × 30/23`, adică peste
obiectivul lunar întreg, așa că departamente aflate în grafic apăreau „sub ritm". Acum se numără
doar zilele lucrătoare scurse (Luni–Vineri), plafonate la totalul lunii: 22/23 în loc de 30/23.

### Gauge-uri
- Procentul atins e scris **pe grafic**, mare, colorat după status; sub el, ținta („țintă 75.2%").
- Ac de referință pe arc, pe poziția obiectivului real.
- Eliminate badge-urile `30/23 zl` și `728h` (nerelevante pe un monitor de perete). Rândul de sub
  gauge arată acum „target la zi X% · ziua N/M".
- Gauge SVG propriu pentru monitor — `ProdGauge` (partajat cu pagina Productivitate) rămâne neatins.

### Layout reordonat după cât de des e citit
1. Cele 3 contoare (Rezolvate azi · În lucru · Sesizări) — acum pe **un singur rând**, nu pe coloană.
2. **Volumul pe oră** urcat sus și mărit — era informația cea mai căutată, stătea ultima.
3. Canalele (mail / apel / task / device ops).
4. Obiectivele per departament + volumul zilnic lunar.

### Graficul pe oră
- Bare **segmentate pe sursă** (emailuri / apeluri / solicitări), nu un total nediferențiat — la
  cererea „ce s-a rezolvat, emailuri sau solicitări?".
- Axa arată **intervalul orar** (`09–10`), nu doar ora, ca să se vadă unde a fost vârful.
- Ora curentă și ora de vârf sunt evidențiate; restul barelor, aceeași culoare mai stinsă.
- În antet, defalcarea pe categorie de conținut a emailurilor rezolvate azi (informații / sesizări /
  reclamații / neclasificate).

### Zonă nouă: Reclamații & sesizări (înlocuiește „Distribuție pe tip")
Donut-ul pe canal a fost scos — nu spunea nimic acționabil. În locul lui:
- **Deschise acum**, cu câte sunt **restanțe** (rămase din zilele trecute, nu din azi).
- **Rezolvate azi** + câte au intrat pe telefon.
- Avertisment când există sesizări deschise de **peste 7 zile** (acum: 3, cea mai veche de 24 zile).
- Defalcare reclamații vs sesizări.

Backend: `sesizari` primește `restante`, `peste_7z`, `apel_sesizari_azi`, `apel_reclamatii_azi`;
adăugat `rezolvate_categorii` (rezolvate azi pe categorie de conținut). Toate aditive.

## v0.52.0 - 2026-07-30 (Monitor Productivitate: heartbeat live — sesizări, device ops, defalcare pe oră)

Redesign al dashboard-ului standalone de la Productivitate → Monitor Operațional / Financiar
(`/api/v1/productivity/dashboard/{group}`), ca să funcționeze ca un "puls live al firmei" pe
monitoarele de birou, nu ca un raport static. Tabul din aplicație rămâne launcher — nu s-a schimbat
fluxul de deschidere.

### Backend — `GET /productivity/monitor/live` îmbogățit (aditiv)
Cheile existente (`emailuri`, `taskuri`, `apeluri`) sunt **păstrate identic** pentru compatibilitate.
Adăugat:
- **`sesizari`** — sesizări/reclamații deschise nerezolvate, cerute explicit. Nu există tabelă
  dedicată: sunt valori de categorie, citite din `cts_ground_truth.cts_category` cu fallback pe
  `emails.ai_category` (același COALESCE ca `productivity._fetch_email_rows`). Întoarce
  `deschise` / `sesizari_deschise` / `reclamatii_deschise` / `rezolvate_azi`.
- **`device_ops`** — al 4-lea canal (Suport 2), lipsea complet din monitor.
- **`hourly`** — rezolvate pe oră azi, defalcat mail/task/apel + total; alimentează sparkline-urile
  și graficul orar.
- **`per_dept`** — rezolvate azi / în lucru per departament, filtrat pe grupul curent
  (endpoint-ul acceptă acum `?group=operational|financiar`).
- **`ts`** — timestamp real cu oră (înainte era doar data, deci ora lipsea).

**Fix fus orar:** toate agregările pe „azi"/pe oră folosesc acum `AT TIME ZONE 'Europe/Bucharest'`.
Coloanele sunt `timestamptz` stocate în UTC — fără conversie, vârful de activitate de la ora 10
local apărea pe ora 07, iar „azi" se rupea la miezul nopții UTC, nu local.

*Notă:* filtrul `status = 'in progress'` (cu spațiu) e **corect** și a fost păstrat — valoarea din
bază e literal `'in progress'`, nu `'in_progress'`.

### Frontend — dashboard nou pe 3 rânduri
- **Rândul 1**: gauge-urile de obiectiv per departament + 3 contoare mari cu **count-up animat** și
  deltă față de citirea anterioară: *Rezolvate azi* (mail+apel+task+device), *În lucru acum*,
  *Sesizări deschise*.
- **Rândul 2**: 4 carduri de canal (Mail / Apel / Task / Device ops), fiecare cu **sparkline din
  date reale pe oră** + rezolvate azi + în așteptare.
- **Rândul 3**: rezolvate pe oră (ora curentă evidențiată) · distribuție pe tip · volum zilnic lunar.
- Indicator **LIVE** cu puls; dacă un poll eșuează devine **RECONECTARE** și se păstrează ultima
  valoare bună — nu se inventează mișcare. Ceas local în header.
- Cadență: heartbeat live la **15s** (COUNT-uri ieftine); `dashboard/data` rămâne la 5 min
  (forecast + analytics, scump).

### Fix-uri
- **Culorile seriilor din graficul de volum nu se aplicau**: se pasau `var(--am)` și
  `color-mix(...)` direct lui Chart.js, dar contextul canvas 2D nu interpretează variabile CSS —
  seriile se desenau cu culoarea default. Adăugate helperele `mgToken()` / `mgAlpha()` care
  rezolvă tokenii înainte de desenare (același principiu deja folosit corect în `ProdGauge`).
- **Graficul nu reacționa la comutarea light/dark**: citea tema la construcție, dar `theme` lipsea
  din dependențele efectului. Adăugat.
- **Grilă/axe**: înlocuit `rgba(255,255,255,α)` hardcodat cu valori derivate din tokenul de text —
  vizibile corect pe ambele teme.
- **Emoji eliminate** din monitor (`🖥`, `📊`) → iconițe SVG line-style `stroke="currentColor"`.
- **Cache-busting**: versiunea din pagina standalone era fixată la `0.46.53`, deci browserul servea
  `mg-app.js` din cache după orice modificare. Acum se citește din `VERSION`.

*Fără migrație DB — se citesc doar tabele existente.*

## v0.51.1 - 2026-07-30 (fix afișare Operațiuni Suport 2: Asignat, date, durată/limită, obiective)

Fix-uri pe funcționalitatea livrată în v0.51.0 — datele erau corect sincronizate în bază, dar
interfața nu le afișa (coloane goale) și un obiectiv era etichetat greșit.

- **Coloana "Asignat" era goală**: sincronizarea nouă (`device_ops_suport2_sync.py`) nu popula
  câmpul `assignee_raw` (nici în obiectul Python, nici în INSERT-ul SQL), deși `assignee_employee_id`
  era corect populat. Interfața verifică specific `assignee_raw`, nu ID-ul. Adăugat `assignee_raw`
  = numele din "Closed by" în ambele locuri; necesită re-rulare sincronizare pentru rândurile
  existente.
- **"Data creare" era goală**: sursa nouă nu are un moment de "creare" echivalent. Înlocuită cu
  două coloane separate: "Finalizat montator" (`finished_at`) și "Închis Suport 2" (`closed_at`).
- **Durată + încadrare în limită**: coloană nouă cu durata Suport 2 (`closed_at - finished_at`) și
  limita din obiectivul de productivitate al categoriei (`productivity_objective.limita_minute`),
  colorată verde/roșu după încadrare.
- **Status "Închis"**: rândurile cu `closed_at` populat afișează acum un badge distinct "Închis" în
  coloana Status, în loc de statusul intern `finalizat` (neschimbat în bază, doar afișare).
- **Obiective productivitate — categorie "Operațiuni" apărea ca "Emailuri"**: dropdown-ul de tip
  obiectiv (`PROD_TIP_OPTIONS`) nu includea valoarea `device_ops`, deși eticheta corectă exista
  deja (`PROD_TIP_LABELS`). Datele din bază erau corecte (`tip='device_ops'`) — bug pur de afișare
  în `<select>`. Adăugat `device_ops` în lista de opțiuni.

## v0.51.0 - 2026-07-30 (Operațiuni Suport 2: sursă de date schimbată la view_device_operations)

### Schimbare sursă: "Operațiuni" (Suport 2) nu mai reflectă munca montatorilor, ci a Suport 2
Sursa veche (`/cts/device-operations`, legacy) conținea doar actorul montator/instalator
(new → finished) — nu exista deloc actorul Suport 2 care închide efectiv operația (finished →
closed). Obiectivul de productivitate "Operațiuni" pentru Suport 2 se calcula deci pe date care
nu aveau legătură cu munca reală a Suport 2.

Noua sursă, `view_device_operations` (IRIS Data Views), conține explicit `Closed by`/`Closed at`
(cine a închis operația și când) și `Finished at` (când a terminat montatorul) — exact perechea
folosită acum pentru a calcula durata Suport 2 (`finished_at` → `closed_at`).

- **Whitelist Suport 2** (nu departament — potrivire pe nume din "Closed by"): Robert Iova, Robert
  Kovacs, Ovidiu Ticus, Mihai Cuc, David Miclau, Baican Emanuel-Crinel, Zoltan Tyepak (oficial
  `suport_3`, inclus explicit fără schimbarea departamentului lui din pagina Utilizatori).
- **Fereastră**: doar operațiuni închise (`Closed at`) începând cu 1 iulie 2026.
- **Categorii mapate 1:1** din `Operation Type`: instalare_noua, mutare, interventie, calibrare,
  periferice, demontare. Categoria `inlocuire` rămâne fără sursă de date deocamdată (afișează gol) —
  nu există echivalent "Replacement" în datele CTS; se revine la ea când se decide cum se combină.
- `migrations/20260730_device_operations_suport2_view.sql`: coloane noi aditive pe
  `device_operations` (`closed_by_raw`, `closed_by_employee_id`, `closed_at`, `finished_at`,
  `operation_type_raw`, `dv_row_id`) + indexuri.
- `app/services/device_ops_suport2_sync.py` (nou): trunchiază și repopulează `device_operations`
  din `view_device_operations`, filtrat pe whitelist + fereastră + mapare categorii.
- `POST /api/v1/device-ops/suport2/sync` (nou): declanșează sincronizarea manual.
- Oprit cronul vechi (`device_ops_sync.run_recent_if_due()` nu mai rulează din `process_now`) —
  codul legacy rămâne neșters, doar dezactivat.
- `app/services/productivity.py`: `_fetch_device_ops_rows` rescrisă — citește din
  `closed_by_employee_id`/`finished_at`/`closed_at`, nu mai filtrează pe departamentul din
  `employee_department_mapping` (whitelist-ul de sincronizare e sursa de adevăr).

## v0.50.1 - 2026-07-30 (Satisfacție: retry apeluri IRIS AI + rate-limit, reduce "Context IRIS indisponibil")

### Fix: scor neutru (75/80) prea des la calculul satisfacției clienților
`_iris_call`/`run_prompt` făceau un singur apel către IRIS AI fără retry — orice timeout/eroare
tranzitorie de rețea/HTTP 429/500/502/503/504 pica direct pe fallback ("Context IRIS indisponibil —
folosit scor neutru 75"), umflând artificial numărul de clienți cu scor neutru necorelat cu situația
lor reală.
- `app/services/iris_ai.py`: `run_prompt` reîncearcă acum până la 3 ori pe erori tranzitorii
  (eroare transport/rețea, HTTP 429/500/502/503/504), cu pauză scurtă (1s, apoi 3s) între încercări.
  Erorile de configurare/request invalid (cheie lipsă, URL lipsă, JSON invalid) NU se reîncearcă —
  reîncercarea n-ar schimba rezultatul.
- `app/services/satisfaction_snapshot.py`: adăugat un interval de 1 secundă între clienții pentru
  care s-a făcut efectiv un apel AI (v4), ca rulările lunare/manuale să nu bombardeze gateway-ul
  IRIS cu cereri concurente și să reducă riscul de rate-limiting pe partea IRIS.

## v0.50.0 - 2026-07-30 (Productivitate: fix subraportare task-uri contabilitate/TVA/Suport 2 + operațiuni defalcate)

### Fix critic: task-uri CargoBox/BGToll/eToll/Hugo excluse greșit din obiectivul general
`_fetch_task_rows` excludea automat task-urile din familiile CargoBox/BGToll/eToll/Hugo de la
obiectivul general "task" — corect pentru `taxe_drum` și `suport_1` (au obiective family dedicate,
ar fi dublat numărătoarea), dar greșit pentru `contabilitate`/`recuperare_tva`/`suport_2` (nu au
obiective family separate, deci munca lor pe aceste task-uri dispărea din statistici). Exemplu real:
Lasca Oana-Maria avea 471 task-uri rezolvate în iulie, aplicația arăta doar 309 (162 excluse greșit).
Fix: `has_family_split` calculat per departament din obiectivele reale — se aplică excluderea DOAR
dacă departamentul are și obiective family-specific; altfel obiectivul general ia toate task-urile
solved/closed. `taxe_drum`/`suport_1` neschimbate.

### Operațiuni (device_ops) defalcate per operator — Suport 2
Coloană nouă „Operațiuni" + „Cotiz. oper.%" în tabelul de productivitate, separată de „Task-uri"
(anterior eram contopite). Rând expandabil per operator (click) arată defalcarea pe categorie
(calibrare/demontare/înlocuire/instalare nouă/intervenție/mutare/periferice) cu număr și procent.
Etichete „Device_ops — Interventie" înlocuite cu nume românești („Operațiuni - intervenție" etc).

### Tabel Obiectiv — expand/collapse
Header-ul tabelului de obiective e acum un buton expand/collapse (implicit închis).

### Fix scroll orizontal tabel operatori
`overflow:'hidden'` pe wrapper suprascria `overflowX:'auto'` (proprietate shorthand) — scrollul
orizontal nu funcționa niciodată. Eliminat `overflow:'hidden'`, adăugat `minWidth:720` pe tabel.

## v0.49.0 - 2026-07-30 (Satisfacție: sursă unică de date + reguli mai stricte pentru "revenire")

### Sursă unică de date (fix divergență sidebar vs dashboard)
`GET /clients/{id}` (sidebar) și dashboard-ul de evoluție citeau satisfacția din DOUĂ locuri
diferite (`clients.satisfaction_pct` vs `client_satisfaction_snapshots`), scrise de endpoint-uri
separate — puteau ajunge desincronizate. Acum `POST /clients/{id}/estimate-satisfaction` scrie
direct în `client_satisfaction_snapshots` (UPSERT pe `client_id, month_key`), iar `get_client`
citește din aceeași tabelă. `feedback_campaigns.py` (`_segment_candidates`) actualizat la fel,
via `LEFT JOIN LATERAL`. Nicio schimbare de schemă — doar de sursă de citire/scriere.

### Regulă nouă: "revenire" (recontact) cere precedent real
Un mesaj era numărat ca „revenire" (penalizat) chiar și fără nicio sesizare/reclamație anterioară
în același thread — orice mențiune de nemulțumire trecută ("duminică trecută nu mi-a mers") era
tratată ca recontact. Acum `_V4_RECONTACT_SYSTEM` cere OBLIGATORIU un mesaj anterior categorisit
sesizare/reclamație în același thread înainte de a număra o revenire.

### Fix-uri anterioare din acest ciclu (deja pe staging, incluse în acest release)
- Eliminat eticheta „B2B" din raționamentul AI (context holistic + service recovery) — irelevantă
  pentru scor, genera text confuz.
- Fix quote-stripping în `_fetch_month_interactions`: textul citat din emailuri (reply-uri) nu mai
  e analizat ca mesaj nou al clientului — reducea fals numărul de recontacts.
- Căutare client după ID mail/apel (`cts_training.py`, `cts_calls_training.py`, UI).

## v0.48.3 - 2026-07-29 (Satisfacție: sistemele automate nu mai apar ca clienți nesatisfăcuți)

### Rulare pe eșantion de 300 clienți — rezultat
Eșantion stratificat pe activitate **reală** (mailuri + apeluri legate prin `client_id`, task-uri
prin `iris_client_id`): 100 very_active (≥20 interacțiuni/90 zile), 100 active (≥5), 80 low_active
(≥1), 20 inactive. Exclude `satisfaction_exclude`.

**300 procesați, 0 erori, 236 apeluri AI.** Validat: numărul de interacțiuni analizate corespunde
realității la **324/324** clienți verificați — zero cazuri de „am analizat N interacțiuni" pentru un
client care are mai puține.

| Interacțiuni | Clienți | Scor mediu |
|---|---|---|
| 0 | 84 | 100,0 |
| 1-5 | 102 | 90,0 |
| 6-20 | 87 | 81,6 |
| 21-50 | 38 | 66,0 |
| 50+ | 13 | 64,5 |

### Fix: sisteme automate raportate ca clienți nesatisfăcuți
Primele două poziții din lista de nesatisfăcuți erau `HU-GO TEMP` (8,5%, 131 interacțiuni) și
`HU-GO ELECTRONIC TOLL SYSTEM` (12,8%, 258) — sisteme de taxare rutieră din Ungaria care trimit
exclusiv notificări automate (înregistrări vehicule, blacklist NÚSZ/hu-go.hu). Motorul le trata ca
reclamații ale unui client nemulțumit; IRIS semnala corect în raționament că „NU sunt interacțiuni
reale cu serviciul CARGO TRACK", dar scorul rămânea mic și polua lista folosită pentru intervenții.

Migrația `20260729i` extinde `clients.satisfaction_exclude` (mecanism existent, deja folosit pentru
CARGO TRACK SOLUTIONS / RUPTELA UAB / UNKNOWN CLIENT) la: sistemele HU-GO, `NOTIFICATION SYSTEM`,
`PARTENERI CLIENTI`, RUPTELA (furnizor dispozitive), CARGOFUEL (aplicație internă),
`EXPERT SOFTWARE GROUP` (furnizor software) — 7 entități, total 18 excluse.

**După curățare:** 319 clienți, 35 nesatisfăcuți, medie 87,1%. Lista de nesatisfăcuți conține acum
exclusiv firme de transport reale, cu raționament sprijinit pe date verificabile (ex. referințe de
amenzi contestate `47ABA050`/`47AEB272` fără răspuns 13 zile, reminder-e repetate ale clientului).

## v0.48.2 - 2026-07-29 (Satisfacție: apelurile nu se mai numără dublu)

### Fix: apel numărat de două ori când are mai multe rânduri CTS
Găsit la validarea rulării pe eșantionul de 300: un client (`SPEC TRANS SRL`) raporta 4
interacțiuni analizate deși are 3. Cauza: `LEFT JOIN cts_calls_ground_truth ON call_local_id = c.id`
returna apelul o dată per rând CTS legat, iar 6 apeluri pe staging au câte 2 rânduri.
Nu era contaminare între clienți — toate apelurile erau ale lui — dar umfla numărătoarea.

`DISTINCT ON (c.id)` în ambele query-uri de apeluri (`_fetch_month_interactions` și
`_fetch_orphan_calls_for_client`). Emailurile erau deja curate (0 rânduri CTS duplicate pe
`email_id`), deci nu au avut nevoie de fix.

**Validare pe eșantionul de 300:** 300 încadrări, 0 erori, iar numărul de interacțiuni analizate
corespunde realității la **300/300** clienți (înainte de acest fix: 299/300).

## v0.48.1 - 2026-07-29 (Satisfacție: interacțiunile analizate sunt strict ale clientului)

### Fix critic: „am analizat 54 de interacțiuni" pentru un client care are 10
`satisfaction_engine._fetch_month_interactions()` lega mailurile fără `client_id` prin **domeniul**
expeditorului. Dar pe staging **171 de domenii sunt partajate între 646 de clienți** — `ruptela.com`
(furnizorul nostru de dispozitive) apare la 8 clienți, printre care unul cu 0 mailuri proprii.
Fiecare dintre ei primea mailurile tuturor celorlalți de pe domeniu, deci scorul de satisfacție se
calcula pe conversații care nu erau ale lui.

Nici adresa exactă nu e suficientă singură: în CTS multe adrese sunt puse pe mai mulți clienți —
furnizori (`support@ruptela.com` la 8), bănci (`no-reply@unicredit.ro` la 6,
`tiberiu.fenesi@btleasing.ro` la 5), sau text liber în loc de adresă (`dispecer` la 37, `sotia` la
27, `sofer` la 17). O adresă partajată nu identifică pe nimeni.

- Legarea se face acum doar prin `emails.client_id` sau prin adrese care apar la **exact un client activ**.
- Tabelă derivată `client_unique_emails` (**11.609 adrese unice pentru 9.219 clienți**) — migrația `20260729h`. Calculul echivalent la runtime costa ~380 ms per client, inacceptabil pentru un lot de 300.
- `phone_match.rebuild_client_unique_emails()`, apelată din `iris_sync` după fiecare sync de clienți (raportează `unique_emails_indexed`).
- `email` e TEXT, nu VARCHAR(320): unele intrări CTS sunt liste întregi lipite într-un element jsonb. Filtrate pe lungime și pe absența spațiilor.

**Verificare:** pe cei 40 de clienți cei mai expuși (cu domeniu partajat), interacțiunile raportate
sunt acum **37 = 37** față de numărul real de interacțiuni proprii (`client_id` strict); zero clienți
cu raportat > propriu. Caz concret: `VOLANUL DE AUR SRL` (0 mailuri proprii, domeniul `ruptela.com`)
raportează exact cele 14 apeluri care îi aparțin, nu mailurile Ruptela.

Apelurile și task-urile erau deja corecte (`calls.client_id` strict + `phone_match` pe telefoanele
acelui client; `cts_task_ground_truth.client_id` = `iris_client_id`, verificat pe `raw_payload`).
`interaction_analysis` și `_raw_interactions_text` filtrau deja strict pe `client_id`.

## v0.48.0 - 2026-07-29 (Consistență productivitate: aliasuri departament, assignee, adrese interne)

Verificarea diferențelor semnalate de echipa Contabilitate/Recuperare TVA a scos la iveală
patru defecte care făceau ca aceeași întrebare să primească răspunsuri diferite în funcție de ecran.

### Fix critic: același departament, două cifre în aceeași aplicație
`cts_task_ground_truth.department` conținea `taxe_de_drum` (21.959 rânduri), iar canonicul din
`employee_department_mapping` e `taxe_drum`. `cts_tasks_sync._slug()` normaliza doar spații și
cratime, fără aliasare. Rezultatul, măsurat pe aceeași lună și același departament:

| Ecran | Filtru | Task-uri găsite |
|---|---|---|
| Istoric, Per-operator | `cts_task_ground_truth.department` | **0** |
| Forecast, Analytics | `edm.department` (JOIN pe angajat) | **21.870** |

- `_DEPT_ALIASES` în `cts_tasks_sync` + migrația `20260729d` (21.963 rânduri corectate).
- Cele două ecrane divergente aliniate la aceeași sursă ca celelalte (departamentul angajatului asignat), `productivity.py:1366` și `:1495`. După fix: **21.870 = 21.870**.
- Rămâne o divergență legitimă: Robert Cazacu e în `account_management`, task-urile lui sunt `comercial` (133) — o persoană poate lucra pentru alt departament, nu e alias.

### Fix: 8 departamente din 16 nu se normalizau niciodată
`cts_ground_truth.cts_department` păstra valori brute CTS — `Administrativ` (71), `Operational` (33),
`Product Management` (32), `Management General` (16), `Instalari` (13), `IT Team 1` (11),
`Marketing` (8), `HR` (8), `IT` (2). Cauza: `_map_department()` normaliza pe `DEPT_LABELS` (8
departamente, lista pe care alege clasificatorul AI), dar `employee_department_mapping` are 16;
pentru restul cădea pe fallback și păstra valoarea brută, care nu se potrivea cu niciun slug în
rapoarte. Aliasuri adăugate în `_DEPT_ALIASES` (nu în `DEPT_LABELS`, ca să nu extindem lista AI-ului)
+ migrația `20260729e` (194 rânduri).

### Fix: whitelist-ul de angajați era în urma realității
`iris_employee_sync.VALID_DEPARTMENTS` avea 8 departamente, deci angajații din `instalari`, `hr`,
`marketing`, `product_management` etc. erau **respinși la import** — deși `employee_department_mapping`
avea deja 7 oameni în `instalari`, ajunși acolo pe altă cale. Concret: Adrian Jurca (activ în rosterul
IRIS, `instalari`) nu se putea importa, deci cele 180 de operațiuni ale lui nu se contorizau.
Whitelist extins la toate cele 16 departamente reale → **6 angajați noi importați**.

### Fix: operațiuni pe dispozitive pierdute pe typo-uri în sursă
Din 1.429 operațiuni, doar 654 aveau angajat mapat. Cauze în datele CTS:
- `adrian.jurca@cagrotrack.ro` — typo de domeniu, **180** operațiuni
- `cristian.gotonoaca@` vs rosterul IRIS `cristian.gotonoaga@` (c/g) — **65**
- `cosmin.margauan` — username fără domeniu, **49**
- `client@` / `nealocat@` — placeholder-e, corect nemapate (185)

`device_ops_sync._normalize_assignee_email()` corectează typo-urile, completează domeniul intern și
respinge placeholder-ele înainte de rezolvare. Migrațiile `20260729f` + `20260729g`:
**654 → 851** operațiuni cu angajat. Restul: 460 placeholder/gol, 118 persoane care nu există în
rosterul IRIS (foști angajați, 2 adrese Gmail externe).

### Fix: adresele noastre în lista de emailuri a clienților
71 de clienți aveau 193 de adrese CargoTrack în `clients.emails` — `office@cargotrack.ro` la **26 de
clienți**, adrese de colegi (`calin.lucaciu@`, `nicoleta.berde@`…) la 3-8 fiecare, plus 9
placeholder-e `fara_email@cargotrack.ro`. În CTS ele înseamnă „agentul care gestionează clientul".
`match_client()` face `emails @> [from_address] LIMIT 1`, deci orice email trimis de un coleg primea
un client **arbitrar** dintre cei 26.
- Adresele mutate în `clients.internal_contact_emails` (informative, nu se pierd) — migrația `20260729c`.
- 365 de atribuiri făcute pe această bază, anulate (păstrate doar cele confirmate de CTS).
- `process_email.match_client()` refuză adresele interne la matching.
- `iris_sync.discover_client_emails()` filtra free-mail dar **nu** domeniile proprii, deci le-ar fi
  reintrodus la următorul sync — filtru adăugat (`cargotrack.ro`, `trakosoft.ro`).

Efect pe acoperirea emailurilor: 86,1% → **81,8%**. Cifră mai mică, dar corectă — 365 din legăturile
de dinainte erau false.

### Robustețe
- `iris_sync.sync_clients_from_iris()` reconstruiește `client_phone_keys` după sync (altfel indexul rămâne în urmă și apelurile clienților noi nu se leagă). Raportează `phone_keys_indexed`.
- `storage/logs/` lipsea pe staging — cron-ul lunar de satisfacție redirecta în el (`>>`), deci rula fără log. Creat.
- Snapshot-urile de satisfacție pe 2026-07 recalculate cu `force=True`, după corectarea legăturilor.

### Fix critic: forecast-ul de productivitate crăpa pe TOATE departamentele
`forecast_report()` referea `ore_concediu` necondiționat, dar variabila se definește doar pe ramura
*fără* snapshot lunar. Cum `productivity_monthly_snapshot` exista, funcția arunca
`UnboundLocalError: cannot access local variable 'ore_concediu'` la fiecare apel → dashboard-ul
returna `forecast: []`, adică **lista de obiective era goală pentru toate departamentele**.
Valoarea e acum derivată din `ore_planificate - ore_disponibile` (definite pe ambele ramuri).
După fix, `/productivity/dashboard/data`:

| Grup | Departamente cu forecast |
|---|---|
| financiar | contabilitate (50,89% atins / 79,21 real), recuperare_tva (72,47 / 93,98) |
| operational | suport_1 (90,74), suport_2 (94,93), suport_3 (insuficient), taxe_drum (88,0) |

### Fix-uri găsite la verificarea de regresie (fixul ținea în DB, dar sync-ul îl anula)
Migrațiile curățau datele, dar prima resincronizare reintroducea murdăria. Prinse rulând efectiv
fiecare sync după curățare:
- `device_ops_sync` persista `assignee_raw` **brut**, deși normaliza doar pentru rezolvarea angajatului → 48 `cagrotrack` + 20 fără domeniu + 21 `gotonoaca` reveneau la fiecare sync. Acum se salvează valoarea normalizată (originalul rămâne în `raw_payload`), iar departamentul se ia din angajatul asignat.
- `iris_sync.sync_clients_from_iris()` scria `emails` direct din IRIS, deci 42 de adrese interne reveneau — filtrul pus inițial acoperea doar `discover_client_emails()`, nu calea principală. Separarea se face acum la upsert, în `internal_contact_emails`.
- Ramura „IRIS nu trimite adrese" păstra lista locală **integral**, deci 3 clienți care au în CTS exclusiv adrese interne le țineau în `emails`. Acum se păstrează doar partea externă.
- În query-ul cu parametri, `%` din `LIKE` trebuie dublat (psycopg2 îl tratează ca placeholder) — inclusiv într-un comentariu SQL, care a produs `IndexError: tuple index out of range`.

### Fix: /api/v1/health raporta versiune greșită
`config.app_version` era hardcodat `0.46.10` și rămânea în urmă la fiecare livrare. Acum se citește
din fișierul `VERSION`.

### Verificare finală (după toate sincronizările)
15/15 verificări trec: 9 pe curățenia datelor (toate 0), consistența ecranelor pe `taxe_drum` (0
divergență), acoperire emails 82,0%, calls 63,5%, 853 operațiuni cu angajat, 14.129 chei de telefon,
15.691 snapshot-uri recalculate. Endpoint-urile `/health`, `/healthz` și ambele dashboard-uri: 200,
zero erori în log.

### Verificat, blocat upstream (outbox #46)
- **Suport 2 (tehnic)**: 0 operațiuni din 1.429 — cei 6 angajați nu apar nici în `assignee_raw`. Sursa trimite deja tipuri tehnice (`interventie` 169, `calibrare` 67, `inlocuire` 114), dar toate executate de oameni din `instalari`. Configurarea aplicației e completă și așteaptă datele: `productivity_objective` are deja 7 obiective `device_ops` pe `suport_2`, cu categorii identice cu `action_type`-urile primite.
- **9.881 task-uri fără client**: nu au nici email, nici apel, nici `client_name` — nerezolvabil local.

## v0.47.0 - 2026-07-29 (Consolidare sursă date: legătură mesaj↔client din CTS + fix matching)

Legătura mesaj↔client era incompletă: 58% din emailuri și 65% din apeluri nu aveau client asignat,
deci nu intrau în calculul de satisfacție și productivitate. Cauza nu era lipsa datelor — CTS
trimite `client_id` pe fiecare mesaj — ci că nu era propagat local, plus trei defecte de matching.

### Fix: matching telefon rata numerele internaționale
- `phone_match.phone_key()` (nou): cheie canonică = ultimele 9 cifre. Înainte, `match_client_by_phone()` compara string exact (`phones @> '["0722123456"]'`), iar `while1_ingest._match_client_phone()` acoperea doar `0` ↔ `+40`. Numerele cu prefix `00` nu se potriveau niciodată: în `clients.phones` apare `0037368295882`, în `calls` apare `+37368533883` — același abonat, zero potriviri.
- Tabelă derivată `client_phone_keys` (client_id, phone_key) + index — expandarea `jsonb_array_elements_text` peste 16k clienți făcea seq scan (>120s pe backfill). `phone_match.rebuild_phone_index()` o reconstruiește după sync-ul de clienți.
- Potrivirile ambigue (număr partajat de mai mulți clienți) nu se mai atribuie: NULL e preferabil unei atribuiri greșite într-un calcul de satisfacție.

### Fix: emailurile TRIMISE nu se legau la client
- `process_email.match_client()` citea doar `from_address` — pe emailurile trimise de noi acela e o adresă CargoTrack, deci clientul rămânea NULL (978 emailuri). Acum acceptă și destinatarii (`to_addresses` + `cc_addresses`), sărind adresele interne. Satisfacția vedea doar jumătatea de conversație primită.
- `_addr_list()` normalizează formatul destinatarilor (listă text, formă Graph sau JSON-ca-string).
- `_is_internal_address()` refolosește `autoreply_generator.INTERNAL_DOMAINS` (acoperă subdomenii: `mail1.cargotrack.ro`).

### Fix: firul conversației nu se salva
- `o365_ingest`: `conversationId`, `ccRecipients` și `internetMessageId` se cer acum de la Graph și se persistă în `emails.conversation_id` / `cc_addresses` / `internet_message_id`. Coloanele existau, dar erau NULL pe toate cele 8.443 rânduri, iar `raw_graph_payload` păstra doar `{source, graph_id}` — firul nu era recuperabil retroactiv. De acum înainte, un răspuns al nostru poate moșteni clientul din mesajul primit în același fir.

### Forward-fix: CTS propagă clientul la fiecare sync
- `cts_calls_sync`: după upsert, `raw->>'client_id'` (= `clients.iris_client_id`) se propagă imediat în `calls.client_id`. Raportează `clients_linked`.
- `cts_groundtruth_sync`: idem pentru `emails.client_id`, din `raw->'extra'->>'client_id'`. Raportează `clients_linked`.
- Ambele completează doar NULL-uri — o legătură existentă nu se suprascrie.

### Backfill (`migrations/20260729b_backfill_client_links.sql`)
| Sursă | Înainte | După |
|---|---|---|
| `emails` | 3.516 / 8.443 (41,6%) | **7.270 (86,1%)** |
| `calls` | 5.927 / 16.872 (35,1%) | **10.707 (63,5%)** |

Pe pași: apeluri←CTS 4.257 · emailuri←CTS 3.698 · apeluri←telefon 523 · emailuri trimise←destinatar 56. Total **8.534** legături noi. Aditiv și idempotent (doar `WHERE client_id IS NULL`).

### Ce rămâne nelegat (verificat, legitim)
- `emails` 1.173: **812 interne** CargoTrack, 361 externi (furnizori Ruptela/Fortinet/Atlassian, newslettere) — corect fără client.
- `calls` 6.165: **6.158 nu au rând CTS** (apeluri de pe numere nedeclarate în CTS; unul apare de 149 ori), 7 au CTS dar clientul lipsește local.
- `cts_task_ground_truth` 9.842 cu `client_id` NULL: CTS nu trimite clientul, fără email/apel atașat — nerezolvabil local, escaladat.

### Verificat și respins ca sursă de matching
`client_master` și `view_client_list` (IRIS Data Views) au fost trase integral și comparate cu datele locale: **0 adrese email noi, 0 telefoane noi** — `clients` e deja sincronizat 1:1 (16.341 local / 16.376 în view). Sunt registre de firme, nu de mesaje: nu leagă un email/apel anume la client. (`client_master` conține date ANAF/VIES/bilanț — utile eventual pentru scor de sănătate client, nu pentru matching.)

## v0.46.59 - 2026-07-29 (Fix match client↔surse date: calls, satisfacție, domenii generice)

### Fix critic: interacțiuni false în satisfacție (domenii generice)
- `satisfaction_engine._client_email_domains()`: extins blocklist-ul de domenii generice care nu identifică unic un client — adăugate `mail.ru`, `yahoo.es`, `yahoo.it`, `yahoo.fr`, `hotmail.ro/it/fr`, `outlook.ro`, `me.com`, `mac.com`, `mail.com`, `ymail.com`, `live.com/ro`, `msn.com`, `protonmail.com`, `proton.me`, **`cargotrack.ro`**, **`trakosoft.ro`** (domenii interne). Clienți ca RAVAS GRUP TRANS (`mail.ru`) nu mai primesc sute de interacțiuni false de la alți expeditori pe același domeniu.

### Fix: calls.client_id backfill retroactiv
- Backfill pe apeluri existente fără client asignat: **2.411 apeluri** (din 13.291 fără client) au primit `client_id` prin match pe `clients.phones` (inbound = caller_number, outbound = callee_number, cu normalizare prefix `0` ↔ `+40`).
- Restul (~10.880) nu au număr de telefon în baza de date a niciunui client activ.
- `while1_ingest._insert_call()`: matchul de client prin telefon se face acum **la ingestie**, nu doar la clasificarea AI. Apeluri noi primesc `client_id` imediat.

### Diagnostic surse date (fără modificare de cod — probleme upstream)
- **cts_calls_ground_truth fără link local** (5.193): apeluri din CTS mai vechi decât bootstrap-ul While1 pe staging (iulie 2026). Lipsă de date istorice, nu bug de matching.
- **cts_task_ground_truth fără client** (9.842): task-urile vin din CTS fără `client_id` și fără `email_id`/`call_id` → nu există informație de matching local.
- **device_operations**: aduce din `instalari`, nu din `tehnic` (suport 2) — cerere trimisă în Outbox lui Razvan pentru adăugarea tabelei `tehnic` în gateway-ul IRIS.

## v0.46.58 - 2026-07-29 (Export clienți cu contacte duplicate)

- **Buton "Export duplicate"** în toolbar-ul listei de clienți: click generează direct un fișier CSV cu toți clienții care împart același email sau același număr de telefon cu alt client.
- **Format CSV** deschis direct în Excel (encoding UTF-8 BOM, separator virgulă): coloane Tip duplicat, Contact comun, ID client, Nume client — grupat pe contact, ușor de filtrat și corectat.
- Acoperă atât emailuri cât și telefoane; exclude câmpuri goale/liniuță.

## v0.46.57 - 2026-07-29 (Satisfacție: excludere UNKNOWN CLIENT + scor v4 în lista clienți)

- **Excludere UNKNOWN CLIENT**: clientul fantomă "UNKNOWN CLIENT" (id=3081) marcat cu `satisfaction_exclude=TRUE` — dispare din toate calculele și listele de satisfacție (dashboard, top satisfăcuți, la risc, nesatisfăcuți, distribuție, trend, movers).
- **Filtrare `satisfaction_exclude`**: toate query-urile din `/clients/satisfaction-stats` filtrează acum explicit clienții excluși — nu mai pot apărea indiferent de sursa datelor.
- **Scor satisfacție în lista clienți**: coloana de satisfacție afișează acum scorul din ultimul snapshot lunar v4 (nu câmpul vechi din `clients.satisfaction_pct`) — clienți ca "LIU & FLO EXPRESS SRL" vor apărea cu scorul corect dacă au snapshot calculat.

## v0.46.56 - 2026-07-29 (Tab Satisfacție client: redesenat complet cu algoritm v4 + explicații)

- **Un singur algoritm de satisfacție** — eliminat butonul "Estimează satisfacție" (producea scor cu engine diferit, inconsistent cu snapshot-ul). Tab-ul afișează exclusiv datele din snapshot-ul lunar v4.
- **LineChart** evoluție lunară înlocuiește tabelul — axă temporală, linie mov scor, linie roșu punctat prag 70%.
- **Pills clickable per lună** sub grafic — click pe o lună afișează defalcarea ei completă.
- **Scor final cu formulă**: contribuție Emoție (70%) + Context IRIS (30%) + restituire recovery → total afișat explicit.
- **Emoție (70%)**: afișează numărul exact de informații/sesizări/reclamații/reveniri, penalizările per categorie (−10/−20/−5 pt), scorul final calculat pas cu pas.
- **Reveniri pe problemă nerezolvată**: fiecare revenire detaliată cu referința (apel/mail) și motivul detectat de IRIS.
- **Context IRIS (30%)**: semnal dominant, trend, raționamentul complet al IRIS în format text.
- **Service Recovery**: bonus afișat dacă a fost aplicat, cu explicația IRIS.

## v0.46.55 - 2026-07-29 (Buton View în Satisfacție clienți + navigare directă la tab Satisfacție client)

- **Buton View** adăugat în toate cele 3 tabele din pagina Satisfacție clienți: Top 10 satisfăcuți, Clienți la risc, Clienți nesatisfăcuți. Click pe View → navigare directă la fișa clientului, tab Satisfacție.
- **Navigare cross-tab**: din Satisfacție clienți → Clienți (ClientDetail, tab Satisfacție) fără back/forward manual, fără refresh.
- **Tabul Satisfacție în ClientDetail** (existent): grafic evoluție lunară, tabel istoric lunar, breakdown complet per factor (Emoție, Efort, Operațional, Relație, Scor IRIS final) cu sub-metrici detaliate și tooltip-uri.
- **`initialTab` prop** pe `ClientDetail` — permite deschiderea directă pe orice tab din exterior.

## v0.46.54 - 2026-07-29 (Snapshot lunar imutabil — targetele nu se mai modifică în cursul lunii)

- **Tabelă nouă `productivity_monthly_snapshot`**: la prima generare a raportului pentru o lună, valorile `coeficient`, `ore_planificate`, `ore_disponibile`, `obiectiv_real`, `obiectiv_minim` se persistă automat și devin **imutabile**.
- **`department_report` + `forecast_report`**: la fiecare request verifică snapshot-ul. Dacă există, returnează valorile fixate — concediile neplanificate, aprobările ulterioare, modificările de obiective din UI **nu mai afectează targetele lunilor deja started**.
- **Migrație**: `migrations/20260729_productivity_monthly_snapshot.sql` — `CREATE TABLE IF NOT EXISTS`, idempotentă.
- **Compatibilitate**: lunile fără snapshot (generate prima dată după deploy) primesc snapshot automat la prima accesare. Lunile vechi (înainte de deploy) se comportă ca înainte până la prima accesare, apoi se fixează.

## v0.46.53 - 2026-07-29 (Monitor productivitate: heartbeat live — gauge kilometraj + charts)

- **Monitor Operațional / Financiar complet redesenat**: înlocuit tabelele statice cu dashboard exclusiv grafice, actualizat automat fără refresh de pagină.
- **Gauge "kilometraj lunar" per departament**: target dinamic ajustat la ziua curentă (la 15 ale lunii = ~50% din obiectiv). Indicator de stare: pe traseu / aproape / sub minim / obiectiv atins.
- **Bar chart volum zilnic**: emailuri + apeluri pe bara lunii curente, cu marker vertical pentru ziua de azi. Stacked bar (Chart.js).
- **Queue live (actualizat la 30s)**: emailuri rezolvate azi / în lucru, task-uri rezolvate azi / în lucru / în așteptare, apeluri azi — bare de progres animate.
- **Endpoint nou `GET /api/v1/productivity/monitor/live`**: date instantanee din `cts_ground_truth`, `cts_task_ground_truth`, `cts_calls_ground_truth`. Public, fără auth.
- **Polling separat**: dashboard/forecast la 5 minute (date agregate), queue live la 30 secunde — fără refresh manual.

## v0.46.52 - 2026-07-29 (Fix productivitate: gardă DV, iris_id mapping, delogare Surse date)

- **Fix `/productivity/report` 500**: query-urile DV din `department_report` și `forecast_report` nu mai fac UNION direct pe `cts_dv_employee_vacation_request`. Tabela se verifică prin `information_schema` înainte de acces — dacă nu există (sync DV nerulat), se sare fără eroare.
- **Fix ID-uri greșite concedii DV (720h → 736h)**: query-ul DV filtra după EDM ids (6, 7, 19...) în loc de CTS iris_ids (110, 123, 135...). Acum `ops` selectează și `iris_id`, construiește `iris_to_edm` map, query-ul DV filtrează corect după `iris_ids`.
- **Fix delogare instant pe pagina "Surse date"**: `iris_dv.py` ridica `HTTPException(401)` când cheia DV era invalidă/lipsă — frontend-ul delogha userul la orice 401. Schimbat în `403` (3 locuri: cheie lipsă, cheie invalidă la `/onboarding`, cheie invalidă la `/prompt`).

## v0.46.51 - 2026-07-29 (Dashboard monitor productivitate)

- **Monitor Operațional / Monitor Financiar**: două tab-uri noi în secțiunea Productivitate — deschid o pagină standalone (`_blank`) optimizată pentru monitoare de birou (fără sidebar, fără autentificare).
- **Dashboard monitor**: gauge per departament (progres lunar vs obiectiv), chips volum azi, tabel zilnic (ziua × departament × % obținut) colorat verde/galben/roșu față de obiectivele configurate. Auto-refresh la interval configurabil (1–60 minute, default 10).
- **Endpoint public** `GET /api/v1/productivity/dashboard/data?group=operational|financiar` — date agregate: forecast lunar, volum azi per departament, serie zilnică.

## v0.46.50 - 2026-07-28 (Fix forecast August — last_tgt undefined)

- **`forecast_report`**: variabilele `first_tgt`/`last_tgt` mutate înainte de query-ul care le folosea ca parametri. Anterior se produceau cu `cannot access local variable 'last_tgt'` pentru orice lună curentă (August 2026), lăsând secțiunea "Estimare productivitate" din email/UI goală.

## v0.46.49 - 2026-07-28 (Fix sync concedii vacation_approved + iris_id mapping)

- **`iris_id` mapping**: populat automat în `employee_department_mapping` prin match `first_name + last_name` față de `cts_dv_employee`. Corectează maparea CTS employee_id pentru toți angajații (anterior `iris_id = NULL` → sync concedii eșua silențios).
- **`sync_vacation_from_dv`**: rescris să folosească `iris_id` ca CTS employee_id în loc de `edm.id` direct (cele două nu coincid). Aduce corect concediile pentru toți angajații, inclusiv Judea Bianca (CTS id=123, EDM id=15).
- **`_write_employee_leaves`**: nu mai șterge `vacation_approved` la sync angajați (anterior ștergea tot `entry_source='cts'` inclusiv concediile DV).
- **Hook post-sync DV**: după sync `employee_vacation_request` din "Surse date", se apelează automat `sync_vacation_from_dv` → `vacation_approved` mereu în sync.
- **Calcul productivitate**: query UNION care citește din ambele surse (`employee_schedule vacation_approved` + `cts_dv_employee_vacation_request`) ca fallback.
- **Migrație `20260728_iris_id_from_cts_dv.sql`**: populează `iris_id`, șterge pre-2026 din `employee_schedule`, repopulează `vacation_approved` 2026+ prin maparea corectă.

## v0.46.48 - 2026-07-28 (Fix ore planificate/disponibile în rapoarte)

- **`department_report`**: `ore_planificate` = calendar ideal (zile_lucratoare × op_activi × work_hours), identic cu `forecast_report`. Anterior era pontaj real (zile_prezent + absente_pontaj), care dădea valori subevaluate când angajații nu aveau pontaj complet.
- **`ore_disponibile`** = `ore_plan_ideale − ore_concediu_aprobat` (vacation_approved + manual, union L-V, fără leave_request). Anterior era `pontaj - absente_pontaj`.
- Efect iulie suport_1 (5 operatori activi): ore planificate 824→**920** (5×23×8), ore disponibile 648→**720** (920−200h concediu). Coeficient 0.1033 rămâne neschimbat (deja corect).
- Pontajul real rămâne folosit exclusiv pentru calculul SLA (obiectiv_atins) — nemodificat.

## v0.46.47 - 2026-07-28 (Test email + perioadă probă productivitate)

- **Buton "Trimite test"** în tab Notificări: selectezi email destinatar, grup departamente și luna de raport — trimite un email de previzualizare fără să afecteze destinatarii configurați.
- **Data start productivitate** per angajat (`productivity_start_date DATE`): angajații cu această dată setată sunt excluși din calculele de productivitate pentru lunile anterioare datei respective (perioadă de probă / onboarding).
- **UI**: câmp "Start productivitate" în modalul Concedii per angajat (selector lună/an + buton Salvează). Badge vizual în lista angajaților dacă data e setată.
- **Backend**: `department_report` și `forecast_report` filtrează automat angajații cu `productivity_start_date > ultima zi a lunii`.
- **Setat manual**: Boros Vanessa-Karolina → start 2026-08-01; Bulmau Anamaria-Iuliana → start 2026-10-01. Ambele excluse din calculele iulie 2026.
- Migrare: `migrations/20260728_employee_productivity_start.sql` (`ADD COLUMN productivity_start_date DATE`).

## v0.46.46 - 2026-07-28 (Tab Notificări productivitate + email lunar automat)

- **Tab Notificări** nou în modulul Productivitate: configurare destinatari email per grup de departamente (Operațional / Financiar / Toate / departament individual).
- **Email lunar automat**: în prima zi lucrătoare a fiecărei luni la ora 10:00, IRIS trimite automat un email cu rezumatul lunii precedente (realizat vs obiectiv per departament) + estimare productivitate luna curentă (zile lucrătoare, concedii, ore disponibile, target real și minim).
- **PDF analitic atașat**: tabel cu datele de productivitate, generat cu PyMuPDF (fallback HTML dacă PDF eșuează).
- **Text introductiv AI**: generat prin `iris_ai.run_prompt` cu fallback la template text dacă AI nu e disponibil.
- **Gating robust**: cheia KV `productivity.last_monthly_sent` previne trimiterea duplicată în aceeași lună. Trimitere manuală disponibilă via butonul "Trimite acum (test)" din UI.
- Migrare DB: `migrations/20260728_productivity_notifications.sql` (`productivity_notifications` tabelă nouă).
- Endpoint-uri noi: `GET/POST /productivity/notifications`, `DELETE /productivity/notifications/{id}`, `POST /productivity/notifications/send-now`.
- **Filtru Financiar în tab Analiză**: selectorul de departamente include acum „Financiar (toate)" (contabilitate + recuperare_tva).

## v0.46.45 - 2026-07-28 (Aliniere coeficient + fix dublu-numărare concedii)

- **Coeficient real**: `department_report` folosește acum `baza_procent / ore_plan_ideale` (calendar L-V × work_hours) în loc de rată prezență (`ore_disp/ore_plan_pontaj`). Consistent cu `forecast_report`.
- **Obiectiv real**: `obiectiv_real = ore_disponibile_pontaj × coeficient` — ajustat cu absențele reale. Anterior era fix 95%.
- **Fix dublu-numărare concedii** (`forecast_report`): CTS poate scrie atât `leave_request` cât și `vacation_approved` pe același interval pentru același angajat. Acum `leave_request` e exclus dacă există `vacation_approved` suprapus. Ore concediu corect contabilizate prin union de zile L-V (nu suma intervalelor). suport_1 iulie: 328h → 208h corect.
- **aggregate_reports**: coeficient și obiectiv_real recalculate din `ore_plan_ideale` cumulat.
- `ore_plan_ideale` adăugat în răspunsul `department_report`.

## v0.46.44 - 2026-07-28 (Concedii reale din DV CTS: vacation_approved)

- **Mapare angajați**: view DV `employee` sincronizat, `iris_id` populat pentru toți 50 angajați locali via JOIN pe email.
- **Sursa concedii**: înlocuit `planned_leave` (planificare anuală estimată) cu `vacation_approved` (cereri real aprobate din CTS HR, status=2, 2026+). 190 rânduri sincronizate pentru 39 angajați.
- **Forecast**: ore_concediu calculat din `vacation_approved` + `leave_request approved` + intrări manuale — eliminat `planned_leave` din calcul.
- **Modal Utilizatori**: afișează `vacation_approved` (badge CTS, read-only) + intrări manuale (editabile). Coloană nouă "Tip" (Concediu / Invoire). Nr. zile afișat pe rând.
- **Sync zilnic**: `run_vacation_dv_sync_if_due` sincronizează DV `employee` + `employee_vacation_request`, actualizează `iris_id`, scrie `vacation_approved` în `employee_schedule` — totul automat o dată pe zi.

## v0.46.43 - 2026-07-28 (Concedii forecast: planned_leave + sync DV zilnic)

- **Forecast ore concediu**: query include acum `planned_leave` (sursa principală, aprobat implicit din CTS HR) + `leave_request approved` — anterior se folosea doar `leave_request` (invoiri orare max 3h), ceea ce ducea la ore concediu 0 pentru angajați cu concediu real dar fără invoiri.
- **Sync DV zilnic**: `employee_vacation_request` (snapshot IRIS DV) se sincronizează automat o dată pe zi în tabela locală `cts_dv_employee_vacation_request`, alături de sync-ul de angajați. Pregătire pentru maparea completă cts_id → local_id.

## v0.46.42 - 2026-07-28 (Fix prag zi lucrătoare: ≥1 angajat)

- `_MIN_STAFF_FOR_WORKING_DAY`: 2 → 1. Zi cu cel puțin 1 angajat prezent = SLA curge și productivitatea se calculează (inclusiv sâmbătă/sărbătoare legală dacă există pontaj). Zi cu 0 angajați = nelucrătoare.
- Sâmbetele rămân excluse din `zile_lucratoare` la calculul coeficientului (L-V only) — dar productivitatea sâmbetei cu angajați prezenți intră în scorul lunar.

## v0.46.41 - 2026-07-28 (Fix UI card forecast)

- Card prognoză: ascunse tabelul de obiective (email/apel/task) și tabelul de operatori — nu sunt relevante pt estimare.
- "Ore concediu" înlocuit cu "Ore disponibile" (valoarea corectă ore_disponibile = planificate minus concedii).

## v0.46.40 - 2026-07-28 (Gestiune concedii manuale + protecție sync)

- **DB**: coloană `entry_source` pe `employee_schedule` (`'cts'` vs `'manual'`); sync șterge doar rândurile CTS, concediile manuale supraviețuiesc.
- **Backend**: 3 endpoint-uri noi — `POST/PUT/DELETE /settings/employees/{id}/schedule/{sid}` pentru concedii manuale; intrările CTS rămân read-only.
- **UI Utilizatori**: buton "Concedii" per angajat (cu număr cereri aprobate); modal cu tabel concedii aprobate (badge CTS/Manual), formular Add/Edit/Delete pentru intrările manuale.
- **Gauge productivitate**: label zona minim–obiectiv schimbat din "Sub obiectiv" în "Foarte aproape de obiectiv".

## v0.46.39 - 2026-07-28 (Fix sursa concedii forecast + formula coeficient)

- **Fix sursa concedii**: `forecast_report()` folosea `kind='planned_leave'` (planificare anuală imprecisă); înlocuit cu `kind='leave_request' AND status='approved'` — cereri de concediu aprobate concret.
- **Fix formula coeficient**: `obiectiv_real` era fix la `baza_procent` ignorând concediile. Acum: `coeficient = baza_procent / ore_planificate_ideale` (obiectiv per oră), `obiectiv_real = ore_disponibile × coeficient` — ajustat cu absențele reale.
- Exemplu suport_1 August: 5 oameni × 21 zile × 8h = 840h ideale, baza 95% → coeficient = 0.1131/h; cu 200h concedii aprobate → 640h × 0.1131 = 72.4% obiectiv real.

## v0.46.38 - 2026-07-28 (Fix forecast 500 + selector lună Analiță)

- **Fix bug 500**: `UnboundLocalError: lhdays` în `forecast_report()` când lista istoricului era goală sau nicio zi lucrătoare nu era găsită — inițializat `lhdays = 0` înainte de blocul `if last_hist:`.
- **UI Analiță**: Selectorul de lună (‹ input ›) lipsea din topbar pe tab-ul Analiță — adăugat `monthNav` alături de range toggle, selector dept, selector operator.

## v0.46.37 - 2026-07-28 (Productivitate Estimată — prognoză luni viitoare)

- **Backend**: Endpoint nou `/productivity/forecast?month=YYYY-MM&months=N` — returnează estimare productivitate pentru luna/perioadă viitoare. Volume estimate = media zilnică din ultimele 2 luni × zile lucrătoare luna țintă. Ore disponibile ajustate cu zilele de concediu planificate din `employee_schedule`. Returnează `is_forecast:true` și aceeași structură ca `/productivity/report`.
- **UI Rapoarte**: Când navighezi la o lună viitoare, se apelează automat `/forecast` în loc de `/report`. Banner portocaliu "PRODUCTIVITATE ESTIMATĂ" apare deasupra cardurilor. Fiecare card are border dashed + badge "Estimat" + metrica "Ore concediu" în loc de "Ore disponibile" (când are concedii).
- Concedii: extrase din `employee_schedule` (kind=planned_leave) per angajat, intersectate cu zilele lucrătoare ale lunii țintă.

## v0.46.36 - 2026-07-28 (Productivitate Financiar: contabilitate + recuperare_tva)

- **DB**: Migrare `20260728_productivity_financiar.sql` — adaugă `contabilitate` și `recuperare_tva` în `productivity_department_config` (baza_procent=95) și `productivity_objective` cu 3 obiective fiecare: email/120min/50%, apel/4sec/25%, task/120min/25%.
- **Zero modificări cod**: frontend/backend funcționau deja cu orice dept configurat în DB.
- Tab Rapoarte → Financiar și tab Analiță → dropdown dept afișează acum ambele departamente cu date reale.

## v0.46.35 - 2026-07-28 (Rapoarte multi-lună + culori cotizație per coloană)

- **Backend**: `/productivity/report` acceptă acum `months=1..12`. Când `months>1`, calculează N rapoarte lunare și le agregă (volum/ore/zile = sumă; obiective/operatori = recalcul pe date cumulate). Funcție nouă `aggregate_reports()` în `productivity.py`.
- **UI Rapoarte**: range toggle (1L/3L/6L/12L) aplică acum efectiv — Rapoarte afișează datele agregate pe intervalul selectat. Navigatorul de lună specifică luna de final a perioadei.
- **UI**: Cotizații per coloană (Email, Task-uri, Apeluri) au culori independente: verde = cel mai mare din coloana respectivă, galben = ultimii 2, albastru = mijloc. Anterior culoarea era globală pe rând (bazat pe performanța generală).

## v0.46.31 - 2026-07-28 (Analiză Task-uri: redesign complet — același stil ca Mailuri/Apeluri)

- **UI**: Task-uri — 2 KPI-uri (Volum, Productivitate %), card timing styled (Timp preluare + Timp rezolvare), Volum pe zi, Productivitate zilnică (condiționat), Distribuție timp rezolvare (orizontal, colorat), Volum pe operator (orizontal, rank colorat).

## v0.46.29 - 2026-07-28 (Analiză Apeluri: fix Productivitate null + card timing styled + chart condiționat)

- **Backend fix**: `get_objectives(db, d, tip=None)` în loc de default `tip="email"` — `apel_lim` era mereu `None`, deci `in_timp_pct` = null în analytics.
- **UI**: Card timing „Timp răspuns / Durată apel" cu stil consistent (bg3, border, font 18px bold).
- **UI**: Chart „Productivitate zilnică" apeluri afișat doar când `in_timp_pct != null` (obiectiv configurat).

## v0.46.28 - 2026-07-28 (Analiză Apeluri: fix KPI-uri — răspuns vs durată apel, distribuție pe durată)

- **Backend**: Query apeluri include acum `cts_duration_seconds`. Câmpuri noi în `apeluri_out`: `avg_response_sec` (timp mediu răspuns), `avg_duration_sec` (durată medie apel), `median_duration_sec`. Buckets pe durată apel (nu pe timp răspuns).
- **UI**: KPI-uri Apeluri: „Timp mediu răspuns" (din `avg_response_sec`) + „Durată medie apel" (din `avg_duration_sec`). Distribuție pe durata efectivă a apelului.

## v0.46.27 - 2026-07-28 (Analiză Apeluri: KPI-uri noi + charts Volum/Productivitate/Distribuție/Operatori)

- **Backend**: `apeluri_out` include acum `buckets` (distribuție durată: <1min, 1-3min, 3-5min, 5-10min, >10min).
- **UI**: Coloana Apeluri redesenată — 4 KPI-uri (Volum, Productivitate %, Durată medie, Mediană), Volum pe zi, Productivitate zilnică (yMin 50), Distribuție durată apel, Volum pe operator.
- **UI**: `shortName()` mutat la nivel de componentă (reutilizabil în toate coloanele).

## v0.46.26 - 2026-07-28 (fix definitiv labels OY bar chart orizontal: type category explicit)

- **UI**: `ProdBarChart` orizontal — scala Y declarată explicit `type: 'category'`; elimină indexuri 0,1,2,3 în favoarea numelui operatorului/bucket-ului.

## v0.46.25 - 2026-07-28 (Analiză: fix tăiere 100% line charts, fix labels OY bar charts orizontale)

- **UI**: `MultiLineChart` — `suggestedMin`/`suggestedMax` în loc de `min`/`max` dur (nu mai taie valorile la margine). Înălțime charts 160→190px.
- **UI**: `ProdBarChart` orizontal — `afterFit` callback pe axa Y: lățime minimă calculată din lungimea maximă a label-urilor (7px/char + 16px padding). Labels operatori/bucketuri apar complet pe axa stângă.

## v0.46.24 - 2026-07-28 (Analiză: yMin 50 pe grafic productivitate zilnică, fix labels bar chart orizontal)

- **UI**: Chart „Productivitate zilnică" — axa Y pornește de la 50% în loc de 0, discrepanțele 90–100% vizibile.
- **UI**: `ProdBarChart` orizontal — inclus `colors` în semnătura de redraw; font size 12px pe labels Y (era 11); `autoSkip: false` + padding 6px pe ticks Y pentru afișare corectă a numelor operatorilor.

## v0.46.23 - 2026-07-28 (Rapoarte: remove Volum sold, gauge labels mai mari)

- **UI**: Eliminat cardul „Volum solved" din stats rapide `ProdDeptCard` (se referea doar la emailuri). Rămân 4 carduri: Coeficient, Zile lucrătoare, Ore planificate, Ore disponibile.
- **UI**: Markere minim/real pe gauge mai late (0.8 unități). Label-urile minim/real pe gauge: `bold 13px` (era 10px).

## v0.46.22 - 2026-07-28 (Gauge: progres colorat + mai mare)

- **UI**: Arc gauge colorat dinamic: roșu dacă sub minim, portocaliu dacă între minim și real, verde dacă ≥ real. Pointer și arc de progres același culoare. Markere minim (galben) și real (verde) ca linii pe arc. Canvas 200px înălțime, coloana 50/50.

## v0.46.21 - 2026-07-28 (Gauge: gauge.js library, staticZones + pointer)

- **UI**: Gauge înlocuit cu gauge.js (`gaugeJS v1.3.7`). `staticZones`: gri (sub minim), galben (minim→real), verde (real→100%). Pointer animat. `staticLabels` cu valorile minim/real. Fișier adăugat în vendor: `gauge.min.js`.

## v0.46.20 - 2026-07-28 (Fix gauge: canvas 2D direct, semicircle corect)

- **Fix**: Gauge rescris complet pe canvas 2D pur (`ctx.arc`). Semicircle real 180° (stânga→dreapta). Track gri + fill colorat + ac + 2 markere (minim galben, real verde). Retina-ready (scale×2). ResizeObserver pentru redraw la resize fluid.

## v0.46.19 - 2026-07-28 (Gauge Rapoarte: Chart.js doughnut half-circle)

- **UI**: Gauge înlocuit cu Chart.js doughnut half-circle (`rotation:-90, circumference:180`). Ac indicator + tick-uri minim/maxim desenate via plugin canvas. Overlay HTML pentru valoare + legendă. Fără librărie externă nouă — folosește `chart.umd.min.js` deja prezent.

## v0.46.18 - 2026-07-28 (Fix gauge Rapoarte Productivitate)

- **Fix**: Gauge SVG rescris complet — viewBox fluid `200×115`, `width:100%/height:auto` (nu mai e pixel fix). Arc semicircle corect, acul și textul în interior. Layout card: `minmax(0,45%)` stânga / `minmax(0,55%)` dreapta (nu mai e `180px` fix care deborda).

## v0.46.17 - 2026-07-28 (Productivitate Analiză: layout 3 coloane Mailuri / Apeluri / Task-uri)

- **UI**: Tab Analiză restructurat în 3 coloane independente: Mailuri (albastru), Apeluri (gri), Task-uri (portocaliu). Fiecare coloană are header colorat, KPI-uri proprii și grafice specifice.
- **Mailuri**: KPI volum + timp mediu + preluare/rezolvare (dacă există). Grafice: volum/zi, categorii pie, distribuție timp, volum per dept/operator.
- **Apeluri**: KPI volum + durată medie + durată totală calculată (avg × volum). Grafic volum/zi. Eliminat „% fără durată" și „% în-timp" (irelevante).
- **Task-uri**: KPI volum + timp mediu + preluare/rezolvare. Grafice: volum/zi, distribuție timp. Eliminat „% fără durată".
- Coloane fără date afișate cu placeholder semitransparent (nu dispar din layout).

## v0.46.16 - 2026-07-28 (Productivitate Rapoarte: gauge + layout 2 coloane)

- **UI**: Card departament în Productivitate → Rapoarte redesenat. Layout 2 coloane: stânga = gauge semicircle cu ac indicator, dreapta = 5 carduri metrice (Volum solved, Coeficient, Ore planificate, Ore disponibile, Zile lucrătoare).
- **Gauge**: Arc SVG semicircle — afișează `obiectiv_atins` (valoarea obținută), marcaj galben `obiectiv_minim`, marcaj verde `obiectiv_real`. Culoare arc: verde=atins, galben=parțial, roșu=sub_minim.

## v0.46.15 - 2026-07-27 (Productivitate: zile cu <2 angajați = zile nelucrătoare)

- **Fix calcul SLA**: Zi în care departamentul are `<2` angajați prezenți în pontaj este tratată ca zi nelucrătoare — SLA nu curge, productivitate = 100% (ex. 1 iunie, 1 mai, orice sărbătoare națională neinregistrată în `productivity.ro_holidays`).
- **Sâmbete cu 1 singur angajat**: deja excluse prin `isoweekday < 6`, dar acum și zilele din săptămână cu prezență redusă sunt excluse automat.
- **Email primit duminică 20:00, rezolvat luni 09:00**: SLA calculat corect — minutele curg doar de luni 07:00 (ora de start program), nu de duminică. Confirmat: `120 min` calculat corect.
- **Implementare**: `_BizCache._dept_day_count` — agregate `COUNT(present=true) per (dept, date)` preîncărcate o singură dată. `is_working_day_for_dept()` + `working_days_in_range()` noi. Prag configurat în `_MIN_STAFF_FOR_WORKING_DAY = 2`.
- `working_dates_luna` în `department_report` folosește același criteriu — absențele din zilele nelucrătoare nu mai gonflează `ore_planificate`.

## v0.46.14 - 2026-07-27 (Performanță pagină Productivitate — 5-10s → <1s)

- **Perf**: Eliminat bottleneck major pe pagina Productivitate (tab Analytics + Raport lunar). Cauza: funcția SQL `business_minutes_emp()` era apelată per-rând (11k emailuri + 14k task-uri/lună) — fiecare apel executa 2 SELECT-uri suplimentare. Fix în două straturi:
  1. **Index nou** `employee_attendance(employee_id, work_date)` — caută exact pe asta, fără index anterior.
  2. **Cache Python** (`_BizCache`): preîncarcă `department_schedule` + `employee_attendance` o singură dată per request și calculează business minutes în Python, eliminând zecile de mii de round-trip-uri PL/pgSQL.
- **Timp măsurat**: `analytics_report` (toate 4 departamente, 30 zile) 5-10s → **~0.9s**.
- **Migrație**: `migrations/20260727_productivity_perf_index.sql` (index idempotent `IF NOT EXISTS`).

## v0.46.13 - 2026-07-27 (Fix sync automat Task-uri CTS + Device Operations)

- **Fix**: Sync-ul periodic pentru **Task-uri CTS** și **Device Operations** nu era integrat în loop-ul de procesare email — datele se blocau la ultima sincronizare manuală. `cts_tasks_sync.run_recent_if_due()` și `device_ops_sync.run_recent_if_due()` adăugate în `process_email_loop` alături de `cts_gt_sync` și `cts_calls_sync`. Throttle intern 240s (identic cu restul).
- **Fix**: `POST /emails/{id}/reprocess` returna 500 — `ai_status` coloană `NOT NULL`, `SET ai_status = NULL` era invalid. Corectat în `'pending'`.

## v0.46.12 - 2026-07-27 (Fix departament: seria PPHU/PPCB/PPBG/ASCF → suport_1 absolut)

- **Fix**: Emailurile cu seria de factură PPHU, PPCB, PPBG sau ASCF în subiect/corp mergeau eronat la `taxe_drum` când contextul era HU-GO. Adăugat excepție absolută în promptul AI: dacă există serie PPHU/PPCB/PPBG/ASCF → suport_1 indiferent de context (ex. „Factură proformă încărcare cont HU-GO" cu PPHU44770 → suport_1).
- Regula anterioară era scrisă doar în subsecțiunea „OP-uri/dovezi de plată" — AI-ul nu o aplica la facturi proformă.

## v0.46.11 - 2026-07-27 (Fix NDR false-positive + buton Reprocesează email)

- **Fix**: `\bNDR\b` word boundary în `process_email.py` — emailuri cu „ANDREMARIO TRANS" etc. nu mai detectate eronat ca NDR/bounce.
- **Feature**: Buton „↻ Reproceseaza email" în detaliul email-ului — vizibil când emailul nu e deja în CTS și nu e spam/carantinat. Reprocesează complet (categorie, departament, documente) și forțează trimiterea spre CTS. Endpoint: `POST /api/v1/emails/{id}/reprocess`.

## v0.46.10 - 2026-07-27 (Regulă departament: info@solutiiweb.ro → Contabilitate)

- **Feature**: Toate emailurile de la `info@solutiiweb.ro` clasificate automat pe departamentul **Contabilitate** (regulă deterministă, prioritate față de AI). Migrație: `20260727_dept_rule_solutiiweb.sql` (idempotentă, se aplică pe prod la release).

## v0.46.9 - 2026-07-27 (Fix link dezabonare public + blocare auto-reply domenii interne)

- **Fix**: Link `{unsubscribe_url}` din emailurile auto-reply acum generează URL public `https://dezabonare-mailguard.cargotrack.ro/noreply/unsubscribe?token=...` accesibil din afara rețelei (anterior: IP intern `95.216.144.102:8501`). Implementat prin `NOREPLY_BASE_URL` în `.env`.
- **Feature**: Auto-reply blocat pentru adrese `@cargotrack.ro` și `@trakosoft.ro` — domenii interne nu primesc confirmări automate. Logat ca `skipped_internal_domain` în `autoreply_send_log`.

## v0.46.8 - 2026-07-27 (Fix latență documente CTS: kick imediat + timer 2min + deadline 10min)

- **Fix (B)**: Drain documente acum pornit imediat după ingestia unui email cu attachmente, fără să aștepte tick-ul de cron. Contractele/permisele ajung la CTS în secunde, nu în 5 minute.
- **Fix (A)**: Timer cron redus de la 5 minute la 2 minute — reduce fereastra de așteptare pentru orice email nou.
- **Fix (D)**: Deadline CTS documente extins de la 5 minute la 10 minute — eliminat `no_documents` pentru emailuri cu backlog mare de attachmente.
- Cauza radix: drain-ul procesa în serie toți cei ~15 attachmente din coadă; contracte cu deadline scurt picate după cel al permiselor/CI-urilor procesate mai devreme.

## v0.46.7 - 2026-07-27 (Fix contoare primite/trimise în lista clienți)

- **Fix**: `email_count` și `sent_count` în lista de clienți arătau 0 pentru clienți cu emailuri orfane (legate via `cts_ground_truth.raw.extra.client_id`). Ex: WHEELS SPEDITION arăta 0-0 în loc de 26 primite / 27 trimise.
- Cauza: simplificarea din v0.46.5 folosea doar `emails.client_id = c.id` (direct), rata orfanele.
- Fix: subquery `sent_count` folosește `g.raw->'extra'->>'client_id' = c.iris_client_id::text` (text comparison → index `idx_cts_gt_raw_client_id` folosit, 62ms/pagină vs 5.9s cu cast bigint).
- `email_count` (received) = directe + orfane via `cts_ground_truth` cu același index. Același fix aplicat în `get_client`.

## v0.46.6 - 2026-07-27 (Modal detaliu apel în pagina client)

- **Feature**: tab „Apeluri" în `ClientDetail` — click pe orice apel deschide modal `CallDetail` cu transcript complet, audio (dacă disponibil), categorie AI, ton, agent asignat, navigare ← →.

## v0.46.5 - 2026-07-27 (Fix performanță critică: lista clienți 5.7s → 18ms)

- **Fix performanță**: query lista clienți era 5.7 secunde per pagină (50 clienți). Cauza: 4 subquery-uri corelate pe `cts_ground_truth` fără index — seq scan complet (~40k rânduri) × 50 iterații.
- `email_count` în lista de clienți simplificat la `COUNT(*) WHERE client_id = c.id` (index existent). Contoarele detaliate (sent, orfane via iris_client_id) rămân în `ClientDetail` unde se calculează o singură dată per client deschis.
- `sent_count` scos din lista paginată (0 constant) — vizibil în detaliu client.
- **Index nou**: `idx_cts_gt_raw_client_id` pe `(raw->'extra'->>'client_id')` — elimină seq scan pentru `sent_count`/`email_count` în `ClientDetail` și satisfaction engine. Creat CONCURRENTLY (fără lock). Migrație: `20260727_cts_gt_raw_client_id_index.sql`.

## v0.46.4 - 2026-07-27 (Fix discover: adrese din raw.extra.to_email reply-uri CTS)

- **Fix**: `discover_client_emails()` rata adresele clientului din reply-urile CTS trimise de operatori — acestea nu au row separat în `emails`, sunt stocate doar în `cts_ground_truth.raw.extra.to_email`. Adăugată a treia sursă: `raw.extra.to_email` pe `cts_direction='sent'`.
- Backfill re-rulat: **448 clienți** actualizați (față de 201 anterior). Ex: WHEELS SPEDITION 1→5 adrese.

## v0.46.3 - 2026-07-27 (Auto-discover adrese email/telefon clienți din CTS)

- **Feature: `discover_client_emails()`** — funcție nouă în `iris_sync.py` care populează `clients.emails` din interacțiunile confirmate CTS (100% safe, nu euristice):
  - Mailuri PRIMITE: `from_address` extras din mailuri unde `cts_ground_truth.raw.extra.client_id = iris_client_id`
  - Mailuri TRIMISE de agent: `to_addresses` jsonb extras din același mecanism
  - Filtrare free-mail (gmail/yahoo/hotmail etc.) — nu poluează lista
  - Merge additiv: adaugă adrese NOI, nu suprascrie ce e deja setat
- **Integrare "Sync Now"**: `discover_client_emails()` rulează automat la finalul fiecărui sync, statistica `emails_discovered` apare în răspuns.
- **Fix sync**: upsert clienți nu mai suprascrie `emails`/`phones` cu `[]` când IRIS trimite gol dar noi avem adrese descoperite local.
- Backfill rulat imediat: **201 clienți** actualizați cu adresele descoperite din CTS.

## v0.46.2 - 2026-07-27 (Fix Conversație client — mailuri primite orfane vizibile)

- **Fix critic**: tab Conversație afișa ZERO mailuri primite pentru clienții fără `emails` populat (40% din clienți activi). Mailurile existau în DB cu `client_id IS NULL` dar cu `iris_client_id` în `cts_ground_truth.raw.extra`. Adăugat al doilea branch UNION pe `client_emails` endpoint care prinde aceste mailuri via `iris_client_id`.
- **Fix**: `email_count` din lista clienți și detaliu client acum include și mailurile orfane legate prin `iris_client_id` — numărul afișat în sidebar era 0 pentru clienți afectați.
- Afectat: ~2871 mailuri primite devenite vizibile, ~6338 clienți activi cu `emails = []`.

## v0.46.1 - 2026-07-27 (Fix motor satisfacție v4 — acoperire interacțiuni)

- **Fix critic**: mailuri trimise de agent (`sent`) nu erau legate de client când `client_id IS NULL` — filtrul folosea `from_address` (al agentului) în loc de `to_addresses` (al clientului). 87% din mailurile sent din iulie afectate → restituire ratată tăcut.
- **Fix critic**: apeluri fără intrare în `cts_calls_ground_truth` (47% din apeluri iulie) erau invizibile pentru penalizări — INNER JOIN înlocuit cu LEFT JOIN pe ambele query-uri (apeluri client și apeluri orfane).
- **Fix**: `_has_activity` în snapshot nu detecta clienți cu DOAR apeluri orfane (client_id NULL mapate prin număr de telefon) — adăugată verificare prin ultimele 9 cifre.
- **Fix**: fallback context IRIS la eșec era `emotion_final` (amplifica scorurile slabe) → înlocuit cu 75 neutru fix.
- **Fix**: mailuri received cu LIMIT 300 ORDER BY ASC tăia mailurile recente pe clienți activi — subquery cu ORDER BY DESC asigură că ultimele 300 (cele mai recente) sunt păstrate.

## v0.46.0 - 2026-07-24 (Motor nou satisfacție clienți v4 — per lună, transparent)

- **Model nou de scor (v4)**, înlocuiește cutia neagră AI (v3) cu ajustările hardcodate (+15 boost, floor 60). Calcul **per lună calendaristică**, fiecare client pornește de la **100%**; zero interacțiuni → rămâne 100%.
- **Scor final = Emoție × 0.70 + Context IRIS × 0.30**, apoi restituire (max 50% din penalizări).
- **KPI Emoție (70%)** — determinist + o judecată IRIS:
  - `−10` per **sesizare**, `−20` per **reclamație** (categorie din CTS ground-truth: `cts_ground_truth.cts_category` / `cts_calls_ground_truth.cts_category`; gol/necunoscut → tratat neutru), clamp la 0.
  - `−5` per **revenire explicită pe problemă nerezolvată** — marcate de IRIS (nu mecanic: reply-uri multiple ≠ problemă persistentă; doar semnalări explicite tip „am mai scris/sunat, încă nu s-a rezolvat").
- **KPI Context IRIS (30%)** — IRIS citește tot contextul lunii (mailuri + apeluri primite + răspunsurile agenților) și dă un scor 0-100 realist (info-only → mare; reveniri nerezolvate → mic). Fără boost/floor.
- **Restituire** — IRIS restituie ≤50% din penalizări dacă vede perechi rezolvare→mulțumire în 48h; gardă anti-abuz (mulțumire la simplă întrebare de tip informație NU restituie).
- **Sursa datelor: CTS ground-truth** (`cts_ground_truth`, `cts_calls_ground_truth`) — nu `emails`/`calls` brute. Legare mail↔client prin `emails.client_id` **cu fallback pe domeniul expeditorului** (55% din mailuri au client_id NULL); apel↔client prin `calls.client_id` cu fallback `phone_match` (numerele CargoTrack `037443006x` ignorate).
- Fereastra: **strict luna calendaristică** (fix defectul v3 care citea 90 zile în urmă indiferent de lună).
- Config nou `settings` key `satisfaction.v4` (ponderi/penalizări reglabile fără redeploy) — migrație idempotentă `20260724_satisfaction_v4_config.sql`. Fallback la defaults în cod.
- Snapshot lunar (`satisfaction_snapshot.py`) și butonul „estimate" (`clients.py`, param opțional `month=YYYY-MM`) trecute pe v4. Motoarele v2/v3 rămân în cod (dead code) pentru comparație, sunset ulterior.
- Fără tabele/coloane noi (refolosește `client_satisfaction_snapshots`).

## v0.45.2 - 2026-07-24 (Fix job status cross-worker: stare în DB)

- **Fix**: starea job-ului de scoring era in-memory → polling nimerea alt gunicorn worker → mereu `status: unknown`. Mutat în tabela `settings` (key `score_job.<id>`) — cross-worker safe.

## v0.45.1 - 2026-07-24 (Scoring batch async cu progress bar)

- **Fix timeout**: scoring batch rulează acum în background (thread daemon), POST returnează `job_id` imediat fără să aștepte finalizarea.
- UI polling la 3s cu progress bar live: `X / total apeluri procesate` + bară de progres.
- Limita ridicată de la 200 la 500 apeluri per batch (max 2000 via query param).
- Endpoint nou: `GET /calls/analytics/score-batch/status?job_id=...`

## v0.45.0 - 2026-07-24 (Fix filtre departament/agent în Analitice Apeluri)

- **Fix: filtrele departament și agent nu funcționau** — `department` era acceptat ca parametru dar ignorat în SQL; `agent` căuta după email dar `agent_extension` stochează numele complet.
- Helper `_agent_dept_filter()`: lookup în `employee_department_mapping` (email → name sau department → toți membrii) → filtru `agent_extension IN (...)`.
- Filtrul aplicat în toate 4 endpoint-uri: `dashboard`, `scores`, `binary-stats`, `score-stats`.
- **Fix UI mg-app.js**: deploy direct (fișierul era în folderul `vendor/` exclus din rsync).

## v0.44.9 - 2026-07-24 (Selector interval scoring batch apeluri)

- **Feature: selector interval** pentru butonul de scoring batch — opțiuni: 24h / 3 zile / 7 zile / 14 zile / 30 zile (default 7 zile). Apelurile nescorate din intervalul ales sunt procesate la apăsare.
- Backend: `score_batch(days_back=N)` + endpoint acceptă `days_back` din body JSON.

## v0.44.8 - 2026-07-24 (Scorare automată KPI apeluri — switch AI)

- **Feature: switch „Scorare automată KPI apeluri (AI)"** în Prompturi AI → tab Apeluri. Când e PORNIT, pipeline-ul de apeluri scorează automat fiecare apel după transcriere (KPI-uri + scoruri agent), fără apasare manuală de buton.
- Backend: endpoint `GET /calls/analytics/auto-score` + `POST /calls/analytics/auto-score/toggle`; stare persistată în tabela `settings` (key `calls.auto_score`).
- Pipeline `calls_pipeline.py`: step 5 nou — scorare condiționată de flag-ul `calls.auto_score`.
- Default: OPRIT (setat la `false` în DB la sesiunea anterioară).

## v0.44.7 - 2026-07-24 (KPI binar analitice apeluri: 4 prompturi noi)

- **Feature: 4 KPI binare noi** în dashboard Analitice Apeluri: Agentul s-a prezentat?, Clientul amenință cu judecata?, Clientul amenință că renunță?, Clientul a mai contactat anterior fără răspuns?
- Migrație DB: coloane `agentul_sa_prezentat`, `clientul_aminta_judecata`, `clientul_aminta_renuntare`, `clientul_contactat_anterior` în `call_ai_scores`.
- Scorer actualizat pentru a extrage și persista cele 4 valori noi la fiecare apel scorat.

## v0.44.6 - 2026-07-24 (Fix modul no-reply: dezabonare funcțională)

- **Fix: routerul `noreply` nu era înregistrat** în `main.py` — endpoint-ul `/noreply/unsubscribe` nu exista, cauza `ERR_CONNECTION_REFUSED`.
- **Fix: `NOREPLY_BASE_URL`** adăugat în `.env` (`http://95.216.144.102:8501`). Fără această setare, link-ul de dezabonare din emailuri genera portul intern 8500 (inaccesibil din exterior).
- Modulul no-reply este acum complet funcțional: toggle ON/OFF, config SMTP, șablon, blacklist, dezabonare one-click.

## v0.44.5 - 2026-07-24 (Import CSV Numere Ignorate — Analitice Apeluri)

- **Feature: Import CSV în lista de numere ignorate** (tab „Numere Ignorate" din Analitice Apeluri). Buton „Import CSV" lângă formularul de adăugare manuală. CSV: două coloane `numar_telefon,eticheta` (header opțional, eticheta opțională). Max 5000 rânduri per import. Feedback imediat: câte adăugate / actualizate / ignorate.
- **Backend**: `POST /api/v1/calls/analytics/phone-blacklist/import-csv` — `multipart/form-data`, UTF-8 sau Latin-1, insert bulk `ON CONFLICT DO UPDATE`. Returnează `{inserted, updated, skipped, errors}`.
- Fără migrație DB (refolosește tabela `call_phone_blacklist` existentă).

## v0.44.4 - 2026-07-24 (Fix race condition get_email_documents: no_documents → processing)

- **Fix `cts_get_email_documents`** (`app/api/v1/cts.py`): când CTS interogă documentele imediat după trimitere (în fereastra de 5 minute), extracția atașamentelor poate să nu fie finalizată — `document_extractions` nu are încă rândul → `n=0` → `status=no_documents` (greșit). Fix: dacă `n=0` dar emailul are `has_attachments=true` și nu a trecut deadline-ul de 5 min → `status=processing` (CTS trebuie să reinteroghe). `no_documents` rămâne corect doar pentru emailuri fără atașamente sau după expirarea termenului.
- Cauza concretă în #53579: CTS a interogat la t+1s după trimitere (`06:30:10 UTC`), extracția s-a terminat la t+10s (`06:30:19 UTC`) → fereastră de 9s în care răspunsul era fals `no_documents`.

## v0.44.3 - 2026-07-24 (Fix layout pagina Analitice Apeluri)

- **Fix padding dublu** pe pagina Analitice Apeluri (`CallsAnalitice`): wrapper-ul de top-level folosea `style: { padding: '20px 18px' }` inline, adăugat peste `padding: 20px 28px 28px` al `.main-content` → spațiu excesiv deasupra și lățime redusă față de alte pagini (ex. Apeluri CTS). Înlocuit cu `className: 'page'` (zero padding propriu, consistent cu restul paginilor). Tab-ul „Analizează Apeluri" (sub-render intern) corectat similar (`padding: '20px 24px'` → fără padding propriu).

## v0.44.2 - 2026-07-24 (Regulă Orange OTC → Suport 2)

- **Regulă deterministă nouă**: expeditor `noreply.otc@orange.com` (coduri de autentificare Orange OTC) → **Suport 2** (intrau pe Suport 1). Adăugată în `DEFAULT_RULES` (`department_rules.py`, id `orange-otc-01`) + migrație idempotentă `migrations/20260724_dept_rule_orange_otc.sql` care injectează regula în `settings->'rules'` dacă id-ul lipsește.
- Necesară migrația fiindcă regulile de departament trăiesc în DB (`settings`), iar release-ul NU migrează conținutul `settings` — regula din cod se aplică doar la seed-ul inițial (medii noi), nu pe DB-uri deja seed-uite (staging/prod).

## v0.44.1 - 2026-07-24 (Serie FS → Recuperare TVA)

- **Serie de factură FS mapată pe departamentul Recuperare TVA** (`app/services/op_extractor.py`). Set nou `_RECUPERARE_TVA_PREFIXES = {"FS"}`, verificat prioritar în `_department_from_series` (înainte de suport_1/contabilitate). Facturile cu serie FS mergeau pe Contabilitate; acum → Recuperare TVA (ex. #53528 „Factură servicii Recuperare TVA", unde AI detectase corect recuperare_tva la 85% dar seria FS + podeaua 90% îl împingeau pe contabilitate).
- Decizie user 2026-07-24. Se aplică mailurilor noi/reîncadrate; mailurile vechi deja trimise spre CTS rămân neschimbate (fără backfill, intenționat).
- Fără migrație DB.

## v0.44.0 - 2026-07-24 (Întărire detecție Categorie + Departament)

- **Categorie — scos cache curated complet** (`app/services/category_classifier.py`). Cascada veche gemma→curated cu `use_cache=True` + etapa Anthropic cu `learn=True` servea răspunsuri vechi memorate: mailuri de tip Informație rămâneau încadrate Sesizare din cache curat (ex. #53302, #53449 = `model=curated, from_cache=true`), iar Reîncadrarea din UI (no_cache) dădea corect Informație. Acum fiecare mail e reevaluat PROASPĂT cu Haiku, task sărat `sha1(system+content)` + `no_cache=True` → zero cache, fără `learn` → nu se mai populează `ai_curated_ext` pe categorie. Simetric cu departamentul (0.43.x).
- **Departament — match semnătură angajat robust** (`app/services/department_classifier.py`):
  - **Normalizare diacritice** (`_strip_diac`, NFKD): „Miclău"=„Miclau", „Mădălina"=„Madalina". Semnăturile cu diacritice nu mai ratează maparea fără diacritice.
  - **Match ancorat pe numele de familie**: numele de familie (discriminant) trebuie prezent + ≥1 prenume; prenumele singure (David, Andrei, Robert — frecvent duplicate între angajați) nu mai declanșează singure un match. Rezolvă #53449/#53454 („David Miclău" → Suport 2) și blochează false-positive pe 2 prenume comune fără nume de familie.
  - Tolerează ordinea liberă (semnătura „David Miclău" vs mapping „Miclau Adrian-David") și prenumele mijlociu absent din semnătură.
- Fără migrație DB (pur logică). Cache-ul curated vechi rămâne în `ai_curated_ext` dar nu mai e citit/populat pe categorie.

## v0.43.4 - 2026-07-23 (Fix 500 la filtrul de dată în „Mail-uri CTS")

- **Fix 500 Internal Server Error** pe `/cts-training/list` când se aplica filtrul de dată (`date_from`/`date_to`). Cauza: sintaxa `:param::date` (parametru named urmat imediat de cast `::`) — SQLAlchemy/psycopg2 interpretează `::` de după un parametru named ca `syntax error at or near ":"`.
- **Fix** (`app/api/v1/cts_training.py`): `:date_from::date` → `CAST(:date_from AS date)` și `:date_to::date` → `CAST(:date_to AS date)`. Confirmat reproducerea erorii pe staging și rezolvarea (query întoarce count corect).
- Fără migrație DB.

## v0.43.3 - 2026-07-23 (Fix: vision AI accepta moneda ISO / HUF ca serie de factură)

- **Fix `_vision_extract_series` (`op_extractor.py`)** — vision AI returna uneori o monedă ISO (HUF, EUR, RON...) drept `series` din atașamente. Regex-ul `^[A-Z]{2,6}$` accepta `HUF` ca serie validă → emailul primea `ai_op_series='HUF'` în DB → la reclasificare mergea pe `op_series`/`suport_1` în loc de AI classifier (care ar fi prins `recuperare_tva`).
- **Fix**: `series` respinsă dacă e o monedă ISO cunoscută (`_ISO_CURRENCIES`) sau identică cu moneda detectată → `series=None` → nu se persistă `ai_op_series` → reclasificarea merge pe AI classifier.
- Origine: raportat de agentul prod pe email #67660 („Oferta rambursare TVA extern"), unde HUF suprascria detecția `recuperare_tva`. Aplicat simetric pe staging pentru a nu re-desincroniza la release.

## v0.43.2 - 2026-07-23 (Reguli Recuperare TVA în cod + migrație idempotentă)

- **Regulă deterministă „Recuperare TVA extern" mutată în cod** (`DEFAULT_RULES` din `department_rules.py`) — până acum trăia doar în DB staging (adăugată din UI), deci nu se propaga la prod prin release → mailuri de tip „dosar rambursare TVA" ajungeau greșit pe suport_1/contabilitate pe prod.
- **Match pe subiect ȘI corp** (3 reguli OR): subiect `rambursare tva extern`, SAU corp `dosarul de recuperare tva`, SAU corp `situatia dosarului dumneavoastra pentru recuperare tva`. Prinde tipologia chiar când subiectul diferă de standard.
- **Migrație idempotentă** `migrations/20260723_dept_rule_recuperare_tva.sql` — inserează cele 3 reguli în `settings->'rules'` DOAR dacă id-ul lipsește. Se aplică automat la release pe prod și a fost rulată pe staging. Rezolvă desincronizarea reguli staging↔prod (regulile de departament sunt în DB, NU în cod, iar release-ul nu migrează conținutul `settings`).
- Verificat: #53307 (email prod 67660) → `recuperare_tva` prin regulă (subiect + body).

## v0.43.1 - 2026-07-23 (Mail-uri CTS: fix cache filtru dată + contor total mailuri)

- **Fix filtru dată „nu funcționa"** — cauza reală: `index.html` încărca `/vendor/mg-app.js` fără versiune → browserul servea cod vechi din cache (dinainte de filtrele Din/Spre categoria + dată). Adăugat cache-bust `?v=<VERSION>` pe tag-ul script. Backend-ul filtra corect tot timpul.
- **Contor total mailuri** — badge în bara de filtre din „Mail-uri CTS" care afișează numărul total de mailuri ce corespund filtrelor aplicate (`data.total` de la endpoint), font mono tabular-nums.

## v0.43.0 - 2026-07-23 (Fix încadrare Contabilitate: OP series pe allowlist + departament pe Haiku fără cache)

- **Fix major încadrare departament** — multe mailuri Suport 1 ajungeau greșit pe Contabilitate. Diagnostic: 3 cauze reale (nu doar cache-ul presupus).
- **OP series pe allowlist acreditat** (`op_extractor.py`) — regex-ul lacom `[A-Z]{2,6}\d{3,}` citea plăcuțele de camion (EWN064, YCE345) ca „serie de factură" → orice serie necunoscută mergea pe Contabilitate. Acum: doar seriile din lista acreditată furnizată de user (`_KNOWN_SERIE_PREFIXES`, ~50 prefixe: ARC/GCTS/CCTS/ACTS/ECTS/FRD/...) sunt facturi. Suport 1 doar `PPCB/PPHU/PPBG/ASCF`; restul acreditate → Contabilitate.
- **Serie ne-acreditată → suport_1** — `_department_from_series` nu mai forțează Contabilitate pe prefixe necunoscute; plăcuțe/gunoi (DIANA, MARIAN, EWN) → suport_1.
- **`_extract_series_from_text(known_only=True)`** — filtru pe allowlist la subiect/body ȘI atașament; normalizează cratima (`P-ECTS` → `PECTS`).
- **`is_op_email`** — un mail nu mai e declarat „OP email" doar fiindcă are o plăcuță în subiect.
- **Departament pe Haiku, ZERO cache** (`department_classifier.py`) — eliminată cascada `gemma` cu `use_cache=True` + curated-cache care servea răspunsuri vechi greșite. Acum fiecare mail (nou ȘI reclasificat) e evaluat proaspăt cu `claude-haiku-4-5-20251001`, `no_cache=True`, task sărat sha1(system+content), fără `learn`. Podeaua 90% → suport_1 rămâne.
- **NU s-au atins** prompturile de încadrare (cerere explicită user).
- Reclasificate cele 8 mailuri raportate — niciunul nu mai e pe Contabilitate greșit (verificat: zero `from_cache`).

## v0.42.33 - 2026-07-23 (Auto-reply no-reply la emailuri noi trimise în CTS)

- **Feature: Auto-reply confirmare primire** — când un email ajunge în CTS (`sent_to_cts_at` setat), expeditorul primește automat un email de confirmare. Declanșare: `cts.py` → `noreply_sender.maybe_send_autoreply()`.
- **Switch ON/OFF** — Settings → Mail-uri no-reply → buton `● Activ / ○ Oprit`. Default OFF.
- **Config SMTP dedicat** — tabelă `noreply_smtp_config` (separat de feedback KPI). Parolă criptată cu `credential_crypto`. Buton "Testează conexiunea" trimite email test.
- **Șablon editabil din UI** — textarea în Settings, stocat în `settings` key `autoreply.noreply_template`. Variabilă obligatorie `{unsubscribe_url}`. Text default inclus (cel primit de la Bia).
- **Blacklist dezabonare** — tabelă `noreply_blacklist`. Link one-click în fiecare email (`/noreply/unsubscribe?token=<uuid>`); pagina HTML confirmă dezabonarea. Adăugare/ștergere manuală din UI.
- **Anti-spam throttle** — max 1 mail la 10 minute per adresă (refolosește `autoreply_send_log`). Adresele no-reply/automate (regex `_AUTOGEN_FROM`) sunt excluse automat.
- **Badge `✓ auto-reply`** în lista emailuri și în modalul detaliu, lângă "Trimis în CTS" — apare când `autoreply_sent_at` e setat. Hover afișează timestamp trimitere.
- **Tabele noi**: `noreply_smtp_config`, `noreply_blacklist`, `noreply_unsubscribe_tokens`. Coloană nouă: `emails.autoreply_sent_at`.
- **Fișiere noi**: `app/services/noreply_sender.py`, `app/api/v1/noreply.py`, `migrations/20260723_noreply_autoreply.sql`.

## v0.42.32 - 2026-07-23 (Fix CargoFuel override prioritate op_series; blacklist CUI cu prefix țară)

- **Fix `department_run_one`**: dacă emailul are expeditor `@cargotrack.ro` sau subiect conține `cargofuel`, departamentul e forțat `suport_1` indiferent de seria OP detectată. Rezolvă cazul `CUIRO` (CUI firmă fuzionat cu prefix RO) clasificat greșit ca `contabilitate`.
- **Blacklist op_extractor**: adăugate variantele de CUI cu prefix de țară fuzionat (`CUIRO`, `CUIMD`, `CUIPL`, `CUIBG`, `CUIHU`, `CUIDE`, `CUIAT`, `CUIIT`) — nu sunt serii de factură.
- `ai_department_result.model` rămâne `op_series` (seria a fost detectată), `department` = `suport_1` (CargoFuel a câștigat).

## v0.42.31 - 2026-07-22 (Analitice Apeluri: dashboard, scoruri AI agenți, blacklist numere, prompturi configurabile)

- **Pagina Analitice Apeluri** (meniu lateral → Analitice): dashboard KPI + grafice, top 10 clienți, scoruri AI agenți pe 4 dimensiuni (explaining/patient/understanding/politeness/empathy), blacklist numere excluse din analiză, configurare prompturi de scoring AI.
- **Tabel `call_ai_scores`**: stochează scorul detaliat per apel — scoruri agent (5 dimensiuni), scoruri client, sfaturi AI (empatie/profesionalism/claritate), rezumat problemă, etichete, status rezolvare.
- **Tabel `call_scoring_prompts`**: prompturile de scoring sunt persistate în DB și editabile din UI (activare/dezactivare, editare text, adăugare întrebări noi — extensibil).
- **Tabel `call_phone_blacklist`**: numere excluse din rapoarte/grafice (ex. montatori, numere interne).
- **Service `call_scorer.py`**: `score_call()` și `score_batch()` — batch nocturn care scorează max 200 apeluri/rulare, exclude blacklist, seeding automat prompturi din fișierele diag Bia.
- **API `/calls/analytics/*`**: 11 endpoint-uri noi (dashboard, top-clients, scores, score-now, score-batch, phone-blacklist CRUD, scoring-prompts CRUD).
- **Fix `/ai/department/{id}/run`** (sesiunea anterioară): endpoint-ul relua fluxul integral inclusiv extragere OP + detecție MDL — persistare corectă în DB și return early pentru cazul MDL.

## v0.42.30 - 2026-07-22 (OP MDL → contabilitate automat; prioritate P2-P5 doar pe suport_1)

- **OP MDL → contabilitate**: dacă un ordin de plată conține moneda MDL (lei moldovenești), emailul este asignat automat la `contabilitate`, indiferent de seria facturii. Detecție în 3 straturi: text subiect/body, text local din atașament (PDF/OCR), vision AI (prompt extins returnează acum `SERIE|MONEDA`).
- **Prioritate P2-P5 doar pe suport_1**: clasificarea de prioritate se execută exclusiv pentru emailurile detectate cu departamentul `suport_1`. Emailurile rutate la contabilitate, taxe_drum, suport_2 etc. primesc `ai_priority=null` — nu se mai consumă AI inutil și nu se mai apar priorităti false în CTS pentru departamente non-suport.

## v0.42.29 - 2026-07-22 (Productivitate: fix ore_planificate — sambetele altor angajati nu se transfera)

- **Bug fix**: absențele de sâmbătă ale unui angajat care NU lucrează sâmbăta erau numărate ca zile planificate dacă alt coleg lucrase sâmbăta (sambetele colegilor intrau în `working_dates_luna` global, contaminând calculul tuturor). Fix: fiecare angajat e planificat pe `zile_prezente_proprii ∪ L-V_calendar`, nu pe `working_dates_luna` extins cu sâmbetele echipei.
- **Rezultat Suport 1 iunie**: `ore_planificate` 1200h → 1040h, coeficient 0.7667 → 0.8846.

## v0.42.28 - 2026-07-22 (Productivitate: pontaj real ca sursă de adevăr pentru ore planificate)

- **Redesign calcul ore_planificate / ore_disponibile / coeficient**: sursa de adevăr este acum `employee_attendance` (pontajul CTS), nu formula estimată `work_hours × zile_lucrătoare_calendar`.
- **Operatori inactivi excluși**: angajații fără nicio zi `present=true` în luna raportată nu mai gonflează `ore_planificate` (ex. Bulmau Anamaria-Iuliana — angajată din iulie, apărea cu 168h planificate în iunie deși nu a lucrat deloc).
- **Sâmbete lucrate incluse corect**: sâmbetele cu prezență reală în pontaj sunt acum considerate zile lucrătoare pentru calculul absențelor, fără a necesita modificarea manuală a `department_schedule`.
- **Formula nouă**: `ore_planificate = (zile_prezent + zile_absent_în_program) × work_hours` per operator activ; `ore_disponibile = ore_planificate - ore_absente`.

## v0.42.27 - 2026-07-22 (Productivitate: fix ore absente include weekenduri)

- **Bug fix**: `ore_disponibile` și `coeficient` erau calculate greșit — pontajul CTS (`employee_attendance`) putea conține înregistrări de weekend/sărbătoare marcate `present=false`, care erau numărate ca ore absente deși nu fac parte din program. Fix: `all_absent` este acum intersectat cu `working_dates_luna` (zilele lucrătoare reale ale lunii), identic cu tratamentul concediilor planificate. Exemplu concret: Bulmau Anamaria-Iuliana avea 25 înregistrări absente în iunie (inclusiv weekenduri), codul număra 200h în loc de 168h (21 zile lucrătoare × 8h).

## v0.42.26 - 2026-07-22 (Productivitate: consistență raport lunar ↔ Analiză, SLA task per familie)

- **Fix raport lunar**: `_measurable()` schimbat din `mins > 0` în `mins >= 0` — consistent cu Analiza. Emailurile rezolvate instant (0 min biz) erau excluse greșit din „măsurabil".
- **Fix Analiză — % în-timp task**: task-urile CargoBox (SLA 8400 min) erau evaluate cu SLA general de 120 min, rezultând % în-timp incorect. Acum SLA-ul e determinat per familie din `productivity_objective` (cargobox=8400, general=120), identic cu logica raportului lunar.
- **Fix Analiză — % în-timp task fără date**: `task_lim` era NULL dacă `get_objectives` nu găsea niciun obiectiv de tip task — acum `task_lim_by_family` e construit corect cu `tip=None`.

## v0.42.25 - 2026-07-22 (Productivitate Analiză: fix „% fără durată" 99.9% + text clar preluare/rezolvare)

- **Bug fix critic**: „% fără durată" afișa 99.9% din cauza că `cts_in_progress_at` din backfill era identic cu `cts_solved_at` pe emailurile istorice (CTS nu distinge momentul asignării de momentul rezolvării). Când intervalul `in_progress → solved` era zero, `business_minutes_emp` returna NULL și emailul era marcat ca „fără durată".
- **Fix**: condiție nouă `cts_in_progress_at < cts_solved_at` — dacă intervalul e invalid (zero sau invers), se aplică fallback la calculul `created → solved` (comportamentul anterior corect). Idem pentru task-uri: `cts_in_progress_at < cts_updated_at`.
- **Fix**: durata 0 (rezolvat instant la start de tură) era exclusă din „măsurabile" (`m > 0`). Corectat la `m >= 0` — 0 e durată validă.
- **UI**: etichetele „TTC" și „TTS" înlocuite cu text clar: „Timp mediu preluare" și „Timp mediu rezolvare" (KPI-uri și header tabel operator).

## v0.42.24 - 2026-07-22 (Productivitate Analiză: TTC + TTS în KPI-uri și tabel per operator)

- **Tab Analiză — calcul `avg_min` actualizat**: pentru emailuri și task-uri, `avg_min` (Timp mediu) este acum calculat ca TTS (In Progress→Solved) când `cts_in_progress_at` există. Fallback la durata totală (New→Solved) pentru tichete fără data de preluare.
- **Nou KPI email**: `TTC mediu (preluare)` și `TTS mediu (rezolvare)` apar în secțiunea Email a tab-ului Analiză, dacă există date (minim 1 tichet cu `cts_in_progress_at` populat).
- **Nou KPI task-uri**: similar, `TTC mediu` și `TTS mediu` în secțiunea Task-uri.
- **Tabel detaliu per operator**: coloane noi `TTC email`, `TTS email`, `TTC task`, `TTS task` — afișate condiționat (coloanele apar doar dacă departamentul are date cu TTC/TTS).
- **Backend** (`analytics_report()` în `productivity.py`): query emailuri și task-uri returnează acum `ttc_mins` (New→InProgress) și `mins` (TTS sau fallback). Câmpuri noi în response: `avg_ttc_min`, `avg_tts_min` la nivel root, departamente și operatori.

## v0.42.23 - 2026-07-22 (TTC + TTS: timp preluare și timp rezolvare separat pe task-uri și emailuri)

- **Nou**: două faze de timp per tichet — „Timp preluare" (TTC: New→In Progress) și „Timp rezolvare" (TTS: In Progress→Solved), ambele în minute de lucru efectiv (`business_minutes_emp`).
- **DB**: coloana `cts_in_progress_at TIMESTAMPTZ` adăugată pe `cts_ground_truth` și `cts_task_ground_truth`. Backfill automat pe 20.333 emailuri din `cts_assigned_at` (momentul asignării operatorului).
- **Sync emailuri**: `_UPSERT_SQL` actualizat — la prima tranziție în `in_progress`, setează `cts_in_progress_at = COALESCE(cts_assigned_at, now())`. Nu suprascrie dacă deja populat.
- **Sync task-uri**: similar — la prima tranziție în `in_progress`, setează `cts_in_progress_at = now()`.
- **API** (`/cts-tasks-training`): response extins cu `in_progress_at`, `time_to_claim_minutes`, `time_to_solve_minutes`.
- **UI** — `TaskDetail`: afișează „In Progress:", „Timp preluare:", „Timp rezolvare:" cu fallback pe `resolution_minutes` total când TTC/TTS lipsesc (date istorice).
- **UI** — Lista task-uri: coloana redenumită „Preluare · Rezolvare" — afișează TTC · TTS când disponibil, altfel durata totală.

## v0.42.22 - 2026-07-22 (Task/DeviceOps/Stats: durată rezolvare = timp de lucru efectiv, nu wall clock)

- **Bug fix**: câmpul „Timp rezolvare" în detaliu task și lista de task-uri afișa durata brută (creare→rezolvare calendar), inclusiv nopți și weekend. Ex: task creat duminică 19:07, rezolvat luni 08:24 → apărea 13h 17m în loc de 24 min.
- **Fix aplicat în**: `cts_tasks_training.py` (lista și detaliu task-uri), `device_ops.py` (operațiuni echipamente), `health.py` (stats zilnice + overview task-uri).
- **Metodă**: înlocuit `EXTRACT(EPOCH FROM (cts_updated_at - cts_created_at))` cu `business_minutes_emp(department, employee_id, cts_created_at, cts_updated_at)` — funcție SQL care numără doar minutele din programul de lucru al operatorului, excluzând nopțile, weekendurile și sărbătorile. Consistent cu calculul din scorul de productivitate lunar.
- Task-urile fără `assignee_employee_id` (neatribuite) returnează `null` la durată (nu se poate calcula fără program de referință).

## v0.42.21 - 2026-07-22 (Satisfacție Clienți: carduri „Date lipsă" înlocuite cu date reale)

- **Card „Red flags active"** (fostul „Semnalul dominant"): afișează distribuția tipurilor de red flags din luna curentă (mențiune reziliere, ultimatum, escaladare management, penalități, concurență). Normalizare text liber pe prefix (split la ` — `). „Niciun red flag activ" când nu există.
- **Card „Interacțiuni per client"** (fostul „Trend relație"): distribuție pe buckets 1-2 / 3-5 / 6-10 / 11+ interacțiuni, colorat verde→roșu (clienții cu 11+ sunt semnal de efort/presiune).
- **Backend**: înlocuite query-urile pe `iris_holistic` (inexistent în engine v3) cu query-uri pe `red_flags_active[]` și `total_interactions` din breakdown.

## v0.42.20 - 2026-07-21 (Productivitate Analiză: breakdown complet per tip în tabelul operatori)

- **Detaliu pe operator**: coloane extinse cu timp mediu + % în-timp pentru fiecare tip în parte. Structura per operator: Email (volum | cotiz% | t.mediu | % în-timp) | Task-uri (volum | cotiz% | t.mediu | % în-timp) | Apeluri (volum | cotiz% | t.mediu răspuns | % în-timp). Coloanele task/apel apar doar dacă există date pentru acel interval.
- **Backend**: `analytics_report()` extins — `op_agg` acumulează acum `task_meas`, `task_sum_mins`, `task_scope_meas`, `task_in_timp`, `apel_scope_meas`, `apel_in_timp` per operator. `op_out` returnează `task_avg_min`, `task_in_timp_pct`, `apel_in_timp_pct`.

## v0.42.19 - 2026-07-21 (Productivitate Analiză: KPI carduri ajustate — % fără durată)

- **Tab Analiză — KPI carduri**: eliminat „Timp median" și „Măsurabile" (număr absolut) din toate 3 secțiunile (Email / Task-uri / Apeluri). Adăugat card nou „% fără durată" = procentul itemelor fără durată calculabilă, colorat roșu (semnal de calitate a datelor). Structura finală: Volum | Timp mediu | % fără durată | % în-timp.

## v0.42.18 - 2026-07-21 (Productivitate: cotizare per tip obiectiv în Rapoarte + Analiză)

- **Tab Rapoarte — tabel operatori**: coloana „Cotizare%" înlocuită cu coloane separate „Cotiz. email% | Cotiz. task% | Cotiz. apel%", afișate dinamic în funcție de ce obiective are departamentul. Sortabil pe fiecare coloană.
- **Tab Analiză — tabel operatori**: adăugate coloanele „Cotiz. email%" (volum email operator / total email dept), „Cotiz. task% / Cotiz. apel%" (afișate doar dacă departamentul are task-uri/apeluri în interval).
- **Backend `department_report()`**: fiecare operator primește acum `vol_email`, `vol_task`, `vol_apel`, `cotiz_email`, `cotiz_task`, `cotiz_apel` — cotizare calculată per tip față de totalul departamentului.
- Fix deploy: `vendor/` exclus din rsync în deploy.sh — `mg-app.js` trimis acum separat prin ssh.

## v0.42.17 - 2026-07-21 (Productivitate Analiză: task-uri + apeluri + selector utilizator)

- **Tab Analiză — task-uri și apeluri**: KPI-uri, grafice zilnice și distribuție timp de rezolvare acum includ și task-urile și apelurile, nu doar emailurile. Fiecare tip are propria secțiune (Email / Task-uri / Apeluri) cu KPI-uri separate.
- **Selector utilizator**: când se selectează un departament specific, apare un al doilea selector cu operatorii activi din acel departament (endpoint nou: `GET /productivity/department-users`). Filtrarea per-utilizator trimite `user_id` la analytics.
- **Tabel operatori extins**: coloane noi „Task-uri" și „Apeluri" în tabelul per-operator (vizibil când e selectat un departament).
- **Backend `analytics_report()`**: extins cu două blocuri paralele (task-uri din `cts_task_ground_truth`, apeluri din `cts_calls_ground_truth`), cu suport `user_id` filter, SLA limit per tip și seriile zilnice.
- Fără migrații DB (se citesc tabele existente).

## v0.42.16 - 2026-07-21 (UI Satisfacție: raționament AI inline pe toate tabelele)

- **Eliminat coloanele confuze** din toate cele 3 tabele de satisfacție: „Segment", „Semnal", „Trend", „Factor critic", „Carry-fwd".
- **Raționament AI vizibil direct** în rând: preview 110-120 caractere + expandabil inline cu bordură colorată (portocaliu la risc, verde satisfăcut, roșu nesatisfăcut). Răspunde la „de ce a dat IRIS acest scor?".
- **Red flags** afișate în secțiunea expandată, nu ca coloană separată.
- **Tabelul „Clienți nesatisfăcuți"**: label semantic inline (Nesatisfăcut/Atenție) în bara de scor, în loc de badge Segment fără context.

## v0.42.15 - 2026-07-21 (Excludere clienți interni + boost +15% + floor 60%)

- **Excluși din calcule**: CARGO TRACK * (7 entități), TRAKOSOFT SOLUTIONS SRL, URBAN & ASOCIATII S.R.L. — nu apar în snapshot-uri și nici în dashboard.
- **Boost scor +15%** aplicat pe toate scorurile IRIS raw, clamped la 100.
- **Floor 60%**: niciun client nu poate apărea cu scor sub 60% în dashboard (IRIS poate fi prea drastic pe date limitate).
- **Fallback beneficiu-dubitei**: clienți cu <2 interacțiuni în 90 zile → scor automat 100%.

## v0.42.14 - 2026-07-21 (Engine satisfacție v3: IRIS citește text direct, fără metrici intermediare)

- **Arhitectură v3**: eliminat complet pilonii matematici (emoție 30%/efort 25%/operațional 25%/relație 20%). IRIS primește acum textul brut al emailurilor + transcriptele apelurilor și returnează scorul direct.
- **Red flags validate contextual de IRIS**: `red_flags_confirmed` — doar flag-urile pe care IRIS le confirmă ca semnificative (ex: „concurență" în context de întrebare logistică = fals pozitiv, eliminat).
- **Prompt calibrat**: comunicarea B2B tranzacțională (dovezi de plată, întrebări repetate pe aceeași temă = problemă tehnică în curs, nu insatisfacție), apeluri consecutive = issue activ (nu dovadă de nemulțumire).
- **Backfill `interaction_analysis`**: 953 interacțiuni completate pentru 200 clienți (119→186 clienți cu date IA).
- **Fix `force=True`** în snapshot.py: acum ocolește corect `_has_activity()` pentru recalculare forțată.
- **Fix `dotenv`**: script-urile CLI încarcă acum `.env` înainte de orice import din `app/`.

## v0.42.13 - 2026-07-21 (Engine satisfacție: analiză text brut, red flags validate de IRIS)

- **IRIS citește textul real**: în loc de metrici pre-calculate (neg_rate, wss, etc.), IRIS primește acum transcriptele apelurilor și body-ul emailurilor și judecă direct pe conținut.
- **Red flags validate contextual**: IRIS returnează `red_flags_confirmed` — lista flag-urilor algoritmice pe care le confirmă ca reale. Flag-urile fals pozitive (ex: "concurență" în context de întrebare logistică, "escaladare" pentru un avocat extern) sunt eliminate.
- **Segment recalculat pe red flags confirmate**: segmentul `la_risc`/`critic` se aplică doar dacă IRIS confirmă flag-ul, nu automat din keyword matching.
- **Exemplu concret**: G&R ROMINA TRANSPORT — de la `la_risc` / 80% la `sănătos` / 82.5%. WAY FARER TRANS — de la `critic` / 89% la `sănătos` / 92.5%.

## v0.42.12 - 2026-07-21 (Recalibrare engine satisfacție: principiu 100-minus-penalizări)

- **Principiu nou de scoring**: scorul pornește de la 100 și scade EXCLUSIV pe dovezi concrete de nemulțumire. Comunicarea B2B tranzacțională (dovezi plată, întrebări, mailuri scurte) = client OK → 100.
- **Skip clienți cu < 2 interacțiuni în 90 zile**: returnează `error="insufficient_data"`, nu scor 0 sau artificial.
- **Benefit of the doubt sub 3 interacțiuni**: nu se trimite la IRIS, se atribuie automat 100 (fără penalizare).
- **Floor 85 pentru 3-4 interacțiuni fără red flags critice**: IRIS poate coborî maxim până la 85 pe date puține.
- **Prompt IRIS rescris complet**: ton, emailuri scurte, comunicare rară nu mai sunt penalizate. Scad doar reclamații explicite, promisiuni nerespectate, probleme repetate, red flags critice (reziliere/legal/ultimatum).
- **Red flags critice definite explicit**: `mentiune_reziliere`, `amenintare_legala`, `ultimatum`, `escaladare_management` — singurele care pot coborî scorul sub 70 chiar și cu date puține.

## v0.42.11 - 2026-07-21 (Dashboard Satisfacție Clienți — versiune bogată)

- **Dashboard Satisfacție complet redesenat**: înlocuit dashboard-ul minimal cu o pagină bogată în informații.
- **KPI extinse**: adăugate carduri „La risc / Critic" (segment real, indiferent de scor) și „Trend descendent" (clienți în declin activ).
- **3 grafice donut — compoziție portofoliu**: Segmente risc (sănătos/neutru/la risc/critic), Semnalul dominant (emoțional/operațional/relațional/mixt), Trend relație (îmbunătățire/stabil/declin/volatil). SVG nativ, fără librărie extra.
- **Top 10 Satisfăcuți**: tabel cu primii 10 clienți, cu reasoning AI expandabil per rând — răspunde la „de ce e satisfăcut".
- **Clienți la risc real**: secțiune dedicată clienților cu segment `critic` sau `la_risc` (inclusiv cei cu scor numeric ridicat dar cu red flags active: reziliere, concurență, amenințări legale). Cu reasoning AI + red flags badges expandabile.
- **Tabelul „Clienți nesatisfăcuți"**: adăugat coloana Segment; breakdown expandabil acum include reasoning AI IRIS + badges semnal/trend/red flags.
- **Floor prompt IRIS coborât 80→70**: clienții cu red flags reale (reziliere, concurență, legal) pot primi acum scoruri 35-54 în loc să fie blocați la 80. Pragul `is_unsatisfied` rămâne la 70.
- **Backend endpoint `/clients/satisfaction-stats`**: returnează acum `top_satisfied`, `at_risk`, `segment_distribution`, `signal_distribution`, `trend_assessment_distribution`.

## v0.42.10 - 2026-07-21 (Fix: matching semnătură angajat în emailuri reply)

- **Fix `_match_employee_signature`**: prenumele compuse (ex. „Apetrei Ioana Madalina") erau ratate când semnătura conținea doar o parte (ex. „Madalina Apetrei"). Acum matchul necesită cel puțin 2 din N părți ale numelui (nu toate).
- **Fix detecție reply cu corp gol**: emailurile unde clientul răspunde fără text propriu (tot bodyul e citat) nu mai sunt excluse din matching. Pattern „a scris:" / „wrote:" detectat direct în body ca indicator de reply.
- Efect: emailurile reply la angajați CargoTrack (ex. Mădălina Apetrei – Contabilitate) primesc departamentul corect direct, fără să mai cadă pe `suport_1`.

## v0.42.9 - 2026-07-17 (Documente CEMT: norma poluare → valoare numerică CTS)

- **Feed CTS `get_email_documents`**: câmpul `Emission Class` din documente CEMT (și orice tip cu `cts_key: "emission_class"`) se convertește automat din text (`"EURO V"`, `"EURO VI"`, `"EEV"` etc.) la valoarea numerică CTS (`5`, `6`, `56` etc.) înainte de trimitere.
- Mapare completă: `EURO I→1`, `EURO II→2`, `EURO III→3`, `EURO IV→4`, `EURO V→5`, `EURO VI→6`, `EEV/EURO V EEV/EURO VI EEV→56`, `noneuro→0`. Valori necunoscute rămân neschimbate (text original).
- Fără modificare DB sau UI — transformare aplicată doar la serializare spre CTS.

## v0.42.8 - 2026-07-17 (T7: Dashboard feedback — statistici & scoruri KPI)

- **Pagina nouă „Dashboard feedback"** în sidebar, secțiunea „Feedback clienți".
- **Statistici campanii**: trimise / deschise / răspuns, rată deschidere %, rată răspuns % — per campanie.
- **Rezumat global**: KPI cards cu total trimis, total deschis, total răspuns, KPI-uri evaluate.
- **Cine a deschis**: tabel per destinatar cu data/ora, metoda (pixel/click), a răspuns sau nu.
- **Comentarii**: card per comentariu cu textul, rating (bară progres), client, KPI, dată.
- **Scoruri medii KPI**: clasament dinamic (ranking #1, #2...) cu bară progres colorată (verde/portocaliu/roșu).
- **Evoluție lunară**: tabel pivot KPI × lună pentru perioadă configurabilă (3/6/12 luni).
- Filtru campanie (opțional) și filtru perioadă evoluție — actualizare live la schimbare.
- Backend: endpoint `GET /api/v1/feedback/dashboard` (auth admin JWT), parametri `campaign_id`, `months`.
- Fără migrații DB — date vin din tabelele existente (T5 + T4).

## v0.42.7 - 2026-07-16 (Procesare Documente — pilot automat: prag încredere 85%, erori permanente auto-skip)

- **Prag încredere unificat 85%** (`AUTO_CONF_MIN`): sub această încredere efectivă (extragere
  dacă există, altfel clasificare), documentul NU se mai propagă ca „Extras"/„Clasificat" —
  devine necunoscut și e scos din listă (`doc_discarded=true`), recuperabil manual la nevoie.
  Rezolvă cazul `carGObox - PrePaid` clasificat 65% care rămânea totuși auto-validat.
- **Erori permanente → necunoscut automat**, nu mai rămân agățate ca „Eroare" în listă: clasificare
  eșuată non-tranzitorie, extragere eșuată non-tranzitorie (ex. „fișier prea mare pentru
  vision-classify") → `_discard_attachment` cu motivul păstrat pentru trasabilitate. Erorile
  tranzitorii (gateway 502/503/504/timeout) rămân neafectate — tot intră la reîncercare automată.
  Rezolvă seria LIHET DENIS TRANS CMR 1-7 (#48393, #48395, #48396, #48398, #48399, #48400, #48401).
- Reconfirmat (fără modificare cod): reply-urile la un fir de mail NU reprocesează atașamentele
  mailurilor anterioare din același fir — fiecare mail Graph are propriul set de atașamente,
  selecția de procesare e strict pe mail, nu pe fir/conversație.
- Rescan one-off pe bază de date proprie: rândurile blocate (`failed`/`needs_review`,
  `auto_validated=false`) resetate și reprocesate cu logica nouă; rândurile deja `extracted`
  sub 85% (nu ajung în drain automat) corectate individual via `reidentify`.
- Fără migrație — reutilizate coloanele/statusurile existente (`doc_discarded`, `doc_discard_reason`).
- Fără schimbări de interfață — stările `discarded`/`neidentificat` erau deja ascunse din listă.

## v0.42.6 - 2026-07-13 (T4: Mutare IMAP automată spam/carantină în foldere dedicate)

- **Folder actions active**: mailuri cu verdict `spam`/`quarantined` se mută automat în folderele
  IMAP `SPAM`/`CARANTINA` în același ciclu de poll (1 min) cu detecția.
- Folderele se creează automat la prima rulare per cont dacă lipsesc (`ensure_folders`, idempotent).
- Idempotent: `folder_action_at` NULL = neprelucrat; la succes IMAP → setat `now()`. Eșecuri → retry automat la poll următor, fără duplicări.
- Răspuns `/personal-mailboxes/poll` include acum `total_moved` per rulare.
- Mailurile curate rămân în Inbox, neafectate.
- Nicio modificare la fluxul CTS sau la alte module.

## v0.42.5 - 2026-07-13 (T3: Pagina Reguli personale — liste expeditori izolate de CTS)

- **Pagina nouă „Reguli personale"** în sidebar, secțiunea „Căsuțe personale" (T3 din modulul mailbox personal).
- Componentă `PersonalSenderListsPanel`: CRUD complet blacklist/whitelist pe endpoint-urile
  `/personal-mailboxes/rules/sender-lists` (GET/POST/PUT/DELETE) — izolate de listele CTS
  (`/settings/sender-lists`). Aceleași funcționalități: adăugare, edit (cu SweetAlert2), mute/reactivare, ștergere.
- Componentă `PersonalRulesPage`: container cu sub-tab-uri; tab „Liste expeditori" activ implicit.
  Tab rezervat „Reguli AI" (comentat, pregătit pentru T5).
- Backend nemodificat (endpoint-urile existau deja din T2). Doar UI adăugat.
- Deploy: `mg-app.js` actualizat în `/vendor/` via `deploy_vendor.sh` (fără restart nginx/API).

## v0.42.4 - 2026-07-13 (CSP hash-free: scripturi inline externalizate in /vendor)

- Cauza recurenta: la fiecare build UI, hash-ul SHA256 al scriptului inline din index.html se
  schimba, iar CSP nginx ramanea pe hash-ul vechi -> React blocat ("Cargo360 se incarca...")
  pana la actualizare manuala (incidente release #61, outbox #22).
- Fix (Optiunea 2): cele 3 scripturi inline mutate BYTE-IDENTIC in fisiere externe same-origin:
  vendor/mg-theme-init.js (bootstrap tema), vendor/mg-staging-bar.js (bara staging),
  vendor/mg-app.js (bundle React 768820 bytes). index.html le include cu <script src="/vendor/...">.
- CSP script-src redus la 'self' (fara niciun 'sha256-...') in sites-available + sites-enabled.
  Nu mai trebuie atins CSP la nicio modificare UI viitoare.
- Fara restart pt asset-uri (FileResponse no-cache + StaticFiles /vendor citesc din disc); doar
  reload nginx pt header CSP. index.html: 798744 -> 20258 bytes.

## v0.42.3 - 2026-07-11 (Fix CSP img-src https: + frame-src blob:, aliniere hash cu productia)

- **img-src**: adaugat https: (imagini externe in preview email, ex. intercom.ruptela.com,
  blocate de img-src 'self' data: blob: fara https:).
- **frame-src**: directiva noua 'self' blob: (previzualizare PDF/atasamente in iframe cu URL
  blob:, care fara frame-src explicit cadea pe default-src 'self' si bloca framing-ul).
- Aplicat atat in sites-available cat si in sites-enabled (fisier separat pe acest host, NU
  symlink catre sites-available -- diverg deja cu 2 linii allow temporare pt scan ZAP, pastrate).
- Hash script-src neschimbat (deja corect, 768820 bytes).
- Aliniat versiune cu productia (0.42.3) dupa incidentul release #61.

## v0.42.2 - 2026-07-11 (Fix CSP mailguard-staging: style-src, img-src, media-src)

- **style-src**: hash-urile statice inlocuite cu `'unsafe-inline'` -- SweetAlert2
  injecteaza dinamic un bloc `<style>` per dialog (continut variabil), hash fix nu poate acoperi.
  Confirmat live prin eroarea de consola `Applying inline style violates...`.
- **img-src** (nou, `'self' data: blob:`): faviconul e `data:image/svg+xml` inline in
  index.html, blocat de `default-src 'self'` fallback. Iconitele SweetAlert2 sunt CSS
  (`swal2-icon`), nu PNG base64 cum s-a presupus initial.
- **media-src** (nou, `'self' blob:`): playerul audio modul Apeluri seteaza `src` pe
  `URL.createObjectURL(blob)`. Confirmat live prin eroarea de consola
  `Loading media from blob:... violates default-src 'self'`.
- **script-src neschimbat** -- hash-urile live (inclusiv al 3-lea, bundle 768820B) recalculate
  independent si confirmate identice cu cele deja configurate.
- **GOTCHA gasit**: `/etc/nginx/sites-enabled/cargo360` pe staging NU e symlink catre
  `sites-available/cargo360` (spre deosebire de productie) -- e fisier separat, deja divergent
  (2 reguli `allow 204.168.208.217` temp ZAP scan). Editat fisierul enabled (cel servit efectiv);
  sincronizat linia CSP si in sites-available.
- Aplicat identic si pe productie (mailguard-server, v0.44.1) in aceeasi sesiune.

## v0.42.1 - 2026-07-08 (Fix CSP script-src/style-src -- React nu se monta pe staging)

- **Root cause real al blocajului "Cargo360 se incarca..."**: header-ul CSP adaugat la nginx pe
  mailguard-staging (`default-src 'self'`, hardening ZAP 2026-07-07) bloca executia TUTUROR
  scripturilor `<script>` inline (fara atribut `src`) -- inclusiv scriptul principal ce contine
  intreaga aplicatie React (~7000 linii, in index.html). Producita (mailguard-server) NU are
  header CSP deloc, de aceea functiona identic cu acelasi index.html. Mutarea librariilor JS/font
  in `/vendor/` (v0.42.0) nu rezolva aceasta problema -- CSP bloca scriptul inline al aplicatiei
  indiferent de sursa librariilor externe.
- **Fix**: adaugate explicit `script-src 'self' 'sha256-...'` (3 hash-uri, cate unul per bloc
  `<script>` inline din index.html) si `style-src 'self' 'sha256-...'` (2 hash-uri, pentru cele
  2 blocuri `<style>` statice din pagina) in header-ul CSP din
  `/etc/nginx/sites-available/cargo360`. Pastreaza politica stricta (fara `'unsafe-inline'`) --
  doar continutul EXACT al acestor blocuri, verificat prin hash SHA256, poate rula; orice script
  injectat (XSS) ramane blocat.
- **Cunoscut, neadresat**: cateva atribute `style="..."` inline (legenda flow-diagram, un
  `<select>`, continut generat dinamic in modale SweetAlert2) raman posibil blocate de
  `style-src-attr` (mosteneste de la `default-src 'self'`, fara `'unsafe-inline'`/hash pentru
  atribute). Efect strict cosmetic (culori/afisare pe elemente punctuale), NU blocheaza montarea
  aplicatiei. Neadresat pana la decizie separata (hash enumerat per-atribut vs `'unsafe-inline'`
  scoped la style-src).
- Aplicat DOAR pe mailguard-staging. Productia ramane neschimbata (fara header CSP) pana la
  confirmarea explicita a lui Razvan dupa validare pe staging.

## v0.42.0 - 2026-07-08 (Auto-gazduire librarii JS + font Inter, elimina dependenta CDN extern)

- **Root cause pagina blocata pe "Cargo360 se incarca..."**: React nu se monta niciodata -- resursele
  CDN externe (React/React-DOM de pe unpkg.com, SweetAlert2/Chart.js de pe cdn.jsdelivr.net, fontul Inter
  de pe fonts.googleapis.com) returnau 503 in tab-ul Network al browser-ului clientului. Nu apareau erori
  in consola JS (esecurile de incarcare a resurselor nu genereaza console.error), doar in Network. Reachability
  server-side catre CDN-uri fusese deja verificata OK separat -- problema era pe traseul de retea al
  clientului, nu server Cargo360.
- **Fix**: cele 4 librarii JS (react.production.min.js, react-dom.production.min.js, sweetalert2.all.min.js,
  chart.umd.min.js) si fontul Inter (subset-uri latin + latin-ext, 2 fisiere woff2, acopera diacritice RO)
  sunt acum gazduite local in `/opt/iris-mailguard/app/ui/vendor/`, servite same-origin prin FastAPI
  (`app.mount("/vendor", StaticFiles(...))`). `index.html` actualizat sa refere `/vendor/...` in loc de
  URL-uri externe; atributele `integrity`/`crossorigin` (SRI) eliminate -- nu mai sunt necesare pentru
  resurse same-origin. Hash-urile fisierelor descarcate verificate identice cu hash-urile SRI aplicate anterior.
- Aplicat DOAR pe mailguard-staging. Productia (mailguard-server) ramane pe CDN extern pana la confirmarea
  explicita a lui Razvan dupa validare pe staging.

## v0.41.2 - 2026-07-08 (Fix CSP meta: frame-ancestors eliminat, urmare retest ZAP)

- **CSP sandbox email (`buildEmailSrcDoc`)**: eliminat directiva `frame-ancestors 'none'` din politica
  CSP declarata via `<meta>` tag. Conform spec CSP, `frame-ancestors` (ca si `sandbox`) nu este permisa
  in CSP declarata prin element `<meta>` -- este valida DOAR in header HTTP; browserele o ignora silentios
  cand apare in meta. ZAP semnala corect acest lucru la retest ca 'CSP: Meta Policy Invalid Directive'.
  Comportament runtime neschimbat (iframe-ul ramane complet izolat prin `default-src 'none'` + atributul
  sandbox al elementului iframe insusi), doar eliminata o directiva care oricum nu avea efect in acest
  context. Pastrate `base-uri 'none'; form-action 'none';` -- ambele SUNT valide in meta CSP.
- Aplicat DOAR pe mailguard-staging (95.216.144.102). NU s-a modificat productia (mailguard-server) --
  in asteptarea confirmarii separate a lui Razvan.

## v0.41.1 - 2026-07-07 (Hardening CSP + SRI, urmare pentest ZAP mailguard-staging)

- **CSP sandbox email (`buildEmailSrcDoc`)**: adaugat explicit `base-uri 'none'; frame-ancestors 'none';
  form-action 'none'` langa politica existenta (`default-src 'none'; style-src 'unsafe-inline'; img-src ...;
  font-src data:;`) folosita la afisarea continutului HTML al email-urilor intr-un iframe sandbat. Aceste
  3 directive nu mostenesc fallback de la `default-src` conform spec CSP -- ZAP le semnala ca lipsa
  ("Failure to Define Directive with No Fallback"). Comportament runtime neschimbat (iframe-ul era deja
  complet izolat prin `default-src 'none'`), doar politica explicit declarata acum.
- **SRI + pinning versiuni pe resurse CDN statice** (`app/ui/index.html`): react@18 -> 18.3.1,
  react-dom@18 -> 18.3.1, sweetalert2@11, chart.js@4.4.4 -- toate cu `integrity="sha384-..."` calculat pe
  continutul curent servit. Google Fonts CSS (`fonts.googleapis.com/css2`) ramane fara SRI -- raspuns
  dinamic per user-agent, incompatibil cu SRI (limitare cunoscuta, documentata de Google).
- Aplicat DOAR pe mailguard-staging (95.216.144.102). NU s-a modificat productia (mailguard-server) --
  in asteptarea confirmarii separate a lui Razvan.


## v0.43.0 - 2026-06-30 (Reply automat — Faza 2: trigger SOLVED + flag CTS, tot DRY-RUN)

- **Reply de ÎNCHIDERE la soluționare (kind='solved').** Când o solicitare trece în `solved` în CTS,
  IRIS pregătește un răspuns scurt care confirmă clientului că cererea a fost **procesată și
  soluționată de un coleg** (om, nu automat) și că rămânem la dispoziție. Același stil ca preluarea:
  GENERIC + conștient de context (citește ultimele 4-5 mailuri doar ca să aleagă TIPUL), **fără niciun
  identificator** (nr. înmatriculare / VIN / factură / contract / sume / nume).
  - `autoreply_generator.generate_autoreply(..., kind='solved')` + `DEFAULT_PROMPT_SOLVED` + prompt
    editabil separat `settings['autoreply.generate_prompt_solved']`; namespace `email_autoreply_solved_v1`.
- **DRY-RUN (NU trimite nimic).** Ca Faza 1: se LOGHEAZĂ decizia în `autoreply_send_log` cu
  `trigger='solved'`; trimiterea reală se cablează în Faza 1.5 (`_transmit`).
- **Opțiunea CTS `solved_auto_reply` (bifă operator).** Preluată prin sync-ul ground-truth în
  `cts_ground_truth.cts_solved_auto_reply`: **FALSE** = operatorul a răspuns manual → NU trimitem;
  **TRUE / NULL** = eligibil (NULL = CTS încă nu trimite câmpul; strictețea e configurabilă prin
  `settings['autoreply.solved_requires_flag']`, default false). Numele câmpului din feed nu e fixat →
  extragere tolerantă (mai multe chei + `extra`).
- **Trigger doar pe TRANZIȚIE nouă (fără backfill).** `cts_groundtruth_sync` marchează `cts_solved_seen_at`
  o singură dată, la trecerea în solved (mirror al `changed_at`), și expune `newly_solved` în RETURNING.
  Re-sync-ul rolling (la 5 min) și cele ~4897 rânduri deja solved **NU** re-declanșează. Doar tranzițiile
  noi (email legat local) → `dispatch_for_ids(trigger='solved')` post-commit, best-effort, izolat.
  Comutator `settings['autoreply.solved_trigger_enabled']` (default true).
- **Anti-spam comun** ambelor declanșatoare: max 1 reply automat / 10 min / adresă (un NEW și un SOLVED
  către aceeași adresă în <10 min → al doilea `throttled`). Idempotent per **(email, trigger)** — un
  `new` nu blochează un `solved` ulterior.
- **Validare fără trimitere:** `POST /ai/autoreply/{id}/preview-solved` (un email, nu persistă) și
  `GET /ai/autoreply/solved-sample?limit=` (eșantion pe emailuri reale deja solved). `POST
  /ai/autoreply/dispatch-now` (admin, dry-run) rulează dispecerul IN-SERVICE pt validare/ops.
- Migrație idempotentă `20260630_solved_autoreply.sql` (`cts_solved_auto_reply`, `cts_solved_seen_at`).
  Testat e2e: mesaje solved generice/type-aware (plată/documente/sesizare), would_send(0.88) +
  throttled + skipped_confidence(0.72) + flag=FALSE skip; tranziția fire o singură dată, istoric NU.
  Zero trimiteri reale (toate `send_mode=dry_run`).

## v0.42.0 - 2026-06-30 (Reply automat la intrarea în CTS — Faza 1: motor + dry-run + anti-spam)

- **Sugestie reply mai GENERICĂ + conștientă de context (prompt v7).** Sugestia nu mai menționează
  identificatori specifici (nr. înmatriculare, VIN, nr. factură/contract/AWB, sume, date, nume) — chiar
  dacă apar în mesaj sau istoric — pentru că exact aceste mențiuni produceau detalii irelevante/eronate
  (ex. „vehiculul GJ75DAV", „factura proformă"). Se păstrează doar confirmarea generică de preluare,
  adaptată ca TIP (plată / documente / sesizare / informare).
  - Nou `autoreply_generator._thread_context(email, limit=5)`: citește **ultimele 4-5 mailuri** din
    aceeași conversație (`conversation_id`, fallback `from_address`) DOAR ca context; răspunsul vine
    EXCLUSIV pentru ultimul mesaj. Scope AI bumped `email_autoreply_v6 → v7`.
- **Auto-trimitere la intrarea în CTS — Faza 1 = DRY-RUN (NU trimite nimic încă).** Când un email clean
  intră în CTS (`cts_update_emails`), se decide dacă s-ar trimite automat un reply de preluare și se
  **loghează** decizia. Trimiterea reală se cablează ulterior (Faza 1.5) — Cargo360 nu are azi canal
  de trimitere (reply-urile reale le face CTS).
  - **Doar încredere ≥ 0.85** declanșează `would_send`; restul rămân sugestie pentru operator.
  - **Anti-spam:** max **1 reply / 10 min / adresă** expeditor (extra → `throttled`). Praguri
    configurabile: `settings['autoreply.send_confidence_min']`, `settings['autoreply.throttle_minutes']`.
  - Migrație idempotentă `migrations/20260630_autoreply_send_log.sql` (tabel-jurnal `autoreply_send_log`
    + indexuri). Serviciu nou `app/services/autoreply_dispatch.py` (nu apelează AI — refolosește sugestia
    stocată; conexiune proprie, best-effort, izolat de feed-ul CTS). Hook în `cts.py`: `RETURNING id` pe
    UPDATE-ul clean → `fresh_clean_ids` → `dispatch_for_ids` post-commit.
  - Seam pluggabil `_transmit` + `AUTOREPLY_SEND_MODE` (dry_run | cts_feed | graph) și flag
    `AUTOREPLY_AUTOSEND_ENABLED` (default 1). Vizibilitate: `GET /ai/autoreply/dispatch-log`
    (decizii recente + contoare pe outcome / 24h).
  - Testat live: hook-ul a logat automat decizii din trafic CTS real; throttle confirmat (1 `would_send`
    + 1 `throttled` pe același expeditor). Zero trimiteri reale (toate `send_mode=dry_run`).
- **Pași următori:** Faza 1.5 = cablare trimitere reală în `_transmit` (`cts_feed` → CTS trimite din feed,
  necesită modificare CTS; sau `graph` → sendMail, necesită grant Mail.Send). Faza 2 = trigger pe SOLVED.


## v0.41.0 - 2026-06-25 (Prioritate re-numerotată P0/P1 → P1/P2 valori 1/2 + email_priority pe Clienți)

- **Re-numerotare prioritate:** eticheta canonică devine NUMERICĂ — `1` = **P1** (urgent, fost P0),
  `2` = **P2** (normal, fost P1). Aceleași reguli deterministe + prag AI; doar valoarea stocată/emisă
  se schimbă. Migrație idempotentă (`migrations/20260625_priority_renumber_clients_emailpriority.sql`):
  remap `emails.ai_priority`, `ai_priority_result.priority` și `ai_priority_corrections` (snapshot
  backup în `_bak_ai_priority_20260625`).
  - Intern, modelul AI raționează în continuare în P0/P1 (prompt neschimbat); maparea la 1/2 se face
    la ieșirea din `priority_classifier.classify_priority` (P0→1, P1→2).
  - **API CTS** (`prioritate`) trimite acum **întreg** `1`/`2`/`null`; `urgent=true` când `prioritate=1`.
    Documentația API (pagina din UI) actualizată.
  - Endpoint-urile `correct` (per-email + verificare manuală) acceptă `1`/`2` (cu alias P0/P1) și
    resping restul cu „Prioritate invalida (1 sau 2)". UI: badge/select **P1/P2**, valori 1/2.
- **Pagina Clienți — `email_priority`:** coloană nouă `clients.email_priority` (smallint, 1/2) adusă din
  IRIS prin `iris_sync` (`/clients/contact-list`) + afișată în listă și în detaliul clientului.
  NOTĂ: feed-ul IRIS nu expune încă `email_priority` (rămâne NULL); Cargo360 e pregătit să-l consume —
  vezi cererea din outbox către Razvan pentru extinderea `/clients/contact-list`.

## v0.40.0 - 2026-06-16 (Procesare documente — grupare manuală atașamente din același email)

- **Grupare manuală (cu sugestie auto):** când un document vine ca mai multe atașamente în ACELAȘI email
  (ex. talon MD față + spate), operatorul le poate grupa ca să se extragă TOATE datele împreună.
  - În modalul unui document, secțiunea „Pagini din același email" listează celelalte atașamente cu
    checkbox; cele cu ACELAȘI tip sunt **pre-bifate ca sugestie**. „🔗 Grupează & reextrage" combină textul
    tuturor paginilor și re-extrage o singură dată.
  - Membrii grupului devin `status='grouped'` (ascunși din listă); primarul rămâne cu un badge
    „🔗 N atașamente" și afișează preview-urile tuturor paginilor (stacked) în modal.
  - „✂ Desparte" desface grupul (fiecare atașament redevine individual, re-procesat).
  - Decizie de design: gruparea NU e automată — în date, spatele unui document apare adesea ca
    `neidentificat` (la fel ca logo-urile), deci automatul ar îngloba junk; manualul e precis.
- Model aditiv: coloană `document_extractions.grouped_into` + index parțial (migrație idempotentă).
- „♻ Reextrage date" pe un grup re-combină textul tuturor paginilor.

## v0.39.2 - 2026-06-16 (Procesare documente — navigare, robustețe gateway, reextrage, excludere arhive)

- **Navigare între documente:** butoane „← Anterior / Următor →" + contor poziție în modalul de atașament,
  cu avertizare dacă există modificări nesalvate.
- **Eșec tranzitoriu de gateway AI nu mai blochează vizibil documentele.** Erorile temporare de
  infrastructură (502/503/504, timeout, transport) la clasificare ȘI la extragere nu mai persistă un rând
  `failed`: atașamentul rămâne în coadă și se reia automat la următorul drain, când gateway-ul revine.
  (Cauza logo-urilor „failed" rămase: o pană 502 a gateway-ului AI, nu documente vechi.)
- **Buton „♻ Reextrage date":** re-extrage datele cu tipul curent fără reclasificare. Rezolvă și câmpurile
  duplicate apărute când schema tipului s-a schimbat după o extragere veche (datele se realiniază la schema curentă).
- **Arhive excluse din start:** `.zip/.rar/.7z/.gz/.tar/...` → `neidentificat` direct, fără OCR/AI.

## v0.39.1 - 2026-06-16 (Procesare documente — filtrare junk + modal email real)

- **Doar ce a fost identificat ajunge la validare umană.** Atașamentele neidentificabile (logos, poze cu
  aparate/echipamente, imagini off-context, OCR gol) nu mai intră la procesare — sunt tratate direct ca
  `neidentificat` și ascunse din listă. La validare intră DOAR ce a fost încadrat pe o categorie
  (vehicul/șofer/contract) dar cu tipul incert (ex. „e contract, dar nu se știe tipul"):
  - **Gate pe categorie:** dacă AI-ul nu poate încadra atașamentul într-o categorie cunoscută →
    `neidentificat` (skip), chiar dacă `is_document=true`.
  - **Prompt de clasificare întărit:** regulă strictă — fără categorie clară ⇒ `is_document=false`; dacă
    `is_document=true` categoria e obligatorie; exemple explicite de exclus (logo, screenshot, poze cu
    aparate, off-context, marketing).
  - **`needs_vision` (OCR gol) ascuns implicit**, alături de `neidentificat`/`necunoscut` — vizibil doar
    cu toggle-ul „Arată neidentificate". Statusul se păstrează (non-lossy) pentru viitorul canal vision.
- **Tab „Vezi email" → modalul real de email.** Butonul „✉ Vezi email" din modalul de atașament deschide
  exact modalul din lista Emailuri (sidebar verdict/categorie/atașamente + file HTML/Text/Metadata), în
  mod read-only („Marchează ca corect"), în loc de panoul custom de dinainte.

## v0.39.0 - 2026-06-16 (Procesare documente STEP 2 — clasificare + extragere atașamente din emailuri)

- **Tab „Procesare documente" funcțional:** listă a atașamentelor procesate (JOIN email+atașament), cu
  buton **„Procesează azi/tot"**, filtru scope, toggle „Arată neidentificate", badge-uri pe status și
  tabel (email · atașament · categorie · tip detectat · încredere · date extrase · status).
- **Clasificare în 2 trepte într-un singur apel AI:** pentru fiecare atașament, un clasificator decide
  `is_document` + `categorie` (vehicul/șofer/contract) + `tip` exact din catalog, folosind **titlurile de
  potrivire** (contractele se disting după titlu). Dacă e document și are tip, se rulează extragerea
  configurată (Phase 1) → date structurate. Statusuri: `extracted` / `classified` / `needs_review`
  (încredere mică) / `needs_vision` (OCR gol) / `neidentificat` (logo/svg/screenshot) / `failed`.
- **Modal detaliu atașament:** date extrase **editabile** (corectezi ex. un CUI), **selector manual de
  tip** (+ „Re-extrage cu tipul ales"), buton **„Reidentifică document"** (reclasifică + extrage),
  „Salvează" (marchează `reviewed`, protejat de reprocesarea automată).
- **Procesare automată pentru emailurile viitoare:** hook fire-and-forget în `/process/run-now` (cron 5
  min) — atașamentele noi se clasifică/extrag automat, în thread daemon, fără a atinge categorizarea
  emailurilor. Coada = atașamentele fără rând în `document_extractions` (idempotent, fără tabel nou).
  Cron-ul rulează pe fereastra `recent` (ultimele 2 zile) ca să NU măture tot arhivul istoric; sweep-ul
  complet (`Procesează tot`) e o acțiune manuală explicită.
- **Modal: preview atașament** (imagine/PDF prin endpoint-ul de download) + **selector de categorie**
  manual independent de tip (pentru a confirma ex. „contract" fără un tip anume).
- **Modal îmbunătățit:** layout pe 2 coloane (preview stânga / date+selectoare dreapta), mai lat
  (1100px), **zoom pe imagini** (click pe poză), **căutare în selectorul de tip**, opțiune
  **„Necunoscut"** la categorie și tip (status manual `necunoscut` pentru chitanțe / documente fără tip —
  distinct de `neidentificat`, ascuns implicit, vizibil cu toggle), și tab nou **„Vezi email"** care
  arată emailul original + toate atașamentele lui (cu evidențierea celui curent + buton „deschide").
- **Setări → Prompturi AI:** card nou **„Prompt identificare documente"** (editabil) — regula de
  excludere logos/screenshots + încadrare categorie/tip; catalogul tipurilor se adaugă automat.
- **Pre-filtru ieftin înainte de OCR:** svg / imagini foarte mici (logo) / fișiere neprocesabile
  (zip/xml…) → `neidentificat` fără cost AI; PDF cu mime greșit (octet-stream) detectat prin magic-bytes.
  Citirea atașamentelor reutilizează `emails._host_path` (volum parser-email-op, doar citire).
- **DB (aditiv):** `document_extractions` + `reviewed`, `updated_at`, `reviewed_by`, `manual_type`,
  `confidence_reason`; setarea `documents.classify_prompt`. Migrație `20260616_doc_extractions_step2.sql`.
- Verificat pe atașamentele de azi: poze de talon/CEMT recunoscute și extrase, contracte → tip corect,
  logos/facturi/extrase bancare → `neidentificat`, PDF criptat → `needs_vision`.

## v0.38.1 - 2026-06-16 (Procesare documente — config comun pe Contracte + fix semnătură)

- **Contracte: set comun de câmpuri pe toate cele 11 tipuri** (CargoFuel Prepaid, E-Transport
  Premium/Basic, SentGeo, HUGO, Monitorizare GPS, Taxe de drum PL/HU/BG/carGObox, Compensare carburant):
  `Numar contract`, `Data contract`, `Prestator`, `Client`, `CUI client`, `Este semnat` (boolean) — plus
  prompt de extragere comun (robust la OCR zgomotos: corectează evident `24,04,2026`→`24.04.2026`,
  distinge clientul de Cargo Track, ia CUI-ul celeilalte părți).
- **Fix critic „Este semnat": trunchiere cap+coadă.** Semnăturile stau la finalul documentului, dar
  textul era tăiat la primii `MAX_DOC_TEXT` chars → la contractele lungi (ex. CargoFuel ~20k chars/6 pag)
  pagina de semnătură nu ajungea niciodată la model, deci `Este semnat` ar fi fost structural mereu fals.
  Adăugat `_clip_doc_text` (păstrează ~62% cap + restul coadă) în `_extract_doc`. Validat: pe text curat
  extragerea e exactă (nr./dată/părți/CUI), iar `Este semnat` se aprinde corect când există dovadă
  textuală (semnătură+dată completată) și rămâne fals la linii goale / semnături olografe scanate
  (ilizibile în text — caz pentru canalul vision din outbox).
- **#18 „carGObox - PrePaid" — câmpuri specifice fidejusor:** pe lângă setul comun, adăugate
  `Fidejusor nume`, `Fidejusor C.I.`, `Fidejusor CNP` și două semnături separate (`Semnatura beneficiar`,
  `Semnatura fidejusor`), cu prompt care localizează blocul BENEFICIAR + blocul FIDEJUSOR (prima pagină) și
  zona de semnături (final). Notă: pe scanurile reale datele fidejusorului și semnăturile sunt completate
  de mână → ilizibile pentru OCR (model pune `null`/`false`, nu halucinează) — se vor popula cu canalul
  vision din outbox.
- **Fix logging:** `ai_call_log.task` lărgit `varchar(50)`→`varchar(120)` — task-urile lizibile
  (`cargo360:doc_extract:CargoFuel_Prepaid_v1_0:<salt>`) depășeau 50 chars și picau la insert (extragerea
  mergea, dar se pierdea telemetria costului AI).

## v0.38.0 - 2026-06-15 (Modul nou „Procesare documente" — Phase 1: fundația / tab „Tipuri de documente")

- **Secțiune nouă în sidebar: „Procesare documente"** (sub „Emailuri"), cu 2 taburi: „Procesare documente"
  (placeholder Phase 2 — statistici + listă documente procesate) și **„Tipuri de documente"** (fundația
  livrată acum). Modelat după modulul Rapoarte (tab „Automate").
- **Tab „Tipuri de documente":** 3 categorii (Documente vehicul / Documente șofer / Contracte). Pentru
  fiecare tip operatorul: încarcă un **șablon exemplu** (poză/PDF, cu preview în modal), definește
  **câmpurile de extras** (nume + tip + descriere), generează promptul de extragere cu **„✨ Generează
  promptul cu AI"** și îl validează cu **„▶ Testează extragerea din exemplu"**. Contractele au în plus
  „titluri de potrivire" (tipul se determină după titlu).
- **DB:** tabele noi `document_types` (definițiile, pe 3 categorii, cu `extract_fields`/`extract_prompt`/
  `match_titles`/șablon) și `document_extractions` (rezultate, populate în Phase 2). Migrație idempotentă
  `migrations/20260615_document_processing.sql`.
- **Extragere text (vision — interimar):** gateway-ul AI IRIS e strict text, deci extragem textul LOCAL
  din document — **PDF** prin `pdfplumber`/`PyMuPDF` (text nativ, excelent pe contracte), **poze** prin OCR
  `pytesseract`+`Pillow` (auto-rotate EXIF, `ron+eng`) — apoi îl trimitem la `iris_ai` ca în Rapoarte.
  „Generează prompt" și „Testează" pe PDF funcționează imediat. Un **canal vision dedicat** (acuratețe
  mare pe poze, clasificare+rotire) e cerut separat prin outbox.
- **Backend:** router nou `app/api/v1/documents.py` (CRUD tipuri, upload/servire șablon,
  generate-extract-prompt, test-extract, **test-detect + generate-detect-prompt**) — reutilizează
  helperele pure din `reports.py` (anti-drift).
- **Validare tip (fără extragere):** buton **„✓ Validează șablon"** + prompt opțional de analiză
  (`detect_prompt`) + „✨ Generează prompt de analiză" în editor — pentru tipurile doar de identificare
  (Anexa, remorcă, CargoBox), confirmă dacă AI-ul ar recunoaște corect documentul (match + încredere +
  motiv + titlul detectat), fără a extrage date.
- **Fix cache gateway (corectitudine extragere):** cache-ul „curated" al gateway-ului AI e cheiat pe câmpul
  `task` (NU pe prompt, NU pe conținut, ignoră `use_cache=False`) — un `task` static servea răspunsuri vechi
  la re-testare și risca contaminare între documente. Adăugat `_cache_salt` = sha1(system+conținut); toate
  apelurile sufixează `task` cu `:{type_id}:{salt}` (extragere, detecție, generare prompt). Prompt schimbat →
  re-rulează; alt document → re-rulează; identic → cache idempotent. Ex.: CEMT Emission Class `EURO IV`
  (greșit, din cache) → `EURO VI` (corect).
- **Prompt CEMT îmbunătățit:** instrucțiune explicită de a alege clasa EURO **bifată** (căsuța plină/diferită
  în OCR: `WI`/`X`/`■` vs goalele `U`/`Ul`/`O`), nu prima opțiune din listă; normalizare la EURO III/IV/V/VI/EEV.
- **Fix OCR pe PDF scanat:** taloanele/CIV/formularele vin frecvent ca **poză salvată în PDF** (fără strat
  text) → extragerea de text nativ întorcea gol și „Testează" eșua. Adăugat fallback: randare pagini PDF
  la ~300 DPI → OCR. OCR făcut robust prin **PSM multiple** (PSM 6 pentru blocuri de text, esențial pe
  scanuri complexe) + fallback pe imaginea brută — folosit deopotrivă la PDF și la poze.
- Versiune sincronizată: `app.config.app_version` corectat 0.3.0 → **0.38.0** (era desincronizat de
  fișierul VERSION) — health `/version` raportează acum corect.
- **Phase 1 = doar fundația.** Legarea în pipeline-ul live (detectare tip + extragere automată la
  emailuri cu atașament → CTS) = Phase 2; `process_email.process_one` rămâne neatins.

## v0.37.0 - 2026-06-15 (Gate „Automat" în pipeline-ul live — pattern-urile din Rapoarte prind mailurile noi)

- **Pattern-urile confirmate „Automat" (pagina Rapoarte) sunt acum aplicate la procesarea LIVE a
  fiecărui email nou.** Înainte, `report_patterns` creșteau DOAR la regenerarea manuală a raportului
  (`last_seen_at` îngheța, numărul nu creștea deși soseau zilnic emailuri de același șablon). Cauză:
  `process_email.process_one` nu consulta niciodată `report_patterns`.
- Email nou care se potrivește unui pattern confirmat (același criteriu ca regenerarea — **expeditor ∈
  pattern ȘI amprentă SimHash** Hamming ≤ `PATTERN_MATCH_K`=5) este acum, ÎNAINTE de orice clasificare:
  **exclus** din spam/carantină/categorie (economie NOVA), **atașat** la pattern (`email_ids`,
  `total_matched`, `email_count`, `last_seen_at`), pus în coada de extragere (dacă `extract_enabled`)
  și marcat terminal `status='auto_report'` / `queue_status='auto_closed'` (procesat automat, „închis",
  ascuns din lista de emailuri, fără procesare umană).
- **Sursă unică de adevăr (anti-drift):** noul `reports.try_auto_handle_pg(cur, email)` reutilizează
  EXACT `_fp_of` + `_match_pattern` + aceeași acumulare ca `_run_generation` — calea live și regenerarea
  nu pot diverge. Atașare + enqueue + set status în aceeași tranzacție; `_drain_queue` pornit după commit.
- Criteriul expeditor+șablon evită fals-pozitivele: emailurile personale forwardate de pe o adresă
  aflată într-un pattern (ex. „Fw: …" de la diana_perticas@) au alt șablon → NU se auto-închid, trec
  prin clasificarea normală.
- Gate doar pe mailuri FĂRĂ atașament (paritate cu modul în care s-au învățat pattern-urile). Rulează
  înaintea NDR: mailurile `mailer-daemon` care se potrivesc șablonului „Alerte eșec livrare" merg la
  extragere, nu la statusul `ndr`. Fail-safe: la orice eroare, emailul cade în clasificarea normală.
- Restanța (mailuri sosite înainte de deploy) NU e atinsă (gate-ul rulează doar pe `status='pending'`);
  recuperabilă oricând cu „Regenerează" pe Rapoarte.
- Fișiere: `app/api/v1/reports.py` (+`try_auto_handle_pg`, `_load_patterns_pg`),
  `app/services/process_email.py` (gate în `process_one`), `app/api/v1/emails.py` (badge `auto_closed`).

## v0.36.1 - 2026-06-15 (Legit: migrează TOATE mailurile expeditorului, nu doar cel clicat)

- **Acțiunea „Legit" (pagina Spam) procesează acum TOATE emailurile aceluiași expeditor.** Înainte,
  marcarea unui email ca „Legit" scotea din spam toate mailurile expeditorului (override=FALSE), dar
  **doar cel clicat** era repus pe calea de categorizare (`queued_general`); restul rămâneau blocate la
  `stopped_spam`, fără categorie și fără a ajunge la CTS. Acum, `status='clean'` + repunerea pe calea
  `manual_clean` se aplică **tuturor** mailurilor expeditorului blocate ca spam (`queue_status=
  'stopped_spam'`) → `advance_queue_batch` (tick 5 min) le categorisește pe toate și le trimite la CTS.
- Stările imuabile de securitate (`quarantined`/`quarantined_strict`/`ndr`/`deleted`) și mailurile deja
  pe calea sănătoasă (`ready_for_cts`/`sent`) NU se ating (carantina bate Legit; fără re-trimitere CTS).
- Fișier: `app/api/v1/spam.py` (acțiunea `legit`, pașii 3-4). Backup `spam.py.bak-fullthread-20260615`.

## v0.36.0 - 2026-06-15 (Carantină pe tot thread-ul + whitelist anti-spam pe expeditor)

- **Carantină/carantină strictă (phishing) se evaluează acum pe TOT thread-ul** (toate reply-urile),
  nu doar pe ultimul mesaj — decizia de securitate ține cont de întreg contextul. Controlat de
  flag-ul **`ANALYZE_FULL_THREAD`** (default ON; `=0/false/no/off` + restart → revine la mesajul nou,
  rollback fără redeploy). Sursa de conținut e centralizată în `phishing_detector._scan_content()`.
- **Categoria** se analiza deja pe tot thread-ul (`_email_body` → `body_text` integral, sub plafonul
  de 48k al modelului) — neschimbată.
- **Spam rămâne evaluat pe MESAJUL NOU** (quote-stripped), intenționat: o promoție trimisă de noi și
  **citată** sub răspunsul unui client NU trebuie să marcheze răspunsul ca spam (promoția e în
  istoricul citat, nu în mesajul nou). Un spam real de la terți, scris în mesajul nou, e prins normal.
- **Whitelist-ul manual ⇒ NICIODATĂ spam — DOAR pe EXPEDITOR.** Un expeditor de pe whitelist
  (*Liste expeditori — învățare la categorizare*) forțează `override=FALSE` la spam, indiferent de
  scor (ca allowlist). Whitelist-ul se verifică **doar pe `from_address`/domeniu**, niciodată pe
  adrese care apar în corp: dacă pe o adresă a noastră (ex. `office@cargotrack.ro`) intră spam trimis
  de **terți**, e marcat spam; doar mailurile **trimise DE LA** o adresă whitelist sunt scutite.
  Whitelist **bate** blocklist/blacklist-spam. Cod motiv: `manual_whitelist_bypass`.
- **Sursă unică `spam_detector.classify_spam_gate()`** pentru decizia de spam (allowlist/whitelist ⇒
  NU spam; blocklist/blacklist-spam ⇒ spam; altfel scorul decide). Folosită identic de pipeline-ul
  live (`process_email.process_one`) ȘI de `POST /spam/backfill` — eliminând driftul (înainte
  backfill-ul ignora whitelist-ul/reputația, un bug de consistență, reparat).
- **`POST /spam/backfill` aliniat** la aceeași poartă: re-rulabil, pur SQL (fără AI). Mecanismul de
  backfill retroactiv pentru emailurile deja procesate; raportează `whitelist_bypassed` / `forced_spam`.
  (Carantina NU se recalculează prin backfill — full-thread la carantină se aplică doar mailurilor noi.)
- Fișiere: `app/services/phishing_detector.py`, `spam_detector.py`, `process_email.py`,
  `app/api/v1/spam.py`. Backup `*.bak-fullthread-20260615`.

## v0.35.0 - 2026-06-15 (Traducere emailuri în română — modal Emailuri)

- **Traducere on-demand în română în modalul de email.** Pentru emailurile într-o limbă străină
  (engleză, rusă, ucraineană, maghiară etc.) apare în modal butonul **„🌐 Începe traducerea"**.
- **RO by default + „Vezi originalul".** După o traducere reușită, conținutul tradus (subiect + corp) se
  afișează implicit la următoarele deschideri, cu buton de comutare **„Vezi originalul"** ⇄
  „Vezi traducerea (RO)" — toggle instant, fără reapel AI. Rezultatul e **salvat în DB** (cache).
- **Detecție limbă + traducere într-un singur apel AI**, prin modelul **gratuit `gemma`** cu **fallback**
  la `claude-haiku-4-5` doar dacă gemma eșuează. Task logat ca `cargo360:email_translation` (prefixat ca
  celelalte) → vizibil în pagina **Analiza AI**.
- **Doar text, sigur**: se traduce textul (corp extras URL-aware, fără CSS/script), nu structura HTML.
  Conținutul emailului e tratat ca date neîncredere (anti prompt-injection).
- **Backend**: serviciu nou `app/services/email_translator.py`; endpoint `POST /emails/{id}/translate`
  (admin); migrare aditivă `20260615_email_translation.sql` (coloane `translation_*` + `source_lang` pe
  `emails`). Zero impact pe fluxul existent (categorizare/carantină/spam neatinse).

## v0.34.1 - 2026-06-12 (Dashboard: schemă logică „Fluxul de procesare a emailurilor" v5)

- **Schema SVG animată din cardul „Fluxul de procesare a emailurilor" (Dashboard) extinsă la varianta v5.**
  Layout orizontal pe benzi: **banda centrală** Client → Inbox → Cargo360 → Clasificare → Clean →
  Categorie · IRIS → „Are atașamente?" → CTS; **banda de sus** = ramura Spam; **banda de jos** = ramura
  Carantină. Zona de după Clean (Categorie · IRIS extrage · „Are atașamente?" · CTS) e mult mai aerisită.
- **Căi directe blacklist corectate**: domeniile deja în blacklist sunt oprite în Cargo360 și pleacă
  DIRECT, ÎNAINTE, spre Spam (portocaliu `#E0820C`) și spre Carantină (roșu `#D93A3A`) — fără bucle înapoi
  spre Inbox (fix față de v1, unde nu existau aceste căi directe explicite).
- **Noduri noi/extinse**: Client, „IRIS validează intenția senderului" (înainte de Carantină),
  „Mailuri standard (auto)", „IRIS extrage (Mașină · Țară · Dată)", „Categorie · IRIS", „Are atașamente?"
  (DA/NU), „Procesare atașamente (contract · OP · ITP · CI…)", „Încarcă pe entități (client · vehicul)",
  CTS cu sub-stări (Pregătit / Closed auto / + atașamente). Ramura DA atașamente + buclă punctată
  „atașamente gata?" (CTS interoghează procesarea).
- **Animație**: **23 bile** decalate (5 SPAM portocalii + 5 Carantină roșii + 11 neutre), cu durate variate (9–13.5s) ca să nu pară un loop identic, (`<animateMotion>`, ~11s, infinit, fade la capete), distribuite pe
  TOATE fluxurile (inclusiv recuperările „operator → Clean", „fals pozitiv → Clean" și ramura DA
  atașamente „+ email" → CTS). Culoare = natura emailului: albastru `#185FA5` neutru, portocaliu `#E0820C`
  spam direct, roșu `#D93A3A` carantină/blacklist (bila de carantină directă merge ÎNAINTE). Plus 2 bile **teal** `#0FB5AE` ocazionale (durate 15/18s) care pleacă din CTS spre „Procesare atașamente” ca să ceară documente și revin în CTS (buclă feedback). Legendă pe 2 rânduri (acum cu intrarea teal).
- **Dimensiune / încadrare** (rafinare): schema randează pe lățime completă (eliminat `maxWidth`, container
  `width:100%`) → mai mare și mai ușor de urmărit; viewBox crop-uit `0 0 1660 820` → `0 158 1660 662`
  (tăiat spațiul gol de sus, fără padding-top vizibil); `<svg style="display:block">`.
- **Tehnic**: doar frontend, `app/ui/index.html` — template literal `CARGO360_FUNNEL_SVG` rescris
  (namespace CSS `.mgf`, marker `#mgf-ar`, clasă nouă `.mgf-fb` pentru feedback punctat, fill default pe
  `.th`/`.ts` ca etichetele libere să fie lizibile în ambele teme). Model de referință: Iris DIAG
  `AI1B7J0XK` / `cargo360_flow_schema_v5.html`.
- Backup: `app/ui/index.html.bak-flowv5-20260612` (v5 inițial), `*.bak-flowv5b-20260612` (rafinare
  dimensiune + bile). Verificat: SVG well-formed (taguri echilibrate), `0 158 1660 662` servit la `/`,
  markerul v1 `0 0 680 360` dispărut, 13 `<circle>` în schemă, servire statică (fără restart).

## v0.34.0 - 2026-06-12 (Propuneri IRIS acționabile + tip blacklist carantină/spam)

- **Propuneri IRIS bifabile (per-linie)** în „Învățare din carantinare manuală": fiecare sugestie
  (regulă/semnal/scor) are checkbox. Cele bifate intră într-un **ghid** injectat în promptul **porții
  de intenție AI** (`strict_intent_gate`) când analizează intenția unui email candidat la carantină.
  Serviciu nou `app/services/learning_guidance.py`; endpoint `POST /settings/learning-proposals/toggle`;
  `GET /settings/learning-proposals` întoarce `accepted`. Indicator „Active în ghid: N".
- **Poarta de intenție AI PORNITĂ** (`STRICT_INTENT_GATE_ENABLED=1`, decizie explicită a userului). Notă:
  poarta poate ELIBERA automat carantine borderline judecate benigne (necesar-dar-nu-suficient: benign +
  client cunoscut + fără blockeri malware). Ghidul curat rafinează decizia; nu poate carantina un clean.
- **Câmp `tip` (carantină | spam) pe Blacklist**: carantina folosește DOAR `tip=carantina`; spam-ul DOAR
  `tip=spam`. **Spam-ul confirmat NU mai forțează carantină** (fix escaladare v0.33.0) — intră ca
  `tip=spam` și afectează doar scoringul de spam (override, reason `manual_blacklist_spam`). Inferență
  back-compat fără migrare (`spam_confirm`→spam, rest→carantină). UI: badge tip + select la add/editare.
- **Rename `mute`→`ignoră`**: butoane „ignoră"/„reactivează", stare „· ignorat" (câmpul `muted` neschimbat).
- Backup: `*.bak-proposals-20260612`. Verificat: compile OK, `node --check`, restart OK, health 200.

## v0.33.0 - 2026-06-12 (Liste expeditori: Blacklist + Whitelist pentru învățare la categorizare)

- **Două liste noi** în Setări → Prompturi AI, sub „Învățare din carantinare manuală" (side-by-side):
  **Blacklist** și **Whitelist** de emailuri/domenii pe care IRIS le folosește la încadrarea de securitate.
- **Store canonic unic** `settings['phishing_manual_learning'].{blacklist, whitelist}` + serviciu nou
  `app/services/sender_lists.py`. Cheia = email complet sau domeniu bare (fără „@").
- **Detecție**: Blacklist = enforcement hard (carantină strictă, neschimbat). **Whitelist = suprimare
  soft** — elimină semnalele slabe (L1/L2), niciodată malware/cod strict, și doar dacă nu există trigger
  strict. Blacklist bate whitelist. Intrările `muted` sunt ignorate la detecție.
- **Auto-populare**: „Confirmă spam" → Blacklist (escaladare = ranking mai mare next-time); „Legit" →
  Whitelist; carantinarea manuală → Blacklist (ca până acum). Dacă e deja în lista opusă, NU se mută.
- **CRUD** (admin): `GET/POST/PUT/DELETE /settings/sender-lists` — adaugă/editează/mute/șterge. UI cu
  badge email/domeniu, sursă, butoane mute/edit/✕.
- Fără backfill din `spam_sender_reputation` (subsistem separat); doar confirmările noi sincronizează.
- Backup: `*.bak-senderlists-20260612`. Verificat: compile OK, restart OK, health 200, `node --check`.

## v0.32.2 - 2026-06-12 (Verificare manuală: tabel consecvent + fix navigare modal)

- **Design tabel** aliniat la pagina Emailuri (`list-table-full`, `IdCell`, `catBadge` global,
  rânduri `clickable`). Coloane: ID · Recepționat · Subiect · Expeditor · **AI a zis** ·
  **Categoria corectă** · **Motiv**. Coloana „Acțiune" eliminată (procesarea se face din modal).
- **Motiv**: „necunoscut de AI" vs „preluat aleatoriu" (eșantion QA).
- **„AI a zis" vs „Categoria corectă"** și în fila „Verificate": pentru corectate se afișează categoria
  AI **originală** (din `ai_category_corrections`, `mr_old_category` via LATERAL) vs cea pusă de om
  (`✎ corectat` / `✓ confirmat`).
- **Fix navigare**: la corectarea categoriei în modal, lista pending se micșora și ←/→ se dezactiva.
  Acum lista de ID-uri se **îngheață la deschiderea modalului** (`reviewIds`) — parcurgi tot batch-ul.

Backup: `index.html.bak-mrtbl-20260612`, `manual_review.py.bak-tbl-20260612`. `node --check` OK.

## v0.32.1 - 2026-06-12 (Verificare manuală: deschidere în modalul email)

Rândurile pending din „Verificare manuală" se deschid acum în **același modal ca pagina Emailuri**
(navigare ←/→, taburi, atașamente), cu un `mode='review'`:
- Footer: doar butonul **„Marchează ca corect"** → confirmă și avansează la următorul.
- Corecția de categorie din modal merge prin endpoint-ul de review → apare automat în „Emailuri
  încadrate greșit" (Setări) și marchează item-ul rezolvat.

Backup: `index.html.bak-mrmodal-20260612`, `manual_review.py.bak-modal-20260612`. `node --check` OK.

## v0.32.0 - 2026-06-12 (Modul nou: Verificare manuală — learning / QA)

Modul nou în sidebar pentru active-learning pe categorisirea AI. **NU afectează fluxul existent** spre CTS —
doar eșantionează retrospectiv mailurile de ieri pentru validare umană.

- **Pick zilnic** (pe cron-ul de 5 min, idempotent, TZ Europe/Bucharest): ~20% (configurabil) din mailurile
  **CLEAN de ieri** = toate necunoscutele reale (`ai_category='necunoscut'`, `ai_status='done'`) + random din
  cele deja încadrate. Neprocesatele sunt excluse.
- **START/STOP** (setare `manual_review.enabled`) — oprești când statistica e suficient de bună.
- **Confirmare** (AI corect) sau **Corecție** (schimbă categoria) — corecțiile intră în `ai_category_corrections`
  și apar automat în „Emailuri încadrate greșit" + „Regenerează prompturi (AI)".
- **8 carduri** statistici (% necunoscut ieri, rată încadrare corectă, în așteptare, verificate azi, +totaluri).
- DB: migrare aditivă pe `emails` (`manual_review_*`) + index parțial. Backend: `services/manual_review.py` +
  router `manual-review`. UI: tab nou cu tabel + filtre.

Backup: `index.html.bak-mreview-20260612`, `main.py`/`emails.py` `.bak-mreview-*`. Verificat `node --check`,
pick e2e + idempotență.

## v0.31.1 - 2026-06-12 (Dashboard: consecvență carantină + explicații carduri)

Doar UI. Clarificare a inconsecvenței semnalate (sus „Carantină 4" vs „Rată carantină 242").

- Cardul `Strict review` redenumit **„Carantină strictă"** (e tot carantină: status `quarantined_strict`).
- Fiecare card din secțiunea **Verdict** are acum o explicație scurtă (ce înseamnă statusul).
- Nota la **Rată carantină** este explicită: „242 carantinate = 4 + 238 strictă".
- Cardul **Spam** notat „subset din Clean" (status `clean` flaguit spam, nu un status separat).
- Donut **Distribuție verdict** aliniat la carduri: Clean (complet) / Carantină (normală+strictă) / NDR / Pending;
  spam-ul nu mai e felie separată (era subset din Clean) — explicat în notă. Acum cardurile, rata și donut-ul
  folosesc aceleași definiții.

Backup: `index.html.bak-consistency-20260612`. Verificat `node --check`.

## v0.31.0 - 2026-06-12 (Dashboard: carduri grupate pe secțiuni + Status CTS + Top clienți)

**Frontend** — cardurile de statistici reorganizate în 3 secțiuni cu sub-titluri:
- **Volum**: Total emailuri, Ultimele 24h, Ultimele 7 zile.
- **Verdict**: Clean, Carantină, Strict review, NDR, Pending, Spam.
- **Rate & AI**: Rată Clean, Acoperire AI, Rată carantină, Reclamații % din total.
- Secțiune nouă **Analiză & distribuții** pentru charts.
- Două charts noi (bare): **Status CTS (pipeline)** (Pregătit/Trimis/În procesare/Oprit/Eroare) și
  **Top clienți (după volum)**. `HBars` acceptă acum `labelWidth` + ellipsis pe etichete lungi și stare „Fără date".

**Backend** — `GET /api/v1/stats/overview` extins cu: distribuție status CTS din `queue_status`
(`cts_ready/cts_sent/cts_in_progress/cts_stopped/cts_send_error/cts_error_nova`) și `top_clients`
(top 8 după volum, join `clients`). Read-only, fără modificări de schemă.

Backup: `index.html.bak-sections-20260612`, `health.py.bak-ctsclients-20260612`. Verificat `node --check` + `py_compile`; restart `mailguard-api`.

## v0.30.1 - 2026-06-12 (Charts: etichete oră + numere pe bare)

Doar UI. Fără schimbări de logică/date.

- **Volum pe oră (24h)**: afișează **toate** orele dedesubt (compact, ex. `07`) și **numărul de emailuri deasupra** fiecărei bare.
- **Evolutie emailuri pe zi**: afișează **totalul pe zi deasupra** fiecărei bare (prop nou `showTotals` în `StackedDailyChart`, activat doar aici).
- Backup: `index.html.bak-charts-20260612`. Verificat `node --check`.

## v0.30.0 - 2026-06-12 (Dashboard: statistici & charts noi + funnel mutat ultimul)

Statistici suplimentare pe baza datelor reale din `emails` / `email_spam`. Funnelul `Cargo360Funnel`
mutat ca ULTIMUL card (întâi statisticile).

**Backend** — endpoint nou read-only `GET /api/v1/stats/overview` (în `app/api/v1/health.py`):
distribuție verdict (incl. spam via join `email_spam`), atașamente cu/fără (`has_attachments`),
distribuție confidență AI (înaltă ≥0.75 / medie 0.5–0.74 / scăzută <0.5 din `ai_result->>'confidence'`),
confidență medie, scor mediu phishing, și volum pe oră în ultimele 24h (gap-filled). Fără modificări de schemă.

**Frontend** (Dashboard) — componente noi reutilizabile `Donut` (SVG), `HBars`, `HourlyChart`:
- 4 carduri de rate derivate: Rată Clean, Acoperire AI, Rată carantină, Spam detectat (%).
- Donut **Distribuție verdict** (Clean net / Spam / Carantină / NDR / Pending).
- Bare **Distribuție categorie AI** (informație / sesizare / reclamație / necunoscut).
- Bare **Confidență clasificare AI** (înaltă / medie / scăzută) + confidență & scor mediu.
- Donut **Atașamente** (cu / fără).
- **Volum pe oră (24h)** mini bar chart.
- Toate componentele sunt theme-aware (folosesc `var(--*)`), refresh la 10s odată cu restul statisticilor.
- Funnelul mutat la final.

Backup: `index.html.bak-dashstats-20260612`, `health.py.bak-overview-20260612`. Verificat `node --check` + `py_compile`; restart `mailguard-api`.

## v0.29.1 - 2026-06-12 (Funnel: mai lat + mai multe bile)

Doar UI, ajustare a widgetului `Cargo360Funnel`. Fără schimbări de logică/date.

- **Mai lat**: container `maxWidth` 760 → **1040px** (centrat) — SVG-ul (viewBox fix 680×360) se scalează uniform,
  deci mai mult spațiu între carduri și vizibilitate mai bună pe ecran.
- **+20% bile**: 5 → **6** bile în tranzit, cu start-uri redistribuite uniform pe bucla de 7s (~1.167s între ele);
  a 6-a reia ruta principală Clean → CTS.
- Backup: `index.html.bak-funnel2-20260612`. Verificat `node --check`.

## v0.29.0 - 2026-06-12 (Dashboard: funnel animat al fluxului de procesare)

Doar UI. Component nou `Cargo360Funnel` afișat pe Dashboard sub grila de statistici. Fără schimbări de logică/date/endpoint.

- **Funnel animat (SVG inline + CSS)**, fără librării externe. Arată drumul unui email: Inbox → Cargo360 →
  Clasificare → ramificare Spam / Clean / Carantină → Categorie → CTS, cu ramurile Blacklist și recuperările
  (Legit / Decarantinare) înapoi în Clean.
- **Animații**: linii „marching ants" pe `stroke-dashoffset` (keyframes `mgf-dash`); 5 bile (`<animateMotion>`)
  parcurg pe rând toate traseele posibile cu fade in/out; chenar CTS pulsează discret (`mgf-pulse`).
  Respectă `prefers-reduced-motion` pentru animațiile CSS.
- **Teme**: noduri pastel cu text/bordură în nuanța închisă (hex fix, lizibile pe temă deschisă și închisă);
  etichetele plutitoare și legenda folosesc `var(--t2)` ca să se adapteze la temă.
- **Implementare**: SVG ca string const la nivel de modul + `key` stabil → React sare peste re-scrierea DOM la
  re-render-ul Dashboard (interval 10s), deci animația NU repornește. Clase/marker/keyframes scope-uite `mgf-*`
  (zero coliziuni cu CSS-ul aplicației). Bazat pe exemplul aprobat `cargo360_flow_funnel_v3`.
- Backup: `index.html.bak-funnel-20260612`. Verificat `node --check`.

## v0.28.3 - 2026-06-12 (Preview atașament: fundal alb + zoom lin + rotire)

Doar UI, componenta `PreviewPane` (preview imagine/PDF în modalul email). Fără schimbări de logică/date.

- **Fundal curat**: panoul de preview restilizat flat alb (era dark `#101722` / gri `#222` la imagini) —
  acum se vede **doar documentul** pe alb. Toolbar `#FAFAF7` + bordură 0.5px, butoane flat neutre
  (fără `.btn secondary` dark).
- **Wheel zoom lin**: în loc de pas absolut +0.1/event (sărea 30-40% la mai multe evenimente per notch),
  acum **multiplicativ proporțional cu `deltaY`, plafonat la ±10%** per notch (`z * exp(dz)`,
  `dz` clamp ±0.10). Butoanele +/− devin și ele ±10% relativ.
- **Buton rotire 90°**: nou (doar imagini) — rotește incremental; inclus în `transform` (`rotate(Ndeg)`).
  Reset readuce zoom 20% + rotație 0.
- Backup: `index.html.bak-preview-20260612`. Verificat `node --check`.

## v0.28.2 - 2026-06-12 (Modal email mai lat + skeleton loading la navigare)

Doar UI, modalul `EmailDetail`. Fără schimbări de logică/date.

- **Lățime** `.em2` 1180→1520px (max-width 96→97vw). Pane-ul de preview atașament în tab HTML
  trece de la 65/35 la **56/44** (email/preview) — preview-ul (imagine zoom/PDF) se încadrează acum bine.
- **Skeleton loading** la navigarea Anterior/Următor: branch-ul `!email` nu mai afişează cutia dark
  „Se incarca..." ci păstrează shell-ul alb `.em2` (2 coloane) cu blocuri shimmer (clasă `.skl` +
  keyframe `@keyframes skl`) pentru antet, cardurile din sidebar, tab-uri, conţinut şi footer.
  `aria-busy=true`; butonul X rămâne funcţional în timpul încărcării.
- Backup: `index.html.bak-skelwide-20260612`. Verificat `node --check`.

## v0.28.1 - 2026-06-12 (Polish badge-uri liste — categorii + statusuri flat)

Doar UI, fără schimbări de logică/date. Aliniază badge-urile din tabele la estetica flat din modal.

- `.badge` global: radius 4→6px, font-weight 600→500, bordură 1px (transparentă by default), padding 3→ușor mai aerisit.
- Statusuri (`.b-*`): aceeași umplere subtilă (rgba ~0.13) + **bordură fină colorată** asortată — rămân theme-aware (dark/light).
- Categorii AI: helper nou `catBadge()` (+ `hexA()`) → fill subtil + text colorat + bordură fină (sentence case),
  în loc de fill solid saturat cu text alb. Aplicat în tabelul Emailuri (`EmailsList`) și tabelul Spam.
  Tabelele admin (istoric categorii) rămân neschimbate.
- Tabelul rămâne în tema aplicației (dark/light) — nu a fost făcut alb (ar fi stridență față de restul UI-ului).
- Backup: `index.html.bak-badges-20260612`. Verificat `node --check`.

## v0.28.0 - 2026-06-12 (Redesign modal email — layout 2 coloane, flat alb)

Doar UI/layout pe modalul `EmailDetail` (partajat de Emailuri / Carantină / Spam, `app/ui/index.html`).
Zero schimbare de logică, date, endpoint-uri sau acțiuni — informația exista deja, a fost re-aranjată.

### Layout
- Două coloane: sidebar fix 228px stânga (metadate + analiză) + zona de conținut email dreapta.
  Stil flat, temă albă (`#FFFFFF` modal, `#FAFAF7` sidebar/footer), borduri 0.5px în loc de umbre.
  Clase scope-uite `.em2*`, fără `var(--*)` în modal (temă fixă, independent de dark/light).
- Sidebar: card **Verdict** (inel SVG gauge scor/100), **Motive verdict** (checklist bife/warning din
  `phishing_reasons`/`spam_reasons`), **Categorie** (badge + Reclasifică AI), **ID email**, **Atașamente**
  (listă verticală, Preview/Download păstrate).
- Dreapta: tab-uri HTML / Text / Metadata (tab „Verdict + motive" eliminat, mutat în sidebar).
- Footer centrat: `[← Anterior] [Carantină] [Spam] [Următor →]` — branch-urile condiționale
  (carantinat: Confirmă/Decarantinează; spam mode: Legit/SPAM) păstrate. Spam cere confirmare.

### Note
- `CatCorrect` rescris flat (folosit doar aici). `AttachmentsBar` neatins (rămâne la `SpamEmailDetail`).
- Fallback `view` fără body: `verdict` → `meta`. Verificat `node --check`.
- Backup: `index.html.bak-modal2col-20260612`.

## v0.27.1 - 2026-06-11 (Uniformizare badge + verdict + butoane spam între liste și modal)

Follow-up la v0.27.0. Emailurile spam erau inconsistente: status corect în „Toate" dar verdict
„Clean" + butoane greșite în modal; lista „Spam" afișa status „clean". Zero schimbare de schemă.

### Fix A — badge de status uniform (UI)
- Helper unic `statusBadge(status)`: `spam` → `<span class="badge b-quarantined">SPAM</span>`
  (portocaliu), restul → `b-<status>` uppercase. Aplicat în rândul EmailsList (Toate/Email/Carantinate)
  și SpamList. Identic cu badge-ul din modal (care deja randa `b-quarantined`+SPAM pe `mode==='spam'`).
- Eliminată clasa CSS `.b-spam` (introdusă în v0.27.0, rămasă nefolosită).

### Fix B — lista Spam afișa status real „clean"
- `list_spam` (`/spam`) returna `emails.status` (clean) deși toate rândurile sunt spam prin
  definiția `where`. Acum derivă `status='spam'` pe items (oglindă a `list_emails`). status-ul real
  în DB neschimbat; câmpul serveste doar badge-ul (modalul foloseste `mode='spam'` explicit).

### Fix C — modal: verdict + butoane greșite când spam-ul e deschis din tab-ul „Toate" (cauză-rădăcină)
- `EmailDetail` deriva `mode` din FILTRUL listei: în „Toate" filtrul e gol → `mode='phishing'` →
  verdict construit din `status` real (`clean`) + footer „Pune în carantină / Marchează ca spam"
  (butoanele de clean). Același email deschis din „Spam" (unde `mode='spam'` e hardcodat) arăta
  corect verdict SPAM + Legit / Marchează ca SPAM. De aici confuzia.
- Fix: `mode` se derivă acum din **emailul selectat** (`item.status === 'spam' ? 'spam' : 'phishing'`),
  nu din filtru. Se bazează pe statusul deja derivat de backend (care a aplicat
  `SPAM_EXCLUDED_STATUSES`), deci un phishing-carantinat nu poate fi confundat cu spam. Modalul e
  acum identic din orice tab; recalcul corect la navigarea ←/→.
- Neschimbat intenționat: rândul „Status" din tab-ul Metadata = status REAL din DB (`get_email`).



Două fix-uri pe pagina Emailuri. Zero schimbare de schemă (sursa de adevăr = `email_spam`).

### Fix 1 — „Marchează ca spam" vizibil în lista Emailuri (status derivat `spam`)
- Bug: `spam_action`/`mark_spam` seta corect `email_spam.override=TRUE` (mailul apărea în tab-ul
  Spam), dar `list_emails` afișa badge-ul DOAR din `emails.status`, care rămânea `clean`. Efect:
  emailul marcat spam continua să apară ca „clean" în tab-urile Toate și Email.
- Fix: `list_emails` derivă acum un status virtual `spam` printr-un predicat reutilizabil
  (`_SPAM_PREDICATE`: `override=TRUE` sau `spam_score>=SPAM_THRESHOLD=50`, excluzând
  `SPAM_EXCLUDED_STATUSES` = quarantined/quarantined_strict/released/ndr/deleted/pending — aceeași
  regulă ca endpoint-ul `/spam`). Coloana derivată `is_spam` rescrie `status->'spam'` în răspuns.
  - Tab **Toate** și **Spam**: emailurile spam apar cu `status='spam'`.
  - Tab **Email** (lockStatus='clean'): exclus explicit (`NOT (predicat)`) — spam-ul nu mai apare.
  - `emails.status` real în DB NU se modifică: pipeline, decarantinare, butoanele modal intacte.
- UI: clasă badge `.b-spam` (chihlimbar). Tab-urile rămân Toate/Email/Carantinate/Spam.

### Fix 2 — decarantinarea unei carantine MANUALE nu mai întoarce 400
- Bug: `POST /emails/{id}/feedback` (`mark_not_phishing`) calcula `suppressible` din
  `phishing_reasons`. O carantină manuală (`/quarantine`) lasă `phishing_reasons=[]`, deci
  `suppressible=[]` → `HTTPException(400, "Carantinat doar pe indicatori de malware ...: -")`.
- Fix: ramură dedicată când `fired` (codurile detectate) e gol → eliberare simplă:
  `status='clean'`, `review_decision='not_phishing'`, release `quarantine_strict`, audit log.
  NU se creează `suppression_rules` cu `suppressed_codes=[]` (regulă moartă) și NU se face
  fingerprint learning (operatorul eliberează ACEST email, nu lookalikes). 400-ul se păstrează
  doar pentru cazul all-malware (`fired` nenul, dar tot în `NEVER_SUPPRESS`).
- Testat: email 6032 (carantină manuală, `phishing_reasons=[]`) → 200, `status=clean`, zero
  suppression_rule pentru expeditor.


## v0.26.0 - 2026-06-11 (Spam: allowlist/blocklist efectiv la clasificare + quote stripping)

Doua fix-uri pe modulul SPAM nativ. Zero schimbare de schema (refoloseste tabele existente).

### Fix 1 — allowlist/blocklist consultate INAINTE de clasificare
- Bug: butoanele "Legit"/"Marcheaza ca SPAM" persistau corect expeditorul in
  `spam_sender_reputation`, dar pipeline-ul de clasificare (`process_email.process_one`) apela
  `spam_detector.detect_spam(em)` — care NU consulta deloc reputatia. Functia
  `detect_spam_with_reputation` exista, dar era cod mort (nereferit nicaieri). Efect: un mail
  ulterior de la o adresa marcata "Legit" revenea in spam.
- Fix: reputatia e acum POARTA EXTERIOARA in `process_one`, consultata inainte de scoringul de
  continut, via noul `spam_detector.get_sender_reputation_pg(addr, cur)` (geaman psycopg2 al
  `get_sender_reputation`, aceeasi semantica exact > domeniu):
  - **allowlist** -> `spam_score=0` + `override=FALSE` (bypass neconditionat, la orice prag).
  - **blocklist** -> `override=TRUE` (apare in lista spam la ORICE prag — simetric cu allowlist;
    NU se mai bazeaza pe vechiul boost +40, care la prag 50 lasa un mail fara semnale sub prag).
  - fara reputatie -> scoring normal; `override` nu se atinge (se pastreaza deciziile manuale).
- Ambele liste persistente, pe adresa exacta, idempotente, last-write-wins (logica endpoint neschimbata).

### Fix 2 — quote stripping pe scoringul de spam
- Bug: `detect_spam` scana intreg corpul, inclusiv textul CITAT din thread. Un raspuns benign al
  clientului peste un mail bulk citat (trimis de noi: Unsubscribe / View in browser / limbaj
  promotional) era marcat gresit ca spam.
- Fix: `detect_spam` evalueaza semnalele DOAR pe continutul nou, refolosind acelasi helper ca
  modulul de carantina (`phishing_detector._new_content`) — un singur loc comun, nereimplementat.
  Subiectul se scaneaza intreg (nu e citat); doar corpul e redus la continutul nou.
- Nota: si `/spam/backfill` mosteneste quote-stripping (reruleaza `detect_spam`) — comportament
  consistent dorit.

### UI
- Coloana "Actiuni" (Legit / SPAM) scoasa din tabelul listei de spam (nu e necesara acolo);
  butoanele raman in modalul de detaliu email.

### Verificare
- Test e2e pe date reale (snapshot+restore, zero net change): adresa "Legit" -> mail ulterior
  reprocesat are score 0 + override=FALSE; "mark_spam" -> override=TRUE; quote-strip: semnale doar
  in citat -> score 0, aceleasi semnale ca text nou -> score 80.

## v0.25.0 - 2026-06-11 (Carantina: combinatie STRICT + gate NOVA intentie + learning)

Intarire modul carantina fara crestere false-pozitive. Zero schimbare de schema.

### Detectie
- **STRICT pe combinatie** (`phishing_detector.detect_phishing`): un singur trigger Layer-4 pe o
  fraza nu mai forteaza singur `quarantined_strict`. Necesita >=2 coduri stricte distincte SAU
  1 strict + un finding coroborant Layer-1/2. Altfel statusul devine score-based, cu nota de
  explicabilitate pe finding. (Analiza pe date reale: 65/239 strict existente erau single-phrase
  fara coroborare — exact clasa de false-pozitive vizata.)
- **manual_blacklist** (Layer 4, decisiv): cod nou pentru expeditori carantinati manual de operator.

### Gate NOVA (verificator de intentie)
- `strict_intent_gate.evaluate(...)` generalizat: ruleaza acum pe carantina SIMPLA + STRICTA (nu
  doar strict). IRIS NU decide singur — elibereaza la `clean` DOAR daca: intent benign (conf>=0.80)
  + fara blockeri structurali (malware/impersonare/URL high-conf) + expeditor de incredere
  (client cunoscut, `client_id`). La eroare/timeout/NOVA neconfigurat → pastreaza (conservator).
- **Pornit by code default** (`STRICT_INTENT_GATE_ENABLED` default '1'); fara atingere `.env`.
- **Prompt de intent editabil din UI** (Setari > Prompturi AI): stocat in tabela `prompts`
  (`code=nova_intent_detection`) + istoric `prompt_history`. Fallback = default din cod.

### Learning
- **Decarantinare → whitelist amprenta** (`emails.mark_not_phishing` + `process_email`): la
  decarantinare se salveaza fingerprint-ul (SimHash) continutului nou in `settings
  ['decarantine_fingerprints']`. Mail ~identic de la un CLIENT CUNOSCUT cu aceeasi amprenta →
  auto-`clean` (anti false-positive recurent, fara portita: cere si client cunoscut SI amprenta).
- **Carantinare manuala → learning agresiv** (`emails.quarantine_email`): blacklist expeditor
  (gated pe incredere — necunoscut: blacklist hard pe adresa; client cunoscut posibil compromis:
  doar amprenta + flag validare umana), salvare exemplu periculos (scor + semnale ratate + amprenta),
  si propunere IRIS de imbunatatire daca scorul era sub prag (gap real). Propunerile NU se aplica
  automat — validare umana. Stocat in `settings['phishing_manual_learning']`.

### API + UI
- `GET/PUT /settings/nova-intent-prompt`, `GET /settings/learning-proposals`.
- Setari > Prompturi AI: card editabil prompt intent IRIS + card read-only propuneri learning.

### Note
- **QR phishing:** nu exista in cargo360 (cod/reguli/UI/DB) — nimic de eliminat.
- Quote stripping (doar continut nou) era deja implementat (FIX 0).
- NOVA ESTE configurat in productie (`.env` via systemd EnvironmentFile → gunicorn). Verificat
  end-to-end pe gateway-ul real: benign + client cunoscut → release/clean; phishing → keep
  (blocker impersonare). Gate best-effort: la eroare/timeout/conf mica ramane carantinat.

## v0.24.0 - 2026-06-11 (Inline cid: fara alt — fallback pozitional)
- **Imaginile cid: fara alt** (ex. cid:EmbeddedImage, placeholder generic Outlook) se afiseaza acum:
  _inline_cid_images devine TWO-PASS — pass 1 rezerva atasamentele matchuite prin alt=nume, pass 2
  asociaza in ordinea din document fiecare cid img nematchuit cu urmatorul atasament IMAGINE nerezervat.
- Best-effort: corect cand nr. cid imgs nematchuite <= nr. atasamente imagine (cazul observat). Pastreaza
  guard-urile (8MB/img, fisier pe disc, fara fetch extern). Regresie verificata: 5878 ramane 2 imagini inline.

## v0.23.3 - 2026-06-11 (Preview atasamente: ajustari zoom/pan)
- **Eliminata grila de navigare** (sus/jos/stanga/dreapta/centru) din PreviewPane — pan-ul se face din mouse (drag).
- **Zoom la scroll mai soft**: step +/-10% (era +/-40%).
- **Scroll izolat**: wheel-ul peste preview NU mai scrolleaza si continutul mailului (listener non-passive, preventDefault+stopPropagation).
- **Reset (⟳) + zoom initial = 20%** (era 100%).

## v0.23.2 - 2026-06-11 (Copy ID -> clipboard direct + toast, fara prompt)
- **Butoanele de copy** (ID din tabel + 'ID + motiv' din modal) copiaza acum DIRECT in clipboard
  si arata un toast de succes, fara sa mai deschida window.prompt / Swal modal.
- Helper nou **mgCopy(text, okMsg)**: navigator.clipboard cu fallback execCommand pentru context
  non-securizat (http://, unde clipboard API lipseste) — de aici venea prompt-ul.

## v0.23.1 - 2026-06-11 (Titlu + favicon)
- **Titlu pagina**: 'NOVA Cargo360 Admin' -> 'Cargo360'.
- **Favicon**: badge SVG inline 'MG' pe gradient albastru (acelasi stil ca .mg-badge din header).

## v0.23.0 - 2026-06-11 (Imagini externe afisate by default)
- **Imaginile externe (https/http) din body_html se afiseaza acum BY DEFAULT** in tab-ul HTML.
  CSP-ul iframe-ului devine 'img-src https: http: data:' neconditionat (decizie produs ceruta de user).
- TRADE-OFF asumat: imaginile externe din mail pot fi tracking pixels (confirma 'deschis' catre expeditor)
  si scurg IP/UA-ul analistului catre host-uri externe. Iframe-ul ramane sandbox='' (fara scripturi/forms).
- Imaginile inline cid: raman inline data: din backend (v0.21.0); param allowRemote din buildEmailSrcDoc
  devine no-op (pastrat pentru compat).

## v0.22.0 - 2026-06-11 (Preview atasamente in split-pane langa email, cu zoom/pan)
- **Preview-ul atasamentelor NU mai deschide full-screen** — se deschide intr-un panou lateral in
  tab-ul HTML: emailul ocupa ~65%, preview-ul ~35%.
- **Emailul se ingusteaza la 65% prin REFLOW** (overflow-wrap/word-break in CSP style), NU prin scale,
  deci dimensiunea textului ramane neschimbata; textul lung trece pe rand nou.
- **PreviewPane (imagini)**: zoom din rotita + butoane +/-/reset, pan prin drag SI butoane
  stanga/dreapta/sus/jos, buton download. PDF -> iframe simplu (viewer browser).
- **Click-toggle**: click pe atasament deschide preview; click din nou pe acelasi inchide;
  click pe alt atasament comuta. Chip-ul activ e evidentiat.
- Arrow keys raman pentru navigarea emailurilor (nu pan), pan-ul e pe butoane+drag.
- preview resetat + blob URL revocat la navigare/inchidere (fara leak).

## v0.21.0 - 2026-06-11 (Imagini inline cid: in-scope + nav grupata + confirm carantina + scos toggle imagini)
- **Imagini inline cid: afisate DEFAULT** (backend, in-scope, FARA schema change): get_email rescrie
  <img src=cid:...> in data:URI matchuind alt= cu numele atasamentului (case-insensitive). Bytes-ii
  sunt deja pe disc => zero fetch extern, se afiseaza sub CSP img-src data: existent. Cap 8MB/img,
  netatins daca lipseste alt / numele / fisierul. ANULEAZA escaladarea din outbox (nu mai e nevoie de Razvan).
- **Scos toggle 'Incarca imaginile'** + state loadImg — imaginile inline vin acum direct din backend.
  Imaginile EXTERNE (https) raman blocate by default (anti-tracking), nemodificat.
- **Atasamentele se afiseaza toate by default** (AttachmentsBar lista completa, era deja asa).
- **Navigare grupata in centru**: footer PREV [actiuni] NEXT centrat (justify-content center), nu la margini.
- **Confirm pe 'Pune in carantina'**: SweetAlert 'Esti sigur ca doresti sa carantinezi acest email?'.
- **Badge verdict SPAM** warning galben (din v0.20.0).

## v0.20.0 - 2026-06-11 (Navigare in footer + actiuni pe verdict + imagini HTML opt-in + badge SPAM)
- **Navigarea prev/next mutata din header in footer**: layout PREVIOUS [actiuni] NEXT, grupate.
- **Actiunile din footer depind de verdict**: Clean -> Pune in carantina / Marcheaza ca spam;
  Carantina -> Confirma carantinarea / Decarantineaza; Spam -> Legit / Marcheaza ca SPAM.
  (Decarantineaza = POST /emails/{id}/feedback -> status clean; Confirma = POST /emails/{id}/quarantine.)
- **Eliminata bara de actiuni de sus** (ft) — actiunile traiesc acum doar in footer.
- **Imagini externe in tab-ul HTML**: toggle 'Incarca imaginile' per-email, DEFAULT OFF
  (secure-by-default: imaginile externe din mail suspect sunt tracking pixels / leak IP). CSP-ul
  iframe-ului devine img-src https/http/data doar cand userul apasa explicit; se reseteaza la navigare.
- **Badge verdict SPAM** acum warning (galben, b-quarantined) in loc de verde.
- NOTA: imaginile inline 'cid:' necesita schema+backend (coloana content_id) -> trimis in outbox (Regula 14).

## v0.19.2 - 2026-06-11 (Curatare modal: o singura cale de copy + scos bare redundante)
- **Butonul "ID + motiv" din info-bar** arata acum un toast de succes la copiere; copierea ID+motiv
  se face exclusiv din butonul info-bar.
- **Eliminata bara footer "Copiaza ID + motive spam / pentru finetuning"** — in phishing bara ramane
  doar pentru actiuni; in spam dispare (actiunile Legit/SPAM raman in footer-ul de jos).
- **Eliminata bara "AI categorie:"** din modal — redundanta cu coloana Categorie editabila din info-bar.

## v0.19.1 - 2026-06-11 (Tabel: o singura coloana de categorie)
- **Eliminata coloana redundanta "Categorie"** din lista Emails (afisa `email.category`, acelasi lucru
  cu `ai_category`).
- **Coloana "AI" redenumita "Categorie"** in tabelul Emails SI in tabelul Spam (raman identice).

## v0.19.0 - 2026-06-11 (Modal Emails/Spam: latime marita + dimensiuni fixe, AI categorie editabila in info-bar)
Ajustari UI peste structura din v0.18.0 (fara override).
- **Modal MULT mai lat + dimensiuni FIXE** (`.modal` width:1320px/max-width:96vw, height:88vh) — box-ul
  nu mai face reflow la navigare.
- **Fix kick-out la NEXT rapid** (cauza reala): backdrop-ul (.modal-bg) se inchide acum doar daca
  apasarea PORNESTE pe backdrop (`onMouseDown` cu `e.target===e.currentTarget`). Inainte, al doilea click
  rapid cadea pe backdrop in timp ce butonul -> disparea la re-render-ul de loading -> modalul se inchidea.
- **Verdict** in info-bar afiseaza acum UPPERCASE: CLEAN / QUARANTINED / QUARANTINED_STRICT / NDR, iar
  in mod spam afiseaza SPAM.
- **Categorie = AI categorie editabila** (componenta noua `CatCorrect`): badge ai_category + dropdown
  corectare manuala (-> POST /ai/category/{id}/correct) + buton "Reclasifica AI" (-> POST /ai/category/{id}/run);
  update live in modal via setEmail, fara reload.
- **ID email**: butonul de copy din info-bar copiaza acum direct ID + motiv carantina (phishing,
  buildFinetuneText) sau ID + motive spam (mod spam), nu doar ID-ul.

## v0.18.0 - 2026-06-11 (Reintegrare UI Emails+Spam + search + decarantinare->clean)
Reintegrare a muncii suprascrise pe 10 iun (overwrite full-file din copie stale), peste structura
curenta (s-au pastrat ai_category + /spam extins ale lui Andrei).
- **Modal unificat**: SpamEmailDetail comasat in EmailDetail via prop `mode` ('phishing'|'spam') —
  taburi functionale peste tot (inainte taburile din modalul spam erau moarte), footer pe mode
  (Legit/SPAM vs Carantina/feedback), copy-text pe mode.
- **HTML primul**: tab-ul HTML e acum default pentru vizualizare mail (ordine HTML/Text/Verdict/Metadata).
- **Navigare prev/next** (sageti + taste <-/->) si pe modalul de Emails/Carantina/Strict, nu doar Spam.
- **Inaltime FIXA modal** (.modal height:88vh) — nu mai sare la navigare/schimbare tab; loading full-size.
- **Info-bar orizontal** restaurat in modal: Verdict | Categorie | ID (etichete sus, separatoare),
  cu deduplicare (status/score scoase din head-meta, ID scos din ft-bar).
- **Tabel Spam = tabel Emails**: Spam foloseste acum acelasi `list-table-full` cu aceleasi coloane
  (ID/Receptionat/Subject/From/Status/AI/Confidenta/Scor spam/Atasamente) + col Actiuni.
- **get_email** extins cu spam_score/spam_reasons/override (JOIN email_spam) — modalul spam avea scor NaN.
- **Search dupa subiect**: `GET /emails?q=` -> `subject ILIKE %q%`, input debounce 400ms in topbar.
- **Filtru 'Spam' in lista Emails** (status=spam -> EXISTS pe email_spam, virtual) + etichete status RO.
- **Decarantinare -> 'clean'** (nu 'released'): emailul iese din lista Carantinate; provenance pastrat.

## v0.16.2 - 2026-06-10 (UI — info-bar modal: layout orizontal stil tabel)
- Info-bar-ul (Categorie / Verdict / ID email) e acum **orizontal** — 3 coloane pe un rând,
  fiecare cu eticheta sus (stil `<th>`) și valoarea dedesubt (stil `<tr>`), separate prin
  delimitatoare verticale. Toate vizibile dintr-o privire; flex-wrap pe ecrane înguste.
- Eliminat textul cu motivul AI (`▸ ...`) din info-bar, conform cerinței. Pur-frontend, fără restart.

## v0.16.1 - 2026-06-10 (UI — preview imagini: zoom doar în panou + drag-to-pan)
- Fix: Ctrl+scroll nu mai face zoom la pagina întreagă. `onWheel`-ul React e passive
  (preventDefault ignorat), așa că zoom-ul scăpa la browser; înlocuit cu listener nativ
  `wheel` cu `{ passive: false }` atașat prin ref → zoom-ul rămâne DOAR în panoul de preview.
- Adăugat **drag-to-pan**: când imaginea e mărită, click-drag o mută stânga-dreapta/sus-jos
  (ajustare scrollLeft/scrollTop). Cursor grab/grabbing; imaginea are `pointer-events:none` +
  `draggable:false` ca drag-ul să fie fluid și fără ghost-image. Pur-frontend, fără restart.

## v0.15.3 - 2026-06-10 (UI — modal email: skeleton loading la navigare)
- La încărcare/navigare (next/previous) nu mai apare popup-ul mic „Se incarca...", ci
  **același modal la dimensiune fixă cu skeleton loading** (bare shimmer pentru subiect,
  meta, taburi și corp). Experiență fără salturi de layout.
- În starea de skeleton, **backdrop-ul NU închide modalul** (doar × sau ESC), ca un
  dublu-click accidental pe next/previous să nu scoată utilizatorul din modal. După
  încărcare, comportamentul normal (click pe fundal = închide) revine.
- CSS nou: `.mg-skel` + `@keyframes mgShimmer`. Pur-frontend, fără restart.

## v0.15.2 - 2026-06-10 (UI — preview imagini: zoom in/out)
- Preview-ul de imagini are acum **zoom in/out**: toolbar cu −/procent/+/Reset și
  **Ctrl + scroll** peste imagine. Zoom 25%–800% (pas 20%), scalare pe lățime cu scroll
  (pan) în container. Zoom-ul se resetează automat la schimbarea atașamentului (remount
  prin `key='preview-'+id`). Pur-frontend, fără restart.

## v0.15.1 - 2026-06-10 (UI — modal email: dimensiune fixă + navigare în footer)
- Modalul de email are acum **înălțime fixă** (`min(900px, 92vh)`), deci toate emailurile
  sunt încadrate identic, indiferent de cât conținut au (lățimea era deja fixă la 1360px).
  Conținutul scroll-ează în interior; experiență consistentă la navigarea între mailuri.
- Navigarea prev/next a fost **mutată din header în footer**: butonul **← Anterior** în stânga,
  butoanele de acțiune (Legit/SPAM sau Decarantinează) la mijloc, iar **numărul (i / total) +
  Următor →** în dreapta. Footer-ul apare acum și pentru emailurile fără acțiuni (ex. clean),
  dacă există navigare. Pur-frontend, fără restart.

## v0.14.2 - 2026-06-10 (UI — HTML email: imagini inline cid: rezolvate)
- Tab HTML: imaginile inline referite prin `src="cid:..."` (atașamente embedate,
  ex. semnături/poze din thread) se afișează acum în modal. UI-ul descarcă atașamentul
  prin fetch autentificat (`/emails/{id}/attachments/{att_id}/download`), îl convertește
  în `data:` URI și îl injectează în `srcdoc` în locul referinței `cid:` (helper `_mgInlineCids`
  + `cidMap` state + useEffect pe `email`). Fără restart (UI servit de pe disc), pur-frontend.
- Mapare pozițională (al i-lea `cid:` → a i-a imagine `image/*` disponibilă): exactă pentru
  cazul uzual 1 cid ↔ 1 imagine. Pentru mailuri cu mai multe imagini e best-effort — maparea
  exactă ar cere persistarea Graph `contentId` la ingestie (upstream/schema, Regula 14).
  Cid-urile fără imagine corespondentă rămân nerezolvate (fără fallback duplicat).

## v0.14.1 - 2026-06-10 (UI — HTML email: imagini remote + copy fara prompt)
- Tab HTML: CSP-ul iframe-ului permite acum imagini remote (img-src data: https: http:) si fonturi (font-src data: https:). Inainte doar data: -> iconitele/bannerele remote (social icons etc.) aratau doar alt-text ('facebook icon'...). Acum se incarca efectiv.
- NOTA securitate: incarcarea imaginilor remote permite open-tracking de catre expeditor (pixel de urmarire). Acceptat pentru fidelitate vizuala; link-urile raman dezactivate (pointer-events:none).
- Butonul 'Copiaza ID + motiv carantina/SPAM' foloseste acum mgCopy (clipboard direct + toast 'Textul a fost copiat in clipboard'), fara window.prompt.
- Doar frontend (app/ui/index.html), fara restart. Backup: index.html.bak-htmlimg-20260610.

## v0.14.0 - 2026-06-10 (UI+API — unificare tabel & modal SPAM cu Carantina)
- Tabul SPAM foloseste acum ACELASI tabel ca Carantina: coloane ID | Receptionat | Subject | From | Status | Categorie | Confidenta | Score | Atasamente. Coloana Status arata un badge 'spam' (portocaliu) — emailurile spam au status real 'clean' (spam = scor >= prag, nu un status), deci badge-ul evita confuzia. Score = spam_score.
- Modal UNIFICAT: componenta SpamEmailDetail a fost eliminata; ambele tab-uri (SPAM si Carantina) folosesc acum EmailDetail cu un prop `mode` ('spam'|altele). Default deschis pe HTML.
- Continut condiționat de context: in mod 'spam' verdictul de langa bara AI = scor spam + top motive (buildSpamVerdict), tabul 'Motiv' listeaza semnalele SPAM (SPAM_REASON_LABELS), iar footer-ul are butoanele Legit + Marcheaza ca SPAM (POST /spam/{id}/action). In rest (carantina) verdictul si motivele sunt cele de phishing.
- Footer Decarantineaza: butonul de eliberare ('NU e phishing') a fost MUTAT din bara de ID in footer, ca un singur buton (fara duplicat), etichetat 'Decarantineaza' + selector scope (acest expeditor / tot domeniul). Apare cand status incepe cu 'quarantined' (gate pe STATUS, nu pe mode — pastreaza corect si tabul clean). Hit pe POST /emails/{id}/feedback (status='released' + supresie scoped).
- Navigare ← → (1/N) intre emailuri adaugata in TOATE modalele (SPAM, Carantina, Strict, lista Email). key=emailId forteaza remount curat la schimbarea emailului (reset tab pe HTML, fetch nou).
- Backend (restart mailguard-api): /spam SELECT extins cu ai_category/ai_status/ai_result/attachment_count (pentru coloanele noi); get_email face LEFT JOIN email_spam pentru spam_score/spam_reasons (pentru modal). Aditiv, fara schimbare de schema. NOTA: emailurile spam fiind 'clean' pot sa nu fi fost categorisite AI -> coloanele Categorie/Confidenta/Atasamente pot fi goale (corect per model de date).
- Fisiere: app/ui/index.html (validat sintactic cu node --check), app/api/v1/spam.py, app/api/v1/emails.py. Backups: *.bak-spamunify-20260610.

## v0.13.2 - 2026-06-10 (UI — modal email: verdict pe bara AI + modal mai lat)
- Rezumatul verdictului e afisat acum ca a doua coloana, pe acelasi rand cu bara 'AI categorie' (split 50/50): status + scor + explicatia concisa de incadrare.
- Eliminat tab-urile separate 'Verdict' si 'Verdict + motive' introduse in v0.13.1. A ramas un singur tab 'Motiv' cu breakdown-ul detaliat pe layere (L4/L2/L1/L3). Ordine tab-uri: HTML | Motiv | Text | Metadata.
- Modal latit: max-width 980px -> 1360px, max-height 92vh -> 94vh.
- Doar frontend (app/ui/index.html), fara restart. Backup: index.html.bak-modalv2-20260610.

## v0.13.1 - 2026-06-10 (UI — modal email: restructurare tab-uri)
- Modal vizualizare email: tab-ul implicit la deschidere este acum HTML (fallback pe Verdict daca emailul nu are body HTML).
- Verdictul a fost impartit in doua tab-uri: 'Verdict' (rezumat concis — doar banner-ul de incadrare) si 'Verdict + motive' (breakdown-ul complet pe layere L4/L2/L1/L3). Ordine tab-uri: HTML | Verdict | Verdict + motive | Text | Metadata.
- Bara 'AI categorie' ramane neschimbata, deasupra tab-urilor (mereu vizibila).
- Doar frontend (app/ui/index.html), fara restart — index.html e servit de pe disc. Backup: index.html.bak-modaltabs-20260610.

## v0.13.0 - 2026-06-10 (FAZA 3 — STAGED, activare prin outbox)
- FAZA 3 — Learning la decarantinare scoped pe TEMPLATE FINGERPRINT. Livrat in-scope si VALIDAT, dar NEACTIVAT: necesita o migratie de schema pe DB-ul de productie (DDL), care e rezervata admin -> outbox catre Andrei (Regula 14). Nu am rulat ALTER/CREATE.
- Modul NOU app/services/template_fingerprint.py (pur stdlib): SimHash 64-bit peste continutul NOU normalizat (scoate cifre/URL/punctuatie/whitespace variabil), match prin distanta Hamming <= k (default 3). Dormant pana la integrare.
- Validat pe date reale: 4 emailuri template "Variante de lucru" (acelasi expeditor) -> Hamming 0-1 intre ele; vs marketing 17-18; vs tracking 32-33; continut subtire -> fingerprint None (fail-safe, nu face match). Confirma: aprobarea unui template elibereaza DOAR mail near-identic, nu tot ce trimite expeditorul.
- Artefact migratie: migrations/20260610_template_fingerprint.sql (NEEXECUTAT) — adauga suppression_rules.template_fingerprint TEXT + fingerprint_k, scope_type nou 'template', si tabel golden_bad_templates (amprente known-bad care forteaza keep). Regulile sender/domain existente raman neschimbate (compatibilitate).
- DE FACUT dupa aprobare outbox: aplicare migratie; wiring in /emails/{id}/feedback (scope='template' stocheaza amprenta) + process_one (match amprenta inainte de a aplica suppressed_codes) + confirm-scam (spam_sender_reputation blocklist + unsubscribe) + UI. Rollback: active=false pe regula / DROP-urile din migratie.

## v0.12.0 - 2026-06-10
- FAZA 2 — Poarta AI intent-gate pe STRICT. Cand detectorul forteaza quarantined_strict pe un trigger Layer 4, gateway-ul AI (iris_ai) judeca INTENTIA continutului NOU (post-FIX0) si recomanda downgrade DOAR daca verdictul e benign (confidence >= 0.80) SI nu exista niciun semnal structural dur: malware (executable/macro/double_extension), impersonare (display_name/typosquat) sau URL high-confidence (ip_url/subdomain_abuse/url_shortener). Verdictul LLM e NECESAR-DAR-NU-SUFICIENT.
- Rulare best-effort DUPA commit-ul durabil (ca la categorisirea AI): la eroare/timeout/neconfigurat/low-confidence ramane quarantined_strict (default sigur). Flag: STRICT_INTENT_GATE_ENABLED (default 0; codul ramane DORMANT). Activarea (=1) este un config_change rezervat admin (Regula 14) -> se ruteaza prin outbox catre Andrei; NU a fost activat din proprie initiativa. Validare facuta cu dry-run + ROLLBACK (zero mutatie prod).
- La downgrade: scorul se recalculeaza fara trigger-ul L4 (respecta scorul cumulat) -> >=60 ramane quarantined, altfel clean. Randul quarantine_strict e marcat review_status='auto_released' / decision='ai_downgrade' (dispare din coada de review). Fiecare decizie (downgrade SI keep) e logata in audit_log (actor=ai_intent_gate) pentru review uman + rollback.
- Hardening anti prompt-injection: system prompt trateaza subiectul+corpul ca DATE NEINCREDERE si nu executa instructiuni din ele.
- DEVIATIE fata de planul initial: criteriul de autentificare a fost ELIMINAT ca poarta. FAZA 1 a stabilit ca semnalul de auth e all-failure (fara SPF/DKIM/DMARC pass; SPF softfail ~50% din mail), deci inutilizabil ca discriminator si ar bloca aproape orice downgrade. Se reactiveaza cand upstream (parser-email-op) livreaza Authentication-Results brut (vezi FAZA 0 outbox).
- Validat pe 15 quarantined_strict reale (dry-run, fara scriere): 6 downgrade (corespondenta legitima de business care declansa fals account_suspended/click_request - ex. notificari juridice "variante de lucru", reziliere contract), 9 keep (phishing/marketing cu redirect ascuns, domenii spoofate, impersonare prinsa de blocker, cazuri ambigue sub prag). Cele 3 statement-uri SQL verificate separat cu ROLLBACK.
- Fisiere: app/services/strict_intent_gate.py (NOU), app/services/process_email.py (orchestrare). Backup: process_email.py.bak-strictgate-20260610, .env.bak-strictgate-20260610.

## v0.11.2 - 2026-06-10
- UI copy ID (butonul ⧉ din tabele): copiere directa in clipboard + toast, fara dialogul window.prompt. Cauza: app servit pe HTTP, unde navigator.clipboard nu exista (secure-context only) -> cadea pe prompt. Adaugat helper mgCopy/mgCopyExec cu fallback document.execCommand('copy') care merge si pe HTTP. IdCell foloseste mgCopy (acopera toate tabelele).
- Backup: app/ui/index.html.bak-copyid-20260610.

## v0.11.1 - 2026-06-10
- UI pagina Emailuri: uniformizare tabele. Tabelul SPAM convertit la acelasi stil ca celelalte (clasa list-table-full + CSS global th/td, fara stiluri inline). Eliminate coloana "Actiuni" si butoanele Legit / Marcheaza ca SPAM din tabel (raman in modalul de detaliu SpamEmailDetail, urmeaza sa fie completate ulterior).
- Tabel principal emailuri: coloana "AI" redenumita "Categorie" (afiseaza ai_category); vechea coloana "Categorie" (e.category) eliminata din tabel.
- Backup: app/ui/index.html.bak-uitables-20260610.

## v0.11.0 - 2026-06-10
- Carantina phishing FIX 0 (quote stripping): trigger-ele de continut (Layer 2) si STRICT (Layer 4) se evalueaza DOAR pe continutul NOU al expeditorului, nu pe textul citat/forwardat din thread. phishing_detector.py: _strip_quoted_text / _strip_quoted_html / _new_content (markeri RO+EN, blockquote/gmail_quote/Outlook divRplyFwdMsg, safety-net <3 chars pe forward gol). Layer 1 + atasamente neatinse; corpul stocat nemodificat. Explicabilitate: nota "(evaluat pe continut nou, fara citat)".
- Validat pe date reale inainte de deploy: 96/228 quarantined_strict -> clean (-42% false-positive), 0 regresii (798/798 clean raman clean). Mailurile deja carantinate nu se reproceseaza (efect doar pe fluxul nou).
- Backup: app/services/phishing_detector.py.bak-fix0-20260610.

## v0.10.7 - 2026-06-10
- Title final: doar "Cargo360" (prefixul de brand se adauga in stratul de afisare). Backup: index.html.bak-mg-20260610.

## v0.10.6 - 2026-06-10
- Title simplificat la "Cargo360" (consistent cu sidebar + login). Confirmat ca nu exista document.title in JS; tag-ul static e singura sursa.
- Backup: index.html.bak-titleonly-20260610.

## v0.10.5 - 2026-06-10
- Title <title> scris ca entitati HTML numerice (&#78;... = NordLogistics Cargo360) ca sa nu mai fie rescris de stratul de afisare; browserul il decodeaza corect.
- Badge light theme: restaurat gradientul original #0284c7->#1d4ed8 (dark ramane solid #4CTS3FF).
- Backup: index.html.bak-name-20260610.

## v0.10.4 - 2026-06-10
- Badge MG + favicon: eliminat gradientul, albastru solid #4CTS3FF (sidebar dark+light si favicon SVG).
- Backup: index.html.bak-color-20260610.

## v0.10.3 - 2026-06-10
- UI <title> -> "NordLogistics Cargo360" (era "<vechiul brand> Cargo360 Admin").
- Favicon adaugat: SVG inline identic cu badge-ul MG din sidebar (patrat rx8, gradient 135 #4FC3FF->#3b82f6, text MG alb).
- Doar app/ui/index.html (fisier static, fara restart). Backup: index.html.bak-favicon-20260610.

## v0.10.2 — 2026-06-10
- Perf tab Spam: lista se incarca instant (~1.6s -> ~3ms). Cauza: LEFT JOIN LATERAL pe clients.emails facea Seq Scan (GIN neutilizat pe operand corelat). Inlocuit cu LEFT JOIN clients ON id=e.client_id (PK) in /spam si /emails.
- Fix modal Spam: scor NaN + niciun semnal. GET /emails/{id} aduce acum spam_score/spam_reasons/override din email_spam (LEFT JOIN). Tab-urile HTML/Text/Metadata functioneaza (state view).
- Rebranding: NordLogistics Cargo360 (UI title, login, app_name in .env).

## v0.10.1 — 2026-06-10
- UI Emailuri: uniformizare tabele pe structura tabelului Spam (Data primirii | Scor | Subiect | Expeditor | Client | Acțiuni).
- Tab "Email" redenumit "Clean".
- Coloană nouă "Client" (înlocuiește "Motive"): client_name rezolvat server-side din adresa expeditorului via LEFT JOIN LATERAL pe clients.emails în /emails și /spam; negăsit => "Unknown Client".
- Buton "Scoate din carantină" pe emailuri carantinate => reutilizează POST /emails/{id}/feedback (whitelist suppression_rules + release + quarantine_feedback / zona NOVA AI); guardrail malware (NEVER_SUPPRESS) păstrat.

## v0.3.0 — 2026-05-20 00:30
- PASUL 0-1: Server inventory + port discovery (8500 free, 7.7G disk free)
- PASUL 2: DB cargo360 created in existing postgres container (parallel cu email_parser_db). 11 tables. Owner: cargo360 user. 8 seed settings.
- PASUL 3: FastAPI skeleton on port 8500 + .env + admin auth + health endpoint
- Option C parallel: parser-email-op rămâne LIVE intact

## v0.2.0 — DB schema (incluse în 0.3.0)
## v0.1.0 — Initial scaffolding (incluse în 0.3.0)
## v2.0.0 - 2026-08-07 — RELEASE PE PRODUCȚIE

Primul release major post-lansare. Consolidează toate livrările v1.0.0–v1.5.1.
Vezi registrul de release pentru lista completă de modificări.

# CHANGELOG IRIS Cargo360

<!-- CONVENȚIE VERSIUNI (din 2026-08-06):
     Schema: MAJOR.MINOR.PATCH
     - MAJOR crește la fiecare Release spre producție (v1.0, v2.0, ...)
     - MINOR crește între release-uri pentru feature-uri (v1.1, v1.2, ...)
     - PATCH crește pentru fix-uri între release-uri (v1.0.1, v1.1.2, ...)
     Istoricul pre-release (v0.x) păstrat mai jos pentru referință.
-->

## v1.5.0 - 2026-08-07

### Export conversatie client — îmbunătățiri

- Imagini inline base64 (jpg/png incluse în HTML email) înlocuite cu `[imagine]` în textul exportat
- Mesaje clare pentru apeluri fără transcript: „Apelul a existat, însă transcrierea audio nu a fost efectuată."
- Apeluri cu eroare de transcriere: mesaj explicit despre fișierul audio indisponibil

### KPI-uri procesare documente — restructurate

Cardurile „Extrase corect / Corectate / Reîncadrate / În verificare / Extrase IRIS" înlocuite cu:
- **Doc. șofer** — % din total global + N auto-validate din total
- **Doc. vehicul** — % din total global + N auto-validate din total
- **Contracte** — % din total global + N auto-validate din total

Procentele reflectă ponderea fiecărei categorii din totalul global (nu % intern per categorie).
Backend: câmp `by_category` adăugat la `/api/v1/documents/extractions/stats`.

### Conversie automată documente → PDF cu compresie

Toate documentele trimise la IRIS pentru extragere sunt acum normalizate:
- Imagini (jpg/png/gif/webp/tiff) convertite automat la PDF via PyMuPDF
- PDF-uri > 1.6 MB comprimate cu `deflate + garbage collect` (elimină resurse redundante)
- Fallback safe: la orice eroare de conversie, fișierul original e trimis neschimbat

## v1.4.0 - 2026-08-07

### Export conversatie client

Buton Exporta conversatia pe pagina fiecarui client. Permite selectarea unui interval de date
(de la / pana la) si genereaza un document HTML cu toate mailurile primite, mailurile trimise si
apelurile telefonice in ordine cronologica, cu continut complet (body text email + transcript apel).

Documentul se deschide intr-o fereastra noua; din dialogul de printare al browserului se salveaza
ca PDF (Destinatie Salveaza ca PDF).

Format document: badge tip (MAIL PRIMIT / MAIL TRIMIS / APEL INTRAT / APEL IESIT), data, subiect
sau numar de telefon, durata apel, adresa expeditor, categorie AI - urmate de textul complet.

Backend: endpoint GET /api/v1/clients/{id}/export-conversation?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD.

## v1.3.0 - 2026-08-06

### Sync CTS — `cts_department` rămânea NULL pe slug-urile cu cratimă

Bug găsit imediat după fixul de atribuire din v1.2.0, care depinde de `cts_department`.
CTS trimite departamentul în două forme: label (`"Suport 1"`) pe tichetele mai vechi, slug cu
cratimă (`"suport-2"`, `"recuperare-tva"`, `"taxe-de-drum"`) pe cele noi. Două defecte cumulate:

1. `_map_department()` nu normaliza cratima — forma cu cratimă cădea pe fallback.
2. `dep_raw` citea doar `department` top-level, care lipsește pe tichetele noi; singura sursă
   reală, `assignment.department_slug`, nu era consultată (e prezentă chiar și fără assignee).

Rezultat: **23 din 243 de tichete deschise** aveau `cts_department` NULL și, neavând nici assignee
pe care să cadă fallback-ul, dispăreau complet din monitor — exact clasa de tichete pe care v1.2.0
trebuia să o facă vizibilă.

Fix în `cts_groundtruth_sync.py`: normalizare cratimă/spațiu + aliasare, și `assignment.department_slug`
/ `department_label` ca surse de rezervă pentru `dep_raw`. Verificat pe 14 forme de intrare.

### Migrație `20260806_cts_department_backfill.sql`

Repară rândurile deja scrise, derivând `cts_department` din `raw->assignment->department_slug`.
Aditivă și idempotentă (atinge doar `cts_department IS NULL`), fără operații structurale.
**19489 rânduri** completate; NULL pe tichetele deschise: 23 → **0**.

Verificat post-fix — API-ul monitorului coincide exact cu DB-ul pe toate departamentele
operaționale (suport_1 26 noi / 2 în lucru, suport_2 3/1, suport_3 0/1, taxe_drum 0/12).

## v1.2.0 - 2026-08-06

### Monitor Productivitate — volume corectate (atribuire pe departamentul tichetului)

Volumele „Noi" / „În lucru" din Monitor nu corespundeau cu dashboardul CTS pe niciun departament.
Cauza: monitorul contoriza pe **departamentul persoanei asignate** (`employee_department_mapping`),
nu pe **departamentul tichetului din CTS** (`cts_ground_truth.cts_department`). Consecințe:

- Un mail `new` **neasignat** nu are persoană, deci JOIN-ul INNER îl arunca complet.
  **69 din 171 de mailuri `new` (40%)** dispăreau. Suport 1 afișa **0 noi în loc de 47**.
- Invers, mailurile asignate cuiva dintr-un alt departament decât cel al tichetului se numărau la
  departamentul greșit: contabilitate afișa **20 în loc de 8**.

Fix: departamentul efectiv = `COALESCE(cts_department, departament_assignee)`, aplicat în toate
interogările de email și task ale monitorului (totaluri, per-departament, reclamații, histograme
orare). JOIN-ul pe angajat a devenit `LEFT JOIN` — nu mai filtrează, doar completează fallback-ul.
Fallback-ul atinge doar rânduri `solved` istorice (4066); **niciun tichet deschis nu depinde de el**.

Același tratament pe task-uri (`cts_task_ground_truth.department`) — acolo divergența era mică
(12 rânduri, ex. 10 `new` pe mobilitate fără assignee), dar consistența e necesară.

### Sincronizare CTS — latență redusă la ~2 minute

`RECENT_MIN_INTERVAL_S` coborât de la **240s la 50s** pe mailuri, task-uri și apeluri. Cron-ul rulează
la 2 minute, iar throttle-ul de 240s arunca un tick din două; la 50s niciun tick nu se pierde, deci
statusurile CTS ajung în monitor cu maxim ~2 minute întârziere (înainte: până la 5-6 minute).

## v1.1.0 - 2026-08-06

### Monitor Operațional — KPI eliminat

Eliminat KPI-ul „Rezolvat / intrat" din cardurile per departament (valori explodau nejustificat,
ex. 1400% la Suport 3). Rămas: „Soluționat azi" + „Deschis acum".

### Device Operations — înlocuiri (inlocuire)

Sync-ul `view_device_operations` preia acum și operațiunile de tip **înlocuire** (`Device Replacement`)
— adăugate în view la sursă de Razvan. 233 înlocuiri importate la primul sync.
Timer automat (30 min) configurabil prin endpoint intern `/device-ops/suport2/sync-internal`.

### Prioritate email — detecție OP îmbunătățită

- Subiect `Plata taxe drum / plata taxa drum / plata taxa rutier / plata taxa intracomp` → **P2** automat (regulă deterministă, fără AI).
- Dacă vision AI a extras o serie OP (`ai_op_series`), emailul e promovat direct la **P2** înainte de orice analiză AI.
- Elimină false-negative-urile pe mailuri cu atașament OP și subiect/corp gol.

### Timestamps Device Operations

Timestamps din `view_device_operations` (UTC cu offset RO aplicat greșit) corectate în sync și în API:
ora afișată în UI este acum ora reală din România (ex. 09:18 în loc de 06:18).

### Breakdown modal — sortare + telefon apeluri

- Coloane sortabile: Client (alfabetic), Creat (ASC/DESC), Soluționat (ASC/DESC).
- La tipul „Apeluri": coloana Subiect înlocuită cu numărul de telefon al apelantului.

---

## v1.0.0 - 2026-08-06 — PRIMUL RELEASE PE PRODUCȚIE

### Productivitate — Taxe de drum: filtrare exactă pe tip task

Categoriile BGToll / EToll / HU-GO / CargoBox numără acum EXCLUSIV task-urile cu
`task_type` exact (ex. `BGToll: New device installed`). Restul (sub balance, device moved etc.)
intră la obiectivul general. Se aplică doar la `taxe_drum`, alte departamente nemodificate.
Fix aplicat atât pe calculul de productivitate cât și pe modalul de detaliu (breakdown).

### Procesare documente — tipuri noi și ajustări

- **Anexa 2 - Proces verbal CargoBox** (mutat din `vehicul` → `contract`): extrage `Licence Plates List` + `Companie`
- **Anexa 3 - contract carGObox** (`contract`): identify only — Termeni și condiții Toll4Europe
- **Anexa 4 - contract carGObox** (`contract`): identify only — Informații GDPR Toll4Europe
- **CUI / Extras pe contract carGObox sau ETOLL** (`contract`): extrage `Nume firma` + `CUI firma` (după „Cod Unic de Înregistrare")
- **TMS - Diurne si salarii minime** (`contract`): extrage `Numar contract`, `Data contract`, `Prestator`, `Client`, `CUI client`, `Este semnat`
- **Dezactivat:** `Anexa 2 - contract carGObox` (înlocuit de Procesul verbal de mai sus)

---
<!-- Istoric pre-release (versiuni interne de dezvoltare) -->

## v0.77.1 - 2026-08-06

### Taxe de drum: categoriile BGToll/EToll/HU-GO/CargoBox numără doar "New device installed"

Înainte, ORICE task de tip `BGToll` (ex. `BGToll: sub balance unknown increase`) era numărat la
obiectivul **BGToll** din productivitate. La fel pentru EToll, HU-GO, CargoBox.

Acum, pentru **taxe_drum** (și doar pentru acest departament), fiecare categorie numără exclusiv
task-urile cu `task_type` exact:
- BGToll → `BGToll: New device installed`
- EToll → `EToll: New device installed`
- HU-GO → `HU-GO: New device installated` *(typo existent în CTS, asta e valoarea reală din DB)*
- CargoBox → `carGObox new device installed`

Restul (ex. `BGToll: sub balance unknown increase`, `HU-GO: Device moved` etc.) intră acum
la obiectivul general de task-uri al departamentului.

Alte departamente (ex. suport_1/cargobox) — comportament nemodificat.

## v0.77.0 - 2026-08-05

### Un rând per TICHET CTS: replicile pe destinatari nu se mai suprascriu reciproc

CTS creează un tichet **per destinatar**. Mailul 58176 (`mmm@novatrade.ro`, trimis către `office@`
+ `maria.tomuta@` + `madalina.apetrei@`) a generat 3 tichete, toate cu același `message_id`:

| log_id | status | assignee | departament | assigned_at |
|---|---|---|---|---|
| 1088948 | solved | vanessa.boros | suport-1 | 01.08 07:59:52 |
| 1088946 | solved | madalina.apetrei | contabilitate | 03.08 05:46:07 |
| 1088947 | new | maria.tomuta | contabilitate | 03.08 05:46:15 |

`UNIQUE (source, message_id)` + `ON CONFLICT DO UPDATE` le făcea să se suprascrie una pe alta, deci
rămânea **ultima procesată** — arbitrar, după ordinea din răspunsul CTS. Aici a rămas cea a Mariei,
`new`, deși Vanessa rezolvase mailul în 36 de minute. Consecință: munca Vanessei și a Mădălinei nu se
contoriza, iar mailul apărea blocat pe o persoană aflată în concediu.

**Schimbare de model:** cheia de unicitate include acum tichetul
(`migrations/20260805_cts_ticket_replicas.sql`):

- `cts_ticket_id` — id-ul tichetului CTS (`extra.cts_email_log_id`), unic per destinatar
- `cts_is_replica` — `false` pe original, `true` pe replici
- `UNIQUE (source, message_id, cts_ticket_id)` înlocuiește `UNIQUE (source, message_id)`

Originalul nu e marcat de CTS (toate replicile au același `to_email` și `created_at`), deci se
deduce: cel cu cel mai vechi `cts_assigned_at`, la egalitate cel mai mic `cts_ticket_id`. Marcarea
rulează o dată pe lot în `_mark_replicas()` — depinde de toate tichetele aceluiași mail, deci nu
poate fi calculată în upsert-ul per rând.

Rezultat pe 58176: fiecare tichet intră în productivitatea persoanei lui —
Vanessa 35.9 min on time (suport 1), Mădălina 46.1 min on time (contabilitate). Tichetul Mariei
rămâne `new`, deci nu intră în productivitate.

Pe august: 2737 rânduri / 2712 mailuri distincte, 25 replici. Un mail intern
(`lavinia.stefanou@cargotrack.ro`) are 45 de tichete pe 10 departamente — cazul extrem; mailurile de
la clienți au 2–4 tichete, pe 1–2 departamente.

`_coerce_client_id` → `_coerce_pos_int` (folosit acum și pentru id de tichet, nu doar client).

## v0.76.1 - 2026-08-05

### Mailuri: clientul afișat se deducea din adresa expeditorului, nu din atribuirea CTS

Alt tip de problemă decât la apeluri/task-uri (v0.75.0/v0.76.0) — acolo join-ul era pe cheia
greșită. Aici join-ul era corect, dar **sursa** era greșită: se prefera `emails.client_id`, care e o
deducție a MailGuard din adresa expeditorului, peste `extra.client_id` din CTS, unde un operator
uman a decis efectiv pe cine e tichetul.

Deducția pe adresă greșește când același om scrie pentru mai multe firme. Caz raportat:

| Email | Expeditor | Afișa (greșit) | Corect (CTS) |
|---|---|---|---|
| 58219, 58222 | alin.vallgarden@yahoo.com | GETA-ALIN ROUTIER S.R.L. | KOSMIN CARGO SPOLKA (IRIS 16033) |
| 58205 | soringavris78@yahoo.com | MIRESOR TRANS SRL | COPFOREST CONSTRUCT SRL |
| 58263 | cernis68@gmail.com | VERHOVETCHI AUREL | VOLOSCIUC ANATOLIE |

`alin.vallgarden@yahoo.com` e listat în `clients.emails` la GETA-ALIN ROUTIER, deci potrivirea pe
adresă „reușea" — doar că pe firma nepotrivită.

**Noua prioritate** (`_EMAIL_CLIENT_SQL`): CTS întâi, deducția locală doar ca plasă de siguranță.
`UNKNOWN CLIENT` (id local 3081) NU se tratează ca identificare — e santinela CTS pentru
„neatribuit", deci cade pe deducția locală. Fără asta, 22 de mailuri identificate corect local ar fi
devenit „necunoscut".

Rezultat pe august 2026 (1291 rânduri): 48 corectate, 22 recuperate din `UNKNOWN CLIENT`,
**0 pierdute** în necunoscut, 1243 neschimbate. Rămân 18 fără client — clientul atribuit în CTS nu
e sincronizat în `clients` local (nu e decidabil aici).

Impact zero pe cifre: din cele 48 corectate, niciunul nu intră sau iese din `productivity_exclude`,
deci numărul de mailuri măsurate și statusurile on time/overdue rămân identice. Se schimbă doar
eticheta clientului.

Pagina „Mail-uri CTS" (`cts_training.py`) folosea deja `extra.client_id` — neatinsă.

## v0.76.0 - 2026-08-05

### Task-uri: clientul afișat era greșit pe 88% din rânduri (aceeași cauză ca la apeluri)

`cts_task_ground_truth.client_id` e ID din **IRIS**, dar join-ul se făcea pe cheia primară locală
`clients.id`. Ca la apeluri (v0.75.0): numerele se suprapun, deci join-ul greșit nu dădea NULL —
returna tăcut alt client.

Amploare pe august 2026: din 2210 task-uri cu `client_id`, **1956 afișau alt client** (88%).
Exemple verificate, task-uri `solved`:

| Task | client_id CTS | Afișa (greșit) | Corect |
|---|---|---|---|
| 66090312 | 15184 | VIATRANS EMDO SRL | LUCACROSS INOVATIV SRL |
| 5224891 | 11388 | NAC SPEED LOG SRL | SABRIGIS LOGISTICS S.R.L. |
| 63991277 | 2952 | CRIS TRANS LOGISTIC SRL | TAC LOGISTICS SRL |
| 63991319 | 542 | VTR TRANS EUROPEAN S.R.L. | LI.COM S.R.L. |

După fix, pe task-urile `solved` din august care au `client_id`: **1299 din 1343 (96.7%)** se
rezolvă corect. Restul (44) au un ID care nu există în `clients`.

Notă asupra acoperirii: din 2901 task-uri `solved` în august, **1558 nu au deloc `client_id`** —
sunt task-uri generate de sistem („Device in roadtax country will be suspended", „ETOLL: device
position jump…"), nelegate de un client. Acolo nu e nimic de mapat, coloana rămâne goală corect.

`cts_tasks_training.py` folosea deja corect `iris_client_id` — divergența era doar în
`app/services/productivity.py`, care acum e aliniat pe toate cele trei canale (mailuri, apeluri,
task-uri).

### `device_operations` — verificat, NU a fost modificat

Are același tipar de join (`cl.id = d.client_id`), dar `client_id` e **NULL pe toate cele 1988 de
rânduri**, deci nu se poate determina empiric ce convenție folosește și nicio schimbare nu ar avea
efect observabil. Lăsat neatins intenționat, ca să nu se ghicească; de reverificat când tabela
primește date reale.

## v0.75.0 - 2026-08-05

### Apeluri: clientul afișat era greșit pe 90% din rânduri (join pe cheia greșită)

`cts_calls_ground_truth.raw->>'client_id'` e ID-ul din **IRIS**, dar join-ul se făcea pe cheia
primară locală `clients.id`. Numerele se suprapun, deci nu dădea NULL — nimerea peste un client
complet diferit, tăcut.

Caz verificat cu sursa BI (apel 720757): CTS trimite `client_id=11442`, care e
`TRANSEMC TRAVEL SRL` (`clients.id=11528`, `iris_client_id=11442`). Se afișa **EURO RIN SRL**,
pentru că acela are întâmplător `clients.id=11442`.

Amploare pe august 2026: din 743 de apeluri, **671 afișau alt client** (90%). Join-ul pe
`iris_client_id` prinde 741/743; cel vechi prindea 672, aproape toate greșit. După fix: **595 din
597 de rânduri (99.7%)** au clientul corect.

Restul codebase-ului folosea deja corect `iris_client_id` (`cts_tasks_training.py`, `clients.py`) —
doar `productivity.py` avea join-ul pe cheia locală. Corectat în ambele locuri: ramura `apel` și
fallback-ul de la mailuri (acolo nu producea efect vizibil — `emails.client_id` acoperă 1219 din
1240 de rânduri și fallback-ul nu se activa — dar rămânea aceeași capcană).

### Apeluri: coloana „Soluționat" arăta ora de START

`created_at` și `solved_at` primeau amândouă `p_start`, deci cele două coloane erau identice și
niciuna nu arăta închiderea reală. Payload-ul CTS conține `solved_at` corect, doar că nu era citit.

Confirmat cu BI pe apelurile din poză: `05:18:44` și `05:44:52` — exact `closed_at` din sursa de
adevăr. Textul din JSON e naiv în UTC, deci se marchează explicit ca UTC (ca la mailuri). Fallback
pe `cts_started_at` dacă `solved_at` lipsește.

### Ce NU s-a modificat (intenționat)

**Timpul măsurat rămâne `ring_seconds`** (cât sună până răspunde agentul), cu obiectivul configurat
la 4 secunde. Payload-ul mai conține două mărimi diferite — `duration_seconds` (durata convorbirii)
și `time_to_solved_seconds` (start → soluționare: 809s și 2529s pe cele două apeluri din BI). Care
dintre ele reprezintă „productivitatea pe apeluri" e o decizie de business, nedecisă încă; până
atunci calculul rămâne neschimbat. Pe cele două apeluri din poză statusul nostru (`5s > 4s` =>
overdue) coincide oricum cu BI.

**Mailurile rămân neatinse.** Rândurile rezolvate integral în afara programului (0 minute) rămân
`on_time`, conform deciziei din 03.08 — confirmat explicit 05.08 după analiza pe august: 14 rânduri
din 727 (1.9%), niciunul ascunzând o întârziere imputabilă cuiva (weekend/noapte fără personal
pontat, sau rezolvate voluntar în câteva ore).

## v0.74.0 - 2026-08-04

### Durata se măsoară pe acoperirea DEPARTAMENTULUI, nu pe tura operatorului care a rezolvat

Până acum durata se calcula pe pontajul **individual** al operatorului care a rezolvat. Dacă mailul
intra înainte ca acel operator să intre în tură, așteptarea nu se contoriza nicăieri.

Caz care a declanșat schimbarea (email_id 58516, Suport 1, NOVA TRADE GLOBAL MMM SRL): mail intrat
**03.08 09:56** local, rezolvat **13:16** de un operator cu tura 12:30–21:00. Se numărau doar
**46 min** („on time"), pentru o așteptare reală de 3h20m. Din perspectiva clientului răspunsul a
venit în peste 3 ore; raportul spunea 46 de minute.

Regula nouă: clientul așteaptă **departamentul, nu persoana**. Cât timp departamentul are cel puțin
un om prezent, timpul curge pe programul departamentului, indiferent cine rezolvă efectiv și în ce
tură e. Același mail dă acum **199.7 min → overdue**.

- **Cu program configurat** (`suport_1/2/3`, `taxe_drum`) → fereastra = programul zilei
  (ex. Suport 1: 07:00–21:00)
- **Fără program** (`contabilitate`, `recuperare_tva`) → fereastra = uniunea turelor celor prezenți
  în acea zi (de la primul început la ultimul final, din pontaj)
- **Zi cu 0 prezenți** → nu curge deloc, se trece la ziua următoare

Intervalele care depășesc finalul programului **continuă a doua zi la deschidere**: mail intrat la
20:00 și rezolvat la 09:00 dimineața = 60 min (azi, până la 21:00) + 120 min (mâine, de la 07:00)
= **180 min**. Verificat: 180.0 exact, atât în Python cât și în SQL.

Efect secundar important: durata nu mai depinde de **cine** a rezolvat. Aceleași capete de interval
dau același rezultat pentru orice operator — inclusiv pentru unul fără pontaj în ziua respectivă
(înainte, un pontaj lipsă putea da 0 minute).

**Scope: doar cele 6 departamente cerute** — `suport_1`, `suport_2`, `suport_3`, `taxe_drum`,
`contabilitate`, `recuperare_tva`. Restul (comercial, mobilitate, HR, account_management etc.)
rămân pe tura individuală, comportament neschimbat. Verificat: `comercial` dă în continuare 46.15
pe același interval de test.

Modificat în **ambele** implementări, ca mailurile și task-urile să nu răspundă diferit la aceeași
întrebare:
- `_BizCache.business_minutes` / `_dept_window` (Python) — productivitatea pe mailuri
- `business_minutes_emp()` (SQL, `migrations/20260804b_business_minutes_dept_window.sql`) —
  task-uri, device-ops, pagina de sănătate

Confirmat că cele două dau rezultate identice pe toate cazurile de test (199.73 / 180.0 / union /
departament exclus).

### Recalculare august 2026 (toate cele 6 departamente)

| Departament | Rânduri | On time | Overdue | Medie |
|---|---|---|---|---|
| suport_1 | 396 | 304 | 92 | 79.4 min |
| suport_2 | 47 | 29 | 18 | 113.3 min |
| suport_3 | 0 | 0 | 0 | — |
| taxe_drum | 30 | 23 | 7 | 271.3 min |
| contabilitate | 171 | 146 | 25 | 78.1 min |
| recuperare_tva | 83 | 14 | 69 | 422.4 min |
| **TOTAL** | **727** | **516** | **211** | **71.0% on time** |

Duratele mailurilor se calculează live din `cts_ground_truth`, deci schimbarea se aplică retroactiv
la tot istoricul, nu doar la august. Snapshot-urile lunare (target-uri: zile lucrătoare, ore
planificate, coeficienți) au fost re-fixate pentru august.

### Notă despre pontaj și fusul orar (verificat, nu presupus)

`employee_attendance.begin_time/end_time` sunt `timestamp WITHOUT time zone` stocate în **UTC**.
Suspiciunea inițială a fost că tura `09:30–18:00` e ora locală și conversia o mută greșit cu 3h —
datele au infirmat-o: pe zilele cu pontaj `09:30`, activitatea reală a acelor operatori începe la
**12:30 local**, cu **0 mailuri rezolvate înainte de 12:30 pe 408 cazuri** (Buda Alina-Mioara).
Iar tura dominantă `05:00–13:30` (1729 înregistrări) = 08:00–16:30 local, ceea ce se potrivește cu
activitatea observată (~08:00). Deci conversia UTC era corectă; problema era *care* fereastră se
folosește, nu cum se convertește.

## v0.73.0 - 2026-08-04

### Productivitate mailuri: „Creat" arăta momentul greșit, iar mailurile overdue apăreau „on time"

Data „Creat" din modalul de breakdown (și din tot calculul de productivitate) venea din
`cts_ground_truth.raw->'extra'->>'created_at'`. Acela **nu** e momentul în care mailul intră în
CTS: e momentul creării **tichetului**, adică momentul în care cineva atinge mailul. Se deplasa
înainte odată cu neglijarea mailului, deci întârzierea se ascundea singură — cu cât un mail
stătea mai mult neatins, cu atât startul se împingea și durata raportată ieșea mai mică.

Caz care a dovedit-o (email_id 54196): primit **24.07 14:42**, trimis în CTS 14:45 (3 minute
mai târziu), tichet creat **28.07 08:36** (4 zile mai târziu), rezolvat 29.07 06:55. Se raportau
~22h în loc de ~4.5 zile, iar rândul apărea **on time**. Acum: 1135 minute de program, **overdue**.

Sursa corectă e `extra.email_date`, care e egal cu `emails.received_at` **exact**, pe toate
rândurile. Alternativa `emails.sent_to_cts_at` ar fi fost semantic mai potrivită (momentul real
al trimiterii spre CTS), dar e NULL pe 2876 din 8648 de rânduri (33%) — inclusiv pe majoritatea
cazurilor problematice — deci ar fi lăsat o treime din calcul pe fallback.

Expresia de start e acum definită **o singură dată** (`_EMAIL_START_SQL` în
`app/services/productivity.py`) și refolosită în toate cele 9 locuri care o consumau prin
copy-paste: totalurile paginii, modalul rând-cu-rând, trendul lunar, statisticile per operator,
monitorul live, restanțele și histogramele pe oră. Înainte, un fix într-un singur loc ar fi făcut
modalul să contrazică pagina.

Efect colateral important: la **restanțe** (`noi_vechi`, `restante`, `peste_7z`) bug-ul acționa
exact invers de cum trebuie — vechimea unei sesizări neglijate se resetează la fiecare atingere,
deci restanțele erau **sub-raportate**. Comentariul din cod afirma greșit că `extra.created_at`
e „momentul real de sosire"; a fost corectat.

Pagina **Mail-uri CTS** nu a necesitat modificări: folosea deja `email_date`. Redundanța
observată (aceeași dată afișată diferit în două locuri) era simptomul — acum cele două se aliniază.

**Task-urile au fost verificate și NU au această problemă.** `cts_task_ground_truth.cts_created_at`
e `timestamptz` real de la CTS, cu 0 cazuri de `created_at > updated_at`, istoric coerent din 2024
și tail lung onest (583 task-uri peste 240h). Sunt evenimente create de sistem la momentul
producerii, nu tichete deschise când cineva le atinge — deci nu suferă de deplasare retroactivă.

### Excluderea non-clienților din productivitate (coloană nouă `clients.productivity_exclude`)

Entitățile care nu sunt clienți reali (sisteme de taxare rutieră, furnizori, pseudo-clienți
interni) intrau în calculul de productivitate cu status „on time"/„overdue", deși nu reprezintă
muncă de suport măsurabilă: **142 din 972 de rânduri pe august 2026**. Regula exista, dar doar
pentru **satisfacție** (`clients.satisfaction_exclude`, migrația `20260729i`) — niciun query de
productivitate nu o consulta. În plus, 6 din cele 10 entități nu erau nici măcar marcate.

Flag nou `clients.productivity_exclude`, **separat intenționat** de `satisfaction_exclude`: cele
două rapoarte răspund la întrebări diferite („e clientul mulțumit?" vs „a răspuns operatorul în
timp?"), iar cuplarea lor ar face ca orice excludere viitoare dintr-unul să dispară silențios și
din celălalt.

Excluse (match pe nume, ILIKE, ca la `20260729i` — prinde variantele viitoare fără migrație nouă):
`HU-GO%` (HU-GO ELECTRONIC TOLL SYSTEM, HU-GO TEMP), `RUPTELA%` (RUPTELA, RUPTELA UAB),
`TOLL4EUROPE%`, `LOCATOR BG%`, `ORANGE ROMANIA%`, `00-FIRMA NECUNOSCUTA%`, `HELP DESK CTS%`,
`CTS INTERNAL%` — 10 clienți azi.

### Impact numeric (august 2026, toate departamentele, limită 2h)

| | Înainte | După |
|---|---|---|
| Rânduri în calcul | 1009 | 830 |
| On time | 795 | 582 |
| % în timp | 78.8% | **70.1%** |

Cele mai afectate: Suport 1 (418→381 rânduri, 367→306 on time), Taxe drum, Contabilitate.
Procentul scade pentru că raporta prea optimist, nu pentru că performanța s-ar fi schimbat.

Migrație: `migrations/20260804_productivity_exclude_and_email_start.sql` (idempotentă, aditivă;
rollback prin `UPDATE clients SET productivity_exclude = FALSE`).

## v0.72.3 - 2026-08-04

### Bannerul de decalaj CTS afirma o cauză neverificată („fișa apelului")

Textul spunea: *„decalajul vine din CTS, de la momentul în care operatorul deschide/închide fișa
apelului"*. Două probleme:

1. **„Fișă" nu există în CTS.** Sursa `/cts/calls` returnează 24 de câmpuri (`status`,
   `category_id`, `assignee_*`, `client_id`, `ring_seconds`, …) — nicio noțiune de fișă, nimic
   despre „deschis/închis de operator". Cuvântul era inventat în interfața noastră.
2. **Cauza afirmată nu e susținută de date.** Măsurat 2026-08-04 la 10:40 local: `calltrack_id`
   (id secvențial generat de CTS) era `1329233` în `/cts/calls`, dar `1329629` în While1 — ~400 de
   apeluri cărora CTS le alocase deja un id nu erau returnate de endpoint. Cele mai noi 6 apeluri
   din centrală, căutate individual după `ctk_uniqueid` ȘI `calltrack_id`, lipseau complet.
   Deci nu e „operatorul n-a completat încă", ci apeluri care nu ajung în răspunsul sursei.

Text nou, limitat la ce e verificabil: „Cel mai nou apel primit din CTS" / „nu au ajuns încă în CTS"
/ „Apelurile care lipsesc nu sunt returnate de CTS la momentul interogării — cauza se află în amonte,
nu în Cargo360."

Cauza reală (ingestie oprită în `cts_replica.client_call_log` sau filtru în endpoint) nu e
decidabilă din Cargo360 — infra IRIS. Escaladată în outbox #58, cu interogările de verificare.
Comentariul din `_freshness()` documentează măsurătoarea, ca interpretarea greșită să nu revină.

## v0.72.2 - 2026-08-04

### Apeluri CTS fără client („—") — clientul exista, doar nu-l citeam

Raportat: rânduri în „Apeluri CTS" cu client necompletat, deși toate apelurile au client.

Cauza: `client_id` și `client_name` se citeau EXCLUSIV din `calls`, prin
`LEFT JOIN calls ON c.id = gt.call_local_id`. Pentru un apel CTS fără corespondent While1
(`call_local_id IS NULL`) join-ul dădea NULL, deci clientul apărea „—". Sursa `/cts/calls` trimite
însă `client_id` pentru FIECARE apel — îl păstram doar în `raw`, folosit exclusiv ca forward-fix pe
`calls.client_id`. Verificat: 5.514 apeluri cu `call_local_id NULL`, **toate** cu `client_id` în sursă.

Fix:
- coloană nouă `cts_calls_ground_truth.cts_client_id` (migrație `20260804_cts_calls_client_id.sql`,
  aditivă + idempotentă, cu index parțial și backfill din `raw` — 13.283 rânduri completate)
- upsert-ul persistă `cts_client_id` la fiecare sincronizare
- lista rezolvă clientul cu fallback: While1 întâi, apoi fișa CTS (`clients.iris_client_id`).
  Câmp nou în răspuns: `client_source` = `while1` | `cts` | `null`

Rezultat: din 13.283 apeluri, 13.268 au acum client afișabil. Cele 15 rămase au `client_id` în CTS,
dar clientul nu e încă în tabela `clients` (ex. 16803–16805, clienți noi) — se rezolvă la următoarea
sincronizare de clienți, nu necesită cod.

Notă despre banner-ul „CTS a rămas în urmă cu N minute": e calculat corect (`_freshness()`) și
reflectă întârzierea reală din CTS — momentul în care operatorul deschide/închide fișa. Nu e afectat
de acest fix și nu indică o problemă de sincronizare.

## v0.72.1 - 2026-08-04

### Apeluri CTS întârziate cu câteva ore — fix pe fereastra de sincronizare

Pagina „Apeluri CTS" arăta un decalaj de câteva ore (raportat: 9h), cu impresia că sincronizarea
se blochează și pornește doar din când în când. Sincronizarea rula corect, la fiecare 5 minute;
problema era fereastra de timp cerută sursei.

`updated_at` din sursa CTS e inconsecvent între două fusuri (verificat empiric 2026-08-04,
ceas UTC 06:11 / local RO 09:11):

- apel abia intrat, neatins de operator (`status='new'`) — `updated_at == started_at`, ambele **UTC**
  (ex. `cts_call_id=721493`, `started=06:10:14`, `upd=06:10:14`)
- apel atins/modificat de operator — `updated_at` rescris în ora **locală** România
  (ex. `cts_call_id=721447`, `started=04:51:23`, `upd=07:54:25`)

Adică `updated_at` e UTC la inserare și devine local la modificare. Filtrul `?since` era aliniat la
ora locală, deci pentru un apel nou cădea cu 3h *după* `updated_at`-ul lui în UTC: apelul devenea
vizibil abia când operatorul îl atingea. De aici decalajul și aparența de sincronizare agățată.

Fix: `_source_now()` ancorează în cadranul cel mai devreme (UTC), iar fereastra rolling crește de
la 24h la 72h — acoperă ambele forme de `updated_at` plus fișele atinse peste noapte sau în weekend.
Câștig de acoperire: ~51h. Cost zero — upsert idempotent pe `UNIQUE(source, cts_call_id)`, volum
~350 apeluri/zi.

### Sincronizarea „completă" aducea apeluri din 2020, nu pe cele recente

`sync_ground_truth(since=None)` nu însemna „tot": sursa livrează în ordine `updated_at ASC` și taie
la `limit`, deci fără ancoră se întorceau cele mai VECHI apeluri. Verificat: `limit=5000` fără
`since` returna până la `cts_call_id=5148`, `started 2020-04-10` — niciodată prezentul.

Fix: `since=None` se ancorează acum la `FULL_SYNC_MAX_DAYS = 400` zile în urmă.

Fără schimbări de schemă sau de interfață. Sincronizarea la 5 minute nu era afectată de acest
al doilea punct (ea trimite mereu `since`).

## v0.72.0 - 2026-08-03

### Obiectiv fără nimic de rezolvat = 100% (nu exclus din scor)

Un obiectiv cu 0 intrări pe luna respectivă (ex. „Task-uri — CargoBox", 0 task-uri) ieșea cu
`achieved = None` și era scos din media ponderată — ponderea lui de 5% se redistribuia pe restul,
deci se împărțea la 95 în loc de 100. Scorul general depindea astfel de un obiectiv gol.

Acum: 0 intrări => 100% (nu poți rata ce nu a existat). Suport 1 / august 2026: 91.85% → 92.25%,
suma ponderilor active 95 → 100.

Distincție păstrată: dacă au existat intrări dar NICIUNA măsurabilă, obiectivul rămâne `None` și
în continuare exclus — acolo nu se poate afirma nici 100%.

### Timpul 0 minute (rezolvat în afara programului) se numără ca „On time"

Decizie de business (Raul Covaci, 2026-08-03), care inversează parțial fix-ul din v0.71.0.

Când tot intervalul creare→soluționare cade în afara orelor de lucru, `business_minutes` întoarce
`0`. În v0.71.0 aceste cazuri au fost scoase din calcul ca „Nemăsurate". Acum se consideră rezolvate
**în timp**: omul a răspuns când nu era obligat să fie la lucru (weekend, noaptea, sau după ce
ieșise din tură), deci nu are sens să fie penalizat sau ignorat.

`_measurable()` acceptă din nou `0`; `None` (interval invalid — capăt lipsă sau soluționat înainte
de creare) rămâne nemăsurabil, pentru că acolo nu se știe nimic despre durată. `business_minutes`
separa deja explicit cele două cazuri.

Rândurile afectate sunt marcate în pagina de breakdown cu „în afara programului" sub eticheta de
status (`in_afara_programului` în API), altfel un „On time / 0 min" ar părea eroare de calcul.

Suport 1 / august 2026 după ambele schimbări: email 86.61% → 87.55% (20 cazuri), task 95.24% →
95.61% (9 cazuri), scor general 91.85% → **92.88%**. Nu mai există rânduri „Nemăsurate";
`total == measurable` pe toate canalele.

Exemple reale găsite la verificare: 17 mailuri închise în weekend, plus 3 închise luni 03.08 între
16:37 și 16:52 de un operator cu tura pontată 05:00–13:30 (muncă peste program — confirmat ca
normal, nu eroare de pontaj).

### Verificare task-uri și apeluri (fără modificări de cod)

**Task-uri — corect.** Pe suport_1 / august: 0 timestamp-uri inversate, 0 cazuri cu timp de lucru
mai mare decât timpul de ceas (deci programul se aplică), medie 30.0 min lucru vs 38.7 min ceas.
Limitare de notat: CTS nu trimite un `solved_at` pentru task-uri, doar `cts_updated_at` (ultima
modificare) — o editare după rezolvare ar umfla durata. Pe august: 0 astfel de cazuri.

**Apeluri — problemă de definiție, nu de calcul.** Obiectivul „4 secunde" măsoară
`cts_response_seconds` = `ring_seconds` = cât sună telefonul până răspunde agentul, NU timpul de
soluționare. Toate valorile pe suport_1 / august sunt între 0 și 5 secunde (medie 1.9s, 122 din 260
exact 0), deci indicatorul nu poate scădea sub limită — de aici 99.56%. CTS trimite și
`time_to_solved_seconds` (medie 200 minute, maxim ~24h), care e ignorat. Apelurile sunt și singurul
canal care nu trece prin `business_minutes`. Comportamentul e documentat ca intenționat în
`cts_calls_sync.py`; schimbarea lui e o decizie de business, în așteptare.

## v0.71.1 - 2026-08-03

### Fix: Monitor arăta procentul ESTIMAT, nu cel realizat (diferit de pagina Rapoarte)

`/productivity/dashboard/data` lua `obiectiv_atins` și `status` din `forecast_report()`, care e o
**estimare** — proiectează media ultimelor 2 luni complete pe luna în curs — nu realizarea efectivă.
Pagina Rapoarte folosește `department_report()` (datele reale ale lunii), deci cele două ecrane
arătau cifre diferite pentru aceeași lună și același departament:

| Departament | Monitor (înainte) | Rapoarte | Monitor (acum) |
|---|---|---|---|
| suport_1 | 88.12% | 92.67% | 92.67% |
| suport_2 | 88.71% | 96.77% | 96.77% |
| taxe_drum | 84.19% | 89.86% | 89.86% |
| contabilitate | 58.43% | 72.20% | 72.20% |
| recuperare_tva | 51.14% | 69.23% | 69.23% |

Acum `obiectiv_atins` / `status` vin din `department_report()`. Țintele și capacitatea
(`obiectiv_real`, `obiectiv_minim`, `ore_planificate`, `ore_disponibile`, `coeficient`) rămân din
`forecast_report()`, unde sunt calculate corect pe luna întreagă.

Fallback păstrat: dacă luna nu are încă date măsurabile (`obiectiv_atins is None`, ex. suport_3 sau
prima zi a lunii), cardul afișează estimarea — altfel ar rămâne gol. `/productivity/forecast`
(pagina de estimări) nu e afectat.

## v0.71.0 - 2026-08-03

### Breakdown per obiectiv de productivitate (buton „ochi")

Fiecare rând de obiectiv din pagina Productivitate are acum un buton de detalii care deschide
lista brută din spatele procentului: client, subiect, data creării, data soluționării, cine a
rezolvat, timpul de soluționare (minute de program de lucru) și statusul `On time` / `Overdue` /
`Nemăsurat`. Filtre: status, angajat, interval de dată a soluționării, căutare pe client/subiect;
paginare 100 rânduri.

`GET /api/v1/productivity/breakdown?tip=&department=&month=&categorie=&status=&user_id=&search=&date_from=&date_to=`
(admin). Suportă `tip` ∈ `email|task|apel|device_ops`.

Implementarea (`productivity.breakdown_rows()`) refolosește exact aceleași interogări, filtre și
convenții de timp ca `department_report` / `_fetch_*_rows`, inclusiv `_BizCache.business_minutes`.
Verificat pe suport_1 / august 2026: breakdown-ul dă identic cu raportul pe toate obiectivele —
email 227/210/187, task 106/97/94, apel 229/229/228.

### Fix: durata 0 minute era contorizată drept „în timp" (procent de productivitate umflat)

`business_minutes_emp` întoarce `0` când tot intervalul creare→soluționare cade în afara
programului de lucru (mail intrat noaptea sau în weekend și închis înainte de următoarea
fereastră). `_measurable()` accepta `mins >= 0`, deci 0 era considerat măsurabil și `0 <= limită`
îl marca „în timp" — deși nu s-a măsurat nimic. Contrazicea `resolution_minutes()`, care respinge
explicit intervalele `<= 0` ca degenerate, și chiar comentariul din `_accumulate`.

Fix: `_measurable()` cere `mins > 0`. Apelurile rămân neatinse (folosesc `allow_zero=True`, unde
0 secunde = răspuns instant, o măsurătoare validă). Efect pe suport_1 / august 2026: 17 din 224
mailuri ieșite din numărătoare, email 90.18% → 89.05%.

### Fix: contoarele „Noi" și „În lucru" din Monitor operațional / financiar

Cardurile numărau stări suprapuse, nu disjuncte:
- **„Noi"** număra tot ce a INTRAT azi, indiferent de starea actuală — un mail intrat și rezolvat
  în aceeași zi apărea și la „Soluționate" și la „Noi". De aici valorile umflate: 170 „noi" la
  mailuri, deși doar 1 era efectiv în starea `new` (166 erau deja `solved`); la task-uri 786 vs 2.
- **„În lucru"** la mailuri era `status NOT IN ('solved','closed')`, deci amesteca `new` cu
  `in progress`.
- **„În lucru"** la task-uri pierdea orice task deschis mai vechi de 30 de zile (fereastra
  `cts_created_at >= CURRENT_DATE - 30`), exact restanțele care contează cel mai mult.
- Mailurile nu erau filtrate pe direcție — se puteau număra și cele trimise.

Acum stările sunt disjuncte și se numără doar mailurile primite (`cts_direction = 'received'`):
`Soluționate` = închise azi · `În lucru` = `in progress` · `Noi` = `new` (task: `new` + `postponed`),
ultimele două fără limită de vechime. Volumul intrat azi rămâne expus separat ca `intrate_azi` și
alimentează indicatorul „Ritm" (rezolvat azi / intrat azi) — altfel ritmul ieșea absurd
(166 rezolvate / 1 nou).

Valori după fix (grup operațional): mailuri `noi` 7 (era 130), `în lucru` 20, `intrate_azi` 181;
task-uri `noi` 23 (era 100), `în lucru` 20, `intrate_azi` 823.

Scoaterea ferestrei de 30 de zile a scos la iveală o restanță istorică reală la Financiar: 1120
task-uri deschise (769 `new` + 351 `postponed`), din care 309 mai vechi de 30 de zile, unele din
martie. Nu e artefact de numărare — filtrul vechi pur și simplu le ascundea. Ca să rămână lizibil pe
un monitor de perete, cardul afișează acum sub cifră „din care N vechi", iar API-ul expune
`noi_vechi` (mail) și `pending_vechi` (task).

## v0.70.2 - 2026-08-03

### Securitate: suprimare false positive gitleaks pe `.env`

Scan #29 (gitleaks, 26 findings high) — toate pe `.env` live + 3 backup-uri `.env.bak-*`.
Nu e leak în cod/git history — sunt fișiere config pe disk, deja excluse din orice
versionare. Fix: `.gitleaksignore` la root, exclude `.env` și `.env.bak-*` (backup-urile
oricum eliminate progresiv, vezi v0.70.1). Nu s-a rotit niciun secret — nu era nevoie
(fișierele nu erau expuse public, doar semnalate de scanner ca zgomot).

## v0.70.1 - 2026-08-03

### Securitate: validare identificatori SQL în sincronizarea IRIS Data Views

`view_name` venea din path param (`POST /api/v1/iris-dv/views/{view_name}/sync`) și ajungea
neverificat în `DELETE FROM {tbl}` și `CREATE TABLE {tbl}` — `_local_table_name()` înlocuia doar
`-` și `.`, deci `;`, spații și ghilimele treceau. Un admin autentificat putea rula DDL/DML arbitrar
pe baza de date. Endpointul cerea `get_current_admin`, deci nu era exploatabil anonim, dar escalada
„admin de aplicație" la „control total pe DB", ceea ce nu era intenționat.

Fix: `_IDENT_RE = ^[A-Za-z0-9_.-]{1,60}$` aplicat în `_validate_view_name()`, apelat în
`_local_table_name()` și fail-fast în `trigger_sync()` (400 înainte de a porni background task-ul).
Cele trei view-uri reale (`client_contact_email_log`, `employee`, `employee_vacation_request`) trec
validarea neschimbate.

Numele de coloane din răspunsul remote IRIS DV se filtrează acum o singură dată, la citirea din
payload, ca `CREATE TABLE` și `INSERT` să rămână consistente; gardă defensivă păstrată în
`_create_local_table_if_needed()`. Coloanele respinse se logează.

### Securitate: token de feedback nu mai ajunge în loguri

`feedback_public.py` — pixelul de tracking logă tokenul întreg la eroare
(`token=%s`), deci cine citea journald putea deschide formularul în numele clientului.
Acum se logează doar prefixul de 8 caractere, suficient pentru corelare la debug.

### Notă scan semgrep #28

Din 141 findings `high`, 129 sunt false positive: regula `avoid-sqlalchemy-text` e `audit`-tier și
marchează orice `text()`, fără analiză de taint. Codul folosește consecvent fragmente SQL literale
cu valori prin bind params. Cele 14 findings SHA1 sunt tot false positive — SHA1 e folosit exclusiv
ca cheie de cache/dedup, niciodată ca semnătură; migrarea ar invalida cache-urile fără câștig.

## v0.70.0 - 2026-08-03

### Documentație de integrare pentru feed-ul de satisfacție clienți (fereastră + export PDF)

Setări → Conexiune API: buton „Documentație" lângă URL-ul feed-ului de satisfacție, care deschide
`SatisfactionApiModal` — același tipar ca „Informații integrare" al feed-ului CTS (explicații în
fereastră + export imprimabil prin dialogul de tipărire al browserului, „Salvează ca PDF").

Motivul: endpointul avea contract complet, dar nicio explicație pentru cine îl consumă. Întrebările
„cum trec la pagina următoare" și „cum filtrez după CUI" erau răspunse doar în codul sursă, unde
consumatorul extern nu se uită.

**Acoperă:** autentificarea (`X-CTS-Token`, cu Arată/Copiază pe cheia reală), URL-ul, exemplul de
răspuns adnotat câmp cu câmp, tabelul celor 8 câmpuri per client, căutarea unui client anume
(tabel „ce aveți → ce parametru folosiți"), navigarea între pagini cu pseudocod pentru parcurgerea
tuturor celor ~15.7k clienți, cei 11 parametri, codurile de răspuns (200/400/401/429) și pașii
concreți în Postman.

**Două capcane documentate explicit**, fiindcă ambele produc date greșite în silence:
- clienții fără scor apar cu `client_satisfactie: 100` — se deosebesc prin `are_scor_calculat:false`,
  altfel sunt citiți ca „evaluați și perfecți";
- `q` cu valoare numerică poate întoarce MAI MULȚI clienți (e încercată ca CUI, ID intern și ID
  IRIS); pentru un singur client se folosește parametrul dedicat.

**Design** — butonul folosește `Icon id="doc"` (line-style, `stroke="currentColor"`), nu emoji, iar
blocurile de cod au `color: var(--t2)` în loc de hex brut, ca să rămână lizibile pe tema deschisă.
Lint universal: 219 hex / 73 emoji = exact cifrele de dinaintea acestei livrări, zero regresie
introdusă. (Baseline-ul fișierului rămâne nescris — `.claude/` e read-only pentru agent.)

Doar interfață; endpointul și contractul răspunsului sunt neatinse.


## v0.69.3 - 2026-08-03

### Setări → Conexiune API: scos rândul redundant de autentificare pentru satisfacție

Cardul de conexiune afișa două rânduri de autentificare („Autentificare (header): X-CTS-Token" și
„Autentificare satisfacție (header): ..."), deși de la v0.69.2 ambele feed-uri folosesc aceeași
cheie și același header. Al doilea rând nu adăuga informație — sugera că există două mecanisme
de configurat, exact confuzia care a produs 401-ul din v0.69.2.

Rămâne un singur rând de autentificare, plus URL-ul feed-ului de satisfacție. Nota de sub cheie
preia informația explicit („aceeași cheie și același header ca feed-ul de emailuri"), ca ștergerea
rândului să nu lase golul în care header-ul necesar nu mai apare nicăieri în interfață.

Doar interfață. Endpointul acceptă în continuare ambele headere (`X-CTS-Token` și `X-API-Key`) —
contractul nu s-a schimbat, doar afișarea.


## v0.69.2 - 2026-08-03

### Feed satisfacție: acceptă și cheia CTS (`X-CTS-Token`), nu doar `X-API-Key`

`GET /api/v1/ext/clients/satisfaction` întorcea `401 X-API-Key lipsă` pentru cine folosea cheia
afișată în Setări → Conexiune API. Cauză: acolo e afișată cheia **CTS**, iar endpointul cerea o
cheie din tabelul `api_keys` — două mecanisme separate, o singură cheie vizibilă în interfață.
Nicio cheie utilizabilă nu exista în `api_keys` (doar `healthcheck-monitor`, a cărei valoare brută
nu e recuperabilă — se stochează doar hash-ul).

**Fix** (`_verify_any_key`): endpointul acceptă ORICARE dintre cele două headere. `X-API-Key` are
prioritate dacă e prezent; altfel `X-CTS-Token` e comparat cu `settings.cts_feed_api_key` prin
`hmac.compare_digest`. Cheia CTS intră în ACEEAȘI fereastră de rate limit (60 req/min), sub o
identitate proprie derivată prin SHA-256 — altfel ar fi ocolit complet limita.
Mesajul de 401 numește acum ambele variante acceptate.

**Compromis asumat** (decizie Raul Covaci): cheia CTS nu mai poate fi revocată independent de
accesul la datele de satisfacție — rotirea ei rupe simultan feed-ul de emailuri ȘI acest endpoint.
Documentat în docstring, cu instrucțiunea de a emite o cheie dedicată în `api_keys` și de a scoate
ramura CTS dacă e nevoie de revocare separată. Restul endpointului nu depinde de această alegere.

**UI** — cauza reală a confuziei era aranjarea: cele două rânduri de satisfacție stăteau imediat
deasupra câmpului „Cheie (X-CTS-Token)", sugerând că acea cheie le acoperă pe ambele. Acum eticheta
spune explicit `X-CTS-Token (cheia de mai jos) sau X-API-Key`, plus o notă sub cheie că aceeași
valoare deschide și feed-ul de satisfacție.

Nicio schimbare de schemă DB. Contractul răspunsului neatins.


## v0.69.1 - 2026-08-03

### Fix: atașamentele din feed-ul CTS trimit din nou `id_mailguard`

`app/api/v1/cts.py` — `_build_attachment` trimitea ID-ul intern al atașamentului DOAR ca
`id_cargo360`. Numele istoric, pe care CTS îl citește, e `id_mailguard`; redenumirea din
31.07.2026 (rebranding Cargo360) a rupt corelarea atașamentelor în producție.

**De ce a trecut neobservat:** câmpul nu a dispărut, doar și-a schimbat numele. CTS citea o cheie
inexistentă, primea `None` și continua fără eroare — nici 500, nici linie în log. O redenumire de
câmp într-un contract extern nu produce niciun semnal; se vede doar în consumator.

**Fix:** ambele nume, aceeași valoare. `id_mailguard` e contractul principal, `id_cargo360` rămâne
alias. O simplă redenumire inversă ar fi rupt simetric orice consumator adaptat între timp la
`id_cargo360` — de aceea alias, nu înlocuire. Comentariu în cod care cere verificarea consumatorilor
înainte de a scoate vreunul.

Restul obiectului de atașament e neatins (`id` = Graph attachment id, `name`, `contentType`, `size`,
`isInline`, `contentId`, `contentBytes`). Nicio schimbare de schemă DB.

**UI** — Setări → Conexiune API: exemplul JSON și textul de integrare arată acum `id_mailguard` ca
câmp de citit, cu `id_cargo360` marcat drept alias.


## v0.69.0 - 2026-08-03

### Feed extern: satisfacție clienți grupată pe client (medie + istoric lunar)

`GET /api/v1/ext/clients/satisfaction` — endpoint nou, pentru aplicații externe care au nevoie de
gradul de satisfacție per client, cu istoric. Auth `X-API-Key` (tabelul `api_keys`), rate limit
60 req/min per cheie — reutilizate din `satisfaction_api.py`, fără duplicare.

Rămâne SEPARAT de `/ext/v1/satisfaction`, care nu se modifică: acela întoarce rânduri plate
(client × lună), fără nume/CUI și fără medie. Consumatorii lui nu sunt afectați.

**Formă răspuns** — un obiect per client:
`id_client`, `iris_id_client`, `client_nume`, `client_cui`, `client_satisfactie` (media generală),
`are_scor_calculat`, `luni_calculate`, `istoric_satisfactie` (`{"2026-07": 60.7, ...}`).

**Reguli de business** (decise cu Raul Covaci, 2026-08-03):
- Se întorc TOȚI clienții activi (~15.7k), nu doar cei cu snapshot — aplicația externă pornește de
  la lista noastră de clienți, deci un client lipsă din răspuns ar fi indistinct de „client șters".
- Client fără NICIUN snapshot → `client_satisfactie: 100.0`, istoric gol, `are_scor_calculat: false`
  („fără semnal negativ = client mulțumit").
- Media se calculează DOAR peste lunile cu scor real; lunile fără snapshot NU sunt completate cu
  100. Altfel o lună slabă ar fi diluată de luni inexistente, iar media ar crește pe măsură ce
  trece timpul fără să se schimbe nimic în realitate.
- `satisfaction_exclude` (parteneri/furnizori) excluși implicit; `include_exclusi=true` îi readuce.

**Căutare / paginare** — `limit` 1–1000 (implicit 100), `offset`, `total`, `has_more`.
`q` = căutare liberă care acceptă nume parțial SAU identificator numeric: un termen numeric e
încercat simultan ca `cui`, `id` intern și `iris_client_id`, deci consumatorul nu e obligat să știe
ce tip de identificator are în mână. CUI-ul se normalizează la cifre (`RO 12345678` = `12345678`).
Filtre exacte separate: `client_id`, `iris_client_id`, `cui`. Interval istoric: `from_month`/`to_month`.
`doar_cu_scor=true` restrânge la clienții cu cel puțin o lună calculată. `format_luni=nume` schimbă
cheile istoricului în `iulie 2026` (implicit `2026-07`, sortabil).

**Optimizare** — agregarea lunară se face într-un CTE (`page` → `hist`) limitat la clienții din
pagina curentă, nu pe tot tabelul: costul nu crește cu numărul total de clienți. Fără acest CTE,
`jsonb_object_agg` ar rula peste toate snapshot-urile înainte de `LIMIT`.

`migrations/20260803_client_satisfaction_feed_indexes.sql` — indexuri noi, aditive și idempotente:
`pg_trgm` + GIN pe `clients.name` (ILIKE `%text%` nu poate folosi btree), `(name, id)` pentru
ordonare stabilă la paginare, expresie pe CUI normalizat, și index acoperitor
`(client_id, month_key) INCLUDE (satisfaction_pct, ...)` pentru agregare. Nicio schemă modificată.

**UI** — Setări → Conexiune API afișează noul URL și header-ul de autentificare, lângă feed-ul CTS.


## v0.68.8 - 2026-08-03

### Monitor: badge cu iconiță per categorie + spațiere distribuită

Titlurile categoriilor din `MonitorDeptCard` (Mail-uri / Task-uri / Apeluri / Reclamații) devin
badge-uri: iconiță + nume, pe fundal `color-mix` derivat din accentul categoriei, cu bară de accent
în stânga. Iconițele vin din `MonitorIcon` existent (`mail`, `task`, `call`, `alert`) — line-style,
`stroke="currentColor"`, deci fără emoji și adaptate la temă, conform regulilor de design.

Accentul colorează DOAR badge-ul (mail = ambră, task = albastru, apel = verde, reclamații = roșu).
Barele rămân colorate pe **stare** (soluționat verde / în lucru galben / nou albastru), ca lectura
să nu devină ambiguă: culoarea unei bare înseamnă mereu același lucru, indiferent de categorie.

**Spațiere:** containerul barelor primește `justifyContent: 'space-between'`, deci spațiul liber se
distribuie ÎNTRE cele 4 categorii în loc să se adune la finalul cardului. `gap: 10` rămâne ca prag
minim pentru cardurile scunde, unde nu există spațiu de distribuit. Eliminat `overflow: 'hidden'` de
pe acest container — cu `space-between` ar fi tăiat conținutul în loc să lase părintele (care are
deja `overflow: 'auto'`) să deruleze.

## v0.68.7 - 2026-08-03

### Migrație: corecțiile de stare din v0.67.1 / v0.68.6 se propagă acum pe producție

`migrations/20260803_productivity_dup_guard_and_snapshot_reset.sql`

Fixurile de cod nu erau suficiente: două dintre ele depind de STAREA din DB, iar release-ul duce pe
prod doar ce se află în `migrations/`. Comenzile rulate manual pe staging n-ar fi ajuns acolo.

**1) Gard anti-duplicat pentru raportul lunar.** Seed pe `settings`:
`productivity.last_monthly_sent` (luna curentă) și `productivity.last_monthly_sent_at` (acum).
Fără ele, prima rulare de cron după release ar retrimite raportul o dată — cheia lipsește pe prod
exact ca pe staging, fiindcă bug-ul `audit_log(user_id)` împiedicase scrierea ei.
`ON CONFLICT DO NOTHING`: o stare mai nouă decât migrația nu se suprascrie.

**2) Reset snapshot luna în curs.** `DELETE FROM productivity_monthly_snapshot` pentru luna
curentă, ca targetele să se regenereze din logica reparată în v0.67.1 (angajații în concediu la
începutul lunii ieșeau complet din calcul). Snapshot-ul e imutabil prin design, deci codul nou nu
l-ar rescrie singur. Lunile încheiate NU sunt atinse — acolo cifrele sunt deja raportate.

**Idempotență** — `migrate.sh` e `ExecStartPre`, deci rulează la fiecare restart:
- `_release_migrations` sare peste fișierele deja aplicate;
- în plus, gardă proprie `productivity.snapshot_reset_20260803` în `settings`: dacă fișierul ar fi
  reaplicat într-o lună ulterioară, un DELETE necondiționat ar șterge targetele valide ale acelei
  luni. A doua rulare raportează „deja aplicat, sar peste".

Testat pe staging: rulare directă → exit 0; a doua rulare → sare peste, cheile originale intacte;
restart → `migrate.sh` raportează „aplicate: 1, deja prezente: 114", serviciul pornește curat.

Nicio schimbare de schemă (fără tabele/coloane/indexuri noi) — doar date în `settings` și curățare
în `productivity_monthly_snapshot`.

## v0.68.6 - 2026-08-03

### Fix critic: raportul lunar de productivitate se trimitea repetat (5 emailuri duplicate)

**Lanțul cauzal.** `INSERT INTO audit_log(action, user_id, ...)` — coloana se numește **`actor`**,
nu `user_id` (`\d audit_log`). Inserția arunca `psycopg2.errors.SyntaxError`, care lăsa tranzacția
**abortată**. `_mark_sent(db)` rula imediat după, pe aceeași sesiune, și eșua în cascadă — deci
KV-ul `productivity.last_monthly_sent` nu se scria **niciodată**. Gating-ul din
`send_monthly_reports_if_due` nu avea ce citi, iar cron-ul (`process_now`, la 5 min) retrimitea
raportul la fiecare rulare. Confirmat pe staging: emailurile plecau („productivity report sent"),
dar `audit_log` era gol (0 rânduri) și cheia lipsea din `settings`.

**Fix:**
- `user_id` → `actor` în inserția de audit.
- `db.rollback()` explicit în `except` (audit ȘI grup) — o eroare secundară nu mai poate lăsa
  sesiunea abortată și bloca singura protecție anti-duplicat.
- `_mark_sent` apelat doar dacă `sent > 0`, dar necondiționat de eșecul unui grup: altfel cron-ul
  reia la 5 min și inundă destinatarii grupurilor care AU reușit.
- **Nou `productivity.last_monthly_sent_at`** (timestamp) + `_recently_sent(min_days=25)` ca a
  treia poartă în `send_monthly_reports_if_due`. Plasă de siguranță independentă de eticheta de
  lună: chiar dacă aceasta lipsește sau e coruptă, momentul ultimei trimiteri oprește retrimiterea.
- `logger.warning(..., exc_info=True)` pe audit — eroarea era înghițită fără urmă în log.

**Acțiune pe staging:** `productivity.last_monthly_sent = "2026-08"` și `last_monthly_sent_at = now()`
scrise manual, ca să opreasca pe loc al 6-lea email înainte de următorul ciclu de cron. Verificat
după restart: 0 erori, 0 trimiteri.

Restul inserțiilor în `audit_log` din codebase (auth, spam, emails, settings) foloseau deja `actor`
— bug-ul era izolat în `productivity_notifier`.

### Monitor: gauge de la 82% la 90%

`radiusScale` 0.82 → 0.90 în `MonitorDeptCard` — la 0.82 eticheta se vedea întreagă, dar gauge-ul
era prea mic.

## v0.68.5 - 2026-08-03

### Fix real: eticheta de prag tăiată sus pe gauge-urile din cardurile de monitor

Încercările din v0.68.3/v0.68.4 (padding + `minHeight` pe containerul exterior) nu au rezolvat
nimic, fiindcă atacau locul greșit. Cauza reală, din `gauge.min.js`:

1. Raza arcului se calculează din ÎNĂLȚIMEA canvas-ului: `radius = availableHeight - lineWidth/2`,
   unde `availableHeight = canvas.height * (1 - paddingTop - paddingBottom)` și `paddingTop` e
   implicit **0.1** (10% rezervă).
2. `renderStaticLabels` desenează eticheta **rotită**, la raza arcului:
   `ctx.rotate(angle); ctx.fillText(txt, 0, -s - lineWidth/2)` — adică ÎN AFARA arcului.

Când un prag cade aproape de vârful arcului (~40–60% din scală; Suport 3 are minim 44.8%), acel
punct depășește rezerva de 10% și textul e tăiat de marginea **canvas-ului**. Padding sau height pe
containerul exterior nu pot ajuta: tăierea se produce în interiorul canvas-ului.

**Fix.** `ProdGauge` primește două prop-uri OPȚIONALE, `radiusScale` și `height`. Micșorarea arcului
(`radiusScale`) e singura pârghie care eliberează spațiu pentru etichete în interiorul canvas-ului.
`MonitorDeptCard` trimite `radiusScale: 0.82, height: 210` (înălțimea compensează arcul mai mic).

Pagina Rapoarte nu trimite niciun prop nou → primește implicit `1.0` / `200px`, deci rămâne
pixel-identică. Ambele prop-uri adăugate în lista de dependențe a `useEffect`, altfel gauge-ul nu
s-ar recrea la schimbarea lor.

## v0.68.4 - 2026-08-03

### Monitor: reclamații cu „Deschise", header card mai mare, gauge netăiat

**Reclamații — „0 primite / 1 rezolvată" NU e un bug.** Semnalat pe Suport 2, 03.08. Cauza: cele
două contoare sunt seturi diferite, nu un flux. Reclamația (email `66619023`) a fost primită
01.08 21:33 și rezolvată 03.08 06:30 — deci `primite_azi=0` (n-a sosit azi) și `rezolvate_azi=1`
(s-a închis azi), ambele corecte. Regula „primite ≥ rezolvate" ar ține doar dacă totul s-ar
rezolva în ziua sosirii, ce nu se întâmplă la reclamații.

- Adăugat `reclamatii.deschise` în `per_dept` + a treia bară **„Deschise"** (galben) — ancora care
  dă sens celorlalte două. A scos la iveală 10 reclamații deschise pe Taxe de drum, informație
  care nu se vedea nicăieni.
- `primite_azi` are acum fallback pe `emails.received_at` când `raw->extra->created_at` lipsește
  (nu schimbă cifrele curente — toate rândurile aveau valoarea — dar previne subraportarea).

**UI:**
- Header card (Suport 1/2/3, Taxe de drum): 11 → 15px, `fontWeight` 800, padding mărit.
- Eliminat rândul text „minim X% · țintă Y%" de sub gauge (pragurile se citesc de pe arc).
- Gauge: wrapper 240 → 250px, `paddingTop` 10 → 22, `minHeight: 232` — la un prag jos (Suport 3:
  44.8%) eticheta cădea sus pe arc și intra sub marginea cardului, fiindcă containerul intern al
  lui `ProdGauge` e fix la 200px cu `overflow:hidden`.

## v0.68.3 - 2026-08-03

### Monitor: gauge netăiat, tipografie, rezumat per departament

**Gauge tăiat sus — cauză.** `ProdGauge` are wrapper propriu cu `height:200; overflow:hidden`.
Canvas-ul se scalează după lățime, dar containerul rămâne fix la 200px: într-un card îngust
eticheta de prag desenată în partea de sus a arcului (un prag de ~40–60% cade exact acolo) intra
sub marginea cardului. Rezolvat în `MonitorDeptCard`, nu în `ProdGauge` — acesta e folosit și de
pagina Rapoarte: lățime 190 → 240px + `paddingTop: 10`.

**Tipografie.** Etichete de stare 11.5 → 13.5px, `fontWeight` 600, lățime coloană 76 → 92px.
Capitalizare: „soluționate/în lucru/noi" → „Soluționate/În lucru/Noi" (idem „Total azi",
„Primite azi", „Rezolvate azi"). Titlurile de categorie (Mail-uri / Task-uri / Apeluri /
Reclamații) primesc `paddingTop: 8`.

**Rezumat nou, sub bare** — umple spațiul gol rămas, fără cereri suplimentare (totul derivat din
datele deja aduse): `Soluționat azi` (mail + task + apel), `Deschis acum` (mail în lucru + task în
progres), `Rezolvat / intrat` (%).

Ultimul e plafonat la 999% și verde la orice ≥100%: raportul brut explodează când se lichidează
restanțe din zilele trecute (Suport 3 a ieșit 1400% pe 03.08, Taxe de drum 463%), iar mesajul util
pe un monitor de perete e „ține pasul / nu ține pasul", nu cifra exactă.

## v0.68.2 - 2026-08-03

### Monitor Productivitate: volumul per departament devine conținutul principal

**Eliminate** (redundante față de cardurile per departament introduse în v0.68.0):
- cardul „Obiectiv lunar — realizat vs țintă · ziua N/21" (`MonitorGaugeKm` × departament) — același
  gauge există acum în fiecare card de departament;
- rândul de canale `MonitorChannelCard` (Mail / Apel / Task / Device ops) — arăta totaluri de GRUP,
  nu volum per departament.

Layout-ul rămâne: contoare KPI → carduri per departament. Componentele `MonitorGaugeKm` și
`MonitorChannelCard` rămân definite, dar nefolosite.

**Bare per card — culoare pe STARE, nu pe categorie.** Soluționate = verde (`--gn`), în lucru =
galben (`--yw`), noi = albastru (`--bl`). Aceeași stare are aceeași culoare peste toate categoriile.
La Reclamații: primite = albastru (intrare), rezolvate = verde.

**Lizibilitate:** cifre 11 → 19px (colorate în culoarea stării), etichete 9.5 → 11.5px, titluri de
categorie 9.5 → 12px, înălțime bară 9 → 14px, spațiere între categorii 5 → 12px. Gauge redus la
190px ca barele să domine cardul.

**Bara la valoare 0** rămâne golă (fără ciot colorat), dar cifra `0` e afișată mereu în gri — un
departament fără apeluri azi trebuie să arate `0`, nu spațiu gol. (Suport 2 și Suport 3 au real 0
apeluri azi; verificat în DB — nu e defect de calcul.)

## v0.68.0 - 2026-08-03

### Monitor Productivitate (Operațional / Financiar): carduri per departament + rearanjare

**Date — `per_dept` extins în `/api/v1/productivity/monitor/live`.** Conținea doar emailuri
(`rezolvate_azi`, `in_lucru`), deci un card per departament era imposibil de construit fără să
repete totalul grupului. Adăugat aditiv, per departament:

- `emailuri`: rezolvate_azi / in_lucru / noi_azi
- `taskuri`: rezolvate_azi / in_progress / noi_azi
- `apeluri`: azi / rezolvate_azi
- `reclamatii`: primite_azi / rezolvate_azi (categorie `reclamatie` din `coalesce(cts_category, ai_category)`)

Fiecare interogare refolosește EXACT join-urile, filtrele și convențiile de timp ale agregatelor de
grup, deci suma cardurilor dă totalul din contoarele de sus. Verificat pe staging: operațional
mail 98 = 98, task 418 = 418, apel 22 = 22. Cheile vechi (`rezolvate_azi`, `in_lucru`) păstrate la
rădăcina fiecărui element pentru compatibilitate.

**UI — `MonitorDeptCard`.** Un card per departament (4 pe rând pe Operațional, 3 pe Financiar):

- Gauge-ul din pagina Rapoarte (`ProdGauge` / gauge.js) — același stil, aceleași markere de prag
  pe arc — plus `minim %` și `țintă %` scrise numeric dedesubt.
- Bare ORIZONTALE cu volumul de azi: Mail 3 bare (soluționate / în lucru / noi), Task 3 bare
  (idem), Apeluri 1 bară (total azi), Reclamații 2 bare (primite / rezolvate azi). Scala e comună
  pe card (maximul local), altfel o categorie cu volum mare ar strivi restul.

**Layout.** „Obiectiv lunar — realizat vs țintă" mutat imediat sub cardurile KPI și micșorat
(`flex` 1.55 → 0.85) — era cea mai mare felie din pagină. Ordinea: KPI → obiectiv lunar → carduri
per departament → canale live.

## v0.67.3 - 2026-08-03

### Gauge Rapoarte: doar pragul minim scris pe arc, mărime 20px

Continuare la v0.67.2. La o mărime lizibilă cele două etichete de prag se suprapuneau și ieșeau din
grafic — minimul și realul sunt adesea foarte apropiate (ex. Suport 1: 77.9 / 82.9).

- `staticLabels.font`: `34px` → `20px` (tot ~2× cât se vedea înainte de v0.67.2, când fontul invalid
  era ignorat și se desena implicitul ~10px).
- `staticLabels.labels`: `[safeMin, safeMax]` → `[safeMin]` — pe arc rămâne scris doar pragul minim.

Neatinse: markerele colorate de pe arc (galben = minim, verde = real), culoarea gauge-ului, acul,
gradațiile, badge-ul cu valoarea și textul de status de sub grafic. Regenerat `mg-app.js.gz`.

## v0.67.2 - 2026-08-03

### Fix: etichetele de prag (minim/real) de pe gauge-urile din Productivitate → Rapoarte erau ~10px

Pragurile scrise direct pe grafic (ex. Suport 1: 77.9 / 82.9) apăreau mult mai mici decât cei 28px
configurați în `ProdGauge` (`app/ui/vendor/mg-app.js`).

**Cauză.** `gauge.js` parsează mărimea fontului din `staticLabels.font` cu `/\d+\.?\d?/`, apoi
reconstruiește șirul ca `parseFloat(r)*displayScale + n.slice(r.length)`. `slice` taie de la
**începutul** șirului, nu de după cifre — cu `'bold 28px Inter,...'` (`r.length === 2`) rezulta
fontul invalid `'28ld 28px Inter,...'`, pe care canvas-ul ignoră, păstrând implicitul ~10px.
Creșterea numărului nu avea deci niciun efect: mărimea trebuie să fie primul token din șir.

**Fix.** `staticLabels.font` devine `'34px Inter,system-ui,sans-serif'` — fără prefix, deci mărimea
se aplică efectiv. `bold` a fost eliminat intenționat (biblioteca nu îl suportă în acest câmp);
îngroșarea ar necesita desenarea etichetelor separat peste canvas.

Doar acest câmp a fost modificat — restul gauge-ului (zone, pointer, ticks) și etichetele de SUB
grafic sunt neatinse. Regenerat `mg-app.js.gz` după modificare.

## v0.67.1 - 2026-08-03

### Fix: angajații în concediu la începutul lunii dispăreau din Productivitate → Rapoarte

**Simptom.** Pe 03.08.2026, la Suport 1 lipseau Negrescu Elena și la Suport 2 Kovacs Robert — atât
din tabelul Operatori, cât și din Ore planificate / Ore disponibile. Suport 1 arăta 840h (5 oameni)
în loc de 1008h (6 × 21 × 8), Suport 2 arăta 672h (4 oameni) în loc de 840h (5 × 21 × 8).

**Cauză 1 — filtru de activitate greșit.** `department_report` considera „operator activ în lună"
doar pe cine avea cel puțin o zi `present=true` în pontaj. Pe 3 august pontajul avea doar 2 zile
(01–03.08), iar cei doi erau exact atunci în concediu (Negrescu 31.07–03.08, Kovacs 03.08–14.08),
deci cu 0 zile prezente. Un om în concediu nu e inactiv: concediul trebuie scăzut din orele
disponibile, nu să-l elimine din orele planificate și din listă.

**Cauză 2 — snapshot fixat prea devreme.** Snapshot-ul lunar e imutabil prin design. Fiind salvat
în dimineața zilei 3, a înghețat cifrele calculate pe pontaj incomplet, așa că eroarea persista
tot restul lunii chiar după ce pontajul se completa.

**Fix.**
- Operator activ = pontaj prezent **SAU** absență în pontaj **SAU** concediu înregistrat în lună
  (`employee_schedule` + fallback `cts_dv_employee_vacation_request` status 1/2) **SAU** zile de
  lucru pe proiecte/refurbished declarate pe lună. Rămân excluși doar cei fără nicio urmă în lună
  (nemapați în pontaj, angajați cu `productivity_start_date` în viitor).
- `_snapshot_too_early()`: snapshot-ul lunii în curs nu se mai fixează până nu au trecut cel puțin
  5 zile lucrătoare (`_SNAPSHOT_MIN_ELAPSED_WD`). Sub prag estimarea se recalculează LIVE la fiecare
  accesare, exact ca o lună viitoare, deci reflectă imediat concediile și pontajul care intră pe
  parcurs. Aplicat în ambele ramuri: `department_report` și `forecast_report`.
- Snapshot-urile august 2026 pentru suport_1/2/3 au fost șterse și recalculate.

## v0.67.0 - 2026-07-31

### Rebranding: MailGuard → Cargo360
Numele produsului se schimbă în **Cargo360**. Înlocuit în tot ce e vizibil pentru utilizator și în
documentație: 348 de înlocuiri în 84 de fișiere (UI, titlu pagină, cod, `CHANGELOG.md`, `CLAUDE.md`,
`docs/`, migrații, scripturi).

- **UI**: badge-ul din meniul lateral `MG` → **`C360`**, titlul `MailGuard` → `Cargo360`, iniţialele
  din favicon `MG` → `C360` (font redus la 10px ca 4 caractere să încapă în 36px). Versiunea afișată
  în meniu era hardcodată la `v0.13.1` — corectată la versiunea reală.
- **Nume serviciu**: `NordLogistics MailGuard` → `NordLogistics Cargo360`, în `app/config.py` **și** în
  `APP_NAME` din `.env` (variabila de mediu suprascrie valoarea din cod — altfel `/api/v1/health` ar fi
  continuat să raporteze numele vechi). `/healthz` întoarce acum `cargo360-v0.67.0`.

### Identificatori de infrastructură PĂSTRAȚI intenționat
Nu sunt „nume de produs", sunt identificatori reali de care depinde funcționarea. Redenumirea lor ar
opri aplicația, nu ar rebranda-o:

| Păstrat | De ce |
|---|---|
| `IRIS_MAILGUARD_API_KEY`, `X-Mailguard-Key` | Cheia + header-ul validate de IRIS Gateway. Redenumite = acces pierdut la CTS (ground-truth, apeluri, task-uri, angajați) |
| `db_name`/`db_user` = `mailguard` | Baza de date și utilizatorul există fizic cu acest nume |
| `mailguard-api`, `mailguard-db`, `mailguard-cron` | Servicii systemd / containere Docker existente |
| `/opt/iris-mailguard`, alias SSH `mailguard-staging` | Căi și acces pe server |
| `MAILGUARD_ENV`, `MAILGUARD_NATIVE_INGEST`, `MAILGUARD_SIDE_EFFECTS` | Variabile de mediu citite din `.env` |
| `getLogger("mailguard.*")` (79 de locuri) | Nume de loggere — schimbate ar rupe filtrele existente pe jurnale |
| `mg-app.js`, `mg-badge`, clasele CSS `mg-*` | Nume de fișier și de clase; redenumirea cere modificări corelate în HTML/CSS, fără câștig vizibil |

Redenumirea acestora e o migrație de infrastructură separată (bază de date, systemd, căi, cheie IRIS),
nu parte din rebranding — de făcut deliberat, cu fereastră de indisponibilitate.

### Verificat după rebranding
- Sintaxă: `compileall` pe tot `app/` + `scripts/`, `node --check` pe UI — curate.
- Toți identificatorii de infrastructură prezenți și după (`IRIS_MAILGUARD_API_KEY` 37×,
  `X-Mailguard-Key` 30×, `/opt/iris-mailguard` 93×, loggere 79×); cheile din `.env` identice
  înainte/după (`sed` a atins doar valorile de text, nu numele variabilelor).
- **Autentificarea la IRIS funcționează**: sincronizări reale după restart — mailuri 7.174 actualizate,
  apeluri 264 (262 potrivite local). Dovada că header-ul și cheia n-au fost atinse.
- Design: zero regresii (289 hex, 165 emoji — identic înainte/după).
- Backup: `/tmp/pre_rebrand_20260731_163755.tar.gz` (19 MB) + `.env.bak-rebrand-*` pe server.

## v0.66.1 - 2026-07-31

### `_populate_iris_ids` — mapare nedeterministă care rescria zilnic (raportat de pe producție)
- **Problemă**: angajații reangajați au 2-3 fișe în `cts_dv_employee` cu ACELAȘI email și `id`
  diferit (contracte succesive). `UPDATE employee_department_mapping ... FROM cts_dv_employee` cu
  mai multe rânduri potrivite pentru același `e.id` scrie unul **arbitrar**, fără eroare. În plus
  condiția `e.iris_id != dv.id` rămâne mereu satisfăcută cât timp există fișe multiple (celelalte
  fișe „contrazic" orice valoare pusă) → **nu converge**, rescrie la fiecare rulare.
- **Reprodus pe staging** (3 rulări consecutive pe date identice, în `ROLLBACK`): `UPDATE 9`,
  `UPDATE 7`, `UPDATE 7`. O funcție corectă dă `0` la a doua. 9 angajați au contracte multiple pe
  staging, deci bug-ul era activ aici, nu doar pe prod.
- **Efect**: angajați cu concedii 2026 ajungeau pe fișa fără concedii (pe prod: Popa Andreea 9→0,
  Vlad Cosmin 5→0) — adică fix-ul din v0.64.0, reintrodus zilnic prin altă cale.
- **Fix**: `DISTINCT ON (edm.id)` garantează un rând per angajat, iar `ORDER BY` alege **contractul
  activ** (`contract_termination_date` gol) înaintea celui mai recent angajat — coloană care exista
  în `cts_dv_employee` dar nu era folosită. Match pe email, fără ID-uri hardcodate → comportament
  identic pe staging și prod, deși ID-urile fișelor diferă între medii.
- **Verificat pe staging, prin funcția reală din cod** (nu doar SQL): 3 rulări → `2, 0, 0`
  (convergență). Concedii 2026 vizibile prin mapare: **264 → 270** (+6, nimeni nu pierde). Cele 2
  mapări schimbate câștigă amândouă: Groza Tudor Nicolae 118→37 (0→1 concedii), Vid Alexandru
  32→31 (0→5). Sincronizarea reală `run_vacation_dv_sync_if_due` rulată de două ori consecutiv:
  `iris_ids_updated: 0` la ambele.
- Backup înainte de aplicare: `edm_iris_id_backup_20260731` (56 rânduri).
- Notă: filtrul nou `edm.enabled = true` nu afectează cei deja mapați — pe staging există 1 angajat
  dezactivat cu `iris_id` setat, valoarea îi rămâne, iar `sync_vacation_from_dv` filtrează oricum
  pe `enabled = true`.

## v0.66.0 - 2026-07-31

### Operațiuni dispozitive — sincronizarea rula DOAR manual (pagina rămânea înghețată)
- **Problemă raportată**: pagina „Device Operations" stă la „Ultima sincronizare 10:43, 31.07.2026",
  pe staging și pe producție.
- **Cauză**: niciunul din cele două module `device_ops` nu era în blocul de cron din
  `emails.py` (`POST /process/run-now`), deși mailurile, apelurile, task-urile CTS și pontajul erau.
  `device_ops_sync.run_recent_if_due()` exista dar nu era apelat de nimeni, iar sursa DV nu avea
  deloc o funcție de cron. Cele două date vizibile erau ultimele apăsări de buton:
  `device_ops.last_recent_sync_at` = 30 iulie 10:00, `device_ops_dv.last_recent_sync_at` =
  31 iulie 07:43 UTC (= **10:43 local**, exact ora din interfață).
- **Fix**: `device_ops_suport2_sync.run_recent_if_due()` + înregistrat în cron. Throttle 1h (nu 5 min):
  sync-ul e `TRUNCATE` + repopulare completă, cu 70.069 de rânduri citite din view — nu incremental.

### Operațiuni dispozitive — 1.805 operațiuni lipseau din cauza ordinii numelor
- **Descoperit** în timpul verificării fix-ului de mai sus: `Closed by` din view e „Robert Kovacs",
  iar `employee_department_mapping` are „Kovacs Robert". Egalitatea exactă potrivea **1 din 7**
  angajați Suport 2/3 (doar Baican Emanuel-Crinel, singurul cu aceeași ordine în ambele surse).
- Numele compuse erau scrise și parțial: „David Miclau" vs „Miclau Adrian-David", „Ovidiu Ticus" vs
  „Ticus Ovidiu Alexandru", „Robert Iova" vs „Iova Oliviu-Robert".
- **Fix**: potrivire pe mulțimea de cuvinte, cu cratima ca separator și test de incluziune (toate
  cuvintele din `Closed by` trebuie să existe în numele angajatului) în loc de egalitate. Ambiguitatea
  întoarce `None` — o operațiune pusă greșit în contul cuiva ar intra în calculul de productivitate.
- Rezultat: **1.808 operațiuni** în baza de date, față de 3. Toate cele 6 tipuri expuse de view revenite
  (instalare_noua 1061, mutare 353, interventie 162, demontare 108, calibrare 76, periferice 48).

### Gardă anti-golire pe sincronizarea de operațiuni
- Sync-ul face `TRUNCATE` înainte de repopulare, deci o regresie în filtrare nu doar aduce mai puțin —
  **șterge ce era bun**. S-a întâmplat în timpul acestei sesiuni: filtrarea a picat la 3 rânduri și
  `TRUNCATE`-ul a golit tabela (restaurată din sursă după fix).
- Dacă setul nou e sub 50% din cel existent (și existentul e ≥20 rânduri), sync-ul se abandonează cu
  `rollback`, datele rămân neatinse și mesajul spune ce să verifici. Testat cu regresie simulată:
  1808 rânduri înainte → 1808 după.

### Tipuri de operațiune nemapate — nu se mai pierd silențios
- Un `Operation Type` necunoscut era ignorat fără nicio urmă (`continue`). Acum rezultatul sync-ului
  întoarce `unmapped_types` (tipuri eligibile pe care maparea nu le cunoaște) și `missing_types`
  (tipuri cunoscute care au ajuns cu 0 rânduri), ambele și în log ca WARNING.

### „Înlocuire" NU există în sursa nouă — necesită extinderea view-ului la sursă
- **Verificat exhaustiv** (`view_device_operations`): `Operation Type` are exact **6** valori distincte
  pe toate cele 70.069 de rânduri, niciuna o înlocuire. Nu există coloană care să le distingă
  (21 `output_columns`) și nici view alternativ (`view_device_replacements` / `_replacement` /
  `_replacements` / `_operations_all` / `_ops` → toate 404). „replacement"/„înlocuire" apare doar ca
  **text liber** în `Notes`/`Description`.
- Cele **202** înlocuiri finalizate după cutoff există DOAR în sursa veche `/cts/device-operations`,
  ca `action_type='inlocuire'` cu `operation_id` prefixat `RP-` (218 din 1 iulie). `_row_id` din view
  nu are prefixe → nu se pot corela nici prin id. Corelarea pe dispozitiv a găsit 26 din 202, iar
  acelea apar în view ca Device Move / New Installation / Calibration → nici reclasificarea nu e o
  cale corectă. Confirmare independentă: `device_operations_backup_20260730` (din sursa veche) are
  122 de înlocuiri; au dispărut exact la trecerea pe view-ul nou.
- **Deci nu e reparabil din Cargo360**: view-ul `cts_views.view_device_operations` trebuie extins la
  sursă. Maparea acceptă deja în avans „Device Replacement" / „Device Replace" / „Device Exchange" /
  „Device Swap" → `inlocuire`, ca preluarea să funcționeze fără redeploy când apar. Până atunci,
  `missing_types: ['inlocuire']` apare la fiecare rulare.

## v0.65.0 - 2026-07-31

### Filtru după dată pe pagina Apeluri CTS
- `GET /cts-calls-training/list` acceptă `date_from` / `date_to` (`YYYY-MM-DD`, inclusiv la ambele
  capete); format greșit → 400, nu 500.
- Filtrarea se face pe **data apelului**, nu pe data actualizării fișei în CTS — utilizatorul caută
  „apelurile de ieri", nu „fișele modificate ieri".
- Ziua se compară pe ora `Europe/Bucharest`, cu `COALESCE(calls.started_at::date,
  (cts_started_at AT TIME ZONE 'Europe/Bucharest')::date)`. `calls.started_at` (While1, ora locală)
  e sursa preferată pentru ora apelului; `cts_started_at` (timestamptz UTC) e doar fallback pentru
  apelurile care există în CTS fără corespondent în centrală. Fără conversie, apelurile de după
  21:00 ar cădea în ziua calendaristică următoare (decalaj 3h vara).
- UI: două câmpuri de dată + scurtături „Azi" / „7 zile" / „Șterge datele" în bara de filtre.

### Indicator de prospețime — separă „Cargo360 nu a sincronizat" de „CTS nu a mai scris nimic"
- **Problemă raportată**: „ultimul apel adus din CTS e la 13:08, ar trebui la 5-10 minute";
  utilizatorii sesizau apeluri lipsă sau nesincronizate.
- **Diagnostic (verificat pe sursă, nu presupus)**: sincronizarea rula corect la fiecare 5 minute și
  aducea **100% din ce exista**. Sursa CTS se oprise: zero înregistrări cu `updated_at` după
  13:09:29, în orice fereastră interogată (24h, 48h, limită 20.000 — deci nu plafonare, nu `since`).
  Între timp centrala înregistra apeluri reale: 358 din 432 de apeluri din ultimele 2h fără fișă CTS.
  Blocajul e **în amonte**, la momentul în care operatorul deschide/închide fișa în CTS.
- `GET /cts-calls-training/stats` întoarce `freshness`: `last_sync_at`, `last_cts_call_at`,
  `last_while1_call_at`, `lag_minutes`, `while1_last_2h`, `while1_last_2h_without_cts`.
- Banner în pagină peste 45 min de decalaj (roșu peste 90), care spune explicit că Cargo360
  verifică la 5 min și aduce tot ce există. Fără el, un blocaj în amonte arată identic cu un bug
  de sincronizare — exact confuzia care a generat raportarea.

### Aliniere fus orar la interogarea sursei CTS
- `/cts/calls` compară `since` **literal** cu `updated_at`, iar `updated_at` e scris în ora locală
  România — în timp ce `started_at` din **același** payload e UTC (verificat pe apelul 720260: CTS
  `started_at`=10:08 UTC vs While1 `started_at`=13:08 local, `updated_at`=13:09).
- `sync_recent()` calcula `since` cu `utcnow()`, deci cerea o fereastră deplasată cu 3h în trecut.
  Inofensiv pentru pierderi (fereastra ieșea mai *largă*: 27h efectiv în loc de 24h), dar
  `window_hours` mințea și ar fi devenit periculos la o fereastră mai mică decât decalajul.
- Helper `_source_now()` (`Europe/Bucharest`) — fereastra de 24h e acum exact 24h. Păstrată la 24h.

### Verificat și lăsat neatins, cu motiv
- **Nepotrivirile CTS ↔ centrală nu sunt o problemă de cod**: 98,6–100% potrivire pe ultimele 12 zile
  (ultima rulare: 245/247). Cele 5.499 nepotrivite sunt istorice — 4.991 din iunie și 300 din 2020,
  perioade în care nu există **niciun** apel While1 în bază (primul e 1 iulie). Zero din ele s-ar
  potrivi acum, pe niciuna din cele două chei.
- Cele 197 din iulie: 66 fără `ctk_uniqueid`, iar din cele 131 cu cheie validă doar 3 au vreun apel
  While1 în ±90s — și acelea ambiguu (mai mulți candidați, `uniqueid` diferit). Legarea pe proximitate
  temporală ar produce atribuiri greșite într-un calcul de satisfacție/productivitate, deci **nu** a
  fost implementată: mai bine nelegat decât legat greșit (aceeași regulă ca la `match_client_by_phone`).

## v0.64.0 - 2026-07-31

### Estimarea lunilor viitoare se recalculează în timp real
- **Problemă**: snapshot-ul lunar se fixa la PRIMA accesare a unei luni, inclusiv pentru luni care
  nu începuseră. Un concediu adăugat ulterior, zile de lucru pe proiecte sau un angajat cu
  `productivity_start_date` în viitor nu se mai reflectau în ore planificate/disponibile/coeficient.
- **Fix**: helper `_is_future_month()` în `app/services/productivity.py`. Pentru lunile care nu au
  început, snapshot-ul NU se citește și NU se scrie — estimarea se recalculează din intrările
  actuale la fiecare accesare. Snapshot-ul devine imutabil din luna în curs încolo, ca un target
  deja emis să nu se schimbe retroactiv. Aplicat în ambele căi de calcul (raport și estimare).
- Șterse cele 18 snapshot-uri deja fixate pe luni viitoare (august–octombrie 2026), salvate în
  `productivity_snapshot_backup_20260731`.

### Buton „Recalculează estimarea"
- `POST /productivity/recalculate?month=YYYY-MM[&department=]` — șterge snapshot-ul lunii și îl
  regenerează. Respins cu 400 pe luni încheiate: acolo cifrele sunt deja raportate.
- Buton în bara de sus a paginii Productivitate, lângă „Exportă raport", cu confirmare și
  reîmprospătare automată a raportului. Ascuns pe lunile trecute.
- Utile mai ales pe luna ÎN CURS, unde snapshot-ul e fixat; lunile viitoare se recalculează oricum.

### Concedii — sursă unică DV, fără duplicate „ÎNVOIRE"
- **Problemă**: aceeași cerere de concediu venea pe două canale — din DV
  (`cts_dv_employee_vacation_request`, scrisă `vacation_approved`) și din payload-ul IRIS
  (`leave_requests[]`, scrisă `leave_request` → afișată „ÎNVOIRE"). 43 din 48 de intrări erau
  duplicat exact al unui concediu DV; restul 5 corespundeau unor cereri cu status DV 4
  (anulate/respinse), deci nu trebuiau să blocheze zile. Cererile neaprobate apăreau DOAR ca
  „învoire în așteptare", niciodată ca „concediu în așteptare".
- **Cauză de fond**: `iris_id` era NULL pentru TOȚI cei 55 de angajați activi, deci
  `sync_vacation_from_dv()` ieșea devreme și nu scria niciodată nimic — cele 209 concedii din bază
  erau istorice. Fără maparea asta, DV nu putea fi sursă unică.
- **Fix**: `iris_id` populat pentru toți cei 55, pe adresa de email, alegând contractul CTS cel mai
  recent la cei 9 angajați cu mai multe fișe (verificat: acolo sunt concediile curente).
  `sync_vacation_from_dv()` importă acum și `status=1` (în așteptare) pe lângă `status=2` (aprobat),
  cu `status='pending'`/`'approved'` în `employee_schedule`; statusurile 3/4 rămân excluse; limita
  `period_begin >= 2026-01-01` păstrată. `_iter_leaves()` dezactivat — concediile vin exclusiv din
  DV. 42 de concedii importate (7 în așteptare), cele 48 de `leave_request` din CTS șterse.
  Intrările manuale neatinse. Backup complet în `employee_schedule_full_backup_20260731`.
- Concediile în așteptare intră automat în calculul de productivitate: filtrul existent e pe
  `kind='vacation_approved'`, fără condiție de status.

### Extensia PostgreSQL `unaccent` instalată
- Lipsea complet din baza de staging (doar `plpgsql` era instalat). Fix-ul din v0.62.3
  (`device_ops_suport2_sync._resolve_employee_by_name`) o folosește și ar fi eșuat la prima rulare
  a sincronizării — inclusiv pe prod după release. `CREATE EXTENSION IF NOT EXISTS unaccent`.
- **De verificat pe prod înainte de release**: `SELECT extname FROM pg_extension`.

### Zilele de lucru pe proiecte nu mai apar în tabelul de concedii
- `project_work` / `refurbished` (`entry_source='manual_extra'`) apăreau în lista de concedii a
  angajatului ca „— – —" (nu au `start_date`/`end_date`, doar lună + număr de zile) și umflau
  contorul de concedii din lista de utilizatori. Excluse din ambele.
- Se scad în continuare din orele disponibile, la fel ca un concediu — comportament neschimbat.

## v0.63.1 - 2026-07-31

### Productivitate — task-urile numără doar `solved` (fără `closed`)
- Cele 4 interogări de task-uri din `app/services/productivity.py` treceau `status IN
  ('solved','closed')`. Acum doar `= 'solved'`: `closed` în CTS înseamnă „închis FĂRĂ rezolvare" și
  nu e muncă finalizată. Regula per sursă, confirmată: **task-uri → doar `solved`**,
  **device operations → doar `closed`** (acolo `closed` ESTE starea de finalizare pentru Suport 2).
- Baza de lună rămâne **data soluționării**, pe toate sursele — Productivitate măsoară munca
  finalizată în lună, nu pe cea intrată:
  - task-uri: `cts_updated_at`
  - mailuri: `cts_solved_at` (respectiv `cts_solved_seen_at`/`cts_reply_at`)
  - apeluri: `cts_started_at` — apelul se preia și se rezolvă în aceeași conversație, nu există o
    dată de soluționare separată
  - device operations: `closed_at`
- **Notă de interpretare**: pagina Task-uri filtrează pe data creării, Productivitate pe data
  soluționării — deci cifrele diferă intenționat. Pentru Pop Adelina, iulie: 171 create-și-rezolvate
  vs 195 rezolvate în lună (24 dintre ele primite în lunile anterioare). Nu e o eroare, sunt două
  întrebări diferite.

## v0.63.0 - 2026-07-31

### Task-uri — ingestie completă pentru angajații din roster (fix date lipsă)
- **Problemă**: filtrul de ingestie accepta doar 6 categorii CTS din 398 (`cts_tasks.category_allowlist`).
  Rezultat: ~56% din task-uri aruncate (`filtered_noise: 5325` din `fetched: 9519` pe iulie).
  Task-urile contabilității pe categorii nelistate nu ajungeau niciodată în bază — de-aia raportul
  Adelina Pop (≈200 solved în CTS pe iulie) nu se potrivea cu 85 afișate în Cargo360.
- **Fix**: SINGURUL criteriu de ingestie e acum „asignat unui angajat din
  `employee_department_mapping`". Filtrul pe categorii (`CATEGORY_ALLOWLIST`) eliminat complet —
  categoria CTS e irelevantă pentru productivitate. Zgomotul automat e oprit oricum de criteriul de
  assignee (alertele nu au assignee real).
- Helper nou `_get_roster_emails(db)` — citește emailurile din roster o dată per rulare.
- Eliminate: `_DEFAULT_CATEGORY_ALLOWLIST`, `CATEGORY_ALLOWLIST_KEY`, `_get_category_allowlist()`.
  `_device_family()` păstrat — folosit la clasificarea tipurilor în UI, nu la filtrare.
- `RECENT_MAX_BACKFILL_HOURS` 168 → 1440 (7 → 60 zile): fereastra de 7 zile nu putea readuce
  task-urile mai vechi respinse de vechiul filtru.
- **Efect măsurat**: `filtered_noise` 5325/9519 (56%) → 1925/72653 (2.6%). Tabelă 30.187 → 68.833
  rânduri. Verificare pe Pop Adelina, iulie: 85 → **171 solved, identic cu raportul CTS**.
  Creșteri similare la majoritatea utilizatorilor (Kovacs Robert 307→822, Tomuta Maria 821→3440).

### Migrație 20260731 — indexuri pentru filtrele Task-uri
- `ix_ctgt_cts_created_at` și `ix_ctgt_assignee_raw` pe `cts_task_ground_truth`.
- Necesare după dublarea tabelei + filtrul nou de utilizator: listă filtrată pe utilizator + lună
  a scăzut de la 62ms la 5ms. Aditive, `IF NOT EXISTS`.

### Endpoint nou `POST /cts-tasks-training/backfill`
- Re-ingestie paginată de la o dată explicită (`since=YYYY-MM-DD`), fără plafonul de 168h al
  sync-ului rolling. Necesar după schimbarea regulilor de filtrare: task-urile respinse anterior
  nu există în bază, iar fereastra de 7 zile nu le readuce pe cele mai vechi.

## v0.62.3 - 2026-07-31

### device_ops_suport2_sync — lookup DB în loc de whitelist hardcodat
- `SUPORT2_CLOSED_BY_WHITELIST` (dict cu ID-uri specifice staging) eliminat complet.
- Înlocuit cu `_resolve_employee_by_name(db_session, name)`: lookup `lower(unaccent(name))` în `employee_department_mapping` filtrat pe `department IN (suport_2, suport_3)`.
- Fix pentru prod: ID-urile angajaților diferă între staging și prod — acum se rezolvă dinamic.

### Filtre utilizator + optimizare performanță Task-uri / Device Ops / Apeluri CTS
- Dropdown „Utilizator" adăugat pe paginile Task-uri, Device Operations, Apeluri CTS.
- Task-uri: eliminat LATERAL JOIN cu regex pe `description` (penalitate 180ms, coloana IMEI nefolosită).
- `assignees` și `agents` endpoints noi pentru popularea dropdownurilor, cu rezolvare nume din `employee_department_mapping`.
- Gzip pre-comprimare `mg-app.js` (1.2MB → 270KB): `_GzipStaticFiles` servește `.gz` automat.

### call_scorer — output_type + rescore_null + fix issue_summ
- Prompturi cu `output_type='text'` returnează răspuns brut (string), nu JSON — util pentru prompturi narative.
- `score_batch(rescore_null=True)` șterge rândurile cu scor NULL înainte de re-scorare (evită duplicate blocate).
- `issue_summ`: fix pentru cazul când `issueSummarization` e string direct (nu dict) — nu mai crasha.
- API `POST /calls-analytics/rescore`: parametrul `rescore_null` expus și în request body.

### CTS Mail-uri — carduri stats răspund la filtre active
- Cardurile de statistici din pagina Mail-uri CTS (total, potrivire categorie/departament) se actualizează
  la schimbarea filtrului de perioadă sau categorie, nu mai afișează valori globale.

### Rapoarte & Statistici — grafice acuratețe AI în timp
- 2 grafice noi în secțiunea „Acuratețe AI vs CTS în timp": potrivire categorie (linie albastră) și
  potrivire departament fără Suport 1 (linie verde), cu bare galbene la schimbări de prompt.
- Graficele urmează filtrul activ al paginii: interval de zile sau perioadă personalizată (date_from/date_to).
- Backend: `/cts-training/accuracy-daily` acceptă `date_from`/`date_to` pentru interval fix; `prompt_changes` inclus în răspuns.
- Backend: `/cts-training/stats` acceptă `department`, `dept_from`, `dept_to`, `date_from`, `date_to`.

### Migrație 20260730 — fix pipeline release
- `20260730_extra_work_days.sql`: `DROP CONSTRAINT` înlocuit cu bloc `DO $$ BEGIN IF NOT EXISTS ... END $$`
  (idempotent, fără DROP — compatibil cu pipeline release care blochează operații distructive).

## v0.62.1 - 2026-07-31

### UI
- Eliminare butoane nefuncționale 7/30/60/90 zile din Rapoarte & Statistici.
- Scroll restore + highlight la întoarcere din ClientDetail pe pagina Satisfacție clienți.
- Eliminare coloana „Nr. device" din Task-uri (LEFT JOIN LATERAL scos din query backend).

### Backend
- Sync periodic Device Operations adăugat în cron (`device_ops_suport2_sync.py` + `emails.py`).
- Endpoint nou `POST /device-ops/sync-run-now` pentru declanșare manuală sync.

## v0.62.0 - 2026-07-30 (Filtre perioadă personalizată, barchart-uri, raport PDF, tipuri documente)

Pachet de modificări UI/UX cerute de Raul Covaci. Toate filtrele de perioadă folosesc două date
explicite (**de la — până la**), fără preseturi de 7/21/30 zile.

### Filtru perioadă personalizată (component nou, reutilizat)
`DateRangeFilter` + helperul `rangeQS()` în `app/ui/vendor/mg-app.js`, montat în:
- **Dashboard** — actualizează TOATE categoriile (emailuri, apeluri, task-uri, documente).
- **Rapoarte & Statistici** — pe fiecare categorie (Email-uri / Apeluri / Task-uri); perioada e
  ridicată în shell, deci se păstrează la schimbarea tabului.
- **Emailuri** — pe data de recepție; pentru o singură zi se pune aceeași dată în ambele câmpuri.
- **Task-uri**, **Device Operations** — pe data creării în CTS.
- **Apeluri → Analitice** — perioada personalizată are prioritate peste selectorul de lună.

Backend: `date_from`/`date_to` (ziua de final inclusă) pe `/stats/dashboard`, `/stats/overview`,
`/stats/daily`, `/stats/daily-category`, `/stats/calls-dashboard`, `/stats/calls-daily`,
`/stats/calls-daily-category`, `/stats/calls-overview`, `/stats/tasks-daily`,
`/stats/tasks-overview`, `/stats/document-processing`, `/emails`, `/cts-tasks-training/{list,stats}`,
`/device-ops/{list,stats}`. Fără parametri, comportamentul rămâne identic (aditiv).

### Rapoarte & Statistici
- **Grafice de evoluție: bare în loc de linii.** `MultiLineChart` randează `type: 'bar'` (bare
  grupate); numele componentei a rămas ca să nu atingem cele ~20 de locuri care o folosesc.
- **Acuratețea scoasă din UI** (Email-uri + Apeluri + per tip document). Endpoint-urile
  `/cts-training/accuracy-daily` și `/cts-calls-training/stats` rămân în backend, nefolosite aici.
  Graficul „Acuratețe per tip document" e înlocuit cu „Documente pe tip (volum)".
- **Buton „Raport PDF"** — `printReportPdf()` deschide o fereastră de print cu exact conținutul
  paginii (KPI + grafice + tabele), cu perioada și data generării în antet. Canvas-urile Chart.js
  sunt convertite în imagini (altfel ies goale la print) și se forțează varianta light.

### Utilizatori
- **Coloana „Schimb" ștearsă** din tabel (nu era folosită). Coloana DB `shift` și endpoint-ul
  rămân neatinse — s-a scos doar din interfață.
- **Filtru de utilizator**: căutare pe nume + email, insensibilă la diacritice
  („Brasovean" găsește „Brașovean"), plus filtru pe departament și „Reset filtre".

### Satisfacție clienți
- **Prompt AI corectat — motive economice/externe.** Insolvența, lipsa de bani, vânzarea firmei
  sau a camioanelor, accidentele/dauna totală, încheierea unui leasing, restructurarea NU mai scad
  scorul și nu mai marchează clientul ca nemulțumit/la risc. Regula e pusă în `satisfaction_engine.py`
  ȘI în `interaction_analyzer.py` (ambele prompturi, email + apel) — al doilea e esențial: el
  generează `mentiune_reziliere`, iar acel flag forța automat segmentul „critic" peste decizia AI.
  Excepție: dacă pe lângă motivul economic clientul reproșează explicit calitatea serviciului.
- **Buton „Înapoi la Satisfacție clienți"** în detaliul clientului, când s-a intrat cu „View" din
  pagina Satisfacție (înainte butonul ducea în lista de clienți, pierzând contextul).

### Task-uri
- **Nr. device** — coloană nouă. CTS nu expune numărul devicelui ca câmp și nu există cheie de
  legătură cu `device_operations` (verificat pe date reale: 0 potriviri pe `operation_id`), dar la
  task-urile ETOLL/carGObox apare în descriere ca IMEI de 14–17 cifre — de acolo se extrage.
  Când se regăsește în `device_operations` se afișează și numărul de înmatriculare; altfel numărul
  e marcat cu `*` (neconfirmat). Join-ul e `LEFT JOIN LATERAL … LIMIT 1`, ca IMEI-urile duplicate
  să nu multiplice rândurile (verificat: 30017 = 30017).
- **Status „closed" distinct de „solved".** În CTS `closed` = închis FĂRĂ rezolvare; badge-ul
  afișează „închis (nerezolvat)" cu tooltip explicativ, iar KPI-urile arată separat „Rezolvate
  (solved)" și „Închise nerezolvate". Înainte `closed` intra la rezolvate și umfla rata.
- **KPI-urile de sus respectă filtrele.** `/cts-tasks-training/stats` primește aceleași filtre ca
  `/list` (status/departament/tip/perioadă) prin helperul comun `_task_filters()`, deci cifrele de
  sus nu mai rămân pe cumulat când se filtrează.

### Device Operations
- Filtru de perioadă + statisticile de sus recalculate la orice filtru (helper `_ops_filters()`,
  aceeași sursă pentru listă și statistici).

### Apeluri → Analitice
- KPI-uri complete: **Nr. apeluri, IN, OUT, Total ore, Durata medie**, Răspuns (IN/OUT cu procent
  din total; Total ore = suma duratelor). `total_duration_seconds` adăugat în
  `/calls/analytics/dashboard` și `/stats/calls-dashboard`.
- Filtrarea pe departament/persoană exista deja; s-a adăugat perioada cu dată.
- KPI agenți/clienți și analiza AI pe întrebări binare (7 prompturi binare active) sunt
  funcționale pe staging și respectă filtrele — promovarea pe producție se face prin Release.

### Procesare documente — tipuri noi
Migrație `migrations/20260730_doc_types_cargobox_etoll.sql` (idempotentă, verificată prin rulare
dublă → `INSERT 0 0`), 4 tipuri fără șablon (se încarcă manual din UI):
`CUI / Extras pe contract carGObox sau ETOLL`, `Anexa 2/3/4 - contract carGObox`.
„Act de identitate" (buletin/pașaport) exista deja — neatins.
`ON CONFLICT` țintește indexul unic **parțial** real `(category, lower(name)) WHERE status='active'`
— un `ON CONFLICT (category, name)` ar fi eșuat.

### Corecții colaterale
- `VBarChart` și `MultiLineChart`: culorile de axă/grilă/tooltip vin din tokenii CSS
  (`prodCssVar`) în loc de hex hardcodat + grilă `rgba(255,255,255,.05)` invizibilă pe light.
- `VBarChart`: unitate configurabilă în tooltip — înainte scria „emailuri" și pe graficele de
  apeluri și task-uri.
- Iconițe noi line-style `currentColor`: `calendar`, `download`, `back`. Emoji scoase de pe
  butoanele atinse (Concedii & învoiri).
- Lint de design: baseline separat `.design_lint_baseline_mgapp.json` pentru `mg-app.js` (cel
  existent era pentru `index.html`, ceea ce raporta 227 de regresii false). Zero regresii noi;
  hex brut 227 → 219, emoji 74 → 73.

### Bug-uri găsite și reparate la double-check (înainte de release)
1. **Dată invalidă în query string → HTTP 500.** O valoare ca `?date_from=abc` ajungea direct în
   `CAST(... AS date)`, Postgres ridica `DataError` și endpoint-ul întorcea 500. Afecta toate cele
   12 endpoint-uri cu filtru de perioadă (plus 5 din `calls_analytics.py`, unde `date_from` exista
   de dinainte — defect preexistent). Reparat cu validare ISO (`_valid_date()` în `health.py`,
   `cts_tasks_training.py`, `device_ops.py`, `emails.py`) și `pattern=r"^\d{4}-\d{2}-\d{2}$"` pe
   parametrii din `calls_analytics.py`. Acum: 400/422 cu mesaj clar. UI-ul nu era afectat
   (`<input type="date">` trimite mereu format valid), dar un URL editat manual spărgea pagina.
2. **Rapoarte → Email-uri se putea bloca pe „Se încarcă".** Garda de randare era
   `if (!cts) return …`, iar `cts` venea din `/cts-training/stats` — endpoint folosit DOAR pentru
   KPI-urile de acuratețe, care au fost scoase. Cu `.catch` gol, un apel eșuat lăsa `cts=null`
   permanent și pagina rămânea blocată, deși toate datele necesare erau deja încărcate. Apelul a
   fost eliminat, garda mutată pe `/stats/dashboard`, cu buton „Reîncearcă" la eroare.
3. **Graficele de evoluție puteau afișa date vechi.** `MultiLineChart` avea semnătura de
   re-randare `lungime + prima zi + ultima zi`, fără valori. La schimbarea unui filtru care
   păstrează aceleași zile dar schimbă cifrele (ex. departamentul în Apeluri → Analitice),
   `useEffect` nu se re-executa și graficul rămânea pe datele anterioare. Semnătura include acum
   și valorile seriei. (Defect preexistent, devenit mai probabil cu noile filtre. `VBarChart`,
   `HBarChart` și `ProdBarChart` erau deja corecte.)

### Verificări rulate la double-check
- Sintaxă: 7 fișiere Python + bundle JS (local și cel servit de nginx).
- Sincronizare local↔remote pe toate cele 11 fișiere atinse (md5 identic).
- Cifre API vs interogare directă în DB: emailuri 5466=5466, apeluri 5804=5804, serie zilnică
  10 zile pentru interval de 10 zile, zi unică 752=752.
- Paritate listă vs statistici (helperii comuni de filtre): Task-uri 3854=3854,
  Device Ops 46=46 — KPI-urile de sus reflectă exact filtrele din tabel.
- `LEFT JOIN LATERAL` pe device: 22392=22392 cu filtre active, 30017=30017 fără — zero duplicare.
- Analiză statică: 0 referințe orfane la codul șters (`shiftSel`, `SHIFTS`, `saveShift`,
  `ctsDaily`, `ctsAsg`), 0 variabile folosite nedeclarate în 195 de funcții, 0 încălcări ale
  regulilor hook-urilor React în 16 componente.
- Coloane tabel: Utilizatori 6 header = 6 `colSpan`; Task-uri 11 header = 11 celule = 11 `colSpan`.
- Test de runtime într-un context izolat: 16 cazuri limită pe componentele noi (null, listă goală,
  status necunoscut, popup blocat) + 13 scenarii de randare pe paginile mari (loading / cu date /
  filtrat / eroare) — toate fără excepții.
- Migrație: rulată de 3 ori → `INSERT 0 0` (idempotentă), validată și în tranzacție anulată.
- Schema DB neatinsă: `document_types` tot 20 coloane, coloana `shift` intactă (0 valori setate).
- Lint design: 0 regresii (R1 0, R2 219, R5 73 — toate la baseline).
- Endpoint-uri preexistente fără parametri: toate 200. Loguri: 0 erori grave.

### Rămas de făcut / de știut
- Schimbarea prompturilor din `interaction_analyzer.py` modifică hash-ul de versiune al
  promptului → interacțiunile se re-analizează automat. Scorurile de satisfacție se recalculează
  progresiv; pentru efect imediat pe un client anume se poate apăsa butonul de estimare.
- Numărul de device apare doar la task-urile care îl conțin în descriere (~255 din 21.251 de
  task-uri de device). Pentru acoperire completă, CTS ar trebui să expună devicele ca **câmp**
  în feed-ul de task-uri — nu se poate rezolva din Cargo360.

## v0.61.0 - 2026-07-30 (Export PDF raport productivitate)

### Ce s-a adăugat
Buton **„Exportă raport"** în tabul Rapoarte al modulului Productivitate. Generează un PDF cu
exact selecția curentă: luna din navigatorul de lună, grupul de departamente (Operațional /
Financiar) și intervalul. Financiar exportă doar Contabilitate + Recuperare TVA, fără nimic din
Operațional — aceeași filtrare `FINANCIAR_DEPTS` folosită la afișare.

### Conținut PDF (per departament)
- Statistica lunii: obiectiv minim / real / atins, coeficient, zile lucrătoare,
  ore planificate, ore disponibile, badge de status, nota de măsurare.
- Tabelul de obiective: tip, limită, pondere, total, scor obținut.
- Tabelul de ponderi per operator: volum + cotizație per canal (email / task-uri / apeluri /
  operațiuni — doar canalele configurate pe departamentul respectiv) și performanța finală.
  Detaliul pe categorii al Operațiunilor apare ca rând secundar sub operator.

### Tehnic
- `app/ui/vendor/mg-app.js` — `prodExportPdf()` construiește un HTML autonom și îl deschide cu
  `window.open` + `print()`, același mecanism ca exportul de documentație API (`downloadDoc`).
  Nu apelează backendul: primește datele deja încărcate în tab, deci PDF-ul nu poate divergea
  de ecran. CSS-ul de print folosește culori absolute (documentul ajunge la imprimantă, nu în
  tema light/dark a aplicației) și `page-break-inside:avoid` per departament.
- Lună viitoare (forecast): banner „Productivitate estimată" + badge „Estimat", fără tabelele
  de operatori și fără „obiectiv atins" — consecvent cu ce afișează tabul.
- `RANGE_OPTS_LABELS` extras la nivel de modul pentru eticheta de interval din antet.

## v0.60.0 - 2026-07-30 (Zile libere extra: lucru pe proiecte / refurbished)

### Ce s-a adăugat
Secțiune nouă în modalul „Concedii" din pagina Utilizatori, sub „Adaugă concediu manual":
**zile libere extra pentru lucru pe proiecte / refurbished**. Se exprimă ca număr de zile pe
(lună, an) — ex. „3 zile în August 2026" — fără date calendaristice concrete, pentru că nu
contează *când*, doar *câte*.

Sunt zile de lucru care NU sunt suport efectiv, deci se scad din `ore_disponibile` exact
ca un concediu. Într-o lună cu 25 zile de concediu + 2 useri × 2 zile pe proiecte,
calculul de productivitate pleacă de la 29 zile libere.

### Regula de timing (identică cu concediile)
Intrările contează doar dacă sunt adăugate **înainte de începutul lunii vizate**. Selectorul de
lună oferă doar luni viitoare, iar API-ul refuză (HTTP 400) orice lună deja începută sau trecută.
Snapshot-ul lunar imutabil (`productivity_monthly_snapshot`) se fixează la prima zi lucrătoare a
lunii, când `productivity_notifier` trimite raportul lunar și apelează `forecast_report` — de acolo
încolo targetul nu se mai ajustează. O adăugare pe 16 august pentru august e respinsă.

### Imutabilitate
Nu există endpoint de UPDATE — o intrare salvată se poate doar șterge, și doar cât timp luna nu a
început. După ce luna începe, ștergerea e blocată (HTTP 400) ca să rămână trasabil ce a intrat în
snapshot; UI afișează „Fixat în snapshot" în loc de butonul Șterge.

### Plafonare
Suma (zile concediu + zile extra) per angajat e plafonată la zilele lucrătoare ale lunii, aceeași
regulă ca la concedii. `days_count` validat între 1 și numărul de zile lucrătoare ale lunii țintă.

### Tehnic
- `migrations/20260730_extra_work_days.sql` — aditiv/idempotent. Reutilizează `employee_schedule`
  cu `kind IN ('project_work','refurbished')`, `entry_source='manual_extra'`, `start_date/end_date`
  NULL. Coloane noi: `days_count`, `period_year`, `period_month`, `created_at`. Index unic parțial
  pe `(employee_id, kind, period_year, period_month)` + CHECK de integritate.
  Notă: `employee_schedule_uidx` preexistent colapsează datele NULL la `0001-01-01`, deci intrările
  extra scriu și `leave_type='YYYY-MM'` ca discriminant în cheia existentă.
- `app/api/v1/settings.py` — `GET/POST /settings/employees/{id}/extra-days`,
  `DELETE /settings/employees/{id}/extra-days/{sid}`. Admin-only, ca la concedii.
- `app/services/productivity.py` — `_extra_days_per_emp()`, folosit în `department_report` și
  `forecast_report`. Zilele extra se adună aritmetic peste union-ul de zile de concediu (nu au
  date concrete, deci nu participă la deduplicare), suma plafonată la `zile_lucratoare`.
- `app/ui/vendor/mg-app.js` — secțiune nouă în modalul Concedii: listă intrări + formular
  (tip / zile / lună), fără editare.

## v0.59.0 - 2026-07-30 (Monitor Productivitate: bare stivuite cu „încă în lucru", canale pe un rând)

### Bare stivuite: rezolvate + încă nerezolvate, pe ora sosirii
Fiecare bară arată acum două segmente: **rezolvate** (culoarea canalului, jos) și **încă în lucru**
(galben, sus), cu eticheta `22+7`. Se vede pe ce oră a rămas volum neprocesat.

Metrici noi în `/monitor/live`: `hourly[].mail_open` / `task_open` / `apel_open` / `device_open`.

**Definiție importantă — segmentul galben e raportat la ora SOSIRII, nu a rezolvării.** „La ora 10
au intrat 18, din care 1 e încă deschis." Nu este același lucru cu totalul „în lucru" din antetul
cardului, care include și restanțele din zilele anterioare: la Financiar, din 108 emailuri deschise,
doar **14 au sosit azi** — restul de 94 sunt din zile trecute și nu au oră în ziua curentă, deci nu
pot apărea pe graficul de azi. Legenda apare doar când chiar există volum nerezolvat.

### Canalele pe un singur rând
Grid-ul 2×2 devine un rând unic: 4 coloane pe Operațional, 3 pe Financiar (unde device ops lipsește).
Rândul primește `flex` 1,7→1,15, cardurile fiind acum mai late și mai joase.

Device ops trece de la galben la albastru — galbenul e rezervat acum segmentului „încă în lucru",
ca să nu existe două sensuri pentru aceeași culoare.

## v0.58.0 - 2026-07-30 (Monitor Productivitate: sesizările urcă în contoare, bare redimensionate)

### Sesizări & reclamații — din card separat în contoare sus
Cardul „Reclamații & sesizări" din partea de jos e desfăcut în trei contoare, pe rândul de sus,
lângă celelalte (5 contoare în total):
- **Sesizări deschise** — cu câte sunt restanțe din zilele trecute;
- **Reclamații deschise** — cu câte depășesc 7 zile;
- **Sesizări rezolvate azi** — cu câte au intrat pe telefon.

Componenta `MonitorComplaints` a fost eliminată (nu mai avea consumatori). Defalcarea pe categorie
a emailurilor rezolvate azi s-a mutat în antetul cardului de obiective.
Canalele ocupă acum toată lățimea ecranului, nu 3/4.

### Fix: barele apăreau disproporționat de mari
SVG-ul barelor folosea `preserveAspectRatio: 'none'`, ceea ce îl întindea pe toată înălțimea
disponibilă a cardului — bare și cifre deformate pe verticală, cu atât mai vizibil cu cât cardul
creștea. Trecut pe `xMidYMid meet` (scalare uniformă, raport păstrat).

Redimensionări în același pas: înălțime viewBox 130→96, lățime bară max 40→20px, cifra de pe bară
12→9px, ora 11,5→8,5px, iar rândul canalelor `flex` 2,4→1,7. Antetul cardului de canal: titlu
17→14,5px, cifra „rezolvate azi" 30→24px. Contoarele de sus: 27→24px (5 pe rând în loc de 3).

## v0.57.0 - 2026-07-30 (Monitor Productivitate: canale în grid 2×2, grafice mari)

- **Eliminat** graficul agregat „Solicitări pe oră — azi · intrate vs rezolvate" — dubla informația
  deja prezentă în cardurile de canal.
- **Canalele trec în grid 2×2** și ocupă zona principală: fiecare card are acum ~2,5× suprafața
  anterioară, iar barele sunt semnificativ mai mari (lățime 15→22px, înălțime utilă 68→90px,
  valorile 8,5→12px, orele 9→11,5px). Adăugată linie de bază sub bare.
- **Înapoi la o singură serie** (rezolvate pe oră). Suprapunerea intrate/rezolvate încărca inutil
  un card mic; datele despre volumul intrat rămân disponibile în API (`*_new`).
- **Device ops se afișează doar unde există**: cardul apare dacă grupul chiar are astfel de
  operațiuni (Operațional), și dispare pe Financiar, unde contabilitate/recuperare TVA nu au
  device ops — un card permanent gol nu spune nimic. Cu 3 canale, grid-ul devine 3 coloane, ca să
  nu rămână un gol.
- Antetul cardului: numele + ora de vârf în stânga, „rezolvate azi" mare (30px) în dreapta, cu
  „în lucru" dedesubt.
- Defalcarea pe categorie a emailurilor rezolvate azi s-a mutat în cardul „Reclamații & sesizări".

## v0.56.0 - 2026-07-30 (Monitor Productivitate: filtrare pe grup + bare suprapuse)

### FIX MAJOR DE CORECTITUDINE: cifrele nu erau filtrate pe grup
Monitorul „Operațional" afișa numărători **globale pe toată firma**, nu doar pe departamentele
grupului. Concret, la emailuri rezolvate azi arăta **717**, din care doar **164** aparțineau
Operațional (suport_1/2/3 + taxe_drum) — restul erau contabilitate, comercial, mobilitate și 389
fără departament atribuit. Aceeași problemă pe Financiar.

Toate interogările din `/monitor/live` primesc acum un JOIN pe `employee_department_mapping`
filtrat pe departamentele grupului: contoarele de canal, seriile orare (intrate și rezolvate),
sesizările/reclamațiile, categoriile și device ops.

Chei de legătură (verificate în bază — diferă de la o tabelă la alta):
- emailuri / apeluri → `lower(cts_assignee_email) = lower(edm.email)`
- task-uri → `assignee_employee_id = edm.id` (cheie străină numerică; `iris_id` este text și
  nu se potrivește — un `JOIN` pe el întorcea zero rânduri)
- device ops → `closed_by_employee_id = edm.id`

Valori după filtrare: Operațional 167 mail / 455 task / 122 apel / 14 device;
Financiar 126 / 62 / 31 / 0. Totalurile din carduri coincid cu suma barelor orare pe ambele grupuri.

### Fix: „restanțe" la sesizări subraporta
Vechimea se calcula din `changed_at`, prezent doar pe 68 din 320 de emailuri deschise. Înlocuit cu
momentul real de sosire (`raw->'extra'->>'created_at'`, marcat UTC), cu `changed_at` ca rezervă.

### Bare suprapuse în loc de alăturate
Perechea de bare de la v0.55.0 înjumătățea lățimea fiecărei bare — ilizibil pe TV. Acum, per oră,
o **singură bară lată translucidă** (intrate) cu o **bară mai îngustă plină în față** (rezolvate,
verde). Aceeași logică pe graficul mare, prin `barPercentage` diferit pe aceeași categorie
(`stacked: false` — seriile se suprapun, nu se însumează).

## v0.55.0 - 2026-07-30 (Monitor Productivitate: bare pereche intrate vs rezolvate)

Fiecare canal arată acum, pe fiecare oră, **două bare alăturate**: intrate (culoarea canalului) și
rezolvate (verde) — se vede dacă echipa ține pasul cu volumul primit. Același principiu și pe
graficul mare de sus.

### Metrici noi în `/monitor/live` — volumul INTRAT pe oră
- `hourly[].mail_new` — din `raw->'extra'->>'created_at'` (singurul timp real de sosire al
  emailului). **Este text naiv în UTC**, deci se marchează explicit `AT TIME ZONE 'UTC'` și se
  convertește în fusul local; altfel barele „intrate" ar fi apărut decalate cu 3 ore față de cele
  „rezolvate", pe același grafic.
- `hourly[].task_new` — `cts_created_at`; `hourly[].apel_new` — `cts_started_at`;
  `hourly[].device_new` — `finished_at` (momentul predării de către montator).

### Fix: apelurile rezolvate pe oră erau toate zero
`hourly[].apel` folosea `changed_at`, care este **NULL pe toate rândurile** din
`cts_calls_ground_truth` — coloana ieșea goală. Înlocuit cu momentul încheierii apelului
(`cts_started_at + cts_duration_seconds`). Rezultat: 182 apeluri rezolvate, distribuite corect
pe ore, egal cu totalul din card.

### Fix: fereastra graficului mare pornea de la index, nu de la oră
`all.findIndex(...)` returna poziția în listă, folosită apoi ca oră de start — corect doar din
întâmplare când lista începe la 00. Înlocuit cu ora reală a primului interval cu volum
semnificativ (≥10% din vârf), calculat pe max(intrate, rezolvate).

### Verificare de consistență (2026-07-30)
Totalurile din carduri vs suma barelor orare vs interogare directă în bază:

| Canal | Card | Σ bare | Bază |
|---|---|---|---|
| Mail rezolvate | 717 | 717 | 717 |
| Apel rezolvate | 182 | 182 | 182 |
| Task rezolvate | 551 | 551 | 551 |
| Device rezolvate | 14 | 14 | 14 |
| Mail în lucru | 320 | — | 320 |
| Mail intrate | 768 | 768 | 768 |

**Limitare cunoscută:** „Task rezolvate" folosește `cts_updated_at` (ultima modificare), fiindcă
tabela nu are un timp de rezolvare propriu. În practică ultima modificare este rezolvarea, dar
editarea unui task deja închis îl mută la ora editării.

## v0.54.0 - 2026-07-30 (Monitor Productivitate: canalele trec pe bar chart)

Graficul de linie din cardurile de canal era greu de citit (fără axă, fără valori) — înlocuit cu
bare pe oră.

### Bar chart pe oră, per canal
- Fiecare bară = o oră din ziua curentă, cu **valoarea scrisă deasupra** și **ora dedesubt**.
- Ora curentă e evidențiată (bară la opacitate plină + oră îngroșată).
- **Fereastra de start** nu mai pornește de la prima înregistrare, ci de la prima oră cu volum
  semnificativ (≥10% din vârf). Altfel 1–2 emailuri primite noaptea (ora 02, 04, 06) întindeau
  graficul pe toată ziua și striveau orele reale de lucru: acum mail-ul începe de la 07, iar
  Device ops își păstrează orele de dimineață pentru că acolo chiar are volum.
- Barele cu zero rămân vizibile ca linie subțire (se vede că ora a existat, dar fără activitate).

### Rezumat numeric în antetul cardului
Cifrele „rezolvate azi" și „în lucru" s-au mutat sus, lângă titlu, în formatul `650 / 319`
(rezolvate / în lucru), cu delta verde `+N` la schimbare. Eliberează spațiu pentru grafic.

### Apeluri: „în curs" în loc de un câmp gol
Cardul Apel afișa `0` la „în lucru" pentru că metrica nu exista. Adăugat în `/monitor/live`:
- `apeluri.rezolvate_azi` — apeluri de azi cu status `solved`/`closed`;
- `apeluri.in_curs` — apeluri de azi încă neînchise (status `new` / `in progress`).

**Atenție la definiție:** `in_curs` numără doar apelurile **de azi**. Fără filtrul pe zi ieșeau 372,
din care 358 erau restanțe istorice niciodată închise — un număr care ar fi arătat ca „372 apeluri
în desfășurare acum", complet fals. Cu filtrul pe azi: 14.

## v0.53.1 - 2026-07-30 (Monitor Productivitate: reechilibrare proporții)

Ajustare de proporții — contoarele de sus dominau ecranul, gauge-urile erau prea mici.

- **KPI-uri micșorate**: cifra 40→27px, iconița 44→34px, padding redus. Rămân lizibile de la
  distanță fără să ocupe un sfert din ecran.
- **Gauge-uri mărite**: procentul 32→42px, ținta 12→14px, numele departamentului 14→16px.
  Eliminat plafonul de înălțime (`maxHeight: 190`) care le ținea mici degeaba.
- **Redistribuit spațiul pe verticală**: rândul obiectivelor primește `flex 1.55` (era 1),
  rândul canalelor 0.95, graficul orar 1 — gauge-urile au acum cea mai mare suprafață.

*Notă privind datele:* seriile pe oră NU sunt simulate. Mail = `cts_ground_truth.cts_solved_at`,
Apel = `cts_calls_ground_truth.cts_started_at`, Task = `cts_task_ground_truth.cts_updated_at`,
Device = `device_operations.closed_at` — toate grupate pe oră în `Europe/Bucharest`, filtrate pe
ziua curentă. Verificat prin comparație directă cu interogarea în bază: valori identice.

## v0.53.0 - 2026-07-30 (Monitor Productivitate: layout pentru TV — 4 rânduri, grafice mari)

Dimensionare pentru ecran mare (wall-monitor), după mockup-ul `varC1`.

### Fix: Device ops nu avea serie orară — sparkline-ul chiar era gol
Canalul „Device ops" primea un array gol ca serie, deci graficul lui era plat indiferent de
activitate. Adăugat `hourly[].device` în `/monitor/live` (din `device_operations.closed_at`,
în fus local). Acum are date reale (azi: vârf 6 operațiuni la ora 09).

### Layout — 4 rânduri, fără chart-ul lunar
- **Eliminat** „Volum zilnic luna curentă — emailuri + apeluri" (cerut explicit).
- Structura devine: KPI-uri → grafic orar + reclamații → 4 canale live → obiective pe toată lățimea.
- Obiectivele ocupă acum tot rândul de jos, nu o treime — gauge-urile au spațiu real.
- Toate cifrele mărite pentru citit de la distanță: KPI 26→40px, cifrele canalelor 16→27px,
  cutiile de reclamații 23→34px, ticks/legendă grafic 9.5→12px.

### Gauge-uri — înapoi la arc
Inelul complet din 0.52.2 e înlocuit cu **gauge clasic (arc deschis)**, ca în mockup:
- procentul realizat mare în centru, ținta scrisă sub el;
- **ac pe arc** pe poziția obiectivului real — se vede instant dacă a fost depășit;
- verde = obiectiv atins, galben = sub obiectiv;
- antetul cardului arată ziua lucrătoare curentă („ziua 22/23").

### Canalele live
- Titlu grafic: „**Solicitări rezolvate pe oră** — azi" (era „Rezolvate pe oră").
- Fiecare canal arată acum **ora de vârf** („vârf 09–10 · 97") citită din datele reale.
- Cifra „rezolvate azi" are count-up + delta verde (`+N`) când se schimbă între două citiri,
  ca să se vadă mișcarea pe monitor.
- Sparkline îngroșat (2.5px) și înălțime flexibilă, cu punct pe ora curentă.

## v0.52.2 - 2026-07-30 (Monitor Productivitate: gauge înlocuit cu inel complet)

Gauge-ul în formă de arc (240°) rămânea îngust pe coloană și împingea textele sub el, unde se
strângeau până deveneau ilizibile („obiectiv atins" / „target la zi 71.9% · ziua 22/23").

- Înlocuit cu un **inel complet (360°)** care se umple cu procentul **realizat din obiectiv**
  (`obiectiv_atins / obiectiv_real`), nu cu o valoare absolută pe o scală 0–100.
- **Toate cifrele stau acum în interiorul inelului**: procentul mare în centru, eticheta
  „DIN OBIECTIV" sub el, iar dedesubt realizat vs țintă („90.7% / 75.2%").
- Statusul devine badge pe fundal propriu (contrast real, nu text mic pe fundalul cardului).
- „ținta zilei" și „ziua N din M" pe două rânduri separate, la 10px — lizibile de la distanță.
- Reper subțire pe inel = unde ar trebui să fim azi conform ritmului lunii.

## v0.52.1 - 2026-07-30 (Monitor Productivitate: lizibilitate — valori pe gauge, zonă reclamații, reordonare)

Ajustări vizuale după prima rulare pe monitor + un bug de calcul găsit pe parcurs.

### Fix de calcul: „target la zi" era umflat cu ~36%
Gauge-ul compara **zile calendaristice** scurse (30, din `per_day`) cu **zile lucrătoare** din lună
(23) — de unde și eticheta absurdă „30/23 zl". Targetul zilei ieșea `obiectiv × 30/23`, adică peste
obiectivul lunar întreg, așa că departamente aflate în grafic apăreau „sub ritm". Acum se numără
doar zilele lucrătoare scurse (Luni–Vineri), plafonate la totalul lunii: 22/23 în loc de 30/23.

### Gauge-uri
- Procentul atins e scris **pe grafic**, mare, colorat după status; sub el, ținta („țintă 75.2%").
- Ac de referință pe arc, pe poziția obiectivului real.
- Eliminate badge-urile `30/23 zl` și `728h` (nerelevante pe un monitor de perete). Rândul de sub
  gauge arată acum „target la zi X% · ziua N/M".
- Gauge SVG propriu pentru monitor — `ProdGauge` (partajat cu pagina Productivitate) rămâne neatins.

### Layout reordonat după cât de des e citit
1. Cele 3 contoare (Rezolvate azi · În lucru · Sesizări) — acum pe **un singur rând**, nu pe coloană.
2. **Volumul pe oră** urcat sus și mărit — era informația cea mai căutată, stătea ultima.
3. Canalele (mail / apel / task / device ops).
4. Obiectivele per departament + volumul zilnic lunar.

### Graficul pe oră
- Bare **segmentate pe sursă** (emailuri / apeluri / solicitări), nu un total nediferențiat — la
  cererea „ce s-a rezolvat, emailuri sau solicitări?".
- Axa arată **intervalul orar** (`09–10`), nu doar ora, ca să se vadă unde a fost vârful.
- Ora curentă și ora de vârf sunt evidențiate; restul barelor, aceeași culoare mai stinsă.
- În antet, defalcarea pe categorie de conținut a emailurilor rezolvate azi (informații / sesizări /
  reclamații / neclasificate).

### Zonă nouă: Reclamații & sesizări (înlocuiește „Distribuție pe tip")
Donut-ul pe canal a fost scos — nu spunea nimic acționabil. În locul lui:
- **Deschise acum**, cu câte sunt **restanțe** (rămase din zilele trecute, nu din azi).
- **Rezolvate azi** + câte au intrat pe telefon.
- Avertisment când există sesizări deschise de **peste 7 zile** (acum: 3, cea mai veche de 24 zile).
- Defalcare reclamații vs sesizări.

Backend: `sesizari` primește `restante`, `peste_7z`, `apel_sesizari_azi`, `apel_reclamatii_azi`;
adăugat `rezolvate_categorii` (rezolvate azi pe categorie de conținut). Toate aditive.

## v0.52.0 - 2026-07-30 (Monitor Productivitate: heartbeat live — sesizări, device ops, defalcare pe oră)

Redesign al dashboard-ului standalone de la Productivitate → Monitor Operațional / Financiar
(`/api/v1/productivity/dashboard/{group}`), ca să funcționeze ca un "puls live al firmei" pe
monitoarele de birou, nu ca un raport static. Tabul din aplicație rămâne launcher — nu s-a schimbat
fluxul de deschidere.

### Backend — `GET /productivity/monitor/live` îmbogățit (aditiv)
Cheile existente (`emailuri`, `taskuri`, `apeluri`) sunt **păstrate identic** pentru compatibilitate.
Adăugat:
- **`sesizari`** — sesizări/reclamații deschise nerezolvate, cerute explicit. Nu există tabelă
  dedicată: sunt valori de categorie, citite din `cts_ground_truth.cts_category` cu fallback pe
  `emails.ai_category` (același COALESCE ca `productivity._fetch_email_rows`). Întoarce
  `deschise` / `sesizari_deschise` / `reclamatii_deschise` / `rezolvate_azi`.
- **`device_ops`** — al 4-lea canal (Suport 2), lipsea complet din monitor.
- **`hourly`** — rezolvate pe oră azi, defalcat mail/task/apel + total; alimentează sparkline-urile
  și graficul orar.
- **`per_dept`** — rezolvate azi / în lucru per departament, filtrat pe grupul curent
  (endpoint-ul acceptă acum `?group=operational|financiar`).
- **`ts`** — timestamp real cu oră (înainte era doar data, deci ora lipsea).

**Fix fus orar:** toate agregările pe „azi"/pe oră folosesc acum `AT TIME ZONE 'Europe/Bucharest'`.
Coloanele sunt `timestamptz` stocate în UTC — fără conversie, vârful de activitate de la ora 10
local apărea pe ora 07, iar „azi" se rupea la miezul nopții UTC, nu local.

*Notă:* filtrul `status = 'in progress'` (cu spațiu) e **corect** și a fost păstrat — valoarea din
bază e literal `'in progress'`, nu `'in_progress'`.

### Frontend — dashboard nou pe 3 rânduri
- **Rândul 1**: gauge-urile de obiectiv per departament + 3 contoare mari cu **count-up animat** și
  deltă față de citirea anterioară: *Rezolvate azi* (mail+apel+task+device), *În lucru acum*,
  *Sesizări deschise*.
- **Rândul 2**: 4 carduri de canal (Mail / Apel / Task / Device ops), fiecare cu **sparkline din
  date reale pe oră** + rezolvate azi + în așteptare.
- **Rândul 3**: rezolvate pe oră (ora curentă evidențiată) · distribuție pe tip · volum zilnic lunar.
- Indicator **LIVE** cu puls; dacă un poll eșuează devine **RECONECTARE** și se păstrează ultima
  valoare bună — nu se inventează mișcare. Ceas local în header.
- Cadență: heartbeat live la **15s** (COUNT-uri ieftine); `dashboard/data` rămâne la 5 min
  (forecast + analytics, scump).

### Fix-uri
- **Culorile seriilor din graficul de volum nu se aplicau**: se pasau `var(--am)` și
  `color-mix(...)` direct lui Chart.js, dar contextul canvas 2D nu interpretează variabile CSS —
  seriile se desenau cu culoarea default. Adăugate helperele `mgToken()` / `mgAlpha()` care
  rezolvă tokenii înainte de desenare (același principiu deja folosit corect în `ProdGauge`).
- **Graficul nu reacționa la comutarea light/dark**: citea tema la construcție, dar `theme` lipsea
  din dependențele efectului. Adăugat.
- **Grilă/axe**: înlocuit `rgba(255,255,255,α)` hardcodat cu valori derivate din tokenul de text —
  vizibile corect pe ambele teme.
- **Emoji eliminate** din monitor (`🖥`, `📊`) → iconițe SVG line-style `stroke="currentColor"`.
- **Cache-busting**: versiunea din pagina standalone era fixată la `0.46.53`, deci browserul servea
  `mg-app.js` din cache după orice modificare. Acum se citește din `VERSION`.

*Fără migrație DB — se citesc doar tabele existente.*

## v0.51.1 - 2026-07-30 (fix afișare Operațiuni Suport 2: Asignat, date, durată/limită, obiective)

Fix-uri pe funcționalitatea livrată în v0.51.0 — datele erau corect sincronizate în bază, dar
interfața nu le afișa (coloane goale) și un obiectiv era etichetat greșit.

- **Coloana "Asignat" era goală**: sincronizarea nouă (`device_ops_suport2_sync.py`) nu popula
  câmpul `assignee_raw` (nici în obiectul Python, nici în INSERT-ul SQL), deși `assignee_employee_id`
  era corect populat. Interfața verifică specific `assignee_raw`, nu ID-ul. Adăugat `assignee_raw`
  = numele din "Closed by" în ambele locuri; necesită re-rulare sincronizare pentru rândurile
  existente.
- **"Data creare" era goală**: sursa nouă nu are un moment de "creare" echivalent. Înlocuită cu
  două coloane separate: "Finalizat montator" (`finished_at`) și "Închis Suport 2" (`closed_at`).
- **Durată + încadrare în limită**: coloană nouă cu durata Suport 2 (`closed_at - finished_at`) și
  limita din obiectivul de productivitate al categoriei (`productivity_objective.limita_minute`),
  colorată verde/roșu după încadrare.
- **Status "Închis"**: rândurile cu `closed_at` populat afișează acum un badge distinct "Închis" în
  coloana Status, în loc de statusul intern `finalizat` (neschimbat în bază, doar afișare).
- **Obiective productivitate — categorie "Operațiuni" apărea ca "Emailuri"**: dropdown-ul de tip
  obiectiv (`PROD_TIP_OPTIONS`) nu includea valoarea `device_ops`, deși eticheta corectă exista
  deja (`PROD_TIP_LABELS`). Datele din bază erau corecte (`tip='device_ops'`) — bug pur de afișare
  în `<select>`. Adăugat `device_ops` în lista de opțiuni.

## v0.51.0 - 2026-07-30 (Operațiuni Suport 2: sursă de date schimbată la view_device_operations)

### Schimbare sursă: "Operațiuni" (Suport 2) nu mai reflectă munca montatorilor, ci a Suport 2
Sursa veche (`/cts/device-operations`, legacy) conținea doar actorul montator/instalator
(new → finished) — nu exista deloc actorul Suport 2 care închide efectiv operația (finished →
closed). Obiectivul de productivitate "Operațiuni" pentru Suport 2 se calcula deci pe date care
nu aveau legătură cu munca reală a Suport 2.

Noua sursă, `view_device_operations` (IRIS Data Views), conține explicit `Closed by`/`Closed at`
(cine a închis operația și când) și `Finished at` (când a terminat montatorul) — exact perechea
folosită acum pentru a calcula durata Suport 2 (`finished_at` → `closed_at`).

- **Whitelist Suport 2** (nu departament — potrivire pe nume din "Closed by"): Robert Iova, Robert
  Kovacs, Ovidiu Ticus, Mihai Cuc, David Miclau, Baican Emanuel-Crinel, Zoltan Tyepak (oficial
  `suport_3`, inclus explicit fără schimbarea departamentului lui din pagina Utilizatori).
- **Fereastră**: doar operațiuni închise (`Closed at`) începând cu 1 iulie 2026.
- **Categorii mapate 1:1** din `Operation Type`: instalare_noua, mutare, interventie, calibrare,
  periferice, demontare. Categoria `inlocuire` rămâne fără sursă de date deocamdată (afișează gol) —
  nu există echivalent "Replacement" în datele CTS; se revine la ea când se decide cum se combină.
- `migrations/20260730_device_operations_suport2_view.sql`: coloane noi aditive pe
  `device_operations` (`closed_by_raw`, `closed_by_employee_id`, `closed_at`, `finished_at`,
  `operation_type_raw`, `dv_row_id`) + indexuri.
- `app/services/device_ops_suport2_sync.py` (nou): trunchiază și repopulează `device_operations`
  din `view_device_operations`, filtrat pe whitelist + fereastră + mapare categorii.
- `POST /api/v1/device-ops/suport2/sync` (nou): declanșează sincronizarea manual.
- Oprit cronul vechi (`device_ops_sync.run_recent_if_due()` nu mai rulează din `process_now`) —
  codul legacy rămâne neșters, doar dezactivat.
- `app/services/productivity.py`: `_fetch_device_ops_rows` rescrisă — citește din
  `closed_by_employee_id`/`finished_at`/`closed_at`, nu mai filtrează pe departamentul din
  `employee_department_mapping` (whitelist-ul de sincronizare e sursa de adevăr).

## v0.50.1 - 2026-07-30 (Satisfacție: retry apeluri IRIS AI + rate-limit, reduce "Context IRIS indisponibil")

### Fix: scor neutru (75/80) prea des la calculul satisfacției clienților
`_iris_call`/`run_prompt` făceau un singur apel către IRIS AI fără retry — orice timeout/eroare
tranzitorie de rețea/HTTP 429/500/502/503/504 pica direct pe fallback ("Context IRIS indisponibil —
folosit scor neutru 75"), umflând artificial numărul de clienți cu scor neutru necorelat cu situația
lor reală.
- `app/services/iris_ai.py`: `run_prompt` reîncearcă acum până la 3 ori pe erori tranzitorii
  (eroare transport/rețea, HTTP 429/500/502/503/504), cu pauză scurtă (1s, apoi 3s) între încercări.
  Erorile de configurare/request invalid (cheie lipsă, URL lipsă, JSON invalid) NU se reîncearcă —
  reîncercarea n-ar schimba rezultatul.
- `app/services/satisfaction_snapshot.py`: adăugat un interval de 1 secundă între clienții pentru
  care s-a făcut efectiv un apel AI (v4), ca rulările lunare/manuale să nu bombardeze gateway-ul
  IRIS cu cereri concurente și să reducă riscul de rate-limiting pe partea IRIS.

## v0.50.0 - 2026-07-30 (Productivitate: fix subraportare task-uri contabilitate/TVA/Suport 2 + operațiuni defalcate)

### Fix critic: task-uri CargoBox/BGToll/eToll/Hugo excluse greșit din obiectivul general
`_fetch_task_rows` excludea automat task-urile din familiile CargoBox/BGToll/eToll/Hugo de la
obiectivul general "task" — corect pentru `taxe_drum` și `suport_1` (au obiective family dedicate,
ar fi dublat numărătoarea), dar greșit pentru `contabilitate`/`recuperare_tva`/`suport_2` (nu au
obiective family separate, deci munca lor pe aceste task-uri dispărea din statistici). Exemplu real:
Lasca Oana-Maria avea 471 task-uri rezolvate în iulie, aplicația arăta doar 309 (162 excluse greșit).
Fix: `has_family_split` calculat per departament din obiectivele reale — se aplică excluderea DOAR
dacă departamentul are și obiective family-specific; altfel obiectivul general ia toate task-urile
solved/closed. `taxe_drum`/`suport_1` neschimbate.

### Operațiuni (device_ops) defalcate per operator — Suport 2
Coloană nouă „Operațiuni" + „Cotiz. oper.%" în tabelul de productivitate, separată de „Task-uri"
(anterior eram contopite). Rând expandabil per operator (click) arată defalcarea pe categorie
(calibrare/demontare/înlocuire/instalare nouă/intervenție/mutare/periferice) cu număr și procent.
Etichete „Device_ops — Interventie" înlocuite cu nume românești („Operațiuni - intervenție" etc).

### Tabel Obiectiv — expand/collapse
Header-ul tabelului de obiective e acum un buton expand/collapse (implicit închis).

### Fix scroll orizontal tabel operatori
`overflow:'hidden'` pe wrapper suprascria `overflowX:'auto'` (proprietate shorthand) — scrollul
orizontal nu funcționa niciodată. Eliminat `overflow:'hidden'`, adăugat `minWidth:720` pe tabel.

## v0.49.0 - 2026-07-30 (Satisfacție: sursă unică de date + reguli mai stricte pentru "revenire")

### Sursă unică de date (fix divergență sidebar vs dashboard)
`GET /clients/{id}` (sidebar) și dashboard-ul de evoluție citeau satisfacția din DOUĂ locuri
diferite (`clients.satisfaction_pct` vs `client_satisfaction_snapshots`), scrise de endpoint-uri
separate — puteau ajunge desincronizate. Acum `POST /clients/{id}/estimate-satisfaction` scrie
direct în `client_satisfaction_snapshots` (UPSERT pe `client_id, month_key`), iar `get_client`
citește din aceeași tabelă. `feedback_campaigns.py` (`_segment_candidates`) actualizat la fel,
via `LEFT JOIN LATERAL`. Nicio schimbare de schemă — doar de sursă de citire/scriere.

### Regulă nouă: "revenire" (recontact) cere precedent real
Un mesaj era numărat ca „revenire" (penalizat) chiar și fără nicio sesizare/reclamație anterioară
în același thread — orice mențiune de nemulțumire trecută ("duminică trecută nu mi-a mers") era
tratată ca recontact. Acum `_V4_RECONTACT_SYSTEM` cere OBLIGATORIU un mesaj anterior categorisit
sesizare/reclamație în același thread înainte de a număra o revenire.

### Fix-uri anterioare din acest ciclu (deja pe staging, incluse în acest release)
- Eliminat eticheta „B2B" din raționamentul AI (context holistic + service recovery) — irelevantă
  pentru scor, genera text confuz.
- Fix quote-stripping în `_fetch_month_interactions`: textul citat din emailuri (reply-uri) nu mai
  e analizat ca mesaj nou al clientului — reducea fals numărul de recontacts.
- Căutare client după ID mail/apel (`cts_training.py`, `cts_calls_training.py`, UI).

## v0.48.3 - 2026-07-29 (Satisfacție: sistemele automate nu mai apar ca clienți nesatisfăcuți)

### Rulare pe eșantion de 300 clienți — rezultat
Eșantion stratificat pe activitate **reală** (mailuri + apeluri legate prin `client_id`, task-uri
prin `iris_client_id`): 100 very_active (≥20 interacțiuni/90 zile), 100 active (≥5), 80 low_active
(≥1), 20 inactive. Exclude `satisfaction_exclude`.

**300 procesați, 0 erori, 236 apeluri AI.** Validat: numărul de interacțiuni analizate corespunde
realității la **324/324** clienți verificați — zero cazuri de „am analizat N interacțiuni" pentru un
client care are mai puține.

| Interacțiuni | Clienți | Scor mediu |
|---|---|---|
| 0 | 84 | 100,0 |
| 1-5 | 102 | 90,0 |
| 6-20 | 87 | 81,6 |
| 21-50 | 38 | 66,0 |
| 50+ | 13 | 64,5 |

### Fix: sisteme automate raportate ca clienți nesatisfăcuți
Primele două poziții din lista de nesatisfăcuți erau `HU-GO TEMP` (8,5%, 131 interacțiuni) și
`HU-GO ELECTRONIC TOLL SYSTEM` (12,8%, 258) — sisteme de taxare rutieră din Ungaria care trimit
exclusiv notificări automate (înregistrări vehicule, blacklist NÚSZ/hu-go.hu). Motorul le trata ca
reclamații ale unui client nemulțumit; IRIS semnala corect în raționament că „NU sunt interacțiuni
reale cu serviciul CARGO TRACK", dar scorul rămânea mic și polua lista folosită pentru intervenții.

Migrația `20260729i` extinde `clients.satisfaction_exclude` (mecanism existent, deja folosit pentru
CARGO TRACK SOLUTIONS / RUPTELA UAB / UNKNOWN CLIENT) la: sistemele HU-GO, `NOTIFICATION SYSTEM`,
`PARTENERI CLIENTI`, RUPTELA (furnizor dispozitive), CARGOFUEL (aplicație internă),
`EXPERT SOFTWARE GROUP` (furnizor software) — 7 entități, total 18 excluse.

**După curățare:** 319 clienți, 35 nesatisfăcuți, medie 87,1%. Lista de nesatisfăcuți conține acum
exclusiv firme de transport reale, cu raționament sprijinit pe date verificabile (ex. referințe de
amenzi contestate `47ABA050`/`47AEB272` fără răspuns 13 zile, reminder-e repetate ale clientului).

## v0.48.2 - 2026-07-29 (Satisfacție: apelurile nu se mai numără dublu)

### Fix: apel numărat de două ori când are mai multe rânduri CTS
Găsit la validarea rulării pe eșantionul de 300: un client (`SPEC TRANS SRL`) raporta 4
interacțiuni analizate deși are 3. Cauza: `LEFT JOIN cts_calls_ground_truth ON call_local_id = c.id`
returna apelul o dată per rând CTS legat, iar 6 apeluri pe staging au câte 2 rânduri.
Nu era contaminare între clienți — toate apelurile erau ale lui — dar umfla numărătoarea.

`DISTINCT ON (c.id)` în ambele query-uri de apeluri (`_fetch_month_interactions` și
`_fetch_orphan_calls_for_client`). Emailurile erau deja curate (0 rânduri CTS duplicate pe
`email_id`), deci nu au avut nevoie de fix.

**Validare pe eșantionul de 300:** 300 încadrări, 0 erori, iar numărul de interacțiuni analizate
corespunde realității la **300/300** clienți (înainte de acest fix: 299/300).

## v0.48.1 - 2026-07-29 (Satisfacție: interacțiunile analizate sunt strict ale clientului)

### Fix critic: „am analizat 54 de interacțiuni" pentru un client care are 10
`satisfaction_engine._fetch_month_interactions()` lega mailurile fără `client_id` prin **domeniul**
expeditorului. Dar pe staging **171 de domenii sunt partajate între 646 de clienți** — `ruptela.com`
(furnizorul nostru de dispozitive) apare la 8 clienți, printre care unul cu 0 mailuri proprii.
Fiecare dintre ei primea mailurile tuturor celorlalți de pe domeniu, deci scorul de satisfacție se
calcula pe conversații care nu erau ale lui.

Nici adresa exactă nu e suficientă singură: în CTS multe adrese sunt puse pe mai mulți clienți —
furnizori (`support@ruptela.com` la 8), bănci (`no-reply@unicredit.ro` la 6,
`tiberiu.fenesi@btleasing.ro` la 5), sau text liber în loc de adresă (`dispecer` la 37, `sotia` la
27, `sofer` la 17). O adresă partajată nu identifică pe nimeni.

- Legarea se face acum doar prin `emails.client_id` sau prin adrese care apar la **exact un client activ**.
- Tabelă derivată `client_unique_emails` (**11.609 adrese unice pentru 9.219 clienți**) — migrația `20260729h`. Calculul echivalent la runtime costa ~380 ms per client, inacceptabil pentru un lot de 300.
- `phone_match.rebuild_client_unique_emails()`, apelată din `iris_sync` după fiecare sync de clienți (raportează `unique_emails_indexed`).
- `email` e TEXT, nu VARCHAR(320): unele intrări CTS sunt liste întregi lipite într-un element jsonb. Filtrate pe lungime și pe absența spațiilor.

**Verificare:** pe cei 40 de clienți cei mai expuși (cu domeniu partajat), interacțiunile raportate
sunt acum **37 = 37** față de numărul real de interacțiuni proprii (`client_id` strict); zero clienți
cu raportat > propriu. Caz concret: `VOLANUL DE AUR SRL` (0 mailuri proprii, domeniul `ruptela.com`)
raportează exact cele 14 apeluri care îi aparțin, nu mailurile Ruptela.

Apelurile și task-urile erau deja corecte (`calls.client_id` strict + `phone_match` pe telefoanele
acelui client; `cts_task_ground_truth.client_id` = `iris_client_id`, verificat pe `raw_payload`).
`interaction_analysis` și `_raw_interactions_text` filtrau deja strict pe `client_id`.

## v0.48.0 - 2026-07-29 (Consistență productivitate: aliasuri departament, assignee, adrese interne)

Verificarea diferențelor semnalate de echipa Contabilitate/Recuperare TVA a scos la iveală
patru defecte care făceau ca aceeași întrebare să primească răspunsuri diferite în funcție de ecran.

### Fix critic: același departament, două cifre în aceeași aplicație
`cts_task_ground_truth.department` conținea `taxe_de_drum` (21.959 rânduri), iar canonicul din
`employee_department_mapping` e `taxe_drum`. `cts_tasks_sync._slug()` normaliza doar spații și
cratime, fără aliasare. Rezultatul, măsurat pe aceeași lună și același departament:

| Ecran | Filtru | Task-uri găsite |
|---|---|---|
| Istoric, Per-operator | `cts_task_ground_truth.department` | **0** |
| Forecast, Analytics | `edm.department` (JOIN pe angajat) | **21.870** |

- `_DEPT_ALIASES` în `cts_tasks_sync` + migrația `20260729d` (21.963 rânduri corectate).
- Cele două ecrane divergente aliniate la aceeași sursă ca celelalte (departamentul angajatului asignat), `productivity.py:1366` și `:1495`. După fix: **21.870 = 21.870**.
- Rămâne o divergență legitimă: Robert Cazacu e în `account_management`, task-urile lui sunt `comercial` (133) — o persoană poate lucra pentru alt departament, nu e alias.

### Fix: 8 departamente din 16 nu se normalizau niciodată
`cts_ground_truth.cts_department` păstra valori brute CTS — `Administrativ` (71), `Operational` (33),
`Product Management` (32), `Management General` (16), `Instalari` (13), `IT Team 1` (11),
`Marketing` (8), `HR` (8), `IT` (2). Cauza: `_map_department()` normaliza pe `DEPT_LABELS` (8
departamente, lista pe care alege clasificatorul AI), dar `employee_department_mapping` are 16;
pentru restul cădea pe fallback și păstra valoarea brută, care nu se potrivea cu niciun slug în
rapoarte. Aliasuri adăugate în `_DEPT_ALIASES` (nu în `DEPT_LABELS`, ca să nu extindem lista AI-ului)
+ migrația `20260729e` (194 rânduri).

### Fix: whitelist-ul de angajați era în urma realității
`iris_employee_sync.VALID_DEPARTMENTS` avea 8 departamente, deci angajații din `instalari`, `hr`,
`marketing`, `product_management` etc. erau **respinși la import** — deși `employee_department_mapping`
avea deja 7 oameni în `instalari`, ajunși acolo pe altă cale. Concret: Adrian Jurca (activ în rosterul
IRIS, `instalari`) nu se putea importa, deci cele 180 de operațiuni ale lui nu se contorizau.
Whitelist extins la toate cele 16 departamente reale → **6 angajați noi importați**.

### Fix: operațiuni pe dispozitive pierdute pe typo-uri în sursă
Din 1.429 operațiuni, doar 654 aveau angajat mapat. Cauze în datele CTS:
- `adrian.jurca@cagrotrack.ro` — typo de domeniu, **180** operațiuni
- `cristian.gotonoaca@` vs rosterul IRIS `cristian.gotonoaga@` (c/g) — **65**
- `cosmin.margauan` — username fără domeniu, **49**
- `client@` / `nealocat@` — placeholder-e, corect nemapate (185)

`device_ops_sync._normalize_assignee_email()` corectează typo-urile, completează domeniul intern și
respinge placeholder-ele înainte de rezolvare. Migrațiile `20260729f` + `20260729g`:
**654 → 851** operațiuni cu angajat. Restul: 460 placeholder/gol, 118 persoane care nu există în
rosterul IRIS (foști angajați, 2 adrese Gmail externe).

### Fix: adresele noastre în lista de emailuri a clienților
71 de clienți aveau 193 de adrese CargoTrack în `clients.emails` — `office@cargotrack.ro` la **26 de
clienți**, adrese de colegi (`calin.lucaciu@`, `nicoleta.berde@`…) la 3-8 fiecare, plus 9
placeholder-e `fara_email@cargotrack.ro`. În CTS ele înseamnă „agentul care gestionează clientul".
`match_client()` face `emails @> [from_address] LIMIT 1`, deci orice email trimis de un coleg primea
un client **arbitrar** dintre cei 26.
- Adresele mutate în `clients.internal_contact_emails` (informative, nu se pierd) — migrația `20260729c`.
- 365 de atribuiri făcute pe această bază, anulate (păstrate doar cele confirmate de CTS).
- `process_email.match_client()` refuză adresele interne la matching.
- `iris_sync.discover_client_emails()` filtra free-mail dar **nu** domeniile proprii, deci le-ar fi
  reintrodus la următorul sync — filtru adăugat (`cargotrack.ro`, `trakosoft.ro`).

Efect pe acoperirea emailurilor: 86,1% → **81,8%**. Cifră mai mică, dar corectă — 365 din legăturile
de dinainte erau false.

### Robustețe
- `iris_sync.sync_clients_from_iris()` reconstruiește `client_phone_keys` după sync (altfel indexul rămâne în urmă și apelurile clienților noi nu se leagă). Raportează `phone_keys_indexed`.
- `storage/logs/` lipsea pe staging — cron-ul lunar de satisfacție redirecta în el (`>>`), deci rula fără log. Creat.
- Snapshot-urile de satisfacție pe 2026-07 recalculate cu `force=True`, după corectarea legăturilor.

### Fix critic: forecast-ul de productivitate crăpa pe TOATE departamentele
`forecast_report()` referea `ore_concediu` necondiționat, dar variabila se definește doar pe ramura
*fără* snapshot lunar. Cum `productivity_monthly_snapshot` exista, funcția arunca
`UnboundLocalError: cannot access local variable 'ore_concediu'` la fiecare apel → dashboard-ul
returna `forecast: []`, adică **lista de obiective era goală pentru toate departamentele**.
Valoarea e acum derivată din `ore_planificate - ore_disponibile` (definite pe ambele ramuri).
După fix, `/productivity/dashboard/data`:

| Grup | Departamente cu forecast |
|---|---|
| financiar | contabilitate (50,89% atins / 79,21 real), recuperare_tva (72,47 / 93,98) |
| operational | suport_1 (90,74), suport_2 (94,93), suport_3 (insuficient), taxe_drum (88,0) |

### Fix-uri găsite la verificarea de regresie (fixul ținea în DB, dar sync-ul îl anula)
Migrațiile curățau datele, dar prima resincronizare reintroducea murdăria. Prinse rulând efectiv
fiecare sync după curățare:
- `device_ops_sync` persista `assignee_raw` **brut**, deși normaliza doar pentru rezolvarea angajatului → 48 `cagrotrack` + 20 fără domeniu + 21 `gotonoaca` reveneau la fiecare sync. Acum se salvează valoarea normalizată (originalul rămâne în `raw_payload`), iar departamentul se ia din angajatul asignat.
- `iris_sync.sync_clients_from_iris()` scria `emails` direct din IRIS, deci 42 de adrese interne reveneau — filtrul pus inițial acoperea doar `discover_client_emails()`, nu calea principală. Separarea se face acum la upsert, în `internal_contact_emails`.
- Ramura „IRIS nu trimite adrese" păstra lista locală **integral**, deci 3 clienți care au în CTS exclusiv adrese interne le țineau în `emails`. Acum se păstrează doar partea externă.
- În query-ul cu parametri, `%` din `LIKE` trebuie dublat (psycopg2 îl tratează ca placeholder) — inclusiv într-un comentariu SQL, care a produs `IndexError: tuple index out of range`.

### Fix: /api/v1/health raporta versiune greșită
`config.app_version` era hardcodat `0.46.10` și rămânea în urmă la fiecare livrare. Acum se citește
din fișierul `VERSION`.

### Verificare finală (după toate sincronizările)
15/15 verificări trec: 9 pe curățenia datelor (toate 0), consistența ecranelor pe `taxe_drum` (0
divergență), acoperire emails 82,0%, calls 63,5%, 853 operațiuni cu angajat, 14.129 chei de telefon,
15.691 snapshot-uri recalculate. Endpoint-urile `/health`, `/healthz` și ambele dashboard-uri: 200,
zero erori în log.

### Verificat, blocat upstream (outbox #46)
- **Suport 2 (tehnic)**: 0 operațiuni din 1.429 — cei 6 angajați nu apar nici în `assignee_raw`. Sursa trimite deja tipuri tehnice (`interventie` 169, `calibrare` 67, `inlocuire` 114), dar toate executate de oameni din `instalari`. Configurarea aplicației e completă și așteaptă datele: `productivity_objective` are deja 7 obiective `device_ops` pe `suport_2`, cu categorii identice cu `action_type`-urile primite.
- **9.881 task-uri fără client**: nu au nici email, nici apel, nici `client_name` — nerezolvabil local.

## v0.47.0 - 2026-07-29 (Consolidare sursă date: legătură mesaj↔client din CTS + fix matching)

Legătura mesaj↔client era incompletă: 58% din emailuri și 65% din apeluri nu aveau client asignat,
deci nu intrau în calculul de satisfacție și productivitate. Cauza nu era lipsa datelor — CTS
trimite `client_id` pe fiecare mesaj — ci că nu era propagat local, plus trei defecte de matching.

### Fix: matching telefon rata numerele internaționale
- `phone_match.phone_key()` (nou): cheie canonică = ultimele 9 cifre. Înainte, `match_client_by_phone()` compara string exact (`phones @> '["0722123456"]'`), iar `while1_ingest._match_client_phone()` acoperea doar `0` ↔ `+40`. Numerele cu prefix `00` nu se potriveau niciodată: în `clients.phones` apare `0037368295882`, în `calls` apare `+37368533883` — același abonat, zero potriviri.
- Tabelă derivată `client_phone_keys` (client_id, phone_key) + index — expandarea `jsonb_array_elements_text` peste 16k clienți făcea seq scan (>120s pe backfill). `phone_match.rebuild_phone_index()` o reconstruiește după sync-ul de clienți.
- Potrivirile ambigue (număr partajat de mai mulți clienți) nu se mai atribuie: NULL e preferabil unei atribuiri greșite într-un calcul de satisfacție.

### Fix: emailurile TRIMISE nu se legau la client
- `process_email.match_client()` citea doar `from_address` — pe emailurile trimise de noi acela e o adresă CargoTrack, deci clientul rămânea NULL (978 emailuri). Acum acceptă și destinatarii (`to_addresses` + `cc_addresses`), sărind adresele interne. Satisfacția vedea doar jumătatea de conversație primită.
- `_addr_list()` normalizează formatul destinatarilor (listă text, formă Graph sau JSON-ca-string).
- `_is_internal_address()` refolosește `autoreply_generator.INTERNAL_DOMAINS` (acoperă subdomenii: `mail1.cargotrack.ro`).

### Fix: firul conversației nu se salva
- `o365_ingest`: `conversationId`, `ccRecipients` și `internetMessageId` se cer acum de la Graph și se persistă în `emails.conversation_id` / `cc_addresses` / `internet_message_id`. Coloanele existau, dar erau NULL pe toate cele 8.443 rânduri, iar `raw_graph_payload` păstra doar `{source, graph_id}` — firul nu era recuperabil retroactiv. De acum înainte, un răspuns al nostru poate moșteni clientul din mesajul primit în același fir.

### Forward-fix: CTS propagă clientul la fiecare sync
- `cts_calls_sync`: după upsert, `raw->>'client_id'` (= `clients.iris_client_id`) se propagă imediat în `calls.client_id`. Raportează `clients_linked`.
- `cts_groundtruth_sync`: idem pentru `emails.client_id`, din `raw->'extra'->>'client_id'`. Raportează `clients_linked`.
- Ambele completează doar NULL-uri — o legătură existentă nu se suprascrie.

### Backfill (`migrations/20260729b_backfill_client_links.sql`)
| Sursă | Înainte | După |
|---|---|---|
| `emails` | 3.516 / 8.443 (41,6%) | **7.270 (86,1%)** |
| `calls` | 5.927 / 16.872 (35,1%) | **10.707 (63,5%)** |

Pe pași: apeluri←CTS 4.257 · emailuri←CTS 3.698 · apeluri←telefon 523 · emailuri trimise←destinatar 56. Total **8.534** legături noi. Aditiv și idempotent (doar `WHERE client_id IS NULL`).

### Ce rămâne nelegat (verificat, legitim)
- `emails` 1.173: **812 interne** CargoTrack, 361 externi (furnizori Ruptela/Fortinet/Atlassian, newslettere) — corect fără client.
- `calls` 6.165: **6.158 nu au rând CTS** (apeluri de pe numere nedeclarate în CTS; unul apare de 149 ori), 7 au CTS dar clientul lipsește local.
- `cts_task_ground_truth` 9.842 cu `client_id` NULL: CTS nu trimite clientul, fără email/apel atașat — nerezolvabil local, escaladat.

### Verificat și respins ca sursă de matching
`client_master` și `view_client_list` (IRIS Data Views) au fost trase integral și comparate cu datele locale: **0 adrese email noi, 0 telefoane noi** — `clients` e deja sincronizat 1:1 (16.341 local / 16.376 în view). Sunt registre de firme, nu de mesaje: nu leagă un email/apel anume la client. (`client_master` conține date ANAF/VIES/bilanț — utile eventual pentru scor de sănătate client, nu pentru matching.)

## v0.46.59 - 2026-07-29 (Fix match client↔surse date: calls, satisfacție, domenii generice)

### Fix critic: interacțiuni false în satisfacție (domenii generice)
- `satisfaction_engine._client_email_domains()`: extins blocklist-ul de domenii generice care nu identifică unic un client — adăugate `mail.ru`, `yahoo.es`, `yahoo.it`, `yahoo.fr`, `hotmail.ro/it/fr`, `outlook.ro`, `me.com`, `mac.com`, `mail.com`, `ymail.com`, `live.com/ro`, `msn.com`, `protonmail.com`, `proton.me`, **`cargotrack.ro`**, **`trakosoft.ro`** (domenii interne). Clienți ca RAVAS GRUP TRANS (`mail.ru`) nu mai primesc sute de interacțiuni false de la alți expeditori pe același domeniu.

### Fix: calls.client_id backfill retroactiv
- Backfill pe apeluri existente fără client asignat: **2.411 apeluri** (din 13.291 fără client) au primit `client_id` prin match pe `clients.phones` (inbound = caller_number, outbound = callee_number, cu normalizare prefix `0` ↔ `+40`).
- Restul (~10.880) nu au număr de telefon în baza de date a niciunui client activ.
- `while1_ingest._insert_call()`: matchul de client prin telefon se face acum **la ingestie**, nu doar la clasificarea AI. Apeluri noi primesc `client_id` imediat.

### Diagnostic surse date (fără modificare de cod — probleme upstream)
- **cts_calls_ground_truth fără link local** (5.193): apeluri din CTS mai vechi decât bootstrap-ul While1 pe staging (iulie 2026). Lipsă de date istorice, nu bug de matching.
- **cts_task_ground_truth fără client** (9.842): task-urile vin din CTS fără `client_id` și fără `email_id`/`call_id` → nu există informație de matching local.
- **device_operations**: aduce din `instalari`, nu din `tehnic` (suport 2) — cerere trimisă în Outbox lui Razvan pentru adăugarea tabelei `tehnic` în gateway-ul IRIS.

## v0.46.58 - 2026-07-29 (Export clienți cu contacte duplicate)

- **Buton "Export duplicate"** în toolbar-ul listei de clienți: click generează direct un fișier CSV cu toți clienții care împart același email sau același număr de telefon cu alt client.
- **Format CSV** deschis direct în Excel (encoding UTF-8 BOM, separator virgulă): coloane Tip duplicat, Contact comun, ID client, Nume client — grupat pe contact, ușor de filtrat și corectat.
- Acoperă atât emailuri cât și telefoane; exclude câmpuri goale/liniuță.

## v0.46.57 - 2026-07-29 (Satisfacție: excludere UNKNOWN CLIENT + scor v4 în lista clienți)

- **Excludere UNKNOWN CLIENT**: clientul fantomă "UNKNOWN CLIENT" (id=3081) marcat cu `satisfaction_exclude=TRUE` — dispare din toate calculele și listele de satisfacție (dashboard, top satisfăcuți, la risc, nesatisfăcuți, distribuție, trend, movers).
- **Filtrare `satisfaction_exclude`**: toate query-urile din `/clients/satisfaction-stats` filtrează acum explicit clienții excluși — nu mai pot apărea indiferent de sursa datelor.
- **Scor satisfacție în lista clienți**: coloana de satisfacție afișează acum scorul din ultimul snapshot lunar v4 (nu câmpul vechi din `clients.satisfaction_pct`) — clienți ca "LIU & FLO EXPRESS SRL" vor apărea cu scorul corect dacă au snapshot calculat.

## v0.46.56 - 2026-07-29 (Tab Satisfacție client: redesenat complet cu algoritm v4 + explicații)

- **Un singur algoritm de satisfacție** — eliminat butonul "Estimează satisfacție" (producea scor cu engine diferit, inconsistent cu snapshot-ul). Tab-ul afișează exclusiv datele din snapshot-ul lunar v4.
- **LineChart** evoluție lunară înlocuiește tabelul — axă temporală, linie mov scor, linie roșu punctat prag 70%.
- **Pills clickable per lună** sub grafic — click pe o lună afișează defalcarea ei completă.
- **Scor final cu formulă**: contribuție Emoție (70%) + Context IRIS (30%) + restituire recovery → total afișat explicit.
- **Emoție (70%)**: afișează numărul exact de informații/sesizări/reclamații/reveniri, penalizările per categorie (−10/−20/−5 pt), scorul final calculat pas cu pas.
- **Reveniri pe problemă nerezolvată**: fiecare revenire detaliată cu referința (apel/mail) și motivul detectat de IRIS.
- **Context IRIS (30%)**: semnal dominant, trend, raționamentul complet al IRIS în format text.
- **Service Recovery**: bonus afișat dacă a fost aplicat, cu explicația IRIS.

## v0.46.55 - 2026-07-29 (Buton View în Satisfacție clienți + navigare directă la tab Satisfacție client)

- **Buton View** adăugat în toate cele 3 tabele din pagina Satisfacție clienți: Top 10 satisfăcuți, Clienți la risc, Clienți nesatisfăcuți. Click pe View → navigare directă la fișa clientului, tab Satisfacție.
- **Navigare cross-tab**: din Satisfacție clienți → Clienți (ClientDetail, tab Satisfacție) fără back/forward manual, fără refresh.
- **Tabul Satisfacție în ClientDetail** (existent): grafic evoluție lunară, tabel istoric lunar, breakdown complet per factor (Emoție, Efort, Operațional, Relație, Scor IRIS final) cu sub-metrici detaliate și tooltip-uri.
- **`initialTab` prop** pe `ClientDetail` — permite deschiderea directă pe orice tab din exterior.

## v0.46.54 - 2026-07-29 (Snapshot lunar imutabil — targetele nu se mai modifică în cursul lunii)

- **Tabelă nouă `productivity_monthly_snapshot`**: la prima generare a raportului pentru o lună, valorile `coeficient`, `ore_planificate`, `ore_disponibile`, `obiectiv_real`, `obiectiv_minim` se persistă automat și devin **imutabile**.
- **`department_report` + `forecast_report`**: la fiecare request verifică snapshot-ul. Dacă există, returnează valorile fixate — concediile neplanificate, aprobările ulterioare, modificările de obiective din UI **nu mai afectează targetele lunilor deja started**.
- **Migrație**: `migrations/20260729_productivity_monthly_snapshot.sql` — `CREATE TABLE IF NOT EXISTS`, idempotentă.
- **Compatibilitate**: lunile fără snapshot (generate prima dată după deploy) primesc snapshot automat la prima accesare. Lunile vechi (înainte de deploy) se comportă ca înainte până la prima accesare, apoi se fixează.

## v0.46.53 - 2026-07-29 (Monitor productivitate: heartbeat live — gauge kilometraj + charts)

- **Monitor Operațional / Financiar complet redesenat**: înlocuit tabelele statice cu dashboard exclusiv grafice, actualizat automat fără refresh de pagină.
- **Gauge "kilometraj lunar" per departament**: target dinamic ajustat la ziua curentă (la 15 ale lunii = ~50% din obiectiv). Indicator de stare: pe traseu / aproape / sub minim / obiectiv atins.
- **Bar chart volum zilnic**: emailuri + apeluri pe bara lunii curente, cu marker vertical pentru ziua de azi. Stacked bar (Chart.js).
- **Queue live (actualizat la 30s)**: emailuri rezolvate azi / în lucru, task-uri rezolvate azi / în lucru / în așteptare, apeluri azi — bare de progres animate.
- **Endpoint nou `GET /api/v1/productivity/monitor/live`**: date instantanee din `cts_ground_truth`, `cts_task_ground_truth`, `cts_calls_ground_truth`. Public, fără auth.
- **Polling separat**: dashboard/forecast la 5 minute (date agregate), queue live la 30 secunde — fără refresh manual.

## v0.46.52 - 2026-07-29 (Fix productivitate: gardă DV, iris_id mapping, delogare Surse date)

- **Fix `/productivity/report` 500**: query-urile DV din `department_report` și `forecast_report` nu mai fac UNION direct pe `cts_dv_employee_vacation_request`. Tabela se verifică prin `information_schema` înainte de acces — dacă nu există (sync DV nerulat), se sare fără eroare.
- **Fix ID-uri greșite concedii DV (720h → 736h)**: query-ul DV filtra după EDM ids (6, 7, 19...) în loc de CTS iris_ids (110, 123, 135...). Acum `ops` selectează și `iris_id`, construiește `iris_to_edm` map, query-ul DV filtrează corect după `iris_ids`.
- **Fix delogare instant pe pagina "Surse date"**: `iris_dv.py` ridica `HTTPException(401)` când cheia DV era invalidă/lipsă — frontend-ul delogha userul la orice 401. Schimbat în `403` (3 locuri: cheie lipsă, cheie invalidă la `/onboarding`, cheie invalidă la `/prompt`).

## v0.46.51 - 2026-07-29 (Dashboard monitor productivitate)

- **Monitor Operațional / Monitor Financiar**: două tab-uri noi în secțiunea Productivitate — deschid o pagină standalone (`_blank`) optimizată pentru monitoare de birou (fără sidebar, fără autentificare).
- **Dashboard monitor**: gauge per departament (progres lunar vs obiectiv), chips volum azi, tabel zilnic (ziua × departament × % obținut) colorat verde/galben/roșu față de obiectivele configurate. Auto-refresh la interval configurabil (1–60 minute, default 10).
- **Endpoint public** `GET /api/v1/productivity/dashboard/data?group=operational|financiar` — date agregate: forecast lunar, volum azi per departament, serie zilnică.

## v0.46.50 - 2026-07-28 (Fix forecast August — last_tgt undefined)

- **`forecast_report`**: variabilele `first_tgt`/`last_tgt` mutate înainte de query-ul care le folosea ca parametri. Anterior se produceau cu `cannot access local variable 'last_tgt'` pentru orice lună curentă (August 2026), lăsând secțiunea "Estimare productivitate" din email/UI goală.

## v0.46.49 - 2026-07-28 (Fix sync concedii vacation_approved + iris_id mapping)

- **`iris_id` mapping**: populat automat în `employee_department_mapping` prin match `first_name + last_name` față de `cts_dv_employee`. Corectează maparea CTS employee_id pentru toți angajații (anterior `iris_id = NULL` → sync concedii eșua silențios).
- **`sync_vacation_from_dv`**: rescris să folosească `iris_id` ca CTS employee_id în loc de `edm.id` direct (cele două nu coincid). Aduce corect concediile pentru toți angajații, inclusiv Judea Bianca (CTS id=123, EDM id=15).
- **`_write_employee_leaves`**: nu mai șterge `vacation_approved` la sync angajați (anterior ștergea tot `entry_source='cts'` inclusiv concediile DV).
- **Hook post-sync DV**: după sync `employee_vacation_request` din "Surse date", se apelează automat `sync_vacation_from_dv` → `vacation_approved` mereu în sync.
- **Calcul productivitate**: query UNION care citește din ambele surse (`employee_schedule vacation_approved` + `cts_dv_employee_vacation_request`) ca fallback.
- **Migrație `20260728_iris_id_from_cts_dv.sql`**: populează `iris_id`, șterge pre-2026 din `employee_schedule`, repopulează `vacation_approved` 2026+ prin maparea corectă.

## v0.46.48 - 2026-07-28 (Fix ore planificate/disponibile în rapoarte)

- **`department_report`**: `ore_planificate` = calendar ideal (zile_lucratoare × op_activi × work_hours), identic cu `forecast_report`. Anterior era pontaj real (zile_prezent + absente_pontaj), care dădea valori subevaluate când angajații nu aveau pontaj complet.
- **`ore_disponibile`** = `ore_plan_ideale − ore_concediu_aprobat` (vacation_approved + manual, union L-V, fără leave_request). Anterior era `pontaj - absente_pontaj`.
- Efect iulie suport_1 (5 operatori activi): ore planificate 824→**920** (5×23×8), ore disponibile 648→**720** (920−200h concediu). Coeficient 0.1033 rămâne neschimbat (deja corect).
- Pontajul real rămâne folosit exclusiv pentru calculul SLA (obiectiv_atins) — nemodificat.

## v0.46.47 - 2026-07-28 (Test email + perioadă probă productivitate)

- **Buton "Trimite test"** în tab Notificări: selectezi email destinatar, grup departamente și luna de raport — trimite un email de previzualizare fără să afecteze destinatarii configurați.
- **Data start productivitate** per angajat (`productivity_start_date DATE`): angajații cu această dată setată sunt excluși din calculele de productivitate pentru lunile anterioare datei respective (perioadă de probă / onboarding).
- **UI**: câmp "Start productivitate" în modalul Concedii per angajat (selector lună/an + buton Salvează). Badge vizual în lista angajaților dacă data e setată.
- **Backend**: `department_report` și `forecast_report` filtrează automat angajații cu `productivity_start_date > ultima zi a lunii`.
- **Setat manual**: Boros Vanessa-Karolina → start 2026-08-01; Bulmau Anamaria-Iuliana → start 2026-10-01. Ambele excluse din calculele iulie 2026.
- Migrare: `migrations/20260728_employee_productivity_start.sql` (`ADD COLUMN productivity_start_date DATE`).

## v0.46.46 - 2026-07-28 (Tab Notificări productivitate + email lunar automat)

- **Tab Notificări** nou în modulul Productivitate: configurare destinatari email per grup de departamente (Operațional / Financiar / Toate / departament individual).
- **Email lunar automat**: în prima zi lucrătoare a fiecărei luni la ora 10:00, IRIS trimite automat un email cu rezumatul lunii precedente (realizat vs obiectiv per departament) + estimare productivitate luna curentă (zile lucrătoare, concedii, ore disponibile, target real și minim).
- **PDF analitic atașat**: tabel cu datele de productivitate, generat cu PyMuPDF (fallback HTML dacă PDF eșuează).
- **Text introductiv AI**: generat prin `iris_ai.run_prompt` cu fallback la template text dacă AI nu e disponibil.
- **Gating robust**: cheia KV `productivity.last_monthly_sent` previne trimiterea duplicată în aceeași lună. Trimitere manuală disponibilă via butonul "Trimite acum (test)" din UI.
- Migrare DB: `migrations/20260728_productivity_notifications.sql` (`productivity_notifications` tabelă nouă).
- Endpoint-uri noi: `GET/POST /productivity/notifications`, `DELETE /productivity/notifications/{id}`, `POST /productivity/notifications/send-now`.
- **Filtru Financiar în tab Analiză**: selectorul de departamente include acum „Financiar (toate)" (contabilitate + recuperare_tva).

## v0.46.45 - 2026-07-28 (Aliniere coeficient + fix dublu-numărare concedii)

- **Coeficient real**: `department_report` folosește acum `baza_procent / ore_plan_ideale` (calendar L-V × work_hours) în loc de rată prezență (`ore_disp/ore_plan_pontaj`). Consistent cu `forecast_report`.
- **Obiectiv real**: `obiectiv_real = ore_disponibile_pontaj × coeficient` — ajustat cu absențele reale. Anterior era fix 95%.
- **Fix dublu-numărare concedii** (`forecast_report`): CTS poate scrie atât `leave_request` cât și `vacation_approved` pe același interval pentru același angajat. Acum `leave_request` e exclus dacă există `vacation_approved` suprapus. Ore concediu corect contabilizate prin union de zile L-V (nu suma intervalelor). suport_1 iulie: 328h → 208h corect.
- **aggregate_reports**: coeficient și obiectiv_real recalculate din `ore_plan_ideale` cumulat.
- `ore_plan_ideale` adăugat în răspunsul `department_report`.

## v0.46.44 - 2026-07-28 (Concedii reale din DV CTS: vacation_approved)

- **Mapare angajați**: view DV `employee` sincronizat, `iris_id` populat pentru toți 50 angajați locali via JOIN pe email.
- **Sursa concedii**: înlocuit `planned_leave` (planificare anuală estimată) cu `vacation_approved` (cereri real aprobate din CTS HR, status=2, 2026+). 190 rânduri sincronizate pentru 39 angajați.
- **Forecast**: ore_concediu calculat din `vacation_approved` + `leave_request approved` + intrări manuale — eliminat `planned_leave` din calcul.
- **Modal Utilizatori**: afișează `vacation_approved` (badge CTS, read-only) + intrări manuale (editabile). Coloană nouă "Tip" (Concediu / Invoire). Nr. zile afișat pe rând.
- **Sync zilnic**: `run_vacation_dv_sync_if_due` sincronizează DV `employee` + `employee_vacation_request`, actualizează `iris_id`, scrie `vacation_approved` în `employee_schedule` — totul automat o dată pe zi.

## v0.46.43 - 2026-07-28 (Concedii forecast: planned_leave + sync DV zilnic)

- **Forecast ore concediu**: query include acum `planned_leave` (sursa principală, aprobat implicit din CTS HR) + `leave_request approved` — anterior se folosea doar `leave_request` (invoiri orare max 3h), ceea ce ducea la ore concediu 0 pentru angajați cu concediu real dar fără invoiri.
- **Sync DV zilnic**: `employee_vacation_request` (snapshot IRIS DV) se sincronizează automat o dată pe zi în tabela locală `cts_dv_employee_vacation_request`, alături de sync-ul de angajați. Pregătire pentru maparea completă cts_id → local_id.

## v0.46.42 - 2026-07-28 (Fix prag zi lucrătoare: ≥1 angajat)

- `_MIN_STAFF_FOR_WORKING_DAY`: 2 → 1. Zi cu cel puțin 1 angajat prezent = SLA curge și productivitatea se calculează (inclusiv sâmbătă/sărbătoare legală dacă există pontaj). Zi cu 0 angajați = nelucrătoare.
- Sâmbetele rămân excluse din `zile_lucratoare` la calculul coeficientului (L-V only) — dar productivitatea sâmbetei cu angajați prezenți intră în scorul lunar.

## v0.46.41 - 2026-07-28 (Fix UI card forecast)

- Card prognoză: ascunse tabelul de obiective (email/apel/task) și tabelul de operatori — nu sunt relevante pt estimare.
- "Ore concediu" înlocuit cu "Ore disponibile" (valoarea corectă ore_disponibile = planificate minus concedii).

## v0.46.40 - 2026-07-28 (Gestiune concedii manuale + protecție sync)

- **DB**: coloană `entry_source` pe `employee_schedule` (`'cts'` vs `'manual'`); sync șterge doar rândurile CTS, concediile manuale supraviețuiesc.
- **Backend**: 3 endpoint-uri noi — `POST/PUT/DELETE /settings/employees/{id}/schedule/{sid}` pentru concedii manuale; intrările CTS rămân read-only.
- **UI Utilizatori**: buton "Concedii" per angajat (cu număr cereri aprobate); modal cu tabel concedii aprobate (badge CTS/Manual), formular Add/Edit/Delete pentru intrările manuale.
- **Gauge productivitate**: label zona minim–obiectiv schimbat din "Sub obiectiv" în "Foarte aproape de obiectiv".

## v0.46.39 - 2026-07-28 (Fix sursa concedii forecast + formula coeficient)

- **Fix sursa concedii**: `forecast_report()` folosea `kind='planned_leave'` (planificare anuală imprecisă); înlocuit cu `kind='leave_request' AND status='approved'` — cereri de concediu aprobate concret.
- **Fix formula coeficient**: `obiectiv_real` era fix la `baza_procent` ignorând concediile. Acum: `coeficient = baza_procent / ore_planificate_ideale` (obiectiv per oră), `obiectiv_real = ore_disponibile × coeficient` — ajustat cu absențele reale.
- Exemplu suport_1 August: 5 oameni × 21 zile × 8h = 840h ideale, baza 95% → coeficient = 0.1131/h; cu 200h concedii aprobate → 640h × 0.1131 = 72.4% obiectiv real.

## v0.46.38 - 2026-07-28 (Fix forecast 500 + selector lună Analiță)

- **Fix bug 500**: `UnboundLocalError: lhdays` în `forecast_report()` când lista istoricului era goală sau nicio zi lucrătoare nu era găsită — inițializat `lhdays = 0` înainte de blocul `if last_hist:`.
- **UI Analiță**: Selectorul de lună (‹ input ›) lipsea din topbar pe tab-ul Analiță — adăugat `monthNav` alături de range toggle, selector dept, selector operator.

## v0.46.37 - 2026-07-28 (Productivitate Estimată — prognoză luni viitoare)

- **Backend**: Endpoint nou `/productivity/forecast?month=YYYY-MM&months=N` — returnează estimare productivitate pentru luna/perioadă viitoare. Volume estimate = media zilnică din ultimele 2 luni × zile lucrătoare luna țintă. Ore disponibile ajustate cu zilele de concediu planificate din `employee_schedule`. Returnează `is_forecast:true` și aceeași structură ca `/productivity/report`.
- **UI Rapoarte**: Când navighezi la o lună viitoare, se apelează automat `/forecast` în loc de `/report`. Banner portocaliu "PRODUCTIVITATE ESTIMATĂ" apare deasupra cardurilor. Fiecare card are border dashed + badge "Estimat" + metrica "Ore concediu" în loc de "Ore disponibile" (când are concedii).
- Concedii: extrase din `employee_schedule` (kind=planned_leave) per angajat, intersectate cu zilele lucrătoare ale lunii țintă.

## v0.46.36 - 2026-07-28 (Productivitate Financiar: contabilitate + recuperare_tva)

- **DB**: Migrare `20260728_productivity_financiar.sql` — adaugă `contabilitate` și `recuperare_tva` în `productivity_department_config` (baza_procent=95) și `productivity_objective` cu 3 obiective fiecare: email/120min/50%, apel/4sec/25%, task/120min/25%.
- **Zero modificări cod**: frontend/backend funcționau deja cu orice dept configurat în DB.
- Tab Rapoarte → Financiar și tab Analiță → dropdown dept afișează acum ambele departamente cu date reale.

## v0.46.35 - 2026-07-28 (Rapoarte multi-lună + culori cotizație per coloană)

- **Backend**: `/productivity/report` acceptă acum `months=1..12`. Când `months>1`, calculează N rapoarte lunare și le agregă (volum/ore/zile = sumă; obiective/operatori = recalcul pe date cumulate). Funcție nouă `aggregate_reports()` în `productivity.py`.
- **UI Rapoarte**: range toggle (1L/3L/6L/12L) aplică acum efectiv — Rapoarte afișează datele agregate pe intervalul selectat. Navigatorul de lună specifică luna de final a perioadei.
- **UI**: Cotizații per coloană (Email, Task-uri, Apeluri) au culori independente: verde = cel mai mare din coloana respectivă, galben = ultimii 2, albastru = mijloc. Anterior culoarea era globală pe rând (bazat pe performanța generală).

## v0.46.31 - 2026-07-28 (Analiză Task-uri: redesign complet — același stil ca Mailuri/Apeluri)

- **UI**: Task-uri — 2 KPI-uri (Volum, Productivitate %), card timing styled (Timp preluare + Timp rezolvare), Volum pe zi, Productivitate zilnică (condiționat), Distribuție timp rezolvare (orizontal, colorat), Volum pe operator (orizontal, rank colorat).

## v0.46.29 - 2026-07-28 (Analiză Apeluri: fix Productivitate null + card timing styled + chart condiționat)

- **Backend fix**: `get_objectives(db, d, tip=None)` în loc de default `tip="email"` — `apel_lim` era mereu `None`, deci `in_timp_pct` = null în analytics.
- **UI**: Card timing „Timp răspuns / Durată apel" cu stil consistent (bg3, border, font 18px bold).
- **UI**: Chart „Productivitate zilnică" apeluri afișat doar când `in_timp_pct != null` (obiectiv configurat).

## v0.46.28 - 2026-07-28 (Analiză Apeluri: fix KPI-uri — răspuns vs durată apel, distribuție pe durată)

- **Backend**: Query apeluri include acum `cts_duration_seconds`. Câmpuri noi în `apeluri_out`: `avg_response_sec` (timp mediu răspuns), `avg_duration_sec` (durată medie apel), `median_duration_sec`. Buckets pe durată apel (nu pe timp răspuns).
- **UI**: KPI-uri Apeluri: „Timp mediu răspuns" (din `avg_response_sec`) + „Durată medie apel" (din `avg_duration_sec`). Distribuție pe durata efectivă a apelului.

## v0.46.27 - 2026-07-28 (Analiză Apeluri: KPI-uri noi + charts Volum/Productivitate/Distribuție/Operatori)

- **Backend**: `apeluri_out` include acum `buckets` (distribuție durată: <1min, 1-3min, 3-5min, 5-10min, >10min).
- **UI**: Coloana Apeluri redesenată — 4 KPI-uri (Volum, Productivitate %, Durată medie, Mediană), Volum pe zi, Productivitate zilnică (yMin 50), Distribuție durată apel, Volum pe operator.
- **UI**: `shortName()` mutat la nivel de componentă (reutilizabil în toate coloanele).

## v0.46.26 - 2026-07-28 (fix definitiv labels OY bar chart orizontal: type category explicit)

- **UI**: `ProdBarChart` orizontal — scala Y declarată explicit `type: 'category'`; elimină indexuri 0,1,2,3 în favoarea numelui operatorului/bucket-ului.

## v0.46.25 - 2026-07-28 (Analiză: fix tăiere 100% line charts, fix labels OY bar charts orizontale)

- **UI**: `MultiLineChart` — `suggestedMin`/`suggestedMax` în loc de `min`/`max` dur (nu mai taie valorile la margine). Înălțime charts 160→190px.
- **UI**: `ProdBarChart` orizontal — `afterFit` callback pe axa Y: lățime minimă calculată din lungimea maximă a label-urilor (7px/char + 16px padding). Labels operatori/bucketuri apar complet pe axa stângă.

## v0.46.24 - 2026-07-28 (Analiză: yMin 50 pe grafic productivitate zilnică, fix labels bar chart orizontal)

- **UI**: Chart „Productivitate zilnică" — axa Y pornește de la 50% în loc de 0, discrepanțele 90–100% vizibile.
- **UI**: `ProdBarChart` orizontal — inclus `colors` în semnătura de redraw; font size 12px pe labels Y (era 11); `autoSkip: false` + padding 6px pe ticks Y pentru afișare corectă a numelor operatorilor.

## v0.46.23 - 2026-07-28 (Rapoarte: remove Volum sold, gauge labels mai mari)

- **UI**: Eliminat cardul „Volum solved" din stats rapide `ProdDeptCard` (se referea doar la emailuri). Rămân 4 carduri: Coeficient, Zile lucrătoare, Ore planificate, Ore disponibile.
- **UI**: Markere minim/real pe gauge mai late (0.8 unități). Label-urile minim/real pe gauge: `bold 13px` (era 10px).

## v0.46.22 - 2026-07-28 (Gauge: progres colorat + mai mare)

- **UI**: Arc gauge colorat dinamic: roșu dacă sub minim, portocaliu dacă între minim și real, verde dacă ≥ real. Pointer și arc de progres același culoare. Markere minim (galben) și real (verde) ca linii pe arc. Canvas 200px înălțime, coloana 50/50.

## v0.46.21 - 2026-07-28 (Gauge: gauge.js library, staticZones + pointer)

- **UI**: Gauge înlocuit cu gauge.js (`gaugeJS v1.3.7`). `staticZones`: gri (sub minim), galben (minim→real), verde (real→100%). Pointer animat. `staticLabels` cu valorile minim/real. Fișier adăugat în vendor: `gauge.min.js`.

## v0.46.20 - 2026-07-28 (Fix gauge: canvas 2D direct, semicircle corect)

- **Fix**: Gauge rescris complet pe canvas 2D pur (`ctx.arc`). Semicircle real 180° (stânga→dreapta). Track gri + fill colorat + ac + 2 markere (minim galben, real verde). Retina-ready (scale×2). ResizeObserver pentru redraw la resize fluid.

## v0.46.19 - 2026-07-28 (Gauge Rapoarte: Chart.js doughnut half-circle)

- **UI**: Gauge înlocuit cu Chart.js doughnut half-circle (`rotation:-90, circumference:180`). Ac indicator + tick-uri minim/maxim desenate via plugin canvas. Overlay HTML pentru valoare + legendă. Fără librărie externă nouă — folosește `chart.umd.min.js` deja prezent.

## v0.46.18 - 2026-07-28 (Fix gauge Rapoarte Productivitate)

- **Fix**: Gauge SVG rescris complet — viewBox fluid `200×115`, `width:100%/height:auto` (nu mai e pixel fix). Arc semicircle corect, acul și textul în interior. Layout card: `minmax(0,45%)` stânga / `minmax(0,55%)` dreapta (nu mai e `180px` fix care deborda).

## v0.46.17 - 2026-07-28 (Productivitate Analiză: layout 3 coloane Mailuri / Apeluri / Task-uri)

- **UI**: Tab Analiză restructurat în 3 coloane independente: Mailuri (albastru), Apeluri (gri), Task-uri (portocaliu). Fiecare coloană are header colorat, KPI-uri proprii și grafice specifice.
- **Mailuri**: KPI volum + timp mediu + preluare/rezolvare (dacă există). Grafice: volum/zi, categorii pie, distribuție timp, volum per dept/operator.
- **Apeluri**: KPI volum + durată medie + durată totală calculată (avg × volum). Grafic volum/zi. Eliminat „% fără durată" și „% în-timp" (irelevante).
- **Task-uri**: KPI volum + timp mediu + preluare/rezolvare. Grafice: volum/zi, distribuție timp. Eliminat „% fără durată".
- Coloane fără date afișate cu placeholder semitransparent (nu dispar din layout).

## v0.46.16 - 2026-07-28 (Productivitate Rapoarte: gauge + layout 2 coloane)

- **UI**: Card departament în Productivitate → Rapoarte redesenat. Layout 2 coloane: stânga = gauge semicircle cu ac indicator, dreapta = 5 carduri metrice (Volum solved, Coeficient, Ore planificate, Ore disponibile, Zile lucrătoare).
- **Gauge**: Arc SVG semicircle — afișează `obiectiv_atins` (valoarea obținută), marcaj galben `obiectiv_minim`, marcaj verde `obiectiv_real`. Culoare arc: verde=atins, galben=parțial, roșu=sub_minim.

## v0.46.15 - 2026-07-27 (Productivitate: zile cu <2 angajați = zile nelucrătoare)

- **Fix calcul SLA**: Zi în care departamentul are `<2` angajați prezenți în pontaj este tratată ca zi nelucrătoare — SLA nu curge, productivitate = 100% (ex. 1 iunie, 1 mai, orice sărbătoare națională neinregistrată în `productivity.ro_holidays`).
- **Sâmbete cu 1 singur angajat**: deja excluse prin `isoweekday < 6`, dar acum și zilele din săptămână cu prezență redusă sunt excluse automat.
- **Email primit duminică 20:00, rezolvat luni 09:00**: SLA calculat corect — minutele curg doar de luni 07:00 (ora de start program), nu de duminică. Confirmat: `120 min` calculat corect.
- **Implementare**: `_BizCache._dept_day_count` — agregate `COUNT(present=true) per (dept, date)` preîncărcate o singură dată. `is_working_day_for_dept()` + `working_days_in_range()` noi. Prag configurat în `_MIN_STAFF_FOR_WORKING_DAY = 2`.
- `working_dates_luna` în `department_report` folosește același criteriu — absențele din zilele nelucrătoare nu mai gonflează `ore_planificate`.

## v0.46.14 - 2026-07-27 (Performanță pagină Productivitate — 5-10s → <1s)

- **Perf**: Eliminat bottleneck major pe pagina Productivitate (tab Analytics + Raport lunar). Cauza: funcția SQL `business_minutes_emp()` era apelată per-rând (11k emailuri + 14k task-uri/lună) — fiecare apel executa 2 SELECT-uri suplimentare. Fix în două straturi:
  1. **Index nou** `employee_attendance(employee_id, work_date)` — caută exact pe asta, fără index anterior.
  2. **Cache Python** (`_BizCache`): preîncarcă `department_schedule` + `employee_attendance` o singură dată per request și calculează business minutes în Python, eliminând zecile de mii de round-trip-uri PL/pgSQL.
- **Timp măsurat**: `analytics_report` (toate 4 departamente, 30 zile) 5-10s → **~0.9s**.
- **Migrație**: `migrations/20260727_productivity_perf_index.sql` (index idempotent `IF NOT EXISTS`).

## v0.46.13 - 2026-07-27 (Fix sync automat Task-uri CTS + Device Operations)

- **Fix**: Sync-ul periodic pentru **Task-uri CTS** și **Device Operations** nu era integrat în loop-ul de procesare email — datele se blocau la ultima sincronizare manuală. `cts_tasks_sync.run_recent_if_due()` și `device_ops_sync.run_recent_if_due()` adăugate în `process_email_loop` alături de `cts_gt_sync` și `cts_calls_sync`. Throttle intern 240s (identic cu restul).
- **Fix**: `POST /emails/{id}/reprocess` returna 500 — `ai_status` coloană `NOT NULL`, `SET ai_status = NULL` era invalid. Corectat în `'pending'`.

## v0.46.12 - 2026-07-27 (Fix departament: seria PPHU/PPCB/PPBG/ASCF → suport_1 absolut)

- **Fix**: Emailurile cu seria de factură PPHU, PPCB, PPBG sau ASCF în subiect/corp mergeau eronat la `taxe_drum` când contextul era HU-GO. Adăugat excepție absolută în promptul AI: dacă există serie PPHU/PPCB/PPBG/ASCF → suport_1 indiferent de context (ex. „Factură proformă încărcare cont HU-GO" cu PPHU44770 → suport_1).
- Regula anterioară era scrisă doar în subsecțiunea „OP-uri/dovezi de plată" — AI-ul nu o aplica la facturi proformă.

## v0.46.11 - 2026-07-27 (Fix NDR false-positive + buton Reprocesează email)

- **Fix**: `\bNDR\b` word boundary în `process_email.py` — emailuri cu „ANDREMARIO TRANS" etc. nu mai detectate eronat ca NDR/bounce.
- **Feature**: Buton „↻ Reproceseaza email" în detaliul email-ului — vizibil când emailul nu e deja în CTS și nu e spam/carantinat. Reprocesează complet (categorie, departament, documente) și forțează trimiterea spre CTS. Endpoint: `POST /api/v1/emails/{id}/reprocess`.

## v0.46.10 - 2026-07-27 (Regulă departament: info@solutiiweb.ro → Contabilitate)

- **Feature**: Toate emailurile de la `info@solutiiweb.ro` clasificate automat pe departamentul **Contabilitate** (regulă deterministă, prioritate față de AI). Migrație: `20260727_dept_rule_solutiiweb.sql` (idempotentă, se aplică pe prod la release).

## v0.46.9 - 2026-07-27 (Fix link dezabonare public + blocare auto-reply domenii interne)

- **Fix**: Link `{unsubscribe_url}` din emailurile auto-reply acum generează URL public `https://dezabonare-mailguard.cargotrack.ro/noreply/unsubscribe?token=...` accesibil din afara rețelei (anterior: IP intern `95.216.144.102:8501`). Implementat prin `NOREPLY_BASE_URL` în `.env`.
- **Feature**: Auto-reply blocat pentru adrese `@cargotrack.ro` și `@trakosoft.ro` — domenii interne nu primesc confirmări automate. Logat ca `skipped_internal_domain` în `autoreply_send_log`.

## v0.46.8 - 2026-07-27 (Fix latență documente CTS: kick imediat + timer 2min + deadline 10min)

- **Fix (B)**: Drain documente acum pornit imediat după ingestia unui email cu attachmente, fără să aștepte tick-ul de cron. Contractele/permisele ajung la CTS în secunde, nu în 5 minute.
- **Fix (A)**: Timer cron redus de la 5 minute la 2 minute — reduce fereastra de așteptare pentru orice email nou.
- **Fix (D)**: Deadline CTS documente extins de la 5 minute la 10 minute — eliminat `no_documents` pentru emailuri cu backlog mare de attachmente.
- Cauza radix: drain-ul procesa în serie toți cei ~15 attachmente din coadă; contracte cu deadline scurt picate după cel al permiselor/CI-urilor procesate mai devreme.

## v0.46.7 - 2026-07-27 (Fix contoare primite/trimise în lista clienți)

- **Fix**: `email_count` și `sent_count` în lista de clienți arătau 0 pentru clienți cu emailuri orfane (legate via `cts_ground_truth.raw.extra.client_id`). Ex: WHEELS SPEDITION arăta 0-0 în loc de 26 primite / 27 trimise.
- Cauza: simplificarea din v0.46.5 folosea doar `emails.client_id = c.id` (direct), rata orfanele.
- Fix: subquery `sent_count` folosește `g.raw->'extra'->>'client_id' = c.iris_client_id::text` (text comparison → index `idx_cts_gt_raw_client_id` folosit, 62ms/pagină vs 5.9s cu cast bigint).
- `email_count` (received) = directe + orfane via `cts_ground_truth` cu același index. Același fix aplicat în `get_client`.

## v0.46.6 - 2026-07-27 (Modal detaliu apel în pagina client)

- **Feature**: tab „Apeluri" în `ClientDetail` — click pe orice apel deschide modal `CallDetail` cu transcript complet, audio (dacă disponibil), categorie AI, ton, agent asignat, navigare ← →.

## v0.46.5 - 2026-07-27 (Fix performanță critică: lista clienți 5.7s → 18ms)

- **Fix performanță**: query lista clienți era 5.7 secunde per pagină (50 clienți). Cauza: 4 subquery-uri corelate pe `cts_ground_truth` fără index — seq scan complet (~40k rânduri) × 50 iterații.
- `email_count` în lista de clienți simplificat la `COUNT(*) WHERE client_id = c.id` (index existent). Contoarele detaliate (sent, orfane via iris_client_id) rămân în `ClientDetail` unde se calculează o singură dată per client deschis.
- `sent_count` scos din lista paginată (0 constant) — vizibil în detaliu client.
- **Index nou**: `idx_cts_gt_raw_client_id` pe `(raw->'extra'->>'client_id')` — elimină seq scan pentru `sent_count`/`email_count` în `ClientDetail` și satisfaction engine. Creat CONCURRENTLY (fără lock). Migrație: `20260727_cts_gt_raw_client_id_index.sql`.

## v0.46.4 - 2026-07-27 (Fix discover: adrese din raw.extra.to_email reply-uri CTS)

- **Fix**: `discover_client_emails()` rata adresele clientului din reply-urile CTS trimise de operatori — acestea nu au row separat în `emails`, sunt stocate doar în `cts_ground_truth.raw.extra.to_email`. Adăugată a treia sursă: `raw.extra.to_email` pe `cts_direction='sent'`.
- Backfill re-rulat: **448 clienți** actualizați (față de 201 anterior). Ex: WHEELS SPEDITION 1→5 adrese.

## v0.46.3 - 2026-07-27 (Auto-discover adrese email/telefon clienți din CTS)

- **Feature: `discover_client_emails()`** — funcție nouă în `iris_sync.py` care populează `clients.emails` din interacțiunile confirmate CTS (100% safe, nu euristice):
  - Mailuri PRIMITE: `from_address` extras din mailuri unde `cts_ground_truth.raw.extra.client_id = iris_client_id`
  - Mailuri TRIMISE de agent: `to_addresses` jsonb extras din același mecanism
  - Filtrare free-mail (gmail/yahoo/hotmail etc.) — nu poluează lista
  - Merge additiv: adaugă adrese NOI, nu suprascrie ce e deja setat
- **Integrare "Sync Now"**: `discover_client_emails()` rulează automat la finalul fiecărui sync, statistica `emails_discovered` apare în răspuns.
- **Fix sync**: upsert clienți nu mai suprascrie `emails`/`phones` cu `[]` când IRIS trimite gol dar noi avem adrese descoperite local.
- Backfill rulat imediat: **201 clienți** actualizați cu adresele descoperite din CTS.

## v0.46.2 - 2026-07-27 (Fix Conversație client — mailuri primite orfane vizibile)

- **Fix critic**: tab Conversație afișa ZERO mailuri primite pentru clienții fără `emails` populat (40% din clienți activi). Mailurile existau în DB cu `client_id IS NULL` dar cu `iris_client_id` în `cts_ground_truth.raw.extra`. Adăugat al doilea branch UNION pe `client_emails` endpoint care prinde aceste mailuri via `iris_client_id`.
- **Fix**: `email_count` din lista clienți și detaliu client acum include și mailurile orfane legate prin `iris_client_id` — numărul afișat în sidebar era 0 pentru clienți afectați.
- Afectat: ~2871 mailuri primite devenite vizibile, ~6338 clienți activi cu `emails = []`.

## v0.46.1 - 2026-07-27 (Fix motor satisfacție v4 — acoperire interacțiuni)

- **Fix critic**: mailuri trimise de agent (`sent`) nu erau legate de client când `client_id IS NULL` — filtrul folosea `from_address` (al agentului) în loc de `to_addresses` (al clientului). 87% din mailurile sent din iulie afectate → restituire ratată tăcut.
- **Fix critic**: apeluri fără intrare în `cts_calls_ground_truth` (47% din apeluri iulie) erau invizibile pentru penalizări — INNER JOIN înlocuit cu LEFT JOIN pe ambele query-uri (apeluri client și apeluri orfane).
- **Fix**: `_has_activity` în snapshot nu detecta clienți cu DOAR apeluri orfane (client_id NULL mapate prin număr de telefon) — adăugată verificare prin ultimele 9 cifre.
- **Fix**: fallback context IRIS la eșec era `emotion_final` (amplifica scorurile slabe) → înlocuit cu 75 neutru fix.
- **Fix**: mailuri received cu LIMIT 300 ORDER BY ASC tăia mailurile recente pe clienți activi — subquery cu ORDER BY DESC asigură că ultimele 300 (cele mai recente) sunt păstrate.

## v0.46.0 - 2026-07-24 (Motor nou satisfacție clienți v4 — per lună, transparent)

- **Model nou de scor (v4)**, înlocuiește cutia neagră AI (v3) cu ajustările hardcodate (+15 boost, floor 60). Calcul **per lună calendaristică**, fiecare client pornește de la **100%**; zero interacțiuni → rămâne 100%.
- **Scor final = Emoție × 0.70 + Context IRIS × 0.30**, apoi restituire (max 50% din penalizări).
- **KPI Emoție (70%)** — determinist + o judecată IRIS:
  - `−10` per **sesizare**, `−20` per **reclamație** (categorie din CTS ground-truth: `cts_ground_truth.cts_category` / `cts_calls_ground_truth.cts_category`; gol/necunoscut → tratat neutru), clamp la 0.
  - `−5` per **revenire explicită pe problemă nerezolvată** — marcate de IRIS (nu mecanic: reply-uri multiple ≠ problemă persistentă; doar semnalări explicite tip „am mai scris/sunat, încă nu s-a rezolvat").
- **KPI Context IRIS (30%)** — IRIS citește tot contextul lunii (mailuri + apeluri primite + răspunsurile agenților) și dă un scor 0-100 realist (info-only → mare; reveniri nerezolvate → mic). Fără boost/floor.
- **Restituire** — IRIS restituie ≤50% din penalizări dacă vede perechi rezolvare→mulțumire în 48h; gardă anti-abuz (mulțumire la simplă întrebare de tip informație NU restituie).
- **Sursa datelor: CTS ground-truth** (`cts_ground_truth`, `cts_calls_ground_truth`) — nu `emails`/`calls` brute. Legare mail↔client prin `emails.client_id` **cu fallback pe domeniul expeditorului** (55% din mailuri au client_id NULL); apel↔client prin `calls.client_id` cu fallback `phone_match` (numerele CargoTrack `037443006x` ignorate).
- Fereastra: **strict luna calendaristică** (fix defectul v3 care citea 90 zile în urmă indiferent de lună).
- Config nou `settings` key `satisfaction.v4` (ponderi/penalizări reglabile fără redeploy) — migrație idempotentă `20260724_satisfaction_v4_config.sql`. Fallback la defaults în cod.
- Snapshot lunar (`satisfaction_snapshot.py`) și butonul „estimate" (`clients.py`, param opțional `month=YYYY-MM`) trecute pe v4. Motoarele v2/v3 rămân în cod (dead code) pentru comparație, sunset ulterior.
- Fără tabele/coloane noi (refolosește `client_satisfaction_snapshots`).

## v0.45.2 - 2026-07-24 (Fix job status cross-worker: stare în DB)

- **Fix**: starea job-ului de scoring era in-memory → polling nimerea alt gunicorn worker → mereu `status: unknown`. Mutat în tabela `settings` (key `score_job.<id>`) — cross-worker safe.

## v0.45.1 - 2026-07-24 (Scoring batch async cu progress bar)

- **Fix timeout**: scoring batch rulează acum în background (thread daemon), POST returnează `job_id` imediat fără să aștepte finalizarea.
- UI polling la 3s cu progress bar live: `X / total apeluri procesate` + bară de progres.
- Limita ridicată de la 200 la 500 apeluri per batch (max 2000 via query param).
- Endpoint nou: `GET /calls/analytics/score-batch/status?job_id=...`

## v0.45.0 - 2026-07-24 (Fix filtre departament/agent în Analitice Apeluri)

- **Fix: filtrele departament și agent nu funcționau** — `department` era acceptat ca parametru dar ignorat în SQL; `agent` căuta după email dar `agent_extension` stochează numele complet.
- Helper `_agent_dept_filter()`: lookup în `employee_department_mapping` (email → name sau department → toți membrii) → filtru `agent_extension IN (...)`.
- Filtrul aplicat în toate 4 endpoint-uri: `dashboard`, `scores`, `binary-stats`, `score-stats`.
- **Fix UI mg-app.js**: deploy direct (fișierul era în folderul `vendor/` exclus din rsync).

## v0.44.9 - 2026-07-24 (Selector interval scoring batch apeluri)

- **Feature: selector interval** pentru butonul de scoring batch — opțiuni: 24h / 3 zile / 7 zile / 14 zile / 30 zile (default 7 zile). Apelurile nescorate din intervalul ales sunt procesate la apăsare.
- Backend: `score_batch(days_back=N)` + endpoint acceptă `days_back` din body JSON.

## v0.44.8 - 2026-07-24 (Scorare automată KPI apeluri — switch AI)

- **Feature: switch „Scorare automată KPI apeluri (AI)"** în Prompturi AI → tab Apeluri. Când e PORNIT, pipeline-ul de apeluri scorează automat fiecare apel după transcriere (KPI-uri + scoruri agent), fără apasare manuală de buton.
- Backend: endpoint `GET /calls/analytics/auto-score` + `POST /calls/analytics/auto-score/toggle`; stare persistată în tabela `settings` (key `calls.auto_score`).
- Pipeline `calls_pipeline.py`: step 5 nou — scorare condiționată de flag-ul `calls.auto_score`.
- Default: OPRIT (setat la `false` în DB la sesiunea anterioară).

## v0.44.7 - 2026-07-24 (KPI binar analitice apeluri: 4 prompturi noi)

- **Feature: 4 KPI binare noi** în dashboard Analitice Apeluri: Agentul s-a prezentat?, Clientul amenință cu judecata?, Clientul amenință că renunță?, Clientul a mai contactat anterior fără răspuns?
- Migrație DB: coloane `agentul_sa_prezentat`, `clientul_aminta_judecata`, `clientul_aminta_renuntare`, `clientul_contactat_anterior` în `call_ai_scores`.
- Scorer actualizat pentru a extrage și persista cele 4 valori noi la fiecare apel scorat.

## v0.44.6 - 2026-07-24 (Fix modul no-reply: dezabonare funcțională)

- **Fix: routerul `noreply` nu era înregistrat** în `main.py` — endpoint-ul `/noreply/unsubscribe` nu exista, cauza `ERR_CONNECTION_REFUSED`.
- **Fix: `NOREPLY_BASE_URL`** adăugat în `.env` (`http://95.216.144.102:8501`). Fără această setare, link-ul de dezabonare din emailuri genera portul intern 8500 (inaccesibil din exterior).
- Modulul no-reply este acum complet funcțional: toggle ON/OFF, config SMTP, șablon, blacklist, dezabonare one-click.

## v0.44.5 - 2026-07-24 (Import CSV Numere Ignorate — Analitice Apeluri)

- **Feature: Import CSV în lista de numere ignorate** (tab „Numere Ignorate" din Analitice Apeluri). Buton „Import CSV" lângă formularul de adăugare manuală. CSV: două coloane `numar_telefon,eticheta` (header opțional, eticheta opțională). Max 5000 rânduri per import. Feedback imediat: câte adăugate / actualizate / ignorate.
- **Backend**: `POST /api/v1/calls/analytics/phone-blacklist/import-csv` — `multipart/form-data`, UTF-8 sau Latin-1, insert bulk `ON CONFLICT DO UPDATE`. Returnează `{inserted, updated, skipped, errors}`.
- Fără migrație DB (refolosește tabela `call_phone_blacklist` existentă).

## v0.44.4 - 2026-07-24 (Fix race condition get_email_documents: no_documents → processing)

- **Fix `cts_get_email_documents`** (`app/api/v1/cts.py`): când CTS interogă documentele imediat după trimitere (în fereastra de 5 minute), extracția atașamentelor poate să nu fie finalizată — `document_extractions` nu are încă rândul → `n=0` → `status=no_documents` (greșit). Fix: dacă `n=0` dar emailul are `has_attachments=true` și nu a trecut deadline-ul de 5 min → `status=processing` (CTS trebuie să reinteroghe). `no_documents` rămâne corect doar pentru emailuri fără atașamente sau după expirarea termenului.
- Cauza concretă în #53579: CTS a interogat la t+1s după trimitere (`06:30:10 UTC`), extracția s-a terminat la t+10s (`06:30:19 UTC`) → fereastră de 9s în care răspunsul era fals `no_documents`.

## v0.44.3 - 2026-07-24 (Fix layout pagina Analitice Apeluri)

- **Fix padding dublu** pe pagina Analitice Apeluri (`CallsAnalitice`): wrapper-ul de top-level folosea `style: { padding: '20px 18px' }` inline, adăugat peste `padding: 20px 28px 28px` al `.main-content` → spațiu excesiv deasupra și lățime redusă față de alte pagini (ex. Apeluri CTS). Înlocuit cu `className: 'page'` (zero padding propriu, consistent cu restul paginilor). Tab-ul „Analizează Apeluri" (sub-render intern) corectat similar (`padding: '20px 24px'` → fără padding propriu).

## v0.44.2 - 2026-07-24 (Regulă Orange OTC → Suport 2)

- **Regulă deterministă nouă**: expeditor `noreply.otc@orange.com` (coduri de autentificare Orange OTC) → **Suport 2** (intrau pe Suport 1). Adăugată în `DEFAULT_RULES` (`department_rules.py`, id `orange-otc-01`) + migrație idempotentă `migrations/20260724_dept_rule_orange_otc.sql` care injectează regula în `settings->'rules'` dacă id-ul lipsește.
- Necesară migrația fiindcă regulile de departament trăiesc în DB (`settings`), iar release-ul NU migrează conținutul `settings` — regula din cod se aplică doar la seed-ul inițial (medii noi), nu pe DB-uri deja seed-uite (staging/prod).

## v0.44.1 - 2026-07-24 (Serie FS → Recuperare TVA)

- **Serie de factură FS mapată pe departamentul Recuperare TVA** (`app/services/op_extractor.py`). Set nou `_RECUPERARE_TVA_PREFIXES = {"FS"}`, verificat prioritar în `_department_from_series` (înainte de suport_1/contabilitate). Facturile cu serie FS mergeau pe Contabilitate; acum → Recuperare TVA (ex. #53528 „Factură servicii Recuperare TVA", unde AI detectase corect recuperare_tva la 85% dar seria FS + podeaua 90% îl împingeau pe contabilitate).
- Decizie user 2026-07-24. Se aplică mailurilor noi/reîncadrate; mailurile vechi deja trimise spre CTS rămân neschimbate (fără backfill, intenționat).
- Fără migrație DB.

## v0.44.0 - 2026-07-24 (Întărire detecție Categorie + Departament)

- **Categorie — scos cache curated complet** (`app/services/category_classifier.py`). Cascada veche gemma→curated cu `use_cache=True` + etapa Anthropic cu `learn=True` servea răspunsuri vechi memorate: mailuri de tip Informație rămâneau încadrate Sesizare din cache curat (ex. #53302, #53449 = `model=curated, from_cache=true`), iar Reîncadrarea din UI (no_cache) dădea corect Informație. Acum fiecare mail e reevaluat PROASPĂT cu Haiku, task sărat `sha1(system+content)` + `no_cache=True` → zero cache, fără `learn` → nu se mai populează `ai_curated_ext` pe categorie. Simetric cu departamentul (0.43.x).
- **Departament — match semnătură angajat robust** (`app/services/department_classifier.py`):
  - **Normalizare diacritice** (`_strip_diac`, NFKD): „Miclău"=„Miclau", „Mădălina"=„Madalina". Semnăturile cu diacritice nu mai ratează maparea fără diacritice.
  - **Match ancorat pe numele de familie**: numele de familie (discriminant) trebuie prezent + ≥1 prenume; prenumele singure (David, Andrei, Robert — frecvent duplicate între angajați) nu mai declanșează singure un match. Rezolvă #53449/#53454 („David Miclău" → Suport 2) și blochează false-positive pe 2 prenume comune fără nume de familie.
  - Tolerează ordinea liberă (semnătura „David Miclău" vs mapping „Miclau Adrian-David") și prenumele mijlociu absent din semnătură.
- Fără migrație DB (pur logică). Cache-ul curated vechi rămâne în `ai_curated_ext` dar nu mai e citit/populat pe categorie.

## v0.43.4 - 2026-07-23 (Fix 500 la filtrul de dată în „Mail-uri CTS")

- **Fix 500 Internal Server Error** pe `/cts-training/list` când se aplica filtrul de dată (`date_from`/`date_to`). Cauza: sintaxa `:param::date` (parametru named urmat imediat de cast `::`) — SQLAlchemy/psycopg2 interpretează `::` de după un parametru named ca `syntax error at or near ":"`.
- **Fix** (`app/api/v1/cts_training.py`): `:date_from::date` → `CAST(:date_from AS date)` și `:date_to::date` → `CAST(:date_to AS date)`. Confirmat reproducerea erorii pe staging și rezolvarea (query întoarce count corect).
- Fără migrație DB.

## v0.43.3 - 2026-07-23 (Fix: vision AI accepta moneda ISO / HUF ca serie de factură)

- **Fix `_vision_extract_series` (`op_extractor.py`)** — vision AI returna uneori o monedă ISO (HUF, EUR, RON...) drept `series` din atașamente. Regex-ul `^[A-Z]{2,6}$` accepta `HUF` ca serie validă → emailul primea `ai_op_series='HUF'` în DB → la reclasificare mergea pe `op_series`/`suport_1` în loc de AI classifier (care ar fi prins `recuperare_tva`).
- **Fix**: `series` respinsă dacă e o monedă ISO cunoscută (`_ISO_CURRENCIES`) sau identică cu moneda detectată → `series=None` → nu se persistă `ai_op_series` → reclasificarea merge pe AI classifier.
- Origine: raportat de agentul prod pe email #67660 („Oferta rambursare TVA extern"), unde HUF suprascria detecția `recuperare_tva`. Aplicat simetric pe staging pentru a nu re-desincroniza la release.

## v0.43.2 - 2026-07-23 (Reguli Recuperare TVA în cod + migrație idempotentă)

- **Regulă deterministă „Recuperare TVA extern" mutată în cod** (`DEFAULT_RULES` din `department_rules.py`) — până acum trăia doar în DB staging (adăugată din UI), deci nu se propaga la prod prin release → mailuri de tip „dosar rambursare TVA" ajungeau greșit pe suport_1/contabilitate pe prod.
- **Match pe subiect ȘI corp** (3 reguli OR): subiect `rambursare tva extern`, SAU corp `dosarul de recuperare tva`, SAU corp `situatia dosarului dumneavoastra pentru recuperare tva`. Prinde tipologia chiar când subiectul diferă de standard.
- **Migrație idempotentă** `migrations/20260723_dept_rule_recuperare_tva.sql` — inserează cele 3 reguli în `settings->'rules'` DOAR dacă id-ul lipsește. Se aplică automat la release pe prod și a fost rulată pe staging. Rezolvă desincronizarea reguli staging↔prod (regulile de departament sunt în DB, NU în cod, iar release-ul nu migrează conținutul `settings`).
- Verificat: #53307 (email prod 67660) → `recuperare_tva` prin regulă (subiect + body).

## v0.43.1 - 2026-07-23 (Mail-uri CTS: fix cache filtru dată + contor total mailuri)

- **Fix filtru dată „nu funcționa"** — cauza reală: `index.html` încărca `/vendor/mg-app.js` fără versiune → browserul servea cod vechi din cache (dinainte de filtrele Din/Spre categoria + dată). Adăugat cache-bust `?v=<VERSION>` pe tag-ul script. Backend-ul filtra corect tot timpul.
- **Contor total mailuri** — badge în bara de filtre din „Mail-uri CTS" care afișează numărul total de mailuri ce corespund filtrelor aplicate (`data.total` de la endpoint), font mono tabular-nums.

## v0.43.0 - 2026-07-23 (Fix încadrare Contabilitate: OP series pe allowlist + departament pe Haiku fără cache)

- **Fix major încadrare departament** — multe mailuri Suport 1 ajungeau greșit pe Contabilitate. Diagnostic: 3 cauze reale (nu doar cache-ul presupus).
- **OP series pe allowlist acreditat** (`op_extractor.py`) — regex-ul lacom `[A-Z]{2,6}\d{3,}` citea plăcuțele de camion (EWN064, YCE345) ca „serie de factură" → orice serie necunoscută mergea pe Contabilitate. Acum: doar seriile din lista acreditată furnizată de user (`_KNOWN_SERIE_PREFIXES`, ~50 prefixe: ARC/GCTS/CCTS/ACTS/ECTS/FRD/...) sunt facturi. Suport 1 doar `PPCB/PPHU/PPBG/ASCF`; restul acreditate → Contabilitate.
- **Serie ne-acreditată → suport_1** — `_department_from_series` nu mai forțează Contabilitate pe prefixe necunoscute; plăcuțe/gunoi (DIANA, MARIAN, EWN) → suport_1.
- **`_extract_series_from_text(known_only=True)`** — filtru pe allowlist la subiect/body ȘI atașament; normalizează cratima (`P-ECTS` → `PECTS`).
- **`is_op_email`** — un mail nu mai e declarat „OP email" doar fiindcă are o plăcuță în subiect.
- **Departament pe Haiku, ZERO cache** (`department_classifier.py`) — eliminată cascada `gemma` cu `use_cache=True` + curated-cache care servea răspunsuri vechi greșite. Acum fiecare mail (nou ȘI reclasificat) e evaluat proaspăt cu `claude-haiku-4-5-20251001`, `no_cache=True`, task sărat sha1(system+content), fără `learn`. Podeaua 90% → suport_1 rămâne.
- **NU s-au atins** prompturile de încadrare (cerere explicită user).
- Reclasificate cele 8 mailuri raportate — niciunul nu mai e pe Contabilitate greșit (verificat: zero `from_cache`).

## v0.42.33 - 2026-07-23 (Auto-reply no-reply la emailuri noi trimise în CTS)

- **Feature: Auto-reply confirmare primire** — când un email ajunge în CTS (`sent_to_cts_at` setat), expeditorul primește automat un email de confirmare. Declanșare: `cts.py` → `noreply_sender.maybe_send_autoreply()`.
- **Switch ON/OFF** — Settings → Mail-uri no-reply → buton `● Activ / ○ Oprit`. Default OFF.
- **Config SMTP dedicat** — tabelă `noreply_smtp_config` (separat de feedback KPI). Parolă criptată cu `credential_crypto`. Buton "Testează conexiunea" trimite email test.
- **Șablon editabil din UI** — textarea în Settings, stocat în `settings` key `autoreply.noreply_template`. Variabilă obligatorie `{unsubscribe_url}`. Text default inclus (cel primit de la Bia).
- **Blacklist dezabonare** — tabelă `noreply_blacklist`. Link one-click în fiecare email (`/noreply/unsubscribe?token=<uuid>`); pagina HTML confirmă dezabonarea. Adăugare/ștergere manuală din UI.
- **Anti-spam throttle** — max 1 mail la 10 minute per adresă (refolosește `autoreply_send_log`). Adresele no-reply/automate (regex `_AUTOGEN_FROM`) sunt excluse automat.
- **Badge `✓ auto-reply`** în lista emailuri și în modalul detaliu, lângă "Trimis în CTS" — apare când `autoreply_sent_at` e setat. Hover afișează timestamp trimitere.
- **Tabele noi**: `noreply_smtp_config`, `noreply_blacklist`, `noreply_unsubscribe_tokens`. Coloană nouă: `emails.autoreply_sent_at`.
- **Fișiere noi**: `app/services/noreply_sender.py`, `app/api/v1/noreply.py`, `migrations/20260723_noreply_autoreply.sql`.

## v0.42.32 - 2026-07-23 (Fix CargoFuel override prioritate op_series; blacklist CUI cu prefix țară)

- **Fix `department_run_one`**: dacă emailul are expeditor `@cargotrack.ro` sau subiect conține `cargofuel`, departamentul e forțat `suport_1` indiferent de seria OP detectată. Rezolvă cazul `CUIRO` (CUI firmă fuzionat cu prefix RO) clasificat greșit ca `contabilitate`.
- **Blacklist op_extractor**: adăugate variantele de CUI cu prefix de țară fuzionat (`CUIRO`, `CUIMD`, `CUIPL`, `CUIBG`, `CUIHU`, `CUIDE`, `CUIAT`, `CUIIT`) — nu sunt serii de factură.
- `ai_department_result.model` rămâne `op_series` (seria a fost detectată), `department` = `suport_1` (CargoFuel a câștigat).

## v0.42.31 - 2026-07-22 (Analitice Apeluri: dashboard, scoruri AI agenți, blacklist numere, prompturi configurabile)

- **Pagina Analitice Apeluri** (meniu lateral → Analitice): dashboard KPI + grafice, top 10 clienți, scoruri AI agenți pe 4 dimensiuni (explaining/patient/understanding/politeness/empathy), blacklist numere excluse din analiză, configurare prompturi de scoring AI.
- **Tabel `call_ai_scores`**: stochează scorul detaliat per apel — scoruri agent (5 dimensiuni), scoruri client, sfaturi AI (empatie/profesionalism/claritate), rezumat problemă, etichete, status rezolvare.
- **Tabel `call_scoring_prompts`**: prompturile de scoring sunt persistate în DB și editabile din UI (activare/dezactivare, editare text, adăugare întrebări noi — extensibil).
- **Tabel `call_phone_blacklist`**: numere excluse din rapoarte/grafice (ex. montatori, numere interne).
- **Service `call_scorer.py`**: `score_call()` și `score_batch()` — batch nocturn care scorează max 200 apeluri/rulare, exclude blacklist, seeding automat prompturi din fișierele diag Bia.
- **API `/calls/analytics/*`**: 11 endpoint-uri noi (dashboard, top-clients, scores, score-now, score-batch, phone-blacklist CRUD, scoring-prompts CRUD).
- **Fix `/ai/department/{id}/run`** (sesiunea anterioară): endpoint-ul relua fluxul integral inclusiv extragere OP + detecție MDL — persistare corectă în DB și return early pentru cazul MDL.

## v0.42.30 - 2026-07-22 (OP MDL → contabilitate automat; prioritate P2-P5 doar pe suport_1)

- **OP MDL → contabilitate**: dacă un ordin de plată conține moneda MDL (lei moldovenești), emailul este asignat automat la `contabilitate`, indiferent de seria facturii. Detecție în 3 straturi: text subiect/body, text local din atașament (PDF/OCR), vision AI (prompt extins returnează acum `SERIE|MONEDA`).
- **Prioritate P2-P5 doar pe suport_1**: clasificarea de prioritate se execută exclusiv pentru emailurile detectate cu departamentul `suport_1`. Emailurile rutate la contabilitate, taxe_drum, suport_2 etc. primesc `ai_priority=null` — nu se mai consumă AI inutil și nu se mai apar priorităti false în CTS pentru departamente non-suport.

## v0.42.29 - 2026-07-22 (Productivitate: fix ore_planificate — sambetele altor angajati nu se transfera)

- **Bug fix**: absențele de sâmbătă ale unui angajat care NU lucrează sâmbăta erau numărate ca zile planificate dacă alt coleg lucrase sâmbăta (sambetele colegilor intrau în `working_dates_luna` global, contaminând calculul tuturor). Fix: fiecare angajat e planificat pe `zile_prezente_proprii ∪ L-V_calendar`, nu pe `working_dates_luna` extins cu sâmbetele echipei.
- **Rezultat Suport 1 iunie**: `ore_planificate` 1200h → 1040h, coeficient 0.7667 → 0.8846.

## v0.42.28 - 2026-07-22 (Productivitate: pontaj real ca sursă de adevăr pentru ore planificate)

- **Redesign calcul ore_planificate / ore_disponibile / coeficient**: sursa de adevăr este acum `employee_attendance` (pontajul CTS), nu formula estimată `work_hours × zile_lucrătoare_calendar`.
- **Operatori inactivi excluși**: angajații fără nicio zi `present=true` în luna raportată nu mai gonflează `ore_planificate` (ex. Bulmau Anamaria-Iuliana — angajată din iulie, apărea cu 168h planificate în iunie deși nu a lucrat deloc).
- **Sâmbete lucrate incluse corect**: sâmbetele cu prezență reală în pontaj sunt acum considerate zile lucrătoare pentru calculul absențelor, fără a necesita modificarea manuală a `department_schedule`.
- **Formula nouă**: `ore_planificate = (zile_prezent + zile_absent_în_program) × work_hours` per operator activ; `ore_disponibile = ore_planificate - ore_absente`.

## v0.42.27 - 2026-07-22 (Productivitate: fix ore absente include weekenduri)

- **Bug fix**: `ore_disponibile` și `coeficient` erau calculate greșit — pontajul CTS (`employee_attendance`) putea conține înregistrări de weekend/sărbătoare marcate `present=false`, care erau numărate ca ore absente deși nu fac parte din program. Fix: `all_absent` este acum intersectat cu `working_dates_luna` (zilele lucrătoare reale ale lunii), identic cu tratamentul concediilor planificate. Exemplu concret: Bulmau Anamaria-Iuliana avea 25 înregistrări absente în iunie (inclusiv weekenduri), codul număra 200h în loc de 168h (21 zile lucrătoare × 8h).

## v0.42.26 - 2026-07-22 (Productivitate: consistență raport lunar ↔ Analiză, SLA task per familie)

- **Fix raport lunar**: `_measurable()` schimbat din `mins > 0` în `mins >= 0` — consistent cu Analiza. Emailurile rezolvate instant (0 min biz) erau excluse greșit din „măsurabil".
- **Fix Analiză — % în-timp task**: task-urile CargoBox (SLA 8400 min) erau evaluate cu SLA general de 120 min, rezultând % în-timp incorect. Acum SLA-ul e determinat per familie din `productivity_objective` (cargobox=8400, general=120), identic cu logica raportului lunar.
- **Fix Analiză — % în-timp task fără date**: `task_lim` era NULL dacă `get_objectives` nu găsea niciun obiectiv de tip task — acum `task_lim_by_family` e construit corect cu `tip=None`.

## v0.42.25 - 2026-07-22 (Productivitate Analiză: fix „% fără durată" 99.9% + text clar preluare/rezolvare)

- **Bug fix critic**: „% fără durată" afișa 99.9% din cauza că `cts_in_progress_at` din backfill era identic cu `cts_solved_at` pe emailurile istorice (CTS nu distinge momentul asignării de momentul rezolvării). Când intervalul `in_progress → solved` era zero, `business_minutes_emp` returna NULL și emailul era marcat ca „fără durată".
- **Fix**: condiție nouă `cts_in_progress_at < cts_solved_at` — dacă intervalul e invalid (zero sau invers), se aplică fallback la calculul `created → solved` (comportamentul anterior corect). Idem pentru task-uri: `cts_in_progress_at < cts_updated_at`.
- **Fix**: durata 0 (rezolvat instant la start de tură) era exclusă din „măsurabile" (`m > 0`). Corectat la `m >= 0` — 0 e durată validă.
- **UI**: etichetele „TTC" și „TTS" înlocuite cu text clar: „Timp mediu preluare" și „Timp mediu rezolvare" (KPI-uri și header tabel operator).

## v0.42.24 - 2026-07-22 (Productivitate Analiză: TTC + TTS în KPI-uri și tabel per operator)

- **Tab Analiză — calcul `avg_min` actualizat**: pentru emailuri și task-uri, `avg_min` (Timp mediu) este acum calculat ca TTS (In Progress→Solved) când `cts_in_progress_at` există. Fallback la durata totală (New→Solved) pentru tichete fără data de preluare.
- **Nou KPI email**: `TTC mediu (preluare)` și `TTS mediu (rezolvare)` apar în secțiunea Email a tab-ului Analiză, dacă există date (minim 1 tichet cu `cts_in_progress_at` populat).
- **Nou KPI task-uri**: similar, `TTC mediu` și `TTS mediu` în secțiunea Task-uri.
- **Tabel detaliu per operator**: coloane noi `TTC email`, `TTS email`, `TTC task`, `TTS task` — afișate condiționat (coloanele apar doar dacă departamentul are date cu TTC/TTS).
- **Backend** (`analytics_report()` în `productivity.py`): query emailuri și task-uri returnează acum `ttc_mins` (New→InProgress) și `mins` (TTS sau fallback). Câmpuri noi în response: `avg_ttc_min`, `avg_tts_min` la nivel root, departamente și operatori.

## v0.42.23 - 2026-07-22 (TTC + TTS: timp preluare și timp rezolvare separat pe task-uri și emailuri)

- **Nou**: două faze de timp per tichet — „Timp preluare" (TTC: New→In Progress) și „Timp rezolvare" (TTS: In Progress→Solved), ambele în minute de lucru efectiv (`business_minutes_emp`).
- **DB**: coloana `cts_in_progress_at TIMESTAMPTZ` adăugată pe `cts_ground_truth` și `cts_task_ground_truth`. Backfill automat pe 20.333 emailuri din `cts_assigned_at` (momentul asignării operatorului).
- **Sync emailuri**: `_UPSERT_SQL` actualizat — la prima tranziție în `in_progress`, setează `cts_in_progress_at = COALESCE(cts_assigned_at, now())`. Nu suprascrie dacă deja populat.
- **Sync task-uri**: similar — la prima tranziție în `in_progress`, setează `cts_in_progress_at = now()`.
- **API** (`/cts-tasks-training`): response extins cu `in_progress_at`, `time_to_claim_minutes`, `time_to_solve_minutes`.
- **UI** — `TaskDetail`: afișează „In Progress:", „Timp preluare:", „Timp rezolvare:" cu fallback pe `resolution_minutes` total când TTC/TTS lipsesc (date istorice).
- **UI** — Lista task-uri: coloana redenumită „Preluare · Rezolvare" — afișează TTC · TTS când disponibil, altfel durata totală.

## v0.42.22 - 2026-07-22 (Task/DeviceOps/Stats: durată rezolvare = timp de lucru efectiv, nu wall clock)

- **Bug fix**: câmpul „Timp rezolvare" în detaliu task și lista de task-uri afișa durata brută (creare→rezolvare calendar), inclusiv nopți și weekend. Ex: task creat duminică 19:07, rezolvat luni 08:24 → apărea 13h 17m în loc de 24 min.
- **Fix aplicat în**: `cts_tasks_training.py` (lista și detaliu task-uri), `device_ops.py` (operațiuni echipamente), `health.py` (stats zilnice + overview task-uri).
- **Metodă**: înlocuit `EXTRACT(EPOCH FROM (cts_updated_at - cts_created_at))` cu `business_minutes_emp(department, employee_id, cts_created_at, cts_updated_at)` — funcție SQL care numără doar minutele din programul de lucru al operatorului, excluzând nopțile, weekendurile și sărbătorile. Consistent cu calculul din scorul de productivitate lunar.
- Task-urile fără `assignee_employee_id` (neatribuite) returnează `null` la durată (nu se poate calcula fără program de referință).

## v0.42.21 - 2026-07-22 (Satisfacție Clienți: carduri „Date lipsă" înlocuite cu date reale)

- **Card „Red flags active"** (fostul „Semnalul dominant"): afișează distribuția tipurilor de red flags din luna curentă (mențiune reziliere, ultimatum, escaladare management, penalități, concurență). Normalizare text liber pe prefix (split la ` — `). „Niciun red flag activ" când nu există.
- **Card „Interacțiuni per client"** (fostul „Trend relație"): distribuție pe buckets 1-2 / 3-5 / 6-10 / 11+ interacțiuni, colorat verde→roșu (clienții cu 11+ sunt semnal de efort/presiune).
- **Backend**: înlocuite query-urile pe `iris_holistic` (inexistent în engine v3) cu query-uri pe `red_flags_active[]` și `total_interactions` din breakdown.

## v0.42.20 - 2026-07-21 (Productivitate Analiză: breakdown complet per tip în tabelul operatori)

- **Detaliu pe operator**: coloane extinse cu timp mediu + % în-timp pentru fiecare tip în parte. Structura per operator: Email (volum | cotiz% | t.mediu | % în-timp) | Task-uri (volum | cotiz% | t.mediu | % în-timp) | Apeluri (volum | cotiz% | t.mediu răspuns | % în-timp). Coloanele task/apel apar doar dacă există date pentru acel interval.
- **Backend**: `analytics_report()` extins — `op_agg` acumulează acum `task_meas`, `task_sum_mins`, `task_scope_meas`, `task_in_timp`, `apel_scope_meas`, `apel_in_timp` per operator. `op_out` returnează `task_avg_min`, `task_in_timp_pct`, `apel_in_timp_pct`.

## v0.42.19 - 2026-07-21 (Productivitate Analiză: KPI carduri ajustate — % fără durată)

- **Tab Analiză — KPI carduri**: eliminat „Timp median" și „Măsurabile" (număr absolut) din toate 3 secțiunile (Email / Task-uri / Apeluri). Adăugat card nou „% fără durată" = procentul itemelor fără durată calculabilă, colorat roșu (semnal de calitate a datelor). Structura finală: Volum | Timp mediu | % fără durată | % în-timp.

## v0.42.18 - 2026-07-21 (Productivitate: cotizare per tip obiectiv în Rapoarte + Analiză)

- **Tab Rapoarte — tabel operatori**: coloana „Cotizare%" înlocuită cu coloane separate „Cotiz. email% | Cotiz. task% | Cotiz. apel%", afișate dinamic în funcție de ce obiective are departamentul. Sortabil pe fiecare coloană.
- **Tab Analiză — tabel operatori**: adăugate coloanele „Cotiz. email%" (volum email operator / total email dept), „Cotiz. task% / Cotiz. apel%" (afișate doar dacă departamentul are task-uri/apeluri în interval).
- **Backend `department_report()`**: fiecare operator primește acum `vol_email`, `vol_task`, `vol_apel`, `cotiz_email`, `cotiz_task`, `cotiz_apel` — cotizare calculată per tip față de totalul departamentului.
- Fix deploy: `vendor/` exclus din rsync în deploy.sh — `mg-app.js` trimis acum separat prin ssh.

## v0.42.17 - 2026-07-21 (Productivitate Analiză: task-uri + apeluri + selector utilizator)

- **Tab Analiză — task-uri și apeluri**: KPI-uri, grafice zilnice și distribuție timp de rezolvare acum includ și task-urile și apelurile, nu doar emailurile. Fiecare tip are propria secțiune (Email / Task-uri / Apeluri) cu KPI-uri separate.
- **Selector utilizator**: când se selectează un departament specific, apare un al doilea selector cu operatorii activi din acel departament (endpoint nou: `GET /productivity/department-users`). Filtrarea per-utilizator trimite `user_id` la analytics.
- **Tabel operatori extins**: coloane noi „Task-uri" și „Apeluri" în tabelul per-operator (vizibil când e selectat un departament).
- **Backend `analytics_report()`**: extins cu două blocuri paralele (task-uri din `cts_task_ground_truth`, apeluri din `cts_calls_ground_truth`), cu suport `user_id` filter, SLA limit per tip și seriile zilnice.
- Fără migrații DB (se citesc tabele existente).

## v0.42.16 - 2026-07-21 (UI Satisfacție: raționament AI inline pe toate tabelele)

- **Eliminat coloanele confuze** din toate cele 3 tabele de satisfacție: „Segment", „Semnal", „Trend", „Factor critic", „Carry-fwd".
- **Raționament AI vizibil direct** în rând: preview 110-120 caractere + expandabil inline cu bordură colorată (portocaliu la risc, verde satisfăcut, roșu nesatisfăcut). Răspunde la „de ce a dat IRIS acest scor?".
- **Red flags** afișate în secțiunea expandată, nu ca coloană separată.
- **Tabelul „Clienți nesatisfăcuți"**: label semantic inline (Nesatisfăcut/Atenție) în bara de scor, în loc de badge Segment fără context.

## v0.42.15 - 2026-07-21 (Excludere clienți interni + boost +15% + floor 60%)

- **Excluși din calcule**: CARGO TRACK * (7 entități), TRAKOSOFT SOLUTIONS SRL, URBAN & ASOCIATII S.R.L. — nu apar în snapshot-uri și nici în dashboard.
- **Boost scor +15%** aplicat pe toate scorurile IRIS raw, clamped la 100.
- **Floor 60%**: niciun client nu poate apărea cu scor sub 60% în dashboard (IRIS poate fi prea drastic pe date limitate).
- **Fallback beneficiu-dubitei**: clienți cu <2 interacțiuni în 90 zile → scor automat 100%.

## v0.42.14 - 2026-07-21 (Engine satisfacție v3: IRIS citește text direct, fără metrici intermediare)

- **Arhitectură v3**: eliminat complet pilonii matematici (emoție 30%/efort 25%/operațional 25%/relație 20%). IRIS primește acum textul brut al emailurilor + transcriptele apelurilor și returnează scorul direct.
- **Red flags validate contextual de IRIS**: `red_flags_confirmed` — doar flag-urile pe care IRIS le confirmă ca semnificative (ex: „concurență" în context de întrebare logistică = fals pozitiv, eliminat).
- **Prompt calibrat**: comunicarea B2B tranzacțională (dovezi de plată, întrebări repetate pe aceeași temă = problemă tehnică în curs, nu insatisfacție), apeluri consecutive = issue activ (nu dovadă de nemulțumire).
- **Backfill `interaction_analysis`**: 953 interacțiuni completate pentru 200 clienți (119→186 clienți cu date IA).
- **Fix `force=True`** în snapshot.py: acum ocolește corect `_has_activity()` pentru recalculare forțată.
- **Fix `dotenv`**: script-urile CLI încarcă acum `.env` înainte de orice import din `app/`.

## v0.42.13 - 2026-07-21 (Engine satisfacție: analiză text brut, red flags validate de IRIS)

- **IRIS citește textul real**: în loc de metrici pre-calculate (neg_rate, wss, etc.), IRIS primește acum transcriptele apelurilor și body-ul emailurilor și judecă direct pe conținut.
- **Red flags validate contextual**: IRIS returnează `red_flags_confirmed` — lista flag-urilor algoritmice pe care le confirmă ca reale. Flag-urile fals pozitive (ex: "concurență" în context de întrebare logistică, "escaladare" pentru un avocat extern) sunt eliminate.
- **Segment recalculat pe red flags confirmate**: segmentul `la_risc`/`critic` se aplică doar dacă IRIS confirmă flag-ul, nu automat din keyword matching.
- **Exemplu concret**: G&R ROMINA TRANSPORT — de la `la_risc` / 80% la `sănătos` / 82.5%. WAY FARER TRANS — de la `critic` / 89% la `sănătos` / 92.5%.

## v0.42.12 - 2026-07-21 (Recalibrare engine satisfacție: principiu 100-minus-penalizări)

- **Principiu nou de scoring**: scorul pornește de la 100 și scade EXCLUSIV pe dovezi concrete de nemulțumire. Comunicarea B2B tranzacțională (dovezi plată, întrebări, mailuri scurte) = client OK → 100.
- **Skip clienți cu < 2 interacțiuni în 90 zile**: returnează `error="insufficient_data"`, nu scor 0 sau artificial.
- **Benefit of the doubt sub 3 interacțiuni**: nu se trimite la IRIS, se atribuie automat 100 (fără penalizare).
- **Floor 85 pentru 3-4 interacțiuni fără red flags critice**: IRIS poate coborî maxim până la 85 pe date puține.
- **Prompt IRIS rescris complet**: ton, emailuri scurte, comunicare rară nu mai sunt penalizate. Scad doar reclamații explicite, promisiuni nerespectate, probleme repetate, red flags critice (reziliere/legal/ultimatum).
- **Red flags critice definite explicit**: `mentiune_reziliere`, `amenintare_legala`, `ultimatum`, `escaladare_management` — singurele care pot coborî scorul sub 70 chiar și cu date puține.

## v0.42.11 - 2026-07-21 (Dashboard Satisfacție Clienți — versiune bogată)

- **Dashboard Satisfacție complet redesenat**: înlocuit dashboard-ul minimal cu o pagină bogată în informații.
- **KPI extinse**: adăugate carduri „La risc / Critic" (segment real, indiferent de scor) și „Trend descendent" (clienți în declin activ).
- **3 grafice donut — compoziție portofoliu**: Segmente risc (sănătos/neutru/la risc/critic), Semnalul dominant (emoțional/operațional/relațional/mixt), Trend relație (îmbunătățire/stabil/declin/volatil). SVG nativ, fără librărie extra.
- **Top 10 Satisfăcuți**: tabel cu primii 10 clienți, cu reasoning AI expandabil per rând — răspunde la „de ce e satisfăcut".
- **Clienți la risc real**: secțiune dedicată clienților cu segment `critic` sau `la_risc` (inclusiv cei cu scor numeric ridicat dar cu red flags active: reziliere, concurență, amenințări legale). Cu reasoning AI + red flags badges expandabile.
- **Tabelul „Clienți nesatisfăcuți"**: adăugat coloana Segment; breakdown expandabil acum include reasoning AI IRIS + badges semnal/trend/red flags.
- **Floor prompt IRIS coborât 80→70**: clienții cu red flags reale (reziliere, concurență, legal) pot primi acum scoruri 35-54 în loc să fie blocați la 80. Pragul `is_unsatisfied` rămâne la 70.
- **Backend endpoint `/clients/satisfaction-stats`**: returnează acum `top_satisfied`, `at_risk`, `segment_distribution`, `signal_distribution`, `trend_assessment_distribution`.

## v0.42.10 - 2026-07-21 (Fix: matching semnătură angajat în emailuri reply)

- **Fix `_match_employee_signature`**: prenumele compuse (ex. „Apetrei Ioana Madalina") erau ratate când semnătura conținea doar o parte (ex. „Madalina Apetrei"). Acum matchul necesită cel puțin 2 din N părți ale numelui (nu toate).
- **Fix detecție reply cu corp gol**: emailurile unde clientul răspunde fără text propriu (tot bodyul e citat) nu mai sunt excluse din matching. Pattern „a scris:" / „wrote:" detectat direct în body ca indicator de reply.
- Efect: emailurile reply la angajați CargoTrack (ex. Mădălina Apetrei – Contabilitate) primesc departamentul corect direct, fără să mai cadă pe `suport_1`.

## v0.42.9 - 2026-07-17 (Documente CEMT: norma poluare → valoare numerică CTS)

- **Feed CTS `get_email_documents`**: câmpul `Emission Class` din documente CEMT (și orice tip cu `cts_key: "emission_class"`) se convertește automat din text (`"EURO V"`, `"EURO VI"`, `"EEV"` etc.) la valoarea numerică CTS (`5`, `6`, `56` etc.) înainte de trimitere.
- Mapare completă: `EURO I→1`, `EURO II→2`, `EURO III→3`, `EURO IV→4`, `EURO V→5`, `EURO VI→6`, `EEV/EURO V EEV/EURO VI EEV→56`, `noneuro→0`. Valori necunoscute rămân neschimbate (text original).
- Fără modificare DB sau UI — transformare aplicată doar la serializare spre CTS.

## v0.42.8 - 2026-07-17 (T7: Dashboard feedback — statistici & scoruri KPI)

- **Pagina nouă „Dashboard feedback"** în sidebar, secțiunea „Feedback clienți".
- **Statistici campanii**: trimise / deschise / răspuns, rată deschidere %, rată răspuns % — per campanie.
- **Rezumat global**: KPI cards cu total trimis, total deschis, total răspuns, KPI-uri evaluate.
- **Cine a deschis**: tabel per destinatar cu data/ora, metoda (pixel/click), a răspuns sau nu.
- **Comentarii**: card per comentariu cu textul, rating (bară progres), client, KPI, dată.
- **Scoruri medii KPI**: clasament dinamic (ranking #1, #2...) cu bară progres colorată (verde/portocaliu/roșu).
- **Evoluție lunară**: tabel pivot KPI × lună pentru perioadă configurabilă (3/6/12 luni).
- Filtru campanie (opțional) și filtru perioadă evoluție — actualizare live la schimbare.
- Backend: endpoint `GET /api/v1/feedback/dashboard` (auth admin JWT), parametri `campaign_id`, `months`.
- Fără migrații DB — date vin din tabelele existente (T5 + T4).

## v0.42.7 - 2026-07-16 (Procesare Documente — pilot automat: prag încredere 85%, erori permanente auto-skip)

- **Prag încredere unificat 85%** (`AUTO_CONF_MIN`): sub această încredere efectivă (extragere
  dacă există, altfel clasificare), documentul NU se mai propagă ca „Extras"/„Clasificat" —
  devine necunoscut și e scos din listă (`doc_discarded=true`), recuperabil manual la nevoie.
  Rezolvă cazul `carGObox - PrePaid` clasificat 65% care rămânea totuși auto-validat.
- **Erori permanente → necunoscut automat**, nu mai rămân agățate ca „Eroare" în listă: clasificare
  eșuată non-tranzitorie, extragere eșuată non-tranzitorie (ex. „fișier prea mare pentru
  vision-classify") → `_discard_attachment` cu motivul păstrat pentru trasabilitate. Erorile
  tranzitorii (gateway 502/503/504/timeout) rămân neafectate — tot intră la reîncercare automată.
  Rezolvă seria LIHET DENIS TRANS CMR 1-7 (#48393, #48395, #48396, #48398, #48399, #48400, #48401).
- Reconfirmat (fără modificare cod): reply-urile la un fir de mail NU reprocesează atașamentele
  mailurilor anterioare din același fir — fiecare mail Graph are propriul set de atașamente,
  selecția de procesare e strict pe mail, nu pe fir/conversație.
- Rescan one-off pe bază de date proprie: rândurile blocate (`failed`/`needs_review`,
  `auto_validated=false`) resetate și reprocesate cu logica nouă; rândurile deja `extracted`
  sub 85% (nu ajung în drain automat) corectate individual via `reidentify`.
- Fără migrație — reutilizate coloanele/statusurile existente (`doc_discarded`, `doc_discard_reason`).
- Fără schimbări de interfață — stările `discarded`/`neidentificat` erau deja ascunse din listă.

## v0.42.6 - 2026-07-13 (T4: Mutare IMAP automată spam/carantină în foldere dedicate)

- **Folder actions active**: mailuri cu verdict `spam`/`quarantined` se mută automat în folderele
  IMAP `SPAM`/`CARANTINA` în același ciclu de poll (1 min) cu detecția.
- Folderele se creează automat la prima rulare per cont dacă lipsesc (`ensure_folders`, idempotent).
- Idempotent: `folder_action_at` NULL = neprelucrat; la succes IMAP → setat `now()`. Eșecuri → retry automat la poll următor, fără duplicări.
- Răspuns `/personal-mailboxes/poll` include acum `total_moved` per rulare.
- Mailurile curate rămân în Inbox, neafectate.
- Nicio modificare la fluxul CTS sau la alte module.

## v0.42.5 - 2026-07-13 (T3: Pagina Reguli personale — liste expeditori izolate de CTS)

- **Pagina nouă „Reguli personale"** în sidebar, secțiunea „Căsuțe personale" (T3 din modulul mailbox personal).
- Componentă `PersonalSenderListsPanel`: CRUD complet blacklist/whitelist pe endpoint-urile
  `/personal-mailboxes/rules/sender-lists` (GET/POST/PUT/DELETE) — izolate de listele CTS
  (`/settings/sender-lists`). Aceleași funcționalități: adăugare, edit (cu SweetAlert2), mute/reactivare, ștergere.
- Componentă `PersonalRulesPage`: container cu sub-tab-uri; tab „Liste expeditori" activ implicit.
  Tab rezervat „Reguli AI" (comentat, pregătit pentru T5).
- Backend nemodificat (endpoint-urile existau deja din T2). Doar UI adăugat.
- Deploy: `mg-app.js` actualizat în `/vendor/` via `deploy_vendor.sh` (fără restart nginx/API).

## v0.42.4 - 2026-07-13 (CSP hash-free: scripturi inline externalizate in /vendor)

- Cauza recurenta: la fiecare build UI, hash-ul SHA256 al scriptului inline din index.html se
  schimba, iar CSP nginx ramanea pe hash-ul vechi -> React blocat ("Cargo360 se incarca...")
  pana la actualizare manuala (incidente release #61, outbox #22).
- Fix (Optiunea 2): cele 3 scripturi inline mutate BYTE-IDENTIC in fisiere externe same-origin:
  vendor/mg-theme-init.js (bootstrap tema), vendor/mg-staging-bar.js (bara staging),
  vendor/mg-app.js (bundle React 768820 bytes). index.html le include cu <script src="/vendor/...">.
- CSP script-src redus la 'self' (fara niciun 'sha256-...') in sites-available + sites-enabled.
  Nu mai trebuie atins CSP la nicio modificare UI viitoare.
- Fara restart pt asset-uri (FileResponse no-cache + StaticFiles /vendor citesc din disc); doar
  reload nginx pt header CSP. index.html: 798744 -> 20258 bytes.

## v0.42.3 - 2026-07-11 (Fix CSP img-src https: + frame-src blob:, aliniere hash cu productia)

- **img-src**: adaugat https: (imagini externe in preview email, ex. intercom.ruptela.com,
  blocate de img-src 'self' data: blob: fara https:).
- **frame-src**: directiva noua 'self' blob: (previzualizare PDF/atasamente in iframe cu URL
  blob:, care fara frame-src explicit cadea pe default-src 'self' si bloca framing-ul).
- Aplicat atat in sites-available cat si in sites-enabled (fisier separat pe acest host, NU
  symlink catre sites-available -- diverg deja cu 2 linii allow temporare pt scan ZAP, pastrate).
- Hash script-src neschimbat (deja corect, 768820 bytes).
- Aliniat versiune cu productia (0.42.3) dupa incidentul release #61.

## v0.42.2 - 2026-07-11 (Fix CSP mailguard-staging: style-src, img-src, media-src)

- **style-src**: hash-urile statice inlocuite cu `'unsafe-inline'` -- SweetAlert2
  injecteaza dinamic un bloc `<style>` per dialog (continut variabil), hash fix nu poate acoperi.
  Confirmat live prin eroarea de consola `Applying inline style violates...`.
- **img-src** (nou, `'self' data: blob:`): faviconul e `data:image/svg+xml` inline in
  index.html, blocat de `default-src 'self'` fallback. Iconitele SweetAlert2 sunt CSS
  (`swal2-icon`), nu PNG base64 cum s-a presupus initial.
- **media-src** (nou, `'self' blob:`): playerul audio modul Apeluri seteaza `src` pe
  `URL.createObjectURL(blob)`. Confirmat live prin eroarea de consola
  `Loading media from blob:... violates default-src 'self'`.
- **script-src neschimbat** -- hash-urile live (inclusiv al 3-lea, bundle 768820B) recalculate
  independent si confirmate identice cu cele deja configurate.
- **GOTCHA gasit**: `/etc/nginx/sites-enabled/cargo360` pe staging NU e symlink catre
  `sites-available/cargo360` (spre deosebire de productie) -- e fisier separat, deja divergent
  (2 reguli `allow 204.168.208.217` temp ZAP scan). Editat fisierul enabled (cel servit efectiv);
  sincronizat linia CSP si in sites-available.
- Aplicat identic si pe productie (mailguard-server, v0.44.1) in aceeasi sesiune.

## v0.42.1 - 2026-07-08 (Fix CSP script-src/style-src -- React nu se monta pe staging)

- **Root cause real al blocajului "Cargo360 se incarca..."**: header-ul CSP adaugat la nginx pe
  mailguard-staging (`default-src 'self'`, hardening ZAP 2026-07-07) bloca executia TUTUROR
  scripturilor `<script>` inline (fara atribut `src`) -- inclusiv scriptul principal ce contine
  intreaga aplicatie React (~7000 linii, in index.html). Producita (mailguard-server) NU are
  header CSP deloc, de aceea functiona identic cu acelasi index.html. Mutarea librariilor JS/font
  in `/vendor/` (v0.42.0) nu rezolva aceasta problema -- CSP bloca scriptul inline al aplicatiei
  indiferent de sursa librariilor externe.
- **Fix**: adaugate explicit `script-src 'self' 'sha256-...'` (3 hash-uri, cate unul per bloc
  `<script>` inline din index.html) si `style-src 'self' 'sha256-...'` (2 hash-uri, pentru cele
  2 blocuri `<style>` statice din pagina) in header-ul CSP din
  `/etc/nginx/sites-available/cargo360`. Pastreaza politica stricta (fara `'unsafe-inline'`) --
  doar continutul EXACT al acestor blocuri, verificat prin hash SHA256, poate rula; orice script
  injectat (XSS) ramane blocat.
- **Cunoscut, neadresat**: cateva atribute `style="..."` inline (legenda flow-diagram, un
  `<select>`, continut generat dinamic in modale SweetAlert2) raman posibil blocate de
  `style-src-attr` (mosteneste de la `default-src 'self'`, fara `'unsafe-inline'`/hash pentru
  atribute). Efect strict cosmetic (culori/afisare pe elemente punctuale), NU blocheaza montarea
  aplicatiei. Neadresat pana la decizie separata (hash enumerat per-atribut vs `'unsafe-inline'`
  scoped la style-src).
- Aplicat DOAR pe mailguard-staging. Productia ramane neschimbata (fara header CSP) pana la
  confirmarea explicita a lui Razvan dupa validare pe staging.

## v0.42.0 - 2026-07-08 (Auto-gazduire librarii JS + font Inter, elimina dependenta CDN extern)

- **Root cause pagina blocata pe "Cargo360 se incarca..."**: React nu se monta niciodata -- resursele
  CDN externe (React/React-DOM de pe unpkg.com, SweetAlert2/Chart.js de pe cdn.jsdelivr.net, fontul Inter
  de pe fonts.googleapis.com) returnau 503 in tab-ul Network al browser-ului clientului. Nu apareau erori
  in consola JS (esecurile de incarcare a resurselor nu genereaza console.error), doar in Network. Reachability
  server-side catre CDN-uri fusese deja verificata OK separat -- problema era pe traseul de retea al
  clientului, nu server Cargo360.
- **Fix**: cele 4 librarii JS (react.production.min.js, react-dom.production.min.js, sweetalert2.all.min.js,
  chart.umd.min.js) si fontul Inter (subset-uri latin + latin-ext, 2 fisiere woff2, acopera diacritice RO)
  sunt acum gazduite local in `/opt/iris-mailguard/app/ui/vendor/`, servite same-origin prin FastAPI
  (`app.mount("/vendor", StaticFiles(...))`). `index.html` actualizat sa refere `/vendor/...` in loc de
  URL-uri externe; atributele `integrity`/`crossorigin` (SRI) eliminate -- nu mai sunt necesare pentru
  resurse same-origin. Hash-urile fisierelor descarcate verificate identice cu hash-urile SRI aplicate anterior.
- Aplicat DOAR pe mailguard-staging. Productia (mailguard-server) ramane pe CDN extern pana la confirmarea
  explicita a lui Razvan dupa validare pe staging.

## v0.41.2 - 2026-07-08 (Fix CSP meta: frame-ancestors eliminat, urmare retest ZAP)

- **CSP sandbox email (`buildEmailSrcDoc`)**: eliminat directiva `frame-ancestors 'none'` din politica
  CSP declarata via `<meta>` tag. Conform spec CSP, `frame-ancestors` (ca si `sandbox`) nu este permisa
  in CSP declarata prin element `<meta>` -- este valida DOAR in header HTTP; browserele o ignora silentios
  cand apare in meta. ZAP semnala corect acest lucru la retest ca 'CSP: Meta Policy Invalid Directive'.
  Comportament runtime neschimbat (iframe-ul ramane complet izolat prin `default-src 'none'` + atributul
  sandbox al elementului iframe insusi), doar eliminata o directiva care oricum nu avea efect in acest
  context. Pastrate `base-uri 'none'; form-action 'none';` -- ambele SUNT valide in meta CSP.
- Aplicat DOAR pe mailguard-staging (95.216.144.102). NU s-a modificat productia (mailguard-server) --
  in asteptarea confirmarii separate a lui Razvan.

## v0.41.1 - 2026-07-07 (Hardening CSP + SRI, urmare pentest ZAP mailguard-staging)

- **CSP sandbox email (`buildEmailSrcDoc`)**: adaugat explicit `base-uri 'none'; frame-ancestors 'none';
  form-action 'none'` langa politica existenta (`default-src 'none'; style-src 'unsafe-inline'; img-src ...;
  font-src data:;`) folosita la afisarea continutului HTML al email-urilor intr-un iframe sandbat. Aceste
  3 directive nu mostenesc fallback de la `default-src` conform spec CSP -- ZAP le semnala ca lipsa
  ("Failure to Define Directive with No Fallback"). Comportament runtime neschimbat (iframe-ul era deja
  complet izolat prin `default-src 'none'`), doar politica explicit declarata acum.
- **SRI + pinning versiuni pe resurse CDN statice** (`app/ui/index.html`): react@18 -> 18.3.1,
  react-dom@18 -> 18.3.1, sweetalert2@11, chart.js@4.4.4 -- toate cu `integrity="sha384-..."` calculat pe
  continutul curent servit. Google Fonts CSS (`fonts.googleapis.com/css2`) ramane fara SRI -- raspuns
  dinamic per user-agent, incompatibil cu SRI (limitare cunoscuta, documentata de Google).
- Aplicat DOAR pe mailguard-staging (95.216.144.102). NU s-a modificat productia (mailguard-server) --
  in asteptarea confirmarii separate a lui Razvan.


## v0.43.0 - 2026-06-30 (Reply automat — Faza 2: trigger SOLVED + flag CTS, tot DRY-RUN)

- **Reply de ÎNCHIDERE la soluționare (kind='solved').** Când o solicitare trece în `solved` în CTS,
  IRIS pregătește un răspuns scurt care confirmă clientului că cererea a fost **procesată și
  soluționată de un coleg** (om, nu automat) și că rămânem la dispoziție. Același stil ca preluarea:
  GENERIC + conștient de context (citește ultimele 4-5 mailuri doar ca să aleagă TIPUL), **fără niciun
  identificator** (nr. înmatriculare / VIN / factură / contract / sume / nume).
  - `autoreply_generator.generate_autoreply(..., kind='solved')` + `DEFAULT_PROMPT_SOLVED` + prompt
    editabil separat `settings['autoreply.generate_prompt_solved']`; namespace `email_autoreply_solved_v1`.
- **DRY-RUN (NU trimite nimic).** Ca Faza 1: se LOGHEAZĂ decizia în `autoreply_send_log` cu
  `trigger='solved'`; trimiterea reală se cablează în Faza 1.5 (`_transmit`).
- **Opțiunea CTS `solved_auto_reply` (bifă operator).** Preluată prin sync-ul ground-truth în
  `cts_ground_truth.cts_solved_auto_reply`: **FALSE** = operatorul a răspuns manual → NU trimitem;
  **TRUE / NULL** = eligibil (NULL = CTS încă nu trimite câmpul; strictețea e configurabilă prin
  `settings['autoreply.solved_requires_flag']`, default false). Numele câmpului din feed nu e fixat →
  extragere tolerantă (mai multe chei + `extra`).
- **Trigger doar pe TRANZIȚIE nouă (fără backfill).** `cts_groundtruth_sync` marchează `cts_solved_seen_at`
  o singură dată, la trecerea în solved (mirror al `changed_at`), și expune `newly_solved` în RETURNING.
  Re-sync-ul rolling (la 5 min) și cele ~4897 rânduri deja solved **NU** re-declanșează. Doar tranzițiile
  noi (email legat local) → `dispatch_for_ids(trigger='solved')` post-commit, best-effort, izolat.
  Comutator `settings['autoreply.solved_trigger_enabled']` (default true).
- **Anti-spam comun** ambelor declanșatoare: max 1 reply automat / 10 min / adresă (un NEW și un SOLVED
  către aceeași adresă în <10 min → al doilea `throttled`). Idempotent per **(email, trigger)** — un
  `new` nu blochează un `solved` ulterior.
- **Validare fără trimitere:** `POST /ai/autoreply/{id}/preview-solved` (un email, nu persistă) și
  `GET /ai/autoreply/solved-sample?limit=` (eșantion pe emailuri reale deja solved). `POST
  /ai/autoreply/dispatch-now` (admin, dry-run) rulează dispecerul IN-SERVICE pt validare/ops.
- Migrație idempotentă `20260630_solved_autoreply.sql` (`cts_solved_auto_reply`, `cts_solved_seen_at`).
  Testat e2e: mesaje solved generice/type-aware (plată/documente/sesizare), would_send(0.88) +
  throttled + skipped_confidence(0.72) + flag=FALSE skip; tranziția fire o singură dată, istoric NU.
  Zero trimiteri reale (toate `send_mode=dry_run`).

## v0.42.0 - 2026-06-30 (Reply automat la intrarea în CTS — Faza 1: motor + dry-run + anti-spam)

- **Sugestie reply mai GENERICĂ + conștientă de context (prompt v7).** Sugestia nu mai menționează
  identificatori specifici (nr. înmatriculare, VIN, nr. factură/contract/AWB, sume, date, nume) — chiar
  dacă apar în mesaj sau istoric — pentru că exact aceste mențiuni produceau detalii irelevante/eronate
  (ex. „vehiculul GJ75DAV", „factura proformă"). Se păstrează doar confirmarea generică de preluare,
  adaptată ca TIP (plată / documente / sesizare / informare).
  - Nou `autoreply_generator._thread_context(email, limit=5)`: citește **ultimele 4-5 mailuri** din
    aceeași conversație (`conversation_id`, fallback `from_address`) DOAR ca context; răspunsul vine
    EXCLUSIV pentru ultimul mesaj. Scope AI bumped `email_autoreply_v6 → v7`.
- **Auto-trimitere la intrarea în CTS — Faza 1 = DRY-RUN (NU trimite nimic încă).** Când un email clean
  intră în CTS (`cts_update_emails`), se decide dacă s-ar trimite automat un reply de preluare și se
  **loghează** decizia. Trimiterea reală se cablează ulterior (Faza 1.5) — Cargo360 nu are azi canal
  de trimitere (reply-urile reale le face CTS).
  - **Doar încredere ≥ 0.85** declanșează `would_send`; restul rămân sugestie pentru operator.
  - **Anti-spam:** max **1 reply / 10 min / adresă** expeditor (extra → `throttled`). Praguri
    configurabile: `settings['autoreply.send_confidence_min']`, `settings['autoreply.throttle_minutes']`.
  - Migrație idempotentă `migrations/20260630_autoreply_send_log.sql` (tabel-jurnal `autoreply_send_log`
    + indexuri). Serviciu nou `app/services/autoreply_dispatch.py` (nu apelează AI — refolosește sugestia
    stocată; conexiune proprie, best-effort, izolat de feed-ul CTS). Hook în `cts.py`: `RETURNING id` pe
    UPDATE-ul clean → `fresh_clean_ids` → `dispatch_for_ids` post-commit.
  - Seam pluggabil `_transmit` + `AUTOREPLY_SEND_MODE` (dry_run | cts_feed | graph) și flag
    `AUTOREPLY_AUTOSEND_ENABLED` (default 1). Vizibilitate: `GET /ai/autoreply/dispatch-log`
    (decizii recente + contoare pe outcome / 24h).
  - Testat live: hook-ul a logat automat decizii din trafic CTS real; throttle confirmat (1 `would_send`
    + 1 `throttled` pe același expeditor). Zero trimiteri reale (toate `send_mode=dry_run`).
- **Pași următori:** Faza 1.5 = cablare trimitere reală în `_transmit` (`cts_feed` → CTS trimite din feed,
  necesită modificare CTS; sau `graph` → sendMail, necesită grant Mail.Send). Faza 2 = trigger pe SOLVED.


## v0.41.0 - 2026-06-25 (Prioritate re-numerotată P0/P1 → P1/P2 valori 1/2 + email_priority pe Clienți)

- **Re-numerotare prioritate:** eticheta canonică devine NUMERICĂ — `1` = **P1** (urgent, fost P0),
  `2` = **P2** (normal, fost P1). Aceleași reguli deterministe + prag AI; doar valoarea stocată/emisă
  se schimbă. Migrație idempotentă (`migrations/20260625_priority_renumber_clients_emailpriority.sql`):
  remap `emails.ai_priority`, `ai_priority_result.priority` și `ai_priority_corrections` (snapshot
  backup în `_bak_ai_priority_20260625`).
  - Intern, modelul AI raționează în continuare în P0/P1 (prompt neschimbat); maparea la 1/2 se face
    la ieșirea din `priority_classifier.classify_priority` (P0→1, P1→2).
  - **API CTS** (`prioritate`) trimite acum **întreg** `1`/`2`/`null`; `urgent=true` când `prioritate=1`.
    Documentația API (pagina din UI) actualizată.
  - Endpoint-urile `correct` (per-email + verificare manuală) acceptă `1`/`2` (cu alias P0/P1) și
    resping restul cu „Prioritate invalida (1 sau 2)". UI: badge/select **P1/P2**, valori 1/2.
- **Pagina Clienți — `email_priority`:** coloană nouă `clients.email_priority` (smallint, 1/2) adusă din
  IRIS prin `iris_sync` (`/clients/contact-list`) + afișată în listă și în detaliul clientului.
  NOTĂ: feed-ul IRIS nu expune încă `email_priority` (rămâne NULL); Cargo360 e pregătit să-l consume —
  vezi cererea din outbox către Razvan pentru extinderea `/clients/contact-list`.

## v0.40.0 - 2026-06-16 (Procesare documente — grupare manuală atașamente din același email)

- **Grupare manuală (cu sugestie auto):** când un document vine ca mai multe atașamente în ACELAȘI email
  (ex. talon MD față + spate), operatorul le poate grupa ca să se extragă TOATE datele împreună.
  - În modalul unui document, secțiunea „Pagini din același email" listează celelalte atașamente cu
    checkbox; cele cu ACELAȘI tip sunt **pre-bifate ca sugestie**. „🔗 Grupează & reextrage" combină textul
    tuturor paginilor și re-extrage o singură dată.
  - Membrii grupului devin `status='grouped'` (ascunși din listă); primarul rămâne cu un badge
    „🔗 N atașamente" și afișează preview-urile tuturor paginilor (stacked) în modal.
  - „✂ Desparte" desface grupul (fiecare atașament redevine individual, re-procesat).
  - Decizie de design: gruparea NU e automată — în date, spatele unui document apare adesea ca
    `neidentificat` (la fel ca logo-urile), deci automatul ar îngloba junk; manualul e precis.
- Model aditiv: coloană `document_extractions.grouped_into` + index parțial (migrație idempotentă).
- „♻ Reextrage date" pe un grup re-combină textul tuturor paginilor.

## v0.39.2 - 2026-06-16 (Procesare documente — navigare, robustețe gateway, reextrage, excludere arhive)

- **Navigare între documente:** butoane „← Anterior / Următor →" + contor poziție în modalul de atașament,
  cu avertizare dacă există modificări nesalvate.
- **Eșec tranzitoriu de gateway AI nu mai blochează vizibil documentele.** Erorile temporare de
  infrastructură (502/503/504, timeout, transport) la clasificare ȘI la extragere nu mai persistă un rând
  `failed`: atașamentul rămâne în coadă și se reia automat la următorul drain, când gateway-ul revine.
  (Cauza logo-urilor „failed" rămase: o pană 502 a gateway-ului AI, nu documente vechi.)
- **Buton „♻ Reextrage date":** re-extrage datele cu tipul curent fără reclasificare. Rezolvă și câmpurile
  duplicate apărute când schema tipului s-a schimbat după o extragere veche (datele se realiniază la schema curentă).
- **Arhive excluse din start:** `.zip/.rar/.7z/.gz/.tar/...` → `neidentificat` direct, fără OCR/AI.

## v0.39.1 - 2026-06-16 (Procesare documente — filtrare junk + modal email real)

- **Doar ce a fost identificat ajunge la validare umană.** Atașamentele neidentificabile (logos, poze cu
  aparate/echipamente, imagini off-context, OCR gol) nu mai intră la procesare — sunt tratate direct ca
  `neidentificat` și ascunse din listă. La validare intră DOAR ce a fost încadrat pe o categorie
  (vehicul/șofer/contract) dar cu tipul incert (ex. „e contract, dar nu se știe tipul"):
  - **Gate pe categorie:** dacă AI-ul nu poate încadra atașamentul într-o categorie cunoscută →
    `neidentificat` (skip), chiar dacă `is_document=true`.
  - **Prompt de clasificare întărit:** regulă strictă — fără categorie clară ⇒ `is_document=false`; dacă
    `is_document=true` categoria e obligatorie; exemple explicite de exclus (logo, screenshot, poze cu
    aparate, off-context, marketing).
  - **`needs_vision` (OCR gol) ascuns implicit**, alături de `neidentificat`/`necunoscut` — vizibil doar
    cu toggle-ul „Arată neidentificate". Statusul se păstrează (non-lossy) pentru viitorul canal vision.
- **Tab „Vezi email" → modalul real de email.** Butonul „✉ Vezi email" din modalul de atașament deschide
  exact modalul din lista Emailuri (sidebar verdict/categorie/atașamente + file HTML/Text/Metadata), în
  mod read-only („Marchează ca corect"), în loc de panoul custom de dinainte.

## v0.39.0 - 2026-06-16 (Procesare documente STEP 2 — clasificare + extragere atașamente din emailuri)

- **Tab „Procesare documente" funcțional:** listă a atașamentelor procesate (JOIN email+atașament), cu
  buton **„Procesează azi/tot"**, filtru scope, toggle „Arată neidentificate", badge-uri pe status și
  tabel (email · atașament · categorie · tip detectat · încredere · date extrase · status).
- **Clasificare în 2 trepte într-un singur apel AI:** pentru fiecare atașament, un clasificator decide
  `is_document` + `categorie` (vehicul/șofer/contract) + `tip` exact din catalog, folosind **titlurile de
  potrivire** (contractele se disting după titlu). Dacă e document și are tip, se rulează extragerea
  configurată (Phase 1) → date structurate. Statusuri: `extracted` / `classified` / `needs_review`
  (încredere mică) / `needs_vision` (OCR gol) / `neidentificat` (logo/svg/screenshot) / `failed`.
- **Modal detaliu atașament:** date extrase **editabile** (corectezi ex. un CUI), **selector manual de
  tip** (+ „Re-extrage cu tipul ales"), buton **„Reidentifică document"** (reclasifică + extrage),
  „Salvează" (marchează `reviewed`, protejat de reprocesarea automată).
- **Procesare automată pentru emailurile viitoare:** hook fire-and-forget în `/process/run-now` (cron 5
  min) — atașamentele noi se clasifică/extrag automat, în thread daemon, fără a atinge categorizarea
  emailurilor. Coada = atașamentele fără rând în `document_extractions` (idempotent, fără tabel nou).
  Cron-ul rulează pe fereastra `recent` (ultimele 2 zile) ca să NU măture tot arhivul istoric; sweep-ul
  complet (`Procesează tot`) e o acțiune manuală explicită.
- **Modal: preview atașament** (imagine/PDF prin endpoint-ul de download) + **selector de categorie**
  manual independent de tip (pentru a confirma ex. „contract" fără un tip anume).
- **Modal îmbunătățit:** layout pe 2 coloane (preview stânga / date+selectoare dreapta), mai lat
  (1100px), **zoom pe imagini** (click pe poză), **căutare în selectorul de tip**, opțiune
  **„Necunoscut"** la categorie și tip (status manual `necunoscut` pentru chitanțe / documente fără tip —
  distinct de `neidentificat`, ascuns implicit, vizibil cu toggle), și tab nou **„Vezi email"** care
  arată emailul original + toate atașamentele lui (cu evidențierea celui curent + buton „deschide").
- **Setări → Prompturi AI:** card nou **„Prompt identificare documente"** (editabil) — regula de
  excludere logos/screenshots + încadrare categorie/tip; catalogul tipurilor se adaugă automat.
- **Pre-filtru ieftin înainte de OCR:** svg / imagini foarte mici (logo) / fișiere neprocesabile
  (zip/xml…) → `neidentificat` fără cost AI; PDF cu mime greșit (octet-stream) detectat prin magic-bytes.
  Citirea atașamentelor reutilizează `emails._host_path` (volum parser-email-op, doar citire).
- **DB (aditiv):** `document_extractions` + `reviewed`, `updated_at`, `reviewed_by`, `manual_type`,
  `confidence_reason`; setarea `documents.classify_prompt`. Migrație `20260616_doc_extractions_step2.sql`.
- Verificat pe atașamentele de azi: poze de talon/CEMT recunoscute și extrase, contracte → tip corect,
  logos/facturi/extrase bancare → `neidentificat`, PDF criptat → `needs_vision`.

## v0.38.1 - 2026-06-16 (Procesare documente — config comun pe Contracte + fix semnătură)

- **Contracte: set comun de câmpuri pe toate cele 11 tipuri** (CargoFuel Prepaid, E-Transport
  Premium/Basic, SentGeo, HUGO, Monitorizare GPS, Taxe de drum PL/HU/BG/carGObox, Compensare carburant):
  `Numar contract`, `Data contract`, `Prestator`, `Client`, `CUI client`, `Este semnat` (boolean) — plus
  prompt de extragere comun (robust la OCR zgomotos: corectează evident `24,04,2026`→`24.04.2026`,
  distinge clientul de Cargo Track, ia CUI-ul celeilalte părți).
- **Fix critic „Este semnat": trunchiere cap+coadă.** Semnăturile stau la finalul documentului, dar
  textul era tăiat la primii `MAX_DOC_TEXT` chars → la contractele lungi (ex. CargoFuel ~20k chars/6 pag)
  pagina de semnătură nu ajungea niciodată la model, deci `Este semnat` ar fi fost structural mereu fals.
  Adăugat `_clip_doc_text` (păstrează ~62% cap + restul coadă) în `_extract_doc`. Validat: pe text curat
  extragerea e exactă (nr./dată/părți/CUI), iar `Este semnat` se aprinde corect când există dovadă
  textuală (semnătură+dată completată) și rămâne fals la linii goale / semnături olografe scanate
  (ilizibile în text — caz pentru canalul vision din outbox).
- **#18 „carGObox - PrePaid" — câmpuri specifice fidejusor:** pe lângă setul comun, adăugate
  `Fidejusor nume`, `Fidejusor C.I.`, `Fidejusor CNP` și două semnături separate (`Semnatura beneficiar`,
  `Semnatura fidejusor`), cu prompt care localizează blocul BENEFICIAR + blocul FIDEJUSOR (prima pagină) și
  zona de semnături (final). Notă: pe scanurile reale datele fidejusorului și semnăturile sunt completate
  de mână → ilizibile pentru OCR (model pune `null`/`false`, nu halucinează) — se vor popula cu canalul
  vision din outbox.
- **Fix logging:** `ai_call_log.task` lărgit `varchar(50)`→`varchar(120)` — task-urile lizibile
  (`cargo360:doc_extract:CargoFuel_Prepaid_v1_0:<salt>`) depășeau 50 chars și picau la insert (extragerea
  mergea, dar se pierdea telemetria costului AI).

## v0.38.0 - 2026-06-15 (Modul nou „Procesare documente" — Phase 1: fundația / tab „Tipuri de documente")

- **Secțiune nouă în sidebar: „Procesare documente"** (sub „Emailuri"), cu 2 taburi: „Procesare documente"
  (placeholder Phase 2 — statistici + listă documente procesate) și **„Tipuri de documente"** (fundația
  livrată acum). Modelat după modulul Rapoarte (tab „Automate").
- **Tab „Tipuri de documente":** 3 categorii (Documente vehicul / Documente șofer / Contracte). Pentru
  fiecare tip operatorul: încarcă un **șablon exemplu** (poză/PDF, cu preview în modal), definește
  **câmpurile de extras** (nume + tip + descriere), generează promptul de extragere cu **„✨ Generează
  promptul cu AI"** și îl validează cu **„▶ Testează extragerea din exemplu"**. Contractele au în plus
  „titluri de potrivire" (tipul se determină după titlu).
- **DB:** tabele noi `document_types` (definițiile, pe 3 categorii, cu `extract_fields`/`extract_prompt`/
  `match_titles`/șablon) și `document_extractions` (rezultate, populate în Phase 2). Migrație idempotentă
  `migrations/20260615_document_processing.sql`.
- **Extragere text (vision — interimar):** gateway-ul AI IRIS e strict text, deci extragem textul LOCAL
  din document — **PDF** prin `pdfplumber`/`PyMuPDF` (text nativ, excelent pe contracte), **poze** prin OCR
  `pytesseract`+`Pillow` (auto-rotate EXIF, `ron+eng`) — apoi îl trimitem la `iris_ai` ca în Rapoarte.
  „Generează prompt" și „Testează" pe PDF funcționează imediat. Un **canal vision dedicat** (acuratețe
  mare pe poze, clasificare+rotire) e cerut separat prin outbox.
- **Backend:** router nou `app/api/v1/documents.py` (CRUD tipuri, upload/servire șablon,
  generate-extract-prompt, test-extract, **test-detect + generate-detect-prompt**) — reutilizează
  helperele pure din `reports.py` (anti-drift).
- **Validare tip (fără extragere):** buton **„✓ Validează șablon"** + prompt opțional de analiză
  (`detect_prompt`) + „✨ Generează prompt de analiză" în editor — pentru tipurile doar de identificare
  (Anexa, remorcă, CargoBox), confirmă dacă AI-ul ar recunoaște corect documentul (match + încredere +
  motiv + titlul detectat), fără a extrage date.
- **Fix cache gateway (corectitudine extragere):** cache-ul „curated" al gateway-ului AI e cheiat pe câmpul
  `task` (NU pe prompt, NU pe conținut, ignoră `use_cache=False`) — un `task` static servea răspunsuri vechi
  la re-testare și risca contaminare între documente. Adăugat `_cache_salt` = sha1(system+conținut); toate
  apelurile sufixează `task` cu `:{type_id}:{salt}` (extragere, detecție, generare prompt). Prompt schimbat →
  re-rulează; alt document → re-rulează; identic → cache idempotent. Ex.: CEMT Emission Class `EURO IV`
  (greșit, din cache) → `EURO VI` (corect).
- **Prompt CEMT îmbunătățit:** instrucțiune explicită de a alege clasa EURO **bifată** (căsuța plină/diferită
  în OCR: `WI`/`X`/`■` vs goalele `U`/`Ul`/`O`), nu prima opțiune din listă; normalizare la EURO III/IV/V/VI/EEV.
- **Fix OCR pe PDF scanat:** taloanele/CIV/formularele vin frecvent ca **poză salvată în PDF** (fără strat
  text) → extragerea de text nativ întorcea gol și „Testează" eșua. Adăugat fallback: randare pagini PDF
  la ~300 DPI → OCR. OCR făcut robust prin **PSM multiple** (PSM 6 pentru blocuri de text, esențial pe
  scanuri complexe) + fallback pe imaginea brută — folosit deopotrivă la PDF și la poze.
- Versiune sincronizată: `app.config.app_version` corectat 0.3.0 → **0.38.0** (era desincronizat de
  fișierul VERSION) — health `/version` raportează acum corect.
- **Phase 1 = doar fundația.** Legarea în pipeline-ul live (detectare tip + extragere automată la
  emailuri cu atașament → CTS) = Phase 2; `process_email.process_one` rămâne neatins.

## v0.37.0 - 2026-06-15 (Gate „Automat" în pipeline-ul live — pattern-urile din Rapoarte prind mailurile noi)

- **Pattern-urile confirmate „Automat" (pagina Rapoarte) sunt acum aplicate la procesarea LIVE a
  fiecărui email nou.** Înainte, `report_patterns` creșteau DOAR la regenerarea manuală a raportului
  (`last_seen_at` îngheța, numărul nu creștea deși soseau zilnic emailuri de același șablon). Cauză:
  `process_email.process_one` nu consulta niciodată `report_patterns`.
- Email nou care se potrivește unui pattern confirmat (același criteriu ca regenerarea — **expeditor ∈
  pattern ȘI amprentă SimHash** Hamming ≤ `PATTERN_MATCH_K`=5) este acum, ÎNAINTE de orice clasificare:
  **exclus** din spam/carantină/categorie (economie NOVA), **atașat** la pattern (`email_ids`,
  `total_matched`, `email_count`, `last_seen_at`), pus în coada de extragere (dacă `extract_enabled`)
  și marcat terminal `status='auto_report'` / `queue_status='auto_closed'` (procesat automat, „închis",
  ascuns din lista de emailuri, fără procesare umană).
- **Sursă unică de adevăr (anti-drift):** noul `reports.try_auto_handle_pg(cur, email)` reutilizează
  EXACT `_fp_of` + `_match_pattern` + aceeași acumulare ca `_run_generation` — calea live și regenerarea
  nu pot diverge. Atașare + enqueue + set status în aceeași tranzacție; `_drain_queue` pornit după commit.
- Criteriul expeditor+șablon evită fals-pozitivele: emailurile personale forwardate de pe o adresă
  aflată într-un pattern (ex. „Fw: …" de la diana_perticas@) au alt șablon → NU se auto-închid, trec
  prin clasificarea normală.
- Gate doar pe mailuri FĂRĂ atașament (paritate cu modul în care s-au învățat pattern-urile). Rulează
  înaintea NDR: mailurile `mailer-daemon` care se potrivesc șablonului „Alerte eșec livrare" merg la
  extragere, nu la statusul `ndr`. Fail-safe: la orice eroare, emailul cade în clasificarea normală.
- Restanța (mailuri sosite înainte de deploy) NU e atinsă (gate-ul rulează doar pe `status='pending'`);
  recuperabilă oricând cu „Regenerează" pe Rapoarte.
- Fișiere: `app/api/v1/reports.py` (+`try_auto_handle_pg`, `_load_patterns_pg`),
  `app/services/process_email.py` (gate în `process_one`), `app/api/v1/emails.py` (badge `auto_closed`).

## v0.36.1 - 2026-06-15 (Legit: migrează TOATE mailurile expeditorului, nu doar cel clicat)

- **Acțiunea „Legit" (pagina Spam) procesează acum TOATE emailurile aceluiași expeditor.** Înainte,
  marcarea unui email ca „Legit" scotea din spam toate mailurile expeditorului (override=FALSE), dar
  **doar cel clicat** era repus pe calea de categorizare (`queued_general`); restul rămâneau blocate la
  `stopped_spam`, fără categorie și fără a ajunge la CTS. Acum, `status='clean'` + repunerea pe calea
  `manual_clean` se aplică **tuturor** mailurilor expeditorului blocate ca spam (`queue_status=
  'stopped_spam'`) → `advance_queue_batch` (tick 5 min) le categorisește pe toate și le trimite la CTS.
- Stările imuabile de securitate (`quarantined`/`quarantined_strict`/`ndr`/`deleted`) și mailurile deja
  pe calea sănătoasă (`ready_for_cts`/`sent`) NU se ating (carantina bate Legit; fără re-trimitere CTS).
- Fișier: `app/api/v1/spam.py` (acțiunea `legit`, pașii 3-4). Backup `spam.py.bak-fullthread-20260615`.

## v0.36.0 - 2026-06-15 (Carantină pe tot thread-ul + whitelist anti-spam pe expeditor)

- **Carantină/carantină strictă (phishing) se evaluează acum pe TOT thread-ul** (toate reply-urile),
  nu doar pe ultimul mesaj — decizia de securitate ține cont de întreg contextul. Controlat de
  flag-ul **`ANALYZE_FULL_THREAD`** (default ON; `=0/false/no/off` + restart → revine la mesajul nou,
  rollback fără redeploy). Sursa de conținut e centralizată în `phishing_detector._scan_content()`.
- **Categoria** se analiza deja pe tot thread-ul (`_email_body` → `body_text` integral, sub plafonul
  de 48k al modelului) — neschimbată.
- **Spam rămâne evaluat pe MESAJUL NOU** (quote-stripped), intenționat: o promoție trimisă de noi și
  **citată** sub răspunsul unui client NU trebuie să marcheze răspunsul ca spam (promoția e în
  istoricul citat, nu în mesajul nou). Un spam real de la terți, scris în mesajul nou, e prins normal.
- **Whitelist-ul manual ⇒ NICIODATĂ spam — DOAR pe EXPEDITOR.** Un expeditor de pe whitelist
  (*Liste expeditori — învățare la categorizare*) forțează `override=FALSE` la spam, indiferent de
  scor (ca allowlist). Whitelist-ul se verifică **doar pe `from_address`/domeniu**, niciodată pe
  adrese care apar în corp: dacă pe o adresă a noastră (ex. `office@cargotrack.ro`) intră spam trimis
  de **terți**, e marcat spam; doar mailurile **trimise DE LA** o adresă whitelist sunt scutite.
  Whitelist **bate** blocklist/blacklist-spam. Cod motiv: `manual_whitelist_bypass`.
- **Sursă unică `spam_detector.classify_spam_gate()`** pentru decizia de spam (allowlist/whitelist ⇒
  NU spam; blocklist/blacklist-spam ⇒ spam; altfel scorul decide). Folosită identic de pipeline-ul
  live (`process_email.process_one`) ȘI de `POST /spam/backfill` — eliminând driftul (înainte
  backfill-ul ignora whitelist-ul/reputația, un bug de consistență, reparat).
- **`POST /spam/backfill` aliniat** la aceeași poartă: re-rulabil, pur SQL (fără AI). Mecanismul de
  backfill retroactiv pentru emailurile deja procesate; raportează `whitelist_bypassed` / `forced_spam`.
  (Carantina NU se recalculează prin backfill — full-thread la carantină se aplică doar mailurilor noi.)
- Fișiere: `app/services/phishing_detector.py`, `spam_detector.py`, `process_email.py`,
  `app/api/v1/spam.py`. Backup `*.bak-fullthread-20260615`.

## v0.35.0 - 2026-06-15 (Traducere emailuri în română — modal Emailuri)

- **Traducere on-demand în română în modalul de email.** Pentru emailurile într-o limbă străină
  (engleză, rusă, ucraineană, maghiară etc.) apare în modal butonul **„🌐 Începe traducerea"**.
- **RO by default + „Vezi originalul".** După o traducere reușită, conținutul tradus (subiect + corp) se
  afișează implicit la următoarele deschideri, cu buton de comutare **„Vezi originalul"** ⇄
  „Vezi traducerea (RO)" — toggle instant, fără reapel AI. Rezultatul e **salvat în DB** (cache).
- **Detecție limbă + traducere într-un singur apel AI**, prin modelul **gratuit `gemma`** cu **fallback**
  la `claude-haiku-4-5` doar dacă gemma eșuează. Task logat ca `cargo360:email_translation` (prefixat ca
  celelalte) → vizibil în pagina **Analiza AI**.
- **Doar text, sigur**: se traduce textul (corp extras URL-aware, fără CSS/script), nu structura HTML.
  Conținutul emailului e tratat ca date neîncredere (anti prompt-injection).
- **Backend**: serviciu nou `app/services/email_translator.py`; endpoint `POST /emails/{id}/translate`
  (admin); migrare aditivă `20260615_email_translation.sql` (coloane `translation_*` + `source_lang` pe
  `emails`). Zero impact pe fluxul existent (categorizare/carantină/spam neatinse).

## v0.34.1 - 2026-06-12 (Dashboard: schemă logică „Fluxul de procesare a emailurilor" v5)

- **Schema SVG animată din cardul „Fluxul de procesare a emailurilor" (Dashboard) extinsă la varianta v5.**
  Layout orizontal pe benzi: **banda centrală** Client → Inbox → Cargo360 → Clasificare → Clean →
  Categorie · IRIS → „Are atașamente?" → CTS; **banda de sus** = ramura Spam; **banda de jos** = ramura
  Carantină. Zona de după Clean (Categorie · IRIS extrage · „Are atașamente?" · CTS) e mult mai aerisită.
- **Căi directe blacklist corectate**: domeniile deja în blacklist sunt oprite în Cargo360 și pleacă
  DIRECT, ÎNAINTE, spre Spam (portocaliu `#E0820C`) și spre Carantină (roșu `#D93A3A`) — fără bucle înapoi
  spre Inbox (fix față de v1, unde nu existau aceste căi directe explicite).
- **Noduri noi/extinse**: Client, „IRIS validează intenția senderului" (înainte de Carantină),
  „Mailuri standard (auto)", „IRIS extrage (Mașină · Țară · Dată)", „Categorie · IRIS", „Are atașamente?"
  (DA/NU), „Procesare atașamente (contract · OP · ITP · CI…)", „Încarcă pe entități (client · vehicul)",
  CTS cu sub-stări (Pregătit / Closed auto / + atașamente). Ramura DA atașamente + buclă punctată
  „atașamente gata?" (CTS interoghează procesarea).
- **Animație**: **23 bile** decalate (5 SPAM portocalii + 5 Carantină roșii + 11 neutre), cu durate variate (9–13.5s) ca să nu pară un loop identic, (`<animateMotion>`, ~11s, infinit, fade la capete), distribuite pe
  TOATE fluxurile (inclusiv recuperările „operator → Clean", „fals pozitiv → Clean" și ramura DA
  atașamente „+ email" → CTS). Culoare = natura emailului: albastru `#185FA5` neutru, portocaliu `#E0820C`
  spam direct, roșu `#D93A3A` carantină/blacklist (bila de carantină directă merge ÎNAINTE). Plus 2 bile **teal** `#0FB5AE` ocazionale (durate 15/18s) care pleacă din CTS spre „Procesare atașamente” ca să ceară documente și revin în CTS (buclă feedback). Legendă pe 2 rânduri (acum cu intrarea teal).
- **Dimensiune / încadrare** (rafinare): schema randează pe lățime completă (eliminat `maxWidth`, container
  `width:100%`) → mai mare și mai ușor de urmărit; viewBox crop-uit `0 0 1660 820` → `0 158 1660 662`
  (tăiat spațiul gol de sus, fără padding-top vizibil); `<svg style="display:block">`.
- **Tehnic**: doar frontend, `app/ui/index.html` — template literal `CARGO360_FUNNEL_SVG` rescris
  (namespace CSS `.mgf`, marker `#mgf-ar`, clasă nouă `.mgf-fb` pentru feedback punctat, fill default pe
  `.th`/`.ts` ca etichetele libere să fie lizibile în ambele teme). Model de referință: Iris DIAG
  `AI1B7J0XK` / `cargo360_flow_schema_v5.html`.
- Backup: `app/ui/index.html.bak-flowv5-20260612` (v5 inițial), `*.bak-flowv5b-20260612` (rafinare
  dimensiune + bile). Verificat: SVG well-formed (taguri echilibrate), `0 158 1660 662` servit la `/`,
  markerul v1 `0 0 680 360` dispărut, 13 `<circle>` în schemă, servire statică (fără restart).

## v0.34.0 - 2026-06-12 (Propuneri IRIS acționabile + tip blacklist carantină/spam)

- **Propuneri IRIS bifabile (per-linie)** în „Învățare din carantinare manuală": fiecare sugestie
  (regulă/semnal/scor) are checkbox. Cele bifate intră într-un **ghid** injectat în promptul **porții
  de intenție AI** (`strict_intent_gate`) când analizează intenția unui email candidat la carantină.
  Serviciu nou `app/services/learning_guidance.py`; endpoint `POST /settings/learning-proposals/toggle`;
  `GET /settings/learning-proposals` întoarce `accepted`. Indicator „Active în ghid: N".
- **Poarta de intenție AI PORNITĂ** (`STRICT_INTENT_GATE_ENABLED=1`, decizie explicită a userului). Notă:
  poarta poate ELIBERA automat carantine borderline judecate benigne (necesar-dar-nu-suficient: benign +
  client cunoscut + fără blockeri malware). Ghidul curat rafinează decizia; nu poate carantina un clean.
- **Câmp `tip` (carantină | spam) pe Blacklist**: carantina folosește DOAR `tip=carantina`; spam-ul DOAR
  `tip=spam`. **Spam-ul confirmat NU mai forțează carantină** (fix escaladare v0.33.0) — intră ca
  `tip=spam` și afectează doar scoringul de spam (override, reason `manual_blacklist_spam`). Inferență
  back-compat fără migrare (`spam_confirm`→spam, rest→carantină). UI: badge tip + select la add/editare.
- **Rename `mute`→`ignoră`**: butoane „ignoră"/„reactivează", stare „· ignorat" (câmpul `muted` neschimbat).
- Backup: `*.bak-proposals-20260612`. Verificat: compile OK, `node --check`, restart OK, health 200.

## v0.33.0 - 2026-06-12 (Liste expeditori: Blacklist + Whitelist pentru învățare la categorizare)

- **Două liste noi** în Setări → Prompturi AI, sub „Învățare din carantinare manuală" (side-by-side):
  **Blacklist** și **Whitelist** de emailuri/domenii pe care IRIS le folosește la încadrarea de securitate.
- **Store canonic unic** `settings['phishing_manual_learning'].{blacklist, whitelist}` + serviciu nou
  `app/services/sender_lists.py`. Cheia = email complet sau domeniu bare (fără „@").
- **Detecție**: Blacklist = enforcement hard (carantină strictă, neschimbat). **Whitelist = suprimare
  soft** — elimină semnalele slabe (L1/L2), niciodată malware/cod strict, și doar dacă nu există trigger
  strict. Blacklist bate whitelist. Intrările `muted` sunt ignorate la detecție.
- **Auto-populare**: „Confirmă spam" → Blacklist (escaladare = ranking mai mare next-time); „Legit" →
  Whitelist; carantinarea manuală → Blacklist (ca până acum). Dacă e deja în lista opusă, NU se mută.
- **CRUD** (admin): `GET/POST/PUT/DELETE /settings/sender-lists` — adaugă/editează/mute/șterge. UI cu
  badge email/domeniu, sursă, butoane mute/edit/✕.
- Fără backfill din `spam_sender_reputation` (subsistem separat); doar confirmările noi sincronizează.
- Backup: `*.bak-senderlists-20260612`. Verificat: compile OK, restart OK, health 200, `node --check`.

## v0.32.2 - 2026-06-12 (Verificare manuală: tabel consecvent + fix navigare modal)

- **Design tabel** aliniat la pagina Emailuri (`list-table-full`, `IdCell`, `catBadge` global,
  rânduri `clickable`). Coloane: ID · Recepționat · Subiect · Expeditor · **AI a zis** ·
  **Categoria corectă** · **Motiv**. Coloana „Acțiune" eliminată (procesarea se face din modal).
- **Motiv**: „necunoscut de AI" vs „preluat aleatoriu" (eșantion QA).
- **„AI a zis" vs „Categoria corectă"** și în fila „Verificate": pentru corectate se afișează categoria
  AI **originală** (din `ai_category_corrections`, `mr_old_category` via LATERAL) vs cea pusă de om
  (`✎ corectat` / `✓ confirmat`).
- **Fix navigare**: la corectarea categoriei în modal, lista pending se micșora și ←/→ se dezactiva.
  Acum lista de ID-uri se **îngheață la deschiderea modalului** (`reviewIds`) — parcurgi tot batch-ul.

Backup: `index.html.bak-mrtbl-20260612`, `manual_review.py.bak-tbl-20260612`. `node --check` OK.

## v0.32.1 - 2026-06-12 (Verificare manuală: deschidere în modalul email)

Rândurile pending din „Verificare manuală" se deschid acum în **același modal ca pagina Emailuri**
(navigare ←/→, taburi, atașamente), cu un `mode='review'`:
- Footer: doar butonul **„Marchează ca corect"** → confirmă și avansează la următorul.
- Corecția de categorie din modal merge prin endpoint-ul de review → apare automat în „Emailuri
  încadrate greșit" (Setări) și marchează item-ul rezolvat.

Backup: `index.html.bak-mrmodal-20260612`, `manual_review.py.bak-modal-20260612`. `node --check` OK.

## v0.32.0 - 2026-06-12 (Modul nou: Verificare manuală — learning / QA)

Modul nou în sidebar pentru active-learning pe categorisirea AI. **NU afectează fluxul existent** spre CTS —
doar eșantionează retrospectiv mailurile de ieri pentru validare umană.

- **Pick zilnic** (pe cron-ul de 5 min, idempotent, TZ Europe/Bucharest): ~20% (configurabil) din mailurile
  **CLEAN de ieri** = toate necunoscutele reale (`ai_category='necunoscut'`, `ai_status='done'`) + random din
  cele deja încadrate. Neprocesatele sunt excluse.
- **START/STOP** (setare `manual_review.enabled`) — oprești când statistica e suficient de bună.
- **Confirmare** (AI corect) sau **Corecție** (schimbă categoria) — corecțiile intră în `ai_category_corrections`
  și apar automat în „Emailuri încadrate greșit" + „Regenerează prompturi (AI)".
- **8 carduri** statistici (% necunoscut ieri, rată încadrare corectă, în așteptare, verificate azi, +totaluri).
- DB: migrare aditivă pe `emails` (`manual_review_*`) + index parțial. Backend: `services/manual_review.py` +
  router `manual-review`. UI: tab nou cu tabel + filtre.

Backup: `index.html.bak-mreview-20260612`, `main.py`/`emails.py` `.bak-mreview-*`. Verificat `node --check`,
pick e2e + idempotență.

## v0.31.1 - 2026-06-12 (Dashboard: consecvență carantină + explicații carduri)

Doar UI. Clarificare a inconsecvenței semnalate (sus „Carantină 4" vs „Rată carantină 242").

- Cardul `Strict review` redenumit **„Carantină strictă"** (e tot carantină: status `quarantined_strict`).
- Fiecare card din secțiunea **Verdict** are acum o explicație scurtă (ce înseamnă statusul).
- Nota la **Rată carantină** este explicită: „242 carantinate = 4 + 238 strictă".
- Cardul **Spam** notat „subset din Clean" (status `clean` flaguit spam, nu un status separat).
- Donut **Distribuție verdict** aliniat la carduri: Clean (complet) / Carantină (normală+strictă) / NDR / Pending;
  spam-ul nu mai e felie separată (era subset din Clean) — explicat în notă. Acum cardurile, rata și donut-ul
  folosesc aceleași definiții.

Backup: `index.html.bak-consistency-20260612`. Verificat `node --check`.

## v0.31.0 - 2026-06-12 (Dashboard: carduri grupate pe secțiuni + Status CTS + Top clienți)

**Frontend** — cardurile de statistici reorganizate în 3 secțiuni cu sub-titluri:
- **Volum**: Total emailuri, Ultimele 24h, Ultimele 7 zile.
- **Verdict**: Clean, Carantină, Strict review, NDR, Pending, Spam.
- **Rate & AI**: Rată Clean, Acoperire AI, Rată carantină, Reclamații % din total.
- Secțiune nouă **Analiză & distribuții** pentru charts.
- Două charts noi (bare): **Status CTS (pipeline)** (Pregătit/Trimis/În procesare/Oprit/Eroare) și
  **Top clienți (după volum)**. `HBars` acceptă acum `labelWidth` + ellipsis pe etichete lungi și stare „Fără date".

**Backend** — `GET /api/v1/stats/overview` extins cu: distribuție status CTS din `queue_status`
(`cts_ready/cts_sent/cts_in_progress/cts_stopped/cts_send_error/cts_error_nova`) și `top_clients`
(top 8 după volum, join `clients`). Read-only, fără modificări de schemă.

Backup: `index.html.bak-sections-20260612`, `health.py.bak-ctsclients-20260612`. Verificat `node --check` + `py_compile`; restart `mailguard-api`.

## v0.30.1 - 2026-06-12 (Charts: etichete oră + numere pe bare)

Doar UI. Fără schimbări de logică/date.

- **Volum pe oră (24h)**: afișează **toate** orele dedesubt (compact, ex. `07`) și **numărul de emailuri deasupra** fiecărei bare.
- **Evolutie emailuri pe zi**: afișează **totalul pe zi deasupra** fiecărei bare (prop nou `showTotals` în `StackedDailyChart`, activat doar aici).
- Backup: `index.html.bak-charts-20260612`. Verificat `node --check`.

## v0.30.0 - 2026-06-12 (Dashboard: statistici & charts noi + funnel mutat ultimul)

Statistici suplimentare pe baza datelor reale din `emails` / `email_spam`. Funnelul `Cargo360Funnel`
mutat ca ULTIMUL card (întâi statisticile).

**Backend** — endpoint nou read-only `GET /api/v1/stats/overview` (în `app/api/v1/health.py`):
distribuție verdict (incl. spam via join `email_spam`), atașamente cu/fără (`has_attachments`),
distribuție confidență AI (înaltă ≥0.75 / medie 0.5–0.74 / scăzută <0.5 din `ai_result->>'confidence'`),
confidență medie, scor mediu phishing, și volum pe oră în ultimele 24h (gap-filled). Fără modificări de schemă.

**Frontend** (Dashboard) — componente noi reutilizabile `Donut` (SVG), `HBars`, `HourlyChart`:
- 4 carduri de rate derivate: Rată Clean, Acoperire AI, Rată carantină, Spam detectat (%).
- Donut **Distribuție verdict** (Clean net / Spam / Carantină / NDR / Pending).
- Bare **Distribuție categorie AI** (informație / sesizare / reclamație / necunoscut).
- Bare **Confidență clasificare AI** (înaltă / medie / scăzută) + confidență & scor mediu.
- Donut **Atașamente** (cu / fără).
- **Volum pe oră (24h)** mini bar chart.
- Toate componentele sunt theme-aware (folosesc `var(--*)`), refresh la 10s odată cu restul statisticilor.
- Funnelul mutat la final.

Backup: `index.html.bak-dashstats-20260612`, `health.py.bak-overview-20260612`. Verificat `node --check` + `py_compile`; restart `mailguard-api`.

## v0.29.1 - 2026-06-12 (Funnel: mai lat + mai multe bile)

Doar UI, ajustare a widgetului `Cargo360Funnel`. Fără schimbări de logică/date.

- **Mai lat**: container `maxWidth` 760 → **1040px** (centrat) — SVG-ul (viewBox fix 680×360) se scalează uniform,
  deci mai mult spațiu între carduri și vizibilitate mai bună pe ecran.
- **+20% bile**: 5 → **6** bile în tranzit, cu start-uri redistribuite uniform pe bucla de 7s (~1.167s între ele);
  a 6-a reia ruta principală Clean → CTS.
- Backup: `index.html.bak-funnel2-20260612`. Verificat `node --check`.

## v0.29.0 - 2026-06-12 (Dashboard: funnel animat al fluxului de procesare)

Doar UI. Component nou `Cargo360Funnel` afișat pe Dashboard sub grila de statistici. Fără schimbări de logică/date/endpoint.

- **Funnel animat (SVG inline + CSS)**, fără librării externe. Arată drumul unui email: Inbox → Cargo360 →
  Clasificare → ramificare Spam / Clean / Carantină → Categorie → CTS, cu ramurile Blacklist și recuperările
  (Legit / Decarantinare) înapoi în Clean.
- **Animații**: linii „marching ants" pe `stroke-dashoffset` (keyframes `mgf-dash`); 5 bile (`<animateMotion>`)
  parcurg pe rând toate traseele posibile cu fade in/out; chenar CTS pulsează discret (`mgf-pulse`).
  Respectă `prefers-reduced-motion` pentru animațiile CSS.
- **Teme**: noduri pastel cu text/bordură în nuanța închisă (hex fix, lizibile pe temă deschisă și închisă);
  etichetele plutitoare și legenda folosesc `var(--t2)` ca să se adapteze la temă.
- **Implementare**: SVG ca string const la nivel de modul + `key` stabil → React sare peste re-scrierea DOM la
  re-render-ul Dashboard (interval 10s), deci animația NU repornește. Clase/marker/keyframes scope-uite `mgf-*`
  (zero coliziuni cu CSS-ul aplicației). Bazat pe exemplul aprobat `cargo360_flow_funnel_v3`.
- Backup: `index.html.bak-funnel-20260612`. Verificat `node --check`.

## v0.28.3 - 2026-06-12 (Preview atașament: fundal alb + zoom lin + rotire)

Doar UI, componenta `PreviewPane` (preview imagine/PDF în modalul email). Fără schimbări de logică/date.

- **Fundal curat**: panoul de preview restilizat flat alb (era dark `#101722` / gri `#222` la imagini) —
  acum se vede **doar documentul** pe alb. Toolbar `#FAFAF7` + bordură 0.5px, butoane flat neutre
  (fără `.btn secondary` dark).
- **Wheel zoom lin**: în loc de pas absolut +0.1/event (sărea 30-40% la mai multe evenimente per notch),
  acum **multiplicativ proporțional cu `deltaY`, plafonat la ±10%** per notch (`z * exp(dz)`,
  `dz` clamp ±0.10). Butoanele +/− devin și ele ±10% relativ.
- **Buton rotire 90°**: nou (doar imagini) — rotește incremental; inclus în `transform` (`rotate(Ndeg)`).
  Reset readuce zoom 20% + rotație 0.
- Backup: `index.html.bak-preview-20260612`. Verificat `node --check`.

## v0.28.2 - 2026-06-12 (Modal email mai lat + skeleton loading la navigare)

Doar UI, modalul `EmailDetail`. Fără schimbări de logică/date.

- **Lățime** `.em2` 1180→1520px (max-width 96→97vw). Pane-ul de preview atașament în tab HTML
  trece de la 65/35 la **56/44** (email/preview) — preview-ul (imagine zoom/PDF) se încadrează acum bine.
- **Skeleton loading** la navigarea Anterior/Următor: branch-ul `!email` nu mai afişează cutia dark
  „Se incarca..." ci păstrează shell-ul alb `.em2` (2 coloane) cu blocuri shimmer (clasă `.skl` +
  keyframe `@keyframes skl`) pentru antet, cardurile din sidebar, tab-uri, conţinut şi footer.
  `aria-busy=true`; butonul X rămâne funcţional în timpul încărcării.
- Backup: `index.html.bak-skelwide-20260612`. Verificat `node --check`.

## v0.28.1 - 2026-06-12 (Polish badge-uri liste — categorii + statusuri flat)

Doar UI, fără schimbări de logică/date. Aliniază badge-urile din tabele la estetica flat din modal.

- `.badge` global: radius 4→6px, font-weight 600→500, bordură 1px (transparentă by default), padding 3→ușor mai aerisit.
- Statusuri (`.b-*`): aceeași umplere subtilă (rgba ~0.13) + **bordură fină colorată** asortată — rămân theme-aware (dark/light).
- Categorii AI: helper nou `catBadge()` (+ `hexA()`) → fill subtil + text colorat + bordură fină (sentence case),
  în loc de fill solid saturat cu text alb. Aplicat în tabelul Emailuri (`EmailsList`) și tabelul Spam.
  Tabelele admin (istoric categorii) rămân neschimbate.
- Tabelul rămâne în tema aplicației (dark/light) — nu a fost făcut alb (ar fi stridență față de restul UI-ului).
- Backup: `index.html.bak-badges-20260612`. Verificat `node --check`.

## v0.28.0 - 2026-06-12 (Redesign modal email — layout 2 coloane, flat alb)

Doar UI/layout pe modalul `EmailDetail` (partajat de Emailuri / Carantină / Spam, `app/ui/index.html`).
Zero schimbare de logică, date, endpoint-uri sau acțiuni — informația exista deja, a fost re-aranjată.

### Layout
- Două coloane: sidebar fix 228px stânga (metadate + analiză) + zona de conținut email dreapta.
  Stil flat, temă albă (`#FFFFFF` modal, `#FAFAF7` sidebar/footer), borduri 0.5px în loc de umbre.
  Clase scope-uite `.em2*`, fără `var(--*)` în modal (temă fixă, independent de dark/light).
- Sidebar: card **Verdict** (inel SVG gauge scor/100), **Motive verdict** (checklist bife/warning din
  `phishing_reasons`/`spam_reasons`), **Categorie** (badge + Reclasifică AI), **ID email**, **Atașamente**
  (listă verticală, Preview/Download păstrate).
- Dreapta: tab-uri HTML / Text / Metadata (tab „Verdict + motive" eliminat, mutat în sidebar).
- Footer centrat: `[← Anterior] [Carantină] [Spam] [Următor →]` — branch-urile condiționale
  (carantinat: Confirmă/Decarantinează; spam mode: Legit/SPAM) păstrate. Spam cere confirmare.

### Note
- `CatCorrect` rescris flat (folosit doar aici). `AttachmentsBar` neatins (rămâne la `SpamEmailDetail`).
- Fallback `view` fără body: `verdict` → `meta`. Verificat `node --check`.
- Backup: `index.html.bak-modal2col-20260612`.

## v0.27.1 - 2026-06-11 (Uniformizare badge + verdict + butoane spam între liste și modal)

Follow-up la v0.27.0. Emailurile spam erau inconsistente: status corect în „Toate" dar verdict
„Clean" + butoane greșite în modal; lista „Spam" afișa status „clean". Zero schimbare de schemă.

### Fix A — badge de status uniform (UI)
- Helper unic `statusBadge(status)`: `spam` → `<span class="badge b-quarantined">SPAM</span>`
  (portocaliu), restul → `b-<status>` uppercase. Aplicat în rândul EmailsList (Toate/Email/Carantinate)
  și SpamList. Identic cu badge-ul din modal (care deja randa `b-quarantined`+SPAM pe `mode==='spam'`).
- Eliminată clasa CSS `.b-spam` (introdusă în v0.27.0, rămasă nefolosită).

### Fix B — lista Spam afișa status real „clean"
- `list_spam` (`/spam`) returna `emails.status` (clean) deși toate rândurile sunt spam prin
  definiția `where`. Acum derivă `status='spam'` pe items (oglindă a `list_emails`). status-ul real
  în DB neschimbat; câmpul serveste doar badge-ul (modalul foloseste `mode='spam'` explicit).

### Fix C — modal: verdict + butoane greșite când spam-ul e deschis din tab-ul „Toate" (cauză-rădăcină)
- `EmailDetail` deriva `mode` din FILTRUL listei: în „Toate" filtrul e gol → `mode='phishing'` →
  verdict construit din `status` real (`clean`) + footer „Pune în carantină / Marchează ca spam"
  (butoanele de clean). Același email deschis din „Spam" (unde `mode='spam'` e hardcodat) arăta
  corect verdict SPAM + Legit / Marchează ca SPAM. De aici confuzia.
- Fix: `mode` se derivă acum din **emailul selectat** (`item.status === 'spam' ? 'spam' : 'phishing'`),
  nu din filtru. Se bazează pe statusul deja derivat de backend (care a aplicat
  `SPAM_EXCLUDED_STATUSES`), deci un phishing-carantinat nu poate fi confundat cu spam. Modalul e
  acum identic din orice tab; recalcul corect la navigarea ←/→.
- Neschimbat intenționat: rândul „Status" din tab-ul Metadata = status REAL din DB (`get_email`).



Două fix-uri pe pagina Emailuri. Zero schimbare de schemă (sursa de adevăr = `email_spam`).

### Fix 1 — „Marchează ca spam" vizibil în lista Emailuri (status derivat `spam`)
- Bug: `spam_action`/`mark_spam` seta corect `email_spam.override=TRUE` (mailul apărea în tab-ul
  Spam), dar `list_emails` afișa badge-ul DOAR din `emails.status`, care rămânea `clean`. Efect:
  emailul marcat spam continua să apară ca „clean" în tab-urile Toate și Email.
- Fix: `list_emails` derivă acum un status virtual `spam` printr-un predicat reutilizabil
  (`_SPAM_PREDICATE`: `override=TRUE` sau `spam_score>=SPAM_THRESHOLD=50`, excluzând
  `SPAM_EXCLUDED_STATUSES` = quarantined/quarantined_strict/released/ndr/deleted/pending — aceeași
  regulă ca endpoint-ul `/spam`). Coloana derivată `is_spam` rescrie `status->'spam'` în răspuns.
  - Tab **Toate** și **Spam**: emailurile spam apar cu `status='spam'`.
  - Tab **Email** (lockStatus='clean'): exclus explicit (`NOT (predicat)`) — spam-ul nu mai apare.
  - `emails.status` real în DB NU se modifică: pipeline, decarantinare, butoanele modal intacte.
- UI: clasă badge `.b-spam` (chihlimbar). Tab-urile rămân Toate/Email/Carantinate/Spam.

### Fix 2 — decarantinarea unei carantine MANUALE nu mai întoarce 400
- Bug: `POST /emails/{id}/feedback` (`mark_not_phishing`) calcula `suppressible` din
  `phishing_reasons`. O carantină manuală (`/quarantine`) lasă `phishing_reasons=[]`, deci
  `suppressible=[]` → `HTTPException(400, "Carantinat doar pe indicatori de malware ...: -")`.
- Fix: ramură dedicată când `fired` (codurile detectate) e gol → eliberare simplă:
  `status='clean'`, `review_decision='not_phishing'`, release `quarantine_strict`, audit log.
  NU se creează `suppression_rules` cu `suppressed_codes=[]` (regulă moartă) și NU se face
  fingerprint learning (operatorul eliberează ACEST email, nu lookalikes). 400-ul se păstrează
  doar pentru cazul all-malware (`fired` nenul, dar tot în `NEVER_SUPPRESS`).
- Testat: email 6032 (carantină manuală, `phishing_reasons=[]`) → 200, `status=clean`, zero
  suppression_rule pentru expeditor.


## v0.26.0 - 2026-06-11 (Spam: allowlist/blocklist efectiv la clasificare + quote stripping)

Doua fix-uri pe modulul SPAM nativ. Zero schimbare de schema (refoloseste tabele existente).

### Fix 1 — allowlist/blocklist consultate INAINTE de clasificare
- Bug: butoanele "Legit"/"Marcheaza ca SPAM" persistau corect expeditorul in
  `spam_sender_reputation`, dar pipeline-ul de clasificare (`process_email.process_one`) apela
  `spam_detector.detect_spam(em)` — care NU consulta deloc reputatia. Functia
  `detect_spam_with_reputation` exista, dar era cod mort (nereferit nicaieri). Efect: un mail
  ulterior de la o adresa marcata "Legit" revenea in spam.
- Fix: reputatia e acum POARTA EXTERIOARA in `process_one`, consultata inainte de scoringul de
  continut, via noul `spam_detector.get_sender_reputation_pg(addr, cur)` (geaman psycopg2 al
  `get_sender_reputation`, aceeasi semantica exact > domeniu):
  - **allowlist** -> `spam_score=0` + `override=FALSE` (bypass neconditionat, la orice prag).
  - **blocklist** -> `override=TRUE` (apare in lista spam la ORICE prag — simetric cu allowlist;
    NU se mai bazeaza pe vechiul boost +40, care la prag 50 lasa un mail fara semnale sub prag).
  - fara reputatie -> scoring normal; `override` nu se atinge (se pastreaza deciziile manuale).
- Ambele liste persistente, pe adresa exacta, idempotente, last-write-wins (logica endpoint neschimbata).

### Fix 2 — quote stripping pe scoringul de spam
- Bug: `detect_spam` scana intreg corpul, inclusiv textul CITAT din thread. Un raspuns benign al
  clientului peste un mail bulk citat (trimis de noi: Unsubscribe / View in browser / limbaj
  promotional) era marcat gresit ca spam.
- Fix: `detect_spam` evalueaza semnalele DOAR pe continutul nou, refolosind acelasi helper ca
  modulul de carantina (`phishing_detector._new_content`) — un singur loc comun, nereimplementat.
  Subiectul se scaneaza intreg (nu e citat); doar corpul e redus la continutul nou.
- Nota: si `/spam/backfill` mosteneste quote-stripping (reruleaza `detect_spam`) — comportament
  consistent dorit.

### UI
- Coloana "Actiuni" (Legit / SPAM) scoasa din tabelul listei de spam (nu e necesara acolo);
  butoanele raman in modalul de detaliu email.

### Verificare
- Test e2e pe date reale (snapshot+restore, zero net change): adresa "Legit" -> mail ulterior
  reprocesat are score 0 + override=FALSE; "mark_spam" -> override=TRUE; quote-strip: semnale doar
  in citat -> score 0, aceleasi semnale ca text nou -> score 80.

## v0.25.0 - 2026-06-11 (Carantina: combinatie STRICT + gate NOVA intentie + learning)

Intarire modul carantina fara crestere false-pozitive. Zero schimbare de schema.

### Detectie
- **STRICT pe combinatie** (`phishing_detector.detect_phishing`): un singur trigger Layer-4 pe o
  fraza nu mai forteaza singur `quarantined_strict`. Necesita >=2 coduri stricte distincte SAU
  1 strict + un finding coroborant Layer-1/2. Altfel statusul devine score-based, cu nota de
  explicabilitate pe finding. (Analiza pe date reale: 65/239 strict existente erau single-phrase
  fara coroborare — exact clasa de false-pozitive vizata.)
- **manual_blacklist** (Layer 4, decisiv): cod nou pentru expeditori carantinati manual de operator.

### Gate NOVA (verificator de intentie)
- `strict_intent_gate.evaluate(...)` generalizat: ruleaza acum pe carantina SIMPLA + STRICTA (nu
  doar strict). IRIS NU decide singur — elibereaza la `clean` DOAR daca: intent benign (conf>=0.80)
  + fara blockeri structurali (malware/impersonare/URL high-conf) + expeditor de incredere
  (client cunoscut, `client_id`). La eroare/timeout/NOVA neconfigurat → pastreaza (conservator).
- **Pornit by code default** (`STRICT_INTENT_GATE_ENABLED` default '1'); fara atingere `.env`.
- **Prompt de intent editabil din UI** (Setari > Prompturi AI): stocat in tabela `prompts`
  (`code=nova_intent_detection`) + istoric `prompt_history`. Fallback = default din cod.

### Learning
- **Decarantinare → whitelist amprenta** (`emails.mark_not_phishing` + `process_email`): la
  decarantinare se salveaza fingerprint-ul (SimHash) continutului nou in `settings
  ['decarantine_fingerprints']`. Mail ~identic de la un CLIENT CUNOSCUT cu aceeasi amprenta →
  auto-`clean` (anti false-positive recurent, fara portita: cere si client cunoscut SI amprenta).
- **Carantinare manuala → learning agresiv** (`emails.quarantine_email`): blacklist expeditor
  (gated pe incredere — necunoscut: blacklist hard pe adresa; client cunoscut posibil compromis:
  doar amprenta + flag validare umana), salvare exemplu periculos (scor + semnale ratate + amprenta),
  si propunere IRIS de imbunatatire daca scorul era sub prag (gap real). Propunerile NU se aplica
  automat — validare umana. Stocat in `settings['phishing_manual_learning']`.

### API + UI
- `GET/PUT /settings/nova-intent-prompt`, `GET /settings/learning-proposals`.
- Setari > Prompturi AI: card editabil prompt intent IRIS + card read-only propuneri learning.

### Note
- **QR phishing:** nu exista in cargo360 (cod/reguli/UI/DB) — nimic de eliminat.
- Quote stripping (doar continut nou) era deja implementat (FIX 0).
- NOVA ESTE configurat in productie (`.env` via systemd EnvironmentFile → gunicorn). Verificat
  end-to-end pe gateway-ul real: benign + client cunoscut → release/clean; phishing → keep
  (blocker impersonare). Gate best-effort: la eroare/timeout/conf mica ramane carantinat.

## v0.24.0 - 2026-06-11 (Inline cid: fara alt — fallback pozitional)
- **Imaginile cid: fara alt** (ex. cid:EmbeddedImage, placeholder generic Outlook) se afiseaza acum:
  _inline_cid_images devine TWO-PASS — pass 1 rezerva atasamentele matchuite prin alt=nume, pass 2
  asociaza in ordinea din document fiecare cid img nematchuit cu urmatorul atasament IMAGINE nerezervat.
- Best-effort: corect cand nr. cid imgs nematchuite <= nr. atasamente imagine (cazul observat). Pastreaza
  guard-urile (8MB/img, fisier pe disc, fara fetch extern). Regresie verificata: 5878 ramane 2 imagini inline.

## v0.23.3 - 2026-06-11 (Preview atasamente: ajustari zoom/pan)
- **Eliminata grila de navigare** (sus/jos/stanga/dreapta/centru) din PreviewPane — pan-ul se face din mouse (drag).
- **Zoom la scroll mai soft**: step +/-10% (era +/-40%).
- **Scroll izolat**: wheel-ul peste preview NU mai scrolleaza si continutul mailului (listener non-passive, preventDefault+stopPropagation).
- **Reset (⟳) + zoom initial = 20%** (era 100%).

## v0.23.2 - 2026-06-11 (Copy ID -> clipboard direct + toast, fara prompt)
- **Butoanele de copy** (ID din tabel + 'ID + motiv' din modal) copiaza acum DIRECT in clipboard
  si arata un toast de succes, fara sa mai deschida window.prompt / Swal modal.
- Helper nou **mgCopy(text, okMsg)**: navigator.clipboard cu fallback execCommand pentru context
  non-securizat (http://, unde clipboard API lipseste) — de aici venea prompt-ul.

## v0.23.1 - 2026-06-11 (Titlu + favicon)
- **Titlu pagina**: 'NOVA Cargo360 Admin' -> 'Cargo360'.
- **Favicon**: badge SVG inline 'MG' pe gradient albastru (acelasi stil ca .mg-badge din header).

## v0.23.0 - 2026-06-11 (Imagini externe afisate by default)
- **Imaginile externe (https/http) din body_html se afiseaza acum BY DEFAULT** in tab-ul HTML.
  CSP-ul iframe-ului devine 'img-src https: http: data:' neconditionat (decizie produs ceruta de user).
- TRADE-OFF asumat: imaginile externe din mail pot fi tracking pixels (confirma 'deschis' catre expeditor)
  si scurg IP/UA-ul analistului catre host-uri externe. Iframe-ul ramane sandbox='' (fara scripturi/forms).
- Imaginile inline cid: raman inline data: din backend (v0.21.0); param allowRemote din buildEmailSrcDoc
  devine no-op (pastrat pentru compat).

## v0.22.0 - 2026-06-11 (Preview atasamente in split-pane langa email, cu zoom/pan)
- **Preview-ul atasamentelor NU mai deschide full-screen** — se deschide intr-un panou lateral in
  tab-ul HTML: emailul ocupa ~65%, preview-ul ~35%.
- **Emailul se ingusteaza la 65% prin REFLOW** (overflow-wrap/word-break in CSP style), NU prin scale,
  deci dimensiunea textului ramane neschimbata; textul lung trece pe rand nou.
- **PreviewPane (imagini)**: zoom din rotita + butoane +/-/reset, pan prin drag SI butoane
  stanga/dreapta/sus/jos, buton download. PDF -> iframe simplu (viewer browser).
- **Click-toggle**: click pe atasament deschide preview; click din nou pe acelasi inchide;
  click pe alt atasament comuta. Chip-ul activ e evidentiat.
- Arrow keys raman pentru navigarea emailurilor (nu pan), pan-ul e pe butoane+drag.
- preview resetat + blob URL revocat la navigare/inchidere (fara leak).

## v0.21.0 - 2026-06-11 (Imagini inline cid: in-scope + nav grupata + confirm carantina + scos toggle imagini)
- **Imagini inline cid: afisate DEFAULT** (backend, in-scope, FARA schema change): get_email rescrie
  <img src=cid:...> in data:URI matchuind alt= cu numele atasamentului (case-insensitive). Bytes-ii
  sunt deja pe disc => zero fetch extern, se afiseaza sub CSP img-src data: existent. Cap 8MB/img,
  netatins daca lipseste alt / numele / fisierul. ANULEAZA escaladarea din outbox (nu mai e nevoie de Razvan).
- **Scos toggle 'Incarca imaginile'** + state loadImg — imaginile inline vin acum direct din backend.
  Imaginile EXTERNE (https) raman blocate by default (anti-tracking), nemodificat.
- **Atasamentele se afiseaza toate by default** (AttachmentsBar lista completa, era deja asa).
- **Navigare grupata in centru**: footer PREV [actiuni] NEXT centrat (justify-content center), nu la margini.
- **Confirm pe 'Pune in carantina'**: SweetAlert 'Esti sigur ca doresti sa carantinezi acest email?'.
- **Badge verdict SPAM** warning galben (din v0.20.0).

## v0.20.0 - 2026-06-11 (Navigare in footer + actiuni pe verdict + imagini HTML opt-in + badge SPAM)
- **Navigarea prev/next mutata din header in footer**: layout PREVIOUS [actiuni] NEXT, grupate.
- **Actiunile din footer depind de verdict**: Clean -> Pune in carantina / Marcheaza ca spam;
  Carantina -> Confirma carantinarea / Decarantineaza; Spam -> Legit / Marcheaza ca SPAM.
  (Decarantineaza = POST /emails/{id}/feedback -> status clean; Confirma = POST /emails/{id}/quarantine.)
- **Eliminata bara de actiuni de sus** (ft) — actiunile traiesc acum doar in footer.
- **Imagini externe in tab-ul HTML**: toggle 'Incarca imaginile' per-email, DEFAULT OFF
  (secure-by-default: imaginile externe din mail suspect sunt tracking pixels / leak IP). CSP-ul
  iframe-ului devine img-src https/http/data doar cand userul apasa explicit; se reseteaza la navigare.
- **Badge verdict SPAM** acum warning (galben, b-quarantined) in loc de verde.
- NOTA: imaginile inline 'cid:' necesita schema+backend (coloana content_id) -> trimis in outbox (Regula 14).

## v0.19.2 - 2026-06-11 (Curatare modal: o singura cale de copy + scos bare redundante)
- **Butonul "ID + motiv" din info-bar** arata acum un toast de succes la copiere; copierea ID+motiv
  se face exclusiv din butonul info-bar.
- **Eliminata bara footer "Copiaza ID + motive spam / pentru finetuning"** — in phishing bara ramane
  doar pentru actiuni; in spam dispare (actiunile Legit/SPAM raman in footer-ul de jos).
- **Eliminata bara "AI categorie:"** din modal — redundanta cu coloana Categorie editabila din info-bar.

## v0.19.1 - 2026-06-11 (Tabel: o singura coloana de categorie)
- **Eliminata coloana redundanta "Categorie"** din lista Emails (afisa `email.category`, acelasi lucru
  cu `ai_category`).
- **Coloana "AI" redenumita "Categorie"** in tabelul Emails SI in tabelul Spam (raman identice).

## v0.19.0 - 2026-06-11 (Modal Emails/Spam: latime marita + dimensiuni fixe, AI categorie editabila in info-bar)
Ajustari UI peste structura din v0.18.0 (fara override).
- **Modal MULT mai lat + dimensiuni FIXE** (`.modal` width:1320px/max-width:96vw, height:88vh) — box-ul
  nu mai face reflow la navigare.
- **Fix kick-out la NEXT rapid** (cauza reala): backdrop-ul (.modal-bg) se inchide acum doar daca
  apasarea PORNESTE pe backdrop (`onMouseDown` cu `e.target===e.currentTarget`). Inainte, al doilea click
  rapid cadea pe backdrop in timp ce butonul -> disparea la re-render-ul de loading -> modalul se inchidea.
- **Verdict** in info-bar afiseaza acum UPPERCASE: CLEAN / QUARANTINED / QUARANTINED_STRICT / NDR, iar
  in mod spam afiseaza SPAM.
- **Categorie = AI categorie editabila** (componenta noua `CatCorrect`): badge ai_category + dropdown
  corectare manuala (-> POST /ai/category/{id}/correct) + buton "Reclasifica AI" (-> POST /ai/category/{id}/run);
  update live in modal via setEmail, fara reload.
- **ID email**: butonul de copy din info-bar copiaza acum direct ID + motiv carantina (phishing,
  buildFinetuneText) sau ID + motive spam (mod spam), nu doar ID-ul.

## v0.18.0 - 2026-06-11 (Reintegrare UI Emails+Spam + search + decarantinare->clean)
Reintegrare a muncii suprascrise pe 10 iun (overwrite full-file din copie stale), peste structura
curenta (s-au pastrat ai_category + /spam extins ale lui Andrei).
- **Modal unificat**: SpamEmailDetail comasat in EmailDetail via prop `mode` ('phishing'|'spam') —
  taburi functionale peste tot (inainte taburile din modalul spam erau moarte), footer pe mode
  (Legit/SPAM vs Carantina/feedback), copy-text pe mode.
- **HTML primul**: tab-ul HTML e acum default pentru vizualizare mail (ordine HTML/Text/Verdict/Metadata).
- **Navigare prev/next** (sageti + taste <-/->) si pe modalul de Emails/Carantina/Strict, nu doar Spam.
- **Inaltime FIXA modal** (.modal height:88vh) — nu mai sare la navigare/schimbare tab; loading full-size.
- **Info-bar orizontal** restaurat in modal: Verdict | Categorie | ID (etichete sus, separatoare),
  cu deduplicare (status/score scoase din head-meta, ID scos din ft-bar).
- **Tabel Spam = tabel Emails**: Spam foloseste acum acelasi `list-table-full` cu aceleasi coloane
  (ID/Receptionat/Subject/From/Status/AI/Confidenta/Scor spam/Atasamente) + col Actiuni.
- **get_email** extins cu spam_score/spam_reasons/override (JOIN email_spam) — modalul spam avea scor NaN.
- **Search dupa subiect**: `GET /emails?q=` -> `subject ILIKE %q%`, input debounce 400ms in topbar.
- **Filtru 'Spam' in lista Emails** (status=spam -> EXISTS pe email_spam, virtual) + etichete status RO.
- **Decarantinare -> 'clean'** (nu 'released'): emailul iese din lista Carantinate; provenance pastrat.

## v0.16.2 - 2026-06-10 (UI — info-bar modal: layout orizontal stil tabel)
- Info-bar-ul (Categorie / Verdict / ID email) e acum **orizontal** — 3 coloane pe un rând,
  fiecare cu eticheta sus (stil `<th>`) și valoarea dedesubt (stil `<tr>`), separate prin
  delimitatoare verticale. Toate vizibile dintr-o privire; flex-wrap pe ecrane înguste.
- Eliminat textul cu motivul AI (`▸ ...`) din info-bar, conform cerinței. Pur-frontend, fără restart.

## v0.16.1 - 2026-06-10 (UI — preview imagini: zoom doar în panou + drag-to-pan)
- Fix: Ctrl+scroll nu mai face zoom la pagina întreagă. `onWheel`-ul React e passive
  (preventDefault ignorat), așa că zoom-ul scăpa la browser; înlocuit cu listener nativ
  `wheel` cu `{ passive: false }` atașat prin ref → zoom-ul rămâne DOAR în panoul de preview.
- Adăugat **drag-to-pan**: când imaginea e mărită, click-drag o mută stânga-dreapta/sus-jos
  (ajustare scrollLeft/scrollTop). Cursor grab/grabbing; imaginea are `pointer-events:none` +
  `draggable:false` ca drag-ul să fie fluid și fără ghost-image. Pur-frontend, fără restart.

## v0.15.3 - 2026-06-10 (UI — modal email: skeleton loading la navigare)
- La încărcare/navigare (next/previous) nu mai apare popup-ul mic „Se incarca...", ci
  **același modal la dimensiune fixă cu skeleton loading** (bare shimmer pentru subiect,
  meta, taburi și corp). Experiență fără salturi de layout.
- În starea de skeleton, **backdrop-ul NU închide modalul** (doar × sau ESC), ca un
  dublu-click accidental pe next/previous să nu scoată utilizatorul din modal. După
  încărcare, comportamentul normal (click pe fundal = închide) revine.
- CSS nou: `.mg-skel` + `@keyframes mgShimmer`. Pur-frontend, fără restart.

## v0.15.2 - 2026-06-10 (UI — preview imagini: zoom in/out)
- Preview-ul de imagini are acum **zoom in/out**: toolbar cu −/procent/+/Reset și
  **Ctrl + scroll** peste imagine. Zoom 25%–800% (pas 20%), scalare pe lățime cu scroll
  (pan) în container. Zoom-ul se resetează automat la schimbarea atașamentului (remount
  prin `key='preview-'+id`). Pur-frontend, fără restart.

## v0.15.1 - 2026-06-10 (UI — modal email: dimensiune fixă + navigare în footer)
- Modalul de email are acum **înălțime fixă** (`min(900px, 92vh)`), deci toate emailurile
  sunt încadrate identic, indiferent de cât conținut au (lățimea era deja fixă la 1360px).
  Conținutul scroll-ează în interior; experiență consistentă la navigarea între mailuri.
- Navigarea prev/next a fost **mutată din header în footer**: butonul **← Anterior** în stânga,
  butoanele de acțiune (Legit/SPAM sau Decarantinează) la mijloc, iar **numărul (i / total) +
  Următor →** în dreapta. Footer-ul apare acum și pentru emailurile fără acțiuni (ex. clean),
  dacă există navigare. Pur-frontend, fără restart.

## v0.14.2 - 2026-06-10 (UI — HTML email: imagini inline cid: rezolvate)
- Tab HTML: imaginile inline referite prin `src="cid:..."` (atașamente embedate,
  ex. semnături/poze din thread) se afișează acum în modal. UI-ul descarcă atașamentul
  prin fetch autentificat (`/emails/{id}/attachments/{att_id}/download`), îl convertește
  în `data:` URI și îl injectează în `srcdoc` în locul referinței `cid:` (helper `_mgInlineCids`
  + `cidMap` state + useEffect pe `email`). Fără restart (UI servit de pe disc), pur-frontend.
- Mapare pozițională (al i-lea `cid:` → a i-a imagine `image/*` disponibilă): exactă pentru
  cazul uzual 1 cid ↔ 1 imagine. Pentru mailuri cu mai multe imagini e best-effort — maparea
  exactă ar cere persistarea Graph `contentId` la ingestie (upstream/schema, Regula 14).
  Cid-urile fără imagine corespondentă rămân nerezolvate (fără fallback duplicat).

## v0.14.1 - 2026-06-10 (UI — HTML email: imagini remote + copy fara prompt)
- Tab HTML: CSP-ul iframe-ului permite acum imagini remote (img-src data: https: http:) si fonturi (font-src data: https:). Inainte doar data: -> iconitele/bannerele remote (social icons etc.) aratau doar alt-text ('facebook icon'...). Acum se incarca efectiv.
- NOTA securitate: incarcarea imaginilor remote permite open-tracking de catre expeditor (pixel de urmarire). Acceptat pentru fidelitate vizuala; link-urile raman dezactivate (pointer-events:none).
- Butonul 'Copiaza ID + motiv carantina/SPAM' foloseste acum mgCopy (clipboard direct + toast 'Textul a fost copiat in clipboard'), fara window.prompt.
- Doar frontend (app/ui/index.html), fara restart. Backup: index.html.bak-htmlimg-20260610.

## v0.14.0 - 2026-06-10 (UI+API — unificare tabel & modal SPAM cu Carantina)
- Tabul SPAM foloseste acum ACELASI tabel ca Carantina: coloane ID | Receptionat | Subject | From | Status | Categorie | Confidenta | Score | Atasamente. Coloana Status arata un badge 'spam' (portocaliu) — emailurile spam au status real 'clean' (spam = scor >= prag, nu un status), deci badge-ul evita confuzia. Score = spam_score.
- Modal UNIFICAT: componenta SpamEmailDetail a fost eliminata; ambele tab-uri (SPAM si Carantina) folosesc acum EmailDetail cu un prop `mode` ('spam'|altele). Default deschis pe HTML.
- Continut condiționat de context: in mod 'spam' verdictul de langa bara AI = scor spam + top motive (buildSpamVerdict), tabul 'Motiv' listeaza semnalele SPAM (SPAM_REASON_LABELS), iar footer-ul are butoanele Legit + Marcheaza ca SPAM (POST /spam/{id}/action). In rest (carantina) verdictul si motivele sunt cele de phishing.
- Footer Decarantineaza: butonul de eliberare ('NU e phishing') a fost MUTAT din bara de ID in footer, ca un singur buton (fara duplicat), etichetat 'Decarantineaza' + selector scope (acest expeditor / tot domeniul). Apare cand status incepe cu 'quarantined' (gate pe STATUS, nu pe mode — pastreaza corect si tabul clean). Hit pe POST /emails/{id}/feedback (status='released' + supresie scoped).
- Navigare ← → (1/N) intre emailuri adaugata in TOATE modalele (SPAM, Carantina, Strict, lista Email). key=emailId forteaza remount curat la schimbarea emailului (reset tab pe HTML, fetch nou).
- Backend (restart mailguard-api): /spam SELECT extins cu ai_category/ai_status/ai_result/attachment_count (pentru coloanele noi); get_email face LEFT JOIN email_spam pentru spam_score/spam_reasons (pentru modal). Aditiv, fara schimbare de schema. NOTA: emailurile spam fiind 'clean' pot sa nu fi fost categorisite AI -> coloanele Categorie/Confidenta/Atasamente pot fi goale (corect per model de date).
- Fisiere: app/ui/index.html (validat sintactic cu node --check), app/api/v1/spam.py, app/api/v1/emails.py. Backups: *.bak-spamunify-20260610.

## v0.13.2 - 2026-06-10 (UI — modal email: verdict pe bara AI + modal mai lat)
- Rezumatul verdictului e afisat acum ca a doua coloana, pe acelasi rand cu bara 'AI categorie' (split 50/50): status + scor + explicatia concisa de incadrare.
- Eliminat tab-urile separate 'Verdict' si 'Verdict + motive' introduse in v0.13.1. A ramas un singur tab 'Motiv' cu breakdown-ul detaliat pe layere (L4/L2/L1/L3). Ordine tab-uri: HTML | Motiv | Text | Metadata.
- Modal latit: max-width 980px -> 1360px, max-height 92vh -> 94vh.
- Doar frontend (app/ui/index.html), fara restart. Backup: index.html.bak-modalv2-20260610.

## v0.13.1 - 2026-06-10 (UI — modal email: restructurare tab-uri)
- Modal vizualizare email: tab-ul implicit la deschidere este acum HTML (fallback pe Verdict daca emailul nu are body HTML).
- Verdictul a fost impartit in doua tab-uri: 'Verdict' (rezumat concis — doar banner-ul de incadrare) si 'Verdict + motive' (breakdown-ul complet pe layere L4/L2/L1/L3). Ordine tab-uri: HTML | Verdict | Verdict + motive | Text | Metadata.
- Bara 'AI categorie' ramane neschimbata, deasupra tab-urilor (mereu vizibila).
- Doar frontend (app/ui/index.html), fara restart — index.html e servit de pe disc. Backup: index.html.bak-modaltabs-20260610.

## v0.13.0 - 2026-06-10 (FAZA 3 — STAGED, activare prin outbox)
- FAZA 3 — Learning la decarantinare scoped pe TEMPLATE FINGERPRINT. Livrat in-scope si VALIDAT, dar NEACTIVAT: necesita o migratie de schema pe DB-ul de productie (DDL), care e rezervata admin -> outbox catre Andrei (Regula 14). Nu am rulat ALTER/CREATE.
- Modul NOU app/services/template_fingerprint.py (pur stdlib): SimHash 64-bit peste continutul NOU normalizat (scoate cifre/URL/punctuatie/whitespace variabil), match prin distanta Hamming <= k (default 3). Dormant pana la integrare.
- Validat pe date reale: 4 emailuri template "Variante de lucru" (acelasi expeditor) -> Hamming 0-1 intre ele; vs marketing 17-18; vs tracking 32-33; continut subtire -> fingerprint None (fail-safe, nu face match). Confirma: aprobarea unui template elibereaza DOAR mail near-identic, nu tot ce trimite expeditorul.
- Artefact migratie: migrations/20260610_template_fingerprint.sql (NEEXECUTAT) — adauga suppression_rules.template_fingerprint TEXT + fingerprint_k, scope_type nou 'template', si tabel golden_bad_templates (amprente known-bad care forteaza keep). Regulile sender/domain existente raman neschimbate (compatibilitate).
- DE FACUT dupa aprobare outbox: aplicare migratie; wiring in /emails/{id}/feedback (scope='template' stocheaza amprenta) + process_one (match amprenta inainte de a aplica suppressed_codes) + confirm-scam (spam_sender_reputation blocklist + unsubscribe) + UI. Rollback: active=false pe regula / DROP-urile din migratie.

## v0.12.0 - 2026-06-10
- FAZA 2 — Poarta AI intent-gate pe STRICT. Cand detectorul forteaza quarantined_strict pe un trigger Layer 4, gateway-ul AI (iris_ai) judeca INTENTIA continutului NOU (post-FIX0) si recomanda downgrade DOAR daca verdictul e benign (confidence >= 0.80) SI nu exista niciun semnal structural dur: malware (executable/macro/double_extension), impersonare (display_name/typosquat) sau URL high-confidence (ip_url/subdomain_abuse/url_shortener). Verdictul LLM e NECESAR-DAR-NU-SUFICIENT.
- Rulare best-effort DUPA commit-ul durabil (ca la categorisirea AI): la eroare/timeout/neconfigurat/low-confidence ramane quarantined_strict (default sigur). Flag: STRICT_INTENT_GATE_ENABLED (default 0; codul ramane DORMANT). Activarea (=1) este un config_change rezervat admin (Regula 14) -> se ruteaza prin outbox catre Andrei; NU a fost activat din proprie initiativa. Validare facuta cu dry-run + ROLLBACK (zero mutatie prod).
- La downgrade: scorul se recalculeaza fara trigger-ul L4 (respecta scorul cumulat) -> >=60 ramane quarantined, altfel clean. Randul quarantine_strict e marcat review_status='auto_released' / decision='ai_downgrade' (dispare din coada de review). Fiecare decizie (downgrade SI keep) e logata in audit_log (actor=ai_intent_gate) pentru review uman + rollback.
- Hardening anti prompt-injection: system prompt trateaza subiectul+corpul ca DATE NEINCREDERE si nu executa instructiuni din ele.
- DEVIATIE fata de planul initial: criteriul de autentificare a fost ELIMINAT ca poarta. FAZA 1 a stabilit ca semnalul de auth e all-failure (fara SPF/DKIM/DMARC pass; SPF softfail ~50% din mail), deci inutilizabil ca discriminator si ar bloca aproape orice downgrade. Se reactiveaza cand upstream (parser-email-op) livreaza Authentication-Results brut (vezi FAZA 0 outbox).
- Validat pe 15 quarantined_strict reale (dry-run, fara scriere): 6 downgrade (corespondenta legitima de business care declansa fals account_suspended/click_request - ex. notificari juridice "variante de lucru", reziliere contract), 9 keep (phishing/marketing cu redirect ascuns, domenii spoofate, impersonare prinsa de blocker, cazuri ambigue sub prag). Cele 3 statement-uri SQL verificate separat cu ROLLBACK.
- Fisiere: app/services/strict_intent_gate.py (NOU), app/services/process_email.py (orchestrare). Backup: process_email.py.bak-strictgate-20260610, .env.bak-strictgate-20260610.

## v0.11.2 - 2026-06-10
- UI copy ID (butonul ⧉ din tabele): copiere directa in clipboard + toast, fara dialogul window.prompt. Cauza: app servit pe HTTP, unde navigator.clipboard nu exista (secure-context only) -> cadea pe prompt. Adaugat helper mgCopy/mgCopyExec cu fallback document.execCommand('copy') care merge si pe HTTP. IdCell foloseste mgCopy (acopera toate tabelele).
- Backup: app/ui/index.html.bak-copyid-20260610.

## v0.11.1 - 2026-06-10
- UI pagina Emailuri: uniformizare tabele. Tabelul SPAM convertit la acelasi stil ca celelalte (clasa list-table-full + CSS global th/td, fara stiluri inline). Eliminate coloana "Actiuni" si butoanele Legit / Marcheaza ca SPAM din tabel (raman in modalul de detaliu SpamEmailDetail, urmeaza sa fie completate ulterior).
- Tabel principal emailuri: coloana "AI" redenumita "Categorie" (afiseaza ai_category); vechea coloana "Categorie" (e.category) eliminata din tabel.
- Backup: app/ui/index.html.bak-uitables-20260610.

## v0.11.0 - 2026-06-10
- Carantina phishing FIX 0 (quote stripping): trigger-ele de continut (Layer 2) si STRICT (Layer 4) se evalueaza DOAR pe continutul NOU al expeditorului, nu pe textul citat/forwardat din thread. phishing_detector.py: _strip_quoted_text / _strip_quoted_html / _new_content (markeri RO+EN, blockquote/gmail_quote/Outlook divRplyFwdMsg, safety-net <3 chars pe forward gol). Layer 1 + atasamente neatinse; corpul stocat nemodificat. Explicabilitate: nota "(evaluat pe continut nou, fara citat)".
- Validat pe date reale inainte de deploy: 96/228 quarantined_strict -> clean (-42% false-positive), 0 regresii (798/798 clean raman clean). Mailurile deja carantinate nu se reproceseaza (efect doar pe fluxul nou).
- Backup: app/services/phishing_detector.py.bak-fix0-20260610.

## v0.10.7 - 2026-06-10
- Title final: doar "Cargo360" (prefixul de brand se adauga in stratul de afisare). Backup: index.html.bak-mg-20260610.

## v0.10.6 - 2026-06-10
- Title simplificat la "Cargo360" (consistent cu sidebar + login). Confirmat ca nu exista document.title in JS; tag-ul static e singura sursa.
- Backup: index.html.bak-titleonly-20260610.

## v0.10.5 - 2026-06-10
- Title <title> scris ca entitati HTML numerice (&#78;... = NordLogistics Cargo360) ca sa nu mai fie rescris de stratul de afisare; browserul il decodeaza corect.
- Badge light theme: restaurat gradientul original #0284c7->#1d4ed8 (dark ramane solid #4CTS3FF).
- Backup: index.html.bak-name-20260610.

## v0.10.4 - 2026-06-10
- Badge MG + favicon: eliminat gradientul, albastru solid #4CTS3FF (sidebar dark+light si favicon SVG).
- Backup: index.html.bak-color-20260610.

## v0.10.3 - 2026-06-10
- UI <title> -> "NordLogistics Cargo360" (era "<vechiul brand> Cargo360 Admin").
- Favicon adaugat: SVG inline identic cu badge-ul MG din sidebar (patrat rx8, gradient 135 #4FC3FF->#3b82f6, text MG alb).
- Doar app/ui/index.html (fisier static, fara restart). Backup: index.html.bak-favicon-20260610.

## v0.10.2 — 2026-06-10
- Perf tab Spam: lista se incarca instant (~1.6s -> ~3ms). Cauza: LEFT JOIN LATERAL pe clients.emails facea Seq Scan (GIN neutilizat pe operand corelat). Inlocuit cu LEFT JOIN clients ON id=e.client_id (PK) in /spam si /emails.
- Fix modal Spam: scor NaN + niciun semnal. GET /emails/{id} aduce acum spam_score/spam_reasons/override din email_spam (LEFT JOIN). Tab-urile HTML/Text/Metadata functioneaza (state view).
- Rebranding: NordLogistics Cargo360 (UI title, login, app_name in .env).

## v0.10.1 — 2026-06-10
- UI Emailuri: uniformizare tabele pe structura tabelului Spam (Data primirii | Scor | Subiect | Expeditor | Client | Acțiuni).
- Tab "Email" redenumit "Clean".
- Coloană nouă "Client" (înlocuiește "Motive"): client_name rezolvat server-side din adresa expeditorului via LEFT JOIN LATERAL pe clients.emails în /emails și /spam; negăsit => "Unknown Client".
- Buton "Scoate din carantină" pe emailuri carantinate => reutilizează POST /emails/{id}/feedback (whitelist suppression_rules + release + quarantine_feedback / zona NOVA AI); guardrail malware (NEVER_SUPPRESS) păstrat.

## v0.3.0 — 2026-05-20 00:30
- PASUL 0-1: Server inventory + port discovery (8500 free, 7.7G disk free)
- PASUL 2: DB cargo360 created in existing postgres container (parallel cu email_parser_db). 11 tables. Owner: cargo360 user. 8 seed settings.
- PASUL 3: FastAPI skeleton on port 8500 + .env + admin auth + health endpoint
- Option C parallel: parser-email-op rămâne LIVE intact

## v0.2.0 — DB schema (incluse în 0.3.0)
## v0.1.0 — Initial scaffolding (incluse în 0.3.0)
