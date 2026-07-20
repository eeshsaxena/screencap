---
title: "feat: Search UX polish — live results, result count, truncation note, recent searches"
type: feat
status: completed
date: 2026-06-27
---

# feat: Search UX polish — live results, result count, truncation note, recent searches

## Summary

A single polish pass over the in-app Search surface (`SearchView` / `SearchViewModel`): results appear as you type (debounced, latest-query-wins), a result-count + "showing first 200" truncation header sits above the list, prior results stay on screen while a refresh runs instead of a bare spinner, and the idle state offers example-query and recent-search chips. All work is view-layer and view-model only — no daemon verbs, wire schema, or ranking changes.

---

## Problem Frame

SCR-174 (PR #283) shipped the Ask-Your-History Search v1. It works, but the interaction has a cluster of small rough edges tracked in [SCR-182](https://linear.app/zk-email/issue/SCR-182): you must press Return to search; there's no visible result count; each daemon stream is silently capped at 200 rows with no note; the idle state is a static placeholder; and refreshing wipes the list to a spinner. None individually warrants its own ticket — bundled, they raise the surface from "functional" to "polished".

---

## Requirements

- R1. Typing in the search field shows results without pressing Return (debounced live search).
- R2. A result-count header is visible above results — e.g. "142 results across 7 days".
- R3. When any stream hits the 200-row fetch cap, a "Showing first 200" truncation note is visible (surfacing the existing silent cap).
- R4. The idle state offers example queries (chips) and recent searches the user can tap to run.
- R5. While a refresh runs over an already-loaded query, prior results stay visible (no full-screen spinner swap); the first search from idle may still show the spinner.
- R6. Existing SCR-177 (thumbnails/highlighting) and SCR-183 (accessibility, keyboard nav) behavior is preserved — VoiceOver announcements, arrow-key selection, Return-to-open gating, and the consent banner all continue to work.

**Origin actors:** end user searching their local history (single actor).

---

## Scope Boundaries

- No server-side pagination, "load more" affordance, or raising the 200-row cap — the truncation note is informational only.
- No changes to search ranking, fuzzy/typo-tolerant matching, or the query parser.
- No new daemon verbs, wire-schema fields, or `DaemonClient` request/response changes (the limit is already a client-supplied parameter).
- SCR-177 thumbnails/highlighting and SCR-183 accessibility/keyboard-nav are touched only for parity, not re-designed.

---

## Context & Research

### Relevant Code and Patterns

- `macos/Screencap/Views/Search/SearchView.swift` — the SwiftUI surface. `runSearch()` (line ~358) is the only search trigger today, fired from `.onSubmit`. Phase-switch in `content` (line ~113) renders idle/searching/loaded/daemonDown. The top `Section` of `resultsList` (line ~142) already hosts the interpretation label, coverage row, and consent banner — the natural home for a count/truncation header.
- `macos/Screencap/Views/Search/SearchViewModel.swift` — `@MainActor` view model. `Phase` enum (line 15) is the view-state source of truth. `search(_:contentIndexEnabled:)` (line 47) fans out to three streams, each fetched with a hardcoded `limit: 200` (lines 164, 175, 187). The `inFlight` guard (line 48) currently **drops** any search started while one is running — this is the behavior that must change for latest-query-wins. `SearchResults` (line 271) is the view-facing result struct.
- `macos/Screencap/Views/Search/SearchAccessibility.swift` — pure, unit-testable VoiceOver-label builders (the established pattern for testable Search string logic). `searchOutcomeAnnouncement` (line 92) already speaks the result count; extend it for days/truncation parity.
- `macos/Screencap/Controllers/PermissionController.swift` — the repo's `UserDefaults`-injection pattern (`defaults: UserDefaults = .standard`, line ~326) for a persistence store that stays unit-testable against an isolated suite. Mirror this for recent searches.
- `macos/Screencap/Controllers/SearchService.swift` — protocol seam + `FakeSearchService` (in tests) that lets the view model be driven without a live socket.
- `macos/ScreencapTests/SearchViewModelTests.swift`, `SearchAccessibilityTests.swift`, `SearchKeyboardNavTests.swift` — existing test homes for this surface.

### Institutional Learnings

- `docs/solutions/integration-issues/review-data-nullable-timing-swift-consumer-2026-06-01.md` — Swift consumers of daemon data must tolerate nullable/late-arriving fields; relevant to the truncation heuristic, which infers from row count rather than a guaranteed wire field.
- No existing `docs/solutions/` entry covers SwiftUI debouncing or `UserDefaults`-backed view state — these patterns are introduced fresh here but follow established repo conventions (test seams, pure helpers).

### External References

- None needed. SwiftUI debounce-via-cancellable-`Task` and `UserDefaults` persistence are well-trodden, and the codebase already establishes the relevant patterns (test seams, `@MainActor` view model, pure string helpers).

---

## Key Technical Decisions

- **Debounce in the view via a single cancellable handle, latest-wins in the model.** The view debounces keystrokes (~300 ms) using **one** `searchTask` handle that owns both the delay and the search — a keystroke cancels it and starts a new one, so a superseded delay can never orphan-fire a second search. The model drops its `inFlight` drop-guard in favor of cooperative cancellation. Rationale: a debounce alone still races when a slow in-flight search overlaps a newer one; cancellation makes the newest query authoritative. `.onSubmit` (Return) and chip taps fire **immediately**, bypassing the delay but cancelling any pending debounce task first (same single-handle path). Keeping the debounce in the view keeps the model a pure async function tests can `await` deterministically.
- **Cancellation must cover every publish point and the correlation fan-out, not just the terminal publish.** `search()` has multiple post-`await` publish sites (the `.daemonDown` early return as well as the terminal `.loaded`) and a per-recording `timeline.query` fan-out inside `correlateTranscript` (one extra socket round-trip per recording with transcript hits). Guard `!Task.isCancelled` before **every** `phase = …` assignment, and add a cancellation check inside the `correlateTranscript` per-recording loop so a superseded live-search abandons its fan-out early instead of running 15+ wasted serialized UDS calls. Rationale: cancellation-before-publish prevents the wrong result from showing; cancellation-inside-the-loop prevents live typing from hammering the single daemon socket (the amplification is per-recording, not constant).
- **Truncation is a raw-fetch count-equals-cap heuristic, measured against upstream completeness.** The wire carries no `has_more`/total field, so truncation is inferred when a stream's **raw** fetch (before client-side time-filtering) returns exactly the requested limit. The `200` becomes a named constant (`streamFetchLimit`) referenced by the fetches and the heuristic. The raw count is captured **inside each per-stream `if case .ok` block** (the `filtered` arrays shadow the raw hits at the `SearchResults` construction site) and the heuristic is scoped to the **main** timeline fetch only — never the per-recording correlation `timeline.query` calls (which always request 200). Because truncation measures the raw upstream fetch (not the displayed, post-filter count), the note must **not** claim "Showing first 200" (which implies the visible list holds 200); it reads as a completeness cue, e.g. *"Some sources hit their limit — narrow your search to see more."* Accepts a rare false positive (exactly 200 real raw rows) — acceptable for an informational cue.
- **Loading polish via an `isSearching` flag scoped to the `.loaded` phase.** Add `@Published private(set) var isSearching` alongside `Phase`, set on entry and cleared via `defer { isSearching = false }` so every exit path (including the empty-query early return and the daemon-down path) clears it. When a search starts while phase is already `.loaded`, keep the loaded results and set `isSearching = true` rather than transitioning to `.searching`; the first search from `.idle` still shows the spinner. `isSearching` is only meaningful while `phase == .loaded` (an in-place refresh) — it is ignored in other phases. Rationale: a flag keeps the four-case phase exhaustiveness intact; `defer`-clear and the `.loaded`-only scope contain the two-variable-drift cost the flag introduces.
- **Result-count/truncation rendered through pure helpers on `SearchAccessibility`.** Add `resultCountLabel` and `truncationNote` as static methods on the existing `SearchAccessibility` enum (rather than a new `SearchSummary` file) — it is already the pure, no-SwiftUI, unit-testable string-helper home in this directory, and co-locating keeps the visible text and the spoken label sharing one source so they can't drift (the established `coverageText`/`coverageChipLabel` pattern).
- **Recent searches recorded only on an explicit commit, persisted in `UserDefaults`.** A small `RecentSearchesStore` (cap ~6, move-to-front dedupe) backed by `UserDefaults`, injectable for tests per the `PermissionController` pattern, held as `@StateObject`/`@State` (not a recomputed `let`) so it survives view-body rebuilds. **Recording happens only on an explicit commit — `.onSubmit` (Return) or a chip tap — never on a debounce-fired live run.** Rationale: with live search every keystroke-pause is a "real run", so recording on each would flood the cap-6 list with throwaway prefixes ("sales", "salesforce", "salesforce ref", …) that dedupe can't collapse. Live search updates results; committing a search records it. Example chips are a static curated list; persistence (not session-only) because "recent searches" implies survival across launches.

---

## Open Questions

### Resolved During Planning

- *How to detect truncation without a wire change?* — Raw-fetch count-equals-cap heuristic (before time-filtering), scoped to the main timeline fetch; `200` promoted to a named constant. No daemon change.
- *Truncation note wording / per-stream vs aggregate?* — Single aggregate completeness cue worded as upstream-fetch state (*"Some sources hit their limit — narrow your search to see more"*), not "Showing first 200" (which would falsely imply the visible list holds 200). Per-stream breakdown is not surfaced ("per source" leaks the internal three-stream architecture).
- *New phase case vs. flag for loading polish?* — Flag (`isSearching`), `.loaded`-scoped and `defer`-cleared, to preserve phase exhaustiveness.
- *When to record a recent search?* — Only on explicit commit (Return / chip tap), never on a debounce-fired live run, to avoid prefix pollution.
- *New `SearchSummary` file?* — No; fold the count/truncation helpers into the existing `SearchAccessibility` enum.
- *Persist recent searches?* — Yes, `UserDefaults`, capped + deduped.

### Deferred to Implementation

- Exact debounce interval (start at 300 ms; tune by feel on-device).
- Exact example-query strings and recent-search cap (curated during implementation; ~6 recents).
- Final truncation-cue copy (the resolved shape above is the starting wording; refine for tone during implementation).

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

**View-state matrix after the changes** (phase × `isSearching`):

| phase | isSearching | What the view shows |
|---|---|---|
| `.idle` | false | Example + recent-search chips (U4) |
| `.idle` | true | Spinner (first-ever search) |
| `.loaded(prior)` | true | Prior results + thin inline refresh indicator (U2) |
| `.loaded(results)` | false | Results + count/truncation header (U3) |
| `.daemonDown` | — | "Screencap isn't running" (unchanged) |

`isSearching` is only *meaningful* while `phase == .loaded` (an in-place refresh); it is set/cleared in all phases via `defer` but ignored for rendering outside `.loaded`.

**Search trigger flow (U1):**

```
keystroke → query changes
  → cancel the single held searchTask (delay + search are one handle)
  → wait ~300ms (cancellable)        [Return / chip tap skip the wait, still cancel-first]
  → model.search(): no inFlight drop
      → guard !Task.isCancelled before EVERY phase publish (.daemonDown and .loaded)
      → correlateTranscript: check cancellation inside the per-recording loop (abandon fan-out early)
      → stale superseded search discards its result, issues no further socket calls
  → record into RecentSearchesStore ONLY on commit (Return / chip tap), not on debounce runs
```

---

## Implementation Units

### U1. Live debounced search with latest-query-wins

**Goal:** Searching happens as the user types, debounced, with the newest query authoritative when searches overlap.

**Requirements:** R1, R6

**Dependencies:** None

**Files:**
- Modify: `macos/Screencap/Views/Search/SearchView.swift` (debounce on `query` change; keep `.onSubmit` as an immediate-fire path)
- Modify: `macos/Screencap/Views/Search/SearchViewModel.swift` (remove `inFlight` drop-guard; add cooperative cancellation before publishing)
- Test: `macos/ScreencapTests/SearchViewModelTests.swift`

**Approach:**
- View: on `query` change, cancel the **single** held `searchTask` and start a new one that delays ~300 ms (cancellably) then calls `runSearch()` — the delay and the search are the same handle, so a superseded delay can never orphan-fire. `.onSubmit` and (U4) chip taps call `runSearch()` immediately but still cancel any pending task first. Empty/whitespace-only query returns to `.idle` (mirrors the current `runSearch` guard).
- Model: delete the `guard !inFlight else { return }` early-return and the `inFlight`/`defer` bookkeeping. Add `guard !Task.isCancelled` before **every** post-`await` `phase = …` publish — both the `.daemonDown` early return and the terminal `.loaded` — so a superseded search never overwrites a newer one's results regardless of which exit it reaches.
- Model: add a cancellation check inside `correlateTranscript`'s per-recording loop (and after its `await`s) so a superseded live-search stops issuing per-recording `timeline.query` calls instead of running the whole fan-out to completion. (See System-Wide Impact → socket amplification.)

**Patterns to follow:** existing `searchTask` cancel-on-rerun in `runSearch()`; `SearchService` fake seam for tests.

**Test scenarios:**
- Happy path: `search("refund")` with all streams populated still publishes `.loaded` with the merged/ranked items (regression guard that removing `inFlight` didn't break the base path). Covers AE: typing shows results.
- Integration: a search whose task is cancelled before completion (fake service suspends, task cancelled) does **not** publish a stale `.loaded` — the prior phase is retained. Verifies latest-wins at the terminal publish.
- Integration: a cancelled search whose timeline call would map to `.down` does **not** publish `.daemonDown` over a newer search's results. Verifies the guard covers the early-return publish.
- Integration: a cancelled search stops issuing `correlateTranscript` per-recording `timeline.query` calls (assert call count on the fake), not merely that it skips publishing.
- Edge case: empty / whitespace-only query resets phase to `.idle` and runs no fetch.
- Regression: any existing test asserting the old "concurrent search ignored" drop-guard behavior is updated to the latest-wins contract.

**Verification:** Typing a query (without Return) produces results on-device; rapidly editing then pausing shows results for the final query, never a stale earlier one; pressing Return immediately after typing does not fire a duplicate search ~300 ms later.

---

### U2. Loading polish — keep prior results visible while refreshing

**Goal:** A refresh over an already-loaded query keeps the list on screen with a subtle refresh indicator instead of clearing to a spinner.

**Requirements:** R5, R6

**Dependencies:** U1 at integration (the in-place refresh is only *visible* on-device once live re-search lands). The model-level `isSearching` behavior and its tests can be authored independently — the existing `FakeSearchService` exercises a second `search()` over `.loaded` without any view-level debounce.

**Files:**
- Modify: `macos/Screencap/Views/Search/SearchViewModel.swift` (add `@Published private(set) var isSearching`; set on entry + `defer { isSearching = false }`; only transition to `.searching` when phase is not already `.loaded`)
- Modify: `macos/Screencap/Views/Search/SearchView.swift` (render the inline refresh indicator when `isSearching && phase == .loaded`; reset `selectedResultID` when a new `.loaded` publishes; keep the full-screen spinner only for the idle→first-search case)
- Test: `macos/ScreencapTests/SearchViewModelTests.swift`

**Approach:**
- On entering `search`: set `isSearching = true` and `defer { isSearching = false }` (mirrors the old `inFlight`/`defer` pattern so **every** exit — empty-query early return, daemon-down, terminal load — clears it). If current phase is `.loaded`, leave it untouched (results stay visible); otherwise set `.searching`.
- View: render a small `ProgressView` spinner **trailing the result-count label** (U3) in the header row — not a separate row — gated on `isSearching` while results are shown; prior result rows stay at full opacity. The existing `.searching` full-screen `ProgressView` now only renders for the first search from idle.
- Selection on replacement: when a new `.loaded` result set publishes, reset `selectedResultID` to `nil` (the previously-selected row may no longer exist; SCR-183's `returnKeyHandler` already no-ops on a stale id, so this keeps Return-to-open safe rather than acting on an arbitrary row).

**Patterns to follow:** existing `@Published private(set) var phase` exposure; the existing `inFlight`/`defer` clear pattern; header `Section` placement used by the coverage row.

**Test scenarios:**
- Happy path: a second `search` invoked while phase is `.loaded(prior)` keeps phase `.loaded(prior)` and sets `isSearching = true` until completion, then publishes the new `.loaded`.
- Edge case: the first `search` from `.idle` transitions to `.searching` (spinner path preserved).
- Edge case: `isSearching` returns to `false` after a successful load, a daemon-down outcome, **and** an empty-query early return (the `defer` covers all three).

**Verification:** Editing a query that already has results keeps the old results visible with an inline spinner beside the count, not a blank spinner screen; the very first search still shows the centered spinner; a keyboard selection does not silently act on a stale row after the result set changes.

---

### U3. Result-count and truncation header

**Goal:** Show "N results across M days" above results, and a "Showing first 200" note when any stream hit the fetch cap.

**Requirements:** R2, R3, R6

**Dependencies:** None for the helpers/heuristic; the announcement-gating change coordinates with U2's `isSearching`. Sequenced after U1/U2 for a clean diff.

**Files:**
- Modify: `macos/Screencap/Views/Search/SearchAccessibility.swift` (add pure `resultCountLabel` / `truncationNote` static methods — same file as the other Search string helpers, no new file; extend/gate `searchOutcomeAnnouncement`)
- Modify: `macos/Screencap/Views/Search/SearchViewModel.swift` (promote `200` to a `streamFetchLimit` constant; add a `truncated: Bool` flag to `SearchResults`, defaulted so existing call sites still construct it; compute it from raw per-stream fetch counts)
- Modify: `macos/Screencap/Views/Search/SearchView.swift` (render the count label + truncation cue in the header `Section`; count on its own line, truncation cue on the line immediately below in secondary style, above the coverage row)
- Test: `macos/ScreencapTests/SearchAccessibilityTests.swift` (count/truncation helpers + announcement), `macos/ScreencapTests/SearchViewModelTests.swift` (truncation flag)

**Approach:**
- Compute truncation **inside each per-stream `if case .ok` block**, where the raw pre-filter counts live (`rows` for the main timeline fetch, `payload.hits` for content, `hits` for transcript) — capture e.g. `let timelineTruncated = rows.count == streamFetchLimit` per block, then OR them when constructing `SearchResults`. Scope it to the **main** timeline fetch only; the per-recording correlation `timeline.query` calls (which always request 200) must not feed the heuristic, or every time-only query would falsely show truncated.
- `resultCountLabel(results)` → "142 results across 7 days" from `results.items.count` + the distinct-day count of anchored items; pluralize "result"/"day"; the empty state still owns the zero-results copy, so this is for the non-empty case.
- `truncationNote(results)` → the completeness cue worded against the **raw upstream fetch**, e.g. *"Some sources hit their limit — narrow your search to see more"* — never "Showing first 200" (the visible list rarely holds 200 after merge/filter).
- Visible header text and the VoiceOver announcement both consume these helpers (the `coverageText`/`coverageChipLabel` shared-phrase pattern).
- **Gate the announcement on the refresh state:** during an in-place refresh (U2: `phase == .loaded(prior)` while `isSearching`), `searchOutcomeAnnouncement` must stay silent rather than re-speaking the prior query's count — extend its input (or the `.onChange` call site) to suppress while `isSearching`.

**Patterns to follow:** `SearchAccessibility` pure-helper + shared-phrase pattern; `searchResultsGroupedByDay` already derives day grouping (reuse it for the day count).

**Test scenarios:**
- Happy path: `resultCountLabel` for 142 items across 7 distinct days → "142 results across 7 days".
- Edge case: 1 result on 1 day → "1 result across 1 day" (singular both).
- Edge case: results with only unanchored (audio-approximate) items → count present, day clause omitted (no "across 0 days").
- Happy path: a **main-stream** raw fetch of exactly `streamFetchLimit` rows sets `SearchResults.truncated`; fewer does not; a per-recording correlation fetch of 200 does **not** set it.
- Integration: `searchOutcomeAnnouncement` for a truncated, multi-day completed load speaks the count and the truncation cue; during an in-place refresh it stays silent. Covers R3/R6 parity.

**Verification:** A query with many hits shows the count header; a stream hitting its raw cap shows the completeness cue both visually and to VoiceOver; a refresh does not re-announce the stale count.

---

### U4. Idle state — example queries and recent searches

**Goal:** Replace the static idle placeholder with tappable example-query chips and recent searches that run on tap.

**Requirements:** R4, R6

**Dependencies:** U1 (chip tap reuses the immediate-fire `runSearch()` path and the commit-only recording rule)

**Files:**
- Create: `macos/Screencap/Controllers/RecentSearchesStore.swift` (`UserDefaults`-backed, capped, move-to-front dedupe, injectable defaults)
- Modify: `macos/Screencap/Views/Search/SearchView.swift` (hold the store as `@StateObject`/`@State`; idle `content` branch renders chips; record on commit only; chip tap sets `query` and fires immediately)
- Test: `macos/ScreencapTests/RecentSearchesStoreTests.swift` (new)

**Approach:**
- `RecentSearchesStore`: `record(_ query:)` trims, de-dupes (move existing to front), caps at ~6, persists to an injected `UserDefaults`; `recent` returns the list. Constructor takes `defaults: UserDefaults = .standard` (mirrors `PermissionController`); held in the view as `@StateObject`/`@State` so it survives body rebuilds (`MainWindow.detail` builds a fresh `SearchView` per window open, but the data lives in `UserDefaults` regardless).
- Idle-state IA: under the existing "Ask your history / Searches only what's on this Mac" framing, show the curated **example** chips first (unlabeled). When the store is non-empty, a "Recent" section header precedes the recent chips; **when the store is empty, show only the example chips** (no "Recent" header, no placeholder). Example chips are never hidden when recents are present.
- Chip layout: wrap chips (flow-style) so a narrow window doesn't clip; truncate an over-long single chip with an ellipsis. Reuse the coverage-chip visual styling; render chips as plain tappable rows/labels (`.onTapGesture`), **not** `Button`s, so an idle chip never claims the window `.defaultAction` (the SCR-183 default-action hazard the row pattern already avoids).
- Chip tap: set `query` to the chip text and call `runSearch()` immediately (bypassing the debounce, like `.onSubmit`), and record it (a chip tap is a commit).
- Recording: record into the store **only on commit** — inside the `.onSubmit` path and on chip tap — never on a debounce-fired live run, for non-empty trimmed queries.

**Patterns to follow:** `PermissionController`'s injectable-`UserDefaults` test seam; coverage-chip styling; the SCR-183 non-Button-row default-action discipline.

**Test scenarios:**
- Happy path: `record("refund")` then `record("salesforce")` → `recent == ["salesforce", "refund"]` (most-recent first).
- Edge case: recording an existing query moves it to front without duplicating.
- Edge case: recording past the cap evicts the oldest entry.
- Edge case: empty/whitespace query is not recorded.
- Edge case: store reads back the persisted list from a fresh instance over the same injected `UserDefaults` suite (persistence across launches).
- Behavioral (view-level / manual checklist): debounce-fired live runs do **not** record (only Return / chip tap do), so the list never fills with typed prefixes; an idle chip does not steal Return from the focused field.

**Verification:** With no query, the idle state shows example chips (and a "Recent" group only once searches have been committed); tapping a chip runs that search immediately; typing "salesforce refund" and pausing does not leave "sales"/"salesforce ref" in recents; recents survive an app relaunch.

---

## System-Wide Impact

- **Interaction graph:** The debounce changes the search-trigger entry point but reuses the existing `searchTask`/`runSearch()`/`model.search` chain — `openInspect`, keyboard nav, and the consent flow are untouched. The consent banner's Return-gating (SCR-183 review #1) and arrow-key selection must still behave; the new header row carries no actionable control, and idle chips are non-Button rows, so neither introduces a competing window `.defaultAction`.
- **Socket amplification (new under live search):** a free-text search issues 3 initial fetches **plus one `timeline.query` per recording with transcript hits** (`correlateTranscript`). Under live typing, superseded searches must abandon this fan-out early (U1's in-loop cancellation) — otherwise rapid typing stacks overlapping searches each issuing 15+ serialized UDS calls on the single `api.sock`. Debounce bounds trigger *rate*; in-loop cancellation bounds overlap *depth*.
- **Error propagation:** `isSearching` clears via `defer` on every exit (loaded, empty, daemon-down) — a stuck `true` would leave a permanent refresh indicator.
- **State lifecycle risks:** Removing `inFlight` shifts overlap protection to task cancellation; the `Task.isCancelled` guard must sit before **every** publish, not only the terminal one, or a stale search could publish `.daemonDown`/`.loaded` over newer results. The `phase` + `isSearching` pair is a hand-maintained two-variable state (the cost of avoiding a `refreshing` enum case) — contained by `defer`-clear and the `.loaded`-only read scope. Keyboard `selectedResultID` is reset when a new `.loaded` publishes so Return-to-open never acts on a stale row. `RecentSearchesStore` writes to `UserDefaults` under an app-specific key to avoid collisions.
- **API surface parity:** None — no daemon/CLI surface changes. `streamFetchLimit` stays a client constant.
- **Integration coverage:** Latest-wins cancellation and the count/announcement parity are the cross-layer behaviors unit tests should prove; the debounce *timing* itself is verified manually on-device.
- **Unchanged invariants:** `SearchResults` gains a field but keeps its defaulted-init compatibility (existing test/older call sites must still construct it without supplying the new flag). The `Phase` enum keeps its four cases.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Removing `inFlight` introduces a stale-result race | `Task.isCancelled` guard before **every** publish (`.daemonDown` + `.loaded`); explicit latest-wins unit tests for both paths |
| Live search hammers the single daemon socket via transcript correlation fan-out | In-loop cancellation in `correlateTranscript` so superseded searches abandon their per-recording calls; debounce bounds trigger rate |
| Recent searches polluted by debounced typed prefixes | Record only on explicit commit (Return / chip tap), never on debounce runs; test that live runs don't record |
| Truncation cue misleads (over/under-warns; "first 200" implies list holds 200) | Measure raw upstream fetch (not displayed count), main timeline fetch only; word it as a completeness cue, not "Showing first 200" |
| Debounce feels laggy or too eager | Interval is a single tunable constant; tune on-device; Return + chip taps fire immediately |
| `isSearching` left stuck `true` on an early-return path | `defer { isSearching = false }` covers all exits; test daemon-down, empty, and loaded paths |
| New header/chips steal the window default action from Return-to-open | Header carries no control; chips are non-Button rows; verify SCR-183 keyboard-nav tests still pass |
| Keyboard selection acts on a stale row after results change | Reset `selectedResultID` on each new `.loaded`; `returnKeyHandler` already no-ops on stale ids |

---

## Sources & References

- **Origin ticket:** [SCR-182 — Search UX polish](https://linear.app/zk-email/issue/SCR-182/search-ux-polish-live-results-result-count-truncation-note-recent)
- **Predecessor:** [SCR-174 — Ask-Your-History Search v1](https://linear.app/zk-email/issue/SCR-174/ask-your-history-search-in-app-v1) (PR #283); requirements at `docs/brainstorms/2026-06-24-ask-your-history-search-requirements.md`
- **Sibling work already merged:** SCR-177 (thumbnails/highlighting), SCR-183 (`docs/plans/2026-06-26-003-feat-scr-183-search-accessibility-plan.md`)
- Related code: `macos/Screencap/Views/Search/SearchView.swift`, `macos/Screencap/Views/Search/SearchViewModel.swift`, `macos/Screencap/Views/Search/SearchAccessibility.swift`, `macos/Screencap/Controllers/PermissionController.swift`
