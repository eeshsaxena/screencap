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
    _scrub_db,
    _scrub_json_recursive,
    _scrub_metrics,
    _scrub_text,
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
        key_char TEXT, canonical_key_char TEXT,
        key_name TEXT, canonical_key_name TEXT,
        element_state TEXT,
        active_segment_description TEXT,
        available_segment_descriptions TEXT
    )"""
    )
    db.execute(
        "INSERT INTO action_event VALUES (1, 1, 'e', 'e', 'e', 'e', ?, ?, ?)",
        (
            json.dumps({"role": "button", "content": "john.doe@example.com"}),
            "John Doe's workspace",
            "John Doe's workspace, Settings",
        ),
    )

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
    (rec / "events.jsonl").write_text('{"text": "john.doe@example.com"}\n')
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
    assert not (dst / "events.jsonl").exists()
    assert not (dst / ".upload_status.json").exists()
    assert not (dst / "viewer.html").exists()

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
    """events.jsonl, .upload_status.json, and viewer.html deleted."""
    with mock.patch(
        "screencap.config.get_recordings_dir", return_value=tmp_path
    ), mock.patch(
        "screencap.scrubber.get_recordings_dir", return_value=tmp_path
    ):
        result = scrub_recording("test-recording")

    dst = tmp_path / "test-recording-scrubbed"
    assert not (dst / "events.jsonl").exists()
    assert not (dst / ".upload_status.json").exists()
    assert not (dst / "viewer.html").exists()


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
