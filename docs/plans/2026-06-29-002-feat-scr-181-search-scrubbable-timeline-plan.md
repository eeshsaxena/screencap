---
title: "feat: Search results — graphical scrubbable per-day timeline (SCR-181)"
type: feat
status: completed
date: 2026-06-29
origin: docs/brainstorms/2026-06-24-ask-your-history-search-requirements.md
---

# feat: Search results — graphical scrubbable per-day timeline (SCR-181)

## Summary

Close the R5 gap from Ask-Your-History Search v1 (SCR-174, PR #283): v1 shipped per-day `List` `Section`s, but R5 envisioned **positioned markers on a scrubbable per-day time axis**. This plan adds that graphical layer — a horizontal day-strip navigator plus a Canvas-drawn per-day marker axis — **pinned above the existing results List via `.safeAreaInset` (the proven-safe SCR-174 pin)**, leaving the section-list intact as the companion/fallback. Markers are colored by stream, cluster at density for a busy day, and reuse the already-wired `onOpen` → inspect-at-timestamp path so clicking a marker opens Review at the moment. The correctness-bearing geometry and clustering is a pure, table-tested core (mirroring `TimelinePaneScrub`); the new view is additive Swift only — no daemon, schema, or Python change.

---

## Problem Frame

The in-app Search surface works today, but its result presentation is the reliable-but-undifferentiated fallback the v1 plan explicitly settled for: a date-grouped `List`. The SCR-174 brainstorm's R5 and AE1 called for a *scrubbable per-day timeline* — results as markers on a time axis a person can navigate — as the differentiated "see your day at a glance, jump to the moment" experience. The ticket is the deferred visual upgrade (lower priority, picked up after thumbnails/backfill/parser/ranking, all now merged). (Full motivation in origin: docs/brainstorms/2026-06-24-ask-your-history-search-requirements.md.)

---

## Requirements

- R5. Results render as **markers on a scrubbable per-day timeline** with date navigation; selecting a marker opens the native inspect/Review window at that exact timestamp. *(origin R5 — the gap this plan closes)*
- R5a. Marker **density/clustering** is handled so a high-activity day stays legible and performant (the "crowded shelf" concern flagged in origin Outstanding Questions). *(SCR-181 "Do" + Acceptance)*
- R6. Each marker stays **recognizable/verifiable**: it carries the same app/window + timestamp + (for content/audio) snippet the row already shows — surfacing nothing the pointer-only list didn't. *(origin R6, preserved)*

**Origin actors:** A1 (Operator — non-technical user navigating their own history), A2 (Local retrieval layer — unchanged; this plan consumes already-fetched pointers)
**Origin flows:** F1 (Ask and jump to a moment — this plan upgrades the "results render as markers on a per-day timeline" step)
**Origin acceptance examples:** AE1 (covers R2, R5 — the timeline-rendering clause), AE2 (covers R3, R6 — selecting a result opens inspect at the moment, no generated prose)

---

## Scope Boundaries

- **No change to retrieval, ranking, parsing, coverage, consent, or backfill.** This is a presentation-layer addition over the existing `SearchResults` the view-model already produces; `SearchViewModel`, `SearchService`, `QueryParser`, `SearchRanking` are untouched.
- **No backend/daemon/schema/Python change.** Every datum the timeline needs (`anchorMs`, `stream`, `app`, `title`, `snippet`, `approximate`) is already on `SearchResultItem`.
- **The section-list is not removed.** It remains the companion/fallback exactly as today (origin's "reliable" tier); the timeline is additive on top.
- **No new persistent store / no new egress** (R8 preserved) — the timeline renders in-memory pointers only.
- **Unanchored audio hits** (no resolvable `anchorMs`) are **not** placed on the axis (they have no time) — they stay in the existing "Heard in audio (time approximate)" companion section, unchanged.

### Deferred to Follow-Up Work

- **Rename the misnamed `SearchTimelineView.swift`** (it holds `ResultRow` + day helpers, not a timeline) → e.g. `SearchResultRow.swift`. A pure clarity rename touching `project.yml`; deferred to avoid project-regen churn inside this feature. Tracked as a tidy, not a blocker.
- **Day-strip → auto-scroll the companion List to the matching day section.** A nice-to-have linkage; v1 of this timeline keeps the companion list scroll independent (day strip drives only the axis). Revisit after on-device feel.
- **Cross-day "zoomed out" multi-day ribbon.** Origin defers multi-day narrative; the day strip + single-day axis is the v1 navigator.

---

## Context & Research

### Relevant Code and Patterns

- **Results rendering (the seam to extend)** — `macos/Screencap/Views/Search/SearchResultsView.swift`: `resultsList(_:)` builds the `List(selection:)` with a leading coverage/consent/backfill `Section` then per-day `ForEach(days)` sections. The timeline pins above this List. `searchDetailLayout(bar:content:)` (same file) is the `.safeAreaInset(edge: .top)` composition that fixed SCR-174 — the timeline pin must use the **same mechanism**, not a VStack sibling.
- **Day grouping + labels (reuse as-is)** — `SearchResultsView.swift`: `searchResultsGroupedByDay(_:)` → `[(day, items)]` most-recent-first; `searchDayLabel(_:)` → "Today"/"Yesterday"/"EEE MMM d". The day strip and axis source their days/markers from these.
- **Canvas marker timeline (the pattern to mirror)** — `macos/Screencap/Views/Review/TimelinePane.swift`: `Canvas` drawing ticks, **batched into one `Path` per category** (the explicit 10k-event performance note), drag→seconds via the pure `TimelinePaneScrub` enum (`scrubSeconds(forX:width:duration:)` / `cursorX(forSeconds:…)`). This is the template for both the draw loop and the extract-pure-math discipline.
- **Open-at-moment (already wired — just call it)** — `SearchView.openInspect(_:)` → `InspectRouting.decide(recording:anchorMs:isStub:)` → `InspectWindowOpener.shared.pendingSeekMs[recording]` + `openWindow(id: InspectWindowID, value:)`. A marker tap routes the selected `SearchResultItem` straight into this existing `onOpen` callback.
- **Item model** — `SearchViewModel.swift`: `SearchResultItem { id, stream(.screen/.audio/.activity), recording, anchorMs: Int?, approximate, score, snippet, app, title }`; `SearchResultItem.streamTint`/`streamIcon`/`timeLabel`/`primaryText` extensions in `SearchResultsView.swift`.
- **Keyboard selection + Return (the seam to keep in sync)** — `SearchView.swift` owns `@State selectedResultID`; `SearchResultsView.returnKeyHandler` opens the selected item on Return (and deliberately stands down while the field is focused / consent banner / backfill is active — the SCR-183 "don't hijack the default action" discipline). Markers must drive this same `selection`, and must **not** be `Button`s (which would steal the window default action).
- **Pure a11y builders (extend)** — `macos/Screencap/Views/Search/SearchAccessibility.swift`: `resultRowLabel`, `coverageChipLabel`, etc. — SwiftUI-free, unit-tested. Marker/cluster VoiceOver labels belong here as new pure builders.
- **View-hosting test infra (reuse)** — `macos/ScreencapTests/SearchViewHostingHarness.swift` (`SearchViewHost.host` offscreen NSHostingView + `detailColumn` reproducing the NavigationSplitView detail; `SearchFixtures`) and `macos/ScreencapTests/SearchViewLayoutTests.swift` (the SCR-174 starvation guard).

### Institutional Learnings

- **List-as-root sizing inside NavigationSplitView detail** — `docs/solutions/ui-bugs/swiftui-windowgroup-vs-window-singleton-scene-2026-05-18.md` (and the SCR-184 layout guard). A List nested below a sibling in a VStack inside the detail starves to zero height. **The timeline must pin via `.safeAreaInset(edge: .top)` on the List (proven safe — the search field already pins this way), never as a preceding VStack sibling.** This is the single biggest correctness risk and the reason U3 extends the regression guard before integrating.
- **Review-data timing fields are nullable** — `docs/solutions/integration-issues/review-data-nullable-timing-swift-consumer-2026-06-01.md`. The deep-link seek already guards null `startedAt`; this plan inherits that path unchanged via `openInspect` (no new conversion).
- **XcodeGen stale project** — `docs/solutions/build-errors/xcodegen-stale-project-missing-new-sources.md`. New Swift files → `cd macos && xcodegen generate` before trusting any "cannot find … in scope" error.
- **No timer fork-bombs** — the timeline is pure render of already-loaded results; it adds no polling/observer loop.

### External References

- None. Strong local patterns (`TimelinePane` Canvas + `TimelinePaneScrub`, `SearchViewHost`, `SearchAccessibility`); no high-risk external domain. (Decision in Key Technical Decisions.)

---

## Key Technical Decisions

- **Pin the timeline via `.safeAreaInset(edge: .top)` on the results List — additive, List stays the root.** This reuses the exact mechanism `searchDetailLayout` uses for the search field, which is the proven fix for the SCR-174 height-starvation bug. A VStack sibling above the List would reintroduce the bug. *(learning: List-as-root starvation)*
- **The existing per-day section List is preserved unchanged as the companion/fallback.** SCR-181 says "with the section-list as a fallback/companion"; keeping it untouched also keeps the SCR-184 regression guard valid and de-risks the change. The day strip drives only the new axis in v1 (auto-scroll linkage is deferred).
- **Day strip + single-day axis; default to the most-recent day with a hit.** Matches origin's resolved open question ("A day strip; default to the most-recent day containing a hit"). Days are sourced from `searchResultsGroupedByDay` so the strip and the companion sections agree.
- **Correctness-bearing geometry + clustering is a pure, file-scope core (`SearchTimelineLayout`), unit-tested without a render** — mirroring `TimelinePaneScrub`/`SearchRanking`/`SnippetHighlighter`. The view is a thin Canvas/overlay over it.
- **Markers drawn with `Canvas`, batched by stream** (mirror `TimelinePane`'s one-`Path`-per-category batching) so a busy day stays performant; per-stream fetch is already capped at 200 (`SearchViewModel.streamFetchLimit`), bounding per-day marker counts.
- **Clustering at density: markers within a pixel threshold collapse into one cluster glyph showing a count.** Tapping a single marker opens it; tapping a cluster reveals its members (selection drill-in / inline disclosure) rather than opening an arbitrary one. Clustering is pure math in `SearchTimelineLayout` so it is testable and the threshold is tunable in one place.
- **Markers are not `Button`s.** A marker tap sets the shared `selectedResultID` and routes via the existing `onOpen`; it must not register a `.defaultAction`, preserving the SCR-183 Return-ownership discipline. Selection is bidirectional with the companion list (selecting a marker highlights the row and vice versa).
- **No external research; no backend change.** Every datum exists on `SearchResultItem`; the work is a presentation layer with strong local precedent.

---

## Open Questions

### Resolved During Planning

- *How to add the timeline without re-triggering SCR-174 starvation?* — `.safeAreaInset(edge: .top)` on the List (proven-safe pin), never a VStack sibling. Extend the layout regression guard before integrating (U3).
- *Replace or augment the section-list?* — Augment. The section-list stays as the companion/fallback (origin wording + lowest risk).
- *Where does clicking a marker go?* — The already-wired `onOpen`/`openInspect` path (inspect-at-timestamp); no new navigation code.
- *Unanchored audio hits on the axis?* — No (they have no time); they remain in the existing "Heard in audio" companion section.
- *Which day shows by default?* — Most-recent day containing a hit (origin-resolved).

### Deferred to Implementation

- **Axis time bounds per day** — full 00:00–24:00 vs bounding to the day's active span (first→last hit). Active-span bounding spreads markers on a sparse day but makes axes inconsistent across days; pick during implementation against real recordings and pin in `SearchTimelineLayout` tests. Start simple (full local day) unless markers feel cramped.
- **Cluster pixel threshold + cluster disclosure UX** — exact px radius and whether a cluster tap shows a popover, an inline expanded mini-list, or cycles selection. Tune on-device; the math seam makes the threshold a one-line change.
- **Day-strip styling / overflow** — many days (e.g. "last month") may overflow the strip; reuse `FlowLayout` wrap or a horizontal scroll. Decide when the parser commonly yields multi-week windows.
- **Marker hit-target size vs visual tick width** — a 1–2px tick needs a wider invisible hit region for mouse + keyboard; size during manual QA.

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```
SearchResults (already loaded, ranked, time-filtered)
   │  anchored items only (anchorMs != nil)
   ▼
searchResultsGroupedByDay  ──▶  [ (day, [item]) ]  most-recent first
   │
   ▼
[day strip]  ◀── selectedDay (default: first/most-recent group)
   │  items for selectedDay
   ▼
SearchTimelineLayout (PURE):
   placeMarkers(items, dayBounds, width) ─▶ [Marker{ x, item }]
   cluster(markers, threshold)           ─▶ [Node = .single(item) | .cluster(items, x, count)]
   nodeAt(x)                             ─▶ tapped Node (inverse map, for tap/scrub)
   │
   ▼
SearchDayTimeline (VIEW): Canvas ticks (batched per stream tint) + hour gridlines
   + selection/hover highlight + cluster count glyphs
   │  tap single → onOpen(item) ; tap cluster → disclose/select ; sets selectedResultID
   ▼
pinned ABOVE the results List via `.safeAreaInset(edge: .top)`
   (List stays root — SCR-174 starvation guard preserved)
   │
   ▼  (unchanged) companion per-day section List below + "Heard in audio" section
```

---

## Implementation Units

### U1. Pure timeline geometry + clustering core

**Goal:** A SwiftUI-free, file-scope core that maps a day's anchored items to marker X-positions, clusters dense markers, and inverts a tapped/scrubbed X back to a node — the correctness heart, fully unit-tested.

**Requirements:** R5, R5a

**Dependencies:** None

**Files:**
- Create: `macos/Screencap/Views/Search/SearchTimelineLayout.swift` (pure enum/struct: `placeMarkers`, `cluster`, `nodeAt`, day-bounds helper; a `TimelineNode` value type = `.single(SearchResultItem)` / `.cluster(items:count:x:)`)
- Test: `macos/ScreencapTests/SearchTimelineLayoutTests.swift`

**Approach:**
- Mirror `TimelinePaneScrub`: guard non-positive width / zero-span (no NaN, no divide-by-zero), clamp ratios to `[0,1]`.
- `placeMarkers(items:dayStartMs:dayEndMs:width:)` → each anchored item to `x = ratio(anchorMs in [start,end]) * width`. Items outside bounds are clamped or dropped per the resolved day-bounds choice.
- `cluster(markers:thresholdPx:)` → left-to-right sweep collapsing markers within `thresholdPx` into a `.cluster` carrying members + count + representative x; lone markers stay `.single`.
- `nodeAt(x:in:)` → nearest node to a tapped x (inverse of placement) for tap/keyboard targeting.
- Inject the day bounds + threshold as parameters (no globals) so tests are deterministic.

**Execution note:** Build this core test-first — it is pure and is where placement/clustering correctness lives.

**Patterns to follow:** `TimelinePaneScrub` (pure coordinate math + guards), `SearchRanking` (pure, file-scope, tunable in one place).

**Test scenarios:**
- Covers AE1. Happy path: three items across a day → three single markers at expected X ratios for a fixed width/bounds.
- Edge: two items within `thresholdPx` → one `.cluster(count: 2)`; a third far away stays `.single`.
- Edge: all items at the same minute → one cluster with the full count, X at that minute.
- Edge: empty day → no nodes (no crash).
- Edge: single item → one `.single`, centered/placed per its time.
- Edge: width ≤ 0 or zero-span day → no markers, no NaN (mirror `TimelinePaneScrub` guards).
- Edge: `nodeAt(x)` returns the nearest node; a tap between two clusters resolves to the closer one; tap on empty axis with no nodes → nil.
- Edge (R5a): a 200-marker day clusters into a bounded, legible node count (assert node count ≪ marker count at a realistic threshold).

**Verification:** table-driven tests assert marker X, cluster membership, and inverse hit-testing for fixed inputs; core is pure/deterministic.

---

### U2. Day-strip + scrubbable Canvas marker axis view

**Goal:** The graphical timeline view — a day-strip navigator and a Canvas-drawn per-day marker axis (stream-tinted ticks, hour gridlines, cluster glyphs, selection/hover highlight) over the U1 core.

**Requirements:** R5, R5a, R6

**Dependencies:** U1

**Files:**
- Create: `macos/Screencap/Views/Search/SearchDayTimeline.swift` (the day strip + `Canvas` axis + cluster disclosure; takes the day groups, a `selectedDay` binding, the shared `selectedResultID` binding, and an `onOpen` callback)
- Modify: `macos/project.yml` only if needed, then `cd macos && xcodegen generate`
- Test: covered indirectly via U1 (math) and U3 (host render); SwiftUI view body itself is manual-QA per repo norm (`openWindow`/Canvas not unit-testable)

**Approach:**
- Day strip: chips for each day in `searchResultsGroupedByDay` order (labels via `searchDayLabel`), most-recent selected by default; tapping a chip sets `selectedDay`. Reuse `FlowLayout` (or a horizontal scroll) for overflow; chips are tappable non-`Button` elements (SCR-183 discipline) like the idle-state `queryChip`.
- Axis: `GeometryReader` + `Canvas` drawing markers **batched into one `Path` per stream** (`streamTint`: screen=blue, audio=purple, activity=green), hour gridlines/labels, and cluster glyphs (a filled dot with a small count badge). Mirror `TimelinePane`'s batched-path draw for density.
- Selection: a tap maps via `SearchTimelineLayout.nodeAt`; `.single` → set `selectedResultID` + `onOpen(item)`; `.cluster` → disclose members (inline mini-list or popover) and let selection pick one. Highlight the marker matching the current `selectedResultID`.
- Approximate (transcript) markers — note: unanchored ones never reach here (no time); anchored-but-approximate ones (resolved chunk-start) render with a subtle distinct treatment matching the row's "≈ audio" cue.

**Patterns to follow:** `TimelinePane` (Canvas + batched paths + `GeometryReader`), `queryChip`/`FlowLayout` (tappable non-Button chips, wrap), `streamTint`/`streamIcon` extensions.

**Test scenarios:**
- Test expectation: view-body rendering is exercised through U1 (placement/cluster math) and U3 (hosted render + non-blank); SwiftUI `Canvas` + `openWindow` are not unit-testable here — covered by the U3 host assertions and a manual QA checklist.
- Manual QA: day strip lists days with hits, defaults to most-recent; markers land at plausible clock positions; a dense day clusters; tapping a single marker opens inspect at the moment; tapping a cluster reveals members; the selected marker is visibly highlighted.

**Verification:** the timeline region renders with markers for a multi-marker day; day chips switch the visible day; project builds after `xcodegen generate`.

---

### U3. Integrate the timeline as a pinned top inset; preserve the section-list

**Goal:** Surface the timeline above the results List using the proven-safe `.safeAreaInset` pin, keep the existing per-day section list as the companion, and extend the layout regression guard so the pin can't reintroduce SCR-174 starvation.

**Requirements:** R5, R6

**Dependencies:** U2

**Files:**
- Modify: `macos/Screencap/Views/Search/SearchResultsView.swift` (in the `.loaded` path, attach `SearchDayTimeline` via `.safeAreaInset(edge: .top)` on the results `List`; thread `selection` and `onOpen` through; show the timeline only when there is ≥1 anchored item)
- Modify: `macos/Screencap/Views/Search/SearchView.swift` only if the `selectedDay` state must live alongside `selectedResultID` (default-most-recent + reset on new phase, mirroring the existing `selectedResultID` reset in `.onChange(of: model.phase)`)
- Test: `macos/ScreencapTests/SearchViewLayoutTests.swift` (extend with timeline-present cases)

**Approach:**
- Render `SearchDayTimeline` as `.safeAreaInset(edge: .top)` on the existing `List` — the same inset mechanism `searchDetailLayout` uses for the search field. The List remains the detail root; the companion per-day sections and the "Heard in audio" section are unchanged.
- Gate the inset on anchored results existing: empty / authoritative-empty / no-matches / unanchored-only states show no axis (nothing to place), exactly as today.
- `selectedDay` defaults to the first (most-recent) group and resets when a new result set loads.

**Execution note:** Extend the layout regression guard first (a timeline-present fixture asserting the List still renders ≥ items rows and content is non-blank), then wire the inset — the pin is the risk this unit exists to contain.

**Patterns to follow:** `searchDetailLayout` (`.safeAreaInset(edge:.top)` pin), `SearchViewLayoutTests` + `SearchViewHost.detailColumn` (host the real composition in a NavigationSplitView detail), the `.onChange(of: model.phase)` selection-reset in `SearchView`.

**Test scenarios:**
- Happy path (extend `SearchViewLayoutTests`): with `multiDayResults`, the timeline region renders (non-blank) **and** the companion List still reports ≥ `items.count` rows — the pin did not starve the List (the SCR-174 regression class).
- Edge: `authoritativeEmptyResults` / `noMatchesResults` → no timeline region; the empty-state copy still renders (no regression to existing empty states).
- Edge: `unanchoredOnlyResults` → no axis (no anchored items); the "Heard in audio" section still renders its rows.
- Edge: a new phase load resets `selectedDay` to the most-recent group (no stale day from a prior search).

**Verification:** the extended layout tests pass (timeline present + List non-starved); existing `SearchViewLayoutTests` still pass; the section-list behavior is unchanged in non-timeline states.

---

### U4. Marker accessibility + keyboard parity

**Goal:** VoiceOver labels for markers and clusters, and keyboard selection parity so a marker selection drives the existing Return-to-open path without hijacking the default action.

**Requirements:** R5, R6

**Dependencies:** U2, U3

**Files:**
- Modify: `macos/Screencap/Views/Search/SearchAccessibility.swift` (pure builders: `markerLabel(_:)` and `clusterLabel(items:)` — e.g. "Safari at 15:04, on screen" / "5 results between 15:00 and 15:10")
- Modify: `macos/Screencap/Views/Search/SearchDayTimeline.swift` (apply the labels; ensure markers/clusters are accessibility elements with `.isButton` trait but are NOT SwiftUI `Button`s; keep selection in sync with `selectedResultID`)
- Test: `macos/ScreencapTests/SearchAccessibilityTests.swift` (label builders); `macos/ScreencapTests/SearchKeyboardNavTests.swift` (selection→Return-open parity, if the existing seam is unit-reachable)

**Approach:**
- Add pure label builders mirroring `resultRowLabel`: a marker reads its `primaryText`/app + `timeLabel` + stream phrasing + an "approximate, from audio" suffix when applicable; a cluster reads its count + time span, never enumerating raw content (pointer-only posture preserved, R6/R8).
- Markers/clusters carry `.accessibilityElement(children: .ignore)` + `.accessibilityLabel(...)` + `.accessibilityAddTraits(.isButton)` and a tap gesture — the `queryChip`/`ResultRow` discipline — so VoiceOver announces one stop and no element steals the window default action from `returnKeyHandler`.
- Selecting a marker sets `selectedResultID` so the existing Return-to-open handler acts on it (and the companion row highlights); this keeps one selection model across both views.

**Patterns to follow:** `SearchAccessibility.resultRowLabel`/`coverageChipLabel` (pure, tested), `queryChip` (non-Button tappable with `.isButton` trait), `returnKeyHandler` + `selectedResultID` (SCR-183 keyboard discipline).

**Test scenarios:**
- Happy path: `markerLabel` for an activity marker → "Safari. at 15:04" shape; for a screen marker → snippet + "On screen" + time; pure/deterministic.
- Edge: `clusterLabel` for N members → "N results between HH:mm and HH:mm" (span from earliest to latest member); single-member edge reads as one result.
- Edge: an approximate (audio-anchored) marker appends the "approximate, from audio" cue, matching `resultRowLabel`.
- Edge/integration: selecting a marker sets `selectedResultID` such that the Return handler opens that item (parity with arrow-key list selection); a marker does not register `.defaultAction`.

**Verification:** `SearchAccessibility` label tests pass; manual QA confirms VoiceOver announces markers/clusters as single stops and Return opens the marker-selected result; markers don't steal Return from the search field or banners.

---

## System-Wide Impact

- **Interaction graph:** the timeline reads already-loaded `SearchResults` and routes marker taps through the existing `onOpen`/`openInspect` → `InspectWindowOpener` path. No new daemon calls, no capture/recording path touched.
- **Error propagation:** none new — the timeline renders in-memory pointers; the dangling-pointer (evicted recording) and stub-recording cases are already handled in `openInspect`/`InspectRouting` and inherited unchanged.
- **State lifecycle risks:** `selectedDay` must reset on a new result set (mirror the `selectedResultID` reset) so a stale day from a prior search can't show an empty axis; selection stays single-sourced (`selectedResultID`) across timeline and list.
- **Layout risk (primary):** pinning the timeline must use `.safeAreaInset` (not a VStack sibling) or it reintroduces SCR-174 detail starvation — U3 extends the regression guard specifically to catch this.
- **API surface parity:** none — no new backend or model surface; `SearchResultItem` is consumed as-is.
- **Unchanged invariants:** retrieval/ranking/parsing/coverage/consent/backfill, the three daemon verbs, the content index, the capture/redaction pipeline, the inspect window's recording-name identity, and the existing per-day section List + "Heard in audio" section are all unchanged. The timeline is additive, read-only, pointer-only — it widens nothing about what was captured or what leaves the device.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Pinning the timeline re-triggers the SCR-174 detail height-starvation (blank results pane) | Pin via `.safeAreaInset(edge:.top)` (proven-safe, same as the search field); U3 extends `SearchViewLayoutTests` with a timeline-present case asserting the List still renders rows **before** wiring the inset |
| A high-activity day produces an illegible wall of overlapping ticks | Clustering in the pure `SearchTimelineLayout` core (tunable threshold) + Canvas batched-per-stream draw (the `TimelinePane` 10k-event pattern); per-stream 200 cap already bounds counts |
| Markers (as Buttons) hijack the window default action and break Return-to-open | Markers are non-Button tappable elements driving `selectedResultID`; the existing `returnKeyHandler` keeps Return ownership (SCR-183 discipline) |
| New Swift files invisible to the build | `cd macos && xcodegen generate` after adding files (documented learning) |
| Transcript (approximate) markers imply false moment-precision on the axis | Anchored at resolved chunk-start (already), rendered with the distinct "≈ audio" treatment + a11y cue; unanchored audio never placed on the axis |

---

## Sources & References

- **Origin document:** [docs/brainstorms/2026-06-24-ask-your-history-search-requirements.md](docs/brainstorms/2026-06-24-ask-your-history-search-requirements.md) (R5, AE1, F1; "Per-day timeline rendering performance" Outstanding Question)
- Ticket: [SCR-181](https://linear.app/zk-email/issue/SCR-181/search-results-graphical-scrubbable-per-day-timeline); v1: [SCR-174](https://linear.app/zk-email/issue/SCR-174/ask-your-history-search-in-app-v1) (PR #283)
- v1 plan: `docs/plans/2026-06-24-002-feat-ask-your-history-search-plan.md`
- Seam to extend: `macos/Screencap/Views/Search/SearchResultsView.swift`, `SearchView.swift`, `SearchAccessibility.swift`
- Pattern to mirror: `macos/Screencap/Views/Review/TimelinePane.swift` (Canvas + `TimelinePaneScrub`)
- Open-at-moment path: `macos/Screencap/Controllers/InspectRouting.swift`, `State/InspectWindowOpener.swift`
- Test infra: `macos/ScreencapTests/SearchViewHostingHarness.swift`, `SearchViewLayoutTests.swift`
