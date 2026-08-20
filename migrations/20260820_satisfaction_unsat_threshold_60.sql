-- 2026-08-20: pragul de „client nesatisfacut" trece de la 70% la 60%.
--
-- 60% e granita dintre „Satisfacut" (60-74) si „Neutru / satisfactie moderata" (45-59) din
-- tabelul de benzi al promptului V6 — adica singura taxonomie care conteaza acum, dupa ce
-- motorul a devenit „scorul vine exclusiv din promptul V6". Pana azi coexistau trei valori:
-- 70 in cod (`satisfaction_engine`, `clients.py`, 5 locuri in UI), 90 in documentatie (ramas
-- din motorul vechi cu 5 factori) si benzile din prompt.
--
-- CE FACE aceasta migratie: re-derivă DOAR flagul boolean `is_unsatisfied` din
-- `satisfaction_pct`-ul deja stocat. Niciun scor nu se recalculeaza si nu se face niciun apel
-- IRIS. Motivul: dashboard-ul de satisfactie numara nesatisfacutii din coloana `is_unsatisfied`
-- (vezi `/clients/satisfaction-stats`), nu din scor — fara pasul asta, acelasi grafic ar
-- amesteca luni evaluate cu prag 70 si luni evaluate cu prag 60.
--
-- CE NU FACE: nu rescoreaza istoricul. Snapshot-urile de dinainte de 2026-08-20 au fost
-- produse cu logica de traiectorie inlantuita (stare reportata intre saptamani si intre luni,
-- scor lunar = starea finala a ultimei saptamani). Rescorarea lor a fost decisa explicit ca
-- NU se face automat — rămâne disponibila client-cu-client din interfata.
--
-- Idempotenta: `IS DISTINCT FROM` -> ruleaza doar pe randurile care ar chiar schimba valoarea.

UPDATE client_satisfaction_snapshots
   SET is_unsatisfied = (satisfaction_pct < 60)
 WHERE satisfaction_pct IS NOT NULL
   AND is_unsatisfied IS DISTINCT FROM (satisfaction_pct < 60);

DO $$
DECLARE n_unsat INT; n_tot INT;
BEGIN
    SELECT count(*) FILTER (WHERE is_unsatisfied), count(*)
      INTO n_unsat, n_tot
      FROM client_satisfaction_snapshots
     WHERE satisfaction_pct IS NOT NULL;
    RAISE NOTICE 'snapshot-uri cu scor: % , dintre care nesatisfacuti la pragul 60%%: %', n_tot, n_unsat;
END $$;
