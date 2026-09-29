"""T3-D1 — limita de reluări tranzitorii în drain, normalizarea imaginilor pentru vision, o singură
buclă de retry în _vision_transcribe și BAD_JSON în ai_call_log.

Tabelele reale (attachments, document_extractions, settings, ai_call_log) stau pe un Postgres local
efemer (pgserver), cu migrația 20260929b aplicată din fișier. Gateway-ul și pipeline-ul de procesare
sunt înlocuite; imaginile se generează cu Pillow.
"""
import io
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

try:
    import pgserver
except ImportError:          # pragma: no cover
    pgserver = None

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.api.v1 import documents as D
from app.services import feature_flags, iris_ai, op_extractor, vision_image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UP = os.path.join(ROOT, "migrations", "20260929b_doc_transient_attempts.sql")
DOWN = os.path.join(ROOT, "migrations", "down", "20260929b_doc_transient_attempts.down.sql")

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key varchar(100) PRIMARY KEY, value jsonb NOT NULL, description text,
    updated_by varchar(100), updated_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS ai_call_log (
    id bigserial PRIMARY KEY, task varchar(120), model varchar(80), tokens_in integer,
    tokens_out integer, cost_usd numeric(12,6), ok boolean, error_code varchar(40),
    created_at timestamptz NOT NULL DEFAULT now(), email_id bigint);
CREATE TABLE IF NOT EXISTS emails (id bigserial PRIMARY KEY, received_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS attachments (
    id bigserial PRIMARY KEY, email_id bigint, name text, content_type text, storage_path text,
    doc_discarded boolean DEFAULT false, doc_discard_reason text, doc_discarded_at timestamptz);
CREATE TABLE IF NOT EXISTS document_extractions (
    id bigserial PRIMARY KEY, email_id bigint, attachment_id bigint, part_no integer DEFAULT 0,
    part_label text, part_bbox jsonb, document_type_id bigint, category text, detected_type text,
    confidence numeric, data jsonb, raw_text text, method text, model text, status text, error text,
    confidence_reason text, page_from integer, page_to integer, completeness_score numeric,
    retry_reclassify integer DEFAULT 0, extract_confidence numeric, extract_method text,
    reviewed boolean DEFAULT false, created_at timestamptz, extracted_at timestamptz,
    updated_at timestamptz, UNIQUE (attachment_id, part_no));
"""


def _run_file(eng, path):
    with eng.begin() as c:
        c.exec_driver_sql(open(path, encoding="utf-8").read())


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    if pgserver is None:
        pytest.fail("pgserver lipsește — testele T3-D1 cu BD rulează pe un Postgres local efemer. "
                    "Instalare: venv/bin/pip install -r requirements-dev.txt")
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("pgd1")), cleanup_mode="stop")
    eng = sa.create_engine(srv.get_uri())
    with eng.begin() as c:
        c.exec_driver_sql(BASE_SCHEMA)
    _run_file(eng, UP)
    yield eng
    eng.dispose()


@pytest.fixture
def db(pg, monkeypatch):
    with pg.begin() as c:
        c.exec_driver_sql("TRUNCATE attachments, document_extractions, emails, ai_call_log RESTART IDENTITY")
        c.exec_driver_sql("UPDATE settings SET value='false'::jsonb WHERE key LIKE 'processing.%%'")
    Session = sessionmaker(bind=pg)
    monkeypatch.setattr(feature_flags, "_session", lambda: Session())
    feature_flags.reset_cache()
    monkeypatch.setattr(D, "_track_extracted_document", lambda *a, **k: None)
    s = Session()
    yield SimpleNamespace(s=s, pg=pg, Session=Session)
    s.close()
    feature_flags.reset_cache()


def _flag(pg, key, on):
    with pg.begin() as c:
        c.execute(sa.text("UPDATE settings SET value = CAST(:v AS jsonb) WHERE key = :k"),
                  {"k": key, "v": json.dumps(on)})
    feature_flags.reset_cache()


def _new_att(pg):
    with pg.begin() as c:
        eid = c.exec_driver_sql("INSERT INTO emails DEFAULT VALUES RETURNING id").scalar()
        aid = c.exec_driver_sql(
            "INSERT INTO attachments (email_id, name, content_type) VALUES (%s, 'a.pdf', 'application/pdf') "
            "RETURNING id", (eid,)).scalar()
    return {"id": aid, "email_id": eid, "name": "a.pdf", "content_type": "application/pdf"}


def _q(pg, sql, **p):
    with pg.connect() as c:
        return c.execute(sa.text(sql), p).fetchall()


# ── 1. Limita de reluări tranzitorii ─────────────────────────────────────────

@pytest.fixture
def transient(monkeypatch):
    calls = []

    def fake_process(db_, att, force=False):
        calls.append(att["id"])
        return "retry_transient"
    monkeypatch.setattr(D, "_process_attachment", fake_process)
    return calls


def test_fourth_retry_never_calls_gateway_and_goes_to_review(db, transient):
    _flag(db.pg, D._RETRY_LIMIT_KEY, True)
    att = _new_att(db.pg)
    got = [D._drain_process(db.s, att) for _ in range(4)]
    assert got == ["retry_transient", "retry_transient", "transient_exhausted", "transient_exhausted"]
    assert len(transient) == 3, "procesarea (deci gateway-ul) a rulat și după a 3-a încercare"
    [(attempts, last)] = _q(db.pg, "SELECT doc_transient_attempts, doc_transient_last_at FROM attachments")
    assert attempts == 3 and last is not None
    [(status, err, reason)] = _q(db.pg, "SELECT status, error, confidence_reason FROM document_extractions "
                                        "WHERE attachment_id=:a AND part_no=0", a=att["id"])
    assert status == "needs_review" and err == "retry_transient x3" and "3 incercari" in reason


def test_existing_row_keeps_its_data_when_exhausted(db, transient):
    _flag(db.pg, D._RETRY_LIMIT_KEY, True)
    att = _new_att(db.pg)
    with db.pg.begin() as c:
        c.execute(sa.text("INSERT INTO document_extractions (email_id, attachment_id, part_no, status, data, "
                          "retry_reclassify) VALUES (:e, :a, 0, 'needs_review', CAST(:d AS jsonb), 1)"),
                  {"e": att["email_id"], "a": att["id"], "d": json.dumps({"VIN": "X1"})})
    for _ in range(3):
        D._drain_process(db.s, att)
    [(status, data, rr)] = _q(db.pg, "SELECT status, data, retry_reclassify FROM document_extractions")
    assert status == "needs_review" and data == {"VIN": "X1"} and rr == 2


def test_success_after_a_transient_is_left_alone(db, monkeypatch):
    _flag(db.pg, D._RETRY_LIMIT_KEY, True)
    att = _new_att(db.pg)
    seq = iter(["retry_transient", "needs_review"])
    monkeypatch.setattr(D, "_process_attachment", lambda db_, a, force=False: next(seq))
    assert D._drain_process(db.s, att) == "retry_transient"
    assert D._drain_process(db.s, att) == "needs_review"
    assert _q(db.pg, "SELECT count(*) FROM document_extractions")[0][0] == 0


def test_flag_off_retries_forever_as_before(db, transient):
    att = _new_att(db.pg)
    for _ in range(4):
        assert D._drain_process(db.s, att) == "retry_transient"
    assert len(transient) == 4
    assert _q(db.pg, "SELECT doc_transient_attempts FROM attachments")[0][0] == 0
    assert _q(db.pg, "SELECT count(*) FROM document_extractions")[0][0] == 0


def test_debounce_clause_semantics(db):
    for delta in (None, "1 minute", "11 minutes"):
        att = _new_att(db.pg)
        if delta:
            with db.pg.begin() as c:
                c.execute(sa.text("UPDATE attachments SET doc_transient_last_at = now() - CAST(:d AS interval) "
                                  "WHERE id=:a"), {"d": delta, "a": att["id"]})
    ids = [r[0] for r in _q(db.pg, "SELECT a.id FROM attachments a WHERE true" + D._TRANSIENT_DEBOUNCE_SQL
                            + " ORDER BY a.id")]
    assert ids == [1, 3], "doar atașamentul reîncercat acum 1 minut trebuie amânat"


class _FakeDB:
    """Sesiune falsă pentru drain: ține SQL-ul, lacătul reușește, nu există candidați."""
    def __init__(self, log):
        self.log = log

    def execute(self, stmt, params=None):
        self.log.append(str(stmt))
        return SimpleNamespace(scalar=lambda: True, fetchone=lambda: None, fetchall=lambda: [])

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.mark.parametrize("on", [True, False])
def test_drain_selection_uses_debounce_only_with_flag(monkeypatch, on):
    log = []
    monkeypatch.setattr(D, "SessionLocal", lambda: _FakeDB(log))
    monkeypatch.setattr(D.doc_window, "window_days", lambda db_=None: 1)
    monkeypatch.setattr(D, "_retry_limit_enabled", lambda: on)
    D._drain_doc_extractions(scope="recent")
    cand = [q for q in log if "FROM attachments a JOIN emails e" in q]
    assert cand and (("doc_transient_last_at" in cand[0]) is on)


def test_reprocess_by_ids_resets_counter_only_with_flag(db, monkeypatch):
    monkeypatch.setattr(D, "_kick_drain", lambda *a, **k: True)
    monkeypatch.setattr(D.doc_window, "window_days", lambda db_=None: 1)
    att = _new_att(db.pg)
    with db.pg.begin() as c:
        c.exec_driver_sql("UPDATE attachments SET doc_transient_attempts = 3, doc_transient_last_at = now()")
    D.documents_reprocess_by_ids({"email_ids": [att["email_id"]]}, db=db.s, admin={"username": "t"})
    assert _q(db.pg, "SELECT doc_transient_attempts FROM attachments")[0][0] == 3      # OFF: neatins
    _flag(db.pg, D._RETRY_LIMIT_KEY, True)
    D.documents_reprocess_by_ids({"email_ids": [att["email_id"]]}, db=db.s, admin={"username": "t"})
    assert _q(db.pg, "SELECT doc_transient_attempts, doc_transient_last_at FROM attachments")[0] == (0, None)


# ── 2. Normalizarea imaginilor ───────────────────────────────────────────────

def _img_bytes(fmt, size=(300, 200), mode="RGB", noise=False, **kw):
    img = Image.effect_noise(size, 90).convert(mode) if noise else Image.new(mode, size, (120, 30, 200))
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


@pytest.fixture
def norm_on(monkeypatch):
    monkeypatch.setattr(feature_flags, "is_enabled", lambda key: key == vision_image.FLAG_KEY)


@pytest.mark.parametrize("fmt,mime", [("TIFF", "image/tiff"), ("BMP", "image/bmp")])
def test_tiff_and_bmp_become_png(norm_on, fmt, mime):
    raw = _img_bytes(fmt)
    out, out_mime = vision_image.prepare(raw, mime)
    assert out_mime == "image/png"
    assert Image.open(io.BytesIO(out)).format == "PNG" and Image.open(io.BytesIO(out)).size == (300, 200)


def test_large_image_is_scaled_under_limits(norm_on):
    raw = _img_bytes("JPEG", size=(4200, 3000), noise=True, quality=98)
    assert len(raw) > vision_image.MAX_BYTES
    out, out_mime = vision_image.prepare(raw, "image/jpeg")
    im = Image.open(io.BytesIO(out))
    assert len(out) <= vision_image.MAX_BYTES and max(im.size) <= vision_image.MAX_SIDE
    assert out_mime == "image/jpeg" and abs(im.size[0] / im.size[1] - 1.4) < 0.01    # proporțional


def test_wide_but_light_image_is_scaled(norm_on):
    raw = _img_bytes("PNG", size=(3000, 1000))
    out, out_mime = vision_image.prepare(raw, "image/png")
    assert Image.open(io.BytesIO(out)).size == (2000, 667) and out_mime == "image/png"


def test_image_within_limits_is_byte_identical(norm_on):
    raw = _img_bytes("JPEG")
    assert vision_image.prepare(raw, "image/jpeg") == (raw, "image/jpeg")


def test_pdf_untouched(norm_on):
    raw = b"%PDF-1.4\n" + b"x" * (6 * 1024 * 1024)
    assert vision_image.prepare(raw, "application/pdf") == (raw, "application/pdf")


def test_flag_off_leaves_everything_as_is(monkeypatch):
    monkeypatch.setattr(feature_flags, "is_enabled", lambda key: False)
    raw = _img_bytes("TIFF")
    assert vision_image.prepare(raw, "image/tiff") == (raw, "image/tiff")


def test_cannot_fit_or_decode_means_no_ai_call(norm_on, monkeypatch):
    assert vision_image.prepare(b"II*\x00garbage", "image/tiff") is None
    small_garbage = b"\xff\xd8\xffnot-really-a-jpeg"
    assert vision_image.prepare(small_garbage, "image/jpeg") == (small_garbage, "image/jpeg")
    monkeypatch.setattr(vision_image, "MAX_BYTES", 50)
    assert vision_image.prepare(_img_bytes("TIFF", noise=True), "image/tiff") is None


# ── Integrarea în apelurile vision ───────────────────────────────────────────

@pytest.fixture
def gateway(monkeypatch):
    rp = MagicMock(return_value={"ok": True, "text": "TEXT", "parsed": None, "model": "m"})
    monkeypatch.setattr(iris_ai, "run_prompt", rp)
    monkeypatch.setattr(D, "_retry_limit_enabled", lambda: False)
    return rp


def _sent_mimes(rp):
    return [a["mime_type"] for a in rp.call_args.kwargs["attachments"]]


def test_vision_transcribe_sends_png_for_tiff(tmp_path, norm_on, gateway):
    p = tmp_path / "scan.tif"
    p.write_bytes(_img_bytes("TIFF"))
    assert D._vision_transcribe(str(p), "image/tiff") == ("TEXT", None)
    assert _sent_mimes(gateway) == ["image/png"]


def test_vision_transcribe_unfit_image_skips_ai(tmp_path, norm_on, gateway):
    p = tmp_path / "bad.tif"
    p.write_bytes(b"II*\x00garbage")
    txt, err = D._vision_transcribe(str(p), "image/tiff")
    assert txt == "" and "normalizare" in err and not gateway.called


def test_vision_transcribe_flag_off_sends_original(tmp_path, monkeypatch, gateway):
    monkeypatch.setattr(feature_flags, "is_enabled", lambda key: False)
    p = tmp_path / "scan.tif"
    p.write_bytes(_img_bytes("TIFF"))
    D._vision_transcribe(str(p), "image/tiff")
    assert _sent_mimes(gateway) == ["image/tiff"]


def test_classify_vision_and_extract_vision_normalize(tmp_path, norm_on, gateway):
    p = tmp_path / "doc.bmp"
    p.write_bytes(_img_bytes("BMP"))
    gateway.return_value = {"ok": True, "text": '{"type_id": 1}', "parsed": {"type_id": 1}, "model": "m"}
    D._classify_attachment_vision("SYS", str(p), "image/bmp", "", "doc.bmp")
    assert _sent_mimes(gateway) == ["image/png"]
    D._extract_doc_vision("SYS", [(_img_bytes("TIFF"), "image/tiff")], 1, "Talon", fields=[])
    assert _sent_mimes(gateway) == ["image/png"]


def test_op_series_normalizes(tmp_path, norm_on, monkeypatch):
    p = tmp_path / "op.tif"
    p.write_bytes(_img_bytes("TIFF"))
    rp = MagicMock(return_value={"ok": True, "text": "NONE|NONE", "model": "m"})
    monkeypatch.setattr(op_extractor.iris_ai, "run_prompt", rp)
    op_extractor._vision_extract_series(str(p), "image/tiff")
    assert _sent_mimes(rp) == ["image/png"]


# ── 3. O singură buclă de retry în _vision_transcribe ────────────────────────

@pytest.mark.parametrize("limit_on,expected", [(True, 1), (False, 3)])
def test_vision_transcribe_retry_loop(tmp_path, monkeypatch, limit_on, expected):
    monkeypatch.setattr(feature_flags, "is_enabled", lambda key: False)
    monkeypatch.setattr(D, "_retry_limit_enabled", lambda: limit_on)
    monkeypatch.setattr(D, "_ai_budget", lambda vision=False: (3, None))
    monkeypatch.setattr("time.sleep", lambda s: None)
    rp = MagicMock(return_value={"ok": False, "text": "", "error": {"code": "TRANSPORT", "message": "timeout"}})
    monkeypatch.setattr(iris_ai, "run_prompt", rp)
    p = tmp_path / "x.png"
    p.write_bytes(_img_bytes("PNG"))
    txt, err = D._vision_transcribe(str(p), "image/png")
    assert txt == "" and err == "timeout" and rp.call_count == expected


# ── 4. BAD_JSON în ai_call_log ───────────────────────────────────────────────

def test_bad_json_is_logged(db, monkeypatch):
    import app.database
    monkeypatch.setattr(app.database, "SessionLocal", db.Session)
    monkeypatch.setenv("IRIS_AI_URL", "http://gateway.test/run-prompt")
    monkeypatch.setenv("IRIS_AI_KEY", "k")
    monkeypatch.delenv("AI_DISABLED", raising=False)

    class _Resp:
        status_code = 200
        text = "<html>proxy</html>"

        def json(self):
            raise ValueError("not json")
    monkeypatch.setattr(iris_ai.httpx, "post", MagicMock(return_value=_Resp()))
    res = iris_ai.run_prompt("SYS", "content", task="cargo360:doc_classify:abc")
    assert res["ok"] is False and res["error"]["code"] == "BAD_JSON"
    [(task, ok, code, cost)] = _q(db.pg, "SELECT task, ok, error_code, cost_usd FROM ai_call_log")
    assert (task, ok, code, cost) == ("cargo360:doc_classify:abc", False, "BAD_JSON", None)


# ── Migrația ─────────────────────────────────────────────────────────────────

def test_migration_down_then_up(pg):
    _run_file(pg, DOWN)
    with pg.connect() as c:
        cols = c.exec_driver_sql("SELECT column_name FROM information_schema.columns "
                                 "WHERE table_name='attachments' AND column_name LIKE 'doc_transient%%'").fetchall()
        assert cols == []
    _run_file(pg, UP)
    _run_file(pg, UP)
    with pg.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM information_schema.columns WHERE table_name='attachments' "
                                 "AND column_name IN ('doc_transient_attempts','doc_transient_last_at')").scalar() == 2
        assert c.exec_driver_sql("SELECT value FROM settings WHERE key='processing.doc_retry_limit_enabled'"
                                 ).scalar() is False
