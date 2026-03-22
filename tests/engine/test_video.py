"""Tests for video module."""

import time

import av
import pytest
from PIL import Image

from screencap.engine import utils
from screencap.engine.video import (
    ChunkedVideoWriter,
    VideoWriter,
    initialize_video_writer,
    write_video_frame,
)


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
