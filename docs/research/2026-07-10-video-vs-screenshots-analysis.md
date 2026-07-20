---
title: Why Screencap keeps both video and screenshots — a detailed analysis
date: 2026-07-10
type: analysis
status: draft
tags: [capture, video, screenshots, storage, privacy, search, retention]
motivation: >
  Answers "why do we need video and not only screenshots?" and "should screenshots
  always exist / should they be opt-in?" — grounded in the current codebase, to
  inform a possible cut/keep or capture-posture decision.
---

# Why Screencap keeps both video and screenshots

## TL;DR

1. **They are the *same frames* in two serializations, not two capture streams.** In the
   default (action-gated) mode the video encodes the exact frames that would be saved as
   screenshots — both come from one `prev_screen_event`, gated by one `should_save_screen`
   ([recorder.py:631-641](../../src/screencap/engine/recorder.py)). Video is a **superset** at
   the frame-set level; a screenshot is a derived still, never independent source data.

2. **The default is video-only.** `RECORD_VIDEO=True`, `RECORD_IMAGES=False`
   ([config.py:39-43](../../src/screencap/engine/config.py)). An empty `screenshots/` dir is
   the norm, "not an edge" ([RecordingFrameIndex.swift:113-120](../../macos/Screencap/Controllers/RecordingFrameIndex.swift)).
   Screenshots are the **opt-in materialized index**, produced when the search/OCR stack is used.

3. **They are specialized to opposite access patterns.** Video = *sequential watching* (cheap
   at density via H.264 inter-frame compression). Screenshots = *random-access lookup + masking*
   (O(1) JPEG read, OCR-able, maskable per-still). Each is bad at the other's job.

4. **The search index cannot be reconstructed from video today.** The content-index backfill
   hard-*skips* any recording with no `screenshots/` dir
   ([backfill/engine.py:300-306](../../src/screencap/backfill/engine.py)). Frame-from-video
   extraction exists but is wired only to UI thumbnails, never to OCR.

5. **What leaves the machine is video + scrubbed structured data — never the raw DB or the
   search index.** Rich unmasked video upload is gated OFF by default
   (`masked_video_upload`). Screenshots upload only as scrubbed/masked copies, and only when
   they exist.

**Bottom line:** cutting video does **not** simplify search/indexing (that runs on screenshots);
it deletes the *watch* layer (replay/scrub/share) and the temporal training-corpus substrate.
Making screenshots always-materialized does **not** add recoverable information (the frames are
already in the video); it adds double-storage and an unmasked-on-disk privacy footprint. The two
"obvious" simplifications each cut the wrong thing.

---

## 1. The core model: same frames, two serializations

| Capture flag | Default | Meaning |
|---|---|---|
| `RECORD_VIDEO` | `True` | Encode H.264 chunks |
| `RECORD_FULL_VIDEO` | `False` | **Action-gated**: encode only frames near action events |
| `RECORD_IMAGES` | `False` | **Off**: no JPEG stills written to disk |

Source: [config.py:39-43](../../src/screencap/engine/config.py).

In action-gated mode (the default), the screenshot and the video frame are produced from the
**same** capture, at the **same** timestamp, gated by the **same** condition:

```python
# recorder.py:631-641
if should_save_screen:
    events_to_write.append((prev_screen_event, screen_write_q, write_screen_event))     # screenshot
    _video_ok = _disposition.video_allowed if _disposition is not None else True
    if config.RECORD_VIDEO and not config.RECORD_FULL_VIDEO and _video_ok:
        video_event = prev_screen_event._replace(type="screen/video")                    # SAME event data
        events_to_write.append((video_event, video_write_q, write_video_event))
```

Consequences:

- **A screenshot row is always written to `recording.db`** (the action-frame timeline), but the
  image **bytes** are stored only when `RECORD_IMAGES=True` — as a JPEG file (or an inline blob).
  Default: rows exist, pixels live in the video ([recorder.py:758-779](../../src/screencap/engine/recorder.py)).
- **`RECORD_FULL_VIDEO=True`** decouples them: video captures *every* frame at `SCREEN_CAPTURE_FPS`
  (20 fps) while screenshots stay action-gated — video becomes a strict superset.
- **Dedup** (`SCREENSHOT_DEDUP`, default off) is a dHash/hamming gate on the screenshot save; in
  gated mode it also drops the corresponding video frame ([dedup.py](../../src/screencap/engine/dedup.py),
  [recorder.py:600-620](../../src/screencap/engine/recorder.py)).

So "video vs screenshots" is a false binary at capture time. The real question is: for a frame set
that already exists, **which serialization(s) do we materialize, and for whom.**

---

## 2. Definitive consumer map — who reads which artifact

| Consumer | Reads | Purpose | Fallback to the other? |
|---|---|---|---|
| **Content index / OCR** (`index_core`) | **Screenshots** | Extract on-screen text → FTS5 search | **No** — globs `screenshots/` only |
| **`frame.nearest`** (agent frame retrieval) | **Screenshots** | Resolve a timestamp → nearest still stem (pointer-only) | **No** |
| **Post-hoc masking** (`redaction/masking`) | **Screenshots** | Structural masks on stills pre-upload | Stills only |
| **Transcription** (`transcribe`) | **audio.flac** | Speech → text | N/A (independent of both) |
| **`timeline.query` / `transcript.search`** | **recording.db / text** | Structured rows / keyword text | N/A (no pixels) |
| **Library card thumbnail** | **Screenshots**, else **video poster** | First-frame thumbnail | **Yes** → extracts poster from `chunk_0000.mp4` |
| **Review video player** (`VideoPlayerPane`) | **Video** (AVPlayer) | Full-fidelity local replay | **No** |
| **Review timeline** (`TimelinePane`) | **recording.db** events | Event ticks; cursor follows `video.currentTime` | N/A |
| **"What uploads" pane** (`ScreenshotTruthPane`) | **Masked screenshots** | Shows exactly what a cloud copy contains | **No** (by design) |
| **Day playback** (`DayPlaybackEngine`) | **Video** + screenshot frame index | Play across chunks; stills anchor the seek map | Uses **both** |

Key reading: **the machine/search half of the product runs on screenshots; the human-watch half
runs on video.** The only cross-fallback that exists is *thumbnail → video-poster* (one direction,
UI-only). Nothing derives the OCR/search index from video.

---

## 3. Storage & retention economics

Defaults ([config.py:38-87](../../src/screencap/engine/config.py)): H.264 `libx264`, `CRF 23`,
`yuv444p`, `GOP 48`, 15-min chunks (`VIDEO_CHUNK_DURATION=900`), 20 fps cap; JPEG quality 85.
Disk guards: `disk_warn_mb=2000`, `disk_stop_mb=500`. (No production bytes-per-frame telemetry
exists in the tree — a measurement gap, see §7.)

- **For the same action-gated frame set, video is the cheaper serialization.** H.264 inter-frame
  compression (P-frames against a 48-frame GOP) exploits screen redundancy that independent JPEGs
  cannot. "Cut video to save storage" is backwards unless you *also* reduce frame density — at
  which point you've deleted the continuity that replay exists for.
- **Retention is asymmetric.** Video chunks are the eviction unit — evicted per-chunk after upload
  ([retention.py:119-134](../../src/screencap/retention.py)). The `screenshots/` dir is *"kept
  whole until the recording is stubbed"* — never chunk-evicted. So on a long-lived recording,
  screenshots can outlive their video chunks locally. (Worth noting for any always-on-screenshots
  proposal: the persistent-stills footprint is *not* under the per-chunk reclaim policy.)

---

## 4. Cloud & privacy — what actually leaves the machine

The upload enumerates **every** file in the recording dir except a hard denylist, screenshots
included ([upload.py:373-421](../../src/screencap/upload.py)):

- **Never uploaded (hard gate, fails loud):** `recording.db` (+ `-wal`/`-shm`), `*.scrub_failed`,
  `tasks.json` ([upload.py:281-317](../../src/screencap/upload.py)). The **content index
  (`content_index.db`) is likewise local-only**, never uploaded, purged on retroactive disable.
- **Video:** governed by `masked_video_upload` (**default OFF**). OFF → the cloud video copy is the
  **capture-time-blocked** source chunks (sensitive windows never recorded, so never written to
  disk); post-hoc pixel masking is *not* in the path. ON (future) → only `masked_video/` copies may
  ship, enforced by a fail-closed gate ([config.py:714-737](../../src/screencap/engine/config.py),
  [upload.py:348-370](../../src/screencap/upload.py), `SECURITY.md`).
- **Screenshots:** eligible to upload **only as scrubbed/masked copies**, and only when they exist
  (i.e. when the search stack captured them). Default recording → none to ship.

**Search is decoupled from capture.** `content_index_enabled` is a *process-time* gate that reads
screenshots if present; it does not itself turn on `RECORD_IMAGES`
([chunk_processor.py:1053-1093](../../src/screencap/chunk_processor.py)). For search to work, the
app must *also* be capturing stills — the two are wired together at the app/consent layer, not the
engine.

**One seam worth confirming (§7):** the config docstring notes the native review "what uploads"
surface *"currently labels video 'local-only, not uploaded'"* and must be reconciled before
`masked_video_upload` flips ON — while the CLI upload path *is* capable of shipping capture-blocked
chunks. The invariants are solid (raw DB never leaves; rich unmasked video never leaves while the
flag is OFF); the exact video "uploaded-as-blocked vs local-only" label across CLI vs app is a
known, self-flagged reconciliation point, not a settled fact.

---

## 5. Could we collapse to one artifact?

**Drop screenshots, keep only video?** Feasible in theory (the frames are all in the video), but
nothing is built for it and the costs are real:

- The backfill **skips video-only recordings entirely** ([backfill/engine.py:300-306](../../src/screencap/backfill/engine.py));
  `index_core`/`frame_resolve` glob `screenshots/` with no video path.
- Frame-from-video extraction exists (`RecordingFrameIndex.posterFrame` via `AVAssetImageGenerator`,
  gated by a 3-permit decode semaphore; Python `video.extract_frames` via PyAV **streaming** decode)
  but is used only for UI posters. Random-access exact-frame extraction from a GOP-48 H.264 stream
  means decoding from a keyframe — far costlier than an O(1) JPEG read.
- OCR on a re-decoded H.264 frame (`CRF 23`, `yuv444p`) is **double-lossy** vs OCR on the original
  JPEG — a likely accuracy hit for small on-screen text.

**Drop video, keep only screenshots?** Deletes the *watch* layer: the AVPlayer replay
(`VideoPlayerPane`), day-long playback (`DayPlaybackEngine`), the shareable `.mp4`, audio-synced
playback, and the dense-trajectory training corpus — i.e. most of the "Replay, MCP & data flywheel"
strategy track. Search/redaction survive on screenshots; replay and corpus die.

---

## 6. Answering the two driving questions

### "Should we cut video?"

**No — not as an existence question.** Cutting video wouldn't simplify search/indexing (that path
never touches video). It would specifically delete replay, share, day-playback, and the training
corpus. The cost/privacy motivations that usually justify cutting video are **already addressed**
by action-gating (no idle frames), per-chunk eviction, capture-time blocking, and
`masked_video_upload=OFF` (rich video stays local). The only genuinely valid "cut" motivation is
**encoder complexity/reliability** — real, but the sharper move is hardening/sandboxing the encoder,
not deleting the artifact. The honest levers are **density** and **upload policy**, both already
tuned conservative.

### "Should screenshots always exist (vs opt-in)?"

Split it:

- **Always *recoverable*?** — Already true. The frames live in the always-on video and the app
  already extracts stills on demand. `RECORD_IMAGES=False` never loses a moment.
- **Always *materialized on disk*?** — Probably not. That stores the same pixels twice (redundant
  with video, in the least efficient form) and enlarges the **unmasked-stills-on-disk** footprint —
  exactly what the "win on privacy" posture minimizes, which is why materialized stills are gated to
  the search opt-in + consent flow (`content_index_consent_declined`, etc.).

The intuition "they should always exist" is **half-right**: the *information* should never be lost,
and it isn't (video is the superset). Making the JPEG *cache* always-on is the wrong lever.

**The real, strategic decision underneath:** *is searchable / agent-queryable memory a baseline
promise or an opt-in feature?* Today: opt-in — out of the box, recordings are **watchable but not
searchable**; search lights up only after consent, and (crucially) **only for recordings made after
opt-in**, because the backfill can't index video-only history. If the flywheel is the core bet, that
default-dark search is friction; if privacy-conservatism is the wedge, it's correct. The
reconciling middle path — **build the index by extracting frames from the always-on video** — is
architecturally feasible (the poster path proves it) but unbuilt, and carries the decode-cost and
double-lossy-OCR caveats in §5.

---

## 7. Open items / things to confirm before deciding

1. **Measure the real footprint.** No bytes-per-minute telemetry exists for video or screenshots.
   Before any storage-driven decision, measure `chunk_*.mp4` vs an equivalent JPEG set on a real
   recording — the "video is cheaper" claim is sound in theory but unmeasured here.
2. **Reconcile the "what uploads" video label** (§4) — is capture-blocked video actually uploaded
   in the app's current cloud flow, or only via the CLI path? The code flags this itself.
3. **Decide the search posture** (§6) — baseline-derive-from-video vs baseline-always-capture vs
   keep-opt-in. This is the actual product call; the screenshots question is downstream of it.
   **✅ RESOLVED:** baseline-always-capture, but only behind Recall-post-fix-grade guardrails
   (secrets-only scrub, retention bound, at-rest encryption, present-user gating) + a gated default
   flip — see [docs/plans/2026-07-11-001-feat-search-guardrails-default-on-plan.md](../plans/2026-07-11-001-feat-search-guardrails-default-on-plan.md).
   Derive-from-video is deferred (needs the OCR-fidelity spike first).
4. **If search should cover history**, the derive-from-video indexing path (or a capture-time
   default flip) is required — the backfill can't touch video-only recordings today.

---

*Evidence base: four codebase investigations (capture cadence & storage, consumer map, cloud/upload
coupling, derive-from-video feasibility) plus direct reads of `upload.py`, `recorder.py`, and
`config.py`. File:line pointers throughout are the primary sources.*
