-- Procesatorii de plati online (euPlatesc, europayment.services) intra cu prioritatea P2.
-- Cerere business, 2026-09-12. Completeaza `20260911f_contabilitate_platesc_europayment.sql`,
-- care i-a dus pe departamentul Contabilitate — departamentul si prioritatea sunt campuri
-- separate, deci ambele sunt necesare.
--
-- Regula LIVE e in COD (`priority_rules._PAYMENT_PROCESSORS` -> `match()`, id `pay_processor`),
-- nu in `settings`: regulile de prioritate sunt deliberat in cod (semnale tari, putine, stabile),
-- spre deosebire de cele de departament, care se editeaza din Setari. Migratia asta NU instaleaza
-- regula — face doar corectia RETROACTIVA pe mailurile deja intrate.
--
-- Se ating DOAR mailurile netrimise inca la CTS (dupa trimitere, prioritatea din Cargo360 ar
-- diverge de tichetul din CTS) si fara corectie manuala de prioritate.

UPDATE emails
   SET ai_priority = '2',
       ai_priority_at = NOW(),
       ai_priority_result = COALESCE(ai_priority_result, '{}'::jsonb) || jsonb_build_object(
         'priority', '2', 'model', 'rule', 'rule_id', 'pay_processor',
         'reason', 'Notificare de la un procesator de plati online -> P2 (plata).')
 WHERE (position('euplatesc' in lower(COALESCE(from_address, ''))) > 0
        OR position('@europayment.services' in lower(COALESCE(from_address, ''))) > 0)
   AND sent_to_cts_at IS NULL
   AND ai_priority_manual IS NOT TRUE
   AND ai_priority IS DISTINCT FROM '2';

SELECT 'migration 20260912b_euplatesc_priority_p2 applied' AS status;
