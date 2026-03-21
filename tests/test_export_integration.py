"""Integration tests for the export pipeline.

Exercise the real SQLite → process_events → JSONL pipeline with no mocks.
"""

from __future__ import annotations

import io
import json
import os
import re
from datetime import datetime

import pytest
import screencap


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


def test_basic_export_creates_valid_jsonl(tmp_path):
    """E1: Basic export with default flags — full pipeline, no mocks."""
    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()
    db_path = rec_dir / "recording.db"
    create_export_test_db(db_path)

    from screencap.exporter import build_export_metadata, export_recording

    out_file = str(rec_dir / "events.jsonl")
    meta = build_export_metadata(exclude_moves=False)
    count = export_recording(rec_dir, out_file, exclude_moves=False, metadata=meta)

    # --- E1.1: File created ---
    assert os.path.exists(out_file)

    lines = open(out_file).read().strip().split("\n")

    # --- E1.2: _meta header ---
    header = json.loads(lines[0])
    assert header["_meta"] is True
    assert header["format_version"] == 2
    assert header["screencap_version"] == screencap.__version__
    # ISO-8601 check
    datetime.fromisoformat(header["exported_at"])
    assert header["exclude_moves"] is False

    # --- E1.3: All lines parse as JSON ---
    events = []
    for line in lines[1:]:
        events.append(json.loads(line))

    # --- E1.4: Event types are processed (no raw types) ---
    event_types = {e["type"] for e in events}
    assert "mouse.singleclick" in event_types
    assert "key.type" in event_types
    assert "mouse.scroll" in event_types
    # Raw types should NOT be present
    for raw_type in ("mouse.down", "mouse.up", "key.down", "key.up"):
        assert raw_type not in event_types, f"Raw type {raw_type} should not be in output"

    # --- E1.4b: Scroll merge — 2 consecutive scrolls merge unconditionally into 1 ---
    scroll_events = [e for e in events if e["type"] == "mouse.scroll"]
    assert len(scroll_events) == 1
    assert scroll_events[0]["dy"] == -60.0  # sum of two -30.0 scrolls

    # --- E1.5: mouse.move events included ---
    # 3 move rows, each separated by non-move events (clicks/keys/scrolls),
    # so merge_consecutive_mouse_move_events keeps all 3.
    move_events = [e for e in events if e["type"] == "mouse.move"]
    assert len(move_events) == 3

    # --- E1.6: window.switch interleaved and ordered by timestamp ---
    window_events = [e for e in events if e["type"] == "window.switch"]
    assert len(window_events) > 0, "window.switch events should be present"

    # --- E1.7: window.switch deduplicated (consecutive only) ---
    # Deduplication compares each event to the *previous* emitted event only.
    # Non-consecutive duplicates (e.g. Terminal→Chrome→Terminal) are kept
    # because returning to an app is a real user action.
    # Our fixture: Terminal→Terminal(deduped)→Chrome = 2 emitted events.
    assert len(window_events) == 2, (
        f"Expected 2 window.switch events (consecutive duplicate removed), got {len(window_events)}"
    )

    # --- E1.8: Timestamps monotonically increasing ---
    timestamps = [e["timestamp"] for e in events]
    for i in range(1, len(timestamps)):
        assert timestamps[i] >= timestamps[i - 1], (
            f"Timestamp at index {i} ({timestamps[i]}) < previous ({timestamps[i-1]})"
        )

    # --- E1.9: key.type structure and merge/split behavior ---
    key_type_events = [e for e in events if e["type"] == "key.type"]
    # "hello" (0.1s gaps, merged) and "hi" (>0.5s gap from "hello", split)
    # merge_sequential_key_type_events splits on gaps >= KEY_TYPE_MERGE_INTERVAL_SECONDS (0.5s)
    assert len(key_type_events) == 2, (
        f"Expected 2 key.type events ('hello' + 'hi'), got {len(key_type_events)}: "
        f"{[e['text'] for e in key_type_events]}"
    )
    assert key_type_events[0]["text"] == "hello"
    # 5 chars × 2 (press+release each) = 10 children
    assert len(key_type_events[0]["children"]) == 10
    for child in key_type_events[0]["children"]:
        assert "key_char" in child

    assert key_type_events[1]["text"] == "hi"
    # 2 chars × 2 = 4 children
    assert len(key_type_events[1]["children"]) == 4

    # --- E1.10: mouse.singleclick structure ---
    click_events = [e for e in events if e["type"] == "mouse.singleclick"]
    assert len(click_events) == 2
    for ce in click_events:
        assert 0 <= ce["x"] <= 1512
        assert 0 <= ce["y"] <= 982
        assert ce["button"] == "left"

    # --- E1.11: window.switch structure ---
    for ws in window_events:
        assert "app_bundle_id" in ws
        assert "window_title" in ws
        assert "app_name" in ws

    # Chrome event should have domain; app_name derived from bundle_id last component
    chrome_ws = [ws for ws in window_events if ws["app_bundle_id"] == "com.google.Chrome"]
    assert len(chrome_ws) == 1
    assert chrome_ws[0]["domain"] == "example.com"
    assert chrome_ws[0]["app_name"] == "Chrome"

    # Terminal event should not have domain
    terminal_ws = [ws for ws in window_events if ws["app_bundle_id"] == "com.apple.Terminal"]
    assert len(terminal_ws) == 1
    assert terminal_ws[0]["domain"] is None
    assert terminal_ws[0]["app_name"] == "Terminal"

    # --- E1.12: Return value = event count ---
    assert count > 0
    assert count == len(events)

    # --- E1.13: Atomic write — no .tmp file remaining ---
    assert not os.path.exists(out_file + ".tmp")


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


def test_privacy_filter_excludes_and_masks(tmp_path, monkeypatch):
    """E5: Privacy filter in public mode — EXCLUDE suppresses, MASK_WINDOW replaces title."""
    import screencap.config
    monkeypatch.setattr(screencap.config, "_config_cache", None)
    monkeypatch.delenv("SCREENCAP_PRIVACY_MODE", raising=False)

    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()

    extra_windows = [
        {
            "timestamp": 1006.0,
            "title": "1Password — Vault",
            "app_bundle_id": "com.1password.1password",
            "window_id": "300",
            "left": 0, "top": 0, "width": 1512, "height": 982,
        },
        {
            "timestamp": 1009.0,
            "title": "#secret-channel — Slack",
            "app_bundle_id": "com.tinyspeck.slackmacgap",
            "window_id": "400",
            "left": 0, "top": 0, "width": 1512, "height": 982,
        },
    ]
    create_export_test_db(rec_dir / "recording.db", extra_window_events=extra_windows)

    from screencap.engine import Capture
    from screencap.exporter import _write_events, build_privacy_filter

    pf = build_privacy_filter(privacy_mode="public", cloud_intent=False)
    out_file = rec_dir / "events.jsonl"

    with Capture.load(str(rec_dir)) as capture:
        with open(out_file, "w") as f:
            count = _write_events(capture, f, True, None, privacy_filter=pf)

    events = [json.loads(line) for line in open(out_file).read().strip().split("\n")]
    ws_events = [e for e in events if e["type"] == "window.switch"]
    ws_bundles = {e["app_bundle_id"] for e in ws_events}

    # --- E5.1: EXCLUDE app suppressed ---
    assert "com.1password.1password" not in ws_bundles

    # --- E5.2: MASK_WINDOW app title masked ---
    # Slack (CHAT + PUBLIC = MASK_WINDOW) → title replaced with app_name
    slack_ws = [e for e in ws_events if e["app_bundle_id"] == "com.tinyspeck.slackmacgap"]
    assert len(slack_ws) == 1
    # app_name derived from bundle_id: "slackmacgap" → "Slackmacgap"
    assert slack_ws[0]["window_title"] == "Slackmacgap"
    assert slack_ws[0]["domain"] is None

    # --- E5.3: Normal apps pass through ---
    # Terminal (CODE_EDITOR_TERMINAL + PUBLIC = TEXT_REDACT → passes through unchanged)
    terminal_ws = [e for e in ws_events if e["app_bundle_id"] == "com.apple.Terminal"]
    assert len(terminal_ws) == 1
    assert terminal_ws[0]["window_title"] == "bash — 80×24"


def test_privacy_filter_cloud_intent(tmp_path, monkeypatch):
    """E5.4: cloud_intent=True forces public mode — Slack goes from TEXT_REDACT to MASK_WINDOW."""
    import screencap.config
    monkeypatch.setattr(screencap.config, "_config_cache", None)
    monkeypatch.delenv("SCREENCAP_PRIVACY_MODE", raising=False)

    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()

    extra_windows = [
        {
            "timestamp": 1009.0,
            "title": "#secret-channel — Slack",
            "app_bundle_id": "com.tinyspeck.slackmacgap",
            "window_id": "400",
            "left": 0, "top": 0, "width": 1512, "height": 982,
        },
    ]
    create_export_test_db(rec_dir / "recording.db", extra_window_events=extra_windows)

    from screencap.engine import Capture
    from screencap.exporter import _write_events, build_privacy_filter

    def _export_with_filter(pf):
        with Capture.load(str(rec_dir)) as capture:
            buf = io.StringIO()
            _write_events(capture, buf, True, None, privacy_filter=pf)
            buf.seek(0)
            return [json.loads(line) for line in buf.read().strip().split("\n")]

    # Internal mode without cloud_intent: Slack (CHAT) → TEXT_REDACT → passes through
    pf_internal = build_privacy_filter(privacy_mode="internal", cloud_intent=False)
    events_internal = _export_with_filter(pf_internal)
    slack_internal = [
        e for e in events_internal
        if e["type"] == "window.switch" and e["app_bundle_id"] == "com.tinyspeck.slackmacgap"
    ]
    assert len(slack_internal) == 1
    assert slack_internal[0]["window_title"] == "#secret-channel — Slack"

    # Internal mode WITH cloud_intent: forced to PUBLIC → Slack → MASK_WINDOW
    pf_cloud = build_privacy_filter(privacy_mode="internal", cloud_intent=True)
    events_cloud = _export_with_filter(pf_cloud)
    slack_cloud = [
        e for e in events_cloud
        if e["type"] == "window.switch" and e["app_bundle_id"] == "com.tinyspeck.slackmacgap"
    ]
    assert len(slack_cloud) == 1
    assert slack_cloud[0]["window_title"] == "Slackmacgap"  # masked to app_name
    assert slack_cloud[0]["domain"] is None


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


def test_cli_export_stdout(tmp_path, monkeypatch):
    """E6.4: --stdout writes JSONL to terminal, no file created."""
    rec_dir = tmp_path / "test-rec"
    rec_dir.mkdir()
    create_export_test_db(rec_dir / "recording.db")

    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))

    from click.testing import CliRunner
    from screencap.cli import cli

    result = CliRunner().invoke(cli, ["export", "test-rec", "--stdout"])

    assert result.exit_code == 0, result.output

    # JSONL on stdout — filter out stderr warning lines mixed in by CliRunner
    lines = result.output.strip().split("\n")
    jsonl_lines = [line for line in lines if line.startswith("{")]
    assert len(jsonl_lines) > 0, "Expected JSONL lines on stdout"
    for line in jsonl_lines:
        json.loads(line)

    # First JSONL line should be the _meta header
    header = json.loads(jsonl_lines[0])
    assert header["_meta"] is True

    # No file created (--stdout skips file write)
    assert not (rec_dir / "events.jsonl").exists()


def test_cli_export_not_found(tmp_path, monkeypatch):
    """E6.5: Non-existent recording name → non-zero exit code."""
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))

    from click.testing import CliRunner
    from screencap.cli import cli

    result = CliRunner().invoke(cli, ["export", "nonexistent"])

    assert result.exit_code != 0
    assert "not found" in result.output.lower() or "error" in result.output.lower()


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
