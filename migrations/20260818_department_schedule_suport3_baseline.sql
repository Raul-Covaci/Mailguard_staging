-- 2026-08-18: baseline program Suport 3 (și Suport 1/2 ca safety net).
-- Aditiv + idempotent (ON CONFLICT DO NOTHING): nu suprascrie modificări manuale existente.
--
-- Cauza: department_schedule pentru suport_3 lipsea pe producție → business_minutes()
-- cădea pe fallback pontaj individual → durate reclamații afișate incorect (ex: 53 min
-- pentru o reclamație care a durat efectiv 24h+). Fix: inserează programul de referință
-- de pe staging (08:00–17:30, L–V, requires_attendance=false).
--
-- SAFE de re-rulat: ON CONFLICT DO NOTHING păstrează rândurile existente intacte.

INSERT INTO department_schedule (department, weekday, start_time, end_time, requires_attendance, active, updated_by)
VALUES
  -- Suport 3 (Reclamații): 08:00–17:30, Luni–Vineri
  ('suport_3', 1, '08:00:00', '17:30:00', false, true, 'migration_20260818'),
  ('suport_3', 2, '08:00:00', '17:30:00', false, true, 'migration_20260818'),
  ('suport_3', 3, '08:00:00', '17:30:00', false, true, 'migration_20260818'),
  ('suport_3', 4, '08:00:00', '17:30:00', false, true, 'migration_20260818'),
  ('suport_3', 5, '08:00:00', '17:30:00', false, true, 'migration_20260818'),
  -- Suport 1: 07:00–21:00, Luni–Vineri (safety net)
  ('suport_1', 1, '07:00:00', '21:00:00', false, true, 'migration_20260818'),
  ('suport_1', 2, '07:00:00', '21:00:00', false, true, 'migration_20260818'),
  ('suport_1', 3, '07:00:00', '21:00:00', false, true, 'migration_20260818'),
  ('suport_1', 4, '07:00:00', '21:00:00', false, true, 'migration_20260818'),
  ('suport_1', 5, '07:00:00', '21:00:00', false, true, 'migration_20260818'),
  -- Suport 2: 07:00–22:00, Luni–Vineri (safety net)
  ('suport_2', 1, '07:00:00', '22:00:00', false, true, 'migration_20260818'),
  ('suport_2', 2, '07:00:00', '22:00:00', false, true, 'migration_20260818'),
  ('suport_2', 3, '07:00:00', '22:00:00', false, true, 'migration_20260818'),
  ('suport_2', 4, '07:00:00', '22:00:00', false, true, 'migration_20260818'),
  ('suport_2', 5, '07:00:00', '22:00:00', false, true, 'migration_20260818')
ON CONFLICT (department, weekday) DO NOTHING;
