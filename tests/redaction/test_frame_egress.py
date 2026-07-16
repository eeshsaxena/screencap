"""SCR-272 U1 — frame-egress eligibility + masked-still producer.

Vision-free privacy tests for ``screencap.segmentation.frame_egress``. The
load-bearing invariants (from the plan) are:

* AE4 / R10 — when ``derive_skip_intervals(require_canonical=True)`` raises
  (missing / partial ``recording.db``), the producer returns ZERO frames, never
  the raw frame.
* AE2 / R8 — a frame overlapping a blocked-app interval is excluded.
* R9 (no-mutation) — an ALLOW frame with a residual region returns masked bytes
  AND the on-disk original is byte-identical afterward.
* A frame whose masking raises is dropped, not sent raw.
* Dedup collapses near-identical consecutive frames.

OCR region detection is mocked (injected ``detect_regions``) so nothing here
touches Apple Vision.
"""

from __future__ import annotations

import contextlib
import io
import sqlite3
from pathlib import Path

import pytest

from screencap.privacy.mask_primitives import MaskRegion
from screencap.privacy.policy import PrivacyMode
from screencap.segmentation.frame_egress import EgressFrame, produce_egress_frames

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gradient_jpeg(path: Path, *, vertical: bool, size: int = 64) -> None:
    """Write a grayscale gradient JPEG (a non-degenerate dHash source)."""
    from PIL import Image

    img = Image.new("RGB", (size, size))
    px = img.load()
    for x in range(size):
        for y in range(size):
            v = int(255 * (y / size if vertical else x / size))
            px[x, y] = (v, v, v)
    img.save(path, "JPEG", quality=85)


def _white_jpeg(path: Path, *, size: int = 64) -> None:
    from PIL import Image

    Image.new("RGB", (size, size), (255, 255, 255)).save(path, "JPEG", quality=85)


def _avg_brightness(data: bytes) -> float:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        gray = im.convert("L")
        pixels = gray.getdata()
        return sum(pixels) / len(pixels)


def _make_window_db(
    path: Path, rows: list[tuple[float, str, str]]
) -> None:
    """Minimal recording.db with ``window_event`` rows (ts, bundle, title)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(str(path))) as db:
        db.execute(
            "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, pixel_ratio REAL)"
        )
        db.execute("INSERT INTO recording VALUES (1, 0.0, 2.0)")
        db.execute(
            "CREATE TABLE window_event ("
            "id INTEGER PRIMARY KEY, recording_id INTEGER, timestamp REAL, "
            "app_bundle_id TEXT, window_id TEXT, title TEXT, state TEXT, "
            "app_name TEXT, browser_url TEXT)"
        )
        for i, (ts, bundle, title) in enumerate(rows):
            db.execute(
                "INSERT INTO window_event "
                "(id, recording_id, timestamp, app_bundle_id, window_id, title) "
                "VALUES (?, 1, ?, ?, ?, ?)",
                (i + 1, ts, bundle, f"w{i}", title),
            )
        db.commit()


def _stub_all_allow(monkeypatch) -> None:
    """Make the structural gate treat every frame as ALLOW (empty block set)."""
    monkeypatch.setattr(
        "screencap.backfill.skip_intervals.build_classifier_evaluator",
        lambda *a, **k: (None, None),
    )
    monkeypatch.setattr(
        "screencap.backfill.skip_intervals.derive_skip_intervals",
        lambda *a, **k: [],
    )


def _use_public_classifier(monkeypatch) -> None:
    """Pin a deterministic, host-config-independent PUBLIC classifier/evaluator."""
    from screencap.backfill.skip_intervals import build_classifier_evaluator

    classifier, evaluator = build_classifier_evaluator(mode=PrivacyMode.PUBLIC)
    monkeypatch.setattr(
        "screencap.backfill.skip_intervals.build_classifier_evaluator",
        lambda *a, **k: (classifier, evaluator),
    )


# ---------------------------------------------------------------------------
# AE4 / R10 — fail closed when ALLOW eligibility cannot be derived
# ---------------------------------------------------------------------------


def test_derive_raises_returns_zero_frames(tmp_path, monkeypatch):
    """When ``derive_skip_intervals(require_canonical=True)`` raises, the producer
    returns ZERO frames — the raw on-disk frames are never emitted."""
    from screencap.backfill.skip_intervals import CanonicalDerivationError

    monkeypatch.setattr(
        "screencap.backfill.skip_intervals.build_classifier_evaluator",
        lambda *a, **k: (None, None),
    )

    def _boom(*a, **k):
        raise CanonicalDerivationError("partial recording.db")

    monkeypatch.setattr(
        "screencap.backfill.skip_intervals.derive_skip_intervals", _boom
    )

    rec = tmp_path / "rec"
    shots = rec / "screenshots"
    shots.mkdir(parents=True)
    _white_jpeg(shots / "150.0.jpg")
    _white_jpeg(shots / "250.0.jpg")

    frames = produce_egress_frames(
        rec, 0.0, 1000.0, detect_regions=lambda b: []
    )
    assert frames == []
    # The raw frames are still on disk (we did not delete or emit them).
    assert (shots / "150.0.jpg").exists()
    assert (shots / "250.0.jpg").exists()


def test_missing_recording_db_returns_zero_frames(tmp_path, monkeypatch):
    """A missing recording.db → structural fail-closed → zero frames."""
    monkeypatch.setattr(
        "screencap.backfill.skip_intervals.build_classifier_evaluator",
        lambda *a, **k: (None, None),
    )
    rec = tmp_path / "rec"
    shots = rec / "screenshots"
    shots.mkdir(parents=True)
    _white_jpeg(shots / "300.0.jpg")
    assert not (rec / "recording.db").exists()

    frames = produce_egress_frames(
        rec, 0.0, 1000.0, detect_regions=lambda b: []
    )
    assert frames == []


# ---------------------------------------------------------------------------
# AE2 / R8 — a frame inside a blocked-app interval is excluded
# ---------------------------------------------------------------------------


def test_blocked_app_frame_excluded(tmp_path, monkeypatch):
    """A frame under a 1Password (EXCLUDE) window is dropped; a frame under an
    ALLOW window survives."""
    _use_public_classifier(monkeypatch)

    rec = tmp_path / "rec"
    _make_window_db(
        rec / "recording.db",
        [
            (100.0, "com.1password.1password", "Vault"),  # [100, 200) blocked
            (200.0, "com.apple.TextEdit", "Notes"),       # [200, inf) allow
        ],
    )
    shots = rec / "screenshots"
    shots.mkdir(parents=True)
    _white_jpeg(shots / "150.0.jpg")  # under 1Password -> blocked
    _white_jpeg(shots / "250.0.jpg")  # under TextEdit -> allow

    frames = produce_egress_frames(
        rec, 0.0, 1000.0, detect_regions=lambda b: []
    )
    assert [f.timestamp_ms for f in frames] == [250_000]


# ---------------------------------------------------------------------------
# R9 — masked bytes returned, on-disk original never mutated
# ---------------------------------------------------------------------------


def test_allow_frame_masked_and_original_unchanged(tmp_path, monkeypatch):
    """An ALLOW frame with a residual region returns masked (darker) bytes, and
    the on-disk original is byte-identical afterward (never mutated)."""
    _stub_all_allow(monkeypatch)

    rec = tmp_path / "rec"
    shots = rec / "screenshots"
    shots.mkdir(parents=True)
    frame_path = shots / "300.0.jpg"
    _white_jpeg(frame_path)
    original_bytes = frame_path.read_bytes()
    original_brightness = _avg_brightness(original_bytes)

    # Residual OCR "detects" the whole frame as sensitive -> full mask.
    def _full_mask(_img_bytes: bytes) -> list[MaskRegion]:
        return [MaskRegion(x=0, y=0, width=64, height=64, label="residual")]

    frames = produce_egress_frames(rec, 0.0, 1000.0, detect_regions=_full_mask)

    assert len(frames) == 1
    assert isinstance(frames[0], EgressFrame)
    assert frames[0].timestamp_ms == 300_000
    # Masked bytes are darker than the white original (residual paint applied).
    assert _avg_brightness(frames[0].jpeg_bytes) < original_brightness * 0.3
    # The on-disk original is byte-for-byte unchanged.
    assert frame_path.read_bytes() == original_bytes


# ---------------------------------------------------------------------------
# A frame whose masking raises is dropped, not sent raw
# ---------------------------------------------------------------------------


def test_masking_raises_frame_dropped_not_sent_raw(tmp_path, monkeypatch):
    """If residual masking raises for a frame, that frame is dropped — never
    emitted raw — and the on-disk original is untouched."""
    _stub_all_allow(monkeypatch)

    rec = tmp_path / "rec"
    shots = rec / "screenshots"
    shots.mkdir(parents=True)
    frame_path = shots / "300.0.jpg"
    _white_jpeg(frame_path)
    original_bytes = frame_path.read_bytes()

    def _boom(_img_bytes: bytes) -> list[MaskRegion]:
        raise RuntimeError("OCR/masking exploded")

    frames = produce_egress_frames(rec, 0.0, 1000.0, detect_regions=_boom)

    assert frames == []
    assert frame_path.read_bytes() == original_bytes


def test_undecodable_still_dropped(tmp_path, monkeypatch):
    """An undecodable still (masking raises on decode) is dropped, not emitted."""
    _stub_all_allow(monkeypatch)

    rec = tmp_path / "rec"
    shots = rec / "screenshots"
    shots.mkdir(parents=True)
    (shots / "300.0.jpg").write_bytes(b"not a jpeg")

    frames = produce_egress_frames(
        rec, 0.0, 1000.0, detect_regions=lambda b: []
    )
    assert frames == []


# ---------------------------------------------------------------------------
# Dedup collapses near-identical consecutive frames
# ---------------------------------------------------------------------------


def test_dedup_collapses_near_identical(tmp_path, monkeypatch):
    """Two identical consecutive ALLOW frames collapse to one; a visually
    distinct third frame survives."""
    _stub_all_allow(monkeypatch)

    rec = tmp_path / "rec"
    shots = rec / "screenshots"
    shots.mkdir(parents=True)
    _gradient_jpeg(shots / "250.0.jpg", vertical=False)  # kept
    _gradient_jpeg(shots / "251.0.jpg", vertical=False)  # dup of 250 -> dropped
    _gradient_jpeg(shots / "252.0.jpg", vertical=True)   # distinct -> kept

    frames = produce_egress_frames(
        rec, 0.0, 1000.0, detect_regions=lambda b: []
    )
    assert [f.timestamp_ms for f in frames] == [250_000, 252_000]
