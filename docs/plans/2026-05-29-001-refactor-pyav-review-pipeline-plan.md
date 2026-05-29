---
title: "refactor: PyAV-Native Review-Data Video Pipeline"
type: refactor
status: active
date: 2026-05-29
deepened: 2026-05-29
origin: docs/brainstorms/2026-05-29-pyav-review-pipeline-requirements.md
---

# refactor: PyAV-Native Review-Data Video Pipeline

## Summary

Replace the external `ffmpeg`/`ffprobe` CLI dependency in the native-review video path with the already-bundled in-process PyAV library, so native review playback works on a machine with nothing installed. The work adds three engine primitives to `src/screencap/engine/video.py` — chunk concat (remux/stream-copy), pixel-format probe (stream attribute), and a gated yuv444p→yuv420p remediation re-encode — rewrites `viewer.py:_ensure_single_video` to call the PyAV concat (also fixing the HTML viewer's no-ffmpeg path), and wires them into the `review-data` subcommand with a real "can't process this video" failure state.

**Relationship to the upstream plan:** This plan revises the *video-processing internals* of **U2** in [docs/plans/2026-05-27-002-feat-upload-review-screen-plan.md](2026-05-27-002-feat-upload-review-screen-plan.md) — which is `status: active` but **not yet implemented**. U2's command scaffolding (registration, `events.jsonl` export, JSON envelope shape, metadata read) is unchanged and remains owned by that plan; U4 here replaces U2's ffmpeg/ffprobe video processing with the PyAV pipeline and removes the `ffmpeg-not-found` / `remediate-anyway` branches U2 proposed. Whoever implements U2 should follow U4 below for the video-processing portion.

---

## Problem Frame

The native-review path prepares a recording for AVKit playback by shelling out to `ffmpeg`/`ffprobe` for chunk concat, pixel-format probing, and the yuv444p→yuv420p remediation re-encode AVKit's hardware H.264 decoder requires. Those binaries are not bundled, and a `.app` launched from Finder/Launchpad gets the minimal GUI PATH (`/usr/bin:/bin:/usr/sbin:/sbin`), so `shutil.which("ffmpeg")` fails even when the operator ran `brew install ffmpeg` — `CLIClient.mergedEnv()` ([macos/ScreenCap/Controllers/CLIClient.swift](macos/ScreenCap/Controllers/CLIClient.swift)) deliberately does not augment PATH. The failure is structural for the friend-trial operator this feature serves: native review silently fails to play chunked or yuv444p recordings, with an error pointing at a tool that may not even fix the problem. See origin doc for the full pain narrative.

PyAV (`av`) is already a hard dependency, ships inside the app, and is the same library the recorder writes video with — so moving the playback-critical path in-process eliminates both the install burden and the GUI-launched minimal-PATH fragility with zero new dependency.

---

## Requirements

*(Traced from origin: [docs/brainstorms/2026-05-29-pyav-review-pipeline-requirements.md](../brainstorms/2026-05-29-pyav-review-pipeline-requirements.md))*

- R1. The native-review-data path resolves all video processing through in-process PyAV; it does not invoke the `ffmpeg`/`ffprobe` CLI binaries.
- R2. Chunk concatenation (merging `chunk_*.mp4` into a single playable video) is performed in-process via PyAV.
- R3. Pixel-format detection is read directly from the video stream via PyAV — no `ffprobe`, no "remediate anyway" fallback.
- R4. The yuv444p→yuv420p remediation re-encode is performed in-process via PyAV, using the same H.264 encoder family the recorder writes with.
- R5. Native review playback works end-to-end on a machine with no `ffmpeg`/`ffprobe` on PATH — including for chunked, yuv444p recordings.
- R6. The original `video.mp4` consumed by `screencap upload` is unchanged; remediation output remains a review-only artifact. Upload behavior and uploaded bytes are unaffected.
- R7. Remediation is idempotent and gated so it runs at most once per recording.
- R8. moov-atom faststart optimization is unchanged — it continues to use the `ffmpeg` binary and to skip gracefully when the binary is absent.
- R9. When PyAV genuinely cannot decode or process a source video, review surfaces a real "can't process this video" failure state, structurally distinct from a missing-dependency error. The `ffmpeg-not-found` envelope and the ffprobe-missing "remediate anyway" branch are removed.

**Origin actors:** Operator (internal-tool-heavy user running ScreenCap in the SwiftUI app; consumes review playback via the native window).
**Origin acceptance examples:** AE1 (Covers R1, R5), AE2 (Covers R3), AE3 (Covers R7), AE4 (Covers R6), AE5 (Covers R9), AE6 (Covers R8).

---

## Scope Boundaries

- **macOS-native AVFoundation/VideoToolbox transcode** (Swift-side or bundled native helper) — not pursued; PyAV achieves the install-burden goal with less surface (origin Key Decision).
- **moov-atom faststart migration to PyAV** — out. `move_moov_atom` stays on the ffmpeg CLI; it is not on the playback-critical path and already skips gracefully when the binary is absent (R8).
- **Chunk-aware AVPlayer fallback** (playing unmerged chunks) — superseded by in-process PyAV concat.
- **Reducing the project's broader PyAV footprint** — out; PyAV is the chosen engine.
- **The `review-data` command's non-video plumbing** (command registration, `events.jsonl` export, JSON-envelope fields, metadata read) — owned by U2 of the upstream plan, not redefined here. This plan only revises U2's video-processing internals (U4).

### Deferred to Follow-Up Work

- **Change recorder default `VIDEO_PIXEL_FORMAT` from `yuv444p` to `yuv420p`** (yuv420p-at-source): Future plan. The long-term complement — most new recordings would then skip the remediation re-encode entirely. Cross-cutting blast radius (ML training pipeline, scrub pipeline, HTML viewer, all existing recordings). yuv444p is a deliberate code-legibility choice (`docs/solutions/benchmark-results-video-compression.md`), so this is a reconciliation, not a bug fix.
- **`h264_videotoolbox` hardware-encoder optimization** for the remediation re-encode: Deferred (see Key Technical Decisions for why libx264 is chosen for v1). Revisit if friend-trial signal shows the one-shot software re-encode is too slow on long recordings.
- **Cross-reference edit to upstream plan U2**: Update U2's `**Approach:**` in [docs/plans/2026-05-27-002-feat-upload-review-screen-plan.md](2026-05-27-002-feat-upload-review-screen-plan.md) to point at this plan's U4 for video processing — otherwise that plan keeps describing the ffmpeg/ffprobe approach this plan removes, misleading whoever implements U2 first. Offered at handoff; not a code unit here, but it must land before/with U2 implementation.
- **Swift-side nullable-metadata decode contract** (upstream U6/U8): the Swift consumer must decode `started_at`/`duration_seconds` as `Double?` and render the timeline with a fallback origin of 0 when `started_at` is null (a playable recording can return `ok: true` with null metadata). This plan only guarantees the Python side serializes `null` (U4); the Swift decode lives in the upstream plan.

---

## Context & Research

### Relevant Code and Patterns

- **Concat reuse point:** `src/screencap/viewer.py:21` (`_ensure_single_video`) — idempotent (early-returns if `video.mp4` exists), single-chunk symlink branch, multi-chunk ffmpeg concat-demuxer (stream copy) writing `rec_dir/video.mp4`, `FileNotFoundError`→"ffmpeg not found" warning. **Only one caller today:** `open_viewer` (`viewer.py:105`, reached via `screencap view`). The `review-data` subcommand is the planned second caller.
- **Docstring-vs-code discrepancy (confirmed):** the docstring says the merged file goes to a temp dir "to avoid polluting uploads," but the code writes `rec_dir/video.mp4` (`viewer.py:54`, and the symlink at `:38`). Many consumers depend on `rec_dir/video.mp4`: `screencap upload` ships it, the HTML viewer renders it, `catalog.py:197` uses it for stub detection, `engine/capture.py:325` and `recorder.py:198` read it.
- **PyAV write path (mirror for remediation encode):** `src/screencap/engine/video.py` — `initialize_video_writer` (`:244`), `write_video_frame` (`:292`), `VideoWriter` (`:41`). Stream options `{"crf", "preset", "g": VIDEO_GOP_SIZE, "bf": "0"}` (`:285`); container opened with `_FRAG_MP4_OPTIONS = {"movflags": "frag_keyframe+empty_moov", "flush_packets": "1"}` (`:30`). `close()` runs the container close in a thread with a 15s join (PyAV GIL-deadlock workaround, issue #1053).
- **PyAV read path (mirror for probe):** `get_video_info` (`video.py:603`) and `extract_frames` (`video.py:536`) — `av.open(path)`, `streams.video[0]`, `stream.codec_context.codec.name`, `stream.decode()`, `frame.to_image()`. A pix_fmt probe is `av.open(path).streams.video[0].codec_context.pix_fmt` — no decode required.
- **Chunk layout:** `chunk_*.mp4` are written by `ChunkedVideoWriter` (`video.py:642`) → `chunk_{index:04d}.mp4`. Each chunk is **video-only** (no audio stream is ever added; audio is separate FLAC via `engine/audio.py:save_flac`). Each is fragmented MP4, libx264, yuv444p, **`bf=0` (no B-frames)**.
- **Upload enumeration (R6 anchor):** `src/screencap/upload.py:124` (`list_recording_files`) iterates `recording_dir.iterdir()` and skips symlinks, non-files, **dotfiles (`p.name.startswith(".")`, `:134`)**, and `_UPLOAD_EXCLUDE = {".db-shm", ".db-wal"}`. A subdir `rglob` loop (`:149`) descends into non-hidden subdirectories.
- **Catalog stub detection:** `catalog.py:194-199` — `has_media = any(d.glob("*.mp4")) or any(d.glob("*.flac")) or (d/"video.mp4").exists()`; `is_stub = uploaded and not has_media and db is not None`. Note: `pathlib.glob("*.mp4")` matches dotfiles in current Python, so a review artifact must not be counted here.
- **CLI JSON-envelope + error pattern:** structured `{ok, schema_version, ...}` with per-command schema constants (`cli/__init__.py:47-55`), `status` (`:1976`) as the reference, error envelope with non-zero exit at `apps` (`:1899`). TTY auto-detect via `_should_default_to_json()` (`:58`). `resolve_recording_dir` path-traversal guard (`config.py:337`). Heavy imports deferred inside command bodies (CLAUDE.md).
- **moov faststart (R8):** `move_moov_atom` (`video.py:474`) shells to `ffmpeg`, guards with `shutil.which`, early-returns on fragmented MP4, skips gracefully when ffmpeg absent. Reached only via `finalize_video_writer(..., fix_moov=True)`, which production never sets (`recorder.py` calls finalize without `fix_moov`), so it is effectively a no-op today. Not on the review path. Leave untouched.

### Institutional Learnings

- `docs/solutions/bug-fixes/video-pts-offset-bframe-corruption-20260322.md` — **the most directly applicable learning.** Two encoder rules the remediation re-encode must honor: (1) **never override `packet.pts` after `stream.encode()`** — set the frame timestamp before encoding and let it flow through `container.mux()` untouched (corroborated by PyAV docs: set `frame.pts = None` and let the encoder assign from its `rate`); (2) **keep `bf=0`** for screen content — B-frames add no value and cause `pts != dts` reordering. The recorder already disables B-frames, which also makes the concat simpler (PTS == DTS, monotonic).
- `docs/solutions/build-errors/macos-pre14-binary-install-failure.md` — "`minos` is contagious": one native dylib with a higher floor raises the whole bundle's minimum. PyAV/av wheels are already bundled (floor macOS 11.0). Relevant if the `av` floor bump (see Decisions) pulls a wheel with a higher deployment target; the CI `Verify deployment target (minos)` step catches it.
- `docs/solutions/build-errors/pyinstaller-frozen-binary-ci-failures.md` — "test what you bundle." The `_smoke-test` already verifies `av`/libx264 loads in the frozen binary (`cli/__init__.py:3896` `_check_av_codecs`); extend smoke coverage to exercise the review concat/remediation path now that it depends on PyAV.
- `docs/solutions/integration-issues/macos-foundation-process-pipe-pitfalls.md` — spawning external binaries from a bundled `.app` is full of non-obvious failure modes (PATH, TCC identity, pipe deadlocks). Moving concat/probe/remediate in-process removes this entire class for the review path.
- `docs/solutions/benchmark-results-video-compression.md` — yuv444p is a deliberate choice (preserves syntax-highlighting color at near-identical size); remediate to yuv420p **only for the AVKit review copy**, never change the source.
- `docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md` — adjacent, one transferable principle: don't let a missing/partial chunk silently yield a truncated merged video; fail or warn loudly.

### External References

*(PyAV 16.1.0 / libavcodec 62.x verified installed; pin is `av>=10.0.0`. The idioms below target PyAV 14+.)*

- **Remux concat** — [PyAV cookbook: basics/remux](https://pyav.basswood-io.com/docs/stable/cookbook/basics.html). Use `output.add_stream_from_template(in_stream)` (the `add_stream(template=...)` parameter was **removed in PyAV 14**). Demux, skip packets where `packet.dts is None` (flush packets), reassign `packet.stream = out_stream` (auto-rescales timestamps), `output.mux(packet)`. For cross-chunk continuity, offset each subsequent chunk's `pts`/`dts` by the running cumulative duration before reassigning the stream; the recorder's `bf=0` means PTS == DTS so the offset is the previous chunk's total duration.
- **Pixel-format probe** — `stream.codec_context.pix_fmt` returns the format name without decoding (verified: returns `"yuv444p"`/`"yuv420p"` immediately after `av.open`).
- **Transcode** — decode → `frame.reformat(format="yuv420p")` → `frame.pts = None` → `out_stream.encode(frame)` → flush with bare `out_stream.encode()`. libx264 supports yuv444p input, but explicit `reformat` is the zero-surprise path. (PyAV issues #281, #933; discussion #1694 on the v14 zero-size-packet regression — use direct packet mutation, not `Packet.update()`.)

---

## Key Technical Decisions

- **Remediation encoder = `libx264`, not `h264_videotoolbox` (for v1).** Both are present in the installed wheel, but libx264 is chosen because: (1) the success criterion "consistent output across machines (pinned PyAV)" favors a deterministic software encoder over a host-hardware-dependent one; (2) libx264 is *guaranteed* present in the bundle (`_check_av_codecs` verifies it at smoke-test time), whereas videotoolbox availability in the shipped wheel is not guaranteed; (3) videotoolbox lacks CRF rate control, adding complexity; (4) the re-encode is a one-shot, sentinel-gated, review-only pass, so the bounded software-encode cost is acceptable. This resolves the origin's "does the bundled wheel expose h264_videotoolbox" question — researched as "yes in dev," decided as "use libx264 anyway." videotoolbox is deferred (Scope Boundaries).
- **Remediation artifact = `rec_dir/.video_review.mp4` (leading-dot sibling).** A single hidden file satisfies R6 and R7 at once: `screencap upload` already excludes dotfiles (`upload.py:134`) so it is never uploaded with **zero upload.py change** (R6/AE4); and its existence is the idempotency gate (R7/AE3). This collapses U2's proposed `video_review.mp4` + separate `.review_video_pixfmt` sentinel into one file. Catalog `has_media` is adjusted (U3) to ignore hidden `*.mp4` so a lingering artifact never masks stub detection.
- **Merged `video.mp4` stays at `rec_dir/video.mp4`; the docstring is corrected.** This resolves the origin's docstring-vs-code discrepancy in favor of the code's actual behavior, because many consumers depend on that location (upload, HTML viewer, catalog, capture, recorder stats). For chunked recordings the merged file is still yuv444p and is what `upload` ships — R6-compliant. The PyAV concat must preserve *output semantics* (one seekable, playable video), not byte-identity with the old ffmpeg output.
- **Concat is stream-copy remux, not re-encode.** Mirrors today's `ffmpeg -c copy` — fast, lossless, keeps yuv444p. Pixel conversion is the separate, gated remediation step; the two artifacts (`video.mp4` yuv444p for upload/HTML, `.video_review.mp4` yuv420p for AVKit) have distinct consumers and must not be fused.
- **Engine primitives live in `src/screencap/engine/video.py`.** It is the established PyAV home (`extract_frames`, `get_video_info`, `_is_fragmented_mp4`, all writers), so the new concat/probe/remediate functions sit alongside the patterns they reuse (container-close-in-thread, `_FRAG_MP4_OPTIONS`). The implementer may extract a focused submodule if `video.py` grows uncomfortably.
- **Bump the declared `av` floor to match the API used.** The concat uses `add_stream_from_template`, which requires `av>=14`. The shipped wheel is 16.1.0, but the declared `>=10.0.0` pin is now inaccurate. Bump to `av>=14.0.0` in `pyproject.toml` (folded into U1) and verify the CI minos check still passes. Fallback if the bump raises the macOS floor: keep `>=10` and write version-tolerant concat (`add_stream_from_template` if present, else `add_stream(template=...)`).
- **Failure state is structural, not message-based (R9).** The `review-data` command distinguishes a genuine decode/process failure (`{ok: false, error: "can't process this video: ..."}`, non-zero exit) from the removed missing-binary error. The probe never falls back to "remediate anyway": if PyAV opens the file it reads the exact pix_fmt; if PyAV cannot open it, that *is* the genuine failure.

---

## Open Questions

### Resolved During Planning

- **Does the bundled PyAV wheel expose `h264_videotoolbox`?** (origin, affects R4) — Researched: present in the dev wheel (16.1.0). Resolved by *not* using it for v1; libx264 is chosen for determinism + guaranteed availability (see Key Technical Decisions).
- **Does PyAV remux preserve PTS/DTS continuity across chunks?** (origin, affects R2) — Resolved: yes, via `add_stream_from_template` + skip-`dts-None` + cumulative-duration offset. The recorder's `bf=0` (no B-frames) makes PTS == DTS, so the offset is simply each chunk's total duration. Verification scenario in U1 asserts monotonic output PTS and seamless seek.
- **`_ensure_single_video` location discrepancy (temp dir vs `rec_dir/video.mp4`)?** (origin, affects R2/R6) — Resolved: keep `rec_dir/video.mp4` (many consumers depend on it), fix the docstring. The review-only yuv420p artifact is the separate `.video_review.mp4` (see Key Technical Decisions).
- **How is the review artifact kept out of uploads (R6) without a new exclusion list?** — Resolved: the leading-dot name reuses the existing upload dotfile filter.
- **Should the concat output use `movflags=faststart`?** — Resolved: no. The merged `video.mp4` is consumed by `extract_frames`/`get_frame_at`, and moov relocation is documented to break extraction (`video.py:384`, `:426`). Output a plain MP4 with moov-at-end (see U1). Progressive-seek (moov-at-front) would only ever apply to `.video_review.mp4`.

### Deferred to Implementation

- **Exact concat PTS-offset arithmetic** (whether `in_stream.duration` suffices vs tracking last `packet.pts + duration`) — best chosen against real multi-chunk recordings; both approaches are in the external research. The contract is "monotonic, seamlessly seekable merged output."
- **AVKit-safe pixel-format set** — start with `{"yuv420p", "yuvj420p", "nv12"}`; the recorder only ever writes `yuv444p` or `yuv420p`, so the practical rule is "remediate unless already 4:2:0." Pin the exact set when wiring the probe.

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```mermaid
flowchart TD
    A[review-data --json &lt;name&gt;] --> B[resolve_recording_dir]
    B --> C{video.mp4 exists?}
    C -- no, chunks present --> D[PyAV concat chunks → rec_dir/video.mp4<br/>stream copy, PTS offset per chunk]
    C -- single chunk --> E[symlink chunk → video.mp4]
    C -- yes --> F[use existing video.mp4]
    D --> G
    E --> G
    F --> G[PyAV probe: codec_context.pix_fmt]
    G --> H{AVKit-safe 4:2:0?}
    H -- yes --> I[video_path = video.mp4<br/>remediated = false]
    H -- no --> J{.video_review.mp4 exists?}
    J -- yes --> K[video_path = .video_review.mp4<br/>remediated = true · idempotent]
    J -- no --> L[PyAV transcode → .video_review.mp4<br/>libx264 yuv420p, bf=0, frame.pts=None]
    L --> K
    I --> M[ok:true envelope · video_path, events_path,<br/>started_at, duration_seconds, video_pixfmt_remediated]
    K --> M
    D -. PyAV cannot open/decode .-> X[ok:false · 'can't process this video'<br/>non-zero exit · NOT missing-binary]
    G -. .-> X
    L -. .-> X
```

`.video_review.mp4` (leading dot) → excluded from `screencap upload` by the existing dotfile filter; existence gates the re-encode. `move_moov_atom` (ffmpeg CLI, R8) is on the recorder-finalize path, not shown here — unchanged.

---

## Implementation Units

### U1. PyAV chunk concatenation + `_ensure_single_video` rewrite

**Goal:** Add an in-process PyAV chunk-concat primitive to the engine and rewrite `viewer.py:_ensure_single_video` to use it, removing the ffmpeg subprocess (and the missing-binary branch) from the concat path. This also fixes the HTML viewer's no-ffmpeg case as a side benefit.

**Requirements:** R1, R2, R5 (origin AE1 — concat half).

**Dependencies:** None.

**Files:**
- Modify: `src/screencap/engine/video.py` (add `concat_video_chunks(rec_dir) -> Path` or similar)
- Modify: `src/screencap/viewer.py` (`_ensure_single_video` calls the engine fn; fix docstring; drop `FileNotFoundError`/"ffmpeg not found" branch)
- Modify: `pyproject.toml` (`av>=10.0.0` → `av>=14.0.0`; see Key Technical Decisions for the fallback)
- Test: `tests/engine/test_video.py` (extend) and/or `tests/test_viewer.py` (extend)

**Approach:**
- New engine function opens each `sorted(rec_dir.glob("chunk_*.mp4"))` input, creates one output video stream via `output.add_stream_from_template(in_stream)`, demuxes packets, skips `packet.dts is None`, offsets `pts`/`dts` by the running cumulative duration, reassigns `packet.stream = out_stream`, and muxes. Output → `rec_dir/video.mp4`.
- **Write atomically:** concat into a temp sibling (e.g. `video.mp4.tmp-<pid>`) and `os.replace` into `rec_dir/video.mp4` on success, mirroring `exporter.export_recording`'s `.tmp`-then-rename. This makes the existence-gate self-consistent (a present `video.mp4` is always complete) and closes the truncated-read window when a `view` + `review-data` race has two callers concat the same recording, or a reader (upload enumeration / browser fetch) observes mid-write. Replaces the old non-atomic `ffmpeg -y` write.
- Preserve `_ensure_single_video`'s existing contract exactly: early-return if `rec_dir/video.mp4` exists (idempotent); single-chunk → symlink branch unchanged; no chunks → no-op. Only the multi-chunk concat body changes engine.
- **Preserve the timestamp→frame mapping.** The merged PTS timeline must start at ~0 and match the summed chunk durations to within one frame, because `engine/capture.py:get_frame_at` → `extract_frame(video.mp4, ts)` maps a wall-clock timestamp to a video-relative frame; an accumulated offset error would return the wrong screenshot for an action event. The cumulative-duration offset is what preserves this — assert it (test scenarios below), not just "a frame decodes past the boundary".
- **Fail loud on a corrupt/undecodable chunk** (raise a clear error) rather than silently producing a truncated video (institutional learning). The genuine-failure path feeds R9 at the U4 boundary.
- Reuse the container-close-in-a-thread (15s join) pattern from `VideoWriter.close()` for the output container.
- **Container format: plain MP4 with the moov atom at the end — do NOT apply `movflags=faststart` to `rec_dir/video.mp4`.** `extract_frames`/`get_frame_at` consume this file, and moov relocation is documented to break frame extraction (`video.py:384` "this causes extract_frames to fail"; `video.py:426` "fix_moov will cause extract_frames() to fail!!!"). A plain non-fragmented MP4 (moov-at-end) is seekable enough for the HTML viewer and is what `extract_frames` already handles. If progressive-seek (moov-at-front) is ever wanted, apply it only to `.video_review.mp4` (U3), which `extract_frames` never reads.

**Patterns to follow:**
- `src/screencap/engine/video.py` `VideoWriter` / `initialize_video_writer` for container/stream setup and close-in-thread.
- `src/screencap/exporter.py` `export_recording` — the `.tmp`-then-`os.replace` atomic-write idiom.
- PyAV remux cookbook idiom (External References).

**Test scenarios:**
- Happy path: 2+ yuv444p chunks → single `video.mp4`; decoded frame count equals the sum of input frames; output PTS is monotonic and starts at ~0. *(Covers AE1 — concat half.)*
- Happy path: merged video is seekable — decoding at a timestamp past the first chunk boundary returns a frame (continuity across the boundary holds).
- Happy path: timestamp→frame mapping — for a 2-chunk recording, `extract_frame(video.mp4, t)` at a timestamp just after the boundary returns a frame whose content matches the correct source chunk (merged timeline matches summed chunk durations to within one frame). Guards the `capture.py:get_frame_at` consumer.
- Edge case: exactly one chunk → `video.mp4` is created as a symlink to the chunk (existing branch preserved).
- Edge case: `video.mp4` already exists → function is a no-op (idempotency; no re-concat).
- Edge case: no `chunk_*.mp4` present → no-op, no error.
- Edge case: atomic write — a present `video.mp4` is always complete; if concat fails partway, no partial `video.mp4` is left (only the temp sibling, which is cleaned up). Assert no `*.tmp-*` residue on success.
- Error path: a chunk that PyAV cannot open/decode → raises a clear error (not a silent truncated output, not a "ffmpeg not found" message).
- Integration: run with no `ffmpeg` binary on PATH (monkeypatch `PATH=""` or assert no subprocess spawn) → concat still succeeds.

**Verification:**
- `_ensure_single_video` on a chunked recording produces a playable `rec_dir/video.mp4` with no ffmpeg on PATH.
- `screencap view` on a chunked recording still renders (HTML viewer path unchanged in behavior).

---

### U2. PyAV pixel-format probe

**Goal:** Read a video's pixel format directly from the PyAV stream, with a helper that decides whether AVKit remediation is needed — replacing any `ffprobe` call and the "remediate anyway" gamble.

**Requirements:** R3 (origin AE2 — probe half).

**Dependencies:** None (parallel to U1).

**Files:**
- Modify: `src/screencap/engine/video.py` (add `read_pixel_format(video_path) -> str` and `needs_pixfmt_remediation(pix_fmt) -> bool`)
- Test: `tests/engine/test_video.py` (extend)

**Approach:**
- `read_pixel_format` opens the file with `av.open`, reads `streams.video[0].codec_context.pix_fmt` (no decode), closes. Raise a clear error if the container cannot be opened or has no video stream (feeds R9).
- `needs_pixfmt_remediation` returns `pix_fmt not in AVKIT_SAFE_PIX_FMTS` (start with `{"yuv420p", "yuvj420p", "nv12"}`; pin the set during implementation).

**Patterns to follow:**
- `get_video_info` (`video.py:603`) — `av.open` + `streams.video[0]` read-only access.

**Test scenarios:**
- Happy path: a yuv444p file → `read_pixel_format` returns `"yuv444p"`; `needs_pixfmt_remediation("yuv444p")` is `True`.
- Happy path / Covers AE2: a yuv420p file → returns `"yuv420p"`; `needs_pixfmt_remediation` is `False`.
- Edge case: probe does not decode any frames (assert it returns immediately after `av.open`, e.g. via a large file or a timing/structural check).
- Error path: a file with no video stream → raises a clear error.
- Error path: a non-video / corrupt file → raises a clear error (not `None`, not a silent default).

**Verification:**
- `read_pixel_format` returns the correct format for both a yuv444p and a yuv420p fixture without invoking any external binary.

---

### U3. PyAV yuv444p→yuv420p remediation (review-only artifact) + catalog guard

**Goal:** Produce a gated, review-only yuv420p `.video_review.mp4` when the source is not AVKit-safe, and ensure the artifact is invisible to both upload (already handled) and catalog stub detection.

**Requirements:** R4, R6, R7 (origin AE1 — remediation half, AE2, AE3, AE4).

**Dependencies:** U2 (probe + `needs_pixfmt_remediation`).

**Files:**
- Modify: `src/screencap/engine/video.py` (add `remediate_pixfmt_for_review(rec_dir) -> tuple[Path, bool]`)
- Modify: `src/screencap/catalog.py` (`has_media` ignores hidden `*.mp4` so a lingering `.video_review.mp4` never masks stub detection)
- Modify: `src/screencap/cli/__init__.py` (extend the frozen-binary smoke check near `_check_av_codecs` to exercise concat+remediate on a tiny fixture, not just `libx264` load — "test what you bundle")
- Test: `tests/engine/test_video.py` (extend), `tests/test_catalog.py` (extend)

**Approach:**
- `remediate_pixfmt_for_review`: probe `rec_dir/video.mp4`. If AVKit-safe → return `(video.mp4, False)`. Else if `rec_dir/.video_review.mp4` exists → return `(.video_review.mp4, True)` (idempotent skip). Else decode `video.mp4`, `frame.reformat(format="yuv420p")`, `frame.pts = None`, encode with `libx264` + options mirroring the recorder (`crf`/`preset`/`g=VIDEO_GOP_SIZE`/`bf=0`), flush, write `rec_dir/.video_review.mp4`, return `(.video_review.mp4, True)`.
- **Honor the encoder learnings:** set `frame.pts = None` and never override `packet.pts` after `encode()`; keep `bf=0`; reuse container-close-in-thread.
- `.video_review.mp4` is the idempotency sentinel (R7). For finished recordings `video.mp4` never changes, so existence-as-sentinel is safe.
- Catalog: change `any(d.glob("*.mp4"))` to exclude names starting with `.`, so the review artifact is never counted as media (R6 robustness for the post-upload stub case).
- The decode-failure path raises a clear error (feeds R9 at U4).

**Execution note:** Add the encoder regression guard as an explicit assertion — after re-encode, the output's pix_fmt is yuv420p and its decoded PTS is monotonic from ~0 (the documented prior video bug was PTS corruption on encode).

**Patterns to follow:**
- `initialize_video_writer` / `write_video_frame` (`video.py:244`, `:292`) for encode options and the no-`packet.pts`-override rule.
- `docs/solutions/bug-fixes/video-pts-offset-bframe-corruption-20260322.md`.

**Test scenarios:**
- Happy path / Covers AE1 (remediation half): yuv444p `video.mp4` → `.video_review.mp4` is created, is yuv420p, returns `(<.video_review.mp4>, True)`.
- Happy path / Covers AE2: yuv420p `video.mp4` → no artifact created, returns `(<video.mp4>, False)`.
- Idempotency / Covers AE3: a pre-existing `.video_review.mp4` → no re-encode runs (assert via mtime unchanged or an encode-call spy), returns `(<.video_review.mp4>, True)`.
- Output correctness: remediated file is yuv420p, decodes, and PTS is monotonic from ~0.
- R6 / Covers AE4: after remediation, `upload.list_recording_files(rec_dir)` does **not** include `.video_review.mp4`, and `video.mp4` bytes are unchanged.
- Catalog guard: a recording with `uploaded=True`, real media deleted, but `.video_review.mp4` lingering → `has_media` is `False`, `is_stub` is `True` (artifact does not mask the stub).
- Error path: a `video.mp4` PyAV cannot decode → raises a clear error (not a partial/empty `.video_review.mp4`).
- Edge case: source is a single-chunk symlink (`video.mp4` → chunk) and yuv444p → remediation reads through the symlink and writes a real `.video_review.mp4` file.

**Verification:**
- On a yuv444p recording, a second `remediate_pixfmt_for_review` call performs no re-encode and returns the same path.
- `screencap upload` on a remediated-but-not-uploaded recording uploads `video.mp4`, never `.video_review.mp4`.
- The frozen-binary smoke check runs the concat+remediate path on a fixture and confirms a decodable yuv420p output — so the bundled wheel is proven to run the review pipeline, not just load `libx264`.

---

### U4. Wire the PyAV pipeline into `review-data` + real failure state (revises upstream U2)

**Goal:** Make the `review-data --json` command resolve all video processing through U1–U3 — concat (PyAV) → probe (PyAV) → conditional remediation (PyAV) — returning the playable path and a `video_pixfmt_remediated` flag, with a structural "can't process this video" failure state. Removes the `ffmpeg-not-found` envelope and the ffprobe "remediate anyway" branch from U2's design.

**Requirements:** R1, R5, R9 (origin AE1, AE2, AE3, AE5; R8/AE6 verified as unchanged).

**Dependencies:** U1, U2, U3, **and (hard prerequisite) the upstream plan's U2 scaffolding** — command registration, `events.jsonl` export, JSON-envelope shell, and metadata read must already be merged. This unit defines only U2's video-processing portion, so **U4 is blocked until upstream plan U2 lands** (the critical path is `upstream-U2-scaffolding → U4`). Do not implement U4 in isolation: there would be no command shell to wire the PyAV pipeline into. If the two efforts converge, fold U4's video-processing into the same change that builds upstream U2's `review-data` scaffold.

**Files:**
- Modify: `src/screencap/cli/__init__.py` (`review-data` command body — video processing section)
- Test: `tests/cli/` (revise the upstream U2 `tests/test_cli_review_data.py` plan to assert the PyAV pipeline + failure state)

**Approach:**
- Pipeline inside the command: `resolve_recording_dir(name)` → `_ensure_single_video(rec_dir)` (now PyAV) → `(video_path, remediated) = remediate_pixfmt_for_review(rec_dir)` → ensure `events.jsonl` + read metadata (owned by upstream U2) → emit envelope.
- Success envelope adds `video_pixfmt_remediated: bool` and sets `video_path` to the remediated or original path.
- **Nullable metadata fields (Python serialization only):** `started_at` and `duration_seconds` come from `catalog._read_recording_meta`, which returns `None` on a DB read failure, a missing timestamp, or a recording with zero action events. Declare both fields as `float | null` in the envelope (not always-float) and serialize them as JSON `null` when absent — a perfectly playable recording (`ok: true`) can still carry null metadata. The corresponding Swift-side decode contract (decode as `Double?`, fallback timeline origin of 0 when null) belongs to the upstream plan's Swift units (U6/U8), not here — surfaced as a handoff item under Deferred to Follow-Up Work.
- **R9 failure state:** wrap concat/probe/remediation; on a genuine PyAV error emit `{"ok": false, "schema_version": <n>, "error": "can't process this video: <detail>"}` with non-zero exit. No "install ffmpeg" message; no "remediate anyway" fallback. A missing/corrupt-recording error (e.g. `resolve_recording_dir` `ValueError`) stays distinct from the decode-failure error.
- Defer heavy imports (`import av`, engine fns) inside the command body (CLAUDE.md).
- **R8/AE6 is a non-change:** `move_moov_atom` is untouched, off the review path; the AE6 behavior (faststart skipped silently when ffmpeg absent, video still plays) is already covered by `tests/engine/test_fmp4.py` — cite it rather than re-test.

**Patterns to follow:**
- `status` (`cli/__init__.py:1976`) JSON envelope; `apps` (`:1899`) error envelope + non-zero exit; `info` (`:1347`) TTY auto-detect.
- Upstream plan U2 ([docs/plans/2026-05-27-002-feat-upload-review-screen-plan.md](2026-05-27-002-feat-upload-review-screen-plan.md)) for the command scaffolding this unit slots into.

**Test scenarios:**
- Happy path / Covers AE1: no `ffmpeg`/`ffprobe` on PATH + chunked yuv444p recording → merges in-process, remediates, envelope `ok=True`, `video_path` points at `.video_review.mp4`, `video_pixfmt_remediated=True`.
- Happy path / Covers AE2: already-yuv420p recording → `video_pixfmt_remediated=False`, `video_path` points at `video.mp4`, no re-encode.
- Idempotency / Covers AE3: invoking twice in succession returns identical output and performs no second re-encode.
- Error path / Covers AE5: a genuinely corrupt/undecodable source → `ok=False`, error reads "can't process this video", non-zero exit, and is **not** a missing-binary message.
- Error path: path-traversal name (`../foo`) → rejected by `resolve_recording_dir`, error envelope distinct from the decode-failure error.
- Edge case: recording does not exist → error envelope, non-zero exit.
- Edge case: playable recording with no action events (so `_read_recording_meta` returns `duration_seconds=None`) → `ok=True`, `video_path` valid, `started_at`/`duration_seconds` serialized as JSON `null` (not omitted, not 0) — the Swift `Double?` decode must succeed.
- Integration: end-to-end on a real chunked yuv444p fixture with `PATH` stripped of ffmpeg → JSON decodes, `video_path` plays (decodable yuv420p).

**Verification:**
- `screencap review-data --json <chunked-yuv444p>` on a machine with no ffmpeg/ffprobe returns a valid envelope whose `video_path` is a yuv420p, AVKit-decodable file.
- A corrupt recording returns the structural "can't process this video" failure, never an "install ffmpeg" error.

---

## System-Wide Impact

- **Interaction graph:**
  - `viewer.py:_ensure_single_video` → new engine concat (U1). Both consumers — `screencap view` (HTML viewer) and `screencap review-data` — go through it; the HTML viewer's no-ffmpeg merge is fixed as a side benefit.
  - `review-data` command → engine concat/probe/remediate (U4 → U1/U2/U3).
  - `engine/capture.py:get_frame_at` → `extract_frame(video.mp4, ts)` consumes the merged `video.mp4` for timestamp-based frame extraction — depends on the concat preserving the PTS timeline (U1).
  - `remediate_pixfmt_for_review` writes `.video_review.mp4` → consumed only by review playback; excluded from `upload` (dotfile filter) and from `catalog` `has_media` (U3 guard).
- **Error propagation:** Engine functions raise on genuine decode/process failures; U4 translates them into the R9 `{ok:false, "can't process this video"}` envelope with non-zero exit, kept structurally distinct from missing-recording / path-traversal errors. The Swift review window renders this as a "can't process" state (owned by the upstream plan's U5/U8).
- **State lifecycle risks:**
  - Merged `video.mp4` is written into `rec_dir` and is what `upload` ships for **multi-chunk** recordings — intended, unchanged from today. **Single-chunk** recordings keep the existing symlink branch (`video.mp4` → `chunk_0000.mp4`), and `upload` skips symlinks, so for those `upload` ships `chunk_0000.mp4` directly. R6's "unchanged bytes" is therefore trivially satisfied for single-chunk recordings; the multi-chunk case is the only one where `upload` ships a PyAV-produced `video.mp4`.
  - `.video_review.mp4` is the only new on-disk artifact; it is review-only, dot-prefixed, idempotent, and never uploaded.
  - **Concat is atomic** (temp-then-`os.replace`, U1), so a present `video.mp4` is always complete — closing the truncated-read window when a `view` + `review-data` race has two callers concat the same recording or a reader observes mid-write. The remaining race is benign: concurrent remediation could double-encode `.video_review.mp4` transiently but converges to a valid whole-file rewrite. Gate remediation with a file lock only if observed.
- **API surface parity:** `review-data --json` gains a `video_pixfmt_remediated` field and a structural failure envelope. No other consumer of the JSON-envelope contract changes. The HTML viewer's external behavior is unchanged (still produces a single playable video).
- **Integration coverage:** The "works with no ffmpeg on PATH" guarantee (R5/AE1) and the structural-failure guarantee (R9/AE5) cross the unit-test boundary — assert with `PATH`-stripped fixtures and a corrupt-source fixture rather than mocks.
- **Unchanged invariants:**
  - `move_moov_atom` / faststart (R8) — untouched, ffmpeg-CLI, off the review path, graceful-skip behavior already tested.
  - Recorder write path, `VIDEO_PIXEL_FORMAT=yuv444p` default — unchanged; remediation is review-only.
  - `screencap upload` enumeration, signed-URL handling, and uploaded bytes — unchanged (R6).
  - The `review-data` command's non-video plumbing — owned by upstream U2, not redefined here.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| PyAV concat mishandles cross-chunk PTS, producing a non-seekable or stuttering merged video | Recorder writes `bf=0` (no B-frames) → PTS == DTS, so the cumulative-duration offset is straightforward. U1 asserts monotonic output PTS and a post-boundary seek. External research provides the exact remux idiom. |
| `av>=14` floor bump raises the bundle's macOS deployment target (minos contagion) | CI `Verify deployment target (minos)` step catches it; verify pre-merge. Fallback: keep `>=10` and write version-tolerant concat (`add_stream_from_template` else `add_stream(template=...)`). |
| Remediation re-encode reintroduces the documented PTS-corruption bug | Mirror the recorder exactly: `frame.pts = None`, never override `packet.pts` post-encode, `bf=0`. U3 asserts monotonic output PTS as a regression guard. |
| Software libx264 re-encode is slow on a long recording (one-shot but blocking the review-prep step) | Sentinel-gated (`.video_review.mp4`) so it runs once. If friend-trial signal shows pain: (a) accept as part of prep, (b) revisit `h264_videotoolbox`, or (c) the deferred yuv420p-at-source change (most recordings then skip re-encode entirely). |
| `.video_review.mp4` lingers after post-upload media cleanup and masks stub detection | U3 makes catalog `has_media` ignore hidden `*.mp4`. Any future local-media-cleanup path should also delete review artifacts (flagged for the upstream plan). |
| `glob("*.mp4")` matching the dotfile breaks an unexamined consumer | Only `catalog.py:196` globs `*.mp4` broadly (handled in U3); concat uses `chunk_*.mp4` (does not match); upload uses `iterdir` + `startswith(".")` (excludes it). |
| Bundled wheel lacks `h264_videotoolbox` (origin's open question) | Resolved by choosing libx264 (guaranteed present via `_check_av_codecs`); videotoolbox is not on the v1 path. |
| Frozen-binary build doesn't exercise the new review PyAV path | Extend the `_smoke-test` to run concat+remediate on a tiny fixture ("test what you bundle"); the existing `_check_av_codecs` already covers libx264 load. |

---

## Documentation / Operational Notes

- Extend the frozen-binary smoke test (`cli/__init__.py` `_check_*` / `_smoke-test`) to exercise the review concat + remediation path, not just `libx264` load.
- After the work lands, capture a `docs/solutions/` note on the PyAV remux/concat idiom and the AVKit 4:2:0 constraint — both are net-new territory (no existing learning covers PyAV concat or the VideoToolbox yuv420p requirement).
- No new env vars, permissions, or daemon changes. Removes a runtime dependency (ffmpeg/ffprobe binaries) from the review path.
- Offer at handoff: update upstream plan U2's `**Approach:**` to cross-reference this plan's U4 for video processing.

---

## Alternative Approaches Considered

- **Use `h264_videotoolbox` for the remediation re-encode.** Rejected for v1 — host-hardware-dependent output conflicts with the "consistent output across machines" success criterion, availability in the shipped wheel is not guaranteed, and it lacks CRF rate control. Deferred as a speed optimization.
- **Fuse concat + pixel conversion into one decode→re-encode pass for chunked yuv444p recordings.** Rejected — the merged `video.mp4` (yuv444p, shipped by `upload`, rendered by the HTML viewer) and the remediated `.video_review.mp4` (yuv420p, AVKit only) are distinct artifacts with distinct consumers; fusing them would either re-encode the upload artifact (violating R6) or skip producing the canonical merged video.
- **Store the remediated artifact in a temp/cache dir outside `rec_dir`.** Rejected — co-locating as a dot-prefixed sibling gives free upload exclusion (existing filter), trivial idempotency-via-existence, and matches the upstream plan's framing, at the cost of one small catalog guard. The cache approach adds a lifecycle/orphan-cleanup concern for no benefit here.
- **Migrate moov faststart to PyAV too.** Rejected — R8 keeps it on the ffmpeg CLI; it is off the playback-critical path, already skips gracefully when ffmpeg is absent, and is effectively a no-op for the project's fragmented-MP4 files.
- **AVFoundation/VideoToolbox transcode in Swift / a native helper.** Rejected in the origin brainstorm — splits the work across the Python/Swift boundary for the same goal; PyAV reaches the install-burden target with less surface.

---

## Sources & References

- **Origin document:** [docs/brainstorms/2026-05-29-pyav-review-pipeline-requirements.md](../brainstorms/2026-05-29-pyav-review-pipeline-requirements.md)
- **Upstream plan revised by this one:** [docs/plans/2026-05-27-002-feat-upload-review-screen-plan.md](2026-05-27-002-feat-upload-review-screen-plan.md) (U2)
- **Related code:**
  - `src/screencap/engine/video.py` — PyAV write/read paths; new concat/probe/remediate primitives (U1–U3)
  - `src/screencap/viewer.py` — `_ensure_single_video` (rewritten by U1)
  - `src/screencap/upload.py` — `list_recording_files` dotfile exclusion (R6 anchor)
  - `src/screencap/catalog.py` — `has_media` / stub detection (guarded by U3); `_read_recording_meta` (nullable `started_at`/`duration_seconds`)
  - `src/screencap/engine/capture.py` — `get_frame_at` / `extract_frame` consumer of merged `video.mp4` (PTS-timeline dependency, U1)
  - `src/screencap/exporter.py` — `.tmp`-then-`os.replace` atomic-write idiom mirrored by U1
  - `src/screencap/cli/__init__.py` — `review-data` command body (U4), JSON-envelope patterns, `_check_av_codecs`
  - `src/screencap/config.py` — `resolve_recording_dir` guard
  - `pyproject.toml` — `av` dependency floor (U1)
- **Institutional learnings:**
  - [docs/solutions/bug-fixes/video-pts-offset-bframe-corruption-20260322.md](../solutions/bug-fixes/video-pts-offset-bframe-corruption-20260322.md)
  - [docs/solutions/build-errors/macos-pre14-binary-install-failure.md](../solutions/build-errors/macos-pre14-binary-install-failure.md)
  - [docs/solutions/build-errors/pyinstaller-frozen-binary-ci-failures.md](../solutions/build-errors/pyinstaller-frozen-binary-ci-failures.md)
  - [docs/solutions/integration-issues/macos-foundation-process-pipe-pitfalls.md](../solutions/integration-issues/macos-foundation-process-pipe-pitfalls.md)
  - [docs/solutions/benchmark-results-video-compression.md](../solutions/benchmark-results-video-compression.md)
- **External docs:** [PyAV cookbook (remux/transcode)](https://pyav.basswood-io.com/docs/stable/cookbook/basics.html), [PyAV video API (pix_fmt, reformat)](https://pyav.basswood-io.com/docs/stable/api/video.html), [PyAV changelog (v14 add_stream_from_template)](https://pyav.basswood-io.com/docs/15.0/development/changelog.html)
