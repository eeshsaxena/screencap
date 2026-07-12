"""Tests for the clip audio sub-system (U2): FLAC → AAC trim + mux.

These prove the recording's SEPARATE ``audio_*.flac`` is trimmed to the clip
range and muxed into the SAME ``.mp4`` as the U1 video, staying A/V-synced
across a chunk boundary. The load-bearing assertions are (1) an audio stream is
present, (2) its duration ≈ the requested range, and (3) a distinctive audio
burst planted at a known absolute wall-clock instant decodes at the matching
output time — including when the audio anchor (``audio_info.timestamp``) is
offset from the video anchor (``video_start_time``), and when the source FLAC
rate/layout differs from the AAC encoder (forcing a real resample).

Audio is continuous/gapless (unlike the action-gated VFR video), so the fixture
writes one continuous signal split across FLAC chunks whose boundary the clip
range crosses.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import av
import numpy as np
import pytest
import soundfile as sf
from PIL import Image

from screencap.engine.video import export_clip

_BASE = 1_000_000.0  # recording-start wall-clock anchor (seconds), as in U1
_RATE = 16000

# Two sparse video chunks with a ~2.5s idle gap (action-gated VFR): chunk0 red
# {0.0, 0.5}, chunk1 blue {3.0, 3.5}. Mirrors U1's _SPARSE_TWO_CHUNK.
_SPARSE_TWO_CHUNK = [
    [(0.0, (220, 0, 0)), (0.5, (220, 0, 0))],
    [(3.0, (0, 0, 220)), (3.5, (0, 0, 220))],
]


def _write_video(rec_dir: Path, chunks, base: float, size: int = 48, fps: int = 24):
    from screencap.engine.video import VideoWriter

    for i, frames in enumerate(chunks):
        path = rec_dir / f"chunk_{i:04d}.mp4"
        writer = VideoWriter(str(path), width=size, height=size, fps=fps)
        for dt, color in frames:
            writer.write_frame(Image.new("RGB", (size, size), color=color), base + dt)
        writer.close()
        (rec_dir / f"chunk_{i:04d}_manifest.json").write_text(
            json.dumps({"chunk_start": base + frames[0][0], "chunk_end": base + frames[-1][0]})
        )


def _write_db(rec_dir: Path, video_start: float, audio_start: float | None):
    conn = sqlite3.connect(str(rec_dir / "recording.db"))
    conn.execute("CREATE TABLE recording (video_start_time REAL, timestamp REAL)")
    conn.execute("INSERT INTO recording VALUES (?, ?)", (video_start, video_start))
    conn.execute("CREATE TABLE audio_info (timestamp REAL, sample_rate INTEGER)")
    if audio_start is not None:
        conn.execute("INSERT INTO audio_info VALUES (?, ?)", (audio_start, _RATE))
    conn.commit()
    conn.close()


def _write_flac_chunks(
    rec_dir: Path,
    chunk_durs: list[float],
    *,
    burst_p: float | None,
    burst_len: float = 0.15,
    rate: int = _RATE,
    stereo: bool = False,
):
    """Write ``audio_NNNN.flac`` for one continuous signal split by ``chunk_durs``.

    The signal is silence with a single full-scale 1 kHz burst at concatenated
    position ``burst_p`` seconds (from audio start). ``rate``/``stereo`` let a
    test force a source that differs from the 16 kHz-mono AAC target.
    """
    total = sum(chunk_durs)
    n = int(round(total * rate))
    t = np.arange(n) / rate
    sig = np.zeros(n, dtype=np.float32)
    if burst_p is not None:
        b0 = int(round(burst_p * rate))
        b1 = int(round((burst_p + burst_len) * rate))
        b0, b1 = max(0, b0), min(n, b1)
        sig[b0:b1] = 0.9 * np.sin(2 * np.pi * 1000.0 * t[b0:b1]).astype(np.float32)
    data = np.stack([sig, sig], axis=1) if stereo else sig

    pos = 0
    for i, dur in enumerate(chunk_durs):
        cnt = int(round(dur * rate))
        sf.write(
            str(rec_dir / f"audio_{i:04d}.flac"),
            data[pos : pos + cnt],
            rate,
            format="FLAC",
        )
        pos += cnt


def _audio_frames(path: Path):
    """Yield ``(time_seconds, rms)`` per decoded audio frame, or [] if no audio."""
    out = []
    with av.open(str(path)) as c:
        if not c.streams.audio:
            return out
        a = c.streams.audio[0]
        for frame in c.decode(a):
            arr = frame.to_ndarray().astype(np.float64)
            rms = float(np.sqrt(np.mean(arr * arr))) if arr.size else 0.0
            t = float(frame.pts * frame.time_base) if frame.pts is not None else 0.0
            out.append((t, rms))
    return out


def _has_audio(path: Path) -> bool:
    with av.open(str(path)) as c:
        return bool(c.streams.audio)


def _first_video_time(path: Path) -> float:
    with av.open(str(path)) as c:
        for frame in c.decode(video=0):
            return float(frame.time)
    return 0.0


def _burst_time(path: Path) -> float:
    """Output time of the peak-energy (burst) audio frame."""
    frames = _audio_frames(path)
    assert frames, "no audio frames decoded"
    return max(frames, key=lambda tr: tr[1])[0]


def _audio_span(path: Path) -> tuple[float, float]:
    frames = _audio_frames(path)
    assert frames, "no audio frames decoded"
    return frames[0][0], frames[-1][0]


class TestAudioSyncAcrossBoundary:
    def test_audio_in_sync_across_chunk_boundary(self, tmp_path):
        """R6/KTD7: clip audio present, ≈ range duration, A/V start offset ~0,
        and a burst past the audio-chunk boundary lands at its output time."""
        _write_video(tmp_path, _SPARSE_TWO_CHUNK, _BASE)
        _write_db(tmp_path, video_start=_BASE, audio_start=_BASE)  # anchor_delta 0
        # Continuous 4s audio across two FLAC chunks (boundary at 2.0s); burst at
        # abs 3.2s lives in the SECOND chunk, past the boundary.
        _write_flac_chunks(tmp_path, [2.0, 2.0], burst_p=3.2)

        out = tmp_path / "clip.mp4"
        export_clip(tmp_path, 0, 4000, out)

        assert out.is_file()
        assert _has_audio(out), "clip must carry an audio stream"

        first_a, last_a = _audio_span(out)
        # Duration tracks the 4s range (audio not collapsed / not truncated).
        assert abs(last_a - 4.0) <= 0.3, f"audio duration {last_a:.3f}s off 4.0s"
        # A/V start offset ~0: audio and video both open at ~t=0.
        first_v = _first_video_time(out)
        assert first_a <= 0.1, f"audio starts late at {first_a:.3f}s"
        assert first_v <= 0.1, f"video starts late at {first_v:.3f}s"
        assert abs(first_a - first_v) <= 0.15, "A/V start offset too large"
        # The burst (abs 3.2s, past the audio-chunk boundary) is in sync.
        assert abs(_burst_time(out) - 3.2) <= 0.25, (
            f"burst at {_burst_time(out):.3f}s, expected ~3.2s (out of sync / collapsed)"
        )

    def test_video_content_preserved_with_audio(self, tmp_path):
        """The muxed clip keeps U1's video (red→blue) intact alongside audio."""
        _write_video(tmp_path, _SPARSE_TWO_CHUNK, _BASE)
        _write_db(tmp_path, video_start=_BASE, audio_start=_BASE)
        _write_flac_chunks(tmp_path, [2.0, 2.0], burst_p=3.2)

        out = tmp_path / "clip.mp4"
        export_clip(tmp_path, 0, 4000, out)

        colors = []
        with av.open(str(out)) as c:
            for frame in c.decode(video=0):
                r, g, b = frame.to_image().convert("RGB").getpixel((24, 24))[:3]
                colors.append("RGB"[max(range(3), key=[r, g, b].__getitem__)])
        assert colors[0] == "R" and "B" in colors
        assert colors.index("R") < colors.index("B")


class TestAnchorOffset:
    def test_audio_anchor_offset_from_video_anchor(self, tmp_path):
        """KTD7: audio anchor ≠ video anchor → alignment uses the delta, not a
        naive assume-equal placement (which would shift the burst by the delta)."""
        _write_video(tmp_path, _SPARSE_TWO_CHUNK, _BASE)
        delta = 0.5  # audio recording began 0.5s after the video anchor
        _write_db(tmp_path, video_start=_BASE, audio_start=_BASE + delta)
        # Burst at abs (_BASE + 2.4s) → concatenated audio position 2.4 - 0.5 = 1.9s.
        _write_flac_chunks(tmp_path, [2.0, 2.0], burst_p=1.9)

        out = tmp_path / "clip.mp4"
        # Clip [1s, 4s): output t=0 == abs _BASE+1.0. Burst abs _BASE+2.4 → out 1.4s.
        export_clip(tmp_path, 1000, 4000, out)

        assert _has_audio(out)
        bt = _burst_time(out)
        # Correct (delta-aware) position is 1.4s; the naive assume-equal bug would
        # place it at 0.9s. Tolerance cleanly separates the two.
        assert abs(bt - 1.4) <= 0.2, f"burst at {bt:.3f}s, expected ~1.4s (anchor delta ignored?)"
        assert abs(bt - 0.9) > 0.25, f"burst at naive (delta-ignored) position {bt:.3f}s"


class TestNoAudio:
    def test_missing_flac_yields_video_only_no_crash(self, tmp_path):
        """No audio_*.flac → a video-only clip, no crash, no audio stream."""
        _write_video(tmp_path, _SPARSE_TWO_CHUNK, _BASE)
        _write_db(tmp_path, video_start=_BASE, audio_start=None)

        out = tmp_path / "clip.mp4"
        export_clip(tmp_path, 0, 4000, out)

        assert out.is_file()
        assert not _has_audio(out), "no FLAC → clip must be video-only"
        # Video still intact.
        with av.open(str(out)) as c:
            assert c.streams.video
            assert any(True for _ in c.decode(video=0))


class TestResample:
    def test_mismatched_rate_layout_resamples(self, tmp_path):
        """Source FLAC at 44.1 kHz stereo ≠ 16 kHz-mono AAC target → resample
        succeeds and the clip carries a valid audio stream."""
        _write_video(tmp_path, [[(0.0, (220, 0, 0)), (0.5, (220, 0, 0)),
                                 (1.0, (220, 0, 0)), (1.5, (220, 0, 0))]], _BASE)
        _write_db(tmp_path, video_start=_BASE, audio_start=_BASE)
        _write_flac_chunks(tmp_path, [2.0], burst_p=1.0, rate=44100, stereo=True)

        out = tmp_path / "clip.mp4"
        export_clip(tmp_path, 0, 2000, out)

        assert out.is_file()
        assert _has_audio(out), "resampled audio must be present"
        with av.open(str(out)) as c:
            a = c.streams.audio[0]
            assert a.rate == _RATE, f"expected {_RATE} Hz AAC, got {a.rate}"
        first_a, last_a = _audio_span(out)
        assert abs(last_a - 2.0) <= 0.3, f"resampled audio duration {last_a:.3f}s off 2.0s"
