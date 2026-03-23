"""Tests for screencap.viewer module."""

from __future__ import annotations

from unittest import mock

import pytest

from screencap.viewer import _needs_regeneration


class TestNeedsRegeneration:
    """Tests for the _needs_regeneration helper."""

    def test_missing_vs_existing(self, tmp_path):
        viewer = tmp_path / "viewer.html"
        assert _needs_regeneration(viewer, regenerate=False) is True

        viewer.write_text("<html></html>")
        assert _needs_regeneration(viewer, regenerate=False) is False

    def test_regenerate_flag_forces_regen(self, tmp_path):
        viewer = tmp_path / "viewer.html"
        viewer.write_text("<html></html>")
        assert _needs_regeneration(viewer, regenerate=True) is True

    def test_oversized_file_auto_regenerates(self, tmp_path):
        viewer = tmp_path / "viewer.html"
        with mock.patch("screencap.viewer._MAX_VIEWER_SIZE_BYTES", 100):
            viewer.write_bytes(b"x" * 200)
            assert _needs_regeneration(viewer, regenerate=False) is True


class TestOpenViewerMaxEventsWiring:
    """Tests that open_viewer correctly passes max_events to create_html."""

    def _call_open_viewer(self, tmp_path, **kwargs):
        """Call open_viewer with targeted patches on deferred imports."""
        from screencap.viewer import open_viewer

        rec_dir = tmp_path / "test-rec"
        rec_dir.mkdir()

        mock_create = mock.MagicMock(return_value=None)

        with (
            mock.patch("screencap.viewer.resolve_recording_dir", return_value=rec_dir),
            mock.patch("screencap.viewer.find_db", return_value=rec_dir / "recording.db"),
            mock.patch("screencap.viewer.subprocess"),
            mock.patch("screencap.engine.create_html", mock_create),
            mock.patch("screencap.engine.visualize.html.DEFAULT_VIEWER_FRAME_SCALE", 0.5),
            mock.patch("screencap.engine.visualize.html.DEFAULT_VIEWER_FRAME_QUALITY", 75),
        ):
            open_viewer("test-rec", **kwargs)
            return mock_create.call_args

    @pytest.mark.parametrize("input_val, expected", [
        (0, 0),         # 0 passed through (create_html API handles conversion)
        (200, 200),     # explicit value passed through
    ])
    def test_max_events_passthrough(self, tmp_path, input_val, expected):
        call = self._call_open_viewer(tmp_path, max_events=input_val)
        assert call.kwargs["max_events"] == expected

    def test_default_caps_at_500(self, tmp_path):
        call = self._call_open_viewer(tmp_path)
        assert call.kwargs["max_events"] == 500
