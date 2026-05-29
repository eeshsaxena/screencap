"""Tests for video module."""

import time
from pathlib import Path

import av
import pytest
from PIL import Image

from screencap.engine import utils
from screencap.engine.video import (
    ChunkedVideoWriter,
    VideoWriter,
    concat_video_chunks,
    extract_frame,
    initialize_video_writer,
    needs_pixfmt_remediation,
    read_pixel_format,
    write_video_frame,
)


def _write_chunk(
    path: Path,
    color: tuple[int, int, int],
    n_frames: int = 5,
    fps: int = 24,
    size: int = 64,
) -> int:
    """Write a solid-color chunk via VideoWriter (yuv444p, bf=0, like the recorder).

    Returns the number of frames actually decodable from the written file
    (VideoWriter.close adds a trailing key frame, so this is >= n_frames).
    """
    base = time.time()
    writer = VideoWriter(str(path), width=size, height=size, fps=fps)
    for i in range(n_frames):
        writer.write_frame(Image.new("RGB", (size, size), color=color), base + i / fps)
    writer.close()

    container = av.open(str(path))
    try:
        count = sum(1 for _ in container.decode(video=0))
    finally:
        container.close()
    return count


def _dominant_channel(img: "Image.Image") -> str:
    """Return 'R', 'G', or 'B' for the largest channel at the image center."""
    r, g, b = img.convert("RGB").getpixel((img.width // 2, img.height // 2))[:3]
    return "RGB"[max(range(3), key=[r, g, b].__getitem__)]


@pytest.fixture(autouse=True)
def _init_timestamp():
    """Ensure utils timestamp system is initialized."""
    utils.set_start_time(time.time())


class TestWriteVideoFrame:
    """Tests for write_video_frame."""

    def test_write_frame_basic(self, tmp_path):
        """Test writing a basic video frame."""
        output = tmp_path / "test.mp4"
        container, stream, start_ts = initialize_video_writer(
            str(output), 100, 100
        )
        img = Image.new("RGB", (100, 100), color="red")
        last_pts = write_video_frame(
            container, stream, img, start_ts + 0.1, start_ts, 0
        )
        assert last_pts > 0
        container.close()

    def test_write_frame_force_key_frame(self, tmp_path):
        """Test writing a video frame with force_key_frame=True."""
        output = tmp_path / "test.mp4"
        container, stream, start_ts = initialize_video_writer(
            str(output), 100, 100
        )
        img = Image.new("RGB", (100, 100), color="blue")
        last_pts = write_video_frame(
            container, stream, img, start_ts + 0.1, start_ts, 0,
            force_key_frame=True,
        )
        assert last_pts > 0
        container.close()

    def test_pts_starts_at_zero_with_delayed_first_frame(self, tmp_path):
        """Legacy API: first frame arrives 30s late but PTS should start at 0."""
        output = tmp_path / "test.mp4"
        base = time.time()
        container, stream, start_ts = initialize_video_writer(
            str(output), 100, 100
        )
        last_pts = 0
        for i in range(5):
            img = Image.new("RGB", (100, 100), color=(i * 50, 0, 0))
            last_pts = write_video_frame(
                container, stream, img, start_ts + 30.0 + i * 0.5, start_ts + 30.0, last_pts
            )
        for packet in stream.encode():
            container.mux(packet)
        container.close()

        read_container = av.open(str(output))
        first_frame = next(read_container.decode(video=0))
        first_pts_sec = float(first_frame.pts * first_frame.time_base)
        read_container.close()

        assert first_pts_sec < 1.0, (
            f"First frame PTS {first_pts_sec:.3f}s should be near 0, not offset"
        )

    def test_pict_type_enum(self):
        """Test that PictureType.I is valid for pict_type assignment."""
        frame = av.VideoFrame(100, 100, "rgb24")
        frame.pict_type = av.video.frame.PictureType.I
        assert frame.pict_type == av.video.frame.PictureType.I


class TestVideoWriterPTS:
    """Verify PTS starts at 0 and is monotonically increasing."""

    def test_pts_starts_at_zero_with_delayed_first_frame(self, tmp_path):
        """Action-gated: first frame arrives 30s after recording start but PTS=0."""
        output = tmp_path / "test.mp4"
        writer = VideoWriter(str(output), width=100, height=100)

        base = time.time()
        # Simulate 30s idle then 5 frames of activity
        for i in range(5):
            img = Image.new("RGB", (100, 100), color=(i * 50, 0, 0))
            writer.write_frame(img, base + 30.0 + i * 0.5)
        writer.close()

        # Verify start_time is near 0, not 30
        container = av.open(str(output))
        stream = container.streams.video[0]
        first_frame = next(container.decode(stream))
        first_pts_sec = float(first_frame.pts * first_frame.time_base)
        container.close()

        assert first_pts_sec < 1.0, (
            f"First frame PTS {first_pts_sec:.3f}s should be near 0, not offset"
        )

    def test_pts_monotonically_increasing_display_order(self, tmp_path):
        """Decoded frames have monotonically increasing PTS."""
        output = tmp_path / "test.mp4"
        writer = VideoWriter(str(output), width=100, height=100)

        base = time.time()
        for i in range(10):
            img = Image.new("RGB", (100, 100), color=(i * 25, 0, 0))
            writer.write_frame(img, base + i * 0.5)
        writer.close()

        container = av.open(str(output))
        pts_values = []
        for frame in container.decode(video=0):
            pts_values.append(frame.pts)
        container.close()

        # 10 written + 1 final key frame from close()
        assert len(pts_values) >= 10
        for i in range(1, len(pts_values)):
            assert pts_values[i] > pts_values[i - 1], (
                f"PTS not monotonic at frame {i}: {pts_values[i-1]} >= {pts_values[i]}"
            )

    def test_no_bframes_in_output(self, tmp_path):
        """Verify B-frames are disabled (no bi-directional prediction)."""
        output = tmp_path / "test.mp4"
        writer = VideoWriter(str(output), width=100, height=100)

        base = time.time()
        for i in range(10):
            img = Image.new("RGB", (100, 100), color=(i * 25, 0, 0))
            writer.write_frame(img, base + i * 0.5)
        writer.close()

        # With bf=0, every packet should have DTS == PTS (no reorder delay)
        container = av.open(str(output))
        stream = container.streams.video[0]
        for packet in container.demux(stream):
            if packet.pts is not None and packet.dts is not None:
                assert packet.pts == packet.dts, (
                    f"B-frame detected: PTS={packet.pts} != DTS={packet.dts}"
                )
        container.close()


class TestChunkedVideoWriterPTS:
    """Verify chunked writer produces correct PTS per chunk."""

    def test_chunk_pts_starts_at_zero(self, tmp_path):
        """Each chunk should have PTS starting near 0."""
        writer = ChunkedVideoWriter(
            output_dir=tmp_path, width=100, height=100,
            chunk_duration=2.0,
        )
        base = time.time()
        # Write frames with a 30s idle offset, spanning two chunks
        for i in range(10):
            img = Image.new("RGB", (100, 100), color=(i * 25, 0, 0))
            writer.write_frame(img, base + 30.0 + i * 0.5)
        writer.close()

        for chunk_path in sorted(tmp_path.glob("chunk_*.mp4")):
            container = av.open(str(chunk_path))
            first_frame = next(container.decode(video=0))
            first_pts_sec = float(first_frame.pts * first_frame.time_base)
            container.close()
            assert first_pts_sec < 1.0, (
                f"{chunk_path.name}: first PTS={first_pts_sec:.3f}s, expected near 0"
            )


def _video_writer_subprocess(output_dir, frame_q, chunk_q, result_q, terminate, started):
    """Subprocess target: mimics write_events loop with ChunkedVideoWriter.

    Must be at module level for macOS spawn-mode pickling.
    """
    try:
        writer = ChunkedVideoWriter(
            output_dir=output_dir, width=100, height=100,
            chunk_duration=0.5,  # 0.5s chunks
            chunk_rotate_q=chunk_q,
        )
        started.set()
        while not terminate.is_set():
            try:
                img, ts = frame_q.get(timeout=0.05)
            except Exception:
                continue
            # This is the call that can crash during rotation
            writer.write_frame(img, ts)
        writer.close()
        result_q.put({"status": "ok"})
    except Exception as e:
        import traceback
        result_q.put({
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc(),
        })


class TestChunkedVideoWriterRotation:
    """Verify ChunkedVideoWriter rotates chunks and continues recording."""

    def test_rotation_continues_writing_action_gated(self, tmp_path):
        """Frames written after chunk rotation produce a valid second chunk.

        Reproduces the action-gated pattern: sparse frames with gaps.
        The writer must rotate at the chunk boundary and continue writing
        to the new chunk without crashing.
        """
        from multiprocessing import Queue

        chunk_q = Queue()
        writer = ChunkedVideoWriter(
            output_dir=tmp_path, width=100, height=100,
            chunk_duration=2.0,  # 2-second chunks for fast test
            chunk_rotate_q=chunk_q,
        )
        base = time.time()

        # Chunk 0: 3 frames in first 1.5s (action-gated: sparse)
        for i in range(3):
            img = Image.new("RGB", (100, 100), color=(i * 80, 0, 0))
            writer.write_frame(img, base + i * 0.5)

        # Chunk 1: frame arrives at 2.5s (past boundary) — triggers rotation
        img_after = Image.new("RGB", (100, 100), color=(0, 255, 0))
        writer.write_frame(img_after, base + 2.5)

        # More frames in chunk 1
        for i in range(2):
            img = Image.new("RGB", (100, 100), color=(0, i * 100, 0))
            writer.write_frame(img, base + 3.0 + i * 0.5)

        writer.close()

        # Verify both chunks exist and are playable
        chunks = sorted(tmp_path.glob("chunk_*.mp4"))
        assert len(chunks) == 2, f"Expected 2 chunks, got {len(chunks)}: {chunks}"

        for chunk_path in chunks:
            container = av.open(str(chunk_path))
            frames = list(container.decode(video=0))
            container.close()
            assert len(frames) > 0, f"{chunk_path.name} has no frames"

        # Verify rotation event was emitted
        msg = chunk_q.get(timeout=1)
        assert msg["type"] == "chunk_rotated"
        assert msg["completed_index"] == 0

    def test_rotation_with_large_idle_gap(self, tmp_path):
        """Action-gated: long idle gap then activity should rotate cleanly.

        Simulates a user who is idle for 10 seconds (far past the 2s chunk
        boundary), then resumes activity. The writer must rotate and the
        new chunk's PTS should start near 0.
        """
        writer = ChunkedVideoWriter(
            output_dir=tmp_path, width=100, height=100,
            chunk_duration=2.0,
        )
        base = time.time()

        # Single frame in chunk 0
        writer.write_frame(
            Image.new("RGB", (100, 100), color="red"), base
        )

        # 10-second idle gap, then activity — crosses chunk boundary
        writer.write_frame(
            Image.new("RGB", (100, 100), color="blue"), base + 10.0
        )

        writer.close()

        chunks = sorted(tmp_path.glob("chunk_*.mp4"))
        assert len(chunks) == 2, f"Expected 2 chunks, got {len(chunks)}: {chunks}"

        # Second chunk's PTS should start near 0, not at 10s
        container = av.open(str(chunks[1]))
        first_frame = next(container.decode(video=0))
        pts_sec = float(first_frame.pts * first_frame.time_base)
        container.close()
        assert pts_sec < 1.0, f"Chunk 1 PTS starts at {pts_sec:.3f}s, expected near 0"

    def test_video_writer_process_survives_chunk_rotation(self, tmp_path):
        """Video writer process stays alive through chunk rotation.

        Reproduces the bug where local recordings stop at chunk boundary
        because the video writer process crashes during rotation, triggering
        critical-task-death detection.

        Exercises ChunkedVideoWriter.write_frame() → _start_new_chunk() →
        VideoWriter.close() in a real subprocess (spawn mode on macOS),
        which is the same execution context as the production video writer.
        """
        import multiprocessing

        chunk_q = multiprocessing.Queue()
        result_q = multiprocessing.Queue()
        frame_q = multiprocessing.Queue()
        terminate = multiprocessing.Event()
        started = multiprocessing.Event()

        writer_proc = multiprocessing.Process(
            target=_video_writer_subprocess,
            args=(str(tmp_path), frame_q, chunk_q, result_q, terminate, started),
        )
        writer_proc.start()
        assert started.wait(timeout=10), "Video writer subprocess did not start"

        # Simulate action-gated frames: burst, gap, burst (crosses boundary)
        base = time.time()

        # Chunk 0: 3 frames in first 0.3s
        for i in range(3):
            img = Image.new("RGB", (100, 100), color=(i * 80, 0, 0))
            frame_q.put((img, base + i * 0.1))

        # Wait past the 0.5s chunk boundary
        time.sleep(1.0)

        # Post-boundary frames — triggers rotation, where crashes happen
        for i in range(3):
            img = Image.new("RGB", (100, 100), color=(0, i * 80, 0))
            frame_q.put((img, base + 1.0 + i * 0.1))

        time.sleep(0.5)  # Let frames be processed

        # KEY ASSERTION: process must still be alive after chunk rotation
        assert writer_proc.is_alive(), (
            f"Video writer process died during chunk rotation "
            f"(exitcode={writer_proc.exitcode})"
        )

        # Clean shutdown
        terminate.set()
        writer_proc.join(timeout=10)
        assert writer_proc.exitcode == 0, (
            f"Video writer exited with code {writer_proc.exitcode}"
        )

        # Check subprocess result for exceptions
        result = result_q.get(timeout=1)
        assert result["status"] == "ok", (
            f"Subprocess error: {result.get('error')}\n{result.get('traceback')}"
        )

        # Verify chunks were produced
        chunks = sorted(tmp_path.glob("chunk_*.mp4"))
        assert len(chunks) >= 2, f"Expected >= 2 chunks, got {len(chunks)}"

    def test_rotation_survives_close_exception(self, tmp_path):
        """Writer continues recording even if VideoWriter.close() raises
        during chunk rotation (e.g. PTS error, disk I/O, GIL deadlock).

        Without defensive error handling in _start_new_chunk(), this
        exception would propagate up through write_frame() → write_events()
        and kill the video writer process.
        """
        from unittest.mock import patch

        writer = ChunkedVideoWriter(
            output_dir=tmp_path, width=100, height=100,
            chunk_duration=2.0,
        )
        base = time.time()

        # Write frames to chunk 0
        for i in range(3):
            img = Image.new("RGB", (100, 100), color=(i * 80, 0, 0))
            writer.write_frame(img, base + i * 0.5)

        # Patch VideoWriter.close to raise on the NEXT call (rotation close)
        original_close = VideoWriter.close
        close_call_count = 0

        def exploding_close(self):
            nonlocal close_call_count
            close_call_count += 1
            if close_call_count == 1:
                raise RuntimeError("Simulated PTS/encoding failure during close")
            return original_close(self)

        with patch.object(VideoWriter, "close", exploding_close):
            # This frame crosses the boundary — triggers rotation
            # _start_new_chunk() calls close() which raises, but should recover
            img_after = Image.new("RGB", (100, 100), color=(0, 255, 0))
            writer.write_frame(img_after, base + 2.5)

        # More frames in chunk 1 — proves the writer is still functional
        for i in range(2):
            img = Image.new("RGB", (100, 100), color=(0, i * 100, 0))
            writer.write_frame(img, base + 3.0 + i * 0.5)

        writer.close()

        # Chunk 0 may be missing/corrupt (close failed and container was
        # never flushed), but chunk 1 must exist and be playable
        chunks = sorted(tmp_path.glob("chunk_*.mp4"))
        assert any("chunk_0001" in c.name for c in chunks), (
            f"Expected chunk_0001.mp4 after recovery, got: {chunks}"
        )

        chunk_1 = tmp_path / "chunk_0001.mp4"
        container = av.open(str(chunk_1))
        frames = list(container.decode(video=0))
        container.close()
        assert len(frames) > 0, "Chunk 1 has no frames after rotation recovery"


class TestConcatVideoChunks:
    """Tests for the in-process PyAV chunk concat (U1, R1/R2/R5)."""

    def test_concat_frame_count_and_monotonic_pts(self, tmp_path):
        """2 chunks → one video.mp4; frame count is the sum, PTS monotonic from ~0."""
        n0 = _write_chunk(tmp_path / "chunk_0000.mp4", (220, 0, 0))
        n1 = _write_chunk(tmp_path / "chunk_0001.mp4", (0, 0, 220))

        out = concat_video_chunks(tmp_path)
        assert out == tmp_path / "video.mp4"
        assert out.exists()

        container = av.open(str(out))
        stream = container.streams.video[0]
        pts_sec = [float(f.pts * stream.time_base) for f in container.decode(stream)]
        container.close()

        assert len(pts_sec) == n0 + n1, (
            f"merged has {len(pts_sec)} frames, expected {n0 + n1}"
        )
        assert pts_sec[0] < 0.5, f"first PTS {pts_sec[0]:.3f}s should start near 0"
        for i in range(1, len(pts_sec)):
            assert pts_sec[i] > pts_sec[i - 1], (
                f"PTS not monotonic at {i}: {pts_sec[i-1]} >= {pts_sec[i]}"
            )

    def test_concat_is_seekable_past_boundary(self, tmp_path):
        """Decoding past the first chunk boundary returns a frame (continuity holds)."""
        _write_chunk(tmp_path / "chunk_0000.mp4", (220, 0, 0))
        _write_chunk(tmp_path / "chunk_0001.mp4", (0, 0, 220))
        boundary = float(get_chunk_duration(tmp_path / "chunk_0000.mp4"))

        out = concat_video_chunks(tmp_path)
        # A frame must exist strictly past the first chunk's span.
        container = av.open(str(out))
        stream = container.streams.video[0]
        past = [
            f for f in container.decode(stream)
            if float(f.pts * stream.time_base) > boundary
        ]
        container.close()
        assert past, "no frame decoded past the first-chunk boundary"

    def test_concat_timestamp_to_frame_mapping(self, tmp_path):
        """extract_frame just after the boundary returns content from the 2nd chunk.

        Guards capture.py:get_frame_at — the merged timeline must match the
        summed chunk durations so a wall-clock timestamp maps to the right frame.
        """
        _write_chunk(tmp_path / "chunk_0000.mp4", (220, 0, 0))  # red
        _write_chunk(tmp_path / "chunk_0001.mp4", (0, 0, 220))  # blue
        boundary = float(get_chunk_duration(tmp_path / "chunk_0000.mp4"))

        out = concat_video_chunks(tmp_path)

        # Before the boundary → red (first chunk); after → blue (second chunk).
        before = extract_frame(out, max(boundary - 0.05, 0.0), tolerance=0.2)
        after = extract_frame(out, boundary + 0.05, tolerance=0.2)
        assert _dominant_channel(before) == "R", "pre-boundary frame should be red"
        assert _dominant_channel(after) == "B", "post-boundary frame should be blue"

    def test_concat_raises_without_chunks(self, tmp_path):
        """No chunk_*.mp4 → ValueError (caller is expected to pre-check)."""
        with pytest.raises(ValueError, match="No chunk"):
            concat_video_chunks(tmp_path)

    def test_concat_raises_on_corrupt_chunk(self, tmp_path):
        """A chunk PyAV cannot open → RuntimeError, not a silent truncated output."""
        _write_chunk(tmp_path / "chunk_0000.mp4", (220, 0, 0))
        (tmp_path / "chunk_0001.mp4").write_bytes(b"not a valid mp4 file")

        with pytest.raises(RuntimeError, match="chunk_0001"):
            concat_video_chunks(tmp_path)
        # Fail-loud: no partial video.mp4 and no temp residue left behind.
        assert not (tmp_path / "video.mp4").exists()
        assert not list(tmp_path.glob(".video.mp4.*.tmp"))

    def test_concat_no_temp_residue_on_success(self, tmp_path):
        """A successful concat leaves no .tmp sibling behind."""
        _write_chunk(tmp_path / "chunk_0000.mp4", (220, 0, 0))
        _write_chunk(tmp_path / "chunk_0001.mp4", (0, 0, 220))
        concat_video_chunks(tmp_path)
        assert not list(tmp_path.glob(".video.mp4.*.tmp"))

    def test_concat_does_not_spawn_subprocess(self, tmp_path, monkeypatch):
        """Concat is fully in-process — it must not shell out (R1/R5)."""
        import subprocess

        def _fail(*a, **k):  # pragma: no cover - only runs on regression
            raise AssertionError("concat must not spawn a subprocess")

        monkeypatch.setattr(subprocess, "run", _fail)
        monkeypatch.setattr(subprocess, "Popen", _fail)

        _write_chunk(tmp_path / "chunk_0000.mp4", (220, 0, 0))
        _write_chunk(tmp_path / "chunk_0001.mp4", (0, 0, 220))
        out = concat_video_chunks(tmp_path)
        assert out.exists()


def get_chunk_duration(path: Path) -> float:
    """Max decoded PTS (seconds) in a chunk — its on-screen span."""
    container = av.open(str(path))
    stream = container.streams.video[0]
    last = 0.0
    for frame in container.decode(stream):
        last = max(last, float(frame.pts * stream.time_base))
    container.close()
    return last


class TestPixelFormatProbe:
    """Tests for read_pixel_format / needs_pixfmt_remediation (U2, R3)."""

    def test_reads_yuv444p_and_flags_for_remediation(self, tmp_path):
        path = tmp_path / "v444.mp4"
        _write_chunk(path, (200, 0, 0))  # VideoWriter default pix_fmt = yuv444p
        assert read_pixel_format(path) == "yuv444p"
        assert needs_pixfmt_remediation("yuv444p") is True

    def test_reads_yuv420p_and_is_avkit_safe(self, tmp_path):
        """Covers AE2: an already-420 recording needs no remediation."""
        path = tmp_path / "v420.mp4"
        base = time.time()
        writer = VideoWriter(str(path), width=64, height=64, fps=24, pix_fmt="yuv420p")
        for i in range(4):
            writer.write_frame(Image.new("RGB", (64, 64), color=(0, 0, 200)), base + i / 24)
        writer.close()
        assert read_pixel_format(path) == "yuv420p"
        assert needs_pixfmt_remediation("yuv420p") is False

    @pytest.mark.parametrize("pix_fmt", ["yuv420p", "yuvj420p", "nv12"])
    def test_avkit_safe_formats_skip_remediation(self, pix_fmt):
        assert needs_pixfmt_remediation(pix_fmt) is False

    @pytest.mark.parametrize("pix_fmt", ["yuv444p", "yuv422p", "rgb24"])
    def test_non_420_formats_need_remediation(self, pix_fmt):
        assert needs_pixfmt_remediation(pix_fmt) is True

    def test_corrupt_file_raises(self, tmp_path):
        path = tmp_path / "garbage.mp4"
        path.write_bytes(b"definitely not an mp4")
        with pytest.raises(RuntimeError):
            read_pixel_format(path)

    def test_probe_does_not_decode_frames(self, tmp_path, monkeypatch):
        """The probe reads codec_context.pix_fmt only — never iterates frames."""
        path = tmp_path / "v444.mp4"
        _write_chunk(path, (200, 0, 0))

        # Guard against a future regression that adds a decode loop: if the
        # probe decoded, this patched decode would raise.
        import screencap.engine.video as video_mod
        real_open = video_mod.av.open

        class _NoDecodeContainer:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                if name == "decode":
                    raise AssertionError("read_pixel_format must not decode frames")
                return getattr(self._inner, name)

        monkeypatch.setattr(
            video_mod.av, "open", lambda *a, **k: _NoDecodeContainer(real_open(*a, **k))
        )
        assert read_pixel_format(path) == "yuv444p"
