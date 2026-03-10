"""Tests for screencap.viewer module."""

from __future__ import annotations

from unittest import mock

import pytest

from screencap.viewer import _needs_regeneration


class TestNeedsRegeneration:
    """Tests for the _needs_regeneration helper."""

    def test_missing_file(self, tmp_path):
        viewer = tmp_path / "viewer.html"
        assert _needs_regeneration(viewer, regenerate=False) is True

    def test_existing_small_file(self, tmp_path):
        viewer = tmp_path / "viewer.html"
        viewer.write_text("<html></html>")
        assert _needs_regeneration(viewer, regenerate=False) is False

    def test_regenerate_flag(self, tmp_path):
        viewer = tmp_path / "viewer.html"
        viewer.write_text("<html></html>")
        assert _needs_regeneration(viewer, regenerate=True) is True

    def test_oversized_file_triggers_regeneration(self, tmp_path):
        viewer = tmp_path / "viewer.html"
        with mock.patch("screencap.viewer._MAX_VIEWER_SIZE_BYTES", 100):
            viewer.write_bytes(b"x" * 200)
            assert _needs_regeneration(viewer, regenerate=False) is True

    def test_file_under_threshold_no_regeneration(self, tmp_path):
        viewer = tmp_path / "viewer.html"
        with mock.patch("screencap.viewer._MAX_VIEWER_SIZE_BYTES", 1000):
            viewer.write_bytes(b"x" * 500)
            assert _needs_regeneration(viewer, regenerate=False) is False


class TestOpenViewerParams:
    """Tests for open_viewer parameter passing."""

    def test_regenerate_deletes_existing_viewer(self, tmp_path):
        """--regenerate deletes existing viewer.html before regenerating."""
        from screencap.viewer import open_viewer

        rec_dir = tmp_path / "test-rec"
        rec_dir.mkdir()
        viewer = rec_dir / "viewer.html"
        viewer.write_text("<html>old gigabyte file</html>")

        # Mock find_db to return a recording.db path
        with mock.patch("screencap.viewer.find_db", return_value=rec_dir / "recording.db"):
            with mock.patch("screencap.viewer.subprocess"):
                # Mock the deferred sc_engine import inside open_viewer
                mock_create = mock.MagicMock(return_value=None)
                fake_module = mock.MagicMock()
                fake_module.create_html = mock_create
                with mock.patch.dict("sys.modules", {"sc_engine": fake_module}):
                    open_viewer("test-rec", recordings_dir=tmp_path, regenerate=True)

                    mock_create.assert_called_once()
                    # Should have been called with frame_scale=0.5
                    call_kwargs = mock_create.call_args
                    assert call_kwargs.kwargs.get("frame_scale") == 0.5

    def test_max_events_zero_passes_none(self, tmp_path):
        """--max-events 0 disables capping (passes None to create_html)."""
        from screencap.viewer import open_viewer

        rec_dir = tmp_path / "test-rec"
        rec_dir.mkdir()

        with mock.patch("screencap.viewer.find_db", return_value=rec_dir / "recording.db"):
            with mock.patch("screencap.viewer.subprocess"):
                mock_create = mock.MagicMock(return_value=None)
                fake_module = mock.MagicMock()
                fake_module.create_html = mock_create
                with mock.patch.dict("sys.modules", {"sc_engine": fake_module}):
                    open_viewer(
                        "test-rec", recordings_dir=tmp_path, max_events=0
                    )

                    call_kwargs = mock_create.call_args
                    assert call_kwargs.kwargs.get("max_events") is None

    def test_default_max_events_500(self, tmp_path):
        """Default open_viewer uses max_events=500."""
        from screencap.viewer import open_viewer

        rec_dir = tmp_path / "test-rec"
        rec_dir.mkdir()

        with mock.patch("screencap.viewer.find_db", return_value=rec_dir / "recording.db"):
            with mock.patch("screencap.viewer.subprocess"):
                mock_create = mock.MagicMock(return_value=None)
                fake_module = mock.MagicMock()
                fake_module.create_html = mock_create
                with mock.patch.dict("sys.modules", {"sc_engine": fake_module}):
                    open_viewer("test-rec", recordings_dir=tmp_path)

                    call_kwargs = mock_create.call_args
                    assert call_kwargs.kwargs.get("max_events") == 500
