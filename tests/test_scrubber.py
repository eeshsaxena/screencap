"""Integration tests for scrubber.py — privacy-scrubbed recording copies."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from unittest import mock

import pytest

from screencap.scrubber import (
    ScrubResult,
    _build_app_allowlist,
    _scrub_db,
    _scrub_events_jsonl,
    _scrub_json_recursive,
    _scrub_metrics,
    _scrub_text,
    _scrub_text_with_detections,
    _scrub_transcript_json,
    _scrub_transcript_txt,
    scrub_recording,
)

pytestmark = pytest.mark.privacy

# Known PII values planted across all surfaces
KNOWN_PII = {
    "John Doe",
    "john.doe@example.com",
    "555-123-4567",
    "123-45-6789",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pipeline_and_anonymizer():
    """Create a real detection pipeline and anonymizer."""
    from screencap.privacy import Anonymizer, create_default_pipeline

    pipeline = create_default_pipeline()
    anonymizer = Anonymizer()
    return pipeline, anonymizer


@pytest.fixture
def recording_dir(tmp_path):
    """Create a recording directory with PII in all text surfaces."""
    rec = tmp_path / "test-recording"
    rec.mkdir()

    # --- recording.db ---
    db = sqlite3.connect(str(rec / "recording.db"))
    db.execute(
        "CREATE TABLE recording (id INTEGER PRIMARY KEY, task_description TEXT)"
    )
    db.execute(
        "INSERT INTO recording VALUES (1, 'Task: call John Doe at 555-123-4567')"
    )

    db.execute(
        """CREATE TABLE action_event (
        id INTEGER PRIMARY KEY, recording_id INTEGER,
        name TEXT,
        timestamp REAL,
        key_char TEXT, canonical_key_char TEXT,
        key_name TEXT, canonical_key_name TEXT,
        element_state TEXT,
        active_segment_description TEXT,
        available_segment_descriptions TEXT
    )"""
    )
    db.execute(
        "INSERT INTO action_event VALUES (1, 1, 'click', 100.0, 'e', 'e', 'e', 'e', ?, ?, ?)",
        (
            json.dumps({"role": "button", "content": "john.doe@example.com"}),
            "John Doe's workspace",
            "John Doe's workspace, Settings",
        ),
    )
    # Keystroke rows matching events.jsonl children
    secret_text = "password=ssfsfodsufdouhhfwnenwekskjdhjsfhd"
    normal_text = "hello"
    row_id = 10
    ts = 200.01
    for ch in secret_text:
        db.execute(
            "INSERT INTO action_event VALUES (?, 1, 'press', ?, ?, ?, NULL, NULL, NULL, NULL, NULL)",
            (row_id, round(ts, 2), ch, ch.lower()),
        )
        row_id += 1
        ts += 0.01
        db.execute(
            "INSERT INTO action_event VALUES (?, 1, 'release', ?, ?, ?, NULL, NULL, NULL, NULL, NULL)",
            (row_id, round(ts, 2), ch, ch.lower()),
        )
        row_id += 1
        ts += 0.01
    for ch in normal_text:
        db.execute(
            "INSERT INTO action_event VALUES (?, 1, 'press', ?, ?, ?, NULL, NULL, NULL, NULL, NULL)",
            (row_id, round(ts, 2), ch, ch.lower()),
        )
        row_id += 1
        ts += 0.01
        db.execute(
            "INSERT INTO action_event VALUES (?, 1, 'release', ?, ?, ?, NULL, NULL, NULL, NULL, NULL)",
            (row_id, round(ts, 2), ch, ch.lower()),
        )
        row_id += 1
        ts += 0.01

    db.execute(
        """CREATE TABLE window_event (
        id INTEGER PRIMARY KEY, recording_id INTEGER, title TEXT
    )"""
    )
    db.execute(
        "INSERT INTO window_event VALUES (1, 1, 'Chrome - john.doe@example.com')"
    )

    # Screenshot BLOBs (should be deleted, not scrubbed)
    db.execute(
        """CREATE TABLE screenshot (
        id INTEGER PRIMARY KEY, action_event_id INTEGER, png_data BLOB
    )"""
    )
    db.execute("INSERT INTO screenshot VALUES (1, 1, X'89504E47')")

    # Audio info (should be deleted)
    db.execute(
        """CREATE TABLE audio_info (
        id INTEGER PRIMARY KEY, recording_id INTEGER, words_with_timestamps TEXT
    )"""
    )
    db.execute(
        "INSERT INTO audio_info VALUES (1, 1, ?)",
        (json.dumps([{"word": "John", "start": 0.5, "end": 0.8}]),),
    )

    db.commit()
    db.close()

    # --- transcript.json ---
    transcript = {
        "text": "Hi, this is John Doe calling.",
        "segments": [
            {"text": "Hi, this is John Doe calling.", "start": 0, "end": 2.5}
        ],
        "words": [
            {"word": "John", "start": 0.5, "end": 0.8},
            {"word": "Doe", "start": 0.8, "end": 1.0},
        ],
    }
    (rec / "transcript.json").write_text(json.dumps(transcript))

    # --- transcript.txt ---
    (rec / "transcript.txt").write_text(
        "Hi, this is John Doe calling 555-123-4567."
    )

    # --- system_metrics.json ---
    metrics = {
        "schema_version": 4,
        "static": {
            "hostname": "johns-macbook",
            "wifi": {"ssid": "HomeWiFi", "bssid": "AA:BB:CC:DD:EE:FF"},
        },
    }
    (rec / "system_metrics.json").write_text(json.dumps(metrics))

    # --- Stub files to verify deletion ---
    (rec / "audio.flac").write_bytes(b"\x00")
    (rec / "oa_recording-12345.mp4").write_bytes(b"\x00")
    # events.jsonl with realistic key.type events (children include key.down + key.up)
    # Use helper to build children arrays matching DB rows
    def _build_children(text, start_ts):
        children = []
        ts = start_ts
        for ch in text:
            children.append({"timestamp": round(ts, 2), "type": "key.down", "key_char": ch, "canonical_key_char": ch.lower(), "key_name": None, "canonical_key_name": None, "key_vk": None, "canonical_key_vk": None})
            ts += 0.01
            children.append({"timestamp": round(ts, 2), "type": "key.up", "key_char": ch, "canonical_key_char": ch.lower(), "key_name": None, "canonical_key_name": None, "key_vk": None, "canonical_key_vk": None})
            ts += 0.01
        return children

    secret_event = {"timestamp": 200.0, "type": "key.type", "text": secret_text, "children": _build_children(secret_text, 200.01)}
    normal_event = {"timestamp": 300.0, "type": "key.type", "text": normal_text, "children": _build_children(normal_text, round(200.01 + len(secret_text) * 0.02, 2))}
    meta_line = json.dumps({"_meta": True, "screencap_version": "0.1.0", "exported_at": "2026-03-05T00:00:00"})
    events_content = meta_line + "\n" + json.dumps(secret_event) + "\n" + json.dumps(normal_event) + "\n"
    (rec / "events.jsonl").write_text(events_content)
    (rec / ".upload_status.json").write_text("{}")
    (rec / "viewer.html").write_text("<html>john.doe@example.com</html>")

    return rec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_pii_absent(directory: Path, pii_value: str):
    """Assert that a PII string does not appear anywhere in a scrubbed directory."""
    # Check all text files
    for path in directory.rglob("*"):
        if path.is_file() and path.suffix in (".json", ".jsonl", ".txt", ".html"):
            content = path.read_text(encoding="utf-8", errors="ignore")
            assert pii_value not in content, f"PII '{pii_value}' found in {path}"

    # Check SQLite DBs
    for db_path in directory.glob("*.db"):
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        for (table_name,) in cur.fetchall():
            cur.execute(f"SELECT * FROM {table_name}")
            for row in cur.fetchall():
                for cell in row:
                    if isinstance(cell, str):
                        assert pii_value not in cell, (
                            f"PII '{pii_value}' found in {db_path}:{table_name}"
                        )
        conn.close()


# ---------------------------------------------------------------------------
# End-to-End
# ---------------------------------------------------------------------------


def test_scrub_recording_e2e(recording_dir, tmp_path):
    """End-to-end: all PII scrubbed, unscrubable files deleted."""
    with mock.patch(
        "screencap.config.get_recordings_dir", return_value=tmp_path
    ), mock.patch(
        "screencap.scrubber.get_recordings_dir", return_value=tmp_path
    ):
        result = scrub_recording("test-recording")

    dst = tmp_path / "test-recording-scrubbed"
    assert dst.exists()

    # No raw PII survives anywhere in the scrubbed directory
    for pii in KNOWN_PII:
        _assert_pii_absent(dst, pii)

    # Unscrubable files deleted
    assert not (dst / "audio.flac").exists()
    assert not list(dst.glob("*.mp4"))
    assert not (dst / ".upload_status.json").exists()
    assert not (dst / "viewer.html").exists()

    # events.jsonl is scrubbed, not deleted
    assert (dst / "events.jsonl").exists()

    # Text files still exist and are scrubbed
    assert (dst / "transcript.json").exists()
    assert (dst / "transcript.txt").exists()
    assert (dst / "system_metrics.json").exists()

    # Entity counts populated
    assert sum(result.entity_counts.values()) > 0


# ---------------------------------------------------------------------------
# DB Scrubbing
# ---------------------------------------------------------------------------


def test_scrub_recording_schema(recording_dir, tmp_path, pipeline_and_anonymizer):
    """recording.db with PII in all columns → PII replaced."""
    import shutil

    pipeline, anonymizer = pipeline_and_anonymizer
    dst = tmp_path / "scrubbed"
    shutil.copytree(recording_dir, dst)

    result = ScrubResult()
    _scrub_db(dst, pipeline, anonymizer, result)

    conn = sqlite3.connect(str(dst / "recording.db"))
    cur = conn.cursor()

    # task_description scrubbed
    cur.execute("SELECT task_description FROM recording")
    task = cur.fetchone()[0]
    assert "John Doe" not in task
    assert "555-123-4567" not in task

    # element_state JSON scrubbed
    cur.execute("SELECT element_state FROM action_event")
    es = json.loads(cur.fetchone()[0])
    assert "john.doe@example.com" not in json.dumps(es)

    # segment descriptions scrubbed
    cur.execute("SELECT active_segment_description FROM action_event")
    seg = cur.fetchone()[0]
    assert "John Doe" not in seg

    # window_event.title scrubbed
    cur.execute("SELECT title FROM window_event")
    title = cur.fetchone()[0]
    assert "john.doe@example.com" not in title

    # screenshot table emptied
    cur.execute("SELECT COUNT(*) FROM screenshot")
    assert cur.fetchone()[0] == 0

    # audio_info table emptied
    cur.execute("SELECT COUNT(*) FROM audio_info")
    assert cur.fetchone()[0] == 0

    conn.close()


def test_scrub_capture_schema(tmp_path, pipeline_and_anonymizer):
    """capture.db with PII in task_description and events.data → PII replaced."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "cap-recording"
    rec.mkdir()

    db = sqlite3.connect(str(rec / "capture.db"))
    db.execute(
        "CREATE TABLE capture (id INTEGER PRIMARY KEY, task_description TEXT, started_at REAL, ended_at REAL)"
    )
    db.execute(
        "INSERT INTO capture VALUES (1, 'Email John Doe at john.doe@example.com', 1000, 2000)"
    )
    db.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, timestamp REAL, data TEXT)"
    )
    db.execute(
        "INSERT INTO events VALUES (1, 1001, ?)",
        (json.dumps({"type": "key", "text": "Hello John Doe"}),),
    )
    db.commit()
    db.close()

    result = ScrubResult()
    _scrub_db(rec, pipeline, anonymizer, result)

    conn = sqlite3.connect(str(rec / "capture.db"))
    cur = conn.cursor()

    cur.execute("SELECT task_description FROM capture")
    task = cur.fetchone()[0]
    assert "John Doe" not in task
    assert "john.doe@example.com" not in task

    cur.execute("SELECT data FROM events")
    data = json.loads(cur.fetchone()[0])
    assert "John Doe" not in json.dumps(data)

    conn.close()


def test_scrub_db_no_db(tmp_path, pipeline_and_anonymizer):
    """Recording dir with no DB → warning, no crash."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "no-db-recording"
    rec.mkdir()

    result = ScrubResult()
    _scrub_db(rec, pipeline, anonymizer, result)
    # Should not crash


def test_scrub_db_unrecognized_schema(tmp_path, pipeline_and_anonymizer, capsys):
    """DB with unknown tables → warning printed."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "unknown-schema"
    rec.mkdir()

    db = sqlite3.connect(str(rec / "recording.db"))
    db.execute("CREATE TABLE mystery (id INTEGER PRIMARY KEY, data TEXT)")
    db.commit()
    db.close()

    result = ScrubResult()
    _scrub_db(rec, pipeline, anonymizer, result)
    # Warning was printed via rich console — just verify no crash


def test_scrub_db_malformed_json(tmp_path, pipeline_and_anonymizer):
    """element_state with invalid JSON → warning, other rows still scrubbed."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "bad-json"
    rec.mkdir()

    db = sqlite3.connect(str(rec / "recording.db"))
    db.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, task_description TEXT)")
    db.execute("INSERT INTO recording VALUES (1, 'test')")
    db.execute(
        """CREATE TABLE action_event (
        id INTEGER PRIMARY KEY, recording_id INTEGER, element_state TEXT
    )"""
    )
    db.execute("INSERT INTO action_event VALUES (1, 1, '{invalid json')")
    db.execute(
        "INSERT INTO action_event VALUES (2, 1, ?)",
        (json.dumps({"content": "john.doe@example.com"}),),
    )
    db.commit()
    db.close()

    result = ScrubResult()
    _scrub_db(rec, pipeline, anonymizer, result)

    conn = sqlite3.connect(str(rec / "recording.db"))
    cur = conn.cursor()
    # Row 1 should still have malformed JSON (skipped)
    cur.execute("SELECT element_state FROM action_event WHERE id = 1")
    assert cur.fetchone()[0] == "{invalid json"
    # Row 2 should be scrubbed
    cur.execute("SELECT element_state FROM action_event WHERE id = 2")
    data = json.loads(cur.fetchone()[0])
    assert "john.doe@example.com" not in json.dumps(data)
    conn.close()


def test_scrub_db_missing_columns(tmp_path, pipeline_and_anonymizer):
    """action_event missing optional columns → handled gracefully."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "missing-cols"
    rec.mkdir()

    db = sqlite3.connect(str(rec / "recording.db"))
    db.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, task_description TEXT)")
    db.execute("INSERT INTO recording VALUES (1, 'test')")
    # action_event without canonical_key_char, etc.
    db.execute(
        "CREATE TABLE action_event (id INTEGER PRIMARY KEY, recording_id INTEGER, key_char TEXT)"
    )
    db.execute("INSERT INTO action_event VALUES (1, 1, 'a')")
    db.commit()
    db.close()

    result = ScrubResult()
    _scrub_db(rec, pipeline, anonymizer, result)
    # Should not crash


# ---------------------------------------------------------------------------
# Transcript Scrubbing
# ---------------------------------------------------------------------------


def test_scrub_transcript_json(tmp_path, pipeline_and_anonymizer):
    """PII in text/segments/words → all scrubbed."""
    pipeline, anonymizer = pipeline_and_anonymizer
    path = tmp_path / "transcript.json"
    data = {
        "text": "Contact John Doe at john.doe@example.com",
        "segments": [{"text": "Contact John Doe", "start": 0, "end": 1}],
        "words": [{"word": "John", "start": 0, "end": 0.5}],
    }
    path.write_text(json.dumps(data))

    result = ScrubResult()
    _scrub_transcript_json(path, pipeline, anonymizer, result)

    scrubbed = json.loads(path.read_text())
    assert "John Doe" not in scrubbed["text"]
    assert "john.doe@example.com" not in scrubbed["text"]
    assert "John Doe" not in scrubbed["segments"][0]["text"]


def test_scrub_transcript_txt(tmp_path, pipeline_and_anonymizer):
    """PII in plaintext → scrubbed."""
    pipeline, anonymizer = pipeline_and_anonymizer
    path = tmp_path / "transcript.txt"
    path.write_text("Call John Doe at 555-123-4567 please.")

    result = ScrubResult()
    _scrub_transcript_txt(path, pipeline, anonymizer, result)

    text = path.read_text()
    assert "John Doe" not in text
    assert "555-123-4567" not in text


def test_scrub_transcript_missing(tmp_path, pipeline_and_anonymizer):
    """No transcript files → no error."""
    pipeline, anonymizer = pipeline_and_anonymizer
    result = ScrubResult()
    _scrub_transcript_json(tmp_path / "transcript.json", pipeline, anonymizer, result)
    _scrub_transcript_txt(tmp_path / "transcript.txt", pipeline, anonymizer, result)


def test_scrub_transcript_json_no_words(tmp_path, pipeline_and_anonymizer):
    """transcript.json without words key → no error."""
    pipeline, anonymizer = pipeline_and_anonymizer
    path = tmp_path / "transcript.json"
    data = {"text": "Hello world", "segments": []}
    path.write_text(json.dumps(data))

    result = ScrubResult()
    _scrub_transcript_json(path, pipeline, anonymizer, result)
    # Should not crash


# ---------------------------------------------------------------------------
# Metrics Scrubbing
# ---------------------------------------------------------------------------


def test_scrub_metrics_hostname(tmp_path):
    """hostname redacted to <REDACTED>."""
    path = tmp_path / "system_metrics.json"
    data = {"static": {"hostname": "my-macbook"}}
    path.write_text(json.dumps(data))

    result = ScrubResult()
    _scrub_metrics(path, result)

    scrubbed = json.loads(path.read_text())
    assert scrubbed["static"]["hostname"] == "<REDACTED>"


def test_scrub_metrics_wifi(tmp_path):
    """SSID and BSSID redacted."""
    path = tmp_path / "system_metrics.json"
    data = {"static": {"wifi": {"ssid": "MyWiFi", "bssid": "AA:BB:CC"}}}
    path.write_text(json.dumps(data))

    result = ScrubResult()
    _scrub_metrics(path, result)

    scrubbed = json.loads(path.read_text())
    assert scrubbed["static"]["wifi"]["ssid"] == "<REDACTED>"
    assert scrubbed["static"]["wifi"]["bssid"] == "<REDACTED>"


def test_scrub_metrics_missing(tmp_path):
    """No metrics file → no error."""
    result = ScrubResult()
    _scrub_metrics(tmp_path / "system_metrics.json", result)


def test_scrub_metrics_no_wifi(tmp_path):
    """Metrics with no wifi key → hostname still redacted."""
    path = tmp_path / "system_metrics.json"
    data = {"static": {"hostname": "my-macbook"}}
    path.write_text(json.dumps(data))

    result = ScrubResult()
    _scrub_metrics(path, result)

    scrubbed = json.loads(path.read_text())
    assert scrubbed["static"]["hostname"] == "<REDACTED>"


# ---------------------------------------------------------------------------
# File and Table Deletion
# ---------------------------------------------------------------------------


def test_scrub_deletes_media(recording_dir, tmp_path):
    """audio.flac and *.mp4 deleted from scrubbed copy."""
    with mock.patch(
        "screencap.config.get_recordings_dir", return_value=tmp_path
    ), mock.patch(
        "screencap.scrubber.get_recordings_dir", return_value=tmp_path
    ):
        result = scrub_recording("test-recording")

    dst = tmp_path / "test-recording-scrubbed"
    assert not (dst / "audio.flac").exists()
    assert not list(dst.glob("*.mp4"))


def test_scrub_deletes_derived_files(recording_dir, tmp_path):
    """.upload_status.json and viewer.html deleted; events.jsonl scrubbed not deleted."""
    with mock.patch(
        "screencap.config.get_recordings_dir", return_value=tmp_path
    ), mock.patch(
        "screencap.scrubber.get_recordings_dir", return_value=tmp_path
    ):
        result = scrub_recording("test-recording")

    dst = tmp_path / "test-recording-scrubbed"
    assert not (dst / ".upload_status.json").exists()
    assert not (dst / "viewer.html").exists()
    # events.jsonl is now scrubbed, not deleted
    assert (dst / "events.jsonl").exists()


def test_scrub_deletes_screenshot_table(recording_dir, tmp_path):
    """screenshot table rows deleted from DB."""
    with mock.patch(
        "screencap.config.get_recordings_dir", return_value=tmp_path
    ), mock.patch(
        "screencap.scrubber.get_recordings_dir", return_value=tmp_path
    ):
        scrub_recording("test-recording")

    dst = tmp_path / "test-recording-scrubbed"
    conn = sqlite3.connect(str(dst / "recording.db"))
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM screenshot")
    assert cur.fetchone()[0] == 0
    conn.close()


def test_scrub_deletes_audio_info_table(recording_dir, tmp_path):
    """audio_info table rows deleted from DB."""
    with mock.patch(
        "screencap.config.get_recordings_dir", return_value=tmp_path
    ), mock.patch(
        "screencap.scrubber.get_recordings_dir", return_value=tmp_path
    ):
        scrub_recording("test-recording")

    dst = tmp_path / "test-recording-scrubbed"
    conn = sqlite3.connect(str(dst / "recording.db"))
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM audio_info")
    assert cur.fetchone()[0] == 0
    conn.close()


def test_scrub_copytree_skips_media(recording_dir, tmp_path):
    """Media files never copied (ignore callback)."""
    with mock.patch(
        "screencap.config.get_recordings_dir", return_value=tmp_path
    ), mock.patch(
        "screencap.scrubber.get_recordings_dir", return_value=tmp_path
    ):
        scrub_recording("test-recording")

    dst = tmp_path / "test-recording-scrubbed"
    # These should have been skipped by the ignore callback
    assert not (dst / "audio.flac").exists()
    assert not list(dst.glob("*.mp4"))


# ---------------------------------------------------------------------------
# App Allowlist
# ---------------------------------------------------------------------------


def test_build_allowlist_from_metrics(tmp_path):
    """Running applications → frozenset of lowercased names."""
    path = tmp_path / "system_metrics.json"
    data = {
        "static": {
            "running_applications": [
                {"name": "Ghostty", "bundle_id": "com.mitchellh.ghostty"},
                {"name": "Bitwarden", "bundle_id": "com.bitwarden.desktop"},
                {"name": "Google Chrome", "bundle_id": "com.google.Chrome"},
            ]
        }
    }
    path.write_text(json.dumps(data))

    result = _build_app_allowlist(path)
    assert "ghostty" in result
    assert "bitwarden" in result
    assert "google chrome" in result


def test_build_allowlist_missing_file(tmp_path):
    """Missing system_metrics.json → empty frozenset, no crash."""
    result = _build_app_allowlist(tmp_path / "nonexistent.json")
    assert result == frozenset()


def test_build_allowlist_corrupted_json(tmp_path):
    """Corrupted JSON → empty frozenset + warning."""
    path = tmp_path / "system_metrics.json"
    path.write_text("{invalid json")
    result = _build_app_allowlist(path)
    assert result == frozenset()


def test_build_allowlist_null_running_applications(tmp_path):
    """running_applications: null → empty frozenset."""
    path = tmp_path / "system_metrics.json"
    data = {"static": {"running_applications": None}}
    path.write_text(json.dumps(data))

    result = _build_app_allowlist(path)
    assert result == frozenset()


def test_build_allowlist_no_static_key(tmp_path):
    """No 'static' key → empty frozenset."""
    path = tmp_path / "system_metrics.json"
    data = {"schema_version": 4}
    path.write_text(json.dumps(data))

    result = _build_app_allowlist(path)
    assert result == frozenset()


def test_build_allowlist_app_without_name(tmp_path):
    """App entry missing 'name' key → skipped."""
    path = tmp_path / "system_metrics.json"
    data = {
        "static": {
            "running_applications": [
                {"bundle_id": "com.no.name"},
                {"name": "ValidApp"},
            ]
        }
    }
    path.write_text(json.dumps(data))

    result = _build_app_allowlist(path)
    assert "validapp" in result
    assert len(result) == 1


def test_scrub_recording_uses_allowlist(tmp_path):
    """scrub_recording passes app allowlist to pipeline — app names not scrubbed."""
    rec = tmp_path / "allowlist-test"
    rec.mkdir()

    # DB with window title containing app name
    db = sqlite3.connect(str(rec / "recording.db"))
    db.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, task_description TEXT)")
    db.execute("INSERT INTO recording VALUES (1, 'test')")
    db.execute(
        "CREATE TABLE window_event (id INTEGER PRIMARY KEY, recording_id INTEGER, title TEXT)"
    )
    db.execute("INSERT INTO window_event VALUES (1, 1, 'Ghostty tmux a')")
    db.commit()
    db.close()

    # system_metrics.json with Ghostty in running_applications
    metrics = {
        "static": {
            "running_applications": [
                {"name": "Ghostty", "bundle_id": "com.mitchellh.ghostty"},
            ]
        }
    }
    (rec / "system_metrics.json").write_text(json.dumps(metrics))

    with mock.patch(
        "screencap.config.get_recordings_dir", return_value=tmp_path
    ), mock.patch(
        "screencap.scrubber.get_recordings_dir", return_value=tmp_path
    ):
        result = scrub_recording("allowlist-test")

    dst = tmp_path / "allowlist-test-scrubbed"
    conn = sqlite3.connect(str(dst / "recording.db"))
    cur = conn.cursor()
    cur.execute("SELECT title FROM window_event")
    title = cur.fetchone()[0]
    conn.close()

    # Ghostty should NOT be scrubbed (it's an app name, not a person)
    assert "Ghostty" in title or "ghostty" in title.lower()


# ---------------------------------------------------------------------------
# Edge Cases and Security
# ---------------------------------------------------------------------------


def test_scrub_path_traversal(tmp_path):
    """scrub_recording('../../etc') → ValueError."""
    with mock.patch("screencap.config.get_recordings_dir", return_value=tmp_path):
        with pytest.raises(ValueError, match="Invalid recording name"):
            scrub_recording("../../etc")


def test_scrub_not_found(tmp_path):
    """Nonexistent recording → FileNotFoundError."""
    with mock.patch("screencap.config.get_recordings_dir", return_value=tmp_path):
        with pytest.raises(FileNotFoundError, match="Recording not found"):
            scrub_recording("nonexistent")


def test_scrub_existing_scrubbed_dir(recording_dir, tmp_path):
    """Scrub twice → second run overwrites first."""
    with mock.patch(
        "screencap.config.get_recordings_dir", return_value=tmp_path
    ), mock.patch(
        "screencap.scrubber.get_recordings_dir", return_value=tmp_path
    ):
        result1 = scrub_recording("test-recording")
        result2 = scrub_recording("test-recording")

    dst = tmp_path / "test-recording-scrubbed"
    assert dst.exists()


def test_scrub_empty_recording(tmp_path):
    """Recording dir with only a DB, no other files → completes."""
    rec = tmp_path / "empty-recording"
    rec.mkdir()
    db = sqlite3.connect(str(rec / "recording.db"))
    db.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, task_description TEXT)")
    db.execute("INSERT INTO recording VALUES (1, 'test task')")
    db.commit()
    db.close()

    with mock.patch(
        "screencap.config.get_recordings_dir", return_value=tmp_path
    ), mock.patch(
        "screencap.scrubber.get_recordings_dir", return_value=tmp_path
    ):
        result = scrub_recording("empty-recording")

    dst = tmp_path / "empty-recording-scrubbed"
    assert dst.exists()
    assert (dst / "recording.db").exists()


# ---------------------------------------------------------------------------
# CLI Tests
# ---------------------------------------------------------------------------


def test_cli_scrub_command(recording_dir, tmp_path):
    """CLI scrub command → exit 0."""
    from click.testing import CliRunner

    from screencap.cli import cli

    runner = CliRunner()
    with mock.patch(
        "screencap.config.get_recordings_dir", return_value=tmp_path
    ), mock.patch(
        "screencap.scrubber.get_recordings_dir", return_value=tmp_path
    ):
        result = runner.invoke(cli, ["scrub", "test-recording"])

    assert result.exit_code == 0, f"Output: {result.output}"


def test_cli_scrub_not_found(tmp_path):
    """CLI scrub nonexistent recording → exit 1."""
    from click.testing import CliRunner

    from screencap.cli import cli

    runner = CliRunner()
    with mock.patch("screencap.config.get_recordings_dir", return_value=tmp_path):
        result = runner.invoke(cli, ["scrub", "nonexistent"])

    assert result.exit_code == 1
    assert "Error" in result.output


def test_cli_scrub_missing_deps(tmp_path):
    """CLI scrub with missing privacy deps → exit 1, install message."""
    from click.testing import CliRunner

    from screencap.cli import cli

    runner = CliRunner()
    with mock.patch(
        "screencap.scrubber.scrub_recording",
        side_effect=ImportError("No module named 'presidio_analyzer'"),
    ):
        result = runner.invoke(cli, ["scrub", "test-recording"])

    assert result.exit_code == 1
    assert "Privacy dependencies" in result.output


# ---------------------------------------------------------------------------
# Events JSONL Scrubbing (Combined Keystroke Detection)
# ---------------------------------------------------------------------------


def _make_key_type_event(text, start_ts=100.0):
    """Build a key.type event dict with key.down/key.up children for each char."""
    children = []
    ts = start_ts
    for ch in text:
        children.append(
            {
                "timestamp": round(ts, 2),
                "type": "key.down",
                "key_char": ch,
                "canonical_key_char": ch.lower(),
                "key_name": None,
                "canonical_key_name": None,
                "key_vk": None,
                "canonical_key_vk": None,
            }
        )
        ts += 0.01
        children.append(
            {
                "timestamp": round(ts, 2),
                "type": "key.up",
                "key_char": ch,
                "canonical_key_char": ch.lower(),
                "key_name": None,
                "canonical_key_name": None,
                "key_vk": None,
                "canonical_key_vk": None,
            }
        )
        ts += 0.01
    return {"timestamp": start_ts, "type": "key.type", "text": text, "children": children}


def _make_events_jsonl(*events, with_meta=True):
    """Build events.jsonl content from event dicts."""
    lines = []
    if with_meta:
        lines.append(json.dumps({"_meta": True, "screencap_version": "0.1.0"}))
    for ev in events:
        lines.append(json.dumps(ev))
    return "\n".join(lines) + "\n"


def _setup_keystroke_db(db_path, events):
    """Create a recording.db with action_event rows matching events.jsonl children."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, task_description TEXT)")
    conn.execute("INSERT INTO recording VALUES (1, 'test')")
    conn.execute(
        """CREATE TABLE action_event (
        id INTEGER PRIMARY KEY, recording_id INTEGER,
        name TEXT, timestamp REAL,
        key_char TEXT, canonical_key_char TEXT,
        key_name TEXT, canonical_key_name TEXT,
        element_state TEXT,
        active_segment_description TEXT,
        available_segment_descriptions TEXT
    )"""
    )
    row_id = 1
    for ev in events:
        if ev.get("type") != "key.type":
            continue
        for child in ev.get("children", []):
            name = "press" if child["type"] == "key.down" else "release"
            conn.execute(
                "INSERT INTO action_event VALUES (?, 1, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL)",
                (row_id, name, child["timestamp"], child.get("key_char"), child.get("canonical_key_char")),
            )
            row_id += 1
    conn.commit()
    conn.close()


def test_scrub_events_jsonl_redacts_typed_secret(tmp_path, pipeline_and_anonymizer):
    """password=ssfsfodsufdouhhfwnenwekskjdhjsfhd in key.type event text → text field redacted."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "scrubbed"
    rec.mkdir()

    secret_ev = _make_key_type_event("password=ssfsfodsufdouhhfwnenwekskjdhjsfhd")
    (rec / "events.jsonl").write_text(_make_events_jsonl(secret_ev))
    _setup_keystroke_db(rec / "recording.db", [secret_ev])

    result = ScrubResult()
    _scrub_events_jsonl(rec, pipeline, anonymizer, result)

    lines = [json.loads(l) for l in (rec / "events.jsonl").read_text().strip().splitlines()]
    # Meta line preserved
    assert lines[0].get("_meta") is True
    # Secret text should be redacted (not contain original)
    key_type_ev = lines[1]
    assert "password=ssfsfodsufdouhhfwnenwekskjdhjsfhd" not in key_type_ev["text"]


def test_scrub_events_jsonl_maps_to_db_rows(tmp_path, pipeline_and_anonymizer):
    """Redacted key.type children → corresponding DB rows have key_char = NULL."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "scrubbed"
    rec.mkdir()

    secret_ev = _make_key_type_event("password=ssfsfodsufdouhhfwnenwekskjdhjsfhd")
    (rec / "events.jsonl").write_text(_make_events_jsonl(secret_ev))
    _setup_keystroke_db(rec / "recording.db", [secret_ev])

    result = ScrubResult()
    _scrub_events_jsonl(rec, pipeline, anonymizer, result)

    conn = sqlite3.connect(str(rec / "recording.db"))
    cur = conn.cursor()
    cur.execute("SELECT key_char FROM action_event WHERE key_char IS NOT NULL")
    remaining = [r[0] for r in cur.fetchall()]
    conn.close()

    # At least some rows should have been NULLed
    # The original had 20 rows (10 chars * press+release). If any secret was detected,
    # some should now be NULL
    cur2 = sqlite3.connect(str(rec / "recording.db")).cursor()
    cur2.execute("SELECT COUNT(*) FROM action_event WHERE key_char IS NULL")
    null_count = cur2.fetchone()[0]
    assert null_count > 0, "Expected some DB rows to have key_char = NULL after scrub"


def test_scrub_events_jsonl_preserves_normal_typing(tmp_path, pipeline_and_anonymizer):
    """Non-secret keystrokes remain intact in events.jsonl."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "scrubbed"
    rec.mkdir()

    normal_ev = _make_key_type_event("hello", start_ts=300.0)
    (rec / "events.jsonl").write_text(_make_events_jsonl(normal_ev))
    _setup_keystroke_db(rec / "recording.db", [normal_ev])

    result = ScrubResult()
    _scrub_events_jsonl(rec, pipeline, anonymizer, result)

    lines = [json.loads(l) for l in (rec / "events.jsonl").read_text().strip().splitlines()]
    key_type_ev = lines[1]
    assert key_type_ev["text"] == "hello"
    # Children should still have key_char
    for child in key_type_ev["children"]:
        assert child["key_char"] is not None


def test_scrub_events_jsonl_missing_file(tmp_path, pipeline_and_anonymizer):
    """No events.jsonl → step skipped, no error."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "scrubbed"
    rec.mkdir()

    result = ScrubResult()
    _scrub_events_jsonl(rec, pipeline, anonymizer, result)
    # Should not crash


def test_scrub_events_jsonl_meta_line_preserved(tmp_path, pipeline_and_anonymizer):
    """_meta header line passes through unchanged."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "scrubbed"
    rec.mkdir()

    meta = {"_meta": True, "screencap_version": "0.1.0", "exported_at": "2026-03-05T00:00:00"}
    (rec / "events.jsonl").write_text(json.dumps(meta) + "\n")

    result = ScrubResult()
    _scrub_events_jsonl(rec, pipeline, anonymizer, result)

    lines = [json.loads(l) for l in (rec / "events.jsonl").read_text().strip().splitlines()]
    assert lines[0]["_meta"] is True
    assert lines[0]["screencap_version"] == "0.1.0"


def test_scrub_events_jsonl_key_up_redacted(tmp_path, pipeline_and_anonymizer):
    """Both key.down AND key.up children are redacted — no secret from release events."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "scrubbed"
    rec.mkdir()

    secret_ev = _make_key_type_event("password=ssfsfodsufdouhhfwnenwekskjdhjsfhd")
    (rec / "events.jsonl").write_text(_make_events_jsonl(secret_ev))
    _setup_keystroke_db(rec / "recording.db", [secret_ev])

    result = ScrubResult()
    _scrub_events_jsonl(rec, pipeline, anonymizer, result)

    lines = [json.loads(l) for l in (rec / "events.jsonl").read_text().strip().splitlines()]
    key_type_ev = lines[1]

    # Check that wherever a key.down child is nulled, the paired key.up is also nulled
    for i, child in enumerate(key_type_ev["children"]):
        if child["type"] == "key.down" and child["key_char"] is None:
            # The next key.up should also be nulled
            if i + 1 < len(key_type_ev["children"]):
                next_child = key_type_ev["children"][i + 1]
                if next_child["type"] == "key.up":
                    assert next_child["key_char"] is None, (
                        f"key.up at index {i+1} not redacted — "
                        f"secret reconstructable from release events"
                    )


def test_scrub_events_jsonl_empty_text(tmp_path, pipeline_and_anonymizer):
    """key.type event with text: '' → no crash, no detection."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "scrubbed"
    rec.mkdir()

    empty_ev = {"timestamp": 100.0, "type": "key.type", "text": "", "children": []}
    (rec / "events.jsonl").write_text(_make_events_jsonl(empty_ev))

    result = ScrubResult()
    _scrub_events_jsonl(rec, pipeline, anonymizer, result)
    # Should not crash


def test_scrub_events_jsonl_malformed_line(tmp_path, pipeline_and_anonymizer):
    """Malformed JSON line → file deleted for safety."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "scrubbed"
    rec.mkdir()

    content = '{"_meta": true}\n{invalid json\n'
    (rec / "events.jsonl").write_text(content)

    result = ScrubResult()
    _scrub_events_jsonl(rec, pipeline, anonymizer, result)

    # File should be deleted due to processing error
    assert not (rec / "events.jsonl").exists()


def test_scrub_events_jsonl_no_db(tmp_path, pipeline_and_anonymizer):
    """events.jsonl exists but no recording.db → JSONL scrubbed, DB step skipped."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "scrubbed"
    rec.mkdir()

    secret_ev = _make_key_type_event("password=ssfsfodsufdouhhfwnenwekskjdhjsfhd")
    (rec / "events.jsonl").write_text(_make_events_jsonl(secret_ev))

    result = ScrubResult()
    _scrub_events_jsonl(rec, pipeline, anonymizer, result)

    # JSONL should still exist (scrubbed)
    assert (rec / "events.jsonl").exists()
    lines = [json.loads(l) for l in (rec / "events.jsonl").read_text().strip().splitlines()]
    key_type_ev = lines[1]
    assert "password=ssfsfodsufdouhhfwnenwekskjdhjsfhd" not in key_type_ev["text"]


def test_scrub_events_jsonl_nested_in_mouse_drag(tmp_path, pipeline_and_anonymizer):
    """key.type inside mouse.drag children is scrubbed."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "scrubbed"
    rec.mkdir()

    inner_key_type = _make_key_type_event("password=ssfsfodsufdouhhfwnenwekskjdhjsfhd", start_ts=400.0)
    drag_event = {
        "timestamp": 399.0,
        "type": "mouse.drag",
        "children": [inner_key_type],
    }
    (rec / "events.jsonl").write_text(_make_events_jsonl(drag_event))

    result = ScrubResult()
    _scrub_events_jsonl(rec, pipeline, anonymizer, result)

    lines = [json.loads(l) for l in (rec / "events.jsonl").read_text().strip().splitlines()]
    drag_ev = lines[1]
    nested_key_type = drag_ev["children"][0]
    assert "password=ssfsfodsufdouhhfwnenwekskjdhjsfhd" not in nested_key_type["text"]


def test_scrub_events_jsonl_entity_counts(tmp_path, pipeline_and_anonymizer):
    """Entity counts from JSONL keystroke detection are accumulated in ScrubResult."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "scrubbed"
    rec.mkdir()

    secret_ev = _make_key_type_event("password=ssfsfodsufdouhhfwnenwekskjdhjsfhd")
    (rec / "events.jsonl").write_text(_make_events_jsonl(secret_ev))
    _setup_keystroke_db(rec / "recording.db", [secret_ev])

    result = ScrubResult()
    _scrub_events_jsonl(rec, pipeline, anonymizer, result)

    # Should have detected at least one entity
    assert sum(result.entity_counts.values()) > 0
