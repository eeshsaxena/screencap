"""Video capture and frame extraction using PyAV.

This module provides video recording capabilities using libx264 encoding,
Includes both a VideoWriter class
and legacy functional API (initialize/write/finalize).
"""

from __future__ import annotations

import os
import struct
import subprocess
import tempfile
import threading
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

import av
from loguru import logger

from screencap.engine import utils
from screencap.engine.config import config

if TYPE_CHECKING:
    from PIL import Image

# fMP4: crash-safe — writes self-contained fragments to disk progressively,
# so the file is playable even if the process is killed mid-recording.
_FRAG_MP4_OPTIONS: dict[str, str] = {
    "movflags": "frag_keyframe+empty_moov",
    "flush_packets": "1",
}


# =============================================================================
# Video Writer
# =============================================================================


class VideoWriter:
    """H.264 video writer using PyAV.

    Writes frames to an MP4 file with H.264 encoding for maximum compatibility
    and efficient compression.

    Usage:
        writer = VideoWriter("output.mp4", width=1920, height=1080)
        writer.write_frame(image, timestamp)
        writer.close()

    Or as context manager:
        with VideoWriter("output.mp4", width=1920, height=1080) as writer:
            writer.write_frame(image, timestamp)
    """

    def __init__(
        self,
        output_path: str | Path,
        width: int,
        height: int,
        fps: int = 24,
        codec: str | None = None,
        pix_fmt: str | None = None,
        crf: int | None = None,
        preset: str | None = None,
    ) -> None:
        """Initialize video writer.

        Args:
            output_path: Path to output MP4 file.
            width: Video width in pixels.
            height: Video height in pixels.
            fps: Frames per second (default 24).
            codec: Video codec (default from config.VIDEO_ENCODING).
            pix_fmt: Pixel format (default from config.VIDEO_PIXEL_FORMAT).
            crf: Constant Rate Factor (default from config.VIDEO_CRF).
            preset: Encoding preset (default from config.VIDEO_PRESET).
        """

        self.output_path = Path(output_path)
        self.width = width
        self.height = height
        self.fps = fps
        self.codec = codec if codec is not None else config.VIDEO_ENCODING
        self.pix_fmt = pix_fmt if pix_fmt is not None else config.VIDEO_PIXEL_FORMAT
        self.crf = crf if crf is not None else config.VIDEO_CRF
        self.preset = preset if preset is not None else config.VIDEO_PRESET

        self._container = None
        self._stream = None
        self._start_time: float | None = None
        self._last_pts: int = -1
        self._last_frame: "Image.Image" | None = None
        self._last_frame_timestamp: float | None = None
        self._lock = threading.Lock()

    def _init_stream(self) -> None:
        """Initialize the video stream."""
        self._container = av.open(
            str(self.output_path), mode="w", container_options=_FRAG_MP4_OPTIONS,
        )
        self._stream = self._container.add_stream(self.codec, rate=self.fps)
        self._stream.width = self.width
        self._stream.height = self.height
        self._stream.pix_fmt = self.pix_fmt
        self._stream.options = {
            "crf": str(self.crf),
            "preset": self.preset,
            "g": str(config.VIDEO_GOP_SIZE),
            "bf": "0",
        }

    @property
    def start_time(self) -> float | None:
        """Get the start time of the video."""
        return self._start_time

    @property
    def is_open(self) -> bool:
        """Check if writer is open."""
        return self._container is not None

    def write_frame(
        self,
        image: "Image.Image",
        timestamp: float,
        force_key_frame: bool = False,
    ) -> None:
        """Write a frame to the video.

        Args:
            image: PIL Image to write.
            timestamp: Unix timestamp of the frame.
            force_key_frame: Force this frame to be a key frame.
        """
        with self._lock:
            if self._container is None:
                self._init_stream()
                self._start_time = timestamp

            # Convert PIL Image to AVFrame
            av_frame = av.VideoFrame.from_image(image)

            # Force key frame if requested
            if force_key_frame:
                av_frame.pict_type = av.video.frame.PictureType.I

            # Calculate PTS based on elapsed time
            time_diff = timestamp - self._start_time
            pts = int(time_diff * float(Fraction(self._stream.average_rate)))

            # Ensure monotonically increasing PTS
            if pts <= self._last_pts:
                pts = self._last_pts + 1

            av_frame.pts = pts
            self._last_pts = pts

            # Encode and write
            for packet in self._stream.encode(av_frame):
                self._container.mux(packet)

            # Track last frame for finalization
            self._last_frame = image
            self._last_frame_timestamp = timestamp

    def close(self) -> None:
        """Close the video writer and finalize the file.

        This method handles the GIL deadlock issue by closing in a separate thread.
        """
        with self._lock:
            if self._container is None:
                return

            # Write a final key frame to ensure clean ending
            if self._last_frame is not None and self._last_frame_timestamp is not None:
                av_frame = av.VideoFrame.from_image(self._last_frame)
                # pict_type 1 = I-frame (key frame)
                av_frame.pict_type = av.video.frame.PictureType.I

                time_diff = self._last_frame_timestamp - self._start_time
                pts = int(time_diff * float(Fraction(self._stream.average_rate)))
                if pts <= self._last_pts:
                    pts = self._last_pts + 1
                av_frame.pts = pts

                for packet in self._stream.encode(av_frame):
                    self._container.mux(packet)

            # Flush the stream
            for packet in self._stream.encode():
                self._container.mux(packet)

            # Close in separate thread to avoid GIL deadlock
            # https://github.com/PyAV-Org/PyAV/issues/1053
            container = self._container

            def close_container() -> None:
                container.close()

            close_thread = threading.Thread(target=close_container)
            close_thread.start()
            close_thread.join(timeout=15)
            if close_thread.is_alive():
                logger.warning("VideoWriter close thread did not finish in 15s, continuing")

            self._container = None
            self._stream = None

    def __enter__(self) -> "VideoWriter":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.close()


# =============================================================================
# Legacy Functional API
# =============================================================================


def get_video_file_path(recording_timestamp: float, video_dir: str = None) -> str:
    """Generates a file path for a video recording based on a timestamp.

    Args:
        recording_timestamp (float): The timestamp of the recording.
        video_dir (str): Directory for video files. If None, uses capture dir.

    Returns:
        str: The generated file name for the video recording.
    """
    if video_dir is None:
        video_dir = os.path.join(os.getcwd(), "video")
    os.makedirs(video_dir, exist_ok=True)
    return os.path.join(
        video_dir, f"recording-{recording_timestamp}.mp4"
    )


def initialize_video_writer(
    output_path: str,
    width: int,
    height: int,
    fps: int = 24,
    codec: str | None = None,
    pix_fmt: str | None = None,
    crf: int | None = None,
    preset: str | None = None,
) -> tuple[av.container.OutputContainer, av.stream.Stream, float]:
    """Initializes video writer and returns the container, stream, and base timestamp.

    Args:
        output_path (str): Path to the output video file.
        width (int): Width of the video.
        height (int): Height of the video.
        fps (int, optional): Frames per second of the video. Defaults to 24.
        codec (str, optional): Codec (default from config.VIDEO_ENCODING).
        pix_fmt (str, optional): Pixel format (default from config.VIDEO_PIXEL_FORMAT).
        crf (int, optional): Constant Rate Factor (default from config.VIDEO_CRF).
        preset (str, optional): Encoding preset (default from config.VIDEO_PRESET).

    Returns:
        tuple[av.container.OutputContainer, av.stream.Stream, float]: The initialized
            container, stream, and base timestamp.
    """
    if codec is None:
        codec = config.VIDEO_ENCODING
    if pix_fmt is None:
        pix_fmt = config.VIDEO_PIXEL_FORMAT
    if crf is None:
        crf = config.VIDEO_CRF
    if preset is None:
        preset = config.VIDEO_PRESET

    logger.info("initializing video stream...")
    video_container = av.open(output_path, mode="w", container_options=_FRAG_MP4_OPTIONS)
    video_stream = video_container.add_stream(codec, rate=fps)
    video_stream.width = width
    video_stream.height = height
    video_stream.pix_fmt = pix_fmt
    video_stream.options = {"crf": str(crf), "preset": preset, "g": str(config.VIDEO_GOP_SIZE), "bf": "0"}

    base_timestamp = utils.get_timestamp()

    return video_container, video_stream, base_timestamp


def write_video_frame(
    video_container: av.container.OutputContainer,
    video_stream: av.stream.Stream,
    screenshot: "Image.Image",
    timestamp: float,
    video_start_timestamp: float,
    last_pts: int,
    force_key_frame: bool = False,
) -> int:
    """Encodes and writes a video frame to the output container from a given screenshot.

    This function converts a PIL.Image to an AVFrame,
    and encodes it for writing to the video stream. It calculates the
    presentation timestamp (PTS) for each frame based on the elapsed time since
    the base timestamp, ensuring monotonically increasing PTS values.

    Args:
        video_container (av.container.OutputContainer): The output container to which
            the frame is written.
        video_stream (av.stream.Stream): The video stream within the container.
        screenshot (Image.Image): The screenshot to be written as a video frame.
        timestamp (float): The timestamp of the current frame.
        video_start_timestamp (float): The base timestamp from which the video
            recording started.
        last_pts (int): The PTS of the last written frame.
        force_key_frame (bool): Whether to force this frame to be a key frame.

    Returns:
        int: The updated last_pts value, to be used for writing the next frame.

    Note:
        - It is crucial to maintain monotonically increasing PTS values for the
              video stream's consistency and playback.
        - The function logs the current timestamp, base timestamp, and
              calculated PTS values for debugging purposes.
    """
    # Convert the PIL Image to an AVFrame
    av_frame = av.VideoFrame.from_image(screenshot)

    # Optionally force a key frame
    # TODO: force key frames on active window change?
    if force_key_frame:
        av_frame.pict_type = av.video.frame.PictureType.I

    # Calculate the time difference in seconds
    time_diff = timestamp - video_start_timestamp

    # Calculate PTS, taking into account the fractional average rate
    pts = int(time_diff * float(Fraction(video_stream.average_rate)))

    logger.debug(
        f"{timestamp=} {video_start_timestamp=} {time_diff=} {pts=} {force_key_frame=}"
    )

    # Ensure monotonically increasing PTS
    if pts <= last_pts:
        pts = last_pts + 1
        logger.debug(f"incremented {pts=}")
    av_frame.pts = pts
    last_pts = pts  # Update the last_pts

    # Encode and write the frame
    for packet in video_stream.encode(av_frame):
        video_container.mux(packet)

    return last_pts  # Return the updated last_pts for the next call


def finalize_video_writer(
    video_container: av.container.OutputContainer,
    video_stream: av.stream.Stream,
    video_start_timestamp: float,
    last_frame: "Image.Image",
    last_frame_timestamp: float,
    last_pts: int,
    video_file_path: str,
    fix_moov: bool = False,
) -> None:
    """Finalizes the video writer, ensuring all buffered frames are encoded and written.

    Args:
        video_container (av.container.OutputContainer): The AV container to finalize.
        video_stream (av.stream.Stream): The AV stream to finalize.
        video_start_timestamp (float): The base timestamp from which the video
            recording started.
        last_frame (Image.Image): The last frame that was written (to be written again).
        last_frame_timestamp (float): The timestamp of the last frame that was written.
        last_pts (int): The last presentation timestamp.
        video_file_path (str): The path to the video file.
        fix_moov (bool): Whether to move the moov atom to the beginning of the file.
            Setting this to True will fix a bug when displaying the video in Github
            comments causing the video to appear to start a few seconds after 0:00.
            However, this causes extract_frames to fail.
    """
    # Closing the container in the main thread leads to a GIL deadlock.
    # https://github.com/PyAV-Org/PyAV/issues/1053

    # Write a final key frame
    last_pts = write_video_frame(
        video_container,
        video_stream,
        last_frame,
        last_frame_timestamp,
        video_start_timestamp,
        last_pts,
        force_key_frame=True,
    )

    # Closing in the same thread sometimes hangs, so do it in a different thread:

    # Define a function to close the container
    def close_container() -> None:
        logger.info("closing video container...")
        video_container.close()

    # Create a new thread to close the container
    close_thread = threading.Thread(target=close_container)

    # Flush stream
    logger.info("flushing video stream...")
    for packet in video_stream.encode():
        video_container.mux(packet)

    # Start the thread to close the container
    close_thread.start()

    # Wait for the thread to finish execution
    close_thread.join(timeout=15)
    if close_thread.is_alive():
        logger.warning("finalize_video_writer close thread did not finish in 15s, continuing")

    # Move moov atom to beginning of file
    if fix_moov:
        # TODO: fix this
        logger.warning(f"{fix_moov=} will cause extract_frames() to fail!!!")
        move_moov_atom(video_file_path)

    logger.info("done")


def _is_fragmented_mp4(video_file_path: str) -> bool:
    """Check if an MP4 file is fragmented by scanning for moof boxes.

    Walks top-level MP4 box headers per ISO 14496-12 section 4.2:
      - Standard box: 4-byte size (big-endian) + 4-byte type
      - box_size == 1: 64-bit largesize in next 8 bytes
      - box_size == 0: box extends to end of file (last box)
    The presence of any 'moof' box is the definitive indicator of fMP4.
    """
    try:
        with open(video_file_path, "rb") as f:
            file_size = f.seek(0, 2)
            f.seek(0)
            while True:
                box_start = f.tell()
                header = f.read(8)
                if len(header) < 8:
                    break
                box_size = struct.unpack(">I", header[:4])[0]
                box_type = header[4:8]

                if box_size == 1:
                    ext = f.read(8)
                    if len(ext) < 8:
                        break
                    box_size = struct.unpack(">Q", ext)[0]
                elif box_size == 0:
                    box_size = file_size - box_start

                if box_type == b"moof":
                    return True
                if box_size < 8:
                    break
                next_box = box_start + box_size
                if next_box <= box_start or next_box > file_size:
                    break
                f.seek(next_box)
    except (OSError, struct.error):
        pass
    return False


def move_moov_atom(input_file: str, output_file: str = None) -> None:
    """Moves the moov atom to the beginning of the video file using ffmpeg.

    If no output file is specified, modifies the input file in place.
    Gracefully skips if ffmpeg is not found (video still works, just without
    faststart optimization).

    Args:
        input_file (str): The path to the input MP4 file.
        output_file (str, optional): The path to the output MP4 file where the moov
            atom is at the beginning. If None, modifies the input file in place.
    """
    if _is_fragmented_mp4(input_file):
        logger.info("Skipping faststart: file is already fragmented MP4")
        return

    import shutil

    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg not found, skipping moov atom optimization")
        return

    temp_file = None
    if output_file is None:
        # Create a temporary file
        temp_file = tempfile.NamedTemporaryFile(
            delete=False,
            suffix=".mp4",
            dir=os.path.dirname(input_file),
        ).name
        output_file = temp_file

    command = [
        "ffmpeg",
        "-y",  # Automatically overwrite files without asking
        "-i",
        input_file,
        "-codec",
        "copy",  # Avoid re-encoding; just copy streams
        "-movflags",
        "faststart",  # Move the moov atom to the start
        output_file,
    ]
    logger.info(f"{command=}")
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError:
        logger.warning("ffmpeg moov atom optimization failed, skipping")
        if temp_file and os.path.exists(temp_file):
            os.unlink(temp_file)
        return

    if temp_file:
        # Replace the original file with the modified one
        os.replace(temp_file, input_file)


# =============================================================================
# Frame Extraction
# =============================================================================


def extract_frames(
    video_path: str | Path,
    timestamps: list[float],
    tolerance: float = 0.1,
) -> list["Image.Image"]:
    """Extract frames from a video at specified timestamps.

    Args:
        video_path: Path to the video file.
        timestamps: List of timestamps (in seconds) to extract.
        tolerance: Maximum difference between requested and actual frame time.

    Returns:
        List of PIL Images at the requested timestamps.

    Raises:
        ValueError: If no frame found within tolerance for a timestamp.
    """

    video_container = av.open(str(video_path))
    video_stream = video_container.streams.video[0]

    # Storage for matched frames
    frame_by_timestamp: dict[float, "Image.Image" | None] = {t: None for t in timestamps}
    frame_differences: dict[float, float] = {t: float("inf") for t in timestamps}

    # Convert PTS to seconds
    time_base = float(video_stream.time_base)

    for frame in video_container.decode(video_stream):
        frame_timestamp = frame.pts * time_base

        for target_timestamp in timestamps:
            difference = abs(frame_timestamp - target_timestamp)
            if difference <= tolerance and difference < frame_differences[target_timestamp]:
                frame_by_timestamp[target_timestamp] = frame.to_image()
                frame_differences[target_timestamp] = difference

    video_container.close()

    # Check for missing frames
    missing = [t for t, frame in frame_by_timestamp.items() if frame is None]
    if missing:
        raise ValueError(f"No frame within tolerance for timestamps: {missing}")

    # Return in same order as input
    return [frame_by_timestamp[t] for t in timestamps]


def extract_frame(
    video_path: str | Path,
    timestamp: float,
    tolerance: float = 0.1,
) -> "Image.Image":
    """Extract a single frame from a video.

    Args:
        video_path: Path to the video file.
        timestamp: Timestamp (in seconds) to extract.
        tolerance: Maximum difference between requested and actual frame time.

    Returns:
        PIL Image at the requested timestamp.
    """
    return extract_frames(video_path, [timestamp], tolerance)[0]


def get_video_info(video_path: str | Path) -> dict:
    """Get information about a video file.

    Args:
        video_path: Path to the video file.

    Returns:
        Dictionary with video information (duration, width, height, fps, etc).
    """

    video_container = av.open(str(video_path))
    video_stream = video_container.streams.video[0]

    info = {
        "duration": (
            float(video_stream.duration * video_stream.time_base)
            if video_stream.duration
            else (
                float(video_container.duration) / 1_000_000.0
                if video_container.duration is not None
                else None
            )
        ),
        "width": video_stream.width,
        "height": video_stream.height,
        "fps": float(video_stream.average_rate) if video_stream.average_rate else None,
        "codec": video_stream.codec_context.codec.name,
        "frames": video_stream.frames,
    }

    video_container.close()
    return info


# =============================================================================
# Chunked Video Writer (for long captures)
# =============================================================================


class ChunkedVideoWriter:
    """Video writer that automatically chunks output into segments.

    For long captures (hours/days), this splits the video into manageable
    segments to avoid huge files and enable recovery from crashes.

    Usage:
        writer = ChunkedVideoWriter(
            output_dir="capture_abc123/video",
            width=1920, height=1080,
            chunk_duration=600,  # 10 minutes per chunk
        )
        writer.write_frame(image, timestamp)
        writer.close()
    """

    def __init__(
        self,
        output_dir: str | Path,
        width: int,
        height: int,
        chunk_duration: float = 600.0,  # 10 minutes
        fps: int = 24,
        codec: str | None = None,
        pix_fmt: str | None = None,
        crf: int | None = None,
        preset: str | None = None,
        chunk_rotate_q=None,
    ) -> None:
        """Initialize chunked video writer.

        Args:
            output_dir: Directory for video chunks.
            width: Video width in pixels.
            height: Video height in pixels.
            chunk_duration: Duration of each chunk in seconds.
            fps: Frames per second.
            codec: Video codec (default from config.VIDEO_ENCODING).
            pix_fmt: Pixel format (default from config.VIDEO_PIXEL_FORMAT).
            crf: Constant Rate Factor (default from config.VIDEO_CRF).
            preset: Encoding preset.
            chunk_rotate_q: Optional multiprocessing.Queue for rotation notifications.
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.width = width
        self.height = height
        self.chunk_duration = chunk_duration
        self.fps = fps
        self.codec = codec if codec is not None else config.VIDEO_ENCODING
        self.pix_fmt = pix_fmt if pix_fmt is not None else config.VIDEO_PIXEL_FORMAT
        self.crf = crf if crf is not None else config.VIDEO_CRF
        self.preset = preset if preset is not None else config.VIDEO_PRESET
        self.chunk_rotate_q = chunk_rotate_q

        self._current_writer: VideoWriter | None = None
        self._chunk_index = 0
        self._chunk_start_time: float | None = None
        self._start_time: float | None = None
        self._lock = threading.Lock()

    @property
    def start_time(self) -> float | None:
        """Get the start time of the recording."""
        return self._start_time

    @property
    def chunk_paths(self) -> list[Path]:
        """Get list of all chunk file paths."""
        return sorted(self.output_dir.glob("chunk_*.mp4"))

    def _get_chunk_path(self, index: int) -> Path:
        """Get path for a chunk by index."""
        return self.output_dir / f"chunk_{index:04d}.mp4"

    def _start_new_chunk(self, timestamp: float) -> None:
        """Start a new video chunk.

        Defensively handles errors during the previous chunk's close and new
        chunk creation so that a single rotation failure cannot kill the video
        writer process (and therefore the entire recording).
        """
        if self._current_writer is not None:
            try:
                self._current_writer.close()
            except Exception:
                logger.exception(
                    "Error closing chunk {} during rotation — "
                    "continuing with new chunk (at most one final frame lost)",
                    self._chunk_index - 1,
                )
                # Ensure old writer is discarded even on failure
                self._current_writer = None
            # Notify about completed chunk
            if self.chunk_rotate_q is not None:
                try:
                    self.chunk_rotate_q.put({
                        "type": "chunk_rotated",
                        "completed_index": self._chunk_index - 1,
                        "chunk_start_time": self._chunk_start_time,
                        "rotation_time": timestamp,
                    }, timeout=5)
                except Exception:
                    logger.warning("Failed to push chunk rotation event, continuing")

        chunk_path = self._get_chunk_path(self._chunk_index)
        self._current_writer = VideoWriter(
            chunk_path,
            width=self.width,
            height=self.height,
            fps=self.fps,
            codec=self.codec,
            pix_fmt=self.pix_fmt,
            crf=self.crf,
            preset=self.preset,
        )
        self._chunk_start_time = timestamp
        self._chunk_index += 1

    def write_frame(
        self,
        image: "Image.Image",
        timestamp: float,
        force_key_frame: bool = False,
    ) -> None:
        """Write a frame, automatically starting new chunks as needed.

        Args:
            image: PIL Image to write.
            timestamp: Unix timestamp of the frame.
            force_key_frame: Force this frame to be a key frame.
        """
        with self._lock:
            if self._start_time is None:
                self._start_time = timestamp

            # Check if we need a new chunk
            needs_new_chunk = (
                self._current_writer is None
                or (
                    self._chunk_start_time is not None
                    and timestamp - self._chunk_start_time >= self.chunk_duration
                )
            )

            if needs_new_chunk:
                self._start_new_chunk(timestamp)
                force_key_frame = True  # First frame of chunk should be key frame

            self._current_writer.write_frame(image, timestamp, force_key_frame)

    def close(self) -> None:
        """Close the current chunk and finalize."""
        import time as _time

        with self._lock:
            if self._current_writer is not None:
                # Notify about the final chunk before closing
                if self.chunk_rotate_q is not None:
                    try:
                        self.chunk_rotate_q.put({
                            "type": "final_chunk",
                            "completed_index": self._chunk_index - 1,
                            "chunk_start_time": self._chunk_start_time,
                            "rotation_time": _time.time(),
                        }, timeout=5)
                    except Exception:
                        logger.warning("Failed to push final_chunk event, continuing")
                self._current_writer.close()
                self._current_writer = None

    def __enter__(self) -> "ChunkedVideoWriter":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.close()


# =============================================================================
# Review-Data Video Pipeline (in-process PyAV: concat / probe / remediate)
#
# These primitives back the native-review path so it never shells out to the
# ffmpeg/ffprobe CLI binaries (which are not bundled and are unreachable from
# the minimal GUI PATH a Finder-launched .app inherits). PyAV is already a hard
# dependency and the same library the recorder writes video with.
# =============================================================================


def _close_container_in_thread(container: av.container.Container) -> None:
    """Close a PyAV container off the main thread to dodge a GIL deadlock.

    Mirrors ``VideoWriter.close`` / ``finalize_video_writer``: closing in the
    calling thread can hang indefinitely (PyAV issue #1053), so close in a
    worker and join with a 15s budget.
    """
    def _close() -> None:
        container.close()

    close_thread = threading.Thread(target=_close)
    close_thread.start()
    close_thread.join(timeout=15)
    if close_thread.is_alive():
        logger.warning("container close thread did not finish in 15s, continuing")


def _discard_failed_output(
    output: "av.container.OutputContainer | None", tmp_path: Path
) -> None:
    """Best-effort teardown when an atomic write fails: close container, drop temp.

    Shared by the concat and remediation rollback paths so they cannot drift.
    """
    if output is not None:
        try:
            _close_container_in_thread(output)
        except Exception:
            pass
    tmp_path.unlink(missing_ok=True)


def concat_video_chunks(rec_dir: str | Path) -> Path:
    """Concatenate ``chunk_*.mp4`` into a single ``rec_dir/video.mp4`` via PyAV.

    In-process stream-copy remux (no re-encode) — fast, lossless, and pixel
    format preserving (the gated yuv420p conversion is a separate step in
    :func:`remediate_pixfmt_for_review`). Replaces the old ``ffmpeg -f concat``
    subprocess so the merge works with nothing installed on PATH.

    The chunks are written by ``ChunkedVideoWriter`` with ``bf=0`` (no
    B-frames), so within each chunk ``PTS == DTS`` and timestamps are monotonic
    from ~0. To stitch them, each subsequent chunk's packet timestamps are
    offset by the cumulative duration of all preceding chunks, preserving a
    single monotonic timeline that starts at ~0. That timeline is load-bearing:
    ``engine/capture.py:get_frame_at`` maps a wall-clock timestamp to a
    video-relative frame, so an accumulated offset error would extract the
    wrong screenshot.

    The output is a plain (non-fragmented) MP4 with the moov atom at the end —
    **not** faststart. ``extract_frames``/``extract_frame`` consume this file
    and moov relocation is documented to break them (see ``finalize_video_writer``).

    The write is atomic: packets are muxed into a temp sibling that is
    ``os.replace``\\d onto ``video.mp4`` only on success, so a present
    ``video.mp4`` is always complete (closes the truncated-read window when a
    ``view`` + ``review-data`` race has two callers concat the same recording).

    Args:
        rec_dir: Recording directory containing the ``chunk_*.mp4`` files.

    Returns:
        Path to the merged ``rec_dir/video.mp4``.

    Raises:
        ValueError: If no ``chunk_*.mp4`` files are present.
        RuntimeError: If a chunk cannot be opened/decoded by PyAV — fail loud
            rather than silently emit a truncated video.
    """
    rec_dir = Path(rec_dir)
    chunks = sorted(rec_dir.glob("chunk_*.mp4"))
    if not chunks:
        raise ValueError(f"No chunk_*.mp4 files to concatenate in {rec_dir}")

    out_path = rec_dir / "video.mp4"
    # Dot-prefixed temp sibling: excluded from upload (dotfile filter) and from
    # catalog's ``*.mp4`` glob, so a lingering temp can never pollute either.
    tmp_path = rec_dir / f".video.mp4.{os.getpid()}.tmp"
    tmp_path.unlink(missing_ok=True)

    output = av.open(str(tmp_path), mode="w", format="mp4")
    try:
        out_stream = None
        # Cumulative offset in the (shared) input stream time_base. All chunks
        # come from the same recorder config, so their time_bases match and
        # ``add_stream_from_template`` gives the output that same time_base.
        offset = 0
        for chunk in chunks:
            try:
                inp = av.open(str(chunk))
            except Exception as exc:  # corrupt/undecodable chunk — fail loud
                raise RuntimeError(
                    f"Cannot open video chunk for concat: {chunk.name}: {exc}"
                ) from exc
            try:
                in_stream = inp.streams.video[0]
                if out_stream is None:
                    # add_stream_from_template replaces the removed
                    # add_stream(template=...) API (PyAV 14+).
                    out_stream = output.add_stream_from_template(in_stream)
                # Estimate a fallback frame duration (in time_base ticks) for
                # packets missing one, so the next chunk's offset never overlaps
                # the last frame. average_rate/time_base are Fractions, so this
                # stays exact with no float round-trip.
                rate = in_stream.average_rate
                fallback_dur = round(1 / (rate * in_stream.time_base)) if rate else 1
                chunk_end = 0
                for packet in inp.demux(in_stream):
                    # Flush packets carry no timestamps — skip them.
                    if packet.dts is None or packet.pts is None:
                        continue
                    orig_pts = packet.pts
                    dur = packet.duration or fallback_dur
                    packet.pts = orig_pts + offset
                    packet.dts = packet.dts + offset
                    # Reassigning the stream rescales timestamps into the
                    # output time_base (a no-op here, since they match).
                    packet.stream = out_stream
                    output.mux(packet)
                    end = orig_pts + dur
                    if end > chunk_end:
                        chunk_end = end
                offset += chunk_end
            finally:
                inp.close()
        _close_container_in_thread(output)
        output = None
        os.replace(tmp_path, out_path)
    except BaseException:
        _discard_failed_output(output, tmp_path)
        raise

    return out_path


# Pixel formats AVKit's hardware H.264 decoder plays directly. The recorder
# only ever writes yuv444p or yuv420p, so the practical rule is "remediate
# unless already 4:2:0"; the broader set guards against legacy/imported files.
AVKIT_SAFE_PIX_FMTS: frozenset[str] = frozenset({"yuv420p", "yuvj420p", "nv12"})


def read_pixel_format(video_path: str | Path) -> str:
    """Return a video's pixel format read straight from the stream (no decode).

    Replaces the old ``ffprobe`` shell-out: ``codec_context.pix_fmt`` is
    available immediately after ``av.open`` without decoding a single frame.

    Args:
        video_path: Path to the video file.

    Returns:
        The pixel format name (e.g. ``"yuv444p"``, ``"yuv420p"``).

    Raises:
        RuntimeError: If the container cannot be opened, has no video stream,
            or exposes no pixel format — all genuine "can't process" signals
            (feeds the R9 failure state at the command boundary).
    """
    try:
        container = av.open(str(video_path))
    except Exception as exc:
        raise RuntimeError(
            f"Cannot open video to read pixel format: {video_path}: {exc}"
        ) from exc
    try:
        streams = container.streams.video
        if not streams:
            raise RuntimeError(f"No video stream in {video_path}")
        pix_fmt = streams[0].codec_context.pix_fmt
        if not pix_fmt:
            raise RuntimeError(f"Could not determine pixel format for {video_path}")
        return pix_fmt
    finally:
        container.close()


def needs_pixfmt_remediation(pix_fmt: str) -> bool:
    """True when ``pix_fmt`` is not AVKit-safe and needs a yuv420p re-encode."""
    return pix_fmt not in AVKIT_SAFE_PIX_FMTS


def remediate_pixfmt_for_review(rec_dir: str | Path) -> tuple[Path, bool]:
    """Ensure an AVKit-playable copy of ``rec_dir/video.mp4`` exists for review.

    If ``video.mp4`` is already an AVKit-safe 4:2:0 format, returns it unchanged.
    Otherwise produces a yuv420p re-encode at ``rec_dir/.video_review.mp4`` (a
    leading-dot sibling) and returns that. The dot prefix does double duty: the
    upload enumerator's dotfile filter never ships it (R6) and its existence is
    the idempotency gate (R7) — a second call performs no re-encode.

    The re-encode uses ``libx264`` (deterministic cross-machine output,
    guaranteed present in the bundle via ``_check_av_codecs``; videotoolbox is
    deferred) and preserves the source's presentation timeline. That last point
    is load-bearing: recordings are action-gated (variable frame rate with idle
    gaps), so the source frame PTS — not the stream's misleading ``average_rate``
    — define the timeline the review window overlays events on. Each decoded
    frame's PTS flows through ``reformat`` and the encoder untouched; we never
    set ``frame.pts = None`` (which collapses a VFR recording to a few seconds)
    and never override ``packet.pts`` after encode (the documented PTS-corruption
    bug — see docs/solutions/bug-fixes/video-pts-offset-bframe-corruption-20260322.md).
    ``bf=0`` keeps ``PTS == DTS``.

    Args:
        rec_dir: Recording directory containing ``video.mp4``.

    Returns:
        ``(path, remediated)`` — the playable path and whether a re-encode was
        performed (or a prior one is being reused).

    Raises:
        RuntimeError: If PyAV cannot open/decode the source — a genuine
            "can't process this video" signal (feeds R9 at the command boundary).
    """
    rec_dir = Path(rec_dir)
    video_path = rec_dir / "video.mp4"
    review_path = rec_dir / ".video_review.mp4"

    if not needs_pixfmt_remediation(read_pixel_format(video_path)):
        return video_path, False
    if review_path.exists():
        return review_path, True  # idempotent: a prior re-encode is reused

    tmp_path = rec_dir / f".video_review.mp4.{os.getpid()}.tmp"
    tmp_path.unlink(missing_ok=True)

    try:
        inp = av.open(str(video_path))
    except Exception as exc:
        raise RuntimeError(
            f"Cannot open video for review remediation: {video_path}: {exc}"
        ) from exc

    output = None
    try:
        in_stream = inp.streams.video[0]
        output = av.open(str(tmp_path), mode="w", format="mp4")
        # libx264 explicitly (not config.VIDEO_ENCODING) — the review copy must
        # be deterministic across machines regardless of the recorder's encoder.
        out_stream = output.add_stream("libx264")
        out_stream.width = in_stream.width
        out_stream.height = in_stream.height
        out_stream.pix_fmt = "yuv420p"
        # Preserve the source stream timebase so frame PTS map straight through.
        out_stream.codec_context.time_base = in_stream.time_base
        out_stream.options = {
            "crf": str(config.VIDEO_CRF),
            "preset": config.VIDEO_PRESET,
            "g": str(config.VIDEO_GOP_SIZE),
            "bf": "0",
        }

        for frame in inp.decode(in_stream):
            # reformat carries the frame's pts + time_base through unchanged.
            reformatted = frame.reformat(format="yuv420p")
            for packet in out_stream.encode(reformatted):
                output.mux(packet)
        for packet in out_stream.encode():  # flush
            output.mux(packet)

        _close_container_in_thread(output)
        output = None
        inp.close()
        os.replace(tmp_path, review_path)
    except BaseException as exc:
        _discard_failed_output(output, tmp_path)
        try:
            inp.close()
        except Exception:
            pass
        # Wrap only genuine errors into the structured R9 signal. The
        # `isinstance(exc, Exception)` clause is load-bearing: it lets control-flow
        # BaseExceptions (KeyboardInterrupt/SystemExit) and already-structured
        # RuntimeErrors propagate unchanged instead of being masked as a re-encode
        # failure.
        if isinstance(exc, Exception) and not isinstance(exc, RuntimeError):
            raise RuntimeError(
                f"Cannot re-encode video for review: {video_path}: {exc}"
            ) from exc
        raise

    return review_path, True
