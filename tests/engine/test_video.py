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
    remediate_pixfmt_for_review,
    write_video_frame,
)


def _write_chunk(
    path: Path,
    color: tuple[int, int, int],
    n_frames: int = 5,
    fps: int = 24,
    size: int = 64,
    pix_fmt: str | None = None,
) -> int:
    """Write a solid-color video via VideoWriter (yuv444p/bf=0, like the recorder).

    Returns the number of frames actually decodable from the written file
    (VideoWriter.close adds a trailing key frame, so this is >= n_frames).
    """
    base = time.time()
    writer = VideoWriter(str(path), width=size, height=size, fps=fps, pix_fmt=pix_fmt)
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

    def test_concat_absolute_offsets_preserve_interchunk_gap(self, tmp_path):
        """chunk_offsets keep an inter-chunk idle gap so a wall-clock timestamp
        in the 2nd chunk maps to the right frame (SCR-98 regression).

        Mirrors capture.py:get_frame_at, which looks up ``event_ts - video_start``
        in the merged timeline. The legacy summed-span concat collapses the idle
        gap between chunks, so a 2nd-chunk event overshoots the shortened
        timeline and raises "no frame within tolerance". Absolute offsets keep
        the gap, so the lookup resolves to the correct chunk.
        """
        base = time.time()
        writer = ChunkedVideoWriter(
            output_dir=tmp_path, width=64, height=64, chunk_duration=1.0, fps=24
        )
        # chunk 0: red at +0.0, +0.5
        for dt in (0.0, 0.5):
            writer.write_frame(Image.new("RGB", (64, 64), (220, 0, 0)), base + dt)
        # ~2.5s idle gap crosses the 1s boundary → chunk 1: blue at +3.0, +3.5
        for dt in (3.0, 3.5):
            writer.write_frame(Image.new("RGB", (64, 64), (0, 0, 220)), base + dt)
        writer.close()

        chunks = sorted(tmp_path.glob("chunk_*.mp4"))
        assert len(chunks) == 2, f"expected 2 chunks, got {[c.name for c in chunks]}"

        # video_start == first frame, so offsets = chunk_start - video_start —
        # the same value capture.py subtracts and _chunk_offsets_for_concat builds.
        out = concat_video_chunks(tmp_path, chunk_offsets={0: 0.0, 1: 3.0})

        # Merged span tracks wall-clock (~3.5s), not the ~1.1s summed-span.
        container = av.open(str(out))
        stream = container.streams.video[0]
        max_pts_sec = max(
            float(f.pts * stream.time_base) for f in container.decode(stream)
        )
        container.close()
        assert max_pts_sec > 3.0, (
            f"merged span {max_pts_sec:.3f}s collapsed the inter-chunk gap"
        )

        # The 2nd-chunk event (wall-relative t=3.0) resolves — and to blue.
        assert _dominant_channel(extract_frame(out, 3.0, tolerance=0.5)) == "B"
        # The 1st-chunk event still resolves to red.
        assert _dominant_channel(extract_frame(out, 0.0, tolerance=0.5)) == "R"

    def test_concat_without_offsets_collapses_gap(self, tmp_path):
        """Without chunk_offsets, the legacy summed-span behavior is preserved:
        the inter-chunk gap collapses and a 2nd-chunk wall-clock lookup misses.

        Locks the documented fallback (smoke test / missing-manifest callers)
        and proves the regression test above guards the gap, not test setup.
        """
        base = time.time()
        writer = ChunkedVideoWriter(
            output_dir=tmp_path, width=64, height=64, chunk_duration=1.0, fps=24
        )
        for dt in (0.0, 0.5):
            writer.write_frame(Image.new("RGB", (64, 64), (220, 0, 0)), base + dt)
        for dt in (3.0, 3.5):
            writer.write_frame(Image.new("RGB", (64, 64), (0, 0, 220)), base + dt)
        writer.close()

        out = concat_video_chunks(tmp_path)  # no offsets → summed-span

        container = av.open(str(out))
        stream = container.streams.video[0]
        max_pts_sec = max(
            float(f.pts * stream.time_base) for f in container.decode(stream)
        )
        container.close()
        assert max_pts_sec < 2.0, "summed-span timeline should stay short"
        with pytest.raises(ValueError, match="No frame within tolerance"):
            extract_frame(out, 3.0, tolerance=0.5)

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

    def test_no_video_stream_raises(self, tmp_path):
        """A valid container with no video stream raises the distinct error (plan U2).

        Uses an audio-only FLAC — opens cleanly but has no video stream, so the
        `if not streams` branch fires (distinct from the av.open-failure path).
        """
        import numpy as np
        import soundfile as sf

        audio = tmp_path / "audio.flac"
        sf.write(str(audio), np.zeros(2000, dtype="float32"), 16000)
        with pytest.raises(RuntimeError, match="No video stream"):
            read_pixel_format(audio)

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


class TestRemediatePixfmtForReview:
    """Tests for remediate_pixfmt_for_review (U3, R4/R6/R7)."""

    def test_yuv444p_source_is_remediated(self, tmp_path):
        """Covers AE1 (remediation half): yuv444p → yuv420p .video_review.mp4."""
        _write_chunk(tmp_path / "video.mp4", (200, 0, 0))  # yuv444p default
        path, remediated = remediate_pixfmt_for_review(tmp_path)
        assert remediated is True
        assert path == tmp_path / ".video_review.mp4"
        assert path.exists() and not path.is_symlink()
        assert read_pixel_format(path) == "yuv420p"

    def test_yuv420p_source_is_left_untouched(self, tmp_path):
        """Covers AE2: an already-420 recording produces no artifact."""
        _write_chunk(tmp_path / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
        path, remediated = remediate_pixfmt_for_review(tmp_path)
        assert remediated is False
        assert path == tmp_path / "video.mp4"
        assert not (tmp_path / ".video_review.mp4").exists()

    def test_idempotent_skip_reuses_existing_artifact(self, tmp_path):
        """Covers AE3: a second call performs no re-encode."""
        _write_chunk(tmp_path / "video.mp4", (200, 0, 0))
        path1, _ = remediate_pixfmt_for_review(tmp_path)
        # Tamper with the artifact; a re-encode would overwrite the sentinel.
        path1.write_bytes(b"SENTINEL")
        path2, remediated2 = remediate_pixfmt_for_review(tmp_path)
        assert remediated2 is True
        assert path2 == path1
        assert path2.read_bytes() == b"SENTINEL", "re-encode ran on idempotent call"

    def test_remediated_pts_monotonic_from_zero(self, tmp_path):
        """Regression guard: re-encode yields yuv420p with monotonic PTS from ~0."""
        _write_chunk(tmp_path / "video.mp4", (200, 0, 0), n_frames=6)
        path, _ = remediate_pixfmt_for_review(tmp_path)

        container = av.open(str(path))
        stream = container.streams.video[0]
        assert stream.codec_context.pix_fmt == "yuv420p"
        pts = [float(f.pts * stream.time_base) for f in container.decode(stream)]
        container.close()
        assert pts[0] < 0.5, f"first PTS {pts[0]:.3f}s should start near 0"
        for i in range(1, len(pts)):
            assert pts[i] > pts[i - 1], f"PTS not monotonic at {i}"

    def test_no_temp_residue_on_success(self, tmp_path):
        _write_chunk(tmp_path / "video.mp4", (200, 0, 0))
        remediate_pixfmt_for_review(tmp_path)
        assert not list(tmp_path.glob(".video_review.mp4.*.tmp"))

    def test_corrupt_source_raises_and_leaves_no_artifact(self, tmp_path):
        """A video.mp4 PyAV cannot open → RuntimeError, no partial review file."""
        (tmp_path / "video.mp4").write_bytes(b"not a real mp4")
        with pytest.raises(RuntimeError):
            remediate_pixfmt_for_review(tmp_path)
        assert not (tmp_path / ".video_review.mp4").exists()
        assert not list(tmp_path.glob(".video_review.mp4.*.tmp"))

    def test_reads_through_single_chunk_symlink(self, tmp_path):
        """Edge: video.mp4 is a symlink to a yuv444p chunk → real .video_review.mp4."""
        _write_chunk(tmp_path / "chunk_0000.mp4", (200, 0, 0))
        (tmp_path / "video.mp4").symlink_to(tmp_path / "chunk_0000.mp4")
        path, remediated = remediate_pixfmt_for_review(tmp_path)
        assert remediated is True
        assert path == tmp_path / ".video_review.mp4"
        assert path.is_file() and not path.is_symlink()
        assert read_pixel_format(path) == "yuv420p"

    def test_review_artifact_excluded_from_upload(self, tmp_path):
        """Covers AE4 / R6: the artifact is never enumerated for upload."""
        from screencap import upload

        _write_chunk(tmp_path / "video.mp4", (200, 0, 0))
        original_bytes = (tmp_path / "video.mp4").read_bytes()
        remediate_pixfmt_for_review(tmp_path)

        names = [f.name for f in upload.list_recording_files(tmp_path)]
        assert ".video_review.mp4" not in names
        assert "video.mp4" in names
        # R6: the upload artifact's bytes are unchanged by remediation.
        assert (tmp_path / "video.mp4").read_bytes() == original_bytes


class TestReviewPipelineHardening:
    """Regression guards from code review of the PyAV review pipeline (SCR-97)."""

    def test_remediate_preserves_vfr_idle_gap(self, tmp_path):
        """A yuv444p recording with an idle gap keeps that gap after re-encode.

        This is the load-bearing guard for the deliberate deviation from the
        plan's `frame.pts=None`: that idiom collapses a variable-frame-rate
        (action-gated) recording's timeline. The PTS-preserving transcode must
        keep the ~5s gap so review-window event overlay stays aligned.
        """
        video = tmp_path / "video.mp4"
        base = time.time()
        writer = VideoWriter(str(video), width=64, height=64, fps=24)
        # Two activity bursts separated by a 5s idle gap.
        for ts, color in [
            (0.0, (200, 0, 0)), (0.1, (200, 0, 0)),
            (5.0, (0, 0, 200)), (5.1, (0, 0, 200)),
        ]:
            writer.write_frame(Image.new("RGB", (64, 64), color=color), base + ts)
        writer.close()
        assert read_pixel_format(video) == "yuv444p"

        path, remediated = remediate_pixfmt_for_review(tmp_path)
        assert remediated is True

        container = av.open(str(path))
        stream = container.streams.video[0]
        pts = [float(f.pts * stream.time_base) for f in container.decode(stream)]
        container.close()
        # The idle gap survives: the span between first and last frame is ~5s,
        # NOT collapsed to a fraction of a second (which frame.pts=None produces).
        assert (pts[-1] - pts[0]) > 4.5, (
            f"VFR idle gap collapsed: span {pts[-1] - pts[0]:.3f}s (expected ~5s)"
        )

    def test_concat_raises_on_mismatched_chunk_params(self, tmp_path):
        """Chunks with differing dimensions fail loud rather than corrupt silently."""
        _write_chunk(tmp_path / "chunk_0000.mp4", (200, 0, 0), size=64)
        _write_chunk(tmp_path / "chunk_0001.mp4", (0, 0, 200), size=48)
        with pytest.raises(RuntimeError, match="differ from the first"):
            concat_video_chunks(tmp_path)
        assert not (tmp_path / "video.mp4").exists()
        assert not list(tmp_path.glob(".video.mp4.*.tmp"))

    def test_concat_does_not_promote_temp_on_close_timeout(self, tmp_path, monkeypatch):
        """A timed-out container close must not publish a possibly-truncated video.mp4."""
        import screencap.engine.video as video_mod

        _write_chunk(tmp_path / "chunk_0000.mp4", (200, 0, 0))
        _write_chunk(tmp_path / "chunk_0001.mp4", (0, 0, 200))
        monkeypatch.setattr(video_mod, "_close_container_in_thread", lambda c: False)

        with pytest.raises(RuntimeError, match="Timed out finalizing"):
            concat_video_chunks(tmp_path)
        assert not (tmp_path / "video.mp4").exists()
        assert not list(tmp_path.glob(".video.mp4.*.tmp"))

    def test_remediate_does_not_promote_temp_on_close_timeout(self, tmp_path, monkeypatch):
        """A timed-out close must not publish a possibly-truncated .video_review.mp4."""
        import screencap.engine.video as video_mod

        _write_chunk(tmp_path / "video.mp4", (200, 0, 0))  # yuv444p → needs remediation
        monkeypatch.setattr(video_mod, "_close_container_in_thread", lambda c: False)

        with pytest.raises(RuntimeError, match="Timed out finalizing"):
            remediate_pixfmt_for_review(tmp_path)
        assert not (tmp_path / ".video_review.mp4").exists()
        assert not list(tmp_path.glob(".video_review.mp4.*.tmp"))

    def test_remediate_promotes_output_even_if_input_close_raises(self, tmp_path, monkeypatch):
        """A failing inp.close() after a successful encode must NOT discard the review copy.

        Guards todo 001: previously inp.close() ran inside the success path before
        os.replace, so a close error jumped to except and unlinked the complete temp.
        """
        import screencap.engine.video as vm

        video = tmp_path / "video.mp4"
        _write_chunk(video, (200, 0, 0))  # yuv444p → remediated
        real_open = vm.av.open
        state = {"source_reads": 0}

        class _RaiseOnClose:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def close(self):
                raise OSError("simulated input close failure")

        def fake_open(*a, **k):
            container = real_open(*a, **k)
            # Only the SOURCE video, opened for read. read #1 is
            # read_pixel_format's probe; read #2 is remediate's `inp` — wrap that
            # one so its close() raises after a successful encode.
            if k.get("mode") != "w" and str(a[0]) == str(video):
                state["source_reads"] += 1
                if state["source_reads"] >= 2:
                    return _RaiseOnClose(container)
            return container

        monkeypatch.setattr(vm.av, "open", fake_open)

        path, remediated = remediate_pixfmt_for_review(tmp_path)
        assert remediated is True
        assert path == tmp_path / ".video_review.mp4"
        assert path.exists()
        # Verify with the unpatched opener (monkeypatch is still active here).
        container = real_open(str(path))
        try:
            assert container.streams.video[0].codec_context.pix_fmt == "yuv420p"
        finally:
            container.close()

    def test_concat_raises_on_zero_packet_chunk(self, tmp_path, monkeypatch):
        """A chunk that matches the template but demuxes no timestamped packets fails loud.

        Guards todo 002: a zero-packet chunk would leave offset unadvanced and
        overlap the next chunk's PTS silently.
        """
        import screencap.engine.video as vm

        _write_chunk(tmp_path / "chunk_0000.mp4", (200, 0, 0))
        real_open = vm.av.open

        class _EmptyDemux:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def demux(self, *a, **k):
                return iter([vm.av.Packet()])  # bare packet: pts/dts are None → skipped

        def fake_open(*a, **k):
            if k.get("mode") == "w":
                return real_open(*a, **k)
            return _EmptyDemux(real_open(*a, **k))

        monkeypatch.setattr(vm.av, "open", fake_open)

        with pytest.raises(RuntimeError, match="no timestamped packets"):
            concat_video_chunks(tmp_path)
        assert not (tmp_path / "video.mp4").exists()
