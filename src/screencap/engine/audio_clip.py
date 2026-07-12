"""Clip-a-Moment audio sub-system (SCR-219 U2): FLAC → AAC trim + mux.

The recording's audio is stored SEPARATELY from the video chunks: continuous,
gapless ``audio_NNNN.flac`` files (mono, 16 kHz — see
``engine/recorder.py``'s ``SAMPLERATE``/``CHANNELS``) written on their OWN
wall-clock anchor (``audio_info.timestamp``), which is a distinct instant from
the video anchor (``recording.video_start_time``). Audio is captured the whole
time, so — unlike the action-gated (variable-frame-rate) video — the FLAC has no
idle gaps; concatenating the chunks in order yields one continuous stream
anchored at ``audio_start_time``.

This module reads the ``audio_*.flac`` overlapping an absolute
``[start_ms, end_ms)`` window (milliseconds from recording start, the SAME
anchor U1's video trim uses: ``video_start_time``), maps that video-anchored
range onto the audio timeline via ``audio_start_time`` (handling the
audio-vs-video anchor delta), trims to the SAME output-timeline origin as the
video (``t=0`` is the wall-clock instant ``video_start + start`` for BOTH
streams), resamples to the AAC encoder's format, AAC-encodes, and interleaves
the audio alongside a stream-copy of the already-encoded video-only clip into
one A/V-synced ``.mp4``.

There is no existing FLAC→MP4 mux precedent in the repo (the source chunks are
video-only and the scrubbed cloud copy strips media), so this is built from PyAV
primitives (``av.open`` / ``AudioResampler`` / ``add_stream("aac")`` / ``mux`` /
flush).
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

import av
import numpy as np
from loguru import logger

# The clip's canonical audio config. 16 kHz mono matches the recorder's native
# capture (``recorder.py`` ``SAMPLERATE=16000`` / ``CHANNELS=1``) and is an
# AAC-supported rate/layout, so the common case is a pure sample-format
# conversion (FLAC decodes to s16; the ``aac`` encoder wants ``fltp``). A source
# that differs (e.g. an imported 44.1 kHz stereo FLAC) is genuinely resampled +
# downmixed to this target — the "resample if the layout/rate doesn't match the
# encoder" path (KTD7).
_AAC_RATE = 16000
_AAC_FORMAT = "fltp"
_AAC_LAYOUT = "mono"


def mux_clip_audio(
    rec_dir: Path,
    start: float,
    end: float,
    video_path: Path,
    out_path: Path,
) -> bool:
    """Mux the recording's audio for ``[start, end)`` alongside ``video_path``.

    ``start`` / ``end`` are seconds from recording start (the video anchor). The
    caller (``video.export_clip``) MUST already hold the per-recording
    ``terminal_lock`` — the ``audio_*.flac`` are evictable siblings of the video
    chunks, so this read shares the video pass's critical section.

    Writes a single A/V-synced ``.mp4`` to ``out_path`` (atomically: a
    dot-prefixed ``.tmp`` sibling ``os.replace``\\d on a clean finalize) carrying
    a stream-copy of the video-only ``video_path`` plus a freshly AAC-encoded
    audio track trimmed + aligned to the clip timeline.

    Returns:
        ``True`` when a combined A/V ``out_path`` was written; ``False`` when the
        recording captured no audio overlapping the range (no ``audio_*.flac``,
        no anchor, or the range falls outside the captured audio) — in which case
        ``out_path`` is left untouched and the caller promotes the video-only
        clip. A genuine decode/encode failure raises (the caller downgrades to a
        video-only clip rather than losing the export).
    """
    audio_chunks = _audio_chunks(rec_dir)
    if not audio_chunks:
        return False

    video_start, audio_start = _read_audio_anchors(rec_dir)
    if video_start is None:
        # Without the video anchor we cannot place audio on the clip timeline;
        # a shifted audio track is worse than none. Degrade to video-only.
        logger.debug("clip audio: no video anchor for {}; skipping audio", rec_dir.name)
        return False
    if audio_start is None:
        # No audio_info row: best-effort assume audio shares the video anchor
        # (delta 0). Real recordings always write audio_info, so this only bites
        # partial/hand-built fixtures.
        logger.debug(
            "clip audio: no audio_start_time for {}; assuming video anchor",
            rec_dir.name,
        )
        audio_start = video_start

    # anchor_delta bridges the two anchors: a concatenated-audio position ``p``
    # (seconds from audio_start) sits at recording time ``anchor_delta + p`` and
    # at clip-output time ``anchor_delta + p - start``.
    anchor_delta = audio_start - video_start

    durations = [_flac_duration_s(p) for p in audio_chunks]
    cum: list[float] = []
    running = 0.0
    for d in durations:
        cum.append(running)
        running += d
    total_audio = running

    # Kept range in the concatenated-audio (p) domain: keep samples whose
    # clip-output time lands in [0, clip_dur).
    p_lo = max(0.0, start - anchor_delta)
    p_hi = min(total_audio, end - anchor_delta)
    if p_hi <= p_lo:
        # No captured audio overlaps the requested window.
        return False

    # Only the chunks overlapping [p_lo, p_hi) contribute — never decode the
    # whole (possibly hours-long) recording for a short clip.
    overlap = [
        i
        for i, c0 in enumerate(cum)
        if c0 < p_hi and (c0 + durations[i]) > p_lo
    ]
    if not overlap:
        return False
    i0, i1 = overlap[0], overlap[-1] + 1
    buf_start_p = cum[i0]

    buf = _decode_and_resample(audio_chunks[i0:i1])
    if buf is None or buf.shape[1] == 0:
        return False

    n = buf.shape[1]
    j_lo = max(0, min(n, round((p_lo - buf_start_p) * _AAC_RATE)))
    j_hi = max(0, min(n, round((p_hi - buf_start_p) * _AAC_RATE)))
    kept = buf[:, j_lo:j_hi]
    if kept.shape[1] == 0:
        return False

    # Output PTS (in _AAC_RATE sample units) of the first kept sample. Zero when
    # audio covers t=0; positive (a leading silence gap) when audio started after
    # the clip's t=0 — never negative (p_lo is floored at 0).
    first_kept_p = buf_start_p + j_lo / _AAC_RATE
    pts0 = round((anchor_delta + first_kept_p - start) * _AAC_RATE)
    if pts0 < 0:
        pts0 = 0

    _write_av(video_path, kept, pts0, out_path)
    return True


def _audio_chunks(rec_dir: Path) -> list[Path]:
    """Ordered ``audio_*.flac`` for the recording (legacy ``audio.flac`` last)."""
    chunks = sorted(rec_dir.glob("audio_*.flac"))
    if chunks:
        return chunks
    legacy = rec_dir / "audio.flac"
    return [legacy] if legacy.exists() else []


def _read_audio_anchors(rec_dir: Path) -> tuple[float | None, float | None]:
    """Return ``(video_start, audio_start)`` wall-clock anchors from recording.db.

    ``video_start`` mirrors ``viewer._chunk_offsets_for_concat`` /
    ``get_frame_at`` exactly (``video_start_time`` falling back to
    ``timestamp``); ``audio_start`` is the earliest ``audio_info.timestamp`` (the
    instant the FLAC stream began). Either is ``None`` when unavailable.
    """
    db = rec_dir / "recording.db"
    if not db.exists():
        return (None, None)
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return (None, None)
    try:
        video_start: float | None = None
        audio_start: float | None = None
        try:
            row = conn.execute(
                "SELECT video_start_time, timestamp FROM recording LIMIT 1"
            ).fetchone()
            if row is not None:
                anchor = row[0] if row[0] is not None else row[1]
                if anchor is not None:
                    video_start = float(anchor)
        except sqlite3.Error:
            pass
        try:
            arow = conn.execute(
                "SELECT timestamp FROM audio_info "
                "WHERE timestamp IS NOT NULL ORDER BY timestamp LIMIT 1"
            ).fetchone()
            if arow is not None and arow[0] is not None:
                audio_start = float(arow[0])
        except sqlite3.Error:
            pass
        return (video_start, audio_start)
    finally:
        conn.close()


def _flac_duration_s(path: Path) -> float:
    """Duration of a FLAC in seconds, read cheaply from the header.

    Prefers ``soundfile.info`` (reads STREAMINFO only). Falls back to a PyAV
    stream-duration probe, then to a full decoded sample count — the streaming
    FLAC writer does not always stamp total-samples in the header.
    """
    try:
        import soundfile as sf

        info = sf.info(str(path))
        if info.frames and info.samplerate:
            return float(info.frames) / float(info.samplerate)
    except Exception:
        pass
    try:
        with av.open(str(path)) as c:
            stream = c.streams.audio[0]
            if stream.duration is not None and stream.time_base is not None:
                return float(stream.duration * stream.time_base)
            rate = stream.rate or _AAC_RATE
            samples = 0
            for frame in c.decode(stream):
                samples += frame.samples
            return samples / float(rate)
    except Exception as exc:  # noqa: BLE001 — a bad chunk shouldn't kill the clip
        logger.debug("clip audio: could not probe FLAC duration {}: {}", path.name, exc)
        return 0.0


def _decode_and_resample(chunks: list[Path]) -> np.ndarray | None:
    """Decode ``chunks`` in order through ONE resampler → ``(1, N)`` float32.

    The chunks are contiguous slices of one continuous stream, so feeding them
    into a single :class:`av.AudioResampler` (without flushing between chunks)
    yields a gapless, correctly-aligned ``fltp``/mono/``_AAC_RATE`` buffer whose
    sample ``j`` maps to concatenated position ``chunks[0]_start + j/_AAC_RATE``.
    """
    resampler = av.AudioResampler(
        format=_AAC_FORMAT, layout=_AAC_LAYOUT, rate=_AAC_RATE
    )
    parts: list[np.ndarray] = []
    for chunk in chunks:
        with av.open(str(chunk)) as container:
            if not container.streams.audio:
                continue
            stream = container.streams.audio[0]
            for frame in container.decode(stream):
                # We reconstruct timing from sample counts + known chunk
                # positions, so hand the resampler pts-free frames (avoids its
                # pts bookkeeping / monotonicity warnings). Audio is constant
                # rate, so pure sample passthrough is exact.
                frame.pts = None
                for rframe in resampler.resample(frame):
                    parts.append(rframe.to_ndarray())
    for rframe in resampler.resample(None):  # flush the resampler tail
        parts.append(rframe.to_ndarray())
    if not parts:
        return None
    buf = np.concatenate(parts, axis=1)
    # AudioResampler yields fltp already (shape (1, N)); normalize dtype so the
    # per-frame AudioFrame.from_ndarray path is unconditional float32.
    return np.ascontiguousarray(buf, dtype=np.float32)


def _write_av(
    video_path: Path,
    audio: np.ndarray,
    pts0: int,
    out_path: Path,
) -> None:
    """Stream-copy ``video_path`` + encode ``audio`` into one MP4 at ``out_path``.

    ``audio`` is ``(1, N)`` float32 ``fltp``/mono at ``_AAC_RATE``; ``pts0`` is
    the first sample's output PTS in sample units. The video is copied losslessly
    (mirroring ``video.concat_video_chunks``); the audio is chunked to the AAC
    ``frame_size`` and encoded. ``output.mux`` interleaves the two streams by
    DTS. Atomic: a ``.tmp`` sibling ``os.replace``\\d onto ``out_path`` only on a
    clean finalize (no partial A/V clip is ever delivered).
    """
    # Deferred: reuse video.py's atomic-write primitives (this module is imported
    # from inside video.export_clip, so video is already loaded — no cycle).
    from screencap.engine.video import (
        _close_container_in_thread,
        _discard_failed_output,
        _sweep_stale_temps,
    )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _sweep_stale_temps(out_path.parent, f".{out_path.name}.*.tmp")
    tmp_path = out_path.parent / f".{out_path.name}.{os.getpid()}.{uuid4().hex}.tmp"

    output = av.open(
        str(tmp_path), mode="w", format="mp4",
        container_options={"movflags": "faststart"},
    )
    vin = av.open(str(video_path))
    try:
        # ALL streams must be added BEFORE the first mux() writes the container
        # header — add the video (template) and audio streams up front.
        if not vin.streams.video:
            raise RuntimeError(f"video-only clip {video_path} has no video stream")
        v_in = vin.streams.video[0]
        v_out = output.add_stream_from_template(v_in)

        a_out = output.add_stream("aac", rate=_AAC_RATE)
        a_out.codec_context.format = _AAC_FORMAT
        a_out.codec_context.layout = _AAC_LAYOUT
        a_out.codec_context.sample_rate = _AAC_RATE
        a_out.codec_context.time_base = Fraction(1, _AAC_RATE)

        # --- video: lossless stream-copy of the U1 clip (writes the header) ---
        for packet in vin.demux(v_in):
            if packet.dts is None or packet.pts is None:
                continue  # flush packets carry no timestamps
            packet.stream = v_out
            output.mux(packet)

        # --- audio: AAC-encode the trimmed, aligned samples ---
        # (frame_size is valid once the encoder opened at header write.)
        frame_size = a_out.codec_context.frame_size or 1024
        n = audio.shape[1]
        pos = 0
        while pos < n:
            block = np.ascontiguousarray(audio[:, pos : pos + frame_size])
            aframe = av.AudioFrame.from_ndarray(
                block, format=_AAC_FORMAT, layout=_AAC_LAYOUT
            )
            aframe.sample_rate = _AAC_RATE
            aframe.time_base = Fraction(1, _AAC_RATE)
            aframe.pts = pts0 + pos  # sample units == 1/_AAC_RATE ticks
            for packet in a_out.encode(aframe):
                output.mux(packet)
            pos += frame_size
        for packet in a_out.encode(None):  # flush the encoder
            output.mux(packet)

        vin.close()
        closed = _close_container_in_thread(output)
        output = None
        if not closed:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(f"timed out finalizing A/V clip {out_path}")
        os.replace(tmp_path, out_path)
    except BaseException:
        with contextlib.suppress(Exception):
            vin.close()
        _discard_failed_output(output, tmp_path)
        raise
