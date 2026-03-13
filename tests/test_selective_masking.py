"""Tests for selective per-window masking (Phases 1-5).

Covers:
- CaptureDisposition split decisions (Phase 2)
- window_regions_from_geometry mask region generation (Phase 3)
- Retina scaling and display clipping (Phase 3/5)
- load_window_geometry DB round-trip and format handling (Phase 3/5)
- Selective masking in scrubber (Phase 3)
- Multi-monitor coordinate offsets (Phase 5)
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from screencap.privacy.actions import (
    BLOCK_ACTIONS,
    KEYSTROKE_NULL_ACTIONS,
    VIDEO_BLOCK_ACTIONS,
    PrivacyAction,
)
from screencap.privacy.context import (
    DefaultContextClassifier,
    WindowGeometrySnapshot,
    load_window_geometry,
)
from screencap.privacy.masking import (
    MaskRegion,
    MaskStrategy,
    mask_screenshot,
    window_regions_from_geometry,
)
from screencap.privacy.policy import (
    ContextClass,
    DefaultPolicyEvaluator,
    PrivacyConfig,
    PrivacyMode,
    parse_privacy_config,
)
from screencap.privacy.recorder_enforcement import (
    CaptureDisposition,
    RecorderPrivacyFilter,
)

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_jpeg(path: Path, width: int = 200, height: int = 150) -> Path:
    from PIL import Image

    img = Image.new("RGB", (width, height), (255, 255, 255))
    img.save(path, "JPEG")
    return path


def _make_config(**kwargs) -> PrivacyConfig:
    defaults = dict(mode=PrivacyMode.PUBLIC)
    defaults.update(kwargs)
    return PrivacyConfig(**defaults)


def _make_evaluator(**kwargs) -> DefaultPolicyEvaluator:
    cfg = parse_privacy_config({"privacy": kwargs})
    return DefaultPolicyEvaluator(cfg)


def _avg_brightness(path: Path) -> float:
    from PIL import Image

    img = Image.open(path).convert("L")
    pixels = list(img.get_flattened_data())
    return sum(pixels) / len(pixels)


def _create_geometry_db(db_path: Path, rows: list[tuple[float, str]]) -> None:
    """Create a minimal DB with window_geometry table."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE window_geometry ("
        "id INTEGER PRIMARY KEY, "
        "recording_id INTEGER, "
        "recording_timestamp REAL, "
        "screenshot_timestamp REAL, "
        "window_list_json TEXT)"
    )
    for ts, json_data in rows:
        conn.execute(
            "INSERT INTO window_geometry (recording_id, recording_timestamp, "
            "screenshot_timestamp, window_list_json) VALUES (1, 0.0, ?, ?)",
            (ts, json_data),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Phase 2: Action set splitting
# ---------------------------------------------------------------------------


class TestActionSets:
    def test_block_actions_only_exclude(self):
        """BLOCK_ACTIONS should only contain EXCLUDE — MASK_WINDOW no longer
        blocks screenshots."""
        assert BLOCK_ACTIONS == frozenset({PrivacyAction.EXCLUDE})

    def test_keystroke_null_includes_mask_window(self):
        """Keystrokes must be nulled for both EXCLUDE and MASK_WINDOW apps."""
        assert PrivacyAction.EXCLUDE in KEYSTROKE_NULL_ACTIONS
        assert PrivacyAction.MASK_WINDOW in KEYSTROKE_NULL_ACTIONS

    def test_video_block_includes_mask_window(self):
        """Video frames must be blocked for both EXCLUDE and MASK_WINDOW."""
        assert PrivacyAction.EXCLUDE in VIDEO_BLOCK_ACTIONS
        assert PrivacyAction.MASK_WINDOW in VIDEO_BLOCK_ACTIONS


# ---------------------------------------------------------------------------
# Phase 2: CaptureDisposition split decisions
# ---------------------------------------------------------------------------


class TestCaptureDisposition:
    def test_mask_window_allows_screen_blocks_video_keystrokes(self):
        """MASK_WINDOW app: screen passes, video and keystrokes blocked."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None
        )

        # Slack = chat = MASK_WINDOW in public mode
        f.on_window_event({
            "app_bundle_id": "com.tinyspeck.slackmacgap",
            "title": "Slack",
        })

        disp = f.get_capture_disposition()
        assert disp.screen_allowed is True, "MASK_WINDOW should allow screenshots"
        assert disp.video_allowed is False, "MASK_WINDOW should block video"
        assert disp.keystrokes_allowed is False, "MASK_WINDOW should null keystrokes"

    def test_exclude_blocks_everything(self):
        """EXCLUDE app: screen, video, and keystrokes all blocked."""
        config = _make_config(
            exclude_apps=frozenset({"com.1password.1password"}),
        )
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None
        )

        f.on_window_event({
            "app_bundle_id": "com.1password.1password",
            "title": "1Password",
        })

        disp = f.get_capture_disposition()
        assert disp.screen_allowed is False
        assert disp.video_allowed is False
        assert disp.keystrokes_allowed is False

    def test_allow_permits_everything(self):
        """ALLOW app: screen, video, and keystrokes all permitted."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None
        )

        f.on_window_event({
            "app_bundle_id": "com.microsoft.VSCode",
            "title": "main.py",
        })

        disp = f.get_capture_disposition()
        assert disp.screen_allowed is True
        assert disp.video_allowed is True
        assert disp.keystrokes_allowed is True

    def test_is_screen_allowed_backward_compat(self):
        """is_screen_allowed() wraps get_capture_disposition().screen_allowed."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None
        )

        # MASK_WINDOW app — screen allowed
        f.on_window_event({
            "app_bundle_id": "com.tinyspeck.slackmacgap",
            "title": "Slack",
        })
        assert f.is_screen_allowed() is True

    def test_transition_from_mask_window_to_allow(self):
        """Switching from MASK_WINDOW to ALLOW app restores all gates."""
        import time as _time

        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None
        )

        # Enter MASK_WINDOW app
        f.on_window_event({
            "app_bundle_id": "com.tinyspeck.slackmacgap",
            "title": "Slack",
        })
        disp = f.get_capture_disposition()
        assert disp.video_allowed is False

        # Switch to ALLOW app
        f.on_window_event({
            "app_bundle_id": "com.microsoft.VSCode",
            "title": "main.py",
        })

        # After hold expires (hold=0), everything should be allowed
        disp = f.get_capture_disposition()
        assert disp.screen_allowed is True
        assert disp.video_allowed is True
        assert disp.keystrokes_allowed is True


# ---------------------------------------------------------------------------
# Phase 3: window_regions_from_geometry
# ---------------------------------------------------------------------------


class TestWindowRegionsFromGeometry:
    def _make_deps(self, **kwargs):
        evaluator = _make_evaluator(**kwargs)
        classifier = DefaultContextClassifier()
        return classifier, evaluator

    def test_sensitive_window_produces_mask_region(self):
        """A MASK_WINDOW bundle in the window list produces a MaskRegion."""
        classifier, evaluator = self._make_deps(mode="public")

        windows = [
            {
                "bundle_id": "com.tinyspeck.slackmacgap",
                "app_name": "Slack",
                "x": 0, "y": 0, "width": 400, "height": 300,
            }
        ]

        regions = window_regions_from_geometry(
            windows, 800, 600, 1.0, classifier, evaluator
        )

        assert len(regions) == 1
        assert regions[0].x == 0
        assert regions[0].y == 0
        assert regions[0].width == 400
        assert regions[0].height == 300

    def test_allowed_window_produces_no_region(self):
        """An ALLOW bundle (VS Code) should not generate a MaskRegion."""
        classifier, evaluator = self._make_deps(mode="public")

        windows = [
            {
                "bundle_id": "com.microsoft.VSCode",
                "app_name": "Visual Studio Code",
                "x": 0, "y": 0, "width": 800, "height": 600,
            }
        ]

        regions = window_regions_from_geometry(
            windows, 800, 600, 1.0, classifier, evaluator
        )
        assert len(regions) == 0

    def test_mixed_windows_only_masks_sensitive(self):
        """Only sensitive windows get masked; allowed windows pass through."""
        classifier, evaluator = self._make_deps(mode="public")

        windows = [
            {
                "bundle_id": "com.microsoft.VSCode",
                "app_name": "VS Code",
                "x": 0, "y": 0, "width": 400, "height": 600,
            },
            {
                "bundle_id": "com.tinyspeck.slackmacgap",
                "app_name": "Slack",
                "x": 400, "y": 0, "width": 400, "height": 600,
            },
        ]

        regions = window_regions_from_geometry(
            windows, 800, 600, 1.0, classifier, evaluator
        )

        assert len(regions) == 1
        assert regions[0].label == "chat"
        assert regions[0].x == 400

    def test_retina_scaling(self):
        """Window coordinates scaled by pixel_ratio for Retina displays."""
        classifier, evaluator = self._make_deps(mode="public")

        windows = [
            {
                "bundle_id": "com.tinyspeck.slackmacgap",
                "app_name": "Slack",
                "x": 100, "y": 50, "width": 200, "height": 150,
            }
        ]

        regions = window_regions_from_geometry(
            windows, 1600, 1200, 2.0, classifier, evaluator
        )

        assert len(regions) == 1
        r = regions[0]
        assert r.x == 200   # 100 * 2.0
        assert r.y == 100   # 50 * 2.0
        assert r.width == 400   # 200 * 2.0
        assert r.height == 300  # 150 * 2.0

    def test_clipping_to_image_bounds(self):
        """Windows extending beyond image bounds are clipped."""
        classifier, evaluator = self._make_deps(mode="public")

        windows = [
            {
                "bundle_id": "com.tinyspeck.slackmacgap",
                "app_name": "Slack",
                "x": -50, "y": -30, "width": 500, "height": 400,
            }
        ]

        regions = window_regions_from_geometry(
            windows, 800, 600, 1.0, classifier, evaluator
        )

        assert len(regions) == 1
        r = regions[0]
        assert r.x == 0     # clipped from -50
        assert r.y == 0     # clipped from -30
        assert r.width == 450   # -50 + 500 = 450 (visible portion)
        assert r.height == 370  # -30 + 400 = 370 (visible portion)

    def test_window_entirely_outside_skipped(self):
        """A window entirely outside the screenshot bounds is skipped."""
        classifier, evaluator = self._make_deps(mode="public")

        windows = [
            {
                "bundle_id": "com.tinyspeck.slackmacgap",
                "app_name": "Slack",
                "x": 900, "y": 0, "width": 400, "height": 300,
            }
        ]

        regions = window_regions_from_geometry(
            windows, 800, 600, 1.0, classifier, evaluator
        )
        assert len(regions) == 0

    def test_window_without_bundle_id_skipped(self):
        """Windows with no bundle_id are silently skipped."""
        classifier, evaluator = self._make_deps(mode="public")

        windows = [
            {"app_name": "Unknown", "x": 0, "y": 0, "width": 100, "height": 100}
        ]

        regions = window_regions_from_geometry(
            windows, 800, 600, 1.0, classifier, evaluator
        )
        assert len(regions) == 0

    def test_display_origin_offset(self):
        """Multi-monitor: window coordinates offset by display origin."""
        classifier, evaluator = self._make_deps(mode="public")

        # Display origin at (1440, 0) — secondary monitor to the right
        windows = [
            {
                "bundle_id": "com.tinyspeck.slackmacgap",
                "app_name": "Slack",
                "x": 1540, "y": 100, "width": 400, "height": 300,
            }
        ]

        regions = window_regions_from_geometry(
            windows, 800, 600, 1.0, classifier, evaluator,
            display_origin=(1440.0, 0.0),
        )

        assert len(regions) == 1
        r = regions[0]
        # 1540 - 1440 = 100
        assert r.x == 100
        assert r.y == 100

    def test_exclude_window_also_produces_region(self):
        """EXCLUDE windows (password managers) also get mask regions.

        Both BLOCK_ACTIONS and KEYSTROKE_NULL_ACTIONS feed the mask set,
        so EXCLUDE windows are masked if they appear in window geometry."""
        classifier, evaluator = self._make_deps(mode="public")

        windows = [
            {
                "bundle_id": "com.1password.1password",
                "app_name": "1Password",
                "x": 0, "y": 0, "width": 300, "height": 200,
            }
        ]

        regions = window_regions_from_geometry(
            windows, 800, 600, 1.0, classifier, evaluator
        )
        assert len(regions) == 1


# ---------------------------------------------------------------------------
# Phase 3/5: load_window_geometry DB round-trip
# ---------------------------------------------------------------------------


class TestLoadWindowGeometry:
    def test_new_format_with_display_bounds(self, tmp_path):
        """New format: dict with windows + display_bounds."""
        db_path = tmp_path / "test.db"
        data = json.dumps({
            "windows": [
                {"bundle_id": "com.apple.mail", "x": 0, "y": 0,
                 "width": 800, "height": 600}
            ],
            "display_bounds": [1440.0, 0.0, 2560.0, 1440.0],
        })
        _create_geometry_db(db_path, [(100.0, data)])

        geom = load_window_geometry(db_path, 100.0)

        assert geom is not None
        assert len(geom.windows) == 1
        assert geom.windows[0]["bundle_id"] == "com.apple.mail"
        assert geom.display_origin == (1440.0, 0.0)

    def test_legacy_format_plain_list(self, tmp_path):
        """Legacy format: plain list of window dicts."""
        db_path = tmp_path / "test.db"
        data = json.dumps([
            {"bundle_id": "com.apple.mail", "x": 0, "y": 0,
             "width": 800, "height": 600}
        ])
        _create_geometry_db(db_path, [(100.0, data)])

        geom = load_window_geometry(db_path, 100.0)

        assert geom is not None
        assert len(geom.windows) == 1
        assert geom.display_origin == (0.0, 0.0)

    def test_missing_timestamp_returns_none(self, tmp_path):
        """No geometry for given timestamp → None."""
        db_path = tmp_path / "test.db"
        _create_geometry_db(db_path, [])

        assert load_window_geometry(db_path, 999.0) is None

    def test_no_table_returns_none(self, tmp_path):
        """Old recording without window_geometry table → None."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        assert load_window_geometry(db_path, 100.0) is None

    def test_corrupt_json_returns_none(self, tmp_path):
        """Invalid JSON in window_list_json → None."""
        db_path = tmp_path / "test.db"
        _create_geometry_db(db_path, [(100.0, "not valid json {")])

        assert load_window_geometry(db_path, 100.0) is None

    def test_display_bounds_default_when_missing(self, tmp_path):
        """New format dict without display_bounds → origin defaults to (0,0)."""
        db_path = tmp_path / "test.db"
        data = json.dumps({
            "windows": [
                {"bundle_id": "com.apple.mail", "x": 0, "y": 0,
                 "width": 800, "height": 600}
            ],
        })
        _create_geometry_db(db_path, [(100.0, data)])

        geom = load_window_geometry(db_path, 100.0)

        assert geom is not None
        assert geom.display_origin == (0.0, 0.0)


# ---------------------------------------------------------------------------
# Phase 3: Selective masking in mask_screenshot
# ---------------------------------------------------------------------------


class TestSelectiveMaskScreenshot:
    def test_masks_only_specified_regions(self, tmp_path):
        """mask_screenshot with regions only darkens the specified area."""
        from PIL import Image

        img_path = _make_test_jpeg(tmp_path / "test.jpg", width=200, height=150)

        # Mask only the right half
        regions = [MaskRegion(x=100, y=0, width=100, height=150, label="chat")]
        result = mask_screenshot(
            img_path, ContextClass.CHAT, regions=regions
        )

        assert result is True
        img = Image.open(img_path).convert("RGB")

        # Left side (unmasked) should still be bright white
        left_pixel = img.getpixel((10, 10))
        assert all(c > 200 for c in left_pixel), f"Left pixel should be bright: {left_pixel}"

        # Right side (masked) should be near-black
        right_pixel = img.getpixel((150, 75))
        assert all(c < 50 for c in right_pixel), f"Right pixel should be dark: {right_pixel}"
        img.close()

    def test_empty_regions_returns_false(self, tmp_path):
        """Empty regions list → no masking applied, returns False."""
        img_path = _make_test_jpeg(tmp_path / "test.jpg")

        result = mask_screenshot(
            img_path, ContextClass.CHAT, regions=[]
        )
        assert result is False

    def test_none_regions_falls_through_to_strategy(self, tmp_path):
        """regions=None uses the strategy-based path."""
        img_path = _make_test_jpeg(tmp_path / "test.jpg")

        # CHAT in public → FULL_WINDOW strategy from _SURFACE_STRATEGY
        result = mask_screenshot(
            img_path, ContextClass.CHAT, regions=None
        )
        assert result is True
        assert _avg_brightness(img_path) < 50  # full window masked → very dark


# ---------------------------------------------------------------------------
# Phase 3: Scrubber selective masking integration
# ---------------------------------------------------------------------------


class TestScrubberSelectiveMasking:
    def test_selective_masking_with_geometry(self, tmp_path):
        """Scrubber uses stored geometry for selective masking when available."""
        from screencap.privacy.context import WindowContext
        from screencap.scrubber import ScrubResult, _scrub_screenshots_with_policy

        # Set up recording dir with screenshot
        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir()
        _make_test_jpeg(screenshots_dir / "100.0.jpg", width=200, height=150)

        # Set up DB with geometry: Slack on the right half, VS Code on the left
        db_path = tmp_path / "recording.db"
        geom_data = json.dumps({
            "windows": [
                {
                    "bundle_id": "com.microsoft.VSCode",
                    "app_name": "VS Code",
                    "x": 0, "y": 0, "width": 100, "height": 150,
                },
                {
                    "bundle_id": "com.tinyspeck.slackmacgap",
                    "app_name": "Slack",
                    "x": 100, "y": 0, "width": 100, "height": 150,
                },
            ],
            "display_bounds": [0.0, 0.0, 200.0, 150.0],
        })
        _create_geometry_db(db_path, [(100.0, geom_data)])

        evaluator = _make_evaluator(mode="public")
        classifier = DefaultContextClassifier()
        window_events = [
            WindowContext(
                timestamp=99.0,
                app_bundle_id="com.tinyspeck.slackmacgap",
                title="Slack",
            )
        ]
        result = ScrubResult()

        _scrub_screenshots_with_policy(
            tmp_path, evaluator, classifier, window_events, result,
            db_path=db_path, pixel_ratio=1.0,
        )

        # File should still exist (not deleted)
        img_path = screenshots_dir / "100.0.jpg"
        assert img_path.exists()

        # Left half (VS Code) should remain bright, right half (Slack) should be dark
        from PIL import Image
        img = Image.open(img_path).convert("RGB")
        left_pixel = img.getpixel((25, 75))
        right_pixel = img.getpixel((150, 75))
        img.close()

        assert all(c > 200 for c in left_pixel), (
            f"VS Code area should be unmasked: {left_pixel}"
        )
        assert all(c < 50 for c in right_pixel), (
            f"Slack area should be masked: {right_pixel}"
        )

    def test_fallback_to_full_frame_without_geometry(self, tmp_path):
        """Without geometry DB, MASK_WINDOW falls back to full-frame masking."""
        from screencap.privacy.context import WindowContext
        from screencap.scrubber import ScrubResult, _scrub_screenshots_with_policy

        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir()
        _make_test_jpeg(screenshots_dir / "100.0.jpg")

        evaluator = _make_evaluator(mode="public")
        classifier = DefaultContextClassifier()
        window_events = [
            WindowContext(
                timestamp=99.0,
                app_bundle_id="com.tinyspeck.slackmacgap",
                title="Slack",
            )
        ]
        result = ScrubResult()

        # No db_path → no selective masking → full-frame fallback
        _scrub_screenshots_with_policy(
            tmp_path, evaluator, classifier, window_events, result,
        )

        img_path = screenshots_dir / "100.0.jpg"
        assert img_path.exists(), "MASK_WINDOW should keep the file"
        assert _avg_brightness(img_path) < 50, "Should be fully masked"


# ---------------------------------------------------------------------------
# Phase 5: WindowGeometrySnapshot dataclass
# ---------------------------------------------------------------------------


class TestWindowGeometrySnapshot:
    def test_defaults(self):
        snap = WindowGeometrySnapshot(windows=[])
        assert snap.windows == []
        assert snap.display_origin == (0.0, 0.0)

    def test_custom_origin(self):
        snap = WindowGeometrySnapshot(
            windows=[{"bundle_id": "test"}],
            display_origin=(1440.0, 0.0),
        )
        assert snap.display_origin == (1440.0, 0.0)

    def test_frozen(self):
        snap = WindowGeometrySnapshot(windows=[])
        with pytest.raises(AttributeError):
            snap.display_origin = (100.0, 100.0)  # type: ignore[misc]
