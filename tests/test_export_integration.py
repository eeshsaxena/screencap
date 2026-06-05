"""Integration tests for the export pipeline.

Exercise the real SQLite → process_events → JSONL pipeline with no mocks.
"""

from __future__ import annotations

import json
import os
import re

import pytest


def create_export_test_db(db_path, *, include_moves=True, extra_window_events=None):
    """Create a recording.db with known events for export testing.

    Returns dict with counts/metadata for assertion.
    """
    from screencap.engine.db import create_db, crud

    engine, Session = create_db(str(db_path))
    session = Session()

    recording = crud.insert_recording(session, {
        "timestamp": 1000.0,
        "platform": "darwin",
        "monitor_width": 1512,
        "monitor_height": 982,
        "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })

    # --- Window events (inserted first so timestamps are earlier) ---
    # (1) Terminal at t=1001.0
    crud.insert_window_event(session, recording, 1001.0, {
        "title": "bash — 80×24",
        "app_bundle_id": "com.apple.Terminal",
        "window_id": "100",
        "left": 0, "top": 0, "width": 1512, "height": 982,
    })
    # (2) Terminal again at t=1003.5 — consecutive duplicate (same bundle+window_id)
    # Should be deduplicated by deduplicate_window_events()
    crud.insert_window_event(session, recording, 1003.5, {
        "title": "bash — 80×24",
        "app_bundle_id": "com.apple.Terminal",
        "window_id": "100",
        "left": 0, "top": 0, "width": 1512, "height": 982,
    })
    # (3) Chrome at t=1005.0
    crud.insert_window_event(session, recording, 1005.0, {
        "title": "Example Domain — Google Chrome",
        "app_bundle_id": "com.google.Chrome",
        "window_id": "200",
        "left": 0, "top": 0, "width": 1512, "height": 982,
        "browser_url": "https://example.com",
    })

    if extra_window_events:
        for we in extra_window_events:
            crud.insert_window_event(session, recording, we["timestamp"], {
                k: v for k, v in we.items() if k != "timestamp"
            })

    # --- Move events ---
    if include_moves:
        for ts, mx, my in [(1001.5, 100, 100), (1002.5, 200, 200), (1006.0, 300, 300)]:
            crud.insert_action_event(session, recording, ts, {
                "name": "move",
                "mouse_x": float(mx),
                "mouse_y": float(my),
                "mouse_dx": 0.0,
                "mouse_dy": 0.0,
            })

    # --- Click events: 2 click pairs (down+up) ---
    for ts in [1002.0, 1007.0]:
        crud.insert_action_event(session, recording, ts, {
            "name": "click",
            "mouse_x": 500.0,
            "mouse_y": 300.0,
            "mouse_button_name": "left",
            "mouse_pressed": True,
        })
        crud.insert_action_event(session, recording, ts + 0.01, {
            "name": "click",
            "mouse_x": 500.0,
            "mouse_y": 300.0,
            "mouse_button_name": "left",
            "mouse_pressed": False,
        })

    # --- Key events: "hello" (0.1s apart, within 0.5s merge threshold) ---
    for i, char in enumerate("hello"):
        t = 1003.0 + i * 0.1
        crud.insert_action_event(session, recording, t, {
            "name": "press",
            "key_char": char,
            "key_name": char,
        })
        crud.insert_action_event(session, recording, t + 0.02, {
            "name": "release",
            "key_char": char,
            "key_name": char,
        })

    # --- Key events: "hi" at t=1008.0 (>0.5s gap from "hello" at t=1003.4) ---
    # merge_sequential_key_type_events splits on gaps >= 0.5s,
    # so this should produce a separate key.type event.
    for i, char in enumerate("hi"):
        t = 1008.0 + i * 0.1
        crud.insert_action_event(session, recording, t, {
            "name": "press",
            "key_char": char,
            "key_name": char,
        })
        crud.insert_action_event(session, recording, t + 0.02, {
            "name": "release",
            "key_char": char,
            "key_name": char,
        })

    # --- Scroll events ---
    crud.insert_action_event(session, recording, 1004.0, {
        "name": "scroll",
        "mouse_x": 400.0,
        "mouse_y": 400.0,
        "mouse_dx": 0.0,
        "mouse_dy": -30.0,
    })
    crud.insert_action_event(session, recording, 1004.5, {
        "name": "scroll",
        "mouse_x": 400.0,
        "mouse_y": 400.0,
        "mouse_dx": 0.0,
        "mouse_dy": -30.0,
    })

    session.close()

    return {
        "move_rows": 3 if include_moves else 0,
        "click_pairs": 2,
        "key_chars": 5,
        "scroll_rows": 2,
        "window_events": 3 + (len(extra_window_events) if extra_window_events else 0),
    }


def test_export_to_stdout(tmp_path, capsys):
    """E2: Export to stdout — no file on disk, valid JSONL on stdout."""
    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()
    create_export_test_db(rec_dir / "recording.db")

    from screencap.exporter import build_export_metadata, export_recording

    meta = build_export_metadata(exclude_moves=True)
    count = export_recording(rec_dir, output_path=None, exclude_moves=True, metadata=meta)

    # --- E2.1: No file created on disk ---
    assert not os.path.exists(rec_dir / "events.jsonl")

    # --- E2.2: Stdout contains valid JSONL ---
    captured = capsys.readouterr().out.strip()
    lines = captured.split("\n")
    for line in lines:
        json.loads(line)  # raises JSONDecodeError if invalid

    # --- E2.3: First line is _meta header ---
    header = json.loads(lines[0])
    assert header["_meta"] is True
    assert header["format_version"] == 2

    # --- E2.4: Return value matches line count ---
    event_count = len(lines) - 1  # subtract header
    assert count == event_count
    assert count > 0


def test_export_exclude_moves(tmp_path):
    """E3: Export with exclude_moves=True — no mouse.move events in output."""
    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()
    create_export_test_db(rec_dir / "recording.db")

    from screencap.exporter import build_export_metadata, export_recording

    # First, export WITH moves to get the baseline count
    meta_with = build_export_metadata(exclude_moves=False)
    out_with = str(rec_dir / "events_with_moves.jsonl")
    count_with = export_recording(rec_dir, out_with, exclude_moves=False, metadata=meta_with)

    # Now export WITHOUT moves
    meta_without = build_export_metadata(exclude_moves=True)
    out_without = str(rec_dir / "events_no_moves.jsonl")
    count_without = export_recording(
        rec_dir, out_without, exclude_moves=True, metadata=meta_without,
    )

    lines = open(out_without).read().strip().split("\n")
    header = json.loads(lines[0])
    events = [json.loads(line) for line in lines[1:]]
    event_types = {e["type"] for e in events}

    # --- E3.1: mouse.move events absent ---
    move_events = [e for e in events if e["type"] == "mouse.move"]
    assert len(move_events) == 0

    # --- E3.2: _meta header reflects flag ---
    assert header["exclude_moves"] is True

    # --- E3.3: Other events still present ---
    assert "mouse.singleclick" in event_types
    assert "key.type" in event_types
    assert "mouse.scroll" in event_types
    assert "window.switch" in event_types

    # --- E3.4: Total event count lower than E1 (3 fewer — the move events) ---
    assert count_without < count_with
    assert count_without == count_with - 3


def test_export_missing_directory_raises(tmp_path):
    """E4.1: Export of non-existent directory raises ExportError."""
    from screencap.exporter import ExportError, export_recording

    missing_dir = tmp_path / "does-not-exist"
    out_file = str(tmp_path / "events.jsonl")

    with pytest.raises(ExportError, match="No recording database found"):
        export_recording(missing_dir, out_file, exclude_moves=False)

    assert not os.path.exists(out_file)


def test_cli_export_missing_db_shows_error(tmp_path, monkeypatch):
    """E4.3: CLI export of directory without recording.db shows error message."""
    rec_dir = tmp_path / "no-db-rec"
    rec_dir.mkdir()

    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))

    from click.testing import CliRunner
    from screencap.cli import cli

    result = CliRunner().invoke(cli, ["export", "no-db-rec"])

    assert result.exit_code != 0
    assert "no recording database found" in result.output.lower()


def test_cli_export_creates_jsonl(tmp_path, monkeypatch):
    """E6.1-E6.3: screencap export <name> creates events.jsonl with exit code 0."""
    rec_dir = tmp_path / "test-rec"
    rec_dir.mkdir()
    create_export_test_db(rec_dir / "recording.db")

    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))

    from click.testing import CliRunner
    from screencap.cli import cli

    result = CliRunner().invoke(cli, ["export", "test-rec"])

    # --- E6.1: Exit code 0 ---
    assert result.exit_code == 0, result.output

    # --- E6.2: events.jsonl created ---
    assert (rec_dir / "events.jsonl").exists()

    # --- E6.3: Output mentions event count ---
    assert "Exported" in result.output
    # Should contain a number
    assert re.search(r"\d+", result.output)


def test_cli_export_not_found(tmp_path, monkeypatch):
    """E6.5: Non-existent recording name → non-zero exit code."""
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))

    from click.testing import CliRunner
    from screencap.cli import cli

    result = CliRunner().invoke(cli, ["export", "nonexistent"])

    assert result.exit_code != 0
    assert "not found" in result.output.lower() or "error" in result.output.lower()


def test_export_atomic_write_cleanup_on_failure(tmp_path):
    """On failure mid-export, .tmp file is cleaned up and output does not exist."""
    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()
    create_export_test_db(rec_dir / "recording.db")

    from unittest import mock

    from screencap.exporter import export_recording

    out_file = str(rec_dir / "events.jsonl")

    # Patch _write_events to explode after Capture.load succeeds
    with mock.patch(
        "screencap.exporter._write_events",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            export_recording(rec_dir, out_file, exclude_moves=False)

    assert not os.path.exists(out_file + ".tmp")
    assert not os.path.exists(out_file)


def test_cli_export_all(tmp_path, monkeypatch):
    """E6.6: --all exports multiple recordings."""
    for name in ["rec-a", "rec-b"]:
        d = tmp_path / name
        d.mkdir()
        create_export_test_db(d / "recording.db")

    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))

    from click.testing import CliRunner
    from screencap.cli import cli

    result = CliRunner().invoke(cli, ["export", "--all"])

    assert result.exit_code == 0, result.output

    # Each recording dir should have events.jsonl
    assert (tmp_path / "rec-a" / "events.jsonl").exists()
    assert (tmp_path / "rec-b" / "events.jsonl").exists()


# ============================================================================
# CaptureSession.export_events — Unit 5 unified-callable behavioral contracts
# ============================================================================
#
# These tests pin the per-recording threshold + disabled-row + include_moves
# behaviors that must survive the refactor to ``unified_export_events``.
# Cross-references the public-signature contract in
# ``tests/test_cross_layer_contracts.py:35-45``.


def _build_recording_with_thresholds(
    db_path,
    *,
    interval=None,
    distance=None,
):
    """Create a recording.db where the click-threshold columns may be NULL.

    ``interval=None`` and ``distance=None`` leave the threshold columns NULL
    so we can verify the engine's fallback to ``process_events`` defaults
    (0.5s / 5px).
    """
    from screencap.engine.db import create_db, crud

    engine, Session = create_db(str(db_path))
    session = Session()
    rec_data = {
        "timestamp": 1000.0,
        "platform": "darwin",
        "monitor_width": 1024,
        "monitor_height": 768,
    }
    if interval is not None:
        rec_data["double_click_interval_seconds"] = interval
    if distance is not None:
        rec_data["double_click_distance_pixels"] = distance
    recording = crud.insert_recording(session, rec_data)

    # Two click pairs 0.40s apart — at the boundary between default 0.5s
    # (merge → doubleclick) and a tighter 0.3s override (no merge → two
    # singleclicks).
    for ts in (1001.0, 1001.40):
        crud.insert_action_event(session, recording, ts, {
            "name": "click",
            "mouse_x": 100.0,
            "mouse_y": 100.0,
            "mouse_button_name": "left",
            "mouse_pressed": True,
        })
        crud.insert_action_event(session, recording, ts + 0.01, {
            "name": "click",
            "mouse_x": 100.0,
            "mouse_y": 100.0,
            "mouse_button_name": "left",
            "mouse_pressed": False,
        })
    session.close()
    return recording


def test_export_events_null_thresholds_fallback_to_defaults(tmp_path):
    """U5.2: NULL ``double_click_*`` columns fall back to engine defaults.

    Same fixture as U5.1 but with both threshold columns NULL. Default
    interval (0.5s) is wider than the 0.4s click gap, so the two pairs
    merge into one ``mouse.doubleclick``.
    """
    rec_dir = tmp_path / "null-rec"
    rec_dir.mkdir()
    _build_recording_with_thresholds(
        rec_dir / "recording.db", interval=None, distance=None,
    )

    from screencap.engine import Capture

    with Capture.load(str(rec_dir)) as capture:
        events = capture.export_events(include_moves=False)

    types = [e.type for e in events]
    # Default interval (0.5s) > 0.4s gap → merges to one doubleclick.
    assert types.count("mouse.doubleclick") == 1
    assert "mouse.singleclick" not in types


def test_export_events_disabled_rows_excluded(tmp_path):
    """U5.3: ``action_event.disabled=True`` rows never reach the unified
    pipeline.

    The disabled-row filter is applied at the row-fetch boundary inside
    ``CaptureSession.export_events`` (R16). A click with ``disabled=True``
    should not appear in the output as a ``mouse.singleclick``.
    """
    from screencap.engine import Capture
    from screencap.engine.db import create_db, crud
    from screencap.engine.db.models import ActionEvent

    rec_dir = tmp_path / "disabled-rec"
    rec_dir.mkdir()
    db_path = rec_dir / "recording.db"

    engine, Session = create_db(str(db_path))
    session = Session()
    recording = crud.insert_recording(session, {
        "timestamp": 1000.0,
        "platform": "darwin",
        "monitor_width": 1024,
        "monitor_height": 768,
    })
    # Two click pairs. The second pair (at 1003.0) will be disabled.
    for ts in (1001.0, 1003.0):
        crud.insert_action_event(session, recording, ts, {
            "name": "click",
            "mouse_x": 50.0,
            "mouse_y": 50.0,
            "mouse_button_name": "left",
            "mouse_pressed": True,
        })
        crud.insert_action_event(session, recording, ts + 0.01, {
            "name": "click",
            "mouse_x": 50.0,
            "mouse_y": 50.0,
            "mouse_button_name": "left",
            "mouse_pressed": False,
        })
    # Disable both rows of the second pair.
    for evt in session.query(ActionEvent).filter(ActionEvent.timestamp >= 1003.0):
        evt.disabled = True
    session.commit()
    session.close()

    with Capture.load(str(rec_dir)) as capture:
        events = capture.export_events(include_moves=False)

    click_events = [e for e in events if e.type == "mouse.singleclick"]
    # Only the first (enabled) click pair survives.
    assert len(click_events) == 1
    # It must be the surviving (timestamp=1001.x) click, not the disabled one.
    assert all(abs(e.timestamp - 1001.0) < 0.05 for e in click_events)


# ============================================================================
# CaptureSession.export_events — forward-looking window_event.disabled parity
# ============================================================================
#
# These tests pin the symmetry between CLI export, chunk processor, and
# recovery for the (currently absent) ``window_event.disabled`` column.
# Today the column doesn't exist on any schema, but chunk processor and
# recovery already gate it via ``has_column`` so a future migration that
# adds it filters disabled rows on those two paths. CLI export iterates
# the SQLAlchemy ``WindowEvent`` ORM model (which has no ``disabled``
# attribute), so without the raw-SQL pre-pass added in the M-2 fix, CLI
# export would silently emit disabled rows while the other two paths
# filter them — exactly the kind of three-caller asymmetry the unified-
# export refactor exists to prevent.


class TestWindowEventDisabledForwardLooking:
    """Forward-looking parity for ``window_event.disabled``.

    Two cases:

    1. Modern schema (no ``disabled`` column): CLI export behavior is
       unchanged — every window_event row in the DB still appears in the
       output.
    2. Forward-looking schema with the column: rows with ``disabled=1``
       are dropped from the output, matching what chunk processor and
       recovery already do via ``_disabled_clause``.

    The fix adds a raw-SQL pre-pass in
    ``CaptureSession.export_events`` to fetch disabled IDs and skip them
    during ORM iteration. Adding the column to the ``WindowEvent`` ORM
    model is NOT safe because older DBs lack the column and SQLAlchemy
    SELECTs would fail on them (the same failure mode as the recently-
    fixed P2 recovery bug).
    """

    def _build_capture(
        self, capture_dir, *, include_disabled_window=False,
    ):
        """Create a recording.db with three window_event rows.

        When ``include_disabled_window`` is True, the middle row's
        ``window_id`` is "200" — the test then ALTERs the table to add
        a ``disabled`` column and marks that row disabled.
        """
        from screencap.engine.db import create_db, crud

        capture_dir.mkdir(exist_ok=True)
        db_path = capture_dir / "recording.db"

        engine, Session = create_db(str(db_path))
        session = Session()

        recording = crud.insert_recording(session, {
            "timestamp": 1000.0,
            "platform": "darwin",
            "monitor_width": 1024,
            "monitor_height": 768,
            "double_click_interval_seconds": 0.5,
            "double_click_distance_pixels": 5.0,
        })

        # Three window events. With dedup-by-(bundle_id, window_id) the
        # three distinct window_ids each produce a separate
        # ``window.switch`` event in the output.
        crud.insert_window_event(session, recording, 1001.0, {
            "title": "Editor — main.py",
            "app_bundle_id": "com.editor.app",
            "window_id": "100",
            "left": 0, "top": 0, "width": 1024, "height": 768,
        })
        crud.insert_window_event(session, recording, 1002.0, {
            "title": "Browser — example.com",
            "app_bundle_id": "com.browser.app",
            "window_id": "200",
            "left": 0, "top": 0, "width": 1024, "height": 768,
        })
        crud.insert_window_event(session, recording, 1003.0, {
            "title": "Terminal — bash",
            "app_bundle_id": "com.terminal.app",
            "window_id": "300",
            "left": 0, "top": 0, "width": 1024, "height": 768,
        })

        # Need at least one action event so the export pipeline has a
        # row to anchor — without action_events the unified pipeline
        # still emits window.switch events but adding one keeps the
        # fixture closer to a real recording.
        crud.insert_action_event(session, recording, 1004.0, {
            "name": "click",
            "mouse_x": 50.0, "mouse_y": 50.0,
            "mouse_button_name": "left",
            "mouse_pressed": True,
        })
        crud.insert_action_event(session, recording, 1004.01, {
            "name": "click",
            "mouse_x": 50.0, "mouse_y": 50.0,
            "mouse_button_name": "left",
            "mouse_pressed": False,
        })

        session.commit()
        session.close()
        engine.dispose()

        if include_disabled_window:
            # Forward-looking schema: ALTER TABLE to add the column,
            # then mark window_id=200 as disabled. This mirrors what a
            # future migration would do (but isn't done today). Use raw
            # sqlite3 to avoid touching the ORM model.
            import sqlite3

            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    "ALTER TABLE window_event "
                    "ADD COLUMN disabled BOOLEAN DEFAULT 0"
                )
                conn.execute(
                    "UPDATE window_event SET disabled = 1 "
                    "WHERE window_id = '200'"
                )
                conn.commit()

        return db_path

    def test_modern_schema_no_disabled_column_unchanged_behavior(self, tmp_path):
        """No ``window_event.disabled`` column → all 3 window events emit.

        Pins that the raw-SQL pre-pass is a no-op for the current schema:
        ``has_column`` returns False, the disabled-id set stays empty,
        and every ORM row passes through.
        """
        from screencap.engine import Capture

        capture_dir = tmp_path / "modern-rec"
        self._build_capture(capture_dir, include_disabled_window=False)

        with Capture.load(str(capture_dir)) as capture:
            events = capture.export_events(include_moves=False)

        ws = [e for e in events if e.type == "window.switch"]
        bundles = {e.app_bundle_id for e in ws}
        assert bundles == {
            "com.editor.app", "com.browser.app", "com.terminal.app",
        }, (
            "All 3 window_event rows must emit on the current schema "
            f"(no disabled column gating); got bundles={bundles}"
        )

    def test_forward_looking_schema_disabled_rows_filtered(self, tmp_path):
        """Forward-looking ``window_event.disabled`` column is honored.

        With the column present and window_id=200 marked disabled, CLI
        export must skip that row — matching what chunk processor and
        recovery already do via ``_disabled_clause``.

        Pre-fix this test FAILS: the ORM iterator can't see a column
        the model doesn't define, so the disabled row leaks into the
        output. Post-fix the raw-SQL pre-pass fetches the disabled id
        and the ORM loop skips it.
        """
        from screencap.engine import Capture

        capture_dir = tmp_path / "future-rec"
        self._build_capture(capture_dir, include_disabled_window=True)

        with Capture.load(str(capture_dir)) as capture:
            events = capture.export_events(include_moves=False)

        ws = [e for e in events if e.type == "window.switch"]
        bundles = {e.app_bundle_id for e in ws}
        assert "com.browser.app" not in bundles, (
            "window_event row with disabled=1 leaked into CLI export "
            "— forward-looking parity with chunk processor + recovery "
            f"is broken; got bundles={bundles}"
        )
        # The other two rows must still pass through.
        assert bundles == {"com.editor.app", "com.terminal.app"}, (
            f"Expected only the two non-disabled bundles; got {bundles}"
        )


# ============================================================================
# write_events_jsonl — Unit 4 streaming writer
# ============================================================================
#
# These tests pin the contract used by the chunk processor (Unit 6) and
# recovery (Unit 7) when both migrate to the shared writer:
#
# - Atomic .tmp + os.rename (clean run leaves no .tmp).
# - Cleanup-on-exception (failed write never leaves a partial output).
# - Stale-.tmp cleanup at start (defense-in-depth for SIGKILL/OOM).
# - Streaming memory profile (R18: peak RSS ≤ ~1.5× sizeof(processed list)).


def _make_move_events(count: int):
    """Build ``count`` MouseMoveEvent instances spaced 1ms apart.

    Used by the writer tests as a lightweight, deterministic fixture.
    """
    from screencap.engine.events import MouseMoveEvent

    return [
        MouseMoveEvent(timestamp=1000.0 + i * 0.001, x=float(i), y=float(i))
        for i in range(count)
    ]


def test_write_events_jsonl_happy_path(tmp_path):
    """W1: 100 events stream to disk as meta + 100 lines."""
    from screencap.exporter import build_export_metadata, write_events_jsonl

    out_path = tmp_path / "events.jsonl"
    meta = build_export_metadata(exclude_moves=False)
    events = _make_move_events(100)

    count = write_events_jsonl(out_path, events, meta)

    assert count == 100
    assert out_path.exists()
    assert not (tmp_path / "events.jsonl.tmp").exists()

    lines = out_path.read_text().strip().split("\n")
    assert len(lines) == 101  # meta + 100 events

    # Header validation
    header = json.loads(lines[0])
    assert header["_meta"] is True
    assert header["format_version"] == 2
    assert header["exclude_moves"] is False

    # Event validation — all 100 lines parse as JSON with type=mouse.move
    for i, line in enumerate(lines[1:]):
        evt = json.loads(line)
        assert evt["type"] == "mouse.move"
        assert evt["x"] == float(i)
        assert evt["y"] == float(i)


def test_write_events_jsonl_atomicity_on_midwrite_failure(tmp_path):
    """W2: exception mid-write removes .tmp, leaves no final output."""
    from unittest import mock

    from screencap.exporter import build_export_metadata, write_events_jsonl

    out_path = tmp_path / "events.jsonl"
    tmp_target = tmp_path / "events.jsonl.tmp"
    meta = build_export_metadata(exclude_moves=False)
    events = _make_move_events(100)

    # Patch model_dump_json on the 50th event to raise. We wrap the
    # original method so the first 49 events serialize normally, then
    # event #49 (zero-indexed = 50th call) explodes.
    call_count = {"n": 0}
    original = type(events[0]).model_dump_json

    def exploding(self, *args, **kwargs):
        if call_count["n"] == 49:
            raise RuntimeError("simulated mid-write failure")
        call_count["n"] += 1
        return original(self, *args, **kwargs)

    with mock.patch.object(type(events[0]), "model_dump_json", exploding):
        with pytest.raises(RuntimeError, match="simulated mid-write failure"):
            write_events_jsonl(out_path, events, meta)

    # Both the .tmp and the final path should be absent — atomic semantics.
    assert not tmp_target.exists(), ".tmp must be cleaned up on failure"
    assert not out_path.exists(), "final path must not appear on partial write"


def test_write_events_jsonl_clean_run_no_tmp_remaining(tmp_path):
    """W3: successful write leaves no .tmp file."""
    from screencap.exporter import build_export_metadata, write_events_jsonl

    out_path = tmp_path / "events.jsonl"
    meta = build_export_metadata(exclude_moves=True)
    events = _make_move_events(10)

    write_events_jsonl(out_path, events, meta)

    assert out_path.exists()
    assert not (tmp_path / "events.jsonl.tmp").exists()


def test_write_events_jsonl_empty_events(tmp_path):
    """W4: empty events iterable → output has only the meta line."""
    from screencap.exporter import build_export_metadata, write_events_jsonl

    out_path = tmp_path / "events.jsonl"
    meta = build_export_metadata(exclude_moves=False)

    count = write_events_jsonl(out_path, iter([]), meta)

    assert count == 0
    assert out_path.exists()
    lines = out_path.read_text().strip().split("\n")
    assert len(lines) == 1
    header = json.loads(lines[0])
    assert header["_meta"] is True


def test_unique_tmp_path_is_per_call_unique_sibling(tmp_path):
    """todo 006: each call yields a distinct tmp sibling so concurrent writers
    never collide on a shared temp path."""
    from screencap.exporter import _unique_tmp_path

    out = tmp_path / "events.jsonl"
    a = _unique_tmp_path(out)
    b = _unique_tmp_path(out)
    assert a != b, "each call yields a distinct tmp path"
    assert a.parent == out.parent, "tmp is a sibling of the final file"
    assert a.name.startswith("events.jsonl."), a.name
    assert a.suffix == ".tmp"


def test_concurrent_write_events_jsonl_no_corruption(tmp_path):
    """todo 006: two writers to the same out_path use distinct tmps, so the
    final file is one writer's complete, parseable output (atomic
    last-writer-wins) — never an interleaved/corrupt mix — with no orphaned
    tmp left behind."""
    import threading

    from screencap.exporter import build_export_metadata, write_events_jsonl

    out_path = tmp_path / "events.jsonl"
    meta = build_export_metadata(exclude_moves=False)
    barrier = threading.Barrier(2)

    def writer(n):
        barrier.wait()  # maximize overlap
        write_events_jsonl(out_path, _make_move_events(n), meta)

    threads = [threading.Thread(target=writer, args=(n,)) for n in (10, 20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every line parses (no interleaving) and the file is exactly one writer's
    # complete output.
    lines = out_path.read_text().strip().split("\n")
    for line in lines:
        json.loads(line)
    assert (len(lines) - 1) in (10, 20), "final must be one writer's complete output"
    assert not list(tmp_path.glob("events.jsonl.*.tmp")), "no orphaned unique tmp"


def test_write_events_jsonl_stale_tmp_cleanup(tmp_path):
    """W5: a stale .tmp file from a prior crashed run is cleaned at start."""
    from screencap.exporter import build_export_metadata, write_events_jsonl

    out_path = tmp_path / "events.jsonl"
    stale_tmp = tmp_path / "events.jsonl.tmp"

    # Simulate the SIGKILL/OOM case: previous run left a partial .tmp on disk.
    stale_tmp.write_text("this is partial garbage from a previous crash\n")
    assert stale_tmp.exists()

    meta = build_export_metadata(exclude_moves=False)
    events = _make_move_events(5)

    count = write_events_jsonl(out_path, events, meta)

    assert count == 5
    assert out_path.exists()
    assert not stale_tmp.exists(), "stale .tmp must be cleaned up at start"

    lines = out_path.read_text().strip().split("\n")
    # 1 meta + 5 events; no leftover garbage
    assert len(lines) == 6
    assert json.loads(lines[0])["_meta"] is True


def test_write_events_jsonl_consumes_iterator(tmp_path):
    """W6: writer accepts a generator (not just a list) — exhausts it once."""
    from screencap.exporter import build_export_metadata, write_events_jsonl

    out_path = tmp_path / "events.jsonl"
    meta = build_export_metadata(exclude_moves=False)

    def gen():
        for evt in _make_move_events(20):
            yield evt

    g = gen()
    count = write_events_jsonl(out_path, g, meta)

    assert count == 20
    # Generator exhausted — second call should yield nothing
    assert list(g) == []


@pytest.mark.slow
def test_write_events_jsonl_streaming_memory_bound(tmp_path):
    """W7b (R18): the writer itself does NOT materialize its input.

    R18 honest framing: ``process_events``' 11-stage merge pipeline
    intrinsically materializes the action-event list (click pairing
    needs lookahead, drag detection needs lookback, key.type merging
    needs aggregate state). Empirically that pipeline's peak working
    set is ~6× the final processed list size — far above the plan's
    naive "1.5×" target, which was written before the interim
    allocations were measured. So the bound the plan asks for cannot
    be measured against the full ``unified_export_events →
    write_events_jsonl`` pipeline; process_events dominates.

    What we CAN measure precisely is the writer in isolation: feed
    ``write_events_jsonl`` a generator yielding 100K pre-built events
    on demand, and confirm peak ≈ one event's worth (not one list's
    worth). A regression where the writer ``list(events)`` internally
    would push peak from kilobytes to ~tens of MB.

    Threshold: streaming peak ≤ 1MB. The full 100K-event list is ~8MB
    of references; one event in flight is hundreds of bytes; per-line
    serialization buffers a few KB. 1MB is generous headroom for
    tracemalloc noise while still catching a "list inside writer"
    regression by an order of magnitude.

    Sibling test (W7a) covers the seam between the engine and the
    writer at the type level; this test covers the writer's own
    behavior at the memory level.
    """
    import gc
    import tracemalloc

    from screencap.engine.events import MouseMoveEvent
    from screencap.exporter import build_export_metadata, write_events_jsonl

    n = 100_000
    # Pre-build the events outside the measured window. They live in
    # `events_list` and should be the dominant allocation BEFORE
    # tracemalloc starts; the writer-only peak is what we measure.
    events_list = [
        MouseMoveEvent(timestamp=1000.0 + i * 0.001, x=float(i), y=float(i))
        for i in range(n)
    ]

    out = tmp_path / "events.jsonl"
    meta = build_export_metadata(exclude_moves=False)

    def gen():
        for e in events_list:
            yield e

    gc.collect()
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        count = write_events_jsonl(out, gen(), meta)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert count == n
    assert out.exists()

    # Writer should hold ~one event in flight, not the whole list.
    # 1MB is ~125× a single event — plenty of slack for serialization
    # buffers, tracemalloc bookkeeping, and the small windowing the
    # generator does.
    threshold = 1_000_000
    assert peak <= threshold, (
        f"write_events_jsonl peak {peak / 1_000_000:.2f}MB exceeds "
        f"{threshold / 1_000_000:.1f}MB on a {n}-event generator input. "
        f"This usually means the writer is materializing the iterator "
        f"internally (e.g., list(events) inside the function body) "
        f"instead of streaming one event at a time."
    )


def test_v1_scope_guard_no_network_lines_in_capture_export(tmp_path):
    """V1 contract: Capture.export_events() emits ZERO network.* lines.

    The CLI export path calls Capture.export_events() which in turn
    delegates to unified_export_events with network_rows=None (per V1
    plan scope at lines 919-921 + 47-48). Wiring network_rows in V1
    would silently leak metadata to cloud the next time a user runs
    `screencap upload` on a previously-recorded local session.
    """
    from screencap.engine.db import crud
    from screencap.exporter import build_export_metadata, export_recording

    rec_dir = tmp_path / "v1-network-rec"
    rec_dir.mkdir()
    db_path = rec_dir / "recording.db"

    # Seed standard test data + a network_event row.
    create_export_test_db(db_path)

    # Insert a network_event row directly via crud + get_session_for_path.
    from screencap.engine.db import get_session_for_path

    session = get_session_for_path(str(db_path))
    recording = session.query(crud.Recording).first()
    crud.insert_network_event(
        session,
        recording,
        {
            "kind": "request",
            "flow_id": "flow-X",
            "method": "GET",
            "url": "https://example.com/api/secret",
            "host": "example.com",
            "headers_json": json.dumps([["Host", "example.com"]]),
            "body_size": 0,
            "body_sha256": None,
            "content_type": None,
            "direction": None,
            "frame_type": None,
            "http_version": "HTTP/1.1",
            "details_json": None,
            "timestamp": 1004.0,
            "timestamp_ns": 1_004_000_000_000,
        },
    )
    crud.flush_buffers(session)
    session.close()

    out_file = str(rec_dir / "events.jsonl")
    meta = build_export_metadata(exclude_moves=False)
    export_recording(rec_dir, out_file, exclude_moves=False, metadata=meta)

    lines = open(out_file).read().strip().split("\n")
    events = [json.loads(line) for line in lines[1:]]  # skip _meta
    types = [e.get("type") for e in events]
    assert all(not (t or "").startswith("network.") for t in types), (
        f"V1 Capture.export_events() must emit zero network.* lines; got types: {types}"
    )
