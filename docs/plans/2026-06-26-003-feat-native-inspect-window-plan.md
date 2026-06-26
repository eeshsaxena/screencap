---
title: "feat: Native read-only inspect window (looking ≠ uploading)"
type: feat
status: completed
date: 2026-06-26
origin: docs/brainstorms/2026-06-26-inspect-vs-upload-surface-separation-requirements.md
---

# feat: Native read-only inspect window (looking ≠ uploading)

## Summary

Carve a separate read-only **inspect** window out of the existing upload/consent window: a new no-scrub CLI verb feeds a stripped-down SwiftUI window that reuses the playback core (video, scrub timeline, moment-anchored events) but drops all consent machinery. Both Search results and Recordings-list clicks open it; upload stays explicit via a low-emphasis hand-off into the untouched `ReviewWindow`.

---

## Problem Frame

"Just looking" at a recorded moment and "deciding to upload" are different jobs, but today both are served by `ReviewWindow` — an upload/consent surface (masked truth-view, redaction evidence, Upload/Cancel). Search results open it and seek; the Recordings list instead opens an out-of-app browser viewer. The operator who only wanted to find a point in time is pushed at an upload decision, and the looking experience is inconsistent and cluttered. Full motivation and the routing before/after are in the origin doc (see Sources & References).

The technical crux this plan must solve: the native review path **always scrubs**. `screencap review-data` runs the NER scrub before returning (600s timeout) and points the panes at the `-scrubbed` copy. There is no existing way to get raw local playback data without that minutes-long pass — so a read-only "looking" surface cannot reuse `review-data` as-is.

---

## Requirements

- R1. A new read-only native inspect surface, distinct from the upload/consent window, with no Upload button, no masked/before-after view, and no redaction or coverage evidence. (origin R1)
- R2. Both Search results and Recordings-list row clicks open this one inspect surface; the in-app browser HTML link-out is retired in favor of it. (origin R2)
- R3. The upload/consent window (`ReviewWindow`) is unchanged and remains the only place an upload is reviewed and committed. (origin R3)
- R4. Inspect shows the real, local recorded content — local video and real captured frames — with scrub/timeline navigation and moment-anchored event content. (origin R4)
- R5. Inspect is strictly read-only: no editing, redaction, trimming, deleting, or annotating. (origin R5)
- R6. Opened from a Search result, inspect opens at the result's moment; opened from the Recordings list, it opens at the recording's start. (origin R6)
- R7. Multiple inspect windows can be open concurrently to compare moments. (origin R7)
- R8. Inspect deliberately shows unmasked local content (structural): masking is an upload concept, inspect never egresses, and it surfaces the same on-disk content the consent window's local-video pane already renders — no new exposure surface. (origin R8)
- R9. From inspect, a subtle, low-emphasis "Share / Upload…" affordance hands off to the consent window for that recording; never the visual focus, never a primary action. (origin R9)
- R10. The hand-off opens the existing consent window; inspect itself performs no scrubbing, upload, or consent. (origin R10)
- R11. Opening a stub recording (uploaded; local media deleted) in inspect shows the same friendly "local copy deleted — run download" explanation the Recordings list shows today, not an empty or broken view. (origin R11)

**Origin actors:** A1 (Operator — non-technical, looking at own history), A2 (Upload/consent surface — the existing `ReviewWindow`, untouched, hand-off target).
**Origin flows:** F1 (look at a moment from Search), F2 (look at a recording from the Recordings list), F3 (intent flips to share — hand-off), F4 (open a recording whose local copy is gone — stub).
**Origin acceptance examples:** AE1 (covers R1, R2, R6), AE2 (covers R2), AE3 (covers R9, R10), AE4 (covers R11), AE5 (covers R7).

---

## Scope Boundaries

- The upload/consent flow itself — review payload, Upload/Cancel, redaction evidence, masking, coverage — untouched. `ReviewWindow`, `ReviewWindowViewModel`, and `screencap review-data` are not modified beyond a low-risk extraction of shared video/timing helpers (U1).
- Capture, the privacy/redaction pipeline, and what gets uploaded — unchanged.
- Editing, redacting, trimming, deleting, or annotating from inspect — strictly read-only.
- A new playback engine — inspect reuses `VideoPlayerPaneModel` / `VideoPlaybackEngine`, not a rewrite.
- The CLI `screencap view` HTML viewer — stays for CLI / power users; only the **in-app** Recordings-list row click is re-pointed.
- Search ranking, parsing, and coverage behavior — the existing ask-your-history feature, unchanged.
- The inline-in-main-window and mode-switched-window mechanisms (origin Approaches 3 and 1) — rejected in favor of a separate read-only window.

### Deferred to Follow-Up Work

- Capturing the search→review seek (SCR-174), the inspect-vs-review read-path split, and the in-app `screencap view` retirement as `docs/solutions/` learnings — all three are currently undocumented (per learnings research). Best done with `/ce-compound` after this lands, not in this PR.

---

## Context & Research

### Relevant Code and Patterns

- **Window scene + opener pattern to mirror exactly:** the per-recording `ReviewWindow` `WindowGroup` keyed `for: String.self` at `macos/ScreenCap/ScreenCapApp.swift:99-113`; the `ReviewWindowID` constant co-located at `macos/ScreenCap/Views/Review/ReviewWindow.swift:6`; the `ReviewWindowOpener` singleton (with `openReview` closure + out-of-band `pendingSeekMs` seek + `open(recordingName:) -> Bool`) at `macos/ScreenCap/State/ReviewWindowOpener.swift`; and the zero-frame `OpenWindowBridge` that registers the closure inside the main `Window` body at `macos/ScreenCap/ScreenCapApp.swift:179-192`.
- **Playback panes reusable as-is (no review/upload coupling):** `VideoPlayerPane` + `VideoPlayerPaneModel` (protocol-seamed via `VideoPlaybackEngine` / `LiveVideoPlaybackEngine(url:)`), `TimelinePane` (its `riskyIntervals`/`redactionMarkers` inputs are defaulted empty — omit them and it draws event ticks + cursor only), `EventContentPane`, `TimelineEvent` / `TimelineEventParser` (pure JSONL decoder), and `SearchSeek.relativeSeconds(...)` (pure). All under `macos/ScreenCap/Views/Review/` + `macos/ScreenCap/Controllers/SearchSeek.swift`.
- **Review-only surfaces to exclude from inspect:** `ScreenshotTruthPane` ("What actually uploads" / green-shield framing — drop entirely; the AVKit video already shows frames), and `RedactionEvidenceView` / `FailClosedCallout` / `CoverageStrip` in `macos/ScreenCap/Views/Review/RedactionEvidenceView.swift`. `TimingUnavailableCallout` (same file) is worth keeping — it advises when the timeline can't be drawn.
- **The scrub-on-load path (the thing inspect must NOT do):** `LiveReviewDataLoader.load` runs `review-data --json` with a 600s timeout (`macos/ScreenCap/Views/Review/ReviewWindowViewModel.swift:186-192`); Python `prepare_review_data` holds `recording_scrub_lock` and runs `_prepare_scrubbed_copy` (export → recovery → `scrub_recording`), then resolves events/screenshots from the `-scrubbed` dir (`src/screencap/review.py:182`, `:272-299`). Video + timing come from the **original** dir (`_ensure_single_video` + `catalog._read_recording_meta`) — those sub-steps are the reusable half.
- **Raw-local readers that don't scrub but don't return the envelope:** `screencap view` → `open_viewer` reads the raw original dir and opens `viewer.html` (`src/screencap/cli/__init__.py:1045`, `src/screencap/viewer.py:230`); `exporter.ensure_canonical_events` produces canonical `events.jsonl` from the original dir cheaply with no scrub (`src/screencap/review.py:84`). Recorder writes raw frames as flat `screenshots/{ts:.6f}.jpg` (`recorder.py:761`).
- **Stub handling to replicate:** `RecordingSummary.isStub` (`macos/ScreenCap/Models/RecordingSummary.swift`), `isUploadEligible = !uploaded && !isStub`, and the friendly pre-check alert in `RecordingsListView.openInBrowser` (`macos/ScreenCap/Views/RecordingsListView.swift:213-225`).
- **Two callsites to re-point:** `SearchView.openReview` (`macos/ScreenCap/Views/Search/SearchView.swift:246-251`) and the row-body `openInBrowser` in `RecordingsListView` (`macos/ScreenCap/Views/RecordingsListView.swift:118-225`). The per-row Upload button (`:167-177`) stays pointed at `ReviewWindowID`.
- **Test seams:** `ReviewWindowOpenerTests` (opener-closure seam, no-dedupe pin, ID-stability pin), `ReviewWindowViewModelTests` (`FakeReviewDataLoader` with `nextEnvelope`/`nextError`/`loadCallCount`; `makeModel` injection), `VideoPlayerPaneTests` (`FakeVideoPlaybackEngine` records `seekRequests`, fires completions), and the pure-logic suites (`ReviewSeekTargetTests` over `SearchSeek`, `TimelineEventParsingTests`). The `isRunningUnderTests` guard gates scene-level `.task` side effects (`ScreenCapApp.swift:36-38`).

### Institutional Learnings

- **SCR-55 — `WindowGroup` vs `Window` (`docs/solutions/ui-bugs/swiftui-windowgroup-vs-window-singleton-scene-2026-05-18.md`).** `WindowGroup` is multi-window by contract; `openWindow(value:)` for a *new* value materializes a fresh window, for the *same* value focuses the existing one. Inspect-per-recording from two entry points is genuinely "one window per recording" → `WindowGroup` keyed `for: String.self` is correct (matches `ReviewWindow`). Keep a WHY comment on the scene so a later contributor doesn't flip it. `MenuBarExtra`/`OpenWindowAction` paths aren't unit-testable here — only the opener-closure seam is.
- **SCR-102 — nullable review-data timing (`docs/solutions/integration-issues/review-data-nullable-timing-swift-consumer-2026-06-01.md`).** `started_at`/`duration_seconds` serialize as JSON `null` for a playable recording with no action events. Gate readiness on `ok` + paths only; default timing to `0`. Copy the corrected guard (`ReviewWindowViewModel.swift:335-341`), add the regression test, and do NOT re-introduce a timing gate when forking the loader.
- **XcodeGen stale project (`docs/solutions/build-errors/xcodegen-stale-project-missing-new-sources.md`).** `macos/ScreenCap.xcodeproj` is git-ignored and globbed from `macos/project.yml` at generation time. Adding new `.swift` files without regenerating yields `cannot find 'InspectWindow' in scope`. Fix: `cd macos && xcodegen generate` (the build script's directory-mtime freshness check usually handles it).
- **Foundation.Process/Pipe pitfalls (`docs/solutions/integration-issues/macos-foundation-process-pipe-pitfalls.md`).** Reusing `CLIClient` as-is inherits the drain/termination fixes for free; only revisit if a new spawn is introduced (none is — inspect reuses `CLIClient.runJSONRaw`).

### External References

None — local patterns are strong (the existing `ReviewWindow` stack is the exact template) and the work touches no high-risk external surface. External research deliberately skipped.

---

## Key Technical Decisions

- **New `screencap inspect-data --json` verb, not a flag on `review-data`.** A `--no-scrub` flag on `review-data` would risk the consent surface accidentally shipping unscrubbed data and entangle two lifecycles. A distinct verb keeps "looking" and "uploading" separated at the CLI boundary too. Factor `prepare_review_data` into a shared core (video remediation + timing read) reused by both, so the video/timing path can't drift.
- **Inspect reads the original dir; events via canonical export, frames flat.** Events come from `exporter.ensure_canonical_events` over the original `events.jsonl` (cheap, no scrub); frames from the original flat `screenshots/*.jpg`. `redaction`/`coverage` are omitted/null; `screenshots` may be empty (the video carries the visual). The envelope decodes through a guard identical to the review one.
- **Separate `InspectWindowViewModel` with a read-only state machine** (`preparing → ready → failed` only) — no `uploading`/`succeeded`/`refused`/`busy` states, no `UploadController`, no auto-close/refresh effects. This is the structural separation Approach 2 buys; a shared viewmodel with a mode flag was rejected (origin Approach 1).
- **`WindowGroup` keyed `for: String.self`, injecting only `index`.** Mirrors `ReviewWindow`'s multi-window contract (R7) but drops the `auth`/`uploads` env objects (upload-only). One window per recording-name; opening the same recording at a second moment reuses the window and re-seeks (parity with SCR-174), comparing two *different* recordings opens two windows.
- **Share/Upload hand-off reuses the existing `ReviewWindowOpener`.** The inspect toolbar's low-emphasis item calls `ReviewWindowOpener.shared.open(recordingName:)` — no new upload plumbing; the consent window is reached exactly as the Recordings-list Upload button reaches it.
- **Stub guard at the click site, not inside the window.** Pre-check `isStub` where the inspect window is opened (both Search and Recordings) and show the existing friendly download message, mirroring `RecordingsListView.openInBrowser` — a stub has no local media, so `inspect-data` would otherwise fail.

---

## Open Questions

### Resolved During Planning

- *How to get raw local playback data without the scrub?* New `inspect-data` verb reading the original dir (canonical events + flat frames + original-dir video/timing); `review-data` and its scrub are untouched.
- *Where does the Share/Upload hand-off live, kept low-emphasis (origin deferred)?* A secondary toolbar item in the inspect window calling the existing `ReviewWindowOpener` — never a primary/prominent button (R9).
- *Does retiring the in-app browser link-out need a fallback?* The CLI `screencap view` remains the escape hatch for power users and any recording inspect can't render; no in-app fallback button in v1.

### Deferred to Implementation

- Which content classes inspect surfaces from the local events (origin deferred): the canonical export defaults `include_network=False`; decide at implementation whether a local-only looking surface should include network destinations. Default: mirror the review canonical export so `TimelineEventParser` is reused unchanged.
- Exact `inspect-data` envelope field set vs. reusing `ReviewDataEnvelope`'s schema verbatim (with redaction/coverage null) vs. a leaner inspect-specific envelope — settle when wiring `LiveInspectDataLoader`.
- Whether `InspectWindow` should focus an already-open window for the same recording on re-open (SwiftUI value-keyed dedup is automatic for `WindowGroup`; confirm behavior holds without extra `NSApp.windows` logic).

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```
ENTRY POINTS                      WINDOWS                         DATA PATH (Python)

Search result ──set seekMs──┐
                            ├──▶ InspectWindow (read-only)  ──▶  inspect-data --json
Recordings row ─────────────┘     · video + scrub timeline        · original dir
                                  · moment-anchored events         · canonical events (no scrub)
                                  · NO upload / mask / redaction    · flat screenshots
                                  · toolbar: "Share / Upload…"      · redaction/coverage = null
                                         │                          · video + timing (shared core)
                                         ▼
Recordings 'Upload' button ──▶ ReviewWindow (consent, UNCHANGED) ─▶ review-data --json
                                  · masked truth-view + Upload        · scrub_recording (600s)
                                                                      · -scrubbed dir

         prepare_review_data ──factored──▶ shared core (video remediation + timing read)
                                            ├─▶ review path  (+ scrub, scrubbed-dir artifacts)
                                            └─▶ inspect path (no scrub, original-dir artifacts)
```

---

## Implementation Units

### U1. `screencap inspect-data` — raw local playback envelope, no scrub

**Goal:** A new CLI verb returning the playback envelope (video + events + timing) from the **original** recording dir with no scrub, so a read-only window can load instantly.

**Requirements:** R4, R8 (and the foundation for R1)

**Dependencies:** None

**Files:**
- Modify: `src/screencap/review.py` (factor `prepare_review_data` into a shared video+timing core; add a raw inspect-data builder that skips `_prepare_scrubbed_copy`, resolves events via `exporter.ensure_canonical_events` on the original dir and frames from the original flat `screenshots/`, and omits redaction/coverage)
- Modify: `src/screencap/cli/__init__.py` (register `inspect-data` command mirroring `review_data_cmd`'s `--json` + envelope-on-stdout/error-envelope+exit-1 shape)
- Test: `tests/test_review.py` (or a sibling `tests/test_inspect_data.py` following the repo's test-layout convention)

**Approach:**
- Extract the original-dir steps shared with review (`_ensure_single_video` + `remediate_pixfmt_for_review`; timing via `catalog._read_recording_meta`) into a helper both paths call, so the video/timing contract can't drift between inspect and review.
- The inspect builder returns the same envelope keys the Swift decoder already understands (`ok`, `video_path`, `events_path`/`events_paths`, `started_at`, `duration_seconds`, `timing_status`, `video_pixfmt_remediated`) with `redaction`/`coverage` absent and `screenshots` empty or original-dir frames. Do not hold `recording_scrub_lock` and do not create a `-scrubbed` dir.
- Reuse the existing JSON-on-non-TTY defaulting and stderr-only progress.

**Patterns to follow:** `review_data_cmd` and `prepare_review_data` (`src/screencap/cli/__init__.py:1428`, `src/screencap/review.py:182`); the schema-v3 envelope and error-envelope+exit-1 shape; `_should_default_to_json`.

**Test scenarios:**
- Happy path: given a normal local recording, the command returns `ok: true` with `video_path` and `events_path` resolving inside the **original** recording dir (not a `-scrubbed` sibling). Covers AE1.
- Integration (no-scrub invariant): after the command runs, no `<name>-scrubbed` directory exists and no scrub pass was invoked — assert the scrubbed dir is absent / `scrub_recording` is not called.
- Edge: a playable recording with no action events returns `ok: true` with `started_at`/`duration_seconds` as JSON `null` (the SCR-102 contract), not an error.
- Edge: `redaction` and `coverage` are absent/null and the envelope still decodes against the review-data schema.
- Error path: a stub/missing-local recording returns `ok: false` with an `error` string and exit code 1 (not a crash). Covers AE4.

**Verification:** `inspect-data --json` on a fresh local recording returns a video+events envelope sourced from the original dir within a normal CLI latency budget (no minutes-long scrub), and produces no scrubbed artifacts.

---

### U2. Inspect data layer — `InspectWindowViewModel` + loader (read-only state machine)

**Goal:** A Swift view model that loads the inspect envelope and drives a `preparing → ready → failed` machine with no upload concepts.

**Requirements:** R1, R4, R5, R6

**Dependencies:** U1

**Files:**
- Create: `macos/ScreenCap/Views/Inspect/InspectWindowViewModel.swift` (the `InspectData` resolved model, `InspectDataLoader` protocol, `LiveInspectDataLoader` calling `inspect-data --json`, and the read-only state enum)
- Test: `macos/ScreenCapTests/InspectWindowViewModelTests.swift`

**Approach:**
- `InspectData` carries `videoURL`, `eventsURLs`, `startedAt`, `durationSeconds`, `timingStatus` — and deliberately NOT `redaction`/`coverage`/`screenshotURLs` (drop the upload-truth fields).
- State enum has exactly `preparing`, `ready(InspectData)`, `failed(message:)` — no `uploading`/`succeeded`/`refused`/`busy`, no `UploadController`, no effects protocol (the auto-close/refresh effects are upload-completion concerns).
- Readiness guard gates on `ok` + `video_path` + `events_path` only; timing falls back to `0` (SCR-102). Reuse `ReviewTimingStatus.resolve` (or an equivalent) so the timing-callout copy still works.
- `LiveInspectDataLoader` calls `CLIClient.runJSONRaw(["inspect-data", "--json", "--", name])` with a normal timeout (no 600s ceiling — there is no scrub to wait on).

**Execution note:** Implement the loader/state machine test-first against the `FakeInspectDataLoader` seam; the SCR-102 guard is the regression most likely to be reintroduced wrongly.

**Patterns to follow:** `ReviewWindowViewModel` + `ReviewDataLoader`/`LiveReviewDataLoader` (`macos/ScreenCap/Views/Review/ReviewWindowViewModel.swift`), but strip every upload/effects branch; `FakeReviewDataLoader` + `makeModel` injection in `ReviewWindowViewModelTests`.

**Test scenarios:**
- Happy path: `ok` envelope with video + events paths → `.ready` with those URLs.
- Edge (SCR-102 regression): `ok: true` with null `started_at`/`duration_seconds` → `.ready` with `startedAt == 0` / `durationSeconds == 0`, NOT `.failed`.
- Error path: `ok: false` or missing `video_path`/`events_path` → `.failed` with the envelope's error message.
- Error path: loader throws (subprocess error) → `.failed` with the error description.
- Structural: the state type exposes no upload state (a compile-level / exhaustiveness assertion that there is no `.uploading`/`.succeeded`).

**Verification:** Faked envelopes drive the three states deterministically; a null-timing envelope lands on `.ready`.

---

### U3. `InspectWindow` view + scene + opener bridge + Share/Upload hand-off

**Goal:** The read-only window itself — composes the reused playback panes, declares the scene, registers the opener seam, and carries the low-emphasis upload hand-off.

**Requirements:** R1, R4, R5, R7, R9, R10

**Dependencies:** U2

**Files:**
- Create: `macos/ScreenCap/Views/Inspect/InspectWindow.swift` (the `InspectWindowID` constant + the view: video + `TimelinePane` + `EventContentPane` + `TimingUnavailableCallout`, shared `currentTime`/seek wiring; a secondary toolbar "Share / Upload…" item)
- Create: `macos/ScreenCap/State/InspectWindowOpener.swift` (singleton mirroring `ReviewWindowOpener`: `openInspect` closure, `pendingSeekMs`, `open(recordingName:) -> Bool`)
- Modify: `macos/ScreenCap/ScreenCapApp.swift` (declare the `InspectWindow` `WindowGroup` injecting only `index`; register `InspectWindowOpener.shared.openInspect` in `OpenWindowBridge`)
- Modify: `macos/project.yml` only if needed; run `xcodegen generate` after adding files
- Test: `macos/ScreenCapTests/InspectWindowOpenerTests.swift`

**Approach:**
- Body is essentially `ReviewWindow.panesIfAvailable` minus the redaction/coverage/screenshot-truth rows and minus `bottomActions` (no Upload/Cancel row — a read-only window). `TimelinePane` is given empty `riskyIntervals`/`redactionMarkers` (its defaults), so it renders event ticks + cursor only.
- Seek-on-open mirrors SCR-174: read-and-clear `InspectWindowOpener.shared.pendingSeekMs[name]` on `.ready`, seek the `VideoPlayerPaneModel` via `SearchSeek.relativeSeconds`. From the Recordings list there is no pending seek → opens at start (R6).
- The "Share / Upload…" toolbar item is low-emphasis (secondary placement, not a prominent button) and calls `ReviewWindowOpener.shared.open(recordingName:)` — the only bridge to the consent window (R9/R10). Inspect performs no upload itself.
- Scene injects only `index`; verify no reused sub-view reaches for `auth`/`uploads` via `@EnvironmentObject` (the playback panes take plain inputs, so they're safe).

**Technical design:** *(directional)* `InspectWindow` body ≈ `VStack { TimingUnavailableCallout; HStack{ VideoPlayerPane } ; EventContentPane(onSeek:); TimelinePane(onScrub:) }` sharing one `@State currentTime`; toolbar carries the single secondary upload item. No bottom action row.

**Patterns to follow:** `ReviewWindow` composition (`macos/ScreenCap/Views/Review/ReviewWindow.swift:266-326`), the `WindowGroup`/`OpenWindowBridge` declaration (`ScreenCapApp.swift:99-113`, `:179-192`), and `ReviewWindowOpener` (`macos/ScreenCap/State/ReviewWindowOpener.swift`); `ReviewWindowOpenerTests` for the opener test shape.

**Test scenarios:**
- Opener dispatch: assigning `InspectWindowOpener.shared.openInspect` and calling `open(recordingName:)` invokes it with the name; returns `false` when unregistered (mirror `ReviewWindowOpenerTests`).
- No-dedupe pin + `pendingSeekMs` set/read-and-clear semantics match the review opener.
- ID stability: `InspectWindowID` constant pinned by a test (mirrors the review ID-stability test).
- Hand-off seam: invoking the toolbar "Share / Upload…" action calls `ReviewWindowOpener.open(recordingName:)` for the current recording. Covers AE3.
- (Manual QA, noted not unit-testable per SCR-55) the window renders with no Upload button, no masked pane, no redaction evidence; a from-search open seeks to the anchor. Covers AE1.

**Verification:** Opening the inspect scene shows a read-only playback surface with the secondary upload hand-off and no consent UI; the opener and hand-off seams are asserted in tests.

---

### U4. Re-point entry points + stub guard (Search results, Recordings-list click)

**Goal:** Route both "looking" entry points into the inspect window and replace the in-app browser link-out, with the stub pre-check at each site.

**Requirements:** R2, R6, R11

**Dependencies:** U3

**Files:**
- Modify: `macos/ScreenCap/Views/Search/SearchView.swift` (`openReview` → open inspect: set `InspectWindowOpener.shared.pendingSeekMs[recording]` then `openWindow(id: InspectWindowID, value:)`)
- Modify: `macos/ScreenCap/Views/RecordingsListView.swift` (row-body click → open inspect instead of `screencap view`; keep the per-row Upload button pointed at `ReviewWindowID`; reuse the `isStub` pre-check + friendly download alert)
- Test: `macos/ScreenCapTests/SearchViewInspectRoutingTests.swift` (or extend `SearchViewModelTests`) and a Recordings-list routing test
- Test: extend `macos/ScreenCapTests/RecordingsIndexRefreshOnUploadTests.swift` area only if a shared seam is touched (otherwise a focused new test file)

**Approach:**
- Search: on result tap, set the pending seek on `InspectWindowOpener` (not `ReviewWindowOpener`) and open the inspect scene; anchored vs unanchored results behave as today (anchored → seek, unanchored → open at start).
- Recordings list: the row-body click opens the inspect window for the recording; the browser link-out (`CLIClient.runAwaitingExit(["view", ...])`) is removed from the in-app path (the CLI command itself stays). The per-row Upload button is unchanged.
- Stub guard: at both sites, pre-check `rec.isStub` and show the existing "uploaded; local copy deleted — run `screencap download`" alert rather than opening an inspect window that would fail to load. For Search results, apply the same guard against the result's recording.

**Patterns to follow:** the current `SearchView.openReview` (`macos/ScreenCap/Views/Search/SearchView.swift:246-251`), `RecordingsListView.openInBrowser` stub pre-check + `.alert` plumbing (`:213-225`, `:24-30`).

**Test scenarios:**
- Search routing: tapping an anchored result sets `InspectWindowOpener.pendingSeekMs` for that recording and opens the inspect scene (asserted via the opener seam) — and does NOT open `ReviewWindowID`. Covers AE1, AE5.
- Recordings routing: a row-body click opens the inspect window (asserted via the opener seam), not the browser `view` shell-out. Covers AE2.
- Stub guard (Recordings): clicking a stub row shows the friendly download alert and opens no inspect window. Covers AE4.
- Stub guard (Search): selecting a result whose recording is a stub shows the same friendly alert rather than opening inspect. Covers AE4.
- Parity: the per-row Upload button still opens `ReviewWindowID` (unchanged). Covers R3.

**Verification:** From a search result and from a recordings row, the operator lands in the inspect window (browser viewer no longer used in-app); stub rows surface the download message at both sites.

---

## System-Wide Impact

- **Interaction graph:** two entry-point callsites switch openers (`SearchView`, `RecordingsListView`); `OpenWindowBridge` gains one registration; `ScreenCapApp` gains one scene. `ReviewWindow`/`ReviewWindowOpener` and the Upload button are untouched.
- **Error propagation:** `inspect-data` failures surface as `.failed` in the inspect view model (friendly message), and stub recordings are intercepted *before* window open via the click-site guard — no broken/empty window.
- **State lifecycle risks:** `inspect-data` must not create or touch the `-scrubbed` dir or hold `recording_scrub_lock`; the no-scrub invariant is asserted in U1 so a future refactor can't silently start scrubbing on the looking path.
- **API surface parity:** `inspect-data` joins `review-data` as a sibling CLI verb; both must keep the shared video/timing core in lockstep (the U1 extraction enforces this).
- **Integration coverage:** the search→inspect seek and the Recordings→inspect open are seam-tested via the opener closures; full window rendering is manual QA (SCR-55 — `WindowGroup` materialization isn't driven from XCTest).
- **Unchanged invariants:** `ReviewWindow`, `ReviewWindowViewModel`, `screencap review-data` (and its scrub-before-review ordering), the CLI `screencap view` HTML viewer, the per-row Upload button, capture, and the redaction pipeline all keep current behavior.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| `inspect-data` drifts from `review-data` on video/timing handling | Extract the shared original-dir video+timing core in U1; both verbs call it. |
| A future change makes the looking path scrub again (latency + wrong artifact) | U1 asserts the no-scrub invariant (no `-scrubbed` dir, `scrub_recording` not called). |
| New `.swift` files not picked up → `cannot find 'InspectWindow' in scope` | Run `cd macos && xcodegen generate` after adding files (XcodeGen stale-project learning). |
| Reused pane reaches for an upload-only `@EnvironmentObject` → runtime crash | Inject only `index`; the playback panes take plain inputs — verify none use `@EnvironmentObject` for `auth`/`uploads`. |
| Null-timing envelope wrongly treated as failure | Reuse the SCR-102 guard (ok + paths only; timing → 0) with a regression test in U2. |
| Stub recording opens an empty/broken inspect window | Click-site `isStub` pre-check at both entry points (U4), mirroring the existing Recordings-list guard. |

---

## Sources & References

- **Origin document:** [docs/brainstorms/2026-06-26-inspect-vs-upload-surface-separation-requirements.md](docs/brainstorms/2026-06-26-inspect-vs-upload-surface-separation-requirements.md)
- Related ask-your-history search origin: [docs/brainstorms/2026-06-24-ask-your-history-search-requirements.md](docs/brainstorms/2026-06-24-ask-your-history-search-requirements.md)
- Related native-review origin: [docs/brainstorms/2026-06-03-native-redaction-review-before-upload-requirements.md](docs/brainstorms/2026-06-03-native-redaction-review-before-upload-requirements.md)
- Learnings: `docs/solutions/ui-bugs/swiftui-windowgroup-vs-window-singleton-scene-2026-05-18.md`, `docs/solutions/integration-issues/review-data-nullable-timing-swift-consumer-2026-06-01.md`, `docs/solutions/build-errors/xcodegen-stale-project-missing-new-sources.md`
- Key code: `macos/ScreenCap/ScreenCapApp.swift`, `macos/ScreenCap/Views/Review/ReviewWindow.swift`, `macos/ScreenCap/Views/Review/ReviewWindowViewModel.swift`, `macos/ScreenCap/State/ReviewWindowOpener.swift`, `macos/ScreenCap/Views/Search/SearchView.swift`, `macos/ScreenCap/Views/RecordingsListView.swift`, `src/screencap/review.py`, `src/screencap/cli/__init__.py`
