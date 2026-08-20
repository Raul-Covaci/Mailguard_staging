-- 2026-08-21: aliniaza configul motorului de satisfactie cu decizia de business (traiectorie
-- continua) si curata o cheie care nu mai e citita de cod.
--
-- DE CE E NECESARA. `20260818_satisfaction_v6.sql` a inserat cheia `satisfaction.v6` cu
-- `ON CONFLICT (key) DO NOTHING`. Daca rândul exista deja — si exista, pe orice bază care a rulat
-- migratia aceea sau in care cineva a editat configul din UI — valorile din DB BAT valorile
-- implicite din cod (`satisfaction_engine._V6_DEFAULTS`, citite prin `_load_v6_config`). Intre
-- 2026-08-20 si 2026-08-21 comportamentul cerut s-a schimbat de doua ori, deci configul din DB
-- poate fi rămas pe varianta intermediara (start fix la 50 + media simpla a saptamanilor). Fara
-- migratia de fata, un release ar duce codul nou pe prod, dar comportamentul ar rămâne cel vechi.
--
-- COMPORTAMENTUL CERUT (decizie business 2026-08-21):
--   * luna porneste din ULTIMUL SCOR CUNOSCUT al clientului (oricat de vechi), neutru (50) doar
--     daca nu exista niciun scor anterior  -> carry_start_state = true
--   * fiecare saptamana porneste din starea in care s-a incheiat cea precedenta
--   * scorul lunii = starea la finalul ultimei saptamani scorate, si aceea se reporteaza
--     -> month_aggregation = 'last_week_final'
--   Scopul: graficul pe 12 luni sa arate evolutia graduala, nu o resetare la 50 in fiecare luna.
--
-- `start_lookback_months` SE STERGE: lookback-ul e acum NELIMITAT (se caută cea mai recenta luna
-- cu scor, oricat de veche — vezi `_previous_month_state`). Cheia a rămas in DB cu valoarea 3 si
-- ar minti pe oricine inspecteaza configul; codul oricum nu o mai citeste.
--
-- `max_workers` se adauga explicit (paralelizarea snapshot-ului, v3.5.0): 6 clienti simultan.
-- Se poate urca/coborî de aici, fara redeploy.
--
-- NU e aditiva pe cele trei chei: le FORTEAZA la valorile decise. Asta e intentia — configul din
-- DB e sursa de adevar la rulare, deci trebuie sa reflecte decizia, nu o stare intermediara.
-- Celelalte chei ale obiectului (mode, prompt_version, single_kpi, model_hint) rămân neatinse.

UPDATE settings
   SET value = (
           (COALESCE(value, '{}'::jsonb) - 'start_lookback_months')
           || jsonb_build_object(
                'carry_start_state', true,
                'month_aggregation', 'last_week_final',
                'max_workers', 6
              )
       ),
       description = 'Config motor satisfactie V6 — traiectorie continua (luna porneste din '
                     'ultimul scor cunoscut, saptamanile se inlantuie), scor lunar = starea '
                     'finala, lookback nelimitat, 6 clienti in paralel'
 WHERE key = 'satisfaction.v6';

-- Baza fara rândul respectiv (instalare noua): il creeaza complet.
INSERT INTO settings (key, value, description)
VALUES (
    'satisfaction.v6',
    '{"mode": "iris_trajectory_v6",
      "prompt_version": "V6",
      "single_kpi": "iris_stare_finala",
      "carry_start_state": true,
      "month_aggregation": "last_week_final",
      "model_hint": "claude-sonnet-4-6",
      "max_workers": 6}'::jsonb,
    'Config motor satisfactie V6 — traiectorie continua, scor lunar = starea finala'
)
ON CONFLICT (key) DO NOTHING;

DO $$
DECLARE v jsonb;
BEGIN
    SELECT value INTO v FROM settings WHERE key = 'satisfaction.v6';
    RAISE NOTICE 'satisfaction.v6: carry=% agregare=% workers=% lookback_ramas=%',
        v->>'carry_start_state', v->>'month_aggregation', v->>'max_workers',
        COALESCE(v->>'start_lookback_months', '(sters)');
END $$;
