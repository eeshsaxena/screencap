"""Tests for fragmented MP4 (fMP4) crash-safe video recording."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import av
import pytest

from sc_engine.video import (
    _FRAG_MP4_OPTIONS,
    _is_fragmented_mp4,
    extract_frames,
    get_video_info,
    move_moov_atom,
)


class TestFmp4WriteReadRoundtrip:
    """Test 1: fMP4 write + read round-trip."""

    def test_fmp4_write_read_roundtrip(self, tmp_path):
        """Verify fMP4 output is readable and frames are extractable."""
        output = tmp_path / "test.mp4"
        container = av.open(str(output), mode="w", container_options=_FRAG_MP4_OPTIONS)
        stream = container.add_stream("libx264", rate=24)
        stream.width, stream.height = 100, 100
        stream.pix_fmt = "yuv444p"
        stream.options = {"crf": "0", "preset": "ultrafast", "g": "48"}

        # Write 50 frames (just over 2 seconds)
        for i in range(50):
            frame = av.VideoFrame(100, 100, "rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        container.close()

        # Read back and verify
        read_container = av.open(str(output))
        frames = list(read_container.decode(video=0))
        read_container.close()
        assert len(frames) == 50


# Helper script for crash recovery test: writes fMP4 frames in an UNBOUNDED
# loop, signals readiness via a marker file, then keeps writing until killed.
_WRITER_SCRIPT = '''\
import av, sys, os, time, pathlib

output_path = sys.argv[1]
marker_path = sys.argv[2]
GOP_SIZE = 12

container = av.open(
    output_path, mode="w",
    container_options={
        "movflags": "frag_keyframe+empty_moov",
        "flush_packets": "1",
    },
)
stream = container.add_stream("libx264", rate=24)
stream.width, stream.height = 100, 100
stream.pix_fmt = "yuv444p"
stream.options = {"crf": "0", "preset": "ultrafast", "g": str(GOP_SIZE)}

i = 0
while True:  # unbounded - parent MUST kill us
    frame = av.VideoFrame(100, 100, "rgb24")
    for packet in stream.encode(frame):
        container.mux(packet)
    i += 1
    # After 2+ GOPs flushed, signal readiness
    if i == GOP_SIZE * 2:
        pathlib.Path(marker_path).write_text(str(i))
    # Pace writes so we don't spin CPU
    if i > GOP_SIZE * 2:
        time.sleep(0.01)
'''


class TestFmp4CrashRecovery:
    """Test 2: Crash recovery via subprocess SIGKILL."""

    @pytest.mark.skipif(
        sys.platform == "win32", reason="SIGKILL not available on Windows"
    )
    def test_fmp4_crash_recovery_sigkill(self, tmp_path):
        """Verify fMP4 is partially readable after SIGKILL (real crash simulation)."""
        output = tmp_path / "crash.mp4"
        marker = tmp_path / "ready.marker"

        proc = subprocess.Popen(
            [sys.executable, "-c", _WRITER_SCRIPT, str(output), str(marker)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        try:
            # Poll for the marker file (non-blocking, no readline deadlock)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if marker.exists():
                    break
                if proc.poll() is not None:
                    raise AssertionError(
                        f"Writer exited early (rc={proc.returncode})"
                    )
                time.sleep(0.1)
            assert marker.exists(), "Writer did not signal readiness within 15s"

            # Let a few more GOPs flush to disk beyond the marker
            time.sleep(0.5)

            # Kill with SIGKILL (no cleanup, no __dealloc__, no av_write_trailer)
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
            proc.wait()
            raise

        # Verify partial recovery from the orphaned file
        read_container = av.open(str(output))
        recovered_frames = list(read_container.decode(video=0))
        read_container.close()

        # At least one full GOP recovered (fragments flushed before kill)
        assert len(recovered_frames) >= 12, (
            f"Expected >= 12 recovered frames, got {len(recovered_frames)}"
        )


class TestStandardMp4BackwardCompat:
    """Test 3: Existing standard MP4 files still work."""

    def test_standard_mp4_still_readable(self, tmp_path):
        """Verify existing standard MP4 files still work with extract_frames."""
        # Write a standard (non-fMP4) file
        output = tmp_path / "standard.mp4"
        container = av.open(str(output), mode="w")  # no movflags
        stream = container.add_stream("libx264", rate=24)
        stream.width, stream.height = 100, 100
        stream.pix_fmt = "yuv444p"
        stream.options = {"crf": "0", "preset": "ultrafast"}
        for i in range(24):
            frame = av.VideoFrame(100, 100, "rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        container.close()

        # Read back with extract_frames
        frames = extract_frames(str(output), [0.0, 0.5])
        assert len(frames) == 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_test_video(path, fmp4=False, num_frames=48, fps=24):
    """Write a test video with known frame count and fps."""
    opts = _FRAG_MP4_OPTIONS if fmp4 else {}
    container = av.open(str(path), mode="w", container_options=opts)
    stream = container.add_stream("libx264", rate=fps)
    stream.width, stream.height = 100, 100
    stream.pix_fmt = "yuv444p"
    stream.options = {"crf": "0", "preset": "ultrafast", "g": "12"}
    for _ in range(num_frames):
        frame = av.VideoFrame(100, 100, "rgb24")
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()


# ---------------------------------------------------------------------------
# Test 4: get_video_info duration normalization
# ---------------------------------------------------------------------------


class TestGetVideoInfoDuration:
    """Test 4: get_video_info returns correct duration for both formats."""

    def test_get_video_info_duration_standard_mp4(self, tmp_path):
        """Duration in seconds for a standard MP4 file (stream.duration path)."""
        path = tmp_path / "standard.mp4"
        _write_test_video(path, fmp4=False, num_frames=48, fps=24)

        info = get_video_info(str(path))
        assert info["duration"] is not None
        # 48 frames at 24fps = 2.0 seconds
        assert abs(info["duration"] - 2.0) < 0.1

    def test_get_video_info_duration_fmp4(self, tmp_path):
        """Duration in seconds for a fragmented MP4 file."""
        path = tmp_path / "fmp4.mp4"
        _write_test_video(path, fmp4=True, num_frames=48, fps=24)

        info = get_video_info(str(path))
        assert info["duration"] is not None
        assert abs(info["duration"] - 2.0) < 0.1

    def test_get_video_info_duration_fallback_branch(self, tmp_path):
        """Exercise get_video_info() fallback by forcing stream.duration=None."""
        from types import SimpleNamespace
        from unittest.mock import patch

        path = tmp_path / "fmp4.mp4"
        _write_test_video(path, fmp4=True, num_frames=48, fps=24)

        # Open once to capture real metadata for the shim.
        real_container = av.open(str(path))
        real_stream = real_container.streams.video[0]
        container_duration_us = real_container.duration
        assert container_duration_us is not None, "fMP4 container.duration is None"

        fake_stream = SimpleNamespace(
            duration=None,  # Force fallback branch
            time_base=real_stream.time_base,
            width=real_stream.width,
            height=real_stream.height,
            average_rate=real_stream.average_rate,
            codec_context=SimpleNamespace(
                codec=SimpleNamespace(name=real_stream.codec_context.codec.name),
            ),
            frames=real_stream.frames,
        )
        fake_container = SimpleNamespace(
            streams=SimpleNamespace(video=[fake_stream]),
            duration=container_duration_us,
            close=real_container.close,
        )

        with patch("sc_engine.video.av.open", return_value=fake_container):
            info = get_video_info(str(path))

        assert info["duration"] is not None
        assert abs(info["duration"] - 2.0) < 0.1, (
            f"Expected ~2.0s, got {info['duration']}s "
            f"(raw container.duration={container_duration_us})"
        )


# ---------------------------------------------------------------------------
# Tests for _is_fragmented_mp4 and move_moov_atom guard
# ---------------------------------------------------------------------------


class TestIsFragmentedMp4:
    """Tests for the _is_fragmented_mp4 binary box scanner."""

    def test_detects_fmp4(self, tmp_path):
        """_is_fragmented_mp4 returns True for fMP4 files."""
        path = tmp_path / "fmp4.mp4"
        _write_test_video(path, fmp4=True, num_frames=24)
        assert _is_fragmented_mp4(str(path)) is True

    def test_detects_standard_mp4(self, tmp_path):
        """_is_fragmented_mp4 returns False for standard MP4 files."""
        path = tmp_path / "standard.mp4"
        _write_test_video(path, fmp4=False, num_frames=24)
        assert _is_fragmented_mp4(str(path)) is False

    def test_returns_false_for_nonexistent_file(self):
        """_is_fragmented_mp4 returns False for missing files."""
        assert _is_fragmented_mp4("/nonexistent/path.mp4") is False

    def test_returns_false_for_empty_file(self, tmp_path):
        """_is_fragmented_mp4 returns False for empty files."""
        path = tmp_path / "empty.mp4"
        path.write_bytes(b"")
        assert _is_fragmented_mp4(str(path)) is False


class TestMoveMovAtomGuard:
    """Tests that move_moov_atom skips fMP4 files."""

    def test_skips_fmp4(self, tmp_path):
        """move_moov_atom returns early for fMP4 files without running ffmpeg."""
        from unittest.mock import patch

        path = tmp_path / "fmp4.mp4"
        _write_test_video(path, fmp4=True, num_frames=24)

        with patch("subprocess.run") as mock_run:
            move_moov_atom(str(path))
            mock_run.assert_not_called()


class TestDefaultEncodingRoundtrip:
    """Test that new default encoding settings (CRF 23, faster) produce valid fMP4."""

    def test_videowriter_default_settings_roundtrip(self, tmp_path):
        """VideoWriter with config defaults produces playable fMP4, extract_frames works."""
        from PIL import Image

        from sc_engine.video import VideoWriter, extract_frames

        output = tmp_path / "defaults.mp4"
        width, height = 200, 200

        # Write 10 frames using config defaults (CRF 23, faster, yuv444p)
        writer = VideoWriter(output, width=width, height=height, fps=24)
        source_frames = []
        for i in range(10):
            img = Image.new("RGB", (width, height), color=(50 + i * 20, 100, 150))
            source_frames.append(img)
            writer.write_frame(img, i / 24.0)
        writer.close()

        # File should exist and be smaller than lossless
        assert output.exists()
        file_size = output.stat().st_size
        assert file_size > 0

        # Should be valid fMP4
        assert _is_fragmented_mp4(str(output))

        # extract_frames should work
        timestamps = [i / 24.0 for i in range(10)]
        extracted = extract_frames(output, timestamps, tolerance=0.5)
        assert len(extracted) == 10
        for frame in extracted:
            assert frame.size == (width, height)
