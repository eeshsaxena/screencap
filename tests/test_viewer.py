"""Tests for screencap.viewer module."""

from __future__ import annotations

import time
from pathlib import Path
from unittest import mock

import av
import pytest
from PIL import Image

from screencap.engine.video import VideoWriter
from screencap.viewer import _ensure_single_video, _needs_regeneration


def _write_chunk(path: Path, color: tuple[int, int, int], n_frames: int = 4) -> None:
    """Write a tiny solid-color chunk via VideoWriter (mirrors the recorder)."""
    base = time.time()
    writer = VideoWriter(str(path), width=48, height=48, fps=24)
    for i in range(n_frames):
        writer.write_frame(Image.new("RGB", (48, 48), color=color), base + i / 24)
    writer.close()


class TestEnsureSingleVideo:
    """Tests for _ensure_single_video orchestration (U1)."""

    def test_no_chunks_is_noop(self, tmp_path):
        _ensure_single_video(tmp_path)
        assert not (tmp_path / "video.mp4").exists()

    def test_existing_video_is_noop(self, tmp_path):
        """Idempotent: a present video.mp4 is never overwritten."""
        _write_chunk(tmp_path / "chunk_0000.mp4", (200, 0, 0))
        _write_chunk(tmp_path / "chunk_0001.mp4", (0, 0, 200))
        sentinel = tmp_path / "video.mp4"
        sentinel.write_bytes(b"already here")

        _ensure_single_video(tmp_path)
        assert sentinel.read_bytes() == b"already here"

    def test_single_chunk_symlinks(self, tmp_path):
        """A lone chunk is symlinked, not re-muxed."""
        _write_chunk(tmp_path / "chunk_0000.mp4", (200, 0, 0))
        _ensure_single_video(tmp_path)
        video = tmp_path / "video.mp4"
        assert video.is_symlink()
        assert video.resolve() == (tmp_path / "chunk_0000.mp4").resolve()

    def test_multi_chunk_concats_without_ffmpeg(self, tmp_path, monkeypatch):
        """2+ chunks merge in-process even with no ffmpeg/ffprobe on PATH (R5)."""
        monkeypatch.setenv("PATH", "")
        _write_chunk(tmp_path / "chunk_0000.mp4", (200, 0, 0))
        _write_chunk(tmp_path / "chunk_0001.mp4", (0, 0, 200))

        _ensure_single_video(tmp_path)

        video = tmp_path / "video.mp4"
        assert video.is_file() and not video.is_symlink()
        container = av.open(str(video))
        frames = sum(1 for _ in container.decode(video=0))
        container.close()
        assert frames > 0


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
