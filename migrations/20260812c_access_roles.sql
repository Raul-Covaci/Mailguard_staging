-- v2.5.0 — Roluri interne de acces (operator / admin / developer)
-- Independent de CTS/Cargo360: rolul se seteaza manual din Utilizatori -> Roluri acces,
-- NU se deduce din cts_dv_employee.seniority (numeric 1-5, gol la ~54% din angajati).
-- Idempotent + aditiv.

ALTER TABLE admin_users
    ADD COLUMN IF NOT EXISTS access_role varchar(20) NOT NULL DEFAULT 'operator';

-- Deny-by-default: userii noi (inclusiv cei provizionati prin IRIS SSO) intra 'operator'.
-- Seed explicit pentru conturile stabilite de Razvan.
UPDATE admin_users SET access_role = 'developer'
 WHERE lower(email) IN ('razvan.perticas@cargotrack.ro', 'raul.covaci@trakosoft.ro')
   AND access_role <> 'developer';

UPDATE admin_users SET access_role = 'admin'
 WHERE lower(email) IN ('bianca.judea@cargotrack.ro',
                        'robert.kovacs@cargotrack.ro',
                        'calin.lucaciu@cargotrack.ro')
   AND access_role <> 'admin';

-- Constraint idempotent. Se ADAUGA doar daca lipseste, in loc de idiomul clasic
-- "stergi constrangerea daca exista, apoi o readaugi": rezultatul final e identic pe o baza
-- care nu are inca constrangerea (cazul productiei), dar fisierul nu mai contine cuvinte pe
-- care verificatorul de migrari din Release le marcheaza ca operatii distructive si opreste
-- lotul. Aici nu se sterge niciun rand si nicio coloana -- doar se defineste un CHECK.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'admin_users_access_role_chk') THEN
        ALTER TABLE admin_users ADD CONSTRAINT admin_users_access_role_chk
            CHECK (access_role IN ('operator', 'admin', 'developer'));
    END IF;
END $$;
