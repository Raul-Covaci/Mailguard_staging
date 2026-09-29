"""Pipeline apeluri (audio → transcriere → clasificare → diarizare → scoring AI) — fire-and-forget.

Oglindeste pattern-ul _kick_drain / _drain_doc_extractions din documents.py.
Advisory lock key 778240 (distinct de 778231 al drain-ului de documente) — garanteaza
un singur worker activ pe cluster cu 4 gunicorn workers, fara CPU-thrash la backlog mare.
"""
import logging
import threading

from sqlalchemy import text

from app.database import SessionLocal

logger = logging.getLogger("mailguard.calls_pipeline")

_CALLS_LOCK_KEY = 778240
_pipeline_lock = threading.Lock()
_pipeline_active = False


def _run_pipeline(limit: int) -> None:
    """Executa cei 4 pasi ai pipeline-ului pe o sesiune DB dedicata pentru advisory lock."""
    lock_db = SessionLocal()
    locked = False
    try:
        locked = bool(
            lock_db.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": _CALLS_LOCK_KEY}
            ).scalar()
        )
        if not locked:
            logger.info("calls pipeline: deja in curs pe alt worker (advisory lock) — skip")
            return

        from app.services import call_audio, call_transcribe, call_classifier

        try:
            r_audio = call_audio.process_pending_batch(limit=limit)
            logger.info("calls_audio: %s", r_audio)
        except Exception:
            logger.exception("calls_audio.process_pending_batch failed")

        try:
            r_tr = call_transcribe.process_pending_batch(limit=limit)
            logger.info("calls_transcribe: %s", r_tr)
        except Exception:
            logger.exception("call_transcribe.process_pending_batch failed")

        from app.services import env_guard
        if not env_guard.ai_allowed():
            # T3-G2: în afara producției — audio + transcriere rulează, pașii AI nu.
            logger.info("calls pipeline: categorie/diarizare/scorare sarite (mediu non-productie)")
            return
        try:
            r_cls = call_classifier.process_pending_batch(limit=limit)
            logger.info("calls_classify: %s", r_cls)
        except Exception:
            logger.exception("call_classifier.process_pending_batch failed")

        try:
            r_dia = call_classifier.process_diarize_batch(limit=limit)
            logger.info("calls_diarize: %s", r_dia)
        except Exception:
            logger.exception("call_classifier.process_diarize_batch failed")

        try:
            auto_score_enabled = bool(lock_db.execute(
                text("SELECT value FROM settings WHERE key='calls.auto_score' LIMIT 1")
            ).scalar())
            if auto_score_enabled:
                from app.services import call_scorer
                r_score = call_scorer.score_batch(limit=limit)
                logger.info("calls_score: %s", r_score)
        except Exception:
            logger.exception("call_scorer.score_batch failed")

    except Exception:
        logger.exception("calls pipeline lock/setup failed")
    finally:
        if locked:
            try:
                lock_db.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": _CALLS_LOCK_KEY}
                )
            except Exception:
                pass
        lock_db.close()


def kick(limit: int = 50) -> bool:
    """Porneste pipeline-ul de apeluri intr-un thread daemon, fara suprapunere.
    Best-effort: NICIODATA nu arunca catre apelant (process_now).
    Intoarce True daca thread-ul a fost pornit, False daca era deja activ."""
    global _pipeline_active
    try:
        with _pipeline_lock:
            if _pipeline_active:
                return False
            _pipeline_active = True

        def _run():
            global _pipeline_active
            try:
                _run_pipeline(limit)
            finally:
                with _pipeline_lock:
                    _pipeline_active = False

        threading.Thread(target=_run, daemon=True).start()
        return True
    except Exception:
        logger.exception("calls_pipeline.kick failed")
        return False
