"""Muted-span transcript post-processing (SCR-218 U6).

When the mic is muted mid-recording the capture stream stops, so a chunk's FLAC
holds only the *unmuted* audio — a shorter file with the muted spans removed.
Whisper's segment timestamps are therefore in *compressed* FLAC time. This
module maps them back to wall-clock, drops the speech captured in the
stop/start-latency window, and inserts an explicit marker over each muted span,
so no muted speech reaches the **transcript** (and, since scrubbed transcripts
are cloud-bound, the transcript that reaches the cloud).

Scope note: this protects the *transcript* only. The raw ``audio_NNNN.flac`` is
uploaded unscrubbed for cloud recordings, so the ~100 ms of audio captured
between the mute command and the actual stream stop still lands in that FLAC.
Genuinely-muted audio never reaches the cloud (the stream was stopped, so it was
never captured), but trimming that latency sliver from the cloud-bound FLAC is a
separate capture-side concern (see the SCR-218 follow-up).

The exact FLAC-gap boundaries are not recorded (only the ``muted_intervals``
command/confirm times are), so the drop uses a small guard margin around each
span to conservatively remove boundary-latency audio. The guard is symmetric,
which can drop up to ~guard seconds of legitimate speech at each unmute edge —
a deliberate privacy-first trade-off. Precise sample-accurate alignment against
real whisper output is validated in hardware capture QA; the coordinate logic
here is unit-tested with controlled inputs.
"""

from __future__ import annotations

from typing import Any

MUTED_MARKER_TEXT = "[microphone muted]"

# Guard around each muted span (seconds) for the drop test. Comfortably larger
# than the ~100 ms engine poll latency, so a word the user was still saying as
# they pressed mute is removed rather than leaked.
_DEFAULT_GUARD_S = 0.35


def _merge_overlapping(
    intervals: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Coalesce overlapping / touching muted intervals (SCR-254 polish).

    Defensive: a double-open (two mutes without an intervening unmute, or a crash
    that left a stray open interval) would otherwise emit two overlapping
    ``[microphone muted]`` markers. Merging first guarantees one marker per
    contiguous muted region. ``_unmuted_spans`` already tolerates overlaps, so
    this only changes the emitted markers, never the compressed timeline.
    """
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:  # overlapping or adjacent
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _unmuted_spans(
    intervals: list[tuple[float, float]], duration: float
) -> list[tuple[float, float]]:
    """The gaps *between* muted intervals within ``[0, duration]`` — their
    concatenation is the compressed FLAC timeline."""
    spans: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in sorted(intervals):
        if start > cursor:
            spans.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        spans.append((cursor, duration))
    return spans


def _compressed_to_wall(c: float, spans: list[tuple[float, float]]) -> float:
    """Map a compressed FLAC offset to chunk-relative wall-clock by walking the
    unmuted spans (whose concatenation is the compressed timeline)."""
    acc = 0.0
    for span_start, span_end in spans:
        length = span_end - span_start
        if c <= acc + length:
            return span_start + (c - acc)
        acc += length
    # Past the last unmuted sample: clamp to the end of the last span.
    return spans[-1][1] if spans else c


def apply_muted_intervals_to_segments(
    segments: list[dict[str, Any]],
    muted_intervals: list[tuple[float, float | None]],
    chunk_start_ts: float,
    chunk_end_ts: float,
    *,
    guard_s: float = _DEFAULT_GUARD_S,
    marker_text: str = MUTED_MARKER_TEXT,
) -> tuple[list[dict[str, Any]], str]:
    """Rewrite a chunk's transcript segments for muted spans.

    Args:
        segments: chunk-relative whisper segments ``[{start, end, text}, ...]``
            in compressed FLAC time.
        muted_intervals: recording-relative ``[(start, end | None), ...]``; an
            open interval (``end`` is ``None``) is treated as muted to chunk end.
        chunk_start_ts / chunk_end_ts: the chunk's recording-relative span.

    Returns ``(segments, joined_text)`` where segments are re-expanded to
    wall-clock, latency-window speech is dropped, and a marker spans each muted
    interval. With no muted intervals the input is returned unchanged.
    """
    duration = chunk_end_ts - chunk_start_ts

    # Clamp intervals to chunk-relative [0, duration]; open -> chunk end.
    rel: list[tuple[float, float]] = []
    for start, end in muted_intervals:
        rs = max(0.0, start - chunk_start_ts)
        re_ = duration if end is None else min(duration, end - chunk_start_ts)
        if re_ > rs:
            rel.append((rs, re_))

    if not rel:
        return segments, _join_text(segments)

    # Coalesce overlaps so a defensive double-open yields one marker, not two.
    rel = _merge_overlapping(rel)

    spans = _unmuted_spans(rel, duration)

    kept: list[dict[str, Any]] = []
    for seg in segments:
        ws = _compressed_to_wall(float(seg["start"]), spans)
        we = _compressed_to_wall(float(seg["end"]), spans)
        if _overlaps_any(ws, we, rel, guard_s):
            continue  # latency-window / muted speech — dropped, never leaked
        kept.append({**seg, "start": ws, "end": we})

    for rs, re_ in rel:
        kept.append({"start": rs, "end": re_, "text": marker_text})

    kept.sort(key=lambda s: s["start"])
    return kept, _join_text(kept)


def _overlaps_any(
    start: float, end: float, intervals: list[tuple[float, float]], guard: float
) -> bool:
    for m_start, m_end in intervals:
        if start < m_end + guard and end > m_start - guard:
            return True
    return False


def _join_text(segments: list[dict[str, Any]]) -> str:
    return " ".join(str(s.get("text", "")).strip() for s in segments).strip()
