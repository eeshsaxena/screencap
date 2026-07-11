"""Unit tests for muted-span transcript post-processing (SCR-218 U6).

The transform takes a chunk's whisper segments (in compressed FLAC time — a
muted span removed samples) plus the recording-relative muted intervals, and:
(1) re-expands surviving segments back to wall-clock so they don't collide with
the marker, (2) drops speech captured in the stop/start-latency window (the
leak the plan hardened against), and (3) inserts the ``[microphone muted]``
marker. Correctness here is coordinate logic, tested with controlled inputs;
real whisper output + real FLAC timing is validated in hardware QA.
"""

from __future__ import annotations

from screencap.redaction.muted_marker import (
    MUTED_MARKER_TEXT,
    apply_muted_intervals_to_segments,
)


def _seg(start, end, text="hello"):
    return {"start": start, "end": end, "text": text}


def test_no_muted_intervals_passes_segments_through():
    segs = [_seg(1.0, 2.0, "a"), _seg(3.0, 4.0, "b")]
    out, text = apply_muted_intervals_to_segments(segs, [], 0.0, 60.0)
    assert out == segs
    assert "a" in text and "b" in text
    assert MUTED_MARKER_TEXT not in text


def test_chunk_fully_muted_yields_only_marker():
    # chunk [100,160]; muted the whole chunk. Whatever whisper produced from the
    # (empty) FLAC is dropped; only the marker remains.
    out, text = apply_muted_intervals_to_segments(
        [_seg(0.0, 1.0, "leak")], [(100.0, 160.0)], 100.0, 160.0
    )
    assert [s["text"] for s in out] == [MUTED_MARKER_TEXT]
    assert "leak" not in text
    assert MUTED_MARKER_TEXT in text


def test_speech_before_and_after_a_midchunk_mute_survives_and_realigns():
    # chunk [100,160]; muted [120,135] (15s). FLAC = [0,20] ++ [35,60] in
    # wall-clock -> compressed [0,20]++[20,45]. Speech at compressed 5-8
    # (pre-mute) and 25-28 (post-mute, compressed).
    segs = [_seg(5.0, 8.0, "before"), _seg(25.0, 28.0, "after")]
    out, _text = apply_muted_intervals_to_segments(segs, [(120.0, 135.0)], 100.0, 160.0)
    texts = [s["text"] for s in out]
    assert "before" in texts and "after" in texts
    assert MUTED_MARKER_TEXT in texts

    before = next(s for s in out if s["text"] == "before")
    after = next(s for s in out if s["text"] == "after")
    marker = next(s for s in out if s["text"] == MUTED_MARKER_TEXT)

    # 'before' stays put (chunk-relative 5-8, before the mute at 20).
    assert before["start"] == 5.0
    # 'after' re-expands past the 15s gap: compressed 25 -> wall 25+15 = 40.
    assert after["start"] == 40.0
    # Nothing overlaps the marker span [20,35].
    assert after["start"] >= marker["end"]
    assert before["end"] <= marker["start"]


def test_speech_in_the_stop_latency_window_is_dropped():
    # A word the user was still saying as they hit mute: it sits right at the
    # muted-span boundary and must NOT survive (the cloud-leak case, AE1).
    # chunk [0,60]; muted [20,40]. A segment at compressed ~19.9-20.1 (boundary).
    segs = [_seg(19.9, 20.2, "still-talking"), _seg(2.0, 3.0, "clean")]
    out, text = apply_muted_intervals_to_segments(segs, [(20.0, 40.0)], 0.0, 60.0)
    texts = [s["text"] for s in out]
    assert "still-talking" not in texts  # dropped (guard margin around the span)
    assert "clean" in texts
    assert MUTED_MARKER_TEXT in text


def test_open_interval_marks_to_chunk_end():
    # An interval still open (unmute never happened before chunk end) marks to
    # the chunk's end.
    out, _text = apply_muted_intervals_to_segments(
        [], [(130.0, None)], 100.0, 160.0
    )
    marker = next(s for s in out if s["text"] == MUTED_MARKER_TEXT)
    assert marker["start"] == 30.0  # 130 - 100
    assert marker["end"] == 60.0  # chunk end


def test_marker_text_is_pii_free_and_stable():
    # The marker is cloud-bound; it must be a fixed, non-sensitive string that
    # survives scrubbing unchanged.
    assert MUTED_MARKER_TEXT == "[microphone muted]"
