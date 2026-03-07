"""Tests for privacy v3 Phase 4: structural masking.

Tests that:
- apply_mask renders a valid darkened JPEG
- MASK_WINDOW keeps screenshots with masked content (not deleted)
- corrupt images fall back to deletion safely
- all communication surfaces are masked in public mode
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from screencap.privacy.actions import PrivacyAction
from screencap.privacy.context import DefaultContextClassifier, WindowContext
from screencap.privacy.masking import (
    MaskRegion,
    MaskStrategy,
    _apply_mask_to_image,
    full_window_geometry,
    mask_screenshot,
)
from screencap.privacy.policy import (
    ContextClass,
    DefaultPolicyEvaluator,
    PrivacyMode,
    parse_privacy_config,
)
from screencap.scrubber import ScrubResult, _scrub_screenshots_with_policy

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_jpeg(path: Path, width: int = 100, height: int = 80) -> Path:
    """Create a small solid-white JPEG for testing."""
    from PIL import Image

    img = Image.new("RGB", (width, height), (255, 255, 255))
    img.save(path, "JPEG")
    return path


def _make_evaluator(**kwargs) -> DefaultPolicyEvaluator:
    cfg = parse_privacy_config({"privacy": kwargs})
    return DefaultPolicyEvaluator(cfg)


def _make_window_events(specs: list[tuple[float, str]]) -> list[WindowContext]:
    return [
        WindowContext(timestamp=ts, app_bundle_id=bid, title="")
        for ts, bid in specs
    ]


def _avg_brightness(path: Path) -> float:
    """Return average pixel brightness (0-255) of a JPEG."""
    from PIL import Image

    img = Image.open(path).convert("L")  # grayscale
    pixels = list(img.get_flattened_data())
    return sum(pixels) / len(pixels)


# ---------------------------------------------------------------------------
# _apply_mask_to_image rendering
# ---------------------------------------------------------------------------


class TestApplyMaskToImage:
    def test_produces_valid_darkened_jpeg(self, tmp_path):
        """_apply_mask_to_image renders masks that make the image visually darker."""
        from PIL import Image

        img_path = _make_test_jpeg(tmp_path / "test.jpg")
        original_brightness = _avg_brightness(img_path)

        regions = [MaskRegion(x=0, y=0, width=100, height=80, label="email")]
        img = Image.open(img_path).convert("RGB")
        _apply_mask_to_image(img, regions)
        img.save(img_path, "JPEG", quality=85, exif=b"")
        img.close()

        # File is still a valid JPEG
        img = Image.open(img_path)
        assert img.format == "JPEG"
        assert img.size == (100, 80)
        img.close()

        # Content is significantly darker
        masked_brightness = _avg_brightness(img_path)
        assert masked_brightness < original_brightness * 0.3


# ---------------------------------------------------------------------------
# Scrubber MASK_WINDOW integration
# ---------------------------------------------------------------------------


class TestMaskWindowIntegration:
    def _setup(self, tmp_path, timestamps, window_specs, mode="public"):
        """Set up a recording dir with screenshots and window events."""
        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir()
        for ts in timestamps:
            _make_test_jpeg(screenshots_dir / f"{ts}.jpg")

        evaluator = _make_evaluator(mode=mode)
        classifier = DefaultContextClassifier()
        window_events = _make_window_events(window_specs)
        result = ScrubResult()
        return tmp_path, evaluator, classifier, window_events, result

    def test_keeps_file_with_masked_content(self, tmp_path):
        """MASK_WINDOW: file still exists but pixel content is darkened."""
        dst, evaluator, classifier, window_events, result = self._setup(
            tmp_path,
            timestamps=[22.0],
            # Slack (chat) in public → MASK_WINDOW
            window_specs=[(20.0, "com.tinyspeck.slackmacgap")],
        )
        img_path = dst / "screenshots" / "22.0.jpg"
        original_brightness = _avg_brightness(img_path)

        _scrub_screenshots_with_policy(
            dst, evaluator, classifier, window_events, [], result
        )

        assert img_path.exists(), "MASK_WINDOW should keep the file, not delete it"
        assert _avg_brightness(img_path) < original_brightness * 0.3

    def test_fallback_deletes_on_corrupt_image(self, tmp_path):
        """MASK_WINDOW on a corrupt file → deleted safely, audit says exclude."""
        dst, evaluator, classifier, window_events, result = self._setup(
            tmp_path,
            timestamps=[22.0],
            window_specs=[(20.0, "com.tinyspeck.slackmacgap")],
        )
        # Overwrite with corrupt data
        corrupt_path = dst / "screenshots" / "22.0.jpg"
        corrupt_path.write_bytes(b"not a jpeg")

        _scrub_screenshots_with_policy(
            dst, evaluator, classifier, window_events, [], result
        )

        assert not corrupt_path.exists(), "Corrupt image should be deleted for safety"
        # Audit must reflect what actually happened (exclude), not what was attempted
        assert len(result.audit_entries) == 1
        assert result.audit_entries[0].action == "exclude"

    def test_communication_surfaces_masked_not_deleted_in_public(self, tmp_path):
        """Email, chat, calendar, video_call: all masked (kept) in public mode."""
        # Each surface at a different timestamp with its representative bundle ID
        surfaces = [
            (10.0, "com.apple.mail"),            # email
            (20.0, "com.tinyspeck.slackmacgap"),  # chat
            (30.0, "com.apple.iCal"),             # calendar
            (40.0, "us.zoom.xos.meeting"),        # video_call
        ]
        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir()
        for ts, _ in surfaces:
            _make_test_jpeg(screenshots_dir / f"{ts}.jpg")

        evaluator = _make_evaluator(mode="public")
        classifier = DefaultContextClassifier()
        window_events = _make_window_events(surfaces)
        result = ScrubResult()

        _scrub_screenshots_with_policy(
            tmp_path, evaluator, classifier, window_events, [], result
        )

        for ts, bundle_id in surfaces:
            img_path = screenshots_dir / f"{ts}.jpg"
            assert img_path.exists(), (
                f"Screenshot for {bundle_id} at t={ts} should be masked, not deleted"
            )

        # Verify audit entries record mask_window, not exclude
        mask_entries = [
            e for e in result.audit_entries if e.action == "mask_window"
        ]
        assert len(mask_entries) == 4

    def test_exclude_still_deletes(self, tmp_path):
        """EXCLUDE (password manager) still deletes — masking doesn't interfere."""
        dst, evaluator, classifier, window_events, result = self._setup(
            tmp_path,
            timestamps=[22.0],
            window_specs=[(20.0, "com.1password.1password")],
        )

        _scrub_screenshots_with_policy(
            dst, evaluator, classifier, window_events, [], result
        )

        assert not (dst / "screenshots" / "22.0.jpg").exists()
