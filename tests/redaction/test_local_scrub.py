"""Secrets-only index-time scrub (search guardrails U3 / R1).

Vision-free: OCR geometry is injected via a fake ``OcrResult`` so the redaction +
paint + re-encrypt + redacted-index path runs in CI's ``pytest -m privacy`` lane
with no Apple Vision. Detection uses the deterministic regex/detect-secrets/email
detectors so the assertions are stable.
"""

from __future__ import annotations

import io
import os

import pytest
from PIL import Image

from screencap import still_io
from screencap.redaction.local_scrub import LocalScrubber, create_local_scrub_pipeline
from screencap.redaction.ocr import OcrResult, OcrTextBlock

pytestmark = pytest.mark.privacy


def _jpeg(color: str = "white", size: tuple[int, int] = (240, 60)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="JPEG")
    return out.getvalue()


def _mean_brightness(jpeg_bytes: bytes) -> float:
    im = Image.open(io.BytesIO(jpeg_bytes)).convert("L")
    hist = im.histogram()
    total = sum(hist)
    return sum(i * n for i, n in enumerate(hist)) / total if total else 0.0


def _block(text: str, bbox=(4, 4, 230, 50)) -> OcrTextBlock:
    def char_bboxes(start: int, length: int):
        x, y, w, h = bbox
        cx = min(x + start * 2, x + w)
        cw = max(1, min(length * 2, w))
        return (cx, y, cw, h)

    return OcrTextBlock(text=text, bbox=bbox, char_bboxes=char_bboxes)


def _ocr(*texts: str) -> OcrResult:
    return OcrResult(
        text_blocks=[_block(t) for t in texts], image_width=240, image_height=60
    )


# ---------------------------------------------------------------------------
# LocalScrubber.scrub
# ---------------------------------------------------------------------------


def test_redacts_password_and_paints():
    res = LocalScrubber().scrub(_jpeg(), _ocr("password: hunter2secretvalue"))
    assert res.detections >= 1
    assert "hunter2secretvalue" not in res.redacted_text
    assert "PASSWORD" in res.redacted_text.upper()
    assert res.painted_bytes is not None
    Image.open(io.BytesIO(res.painted_bytes)).verify()  # valid JPEG


def test_redacts_credit_card_and_email():
    res = LocalScrubber().scrub(
        _jpeg(), _ocr("card 4111 1111 1111 1111 mail a@example.com")
    )
    assert "4111 1111 1111 1111" not in res.redacted_text
    assert "a@example.com" not in res.redacted_text
    assert res.painted_bytes is not None


def test_clean_frame_untouched():
    res = LocalScrubber().scrub(_jpeg(), _ocr("the quarterly report is due friday"))
    assert res.detections == 0
    assert res.painted_bytes is None  # no re-encode → still stays byte-identical
    assert "quarterly report" in res.redacted_text


def test_person_names_preserved():
    # KTD1: no PERSON NER in the local profile — names are legitimate search keys.
    res = LocalScrubber().scrub(_jpeg(), _ocr("meeting with John Smith re budget"))
    assert res.painted_bytes is None
    assert "John Smith" in res.redacted_text


def test_paint_darkens_the_frame():
    res = LocalScrubber().scrub(_jpeg("white"), _ocr("password: topsecretvalue123"))
    # A near-black box was painted over the secret → the frame gets darker.
    assert _mean_brightness(res.painted_bytes) < _mean_brightness(_jpeg("white"))


def test_pipeline_builds_without_ner():
    # The local pipeline must be model-free (no PERSON NER detector loaded).
    pipe = create_local_scrub_pipeline()
    detector_names = {type(d).__name__ for d in pipe._detectors}
    assert "PiiDetector" not in detector_names


# ---------------------------------------------------------------------------
# index_range integration (encrypted still → redacted index + re-painted still)
# ---------------------------------------------------------------------------


class _FakeOcr:
    def __init__(self, *texts: str):
        self._texts = texts

    def recognize(self, path, **_kw):  # pragma: no cover - scrub path uses bytes
        raise AssertionError("scrub path must OCR in-memory bytes, not a path")

    def recognize_bytes(self, data, **_kw):
        return _ocr(*self._texts)


def test_index_range_encrypted_redacts_and_repaints(tmp_path):
    from screencap.content_index import ContentIndex
    from screencap.index_core import index_range

    key = os.urandom(32)
    cap = tmp_path / "recordings" / "rec-1"
    ss = cap / "screenshots"
    ss.mkdir(parents=True)
    enc_path = ss / f"{150.0:.6f}.jpg.enc"
    still_io.write_encrypted_still(enc_path, _jpeg("white"), key)

    store = tmp_path / "content_index.db"
    ocr = _FakeOcr("password: leakedsecretvalue")

    res = index_range(
        cap, 100.0, 200.0, [], ocr=ocr, store_path=store,
        scrub=LocalScrubber(), corpus_key=key,
    )
    assert res.completed_range

    # Indexed text is redacted (secret gone, the tag/keyword survives for recall).
    with ContentIndex(store) as ix:
        assert ix.search("leakedsecretvalue").hits == []
        assert ix.search("password").hits

    # The still was re-encrypted (still decryptable) AND painted (darker).
    painted = still_io.open_still(enc_path, key)
    Image.open(io.BytesIO(painted)).verify()
    assert _mean_brightness(painted) < _mean_brightness(_jpeg("white"))
    # No plaintext .jpg was written to disk during the pass.
    assert list(ss.glob("*.jpg")) == []


def test_index_range_clean_encrypted_still_not_rewritten(tmp_path):
    from screencap.index_core import index_range

    key = os.urandom(32)
    cap = tmp_path / "recordings" / "rec-2"
    ss = cap / "screenshots"
    ss.mkdir(parents=True)
    enc_path = ss / f"{150.0:.6f}.jpg.enc"
    still_io.write_encrypted_still(enc_path, _jpeg("white"), key)
    before = enc_path.read_bytes()

    index_range(
        cap, 100.0, 200.0, [], ocr=_FakeOcr("nothing sensitive here"),
        store_path=tmp_path / "ci.db", scrub=LocalScrubber(), corpus_key=key,
    )
    # No detections → still not re-encrypted → bytes unchanged.
    assert enc_path.read_bytes() == before


def test_index_range_scrub_idempotent(tmp_path):
    from screencap.content_index import ContentIndex
    from screencap.index_core import index_range

    key = os.urandom(32)
    cap = tmp_path / "recordings" / "rec-3"
    ss = cap / "screenshots"
    ss.mkdir(parents=True)
    enc_path = ss / f"{150.0:.6f}.jpg.enc"
    still_io.write_encrypted_still(enc_path, _jpeg("white"), key)
    store = tmp_path / "content_index.db"
    ocr = _FakeOcr("password: leakedsecretvalue")

    for _ in range(2):
        index_range(
            cap, 100.0, 200.0, [], ocr=ocr, store_path=store,
            scrub=LocalScrubber(), corpus_key=key,
        )

    # write_chunk replaces the whole range → no duplicate rows on re-run.
    with ContentIndex(store) as ix:
        hits = ix.search("password").hits
        assert len(hits) == 1
    # Still remains a valid decryptable JPEG after two passes.
    Image.open(io.BytesIO(still_io.open_still(enc_path, key))).verify()


def test_plaintext_still_painted_in_place(tmp_path):
    from screencap.index_core import index_range

    cap = tmp_path / "recordings" / "rec-4"
    ss = cap / "screenshots"
    ss.mkdir(parents=True)
    jpg_path = ss / f"{150.0:.6f}.jpg"
    jpg_path.write_bytes(_jpeg("white"))

    index_range(
        cap, 100.0, 200.0, [], ocr=_FakeOcr("password: leakedsecretvalue"),
        store_path=tmp_path / "ci.db", scrub=LocalScrubber(), corpus_key=None,
    )
    # Plaintext still atomically replaced with the painted (darker) frame.
    assert jpg_path.exists()
    assert _mean_brightness(jpg_path.read_bytes()) < _mean_brightness(_jpeg("white"))
    assert list(ss.glob("*.part")) == []  # no temp left behind


def test_local_scrubber_build_failure_propagates(monkeypatch):
    # Fail-closed contract (KTD2): if the detection pipeline can't be built the
    # scrubber constructor raises, so the caller (chunk_processor) skips indexing
    # rather than indexing unredacted text.
    from screencap.redaction import local_scrub

    def _boom():
        raise ImportError("no detectors")

    monkeypatch.setattr(local_scrub, "create_local_scrub_pipeline", _boom)
    with pytest.raises(ImportError):
        LocalScrubber()
