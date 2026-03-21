"""Tests for file-based screenshot storage.

Covers the write path (write_screen_event), read path (get_frame_at,
namer), and samples.py glob updates.
"""

from __future__ import annotations

import base64
import io
import math
import os
import sqlite3
import time
from pathlib import Path
from unittest import mock

import pytest
from PIL import Image


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_image(width=100, height=100, color="red"):
    """Create a small PIL Image for testing."""
    return Image.new("RGB", (width, height), color=color)


def _make_jpeg_bytes(img=None):
    """Return JPEG bytes for a test image."""
    from sc_engine.config import config

    if img is None:
        img = _make_test_image()
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=config.SCREENSHOT_JPEG_QUALITY)
    return buf.getvalue()


def _make_recording_db(
    db_path: Path,
    *,
    with_image_path: bool = False,
    with_blobs: bool = False,
    screenshots: list[tuple[float, str | None, bytes | None]] | None = None,
):
    """Create a minimal recording.db with screenshot table.

    screenshots: list of (timestamp, image_path, png_data) tuples.
    """
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    cur.execute(
        "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, "
        "platform TEXT, task_description TEXT, video_start_time REAL)"
    )
    cur.execute("INSERT INTO recording VALUES (1, ?, 'darwin', '', NULL)", (time.time(),))

    cols = (
        "id INTEGER PRIMARY KEY, recording_timestamp REAL, "
        "recording_id INTEGER, timestamp REAL, png_data BLOB"
    )
    if with_image_path:
        cols += ", image_path TEXT"
    cur.execute(f"CREATE TABLE screenshot ({cols})")

    if screenshots:
        for i, (ts, img_path, png_data) in enumerate(screenshots):
            if with_image_path:
                cur.execute(
                    "INSERT INTO screenshot VALUES (?, ?, 1, ?, ?, ?)",
                    (i + 1, time.time(), ts, png_data, img_path),
                )
            else:
                cur.execute(
                    "INSERT INTO screenshot VALUES (?, ?, 1, ?, ?)",
                    (i + 1, time.time(), ts, png_data),
                )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Phase 1: Write path — write_screen_event
# ---------------------------------------------------------------------------


class TestWriteScreenEvent:
    """Test that write_screen_event saves JPEG files to disk."""

    def test_saves_jpeg_to_screenshots_dir(self, tmp_path):
        """With screenshots_dir set, saves JPEG file and stores path in event_data."""
        from unittest.mock import MagicMock

        from sc_engine.db.models import Recording

        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir()

        img = _make_test_image()
        ts = 1709641234.567000

        event = MagicMock()
        event.type = "screen"
        event.data = img
        event.timestamp = ts

        db = MagicMock()
        recording = MagicMock(spec=Recording)
        perf_q = MagicMock()

        with mock.patch("sc_engine.recorder.config") as mock_config:
            mock_config.RECORD_IMAGES = True
            mock_config.SCREENSHOT_JPEG_QUALITY = 85
            from sc_engine.recorder import write_screen_event

            state = write_screen_event(
                db, recording, event, perf_q,
                screenshots_dir=str(screenshots_dir),
            )

        # Verify file was created
        expected_file = screenshots_dir / f"{ts:.6f}.jpg"
        assert expected_file.exists()

        # Verify file is a valid JPEG
        loaded = Image.open(expected_file)
        assert loaded.format == "JPEG"

        # Verify file permissions (owner read/write only)
        file_stat = os.stat(expected_file)
        assert file_stat.st_mode & 0o777 == 0o600

        # Verify crud.insert_screenshot was called with image_path
        from sc_engine.db import crud

        db_call = db  # the mock
        # The insert_screenshot call should have image_path in event_data
        crud_call = None
        with mock.patch("sc_engine.recorder.crud") as mock_crud, \
             mock.patch("sc_engine.recorder.config") as mock_config:
            mock_config.RECORD_IMAGES = True
            mock_config.SCREENSHOT_JPEG_QUALITY = 85
            state = write_screen_event(
                db, recording, event, perf_q,
                screenshots_dir=str(screenshots_dir),
            )
            args = mock_crud.insert_screenshot.call_args
            event_data = args[0][3]
            assert "image_path" in event_data
            assert event_data["image_path"] == f"screenshots/{ts:.6f}.jpg"
            assert "png_data" not in event_data

        # Verify state is returned for next iteration
        assert state["screenshots_dir"] == str(screenshots_dir)

    def test_falls_back_to_blob_without_screenshots_dir(self, tmp_path):
        """Without screenshots_dir, stores blob in event_data (backward compat)."""
        from unittest.mock import MagicMock

        img = _make_test_image()
        event = MagicMock()
        event.type = "screen"
        event.data = img
        event.timestamp = 1709641234.567000

        with mock.patch("sc_engine.recorder.crud") as mock_crud, \
             mock.patch("sc_engine.recorder.config") as mock_config:
            mock_config.RECORD_IMAGES = True
            mock_config.SCREENSHOT_JPEG_QUALITY = 85
            from sc_engine.recorder import write_screen_event

            state = write_screen_event(
                MagicMock(), MagicMock(), event, MagicMock(),
                screenshots_dir=None,
            )
            args = mock_crud.insert_screenshot.call_args
            event_data = args[0][3]
            assert "png_data" in event_data
            assert "image_path" not in event_data

    def test_skips_invalid_timestamp(self, tmp_path):
        """Skips file write for non-finite or non-positive timestamps."""
        from unittest.mock import MagicMock

        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir()

        for bad_ts in [float("nan"), float("inf"), float("-inf"), 0, -1.0]:
            event = MagicMock()
            event.type = "screen"
            event.data = _make_test_image()
            event.timestamp = bad_ts

            with mock.patch("sc_engine.recorder.crud") as mock_crud, \
                 mock.patch("sc_engine.recorder.config") as mock_config:
                mock_config.RECORD_IMAGES = True
                from sc_engine.recorder import write_screen_event

                write_screen_event(
                    MagicMock(), MagicMock(), event, MagicMock(),
                    screenshots_dir=str(screenshots_dir),
                )
                # Should still insert screenshot (just without image data)
                assert mock_crud.insert_screenshot.called

        # No JPEG files should have been created
        assert list(screenshots_dir.glob("*.jpg")) == []

    def test_io_error_logs_warning_and_continues(self, tmp_path):
        """I/O errors during save log a warning and skip the screenshot."""
        from unittest.mock import MagicMock

        event = MagicMock()
        event.type = "screen"
        event.data = _make_test_image()
        event.timestamp = 1709641234.567000

        with mock.patch("sc_engine.recorder.crud") as mock_crud, \
             mock.patch("sc_engine.recorder.config") as mock_config, \
             mock.patch("sc_engine.recorder.logger") as mock_logger:
            mock_config.RECORD_IMAGES = True
            # Use a non-existent path to trigger OSError
            from sc_engine.recorder import write_screen_event

            state = write_screen_event(
                MagicMock(), MagicMock(), event, MagicMock(),
                screenshots_dir="/nonexistent/path/screenshots",
            )
            # Should not crash
            assert mock_crud.insert_screenshot.called
            assert mock_logger.warning.called

    def test_returns_state_dict(self, tmp_path):
        """write_screen_event returns state dict preserving extra kwargs."""
        from unittest.mock import MagicMock

        event = MagicMock()
        event.type = "screen"
        event.data = _make_test_image()
        event.timestamp = 1709641234.567000

        with mock.patch("sc_engine.recorder.crud"), \
             mock.patch("sc_engine.recorder.config") as mock_config:
            mock_config.RECORD_IMAGES = False
            from sc_engine.recorder import write_screen_event

            state = write_screen_event(
                MagicMock(), MagicMock(), event, MagicMock(),
                screenshots_dir="/some/dir",
                extra_key="preserved",
            )
            assert state["screenshots_dir"] == "/some/dir"
            assert state["extra_key"] == "preserved"


class TestScreenPreCallback:
    """Test screen_pre_callback creates directory and returns state."""

    def test_creates_directory(self, tmp_path):
        from unittest.mock import MagicMock

        from sc_engine.recorder import screen_pre_callback

        screenshots_dir = tmp_path / "screenshots"
        assert not screenshots_dir.exists()

        state = screen_pre_callback(MagicMock(), MagicMock(), screenshots_dir=str(screenshots_dir))

        assert screenshots_dir.exists()
        assert state == {"screenshots_dir": str(screenshots_dir)}

        # Verify directory permissions (owner only)
        dir_stat = os.stat(screenshots_dir)
        assert dir_stat.st_mode & 0o777 == 0o700

    def test_none_screenshots_dir(self):
        from unittest.mock import MagicMock

        from sc_engine.recorder import screen_pre_callback

        state = screen_pre_callback(MagicMock(), MagicMock(), screenshots_dir=None)
        assert state == {"screenshots_dir": None}


# ---------------------------------------------------------------------------
# Phase 2: Read path — get_frame_at with screenshot fallback
# ---------------------------------------------------------------------------


class TestGetFrameAtScreenshotFallback:
    """Test CaptureSession.get_frame_at falls back to screenshot files."""

    def _make_capture_with_screenshots(self, tmp_path):
        """Create a capture dir with file-based screenshots and a DB."""
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
        _make_recording_db(db_path, with_image_path=True, screenshots=screenshot_rows)

        return capture_dir, timestamps

    def test_returns_screenshot_when_no_video(self, tmp_path):
        """get_frame_at returns nearest screenshot image when no video exists."""
        capture_dir, timestamps = self._make_capture_with_screenshots(tmp_path)

        from sc_engine.capture import CaptureSession

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

        from sc_engine.capture import CaptureSession

        session = CaptureSession.load(capture_dir)
        try:
            # Request timestamp between [1] and [2], closer to [2]
            frame = session.get_frame_at(timestamps[1] + 0.7, tolerance=1.0)
            assert frame is not None
        finally:
            session.close()

    def test_returns_none_outside_tolerance(self, tmp_path):
        """get_frame_at returns None when no screenshot is within tolerance."""
        capture_dir, timestamps = self._make_capture_with_screenshots(tmp_path)

        from sc_engine.capture import CaptureSession

        session = CaptureSession.load(capture_dir)
        try:
            # Way outside any screenshot timestamp
            frame = session.get_frame_at(timestamps[-1] + 100.0, tolerance=0.5)
            assert frame is None
        finally:
            session.close()

    def test_returns_none_when_no_screenshots(self, tmp_path):
        """get_frame_at returns None when DB has no screenshot file paths."""
        capture_dir = tmp_path / "capture"
        capture_dir.mkdir()

        db_path = capture_dir / "recording.db"
        _make_recording_db(db_path, with_image_path=True, screenshots=[])

        from sc_engine.capture import CaptureSession

        session = CaptureSession.load(capture_dir)
        try:
            frame = session.get_frame_at(time.time())
            assert frame is None
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Phase 2: Read path — namer with file-based screenshots
# ---------------------------------------------------------------------------


class TestNamerFileScreenshots:
    """Test namer reads from image_path files when png_data is NULL."""

    def test_reads_from_image_path(self, tmp_path):
        """namer extracts screenshots from file paths when available."""
        capture_dir = tmp_path
        screenshots_dir = capture_dir / "screenshots"
        screenshots_dir.mkdir()

        # Create JPEG files
        timestamps = []
        for i in range(3):
            ts = 1709641234.0 + i
            timestamps.append(ts)
            img = _make_test_image()
            img.save(screenshots_dir / f"{ts:.6f}.jpg", format="JPEG", quality=95)

        # Create DB with image_path but no png_data
        db_path = capture_dir / "recording.db"
        screenshot_rows = [
            (ts, f"screenshots/{ts:.6f}.jpg", None) for ts in timestamps
        ]
        _make_recording_db(db_path, with_image_path=True, screenshots=screenshot_rows)

        from screencap.namer import _sample_screenshots_from_db

        result = _sample_screenshots_from_db(db_path)
        assert len(result) == 3
        # Verify they are valid base64-encoded JPEGs
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
        _make_recording_db(db_path, with_image_path=True, screenshots=screenshot_rows)

        from screencap.namer import _sample_screenshots_from_db

        result = _sample_screenshots_from_db(db_path)
        assert len(result) == 3

    def test_handles_old_db_without_image_path_column(self, tmp_path):
        """namer works on old DBs that don't have the image_path column."""
        db_path = tmp_path / "recording.db"

        img = _make_test_image()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        screenshot_rows = [
            (1709641234.0 + i, None, png_bytes) for i in range(3)
        ]
        # No image_path column
        _make_recording_db(db_path, with_image_path=False, screenshots=screenshot_rows)

        from screencap.namer import _sample_screenshots_from_db

        result = _sample_screenshots_from_db(db_path)
        assert len(result) == 3

    def test_prefers_files_over_blobs(self, tmp_path):
        """When both image_path and png_data exist, prefers file-based."""
        capture_dir = tmp_path
        screenshots_dir = capture_dir / "screenshots"
        screenshots_dir.mkdir()

        # Create distinctive JPEG files (green)
        green_img = _make_test_image(color="green")
        ts = 1709641234.0
        green_img.save(screenshots_dir / f"{ts:.6f}.jpg", format="JPEG", quality=95)

        # Create DB with both image_path and png_data (red blob)
        red_img = _make_test_image(color="red")
        buf = io.BytesIO()
        red_img.save(buf, format="PNG")
        red_bytes = buf.getvalue()

        db_path = capture_dir / "recording.db"
        _make_recording_db(
            db_path,
            with_image_path=True,
            screenshots=[(ts, f"screenshots/{ts:.6f}.jpg", red_bytes)],
        )

        from screencap.namer import _sample_screenshots_from_db

        result = _sample_screenshots_from_db(db_path)
        assert len(result) == 1
        # The result should come from the file (green), not the blob (red)
        data = base64.b64decode(result[0])
        img = Image.open(io.BytesIO(data))
        # Green channel should dominate
        r, g, b = img.getpixel((50, 50))
        assert g > r  # green image from file, not red from blob


# ---------------------------------------------------------------------------
# Phase 4: Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Old recordings with png_data blobs still work."""

    def test_screenshot_image_property_with_blobs(self):
        """Screenshot.image property still works with png_data blobs."""
        from sc_engine.db.models import Screenshot

        img = _make_test_image()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        screenshot = Screenshot(png_data=png_bytes)
        assert screenshot.image is not None
        assert screenshot.image.size == (100, 100)

    def test_image_path_column_exists_on_model(self):
        """Screenshot model has image_path column."""
        from sc_engine.db.models import Screenshot

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
            img = _make_test_image()
            img.save(screenshots_dir / f"{i}.jpg", format="JPEG")

        with mock.patch("sc_engine.samples.get_example_path", return_value=tmp_path):
            from sc_engine.samples import get_example_info

            info = get_example_info("test")
            assert info["screenshot_count"] == 3
            assert info["has_screenshots"] is True

    def test_get_example_info_counts_both_png_and_jpg(self, tmp_path):
        """get_example_info counts both .png and .jpg files."""
        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir()

        _make_test_image().save(screenshots_dir / "1.png", format="PNG")
        _make_test_image().save(screenshots_dir / "2.jpg", format="JPEG")

        with mock.patch("sc_engine.samples.get_example_path", return_value=tmp_path):
            from sc_engine.samples import get_example_info

            info = get_example_info("test")
            assert info["screenshot_count"] == 2

    def test_get_example_screenshots_includes_jpg(self, tmp_path):
        """get_example_screenshots returns .jpg files."""
        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir()

        _make_test_image().save(screenshots_dir / "1.jpg", format="JPEG")
        _make_test_image().save(screenshots_dir / "2.png", format="PNG")

        with mock.patch("sc_engine.samples.get_example_path", return_value=tmp_path):
            from sc_engine.samples import get_example_screenshots

            paths = get_example_screenshots("test")
            extensions = {p.suffix for p in paths}
            assert ".jpg" in extensions
            assert ".png" in extensions
            assert len(paths) == 2
