"""U6 — post-hoc video-frame masking, fail-closed.

The highest privacy-risk unit: a silent no-op here = unscrubbed video uploaded.
Tests are written FAIL-CLOSED-FIRST — every way coverage can be unprovable or
the machinery can fail must yield a FAILED outcome with NO masked copy, asserted
BEFORE the masking happy path. Small synthetic mp4 fixtures (a few solid-color
frames built with PyAV) genuinely exercise decode/re-encode on every path.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from fractions import Fraction
from pathlib import Path

import av
import pytest
from PIL import Image

from screencap.video_mask import (
    MAX_GEOMETRY_GAP_SECONDS,
    MaskOutcomeStatus,
    mask_video_chunk,
)

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Fixtures: tiny real mp4s + a minimal recording.db with geometry tables.
# ---------------------------------------------------------------------------

WIDTH = 64
HEIGHT = 48
FPS = 10
# A 1-second chunk at 10 fps. Frame i is at PTS == i (time_base 1/FPS), so
# frame i's seconds-from-start == i / FPS. With chunk_start_abs == start_ts,
# frame i's absolute ts == start_ts + i/FPS.
N_FRAMES = 10


def _make_mp4(path: Path, *, color=(200, 30, 30), n_frames: int = N_FRAMES) -> None:
    """Write a tiny solid-color mp4 with deterministic per-frame PTS."""
    container = av.open(str(path), mode="w", format="mp4")
    stream = container.add_stream("libx264", rate=FPS)
    stream.width = WIDTH
    stream.height = HEIGHT
    stream.pix_fmt = "yuv420p"
    stream.codec_context.time_base = Fraction(1, FPS)
    stream.options = {"crf": "23", "preset": "ultrafast", "g": "1", "bf": "0"}
    for i in range(n_frames):
        img = Image.new("RGB", (WIDTH, HEIGHT), color)
        frame = av.VideoFrame.from_image(img)
        frame.pts = i
        frame.time_base = Fraction(1, FPS)
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()


def _make_db(
    path: Path,
    *,
    geometry: list[tuple[float, list[dict]]],
    failures: list[float] | None = None,
    with_failure_table: bool = True,
    pixel_ratio: float = 1.0,
) -> None:
    """Create a minimal recording.db with window_geometry (+ failure) rows.

    geometry: list of (screenshot_timestamp, window_list) — each window_list a
    list of dicts with keys x/y/width/height/bundle_id/app_name.
    """
    with contextlib.closing(sqlite3.connect(str(path))) as db:
        db.execute(
            "CREATE TABLE recording (id INTEGER PRIMARY KEY, pixel_ratio REAL)"
        )
        db.execute("INSERT INTO recording (id, pixel_ratio) VALUES (1, ?)", (pixel_ratio,))
        db.execute(
            "CREATE TABLE window_geometry ("
            "id INTEGER PRIMARY KEY, recording_id INTEGER, "
            "recording_timestamp REAL, screenshot_timestamp REAL, "
            "window_list_json TEXT)"
        )
        for ts, windows in geometry:
            payload = json.dumps(
                {"windows": windows, "display_bounds": [0, 0, WIDTH, HEIGHT]}
            )
            db.execute(
                "INSERT INTO window_geometry "
                "(recording_id, screenshot_timestamp, window_list_json) "
                "VALUES (1, ?, ?)",
                (ts, payload),
            )
        if with_failure_table:
            db.execute(
                "CREATE TABLE window_geometry_capture_failure ("
                "id INTEGER PRIMARY KEY, recording_id INTEGER, "
                "recording_timestamp REAL, screenshot_timestamp REAL, detail TEXT)"
            )
            for ts in failures or []:
                db.execute(
                    "INSERT INTO window_geometry_capture_failure "
                    "(recording_id, screenshot_timestamp, detail) VALUES (1, ?, ?)",
                    (ts, "insert failed"),
                )
        db.commit()


# A sensitive window (password manager → EXCLUDE → in VIDEO_BLOCK_ACTIONS) and
# a benign one (code editor → TEXT_REDACT → NOT a video-block action).
_SENSITIVE_WIN = {
    "bundle_id": "com.1password.1password",
    "app_name": "1Password",
    "x": 8, "y": 8, "width": 32, "height": 24,
}
_BENIGN_WIN = {
    "bundle_id": "com.apple.Terminal",
    "app_name": "Terminal",
    "x": 0, "y": 0, "width": WIDTH, "height": HEIGHT,
}


def _is_masked_fill(px: tuple[int, int, int], tol: int = 12) -> bool:
    """True if a decoded pixel is (near) the opaque mask fill.

    H.264 is lossy, so the (30,30,30) fill decodes to ~(28,28,28); assert
    proximity to the fill AND clear distance from the bright source color
    (200,30,30), which is the privacy-relevant property (no content bleed).
    """
    from screencap.video_mask import _MASK_FILL

    return all(abs(a - b) <= tol for a, b in zip(px, _MASK_FILL))


def _dense_samples(start: float, end: float, windows: list[dict]) -> list[tuple[float, list[dict]]]:
    """Geometry samples every 0.5s (< MAX gap) across [start, end]."""
    out = []
    t = start
    step = 0.5
    while t <= end + 1e-9:
        out.append((round(t, 3), list(windows)))
        t += step
    return out


@pytest.fixture
def chunk_mp4(tmp_path) -> Path:
    p = tmp_path / "chunk_0000.mp4"
    _make_mp4(p)
    return p


@pytest.fixture
def scrubbed_out(tmp_path) -> Path:
    # Sibling-style scrubbed dir; masker writes the output here.
    d = tmp_path / "rec-scrubbed" / "masked_video"
    d.mkdir(parents=True, exist_ok=True)
    return d / "chunk_0000.mp4"


# ---------------------------------------------------------------------------
# FAIL-CLOSED PATHS (written first — silent no-op is most dangerous here).
# ---------------------------------------------------------------------------


def test_failclosed_geometry_capture_failure_marker(chunk_mp4, scrubbed_out, tmp_path):
    """Case (b): a capture-failure marker in the span → FAILED, no copy."""
    db = tmp_path / "recording.db"
    # Dense, sensitive-free geometry that WOULD otherwise pass case (a) — but a
    # capture-failure marker in the span makes coverage unprovable.
    _make_db(
        db,
        geometry=_dense_samples(0.0, 1.0, []),
        failures=[0.4],
    )
    outcome = mask_video_chunk(
        chunk_mp4, db, start_ts=0.0, end_ts=1.0, chunk_start_abs=0.0,
        output_path=scrubbed_out, pixel_ratio=1.0,
    )
    assert outcome.status is MaskOutcomeStatus.FAILED
    assert outcome.output_path is None
    assert not scrubbed_out.exists()
    assert "coverage_unprovable" in outcome.reason


def test_failclosed_no_geometry_samples(chunk_mp4, scrubbed_out, tmp_path):
    """Case (b): empty geometry for the span → FAILED, never 'unmasked safe'.

    "No geometry sample" must NOT be treated as "0 expected, 0 masked."
    """
    db = tmp_path / "recording.db"
    _make_db(db, geometry=[])  # no samples in span at all
    outcome = mask_video_chunk(
        chunk_mp4, db, start_ts=0.0, end_ts=1.0, chunk_start_abs=0.0,
        output_path=scrubbed_out, pixel_ratio=1.0,
    )
    assert outcome.status is MaskOutcomeStatus.FAILED
    assert outcome.output_path is None
    assert not scrubbed_out.exists()


def test_failclosed_sparse_gap_exceeds_bound(scrubbed_out, tmp_path):
    """Case (b): an inter-sample gap > MAX_GEOMETRY_GAP_SECONDS → FAILED.

    The chunk's real frames span the gap (the masker derives the span from the
    chunk's PTS extent, so the gap must be within the frames that exist)."""
    db = tmp_path / "recording.db"
    end = MAX_GEOMETRY_GAP_SECONDS + 1.0
    # A chunk whose decoded frames actually span [0, end] (frame i at i/FPS).
    long_chunk = tmp_path / "chunk_long.mp4"
    _make_mp4(long_chunk, n_frames=int(round(end * FPS)) + 1)
    _make_db(
        db,
        geometry=[(0.0, []), (end, [])],  # gap == end > bound
    )
    outcome = mask_video_chunk(
        long_chunk, db, start_ts=0.0, end_ts=end, chunk_start_abs=0.0,
        output_path=scrubbed_out, pixel_ratio=1.0,
    )
    assert outcome.status is MaskOutcomeStatus.FAILED
    assert not scrubbed_out.exists()
    assert "gap" in outcome.reason


def test_failclosed_leading_edge_gap(scrubbed_out, tmp_path):
    """Case (b): span start → first sample gap > bound → FAILED.

    A sample exists, but it is too far from the span start: the leading frames
    have no nearby observation. Must fail closed (not pass on the lone sample).
    """
    db = tmp_path / "recording.db"
    end = MAX_GEOMETRY_GAP_SECONDS + 1.0
    long_chunk = tmp_path / "chunk_long.mp4"
    _make_mp4(long_chunk, n_frames=int(round(end * FPS)) + 1)
    # Single sample near the END; the leading edge is uncovered.
    _make_db(db, geometry=[(end, [])])
    outcome = mask_video_chunk(
        long_chunk, db, start_ts=0.0, end_ts=end, chunk_start_abs=0.0,
        output_path=scrubbed_out, pixel_ratio=1.0,
    )
    assert outcome.status is MaskOutcomeStatus.FAILED
    assert not scrubbed_out.exists()


def test_failclosed_pyav_decode_error(scrubbed_out, tmp_path):
    """A corrupt/undecodable chunk → FAILED, no copy (machinery unavailable)."""
    db = tmp_path / "recording.db"
    # Geometry proves a sensitive window so we reach the transcode step.
    _make_db(db, geometry=_dense_samples(0.0, 1.0, [_SENSITIVE_WIN]))
    bad = tmp_path / "chunk_0000.mp4"
    bad.write_bytes(b"not a real mp4 file at all")
    outcome = mask_video_chunk(
        bad, db, start_ts=0.0, end_ts=1.0, chunk_start_abs=0.0,
        output_path=scrubbed_out, pixel_ratio=1.0,
    )
    assert outcome.status is MaskOutcomeStatus.FAILED
    assert outcome.output_path is None
    assert not scrubbed_out.exists()
    # A corrupt chunk now fails closed at the PTS-extent probe (the first read,
    # before coverage/transcode) — the unreadable extent is itself unprovable.
    assert "pts_extent_unprovable" in outcome.reason


def test_failclosed_classifier_unavailable(chunk_mp4, scrubbed_out, tmp_path, monkeypatch):
    """If the policy classifier can't be built → FAILED, never a clear copy."""
    db = tmp_path / "recording.db"
    _make_db(db, geometry=_dense_samples(0.0, 1.0, [_SENSITIVE_WIN]))

    import screencap.config as cfg

    def _boom():
        raise RuntimeError("classifier model failed to load")

    monkeypatch.setattr(cfg, "get_privacy_config", _boom)
    outcome = mask_video_chunk(
        chunk_mp4, db, start_ts=0.0, end_ts=1.0, chunk_start_abs=0.0,
        output_path=scrubbed_out, pixel_ratio=1.0,
    )
    assert outcome.status is MaskOutcomeStatus.FAILED
    assert not scrubbed_out.exists()
    assert "classifier_unavailable" in outcome.reason


def test_failclosed_silent_noop_guard(chunk_mp4, scrubbed_out, tmp_path, monkeypatch):
    """Sensitive windows expected but masker draws nothing → FAILED, drop copy.

    A misconfigured renderer that produces zero rectangles on a sensitive chunk
    must fail closed rather than uploading the (clear) re-encode.
    """
    db = tmp_path / "recording.db"
    _make_db(db, geometry=_dense_samples(0.0, 1.0, [_SENSITIVE_WIN]))

    import screencap.video_mask as vm

    # Force the per-frame rect conversion to yield nothing despite a sensitive
    # interval being present — simulates a coordinate/policy misconfig.
    monkeypatch.setattr(vm, "_windows_to_rects", lambda *a, **k: [])
    outcome = mask_video_chunk(
        chunk_mp4, db, start_ts=0.0, end_ts=1.0, chunk_start_abs=0.0,
        output_path=scrubbed_out, pixel_ratio=1.0,
    )
    assert outcome.status is MaskOutcomeStatus.FAILED
    assert not scrubbed_out.exists()
    assert "masked_nothing" in outcome.reason


# ---------------------------------------------------------------------------
# CASE (a): geometry proves no sensitive window → faithful unmasked copy.
# ---------------------------------------------------------------------------


def test_case_a_provably_safe_unmasked(chunk_mp4, scrubbed_out, tmp_path):
    db = tmp_path / "recording.db"
    # Dense coverage, only a benign (non-video-block) window.
    _make_db(db, geometry=_dense_samples(0.0, 1.0, [_BENIGN_WIN]))
    outcome = mask_video_chunk(
        chunk_mp4, db, start_ts=0.0, end_ts=1.0, chunk_start_abs=0.0,
        output_path=scrubbed_out, pixel_ratio=1.0,
    )
    assert outcome.status is MaskOutcomeStatus.UNMASKED_PROVABLY_SAFE
    assert outcome.regions_masked == 0
    assert outcome.output_path == scrubbed_out
    assert scrubbed_out.exists()
    assert "no sensitive window" in outcome.reason
    # The copy is a real, decodable video.
    c = av.open(str(scrubbed_out))
    assert c.streams.video
    c.close()


# ---------------------------------------------------------------------------
# CASE (c): sensitive windows present → masked copy, regions > 0.
# ---------------------------------------------------------------------------


def test_case_c_masked_regions_present(chunk_mp4, scrubbed_out, tmp_path):
    db = tmp_path / "recording.db"
    _make_db(db, geometry=_dense_samples(0.0, 1.0, [_SENSITIVE_WIN]))
    outcome = mask_video_chunk(
        chunk_mp4, db, start_ts=0.0, end_ts=1.0, chunk_start_abs=0.0,
        output_path=scrubbed_out, pixel_ratio=1.0,
    )
    assert outcome.status is MaskOutcomeStatus.MASKED
    assert outcome.regions_masked > 0
    assert scrubbed_out.exists()

    # The masked region's pixels are the opaque fill (not the source color).
    c = av.open(str(scrubbed_out))
    frame = next(c.decode(c.streams.video[0]))
    img = frame.to_image().convert("RGB")
    c.close()
    # Sensitive win at (8,8,32x24); sample a pixel well inside it.
    px = img.getpixel((20, 20))
    assert _is_masked_fill(px), f"masked pixel not opaque fill: {px}"
    # A pixel far outside the (dilated) window keeps source color (200,30,30).
    corner = img.getpixel((WIDTH - 1, HEIGHT - 1))
    assert not _is_masked_fill(corner)


def test_case_c_moving_window_overmasked(chunk_mp4, scrubbed_out, tmp_path):
    """A window that moves between two samples is conservatively over-masked.

    Sample 0 has the window at the left; sample 1 (0.5s later) has it moved
    right. Hold-and-pad means the window is masked across the whole interval at
    its sampled position — the moved-to region at the boundary frame is covered
    by the held rectangle, never point-interpolated into a partial gap.
    """
    db = tmp_path / "recording.db"
    left = {**_SENSITIVE_WIN, "x": 4, "y": 8, "width": 24, "height": 24}
    right = {**_SENSITIVE_WIN, "x": 36, "y": 8, "width": 24, "height": 24}
    _make_db(
        db,
        geometry=[(0.0, [left]), (0.5, [right]), (1.0, [right])],
    )
    outcome = mask_video_chunk(
        chunk_mp4, db, start_ts=0.0, end_ts=1.0, chunk_start_abs=0.0,
        output_path=scrubbed_out, pixel_ratio=1.0,
    )
    assert outcome.status is MaskOutcomeStatus.MASKED
    assert outcome.regions_masked > 0

    c = av.open(str(scrubbed_out))
    frames = list(c.decode(c.streams.video[0]))
    c.close()
    # Frame at t≈0.0 (frame 0): left position masked.
    img0 = frames[0].to_image().convert("RGB")
    assert _is_masked_fill(img0.getpixel((12, 16)))
    # Frame at t≈0.6 (frame 6): held right position masked.
    img6 = frames[6].to_image().convert("RGB")
    assert _is_masked_fill(img6.getpixel((44, 16)))


# ---------------------------------------------------------------------------
# AE1: local video untouched — only the materialized copy is masked.
# ---------------------------------------------------------------------------


def test_ae1_local_chunk_untouched(chunk_mp4, scrubbed_out, tmp_path):
    db = tmp_path / "recording.db"
    _make_db(db, geometry=_dense_samples(0.0, 1.0, [_SENSITIVE_WIN]))
    before = chunk_mp4.read_bytes()
    mask_video_chunk(
        chunk_mp4, db, start_ts=0.0, end_ts=1.0, chunk_start_abs=0.0,
        output_path=scrubbed_out, pixel_ratio=1.0,
    )
    after = chunk_mp4.read_bytes()
    assert before == after, "source (local) chunk was mutated"
    # And the masked copy lives at a DIFFERENT path (the scrubbed sibling dir).
    assert scrubbed_out != chunk_mp4
    assert scrubbed_out.exists()


# ---------------------------------------------------------------------------
# AE10: crash mid-mask leaves no complete copy; re-run re-masks.
# ---------------------------------------------------------------------------


def test_ae10_crash_midmask_no_partial(chunk_mp4, scrubbed_out, tmp_path, monkeypatch):
    """A crash during transcode leaves NO complete copy at output_path.

    Simulate a mid-encode crash by raising partway through the decode loop.
    The atomic temp+rename gate means output_path never appears; only a temp
    (which is dot-prefixed and swept on re-run) could remain.
    """
    db = tmp_path / "recording.db"
    _make_db(db, geometry=_dense_samples(0.0, 1.0, [_SENSITIVE_WIN]))

    import screencap.video_mask as vm

    real_rects = vm._windows_to_rects
    calls = {"n": 0}

    def _crash_after_two(*a, **k):
        calls["n"] += 1
        if calls["n"] > 2:
            raise RuntimeError("simulated crash mid-mask")
        return real_rects(*a, **k)

    monkeypatch.setattr(vm, "_windows_to_rects", _crash_after_two)
    outcome = mask_video_chunk(
        chunk_mp4, db, start_ts=0.0, end_ts=1.0, chunk_start_abs=0.0,
        output_path=scrubbed_out, pixel_ratio=1.0,
    )
    assert outcome.status is MaskOutcomeStatus.FAILED
    assert not scrubbed_out.exists(), "a partial masked copy is present (uploadable!)"

    # Re-run cleanly (no crash) → re-detects incompleteness and produces a copy.
    monkeypatch.setattr(vm, "_windows_to_rects", real_rects)
    outcome2 = mask_video_chunk(
        chunk_mp4, db, start_ts=0.0, end_ts=1.0, chunk_start_abs=0.0,
        output_path=scrubbed_out, pixel_ratio=1.0,
    )
    assert outcome2.status is MaskOutcomeStatus.MASKED
    assert scrubbed_out.exists()
    # No leftover .tmp files in the output dir.
    leftover = list(scrubbed_out.parent.glob(".*tmp"))
    assert not leftover, f"orphan temp files left behind: {leftover}"


# ---------------------------------------------------------------------------
# Outcome-status discipline: provably-safe vs failed are DISTINCT.
# ---------------------------------------------------------------------------


def test_provably_safe_distinct_from_failed(chunk_mp4, scrubbed_out, tmp_path):
    """Case (a) and case (b) both 'mask nothing' but are NOT the same status."""
    db_safe = tmp_path / "safe.db"
    _make_db(db_safe, geometry=_dense_samples(0.0, 1.0, [_BENIGN_WIN]))
    safe = mask_video_chunk(
        chunk_mp4, db_safe, start_ts=0.0, end_ts=1.0, chunk_start_abs=0.0,
        output_path=scrubbed_out, pixel_ratio=1.0,
    )

    db_bad = tmp_path / "bad.db"
    _make_db(db_bad, geometry=[])  # unprovable
    out2 = tmp_path / "rec-scrubbed" / "masked_video" / "chunk_0001.mp4"
    out2.parent.mkdir(parents=True, exist_ok=True)
    bad = mask_video_chunk(
        chunk_mp4, db_bad, start_ts=0.0, end_ts=1.0, chunk_start_abs=0.0,
        output_path=out2, pixel_ratio=1.0,
    )

    assert safe.status is MaskOutcomeStatus.UNMASKED_PROVABLY_SAFE
    assert bad.status is MaskOutcomeStatus.FAILED
    assert safe.status is not bad.status
    assert safe.ok and not bad.ok


# ---------------------------------------------------------------------------
# SCR-126 Fix 2: span derived from the chunk's real decoded PTS extent, plus
# the fail-closed in-loop guard and the origin-bracketing backstop.
# ---------------------------------------------------------------------------


def test_scr126_trailing_frames_masked_when_caller_span_too_short(
    chunk_mp4, scrubbed_out, tmp_path
):
    """The trailing-frame leak: a caller span shorter than the chunk's real
    frames must NOT leave trailing frames clear while reporting MASKED.

    Pre-fix, intervals were built only to the caller's (short) end_ts, so frames
    beyond it got no interval and were drawn CLEAR while regions>0 still reported
    MASKED. Post-fix the masker derives the span from the chunk's PTS extent, so
    EVERY frame (incl. the trailing ones) is masked. Decoding a trailing frame
    proves the sensitive region is actually filled."""
    db = tmp_path / "recording.db"
    # Sensitive window present across the whole real extent.
    _make_db(db, geometry=_dense_samples(0.0, 1.0, [_SENSITIVE_WIN]))
    # Caller passes a span that ends at 0.4s — far short of the real ~0.9s of
    # frames (mimics terminal_stage's uniform chunk_dur underestimate).
    outcome = mask_video_chunk(
        chunk_mp4, db, start_ts=0.0, end_ts=0.4, chunk_start_abs=0.0,
        output_path=scrubbed_out, pixel_ratio=1.0,
    )
    assert outcome.status is MaskOutcomeStatus.MASKED
    assert scrubbed_out.exists()

    c = av.open(str(scrubbed_out))
    frames = list(c.decode(c.streams.video[0]))
    c.close()
    assert len(frames) == N_FRAMES
    # Frame 8 is at t≈0.8s — WELL beyond the caller's 0.4 span. It must be masked
    # (pre-fix it shipped clear). Sample a pixel inside the sensitive window.
    trailing = frames[8].to_image().convert("RGB")
    assert _is_masked_fill(trailing.getpixel((20, 20))), (
        "trailing frame beyond the caller span shipped CLEAR — the SCR-126 "
        "trailing-frame leak is open"
    )


def test_scr126_out_of_span_frame_fails_closed(
    chunk_mp4, scrubbed_out, tmp_path, monkeypatch
):
    """A decoded frame outside the proven span fails closed (no clear ship).

    Force the PTS-extent probe to under-report (end at 0.2s) so frames past 0.2
    fall outside the coverage-proven span. The in-loop guard must raise → FAILED
    → no copy, rather than drawing the unproven frames clear."""
    import screencap.video_mask as vm

    db = tmp_path / "recording.db"
    _make_db(db, geometry=_dense_samples(0.0, 1.0, [_SENSITIVE_WIN]))
    # Probe lies: claims the chunk ends at 0.2s though it really runs to ~0.9s.
    monkeypatch.setattr(vm, "_probe_pts_extent", lambda _p: (0.0, 0.2))
    outcome = mask_video_chunk(
        chunk_mp4, db, start_ts=0.0, end_ts=1.0, chunk_start_abs=0.0,
        output_path=scrubbed_out, pixel_ratio=1.0,
    )
    assert outcome.status is MaskOutcomeStatus.FAILED
    assert not scrubbed_out.exists()
    assert "outside coverage-proven span" in outcome.reason


def test_scr126_misaligned_origin_fails_closed_not_wrong_window(
    chunk_mp4, scrubbed_out, tmp_path
):
    """A grossly mis-aligned absolute origin fails closed, not masks the wrong
    window. Geometry exists only around the TRUE window [10, 11]; an origin of
    0.0 lands the derived span [0, ~0.9] where there is NO geometry → coverage
    unprovable → FAILED. The masker does not silently reuse geometry from an
    unrelated time (R7 backstop via coverage over the real extent)."""
    db = tmp_path / "recording.db"
    _make_db(db, geometry=_dense_samples(10.0, 11.0, [_SENSITIVE_WIN]))
    outcome = mask_video_chunk(
        chunk_mp4, db, start_ts=0.0, end_ts=1.0, chunk_start_abs=0.0,
        output_path=scrubbed_out, pixel_ratio=1.0,
    )
    assert outcome.status is MaskOutcomeStatus.FAILED
    assert not scrubbed_out.exists()


def test_scr126_live_overshoot_end_ts_does_not_failclose(
    chunk_mp4, scrubbed_out, tmp_path
):
    """Live-path parity (R9): rotation_time overshoots the last decoded frame, so
    a too-large caller end_ts must NOT spuriously fail closed on the idle gap
    before rotation. The masker derives the real extent (~0.9s), so dense
    geometry over the frames yields MASKED — not a FAILED from a 4s edge gap."""
    db = tmp_path / "recording.db"
    _make_db(db, geometry=_dense_samples(0.0, 1.0, [_SENSITIVE_WIN]))
    # end_ts=5.0 simulates rotation firing 4s after the last action-gated frame.
    outcome = mask_video_chunk(
        chunk_mp4, db, start_ts=0.0, end_ts=5.0, chunk_start_abs=0.0,
        output_path=scrubbed_out, pixel_ratio=1.0,
    )
    assert outcome.status is MaskOutcomeStatus.MASKED, (
        "honest live chunk spuriously failed closed on the rotation overshoot"
    )
    assert scrubbed_out.exists()
