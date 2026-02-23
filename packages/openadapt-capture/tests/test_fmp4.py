"""Tests for fragmented MP4 (fMP4) crash-safe video recording."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import av
import pytest

from openadapt_capture.video import _FRAG_MP4_OPTIONS, extract_frames


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
