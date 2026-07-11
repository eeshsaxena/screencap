"""End-to-end integration of the search-by-default data plane (search guardrails).

An automated, Vision-free proxy for the plan's *manual* dev-machine E2E, composing
the real units (U2 encrypted capture, U3 secrets-scrub-at-index, U4 SQLCipher index,
U8 gate + frame.read + key-pull). It asserts the whole chain the manual test walks:

  ready system → gate defaults stills ON + encrypted → capture writes only *.jpg.enc
  (no plaintext) → the index pass scrubs secrets (a planted secret is NOT findable and
  its region is painted) while benign text IS findable → frame.read serves the scrubbed
  still → pulling the corpus key makes the next recording resolve video-only.

The only manual-E2E bits NOT covered here (they need a signed app + a human) are the
Touch ID *prompt* and the Finder *visual* — the gate/grace logic itself is unit-tested
(``test_default_gate`` / Swift ``PresenceGateTests``); here the on-disk state is
asserted directly. Requires the SQLCipher binding, so it is skipped where absent
(CI privacy lane) — matching the plan's "dev machine" framing.
"""

from __future__ import annotations

import io
import os

import pytest
from PIL import Image

pytest.importorskip("pysqlcipher3")

from screencap import (  # noqa: E402
    capture_gate,
    corpus_crypto,
    scrub_state,
    still_io,
)
from screencap.content_index import ContentIndex  # noqa: E402
from screencap.index_core import index_range  # noqa: E402
from screencap.redaction.local_scrub import LocalScrubber  # noqa: E402
from screencap.redaction.ocr import OcrResult, OcrTextBlock  # noqa: E402

pytestmark = pytest.mark.privacy

_TS = 150.0
_STEM = f"{_TS:.6f}"


def _jpeg(color="white", size=(240, 60)) -> bytes:
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
        return (min(x + start * 2, x + w), y, max(1, min(length * 2, w)), h)

    return OcrTextBlock(text=text, bbox=bbox, char_bboxes=char_bboxes)


class _FakeOcr:
    """Vision stand-in: returns a fixed transcript with geometry for the still."""

    def __init__(self, *texts: str):
        self._texts = texts

    def recognize(self, path, **_kw):  # pragma: no cover - scrub path uses bytes
        raise AssertionError("scrub path must OCR bytes")

    def recognize_bytes(self, data, **_kw):
        return OcrResult(text_blocks=[_block(t) for t in self._texts], image_width=240, image_height=60)


@pytest.fixture
def ready_system(tmp_path, monkeypatch):
    """A machine where the search guardrails are on and healthy."""
    key = os.urandom(32)
    key_file = tmp_path / "corpus.key"
    key_file.write_text(corpus_crypto._encode_key(key))
    recordings = tmp_path / "recordings"
    (recordings / "rec-e2e" / "screenshots").mkdir(parents=True)

    monkeypatch.setenv(corpus_crypto.CORPUS_KEY_FILE_ENV, str(key_file))
    monkeypatch.setenv("SCREENCAP_CORPUS_ENCRYPTED", "1")
    monkeypatch.setenv("SCREENCAP_SCREENSHOT_RETENTION_DAYS", "30")
    monkeypatch.setenv("SCREENCAP_SEARCH_DISCLOSURE_ACKNOWLEDGED", "1")
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings))
    from screencap import config

    config.invalidate_config_cache()
    return {
        "key": key,
        "key_file": key_file,
        "recordings": recordings,
        "rec_dir": recordings / "rec-e2e",
        "store": tmp_path / "content_index.db",
    }


def test_search_guardrails_end_to_end(ready_system, monkeypatch):
    key = ready_system["key"]
    rec_dir = ready_system["rec_dir"]
    ss = rec_dir / "screenshots"
    store = ready_system["store"]

    # 1) Ready system → the gate defaults stills ON and encrypted (U8).
    gate = capture_gate.gather_and_resolve(explicit_capture_images=None, scrub_enabled=True)
    assert gate.capture_images is True
    assert gate.capture_images_encrypted is True
    assert gate.reason == "default_on"

    # 2) Capture writes only *.jpg.enc — no plaintext on disk (U2 / "Finder shows only .jpg.enc").
    still_io.write_encrypted_still(ss / f"{_STEM}.jpg.enc", _jpeg("white"), key)
    assert [p.name for p in ss.iterdir()] == [f"{_STEM}.jpg.enc"]
    assert not any(p.name.endswith(".jpg") for p in ss.iterdir())

    # 3) The index pass scrubs secrets (U3) and writes to the SQLCipher index (U4):
    #    the planted secret is NOT findable + its region is painted; benign text IS.
    ocr = _FakeOcr("password: plantedsecretvalue and the quarterly report is due")
    result = index_range(
        rec_dir, 100.0, 200.0, [], ocr=ocr, store_path=store,
        scrub=LocalScrubber(), corpus_key=key,
    )
    assert result.completed_range
    scrub_state.mark_chunk_scrubbed(rec_dir, 100_000, 200_000)

    with ContentIndex(store) as ix:  # auto-encrypted: corpus_encrypted + key present
        assert ix.available
        assert ix.search("plantedsecretvalue").hits == []  # secret scrubbed out
        assert ix.search("quarterly").hits  # benign on-screen text found

    painted = still_io.open_still(ss / f"{_STEM}.jpg.enc", key)
    assert _mean_brightness(painted) < _mean_brightness(_jpeg("white"))  # region painted

    # 4) frame.read serves the ALLOW, scrubbed still (U8). (No recording.db here → the
    #    blocked-frame predicate is stubbed allow-all; ALLOW filtering is tested in
    #    test_frame_read_verb / frame.nearest.)
    from screencap import frame_blocked
    from screencap.daemon.app import _run_frame_read

    monkeypatch.setattr(frame_blocked, "build_is_blocked", lambda *a, **k: (lambda _ts: False))
    served = _run_frame_read("rec-e2e", _STEM, max_bytes=8_000_000)
    assert served is not None
    assert served[1] == "image/jpeg"
    Image.open(io.BytesIO(served[0])).verify()

    # 5) Pull the corpus key → the next recording resolves video-only (U8 / R8).
    ready_system["key_file"].unlink()
    from screencap import config

    config.invalidate_config_cache()
    gate_after = capture_gate.gather_and_resolve(explicit_capture_images=None, scrub_enabled=True)
    assert gate_after.capture_images is False
    assert gate_after.capture_images_encrypted is False
    assert gate_after.reason == "default_off_not_ready"
