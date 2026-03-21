"""Integration tests for scrub_pipeline.py — shared scrubbing functions."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from screencap.privacy import Anonymizer, Detection, DetectionResult
from screencap.scrub_pipeline import (
    BlockedInterval,
    ElementStateDetection,
    ScrubContext,
    ScrubResult,
    build_scrub_context,
    scrub_events_jsonl,
    scrub_transcripts,
)
from screencap.privacy.actions import PrivacyAction
from screencap.privacy.reasons import ReasonCode

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline():
    """Create a mock pipeline/anonymizer that detects 'John Smith' as PERSON."""
    pipeline = MagicMock()

    def mock_detect(text):
        detections = []
        idx = text.find("John Smith")
        while idx != -1:
            detections.append(Detection(
                entity_type="PERSON",
                start=idx,
                end=idx + 10,
                score=0.95,
                source="test",
            ))
            idx = text.find("John Smith", idx + 10)
        return DetectionResult(text, detections)

    pipeline.detect = mock_detect
    anonymizer = Anonymizer()
    return pipeline, anonymizer


def _create_recording_db(
    db_path: Path,
    *,
    window_events=None,
    action_events=None,
):
    """Create a minimal recording.db with optional rows."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE action_event ("
        "id INTEGER PRIMARY KEY, timestamp REAL, name TEXT, "
        "key_char TEXT, key_name TEXT, key_vk TEXT, "
        "canonical_key_char TEXT, canonical_key_name TEXT, canonical_key_vk TEXT, "
        "text TEXT, element_state TEXT, "
        "active_segment_description TEXT, available_segment_descriptions TEXT)"
    )
    conn.execute(
        "CREATE TABLE window_event ("
        "id INTEGER PRIMARY KEY, timestamp REAL, title TEXT, "
        "app_bundle_id TEXT, window_id TEXT, browser_url TEXT)"
    )
    conn.execute(
        "CREATE TABLE recording ("
        "id INTEGER PRIMARY KEY, timestamp REAL, pixel_ratio REAL)"
    )
    conn.execute("INSERT INTO recording VALUES (1, 1000.0, 2.0)")

    if window_events:
        for we in window_events:
            conn.execute(
                "INSERT INTO window_event (timestamp, app_bundle_id, title, window_id) "
                "VALUES (?, ?, ?, ?)",
                (we["timestamp"], we["app_bundle_id"], we.get("title", ""), we.get("window_id", "1")),
            )

    if action_events:
        for ae in action_events:
            conn.execute(
                "INSERT INTO action_event (timestamp, name, key_char, element_state) "
                "VALUES (?, ?, ?, ?)",
                (ae["timestamp"], ae.get("name", "press"), ae.get("key_char"), ae.get("element_state")),
            )

    conn.commit()
    conn.close()


def _write_events_jsonl(path: Path, events: list[dict]) -> None:
    with open(path, "w") as f:
        f.write(json.dumps({"_meta": True, "format_version": 2}) + "\n")
        for evt in events:
            f.write(json.dumps(evt) + "\n")


# ---------------------------------------------------------------------------
# G1: Blocked-app interval keystrokes nulled in chunk events JSONL
# ---------------------------------------------------------------------------


class TestBlockedAppIntervalScrubbing:
    """G1: Events during EXCLUDE app intervals are nulled."""

    def test_keystrokes_during_blocked_interval_are_nulled(self, tmp_path):
        """Keystrokes within a blocked-app interval get content nulled."""
        pipeline, anonymizer = _make_pipeline()

        events = [
            {
                "type": "key.type",
                "timestamp": 1005.0,
                "text": "password123",
                "children": [
                    {"type": "key.down", "timestamp": 1005.0, "key_char": "p"},
                ],
            },
        ]
        events_path = tmp_path / "events_0000.jsonl"
        _write_events_jsonl(events_path, events)

        # Interval covers 1000-1010, event at 1005 should be blocked
        blocked = [
            BlockedInterval(
                start=1000.0, end=1010.0,
                action=PrivacyAction.EXCLUDE,
                reason=ReasonCode.POLICY_EXCLUDED_APP,
            ),
        ]
        ctx = ScrubContext(blocked_intervals=blocked)
        result = ScrubResult()

        scrub_events_jsonl(
            events_path, pipeline, anonymizer,
            ctx=ctx, result=result,
        )

        scrubbed = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
        key_evt = scrubbed[1]
        assert key_evt["text"] is None
        assert key_evt["children"][0]["key_char"] is None

        # Audit entry recorded
        assert any(e.surface == "event" for e in result.audit_entries)


# ---------------------------------------------------------------------------
# G2: AXSecureTextField keystrokes nulled in chunk events JSONL
# ---------------------------------------------------------------------------


class TestSecureFieldScrubbing:
    """G2: Keystrokes during secure-field intervals are nulled."""

    def test_secure_field_interval_from_db(self, tmp_path):
        """build_scrub_context produces blocked intervals from AXSecureTextField."""
        db_path = tmp_path / "recording.db"
        _create_recording_db(
            db_path,
            action_events=[
                {
                    "timestamp": 1005.0,
                    "name": "press",
                    "key_char": "x",
                    "element_state": json.dumps({"AXRole": "AXSecureTextField"}),
                },
            ],
        )

        ctx = build_scrub_context(db_path)
        assert len(ctx.blocked_intervals) > 0
        # The interval should cover timestamp 1005.0
        assert any(
            iv.start <= 1005.0 < iv.end
            for iv in ctx.blocked_intervals
        )
        assert ctx.blocked_intervals[0].reason == ReasonCode.SECURE_FIELD_DETECTED

    def test_secure_field_keystrokes_nulled_in_events(self, tmp_path):
        """Events during secure-field intervals get content nulled."""
        pipeline, anonymizer = _make_pipeline()
        db_path = tmp_path / "recording.db"
        _create_recording_db(
            db_path,
            action_events=[
                {
                    "timestamp": 1005.0,
                    "name": "press",
                    "key_char": "x",
                    "element_state": json.dumps({"AXRole": "AXSecureTextField"}),
                },
            ],
        )

        ctx = build_scrub_context(db_path, time_range=(1000.0, 2000.0))

        events = [
            {
                "type": "key.type",
                "timestamp": 1005.5,
                "text": "secretpassword",
                "children": [
                    {"type": "key.down", "timestamp": 1005.5, "key_char": "s"},
                ],
            },
        ]
        events_path = tmp_path / "events_0000.jsonl"
        _write_events_jsonl(events_path, events)

        result = ScrubResult()
        scrub_events_jsonl(
            events_path, pipeline, anonymizer,
            ctx=ctx, result=result,
        )

        scrubbed = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
        key_evt = scrubbed[1]
        assert key_evt["text"] is None


# ---------------------------------------------------------------------------
# G3: Element-state xref detections in chunk path
# ---------------------------------------------------------------------------


class TestXrefDetectionInChunks:
    """G3: PII from element_state cross-referenced against keystrokes."""

    def test_xref_from_db_detects_pii(self, tmp_path):
        """build_scrub_context with pipeline collects xref detections from DB."""
        pipeline, anonymizer = _make_pipeline()
        db_path = tmp_path / "recording.db"
        _create_recording_db(
            db_path,
            action_events=[
                {
                    "timestamp": 1005.0,
                    "name": "press",
                    "key_char": "J",
                    "element_state": json.dumps({"AXValue": "John Smith"}),
                },
            ],
        )

        ctx = build_scrub_context(
            db_path,
            time_range=(1000.0, 2000.0),
            pipeline=pipeline,
            anonymizer=anonymizer,
        )

        assert len(ctx.xref_detections) > 0
        assert any(d.entity_type == "PERSON" for d in ctx.xref_detections)

    def test_xref_applied_to_keystrokes(self, tmp_path):
        """Xref detections null matching keystrokes in events JSONL."""
        pipeline, anonymizer = _make_pipeline()

        xref = [
            ElementStateDetection(
                original_text="John Smith",
                entity_type="PERSON",
                timestamps=frozenset({1005.0}),
            ),
        ]
        ctx = ScrubContext(xref_detections=xref)

        events = [
            {
                "type": "key.type",
                "timestamp": 1005.0,
                "text": "John Smith",
                "children": [
                    {"type": "key.down", "timestamp": 1005.0, "key_char": "J"},
                    {"type": "key.up", "timestamp": 1005.01},
                    {"type": "key.down", "timestamp": 1005.1, "key_char": "o"},
                    {"type": "key.up", "timestamp": 1005.11},
                    {"type": "key.down", "timestamp": 1005.2, "key_char": "h"},
                    {"type": "key.up", "timestamp": 1005.21},
                    {"type": "key.down", "timestamp": 1005.3, "key_char": "n"},
                    {"type": "key.up", "timestamp": 1005.31},
                    {"type": "key.down", "timestamp": 1005.4, "key_char": " "},
                    {"type": "key.up", "timestamp": 1005.41},
                    {"type": "key.down", "timestamp": 1005.5, "key_char": "S"},
                    {"type": "key.up", "timestamp": 1005.51},
                    {"type": "key.down", "timestamp": 1005.6, "key_char": "m"},
                    {"type": "key.up", "timestamp": 1005.61},
                    {"type": "key.down", "timestamp": 1005.7, "key_char": "i"},
                    {"type": "key.up", "timestamp": 1005.71},
                    {"type": "key.down", "timestamp": 1005.8, "key_char": "t"},
                    {"type": "key.up", "timestamp": 1005.81},
                    {"type": "key.down", "timestamp": 1005.9, "key_char": "h"},
                    {"type": "key.up", "timestamp": 1005.91},
                ],
            },
        ]
        events_path = tmp_path / "events_0000.jsonl"
        _write_events_jsonl(events_path, events)

        result = ScrubResult()
        scrub_events_jsonl(
            events_path, pipeline, anonymizer,
            ctx=ctx, result=result,
        )

        scrubbed = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
        key_evt = scrubbed[1]
        # key_char fields for the matching children should be nulled
        down_children = [c for c in key_evt["children"] if c.get("type") == "key.down"]
        assert all(c["key_char"] is None for c in down_children)


# ---------------------------------------------------------------------------
# G5: words[*].word in transcript JSON scrubbed
# ---------------------------------------------------------------------------


class TestTranscriptWordsScrubbing:
    """G5: words[*].word entries are scrubbed in transcript JSON."""

    def test_words_array_scrubbed(self, tmp_path):
        """words[*].word containing PII must be scrubbed."""
        pipeline, anonymizer = _make_pipeline()

        data = {
            "text": "Call with John Smith",
            "segments": [
                {"start": 0, "end": 5, "text": "Call with John Smith"},
            ],
            "words": [
                {"word": "Call", "start": 0.0, "end": 0.5},
                {"word": "with", "start": 0.5, "end": 1.0},
                {"word": "John Smith", "start": 1.0, "end": 2.0},
            ],
        }
        path = tmp_path / "transcript_0000.json"
        path.write_text(json.dumps(data))

        scrub_transcripts([path], pipeline, anonymizer)

        result_data = json.loads(path.read_text())
        # Top-level text scrubbed
        assert "John Smith" not in result_data["text"]
        # Segments scrubbed
        assert "John Smith" not in result_data["segments"][0]["text"]
        # words[*].word scrubbed (G5)
        assert "John Smith" not in result_data["words"][2]["word"]
        assert "<PERSON>" in result_data["words"][2]["word"]

    def test_words_without_pii_unchanged(self, tmp_path):
        """words without PII should be left unchanged."""
        pipeline, anonymizer = _make_pipeline()

        data = {
            "text": "hello world",
            "words": [
                {"word": "hello", "start": 0.0, "end": 0.5},
                {"word": "world", "start": 0.5, "end": 1.0},
            ],
        }
        path = tmp_path / "transcript_0000.json"
        path.write_text(json.dumps(data))

        scrub_transcripts([path], pipeline, anonymizer)

        result_data = json.loads(path.read_text())
        assert result_data["words"][0]["word"] == "hello"
        assert result_data["words"][1]["word"] == "world"


# ---------------------------------------------------------------------------
# build_scrub_context with time_range scoping
# ---------------------------------------------------------------------------


class TestBuildScrubContext:
    """Unit tests for build_scrub_context()."""

    def test_empty_db(self, tmp_path):
        """Empty DB produces empty context without errors."""
        db_path = tmp_path / "recording.db"
        _create_recording_db(db_path)

        ctx = build_scrub_context(db_path)
        assert ctx.blocked_intervals == []
        assert ctx.xref_detections == []
        assert ctx.pixel_ratio == 2.0

    def test_none_db_path(self):
        """None db_path produces empty context."""
        ctx = build_scrub_context(None)
        assert ctx.blocked_intervals == []

    def test_time_range_scoping(self, tmp_path):
        """time_range limits DB queries to the specified range."""
        pipeline, anonymizer = _make_pipeline()
        db_path = tmp_path / "recording.db"
        _create_recording_db(
            db_path,
            action_events=[
                # In range
                {
                    "timestamp": 1005.0,
                    "name": "press",
                    "element_state": json.dumps({"AXRole": "AXSecureTextField"}),
                },
                # Out of range
                {
                    "timestamp": 2005.0,
                    "name": "press",
                    "element_state": json.dumps({"AXRole": "AXSecureTextField"}),
                },
            ],
        )

        ctx = build_scrub_context(
            db_path,
            time_range=(1000.0, 1500.0),
        )

        # Only the in-range event should produce an interval
        assert len(ctx.blocked_intervals) == 1
        assert ctx.blocked_intervals[0].start <= 1005.0

    def test_pixel_ratio_read_from_db(self, tmp_path):
        """pixel_ratio is read from recording table."""
        db_path = tmp_path / "recording.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE recording (id INTEGER PRIMARY KEY, pixel_ratio REAL)"
        )
        conn.execute("INSERT INTO recording VALUES (1, 3.0)")
        conn.execute(
            "CREATE TABLE action_event (id INTEGER PRIMARY KEY, timestamp REAL)"
        )
        conn.execute(
            "CREATE TABLE window_event (id INTEGER PRIMARY KEY, timestamp REAL)"
        )
        conn.commit()
        conn.close()

        ctx = build_scrub_context(db_path)
        assert ctx.pixel_ratio == 3.0

    def test_graceful_degradation_on_missing_tables(self, tmp_path):
        """Missing tables don't crash — returns empty context."""
        db_path = tmp_path / "recording.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO recording VALUES (1)")
        conn.commit()
        conn.close()

        ctx = build_scrub_context(db_path)
        assert ctx.blocked_intervals == []
