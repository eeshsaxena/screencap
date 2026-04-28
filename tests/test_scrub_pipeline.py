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
    mask_screenshots,
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


# ---------------------------------------------------------------------------
# G4: Shared mask_screenshots integration
# ---------------------------------------------------------------------------


class TestMaskScreenshotsIntegration:
    """G4: mask_screenshots is the shared function used by both callers."""

    def _make_jpeg(self, path):
        from PIL import Image
        img = Image.new("RGB", (100, 80), (255, 255, 255))
        img.save(path, "JPEG")

    def test_exclude_app_deletes_screenshot(self, tmp_path):
        """EXCLUDE app screenshot is deleted by mask_screenshots."""
        from screencap.privacy.context import DefaultContextClassifier, WindowContext
        from screencap.privacy.policy import DefaultPolicyEvaluator, parse_privacy_config

        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir()
        self._make_jpeg(screenshots_dir / "5.0.jpg")

        cfg = parse_privacy_config({"privacy": {
            "mode": "public",
            "exclude_apps": ["com.1password.1password"],
        }})
        evaluator = DefaultPolicyEvaluator(cfg)
        classifier = DefaultContextClassifier()
        window_events = [
            WindowContext(timestamp=1.0, app_bundle_id="com.1password.1password", title="")
        ]

        ctx = ScrubContext(
            window_events=window_events,
            evaluator=evaluator,
            classifier=classifier,
        )
        result = ScrubResult()
        mask_screenshots(screenshots_dir, ctx, result=result)

        assert not (screenshots_dir / "5.0.jpg").exists()
        assert len(result.audit_entries) == 1
        assert result.audit_entries[0].action == "exclude"

    def test_mask_window_app_keeps_masked_file(self, tmp_path):
        """MASK_WINDOW app screenshot is kept but content is masked."""
        from PIL import Image

        from screencap.privacy.context import DefaultContextClassifier, WindowContext
        from screencap.privacy.policy import DefaultPolicyEvaluator, parse_privacy_config

        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir()
        img_path = screenshots_dir / "5.0.jpg"
        self._make_jpeg(img_path)
        original = Image.open(img_path).convert("L")
        original_brightness = sum(original.getdata()) / len(list(original.getdata()))
        original.close()

        cfg = parse_privacy_config({"privacy": {"mode": "public"}})
        evaluator = DefaultPolicyEvaluator(cfg)
        classifier = DefaultContextClassifier()
        window_events = [
            WindowContext(timestamp=1.0, app_bundle_id="com.tinyspeck.slackmacgap", title="Slack")
        ]

        ctx = ScrubContext(
            window_events=window_events,
            evaluator=evaluator,
            classifier=classifier,
        )
        result = ScrubResult()
        mask_screenshots(screenshots_dir, ctx, result=result)

        assert img_path.exists(), "MASK_WINDOW should keep the file"
        masked = Image.open(img_path).convert("L")
        masked_brightness = sum(masked.getdata()) / len(list(masked.getdata()))
        masked.close()
        assert masked_brightness < original_brightness * 0.3

    def test_no_evaluator_is_noop(self, tmp_path):
        """mask_screenshots with no evaluator does nothing."""
        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir()
        self._make_jpeg(screenshots_dir / "5.0.jpg")

        ctx = ScrubContext()  # no evaluator/classifier
        result = ScrubResult()
        mask_screenshots(screenshots_dir, ctx, result=result)

        assert (screenshots_dir / "5.0.jpg").exists()
        assert len(result.audit_entries) == 0


# ---------------------------------------------------------------------------
# G5: Phase 2 OCR pass on TEXT_REDACT/ALLOW screenshots
# ---------------------------------------------------------------------------


class TestOcrPassTextRedactAllow:
    """Phase 2: OCR-based PII masking for TEXT_REDACT and ALLOW screenshots."""

    def _setup_real_jpeg(self, tmp_path, text: str, ts: float = 15.0) -> Path:
        from PIL import Image, ImageDraw, ImageFont

        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir(exist_ok=True)
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

    def _make_evaluator(self, action: PrivacyAction):
        from screencap.privacy.actions import ActionDecision

        class _FixedEvaluator:
            def evaluate(self, context, metadata, mode=None):
                return ActionDecision(action=action, reason="test_ocr_phase2")

        return _FixedEvaluator()

    def _mask(self, dst, evaluator, window_events, result):
        from screencap.privacy.context import DefaultContextClassifier

        ctx = ScrubContext(
            window_events=window_events,
            evaluator=evaluator,
            classifier=DefaultContextClassifier(),
        )
        mask_screenshots(dst / "screenshots", ctx, result=result)

    def _window_events(self, ts=10.0, bundle="com.microsoft.VSCode"):
        from screencap.privacy.context import WindowContext

        return [WindowContext(timestamp=ts, app_bundle_id=bundle, title="")]

    @pytest.mark.skipif(
        __import__("sys").platform != "darwin", reason="Vision framework requires macOS"
    )
    def test_text_redact_ocr_masks_pii(self, tmp_path):
        """TEXT_REDACT screenshot with PII → OCR detects and masks regions."""
        from PIL import Image

        dst = self._setup_real_jpeg(tmp_path, "Email: alice@example.com")
        img_path = dst / "screenshots" / "15.0.jpg"

        with Image.open(img_path) as orig:
            orig_bytes = orig.tobytes()

        result = ScrubResult()
        self._mask(
            dst,
            self._make_evaluator(PrivacyAction.TEXT_REDACT),
            self._window_events(),
            result,
        )

        assert img_path.exists()
        with Image.open(img_path) as masked:
            assert masked.tobytes() != orig_bytes, "PII regions should be masked"
        assert len(result.audit_entries) == 1
        assert "ocr_masked" in result.audit_entries[0].reason

    @pytest.mark.skipif(
        __import__("sys").platform != "darwin", reason="Vision framework requires macOS"
    )
    def test_allow_ocr_masks_pii(self, tmp_path):
        """ALLOW screenshot with PII → OCR detects and masks regions."""
        from PIL import Image

        dst = self._setup_real_jpeg(tmp_path, "Email: bob@example.com")
        img_path = dst / "screenshots" / "15.0.jpg"

        with Image.open(img_path) as orig:
            orig_bytes = orig.tobytes()

        result = ScrubResult()
        self._mask(
            dst,
            self._make_evaluator(PrivacyAction.ALLOW),
            self._window_events(),
            result,
        )

        assert img_path.exists()
        with Image.open(img_path) as masked:
            assert masked.tobytes() != orig_bytes, "PII regions should be masked"
        assert len(result.audit_entries) == 1
        assert "ocr_masked" in result.audit_entries[0].reason

    def test_dhash_cache_reuses_regions(self, tmp_path):
        """Two identical JPEGs → OCR called once, second reuses cache."""
        from unittest.mock import patch, MagicMock
        from screencap.privacy.masking import MaskRegion

        # Create two identical screenshots
        self._setup_real_jpeg(tmp_path, "some text", ts=15.0)
        self._setup_real_jpeg(tmp_path, "some text", ts=16.0)

        fake_regions = [MaskRegion(x=10, y=10, width=50, height=20, label="TEST")]

        call_count = 0
        original_ocr_mask = None

        def counting_ocr(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return fake_regions

        result = ScrubResult()
        evaluator = self._make_evaluator(PrivacyAction.TEXT_REDACT)

        with patch(
            "screencap.scrub_pipeline.ocr_mask_screenshot",
            side_effect=counting_ocr,
        ):
            self._mask(tmp_path, evaluator, self._window_events(), result)

        assert call_count == 1, f"OCR should be called once (first), got {call_count}"
        assert len(result.audit_entries) == 2
        # Second screenshot should be a cache hit
        assert "ocr_cache_hit" in result.audit_entries[1].reason

    def test_dhash_cache_invalidated_on_bounds_change(self, tmp_path):
        """Two identical JPEGs but different window geometry → OCR called twice."""
        from unittest.mock import patch
        from screencap.privacy.masking import MaskRegion

        self._setup_real_jpeg(tmp_path, "some text", ts=15.0)
        self._setup_real_jpeg(tmp_path, "some text", ts=16.0)

        call_count = 0

        def counting_ocr(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return []

        # Return different bounds for each call
        bounds_sequence = iter([
            (0, 0, 400, 200),   # first screenshot
            (100, 0, 400, 200), # second — different position
        ])

        def varying_bounds(*args, **kwargs):
            return next(bounds_sequence, None)

        result = ScrubResult()
        evaluator = self._make_evaluator(PrivacyAction.TEXT_REDACT)

        with patch(
            "screencap.scrub_pipeline.ocr_mask_screenshot",
            side_effect=counting_ocr,
        ), patch(
            "screencap.scrub_pipeline._active_window_bounds",
            side_effect=varying_bounds,
        ):
            self._mask(tmp_path, evaluator, self._window_events(), result)

        assert call_count == 2, f"OCR should be called for both (bounds differ), got {call_count}"

    def test_ocr_failure_text_redact_falls_to_mask_window(self, tmp_path):
        """OCR error on TEXT_REDACT → file exists with MASK_WINDOW audit."""
        from unittest.mock import patch

        dst = self._setup_real_jpeg(tmp_path, "Some text here")
        img_path = dst / "screenshots" / "15.0.jpg"

        result = ScrubResult()
        evaluator = self._make_evaluator(PrivacyAction.TEXT_REDACT)

        with patch(
            "screencap.scrub_pipeline.ocr_mask_screenshot",
            side_effect=RuntimeError("OCR engine failed"),
        ):
            self._mask(dst, evaluator, self._window_events(), result)

        assert img_path.exists(), "Should MASK_WINDOW, not delete"
        assert len(result.audit_entries) == 1
        assert result.audit_entries[0].action == "mask_window"
        assert "ocr_failed" in result.audit_entries[0].reason

    def test_no_geometry_ocr_runs_full_image(self, tmp_path):
        """No window_geometry table → OCR runs with roi=None (full image)."""
        from unittest.mock import patch
        from screencap.privacy.masking import MaskRegion

        dst = self._setup_real_jpeg(tmp_path, "Email: test@test.com")

        captured_roi = []

        def capturing_ocr(img_path, pipeline, ocr, roi=None):
            captured_roi.append(roi)
            return [MaskRegion(x=10, y=10, width=50, height=20, label="EMAIL")]

        result = ScrubResult()
        evaluator = self._make_evaluator(PrivacyAction.TEXT_REDACT)

        # No db_path → no geometry → no active window bounds → roi=None
        with patch(
            "screencap.scrub_pipeline.ocr_mask_screenshot",
            side_effect=capturing_ocr,
        ):
            self._mask(dst, evaluator, self._window_events(), result)

        assert len(captured_roi) == 1
        assert captured_roi[0] is None, "ROI should be None when no geometry"

    @pytest.mark.skipif(
        __import__("sys").platform != "darwin", reason="Vision framework requires macOS"
    )
    def test_allow_clean_text_no_masking(self, tmp_path):
        """ALLOW screenshot with no PII → file untouched, audit shows ocr_clean."""
        from PIL import Image

        dst = self._setup_real_jpeg(tmp_path, "Hello World 2026")
        img_path = dst / "screenshots" / "15.0.jpg"

        with Image.open(img_path) as orig:
            orig_bytes = orig.tobytes()

        result = ScrubResult()
        self._mask(
            dst,
            self._make_evaluator(PrivacyAction.ALLOW),
            self._window_events(),
            result,
        )

        assert img_path.exists()
        with Image.open(img_path) as after:
            assert after.tobytes() == orig_bytes, "No PII → no masking"
        assert len(result.audit_entries) == 1
        assert "ocr_clean" in result.audit_entries[0].reason


# ---------------------------------------------------------------------------
# G6: Unit tests for Phase 2 helper functions
# ---------------------------------------------------------------------------


class TestActiveWindowBounds:
    """Unit tests for _active_window_bounds()."""

    def test_finds_window_by_bundle_id(self):
        from screencap.scrub_pipeline import _active_window_bounds
        from screencap.privacy.context import WindowGeometrySnapshot

        geom = WindowGeometrySnapshot(
            windows=[
                {"bundle_id": "com.apple.finder", "x": 0, "y": 0, "width": 100, "height": 100},
                {"bundle_id": "com.microsoft.VSCode", "x": 50, "y": 25, "width": 200, "height": 150},
            ],
            display_origin=(0.0, 0.0),
        )
        bounds = _active_window_bounds(geom, "com.microsoft.VSCode", 2.0, 800, 600)
        # pixel coords: x=50*2=100, y=25*2=50, w=200*2=400, h=150*2=300
        assert bounds == (100, 50, 400, 300)

    def test_falls_back_to_first_window(self):
        from screencap.scrub_pipeline import _active_window_bounds
        from screencap.privacy.context import WindowGeometrySnapshot

        geom = WindowGeometrySnapshot(
            windows=[
                {"bundle_id": "com.apple.finder", "x": 10, "y": 20, "width": 100, "height": 80},
            ],
            display_origin=(0.0, 0.0),
        )
        bounds = _active_window_bounds(geom, "com.nonexistent.app", 1.0, 200, 200)
        assert bounds == (10, 20, 100, 80)

    def test_returns_none_when_no_geometry(self):
        from screencap.scrub_pipeline import _active_window_bounds

        assert _active_window_bounds(None, "any", 2.0, 800, 600) is None

    def test_returns_none_when_empty_windows(self):
        from screencap.scrub_pipeline import _active_window_bounds
        from screencap.privacy.context import WindowGeometrySnapshot

        geom = WindowGeometrySnapshot(windows=[], display_origin=(0.0, 0.0))
        assert _active_window_bounds(geom, "any", 2.0, 800, 600) is None


class TestActiveWindowRoi:
    """Unit tests for _active_window_roi()."""

    def test_converts_pixel_bounds_to_vision_coords(self):
        from screencap.scrub_pipeline import _active_window_roi

        # Window at top-left quarter: (0, 0, 400, 300) in 800x600 image
        roi = _active_window_roi((0, 0, 400, 300), 800, 600)
        x, y, w, h = roi
        assert w == pytest.approx(0.5)   # 400/800
        assert h == pytest.approx(0.5)   # 300/600
        assert x == pytest.approx(0.0)
        # Y-flipped: top-left in PIL → bottom-left in Vision
        # roi_y = 1.0 - 0.0 - 0.5 = 0.5
        assert y == pytest.approx(0.5)

    def test_full_image_roi(self):
        from screencap.scrub_pipeline import _active_window_roi

        roi = _active_window_roi((0, 0, 800, 600), 800, 600)
        assert roi == pytest.approx((0.0, 0.0, 1.0, 1.0))

    def test_bottom_right_window(self):
        from screencap.scrub_pipeline import _active_window_roi

        # Window at bottom-right: (400, 300, 400, 300) in 800x600
        roi = _active_window_roi((400, 300, 400, 300), 800, 600)
        x, y, w, h = roi
        assert x == pytest.approx(0.5)   # 400/800
        assert w == pytest.approx(0.5)   # 400/800
        assert h == pytest.approx(0.5)   # 300/600
        # Y-flipped: y1=300/600=0.5, roi_y=1.0-0.5-0.5=0.0
        assert y == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Unit 2 (R6/R12): Pointer-coordinate suppression in SCRUB_BLOCK_ACTIONS
# intervals — drop in-interval mouse.move events from cloud-bound JSONL
# ---------------------------------------------------------------------------


def _make_move(ts: float, x: float = 100.0, y: float = 200.0) -> dict:
    return {"type": "mouse.move", "timestamp": ts, "x": x, "y": y, "path": []}


def _read_events(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _scrub_with_intervals(
    tmp_path: Path,
    events: list[dict],
    intervals: list[BlockedInterval],
    *,
    xref: list[ElementStateDetection] | None = None,
) -> tuple[list[dict], ScrubResult]:
    """Helper: write events JSONL, run scrub_events_jsonl, return scrubbed events."""
    pipeline, anonymizer = _make_pipeline()
    events_path = tmp_path / "events_0000.jsonl"
    _write_events_jsonl(events_path, events)
    ctx = ScrubContext(
        blocked_intervals=intervals,
        xref_detections=xref or [],
    )
    result = ScrubResult()
    scrub_events_jsonl(
        events_path, pipeline, anonymizer,
        ctx=ctx, result=result,
    )
    return _read_events(events_path), result


class TestMouseMoveSuppression:
    """R6/R12: drop in-interval mouse.move events at scrub time."""

    def test_mask_window_drops_in_interval_mouse_moves(self, tmp_path):
        """Moves before/during/after a MASK_WINDOW interval — only in-interval dropped."""
        events = [
            _make_move(900.0),   # before
            _make_move(1005.0),  # in interval
            _make_move(1007.0),  # in interval
            _make_move(1100.0),  # after
        ]
        intervals = [
            BlockedInterval(
                start=1000.0, end=1010.0,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        # Filter out the meta header (line 0)
        moves = [e for e in scrubbed if e.get("type") == "mouse.move"]
        timestamps = sorted(e["timestamp"] for e in moves)
        assert timestamps == [900.0, 1100.0]

    def test_exclude_drops_moves_and_nulls_keystrokes(self, tmp_path):
        """EXCLUDE interval — moves dropped, keystroke content nulled (existing)."""
        events = [
            _make_move(1005.0),   # in interval — dropped
            {
                "type": "key.type",
                "timestamp": 1006.0,
                "text": "secret",
                "children": [
                    {"type": "key.down", "timestamp": 1006.0, "key_char": "s"},
                ],
            },
            _make_move(1100.0),   # after — kept
        ]
        intervals = [
            BlockedInterval(
                start=1000.0, end=1010.0,
                action=PrivacyAction.EXCLUDE,
                reason=ReasonCode.POLICY_EXCLUDED_APP,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        moves = [e for e in scrubbed if e.get("type") == "mouse.move"]
        assert [m["timestamp"] for m in moves] == [1100.0]

        keys = [e for e in scrubbed if e.get("type") == "key.type"]
        assert len(keys) == 1
        assert keys[0]["text"] is None
        assert keys[0]["children"][0]["key_char"] is None

    def test_text_redact_drops_in_interval_moves(self, tmp_path):
        """TEXT_REDACT interval — moves dropped (validates expanded set)."""
        events = [
            _make_move(900.0),
            _make_move(1005.0),
            _make_move(1100.0),
        ]
        intervals = [
            BlockedInterval(
                start=1000.0, end=1010.0,
                action=PrivacyAction.TEXT_REDACT,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        moves = [e for e in scrubbed if e.get("type") == "mouse.move"]
        assert sorted(m["timestamp"] for m in moves) == [900.0, 1100.0]

    def test_ocr_fallback_drops_in_interval_moves(self, tmp_path):
        """OCR_FALLBACK interval — moves dropped."""
        events = [
            _make_move(1005.0),
            _make_move(2005.0),
        ]
        intervals = [
            BlockedInterval(
                start=1000.0, end=1500.0,
                action=PrivacyAction.OCR_FALLBACK,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        moves = [e for e in scrubbed if e.get("type") == "mouse.move"]
        assert [m["timestamp"] for m in moves] == [2005.0]

    def test_drag_in_interval_keeps_drag_drops_all_in_interval_children(
        self, tmp_path,
    ):
        """mouse.drag with all children inside a blocked interval — drag
        retained as audit shell with coordinates nulled, but EVERY child
        whose timestamp lands inside the interval is dropped (mouse.move
        AND mouse.down AND mouse.up all carry coordinates inside the
        blocked interval)."""
        events = [
            {
                "type": "mouse.drag",
                "timestamp": 1005.0,
                "x": 100.0,
                "y": 200.0,
                "dx": 50.0,
                "dy": 30.0,
                "button": "left",
                "children": [
                    {
                        "type": "mouse.down",
                        "timestamp": 1005.0,
                        "x": 100.0, "y": 200.0,
                        "button": "left",
                    },
                    _make_move(1005.5, 110.0, 210.0),  # in-interval child
                    _make_move(1006.0, 130.0, 220.0),  # in-interval child
                    {
                        "type": "mouse.up",
                        "timestamp": 1007.0,
                        "x": 150.0, "y": 230.0,
                        "button": "left",
                    },
                ],
            },
        ]
        intervals = [
            BlockedInterval(
                start=1000.0, end=1010.0,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        drags = [e for e in scrubbed if e.get("type") == "mouse.drag"]
        assert len(drags) == 1, "mouse.drag itself must be retained as audit shell"
        # Every child timestamp (1005.0, 1005.5, 1006.0, 1007.0) lands
        # inside [1000.0, 1010.0) — all dropped regardless of type.
        # Pre-fix the mouse.down/mouse.up survived and leaked the
        # start/end coordinates of the drag.
        assert drags[0]["children"] == [], (
            "all children with timestamps inside the blocked interval must "
            "be dropped (mouse.up/mouse.down leak coordinates as much as "
            "mouse.move waypoints do)"
        )
        # Parent drag coordinates also nulled (pre-existing behavior).
        assert drags[0]["x"] is None
        assert drags[0]["y"] is None
        assert drags[0]["dx"] is None
        assert drags[0]["dy"] is None

    def test_drag_outside_interval_keeps_all_children(self, tmp_path):
        """drag with mouse.move children outside any blocked interval — all kept."""
        events = [
            {
                "type": "mouse.drag",
                "timestamp": 2000.0,
                "x": 100.0, "y": 200.0,
                "dx": 50.0, "dy": 30.0,
                "button": "left",
                "children": [
                    _make_move(2000.5, 110.0, 210.0),
                    _make_move(2001.0, 130.0, 220.0),
                ],
            },
        ]
        intervals = [
            BlockedInterval(
                start=1000.0, end=1010.0,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        drags = [e for e in scrubbed if e.get("type") == "mouse.drag"]
        assert len(drags) == 1
        child_types = [c.get("type") for c in drags[0]["children"]]
        assert child_types.count("mouse.move") == 2, "moves outside intervals retained"

    def test_empty_intervals_no_drops(self, tmp_path):
        """Empty intervals list — no events dropped (regression check)."""
        events = [
            _make_move(900.0),
            _make_move(1005.0),
            _make_move(1100.0),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, [])

        moves = [e for e in scrubbed if e.get("type") == "mouse.move"]
        assert sorted(m["timestamp"] for m in moves) == [900.0, 1005.0, 1100.0]

    def test_all_events_outside_intervals_unchanged(self, tmp_path):
        """All events outside blocked intervals — output identical (no side effect)."""
        events = [
            _make_move(900.0),
            _make_move(950.0),
            {
                "type": "mouse.singleclick",
                "timestamp": 970.0,
                "x": 100.0, "y": 200.0,
                "button": "left",
                "children": [],
            },
        ]
        intervals = [
            BlockedInterval(
                start=2000.0, end=3000.0,
                action=PrivacyAction.EXCLUDE,
                reason=ReasonCode.POLICY_EXCLUDED_APP,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        # All 3 events written + meta header
        non_meta = [e for e in scrubbed if not e.get("_meta")]
        assert len(non_meta) == 3
        moves = [e for e in non_meta if e.get("type") == "mouse.move"]
        assert sorted(m["timestamp"] for m in moves) == [900.0, 950.0]

    def test_allow_action_does_not_drop_moves(self, tmp_path):
        """ALLOW interval — no events dropped (ALLOW not in SCRUB_BLOCK_ACTIONS).

        The scrubber's blocked_intervals list is built from SCRUB_BLOCK_ACTIONS,
        so ALLOW frames never produce a BlockedInterval. We assert this
        invariant by verifying the dropping logic only triggers on intervals
        that are present — an ALLOW frame produces no interval, so its
        timestamps aren't dropped.
        """
        # No intervals are produced for ALLOW frames; this test verifies
        # that the find_blocked_interval lookup never matches.
        events = [
            _make_move(1005.0),
            _make_move(1006.0),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, [])

        moves = [e for e in scrubbed if e.get("type") == "mouse.move"]
        assert len(moves) == 2

    def test_mouse_move_at_interval_boundaries(self, tmp_path):
        """Boundary timestamps follow half-open [start, end) convention."""
        events = [
            _make_move(999.999),   # just before — kept
            _make_move(1000.0),    # exactly at start — dropped (start is inclusive)
            _make_move(1009.999),  # just before end — dropped
            _make_move(1010.0),    # exactly at end — kept (end is exclusive)
        ]
        intervals = [
            BlockedInterval(
                start=1000.0, end=1010.0,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        moves = [e for e in scrubbed if e.get("type") == "mouse.move"]
        timestamps = sorted(m["timestamp"] for m in moves)
        assert timestamps == [999.999, 1010.0]

    def test_non_mouse_move_in_interval_still_nulled_not_dropped(self, tmp_path):
        """Non-mouse.move events in blocked intervals → content nulled, event retained.

        Mouse coordinate fields on retained mouse events are now also nulled
        (R6/R12 fix for drag-coord leak — clicks/drags/scrolls inside blocked
        intervals get x/y/dx/dy zeroed so coarse interaction geometry doesn't
        leak alongside the nulled key content).
        """
        events = [
            {
                "type": "key.type",
                "timestamp": 1005.0,
                "text": "secret",
                "children": [
                    {"type": "key.down", "timestamp": 1005.0, "key_char": "s"},
                ],
            },
            {
                "type": "mouse.singleclick",
                "timestamp": 1006.0,
                "x": 100.0, "y": 200.0,
                "button": "left",
                "children": [],
            },
        ]
        intervals = [
            BlockedInterval(
                start=1000.0, end=1010.0,
                action=PrivacyAction.EXCLUDE,
                reason=ReasonCode.POLICY_EXCLUDED_APP,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        non_meta = [e for e in scrubbed if not e.get("_meta")]
        assert len(non_meta) == 2  # both retained
        key = next(e for e in non_meta if e["type"] == "key.type")
        assert key["text"] is None
        click = next(e for e in non_meta if e["type"] == "mouse.singleclick")
        # Mouse coord fields zeroed by null_event_content for retained
        # mouse events in SCRUB_BLOCK_ACTIONS intervals (event shape
        # preserved: timestamp/type/button intact, positional fields nulled).
        assert click["x"] is None
        assert click["y"] is None
        assert click["timestamp"] == 1006.0
        assert click["type"] == "mouse.singleclick"
        assert click["button"] == "left"


class TestMaskRegionPointerSuppression:
    """Todo 002: ``PrivacyAction.MASK_REGION`` is in SCRUB_BLOCK_ACTIONS so
    pointer geometry inside MASK_REGION intervals is dropped at scrub time.

    Forward-looking: shared mode (which routes EMAIL/CHAT/CALENDAR/
    VIDEO_CALL/CLOUD_STORAGE → MASK_REGION) is currently gated by
    ``parse_privacy_config``, but direct ``PrivacyConfig`` construction —
    common in tests, possible in any future programmatic caller —
    bypasses the gate. This test constructs the ``BlockedInterval``
    directly (mirroring the existing TEXT_REDACT/OCR_FALLBACK pattern)
    so the suppression posture is verifiable today.
    """

    def test_mask_region_drops_in_interval_mouse_moves(self, tmp_path):
        """MASK_REGION interval — in-interval mouse.move events dropped."""
        events = [
            _make_move(900.0),
            _make_move(1005.0),
            _make_move(1100.0),
        ]
        intervals = [
            BlockedInterval(
                start=1000.0, end=1010.0,
                action=PrivacyAction.MASK_REGION,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        moves = [e for e in scrubbed if e.get("type") == "mouse.move"]
        assert sorted(m["timestamp"] for m in moves) == [900.0, 1100.0]

    def test_mask_region_drag_drops_move_children(self, tmp_path):
        """MASK_REGION drag — move children dropped (mirrors MASK_WINDOW
        behavior; covered by the same in-interval child-filter at
        ``scrub_pipeline.py:967-978``)."""
        events = [
            {
                "type": "mouse.drag",
                "timestamp": 1005.0,
                "x": 100.0, "y": 200.0,
                "dx": 50.0, "dy": 30.0,
                "button": "left",
                "children": [
                    {
                        "type": "mouse.down",
                        "timestamp": 1005.0,
                        "x": 100.0, "y": 200.0,
                        "button": "left",
                    },
                    _make_move(1005.5, 110.0, 210.0),
                    {
                        "type": "mouse.up",
                        "timestamp": 1006.0,
                        "x": 150.0, "y": 230.0,
                        "button": "left",
                    },
                ],
            },
        ]
        intervals = [
            BlockedInterval(
                start=1000.0, end=1010.0,
                action=PrivacyAction.MASK_REGION,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        drags = [e for e in scrubbed if e.get("type") == "mouse.drag"]
        assert len(drags) == 1, "drag retained"
        child_types = [c.get("type") for c in drags[0]["children"]]
        assert "mouse.move" not in child_types, "move children dropped"


class TestMergedMouseMoveTimeRangeLeak:
    """P1: ``merge_consecutive_mouse_move_events`` collapses a run of raw
    moves into one event whose ``timestamp`` is the START of the run and
    ``last_timestamp`` is the END. Pre-fix, the scrub layer's drop check
    used ``find_blocked_interval(timestamp)`` — a point lookup at the START
    timestamp — so a merge that began BEFORE a blocked interval but ended
    INSIDE it (e.g. window switch into Slack mid-drag) slipped past the
    drop check. The merged event then leaked coordinates from inside the
    sensitive interval via ``path`` waypoints / ``x``/``y`` (last position).

    The fix adds ``last_timestamp`` to ``MouseMoveEvent`` and switches the
    drop check to a range-overlap test — any merge whose ``[timestamp,
    last_timestamp]`` span intersects a blocked interval is dropped.
    """

    def _make_merged_move(
        self,
        ts: float,
        last_ts: float,
        path: list[tuple[float, float]],
    ) -> dict:
        """Build a merged mouse.move JSONL event mimicking what
        ``merge_consecutive_mouse_move_events`` produces."""
        last_x, last_y = path[-1]
        return {
            "type": "mouse.move",
            "timestamp": ts,
            "last_timestamp": last_ts,
            "x": last_x,
            "y": last_y,
            "path": path,
        }

    def test_merged_move_spanning_into_interval_dropped(self, tmp_path):
        """Regression: merged move whose ``timestamp`` lies BEFORE a blocked
        interval but whose ``last_timestamp`` lies INSIDE it must be DROPPED.

        Pre-fix this would have leaked coordinates inside the interval
        (the path waypoint at (210, 310) and the final x/y) via the
        ``find_blocked_interval(0.9)`` returning None — point lookup misses
        the merge tail crossing the boundary.
        """
        events = [
            self._make_merged_move(
                ts=0.9,
                last_ts=1.10,
                path=[(100.0, 200.0), (210.0, 310.0), (220.0, 320.0)],
            ),
        ]
        intervals = [
            BlockedInterval(
                start=1.0, end=1.2,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        moves = [e for e in scrubbed if e.get("type") == "mouse.move"]
        assert moves == [], (
            "Merged move spanning into MASK_WINDOW interval must be dropped; "
            "leaking the in-interval path waypoint and last (x, y) violates the "
            "cloud-bound pointer-suppression guarantee."
        )

    def test_merged_move_entirely_before_interval_retained(self, tmp_path):
        """Negative: merged move ending BEFORE the blocked interval starts
        is retained — the merged span doesn't intersect any block."""
        events = [
            self._make_merged_move(
                ts=0.9,
                last_ts=0.95,
                path=[(100.0, 200.0), (110.0, 210.0)],
            ),
        ]
        intervals = [
            BlockedInterval(
                start=1.0, end=1.2,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        moves = [e for e in scrubbed if e.get("type") == "mouse.move"]
        assert len(moves) == 1
        assert moves[0]["timestamp"] == 0.9

    def test_unmerged_single_move_outside_interval_retained(self, tmp_path):
        """Negative: a single (unmerged) move with no ``last_timestamp``
        outside any interval is retained. Confirms the helper degenerates
        to a point-lookup when ``last_timestamp`` is missing."""
        events = [_make_move(0.9)]  # no last_timestamp field
        intervals = [
            BlockedInterval(
                start=1.0, end=1.2,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        moves = [e for e in scrubbed if e.get("type") == "mouse.move"]
        assert len(moves) == 1
        assert moves[0]["timestamp"] == 0.9
        assert "last_timestamp" not in moves[0] or moves[0].get("last_timestamp") is None

    def test_drag_child_merged_move_spanning_into_interval_dropped(self, tmp_path):
        """Drag children: an inline merged ``mouse.move`` child whose span
        crosses into a blocked interval is dropped from ``children``.
        Same range-overlap semantics as the standalone drop check."""
        merged_child_in = self._make_merged_move(
            ts=0.9,
            last_ts=1.10,
            path=[(100.0, 200.0), (210.0, 310.0)],
        )
        merged_child_out = self._make_merged_move(
            ts=0.5,
            last_ts=0.6,
            path=[(50.0, 50.0), (55.0, 55.0)],
        )
        events = [
            {
                "type": "mouse.drag",
                "timestamp": 0.4,
                "x": 50.0, "y": 50.0,
                "dx": 200.0, "dy": 270.0,
                "button": "left",
                "children": [
                    {
                        "type": "mouse.down",
                        "timestamp": 0.4,
                        "x": 50.0, "y": 50.0,
                        "button": "left",
                    },
                    merged_child_out,  # entirely before interval — kept
                    merged_child_in,   # spans into interval — dropped
                    {
                        "type": "mouse.up",
                        "timestamp": 1.3,
                        "x": 220.0, "y": 320.0,
                        "button": "left",
                    },
                ],
            },
        ]
        intervals = [
            BlockedInterval(
                start=1.0, end=1.2,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        drags = [e for e in scrubbed if e.get("type") == "mouse.drag"]
        assert len(drags) == 1, "drag retained"
        children = drags[0]["children"]
        # The merged_child_out (0.5, 0.6) is retained; merged_child_in (0.9, 1.10) dropped
        move_children = [c for c in children if c.get("type") == "mouse.move"]
        assert len(move_children) == 1, (
            "merged drag child spanning into blocked interval must be dropped"
        )
        assert move_children[0]["timestamp"] == 0.5

    def test_merged_move_last_timestamp_at_interval_start_boundary_dropped(
        self, tmp_path,
    ):
        """M-1 fix: merged move whose ``last_timestamp`` lands EXACTLY on
        an interval ``start_ts`` is DROPPED. The half-open ``[start, end)``
        convention makes ``start`` inclusive — the last waypoint at
        ``t == blocked.start`` IS inside the blocked interval and may
        carry in-interval pointer coordinates. Pre-fix the forward branch
        of ``_interval_intersects`` used ``i.start < end_ts`` (strict),
        so ``10 < 10`` was False and the move slipped through, leaking
        the last waypoint's coordinates. Post-fix the check is
        ``i.start <= end_ts`` (inclusive)."""
        events = [
            self._make_merged_move(
                ts=0.9,
                last_ts=1.0,  # exactly at interval start
                path=[(100.0, 200.0), (110.0, 210.0)],
            ),
        ]
        intervals = [
            BlockedInterval(
                start=1.0, end=1.2,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        moves = [e for e in scrubbed if e.get("type") == "mouse.move"]
        assert moves == [], (
            "merged move ending exactly at interval start (half-open boundary, "
            "start inclusive) must be dropped — the last waypoint is AT the "
            "blocked interval's start"
        )

    def test_merged_move_last_timestamp_just_past_interval_start_dropped(
        self, tmp_path,
    ):
        """Boundary: merged move whose ``last_timestamp`` is one millisecond
        PAST the interval start is DROPPED — the move tail crossed into
        the blocked interval and may carry in-interval coordinates."""
        events = [
            self._make_merged_move(
                ts=0.9,
                last_ts=1.001,  # one ms past interval start
                path=[(100.0, 200.0), (110.0, 210.0)],
            ),
        ]
        intervals = [
            BlockedInterval(
                start=1.0, end=1.2,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        moves = [e for e in scrubbed if e.get("type") == "mouse.move"]
        assert moves == [], (
            "merged move whose last_timestamp crosses into interval must be dropped"
        )


class TestDragCoordinateNulling:
    """Todo 003: ``null_event_content`` must zero mouse coordinate fields
    on retained mouse events (drag/click/scroll/etc.) inside a
    SCRUB_BLOCK_ACTIONS interval. Drag-child mouse.move waypoints are
    already dropped by the existing in-interval child filter; this
    closes the parent-drag positional-envelope leak (start/end coords,
    displacement) and the analogous leak on retained click/scroll.
    """

    def test_drag_in_interval_nulls_all_coordinate_fields(self, tmp_path):
        """A mouse.drag whose timestamp lands in a SCRUB_BLOCK_ACTIONS
        interval emits an event with all coordinate fields null but
        timestamp/type/button intact (event shape preserved for audit)."""
        events = [
            {
                "type": "mouse.drag",
                "timestamp": 1005.0,
                "x": 120.0,
                "y": 340.0,
                "dx": 380.0,
                "dy": 0.0,
                "button": "left",
                "children": [
                    {
                        "type": "mouse.down",
                        "timestamp": 1005.0,
                        "x": 120.0, "y": 340.0,
                        "button": "left",
                    },
                    {
                        "type": "mouse.up",
                        "timestamp": 1006.0,
                        "x": 500.0, "y": 340.0,
                        "button": "left",
                    },
                ],
            },
        ]
        intervals = [
            BlockedInterval(
                start=1000.0, end=1010.0,
                action=PrivacyAction.EXCLUDE,
                reason=ReasonCode.POLICY_EXCLUDED_APP,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        drags = [e for e in scrubbed if e.get("type") == "mouse.drag"]
        assert len(drags) == 1
        drag = drags[0]
        # All positional fields nulled — no leak of (120, 340) → (500, 340)
        assert drag["x"] is None
        assert drag["y"] is None
        assert drag["dx"] is None
        assert drag["dy"] is None
        # Shape preserved: timestamp/type/button intact for audit
        assert drag["timestamp"] == 1005.0
        assert drag["type"] == "mouse.drag"
        assert drag["button"] == "left"
        # Drag children (mouse.down/mouse.up) — also mouse events,
        # also nulled by the recursive null_event_content walk
        for child in drag["children"]:
            assert child["x"] is None
            assert child["y"] is None

    def test_scroll_in_interval_nulls_dx_dy(self, tmp_path):
        """mouse.scroll inside blocked interval — dx/dy nulled too."""
        events = [
            {
                "type": "mouse.scroll",
                "timestamp": 1005.0,
                "x": 200.0, "y": 400.0,
                "dx": 0.0, "dy": -120.0,
            },
        ]
        intervals = [
            BlockedInterval(
                start=1000.0, end=1010.0,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        scrolls = [e for e in scrubbed if e.get("type") == "mouse.scroll"]
        assert len(scrolls) == 1
        scroll = scrolls[0]
        assert scroll["x"] is None
        assert scroll["y"] is None
        assert scroll["dx"] is None
        assert scroll["dy"] is None
        assert scroll["timestamp"] == 1005.0  # shape preserved

    def test_drag_outside_interval_keeps_coordinates(self, tmp_path):
        """Drag outside any blocked interval — coordinates preserved
        (regression check: nulling only fires inside intervals)."""
        events = [
            {
                "type": "mouse.drag",
                "timestamp": 2000.0,
                "x": 120.0, "y": 340.0,
                "dx": 380.0, "dy": 0.0,
                "button": "left",
                "children": [],
            },
        ]
        intervals = [
            BlockedInterval(
                start=1000.0, end=1010.0,
                action=PrivacyAction.EXCLUDE,
                reason=ReasonCode.POLICY_EXCLUDED_APP,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        drags = [e for e in scrubbed if e.get("type") == "mouse.drag"]
        assert len(drags) == 1
        drag = drags[0]
        assert drag["x"] == 120.0
        assert drag["y"] == 340.0
        assert drag["dx"] == 380.0
        assert drag["dy"] == 0.0


class TestSCRUBBlockActionsIntegration:
    """Verify SCRUB_BLOCK_ACTIONS expanded set is used by build_scrub_context."""

    def test_text_redact_app_produces_blocked_interval_via_context(self, tmp_path):
        """build_scrub_context → SCRUB_BLOCK_ACTIONS → TEXT_REDACT app intervals exist."""
        from screencap.privacy.context import DefaultContextClassifier
        from screencap.privacy.policy import (
            DefaultPolicyEvaluator,
            parse_privacy_config,
        )

        db_path = tmp_path / "recording.db"
        _create_recording_db(
            db_path,
            window_events=[
                # VSCode → CODE_EDITOR_TERMINAL → TEXT_REDACT under PUBLIC mode
                {"timestamp": 1000.0, "app_bundle_id": "com.microsoft.VSCode"},
                {"timestamp": 2000.0, "app_bundle_id": "com.apple.finder"},
            ],
        )

        cfg = parse_privacy_config({"privacy": {"mode": "public"}})
        evaluator = DefaultPolicyEvaluator(cfg)
        classifier = DefaultContextClassifier()

        ctx = build_scrub_context(db_path, evaluator, classifier)

        # Under PUBLIC mode VSCode is TEXT_REDACT, which is in
        # SCRUB_BLOCK_ACTIONS. With the old BLOCK_ACTIONS (EXCLUDE only),
        # there would be zero intervals. Now there must be one covering the
        # VSCode timestamp range.
        assert any(
            iv.start == 1000.0 and iv.action == PrivacyAction.TEXT_REDACT
            for iv in ctx.blocked_intervals
        ), f"Expected TEXT_REDACT interval, got {ctx.blocked_intervals!r}"

    def test_build_blocked_intervals_default_unchanged(self):
        """Default actions=BLOCK_ACTIONS preserves screenshot-only semantics.

        Regression check: callers that pass no actions kwarg get the
        capture-time semantics (only EXCLUDE → blocked). This is what
        test_domain_propagation.py and test_scrubber_policy.py rely on.
        """
        from screencap.privacy.context import (
            DefaultContextClassifier,
            WindowContext,
        )
        from screencap.privacy.policy import (
            DefaultPolicyEvaluator,
            parse_privacy_config,
        )
        from screencap.scrub_pipeline import build_blocked_intervals

        cfg = parse_privacy_config({"privacy": {"mode": "public"}})
        evaluator = DefaultPolicyEvaluator(cfg)
        classifier = DefaultContextClassifier()

        # VSCode → TEXT_REDACT under PUBLIC mode (not in default BLOCK_ACTIONS)
        events = [
            WindowContext(
                timestamp=1.0,
                app_bundle_id="com.microsoft.VSCode",
                title="main.py",
            ),
        ]

        # Default actions=BLOCK_ACTIONS — TEXT_REDACT does NOT match
        intervals_default = build_blocked_intervals(events, evaluator, classifier)
        assert intervals_default == []

        # Explicit actions=SCRUB_BLOCK_ACTIONS — TEXT_REDACT DOES match
        from screencap.privacy.actions import SCRUB_BLOCK_ACTIONS as _SBA
        intervals_scrub = build_blocked_intervals(
            events, evaluator, classifier, actions=_SBA,
        )
        assert len(intervals_scrub) == 1
        assert intervals_scrub[0].action == PrivacyAction.TEXT_REDACT


class TestBuildScrubContextFailureLogging:
    """Todo 022: ``build_scrub_context`` outer-except path must emit a
    WARNING log so silent disabling of pointer suppression is visible to
    operators. The previous ``except Exception: pass`` swallowed every
    failure (busy SQLite, missing window_event table on older recordings,
    OOM during interval construction) and returned an empty context with
    no signal — the entire recording's mouse.move suppression silently
    disabled with ``had_errors=False`` from the downstream scrubber.
    """

    def test_warning_logged_on_outer_except(self, tmp_path, caplog):
        """Force the outer try/except to fire by patching
        ``open_recording_db`` to raise. Assert WARNING-level log is
        emitted naming the failure cause and that the returned context
        is still empty (graceful degradation preserved)."""
        import logging
        from unittest.mock import patch

        db_path = tmp_path / "recording.db"
        _create_recording_db(db_path)

        with patch(
            "screencap.scrub_pipeline.open_recording_db",
            side_effect=RuntimeError("simulated DB busy"),
        ), caplog.at_level(logging.WARNING, logger="screencap.scrub_pipeline"):
            ctx = build_scrub_context(db_path, evaluator=None, classifier=None)

        # Empty context returned (graceful degradation preserved)
        assert ctx.blocked_intervals == []
        assert ctx.window_events == []

        # WARNING log captured naming the failure cause
        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and "build_scrub_context failed" in r.getMessage()
        ]
        assert len(warnings) == 1, (
            f"Expected exactly one WARNING log, got: {[r.getMessage() for r in caplog.records]}"
        )
        msg = warnings[0].getMessage()
        assert "pointer suppression DISABLED" in msg
        assert "simulated DB busy" in msg

    def test_normal_path_does_not_log_warning(self, tmp_path, caplog):
        """Successful build_scrub_context emits no WARNING (regression check
        — the warning must be tied to the failure path, not unconditional)."""
        import logging

        db_path = tmp_path / "recording.db"
        _create_recording_db(db_path)

        with caplog.at_level(logging.WARNING, logger="screencap.scrub_pipeline"):
            ctx = build_scrub_context(db_path, evaluator=None, classifier=None)

        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and "build_scrub_context failed" in r.getMessage()
        ]
        assert warnings == [], (
            f"Unexpected WARNING on success path: {[r.getMessage() for r in warnings]}"
        )
        # Sanity: context was built (no exception)
        assert isinstance(ctx.blocked_intervals, list)


class TestOverlappingIntervalsLookup:
    """P1-overlap: ``find_blocked_interval`` and ``_interval_intersects``
    must handle overlapping intervals. ``merge_intervals`` only sorts by
    ``start`` and explicitly tolerates overlaps — a long app-window
    MASK_WINDOW interval can be concatenated with a short, nested
    secure-field EXCLUDE interval (both produced by ``build_scrub_context``).

    Pre-fix, ``find_blocked_interval`` did a single point lookup at
    ``bisect_right(_starts, ts) - 1``. With intervals
    ``[(10, 100, MASK_WINDOW), (20, 21, EXCLUDE)]`` and ``ts=30``:

    * ``bisect_right([10, 20], 30) - 1 = 1`` → checks ``(20, 21)``,
      ``20 <= 30 < 21`` is False → returns ``None``.
    * Pointer events at ``ts=30`` leak from inside the MASK_WINDOW
      interval that was masked by the closer-but-shorter EXCLUDE.

    The fix walks backwards through earlier intervals (all of which
    have ``start <= ts`` by sorted order) until one with ``end > ts``
    is found.
    """

    def test_regression_short_interval_masks_long_overlapping_interval(self):
        """Exact user scenario: ``[(10, 100, MASK_WINDOW), (20, 21, EXCLUDE)]``
        with ``ts=30`` must return the MASK_WINDOW interval (was None pre-fix).
        """
        from screencap.scrub_pipeline import find_blocked_interval

        intervals = [
            BlockedInterval(
                start=10.0, end=100.0,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
            BlockedInterval(
                start=20.0, end=21.0,
                action=PrivacyAction.EXCLUDE,
                reason=ReasonCode.SECURE_FIELD_DETECTED,
            ),
        ]

        hit = find_blocked_interval(30.0, intervals)
        assert hit is not None, (
            "Pre-fix bisect picked the closer-but-shorter EXCLUDE interval and "
            "returned None — leaking pointer events at ts=30 from inside the "
            "long MASK_WINDOW interval."
        )
        assert hit.start == 10.0
        assert hit.end == 100.0
        assert hit.action == PrivacyAction.MASK_WINDOW

    def test_multi_overlap_nested_intervals(self):
        """Three nested intervals — every timestamp inside the outer interval
        must hit some interval (not None) regardless of how the bisect lookup
        positions the cursor."""
        from screencap.scrub_pipeline import find_blocked_interval

        intervals = [
            BlockedInterval(
                start=0.0, end=100.0,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
            BlockedInterval(
                start=10.0, end=50.0,
                action=PrivacyAction.TEXT_REDACT,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
            BlockedInterval(
                start=20.0, end=30.0,
                action=PrivacyAction.EXCLUDE,
                reason=ReasonCode.SECURE_FIELD_DETECTED,
            ),
        ]

        # ts=5: only outer (0, 100) contains it
        hit = find_blocked_interval(5.0, intervals)
        assert hit is not None and hit.start == 0.0

        # ts=15: outer + middle contain it; either is acceptable
        hit = find_blocked_interval(15.0, intervals)
        assert hit is not None
        assert hit.start <= 15.0 < hit.end

        # ts=25: all three contain it; latest-start (innermost) wins from
        # the bisect cursor — but any non-None is correct
        hit = find_blocked_interval(25.0, intervals)
        assert hit is not None
        assert hit.start <= 25.0 < hit.end

        # ts=40: only outer + middle (TEXT_REDACT) contain it; innermost
        # has ended, but the walk-backwards must skip it and find the
        # middle one — pre-fix would return None here
        hit = find_blocked_interval(40.0, intervals)
        assert hit is not None
        assert hit.start <= 40.0 < hit.end

        # ts=75: only outer contains it; walk-backwards must skip the
        # middle (ended at 50) and find the outer — pre-fix would also
        # return None here
        hit = find_blocked_interval(75.0, intervals)
        assert hit is not None
        assert hit.start == 0.0
        assert hit.end == 100.0

        # ts=150: nothing contains it
        assert find_blocked_interval(150.0, intervals) is None

    def test_overlap_boundary_lookups(self):
        """Boundary semantics for overlapping intervals:

        * ``ts=15`` is inside both (10, 20) and (15, 25) — half-open
          [start, end) means start is inclusive, end exclusive — must
          return one of them (not None).
        * ``ts=22`` is past (10, 20) but inside (15, 25) — pre-fix the
          bisect cursor would land on (15, 25), find ``15 <= 22 < 25``
          → True, return it. Post-fix: same result. Importantly, even
          if (15, 25) had ended at 20, the walk would step back to (10,
          20) and reject it (``22 >= 20``) → None, which is correct.
        """
        from screencap.scrub_pipeline import find_blocked_interval

        intervals = [
            BlockedInterval(
                start=10.0, end=20.0,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
            BlockedInterval(
                start=15.0, end=25.0,
                action=PrivacyAction.TEXT_REDACT,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]

        # ts=15: both contain it
        hit = find_blocked_interval(15.0, intervals)
        assert hit is not None
        assert hit.start <= 15.0 < hit.end

        # ts=22: only (15, 25) contains it
        hit = find_blocked_interval(22.0, intervals)
        assert hit is not None
        assert hit.start == 15.0
        assert hit.end == 25.0

        # ts=20: only (15, 25) contains it (end of (10, 20) is exclusive)
        hit = find_blocked_interval(20.0, intervals)
        assert hit is not None
        assert hit.start == 15.0
        assert hit.end == 25.0

        # ts=25: nothing contains it
        assert find_blocked_interval(25.0, intervals) is None

    def test_pure_non_overlap_unchanged(self):
        """Non-overlapping cases still work — picks one from existing tests
        to confirm the walk-backwards loop terminates after one iteration
        and behaves identically to the original point lookup."""
        from screencap.scrub_pipeline import find_blocked_interval

        intervals = [
            BlockedInterval(
                start=100.0, end=200.0,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
            BlockedInterval(
                start=300.0, end=400.0,
                action=PrivacyAction.EXCLUDE,
                reason=ReasonCode.POLICY_EXCLUDED_APP,
            ),
            BlockedInterval(
                start=500.0, end=600.0,
                action=PrivacyAction.TEXT_REDACT,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]

        # Inside first
        hit = find_blocked_interval(150.0, intervals)
        assert hit is not None and hit.start == 100.0

        # Between first and second — pre-fix returned None; post-fix
        # also returns None (walk steps back to (100, 200), rejects, then
        # idx=-1 — terminates correctly)
        assert find_blocked_interval(250.0, intervals) is None

        # Inside second
        hit = find_blocked_interval(350.0, intervals)
        assert hit is not None and hit.start == 300.0

        # Inside third
        hit = find_blocked_interval(550.0, intervals)
        assert hit is not None and hit.start == 500.0

        # Past end of all
        assert find_blocked_interval(700.0, intervals) is None

        # Before start of all
        assert find_blocked_interval(50.0, intervals) is None

    def test_interval_intersects_with_overlap_in_first_branch(self):
        """``_interval_intersects([5, 15], …)`` over
        ``[(10, 100), (20, 21)]`` — first branch must find ``(10, 100)``.

        Trace:
        * ``find_blocked_interval(5)`` walks back from
          ``bisect_right([10, 20], 5) - 1 = -1`` → None.
        * Forward branch: ``bisect_right([10, 20], 5) = 0`` →
          ``intervals[0] = (10, 100)``, ``10 < 15 = end_ts`` → return.
        """
        from screencap.scrub_pipeline import _interval_intersects

        intervals = [
            BlockedInterval(
                start=10.0, end=100.0,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
            BlockedInterval(
                start=20.0, end=21.0,
                action=PrivacyAction.EXCLUDE,
                reason=ReasonCode.SECURE_FIELD_DETECTED,
            ),
        ]

        # Span [5, 15] — start before all, end inside (10, 100)
        hit = _interval_intersects(5.0, 15.0, intervals)
        assert hit is not None
        assert hit.start == 10.0
        assert hit.end == 100.0

        # Span [105, 110] — entirely past all intervals
        assert _interval_intersects(105.0, 110.0, intervals) is None

        # Span [25, 30] — entirely inside (10, 100), past (20, 21).
        # First branch: find_blocked_interval(25) walks back from
        # idx = bisect_right([10, 20], 25) - 1 = 1 → check (20, 21):
        # 20 <= 25 < 21 is False → walk back to idx=0 → (10, 100):
        # 10 <= 25 < 100 → return (10, 100). Pre-fix this returned None.
        hit = _interval_intersects(25.0, 30.0, intervals)
        assert hit is not None
        assert hit.start == 10.0

    def test_merged_move_overlap_regression(self, tmp_path):
        """P1-overlap end-to-end: merged ``mouse.move`` whose start lands
        in the gap after a short EXCLUDE but inside the still-active long
        MASK_WINDOW must be DROPPED.

        Intervals: ``[(10, 100, MASK_WINDOW), (20, 21, EXCLUDE)]``.
        Merged move ``[15, 35]`` — start at 15 is inside MASK_WINDOW, end
        at 35 is also inside MASK_WINDOW. Pre-fix, the first branch of
        ``_interval_intersects`` called ``find_blocked_interval(15)``
        which returned None (bisect picked the closer-but-shorter
        EXCLUDE). The forward branch then checked the FIRST interval
        starting after 15 — that's (20, 21) which does start before 35,
        so it would have correctly returned (20, 21) → drop. But for a
        merge entirely INSIDE the MASK_WINDOW (e.g., [25, 35]), the
        forward branch would find no later interval at all (idx=2 out
        of range) and return None — leaking coordinates.
        """
        from screencap.scrub_pipeline import _interval_intersects

        intervals = [
            BlockedInterval(
                start=10.0, end=100.0,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
            BlockedInterval(
                start=20.0, end=21.0,
                action=PrivacyAction.EXCLUDE,
                reason=ReasonCode.SECURE_FIELD_DETECTED,
            ),
        ]

        # Merged move [15, 35] — start 15 is inside MASK_WINDOW; first
        # branch must find (10, 100).
        hit = _interval_intersects(15.0, 35.0, intervals)
        assert hit is not None and hit.start == 10.0

        # Merged move [25, 35] — entirely inside MASK_WINDOW, past
        # EXCLUDE. Pre-fix this would have returned None (first branch
        # missed MASK_WINDOW because EXCLUDE was closer; forward branch
        # found no interval starting after 25).
        hit = _interval_intersects(25.0, 35.0, intervals)
        assert hit is not None and hit.start == 10.0

        # Now exercise the JSONL drop path with the same scenario:
        # merged move with start 25 and last_timestamp 35 must be dropped.
        events = [
            {
                "type": "mouse.move",
                "timestamp": 25.0,
                "last_timestamp": 35.0,
                "x": 220.0, "y": 320.0,
                "path": [(100.0, 200.0), (220.0, 320.0)],
            },
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        moves = [e for e in scrubbed if e.get("type") == "mouse.move"]
        assert moves == [], (
            "Merged move entirely inside the long MASK_WINDOW (with a nested "
            "shorter EXCLUDE earlier in the list) must be dropped — pre-fix the "
            "bisect lookup picked the closer EXCLUDE and missed the "
            "still-active MASK_WINDOW."
        )

    def test_build_scrub_context_overlap_drops_in_window_move(self, tmp_path):
        """Integration: ``build_scrub_context`` with a MASK_WINDOW app and
        a nested AXSecureTextField produces overlapping intervals
        (long MASK_WINDOW + short EXCLUDE). A ``mouse.move`` at a
        timestamp inside the MASK_WINDOW but past the EXCLUDE must be
        dropped from the scrubbed JSONL.

        This is the cross-layer regression: ``build_scrub_context``
        actually composes overlapping intervals via ``merge_intervals``
        in the wild — Slack / 1Password during a screen recording.
        """
        from screencap.privacy.context import DefaultContextClassifier
        from screencap.privacy.policy import (
            DefaultPolicyEvaluator,
            parse_privacy_config,
        )

        db_path = tmp_path / "recording.db"
        # 1Password is in PASSWORD_MANAGER context → MASK_WINDOW (or
        # EXCLUDE depending on mode); use Slack which is in CHAT context
        # under PUBLIC mode → MASK_WINDOW for the long-overlap interval.
        # Add an action_event with AXSecureTextField at 1020.0 to inject
        # a short overlapping EXCLUDE.
        _create_recording_db(
            db_path,
            window_events=[
                # Slack chat session from 1000 to 1100 — MASK_WINDOW
                {"timestamp": 1000.0, "app_bundle_id": "com.tinyspeck.slackmacgap"},
                # Switch to a non-blocked app at 1100
                {"timestamp": 1100.0, "app_bundle_id": "com.apple.finder"},
            ],
            action_events=[
                # Secure-field event inside the Slack interval — EXCLUDE
                # spans roughly (1020.0, 1020.0 + DEFAULT_TRANSITION_HOLD)
                {
                    "timestamp": 1020.0,
                    "name": "press",
                    "key_char": "x",
                    "element_state": json.dumps({"AXRole": "AXSecureTextField"}),
                },
            ],
        )

        cfg = parse_privacy_config({"privacy": {"mode": "public"}})
        evaluator = DefaultPolicyEvaluator(cfg)
        classifier = DefaultContextClassifier()

        ctx = build_scrub_context(
            db_path,
            evaluator=evaluator,
            classifier=classifier,
        )

        # Sanity: we got both kinds of intervals — long Slack MASK_WINDOW
        # and short secure-field EXCLUDE.
        assert any(
            iv.action == PrivacyAction.MASK_WINDOW
            for iv in ctx.blocked_intervals
        ), f"Expected a MASK_WINDOW interval; got {ctx.blocked_intervals}"
        assert any(
            iv.action == PrivacyAction.EXCLUDE
            and iv.reason == ReasonCode.SECURE_FIELD_DETECTED
            for iv in ctx.blocked_intervals
        ), f"Expected a secure-field EXCLUDE interval; got {ctx.blocked_intervals}"

        # Mouse move at 1030.0 — past the secure-field EXCLUDE interval
        # (which ends ~1021.0), but still inside the Slack MASK_WINDOW
        # (1000.0 → 1100.0). Pre-fix, the bisect cursor would land on
        # the secure-field EXCLUDE (closer start than Slack), find it
        # had ended, and return None — leaking the move from inside
        # the masked Slack window.
        events = [_make_move(1030.0, 200.0, 300.0)]
        events_path = tmp_path / "events_0000.jsonl"
        _write_events_jsonl(events_path, events)

        pipeline, anonymizer = _make_pipeline()
        result = ScrubResult()
        scrub_events_jsonl(
            events_path, pipeline, anonymizer,
            ctx=ctx, result=result,
        )

        scrubbed = [
            json.loads(l) for l in events_path.read_text().splitlines() if l.strip()
        ]
        moves = [e for e in scrubbed if e.get("type") == "mouse.move"]
        assert moves == [], (
            "mouse.move at ts=1030 inside the Slack MASK_WINDOW (1000→1100) "
            "must be dropped despite a shorter secure-field EXCLUDE "
            "interval starting at 1020 having already ended."
        )


class TestDragSpansIntoBlockedInterval:
    """Bug H: a ``mouse.drag`` whose START is OUTSIDE a blocked interval
    but whose CHILDREN extend INTO the interval was leaking the END
    coordinate ``(x+dx, y+dy)`` and any non-mouse.move children
    (mouse.up/mouse.down) whose timestamps landed inside the interval.

    Pre-fix the parent-only ``find_blocked_interval(event_ts)`` check
    saw the drag's start outside any interval and skipped nulling.
    The mouse.move children filter dropped only mouse.move children,
    leaving mouse.up/mouse.down survivors with in-interval coordinates.

    Post-fix: compute the drag's effective span from its children, range-
    overlap-test it, and if any overlap exists drop EVERY in-interval
    child plus null the parent drag's coordinate fields.
    """

    def test_drag_starts_outside_ends_inside_blocked_interval(self, tmp_path):
        """Drag start at t=0.9 (outside), mouse.up child at t=1.5 (inside
        [1.0, 2.0) MASK_WINDOW). Pre-fix the parent x/y/dx/dy survived
        and the mouse.up child survived too. Post-fix everything is
        suppressed."""
        events = [
            {
                "type": "mouse.drag",
                "timestamp": 0.9,
                "x": 100.0, "y": 200.0,
                "dx": 400.0, "dy": 100.0,
                "button": "left",
                "children": [
                    {
                        "type": "mouse.down",
                        "timestamp": 0.9,
                        "x": 100.0, "y": 200.0,
                        "button": "left",
                    },
                    {
                        "type": "mouse.up",
                        "timestamp": 1.5,  # inside blocked interval
                        "x": 500.0, "y": 300.0,
                        "button": "left",
                    },
                ],
            },
        ]
        intervals = [
            BlockedInterval(
                start=1.0, end=2.0,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        scrubbed, result = _scrub_with_intervals(tmp_path, events, intervals)

        drags = [e for e in scrubbed if e.get("type") == "mouse.drag"]
        assert len(drags) == 1
        drag = drags[0]
        # Parent coords nulled (pre-fix these survived)
        assert drag["x"] is None
        assert drag["y"] is None
        assert drag["dx"] is None
        assert drag["dy"] is None
        # Audit shape preserved
        assert drag["timestamp"] == 0.9
        assert drag["button"] == "left"

        # mouse.up child at 1.5 dropped (was leaking END coordinate)
        child_types = [c.get("type") for c in drag["children"]]
        assert "mouse.up" not in child_types
        # mouse.down at 0.9 (outside interval) is retained
        assert child_types == ["mouse.down"]
        kept_down = drag["children"][0]
        # The retained mouse.down's own coordinates are nulled by the
        # recursive null_event_content walk on the parent drag — any
        # drag that touches a blocked interval has the whole envelope
        # suppressed.
        assert kept_down["x"] is None
        assert kept_down["y"] is None

        # Audit entry recorded
        assert any(
            e.surface == "event" and e.action == PrivacyAction.MASK_WINDOW.value
            for e in result.audit_entries
        )

    def test_drag_starts_inside_ends_outside_blocked_interval(self, tmp_path):
        """Drag start at t=1.5 (inside MASK_WINDOW [1.0, 2.0)), mouse.up
        child at t=2.5 (outside, in ALLOW). Any overlap means parent
        nulled — pre-existing single-interval behavior covers START
        inside, but we add an explicit test for symmetry."""
        events = [
            {
                "type": "mouse.drag",
                "timestamp": 1.5,
                "x": 100.0, "y": 200.0,
                "dx": 400.0, "dy": 100.0,
                "button": "left",
                "children": [
                    {
                        "type": "mouse.down",
                        "timestamp": 1.5,
                        "x": 100.0, "y": 200.0,
                        "button": "left",
                    },
                    {
                        "type": "mouse.up",
                        "timestamp": 2.5,  # outside blocked interval
                        "x": 500.0, "y": 300.0,
                        "button": "left",
                    },
                ],
            },
        ]
        intervals = [
            BlockedInterval(
                start=1.0, end=2.0,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        drags = [e for e in scrubbed if e.get("type") == "mouse.drag"]
        assert len(drags) == 1
        drag = drags[0]
        # Parent coords nulled (start inside)
        assert drag["x"] is None
        assert drag["y"] is None
        assert drag["dx"] is None
        assert drag["dy"] is None

        # mouse.down at 1.5 (inside) dropped, mouse.up at 2.5 (outside) kept
        child_types = [c.get("type") for c in drag["children"]]
        assert "mouse.down" not in child_types
        assert child_types == ["mouse.up"]

    def test_drag_entirely_outside_blocked_intervals_keeps_everything(
        self, tmp_path,
    ):
        """Regression: drag entirely outside any blocked interval — parent
        coords AND children kept, nothing nulled."""
        events = [
            {
                "type": "mouse.drag",
                "timestamp": 5.0,
                "x": 100.0, "y": 200.0,
                "dx": 50.0, "dy": 30.0,
                "button": "left",
                "children": [
                    {
                        "type": "mouse.down",
                        "timestamp": 5.0,
                        "x": 100.0, "y": 200.0,
                        "button": "left",
                    },
                    {
                        "type": "mouse.up",
                        "timestamp": 5.5,
                        "x": 150.0, "y": 230.0,
                        "button": "left",
                    },
                ],
            },
        ]
        intervals = [
            BlockedInterval(
                start=1.0, end=2.0,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        scrubbed, result = _scrub_with_intervals(tmp_path, events, intervals)

        drags = [e for e in scrubbed if e.get("type") == "mouse.drag"]
        assert len(drags) == 1
        drag = drags[0]
        # Coords preserved
        assert drag["x"] == 100.0
        assert drag["y"] == 200.0
        assert drag["dx"] == 50.0
        assert drag["dy"] == 30.0
        # Children preserved with coords
        child_types = [c.get("type") for c in drag["children"]]
        assert child_types == ["mouse.down", "mouse.up"]
        for c in drag["children"]:
            assert c["x"] is not None
            assert c["y"] is not None

        # No event-surface audit entry for this drag (no overlap)
        drag_audits = [
            e for e in result.audit_entries
            if e.surface == "event" and e.timestamp == 5.0
        ]
        assert drag_audits == []

    def test_drag_entirely_inside_blocked_interval(self, tmp_path):
        """Pre-existing case: drag with start AND end inside a blocked
        interval — parent nulled, all children dropped (regression check
        that the new range-overlap branch still handles this correctly)."""
        events = [
            {
                "type": "mouse.drag",
                "timestamp": 1.2,
                "x": 100.0, "y": 200.0,
                "dx": 50.0, "dy": 30.0,
                "button": "left",
                "children": [
                    {
                        "type": "mouse.down",
                        "timestamp": 1.2,
                        "x": 100.0, "y": 200.0,
                        "button": "left",
                    },
                    {
                        "type": "mouse.up",
                        "timestamp": 1.7,
                        "x": 150.0, "y": 230.0,
                        "button": "left",
                    },
                ],
            },
        ]
        intervals = [
            BlockedInterval(
                start=1.0, end=2.0,
                action=PrivacyAction.EXCLUDE,
                reason=ReasonCode.POLICY_EXCLUDED_APP,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        drags = [e for e in scrubbed if e.get("type") == "mouse.drag"]
        assert len(drags) == 1
        drag = drags[0]
        assert drag["x"] is None and drag["y"] is None
        assert drag["dx"] is None and drag["dy"] is None
        # All children inside [1.0, 2.0) → all dropped
        assert drag["children"] == []

    def test_drag_with_mouse_up_child_inside_blocked_interval(self, tmp_path):
        """Targeted: drag with one in-interval mouse.up child — that
        child is dropped (NEW behavior; pre-fix only mouse.move children
        were dropped, mouse.up survived and leaked the END coordinate)."""
        events = [
            {
                "type": "mouse.drag",
                "timestamp": 0.5,
                "x": 100.0, "y": 200.0,
                "dx": 400.0, "dy": 100.0,
                "button": "left",
                "children": [
                    {
                        "type": "mouse.down",
                        "timestamp": 0.5,
                        "x": 100.0, "y": 200.0,
                        "button": "left",
                    },
                    {
                        "type": "mouse.up",
                        "timestamp": 1.5,  # inside [1.0, 2.0)
                        "x": 500.0, "y": 300.0,
                        "button": "left",
                    },
                ],
            },
        ]
        intervals = [
            BlockedInterval(
                start=1.0, end=2.0,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        drag = next(e for e in scrubbed if e.get("type") == "mouse.drag")
        child_types = [c.get("type") for c in drag["children"]]
        # mouse.up at 1.5 dropped — it carried (500, 300) inside the
        # blocked interval. mouse.down at 0.5 (outside) is kept.
        assert "mouse.up" not in child_types
        assert child_types == ["mouse.down"]

    def test_drag_with_mixed_children_all_in_interval_types_dropped(
        self, tmp_path,
    ):
        """Drag with mouse.move + mouse.up + key.type children all
        inside the blocked interval — every in-interval child dropped
        regardless of type (mouse.move and mouse.up leak coordinates;
        key.type leaks content)."""
        events = [
            {
                "type": "mouse.drag",
                "timestamp": 0.5,
                "x": 100.0, "y": 200.0,
                "dx": 400.0, "dy": 100.0,
                "button": "left",
                "children": [
                    {
                        "type": "mouse.down",
                        "timestamp": 0.5,
                        "x": 100.0, "y": 200.0,
                        "button": "left",
                    },
                    _make_move(1.2, 200.0, 220.0),  # in interval
                    {
                        "type": "key.type",  # in interval
                        "timestamp": 1.3,
                        "text": "secret",
                        "children": [
                            {"type": "key.down", "timestamp": 1.3, "key_char": "s"},
                        ],
                    },
                    {
                        "type": "mouse.up",
                        "timestamp": 1.5,  # in interval
                        "x": 500.0, "y": 300.0,
                        "button": "left",
                    },
                ],
            },
        ]
        intervals = [
            BlockedInterval(
                start=1.0, end=2.0,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        drag = next(e for e in scrubbed if e.get("type") == "mouse.drag")
        child_types = [c.get("type") for c in drag["children"]]
        # Only the mouse.down at 0.5 (outside the interval) survives;
        # mouse.move/key.type/mouse.up at 1.2/1.3/1.5 (inside) are
        # dropped regardless of event type.
        assert child_types == ["mouse.down"]

    def test_drag_last_child_timestamp_at_interval_start_boundary(
        self, tmp_path,
    ):
        """Boundary (M-1 + H interaction): drag's last child timestamp
        lands EXACTLY on the blocked interval's start. Post-M-1 fix the
        forward branch of ``_interval_intersects`` is inclusive so the
        drag span ``[0.5, 1.0]`` IS detected as overlapping the
        blocked interval ``[1.0, 2.0)``. Pre-fix the strict ``<`` would
        miss this and the drag would survive intact, leaking the
        boundary mouse.up coordinates."""
        events = [
            {
                "type": "mouse.drag",
                "timestamp": 0.5,
                "x": 100.0, "y": 200.0,
                "dx": 400.0, "dy": 100.0,
                "button": "left",
                "children": [
                    {
                        "type": "mouse.down",
                        "timestamp": 0.5,
                        "x": 100.0, "y": 200.0,
                        "button": "left",
                    },
                    {
                        "type": "mouse.up",
                        "timestamp": 1.0,  # exactly at interval start
                        "x": 500.0, "y": 300.0,
                        "button": "left",
                    },
                ],
            },
        ]
        intervals = [
            BlockedInterval(
                start=1.0, end=2.0,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        drag = next(e for e in scrubbed if e.get("type") == "mouse.drag")
        # Drag span [0.5, 1.0] overlaps blocked [1.0, 2.0) at the
        # boundary — parent nulled, mouse.up at boundary dropped.
        assert drag["x"] is None
        assert drag["y"] is None
        child_types = [c.get("type") for c in drag["children"]]
        assert "mouse.up" not in child_types

    def test_drag_with_no_children_and_overlap(self, tmp_path):
        """Edge case: drag with no children — drag span degenerates to
        a point at ``event_ts``. With ``event_ts`` inside the blocked
        interval the existing point-lookup branch already nulls the
        drag (this test just confirms the new branch doesn't regress
        the empty-children path)."""
        events = [
            {
                "type": "mouse.drag",
                "timestamp": 1.5,
                "x": 100.0, "y": 200.0,
                "dx": 50.0, "dy": 30.0,
                "button": "left",
                "children": [],
            },
        ]
        intervals = [
            BlockedInterval(
                start=1.0, end=2.0,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        scrubbed, _ = _scrub_with_intervals(tmp_path, events, intervals)

        drag = next(e for e in scrubbed if e.get("type") == "mouse.drag")
        assert drag["x"] is None
        assert drag["y"] is None
        assert drag["dx"] is None
        assert drag["dy"] is None


class TestIntervalIntersectsBoundary:
    """Bug M-1: ``_interval_intersects`` second branch had an off-by-one
    error — used strict ``intervals[idx].start < end_ts`` so a merged
    move whose ``last_timestamp == blocked.start`` was missed
    (``10 < 10`` is False), leaking the last waypoint's coordinates
    inside the blocked interval. Fix is one character: ``<`` → ``<=``.
    """

    def test_merged_move_last_timestamp_equals_interval_start_intersects(self):
        """``_interval_intersects(0.9, 1.0, [(1.0, 1.2)])`` must return
        the interval — the last waypoint at t=1.0 is AT the blocked
        interval's start, which is inside the half-open ``[1.0, 1.2)``
        (start inclusive). Pre-fix returned None (``1.0 < 1.0`` is False)."""
        from screencap.scrub_pipeline import _interval_intersects

        intervals = [
            BlockedInterval(
                start=1.0, end=1.2,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        hit = _interval_intersects(0.9, 1.0, intervals)
        assert hit is not None, (
            "merged move ending at the boundary (last_timestamp == "
            "blocked.start) must intersect — pre-fix `start < end_ts` "
            "missed this"
        )
        assert hit.start == 1.0

    def test_merged_move_last_timestamp_just_before_interval_start_no_intersect(
        self,
    ):
        """``_interval_intersects(0.9, 0.999, [(1.0, 1.2)])`` returns None
        — the move ends BEFORE the interval starts (no overlap)."""
        from screencap.scrub_pipeline import _interval_intersects

        intervals = [
            BlockedInterval(
                start=1.0, end=1.2,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        assert _interval_intersects(0.9, 0.999, intervals) is None

    def test_merged_move_end_at_interval_end_boundary(self):
        """Boundary case: merged move ``[0.5, 1.2]`` over
        ``[(1.0, 1.2), (1.2, 1.5)]``. The first interval contains
        ``start_ts=0.5``? No — but the forward branch finds
        ``intervals[0].start = 1.0 <= 1.2`` → intersects. If a NEXT
        interval starts at 1.2 (touching), the move's end touches that
        next interval too — but we only return the first match found
        by the cheap path / forward bisect, so the assertion is just
        that SOME interval is returned."""
        from screencap.scrub_pipeline import _interval_intersects

        intervals = [
            BlockedInterval(
                start=1.0, end=1.2,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
            BlockedInterval(
                start=1.2, end=1.5,
                action=PrivacyAction.EXCLUDE,
                reason=ReasonCode.POLICY_EXCLUDED_APP,
            ),
        ]
        hit = _interval_intersects(0.5, 1.2, intervals)
        # The cheap path: find_blocked_interval(0.5) → None (before all).
        # Forward: bisect_right([1.0, 1.2], 0.5) = 0 → intervals[0].start = 1.0
        # 1.0 <= 1.2 → return (1.0, 1.2).
        assert hit is not None
        assert hit.start == 1.0

    def test_unmerged_move_at_interval_start_handled_by_first_branch(self):
        """An unmerged move (``end_ts == start_ts``) AT the interval start
        is caught by the cheap path (first branch) via
        ``find_blocked_interval``. The second branch's inclusive ``<=``
        is only relevant for real ranges (``end_ts > start_ts``), so
        this assertion confirms the unmerged path still returns the
        interval correctly via the cheap path."""
        from screencap.scrub_pipeline import _interval_intersects

        intervals = [
            BlockedInterval(
                start=1.0, end=1.2,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        # Unmerged: start_ts == end_ts == 1.0 → cheap path:
        # find_blocked_interval(1.0) → 1.0 <= 1.0 < 1.2 → return interval.
        hit = _interval_intersects(1.0, 1.0, intervals)
        assert hit is not None and hit.start == 1.0

    def test_unmerged_move_just_before_interval_returns_none(self):
        """Unmerged move (``end_ts == start_ts``) at ``ts=0.999`` does
        NOT intersect ``[1.0, 1.2)``. Verifies the second branch's
        early-return ``end_ts <= start_ts`` does not falsely match."""
        from screencap.scrub_pipeline import _interval_intersects

        intervals = [
            BlockedInterval(
                start=1.0, end=1.2,
                action=PrivacyAction.MASK_WINDOW,
                reason=ReasonCode.POLICY_MODE_DEFAULT,
            ),
        ]
        assert _interval_intersects(0.999, 0.999, intervals) is None
