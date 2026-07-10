"""U2 — the deterministic, coverage-qualified aggregation layer
(``screencap.segmentation.aggregate.aggregate_window``).

Covers AE2: durations/counts come from authoritative event data, never
estimated — and a static-focus span (reading / lunch / idle) is reported as
UNCOVERED rather than silently credited to the last-focused app.

The load-bearing rule under test: ``_compute_dominant_app`` credits the whole
inter-event gap to the last-focused app, so a static-focus span becomes "active"
in that app. This layer instead credits ACTIVE time only where a focus span is
corroborated by ``action_event`` timestamps and/or on-disk ``screenshots/*.jpg``,
and surfaces the uncorroborated remainder as an explicit uncovered descriptor.

Vision-free (raw sqlite + tiny jpeg headers, no OCR), and only reads local
event data, so it is safe on the CI privacy lane; marked privacy so the
coverage guarantee runs as a CI guard.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

import pytest

from screencap.segmentation import aggregate

pytestmark = pytest.mark.privacy

# A fixed base epoch so windows are exact and readable.
_BASE = 1_770_000_000.0  # some Unix second in 2026


def _make_recording_db(
    rec_dir: Path,
    *,
    started: float,
    end: float,
    windows: list[dict],
    actions: list[dict] | None = None,
    screenshots: list[float] | None = None,
) -> None:
    """Build a minimal recording dir (recording + window_event + action_event)
    plus optional flat ``screenshots/*.jpg`` files.

    ``windows`` items: ``{ts, bundle, app_name?, title?}`` — one per focus change.
    ``actions`` items: ``{ts, name?}`` — ``name`` defaults to ``"click"``; a
    ``"move"`` name is written but excluded from active/count corroboration
    (mirrors the manifest's ``name != 'move'`` filter). A trailing ``click`` at
    ``end`` is always inserted so the recording has a duration.
    """
    rec_dir.mkdir(parents=True, exist_ok=True)
    db = rec_dir / "recording.db"
    with contextlib.closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, pixel_ratio REAL)"
        )
        conn.execute("INSERT INTO recording VALUES (1, ?, 2.0)", (started,))
        conn.execute(
            """CREATE TABLE window_event (
                id INTEGER PRIMARY KEY, recording_id INTEGER, timestamp REAL,
                app_bundle_id TEXT, window_id TEXT, title TEXT, state TEXT,
                app_name TEXT, browser_url TEXT
            )"""
        )
        for i, w in enumerate(windows, start=1):
            conn.execute(
                "INSERT INTO window_event (id, recording_id, timestamp, app_bundle_id, "
                "window_id, title, state, app_name, browser_url) "
                "VALUES (?, 1, ?, ?, ?, ?, NULL, ?, NULL)",
                (i, w["ts"], w.get("bundle"), f"w{i}", w.get("title"),
                 w.get("app_name")),
            )
        conn.execute(
            """CREATE TABLE action_event (
                id INTEGER PRIMARY KEY, recording_id INTEGER, name TEXT,
                timestamp REAL, key_char TEXT, element_state TEXT
            )"""
        )
        rows = list(actions or [])
        rows.append({"ts": end, "name": "click"})
        for j, a in enumerate(rows, start=1):
            conn.execute(
                "INSERT INTO action_event (id, recording_id, name, timestamp) "
                "VALUES (?, 1, ?, ?)",
                (j, a.get("name", "click"), a["ts"]),
            )
        conn.commit()

    if screenshots:
        shots = rec_dir / "screenshots"
        shots.mkdir(exist_ok=True)
        for ts in screenshots:
            (shots / f"{ts}.jpg").write_bytes(b"\xff\xd8\xff")  # tiny jpeg header


def _app(result: aggregate.WindowAggregate, bundle: str) -> aggregate.AppActivity | None:
    return next((a for a in result.apps if a.app == bundle), None)


# --- dense activity: the app gets covered active time ----------------------


def test_dense_activity_yields_covered_active_time(tmp_path):
    """A focus span densely corroborated by action events reports covered active
    time for that app (AE2 — computed, not estimated)."""
    rec = tmp_path / "dense"
    start = _BASE
    end = _BASE + 300
    _make_recording_db(
        rec,
        started=start,
        end=end,
        windows=[{"ts": start, "bundle": "com.salesforce.app", "app_name": "Salesforce"}],
        # a click every 10s across the whole span → fully corroborated
        actions=[{"ts": start + t} for t in range(0, 300, 10)],
    )
    result = aggregate.aggregate_window(
        int(start * 1000), int(end * 1000), recordings_dir=tmp_path
    )
    sf = _app(result, "com.salesforce.app")
    assert sf is not None, "Salesforce must appear with covered active time"
    # Most of the 300s window is covered (dense clicks); the layer never claims
    # the exact 300s wall-clock, but covered time is a large fraction of it.
    assert sf.covered_active_ms > 150_000
    assert sf.covered_active_ms <= 300_000
    assert sf.event_count >= 25  # ~30 clicks minus the trailing one at `end`
    # It carries backing source pointers (recording + interval).
    assert sf.sources, "covered active time must carry backing source pointers"
    assert all(s.recording == "dense" for s in sf.sources)


def test_screenshots_alone_corroborate_active_time(tmp_path):
    """Screenshots present in a focus span corroborate active time even with no
    action events (screenshot-presence is a valid coverage signal)."""
    rec = tmp_path / "shots"
    start = _BASE
    end = _BASE + 120
    _make_recording_db(
        rec,
        started=start,
        end=end,
        windows=[{"ts": start, "bundle": "com.apple.Safari", "app_name": "Safari"}],
        actions=[],  # no user actions in-span (trailing click at end only)
        screenshots=[start + 10, start + 20, start + 30, start + 40],
    )
    result = aggregate.aggregate_window(
        int(start * 1000), int(end * 1000), recordings_dir=tmp_path
    )
    safari = _app(result, "com.apple.Safari")
    assert safari is not None
    assert safari.covered_active_ms > 0, "screenshots alone must corroborate coverage"


# --- static-focus gap: reported UNCOVERED, not credited to the app ---------


def test_static_focus_gap_is_uncovered_not_active_time(tmp_path):
    """THE load-bearing test. A long static-focus gap with NO action events and NO
    screenshots is reported as uncovered — NOT credited as active time in the
    last-focused app (the ``_compute_dominant_app`` over-count bug)."""
    rec = tmp_path / "lunch"
    start = _BASE
    end = _BASE + 3600  # a full hour of Salesforce focus, but the user is at lunch
    _make_recording_db(
        rec,
        started=start,
        end=end,
        windows=[{"ts": start, "bundle": "com.salesforce.app", "app_name": "Salesforce"}],
        actions=[],       # nothing happened
        screenshots=[],   # no frames captured in the gap
    )
    result = aggregate.aggregate_window(
        int(start * 1000), int(end * 1000), recordings_dir=tmp_path
    )
    sf = _app(result, "com.salesforce.app")
    # The naive layer would report ~3600s of Salesforce. The honest layer must
    # not: the hour is uncovered, not active app time.
    if sf is not None:
        assert sf.covered_active_ms == 0, (
            "a static-focus lunch span must not be credited as active app time"
        )
    # And the uncovered hour is surfaced explicitly.
    assert result.uncovered_ms > 3_000_000, "the idle hour must be surfaced as uncovered"
    assert result.uncovered_spans, "uncovered spans must be enumerated for the caller"


# --- app / task filter -----------------------------------------------------


def test_app_filter_narrows_to_one_app(tmp_path):
    """The optional ``app`` filter narrows the result to matching apps only."""
    rec = tmp_path / "multi"
    start = _BASE
    end = _BASE + 200
    _make_recording_db(
        rec,
        started=start,
        end=end,
        windows=[
            {"ts": start, "bundle": "com.salesforce.app", "app_name": "Salesforce"},
            {"ts": start + 100, "bundle": "com.apple.Safari", "app_name": "Safari"},
        ],
        actions=(
            [{"ts": start + t} for t in range(0, 100, 10)]
            + [{"ts": start + t} for t in range(100, 200, 10)]
        ),
    )
    result = aggregate.aggregate_window(
        int(start * 1000), int(end * 1000), app="salesforce", recordings_dir=tmp_path
    )
    assert [a.app for a in result.apps] == ["com.salesforce.app"]
    assert _app(result, "com.apple.Safari") is None


# --- empty window: honest zero, not a fabricated figure --------------------


def test_empty_window_returns_zero_with_coverage_note(tmp_path):
    """An empty window (no recordings intersect it) returns zeroed apps and a
    coverage note — never a fabricated figure."""
    result = aggregate.aggregate_window(
        int(_BASE * 1000), int((_BASE + 100) * 1000), recordings_dir=tmp_path
    )
    assert result.apps == []
    assert result.covered_active_ms == 0
    assert result.coverage_note, "an empty window must carry an explicit coverage note"


# --- coverage descriptor rides the figure ----------------------------------


def test_result_carries_coverage_descriptor(tmp_path):
    """The returned figure carries its coverage descriptor so a caller can narrate
    '≈X over covered spans' rather than presenting an exact wall-clock number."""
    rec = tmp_path / "mixed"
    start = _BASE
    end = _BASE + 600
    # 5 min of clicks, then 5 min of nothing (idle).
    _make_recording_db(
        rec,
        started=start,
        end=end,
        windows=[{"ts": start, "bundle": "com.salesforce.app", "app_name": "Salesforce"}],
        actions=[{"ts": start + t} for t in range(0, 300, 10)],
    )
    result = aggregate.aggregate_window(
        int(start * 1000), int(end * 1000), recordings_dir=tmp_path
    )
    # window is 600s; covered < window (the idle tail is uncovered).
    assert result.window_ms == 600_000
    assert result.covered_active_ms < result.window_ms
    assert result.uncovered_ms > 0
    # covered + uncovered accounts for the window (no double-counting / no
    # phantom time beyond the window).
    assert result.covered_active_ms + result.uncovered_ms <= result.window_ms
    # A human-readable coverage note is present for narration.
    assert isinstance(result.coverage_note, str) and result.coverage_note
