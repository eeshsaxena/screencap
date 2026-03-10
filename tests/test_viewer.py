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
    """Tests that open_viewer correctly translates max_events to create_html."""

    def _call_open_viewer(self, tmp_path, **kwargs):
        """Call open_viewer with mocked create_html, return call kwargs."""
        rec_dir = tmp_path / "test-rec"
        rec_dir.mkdir()

        with mock.patch("screencap.viewer.find_db", return_value=rec_dir / "recording.db"):
            with mock.patch("screencap.viewer.subprocess"):
                mock_create = mock.MagicMock(return_value=None)
                fake_module = mock.MagicMock()
                fake_module.create_html = mock_create
                with mock.patch.dict("sys.modules", {"sc_engine": fake_module}):
                    from screencap.viewer import open_viewer
                    open_viewer("test-rec", recordings_dir=tmp_path, **kwargs)
                    return mock_create.call_args

    @pytest.mark.parametrize("input_val, expected", [
        (0, None),       # 0 disables caps
        (200, 200),      # explicit value passed through
    ])
    def test_max_events_translation(self, tmp_path, input_val, expected):
        call = self._call_open_viewer(tmp_path, max_events=input_val)
        assert call.kwargs["max_events"] == expected

    def test_default_caps_at_500(self, tmp_path):
        call = self._call_open_viewer(tmp_path)
        assert call.kwargs["max_events"] == 500
