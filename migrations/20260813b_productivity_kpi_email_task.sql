-- KPI productivitate: email + task la 240 min, pondere 25% fiecare.
--
-- CONTEXT. Obiectivele si ponderile NU sunt in cod, ci in tabelele
-- `productivity_department_config` + `productivity_objective`. O modificare facuta din UI
-- (Productivitate -> Obiective & Ponderi) ramane in baza pe care s-a facut, deci nu ajunge pe
-- staging/productie prin git. De aceea schimbarea vine ca migrare: se aplica automat la restart
-- (scripts/migrate.sh, ExecStartPre) si prin Release pe productie.
--
-- SCHEMA TINTA CERUTA (business owner, 2026-08-13):
--     Email                                   240 min   25%
--     Task-uri                                240 min   25%
--     Solutionare reclamatii (new -> solved)  6720 min  30%
--     Contact reclamatii (new -> in progress)  240 min  20%
--
-- Migrarea de fata acopera DOAR primele doua. Cele pe reclamatii vin in acelasi release cu
-- sursa de date din CTS (tabela cts_replica.quality_evaluation, inca neexpusa de IRIS Gateway)
-- si cu codul care le calculeaza. Motivul pentru care NU se pun acum:
-- `department_report` trateaza un obiectiv fara nicio intrare in luna ca indeplinit 100%
-- ("nu poti rata ce nu a existat"). Introduse inainte de a exista datele, cele doua ar aduce
-- 50% din scor cadou, la 100%, si productivitatea departamentului ar sari artificial.
--
-- PONDERI EFECTIVE dupa aceasta migrare: scorul lunar e o medie PONDERATA NORMALIZATA pe
-- obiectivele active (`weighted_sum / weight_active`), deci email 25 + task 25 se comporta ca
-- 50/50 pana cand intra si cele doua obiective pe reclamatii.
--
-- Obiectivele care nu fac parte din schema tinta NU se sterg -- li se pune pondere 0. Raman
-- vizibile in UI si in istoric, ies din scor, si se pot reactiva schimband o cifra. Un rand
-- cu pondere 0 nu intra nici in `weighted_sum`, nici in `weight_active`.
--
-- Snapshot-ul lunar (`productivity_monthly_snapshot`) NU trebuie recalculat: el fixeaza orele,
-- coeficientul si obiectiv_real/minim, care nu depind de setul de obiective. Procentul atins se
-- recalculeaza din obiective la fiecare accesare, deci se schimba si pentru luna in curs.
--
-- Idempotenta: upsert pe indexul unic (department, tip, COALESCE(categorie,'')).

DO $$
DECLARE
    -- Departamentul vizat. Singurul loc de schimbat daca schema se aplica altui departament.
    v_dept    CONSTANT text := 'suport_1';
    v_limita  CONSTANT int  := 240;
    v_pondere CONSTANT numeric := 25;
BEGIN
    -- Config de departament: necesar pentru ca departamentul sa fie scorat deloc.
    -- baza_procent 95 = valoarea folosita de toate departamentele configurate.
    INSERT INTO productivity_department_config(department, baza_procent, updated_at, updated_by)
    VALUES (v_dept, 95, now(), 'migration 20260813b')
    ON CONFLICT (department) DO NOTHING;

    -- Email + Task la 240 min / 25%.
    INSERT INTO productivity_objective(department, tip, categorie, limita_minute, pondere, unitate)
    VALUES (v_dept, 'email', NULL, v_limita, v_pondere, 'minute'),
           (v_dept, 'task',  NULL, v_limita, v_pondere, 'minute')
    ON CONFLICT (department, tip, COALESCE(categorie, ''::text)) DO UPDATE
       SET limita_minute = EXCLUDED.limita_minute,
           pondere       = EXCLUDED.pondere,
           unitate       = EXCLUDED.unitate;

    -- Restul obiectivelor departamentului ies din scor (pondere 0), fara sa fie sterse.
    UPDATE productivity_objective
       SET pondere = 0
     WHERE department = v_dept
       AND (tip, COALESCE(categorie, '')) NOT IN (('email', ''), ('task', ''))
       AND pondere <> 0;
END $$;
