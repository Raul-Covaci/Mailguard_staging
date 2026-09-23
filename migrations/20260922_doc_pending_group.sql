-- Grupare pagini contract multi-poza: amanarea discard-ului pana dupa autogrupare.
--
-- O pagina de MIJLOC a unui contract fotografiat (doar "ART. 6", clauze, fara titlu/numar) nu poate
-- fi identificata izolat, deci cadea sub AUTO_CONF_MIN/DOC_DISCARD_CONF_MIN si era aruncata de
-- _process_attachment INAINTE ca _autogroup_holistic sa ruleze. Gruparea gaseste 0 rinduri
-- ('status IN (extracted,classified,needs_review)') si iese, deci paginile nu se mai unesc niciodata.
--
-- pending_group = randul e pastrat PROVIZORIU, doar ca autogruparea sa il poata revendica. Ce rimine
-- nerevendicat dupa grupare se arunca cu motivul original (comportamentul vechi, doar amanat).
ALTER TABLE document_extractions
    ADD COLUMN IF NOT EXISTS pending_group boolean NOT NULL DEFAULT false;

-- Partial: interogam doar randurile provizorii ale unui email, niciodata tot tabelul.
CREATE INDEX IF NOT EXISTS idx_doc_ext_pending_group
    ON document_extractions (email_id)
    WHERE pending_group;
