"""Behavior tests for the Scrubber class — single-source-of-truth orchestration.

These tests target the public interface of ``screencap.scrubber.Scrubber``:
given a recording directory, calling ``run()`` (post-hoc) or ``run_chunk()``
(per-chunk) produces a privacy-scrubbed directory. No internal pipeline
functions are mocked — assertions look at directory state.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pipeline_and_anonymizer():
    """Real detection pipeline + anonymizer (shared across module for speed)."""
    from screencap.privacy import Anonymizer, create_default_pipeline

    return create_default_pipeline(), Anonymizer()


def _make_recording_with_db_pii(rec: Path, *, secret: str) -> None:
    """Create a minimal recording.db with one PII string in action_event."""
    rec.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(rec / "recording.db"))
    db.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, task_description TEXT)")
    db.execute("INSERT INTO recording VALUES (1, ?)", (f"Task: contact {secret}",))
    db.execute(
        """CREATE TABLE action_event (
            id INTEGER PRIMARY KEY,
            recording_id INTEGER,
            name TEXT,
            timestamp REAL,
            key_char TEXT,
            canonical_key_char TEXT,
            key_name TEXT,
            canonical_key_name TEXT,
            element_state TEXT,
            active_segment_description TEXT,
            available_segment_descriptions TEXT
        )"""
    )
    db.commit()
    db.close()


def _write_events_jsonl(rec: Path, *, key_type_text: str) -> None:
    """Write a minimal events.jsonl with a single key.type event."""
    meta = {"_meta": True, "screencap_version": "0.1.0", "exported_at": "2026-05-04T00:00:00"}
    event = {"timestamp": 100.0, "type": "key.type", "text": key_type_text, "children": []}
    (rec / "events.jsonl").write_text(json.dumps(meta) + "\n" + json.dumps(event) + "\n")


# ---------------------------------------------------------------------------
# Tracer: Scrubber.run() scrubs PII in recording.db
# ---------------------------------------------------------------------------


def test_run_scrubs_pii_in_recording_db(tmp_path, pipeline_and_anonymizer):
    """Scrubber.run() removes a known PII string from recording.task_description."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "rec"
    secret = "john.doe@example.com"
    _make_recording_with_db_pii(rec, secret=secret)

    from screencap.scrubber import Scrubber

    Scrubber(rec, pipeline=pipeline, anonymizer=anonymizer).run()

    db = sqlite3.connect(str(rec / "recording.db"))
    row = db.execute("SELECT task_description FROM recording").fetchone()
    db.close()
    assert secret not in row[0]


def test_run_scrubs_pii_in_events_jsonl(tmp_path, pipeline_and_anonymizer):
    """Scrubber.run() removes PII from a key.type event text in events.jsonl."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "rec"
    secret = "alice@contoso.example"
    _make_recording_with_db_pii(rec, secret="placeholder@example.com")
    _write_events_jsonl(rec, key_type_text=f"my email is {secret}")

    from screencap.scrubber import Scrubber

    Scrubber(rec, pipeline=pipeline, anonymizer=anonymizer).run()

    body = (rec / "events.jsonl").read_text()
    assert secret not in body


def test_run_scrubs_pii_in_transcripts(tmp_path, pipeline_and_anonymizer):
    """Scrubber.run() removes PII from transcript_*.txt and transcript_*.json."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "rec"
    secret_email = "alice@contoso.example"
    _make_recording_with_db_pii(rec, secret="placeholder@example.com")
    (rec / "transcript.txt").write_text(f"Contact us at {secret_email} for details.")
    transcript = {
        "text": f"Contact {secret_email}",
        "segments": [{"text": f"Contact {secret_email}", "start": 0.0, "end": 1.0}],
        "words": [
            {"word": "Contact", "start": 0.0, "end": 0.3},
            {"word": secret_email, "start": 0.3, "end": 1.0},
        ],
    }
    (rec / "transcript.json").write_text(json.dumps(transcript))

    from screencap.scrubber import Scrubber

    Scrubber(rec, pipeline=pipeline, anonymizer=anonymizer).run()

    assert secret_email not in (rec / "transcript.txt").read_text()
    assert secret_email not in (rec / "transcript.json").read_text()


def test_run_redacts_hostname_and_wifi_in_metrics(tmp_path, pipeline_and_anonymizer):
    """Scrubber.run() rule-redacts hostname + wifi.ssid/bssid in system_metrics.json."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "rec"
    _make_recording_with_db_pii(rec, secret="placeholder@example.com")
    metrics = {
        "static": {
            "hostname": "alices-laptop",
            "wifi": {"ssid": "HomeNet", "bssid": "AA:BB:CC:DD:EE:FF"},
        },
    }
    (rec / "system_metrics.json").write_text(json.dumps(metrics))

    from screencap.scrubber import Scrubber

    Scrubber(rec, pipeline=pipeline, anonymizer=anonymizer).run()

    scrubbed = json.loads((rec / "system_metrics.json").read_text())
    assert scrubbed["static"]["hostname"] == "<REDACTED>"
    assert scrubbed["static"]["wifi"]["ssid"] == "<REDACTED>"
    assert scrubbed["static"]["wifi"]["bssid"] == "<REDACTED>"


def _make_recording_with_blocked_interval(
    rec: Path, *, blocked_bundle_id: str, blocked_start: float, blocked_end: float,
) -> None:
    """Create recording.db with window switching to a blocked app for one interval.

    action_event rows: one before the interval (keeps content), one inside
    (must be nulled), one after (keeps content). Window events frame the
    interval with a benign editor app on either side.
    """
    rec.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(rec / "recording.db"))
    db.execute(
        "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, pixel_ratio REAL)"
    )
    db.execute("INSERT INTO recording VALUES (1, 0.0, 2.0)")
    db.execute(
        """CREATE TABLE action_event (
            id INTEGER PRIMARY KEY,
            recording_id INTEGER,
            name TEXT,
            timestamp REAL,
            key_char TEXT,
            canonical_key_char TEXT,
            key_name TEXT,
            canonical_key_name TEXT,
            element_state TEXT,
            active_segment_description TEXT,
            available_segment_descriptions TEXT
        )"""
    )
    for row_id, ts, ch in [
        (1, blocked_start - 5, "a"),
        (2, (blocked_start + blocked_end) / 2, "s"),
        (3, blocked_end + 5, "d"),
    ]:
        db.execute(
            "INSERT INTO action_event VALUES "
            "(?, 1, 'press', ?, ?, ?, ?, ?, NULL, NULL, NULL)",
            (row_id, ts, ch, ch, ch, ch),
        )
    db.execute(
        """CREATE TABLE window_event (
            id INTEGER PRIMARY KEY,
            recording_id INTEGER,
            timestamp REAL,
            app_bundle_id TEXT,
            window_id TEXT,
            title TEXT,
            state TEXT,
            browser_url TEXT,
            secure_input INTEGER
        )"""
    )
    db.execute(
        "INSERT INTO window_event VALUES "
        "(1, 1, ?, 'com.microsoft.VSCode', 'w-vscode', 'Editor', NULL, NULL, 0)",
        (blocked_start - 10,),
    )
    db.execute(
        "INSERT INTO window_event VALUES "
        "(2, 1, ?, ?, 'w-blocked', 'Vault', NULL, NULL, 0)",
        (blocked_start, blocked_bundle_id),
    )
    db.execute(
        "INSERT INTO window_event VALUES "
        "(3, 1, ?, 'com.microsoft.VSCode', 'w-vscode', 'Editor', NULL, NULL, 0)",
        (blocked_end,),
    )
    db.commit()
    db.close()


def test_run_nulls_action_rows_inside_blocked_interval(tmp_path, pipeline_and_anonymizer):
    """Scrubber.run() nulls action_event content inside an excluded-app interval."""
    from screencap.privacy.context import DefaultContextClassifier
    from screencap.privacy.policy import DefaultPolicyEvaluator, parse_privacy_config

    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "rec"
    blocked_start, blocked_end = 100.0, 200.0
    _make_recording_with_blocked_interval(
        rec,
        blocked_bundle_id="com.1password.1password",
        blocked_start=blocked_start,
        blocked_end=blocked_end,
    )

    # Internal mode keeps the surrounding editor windows ALLOW so we can
    # cleanly observe the EXCLUDE interval around the password manager.
    cfg = parse_privacy_config({"privacy": {"mode": "internal"}})
    evaluator = DefaultPolicyEvaluator(cfg)
    classifier = DefaultContextClassifier()

    from screencap.scrubber import Scrubber

    Scrubber(
        rec,
        pipeline=pipeline,
        anonymizer=anonymizer,
        evaluator=evaluator,
        classifier=classifier,
    ).run()

    db = sqlite3.connect(str(rec / "recording.db"))
    rows = {
        row[0]: row[1]
        for row in db.execute("SELECT id, key_char FROM action_event").fetchall()
    }
    db.close()
    assert rows[1] == "a", "row before interval should be preserved"
    assert rows[2] is None, "row inside blocked interval should be nulled"
    assert rows[3] == "d", "row after interval should be preserved"


def test_run_writes_audit_log_when_blocked_intervals_present(
    tmp_path, pipeline_and_anonymizer,
):
    """privacy_audit.json is written when blocked-interval audit entries exist."""
    from screencap.privacy.context import DefaultContextClassifier
    from screencap.privacy.policy import DefaultPolicyEvaluator, parse_privacy_config

    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "rec"
    _make_recording_with_blocked_interval(
        rec,
        blocked_bundle_id="com.1password.1password",
        blocked_start=100.0,
        blocked_end=200.0,
    )

    cfg = parse_privacy_config({"privacy": {"mode": "internal"}})
    evaluator = DefaultPolicyEvaluator(cfg)
    classifier = DefaultContextClassifier()

    from screencap.scrubber import Scrubber

    Scrubber(
        rec,
        pipeline=pipeline,
        anonymizer=anonymizer,
        evaluator=evaluator,
        classifier=classifier,
    ).run()

    audit_path = rec / "privacy_audit.json"
    assert audit_path.exists()
    entries = json.loads(audit_path.read_text())
    assert any(e.get("surface") == "db_field" for e in entries)


# ---------------------------------------------------------------------------
# run_chunk(): per-chunk scrubbing
# ---------------------------------------------------------------------------


def _write_chunk_events_jsonl(rec: Path, idx: int, *, key_type_text: str) -> Path:
    """Write a minimal events_NNNN.jsonl with one key.type event."""
    meta = {"_meta": True, "screencap_version": "0.1.0", "exported_at": "2026-05-04T00:00:00"}
    event = {"timestamp": 100.0, "type": "key.type", "text": key_type_text, "children": []}
    path = rec / f"events_{idx:04d}.jsonl"
    path.write_text(json.dumps(meta) + "\n" + json.dumps(event) + "\n")
    return path


def test_run_chunk_scrubs_events_file_for_index(tmp_path, pipeline_and_anonymizer):
    """Scrubber.run_chunk() scrubs only the events_NNNN.jsonl for the given index."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "rec"
    _make_recording_with_db_pii(rec, secret="placeholder@example.com")
    secret = "carol@contoso.example"
    _write_chunk_events_jsonl(rec, idx=2, key_type_text=f"reach me at {secret}")
    other_secret = "untouched@contoso.example"
    other = _write_chunk_events_jsonl(rec, idx=5, key_type_text=f"do not scrub {other_secret}")

    from screencap.scrubber import Scrubber

    Scrubber(rec, pipeline=pipeline, anonymizer=anonymizer).run_chunk(
        idx=2, start_ts=0.0, end_ts=1000.0, transcript_path=None,
    )

    assert secret not in (rec / "events_0002.jsonl").read_text()
    assert other_secret in other.read_text(), "other-index events file must be untouched"


def test_run_chunk_scrubs_transcripts_and_v1_manifest(tmp_path, pipeline_and_anonymizer):
    """run_chunk() scrubs the chunk's transcript files and v1 manifest task titles."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "rec"
    _make_recording_with_db_pii(rec, secret="placeholder@example.com")

    secret_t = "dave@contoso.example"
    transcript_txt = rec / "transcript_0003.txt"
    transcript_txt.write_text(f"Reach {secret_t} for sales.")
    transcript_json = rec / "transcript_0003.json"
    transcript_json.write_text(json.dumps({"text": f"Reach {secret_t}"}))

    secret_m = "erin@contoso.example"
    manifest = {
        "format_version": 1,
        "tasks": [
            {
                "dominant_title": f"Compose to {secret_m}",
                "dominant_app": "com.apple.mail",
                "start_ts": 0.0,
                "end_ts": 100.0,
            }
        ],
    }
    manifest_path = rec / "chunk_0003_manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    from screencap.scrubber import Scrubber

    Scrubber(rec, pipeline=pipeline, anonymizer=anonymizer).run_chunk(
        idx=3, start_ts=0.0, end_ts=1000.0, transcript_path=transcript_txt,
    )

    assert secret_t not in transcript_txt.read_text()
    assert secret_t not in transcript_json.read_text()
    assert secret_m not in manifest_path.read_text()


def test_run_chunk_deletes_excluded_app_screenshots(tmp_path, pipeline_and_anonymizer):
    """run_chunk() removes screenshots taken while an excluded app is frontmost."""
    from screencap.privacy.context import DefaultContextClassifier
    from screencap.privacy.policy import DefaultPolicyEvaluator, parse_privacy_config

    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "rec"
    blocked_start, blocked_end = 100.0, 200.0
    _make_recording_with_blocked_interval(
        rec,
        blocked_bundle_id="com.1password.1password",
        blocked_start=blocked_start,
        blocked_end=blocked_end,
    )

    # Place screenshots in chunk_4/screenshots/ — one inside the blocked
    # interval (must be deleted), one outside (must remain). The
    # associate_screenshot lookup uses a 5s window, so place each shot
    # within 5s of the window event it should associate with.
    shot_dir = rec / "chunk_4" / "screenshots"
    shot_dir.mkdir(parents=True)
    inside = shot_dir / f"{blocked_start + 1.0}.jpg"
    outside = shot_dir / f"{blocked_start - 9.0 + 1.0}.jpg"  # 1s after VSCode at t-10
    inside.write_bytes(b"\x00")
    outside.write_bytes(b"\x00")

    cfg = parse_privacy_config({"privacy": {"mode": "internal"}})
    evaluator = DefaultPolicyEvaluator(cfg)
    classifier = DefaultContextClassifier()

    from screencap.scrubber import Scrubber

    Scrubber(
        rec,
        pipeline=pipeline,
        anonymizer=anonymizer,
        evaluator=evaluator,
        classifier=classifier,
    ).run_chunk(idx=4, start_ts=0.0, end_ts=300.0, transcript_path=None)

    assert not inside.exists(), "screenshot inside excluded interval should be deleted"
    assert outside.exists(), "screenshot outside excluded interval should remain"


# ---------------------------------------------------------------------------
# scrub_recording() CLI entry-point: copy + delegate + summarize
# ---------------------------------------------------------------------------


def test_scrub_recording_copies_and_scrubs_pii(tmp_path):
    """``scrub_recording`` copies the source dir to ``<name>-scrubbed`` and scrubs PII."""
    from unittest import mock

    name = "test-recording"
    src = tmp_path / name
    secret = "frank@contoso.example"
    _make_recording_with_db_pii(src, secret=secret)

    from screencap.scrubber import scrub_recording

    with mock.patch(
        "screencap.config.get_recordings_dir", return_value=tmp_path,
    ), mock.patch(
        "screencap.scrubber.get_recordings_dir", return_value=tmp_path,
    ):
        result = scrub_recording(name)

    dst = tmp_path / f"{name}-scrubbed"
    assert dst.exists()
    assert src.exists(), "source recording must remain untouched"
    db = sqlite3.connect(str(dst / "recording.db"))
    row = db.execute("SELECT task_description FROM recording").fetchone()
    db.close()
    assert secret not in row[0]
    assert result.output_dir == dst


def test_scrub_recording_raises_when_recording_missing(tmp_path):
    """``scrub_recording`` raises FileNotFoundError when the recording does not exist."""
    from unittest import mock

    from screencap.scrubber import scrub_recording

    with mock.patch(
        "screencap.config.get_recordings_dir", return_value=tmp_path,
    ), mock.patch(
        "screencap.scrubber.get_recordings_dir", return_value=tmp_path,
    ), pytest.raises(FileNotFoundError):
        scrub_recording("does-not-exist")
