"""Tests for selective per-window masking.

Covers the core behavioral contracts:
- CaptureDisposition split decisions (MASK_WINDOW vs EXCLUDE vs ALLOW)
- window_regions_from_geometry: mixed windows, Retina, clipping, multi-monitor
- load_window_geometry: format handling, graceful degradation
- Scrubber integration: selective masking with geometry, fallback without
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from screencap.privacy.actions import PrivacyAction
from screencap.privacy.context import (
    DefaultContextClassifier,
    load_window_geometry,
)
from screencap.privacy.masking import (
    MaskRegion,
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
    RecorderPrivacyFilter,
)

from tests._image_helpers import _avg_brightness

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


def _make_classifier_evaluator(**kwargs):
    evaluator = _make_evaluator(**kwargs)
    classifier = DefaultContextClassifier()
    return classifier, evaluator


# ---------------------------------------------------------------------------
# CaptureDisposition: split decisions for MASK_WINDOW vs EXCLUDE vs ALLOW
# ---------------------------------------------------------------------------


class TestCaptureDisposition:
    def test_mask_window_allows_screen_blocks_video_keystrokes(self):
        """MASK_WINDOW app (Slack): screenshots pass, video and keystrokes blocked."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None
        )

        f.on_window_event({
            "app_bundle_id": "com.tinyspeck.slackmacgap",
            "title": "Slack",
        })

        disp = f.get_capture_disposition()
        assert disp.screen_allowed is True, "MASK_WINDOW should allow screenshots"
        assert disp.video_allowed is False, "MASK_WINDOW should block video"
        assert disp.keystrokes_allowed is False, "MASK_WINDOW should null keystrokes"

    def test_exclude_blocks_everything(self):
        """EXCLUDE app (1Password): screen, video, and keystrokes all blocked."""
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
        """ALLOW app (VS Code): screen, video, and keystrokes all permitted."""
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

    def test_mask_window_to_allow_restores_all_gates(self):
        """Switching from Slack (MASK_WINDOW) to VS Code (ALLOW) restores
        video and keystrokes after hold expires."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None
        )

        f.on_window_event({
            "app_bundle_id": "com.tinyspeck.slackmacgap",
            "title": "Slack",
        })
        f.on_window_event({
            "app_bundle_id": "com.microsoft.VSCode",
            "title": "main.py",
        })

        disp = f.get_capture_disposition()
        assert disp.screen_allowed is True
        assert disp.video_allowed is True
        assert disp.keystrokes_allowed is True


# ---------------------------------------------------------------------------
# window_regions_from_geometry: mask region generation
# ---------------------------------------------------------------------------


class TestWindowRegionsFromGeometry:
    def test_mixed_windows_only_masks_sensitive(self):
        """VS Code beside Slack: only Slack gets a mask region."""
        classifier, evaluator = _make_classifier_evaluator(mode="public")

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
        assert regions[0].width == 400

    def test_retina_scaling_doubles_coordinates(self):
        """Retina display (pixel_ratio=2.0): logical points → physical pixels."""
        classifier, evaluator = _make_classifier_evaluator(mode="public")

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

    def test_partially_offscreen_window_clipped_to_image(self):
        """Window extending beyond display edges is clipped; fully offscreen is skipped."""
        classifier, evaluator = _make_classifier_evaluator(mode="public")

        windows = [
            # Partially offscreen (top-left extends past origin)
            {
                "bundle_id": "com.tinyspeck.slackmacgap",
                "app_name": "Slack",
                "x": -50, "y": -30, "width": 500, "height": 400,
            },
            # Fully offscreen (right of display)
            {
                "bundle_id": "com.apple.mail",
                "app_name": "Mail",
                "x": 900, "y": 0, "width": 400, "height": 300,
            },
        ]

        regions = window_regions_from_geometry(
            windows, 800, 600, 1.0, classifier, evaluator
        )

        assert len(regions) == 1  # only partially-offscreen Slack, not fully-offscreen Mail
        r = regions[0]
        assert r.x == 0     # clipped from -50
        assert r.y == 0     # clipped from -30
        assert r.width == 450   # visible portion: -50+500=450
        assert r.height == 370  # visible portion: -30+400=370

    def test_window_without_bundle_id_skipped(self):
        """Window Server / menu bar items with no bundle_id don't crash."""
        classifier, evaluator = _make_classifier_evaluator(mode="public")

        windows = [
            {"app_name": "Window Server", "x": 0, "y": 0, "width": 100, "height": 100}
        ]

        regions = window_regions_from_geometry(
            windows, 800, 600, 1.0, classifier, evaluator
        )
        assert len(regions) == 0

    def test_display_origin_offset_for_secondary_monitor(self):
        """Secondary monitor at x=1440: window coordinates offset by display origin."""
        classifier, evaluator = _make_classifier_evaluator(mode="public")

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
        assert r.x == 100  # 1540 - 1440
        assert r.y == 100


# ---------------------------------------------------------------------------
# load_window_geometry: DB round-trip and format handling
# ---------------------------------------------------------------------------


class TestLoadWindowGeometry:
    def test_new_format_with_display_bounds(self, tmp_path):
        """New format dict: windows + display_bounds parsed correctly."""
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
        """Old recordings store a plain list — display_origin defaults to (0,0)."""
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

    @pytest.mark.parametrize("setup_db,desc", [
        ("empty_table", "no rows for timestamp"),
        ("no_table", "old recording without window_geometry table"),
        ("corrupt_json", "invalid JSON in window_list_json"),
    ])
    def test_graceful_none_on_missing_or_corrupt_data(self, tmp_path, setup_db, desc):
        """load_window_geometry returns None gracefully for: {desc}."""
        db_path = tmp_path / "test.db"
        if setup_db == "empty_table":
            _create_geometry_db(db_path, [])
        elif setup_db == "no_table":
            conn = sqlite3.connect(str(db_path))
            conn.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY)")
            conn.commit()
            conn.close()
        elif setup_db == "corrupt_json":
            _create_geometry_db(db_path, [(100.0, "not valid json {")])

        assert load_window_geometry(db_path, 100.0) is None


# ---------------------------------------------------------------------------
# Selective masking: pixel-level proof
# ---------------------------------------------------------------------------


class TestSelectiveMaskScreenshot:
    def test_masks_only_specified_region_preserves_rest(self, tmp_path):
        """mask_screenshot with regions darkens only the target area."""
        from PIL import Image

        img_path = _make_test_jpeg(tmp_path / "test.jpg", width=200, height=150)

        regions = [MaskRegion(x=100, y=0, width=100, height=150, label="chat")]
        result = mask_screenshot(img_path, ContextClass.CHAT, regions=regions)

        assert result is True
        img = Image.open(img_path).convert("RGB")
        left_pixel = img.getpixel((10, 10))
        right_pixel = img.getpixel((150, 75))
        img.close()

        assert all(c > 200 for c in left_pixel), f"Unmasked area should be bright: {left_pixel}"
        assert all(c < 50 for c in right_pixel), f"Masked area should be dark: {right_pixel}"


# ---------------------------------------------------------------------------
# Scrubber integration: end-to-end selective masking
# ---------------------------------------------------------------------------


class TestScrubberSelectiveMasking:
    def test_selective_masking_with_geometry(self, tmp_path):
        """Full pipeline: DB geometry → region generation → pixel masking.

        VS Code on left half stays bright, Slack on right half gets masked.
        """
        from PIL import Image

        from screencap.privacy.context import WindowContext
        from screencap.scrubber import ScrubContext, ScrubResult, mask_screenshots

        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir()
        _make_test_jpeg(screenshots_dir / "100.0.jpg", width=200, height=150)

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
            WindowContext(timestamp=99.0, app_bundle_id="com.tinyspeck.slackmacgap", title="Slack")
        ]
        result = ScrubResult()
        ctx = ScrubContext(window_events=window_events, evaluator=evaluator,
                           classifier=classifier, pixel_ratio=1.0)

        mask_screenshots(screenshots_dir, ctx, db_path=db_path, result=result)

        img_path = screenshots_dir / "100.0.jpg"
        assert img_path.exists()

        img = Image.open(img_path).convert("RGB")
        left_pixel = img.getpixel((25, 75))
        right_pixel = img.getpixel((150, 75))
        img.close()

        assert all(c > 200 for c in left_pixel), f"VS Code area should be unmasked: {left_pixel}"
        assert all(c < 50 for c in right_pixel), f"Slack area should be masked: {right_pixel}"

    def test_fallback_to_full_frame_without_geometry(self, tmp_path):
        """No geometry DB → MASK_WINDOW falls back to full-frame masking (not deletion)."""
        from screencap.privacy.context import WindowContext
        from screencap.scrubber import ScrubContext, ScrubResult, mask_screenshots

        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir()
        _make_test_jpeg(screenshots_dir / "100.0.jpg")

        evaluator = _make_evaluator(mode="public")
        classifier = DefaultContextClassifier()
        window_events = [
            WindowContext(timestamp=99.0, app_bundle_id="com.tinyspeck.slackmacgap", title="Slack")
        ]
        result = ScrubResult()
        ctx = ScrubContext(window_events=window_events, evaluator=evaluator,
                           classifier=classifier)

        mask_screenshots(screenshots_dir, ctx, result=result)

        img_path = screenshots_dir / "100.0.jpg"
        assert img_path.exists(), "MASK_WINDOW should keep the file"
        assert _avg_brightness(img_path) < 50, "Should be fully masked"


# ---------------------------------------------------------------------------
# Background masking: ALLOW/TEXT_REDACT foreground + sensitive background
# ---------------------------------------------------------------------------


class TestBackgroundWindowMasking:
    """Tests for masking sensitive background windows when the foreground
    app evaluates to ALLOW or TEXT_REDACT.

    This is the core bug fix: previously, ALLOW foreground caused the
    entire screenshot to be kept as-is, even when Slack/Mail/etc were
    visible in the background.
    """

    def _build_scrub_ctx(self, tmp_path, *, foreground_bundle, foreground_title,
                         geometry_windows, evaluator_kwargs=None,
                         pixel_ratio=1.0, img_w=200, img_h=150):
        """Build the scene + ScrubContext WITHOUT calling mask_screenshots.

        Returns (screenshots_dir, ctx, db_path, result) so a caller can apply
        monkeypatches (e.g. faking the OCR stack) before running the scrub.
        """
        from screencap.privacy.context import WindowContext
        from screencap.scrubber import ScrubContext, ScrubResult

        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir()
        _make_test_jpeg(screenshots_dir / "100.0.jpg", width=img_w, height=img_h)

        db_path = tmp_path / "recording.db"
        geom_data = json.dumps({
            "windows": geometry_windows,
            "display_bounds": [0.0, 0.0, float(img_w), float(img_h)],
        })
        _create_geometry_db(db_path, [(100.0, geom_data)])

        kw = evaluator_kwargs or {}
        evaluator = _make_evaluator(**kw)
        classifier = DefaultContextClassifier()
        window_events = [
            WindowContext(timestamp=99.0, app_bundle_id=foreground_bundle,
                          title=foreground_title)
        ]
        result = ScrubResult()
        ctx = ScrubContext(window_events=window_events, evaluator=evaluator,
                           classifier=classifier, pixel_ratio=pixel_ratio)
        return screenshots_dir, ctx, db_path, result

    def _setup_scrub(self, tmp_path, *, foreground_bundle, foreground_title,
                     geometry_windows, evaluator_kwargs=None,
                     pixel_ratio=1.0, img_w=200, img_h=150):
        """Common setup for background masking tests: build scene then scrub."""
        from screencap.scrubber import mask_screenshots

        screenshots_dir, ctx, db_path, result = self._build_scrub_ctx(
            tmp_path,
            foreground_bundle=foreground_bundle,
            foreground_title=foreground_title,
            geometry_windows=geometry_windows,
            evaluator_kwargs=evaluator_kwargs,
            pixel_ratio=pixel_ratio,
            img_w=img_w,
            img_h=img_h,
        )

        mask_screenshots(screenshots_dir, ctx, db_path=db_path, result=result)
        return screenshots_dir / "100.0.jpg", result

    def test_allow_foreground_masks_sensitive_background(self, tmp_path):
        """ALLOW foreground (VS Code, internal mode) + MASK_WINDOW background
        (banking app): banking region masked, VS Code region preserved."""
        from PIL import Image

        # Internal mode: CODE_EDITOR_TERMINAL → ALLOW, AUTH_FLOW → MASK_WINDOW
        img_path, result = self._setup_scrub(
            tmp_path,
            foreground_bundle="com.microsoft.VSCode",
            foreground_title="main.py",
            evaluator_kwargs={"mode": "internal"},
            geometry_windows=[
                {"bundle_id": "com.microsoft.VSCode", "app_name": "VS Code",
                 "x": 0, "y": 0, "width": 100, "height": 150},
                {"bundle_id": "com.robinhood.Robinhood", "app_name": "Robinhood",
                 "x": 100, "y": 0, "width": 100, "height": 150},
            ],
        )

        assert img_path.exists()
        img = Image.open(img_path).convert("RGB")
        left_pixel = img.getpixel((25, 130))   # VS Code area, away from any label
        right_pixel = img.getpixel((150, 130))  # Robinhood (banking) area, below label
        img.close()

        assert all(c > 200 for c in left_pixel), f"VS Code should be unmasked: {left_pixel}"
        assert all(c < 50 for c in right_pixel), f"Banking app should be masked: {right_pixel}"

        # Audit entry should reflect both foreground decision + background masking
        bg_entries = [e for e in result.audit_entries
                      if "background_windows_masked" in e.reason]
        assert len(bg_entries) == 1
        assert "geometry" in bg_entries[0].evidence_type

    def test_allow_foreground_exclude_background_masks_not_deletes(self, tmp_path):
        """ALLOW foreground + EXCLUDE background (1Password):
        background region masked (not file deleted) — foreground content preserved."""
        from PIL import Image

        # Internal mode: CODE_EDITOR_TERMINAL → ALLOW, PASSWORD_MANAGER → EXCLUDE
        img_path, result = self._setup_scrub(
            tmp_path,
            foreground_bundle="com.microsoft.VSCode",
            foreground_title="main.py",
            evaluator_kwargs={"mode": "internal"},
            geometry_windows=[
                {"bundle_id": "com.microsoft.VSCode", "app_name": "VS Code",
                 "x": 0, "y": 0, "width": 100, "height": 150},
                {"bundle_id": "com.1password.1password", "app_name": "1Password",
                 "x": 100, "y": 0, "width": 100, "height": 150},
            ],
        )

        assert img_path.exists(), "File should NOT be deleted when foreground is ALLOW"
        img = Image.open(img_path).convert("RGB")
        right_pixel = img.getpixel((150, 130))  # 1Password area, below label text
        img.close()
        assert all(c < 50 for c in right_pixel), f"1Password should be masked: {right_pixel}"

    def test_allow_foreground_all_allow_background_unchanged(self, tmp_path):
        """ALLOW foreground + all ALLOW background: screenshot unchanged."""
        # Internal mode: CODE_EDITOR_TERMINAL → ALLOW, ADMIN_CONSOLE → ALLOW
        img_path, result = self._setup_scrub(
            tmp_path,
            foreground_bundle="com.microsoft.VSCode",
            foreground_title="main.py",
            evaluator_kwargs={"mode": "internal"},
            geometry_windows=[
                {"bundle_id": "com.microsoft.VSCode", "app_name": "VS Code",
                 "x": 0, "y": 0, "width": 100, "height": 150},
                {"bundle_id": "com.apple.Preview", "app_name": "Preview",
                 "x": 100, "y": 0, "width": 100, "height": 150},
            ],
        )

        assert img_path.exists()
        assert _avg_brightness(img_path) > 200, "All-ALLOW screenshot should stay bright"
        # No background_windows_masked audit entries
        bg_entries = [e for e in result.audit_entries
                      if "background_windows_masked" in e.reason]
        assert len(bg_entries) == 0

    def test_allow_foreground_no_geometry_unchanged(self, tmp_path):
        """ALLOW foreground + no geometry data: screenshot kept as-is (fail-open)."""
        from screencap.privacy.context import WindowContext
        from screencap.scrubber import ScrubContext, ScrubResult, mask_screenshots

        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir()
        _make_test_jpeg(screenshots_dir / "100.0.jpg")

        # Internal mode: CODE_EDITOR_TERMINAL → ALLOW
        evaluator = _make_evaluator(mode="internal")
        classifier = DefaultContextClassifier()
        window_events = [
            WindowContext(timestamp=99.0, app_bundle_id="com.microsoft.VSCode",
                          title="main.py")
        ]
        result = ScrubResult()
        ctx = ScrubContext(window_events=window_events, evaluator=evaluator,
                           classifier=classifier)

        mask_screenshots(screenshots_dir, ctx, result=result)

        img_path = screenshots_dir / "100.0.jpg"
        assert img_path.exists()
        assert _avg_brightness(img_path) > 200, "No geometry = no masking"

    def test_text_redact_background_masked_on_scrub(self, tmp_path):
        """ALLOW foreground + TEXT_REDACT background (Slack in internal mode):
        Slack region masked because we can't text-redact a partial region."""
        from PIL import Image

        # Internal mode: CODE_EDITOR_TERMINAL → ALLOW, CHAT → TEXT_REDACT
        img_path, result = self._setup_scrub(
            tmp_path,
            foreground_bundle="com.microsoft.VSCode",
            foreground_title="main.py",
            evaluator_kwargs={"mode": "internal"},
            geometry_windows=[
                {"bundle_id": "com.microsoft.VSCode", "app_name": "VS Code",
                 "x": 0, "y": 0, "width": 100, "height": 150},
                {"bundle_id": "com.tinyspeck.slackmacgap", "app_name": "Slack",
                 "x": 100, "y": 0, "width": 100, "height": 150},
            ],
        )

        assert img_path.exists()
        img = Image.open(img_path).convert("RGB")
        left_pixel = img.getpixel((25, 130))   # VS Code area
        right_pixel = img.getpixel((150, 130))  # Slack area
        img.close()

        assert all(c > 200 for c in left_pixel), f"VS Code should be unmasked: {left_pixel}"
        assert all(c < 50 for c in right_pixel), f"Slack (TEXT_REDACT) should be masked: {right_pixel}"

    def test_background_masked_even_when_foreground_ocr_pass_runs(
        self, tmp_path, monkeypatch
    ):
        """SCR-110 regression guard that runs WITHOUT pyobjc-Vision.

        Background-window masking and the foreground OCR pass operate on
        disjoint regions, so a sensitive background window must be masked
        *regardless* of whether the foreground OCR pass ran. The original bug
        gated background masking on ``ocr_ran or cache_hit``, so a sensitive
        background window leaked whenever Vision ran the foreground OCR pass.

        The other tests in this class run with ``ocr_ran=False`` (no Vision),
        so they pass with or without that gate and cannot catch the regression
        off a Mac. Real Vision only runs on macOS, so here we fake the OCR
        stack: a stub ``ocr_mask_screenshot`` forces the foreground OCR pass to
        run (``ocr_ran=True``) while returning no foreground regions, and we
        assert the background window is still masked. This makes the SCR-110
        invariant checkable on every CI run without Vision installed.
        """
        from PIL import Image

        from screencap.scrubber import mask_screenshots

        # Same scene as test_allow_foreground_masks_sensitive_background:
        # foreground VS Code (ALLOW) + background Robinhood (banking → MASK_WINDOW).
        # Build the scene first so we can fake the OCR stack BEFORE mask runs.
        screenshots_dir, ctx, db_path, result = self._build_scrub_ctx(
            tmp_path,
            foreground_bundle="com.microsoft.VSCode",
            foreground_title="main.py",
            evaluator_kwargs={"mode": "internal"},
            geometry_windows=[
                {"bundle_id": "com.microsoft.VSCode", "app_name": "VS Code",
                 "x": 0, "y": 0, "width": 100, "height": 150},
                {"bundle_id": "com.robinhood.Robinhood", "app_name": "Robinhood",
                 "x": 100, "y": 0, "width": 100, "height": 150},
            ],
        )

        # Fake the Vision/OCR stack: VisionOcr() and create_default_pipeline()
        # only need to return non-None so the OCR pass is enabled; the real OCR
        # work is replaced by a stub that records the call and returns no
        # foreground regions (so ocr_ran=True and foreground content is kept).
        ocr_called = {"count": 0}

        class _FakeVisionOcr:
            def __init__(self, *args, **kwargs):
                pass

        def _fake_ocr_mask_screenshot(*args, **kwargs):
            ocr_called["count"] += 1
            return []

        monkeypatch.setattr("screencap.privacy.ocr.VisionOcr", _FakeVisionOcr)
        monkeypatch.setattr(
            "screencap.privacy.create_default_pipeline", lambda: object()
        )
        monkeypatch.setattr(
            "screencap.scrubber.ocr_mask_screenshot", _fake_ocr_mask_screenshot
        )

        mask_screenshots(screenshots_dir, ctx, db_path=db_path, result=result)

        img_path = screenshots_dir / "100.0.jpg"

        # Precondition: the foreground OCR pass actually ran. Without this the
        # test is vacuous (ocr_ran=False) and would not guard SCR-110.
        assert ocr_called["count"] >= 1, (
            "foreground OCR pass did not run; the SCR-110 guard would be vacuous"
        )

        # Invariant: the sensitive background window is masked even though the
        # foreground OCR pass ran on this screenshot.
        assert img_path.exists()
        img = Image.open(img_path).convert("RGB")
        left_pixel = img.getpixel((25, 130))    # VS Code (foreground, ALLOW)
        right_pixel = img.getpixel((150, 130))   # Robinhood (background, MASK_WINDOW)
        img.close()

        assert all(c > 200 for c in left_pixel), f"VS Code should be unmasked: {left_pixel}"
        assert all(c < 50 for c in right_pixel), (
            "Banking app must be masked even when the foreground OCR pass ran "
            f"(SCR-110): {right_pixel}"
        )

        bg_entries = [e for e in result.audit_entries
                      if "background_windows_masked" in e.reason]
        assert len(bg_entries) == 1


# ---------------------------------------------------------------------------
# Z-order-aware masking: foreground ALLOW windows cut through background masks
# ---------------------------------------------------------------------------


class TestZOrderMasking:
    """Tests that non-masked foreground windows are never obscured by
    masks applied to sensitive background windows behind them.

    Uses internal mode where VS Code (CODE_EDITOR_TERMINAL) → ALLOW and
    Slack (CHAT) → TEXT_REDACT. Passes _BG_MASK_ACTIONS to include
    TEXT_REDACT in the mask trigger set (matching scrubber behavior).
    """

    _BG_MASK_ACTIONS = frozenset({
        PrivacyAction.EXCLUDE,
        PrivacyAction.MASK_WINDOW,
        PrivacyAction.MASK_REGION,
        PrivacyAction.TEXT_REDACT,
        PrivacyAction.OCR_FALLBACK,
    })

    def test_foreground_allow_cuts_through_background_mask(self, tmp_path):
        """VS Code (ALLOW) overlapping Slack (TEXT_REDACT): VS Code pixels preserved."""
        from PIL import Image

        classifier, evaluator = _make_classifier_evaluator(mode="internal")

        # Slack covers full area, VS Code overlaps the left half (foreground)
        windows = [
            # Front-to-back order (CGWindowListCopyWindowInfo order)
            {"bundle_id": "com.microsoft.VSCode", "app_name": "VS Code",
             "x": 0, "y": 0, "width": 100, "height": 150},
            {"bundle_id": "com.tinyspeck.slackmacgap", "app_name": "Slack",
             "x": 0, "y": 0, "width": 200, "height": 150},
        ]

        regions = window_regions_from_geometry(
            windows, 200, 150, 1.0, classifier, evaluator,
            mask_actions=self._BG_MASK_ACTIONS,
            respect_z_order=True,
        )

        # Should produce a bitmap-based region
        assert len(regions) == 1
        assert regions[0].label == "z_order_mask"

        # Apply to an image and verify pixels
        img_path = _make_test_jpeg(tmp_path / "test.jpg", width=200, height=150)
        mask_screenshot(img_path, None, regions=regions)

        img = Image.open(img_path).convert("RGB")
        # Left half (VS Code foreground) should be preserved (bright)
        left_pixel = img.getpixel((25, 75))
        # Right half (Slack only, no foreground cover) should be masked (dark)
        right_pixel = img.getpixel((150, 75))
        img.close()

        assert all(c > 200 for c in left_pixel), f"VS Code area should be unmasked: {left_pixel}"
        assert all(c < 50 for c in right_pixel), f"Slack area should be masked: {right_pixel}"

    def test_no_z_order_masks_entire_overlapping_region(self, tmp_path):
        """Without z-order: VS Code + Slack overlap → Slack mask covers VS Code too."""
        from PIL import Image

        classifier, evaluator = _make_classifier_evaluator(mode="internal")

        windows = [
            {"bundle_id": "com.microsoft.VSCode", "app_name": "VS Code",
             "x": 0, "y": 0, "width": 100, "height": 150},
            {"bundle_id": "com.tinyspeck.slackmacgap", "app_name": "Slack",
             "x": 0, "y": 0, "width": 200, "height": 150},
        ]

        regions = window_regions_from_geometry(
            windows, 200, 150, 1.0, classifier, evaluator,
            mask_actions=self._BG_MASK_ACTIONS,
            respect_z_order=False,
        )

        # Without z-order, Slack produces a region covering the full 200px width
        slack_regions = [r for r in regions if r.label == "chat"]
        assert len(slack_regions) == 1
        assert slack_regions[0].width == 200

        img_path = _make_test_jpeg(tmp_path / "test.jpg", width=200, height=150)
        mask_screenshot(img_path, None, regions=regions)

        img = Image.open(img_path).convert("RGB")
        # Left half should also be masked (no z-order awareness)
        left_pixel = img.getpixel((25, 75))
        img.close()
        assert all(c < 50 for c in left_pixel), f"Without z-order, left should be masked: {left_pixel}"

    def test_all_foreground_allow_no_mask_applied(self):
        """All windows are ALLOW: z-order path returns empty list."""
        classifier, evaluator = _make_classifier_evaluator(mode="internal")

        windows = [
            {"bundle_id": "com.microsoft.VSCode", "app_name": "VS Code",
             "x": 0, "y": 0, "width": 200, "height": 150},
            {"bundle_id": "com.apple.Preview", "app_name": "Preview",
             "x": 50, "y": 50, "width": 100, "height": 100},
        ]

        regions = window_regions_from_geometry(
            windows, 200, 150, 1.0, classifier, evaluator,
            mask_actions=self._BG_MASK_ACTIONS,
            respect_z_order=True,
        )
        assert len(regions) == 0

    def test_mask_frame_respects_z_order(self):
        """mask_frame() uses z-order: ALLOW foreground hides MASK_WINDOW background."""
        from PIL import Image

        config = _make_config(mode=PrivacyMode.PUBLIC)
        pf = RecorderPrivacyFilter(config, cloud_intent=True)

        img = Image.new("RGB", (200, 150), (255, 255, 255))
        geometry = {
            "windows": [
                # Front-to-back: VS Code foreground covers left half
                {"bundle_id": "com.microsoft.VSCode", "app_name": "VS Code",
                 "x": 0, "y": 0, "width": 100, "height": 150},
                # Slack background spans full width
                {"bundle_id": "com.tinyspeck.slackmacgap", "app_name": "Slack",
                 "x": 0, "y": 0, "width": 200, "height": 150},
            ],
            "display_bounds": [0, 0, 200, 150],
        }

        pf.mask_frame(img, geometry, 1.0)

        # Left half (VS Code foreground) should be preserved (bright)
        left_pixel = img.getpixel((25, 75))
        assert all(c > 200 for c in left_pixel), (
            f"VS Code area should be unmasked: {left_pixel}"
        )
        # Right half (Slack only, no foreground cover) should be masked (dark)
        right_pixel = img.getpixel((150, 75))
        assert all(c < 50 for c in right_pixel), (
            f"Slack area should be masked: {right_pixel}"
        )
        img.close()
