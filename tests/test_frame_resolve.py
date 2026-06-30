"""SCR-186 U1 — pure nearest-frame resolution core.

Mirrors ``macos/ScreenCapTests/RecordingFrameIndexTests.swift`` for selection,
listing, and cap semantics, and adds the two cases the Swift suite cannot cover:
the round-half-away-from-zero rule (Swift fixtures all land on whole ms) and the
injected ALLOW-only blocked-frame filter (SCR-186 R8, Python-only).
"""

from __future__ import annotations

from screencap.frame_resolve import (
    Frame,
    _round_half_away,
    load_frames,
    nearest_frame,
)


def _write_frames(root, recording, epochs, extra=()):
    """Create ``<root>/<recording>/screenshots/`` and write empty frame files.

    Mirrors the Swift ``makeFrames`` fixture (recorder convention ``{epoch:.6f}.jpg``).
    """
    screenshots = root / recording / "screenshots"
    screenshots.mkdir(parents=True, exist_ok=True)
    for e in epochs:
        (screenshots / f"{e:.6f}.jpg").write_bytes(b"")
    for name in extra:
        (screenshots / name).write_bytes(b"")
    return screenshots


# ---------------------------------------------------------------------------
# Rounding rule (Swift .rounded() = half-away-from-zero, not Python round())
# ---------------------------------------------------------------------------


def test_round_half_away_rounds_up_at_exact_half():
    # The exact-.5 boundary is where Python's banker's round() diverges from
    # Swift's .rounded(): round(2.5) == 2 / round(4.5) == 4 (round-to-even),
    # but Swift (and us) round away from zero.
    assert _round_half_away(2.5) == 3
    assert _round_half_away(4.5) == 5
    assert round(2.5) == 2 and round(4.5) == 4  # documents the divergence we avoid
    # Non-boundary values are unaffected.
    assert _round_half_away(2.4) == 2
    assert _round_half_away(2.6) == 3


# ---------------------------------------------------------------------------
# nearest_frame (pure selection) — mirrors testNearestPicksClosestByAbsoluteDistance
# ---------------------------------------------------------------------------


def _frames(*ms_values):
    return [Frame(ts=ms / 1000.0, ms=ms, stem=f"f{ms}") for ms in ms_values]


def test_nearest_picks_closest_by_absolute_distance():
    frames = _frames(1000, 5000, 9000)
    assert nearest_frame(frames, 1000, 1_000_000)[0] == "f1000"   # exact
    assert nearest_frame(frames, 6000, 1_000_000)[0] == "f5000"   # between -> nearer
    assert nearest_frame(frames, -100, 1_000_000)[0] == "f1000"   # before first
    assert nearest_frame(frames, 99_999, 1_000_000)[0] == "f9000"  # after last


def test_nearest_exact_hit_has_zero_delta():
    stem, delta = nearest_frame(_frames(1000, 5000), 5000, 30_000)
    assert stem == "f5000"
    assert delta == 0


def test_nearest_delta_is_signed():
    # Frame before the anchor -> negative delta; after -> positive.
    _, delta_before = nearest_frame(_frames(1000), 1500, 30_000)
    assert delta_before == -500
    _, delta_after = nearest_frame(_frames(2000), 1500, 30_000)
    assert delta_after == 500


def test_nearest_empty_is_none():
    assert nearest_frame([], 1000, 30_000) is None


def test_nearest_earliest_wins_on_tie():
    # Anchor equidistant between two frames -> the earlier frame wins (stable min
    # over an ascending list; coincides with Swift's strict-< predicate).
    frames = _frames(1000, 3000)
    stem, _ = nearest_frame(frames, 2000, 30_000)
    assert stem == "f1000"


def test_nearest_cap_is_inclusive():
    frames = _frames(1000)
    assert nearest_frame(frames, 1000 + 30_000, 30_000) is not None   # exactly at cap
    assert nearest_frame(frames, 1000 + 30_001, 30_000) is None       # one past cap


# ---------------------------------------------------------------------------
# load_frames — mirrors testLoadFramesParsesSortsAndSkipsNonFrames
# ---------------------------------------------------------------------------


def test_load_frames_parses_sorts_and_skips_non_frames(tmp_path):
    _write_frames(
        tmp_path,
        "rec",
        epochs=[1_719_400_002.5, 1_719_400_000.0, 1_719_400_001.25],
        extra=["notanumber.jpg", "1719400000.000000.png"],
    )
    frames = load_frames(tmp_path / "rec" / "screenshots")
    assert [f.ms for f in frames] == [
        1_719_400_000_000,
        1_719_400_001_250,
        1_719_400_002_500,
    ]


def test_load_frames_excludes_jpeg_extension(tmp_path):
    # Swift lists *.jpg only; parse_screenshot_timestamp also accepts .jpeg, so
    # the glob (not the parser) is what enforces parity here.
    screenshots = tmp_path / "rec" / "screenshots"
    screenshots.mkdir(parents=True)
    (screenshots / "1719400000.000000.jpg").write_bytes(b"")
    (screenshots / "1719400001.000000.jpeg").write_bytes(b"")
    frames = load_frames(screenshots)
    assert [f.stem for f in frames] == ["1719400000.000000"]


def test_load_frames_missing_dir_is_empty(tmp_path):
    assert load_frames(tmp_path / "nope" / "screenshots") == []

# The recording-level composition (load_frames -> ALLOW filter -> nearest_frame),
# the cap, and the blocked-frame / fail-closed behaviour are exercised end-to-end
# against the real recording.db machinery in tests/daemon/test_read_only_verbs.py
# (the frame.nearest verb) and tests/test_frame_blocked.py (the predicate).
