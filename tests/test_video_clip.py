"""Tests for the cross-chunk, PTS-preserving video clip trim (U1).

Playback-verifying integration tests on SPARSE (variable-frame-rate,
action-gated) footage. The load-bearing assertion is that the exported clip's
duration matches the requested wall-clock range — idle gaps held as
freeze-frames — rather than collapsing to the sparse decoded-frame count (which
is exactly what a ``frame.pts = None`` re-encode would produce, see KTD1 and
``src/screencap/engine/video.py`` ``remediate_pixfmt_for_review``).

Fixtures build tiny multi-chunk recordings whose per-chunk manifests +
``recording.db`` anchor lets the trim place each chunk at its absolute recording
offset, so a frame written at input timestamp ``base + dt`` sits at absolute
recording time ``dt`` (seconds) — clip range ``[start_ms, end_ms)`` therefore
selects frames whose ``dt`` lies in ``[start_ms/1000, end_ms/1000)``.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path

import av
import pytest
from PIL import Image

from screencap.engine.video import (
    MaskedVideoRequiredError,
    NoFramesInRangeError,
    VideoWriter,
    export_clip_video,
)

_BASE = 1_000_000.0  # arbitrary recording-start wall-clock anchor (seconds)


def _make_recording(
    rec_dir: Path,
    chunks: list[list[tuple[float, tuple[int, int, int]]]],
    *,
    base: float = _BASE,
    with_metadata: bool = True,
    size: int = 48,
    fps: int = 24,
) -> float:
    """Write a synthetic multi-chunk recording under ``rec_dir``.

    ``chunks`` is one list per ``chunk_NNNN.mp4``; each inner entry is
    ``(dt_seconds, (r, g, b))`` — a frame written at input timestamp
    ``base + dt`` with that solid color. Each chunk's manifest records
    ``chunk_start = base + (first dt of the chunk)`` and ``recording.db`` records
    ``video_start_time = base``, so the trim's absolute placement maps a frame's
    recording time back to its ``dt``.

    ``with_metadata=False`` omits the manifests + db so the summed-span fallback
    path is exercised.
    """
    for i, frames in enumerate(chunks):
        path = rec_dir / f"chunk_{i:04d}.mp4"
        writer = VideoWriter(str(path), width=size, height=size, fps=fps)
        for dt, color in frames:
            writer.write_frame(Image.new("RGB", (size, size), color=color), base + dt)
        writer.close()
        if with_metadata:
            first_dt = frames[0][0]
            last_dt = frames[-1][0]
            (rec_dir / f"chunk_{i:04d}_manifest.json").write_text(
                json.dumps({"chunk_start": base + first_dt, "chunk_end": base + last_dt})
            )
    if with_metadata:
        conn = sqlite3.connect(str(rec_dir / "recording.db"))
        conn.execute("CREATE TABLE recording (video_start_time REAL, timestamp REAL)")
        conn.execute("INSERT INTO recording VALUES (?, ?)", (base, base))
        conn.commit()
        conn.close()
    return base


def _decode(path: Path) -> list[tuple[float, "Image.Image"]]:
    """Return ``[(frame.time_seconds, image), ...]`` for every frame in ``path``."""
    container = av.open(str(path))
    try:
        out = []
        for frame in container.decode(video=0):
            out.append((float(frame.time), frame.to_image()))
        return out
    finally:
        container.close()


def _dominant(img: "Image.Image") -> str:
    """'R' / 'G' / 'B' for the largest channel at the image center."""
    r, g, b = img.convert("RGB").getpixel((img.width // 2, img.height // 2))[:3]
    return "RGB"[max(range(3), key=[r, g, b].__getitem__)]


def _pts_list(path: Path) -> list[int]:
    container = av.open(str(path))
    try:
        return [f.pts for f in container.decode(video=0)]
    finally:
        container.close()


def _top_level_atoms(path: Path) -> list[str]:
    """Return the ordered top-level MP4 box types (e.g. ['ftyp','moov','mdat'])."""
    data = path.read_bytes()
    atoms: list[str] = []
    off = 0
    n = len(data)
    while off + 8 <= n:
        size = struct.unpack(">I", data[off : off + 4])[0]
        atype = data[off + 4 : off + 8].decode("latin-1", "replace")
        atoms.append(atype)
        if size == 0:  # extends to EOF
            break
        if size == 1:  # 64-bit largesize
            if off + 16 > n:
                break
            size = struct.unpack(">Q", data[off + 8 : off + 16])[0]
        if size < 8:
            break
        off += size
    return atoms


# Two sparse chunks with a ~2.5s idle gap between them (action-gated VFR).
# Absolute frame times: chunk0 -> {0.0, 0.5} red, chunk1 -> {3.0, 3.5} blue.
_SPARSE_TWO_CHUNK = [
    [(0.0, (220, 0, 0)), (0.5, (220, 0, 0))],
    [(3.0, (0, 0, 220)), (3.5, (0, 0, 220))],
]


class TestSparseDuration:
    def test_duration_matches_wall_clock_range_not_frame_count(self, tmp_path):
        """AE4/KTD1: sparse VFR range → output duration ≈ requested range.

        Only 4 source frames span the [0, 4s) range, so a uniform-cadence
        re-encode (the ``frame.pts = None`` failure) would collapse to well
        under a second. Preserving source PTS holds the idle gaps as
        freeze-frames, so the clip lasts ~4s.
        """
        _make_recording(tmp_path, _SPARSE_TWO_CHUNK)
        out = tmp_path / "clip.mp4"

        export_clip_video(tmp_path, 0, 4000, out)

        assert out.is_file()
        frames = _decode(out)
        times = [t for t, _ in frames]
        # Freeze-frames preserved: last presentation time tracks the 4s range,
        # nowhere near the ~0.17s a 4-frame uniform-cadence clip would show.
        assert times[-1] >= 3.4, f"clip collapsed: last frame at {times[-1]:.3f}s"
        assert abs(times[-1] - 4.0) <= 1.0, f"duration {times[-1]:.3f}s off range 4.0s"
        # Starts at 0, PTS strictly monotonic (KTD2).
        assert times[0] <= 0.05
        pts = _pts_list(out)
        assert pts[0] == 0
        assert all(b > a for a, b in zip(pts, pts[1:])), f"non-monotonic PTS: {pts}"


class TestCrossBoundary:
    def test_range_spanning_two_chunks_is_one_continuous_clip(self, tmp_path):
        """R4: a range crossing a chunk boundary → one file, no seam, red→blue."""
        _make_recording(tmp_path, _SPARSE_TWO_CHUNK)
        out = tmp_path / "clip.mp4"

        export_clip_video(tmp_path, 0, 4000, out)

        frames = _decode(out)
        colors = [_dominant(img) for _, img in frames]
        assert colors[0] == "R", f"clip should open red, got {colors[:3]}"
        assert "B" in colors, "clip should include the 2nd chunk's blue frames"
        # Red before blue — chunk order preserved across the boundary.
        assert colors.index("R") < colors.index("B")
        pts = _pts_list(out)
        assert all(b > a for a, b in zip(pts, pts[1:]))


class TestMidGopStart:
    def test_start_mid_gop_begins_at_requested_second(self, tmp_path):
        """A start between key frames → the frame visible at that instant opens
        the clip at t≈0 (decoded from the preceding key frame, dropped to start)."""
        # One dense chunk: R until 0.375s, G until ~0.6s, B after. GOP has a
        # single key frame at t=0, so t=0.5 is genuinely mid-GOP.
        chunk = [
            (0.0, (220, 0, 0)),
            (0.2, (220, 0, 0)),
            (0.4, (0, 220, 0)),
            (0.6, (0, 220, 0)),
            (0.8, (0, 0, 220)),
            (1.0, (0, 0, 220)),
        ]
        _make_recording(tmp_path, [chunk])
        out = tmp_path / "clip.mp4"

        # 0.5s falls between the (quantized) 0.4 and 0.6 green frames; the frame
        # on screen at 0.5s is green.
        export_clip_video(tmp_path, 500, 1100, out)

        frames = _decode(out)
        assert frames[0][0] <= 0.05, f"clip should begin at ~0, got {frames[0][0]:.3f}"
        assert _dominant(frames[0][1]) == "G", "the frame visible at 0.5s is green"


class TestFailClosed:
    def test_masked_video_upload_on_fails_closed(self, tmp_path):
        """AE3/KTD3: frozen masked_video_upload ON → refuse, write no file."""
        _make_recording(tmp_path, _SPARSE_TWO_CHUNK)
        (tmp_path / ".recording_intent").write_text(
            json.dumps({"masked_video_upload": True})
        )
        out = tmp_path / "clip.mp4"

        with pytest.raises(MaskedVideoRequiredError) as exc:
            export_clip_video(tmp_path, 0, 4000, out)

        assert exc.value.reason == "masked_video_required"
        assert not out.exists(), "no file may be written when failing closed"

    def test_masked_video_upload_off_exports(self, tmp_path):
        """The default flag-OFF posture exports normally (control for the above)."""
        _make_recording(tmp_path, _SPARSE_TWO_CHUNK)
        (tmp_path / ".recording_intent").write_text(
            json.dumps({"masked_video_upload": False})
        )
        out = tmp_path / "clip.mp4"
        export_clip_video(tmp_path, 0, 4000, out)
        assert out.is_file()


class TestBadRange:
    def test_zero_length_range_errors_and_writes_nothing(self, tmp_path):
        _make_recording(tmp_path, _SPARSE_TWO_CHUNK)
        out = tmp_path / "clip.mp4"

        with pytest.raises(NoFramesInRangeError):
            export_clip_video(tmp_path, 2000, 2000, out)
        assert not out.exists()

        with pytest.raises(NoFramesInRangeError):
            export_clip_video(tmp_path, 3000, 1000, out)  # inverted
        assert not out.exists()

    def test_out_of_range_after_footage_errors_and_writes_nothing(self, tmp_path):
        _make_recording(tmp_path, _SPARSE_TWO_CHUNK)
        out = tmp_path / "clip.mp4"
        # Footage ends ~3.5s; a window past it decodes no in-range frames.
        with pytest.raises(NoFramesInRangeError):
            export_clip_video(tmp_path, 60_000, 65_000, out)
        assert not out.exists()

    def test_no_chunks_errors(self, tmp_path):
        out = tmp_path / "clip.mp4"
        with pytest.raises(NoFramesInRangeError):
            export_clip_video(tmp_path, 0, 4000, out)
        assert not out.exists()


class TestFaststart:
    def test_moov_atom_at_front(self, tmp_path):
        """faststart: the moov box precedes mdat so the clip streams immediately."""
        _make_recording(tmp_path, _SPARSE_TWO_CHUNK)
        out = tmp_path / "clip.mp4"
        export_clip_video(tmp_path, 0, 4000, out)

        atoms = _top_level_atoms(out)
        assert "moov" in atoms and "mdat" in atoms, atoms
        assert atoms.index("moov") < atoms.index("mdat"), f"moov not at front: {atoms}"


class TestEvictionLock:
    def test_held_terminal_lock_reports_busy_no_file(self, tmp_path):
        """The read is serialized by the per-recording terminal_lock: while a
        concurrent holder (an eviction pass) owns it, the clip refuses rather
        than tearing a chunk read — and writes no file."""
        from screencap.terminal_stage import TerminalStageBusy, terminal_lock

        _make_recording(tmp_path, _SPARSE_TWO_CHUNK)
        out = tmp_path / "clip.mp4"

        with terminal_lock(tmp_path.name, timeout=5.0):
            with pytest.raises(TerminalStageBusy):
                export_clip_video(tmp_path, 0, 4000, out, lock_timeout=0.2)
        assert not out.exists()


class TestProgress:
    def test_progress_callback_is_invoked_determinately(self, tmp_path):
        """R10/KTD6 hook: on_progress reports (frames_done, frames_total) with a
        non-zero, non-decreasing total so a determinate bar can be driven."""
        _make_recording(tmp_path, _SPARSE_TWO_CHUNK)
        out = tmp_path / "clip.mp4"
        seen: list[tuple[int, int]] = []
        export_clip_video(tmp_path, 0, 4000, out, on_progress=lambda d, t: seen.append((d, t)))

        assert seen, "progress callback was never called"
        assert all(t > 0 for _, t in seen)
        dones = [d for d, _ in seen]
        assert dones == sorted(dones) and dones[-1] >= 1


class TestFallbackNoMetadata:
    def test_summed_span_fallback_without_manifests(self, tmp_path):
        """Missing manifests/db → chunks still assemble back-to-back (best effort).

        The clip must still be produced (it just cannot reconstruct wall-clock
        idle gaps it has no record of).
        """
        _make_recording(tmp_path, _SPARSE_TWO_CHUNK, with_metadata=False)
        out = tmp_path / "clip.mp4"
        export_clip_video(tmp_path, 0, 60_000, out)
        assert out.is_file()
        colors = [_dominant(img) for _, img in _decode(out)]
        assert "R" in colors and "B" in colors
