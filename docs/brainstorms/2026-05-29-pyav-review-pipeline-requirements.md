---
date: 2026-05-29
topic: pyav-native-review-pipeline
---

# PyAV-Native Review-Data Video Pipeline

## Summary

Replace the external `ffmpeg`/`ffprobe` CLI dependency in the native-review-data path with the already-bundled in-process PyAV library, so native review playback works on a machine with nothing installed. Covers the full playback-critical path — chunk concat, pixel-format probe, and yuv444p→yuv420p remediation — and revises the U2 unit of the active upload-review-screen plan.

---

## Problem Frame

The pre-upload review window (the upload-review-screen plan) prepares a recording for native AVKit playback by shelling out to the `ffmpeg` and `ffprobe` binaries — for chunk concatenation, pixel-format probing, and the yuv444p→yuv420p remediation re-encode that AVKit's hardware decoder requires. Those binaries are not bundled and are not guaranteed to be on PATH.

Worse, the failure is structural for the exact operator this feature serves. The review pipeline runs as the `screencap` CLI spawned by the SwiftUI shell, and `CLIClient.mergedEnv()` ([macos/ScreenCap/Controllers/CLIClient.swift:413](macos/ScreenCap/Controllers/CLIClient.swift)) deliberately inherits the app's environment without augmenting PATH. A `.app` launched from Finder/Launchpad gets the minimal GUI PATH (`/usr/bin:/bin:/usr/sbin:/sbin`), which excludes `/opt/homebrew/bin` and `/usr/local/bin` — so `shutil.which("ffmpeg")` fails even on a machine where the operator ran `brew install ffmpeg`. The team already hit this gotcha for the daemon and hardcoded a PATH workaround in [macos/ScreenCap/Resources/com.screencap.daemon.plist:10](macos/ScreenCap/Resources/com.screencap.daemon.plist), but the review-data spawn path doesn't get it.

The cost lands on friend-trial operators: native review silently fails to play chunked or yuv444p recordings, with an error that tells them to install a tool that may not even fix the problem.

---

## Requirements

**Pipeline engine**

- R1. The native-review-data path resolves all video processing through the in-process PyAV library; it does not invoke the `ffmpeg` or `ffprobe` CLI binaries.
- R2. Chunk concatenation (merging `chunk_*.mp4` into a single playable video) is performed in-process via PyAV.
- R3. Pixel-format detection is read directly from the video stream via PyAV — no `ffprobe` invocation and no "remediate anyway" fallback gamble.
- R4. The yuv444p→yuv420p remediation re-encode is performed in-process via PyAV, using the same H.264 encoder family the recorder already writes with.

**Behavior & compatibility**

- R5. Native review playback works end-to-end on a machine with no `ffmpeg`/`ffprobe` binary on PATH — including for chunked, yuv444p recordings.
- R6. The original `video.mp4` consumed by `screencap upload` is unchanged; remediation output remains a review-only artifact. Upload behavior and uploaded bytes are unaffected.
- R7. Remediation is idempotent and gated so it runs at most once per recording (it does not re-encode on every review open).
- R8. moov-atom faststart optimization is unchanged — it continues to shell out to the `ffmpeg` binary and to skip gracefully when the binary is absent.

**Failure behavior**

- R9. When PyAV genuinely cannot decode or process a source video, review surfaces a real "can't process this video" failure state, structurally distinct from a missing-dependency error. The "ffmpeg not found — install ffmpeg" envelope and the ffprobe-missing "remediate anyway" branch are removed.

---

## Acceptance Examples

- AE1. **Covers R1, R5.** Given a machine with no `ffmpeg`/`ffprobe` on PATH and a chunked, yuv444p recording, when the operator opens native review, the chunks merge in-process, the video is remediated to yuv420p, and it plays in AVKit.
- AE2. **Covers R3.** Given a recording whose video is already yuv420p, when review-data runs, the probe detects it and no re-encode occurs.
- AE3. **Covers R7.** Given a recording already remediated once, when review is opened again, no re-encode runs (idempotent / sentinel-gated).
- AE4. **Covers R6.** Given a recording that was reviewed (and remediated) but not yet uploaded, when `screencap upload` runs, it uploads the original `video.mp4`, not the review artifact.
- AE5. **Covers R9.** Given a genuinely corrupt or undecodable source video, when review-data runs, it surfaces a "can't process this video" failure — not a missing-binary error.
- AE6. **Covers R8.** Given no `ffmpeg` binary on PATH, when a recording is finalized, faststart optimization is skipped silently and the video still plays.

---

## Success Criteria

- A friend-trial operator who never installed ffmpeg can open the review window and play any recording — chunked or not, yuv444p or not — with no install step and no dependency error.
- Review processing produces consistent output across machines (pinned PyAV), independent of any host ffmpeg version or build.
- `ce-plan` can revise U2 (and its reuse of `_ensure_single_video`) to a PyAV implementation without re-deciding the engine, the scope, or the failure-state shape. The removed `ffmpeg-not-found` / `remediate-anyway` branches are explicitly gone, not left ambiguous.

---

## Scope Boundaries

- macOS-native AVFoundation/VideoToolbox transcode (Swift-side or a bundled native helper) — not pursued; PyAV achieves the install-burden goal with less surface.
- Changing the recorder's default pixel format to yuv420p (yuv420p-at-source) — stays deferred to its own brainstorm/plan cycle. It is the long-term complement (most new recordings would then skip re-encode), not part of this change.
- moov-atom faststart — stays on the ffmpeg CLI; not migrated to PyAV.
- Chunk-aware AVPlayer fallback (playing unmerged chunks) — superseded by in-process PyAV concat.
- Reducing or removing the project's broader PyAV footprint — out; PyAV is the chosen engine.

---

## Key Decisions

- **Engine = in-process PyAV, not the ffmpeg CLI and not AVFoundation.** PyAV (`av>=10.0.0`) is already a hard dependency, ships inside the app, and is the same library the recorder writes video with. It eliminates the install burden *and* the GUI-launched minimal-PATH fragility with zero new dependency and a single video paradigm. AVFoundation would split the work across the Python/Swift boundary for the same goal; the ffmpeg CLI is verifiably broken for the GUI operator this feature targets.
- **Migrate the whole playback-critical path (concat + probe + remediation), not just remediation.** Chunked recordings die at the concat step without the binary, so a partial migration would not meet the "works out of the box" bar.
- **Keep moov faststart on the CLI.** It is not on the playback-critical path and already skips gracefully when the binary is absent; migrating it adds work for no playback benefit.
- **Probe via PyAV stream attribute instead of ffprobe + "remediate anyway".** PyAV reads the pixel format exactly, so the blind-remediation fallback is unnecessary and is removed.

---

## Dependencies / Assumptions

- PyAV (`av>=10.0.0`) is and remains a hard dependency present in every `screencap` CLI process (verified: [pyproject.toml:49](pyproject.toml); recorder write path at [src/screencap/engine/video.py:280](src/screencap/engine/video.py)).
- `_ensure_single_video` is shared between the HTML viewer and native review ([src/screencap/viewer.py:21](src/screencap/viewer.py)). Migrating concat to PyAV affects both consumers; the HTML viewer's chunked merge stops needing the binary as a side benefit, but the migration must preserve its existing output semantics.
- Remediation re-encode is one-shot, sentinel-gated, and review-only, so its CPU cost is bounded even on software (libx264) encode.

---

## Outstanding Questions

### Deferred to Planning

- [Affects R4][Needs research] Does the bundled PyAV wheel expose the `h264_videotoolbox` hardware encoder? If yes, use it for remediation speed; if no, software libx264 is the acceptable fallback for one-shot review prep.
- [Affects R2][Technical] PyAV concat-via-remux must preserve timestamp/PTS continuity across chunks (the CLI concat demuxer handles this today). Verify the remux path produces a seamlessly seekable merged file.
- [Affects R2, R6][Technical] Reconcile the `_ensure_single_video` docstring-vs-code discrepancy — the docstring says the merged file goes to a temp dir "to avoid polluting uploads," but the code writes `rec_dir/video.mp4` ([src/screencap/viewer.py:22](src/screencap/viewer.py) vs [:54](src/screencap/viewer.py)). Resolve the intended location when reimplementing concat, since it bears on R6 (what `upload` ships).
