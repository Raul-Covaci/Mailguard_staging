-- 2026-08-18: baseline program departamente (Suport 1/2/3 + Taxe drum).
-- Idempotentă: INSERT dacă rândul lipsește; UPDATE numai dacă updated_by e tot o migrație
-- (updated_by LIKE 'migration_%') — nu suprascrie modificări făcute manual din UI.
--
-- Cauza: department_schedule era complet gol pe producție → business_minutes() pentru
-- departamentele din _DEPT_WINDOW_DEPARTMENTS (suport_1/2/3, taxe_drum) nu găsea fereastră
-- → durate reclamații afișate incorect (ex: 53 min în loc de 24h+).
--
-- suport_3: 08:00–16:30, L–V, fără sâmbătă (nu există schimb 2, nu lucrează sâmbăta).
-- suport_1/2, taxe_drum: safety net — valorile de pe staging confirmate cu Razvan Perticas.
-- SAFE de re-rulat la orice release viitor.

INSERT INTO department_schedule (department, weekday, start_time, end_time, requires_attendance, active, updated_by)
VALUES
  -- Suport 3 (Reclamații): 08:00–16:30, Luni–Vineri, fără sâmbătă
  ('suport_3', 1, '08:00:00', '16:30:00', false, true, 'migration_20260818'),
  ('suport_3', 2, '08:00:00', '16:30:00', false, true, 'migration_20260818'),
  ('suport_3', 3, '08:00:00', '16:30:00', false, true, 'migration_20260818'),
  ('suport_3', 4, '08:00:00', '16:30:00', false, true, 'migration_20260818'),
  ('suport_3', 5, '08:00:00', '16:30:00', false, true, 'migration_20260818'),
  -- Suport 1: 07:00–21:00, Luni–Vineri
  ('suport_1', 1, '07:00:00', '21:00:00', false, true, 'migration_20260818'),
  ('suport_1', 2, '07:00:00', '21:00:00', false, true, 'migration_20260818'),
  ('suport_1', 3, '07:00:00', '21:00:00', false, true, 'migration_20260818'),
  ('suport_1', 4, '07:00:00', '21:00:00', false, true, 'migration_20260818'),
  ('suport_1', 5, '07:00:00', '21:00:00', false, true, 'migration_20260818'),
  -- Suport 2: 07:00–22:00, Luni–Vineri
  ('suport_2', 1, '07:00:00', '22:00:00', false, true, 'migration_20260818'),
  ('suport_2', 2, '07:00:00', '22:00:00', false, true, 'migration_20260818'),
  ('suport_2', 3, '07:00:00', '22:00:00', false, true, 'migration_20260818'),
  ('suport_2', 4, '07:00:00', '22:00:00', false, true, 'migration_20260818'),
  ('suport_2', 5, '07:00:00', '22:00:00', false, true, 'migration_20260818'),
  -- Taxe drum: 08:00–21:00, Luni–Vineri
  ('taxe_drum', 1, '08:00:00', '21:00:00', false, true, 'migration_20260818'),
  ('taxe_drum', 2, '08:00:00', '21:00:00', false, true, 'migration_20260818'),
  ('taxe_drum', 3, '08:00:00', '21:00:00', false, true, 'migration_20260818'),
  ('taxe_drum', 4, '08:00:00', '21:00:00', false, true, 'migration_20260818'),
  ('taxe_drum', 5, '08:00:00', '21:00:00', false, true, 'migration_20260818')
ON CONFLICT (department, weekday) DO UPDATE
  SET start_time           = EXCLUDED.start_time,
      end_time             = EXCLUDED.end_time,
      requires_attendance  = EXCLUDED.requires_attendance,
      active               = EXCLUDED.active,
      updated_by           = EXCLUDED.updated_by,
      updated_at           = now()
  WHERE department_schedule.updated_by LIKE 'migration_%'
     OR department_schedule.updated_by IS NULL;
