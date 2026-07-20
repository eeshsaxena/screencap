---
date: 2026-05-27
topic: upload-review-screen
---

# Pre-Upload Review Screen in the macOS Shell

## Summary

The SwiftUI macOS shell gains a per-recording Upload affordance that, when clicked, opens a dedicated native review window containing a video player and a scrub-synced action timeline. The user makes a binary decision — Upload or Cancel — and the actual upload shells out to the existing `screencap upload` CLI path. No changes are made to `upload.py`, the up-front `local`/`cloud`/`both` CLI choice, or the existing HTML viewer.

---

## Problem Frame

The strategy doc treats UX & native experience as a load-bearing track this quarter and names "% of users opting traces into the training corpus" as the lagging signal that the data flywheel is forming — a metric explicitly tracked as `n/a` until the upload pipeline ships through the consumer surface. The Python upload pipeline already exists (`upload.py`, `screencap upload`), but the macOS SwiftUI shell has zero upload affordances today: the only retry path the app surfaces is the literal string "Run `screencap upload` to retry" in `RecordingStateMachine.swift`, which sends a non-technical operator to a terminal they shouldn't need.

The persona is the internal-tool-heavy operator running Screencap in a SwiftUI app. When they finish a recording, they have no way to (a) push a `local` recording to the cloud from the GUI, or (b) verify what's about to leave their machine before consenting. The HTML viewer covers verification but lives in a separate browser window with no upload action — review and consent are split across two surfaces and neither is native.

Without a native consent surface, the strategic opt-in metric cannot move: any reasonable operator either uploads blindly via the CLI or doesn't upload at all. The cost compounds — every friend-trial recording that isn't consented to is lost training corpus, and every operator who has to open Terminal to share a session is one whose trust in "this is a GUI product" erodes.

---

## Key Flows

- F1. **Operator reviews and uploads a local recording**
  - **Trigger:** A SwiftUI-app operator finished a recording earlier (local mode) and now wants to share it.
  - **Steps:** Operator opens the Recordings list → clicks **Upload** on the desired row → a dedicated review window opens with a video player and a scrub-synced action timeline → operator scrubs through both, confirms nothing sensitive was captured → clicks **Upload** → upload progress is shown in the window → window resolves to success and closes (or surfaces an error and stays open).
  - **Outcome:** Recording is uploaded, the row updates to `uploaded == true`, the consent decision was made in a native surface with the artifact visible.
  - **Covered by:** R1, R3, R4, R6, R8, R9, R11, R12.

- F2. **Operator opens the review window, decides not to upload**
  - **Trigger:** Same as F1, but during review the operator spots something they don't want shared.
  - **Steps:** Operator clicks **Upload** on the row → review window opens → operator scrubs and finds a problem → clicks **Cancel** (or closes the window).
  - **Outcome:** No upload occurs. The recording is unchanged on disk; the row still shows the Upload affordance, available for a later decision.
  - **Covered by:** R7, R8, R10.

---

## Requirements

**Trigger and entry point**
- R1. Each row in the SwiftUI recordings list shows an Upload affordance for eligible recordings.
- R2. A recording is eligible when `uploaded == false` and `isStub == false`. Already-uploaded recordings and stub rows (local files deleted post-upload) show no Upload affordance.
- R3. Clicking Upload opens a dedicated review window scoped to that single recording. Multiple review windows may be open concurrently if the operator opens several.

**Review window structure**
- R4. The review window contains two first-class panes: a video player and a scrub-synced action timeline. Both are native SwiftUI components — no WKWebView embedding of `viewer.html`.
- R5. The video player plays the recording's `video.mp4`. For chunked recordings (multiple `chunk_*.mp4` files, no merged `video.mp4`), the existing ffmpeg concat path used by `viewer.py` (`_ensure_single_video`) prepares a single playable file before the player renders.
- R6. The action timeline renders events sourced from `recording.db`. Scrubbing the timeline seeks the video; playing the video advances the timeline cursor.
- R7. The window is independently dismissible — closing the window without clicking Upload is treated as Cancel.

**Consent flow**
- R8. The window's terminal actions are exactly two: **Upload** (primary) and **Cancel**. There is no in-screen per-file, per-category, or time-range exclusion.
- R9. Upload triggers the existing upload pipeline against the full recording directory.
- R10. Cancel leaves the recording exactly as it is on disk; no upload occurs and no local files are touched.
- R11. When Upload is clicked, upload progress is surfaced inside the review window via the existing stderr-event channel. The window remains open until upload completes (resolves to success and auto-closes after a short confirmation) or fails (stays open with an error state and a retry affordance).

**Integration**
- R12. Upload executes by shelling out to `screencap upload <name>`, reusing the existing CLI path. The macOS shell does not reimplement signed-URL upload in Swift.
- R13. The existing HTML viewer (`screencap view`) is unchanged. CLI users keep using it; native users use the new review window.

---

## Acceptance Examples

- AE1. **Covers R2.** Given a recording with `uploaded == true` in `screencap list --json`, when the recordings list renders that row, the Upload affordance is absent.
- AE2. **Covers R2.** Given a recording with `isStub == true` (local media deleted after a prior upload), when the recordings list renders that row, the Upload affordance is absent.
- AE3. **Covers R5.** Given a chunked recording with `chunk_*.mp4` files and no `video.mp4`, when the operator clicks Upload, the review window shows a brief preparation state while ffmpeg concat runs, then the video player renders.
- AE4. **Covers R7, R10.** Given an open review window with no upload in progress, when the operator clicks the window's red close button, no upload begins and the recording's on-disk state is unchanged.
- AE5. **Covers R11.** Given an upload that fails partway through, when the failure is reported on the stderr-event channel, the review window stays open, surfaces the error, and offers retry — it does not silently close.

---

## Success Criteria

- A friend-trial operator can complete record → review → upload from the macOS app without opening a terminal.
- After using the review window, operators self-report (in friend-trial feedback) that they felt the consent decision was informed — specifically, that they could verify what was in the recording before clicking Upload.
- The strategic opt-in metric ("% of users opting traces into the training corpus") moves from `n/a` to a real number, because there is now a GUI path to opt in.
- `ce-plan` can sequence the implementation without inventing UX behavior, trigger semantics, or scope boundaries — it only resolves the technical questions deferred below.

---

## Scope Boundaries

- Auto-after-stop trigger (the review window opens automatically when a recording stops).
- Per-file, per-category, or time-range redaction inside the review window.
- Re-upload of already-uploaded recordings (no `--force` affordance is surfaced).
- Changes to the CLI's up-front `local`/`cloud`/`both` choice or `--no-live-upload` defaults.
- Replacing the HTML viewer (`screencap view`) — it stays as-is for CLI users.
- Batch / queue UX for processing multiple recordings together (held as a future direction; see Key Decisions).
- A captured-apps summary or per-file checklist as additional panes in the review surface.
- Audio-specific review affordances (audio is reviewable only through video playback).

---

## Key Decisions

- **Trigger is upload-intent (per-row button), not recording-stop.** Rationale: keeps live-uploaded recordings out of the consent path, avoids a forced popup that would annoy operators during long recording sessions, and aligns with the user's stated mental model ("the user can choose to upload, and then review").
- **Dedicated window over modal sheet.** Rationale: paired video + timeline benefits from real estate; operators may want the window side-by-side with another app to verify it wasn't captured; aligns with the HTML viewer's "separate space" mental model and gives the surface room to grow (event detail panes, eventual redaction UI) without re-architecting.
- **Binary decision (Upload / Cancel), no in-screen redaction.** Rationale: smallest scope that ships the consent gate; `upload.py` has no file-exclusion API today and adding one is meaningful work. Redaction is a deliberate follow-up triggered by friend-trial evidence of demand.
- **Shell out to `screencap upload`, do not reimplement upload in Swift.** Rationale: the codebase already uses this pattern via `CLIClient` and `CLIRecorderService`; the Python codepath is the source of truth for signed-URL handling, WAL checkpointing, and chunk semantics.
- **Native rendering of the action timeline, not a WKWebView of `viewer.html`.** Rationale: matches the rest of the SwiftUI shell, avoids browser-embed performance and styling issues, and keeps the consent moment feeling native.
- **Upload queue (the higher-upside Approach C from brainstorm) is deferred.** Rationale: requires evidence of end-of-day batch-triage behavior that doesn't yet exist; revisit if friend-trial operators accumulate unreviewed recordings or report that per-row review feels heavy.

---

## Dependencies / Assumptions

- The Python upload pipeline (`screencap upload`, `src/screencap/upload.py`) emits structured stderr events the Swift shell can consume, in the same pattern `RecorderController` already uses for live recording state. Verified that the pipeline exists; the stderr-event contract for upload progress needs verification during planning.
- `recording.db` event data can be reached from Swift to render the timeline. Currently the shell consumes `screencap list --json` (summary rows only), not event-level data — exposing an event-level read path may be required.
- The existing `_ensure_single_video` ffmpeg concat in `src/screencap/viewer.py` is reusable from the SwiftUI shell's upload path (either by invoking it via the CLI or by replicating the ffmpeg call from Swift). If it isn't, a chunk-aware AVKit player becomes the fallback.
- AVKit can play the produced `video.mp4` without further conversion. The codec produced by the recorder is assumed to be H.264 or HEVC in an MP4 container, which AVKit handles natively.

---

## Outstanding Questions

### Resolve Before Planning

_(none — all product decisions are settled)_

### Deferred to Planning

- [Affects R11][Technical] What happens to an in-progress upload if the operator closes the review window? Options: keep upload running in the background and surface a small status indicator elsewhere; show a confirm prompt; or cancel the upload. Pick during planning based on what the shell-out path actually supports for cancellation.
- [Affects R6][Needs research] What's the cleanest path to expose `recording.db` event data to the Swift shell? Current options: extend `screencap list` / add a new `screencap events --json <name>` subcommand; read the SQLite file directly from Swift; or have the daemon serve events over the existing API socket. Likely answered during planning by inspecting `daemon/` and `_stderr_events.py`.
- [Affects R5][Technical] Should ffmpeg concat happen eagerly (when recording stops) or lazily (when the operator clicks Upload)? Lazy is simpler; eager makes the review window open instantly. Decide during planning.
- [Affects R11][Technical] What's the right success state when upload finishes — auto-close the window, show a confirmation toast and stay open, or surface a "View on web" link? Implementation detail, resolvable in planning.
