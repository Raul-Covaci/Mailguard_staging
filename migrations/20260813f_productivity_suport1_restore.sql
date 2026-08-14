-- Repune obiectivele Suport 1 asa cum erau inainte de 20260813b.
--
-- DE CE. Migrarea 20260813b a mutat schema "email 240/25 + task 240/25, restul pondere 0" pe
-- `suport_1`, pentru ca asa se intelesese cerinta initial. Clarificat ulterior in aceeasi zi:
-- schema (cu cele doua praguri pe reclamatii) e a departamentului care PROCESEAZA reclamatiile,
-- adica Suport 3 -- vezi 20260813e. Pe Suport 1 efectul a fost nedorit: apelurile (pondere 25) si
-- task-urile CargoBox (pondere 5) ieseau din scor, iar limita de email trecea de la 120 la 240.
--
-- Fisierul 20260813b RAMINE in repo, desi efectul lui se anuleaza aici: e deja comis si posibil
-- aplicat pe staging, iar `_release_migrations` marcheaza ce s-a rulat, nu ce exista pe disc.
-- Stergerea lui ar fi lasat mediile sa divergheze -- unde rulase ramanea aplicat, pe o baza noua
-- nu mai rula deloc. Asa, ORICE mediu ajunge in aceeasi stare finala: 20260813b (daca ruleaza)
-- urmata de aceasta.
--
-- Valorile repuse sint cele din configurarea de dinainte (verificate in baza de dezvoltare):
--     email                 120 min   50%
--     task                  120 min   20%
--     task / cargobox      8400 min    5%
--     apel                    4 sec   25%
--
-- Idempotent: upsert pe indexul unic (department, tip, COALESCE(categorie,'')).

DO $$
DECLARE
    v_dept CONSTANT text := 'suport_1';
BEGIN
    INSERT INTO productivity_objective(department, tip, categorie, limita_minute, pondere, unitate)
    VALUES (v_dept, 'email', NULL,       120, 50, 'minute'),
           (v_dept, 'task',  NULL,       120, 20, 'minute'),
           (v_dept, 'task',  'cargobox', 8400, 5, 'minute'),
           (v_dept, 'apel',  NULL,          4, 25, 'secunde')
    ON CONFLICT (department, tip, COALESCE(categorie, ''::text)) DO UPDATE
       SET limita_minute = EXCLUDED.limita_minute,
           pondere       = EXCLUDED.pondere,
           unitate       = EXCLUDED.unitate;
END $$;
