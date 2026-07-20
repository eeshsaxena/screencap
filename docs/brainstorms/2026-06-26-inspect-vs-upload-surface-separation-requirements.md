---
date: 2026-06-26
topic: inspect-vs-upload-surface-separation
---

# Read-Only Inspect Surface vs. Upload/Consent Surface

## Summary

A new read-only native **inspect** surface for just looking at a recorded moment — the single destination for both Search results and Recordings-list clicks — showing the real local content (video, scrub timeline, moment-anchored events) with **none** of the upload ceremony. The existing upload/consent window is untouched and is reachable from inspect only through a subtle, low-emphasis "Share / Upload…" hand-off.

---

## Problem Frame

The persona is a non-technical operator (CSM, ops analyst, sales engineer) who wants to *look at* their own recorded history — recall a moment, recognize what was on screen, scrub around it. That is a fundamentally different job from *deciding to share a recording to the cloud*, which is a privacy-weighted consent decision.

Today the macOS app has only one native moment-viewing surface — `ReviewWindow` — and it is purpose-built for the second job. It prepares the scrubbed upload payload, shows the masked-screenshot "truth view" beside the local video (the before/after), surfaces redaction evidence, coverage disclosures, and fail-closed callouts, and presents binary Upload/Cancel. Both "looking" entry points are wired into the wrong place:

- A **Search** result tap opens `ReviewWindow` and seeks it to the hit moment ([macos/Screencap/Views/Search/SearchView.swift:250](macos/Screencap/Views/Search/SearchView.swift)). An operator who just wanted to find a point in time lands in the full consent surface, Upload button and masking and all.
- A **Recordings-list** row click shells out to `screencap view` and opens the **browser** HTML viewer — a separate, non-native experience already marked in code as *"replaced by the native viewer in v1.1"* ([macos/Screencap/Views/RecordingsListView.swift:213](macos/Screencap/Views/RecordingsListView.swift)).

So "just looking" is split across the wrong consent surface and an out-of-app browser, while the one deliberate place to upload (the Recordings-list Upload button → `ReviewWindow`) is correct but overloaded to also serve looking. The cost: the operator is pushed toward an upload decision when they only wanted to see something, and the looking experience is inconsistent and cluttered with consent machinery that does not apply.

```
                        TODAY                                  PROPOSED
  Search result   ─▶ ReviewWindow (upload/consent)   Search result   ─┐
  Recordings row  ─▶ browser HTML viewer             Recordings row  ─┴─▶ Inspect window (read-only)
  Upload button   ─▶ ReviewWindow (upload/consent)                          │ subtle "Share/Upload…"
                                                                            ▼
                                                     Upload button ──▶ ReviewWindow (unchanged)
```

---

## Actors

- A1. **Operator** — non-technical internal-tool user looking at their own recorded history, reached from Search or the Recordings list. Every decision here optimizes for this actor.
- A2. **Upload/consent surface** — the existing `ReviewWindow` (review-the-payload → Upload/Cancel). Named for scope clarity: it is untouched, and the inspect surface hands off *to* it; it is the only audited path by which anything leaves the device.

---

## Key Flows

- F1. **Look at a moment from Search**
  - **Trigger:** A1 taps a Search result.
  - **Actors:** A1
  - **Steps:** A read-only inspect window opens at the hit moment → shows the real local content with scrub timeline and moment-anchored events → A1 scrubs, recognizes the moment, closes it. No Upload button, no masked/before-after view, no redaction evidence anywhere on the surface.
  - **Outcome:** A1 found and saw the moment in seconds with zero consent ceremony; nothing left the device.
  - **Covered by:** R1, R2, R4, R5, R6

- F2. **Look at a recording from the Recordings list**
  - **Trigger:** A1 clicks a recording row.
  - **Actors:** A1
  - **Steps:** The same native inspect window opens at the recording's start (replacing the browser HTML link-out) → A1 navigates the recording read-only.
  - **Outcome:** One consistent native looking experience, no browser hop.
  - **Covered by:** R1, R2, R4, R6

- F3. **Intent flips to share**
  - **Trigger:** While looking, A1 decides they do want to upload this recording.
  - **Actors:** A1, A2
  - **Steps:** A1 uses the subtle "Share / Upload…" hand-off in the inspect window → the existing consent window opens for that recording → the unchanged review-then-upload flow takes over.
  - **Outcome:** A1 is never stranded; upload stays a deliberate, consent-gated act on its own surface.
  - **Covered by:** R3, R9, R10

- F4. **Open a recording whose local copy is gone**
  - **Trigger:** A1 opens a stub recording (already uploaded; local media deleted) in inspect.
  - **Actors:** A1
  - **Steps:** Inspect detects the stub state → shows a friendly explanation (local copy deleted; how to retrieve) instead of an empty or broken view.
  - **Outcome:** A1 understands why there is nothing to play and what to do, rather than seeing a broken surface.
  - **Covered by:** R11

---

## Requirements

**Surface separation & routing**
- R1. A new read-only native inspect surface for viewing a recorded moment, distinct from the upload/consent window. It carries no Upload button, no masked / before-after view, and no redaction or coverage evidence.
- R2. Both Search results and Recordings-list row clicks open this one inspect surface. The **in-app** browser HTML link-out from the Recordings list is retired in favor of it.
- R3. The upload/consent window is unchanged. It remains the only place an upload is reviewed and committed (binary Upload/Cancel) and the only path by which data leaves the device.

**Inspect content & behavior**
- R4. Inspect shows the real, local recorded content as primary — the local video and real captured frames — with scrub/timeline navigation and moment-anchored event content (app/window context, typed text, etc.).
- R5. Inspect is strictly read-only: no editing, redaction, trimming, deleting, or annotating of the recording.
- R6. Opened from a Search result, inspect opens at the result's moment (the existing seek behavior). Opened from the Recordings list, it opens at the recording's start.
- R7. Multiple inspect windows can be open concurrently, so the operator can compare two moments side-by-side.
- R8. Inspect deliberately shows **unmasked** local content (structural): masking is an upload concept, and inspect never egresses, so it shows the same on-disk local content the consent window's local-video pane already renders — introducing no new exposure surface.

**Upload hand-off**
- R9. From inspect, a subtle, low-emphasis "Share / Upload…" affordance hands off to the consent window for that recording. It is never the visual focus and never appears as a primary action.
- R10. The hand-off opens the existing consent window; inspect itself performs no scrubbing, upload, or consent.

**Honest states**
- R11. Opening a stub recording (uploaded; local media deleted) in inspect shows the same friendly explanation the Recordings list shows today (local copy deleted; how to retrieve it), not an empty or broken view.

---

## Acceptance Examples

- AE1. **Covers R1, R2, R6.** Given recordings exist, when the operator taps a Search result, then a read-only inspect window opens at that moment with no Upload button, no masked/before-after view, and no redaction evidence on the surface.
- AE2. **Covers R2.** Given the Recordings list, when the operator clicks a row, then the native inspect surface opens (not the browser HTML viewer).
- AE3. **Covers R9, R10.** Given an open inspect window, when the operator uses the "Share / Upload…" hand-off, then the existing consent window opens for that recording and inspect itself performs no upload.
- AE4. **Covers R11.** Given a stub recording (uploaded, local media deleted), when the operator opens it in inspect, then a friendly "local copy deleted — run download" explanation is shown instead of an empty view.
- AE5. **Covers R7.** Given two Search results, when the operator opens both, then two inspect windows coexist for side-by-side comparison.

---

## Success Criteria

- **Human outcome:** from a Search result or a Recordings row, the operator lands in a read-only viewer at the right moment with zero upload/consent affordances in their face — and can still reach upload deliberately when intent flips.
- **Trust outcome:** the looking flow never presents an upload action as the focus and never moves data off-device; the consent window stays the single audited path for any egress.
- **Downstream handoff:** ce-plan has the surface (a separate read-only window), its reach (Search + Recordings, retiring the in-app browser link-out), its content (the reused local playback core, no consent machinery), the hand-off model, and the read-only invariant — without needing to invent product behavior.

---

## Scope Boundaries

- The upload/consent flow itself — review payload, Upload/Cancel, redaction evidence, masking, coverage disclosures — untouched.
- Capture, the privacy/redaction pipeline, and what gets uploaded — unchanged.
- Editing, redacting, trimming, deleting, or annotating from inspect — strictly read-only.
- A new playback engine — inspect reuses the existing playback core, not a rewrite.
- The inline-in-main-window mechanism (Approach 3) and the mode-switched-shared-window mechanism (Approach 1) — considered and rejected in favor of a separate read-only window.
- Search ranking, parsing, and coverage behavior — the existing ask-your-history feature ([docs/brainstorms/2026-06-24-ask-your-history-search-requirements.md](docs/brainstorms/2026-06-24-ask-your-history-search-requirements.md)), unchanged here.
- The CLI `screencap view` HTML viewer — stays for CLI / power users; only the **in-app** row click is re-pointed to native inspect.

---

## Key Decisions

- **Two surfaces, not three.** Review-for-upload and upload are one consent job and stay fused; the genuinely separate job is read-only looking. Splitting upload from review would require a granular-consent model that does not exist and is not wanted (binary consent is a settled decision — see [docs/brainstorms/2026-06-03-native-redaction-review-before-upload-requirements.md](docs/brainstorms/2026-06-03-native-redaction-review-before-upload-requirements.md)).
- **One inspect surface for both entry points.** The "looking" job is identical whether reached from Search or the Recordings list; unifying avoids two divergent looking experiences and matches the already-documented "native viewer replaces the browser in v1.1" intent in the Recordings list.
- **Separate read-only window, not a mode flag (Approach 2 over Approach 1).** A mode flag keeps the exact conflation being removed alive inside an already-complex consent window (which juggles sign-in, upload counting, refusal/busy states) and risks upload affordances leaking back into looking. A separate window gives each surface one job and still reuses the playback core.
- **Real, unmasked local content in inspect.** "Just looking" means recognizing the moment as experienced; masking is an upload concept. Safe because inspect never egresses — the same local content the consent window's local-video pane already shows.
- **Subtle upload hand-off, not no-bridge and not a prominent button.** Avoids stranding the operator when intent flips, without re-importing upload-as-focus into the looking flow.
- **Upload stays explicit and consent-gated.** Demoting upload to an opt-in action (Recordings-list button + inspect hand-off) matches the operator instinct that looking ≠ uploading.

---

## Dependencies / Assumptions

- **The playback core already exists in the consent window** and is the intended reuse base: local video playback ([macos/Screencap/Views/Review/VideoPlayerPane.swift](macos/Screencap/Views/Review/VideoPlayerPane.swift)), the scrub timeline ([macos/Screencap/Views/Review/TimelinePane.swift](macos/Screencap/Views/Review/TimelinePane.swift)), moment-anchored event content ([macos/Screencap/Views/Review/EventContentPane.swift](macos/Screencap/Views/Review/EventContentPane.swift)), and the search-result seek ([macos/Screencap/State/ReviewWindowOpener.swift](macos/Screencap/State/ReviewWindowOpener.swift)). Extracting a shared read-only core that both windows draw from is the main structural change.
- **Search currently opens the consent window** via `openWindow(id: ReviewWindowID)` + `pendingSeekMs` ([macos/Screencap/Views/Search/SearchView.swift:246](macos/Screencap/Views/Search/SearchView.swift)). Re-pointing it at the inspect window is the core search-side wiring change.
- **The Recordings list currently opens the browser viewer** via `screencap view` and exposes a separate per-row Upload button that opens the consent window ([macos/Screencap/Views/RecordingsListView.swift:167](macos/Screencap/Views/RecordingsListView.swift)). Re-pointing the row click to native inspect while keeping the Upload button is the recordings-side change.
- **Inspect surfaces the local recording as it exists on disk** — the same content the consent window's local-video pane already renders — so it adds no new egress or exposure surface. The masked screenshots live only in the separate `-scrubbed` upload copy and are not part of inspect.
- **Stub-recording handling** (uploaded; local media deleted) already has a friendly message + `screencap download` guidance in the Recordings list ([macos/Screencap/Views/RecordingsListView.swift:213](macos/Screencap/Views/RecordingsListView.swift)); inspect should reuse that semantics.

---

## Outstanding Questions

### Resolve Before Planning

_(none — product decisions are settled)_

### Deferred to Planning

- [Affects R1, R4][Technical] How to factor the shared playback core so the inspect window and the consent window both draw from it without entangling the consent lifecycle — extract a shared read-only player view vs. duplicate the panes.
- [Affects R4][Technical] What event/content inspect shows from the **local** recording vs. the consent window's scrubbed set — inspect reads the raw local events; confirm which content classes (e.g., network destinations, currently excluded from the upload export) are appropriate to show in a local-only looking surface.
- [Affects R9][Design] Exact placement and form of the subtle "Share / Upload…" hand-off (toolbar overflow, menu, etc.) — kept low-emphasis per R9.
- [Affects R2][Technical][Needs research] Whether retiring the in-app browser link-out needs a fallback for recordings the native inspect surface can't render (e.g., very old recordings), or whether the CLI `screencap view` remains the only escape hatch.
