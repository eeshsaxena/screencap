---
date: 2026-06-03
topic: native-redaction-review-before-upload
---

# Native Redaction Review Before Upload

## Summary

Enrich the existing native pre-upload review window so the operator reviews *exactly what the upload will ship* — the masked screenshots, scrubbed events, and scrubbed transcript that make up the post-hoc upload payload — surfaced moment-by-moment, with visible evidence of what redaction removed and an honest statement of what it does not touch, so a non-technical operator can confidently confirm nothing sensitive leaves their machine. The local video plays as a navigation aid alongside a "what actually uploads" view. This moves scrubbing ahead of the consent moment.

---

## Problem Frame

The strategy names "win on privacy" as half the approach and "% of users opting traces into the training corpus" as the lagging signal the data flywheel is forming. The consent moment — where an operator agrees to send a recording to the cloud — is where that bet is won or lost, and the persona making the decision is a non-technical operator (CSM, ops analyst, sales engineer), not a CLI user.

Two facts about the current pipeline make that consent moment unsafe to trust:

1. **The operator reviews the wrong artifact, and scrub happens after consent.** ScreenCap has two upload paths. The *live chunk path* (cloud-intent recordings) uploads video/audio redacted at capture time. The *post-hoc `screencap upload` path* — what the review window's Upload button triggers — runs `scrub_recording()` only *after* Upload is clicked ([src/screencap/cli/__init__.py:2913](src/screencap/cli/__init__.py)), producing a separate scrubbed copy that **excludes all video and audio** (no post-hoc media-redaction engine exists) and ships masked `screenshots/*.jpg` + scrubbed `recording.db`/`events.jsonl` + scrubbed transcript. Yet today's native window renders the **raw original** dir and plays the local `video.mp4` (a symlink to the chunk, never uploaded). So the operator reviews scary raw data and a video that will *not* leave, while the bytes that *do* leave (masked screenshots + scrubbed structured data) are produced only after consent — and go unreviewed.

2. **The native window has no content to review anyway.** The native timeline ([macos/ScreenCap/Views/Review/TimelinePane.swift](macos/ScreenCap/Views/Review/TimelinePane.swift)) draws category-colored tick marks from a `TimelineEvent` that carries only time, type, and a coarse category. It shows *that* a window event fired at 0:14, never *which* window or *what* title. The HTML viewer renders the actual captured content, but it is a separate browser surface with no upload action.

The cost compounds at launch: an operator who can't verify what leaves their machine either uploads blind or never opts in, and the privacy claim that anchors the strategy goes unproven exactly where it matters most.

---

## Actors

- A1. **Operator** (non-technical, SwiftUI app): finishes a recording, wants to share it to the cloud, and must be able to confirm nothing sensitive is exposed before consenting. The primary actor every decision here optimizes for.
- A2. **CLI user**: continues to use `screencap upload` and `screencap view` directly. Their flow is explicitly unchanged; named so scope boundaries stay clear.

---

## Key Flows

- F1. **Operator reviews the real payload and uploads**
  - **Trigger:** A1 opens review for a recording they want to share.
  - **Actors:** A1
  - **Steps:** Operator opens the review window → the scrubbed copy is prepared (progress shown) → the window renders the local video for navigation alongside the masked-screenshot "what actually uploads" view, moment-anchored event content, and a redaction summary/coverage note → operator scrubs to specific moments, sees what was captured and what was redacted → clicks **Upload** → the already-prepared scrubbed copy uploads with progress shown.
  - **Outcome:** The exact payload the operator reviewed is uploaded; the consent decision was made against real, faithful data in a native surface.
  - **Covered by:** R1, R2, R3, R4, R5, R6, R7, R8, R9, R10, R15.

- F2. **Operator inspects, finds a problem, declines**
  - **Trigger:** Same as F1, but during review the operator spots sensitive content the scrubber did not (and cannot) remove — e.g., a confidential figure visible on-screen in a masked screenshot, inside an allowed app the window-masker didn't cover.
  - **Actors:** A1
  - **Steps:** Operator opens review → the surface flags moments worth a look → operator jumps to a flagged moment, sees the problem → clicks **Cancel** or closes the window.
  - **Outcome:** Nothing uploads; the original recording is untouched and remains available for a later decision.
  - **Covered by:** R9, R10, R11, R13.

---

## Requirements

**Review payload faithfulness**
- R1. The review surface presents the exact post-hoc upload payload — masked `screenshots/*.jpg`, scrubbed `events.jsonl`/`recording.db`, and scrubbed transcript — not the raw original recording. What the operator reviews is what will leave the device.
- R2. Scrubbing runs before the consent decision is presented, reversing today's order where scrub runs only after Upload is clicked.
- R3. When the operator consents, the upload ships the same already-prepared scrubbed copy that was reviewed; it does not re-scrub or produce a different artifact (reviewed == uploaded).
- R4. While the scrubbed copy is being prepared, the window shows a progress/preparing state and stays responsive rather than appearing hung.
- R15. The visual surface shows two clearly distinguished things: the local `video.mp4` as a smooth navigation aid (labeled local-only, not uploaded) and the masked screenshots that actually upload at the current moment. The operator's confidence rests on the screenshot-truth view; the video is for orientation.

**Moment-anchored content review**
- R5. The review window surfaces the captured event/text content from the scrubbed copy — the same content classes the HTML viewer renders (window/app context, typed text, URLs, network destinations) plus the scrubbed transcript — not only the category tick marks shown today.
- R6. Event content is anchored to the timeline: scrubbing to a moment surfaces the events captured at that moment, and selecting an event seeks the video/screenshot view to it. The operator inspects specific moments rather than reading a flat list.
- R7. The content view reflects post-scrub state: data removed by redaction does not appear as live content.

**Redaction transparency**
- R8. The window makes redaction visible at two levels: a per-recording summary (categories and counts of what was removed, plus how many segments were hidden) and per-moment markers shown inline as the operator scrubs to where the scrubber acted. Evidence is framed as protection ("removed" / "protected"), not as an alarm, so it reassures rather than deters consent. The operator sees the scrubber acted rather than inferring it from absence.
- R9. The window honestly discloses coverage facts in non-technical language: video and audio stay **local and are never uploaded**; the only audio content that leaves is the scrubbed transcript text; the uploaded visual is the masked screenshots, where window-level masking covers blocked apps but **on-screen PII inside an allowed app is not automatically redacted** and is the operator's to catch.
- R13. The surface flags moments worth manual review on the timeline — the leak vectors redaction cannot reach — such as secure-field focus, masked/hidden-app intervals, and high-redaction-density spots, directing the operator's attention there (especially to screenshots in allowed apps) rather than relying on them to scrub the entire recording. Flags are advisory: they guide the eye but do not gate Upload.
- R14. When redaction could not analyze some captured content and removed it fail-closed, the surface shows that distinctly (e.g., "some content couldn't be analyzed and was removed to be safe"), separate from ordinary redaction evidence — so the operator sees the safety net engaged on the hard cases rather than mistaking incomplete analysis for clean content.

**Consent model and integration**
- R10. Terminal actions remain exactly two: **Upload** (consent) and **Cancel**. Enriched review does not introduce per-file, per-category, or per-interval granular consent.
- R11. Cancel — or closing the window — uploads nothing and leaves the original recording untouched on disk.
- R12. The enriched review lives only in the native window; the HTML viewer (`screencap view`) and the up-front local/cloud/both choice are unchanged.

---

## Acceptance Examples

- AE1. **Covers R1, R3.** Given a recording whose raw events contain a value the scrubber removes, when the operator opens review, the content shown is the scrubbed version (the value is gone) and the visual is the masked screenshots; and when they click Upload, the bytes shipped match what was reviewed.
- AE2. **Covers R2, R4.** Given a long recording, when the operator opens review, a preparing state is shown while the scrubbed copy is produced, then the visual and content panes render; the window does not appear frozen.
- AE3. **Covers R7, R8.** Given a field the scrubber redacted at a given moment, when the operator scrubs to that moment, the live content does not show the value and a redaction indicator/summary reflects that something was removed there.
- AE4. **Covers R9.** Given any recording, when the review window is open, a coverage statement is visible that states video and audio are not uploaded (local-only), the transcript text is uploaded scrubbed, and on-screen PII inside allowed apps in the screenshots is the operator's to verify.
- AE5. **Covers R10, R11.** Given an open review window with no upload in progress, when the operator clicks Cancel or closes the window, no upload occurs and the original recording's on-disk state is unchanged.
- AE6. **Covers R6, R15.** Given a recording with events, when the operator selects an event in the content view, the video and the masked-screenshot view seek to that event's moment; and when they scrub, the surfaced content updates to that moment.
- AE7. **Covers R13.** Given a recording with a secure-field focus interval, when the review window opens, that moment is flagged on the timeline as worth manual review, and the flag does not block the Upload action.
- AE8. **Covers R14.** Given a recording where the scrubber failed closed on some content, when the operator opens review, the surface indicates distinctly that some content couldn't be analyzed and was removed — separate from the ordinary redaction summary.

---

## Success Criteria

- A non-technical operator can open the native window, inspect specific moments, see what redaction removed and what it does not cover, and consent — without opening a browser or terminal — and feels confident about what leaves their machine.
- The uploaded payload provably equals the reviewed payload (no scrub-after-consent drift).
- Friend-trial probe: operators report that seeing the captured events and redaction changed or genuinely confirmed their upload decision — validating that this forward bet addresses a real consent need.
- `ce-plan` can sequence implementation without inventing UX behavior, the review-payload model, or scope boundaries — it only resolves the technical questions deferred below.

---

## Scope Boundaries

- In-window redaction *editing* — excluding specific files, regions, or time ranges before upload (still deferred; no upload-side exclusion API exists today).
- Per-frame pixel redaction of on-screen content beyond the existing window-level screenshot masking (a separate future bet — ideation idea #6, "hot-path AX-keyed redact-before-persist").
- Reviewing or changing the *live chunk upload path* (cloud-intent recordings, capture-time-redacted media); this feature is scoped to the post-hoc `screencap upload` path only.
- Audio-specific review affordances (audio scrubber / waveform); audio is local-only and not uploaded, so the only audio content reviewed is the scrubbed transcript text.
- Granular or partial consent (per-file, per-category, per-interval); consent stays binary.
- Changes to the HTML viewer (`screencap view`) — it stays as-is for CLI users.
- Re-implementing the upload / signed-URL path in Swift; upload still shells out to `screencap upload`.
- Changes to the up-front local/cloud/both choice or auto-upload defaults.

---

## Key Decisions

- **Review the scrubbed copy, not the raw original (scrub-before-review).** "Confident nothing sensitive is exposed" is only honest if reviewed == uploaded. Today the window renders the raw original and plays a local video, while the post-hoc `screencap upload` path produces and ships a separate scrubbed copy only *after* consent ([src/screencap/cli/__init__.py:2913](src/screencap/cli/__init__.py)). Reviewing the raw artifact both misleads and proves nothing about redaction, so scrub moves ahead of review.
- **The feature targets the post-hoc upload path, whose payload excludes raw media.** Verified via git history (cef86665, 475c5357, 74cb57d6, 1c9253ce): the manual `screencap upload` path deliberately excludes video/audio from the cloud payload ("no redaction engine for media") and ships masked screenshots + scrubbed structured data + transcript. The separate live chunk path uploads capture-time-redacted media and is out of scope here. The review surface is therefore built around the screenshots + scrubbed structured data, not the video.
- **Visual surface = local video for navigation + masked-screenshot truth view.** The video is a familiar way to move through the recording but is local-only; the masked screenshots are what actually upload, so they carry the "this is what leaves" assurance. Showing both gives the clearest mental model (your recording vs. what leaves) at the cost of rendering two visual sources.
- **Native integration over launching the HTML viewer.** The minimal alternative — a "review in detail" button that opens `screencap view` — was rejected: launch-readiness and the "everything moves to native UI" direction require the consent decision to be one native surface, not split across a browser.
- **Binary consent retained.** Smallest scope that ships the trustworthy gate; granular consent would need an upload-side exclusion API that does not exist. The bet is enriching *what is reviewed*, not adding partial-consent controls.
- **Make redaction visible + disclose blind spots honestly, rather than build a "miss detector."** "What it didn't catch" cannot be computed — if the scrubber knew a value was sensitive it would have removed it. The only honest form is showing what redaction *did* (R8) and truthfully naming what it does not cover (R9), leaving the operator's eyes as the safeguard for on-screen PII in screenshots. This disclosure is cheap and is the strongest launch-time privacy story.
- **Redaction evidence is shown at two levels and framed as protection, not alarm.** A per-recording summary serves the non-technical glance; per-moment markers serve forensic inspection (R8). Framing the same fact as "removed/protected" rather than an itemized threat list is a deliberate choice to inform consent without deterring it, since alarming counts would suppress the opt-in metric the feature exists to move.
- **Blind-spot review is guided but never gated.** The surface actively flags risky moments (R13) because a passive "review it yourself" disclosure is honest-but-useless for a non-technical operator — but flags stay advisory. Forcing a walkthrough before Upload unlocks was rejected as friction that would suppress consent; the operator stays in control of how much they inspect.

---

## Dependencies / Assumptions

- The post-hoc upload payload is produced by `scrub_recording()` → a `<name>-scrubbed` sibling dir that **excludes `.mp4`/`.flac`** (`_SKIP_EXTENSIONS`, [src/screencap/scrubber.py](src/screencap/scrubber.py)) and contains masked `screenshots/*.jpg`, scrubbed `recording.db`/`events.jsonl`, transcript, and metadata. `upload_recording` ships that dir ([src/screencap/cli/__init__.py:2913](src/screencap/cli/__init__.py)).
- `scrub_recording()` returns a `ScrubResult` (`entity_counts`, `audit_entries`, `rule_based_redactions`) and writes `privacy_audit.json` into the scrubbed dir; audit entries carry per-timestamp + category and are export-safe (never the redacted value), which is the natural source for R8 markers. Blocked intervals (R13) and the fail-closed signal (R14) are **not** first-class on `ScrubResult` today and likely need to be threaded through. Redaction evidence reaching the native shell is a path that does not exist today (the shell consumes `screencap list --json` summaries + `review-data --json` paths only).
- `screencap review-data` currently reads the **raw original** dir ([src/screencap/review.py](src/screencap/review.py)) and returns the local `video.mp4` + `events.jsonl`. Re-pointing it (or a sibling command) at the scrubbed artifacts is the core wiring change for R1/R5.
- `scrub_recording()` is **non-idempotent** (it rmtrees and rebuilds the `-scrubbed` dir) and `screencap upload` **always re-scrubs** with no reuse guard. R3 (reviewed == uploaded) needs an upload-side reuse/skip path so upload ships the already-prepared scrubbed copy rather than rebuilding it.
- Scrubbing is a heavy detection (NER) pass, so running it before review carries a latency cost (R4). The local video review builds on the PyAV review-data pipeline ([docs/brainstorms/2026-05-29-pyav-review-pipeline-requirements.md](docs/brainstorms/2026-05-29-pyav-review-pipeline-requirements.md)) and inherits its in-process / no-ffmpeg constraints.
- The cloud upload pipeline is mid-migration (zkairdrop → proteus-photos); this feature touches the review/consent surface and the scrub-before-upload ordering, not the cloud destination.

---

## Outstanding Questions

### Resolve Before Planning

_(none — product decisions are settled)_

### Deferred to Planning

- [Affects R5, R8][Needs research] Cleanest path to expose the scrubbed copy's event content and redaction audit to the native shell: extend the `screencap review-data` envelope, add a sibling subcommand, read the scrubbed SQLite/JSONL/`privacy_audit.json` directly from Swift, or serve it over the daemon socket.
- [Affects R4][Technical] Scrub timing — eager at record-stop vs lazy at review-open. Lazy is simpler; eager makes the window open instantly. Trade latency against instant-open.
- [Affects R3, R11][Technical] Lifecycle of the scrubbed copy when the operator reviews but cancels (keep, cache, or discard), and the upload-side reuse guard by which upload ships the reviewed copy to guarantee reviewed == uploaded.
- [Affects R5][Technical] Align the reviewed event set with the uploaded set — e.g., `events.jsonl` export defaults to `include_network=False`; if network destinations are excluded from the upload, the review must not show them (review == upload), and vice versa.
- [Affects R14][Technical] Behavior when scrubbing fails for the *whole* recording (not just some fields): the upload CLI aborts today ("cannot upload without scrubbing"), and the window has an existing preparation-failure state. Confirm review treats a total scrub failure as a preparation failure (no review, no upload), distinct from the per-field fail-closed case R14 surfaces.
