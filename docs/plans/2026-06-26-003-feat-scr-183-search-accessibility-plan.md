---
title: "feat: Accessibility for Search (VoiceOver labels + keyboard navigation)"
type: feat
status: active
date: 2026-06-26
origin: https://linear.app/zk-email/issue/SCR-183/accessibility-for-search-voiceover-labels-keyboard-navigation
---

# feat: Accessibility for Search (VoiceOver labels + keyboard navigation)

## Summary

Give the in-app Search surface (SCR-174) the accessibility pass it shipped without: VoiceOver labels for the custom-drawn bits (coverage chips, consent banner + buttons, result rows), full keyboard operability (focus the field on open, arrow through results, Return opens the focused result in Review), and a Dynamic-Type / larger-text check so the rows don't break. The implementation approach mirrors the codebase's established testable-view-logic pattern — extract label strings into pure helpers that XCTest asserts on, then apply them via `.accessibilityElement` / `.accessibilityLabel` in the view. Behavioral concerns that have no unit-test harness (focus-on-open, arrow navigation, Dynamic Type layout) are verified manually with VoiceOver + keyboard.

---

## Problem Frame

The Search surface was built fast as the v1 "ask your history" wedge (SCR-174, PR #283) and its doc review flagged — as an FYI — that no accessibility work was done. The surface is heavily custom-drawn: coverage state is a colored dot plus text (`coverageChip`), the consent prompt is a hand-built banner with two buttons, and each result is a `Button` wrapping a multi-`Text` `HStack`. To VoiceOver this reads as a pile of disconnected fragments (or, for the color-dot state, nothing at all), and there's no verified keyboard path from the search field through the results into Review. For a product whose persona explicitly includes non-technical operators (STRATEGY.md, "UX & native experience" track, load-bearing this quarter), a search feature that isn't operable with VoiceOver or the keyboard is incomplete.

---

## Requirements

- R1. Coverage chips announce a single meaningful label that conveys the state the color currently conveys only visually — e.g. "On screen: not indexed", "Audio: 3 results", "Activity: no matches". (Ticket: VoiceOver labels for coverage chips.)
- R2. The consent banner is a coherent VoiceOver element: its heading + explanatory body are announced together, and the two actions read as clearly-labeled buttons ("Not now", "Turn on"). (Ticket: VoiceOver labels for the consent banner + its buttons.)
- R3. Each result row announces one combined label — app/snippet + time + stream, plus an "approximate, from audio" qualifier where applicable — instead of fragmented `Text` runs; the decorative thumbnail stays hidden. The label must surface **no more than the row already shows visibly** (snippet only, never full OCR text). (Ticket: result rows announce app/snippet + time + stream.)
- R4. Opening the Search surface moves keyboard focus to the search field. (Ticket: focus the search field on open.)
- R5. Results are navigable by arrow keys; Return opens the focused result in the Review window at its moment. (Ticket: arrow through results, Return to open in Review.)
- R6. Dynamic Type / larger accessibility text sizes do not break result-row layout (no clipping, overlap, or lost time/snippet). Verify first; harden the row layout only if it breaks. (Ticket: verify Dynamic Type doesn't break the rows.)
- R7. The **non-result states** are accessible, not just the result rows: decorative-only icons across the surface are hidden from VoiceOver (search-field magnifying glass, consent-banner viewfinder, interpretation wand, lock); the searching, empty / "nothing recorded", idle, and daemon-down states each read as **one coherent message**; and a **search-outcome change** (results ready / no matches / ScreenCap not running) is **announced** to VoiceOver so a user who can't see the screen learns the result of their search. (Derived from completing "fully operable with VoiceOver" — without this, a blind user presses Return and hears only silence or "progress indicator".)
- R8. Acceptance: the Search surface is fully operable end-to-end with VoiceOver + keyboard, and result rows announce meaningful labels.

**Origin actors:** A1 (Operator — the non-technical internal-tool user searching their own history), carried from SCR-174. This work is squarely in service of A1's operability.

---

## Scope Boundaries

- No changes to capture, the daemon, the search query model, ranking, or the privacy/redaction pipeline — this is a read-only UI polish on an existing surface (consistent with SCR-174's "Outside this feature's identity").
- No bump of the macOS deployment target (stays 13.0); accessibility work must fit within macOS 13 APIs.
- No new accessibility audit of other surfaces (Calendar, Recordings, Privacy, Review) — SCR-183 is scoped to Search only.
- No global hotkey / Spotlight overlay (that is the deferred SCR-174 destination, not this ticket).
- Does not introduce a UI-test (XCUITest) harness; behavioral acceptance is verified manually (see Risks).

---

## Context & Research

### Relevant Code and Patterns

- `macos/ScreenCap/Views/Search/SearchView.swift` — owns the search field (`searchField`), `coverageChip` / `coverageRow` / `coverageText` / `coverageColor`, the `consentBanner`, the results `List` (`resultsList`), and the idle/empty/daemon-down state messages. All target elements live here except the row.
- `macos/ScreenCap/Views/Search/SearchTimelineView.swift` — holds `ResultRow` (the result cell) plus the `SearchResultItem` display extension (`primaryText`, `secondaryText`, `timeLabel`, `streamIcon`, `streamTint`) and the day-grouping helpers. The thumbnail cell is already `.accessibilityHidden(true)` with a comment noting "the Button already announces the row's text + time" — R3 makes that announcement real and combined.
- `macos/ScreenCap/Views/Search/SearchViewModel.swift` — defines `StreamState` (the enum coverage chips render), `CoverageReport`, and `SearchResultItem` (`stream`, `app`, `title`, `snippet`, `anchorMs`, `approximate`). Pure data; no view changes needed here, but the label helpers read these types.
- `macos/ScreenCap/Views/MainWindow.swift` — hosts Search: `detail` returns a fresh `SearchView()` whenever `section == .search` (so `.onAppear`/`.task` fire on each open — the natural hook for R4 focus-on-open). The sidebar itself is a `List(selection: $section)` (the one existing `List(selection:)` precedent in the app).
- `macos/ScreenCap/ScreenCapApp.swift:140-170` — the canonical existing accessibility pattern to mirror: decorative sub-views get `.accessibilityHidden(true)`, the composite element gets one `.accessibilityLabel(...)` whose text is built by a pure `private var`/function (`accessibilityDescription`). Replicate this shape for chips and rows.
- `macos/ScreenCapTests/PrivacyBadgeStyleTests.swift` and `SnippetHighlighterTests.swift` — the testable-view-logic pattern: display logic lives in a pure helper (`PrivacyBadgeStyle.derive(for:)`, `SnippetHighlighter.attributed(...)`) and tests assert on its output. Accessibility-label helpers follow the same shape.

### Institutional Learnings

- `docs/solutions/ui-bugs/swiftui-windowgroup-vs-window-singleton-scene-2026-05-18.md` — Search/Review are singleton `Window` scenes; relevant only as a reminder that focus/`@FocusState` lives inside the window body, not at `App` scope.
- No existing `docs/solutions/` entry covers SwiftUI accessibility or macOS keyboard navigation — this is new ground for the repo, which is why R5's Return mechanism is treated as an execution-time unknown rather than asserted up front.

### External References

- macOS 13 API boundary (verified in `macos/project.yml`: `deploymentTarget.macOS: "13.0"`): `@FocusState` + `.focused()` are available (macOS 12+); `List(selection:)` arrow-key navigation is available; **`.onKeyPress` and `.defaultFocus` are macOS 14+ and therefore unavailable.** This is the single most plan-shaping external fact — it forces the keyboard design onto the older `List(selection:)` + `.keyboardShortcut` / activatable-row toolkit and makes the exact Return-handling mechanism something to confirm against real macOS 13 behavior.

---

## Key Technical Decisions

- **Build every label in a pure helper, apply in the view.** Mirrors `PrivacyBadgeStyle` / `SnippetHighlighter`. This is the only way to get test coverage for the labels given there is no ViewInspector/XCUITest in the project, and it keeps the label logic (which encodes all the state→phrase mapping) out of `body`.
- **Compose each result row into one accessibility element** (`.accessibilityElement(children: .ignore)` + one `.accessibilityLabel`) rather than labeling each `Text`. A combined element is what makes the row a single meaningful VoiceOver stop and is required for the keyboard "Return opens the focused result" story to feel coherent. The thumbnail stays `.accessibilityHidden(true)`.
- **Keyboard navigation via native `List(selection:)`, not a custom focus layer.** A Medium-priority polish ticket does not justify a bespoke keyboard-focus system. Use a selection binding + tagged rows for arrow navigation (macOS 13-supported) and resolve Return-to-open with the macOS 13 toolkit. This keeps the change small and within framework behavior.
- **Return-handling mechanism is deferred to implementation** (see Open Questions). Because `.onKeyPress` is unavailable on macOS 13, the exact way Return on a selected row opens Review — a hidden `Button` bound to `.keyboardShortcut(.defaultAction)` acting on the current selection, vs. converting rows to selection-activation, vs. a double-click + Return pairing — depends on observed macOS 13 List behavior and must be validated by running it. The plan commits to the outcome (R5), not the mechanism.
- **Dynamic Type is verify-first.** The ticket says "verify it doesn't break." Treat U5 as a characterization check; only harden the row layout (the fixed `56×32` thumbnail + `Spacer` + trailing time stack is the suspect) if the check shows breakage.
- **Privacy parity for spoken labels.** VoiceOver labels read only what is already visible in the row (the snippet, not the underlying full OCR/transcript text) — preserving SCR-174's pointer-only / "no more than necessary" posture (R8 of origin) in the audio channel too.

---

## Open Questions

### Resolved During Planning

- **Are there separate timeline "markers" to make accessible?** No. Despite `SearchTimelineView.swift`'s name and the origin's "scrubbable per-day timeline" language, results render as a `List` with per-day `Section`s; there are no marker glyphs. Scope is the chips, banner, rows, field, and list navigation.
- **Where does "focus on open" hook in?** `MainWindow.detail` constructs a fresh `SearchView()` each time the Search section is selected, so a `.task`/`.onAppear` that sets `@FocusState` fires on every open. No cross-view focus plumbing needed.
- **What's the test seam?** Pure label-builder helpers (XCTest), per the `PrivacyBadgeStyle` pattern. Behavioral keyboard/focus/Dynamic-Type checks are manual.

### Deferred to Implementation

- **Exact Return-to-open mechanism on macOS 13** — resolve by running the List on macOS 13 (hidden default-action `Button` on current selection vs. selection-activation vs. double-click pairing). Candidate mechanisms are enumerated in U4; the choice depends on real behavior.
- **Whether Return must be focus-scoped to avoid colliding with the search field's `.onSubmit`** — in the field, Return runs the search; in the list, Return opens the result. Confirm these don't both fire when a `.keyboardShortcut(.defaultAction)` is present in the same window; if they do, gate the list shortcut on focus/selection state.
- **Whether the row layout actually breaks under larger text** (U5) — and if so, the specific hardening (`ViewThatFits`, allow the snippet/time to wrap, `@ScaledMetric` thumbnail, or a vertical fallback). Determined by the verification, not pre-committed.
- **Whether VoiceOver should announce coverage as its own grouped summary** vs. three independent chip elements — decide while hearing it; default is three independent chips (one per stream) since they map to distinct states.
- **Whether macOS 13's `List(selection:)` auto-announces the selected row's selected state** (e.g. "…, selected") during arrow navigation, or whether U4 must add `.accessibilityAddTraits(.isSelected)` on the selected row. Resolve by listening with VoiceOver during U4; affects whether keyboard nav reads coherently for a VoiceOver user.

---

## Implementation Units

### U1. VoiceOver labels for coverage chips, consent banner & static chrome

**Goal:** Make the coverage row and the consent banner first-class VoiceOver elements (chips announce "{stream}: {state}", carrying the meaning the color dot conveys silently; the banner reads as one element with two clearly-labeled buttons), and clean up the surface's decorative-icon noise so VoiceOver doesn't stop on orphaned glyphs.

**Requirements:** R1, R2, R7 (decorative-icon hygiene)

**Dependencies:** None

**Files:**
- Create: `macos/ScreenCap/Views/Search/SearchAccessibility.swift` (pure label-builder helpers shared across units — e.g. `coverageChipAccessibilityLabel(stream:state:)`)
- Modify: `macos/ScreenCap/Views/Search/SearchView.swift` (apply `.accessibilityElement`/`.accessibilityLabel` on `coverageChip` and `consentBanner`; mark decorative glyphs hidden)
- Test: `macos/ScreenCapTests/SearchAccessibilityTests.swift`

**Approach:**
- Add a pure helper that maps `(streamLabel, StreamState)` → spoken string, reusing the existing `coverageText` phrasing ("not indexed", "no matches", "limited", "unavailable", "{count} results"). The visible chip keeps its compact text; the accessibility label spells out the count as "N results" and never relies on color.
- On `coverageChip`, hide the `Circle` (`.accessibilityHidden(true)`) and set the chip's `.accessibilityLabel` to the helper output. Keep the `.notRun` case rendering `EmptyView()` (no element).
- On `consentBanner`, group the heading + body so VoiceOver reads the prompt as one element; leave the two `Button`s as their own elements (SwiftUI already exposes them as buttons — confirm their titles "Not now"/"Turn on" are the labels, add hints if ambiguous).
- **Decorative-icon hygiene (D2/D4/D6 from review):** hide the icons whose meaning the adjacent text already carries — the search-field `Image(systemName: "magnifyingglass")`, the consent-banner `Image(systemName: "text.viewfinder")`, the interpretation `Label(..., systemImage: "wand.and.stars")` (apply `.labelStyle(.titleOnly)` or hide its icon), and the `Label("Searches only what's on this Mac", systemImage: "lock.fill")` (`.labelStyle(.titleOnly)`). Without this, VoiceOver announces "magnifying glass", "text viewfinder", "wand", "lock" as orphaned stops, fragmenting the flow this plan is trying to make coherent.
- Give the search `TextField` an `.accessibilityLabel("Search your history")` so the focused field (U3) announces meaningfully — the placeholder is illustrative, not a label.

**Patterns to follow:**
- `ScreenCapApp.swift:140-170` (decorative-hidden + one composite label, label text from a pure helper).
- `PrivacyBadgeStyle` (`macos/ScreenCap/Views/Privacy/PrivacyBadgeStyle.swift`) for the pure-derive-then-assert shape.

**Test scenarios:**
- Happy path: `coverageChipAccessibilityLabel(stream: "On screen", state: .ok(count: 3))` → "On screen: 3 results" (and singular "1 result" for count 1).
- Edge case: `.notIndexed` → "On screen: not indexed"; `.empty` → "{stream}: no matches"; `.degraded` → "{stream}: limited"; `.unavailable` → "{stream}: unavailable".
- Edge case: `.notRun` → helper returns nil / chip produces no element (assert the view hides it; assert the helper's contract for `.notRun`).
- Edge case: label is independent of color — assert the same `.empty` and `.notRun` both reading as text (they share `coverageColor == .secondary` but must read differently: "no matches" vs. no element).
- Verification (manual, no unit seam for `.accessibilityHidden`): VoiceOver does not stop on the magnifying-glass, viewfinder, wand, or lock icons — each is silent; the interpretation label still reads its full text.

**Verification:**
- With VoiceOver on, navigating the coverage row speaks each present stream's state in words; the color dot is not announced separately.
- The consent prompt is read as a single coherent sentence, then "Not now, button" and "Turn on, button".
- No spurious VoiceOver stops on the decorative search/banner/interpretation/lock icons.

---

### U2. Combined VoiceOver label for result rows

**Goal:** Replace the row's fragmented `Text` runs with one combined accessibility element that announces app/snippet + time + stream (+ "approximate, from audio" when `approximate`), keeping the thumbnail hidden and surfacing nothing beyond what's visible.

**Requirements:** R3

**Dependencies:** U1 (shares `SearchAccessibility.swift`)

**Files:**
- Modify: `macos/ScreenCap/Views/Search/SearchTimelineView.swift` (`ResultRow.body`: wrap as one accessibility element with a computed label)
- Modify: `macos/ScreenCap/Views/Search/SearchAccessibility.swift` (add `resultRowAccessibilityLabel(item:)`)
- Test: `macos/ScreenCapTests/SearchAccessibilityTests.swift`

**Approach:**
- Add `resultRowAccessibilityLabel(_ item: SearchResultItem) -> String` that composes the existing display fields: stream phrasing ("On screen" / "Heard in audio" / the app name for activity), the primary text (snippet or app), the secondary text (window title for activity), and `timeLabel` — with an "approximate, from audio" suffix when `item.approximate`, and a graceful form when `anchorMs`/snippet are missing ("(no preview)", "time unknown").
- In `ResultRow.body`, apply `.accessibilityElement(children: .ignore)` + `.accessibilityLabel(...)`; keep `thumbnailCell.accessibilityHidden(true)`. Confirm the enclosing `Button` exposes the combined label as its accessibility label and a `.button` trait (so it reads "…, button").
- Order the label so the most-recognizable token leads (app/snippet), matching how the row reads visually left-to-right.

**Patterns to follow:**
- The `SearchResultItem` display extension already centralizes `primaryText`/`secondaryText`/`timeLabel`/`streamIcon` — the label helper composes those, it does not re-derive them.
- `SnippetHighlighter` test style for asserting produced strings.

**Test scenarios:**
- Happy path (activity): item with `stream: .activity`, `app: "Salesforce"`, `title: "Refunds"`, `anchorMs` at 14:30 → label contains "Salesforce", "Refunds", "14:30" and reads as activity (no "approximate").
- Happy path (screen): `stream: .screen`, `snippet: "refund error"`, time present → "On screen", "refund error", time; no "approximate".
- Happy path (audio): `stream: .audio`, `approximate: true` → includes "Heard in audio" and the "approximate" qualifier.
- Edge case: `snippet` nil/empty → label uses "(no preview)" (mirrors `primaryText`), not an empty string.
- Edge case: `anchorMs` nil (unanchored audio hit) → label degrades to a "time unknown"/no-time form rather than reading "—".
- Integration: assert the thumbnail remains accessibility-hidden (no media/frame is announced) — privacy parity (R3/origin R8).

**Verification:**
- VoiceOver lands on each row as a single stop and speaks one sentence (app/snippet, time, stream, approximate-if-audio), then "button".
- No separate stops for the snippet, time, or "≈ audio" sub-views; the thumbnail is silent.

---

### U3. Focus the search field when Search opens

**Goal:** When the user switches to the Search section, keyboard focus lands in the search `TextField` so they can type immediately.

**Requirements:** R4

**Dependencies:** None

**Files:**
- Modify: `macos/ScreenCap/Views/Search/SearchView.swift` (`@FocusState` + `.focused()` on the field; set focus in `.task`/`.onAppear`)
- Test: none (behavioral focus has no unit-test seam) — manual verification.

**Approach:**
- Add `@FocusState private var searchFieldFocused: Bool`, bind `.focused($searchFieldFocused)` on the `TextField`, and set it true on appear. Because `MainWindow.detail` builds a fresh `SearchView()` per open, on-appear focus is correct and re-fires each time the section is entered.
- The field's VoiceOver label ("Search your history") is added in U1; this unit just confirms the focused field announces it (the placeholder is illustrative, not a label).

**Patterns to follow:**
- Standard SwiftUI `@FocusState`; no existing in-repo precedent (this is the first `@FocusState` use — note it as a new pattern worth a `docs/solutions/` entry on completion).

**Test scenarios:**
- Test expectation: none — focus-on-open is a runtime/AppKit behavior with no unit-test harness in this project. Covered by the manual verification checklist (U5/Verification) instead.

**Verification:**
- Opening Search (sidebar → Search) places the caret in the field; typing appears without a click.
- Returning to Search after leaving and coming back re-focuses the field.
- VoiceOver announces the focused element as the labeled search field.

---

### U4. Keyboard navigation through results + Return-to-open

**Goal:** From the results list, arrow keys move a visible selection through results and Return opens the focused result in the Review window at its moment — without breaking the field's existing Return-to-search.

**Requirements:** R5

**Dependencies:** U2 (combined row label makes the selected row announce coherently), U3 (field focus is the entry point of the keyboard flow)

**Files:**
- Modify: `macos/ScreenCap/Views/Search/SearchView.swift` (`resultsList`: add selection state + binding, tag rows, wire Return-to-open)
- Test: `macos/ScreenCapTests/SearchKeyboardNavTests.swift` (pure selection→action resolution only)

**Approach:**
- Introduce `@State private var selectedResultID: SearchResultItem.ID?` and convert the results `List` to `List(selection: $selectedResultID)`, tagging each row with `.tag(item.id)` so macOS 13 gives arrow-key navigation for free. The current per-row `Button` may need to become a selectable row (a Button inside a selectable List can fight selection) — reconcile during implementation.
- Resolve Return-to-open via the macOS 13 toolkit (deferred mechanism — see Open Questions). Extract a pure `func reviewTarget(for id: SearchResultItem.ID?, in results: SearchResults) -> SearchResultItem?` so the "which item does Return act on" logic is unit-testable even though the key event itself is not.
- Preserve click-to-open (existing `openReview`) and the field's `.onSubmit` search. Ensure Return semantics are disambiguated by focus location (field vs. list); if a window-scoped `.keyboardShortcut(.defaultAction)` collides with field submit, gate it on `selectedResultID != nil` / list focus.

**Execution note:** Verify keyboard behavior on a real macOS 13 target (not just 14+). Because `.onKeyPress` is unavailable, expect to iterate on the Return mechanism against observed List behavior — implement the smallest thing that works and confirm by ear/keyboard before adding machinery.

**Technical design:** *(directional guidance for the focus/Return model, not implementation spec)*

| Focus location | Up/Down arrow | Return |
|---|---|---|
| Search field | move caret in field | run search (`.onSubmit`, existing) |
| Results list | move selection between rows | open selected result in Review |

The two Return meanings never apply at once because focus is in exactly one place; the implementation risk is a *window-scoped* shortcut leaking across that boundary — that's the thing to validate.

**Patterns to follow:**
- `MainWindow.sidebar` (`List(selection: $section)`) is the only in-app `List(selection:)` precedent — but it drives selection through `NavigationLink(value:)` rows, not the `.tag(item.id)` model this unit needs (there is no existing `.tag()` usage anywhere in the app). It demonstrates the selection-binding shape, not the row-selection mechanics; expect to build those without a direct in-repo template.
- `openReview(_:)` already encapsulates the open-at-moment action (sets `ReviewWindowOpener.shared.pendingSeekMs` then `openWindow`); Return reuses it, it is not reimplemented.

**Test scenarios:**
- Happy path: `reviewTarget(for: someID, in: results)` returns the matching `SearchResultItem` (so Return opens the right recording/anchor).
- Edge case: `reviewTarget(for: nil, in: results)` → nil (Return with no selection is a no-op, not a crash / not opening row 0).
- Edge case: `reviewTarget` for an ID not present in `results.items` (stale selection after a re-search) → nil.
- Integration (manual): arrow keys move the visible selection; Return opens Review at the selected moment; the search field's Return still runs a search, not an open.

**Verification:**
- With only the keyboard: focus field → type → Return runs search → Tab/arrow into results → arrows move selection → Return opens the focused result in Review at its timestamp.
- Mouse click-to-open still works; no double-fire (search + open) from a single Return.

---

### U6. Accessibility for non-result states + search-outcome announcements

**Goal:** Make the surface's non-result states (searching, empty / "nothing recorded", idle, daemon-down) read as coherent single messages, and announce search-outcome changes to VoiceOver so a user who can't see the screen learns the result of a search instead of hearing silence.

**Requirements:** R7

**Dependencies:** U1 (shares `SearchAccessibility.swift`)

**Files:**
- Modify: `macos/ScreenCap/Views/Search/SearchView.swift` (`.searching` `ProgressView` label; combine `emptyRow` and `stateMessage` into single accessibility elements; fire an announcement on `model.phase` change)
- Modify: `macos/ScreenCap/Views/Search/SearchAccessibility.swift` (pure `searchOutcomeAnnouncement(for:)` builder mapping a phase/results to the spoken string)
- Test: `macos/ScreenCapTests/SearchAccessibilityTests.swift`

**Approach:**
- **Searching label (D5):** give the `.searching` `ProgressView` a label — `ProgressView("Searching…")` or `.accessibilityLabel("Searching your history")` — so VoiceOver announces the searching state instead of a bare "progress indicator".
- **Combined state messages (D3):** apply `.accessibilityElement(children: .ignore)` + one `.accessibilityLabel` to `emptyRow` (both the "No matches…" and "Nothing recorded then…" variants) and to `stateMessage` (idle and daemon-down), composing headline + detail into one sentence. Today each is a `VStack` of `Text` that VoiceOver reads as disconnected fragments.
- **Outcome announcement (D1 — the headline gap):** on `model.phase` transition to `.loaded`/`.daemonDown`, post an announcement (e.g. "12 results", "No matches", "ScreenCap isn't running"). Use the macOS-13-available `NSAccessibility.post(element:notification:)` with an announcement (or the SwiftUI `AccessibilityNotification.Announcement` equivalent) via `.onChange(of: model.phase)` in `SearchView.body`. Build the spoken string from a pure `searchOutcomeAnnouncement(for:)` helper so it is unit-testable; the posting itself is the thin untestable shell.

**Execution note:** Confirm the announcement API used is available and actually speaks on macOS 13 (announcement posting has had quirks across releases) — verify by ear with VoiceOver, not just by compiling.

**Patterns to follow:**
- Same pure-helper-then-apply shape as U1/U2; `ScreenCapApp.swift` decorative-hidden + composite-label precedent.

**Test scenarios:**
- Happy path: `searchOutcomeAnnouncement(for: .loaded(results-with-12))` → "12 results" (and "1 result" singular).
- Edge case: `.loaded` with zero items → "No matches" (and the authoritative-empty variant → "Nothing recorded then" if that distinction is surfaced).
- Edge case: `.daemonDown` → "ScreenCap isn't running".
- Edge case: combined empty-state label for both `isAuthoritativeEmpty` true ("Nothing recorded then. No activity was recorded in that time range.") and false ("No matches. Try different words…").
- Manual: with VoiceOver on, running a search announces the outcome; the searching, empty, idle, and daemon-down states each read as one sentence.

**Verification:**
- A VoiceOver user who runs a search hears the outcome announced (count / no matches / not running) without sighted inspection.
- Idle, searching, empty, and daemon-down states each read as a single coherent message, not fragmented `Text` runs.

---

### U5. Dynamic Type / larger-text verification and row hardening

**Goal:** Confirm result rows survive larger accessibility text sizes; harden the row layout only if the check shows breakage. Capture the end-to-end VoiceOver + keyboard manual acceptance.

**Requirements:** R6, R8

**Dependencies:** U2, U4, U6 (verify the final rows, keyboard behavior, and non-result states together)

**Files:**
- Modify (conditional): `macos/ScreenCap/Views/Search/SearchTimelineView.swift` (`ResultRow` layout — only if verification finds clipping/overlap)
- Test: none (layout-under-Dynamic-Type has no unit-test seam) — manual verification + a recorded checklist.

**Approach:**
- With the system "larger text" / accessibility text size raised, exercise the rows across stream types and long snippets/app names. The suspect is the fixed `HStack` with a `56×32` thumbnail + `Spacer(minLength: 8)` + a trailing time column — at large sizes the snippet (`lineLimit(2)`) and the trailing time can collide or clip.
- If it breaks, harden minimally: allow the snippet/time to wrap or stack (`ViewThatFits` or a vertical fallback), and/or scale the thumbnail with `@ScaledMetric`. Prefer the smallest change that keeps app/snippet, time, and stream all legible.
- Record a short manual acceptance checklist in the PR description as the standing proof for R8, since there is no automated UI test. The checklist must cover: VoiceOver labels for chips/banner/rows; decorative icons silent; field focus on open; searching/empty/idle/daemon-down states each read as one message; search-outcome announced on completion; arrow + Return navigation; Dynamic Type intact. Note which macOS version it was run on.

**Patterns to follow:**
- Existing `ResultRow` layout; keep the visual design, change only what's needed for resilience.

**Test scenarios:**
- Test expectation: none (layout/Dynamic-Type behavior is not unit-testable here). Covered by the manual checklist below.
- Manual: at the largest accessibility text size, each stream's row shows app/snippet, time, and stream without clipping or overlap; short and long snippets both lay out.
- Manual (R8 end-to-end): the full VoiceOver + keyboard flow from U1–U4 and U6 holds together in one pass.

**Verification:**
- Rows remain legible and non-overlapping at large text sizes.
- A completed manual checklist demonstrates the surface is fully operable with VoiceOver + keyboard (R8).

---

## System-Wide Impact

- **Interaction graph:** Touches only `SearchView`, `ResultRow`/`SearchTimelineView`, and a new `SearchAccessibility` helper. `openReview` and `ReviewWindowOpener` are reused unchanged (Return calls the same path as click). The only new runtime side effect is a VoiceOver announcement posted on `model.phase` change (U6) — observable only to assistive tech, no visual/behavioral change. No daemon, model, or privacy-pipeline code is touched.
- **State lifecycle:** New `@State` (`selectedResultID`) and `@FocusState` (`searchFieldFocused`) are view-local and reset with each fresh `SearchView()` per section open — no persistence, no cross-window state. The phase-change announcement (U6) keys off `model.phase` via `.onChange`; ensure it fires once per transition, not per re-render.
- **API surface parity:** None — internal SwiftUI view changes only; no CLI/daemon contract change.
- **Unchanged invariants:** SCR-174's pointer-only / no-egress posture is preserved and extended to the audio VoiceOver channel — labels read only what the row already shows. The thumbnail stays `.accessibilityHidden(true)`. The search query model, ranking, and coverage semantics are untouched.
- **Integration coverage:** The label helpers are unit-tested; the keyboard/focus/Dynamic-Type behaviors are covered by a manual checklist (no XCUITest harness in the project — see Risks).

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| macOS 13 lacks `.onKeyPress`/`.defaultFocus`, so the Return-to-open mechanism is uncertain. | Use `List(selection:)` (13-supported) for arrow nav; enumerate candidate Return mechanisms in U4 and validate against real macOS 13 behavior; commit to outcome (R5), defer mechanism. |
| Window-scoped Return could fire both the field's search and the list's open. | Disambiguate by focus location; gate any default-action shortcut on `selectedResultID != nil` / list focus; verify no double-fire. |
| A `Button`-based row fights `List` selection (selection vs. activation). | Reconcile row to a selectable form during U4; keep click-to-open working; the pure `reviewTarget` seam keeps the action logic test-covered regardless of the row mechanics chosen. |
| Dynamic Type breaks the fixed-size row. | U5 is verify-first; harden minimally (`ViewThatFits`/wrap/`@ScaledMetric`) only on observed breakage. |
| The VoiceOver announcement API (U6) may not reliably speak on macOS 13 (announcement posting has had cross-release quirks). | Build the spoken string in a unit-tested pure helper; verify the posting actually speaks by ear on macOS 13 (U6 execution note); if `AccessibilityNotification.Announcement` is flaky, fall back to `NSAccessibility.post`. |
| No UI-test harness → behavioral acceptance (R4/R5/R6/R7/R8) isn't automatable. | Cover label/announcement-string logic with pure-helper XCTest; record a manual VoiceOver + keyboard checklist in the PR as standing proof; this matches the repo's existing test posture (no ViewInspector/XCUITest). |

---

## Documentation / Operational Notes

- On completion, add a `docs/solutions/` entry capturing the macOS-13 keyboard-navigation pattern that worked (the Return-to-open mechanism) and the `@FocusState`-on-open pattern — both are first-in-repo and likely to recur as other surfaces (Recordings, Review) get the same treatment.
- The manual VoiceOver + keyboard checklist belongs in the PR description (and is the artifact for R8).

---

## Sources & References

- **Origin issue:** [SCR-183 — Accessibility for Search (VoiceOver labels + keyboard navigation)](https://linear.app/zk-email/issue/SCR-183/accessibility-for-search-voiceover-labels-keyboard-navigation)
- **Parent feature:** [SCR-174 — Ask-Your-History Search (in-app, v1)](https://linear.app/zk-email/issue/SCR-174/ask-your-history-search-in-app-v1), requirements at `docs/brainstorms/2026-06-24-ask-your-history-search-requirements.md`, plan at `docs/plans/2026-06-24-001-feat-ask-your-history-search-plan.md`
- **Related (recent search work):** `docs/plans/2026-06-26-002-feat-search-result-thumbnails-highlighting-plan.md` (SCR-177 — added the thumbnail/highlight to `ResultRow`)
- Code: `macos/ScreenCap/Views/Search/SearchView.swift`, `macos/ScreenCap/Views/Search/SearchTimelineView.swift`, `macos/ScreenCap/Views/Search/SearchViewModel.swift`, `macos/ScreenCap/Views/MainWindow.swift`, `macos/ScreenCap/ScreenCapApp.swift`
- Constraint: `macos/project.yml` (`deploymentTarget.macOS: "13.0"`)
- Test pattern: `macos/ScreenCapTests/PrivacyBadgeStyleTests.swift`, `macos/ScreenCapTests/SnippetHighlighterTests.swift`
