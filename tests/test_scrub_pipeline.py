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
