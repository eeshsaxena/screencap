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

    def test_multi_chunk_preserves_idle_gap_with_metadata(self, tmp_path):
        """recording.db + manifests → absolute offsets keep the inter-chunk idle
        gap, so a 2nd-chunk wall-clock lookup resolves (SCR-98, full caller path).

        This is the production fix path: _ensure_single_video reads
        video_start_time (DB) and each chunk_start (manifest), builds per-chunk
        offsets, and the merged timeline tracks wall-clock. Without it, the
        2nd-chunk lookup raised "no frame within tolerance".
        """
        import json
        import sqlite3

        from screencap.engine.video import ChunkedVideoWriter, extract_frame

        base = time.time()
        writer = ChunkedVideoWriter(
            output_dir=tmp_path, width=48, height=48, chunk_duration=1.0, fps=24
        )
        for dt in (0.0, 0.5):  # chunk 0: red
            writer.write_frame(Image.new("RGB", (48, 48), (220, 0, 0)), base + dt)
        for dt in (3.0, 3.5):  # ~2.5s gap → chunk 1: blue
            writer.write_frame(Image.new("RGB", (48, 48), (0, 0, 220)), base + dt)
        writer.close()
        assert len(sorted(tmp_path.glob("chunk_*.mp4"))) == 2

        # Minimal recording row carrying the anchor get_frame_at subtracts.
        conn = sqlite3.connect(str(tmp_path / "recording.db"))
        conn.execute("CREATE TABLE recording (video_start_time REAL, timestamp REAL)")
        conn.execute("INSERT INTO recording VALUES (?, ?)", (base, base))
        conn.commit()
        conn.close()
        # Per-chunk manifests carrying each chunk's wall-clock start.
        for idx, chunk_start in ((0, base + 0.0), (1, base + 3.0)):
            (tmp_path / f"chunk_{idx:04d}_manifest.json").write_text(
                json.dumps({"chunk_start": chunk_start, "chunk_end": chunk_start + 0.5})
            )

        _ensure_single_video(tmp_path)

        out = tmp_path / "video.mp4"
        assert out.is_file() and not out.is_symlink()
        # 2nd-chunk event at wall-relative t=3.0 resolves (pre-fix: raised) to blue.
        frame = extract_frame(out, 3.0, tolerance=0.5).convert("RGB")
        r, _, b = frame.getpixel((24, 24))[:3]
        assert b > r, f"2nd-chunk lookup should be blue, got rgb r={r} b={b}"


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
