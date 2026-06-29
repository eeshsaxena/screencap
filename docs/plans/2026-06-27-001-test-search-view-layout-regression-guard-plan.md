---
title: "test: Snapshot/UI regression guard for Search views"
type: test
status: completed
date: 2026-06-27
origin: linear SCR-184 (https://linear.app/zk-email/issue/SCR-184)
---

# test: Snapshot/UI regression guard for Search views

## Summary

Add view-rendering tests for the macOS `SearchView` that fail if the results pane collapses to zero size when results exist (the SCR-174 blank/frozen-detail regression class), and that structurally cover the core Search states. Implemented natively with `NSHostingView` inside the existing `ScreenCapTests` unit-test target — no new SPM dependency, no new test target, no pixel-diff golden images. A small behavior-preserving extraction makes the phase-driven content hostable without a live daemon or socket.

---

## Problem Frame

The SwiftUI Search views (SCR-174, PR #283) are build-plus-manual-QA only. A layout bug shipped where the results pane rendered completely blank and unresponsive on free-text queries: a `ScrollView`/`List` nested below sibling views in a `VStack` inside the `NavigationSplitView` detail never received height, starving every sibling. The view-model unit tests (`SearchViewModelTests`) all passed — nothing exercised layout, so nothing caught it. The fix (commit `995af6c7`: List-as-detail-root + `.safeAreaInset`) has no automated regression guard, so the same class of collapse could silently return on any future refactor of `SearchView.body`.

---

## Requirements

- R1. Structurally cover the core `SearchView` states: idle, searching, daemon-down, loaded-with-results (multi-day), empty ("Nothing recorded then" and "No matches"), and the consent banner.
- R2. A test fails if the results region renders empty / zero-size when `phase == .loaded` with results — guarding the exact regression class, not just view-model output.
- R3. Use the lightest viable approach for this repo: native `NSHostingView` hosting in the existing `ScreenCapTests` unit-test target. No new SPM dependency, no new target, no pixel/image golden files.
- R4. Tests are deterministic and CI-robust: no live socket/daemon, no embedded-CLI process spawn, no font/anti-aliasing pixel flakiness.

---

## Scope Boundaries

- **Not** adding image/pixel snapshot golden files or a snapshot-diffing library (SnapshotTesting). Rejected for macOS-CI flakiness and a new dependency — see Key Technical Decisions.
- **Not** adding an XCUITest UI-test target or launching the real app/daemon.
- **Not** changing any Search behavior, view-model logic, query parsing, ranking, or coverage mapping.
- **Not** re-testing concerns already covered: keyboard navigation (`SearchKeyboardNavTests`, SCR-183), VoiceOver label strings (`SearchAccessibilityTests`), query parsing (`QueryParserTests`), search orchestration (`SearchViewModelTests`), snippet highlighting (`SnippetHighlighterTests`).
- **Not** testing `ResultRow` thumbnail loading internals (covered by `ThumbnailLoaderTests` / `RecordingFrameIndexTests`); result rows are hosted as-is, with a no-op thumbnail loader.

### Deferred to Follow-Up Work

- Documenting the new view-hosting test pattern as a `docs/solutions/` learning so future view tests follow it: separate compound/ce-compound pass after this lands (not required for the regression guard itself).

---

## Context & Research

### Relevant Code and Patterns

- `macos/ScreenCap/Views/Search/SearchView.swift` — the view under test. Self-constructs `@StateObject private var model = SearchViewModel()` (not injectable), depends on `@EnvironmentObject RecordingsIndex` and `@Environment(\.openWindow)`, and runs `.task { loadSettings() }` (spawns the embedded CLI). The phase rendering lives in the `content` computed property (switch over `model.phase`) and `resultsList(_:)`.
- `macos/ScreenCap/Views/Search/SearchViewModel.swift` — defines `SearchViewModel.Phase` (`.idle`, `.searching`, `.loaded(SearchResults)`, `.daemonDown`) and the view-facing value types `SearchResults`, `SearchResultItem` (incl. `.anchorMs`, `.stream`), `CoverageReport`, `StreamState`. All are plain `Equatable`/`Sendable` structs/enums constructible directly in a test — the seam that makes phase-driven hosting possible without running a real search.
- `macos/ScreenCap/Views/RecordingsListView.swift` — the native List-as-detail-root + `.safeAreaInset` pattern the fix mirrored; reference for the correct composition.
- `macos/ScreenCapTests/SearchViewModelTests.swift`, `macos/ScreenCapTests/SearchAccessibilityTests.swift` — existing test style: `import XCTest` + `@testable import ScreenCap`, `final class …: XCTestCase`. All current tests are pure logic/string tests — **none host a SwiftUI view**, so this plan establishes the first view-hosting test pattern in the repo.
- `macos/project.yml` — single `ScreenCapTests` target of type `bundle.unit-test`; **no `packages:` section (zero SPM dependencies)**; macOS 13.0 floor, Swift 5.9, `SWIFT_STRICT_CONCURRENCY: complete`. Test sources are a path glob (`path: ScreenCapTests`), so a new file is picked up on the next `xcodegen generate`.

### Institutional Learnings

- `docs/solutions/build-errors/xcodegen-stale-project-missing-new-sources.md` — `macos/ScreenCap.xcodeproj` is git-ignored and generated from `project.yml`; the build script does **not** detect added source files. A new test file will silently not compile until `xcodegen generate` is re-run. Must regenerate after adding the new test file(s); call this out in verification.
- Memory: macOS app build/test runs via XcodeGen + `xcodebuild`; there is a known-flaky daemon-reconnect test unrelated to this work. Strict-concurrency-complete means all hosting/layout/assertions run `@MainActor`.

### External References

- None gathered. The approach uses only `AppKit.NSHostingView` + `XCTest`, both first-party and well-established; the repo already pins the relevant patterns locally.

---

## Key Technical Decisions

- **Native `NSHostingView` + layout/structural assertions, not a snapshot library or XCUITest** (user-confirmed): The acceptance criterion is a *layout* assertion ("results pane renders non-zero size"), which image snapshots serve poorly — macOS CI introduces font/anti-aliasing/OS-version pixel noise, and golden images need per-change maintenance. SnapshotTesting would also be the repo's first SPM dependency. XCUITest needs a new target, a running daemon + TCC grants, and cannot deterministically force the `.loaded`-with-results state. `NSHostingView` lays out in-memory (headless-friendly), needs no dependency or new target, runs in the existing unit-test target, and lets us assert the exact thing that broke. ("Snapshot" here means structural/layout snapshot, not pixel image.)
- **Make the content hostable via a behavior-preserving extraction, not by injecting the live model:** Hosting the full `SearchView` would drag in `@StateObject SearchViewModel` (private, self-constructed), `RecordingsIndex` env, `openWindow`, and a `.task` that spawns the embedded CLI. Instead, extract the phase-driven rendering into a pure view parameterized by a plain `Phase` value (+ display flags and action callbacks). Tests construct phases directly and host the pure view with no model/env/process. This keeps tests deterministic and is the repo-idiomatic "pure seam behind the view" pattern already used for `SearchViewModel`/`SearchAccessibility`.
- **Reproduce the regression's composition via a shared layout seam *inside a representative container*, not a copy:** The collapse lived at the boundary between `SearchView.body`'s `.frame(maxHeight: .infinity).safeAreaInset(edge: .top){ … }` wrapper and the results `List` — but crucially, the starvation was `NavigationSplitView`-detail-specific (`SearchTimelineView.swift` notes "a List that is the detail root sizes reliably; NavigationSplitView detail does not — it starves every sibling of height"). A bare `NSHostingView` with a fixed frame hands every child a definite height, which is exactly the condition under which the bug does **not** manifest — so hosting the content in a bare frame would make the negative control (U3) render non-collapsed and the guard toothless. Therefore both `SearchView.body` and the regression test render the content through one shared internal layout helper, and the regression test hosts that helper **inside a minimal `NavigationSplitView { … } detail: { … }`** so the detail column's height-proposal behavior — the actual failure mechanism — is in play. The shared seam keeps the composition shape from drifting from production.
- **Honest scope of the guard:** The guard protects the `searchDetailLayout` seam + the `SearchResultsView` (List-as-root) shape against collapse. It does **not** host the full `SearchView.body`, so a future refactor that re-composes `body` to bypass the seam (re-nesting a `List` below siblings directly in `body`) is outside its reach. Mitigation: U1 keeps `SearchView.body` a thin delegator to `searchDetailLayout`, so the seam is the single composition point a regression would have to go through; this constraint is stated in U1 and called out as a residual risk rather than silently assumed away.
- **Anchor assertions on stable `.accessibilityIdentifier`s, not text or view-tree position:** Add identifiers to each state's root region (idle/searching/daemon-down message, results list, empty row, consent banner). These are inert in production and give the test harness a stable handle to locate the bridged `NSView` and measure it.

---

## Open Questions

### Resolved During Planning

- Snapshot library vs native vs XCUITest: native `NSHostingView` (user-confirmed; see Key Technical Decisions).
- Where the tests live: the existing `ScreenCapTests` unit-test target — no new target.
- How to drive states without a daemon: construct `Phase`/`SearchResults` values directly and host a pure extracted view.

### Deferred to Implementation

- **Which hosting container actually reproduces the collapse:** does the pre-fix `VStack`-nested-`List` shape collapse to zero in a bare `NSHostingView` at a fixed frame, only inside a `NavigationSplitView` detail (U2's `hostInDetailColumn`), or only inside the real `MainWindow` detail? This is the load-bearing unknown — it determines whether U3's negative control can prove teeth at all. Resolve empirically as the first step of U3 (the negative control is a hard gate, not an afterthought); escalate the container fidelity until the broken shape collapses, or fall back to a documented structural-only guard.
- **Exact `NSView` traversal to measure the results region:** how SwiftUI's `List`/state roots bridge to the AppKit tree on macOS 13, and whether `.accessibilityIdentifier` lands on a discrete `NSView` or a container. Resolve by walking the hosted hierarchy at implementation time; fall back to locating the backing `NSScrollView`/`NSClipView` by class if identifier lookup is unreliable.
- **Whether `NSHostingView` needs attachment to an offscreen `NSWindow`** for layout/measurement to settle, or whether `setFrameSize` + `layoutSubtreeIfNeeded()` suffices. Determine empirically; attach an offscreen window only if measurements come back zero without one.
- **Exact non-zero height threshold** for the results region (e.g. host height minus the top bar). Pick a value that is clearly non-collapsed yet robust to row-height differences.

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

Shared layout seam used by both production and tests, so the regression test exercises the real composition:

```
// Production: SearchView.body
searchDetailLayout(
    bar:     { searchField }                      // real field, FocusState, query
    content: { SearchResultsView(phase: model.phase, …) }
)

// Test (regression guard): same seam, dummy bar, INSIDE a representative detail column
NavigationSplitView {
    EmptyView()                                       // minimal sidebar
} detail: {
    searchDetailLayout(
        bar:     { Color.clear.frame(height: 44) }    // stand-in top bar
        content: { SearchResultsView(phase: .loaded(resultsWithItems), …) }
    )
}
        │ host in NSHostingView, setFrameSize(800×600), layoutSubtreeIfNeeded()
        │ (the NavigationSplitView detail reproduces the height-proposal behavior
        │  that caused the original starvation — a bare frame would not)
        ▼
   walk NSView tree → find accessibilityIdentifier "search.results.list"
        ▼
   assert region.height ≥ threshold   // FAILS if results pane collapses
```

`searchDetailLayout` is a single internal helper (`func` or `ViewModifier`) applying `.frame(maxWidth: .infinity, maxHeight: .infinity).safeAreaInset(edge: .top) { bar }`. `SearchResultsView` is the extracted pure view holding the `phase` switch, `resultsList`, coverage/consent/empty/state-message subviews, and the Return-key handler — parameterized, no model/env/CLIClient.

---

## Implementation Units

### U1. Extract a pure, hostable Search content view + shared layout seam

**Goal:** Make the phase-driven Search rendering hostable in a test without a live model, environment, or CLI process — a behavior-preserving refactor of `SearchView`.

**Requirements:** R2, R3, R4

**Dependencies:** None

**Files:**
- Modify: `macos/ScreenCap/Views/Search/SearchView.swift`
- Create: `macos/ScreenCap/Views/Search/SearchResultsView.swift` (extracted pure view + `searchDetailLayout` seam; exact file split is the implementer's call)

**Approach:**
- Extract the `content` switch, `resultsList(_:)`, and their helpers (`stateMessage`, `coverageRow`/`coverageChip`, `consentBanner`, `emptyRow`, `interpretationText`, `returnKeyHandler`) into a new `SearchResultsView: View`.
- Parameterize it with plain inputs: `phase: SearchViewModel.Phase`, `consentDeclined: Bool`, `selection: Binding<SearchResultItem.ID?>`, `frameIndex`/`thumbnailLoader` (mirror `ResultRow`'s existing **optional** signatures — both are `actor`s; tests pass the defaulted instances or `nil`, and a nil loader renders the existing miss placeholder, so no special "no-op" type is needed), `isSearchFieldFocused: Bool`, and action callbacks (`onEnableConsent`, `onDeclineConsent`, `onOpen(SearchResultItem)`).
- Keep `SearchView.body` a **thin delegator** to `searchDetailLayout` — the seam must remain the single composition point so a future collapse-reintroducing change has to go through the shape the U3 guard exercises (see Key Technical Decisions "Honest scope of the guard").
- Move the `.frame(maxWidth: .infinity, maxHeight: .infinity)` that body applied to `content` into `SearchResultsView` (or the shared seam) so the view sizes itself when hosted.
- Introduce the `searchDetailLayout` seam (search-field bar via `.safeAreaInset` + content) and have `SearchView.body` compose through it; `SearchView` keeps the `@StateObject` model, env, `query`/`@FocusState`, `.task`/`.onAppear`/`.onChange`/`.alert`, and the action methods (`runSearch`, `openInspect`, `enableConsent`, `declineConsent`, `loadSettings`), wiring them as the callbacks/inputs.
- Preserve every gating invariant: the Return-key default-action handler must still stand down when the search field is focused or the consent banner is showing (SCR-183 review #1).

**Execution note:** Behavior-preserving extraction — keep the diff mechanical; no logic or string changes. Run the existing Search tests after extracting to confirm no behavior drift before adding new tests.

**Patterns to follow:** The existing "pure logic behind a seam" split (`SearchViewModel`, `SearchAccessibility`, `searchReviewTarget`); `RecordingsListView`'s List-as-root + `.safeAreaInset` composition.

**Test scenarios:**
- Test expectation: none directly — this unit is a behavior-preserving extraction; its correctness is verified by (a) existing `SearchViewModelTests` / `SearchKeyboardNavTests` / `SearchAccessibilityTests` continuing to pass and (b) the new U3/U4 tests exercising the extracted view.

**Verification:**
- The macOS app builds after `xcodegen generate`; existing Search tests pass unchanged.
- `SearchView` still renders idle by default and the consent/keyboard/openInspect flows are wired through the new callbacks with no behavior change.
- Explicitly confirm the Return-key gating still holds after extraction — focused field → handler absent; consent banner up → handler absent (SCR-183 review #1). Passing focus as an `isSearchFieldFocused: Bool` snapshot rather than a live `@FocusState` may change update timing, so verify this on-device/manually rather than assuming `SearchKeyboardNavTests` covers the new wiring; if the snapshot proves lossy, keep the Return handler in `SearchView` (passing only `selection` + `onOpen` to the extracted view).

---

### U2. View-hosting test harness + stable accessibility identifiers

**Goal:** Provide the first SwiftUI view-hosting test utility in the repo and stable anchors for locating each state's rendered region.

**Requirements:** R2, R3 (enables R1's coverage, which U4 fulfills)

**Dependencies:** U1

**Files:**
- Create: `macos/ScreenCapTests/SearchViewHostingHarness.swift` (or a shared `ViewHostingHarness` helper)
- Modify: `macos/ScreenCap/Views/Search/SearchResultsView.swift` (add `.accessibilityIdentifier`s)

**Approach:**
- Harness (`@MainActor`): host any `some View` in an `NSHostingView`, set a fixed `frameSize`, force layout (`layoutSubtreeIfNeeded()`; attach an offscreen `NSWindow` only if measurements require it), and expose helpers to (a) find a descendant `NSView` by `accessibilityIdentifier`, and (b) return its rendered size. Include a class-based fallback finder (e.g. nearest `NSScrollView`) for when identifier propagation is unreliable.
- Provide a `hostInDetailColumn(_:)` wrapper that embeds the view under test in a minimal `NavigationSplitView { EmptyView() } detail: { … }` before hosting — so the regression test (U3) exercises the detail-column height-proposal behavior that produced the original starvation, which a bare fixed-frame host does not reproduce. State-coverage hosting (U4) can use the plain (non-split) host.
- Provide a `makeResults(...)` factory to build `SearchResults`/`SearchResultItem` fixtures (multi-day anchored items, unanchored audio item, empty + timeWindow, consent-needed) so each test reads as one clear arrange step.
- Add stable identifiers to `SearchResultsView` regions: e.g. `search.state.idle`, `search.state.searching`, `search.state.daemonDown`, `search.results.list`, `search.results.empty`, `search.consentBanner`.

**Patterns to follow:** `@testable import ScreenCap`; existing test fixtures' construction style in `SearchViewModelTests`.

**Test scenarios:**
- Happy path: harness hosts a trivial identified view and returns a non-zero size for that identifier — proves the find-and-measure path works before relying on it in U3/U4. (Smoke test for the harness itself.)
- Edge case: requesting a missing identifier returns nil/absent rather than crashing, so state-presence assertions are well-defined.

**Verification:**
- The smoke test reliably finds and measures a hosted identified view in CI (`xcodebuild test`), with or without an attached offscreen window as determined during implementation.

---

### U3. Regression guard: results region renders non-zero when results exist

**Goal:** Directly enforce the acceptance criterion — fail if the results pane collapses to empty/zero-size when `phase == .loaded` with results.

**Requirements:** R2, R4

**Dependencies:** U1, U2

**Files:**
- Create: `macos/ScreenCapTests/SearchViewLayoutTests.swift`

**Approach:**
- Host the content through the shared `searchDetailLayout` seam (dummy top bar + `SearchResultsView(phase: .loaded(results), …)`) **inside U2's `hostInDetailColumn` wrapper** at a fixed size (e.g. 800×600), force layout, locate `search.results.list`, and assert its rendered height ≥ a non-collapsed threshold and that row content is present.
- Include a negative-control to prove the guard has teeth: reconstruct the pre-fix composition (results `List` nested below a sibling in a `VStack`, no max-height) and assert that under the same hosting it *does* collapse (region height below threshold).
- **Gate on the negative control before trusting the guard:** the original starvation was `NavigationSplitView`-detail-specific, so the broken shape may NOT collapse under a bare fixed-frame host. Empirically confirm the broken shape collapses under the chosen hosting; if it does not collapse even inside `hostInDetailColumn`, the container is not reproducing the failure mechanism — escalate (host the real `SearchView`/`MainWindow` detail composition, or whatever minimally reproduces the height ambiguity) until the control collapses. If no hosting reproduces it, drop the "proves teeth" claim and document the guard as **structural-only** (asserts the list renders, not that it survives the specific starvation) rather than shipping a green-by-construction test. Keep the broken shape local to the test file.

**Patterns to follow:** U2 harness + fixtures; `@MainActor` test methods.

**Test scenarios:**
- Covers AE (ticket acceptance): Happy path — `phase == .loaded` with multi-day results → `search.results.list` region height ≥ threshold and contains result-row content (non-collapsed).
- Edge case: `phase == .loaded` with a single result (one day) → results region still non-zero.
- Edge case: results containing only unanchored audio items → the "Heard in audio (time approximate)" section renders at non-zero size.
- Edge case (negative control / guard-has-teeth): the pre-fix `VStack`-nested-`List` composition, hosted **inside `hostInDetailColumn`**, collapses below threshold — proving the guard fails on the regression class. This control is a hard gate (see Approach): if it does not collapse, the harness is not reproducing the bug and the guard is not yet trustworthy.

**Verification:**
- The guard test passes on current `main` (post-fix) AND the negative control empirically collapses under the chosen hosting — both must hold. If the control cannot be made to collapse in any hosting, the unit ships as a documented structural-only check, not as a teeth-proven regression guard.

---

### U4. State coverage: structural snapshots of the core Search states

**Goal:** Snapshot-cover each core state so layout/structure changes that break a state surface in CI.

**Requirements:** R1, R4

**Dependencies:** U1, U2

**Files:**
- Modify: `macos/ScreenCapTests/SearchViewLayoutTests.swift` (or a sibling `SearchViewStateTests.swift`)

**Approach:**
- One test per state: build the phase fixture, host `SearchResultsView` (idle/searching/daemon-down need no results), force layout, assert the state's identifier region is present and renders at non-zero size with no crash. For multi-state distinctions, also assert a discriminating marker (e.g. day section headers for multi-day; "Nothing recorded then" vs "No matches"; the consent banner's two buttons).
- Drive the empty-state branch precisely: authoritative empty requires `timeWindow != nil` and `coverage.activity == .empty` ("Nothing recorded then"); otherwise "No matches".
- Drive the consent banner via `consentNeeded == true` with `consentDeclined == false`.

**Patterns to follow:** U2 harness + fixtures; existing per-case test granularity in `SearchAccessibilityTests`.

**Test scenarios:**
- Happy path: idle → `search.state.idle` present, renders non-zero, no crash.
- Happy path: searching → `search.state.searching` (progress) present, renders non-zero.
- Happy path: daemon-down → `search.state.daemonDown` present; carries the "ScreenCap isn't running" message.
- Happy path: loaded multi-day with results → multiple day `Section` headers render; rows present (overlaps U3 but asserts the multi-day grouping specifically).
- Edge case: authoritative empty (timeWindow + activity `.empty`) → `search.results.empty` shows "Nothing recorded then".
- Edge case: non-authoritative empty (no timeWindow) → `search.results.empty` shows "No matches".
- Happy path: consent banner (`consentNeeded`, not declined) → `search.consentBanner` present with "Turn on" and "Not now" buttons.

**Verification:**
- All seven state assertions pass under `xcodebuild test` after `xcodegen generate`; each state renders deterministically with no daemon/socket/process involvement.

---

## System-Wide Impact

- **Interaction graph:** U1 touches `SearchView.body` composition — the Return-key `.defaultAction` handler, double-click-to-open, consent enable/decline, and `openInspect`/`openWindow` wiring must be preserved through the new callbacks. The keyboard-selection gating (stand down while field focused or consent banner up) is behavior-sensitive and guarded by `SearchKeyboardNavTests`.
- **Error propagation:** Tests must not trigger real side effects — `onOpen`/consent callbacks are no-ops in tests; no `CLIClient` spawn, no `openWindow`. Production wiring is unchanged.
- **State lifecycle risks:** None new at runtime; the refactor is behavior-preserving. The risk is layout drift introduced by moving the `.frame`/`.safeAreaInset` — covered by U3/U4.
- **API surface parity:** No public/CLI/daemon surface changes. Internal-only Swift extraction.
- **Integration coverage:** The new tests exercise real SwiftUI→AppKit layout (what mocks and view-model tests cannot prove) — that is the point of the plan.
- **Unchanged invariants:** Search behavior, query parsing, ranking, coverage mapping, accessibility label strings, the `.safeAreaInset` field pinning, and `loadSettings` remain exactly as-is. New `.accessibilityIdentifier`s are inert and do not alter VoiceOver labels (which use `.accessibilityLabel`).

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| SwiftUI→AppKit bridging: `.accessibilityIdentifier` may not land on a discrete `NSView`, or `List` measurement is opaque on macOS 13 | Class-based fallback finder (nearest `NSScrollView`/`NSClipView`); U2 smoke test validates find-and-measure before U3/U4 depend on it; exact traversal deferred to implementation |
| `NSHostingView` layout doesn't settle without a window (zero measurements) | Attach an offscreen `NSWindow` in the harness if needed; determined empirically in U2 |
| Extraction subtly changes layout (lost `.frame`, altered `.safeAreaInset` nesting) | Shared `searchDetailLayout` seam used by both prod and test; run existing Search tests after U1; U3 negative control proves sensitivity |
| New test file silently not compiled (XcodeGen stale project) | Run `xcodegen generate` after adding files; documented in `docs/solutions/build-errors/xcodegen-stale-project-missing-new-sources.md` and in each unit's verification |
| **Guard toothless: broken shape doesn't collapse under bare hosting** (the original starvation was `NavigationSplitView`-detail-specific, so a fixed frame may give the broken shape height) | U2's `hostInDetailColumn` wraps the content in a real `NavigationSplitView` detail; U3 makes the negative-control collapse a **hard gate** with empirical verification and an escalation path, falling back to a documented structural-only guard rather than shipping green-by-construction |
| **Guard blind to `SearchView.body` re-composition** that bypasses the seam (the stated regression vector) | U1 keeps `body` a thin delegator to `searchDetailLayout` so the seam is the single composition point; the guard's scope is stated honestly in Key Technical Decisions rather than overclaimed |
| FocusState passed as a `Bool` snapshot changes Return-gating behavior | U1 verification explicitly checks the gating post-extraction; fallback keeps the Return handler in `SearchView` if the snapshot proves lossy |

---

## Documentation / Operational Notes

- After adding the new test file(s), run `xcodegen generate` (or the repo build script's regeneration path) before `xcodebuild test` — the build script does not auto-detect added sources (see institutional learning).
- All hosting, layout, and assertions run on the main actor (`SWIFT_STRICT_CONCURRENCY: complete`); annotate test methods/harness `@MainActor`.
- Consider a follow-up `docs/solutions/` entry documenting the view-hosting test pattern once it lands (listed under Deferred to Follow-Up Work).

---

## Sources & References

- **Origin issue:** [SCR-184 — Snapshot/UI tests for Search views (layout regression guard)](https://linear.app/zk-email/issue/SCR-184)
- **Parent / context:** [SCR-174 — Ask-Your-History Search (in-app, v1)](https://linear.app/zk-email/issue/SCR-174); `docs/brainstorms/2026-06-24-ask-your-history-search-requirements.md`
- **Regression fix being guarded:** commit `995af6c7` — "fix(scr-174): render search results as a List to fix blank/frozen detail"
- Code under test: `macos/ScreenCap/Views/Search/SearchView.swift`, `macos/ScreenCap/Views/Search/SearchViewModel.swift`
- Test conventions: `macos/ScreenCapTests/SearchViewModelTests.swift`, `macos/ScreenCapTests/SearchAccessibilityTests.swift`; `macos/project.yml`
- Learning: `docs/solutions/build-errors/xcodegen-stale-project-missing-new-sources.md`
