"""Tests for privacy v3 Phase 3: post-processing enforcement.

Tests that the scrubber integrates with the policy engine to:
- build blocked-app intervals from window events
- route screenshots by policy (delete vs keep)
- null event content during blocked-app periods
- null DB rows during blocked-app intervals
- emit export-safe audit entries
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from screencap.privacy.actions import PrivacyAction
from screencap.privacy.context import (
    DefaultContextClassifier,
    WindowContext,
)
from screencap.privacy.policy import (
    DefaultPolicyEvaluator,
    PrivacyConfig,
    PrivacyMode,
    parse_privacy_config,
)
from screencap.privacy.reasons import AuditEntry
from screencap.scrubber import (
    ScrubResult,
    _BlockedInterval,
    _build_blocked_intervals,
    _null_db_rows_for_intervals,
    _scrub_events_jsonl,
    _scrub_screenshots_with_policy,
    _write_audit_log,
)

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_evaluator(**kwargs) -> DefaultPolicyEvaluator:
    cfg = parse_privacy_config({"privacy": kwargs})
    return DefaultPolicyEvaluator(cfg)


def _make_window_events(specs: list[tuple[float, str]]) -> list[WindowContext]:
    """Build WindowContext list from (timestamp, bundle_id) pairs."""
    return [
        WindowContext(timestamp=ts, app_bundle_id=bid, title="")
        for ts, bid in specs
    ]


# ---------------------------------------------------------------------------
# Blocked-app interval building
# ---------------------------------------------------------------------------


class TestBuildBlockedIntervals:
    def test_excluded_app_produces_interval(self):
        """App in exclude_apps → blocked interval for its frontmost period."""
        evaluator = _make_evaluator(
            mode="public",
            exclude_apps=["com.1password.1password"],
        )
        classifier = DefaultContextClassifier()
        window_events = _make_window_events([
            (10.0, "com.microsoft.VSCode"),
            (20.0, "com.1password.1password"),
            (30.0, "com.microsoft.VSCode"),
        ])

        intervals = _build_blocked_intervals(
            window_events, evaluator, classifier
        )

        assert len(intervals) == 1
        assert intervals[0].start == 20.0
        assert intervals[0].end == 30.0
        assert intervals[0].action == PrivacyAction.EXCLUDE

    def test_allowed_app_produces_no_interval(self):
        """Code editor in internal mode → ALLOW, no blocked interval."""
        evaluator = _make_evaluator(mode="internal")
        classifier = DefaultContextClassifier()
        window_events = _make_window_events([
            (10.0, "com.microsoft.VSCode"),
        ])

        intervals = _build_blocked_intervals(
            window_events, evaluator, classifier
        )

        assert intervals == []

    def test_empty_window_events(self):
        evaluator = _make_evaluator(mode="public")
        classifier = DefaultContextClassifier()

        intervals = _build_blocked_intervals([], evaluator, classifier)

        assert intervals == []


# ---------------------------------------------------------------------------
# Screenshot routing
# ---------------------------------------------------------------------------


class TestScreenshotRouting:
    def _setup_screenshots(self, tmp_path, timestamps: list[float]) -> Path:
        """Create a recording dir with screenshot files at given timestamps."""
        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir()
        for ts in timestamps:
            (screenshots_dir / f"{ts}.jpg").write_bytes(b"\xff\xd8\xff")
        return tmp_path

    def test_excluded_app_screenshot_deleted(self, tmp_path):
        """Screenshot during 1Password frontmost period → file deleted."""
        dst = self._setup_screenshots(tmp_path, [22.0])
        evaluator = _make_evaluator(
            mode="public",
            exclude_apps=["com.1password.1password"],
        )
        classifier = DefaultContextClassifier()
        window_events = _make_window_events([
            (20.0, "com.1password.1password"),
            (30.0, "com.microsoft.VSCode"),
        ])
        result = ScrubResult()

        _scrub_screenshots_with_policy(
            dst, evaluator, classifier, window_events, [], result
        )

        assert not (dst / "screenshots" / "22.0.jpg").exists()

    def test_allowed_app_screenshot_kept(self, tmp_path):
        """Screenshot during VSCode in internal mode → file kept."""
        dst = self._setup_screenshots(tmp_path, [15.0])
        evaluator = _make_evaluator(mode="internal")
        classifier = DefaultContextClassifier()
        window_events = _make_window_events([
            (10.0, "com.microsoft.VSCode"),
        ])
        result = ScrubResult()

        _scrub_screenshots_with_policy(
            dst, evaluator, classifier, window_events, [], result
        )

        assert (dst / "screenshots" / "15.0.jpg").exists()

    def test_ocr_fallback_screenshot_kept(self, tmp_path):
        """Screenshot during code editor in public mode → OCR_FALLBACK → kept."""
        dst = self._setup_screenshots(tmp_path, [15.0])
        evaluator = _make_evaluator(mode="public")
        classifier = DefaultContextClassifier()
        window_events = _make_window_events([
            (10.0, "com.microsoft.VSCode"),
        ])
        result = ScrubResult()

        _scrub_screenshots_with_policy(
            dst, evaluator, classifier, window_events, [], result
        )

        assert (dst / "screenshots" / "15.0.jpg").exists()

    def test_emits_audit_entry_with_no_raw_text(self, tmp_path):
        """Every routed screenshot produces an audit entry without raw text."""
        dst = self._setup_screenshots(tmp_path, [22.0])
        evaluator = _make_evaluator(
            mode="public",
            exclude_apps=["com.1password.1password"],
        )
        classifier = DefaultContextClassifier()
        window_events = _make_window_events([
            (20.0, "com.1password.1password"),
        ])
        result = ScrubResult()

        _scrub_screenshots_with_policy(
            dst, evaluator, classifier, window_events, [], result
        )

        assert len(result.audit_entries) == 1
        entry = result.audit_entries[0]
        assert entry.surface == "screenshot"
        assert entry.action == "exclude"
        assert entry.reason == "policy_excluded_app"
        # Export-safe: no raw text in any field
        assert "1password" not in entry.evidence_type


# ---------------------------------------------------------------------------
# Events JSONL blocked-app handling
# ---------------------------------------------------------------------------


class TestEventsJsonlBlockedIntervals:
    def _make_events_jsonl(self, tmp_path, events: list[dict]) -> Path:
        rec = tmp_path / "scrubbed"
        rec.mkdir(exist_ok=True)
        lines = [json.dumps({"_meta": True, "screencap_version": "0.1.0"})]
        for ev in events:
            lines.append(json.dumps(ev))
        (rec / "events.jsonl").write_text("\n".join(lines) + "\n")
        return rec

    def test_event_content_nulled_during_blocked_interval(
        self, tmp_path, pipeline_and_anonymizer
    ):
        """Event at t=25 inside blocked interval [20,30) → text and key_char nulled."""
        pipeline, anonymizer = pipeline_and_anonymizer
        event = {
            "timestamp": 25.0,
            "type": "key.type",
            "text": "hello",
            "children": [
                {"type": "key.down", "key_char": "h", "canonical_key_char": "h"},
                {"type": "key.up", "key_char": "h", "canonical_key_char": "h"},
            ],
        }
        rec = self._make_events_jsonl(tmp_path, [event])
        intervals = [
            _BlockedInterval(
                start=20.0,
                end=30.0,
                action=PrivacyAction.EXCLUDE,
                reason="policy_excluded_app",
            )
        ]
        result = ScrubResult()

        _scrub_events_jsonl(rec, pipeline, anonymizer, result, intervals)

        lines = [
            json.loads(l)
            for l in (rec / "events.jsonl").read_text().strip().splitlines()
        ]
        blocked_ev = lines[1]
        assert blocked_ev["text"] is None
        assert blocked_ev["children"][0]["key_char"] is None
        assert blocked_ev["children"][1]["key_char"] is None

    def test_event_outside_interval_gets_normal_scrubbing(
        self, tmp_path, pipeline_and_anonymizer
    ):
        """Event at t=35 outside blocked interval [20,30) → text preserved."""
        pipeline, anonymizer = pipeline_and_anonymizer
        event = {
            "timestamp": 35.0,
            "type": "key.type",
            "text": "hello",
            "children": [],
        }
        rec = self._make_events_jsonl(tmp_path, [event])
        intervals = [
            _BlockedInterval(
                start=20.0,
                end=30.0,
                action=PrivacyAction.EXCLUDE,
                reason="policy_excluded_app",
            )
        ]
        result = ScrubResult()

        _scrub_events_jsonl(rec, pipeline, anonymizer, result, intervals)

        lines = [
            json.loads(l)
            for l in (rec / "events.jsonl").read_text().strip().splitlines()
        ]
        assert lines[1]["text"] == "hello"


# ---------------------------------------------------------------------------
# DB row nulling during blocked intervals
# ---------------------------------------------------------------------------


class TestDbRowNulling:
    def _setup_action_event_db(self, tmp_path, rows: list[tuple]) -> Path:
        """Create recording.db with action_event rows.

        rows: list of (id, timestamp, key_char, element_state) tuples.
        """
        rec = tmp_path / "scrubbed"
        rec.mkdir(exist_ok=True)
        db_path = rec / "recording.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE recording (id INTEGER PRIMARY KEY, task_description TEXT)"
        )
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
        for row_id, ts, key_char, element_state in rows:
            conn.execute(
                "INSERT INTO action_event VALUES (?, 1, 'press', ?, ?, ?, NULL, NULL, ?, NULL, NULL)",
                (row_id, ts, key_char, key_char, element_state),
            )
        conn.commit()
        conn.close()
        return rec

    def test_rows_in_blocked_interval_are_nulled(self, tmp_path):
        """action_event rows at t=25 inside [20,30) → key_char and element_state NULL."""
        rec = self._setup_action_event_db(
            tmp_path,
            [
                (1, 15.0, "a", '{"role": "textField"}'),  # before interval
                (2, 25.0, "s", '{"role": "button"}'),  # inside interval
                (3, 35.0, "d", '{"role": "list"}'),  # after interval
            ],
        )
        intervals = [
            _BlockedInterval(
                start=20.0,
                end=30.0,
                action=PrivacyAction.EXCLUDE,
                reason="policy_excluded_app",
            )
        ]
        result = ScrubResult()

        _null_db_rows_for_intervals(rec, intervals, result)

        conn = sqlite3.connect(str(rec / "recording.db"))
        cur = conn.cursor()

        # Row inside interval → nulled
        cur.execute("SELECT key_char, element_state FROM action_event WHERE id = 2")
        row = cur.fetchone()
        assert row[0] is None
        assert row[1] is None

        # Rows outside interval → preserved
        cur.execute("SELECT key_char, element_state FROM action_event WHERE id = 1")
        row = cur.fetchone()
        assert row[0] == "a"
        assert row[1] == '{"role": "textField"}'

        cur.execute("SELECT key_char, element_state FROM action_event WHERE id = 3")
        row = cur.fetchone()
        assert row[0] == "d"
        conn.close()


# ---------------------------------------------------------------------------
# Export-safe audit output
# ---------------------------------------------------------------------------


class TestAuditOutput:
    def test_audit_log_excludes_raw_text(self, tmp_path):
        """privacy_audit.json must not contain raw PII or sensitive content."""
        result = ScrubResult()
        result.audit_entries = [
            AuditEntry(
                timestamp=25.0,
                surface="screenshot",
                action="exclude",
                reason="policy_excluded_app",
                context_class="password_manager",
                evidence_type="bundle_id",
            ),
            AuditEntry(
                timestamp=26.0,
                surface="event",
                action="mask_window",
                reason="context_email_surface",
                context_class="email",
                evidence_type="domain",
            ),
        ]

        _write_audit_log(tmp_path, result)

        audit_path = tmp_path / "privacy_audit.json"
        assert audit_path.exists()
        data = json.loads(audit_path.read_text())
        assert len(data) == 2

        # Verify structure: only safe fields present
        for entry in data:
            assert set(entry.keys()) == {
                "timestamp",
                "surface",
                "action",
                "reason",
                "context_class",
                "evidence_type",
            }
            # No field contains anything that could be raw PII
            for value in entry.values():
                if isinstance(value, str):
                    assert "@" not in value
                    assert "password" not in value.lower() or value == "password_manager"


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
