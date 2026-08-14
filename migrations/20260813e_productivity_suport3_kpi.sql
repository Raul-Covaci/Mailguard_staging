-- KPI productivitate Suport 3: email, task-uri si cele doua praguri pe reclamatii.
--
-- CONTEXT. Obiectivele si ponderile NU sint in cod, ci in `productivity_department_config` +
-- `productivity_objective`. O modificare din UI (Productivitate -> Obiective & Ponderi) ramine pe
-- baza pe care s-a facut, deci nu ajunge pe staging/productie prin git -- de aceea vine ca migrare.
--
-- SCHEMA CERUTA (business owner, 2026-08-13):
--     Email                                    240 min   25%
--     Task-uri                                 240 min   25%
--     Solutionare reclamatii (creare -> solved) 6720 min  30%
--     Contact reclamatii (creare -> in progress) 240 min  20%
--
-- RECLAMATIILE. Toate reclamatiile firmei intra la Suport 3, indiferent de departamentul
-- persoanei evaluate -- Suport 3 e echipa care le proceseaza. Pe Monitorul Operational aceleasi
-- reclamatii se afiseaza per departamentul evaluat; sint doua intrebari diferite ("ale cui sint"
-- vs "cine le lucreaza"), cu acelasi set de date.
--
-- DESPRE 6720 MIN. Cifra vine din "14 zile lucratoare x 8h". SLA-ul nostru numara insa minute din
-- PROGRAMUL departamentului, iar Suport 3 e configurat 08:00-17:30 (9.5h/zi), deci 6720 de minute
-- inseamna 11.8 zile lucratoare reale, nu 14. Pastram cifra ceruta; pentru "14 zile" adevarate
-- limita ar fi 14 x 9.5 x 60 = 7980. Se poate schimba din UI, fara migrare.
--
-- Obiectivele care nu fac parte din schema NU se sterg -- li se pune pondere 0: raman vizibile in
-- istoric, ies din scor (nu intra nici in `weighted_sum`, nici in `weight_active`) si se pot
-- reactiva schimband o cifra.
--
-- Snapshot-ul lunar nu trebuie recalculat: fixeaza orele si obiectiv_real, care nu depind de setul
-- de obiective. Procentul atins se recalculeaza din obiective la fiecare accesare, deci se schimba
-- si pentru luna in curs.
--
-- Idempotenta: upsert pe indexul unic (department, tip, COALESCE(categorie,'')).

DO $$
DECLARE
    v_dept CONSTANT text := 'suport_3';
BEGIN
    INSERT INTO productivity_department_config(department, baza_procent, updated_at, updated_by)
    VALUES (v_dept, 95, now(), 'migration 20260813e')
    ON CONFLICT (department) DO NOTHING;

    INSERT INTO productivity_objective(department, tip, categorie, limita_minute, pondere, unitate)
    VALUES (v_dept, 'email',      NULL,          240, 25, 'minute'),
           (v_dept, 'task',       NULL,          240, 25, 'minute'),
           (v_dept, 'reclamatie', 'solutionare', 6720, 30, 'minute'),
           (v_dept, 'reclamatie', 'contact',      240, 20, 'minute')
    ON CONFLICT (department, tip, COALESCE(categorie, ''::text)) DO UPDATE
       SET limita_minute = EXCLUDED.limita_minute,
           pondere       = EXCLUDED.pondere,
           unitate       = EXCLUDED.unitate;

    UPDATE productivity_objective
       SET pondere = 0
     WHERE department = v_dept
       AND (tip, COALESCE(categorie, '')) NOT IN
           (('email', ''), ('task', ''), ('reclamatie', 'solutionare'), ('reclamatie', 'contact'))
       AND pondere <> 0;
END $$;
