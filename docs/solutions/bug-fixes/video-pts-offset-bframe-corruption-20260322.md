---
title: "Video PTS offset and corruption in action-gated recording mode"
date: 2026-03-22
problem_type: bug_fix
component: screencap.engine.video
platform: macos
severity: high
symptoms:
  - "ffprobe reports start_time=33.5 instead of 0 for action-gated recordings"
  - "Video players show 30+ seconds of nothing before content appears"
  - "Frames appear in scrambled order during playback"
  - "PTS values are non-monotonic (33.5 → 39.8 → 11.9 → 16.5)"
  - "DTS is correctly monotonic but PTS jumps erratically"
root_cause: "packet.pts override after encoding assigns wrong PTS when H.264 B-frames reorder packets"
tags:
  - video
  - h264
  - pts
  - bframes
  - pyav
  - action-gated
  - fmp4
  - screen-recording
files_changed:
  - src/screencap/engine/video.py
  - src/screencap/engine/recorder.py
  - tests/engine/test_video.py
---

# Video PTS offset and corruption in action-gated recording mode

## Problem / Goal

Action-gated recordings (`RECORD_FULL_VIDEO=False`, the default) produced MP4 files with broken playback. When the user was idle before their first action, `ffprobe` reported `start_time=33.500000` and video players showed ~30s of blank content. In the `privacy-test` recording, frames also appeared in completely scrambled order.

## What Didn't Work

The initial ticket hypothesized that `ChunkedVideoWriter` was setting `video_start_timestamp` in the DB at writer creation time instead of first-frame time. This was partially correct (the DB issue was real) but wasn't the primary cause of the PTS corruption.

## Solution

Two interacting bugs, both in `VideoWriter.write_frame()`:

**Bug 1: `packet.pts` override corrupts encoder-assigned timestamps**

```python
# Before (WRONG):
for packet in self._stream.encode(av_frame):
    packet.pts = pts          # 'pts' is for the frame just submitted
    self._container.mux(packet)  # but packet may encode a DIFFERENT frame

# After (CORRECT):
for packet in self._stream.encode(av_frame):
    self._container.mux(packet)  # let encoder's PTS flow through
```

With H.264 B-frames, `stream.encode(frame_N)` may output a packet for a *previously buffered* frame, not frame_N. Overriding `packet.pts` with frame_N's PTS corrupted every packet's timestamp.

**Bug 2: B-frames enabled by default (useless for screen recording)**

The default `faster` preset uses `bframes=3`. B-frames exploit bi-directional temporal prediction — useful for natural video but pointless for screenshots that change discretely.

```python
# Added bf=0 to codec options:
self._stream.options = {
    "crf": str(self.crf),
    "preset": self.preset,
    "g": str(config.VIDEO_GOP_SIZE),
    "bf": "0",  # disable B-frames
}
```

**Bug 3: DB `video_start_time` set at recording start, not first frame**

```python
# Before: set at writer init (recording start)
video_start_timestamp = utils.get_timestamp()
crud.update_video_start_time(db, recording, video_start_timestamp)

# After: update on first frame write
if not _video_start_updated and chunked_writer.start_time is not None:
    crud.update_video_start_time(db, recording_timestamp, chunked_writer.start_time)
```

**Bug 4: `num_copies=2` first-frame workaround (now unnecessary)**

`write_video_event()` wrote the first frame twice as a workaround for B-frame buffering causing the first I-frame to be unavailable. With `bf=0`, the encoder outputs immediately — removed the workaround.

**Files changed:**
- `src/screencap/engine/video.py:107-112` — Added `"bf": "0"` to `VideoWriter._init_stream()` codec options
- `src/screencap/engine/video.py:285` — Added `"bf": "0"` to legacy `initialize_video_writer()`
- `src/screencap/engine/video.py:157,186,352` — Removed `packet.pts = pts` overrides (3 locations)
- `src/screencap/engine/recorder.py:991-1014` — Updated `chunked_write_video_event()` to write DB `video_start_time` on first frame, removed `_has_written` monkeypatch
- `src/screencap/engine/recorder.py:1046-1106` — Removed `num_copies=2` workaround from `write_video_event()`

## Why This Works

1. **Without B-frames**, every packet comes out in display order. `packet.pts == packet.dts` for every packet — no reordering confusion.

2. **Without the `packet.pts` override**, the encoder assigns PTS values correctly based on `av_frame.pts` that we set. The PTS values we set on frames are correct (relative to `_start_time`), it was only the *packet-level* override that corrupted them.

3. **The PTS offset was a consequence of both bugs interacting**: In action-gated mode, the first I-frame was buffered by the B-frame encoder. When the next frame arrived (33.5s later), the encoder output the buffered I-frame's packet — but the override stamped it with the 33.5s PTS of the second frame.

## Debugging Approach

The key diagnostic was comparing PTS vs DTS in `ffprobe` packet output:

```bash
ffprobe -v error -show_entries packet=pts_time,dts_time,flags -select_streams v:0 chunk_0000.mp4
```

This revealed:
- DTS monotonically increasing (0 → 38.2s) — correct decode order
- PTS jumping erratically (33.5 → 39.8 → 11.9) — corrupted display order
- PTS ≠ DTS — B-frame reordering present
- First packet: DTS=0, PTS=33.5 — 33.5s offset

The 33.5s PTS-DTS gap on the first packet was far too large for normal B-frame reordering (which causes ~2-3 frame delays). This pointed to the `packet.pts` override as the culprit.

## Prevention / Gotchas

- **Never override `packet.pts` after `stream.encode()`** in PyAV. The encoder manages PTS correctly through frame reordering. Setting `av_frame.pts` before encoding is fine — that tells the encoder the frame's display time. But the output packet may correspond to a different frame.
- **B-frames are unnecessary for screen recording.** Screenshots change discretely (window switches, text appearing), not via motion blur. Always use `bf=0` for this use case.
- **x264 preset B-frame defaults**: `ultrafast` has `bframes=0`, but `superfast` through `veryslow` all use `bframes=3`. The screencap default `faster` preset was enabling B-frames without anyone realizing the implications.
- **Test PTS correctness explicitly.** The existing video tests only checked `last_pts > 0` — they didn't verify the output file's PTS values. Added tests that decode the output and assert monotonic PTS starting at 0.

## Related

- PR: #117
- Ticket: `docs/epics/02-recording-pipeline/tickets/2026-03-21-fix-video-pts-offset-action-gated.md`
- Evidence: `privacy-test/chunk_0000.mp4` in `screencap-recordings-dev` bucket
- Video compression benchmarks: `docs/solutions/benchmark-results-video-compression.md`
