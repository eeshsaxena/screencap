"""Tests for screencap.catalog."""

import json
import sqlite3
import time
from pathlib import Path

import pytest

from screencap.catalog import find_db, get_seen_bundle_ids, list_recordings, read_intent


@pytest.fixture
def recordings_dir(tmp_path):
    return tmp_path / "recordings"


def _make_recording(base: Path, name: str, *, audio: bool = False, duration: float = 60.0):
    """Create a recording directory with real engine DB schema."""
    from screencap.engine.db import create_db, crud

    d = base / name
    d.mkdir(parents=True)

    db_path = d / "recording.db"
    started = time.time() - duration
    engine, Session = create_db(str(db_path))
    session = Session()

    recording = crud.insert_recording(session, {
        "timestamp": started,
        "platform": "darwin",
        "monitor_width": 1920,
        "monitor_height": 1080,
        "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })
    crud.insert_action_event(session, recording, started + duration, {
        "name": "click",
        "mouse_x": 100.0,
        "mouse_y": 200.0,
        "mouse_button_name": "left",
        "mouse_pressed": True,
    })
    session.close()
    engine.dispose()

    if audio:
        (d / "audio.flac").write_bytes(b"fake")

    return d


def _make_recording_with_window_events(base: Path, name: str, bundle_ids: list[str]):
    """Create a recording dir with real engine DB and window events."""
    from screencap.engine.db import create_db, crud

    d = base / name
    d.mkdir(parents=True)

    db_path = d / "recording.db"
    engine, Session = create_db(str(db_path))
    session = Session()

    recording = crud.insert_recording(session, {
        "timestamp": 1000.0,
        "platform": "darwin",
        "monitor_width": 1920,
        "monitor_height": 1080,
        "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })
    for i, bid in enumerate(bundle_ids):
        crud.insert_window_event(session, recording, 1000.0 + i, {
            "title": "title",
            "app_bundle_id": bid,
            "window_id": "w1",
            "left": 0, "top": 0, "width": 1920, "height": 1080,
        })
    session.close()
    engine.dispose()

    return d


def test_list_empty(recordings_dir):
    recordings_dir.mkdir(parents=True)
    result = list_recordings(recordings_dir)
    assert result == []


def test_list_nonexistent(tmp_path):
    result = list_recordings(tmp_path / "nope")
    assert result == []


def test_list_single(recordings_dir):
    _make_recording(recordings_dir, "test1", audio=True, duration=120)
    result = list_recordings(recordings_dir)
    assert len(result) == 1
    assert result[0].name == "test1"
    assert result[0].has_audio is True
    assert "2m" in result[0].duration


def test_list_multiple(recordings_dir):
    _make_recording(recordings_dir, "alpha", duration=60)
    _make_recording(recordings_dir, "beta", audio=True, duration=300)
    result = list_recordings(recordings_dir)
    assert len(result) == 2
    names = [r.name for r in result]
    assert "alpha" in names
    assert "beta" in names


def test_skips_non_recording_dirs(recordings_dir):
    recordings_dir.mkdir(parents=True)
    # Dir without recording.db should be skipped
    (recordings_dir / "random_dir").mkdir()
    result = list_recordings(recordings_dir)
    assert len(result) == 0


def test_find_db_returns_recording_db(tmp_path):
    (tmp_path / "recording.db").touch()
    assert find_db(tmp_path).name == "recording.db"


def test_find_db_returns_none(tmp_path):
    assert find_db(tmp_path) is None


# --- read_intent tests ---


@pytest.mark.parametrize("destination", ["cloud", "local"])
def test_read_intent_returns_destination(tmp_path, destination):
    intent_path = tmp_path / ".recording_intent"
    intent_path.write_text(
        json.dumps(
            {
                "version": 1,
                "destination": destination,
                "privacy_mode": "public",
                "created_at": "2026-03-07T14:30:00Z",
                "source": "flag",
            }
        )
    )
    assert read_intent(tmp_path) == destination


def test_read_intent_missing(tmp_path):
    assert read_intent(tmp_path) is None


def test_read_intent_corrupt(tmp_path):
    intent_path = tmp_path / ".recording_intent"
    intent_path.write_text("NOT VALID JSON {{{")
    assert read_intent(tmp_path) is None


# --- list_recordings intent integration tests ---


def test_list_recordings_with_intent(recordings_dir):
    d = _make_recording(recordings_dir, "cloud-rec", duration=30)
    intent_path = d / ".recording_intent"
    intent_path.write_text(
        json.dumps(
            {
                "version": 1,
                "destination": "cloud",
                "privacy_mode": "public",
                "created_at": "2026-03-07T14:30:00Z",
                "source": "flag",
            }
        )
    )
    result = list_recordings(recordings_dir)
    assert len(result) == 1
    assert result[0].name == "cloud-rec"
    assert result[0].intent == "cloud"


def test_list_recordings_legacy_no_intent(recordings_dir):
    _make_recording(recordings_dir, "legacy-rec", duration=45)
    result = list_recordings(recordings_dir)
    assert len(result) == 1
    assert result[0].name == "legacy-rec"
    assert result[0].intent is None


# --- Unit 4c: raw started_at + duration_seconds for SwiftUI consumers ---


def test_list_recordings_populates_raw_started_at(recordings_dir):
    """RecordingInfo.started_at carries the Unix timestamp from the recording row."""
    d = _make_recording(recordings_dir, "raw-fields-rec", duration=42.5)
    result = list_recordings(recordings_dir)
    assert len(result) == 1
    info = result[0]
    assert info.started_at is not None
    assert isinstance(info.started_at, float)
    # Started ~42.5s ago, so it should be in the past
    assert info.started_at < time.time()
    # Sanity: the formatted date matches the raw timestamp
    from datetime import datetime
    assert datetime.fromtimestamp(info.started_at).strftime("%Y-%m-%d") == info.date


def test_list_recordings_populates_raw_duration_seconds(recordings_dir):
    """RecordingInfo.duration_seconds carries the raw float duration."""
    _make_recording(recordings_dir, "duration-rec", duration=125.0)
    result = list_recordings(recordings_dir)
    info = result[0]
    assert info.duration_seconds is not None
    assert isinstance(info.duration_seconds, float)
    assert info.duration_seconds > 0
    # Sanity: formatted duration matches the raw float
    # (should be "2m 5s" for 125s)
    assert "2m" in info.duration


def test_recording_info_namedtuple_asdict_includes_new_fields(recordings_dir):
    """_asdict() serializes the new fields (so JSON output picks them up)."""
    _make_recording(recordings_dir, "asdict-rec", duration=10.0)
    info = list_recordings(recordings_dir)[0]
    d = info._asdict()
    assert "started_at" in d
    assert "duration_seconds" in d
    assert d["started_at"] is not None
    assert d["duration_seconds"] is not None


# --- U3 catalog guard: hidden review artifact must not mask stub detection ---


def test_lingering_review_artifact_does_not_mask_stub(recordings_dir):
    """An uploaded recording with only a hidden .video_review.mp4 is still a stub.

    pathlib glob("*.mp4") matches dotfiles, so without the guard the review
    artifact would count as media and hide a post-upload stub (R6).
    """
    d = _make_recording(recordings_dir, "stub-rec", duration=30)
    (d / ".upload_status.json").write_text("{}")  # mark uploaded
    (d / ".video_review.mp4").write_bytes(b"fake review video")  # lingering artifact

    info = list_recordings(recordings_dir)[0]
    assert info.uploaded is True
    assert info.is_stub is True


def test_real_video_is_not_a_stub(recordings_dir):
    """Control: a real (non-hidden) video.mp4 counts as media even when uploaded."""
    d = _make_recording(recordings_dir, "live-rec", duration=30)
    (d / ".upload_status.json").write_text("{}")
    (d / "video.mp4").write_bytes(b"real video bytes")

    info = list_recordings(recordings_dir)[0]
    assert info.uploaded is True
    assert info.is_stub is False


# --- get_seen_bundle_ids tests ---


def test_get_seen_bundle_ids_single_dir(tmp_path):
    d = _make_recording_with_window_events(
        tmp_path, "rec1", ["com.example.foo", "com.example.bar"]
    )
    result = get_seen_bundle_ids([d])
    assert result == {"com.example.foo", "com.example.bar"}


def test_get_seen_bundle_ids_multiple_dirs(tmp_path):
    d1 = _make_recording_with_window_events(tmp_path, "rec1", ["com.example.foo"])
    d2 = _make_recording_with_window_events(tmp_path, "rec2", ["com.example.bar"])
    result = get_seen_bundle_ids([d1, d2])
    assert result == {"com.example.foo", "com.example.bar"}


def test_get_seen_bundle_ids_deduplicates(tmp_path):
    d1 = _make_recording_with_window_events(tmp_path, "rec1", ["com.example.foo"])
    d2 = _make_recording_with_window_events(tmp_path, "rec2", ["com.example.foo"])
    result = get_seen_bundle_ids([d1, d2])
    assert result == {"com.example.foo"}


def test_get_seen_bundle_ids_no_window_event_table(tmp_path):
    """Dirs without window_event table are silently skipped."""
    d = tmp_path / "rec1"
    d.mkdir()
    db_path = d / "recording.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    result = get_seen_bundle_ids([d])
    assert result == set()


def test_get_seen_bundle_ids_empty_dirs(tmp_path):
    result = get_seen_bundle_ids([])
    assert result == set()
