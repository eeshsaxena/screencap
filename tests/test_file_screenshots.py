"""Tests for file-based screenshot storage.

Covers the write path (write_screen_event), read path (get_frame_at,
namer), and samples.py glob updates.
"""

from __future__ import annotations

import base64
import io
import os
import time
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

import pytest
from PIL import Image

from screencap.engine.config import config
from screencap.engine.recorder import Event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_image(width=100, height=100, color="red"):
    """Create a small PIL Image for testing."""
    return Image.new("RGB", (width, height), color=color)


def _make_capture_db(db_path, *, screenshots=None):
    """Create a recording.db at *db_path* using the real engine API.

    screenshots: list of (timestamp, image_path | None, png_data | None).
    """
    from screencap.engine.db import create_db, crud

    engine, Session = create_db(str(db_path))
    session = Session()

    recording = crud.insert_recording(session, {
        "timestamp": 1000.0,
        "platform": "darwin",
        "monitor_width": 1920,
        "monitor_height": 1080,
        "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })

    if screenshots:
        for ts, image_path, png_data in screenshots:
            event_data = {}
            if image_path is not None:
                event_data["image_path"] = image_path
            if png_data is not None:
                event_data["png_data"] = png_data
            crud.insert_screenshot(session, recording, ts, event_data)

    session.close()
    engine.dispose()


@pytest.fixture(autouse=True)
def _screenshot_config():
    """Set known config values for screenshot tests, restore after."""
    orig_images = config.RECORD_IMAGES
    orig_quality = config.SCREENSHOT_JPEG_QUALITY
    object.__setattr__(config, "RECORD_IMAGES", True)
    object.__setattr__(config, "SCREENSHOT_JPEG_QUALITY", 85)
    yield
    object.__setattr__(config, "RECORD_IMAGES", orig_images)
    object.__setattr__(config, "SCREENSHOT_JPEG_QUALITY", orig_quality)


# ---------------------------------------------------------------------------
# Write path — write_screen_event
# ---------------------------------------------------------------------------


class TestWriteScreenEvent:
    """Test that write_screen_event saves JPEG files to disk."""

    def test_saves_jpeg_to_screenshots_dir(self, recording_db):
        """With screenshots_dir set, saves JPEG file and stores path in DB."""
        from screencap.engine.db.models import Screenshot
        from screencap.engine.recorder import write_screen_event

        screenshots_dir = Path(recording_db.db_path).parent / "screenshots"
        screenshots_dir.mkdir()

        img = _make_test_image()
        ts = 1709641234.567000
        event = Event(timestamp=ts, type="screen", data=img)

        state = write_screen_event(
            recording_db.session, recording_db.recording, event, MagicMock(),
            screenshots_dir=str(screenshots_dir),
        )

        # Verify JPEG file was created with correct permissions
        expected_file = screenshots_dir / f"{ts:.6f}.jpg"
        assert expected_file.exists()
        loaded = Image.open(expected_file)
        assert loaded.format == "JPEG"
        assert os.stat(expected_file).st_mode & 0o777 == 0o600

        # Verify DB row has image_path, not png_data
        row = recording_db.session.query(Screenshot).one()
        assert row.image_path == f"screenshots/{ts:.6f}.jpg"
        assert row.png_data is None

        # Verify state passthrough
        assert state["screenshots_dir"] == str(screenshots_dir)

    def test_falls_back_to_blob_without_screenshots_dir(self, recording_db):
        """Without screenshots_dir, stores JPEG blob in DB."""
        from screencap.engine.db.models import Screenshot
        from screencap.engine.recorder import write_screen_event

        event = Event(timestamp=1709641234.567, type="screen", data=_make_test_image())

        write_screen_event(
            recording_db.session, recording_db.recording, event, MagicMock(),
            screenshots_dir=None,
        )

        row = recording_db.session.query(Screenshot).one()
        assert row.png_data is not None
        assert row.image_path is None

    def test_skips_invalid_timestamp(self, recording_db):
        """Skips file write for non-finite or non-positive timestamps."""
        from screencap.engine.db.models import Screenshot
        from screencap.engine.recorder import write_screen_event

        screenshots_dir = Path(recording_db.db_path).parent / "screenshots"
        screenshots_dir.mkdir()

        for bad_ts in [float("nan"), float("inf"), float("-inf"), 0, -1.0]:
            event = Event(timestamp=bad_ts, type="screen", data=_make_test_image())
            write_screen_event(
                recording_db.session, recording_db.recording, event, MagicMock(),
                screenshots_dir=str(screenshots_dir),
            )

        # No JPEG files should have been created
        assert list(screenshots_dir.glob("*.jpg")) == []
        # But screenshot rows were still inserted
        assert recording_db.session.query(Screenshot).count() == 5

    def test_io_error_continues(self, recording_db):
        """I/O errors during save don't crash — screenshot still inserted."""
        from screencap.engine.db.models import Screenshot
        from screencap.engine.recorder import write_screen_event

        event = Event(timestamp=1709641234.567, type="screen", data=_make_test_image())

        state = write_screen_event(
            recording_db.session, recording_db.recording, event, MagicMock(),
            screenshots_dir="/nonexistent/path/screenshots",
        )

        # Should not crash, screenshot still inserted (without image data)
        row = recording_db.session.query(Screenshot).one()
        assert row.image_path is None
        assert row.png_data is None
        assert state["screenshots_dir"] == "/nonexistent/path/screenshots"

    def test_returns_state_dict(self, recording_db):
        """write_screen_event returns state dict preserving extra kwargs."""
        from screencap.engine.recorder import write_screen_event

        object.__setattr__(config, "RECORD_IMAGES", False)

        event = Event(timestamp=1709641234.567, type="screen", data=_make_test_image())

        state = write_screen_event(
            recording_db.session, recording_db.recording, event, MagicMock(),
            screenshots_dir="/some/dir",
            extra_key="preserved",
        )
        assert state["screenshots_dir"] == "/some/dir"
        assert state["extra_key"] == "preserved"


class TestScreenPreCallback:
    """Test screen_pre_callback creates directory and returns state."""

    def test_creates_directory(self, tmp_path):
        from screencap.engine.recorder import screen_pre_callback

        screenshots_dir = tmp_path / "screenshots"
        assert not screenshots_dir.exists()

        state = screen_pre_callback(MagicMock(), MagicMock(), screenshots_dir=str(screenshots_dir))

        assert screenshots_dir.exists()
        assert state == {"screenshots_dir": str(screenshots_dir)}
        assert os.stat(screenshots_dir).st_mode & 0o777 == 0o700

    def test_none_screenshots_dir(self):
        from screencap.engine.recorder import screen_pre_callback

        state = screen_pre_callback(MagicMock(), MagicMock(), screenshots_dir=None)
        assert state == {"screenshots_dir": None}


# ---------------------------------------------------------------------------
# Read path — get_frame_at with screenshot fallback
# ---------------------------------------------------------------------------


class TestGetFrameAtScreenshotFallback:
    """Test CaptureSession.get_frame_at falls back to screenshot files."""

    def _make_capture_with_screenshots(self, tmp_path):
        """Create a capture dir with file-based screenshots and a real engine DB."""
        capture_dir = tmp_path / "capture"
        capture_dir.mkdir()
        screenshots_dir = capture_dir / "screenshots"
        screenshots_dir.mkdir()

        base_ts = 1709641234.0
        timestamps = []
        for i in range(5):
            ts = base_ts + i
            timestamps.append(ts)
            img = _make_test_image(color=["red", "green", "blue", "yellow", "purple"][i])
            img.save(screenshots_dir / f"{ts:.6f}.jpg", format="JPEG", quality=95)

        db_path = capture_dir / "recording.db"
        screenshot_rows = [
            (ts, f"screenshots/{ts:.6f}.jpg", None) for ts in timestamps
        ]
        _make_capture_db(db_path, screenshots=screenshot_rows)

        return capture_dir, timestamps

    def test_returns_screenshot_when_no_video(self, tmp_path):
        """get_frame_at returns nearest screenshot image when no video exists."""
        capture_dir, timestamps = self._make_capture_with_screenshots(tmp_path)

        from screencap.engine.capture import CaptureSession

        session = CaptureSession.load(capture_dir)
        try:
            frame = session.get_frame_at(timestamps[2])
            assert frame is not None
            assert isinstance(frame, Image.Image)
        finally:
            session.close()

    def test_returns_nearest_screenshot(self, tmp_path):
        """get_frame_at returns the screenshot closest to the requested timestamp."""
        capture_dir, timestamps = self._make_capture_with_screenshots(tmp_path)

        from screencap.engine.capture import CaptureSession

        session = CaptureSession.load(capture_dir)
        try:
            frame = session.get_frame_at(timestamps[1] + 0.7, tolerance=1.0)
            assert frame is not None
        finally:
            session.close()

    def test_returns_none_outside_tolerance(self, tmp_path):
        """get_frame_at returns None when no screenshot is within tolerance."""
        capture_dir, timestamps = self._make_capture_with_screenshots(tmp_path)

        from screencap.engine.capture import CaptureSession

        session = CaptureSession.load(capture_dir)
        try:
            frame = session.get_frame_at(timestamps[-1] + 100.0, tolerance=0.5)
            assert frame is None
        finally:
            session.close()

    def test_returns_none_when_no_screenshots(self, tmp_path):
        """get_frame_at returns None when DB has no screenshot file paths."""
        capture_dir = tmp_path / "capture"
        capture_dir.mkdir()

        db_path = capture_dir / "recording.db"
        _make_capture_db(db_path, screenshots=[])

        from screencap.engine.capture import CaptureSession

        session = CaptureSession.load(capture_dir)
        try:
            frame = session.get_frame_at(time.time())
            assert frame is None
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Read path — namer with file-based screenshots
# ---------------------------------------------------------------------------


class TestNamerFileScreenshots:
    """Test namer reads from image_path files when png_data is NULL."""

    def test_reads_from_image_path(self, tmp_path):
        """namer extracts screenshots from file paths when available."""
        capture_dir = tmp_path
        screenshots_dir = capture_dir / "screenshots"
        screenshots_dir.mkdir()

        timestamps = []
        for i in range(3):
            ts = 1709641234.0 + i
            timestamps.append(ts)
            _make_test_image().save(screenshots_dir / f"{ts:.6f}.jpg", format="JPEG", quality=95)

        db_path = capture_dir / "recording.db"
        screenshot_rows = [
            (ts, f"screenshots/{ts:.6f}.jpg", None) for ts in timestamps
        ]
        _make_capture_db(db_path, screenshots=screenshot_rows)

        from screencap.namer import _sample_screenshots_from_db

        result = _sample_screenshots_from_db(db_path)
        assert len(result) == 3
        for b64 in result:
            data = base64.b64decode(b64)
            img = Image.open(io.BytesIO(data))
            assert img.format == "JPEG"

    def test_falls_back_to_blobs(self, tmp_path):
        """namer falls back to png_data when image_path is not available."""
        db_path = tmp_path / "recording.db"

        img = _make_test_image()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        screenshot_rows = [
            (1709641234.0 + i, None, png_bytes) for i in range(3)
        ]
        _make_capture_db(db_path, screenshots=screenshot_rows)

        from screencap.namer import _sample_screenshots_from_db

        result = _sample_screenshots_from_db(db_path)
        assert len(result) == 3

    def test_prefers_files_over_blobs(self, tmp_path):
        """When both image_path and png_data exist, prefers file-based."""
        capture_dir = tmp_path
        screenshots_dir = capture_dir / "screenshots"
        screenshots_dir.mkdir()

        green_img = _make_test_image(color="green")
        ts = 1709641234.0
        green_img.save(screenshots_dir / f"{ts:.6f}.jpg", format="JPEG", quality=95)

        red_img = _make_test_image(color="red")
        buf = io.BytesIO()
        red_img.save(buf, format="PNG")
        red_bytes = buf.getvalue()

        db_path = capture_dir / "recording.db"
        _make_capture_db(
            db_path,
            screenshots=[(ts, f"screenshots/{ts:.6f}.jpg", red_bytes)],
        )

        from screencap.namer import _sample_screenshots_from_db

        result = _sample_screenshots_from_db(db_path)
        assert len(result) == 1
        data = base64.b64decode(result[0])
        img = Image.open(io.BytesIO(data))
        r, g, b = img.getpixel((50, 50))
        assert g > r  # green image from file, not red from blob


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Old recordings with png_data blobs still work."""

    def test_screenshot_image_property_with_blobs(self):
        """Screenshot.image property still works with png_data blobs."""
        from screencap.engine.db.models import Screenshot

        img = _make_test_image()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        screenshot = Screenshot(png_data=png_bytes)
        assert screenshot.image is not None
        assert screenshot.image.size == (100, 100)

    def test_image_path_column_exists_on_model(self):
        """Screenshot model has image_path column."""
        from screencap.engine.db.models import Screenshot

        assert hasattr(Screenshot, "image_path")


# ---------------------------------------------------------------------------
# samples.py glob updates
# ---------------------------------------------------------------------------


class TestSamplesGlobs:
    """Test that samples.py matches both .png and .jpg files."""

    def test_get_example_info_counts_jpg(self, tmp_path):
        """get_example_info counts .jpg files in screenshots dir."""
        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir()

        for i in range(3):
            _make_test_image().save(screenshots_dir / f"{i}.jpg", format="JPEG")

        with mock.patch("screencap.engine.samples.get_example_path", return_value=tmp_path):
            from screencap.engine.samples import get_example_info

            info = get_example_info("test")
            assert info["screenshot_count"] == 3
            assert info["has_screenshots"] is True

    def test_get_example_info_counts_both_png_and_jpg(self, tmp_path):
        """get_example_info counts both .png and .jpg files."""
        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir()

        _make_test_image().save(screenshots_dir / "1.png", format="PNG")
        _make_test_image().save(screenshots_dir / "2.jpg", format="JPEG")

        with mock.patch("screencap.engine.samples.get_example_path", return_value=tmp_path):
            from screencap.engine.samples import get_example_info

            info = get_example_info("test")
            assert info["screenshot_count"] == 2

    def test_get_example_screenshots_includes_jpg(self, tmp_path):
        """get_example_screenshots returns .jpg files."""
        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir()

        _make_test_image().save(screenshots_dir / "1.jpg", format="JPEG")
        _make_test_image().save(screenshots_dir / "2.png", format="PNG")

        with mock.patch("screencap.engine.samples.get_example_path", return_value=tmp_path):
            from screencap.engine.samples import get_example_screenshots

            paths = get_example_screenshots("test")
            extensions = {p.suffix for p in paths}
            assert ".jpg" in extensions
            assert ".png" in extensions
            assert len(paths) == 2
