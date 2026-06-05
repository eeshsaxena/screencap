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
import sys
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
    BlockedInterval as _BlockedInterval,
    ScrubContext,
    ScrubResult,
    build_blocked_intervals as _build_blocked_intervals,
    build_secure_field_intervals as _build_secure_field_intervals,
    mask_screenshots,
)
from screencap.scrubber import (
    _null_db_rows_for_intervals,
    _scrub_events_jsonl,
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

    def _mask(self, dst, evaluator, classifier, window_events, result, **kwargs):
        ctx = ScrubContext(
            window_events=window_events,
            evaluator=evaluator,
            classifier=classifier,
            pixel_ratio=kwargs.get("pixel_ratio", 2.0),
        )
        mask_screenshots(dst / "screenshots", ctx, result=result, **{
            k: v for k, v in kwargs.items() if k != "pixel_ratio"
        })

    def test_allowed_app_screenshot_kept(self, tmp_path):
        """Screenshot during VSCode in internal mode → file kept."""
        dst = self._setup_screenshots(tmp_path, [15.0])
        evaluator = _make_evaluator(mode="internal")
        classifier = DefaultContextClassifier()
        window_events = _make_window_events([
            (10.0, "com.microsoft.VSCode"),
        ])
        result = ScrubResult()

        self._mask(dst, evaluator, classifier, window_events, result)

        assert (dst / "screenshots" / "15.0.jpg").exists()

    def test_ocr_fallback_without_vision_falls_to_mask_window(self, tmp_path):
        """OCR_FALLBACK with Vision unavailable fails closed to MASK_WINDOW:
        file is kept but pixels are replaced with a mask."""
        from unittest.mock import patch
        from PIL import Image

        from screencap.privacy.actions import ActionDecision

        dst = self._setup_screenshots(tmp_path, [15.0])
        # Write a real JPEG so masking succeeds
        img_path = dst / "screenshots" / "15.0.jpg"
        img = Image.new("RGB", (100, 80), (255, 255, 255))
        img.save(img_path, "JPEG")
        img.close()

        class _OcrFallbackEvaluator:
            def evaluate(self, context, metadata, mode=None):
                return ActionDecision(
                    action=PrivacyAction.OCR_FALLBACK,
                    reason="test_ocr_fallback",
                )

        classifier = DefaultContextClassifier()
        window_events = _make_window_events([
            (10.0, "com.microsoft.VSCode"),
        ])
        result = ScrubResult()

        # Patch VisionOcr to simulate missing Vision framework
        with patch(
            "screencap.privacy.ocr.VisionOcr",
            side_effect=ImportError("No module named 'Vision'"),
        ):
            self._mask(dst, _OcrFallbackEvaluator(), classifier, window_events, result)

        assert img_path.exists(), "OCR_FALLBACK should mask, not delete"
        assert len(result.audit_entries) == 1
        assert result.audit_entries[0].action == "mask_window"

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

        self._mask(dst, evaluator, classifier, window_events, result)

        assert len(result.audit_entries) == 1
        entry = result.audit_entries[0]
        assert entry.surface == "screenshot"
        assert entry.action == "exclude"
        assert entry.reason == "policy_excluded_app"
        # Export-safe: no raw text in any field
        assert "1password" not in entry.evidence_type


# ---------------------------------------------------------------------------
# OCR_FALLBACK integration
# ---------------------------------------------------------------------------


class TestOcrFallbackIntegration:
    """End-to-end tests for OCR_FALLBACK branch in mask_screenshots()."""

    def _setup_real_jpeg(self, tmp_path, text: str, ts: float = 15.0) -> Path:
        """Create a recording dir with a real JPEG containing rendered text."""
        from PIL import Image, ImageDraw, ImageFont

        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir()
        img_path = screenshots_dir / f"{ts}.jpg"
        img = Image.new("RGB", (800, 200), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 36)
        except OSError:
            font = ImageFont.load_default(size=36)
        draw.text((40, 60), text, fill=(0, 0, 0), font=font)
        img.save(img_path, "JPEG", quality=95)
        img.close()
        return tmp_path

    def _make_ocr_fallback_evaluator(self):
        """Return an evaluator that always yields OCR_FALLBACK."""
        from screencap.privacy.actions import ActionDecision

        class _OcrFallbackEvaluator:
            def evaluate(self, context, metadata, mode=None):
                return ActionDecision(
                    action=PrivacyAction.OCR_FALLBACK,
                    reason="test_ocr_fallback",
                )

        return _OcrFallbackEvaluator()

    @pytest.mark.skipif(
        sys.platform != "darwin", reason="Vision framework requires macOS"
    )
    def test_ocr_fallback_masks_pii(self, tmp_path):
        """OCR_FALLBACK with PII in screenshot → file masked, audit records OCR_FALLBACK."""
        from PIL import Image

        dst = self._setup_real_jpeg(tmp_path, "Email: alice@example.com")
        img_path = dst / "screenshots" / "15.0.jpg"

        # Read original pixel data for comparison
        with Image.open(img_path) as orig:
            orig_bytes = orig.tobytes()

        evaluator = self._make_ocr_fallback_evaluator()
        classifier = DefaultContextClassifier()
        window_events = _make_window_events([(10.0, "com.apple.Safari")])
        result = ScrubResult()

        ctx = ScrubContext(
            window_events=window_events,
            evaluator=evaluator,
            classifier=classifier,
        )
        mask_screenshots(dst / "screenshots", ctx, result=result)

        assert img_path.exists(), "OCR_FALLBACK should mask, not delete"
        # Pixels should differ (masking applied)
        with Image.open(img_path) as masked:
            assert masked.tobytes() != orig_bytes
        assert len(result.audit_entries) == 1
        assert result.audit_entries[0].action == "ocr_fallback"

    @pytest.mark.skipif(
        sys.platform != "darwin", reason="Vision framework requires macOS"
    )
    def test_ocr_fallback_clean_text_passes_through(self, tmp_path):
        """OCR_FALLBACK with no PII → file untouched, audit records ALLOW."""
        dst = self._setup_real_jpeg(tmp_path, "Hello World 2026")
        img_path = dst / "screenshots" / "15.0.jpg"

        evaluator = self._make_ocr_fallback_evaluator()
        classifier = DefaultContextClassifier()
        window_events = _make_window_events([(10.0, "com.apple.Safari")])
        result = ScrubResult()

        ctx = ScrubContext(
            window_events=window_events,
            evaluator=evaluator,
            classifier=classifier,
        )
        mask_screenshots(dst / "screenshots", ctx, result=result)

        assert img_path.exists()
        assert len(result.audit_entries) == 1
        assert result.audit_entries[0].action == "allow"

    def test_ocr_fallback_ocr_failure_falls_to_mask_window(self, tmp_path):
        """OCR error → MASK_WINDOW (fail-closed)."""
        from unittest.mock import patch
        from PIL import Image

        dst = self._setup_real_jpeg(tmp_path, "Some text here")
        img_path = dst / "screenshots" / "15.0.jpg"

        evaluator = self._make_ocr_fallback_evaluator()
        classifier = DefaultContextClassifier()
        window_events = _make_window_events([(10.0, "com.apple.Safari")])
        result = ScrubResult()

        ctx = ScrubContext(
            window_events=window_events,
            evaluator=evaluator,
            classifier=classifier,
        )
        # Patch ocr_mask_screenshot to simulate OCR failure
        with patch(
            "screencap.scrubber.ocr_mask_screenshot",
            side_effect=RuntimeError("OCR engine failed"),
        ):
            mask_screenshots(dst / "screenshots", ctx, result=result)

        assert img_path.exists(), "Should fall back to MASK_WINDOW, not delete"
        assert len(result.audit_entries) == 1
        assert result.audit_entries[0].action == "mask_window"

    def test_ocr_fallback_vision_unavailable(self, tmp_path):
        """Vision not installed → MASK_WINDOW (graceful degradation)."""
        from unittest.mock import patch

        dst = self._setup_real_jpeg(tmp_path, "Some text here")
        img_path = dst / "screenshots" / "15.0.jpg"

        evaluator = self._make_ocr_fallback_evaluator()
        classifier = DefaultContextClassifier()
        window_events = _make_window_events([(10.0, "com.apple.Safari")])
        result = ScrubResult()

        ctx = ScrubContext(
            window_events=window_events,
            evaluator=evaluator,
            classifier=classifier,
        )
        # Patch VisionOcr constructor to raise ImportError — _ocr will be None
        with patch(
            "screencap.privacy.ocr.VisionOcr",
            side_effect=ImportError("No module named 'Vision'"),
        ):
            mask_screenshots(dst / "screenshots", ctx, result=result)

        assert img_path.exists(), "Should fall back to MASK_WINDOW"
        assert len(result.audit_entries) == 1
        assert result.audit_entries[0].action == "mask_window"


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
        """Event at t=25 inside blocked interval [20,30) → all key fields nulled."""
        pipeline, anonymizer = pipeline_and_anonymizer
        event = {
            "timestamp": 25.0,
            "type": "key.type",
            "text": "hello",
            "children": [
                {
                    "type": "key.down",
                    "key_char": "h", "canonical_key_char": "h",
                    "key_name": "h", "canonical_key_name": "h",
                },
                {
                    "type": "key.up",
                    "key_char": "h", "canonical_key_char": "h",
                    "key_name": "h", "canonical_key_name": "h",
                },
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

        _scrub_events_jsonl(rec, pipeline, anonymizer, result, ctx=ScrubContext(blocked_intervals=intervals))

        lines = [
            json.loads(l)
            for l in (rec / "events.jsonl").read_text().strip().splitlines()
        ]
        blocked_ev = lines[1]
        assert blocked_ev["text"] is None
        child_down = blocked_ev["children"][0]
        child_up = blocked_ev["children"][1]
        assert child_down["key_char"] is None
        assert child_down["key_name"] is None
        assert child_down["canonical_key_name"] is None
        assert child_up["key_char"] is None
        assert child_up["key_name"] is None

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

        _scrub_events_jsonl(rec, pipeline, anonymizer, result, ctx=ScrubContext(blocked_intervals=intervals))

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
        key_name is set to the same value as key_char to mimic real data.
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
            key_vk TEXT, canonical_key_vk TEXT,
            element_state TEXT,
            active_segment_description TEXT,
            available_segment_descriptions TEXT
        )"""
        )
        for row_id, ts, key_char, element_state in rows:
            conn.execute(
                "INSERT INTO action_event VALUES (?, 1, 'press', ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                (row_id, ts, key_char, key_char, key_char, key_char, key_char, key_char, element_state),
            )
        conn.commit()
        conn.close()
        return rec

    def test_rows_in_blocked_interval_are_nulled(self, tmp_path):
        """action_event rows at t=25 inside [20,30) → all key fields and element_state NULL."""
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

        # Row inside interval → all sensitive fields nulled
        cur.execute(
            "SELECT key_char, key_name, canonical_key_name, key_vk, canonical_key_vk, element_state "
            "FROM action_event WHERE id = 2"
        )
        row = cur.fetchone()
        assert row[0] is None  # key_char
        assert row[1] is None  # key_name
        assert row[2] is None  # canonical_key_name
        assert row[3] is None  # key_vk
        assert row[4] is None  # canonical_key_vk
        assert row[5] is None  # element_state

        # Rows outside interval → preserved
        cur.execute(
            "SELECT key_char, key_name, element_state "
            "FROM action_event WHERE id = 1"
        )
        row = cur.fetchone()
        assert row[0] == "a"
        assert row[1] == "a"
        assert row[2] == '{"role": "textField"}'

        cur.execute("SELECT key_char, key_name FROM action_event WHERE id = 3")
        row = cur.fetchone()
        assert row[0] == "d"
        assert row[1] == "d"
        conn.close()

    def test_last_interval_with_infinity_end_nulls_all_remaining(self, tmp_path):
        """Last window event produces an interval with end=inf — all subsequent rows nulled."""
        rec = self._setup_action_event_db(
            tmp_path,
            [
                (1, 15.0, "a", None),  # before interval
                (2, 25.0, "s", None),  # inside interval
                (3, 99.0, "d", None),  # also inside (inf end)
            ],
        )
        intervals = [
            _BlockedInterval(
                start=20.0,
                end=float("inf"),
                action=PrivacyAction.EXCLUDE,
                reason="policy_excluded_app",
            )
        ]
        result = ScrubResult()

        _null_db_rows_for_intervals(rec, intervals, result)

        conn = sqlite3.connect(str(rec / "recording.db"))
        cur = conn.cursor()

        # Before interval → preserved
        cur.execute("SELECT key_char FROM action_event WHERE id = 1")
        assert cur.fetchone()[0] == "a"

        # Both rows inside → nulled
        cur.execute("SELECT key_char, key_name FROM action_event WHERE id = 2")
        row = cur.fetchone()
        assert row[0] is None
        assert row[1] is None

        cur.execute("SELECT key_char, key_name FROM action_event WHERE id = 3")
        row = cur.fetchone()
        assert row[0] is None
        assert row[1] is None
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
        entries = data["entries"]
        assert len(entries) == 2

        # Verify structure: only safe fields present
        for entry in entries:
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
# Shared mode rejection
# ---------------------------------------------------------------------------


class TestSharedModeRejected:
    def test_shared_mode_raises_config_error(self):
        """shared mode is not yet enforced — config must reject it."""
        from screencap.privacy.policy import InvalidPrivacyConfigError

        with pytest.raises(InvalidPrivacyConfigError, match="shared.*not yet enforced"):
            parse_privacy_config({"privacy": {"mode": "shared"}})


# ---------------------------------------------------------------------------
# Window event title nulling during blocked intervals
# ---------------------------------------------------------------------------


class TestWindowEventTitleNulling:
    def test_window_event_title_nulled_in_blocked_interval(self, tmp_path):
        """window_event.title must be nulled during blocked-app intervals."""
        rec = tmp_path / "scrubbed"
        rec.mkdir()
        db_path = rec / "recording.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE recording (id INTEGER PRIMARY KEY, task_description TEXT)"
        )
        conn.execute("INSERT INTO recording VALUES (1, 'test')")
        conn.execute(
            """CREATE TABLE window_event (
            id INTEGER PRIMARY KEY, recording_id INTEGER,
            timestamp REAL, title TEXT, state TEXT
        )"""
        )
        conn.execute(
            "INSERT INTO window_event VALUES (1, 1, 15.0, 'VSCode', NULL)"
        )
        conn.execute(
            "INSERT INTO window_event VALUES (2, 1, 25.0, 'Inbox - john@example.com', '{\"focused\": true}')"
        )
        conn.execute(
            "INSERT INTO window_event VALUES (3, 1, 35.0, 'Finder', NULL)"
        )
        conn.commit()
        conn.close()

        intervals = [
            _BlockedInterval(
                start=20.0,
                end=30.0,
                action=PrivacyAction.MASK_WINDOW,
                reason="context_email_surface",
            )
        ]
        result = ScrubResult()
        _null_db_rows_for_intervals(rec, intervals, result)

        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        # Inside interval → nulled
        cur.execute("SELECT title, state FROM window_event WHERE id = 2")
        row = cur.fetchone()
        assert row[0] is None
        assert row[1] is None
        # Outside interval → preserved
        cur.execute("SELECT title FROM window_event WHERE id = 1")
        assert cur.fetchone()[0] == "VSCode"
        conn.close()


# ---------------------------------------------------------------------------
# Nested event content nulling
# ---------------------------------------------------------------------------


class TestNestedEventNulling:
    def test_key_type_inside_mouse_drag_is_nulled(
        self, tmp_path, pipeline_and_anonymizer
    ):
        """key.type nested inside a blocked mouse.drag must not leak content.

        SCR-30 drag-aware handling (R6/R12): a child whose own timestamp
        lands inside the blocked interval is dropped entirely; a child
        outside the interval survives but has its keystroke content nulled
        via ``null_text_content``'s recursion, because the overlapping drag
        content-suppresses the whole gesture.
        """
        pipeline, anonymizer = pipeline_and_anonymizer
        # ts 25.0 is inside the [20, 30] EXCLUDE interval -> child dropped.
        inside_key_type = {
            "timestamp": 25.0,
            "type": "key.type",
            "text": "secret-password",
            "children": [
                {"type": "key.down", "key_char": "s", "canonical_key_char": "s"},
                {"type": "key.up", "key_char": "s", "canonical_key_char": "s"},
            ],
        }
        # ts 18.0 is outside the interval -> child survives, content nulled.
        outside_key_type = {
            "timestamp": 18.0,
            "type": "key.type",
            "text": "also-secret",
            "children": [
                {"type": "key.down", "key_char": "a", "canonical_key_char": "a"},
            ],
        }
        drag_event = {
            "timestamp": 18.0,
            "type": "mouse.drag",
            "children": [outside_key_type, inside_key_type],
        }
        rec = tmp_path / "scrubbed"
        rec.mkdir()
        lines = [
            json.dumps({"_meta": True, "screencap_version": "0.1.0"}),
            json.dumps(drag_event),
        ]
        (rec / "events.jsonl").write_text("\n".join(lines) + "\n")

        intervals = [
            _BlockedInterval(
                start=20.0,
                end=30.0,
                action=PrivacyAction.EXCLUDE,
                reason="policy_excluded_app",
            )
        ]
        result = ScrubResult()
        _scrub_events_jsonl(rec, pipeline, anonymizer, result, ctx=ScrubContext(blocked_intervals=intervals))

        output = [
            json.loads(l)
            for l in (rec / "events.jsonl").read_text().strip().splitlines()
        ]
        drag = output[1]
        # The in-interval child is dropped; only the outside child survives.
        assert len(drag["children"]) == 1
        survived = drag["children"][0]
        assert survived["timestamp"] == 18.0
        # Surviving nested key.type still has its content nulled (recursion).
        assert survived["text"] is None
        assert survived["children"][0]["key_char"] is None


# ---------------------------------------------------------------------------
# AXSecureTextField detection in post-processing
# ---------------------------------------------------------------------------


class TestSecureFieldIntervals:
    def _setup_db_with_element_state(self, tmp_path, rows):
        """Create recording.db with action_event rows containing element_state.

        rows: list of (id, timestamp, element_state_json_str) tuples.
        """
        db_path = tmp_path / "recording.db"
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
            key_vk TEXT, canonical_key_vk TEXT,
            element_state TEXT,
            active_segment_description TEXT,
            available_segment_descriptions TEXT
        )"""
        )
        for row_id, ts, es in rows:
            conn.execute(
                "INSERT INTO action_event VALUES (?, 1, 'click', ?, NULL, NULL, NULL, NULL, NULL, NULL, ?, NULL, NULL)",
                (row_id, ts, es),
            )
        conn.commit()
        conn.close()
        return db_path

    @pytest.mark.parametrize("ax_key", ["AXRole", "AXSubrole"])
    def test_secure_text_field_produces_interval(self, tmp_path, ax_key):
        """AXSecureTextField in AXRole or AXSubrole → blocked interval."""
        db_path = self._setup_db_with_element_state(tmp_path, [
            (1, 10.0, '{"AXRole": "AXTextField"}'),
            (2, 20.0, json.dumps({ax_key: "AXSecureTextField"})),
            (3, 30.0, '{"AXRole": "AXTextField"}'),
        ])

        intervals = _build_secure_field_intervals(db_path, hold_seconds=1.0)

        assert len(intervals) == 1
        assert intervals[0].start == 20.0
        assert intervals[0].end == 21.0
        assert intervals[0].reason == "secure_field_detected"

    def test_no_secure_fields_no_intervals(self, tmp_path):
        """Normal text fields produce no intervals."""
        db_path = self._setup_db_with_element_state(tmp_path, [
            (1, 10.0, '{"AXRole": "AXTextField"}'),
            (2, 20.0, '{"AXRole": "AXButton"}'),
        ])

        intervals = _build_secure_field_intervals(db_path, hold_seconds=1.0)

        assert intervals == []

    def test_adjacent_secure_fields_merged(self, tmp_path):
        """Overlapping secure field intervals are merged."""
        db_path = self._setup_db_with_element_state(tmp_path, [
            (1, 10.0, '{"AXRole": "AXSecureTextField"}'),
            (2, 10.5, '{"AXRole": "AXSecureTextField"}'),  # overlaps with first
        ])

        intervals = _build_secure_field_intervals(db_path, hold_seconds=1.0)

        # Should merge into a single interval [10.0, 11.5)
        assert len(intervals) == 1
        assert intervals[0].start == 10.0
        assert intervals[0].end == 11.5


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
